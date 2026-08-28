from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from plugin.server.application.package_management.coordinator import (
    InstallationCoordinator,
    LocalImportRequest,
    LocalReplacementRequest,
    MarketFreshInstallRequest,
    MarketReplacementInstallRequest,
    MarketReplacementPlanChangedError,
    MarketReplacementProfilePathError,
    MarketReplacementTransactionError,
    MarketReplacementTransactionRequest,
    install_result_profile_dirs,
    install_result_target_dirs,
    single_install_target,
)
from plugin.server.application.package_management.replacement import (
    ReplacePluginError,
    ReplacePluginResult,
)

pytestmark = pytest.mark.plugin_unit


class _FakeLocalImportPort:
    def __init__(self, tmp_path: Path) -> None:
        self.saved_path = tmp_path / "managed" / "demo.neko-plugin"
        self.target_dir = tmp_path / "plugins" / "demo"
        self.profile_dir = tmp_path / "profiles" / "demo"
        self.events: list[object] = []
        self.warning: str | None = None
        self.fail_at: str | None = None
        self.cleanup_calls: list[
            tuple[dict[str, object] | None, list[Path], list[Path]]
        ] = []

    async def save_uploaded_bytes(
        self,
        *,
        filename: str,
        content: bytes,
    ) -> dict[str, object]:
        self.events.append(("save", filename, content))
        return self._saved(filename)

    async def copy_package_file(
        self,
        *,
        filename: str,
        package_path: str,
    ) -> dict[str, object]:
        self.events.append(("copy", filename, package_path))
        return self._saved(filename)

    async def sha256_file(self, *, path: Path) -> str:
        self.events.append(("hash", path))
        if self.fail_at == "hash":
            raise OSError("hash failed")
        return "a" * 64

    async def install_package(
        self,
        *,
        package: str,
        profiles_root: str | None,
        allow_external_profiles_root: bool,
        on_conflict: str,
        use_staging: bool,
        forced_directory_name: str | None,
    ) -> dict[str, object]:
        self.events.append(
            (
                "install",
                package,
                profiles_root,
                allow_external_profiles_root,
                on_conflict,
                use_staging,
                forced_directory_name,
            )
        )
        if self.fail_at == "install":
            raise RuntimeError("install failed")
        return {
            "installed_plugins": [{"target_dir": str(self.target_dir)}],
            "profile_dir": str(self.profile_dir),
            "profile_reused": False,
        }

    async def record_import_source(
        self,
        *,
        install_result: dict[str, object],
        package_filename: str,
        package_sha256: str,
    ) -> str | None:
        self.events.append(
            ("record", install_result, package_filename, package_sha256)
        )
        if self.fail_at == "record":
            raise RuntimeError("record failed")
        return self.warning

    async def cleanup_failure(
        self,
        *,
        saved: dict[str, object] | None,
        target_dirs: list[Path],
        profile_dirs: list[Path],
    ) -> None:
        self.events.append("cleanup")
        self.cleanup_calls.append((saved, list(target_dirs), list(profile_dirs)))

    def _saved(self, filename: str) -> dict[str, object]:
        return {
            "name": filename,
            "path": str(self.saved_path),
            "size": 123,
        }


class _FakeMarketInstallPort(_FakeLocalImportPort):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.plugin_id = "demo"
        self.activate_result = True
        self.market_warnings = ["registry warning"]
        self.recorded_market_detail: dict[str, object] | None = None

    async def install_package(
        self,
        *,
        package: str,
        profiles_root: str | None,
        allow_external_profiles_root: bool,
        on_conflict: str,
        use_staging: bool,
        forced_directory_name: str | None,
    ) -> dict[str, object]:
        result = await super().install_package(
            package=package,
            profiles_root=profiles_root,
            allow_external_profiles_root=allow_external_profiles_root,
            on_conflict=on_conflict,
            use_staging=use_staging,
            forced_directory_name=forced_directory_name,
        )
        result.update(
            {
                "package_id": "package-demo",
                "payload_hash": "c" * 64,
            }
        )
        return result

    async def read_installed_plugin_id(self, *, target_dir: Path) -> str:
        self.events.append(("read-id", target_dir))
        return self.plugin_id

    async def record_imported_fallback(
        self,
        *,
        target_dir: Path,
        package_filename: str,
        package_sha256: str,
        package_id: str,
        profile_dir: str,
    ) -> dict[str, object]:
        self.events.append(
            (
                "record-imported",
                target_dir,
                package_filename,
                package_sha256,
                package_id,
                profile_dir,
            )
        )
        return {
            "channel": "imported",
            "directory_name": target_dir.name,
            "plugin_id": self.plugin_id,
            "package_filename": package_filename,
            "package_sha256": package_sha256,
        }

    async def record_market_install(
        self,
        *,
        target_dir: Path,
        plugin_id: str,
        market_detail: dict[str, object],
        package_id: str,
        profile_dir: str,
    ) -> tuple[dict[str, object], list[str]]:
        self.events.append(("record-market", target_dir, plugin_id, package_id, profile_dir))
        self.recorded_market_detail = dict(market_detail)
        if self.fail_at == "record-market":
            raise RuntimeError("market record failed")
        return (
            {
                "channel": "market",
                "directory_name": target_dir.name,
                "plugin_id": plugin_id,
                "version": market_detail["version"],
                "package_sha256": market_detail["package_sha256"],
                "payload_hash": market_detail.get("payload_hash"),
                "published_at": market_detail.get("published_at", ""),
                "previous_version": None,
            },
            list(self.market_warnings),
        )

    async def record_market_upgrade(
        self,
        *,
        target_dir: Path,
        plugin_id: str,
        market_detail: dict[str, object],
        package_id: str,
        profile_dir: str,
    ) -> tuple[dict[str, object], list[str]]:
        self.events.append(
            ("record-upgrade", target_dir, plugin_id, package_id, profile_dir)
        )
        self.recorded_market_detail = dict(market_detail)
        if self.fail_at == "record-upgrade":
            raise RuntimeError("market upgrade record failed")
        return (
            {
                "channel": "market",
                "directory_name": target_dir.name,
                "plugin_id": plugin_id,
                "version": market_detail["version"],
                "package_sha256": market_detail["package_sha256"],
                "payload_hash": market_detail.get("payload_hash"),
                "published_at": market_detail.get("published_at", ""),
                "previous_version": "1.0.0",
            },
            list(self.market_warnings),
        )

    async def activate_fresh_candidate(
        self,
        *,
        plugin_id: str,
        target_dir: Path,
    ) -> bool:
        self.events.append(("activate", plugin_id, target_dir))
        if self.fail_at == "activate":
            raise RuntimeError("activation failed")
        return self.activate_result


class _FakeLocalReplacementPort:
    def __init__(self, tmp_path: Path) -> None:
        self.events: list[str] = []
        self.installed_plugin_id = "demo"
        self.backup_is_valid = True
        self.backup_dir = tmp_path / "backup"

    async def deploy_local_replacement(
        self,
        request: LocalReplacementRequest,
    ) -> dict[str, object]:
        self.events.append("deploy")
        return {"installed_plugins": [], "request_package": request.package}

    async def read_installed_plugin_id(self, *, target_dir: Path) -> str:
        self.events.append(f"read:{target_dir.name}")
        return self.installed_plugin_id

    async def validate_manifestless_backup(self, *, backup_dir: Path) -> bool:
        self.events.append(f"backup:{backup_dir.name}")
        return self.backup_is_valid

    async def replace_local_package(
        self,
        *,
        install_new,
        validate_new,
        validate_backup,
        **_kwargs: object,
    ) -> ReplacePluginResult:
        self.events.append("replace")
        if validate_backup is not None:
            await validate_backup(self.backup_dir)
        install_result = await install_new()
        await validate_new()
        return ReplacePluginResult(
            restarted=True,
            rollback_status="not_needed",
            install_result=install_result,
            backup_dir=self.backup_dir,
        )


class _FakeMarketReplacementTransactionPort:
    def __init__(self, tmp_path: Path) -> None:
        self.events: list[str] = []
        self.snapshot_matches_result = True
        self.installed_plugin_id = "demo"
        self.fail_before_deploy = False
        self.fail_after_deploy = False
        self.source_restore_result = True
        self.profile_error: Exception | None = None
        self.backup_dir = tmp_path / "backup"

    async def run_serialized(self, operation) -> ReplacePluginResult:
        self.events.append("lock")
        return await operation()

    async def snapshot_matches(self, **_kwargs: object) -> bool:
        self.events.append("snapshot")
        return self.snapshot_matches_result

    async def deploy_replacement(
        self,
        request: MarketReplacementInstallRequest,
    ) -> dict[str, object]:
        self.events.append(f"deploy:{request.mode}")
        return {"operation": request.mode}

    async def resolve_profile_dir(
        self,
        *,
        installed_package_id: str,
        default_profiles_root: Path,
        **_kwargs: object,
    ) -> Path:
        self.events.append("profile")
        if self.profile_error is not None:
            raise self.profile_error
        return default_profiles_root / installed_package_id

    async def read_installed_plugin_id(self, *, target_dir: Path) -> str:
        self.events.append(f"read:{target_dir.name}")
        return self.installed_plugin_id

    async def replace_plugin(
        self,
        *,
        install_new,
        validate_new,
        on_rollback_start,
        **_kwargs: object,
    ) -> ReplacePluginResult:
        self.events.append("replace")
        if self.fail_before_deploy:
            raise ReplacePluginError(
                stage="backup",
                rollback_status="completed",
                cause=RuntimeError("backup failed"),
            )
        try:
            install_result = await install_new()
            if self.fail_after_deploy:
                raise RuntimeError("restart failed")
            await validate_new()
        except Exception as exc:
            if on_rollback_start is not None:
                on_rollback_start()
            raise ReplacePluginError(
                stage="validate",
                rollback_status="completed",
                cause=exc,
            ) from exc
        return ReplacePluginResult(
            restarted=True,
            rollback_status="not_needed",
            install_result=install_result,
            backup_dir=self.backup_dir,
        )

    async def restore_install_source(self, *, original_entry: object) -> bool:
        self.events.append("restore-source")
        return self.source_restore_result


def test_install_result_parsing_is_owned_by_the_coordinator(tmp_path: Path) -> None:
    first_target = tmp_path / "plugins" / "demo"
    second_target = tmp_path / "plugins" / "helper"
    profile_dir = tmp_path / "profiles" / "package-demo"
    install_result: dict[str, object] = {
        "unpacked_plugins": [
            {
                "target_dir": str(first_target),
                "target_plugin_id": "demo",
            },
            {"target_dir": str(second_target)},
        ],
        "profile_dir": str(profile_dir),
        "profile_reused": False,
    }

    assert install_result_target_dirs(install_result) == [
        first_target,
        second_target,
    ]
    assert install_result_profile_dirs(install_result) == [profile_dir]
    with pytest.raises(ValueError, match="exactly one plugin"):
        single_install_target(install_result)

    install_result["profile_reused"] = True
    assert install_result_profile_dirs(install_result) == []


def test_single_install_target_accepts_legacy_installed_plugins(
    tmp_path: Path,
) -> None:
    target_dir = tmp_path / "plugins" / "demo"

    assert single_install_target(
        {
            "installed_plugins": [
                {
                    "target_dir": str(target_dir),
                    "target_plugin_id": "demo",
                }
            ]
        }
    ) == (target_dir, "demo")


@pytest.mark.asyncio
async def test_local_replacement_composes_manifestless_validation_and_identity(
    tmp_path: Path,
) -> None:
    port = _FakeLocalReplacementPort(tmp_path)
    target_dir = tmp_path / "plugins" / "demo"
    request = LocalReplacementRequest(
        plugin_id="demo",
        directory_name="demo",
        target_dir=target_dir,
        profile_dir=tmp_path / "profiles" / "package-demo",
        package=str(tmp_path / "packages" / "demo.neko-plugin"),
        plugins_root=str(target_dir.parent),
        profiles_root=str(tmp_path / "profiles"),
        manifestless_state=True,
    )

    result = await InstallationCoordinator(
        local_replacement=port
    ).replace_local(request)

    assert port.events == ["replace", "backup:backup", "deploy", "read:demo"]
    assert result.install_result["request_package"] == request.package
    assert result.restarted is True


@pytest.mark.asyncio
async def test_local_replacement_rejects_installed_identity_drift(
    tmp_path: Path,
) -> None:
    port = _FakeLocalReplacementPort(tmp_path)
    port.installed_plugin_id = "other"
    request = LocalReplacementRequest(
        plugin_id="demo",
        directory_name="demo",
        target_dir=tmp_path / "plugins" / "demo",
        profile_dir=tmp_path / "profiles" / "package-demo",
        package=str(tmp_path / "packages" / "demo.neko-plugin"),
        plugins_root=None,
        profiles_root=None,
        manifestless_state=False,
    )

    with pytest.raises(ValueError, match="identity does not match"):
        await InstallationCoordinator(
            local_replacement=port
        ).replace_local(request)

    assert port.events == ["replace", "deploy", "read:demo"]


@pytest.mark.asyncio
async def test_local_import_orders_operations_and_preserves_api_shape(
    tmp_path: Path,
) -> None:
    port = _FakeLocalImportPort(tmp_path)
    port.warning = "source record degraded"

    result = await InstallationCoordinator(port).install_local(
        LocalImportRequest(
            filename="demo.neko-plugin",
            content=b"package-bytes",
            profiles_root=str(tmp_path / "profiles"),
            allow_external_profiles_root=True,
            on_conflict="replace",
        )
    )

    assert [event[0] if isinstance(event, tuple) else event for event in port.events] == [
        "save",
        "hash",
        "install",
        "record",
    ]
    assert result.to_api_dict() == {
        "upload": {
            "name": "demo.neko-plugin",
            "path": str(port.saved_path),
            "size": 123,
        },
        "install": {
            "installed_plugins": [{"target_dir": str(port.target_dir)}],
            "profile_dir": str(port.profile_dir),
            "profile_reused": False,
        },
        "install_source_warning": "source record degraded",
    }
    assert port.cleanup_calls == []


@pytest.mark.asyncio
async def test_local_import_copies_an_existing_package_path(tmp_path: Path) -> None:
    port = _FakeLocalImportPort(tmp_path)

    await InstallationCoordinator(port).install_local(
        LocalImportRequest(
            filename="demo.neko-plugin",
            package_path=str(tmp_path / "download" / "demo.neko-plugin"),
        )
    )

    assert port.events[0] == (
        "copy",
        "demo.neko-plugin",
        str(tmp_path / "download" / "demo.neko-plugin"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "package_path"),
    [(None, None), (b"bytes", "demo.neko-plugin")],
)
async def test_local_import_requires_exactly_one_package_source(
    tmp_path: Path,
    content: bytes | None,
    package_path: str | None,
) -> None:
    port = _FakeLocalImportPort(tmp_path)

    with pytest.raises(ValueError, match="upload_and_install"):
        await InstallationCoordinator(port).install_local(
            LocalImportRequest(
                filename="demo.neko-plugin",
                content=content,
                package_path=package_path,
            )
        )

    assert port.events == []


@pytest.mark.asyncio
async def test_local_import_cleans_the_saved_artifact_when_hashing_fails(
    tmp_path: Path,
) -> None:
    port = _FakeLocalImportPort(tmp_path)
    port.fail_at = "hash"

    with pytest.raises(OSError, match="hash failed"):
        await InstallationCoordinator(port).install_local(
            LocalImportRequest(filename="demo.neko-plugin", content=b"bytes")
        )

    assert port.cleanup_calls == [(port._saved("demo.neko-plugin"), [], [])]


@pytest.mark.asyncio
async def test_local_import_rolls_back_installed_paths_when_recording_fails(
    tmp_path: Path,
) -> None:
    port = _FakeLocalImportPort(tmp_path)
    port.fail_at = "record"

    with pytest.raises(RuntimeError, match="record failed"):
        await InstallationCoordinator(port).install_local(
            LocalImportRequest(filename="demo.neko-plugin", content=b"bytes")
        )

    assert port.cleanup_calls == [
        (
            port._saved("demo.neko-plugin"),
            [port.target_dir],
            [port.profile_dir],
        )
    ]


@pytest.mark.asyncio
async def test_market_fresh_install_owns_identity_hash_record_and_activation_order(
    tmp_path: Path,
) -> None:
    port = _FakeMarketInstallPort(tmp_path)
    port.activate_result = False
    package_path = tmp_path / "download" / "demo.neko-plugin"

    result = await InstallationCoordinator(
        market_install=port
    ).install_market_fresh(
        MarketFreshInstallRequest(
            filename="demo.neko-plugin",
            package_path=str(package_path),
            forced_directory_name="market-demo",
            market_detail={
                "plugin_market_id": "market-demo",
                "version": "1.2.3",
                "package_url": "https://example.invalid/demo.neko-plugin",
                "package_sha256": "b" * 64,
                "payload_hash": "d" * 64,
                "published_at": "2026-08-27T00:00:00Z",
                "expected_plugin_toml_id": "declared-demo",
            },
        )
    )

    assert [event[0] if isinstance(event, tuple) else event for event in port.events] == [
        "copy",
        "hash",
        "install",
        "read-id",
        "record-market",
        "activate",
    ]
    install_event = port.events[2]
    assert isinstance(install_event, tuple)
    assert install_event[-2:] == (True, "market-demo")
    assert port.recorded_market_detail == {
        "plugin_market_id": "market-demo",
        "version": "1.2.3",
        "package_url": "https://example.invalid/demo.neko-plugin",
        "package_sha256": "a" * 64,
        "payload_hash": "c" * 64,
        "published_at": "2026-08-27T00:00:00Z",
    }
    payload = result.to_api_dict()
    assert payload["candidate_selection_required"] is True
    assert payload["install"] == {
        "channel": "market",
        "directory_name": "demo",
        "plugin_id": "demo",
        "version": "1.2.3",
        "package_sha256": "a" * 64,
        "payload_hash": "c" * 64,
        "published_at": "2026-08-27T00:00:00Z",
        "previous_version": None,
    }
    warning = payload["install_source_warning"]
    assert isinstance(warning, str)
    assert "plugin identity mismatch" in warning
    assert "package_sha256 mismatch" in warning
    assert "payload_hash mismatch" in warning
    assert warning.endswith("registry warning")
    assert port.cleanup_calls == []


@pytest.mark.asyncio
async def test_market_fresh_install_falls_back_to_imported_when_detail_is_incomplete(
    tmp_path: Path,
) -> None:
    port = _FakeMarketInstallPort(tmp_path)

    result = await InstallationCoordinator(
        market_install=port
    ).install_market_fresh(
        MarketFreshInstallRequest(
            filename="demo.neko-plugin",
            content=b"package-bytes",
            market_detail={"version": "1.2.3"},
        )
    )

    payload = result.to_api_dict()
    assert payload["install"] == {
        "channel": "imported",
        "directory_name": "demo",
        "plugin_id": "demo",
        "package_filename": "demo.neko-plugin",
        "package_sha256": "a" * 64,
    }
    assert "plugin_market_id, package_url" in str(payload["install_source_warning"])
    event_names = [event[0] if isinstance(event, tuple) else event for event in port.events]
    assert "record-imported" in event_names
    assert "record-market" not in event_names
    assert "candidate_selection_required" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", ["record-market", "activate"])
async def test_market_fresh_install_rolls_back_every_promoted_path_after_failure(
    tmp_path: Path,
    fail_at: str,
) -> None:
    port = _FakeMarketInstallPort(tmp_path)
    port.fail_at = fail_at

    with pytest.raises(RuntimeError):
        await InstallationCoordinator(
            market_install=port
        ).install_market_fresh(
            MarketFreshInstallRequest(
                filename="demo.neko-plugin",
                content=b"package-bytes",
                market_detail={
                    "plugin_market_id": "market-demo",
                    "version": "1.2.3",
                    "package_url": "https://example.invalid/demo.neko-plugin",
                },
            )
        )

    assert port.cleanup_calls == [
        (
            port._saved("demo.neko-plugin"),
            [port.target_dir],
            [port.profile_dir],
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["upgrade", "reinstall"])
async def test_market_replacement_deploys_and_records_through_the_shared_port(
    tmp_path: Path,
    mode: Literal["upgrade", "reinstall"],
) -> None:
    port = _FakeMarketInstallPort(tmp_path)

    result = await InstallationCoordinator(
        market_install=port
    ).install_market_replacement(
        MarketReplacementInstallRequest(
            filename="demo.neko-plugin",
            mode=mode,
            package_path=str(tmp_path / "download" / "demo.neko-plugin"),
            profiles_root=str(tmp_path / "profiles"),
            allow_external_profiles_root=True,
            forced_directory_name="market-demo",
            market_detail={
                "plugin_market_id": "market-demo",
                "version": "2.0.0",
                "package_url": "https://example.invalid/demo.neko-plugin",
                "package_sha256": "b" * 64,
                "payload_hash": "d" * 64,
                "published_at": "2026-08-27T00:00:00Z",
                "expected_plugin_toml_id": "demo",
            },
        )
    )

    assert [event[0] if isinstance(event, tuple) else event for event in port.events] == [
        "copy",
        "hash",
        "install",
        "read-id",
        "record-upgrade",
    ]
    install_event = port.events[2]
    assert isinstance(install_event, tuple)
    assert install_event[-2:] == (True, "market-demo")
    assert port.recorded_market_detail == {
        "plugin_market_id": "market-demo",
        "version": "2.0.0",
        "package_url": "https://example.invalid/demo.neko-plugin",
        "package_sha256": "a" * 64,
        "payload_hash": "c" * 64,
        "published_at": "2026-08-27T00:00:00Z",
    }
    payload = result.to_api_dict()
    assert payload["install"] == {
        "channel": "market",
        "directory_name": "demo",
        "plugin_id": "demo",
        "version": "2.0.0",
        "package_sha256": "a" * 64,
        "payload_hash": "c" * 64,
        "published_at": "2026-08-27T00:00:00Z",
        "previous_version": "1.0.0",
    }
    warning = payload["install_source_warning"]
    assert isinstance(warning, str)
    assert "package_sha256 mismatch" in warning
    assert "payload_hash mismatch" in warning
    assert warning.endswith("registry warning")
    assert "candidate_selection_required" not in payload
    assert port.cleanup_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("actual_plugin_id", ["unexpected", ""])
async def test_market_replacement_rejects_identity_change_and_rolls_back_deploy(
    tmp_path: Path,
    actual_plugin_id: str,
) -> None:
    port = _FakeMarketInstallPort(tmp_path)
    port.plugin_id = actual_plugin_id

    with pytest.raises(ValueError, match="plugin identity mismatch"):
        await InstallationCoordinator(
            market_install=port
        ).install_market_replacement(
            MarketReplacementInstallRequest(
                filename="demo.neko-plugin",
                mode="upgrade",
                content=b"package-bytes",
                forced_directory_name="market-demo",
                market_detail={
                    "plugin_market_id": "market-demo",
                    "version": "2.0.0",
                    "package_url": "https://example.invalid/demo.neko-plugin",
                    "expected_plugin_toml_id": "demo",
                },
            )
        )

    assert port.cleanup_calls == [
        (
            port._saved("demo.neko-plugin"),
            [port.target_dir],
            [port.profile_dir],
        )
    ]
    event_names = [event[0] if isinstance(event, tuple) else event for event in port.events]
    assert "record-upgrade" not in event_names


@pytest.mark.asyncio
async def test_market_replacement_record_failure_cleans_every_deployed_path(
    tmp_path: Path,
) -> None:
    port = _FakeMarketInstallPort(tmp_path)
    port.fail_at = "record-upgrade"

    with pytest.raises(RuntimeError, match="market upgrade record failed"):
        await InstallationCoordinator(
            market_install=port
        ).install_market_replacement(
            MarketReplacementInstallRequest(
                filename="demo.neko-plugin",
                mode="reinstall",
                content=b"package-bytes",
                forced_directory_name="market-demo",
                market_detail={
                    "plugin_market_id": "market-demo",
                    "version": "2.0.0",
                    "package_url": "https://example.invalid/demo.neko-plugin",
                    "expected_plugin_toml_id": "demo",
                },
            )
        )

    assert port.cleanup_calls == [
        (
            port._saved("demo.neko-plugin"),
            [port.target_dir],
            [port.profile_dir],
        )
    ]


def _replacement_transaction_request(
    tmp_path: Path,
) -> MarketReplacementTransactionRequest:
    return MarketReplacementTransactionRequest(
        expected_plugin_id="demo",
        installed_plugin_id="demo",
        installed_package_id="package-demo",
        target_dir=tmp_path / "plugins" / "demo",
        default_profiles_root=tmp_path / "profiles",
        original_entry=object(),
        original_entry_fingerprint=("demo", "1.0.0"),
        deployment=MarketReplacementInstallRequest(
            filename="demo.neko-plugin",
            mode="upgrade",
            content=b"package-bytes",
            market_detail={
                "plugin_market_id": "demo",
                "version": "2.0.0",
                "package_url": "https://example.invalid/demo.neko-plugin",
            },
        ),
    )


@pytest.mark.asyncio
async def test_market_replacement_transaction_orders_revalidation_and_replace(
    tmp_path: Path,
) -> None:
    port = _FakeMarketReplacementTransactionPort(tmp_path)

    result = await InstallationCoordinator(
        market_replacement=port
    ).replace_market(_replacement_transaction_request(tmp_path))

    assert port.events == [
        "lock",
        "snapshot",
        "profile",
        "replace",
        "deploy:upgrade",
        "read:demo",
    ]
    assert result.install_result == {"operation": "upgrade"}
    assert result.restarted is True


@pytest.mark.asyncio
async def test_market_replacement_transaction_rejects_stale_snapshot_before_mutation(
    tmp_path: Path,
) -> None:
    port = _FakeMarketReplacementTransactionPort(tmp_path)
    port.snapshot_matches_result = False

    with pytest.raises(MarketReplacementPlanChangedError):
        await InstallationCoordinator(
            market_replacement=port
        ).replace_market(_replacement_transaction_request(tmp_path))

    assert port.events == ["lock", "snapshot"]


@pytest.mark.asyncio
async def test_market_replacement_rejects_unsafe_profile_before_mutation(
    tmp_path: Path,
) -> None:
    port = _FakeMarketReplacementTransactionPort(tmp_path)
    port.profile_error = ValueError("unsafe profile")

    with pytest.raises(MarketReplacementProfilePathError, match="unsafe profile"):
        await InstallationCoordinator(
            market_replacement=port
        ).replace_market(_replacement_transaction_request(tmp_path))

    assert port.events == ["lock", "snapshot", "profile"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail_before_deploy", "expected_source_restore"),
    [(True, False), (False, True)],
)
async def test_market_replacement_transaction_restores_source_only_after_deploy(
    tmp_path: Path,
    fail_before_deploy: bool,
    expected_source_restore: bool,
) -> None:
    port = _FakeMarketReplacementTransactionPort(tmp_path)
    port.fail_before_deploy = fail_before_deploy
    port.fail_after_deploy = not fail_before_deploy

    with pytest.raises(MarketReplacementTransactionError) as exc_info:
        await InstallationCoordinator(
            market_replacement=port
        ).replace_market(_replacement_transaction_request(tmp_path))

    assert ("restore-source" in port.events) is expected_source_restore
    assert exc_info.value.source_restored is True
    assert exc_info.value.replacement_error.rollback_status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("actual_plugin_id", ["unexpected", ""])
async def test_market_replacement_transaction_identity_failure_restores_source(
    tmp_path: Path,
    actual_plugin_id: str,
) -> None:
    port = _FakeMarketReplacementTransactionPort(tmp_path)
    port.installed_plugin_id = actual_plugin_id

    with pytest.raises(MarketReplacementTransactionError) as exc_info:
        await InstallationCoordinator(
            market_replacement=port
        ).replace_market(_replacement_transaction_request(tmp_path))

    assert "restore-source" in port.events
    assert isinstance(exc_info.value.replacement_error.cause, ValueError)
    assert "replacement target" in str(exc_info.value.replacement_error.cause)
