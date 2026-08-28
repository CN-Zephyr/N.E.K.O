from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plugin.server.application.package_management.profile_cleanup import (
    PackageProfileService,
)
from plugin.server.application.package_management.profile_removal import (
    PackageProfileRemovalCoordinator,
    PackageProfileRemovalRejectedError,
    PackageProfileRemovalTransactionError,
)
from plugin.server.infrastructure.package_management.removal_runtime import (
    build_package_profile_removal_coordinator,
)


pytestmark = pytest.mark.plugin_unit


class _Registry:
    def __init__(
        self,
        *,
        profile_dir: Path,
        removed: bool = True,
        channel: str = "imported",
        profile_installed: bool | None = True,
        other_entries: tuple[object, ...] = (),
        commit_error: Exception | None = None,
    ) -> None:
        self.entry = SimpleNamespace(
            root_id="user",
            directory_name="demo",
            plugin_id="demo",
            package_id="demo_package",
            profile_dir=str(profile_dir),
            profile_installed=profile_installed,
            channel=channel,
            removed=removed,
        )
        self.other_entries = other_entries
        self.commit_error = commit_error
        self.commit_calls: list[Path] = []

    def entry_for_directory(
        self,
        _directory_path: Path,
        *,
        include_removed: bool = False,
    ) -> object | None:
        return self.entry if include_removed or not self.entry.removed else None

    def list_entries(self) -> list[object]:
        return list(self.other_entries)

    def mark_profile_removed(self, *, directory_path: Path) -> None:
        self.commit_calls.append(directory_path)
        assert Path(self.entry.profile_dir).exists() is False
        if self.commit_error is not None:
            raise self.commit_error
        self.entry.profile_dir = ""
        self.entry.profile_installed = False


def _profile(tmp_path: Path) -> tuple[Path, Path, Path]:
    candidate_dir = tmp_path / "plugins" / "demo"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / "demo_package"
    profile_dir.mkdir(parents=True)
    (profile_dir / "settings.toml").write_text(
        "value = true\n",
        encoding="utf-8",
    )
    return candidate_dir, profiles_root, profile_dir


def test_production_composition_builds_isolated_profile_removal_services() -> None:
    first = build_package_profile_removal_coordinator()
    second = build_package_profile_removal_coordinator()

    assert isinstance(first.profile_service, PackageProfileService)
    assert first is not second
    assert first.profile_service is not second.profile_service


@pytest.mark.asyncio
async def test_removes_retired_candidate_profile_after_registry_commit(
    tmp_path: Path,
) -> None:
    candidate_dir, profiles_root, profile_dir = _profile(tmp_path)
    registry = _Registry(profile_dir=profile_dir)
    coordinator = PackageProfileRemovalCoordinator(PackageProfileService())

    result = await coordinator.remove_profile(
        expected_plugin_id="demo",
        candidate_dir=candidate_dir,
        registry=registry,
        profiles_root=profiles_root,
    )

    assert result.deleted_profile_dir == profile_dir
    assert result.cleanup_deferred is False
    assert registry.commit_calls == [candidate_dir]
    assert registry.entry.profile_installed is False
    assert profile_dir.exists() is False
    assert list(profiles_root.glob(".demo_package.deleting-*")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("registry_kwargs", "reason"),
    [
        ({"removed": False}, "candidate_not_retired"),
        ({"channel": "manual"}, "candidate_not_package_managed"),
        ({"profile_installed": None}, "profile_not_package_owned"),
        ({"profile_installed": False}, "profile_not_package_owned"),
    ],
)
async def test_rejects_unretired_or_unproven_profile_ownership(
    tmp_path: Path,
    registry_kwargs: dict[str, object],
    reason: str,
) -> None:
    candidate_dir, profiles_root, profile_dir = _profile(tmp_path)
    registry = _Registry(profile_dir=profile_dir, **registry_kwargs)
    coordinator = PackageProfileRemovalCoordinator(PackageProfileService())

    with pytest.raises(PackageProfileRemovalRejectedError) as exc_info:
        await coordinator.remove_profile(
            expected_plugin_id="demo",
            candidate_dir=candidate_dir,
            registry=registry,
            profiles_root=profiles_root,
        )

    assert exc_info.value.reason == reason
    assert registry.commit_calls == []
    assert profile_dir.is_dir()


@pytest.mark.asyncio
async def test_rejects_removed_registry_row_while_candidate_code_is_present(
    tmp_path: Path,
) -> None:
    candidate_dir, profiles_root, profile_dir = _profile(tmp_path)
    candidate_dir.mkdir(parents=True)
    registry = _Registry(profile_dir=profile_dir)
    coordinator = PackageProfileRemovalCoordinator(PackageProfileService())

    with pytest.raises(PackageProfileRemovalRejectedError) as exc_info:
        await coordinator.remove_profile(
            expected_plugin_id="demo",
            candidate_dir=candidate_dir,
            registry=registry,
            profiles_root=profiles_root,
        )

    assert exc_info.value.reason == "candidate_code_present"
    assert registry.commit_calls == []
    assert profile_dir.is_dir()


@pytest.mark.asyncio
async def test_rejects_candidate_owned_by_another_logical_plugin(
    tmp_path: Path,
) -> None:
    candidate_dir, profiles_root, profile_dir = _profile(tmp_path)
    registry = _Registry(profile_dir=profile_dir)
    coordinator = PackageProfileRemovalCoordinator(PackageProfileService())

    with pytest.raises(PackageProfileRemovalRejectedError) as exc_info:
        await coordinator.remove_profile(
            expected_plugin_id="another-plugin",
            candidate_dir=candidate_dir,
            registry=registry,
            profiles_root=profiles_root,
        )

    assert exc_info.value.reason == "candidate_identity_mismatch"
    assert registry.commit_calls == []
    assert profile_dir.is_dir()


@pytest.mark.asyncio
async def test_shared_profile_is_rejected_without_registry_commit(
    tmp_path: Path,
) -> None:
    candidate_dir, profiles_root, profile_dir = _profile(tmp_path)
    sibling = SimpleNamespace(
        root_id="user",
        directory_name="sibling",
        plugin_id="sibling",
        package_id="demo_package",
        profile_dir=str(profile_dir),
        profile_installed=True,
        channel="market",
        removed=False,
    )
    registry = _Registry(profile_dir=profile_dir, other_entries=(sibling,))
    coordinator = PackageProfileRemovalCoordinator(PackageProfileService())

    with pytest.raises(PackageProfileRemovalRejectedError) as exc_info:
        await coordinator.remove_profile(
            expected_plugin_id="demo",
            candidate_dir=candidate_dir,
            registry=registry,
            profiles_root=profiles_root,
        )

    assert exc_info.value.reason == "profile_missing_shared_or_unsafe"
    assert registry.commit_calls == []
    assert profile_dir.is_dir()


@pytest.mark.asyncio
async def test_registry_commit_failure_restores_exact_profile(tmp_path: Path) -> None:
    candidate_dir, profiles_root, profile_dir = _profile(tmp_path)
    original = (profile_dir / "settings.toml").read_bytes()
    registry = _Registry(
        profile_dir=profile_dir,
        commit_error=PermissionError("registry busy"),
    )
    coordinator = PackageProfileRemovalCoordinator(PackageProfileService())

    with pytest.raises(PackageProfileRemovalTransactionError) as exc_info:
        await coordinator.remove_profile(
            expected_plugin_id="demo",
            candidate_dir=candidate_dir,
            registry=registry,
            profiles_root=profiles_root,
        )

    assert exc_info.value.stage == "commit"
    assert exc_info.value.rollback_status == "completed"
    assert (profile_dir / "settings.toml").read_bytes() == original
    assert list(profiles_root.glob(".demo_package.deleting-*")) == []


@pytest.mark.asyncio
async def test_registry_commit_failure_reports_incomplete_restore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    candidate_dir, profiles_root, profile_dir = _profile(tmp_path)
    registry = _Registry(
        profile_dir=profile_dir,
        commit_error=PermissionError("registry busy"),
    )
    profile_service = PackageProfileService()

    def fail_restore(_staged: object) -> None:
        raise PermissionError("profile busy")

    monkeypatch.setattr(profile_service, "restore", fail_restore)
    coordinator = PackageProfileRemovalCoordinator(profile_service)

    with pytest.raises(PackageProfileRemovalTransactionError) as exc_info:
        await coordinator.remove_profile(
            expected_plugin_id="demo",
            candidate_dir=candidate_dir,
            registry=registry,
            profiles_root=profiles_root,
        )

    assert exc_info.value.stage == "commit"
    assert exc_info.value.rollback_status == "incomplete"
    assert exc_info.value.rollback_errors == ("restore_profile:PermissionError",)
    assert profile_dir.exists() is False
    assert len(list(profiles_root.glob(".demo_package.deleting-*"))) == 1


@pytest.mark.asyncio
async def test_cleanup_failure_records_deferred_hidden_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    candidate_dir, profiles_root, profile_dir = _profile(tmp_path)
    registry = _Registry(profile_dir=profile_dir)
    profile_service = PackageProfileService()
    record_path = tmp_path / "package_profile_cleanup.json"

    def fail_finalize(_staged: object) -> None:
        raise PermissionError("profile busy")

    monkeypatch.setattr(profile_service, "finalize", fail_finalize)
    coordinator = PackageProfileRemovalCoordinator(profile_service)

    result = await coordinator.remove_profile(
        expected_plugin_id="demo",
        candidate_dir=candidate_dir,
        registry=registry,
        profiles_root=profiles_root,
        deferred_cleanup_record_path=lambda: record_path,
    )

    assert result.deleted_profile_dir == profile_dir
    assert result.cleanup_deferred is True
    assert result.cleanup_recorded is True
    assert result.staged_path is not None
    assert result.staged_path.is_dir()
    assert record_path.is_file()
    assert profile_dir.exists() is False
