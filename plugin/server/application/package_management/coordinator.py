"""Thin orchestration for runtime package mutations.

The current slices cover local package import plus fresh and replacement Market
package deployment and the outer replacement transaction. They own transaction
ordering while concrete package IO, provenance persistence, candidate
resolution, and runtime lifecycle remain behind injected ports. Market
transport and Registry cutover are not part of these slices.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol

from plugin.logging_config import get_logger

from .replacement import ReplacePluginError, ReplacePluginResult

logger = get_logger("server.application.package_management.coordinator")


def _validate_package_source(*, content: bytes | None, package_path: str | None) -> None:
    if content is None and package_path is None:
        raise ValueError("upload_and_install requires content or package_path")
    if content is not None and package_path is not None:
        raise ValueError("upload_and_install accepts content or package_path, not both")


def install_result_entries(
    install_result: Mapping[str, object],
) -> list[dict[str, object]]:
    """Return the package installer's normalized plugin entries."""

    installed_plugins = install_result.get("unpacked_plugins")
    if installed_plugins is None:
        installed_plugins = install_result.get("installed_plugins")
    if not isinstance(installed_plugins, list) or not installed_plugins:
        raise ValueError("install returned no plugins")

    entries: list[dict[str, object]] = []
    for item in installed_plugins:
        if not isinstance(item, dict):
            raise ValueError("unpack returned malformed unpacked_plugins entry")
        entries.append(item)
    return entries


def install_result_target_dirs(install_result: Mapping[str, object]) -> list[Path]:
    """Return every plugin directory created by one install operation."""

    target_dirs: list[Path] = []
    for entry in install_result_entries(install_result):
        target_dir = entry.get("target_dir")
        if isinstance(target_dir, str) and target_dir:
            target_dirs.append(Path(target_dir))
    return target_dirs


def install_result_profile_dirs(install_result: Mapping[str, object]) -> list[Path]:
    """Return newly promoted package-profile directories, excluding reused data."""

    if install_result.get("profile_reused") is True:
        return []
    profile_dir = install_result.get("profile_dir")
    if isinstance(profile_dir, str) and profile_dir:
        return [Path(profile_dir)]
    return []


def single_install_target(
    install_result: Mapping[str, object],
) -> tuple[Path, str]:
    """Return the sole Market plugin target and the installer's target id."""

    installed_plugins = install_result_entries(install_result)
    if len(installed_plugins) != 1:
        raise ValueError(
            "Market packages must contain exactly one plugin; "
            f"got {len(installed_plugins)}"
        )
    first = installed_plugins[0]
    target_dir = first.get("target_dir")
    if not isinstance(target_dir, str) or not target_dir:
        raise ValueError("unpack returned no target_dir for plugin")
    target_plugin_id = str(first.get("target_plugin_id", "")) or ""
    return Path(target_dir), target_plugin_id


@dataclass(frozen=True, slots=True)
class LocalImportRequest:
    """One local package import request."""

    filename: str
    content: bytes | None = None
    package_path: str | None = None
    profiles_root: str | None = None
    allow_external_profiles_root: bool = False
    on_conflict: str = "fail"

    def validate(self) -> None:
        _validate_package_source(content=self.content, package_path=self.package_path)


@dataclass(frozen=True, slots=True)
class MarketFreshInstallRequest:
    """One already-downloaded, fresh Market package installation."""

    filename: str
    market_detail: Mapping[str, object]
    content: bytes | None = None
    package_path: str | None = None
    profiles_root: str | None = None
    allow_external_profiles_root: bool = False
    on_conflict: str = "fail"
    forced_directory_name: str | None = None

    def validate(self) -> None:
        _validate_package_source(content=self.content, package_path=self.package_path)


@dataclass(frozen=True, slots=True)
class MarketReplacementInstallRequest:
    """One already-downloaded Market upgrade or reinstall deployment."""

    filename: str
    mode: Literal["upgrade", "reinstall"]
    market_detail: Mapping[str, object]
    content: bytes | None = None
    package_path: str | None = None
    profiles_root: str | None = None
    allow_external_profiles_root: bool = False
    on_conflict: str = "fail"
    forced_directory_name: str | None = None

    def validate(self) -> None:
        _validate_package_source(content=self.content, package_path=self.package_path)
        if self.mode not in {"upgrade", "reinstall"}:
            raise ValueError(f"unsupported Market replacement mode: {self.mode!r}")


@dataclass(frozen=True, slots=True)
class LocalImportResult:
    """Typed local-import result with a compatibility API projection."""

    upload: dict[str, object]
    install: dict[str, object]
    install_source_warning: str | None = None

    def to_api_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "upload": dict(self.upload),
            "install": dict(self.install),
        }
        if self.install_source_warning is not None:
            result["install_source_warning"] = self.install_source_warning
        return result


@dataclass(frozen=True, slots=True)
class LocalReplacementRequest:
    """One confirmed local upgrade, downgrade, or reinstall transaction."""

    plugin_id: str
    directory_name: str
    target_dir: Path
    profile_dir: Path
    package: str
    plugins_root: str | None
    profiles_root: str | None
    manifestless_state: bool
    use_staging: bool = True
    forced_directory_name: str | None = None
    allow_external_profiles_root: bool = False


@dataclass(frozen=True, slots=True)
class MarketFreshInstallResult:
    """Fresh Market result preserving the existing Plugin CLI response DTO."""

    upload: dict[str, object]
    unpack: dict[str, object]
    install: dict[str, object]
    warnings: tuple[str, ...] = ()
    candidate_selection_required: bool = False

    def to_api_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "upload": dict(self.upload),
            "unpack": dict(self.unpack),
            "install": dict(self.install),
        }
        if self.warnings:
            result["install_source_warning"] = "; ".join(self.warnings)
        if self.candidate_selection_required:
            result["candidate_selection_required"] = True
        return result


@dataclass(frozen=True, slots=True)
class MarketReplacementInstallResult:
    """Market replacement deployment preserving the Plugin CLI response DTO."""

    upload: dict[str, object]
    unpack: dict[str, object]
    install: dict[str, object]
    warnings: tuple[str, ...] = ()

    def to_api_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "upload": dict(self.upload),
            "unpack": dict(self.unpack),
            "install": dict(self.install),
        }
        if self.warnings:
            result["install_source_warning"] = "; ".join(self.warnings)
        return result


@dataclass(frozen=True, slots=True)
class MarketReplacementTransactionRequest:
    """One prepared Market replacement after transport verification."""

    expected_plugin_id: str
    installed_plugin_id: str
    installed_package_id: str
    target_dir: Path
    default_profiles_root: Path
    original_entry: object
    original_entry_fingerprint: tuple[object, ...]
    deployment: MarketReplacementInstallRequest
    on_rollback_start: Callable[[], None] | None = None


class MarketReplacementPlanChangedError(RuntimeError):
    """The installed source snapshot changed while transport was in flight."""


class MarketReplacementProfilePathError(RuntimeError):
    """The recorded replacement profile cannot be resolved safely."""


class MarketReplacementTransactionError(RuntimeError):
    """Replacement failed after the shared filesystem transaction began."""

    def __init__(
        self,
        *,
        replacement_error: ReplacePluginError,
        source_restored: bool,
    ) -> None:
        super().__init__(str(replacement_error))
        self.replacement_error = replacement_error
        self.source_restored = source_restored


class PackageInstallPort(Protocol):
    """Package capabilities shared by local and Market installation."""

    async def save_uploaded_bytes(
        self,
        *,
        filename: str,
        content: bytes,
    ) -> dict[str, object]: ...

    async def copy_package_file(
        self,
        *,
        filename: str,
        package_path: str,
    ) -> dict[str, object]: ...

    async def sha256_file(self, *, path: Path) -> str: ...

    async def install_package(
        self,
        *,
        package: str,
        profiles_root: str | None,
        allow_external_profiles_root: bool,
        on_conflict: str,
        use_staging: bool,
        forced_directory_name: str | None,
    ) -> dict[str, object]: ...

    async def cleanup_failure(
        self,
        *,
        saved: dict[str, object] | None,
        target_dirs: list[Path],
        profile_dirs: list[Path],
    ) -> None: ...


class LocalImportPort(PackageInstallPort, Protocol):
    async def record_import_source(
        self,
        *,
        install_result: dict[str, object],
        package_filename: str,
        package_sha256: str,
    ) -> str | None: ...


class LocalReplacementPort(Protocol):
    """Concrete deployment and #2958 replacement capabilities."""

    async def deploy_local_replacement(
        self,
        request: LocalReplacementRequest,
    ) -> dict[str, object]: ...

    async def read_installed_plugin_id(self, *, target_dir: Path) -> str: ...

    async def validate_manifestless_backup(self, *, backup_dir: Path) -> bool: ...

    async def replace_local_package(
        self,
        *,
        request: LocalReplacementRequest,
        install_new: Callable[[], Awaitable[dict[str, object]]],
        validate_new: Callable[[], Awaitable[None]],
        validate_backup: Callable[[Path], Awaitable[None]] | None,
    ) -> ReplacePluginResult: ...


class MarketInstallPort(PackageInstallPort, Protocol):
    async def read_installed_plugin_id(self, *, target_dir: Path) -> str: ...

    async def record_imported_fallback(
        self,
        *,
        target_dir: Path,
        package_filename: str,
        package_sha256: str,
        package_id: str,
        profile_dir: str,
    ) -> dict[str, object]: ...

    async def record_market_install(
        self,
        *,
        target_dir: Path,
        plugin_id: str,
        market_detail: dict[str, object],
        package_id: str,
        profile_dir: str,
    ) -> tuple[dict[str, object], list[str]]: ...

    async def record_market_upgrade(
        self,
        *,
        target_dir: Path,
        plugin_id: str,
        market_detail: dict[str, object],
        package_id: str,
        profile_dir: str,
    ) -> tuple[dict[str, object], list[str]]: ...

    async def activate_fresh_candidate(
        self,
        *,
        plugin_id: str,
        target_dir: Path,
    ) -> bool: ...


class MarketReplacementTransactionPort(Protocol):
    async def run_serialized(
        self,
        operation: Callable[[], Awaitable[ReplacePluginResult]],
    ) -> ReplacePluginResult: ...

    async def snapshot_matches(
        self,
        *,
        expected_plugin_id: str,
        original_entry: object,
        original_entry_fingerprint: tuple[object, ...],
        installed_package_id: str,
    ) -> bool: ...

    async def deploy_replacement(
        self,
        request: MarketReplacementInstallRequest,
    ) -> dict[str, object]: ...

    async def resolve_profile_dir(
        self,
        *,
        original_entry: object,
        installed_package_id: str,
        default_profiles_root: Path,
    ) -> Path: ...

    async def read_installed_plugin_id(self, *, target_dir: Path) -> str: ...

    async def replace_plugin(
        self,
        *,
        plugin_id: str,
        target_dir: Path,
        profile_dir: Path,
        install_new: Callable[[], Awaitable[dict[str, object]]],
        validate_new: Callable[[], Awaitable[None]],
        on_rollback_start: Callable[[], None] | None,
    ) -> ReplacePluginResult: ...

    async def restore_install_source(self, *, original_entry: object) -> bool: ...


class InstallationCoordinator:
    """Compose package operations without owning their concrete services."""

    def __init__(
        self,
        local_import: LocalImportPort | None = None,
        *,
        local_replacement: LocalReplacementPort | None = None,
        market_install: MarketInstallPort | None = None,
        market_replacement: MarketReplacementTransactionPort | None = None,
    ) -> None:
        self._local_import = local_import
        self._local_replacement = local_replacement
        self._market_install = market_install
        self._market_replacement = market_replacement

    async def install_local(self, request: LocalImportRequest) -> LocalImportResult:
        request.validate()
        port = self._local_import
        if port is None:
            raise RuntimeError("local import port is not configured")

        saved: dict[str, object] | None = None
        target_dirs: list[Path] = []
        profile_dirs: list[Path] = []
        try:
            saved, saved_path, saved_name = await _materialize_package(
                port,
                filename=request.filename,
                content=request.content,
                package_path=request.package_path,
            )
            actual_sha256 = await port.sha256_file(path=saved_path)
            install_result = await port.install_package(
                package=str(saved_path),
                profiles_root=request.profiles_root,
                allow_external_profiles_root=request.allow_external_profiles_root,
                on_conflict=request.on_conflict,
                use_staging=True,
                forced_directory_name=None,
            )
            target_dirs = install_result_target_dirs(install_result)
            profile_dirs = install_result_profile_dirs(install_result)
            warning = await port.record_import_source(
                install_result=install_result,
                package_filename=saved_name,
                package_sha256=actual_sha256,
            )
            return LocalImportResult(
                upload=saved,
                install=install_result,
                install_source_warning=warning,
            )
        except Exception:
            await _cleanup_without_masking(
                port,
                saved=saved,
                target_dirs=target_dirs,
                profile_dirs=profile_dirs,
                operation="local import",
            )
            raise

    async def replace_local(
        self,
        request: LocalReplacementRequest,
    ) -> ReplacePluginResult:
        port = self._local_replacement
        if port is None:
            raise RuntimeError("local replacement port is not configured")

        async def install_new() -> dict[str, object]:
            return await port.deploy_local_replacement(request)

        async def validate_new() -> None:
            installed_plugin_id = await port.read_installed_plugin_id(
                target_dir=request.target_dir
            )
            if (
                installed_plugin_id != request.plugin_id
                or request.target_dir.name != request.directory_name
            ):
                raise ValueError(
                    "installed plugin identity does not match the upgrade plan"
                )

        async def validate_manifestless_backup(backup_dir: Path) -> None:
            if not await port.validate_manifestless_backup(backup_dir=backup_dir):
                raise ValueError(
                    "manifest-less plugin state changed before installation"
                )

        return await port.replace_local_package(
            request=request,
            install_new=install_new,
            validate_new=validate_new,
            validate_backup=(
                validate_manifestless_backup if request.manifestless_state else None
            ),
        )

    async def install_market_fresh(
        self,
        request: MarketFreshInstallRequest,
    ) -> MarketFreshInstallResult:
        request.validate()
        port = self._market_install
        if port is None:
            raise RuntimeError("fresh Market install port is not configured")

        warnings: list[str] = []
        saved: dict[str, object] | None = None
        target_dirs: list[Path] = []
        profile_dirs: list[Path] = []
        try:
            saved, saved_path, saved_name = await _materialize_package(
                port,
                filename=request.filename,
                content=request.content,
                package_path=request.package_path,
            )
            actual_sha256 = await port.sha256_file(path=saved_path)
            unpack_result = await port.install_package(
                package=str(saved_path),
                profiles_root=request.profiles_root,
                allow_external_profiles_root=request.allow_external_profiles_root,
                on_conflict=request.on_conflict,
                use_staging=True,
                forced_directory_name=request.forced_directory_name,
            )
            target_dirs = install_result_target_dirs(unpack_result)
            profile_dirs = install_result_profile_dirs(unpack_result)
            target_dir, _target_plugin_id = single_install_target(unpack_result)
            plugin_id = await port.read_installed_plugin_id(target_dir=target_dir)

            market_detail = dict(request.market_detail)
            required_keys = ("plugin_market_id", "version", "package_url")
            missing = [key for key in required_keys if not market_detail.get(key)]
            if missing:
                warnings.append(
                    f"market_detail missing required fields ({', '.join(missing)}); "
                    "falling back to imported channel"
                )
                install_dict = await port.record_imported_fallback(
                    target_dir=target_dir,
                    package_filename=saved_name,
                    package_sha256=actual_sha256,
                    package_id=str(unpack_result.get("package_id") or ""),
                    profile_dir=str(unpack_result.get("profile_dir") or ""),
                )
            else:
                _prepare_market_detail(
                    market_detail=market_detail,
                    plugin_id=plugin_id,
                    actual_sha256=actual_sha256,
                    unpack_result=unpack_result,
                    warnings=warnings,
                )
                install_dict, record_warnings = await port.record_market_install(
                    target_dir=target_dir,
                    plugin_id=plugin_id,
                    market_detail=market_detail,
                    package_id=str(unpack_result.get("package_id") or ""),
                    profile_dir=str(unpack_result.get("profile_dir") or ""),
                )
                warnings.extend(record_warnings)

            activated = await port.activate_fresh_candidate(
                plugin_id=plugin_id,
                target_dir=target_dir,
            )
            return MarketFreshInstallResult(
                upload=saved,
                unpack=unpack_result,
                install=install_dict,
                warnings=tuple(warnings),
                candidate_selection_required=not activated,
            )
        except Exception:
            await _cleanup_without_masking(
                port,
                saved=saved,
                target_dirs=target_dirs,
                profile_dirs=profile_dirs,
                operation="fresh Market install",
            )
            raise

    async def install_market_replacement(
        self,
        request: MarketReplacementInstallRequest,
    ) -> MarketReplacementInstallResult:
        request.validate()
        port = self._market_install
        if port is None:
            raise RuntimeError("Market install port is not configured")

        warnings: list[str] = []
        saved: dict[str, object] | None = None
        target_dirs: list[Path] = []
        profile_dirs: list[Path] = []
        try:
            saved, saved_path, saved_name = await _materialize_package(
                port,
                filename=request.filename,
                content=request.content,
                package_path=request.package_path,
            )
            actual_sha256 = await port.sha256_file(path=saved_path)
            unpack_result = await port.install_package(
                package=str(saved_path),
                profiles_root=request.profiles_root,
                allow_external_profiles_root=request.allow_external_profiles_root,
                on_conflict=request.on_conflict,
                use_staging=request.forced_directory_name is not None,
                forced_directory_name=request.forced_directory_name,
            )
            target_dirs = install_result_target_dirs(unpack_result)
            profile_dirs = install_result_profile_dirs(unpack_result)
            target_dir, _target_plugin_id = single_install_target(unpack_result)
            plugin_id = await port.read_installed_plugin_id(target_dir=target_dir)

            market_detail = dict(request.market_detail)
            required_keys = ("plugin_market_id", "version", "package_url")
            missing = [key for key in required_keys if not market_detail.get(key)]
            if missing:
                warnings.append(
                    f"market_detail missing required fields ({', '.join(missing)}); "
                    "falling back to imported channel"
                )
                install_dict = await port.record_imported_fallback(
                    target_dir=target_dir,
                    package_filename=saved_name,
                    package_sha256=actual_sha256,
                    package_id=str(unpack_result.get("package_id") or ""),
                    profile_dir=str(unpack_result.get("profile_dir") or ""),
                )
            else:
                expected_plugin_id = market_detail.get("expected_plugin_toml_id")
                if (
                    isinstance(expected_plugin_id, str)
                    and expected_plugin_id
                    and expected_plugin_id != plugin_id
                ):
                    raise ValueError(
                        "plugin identity mismatch: Market declared "
                        f"'{expected_plugin_id}' but the package contains "
                        f"plugin id '{plugin_id}'"
                    )
                _prepare_market_detail(
                    market_detail=market_detail,
                    plugin_id=plugin_id,
                    actual_sha256=actual_sha256,
                    unpack_result=unpack_result,
                    warnings=warnings,
                )
                install_dict, record_warnings = await port.record_market_upgrade(
                    target_dir=target_dir,
                    plugin_id=plugin_id,
                    market_detail=market_detail,
                    package_id=str(unpack_result.get("package_id") or ""),
                    profile_dir=str(unpack_result.get("profile_dir") or ""),
                )
                warnings.extend(record_warnings)

            return MarketReplacementInstallResult(
                upload=saved,
                unpack=unpack_result,
                install=install_dict,
                warnings=tuple(warnings),
            )
        except Exception:
            await _cleanup_without_masking(
                port,
                saved=saved,
                target_dirs=target_dirs,
                profile_dirs=profile_dirs,
                operation=f"Market {request.mode}",
            )
            raise

    async def replace_market(
        self,
        request: MarketReplacementTransactionRequest,
    ) -> ReplacePluginResult:
        port = self._market_replacement
        if port is None:
            raise RuntimeError("Market replacement transaction port is not configured")

        async def run_transaction() -> ReplacePluginResult:
            return await self._replace_market_serialized(port, request)

        return await port.run_serialized(run_transaction)

    async def _replace_market_serialized(
        self,
        port: MarketReplacementTransactionPort,
        request: MarketReplacementTransactionRequest,
    ) -> ReplacePluginResult:

        snapshot_matches = await port.snapshot_matches(
            expected_plugin_id=request.expected_plugin_id,
            original_entry=request.original_entry,
            original_entry_fingerprint=request.original_entry_fingerprint,
            installed_package_id=request.installed_package_id,
        )
        if not snapshot_matches:
            raise MarketReplacementPlanChangedError(
                "plugin installation changed while the package was downloading"
            )

        try:
            profile_dir = await port.resolve_profile_dir(
                original_entry=request.original_entry,
                installed_package_id=request.installed_package_id,
                default_profiles_root=request.default_profiles_root,
            )
        except Exception as exc:
            raise MarketReplacementProfilePathError(str(exc)) from exc

        deployment = replace(
            request.deployment,
            profiles_root=str(profile_dir.parent),
            allow_external_profiles_root=True,
        )

        source_write_attempted = False

        async def install_new() -> dict[str, object]:
            nonlocal source_write_attempted
            source_write_attempted = True
            return await port.deploy_replacement(deployment)

        async def validate_new() -> None:
            actual_plugin_id = await port.read_installed_plugin_id(
                target_dir=request.target_dir
            )
            if actual_plugin_id != request.installed_plugin_id:
                raise ValueError(
                    "installed plugin identity does not match the Market "
                    "replacement target"
                )

        try:
            return await port.replace_plugin(
                plugin_id=request.installed_plugin_id,
                target_dir=request.target_dir,
                profile_dir=profile_dir,
                install_new=install_new,
                validate_new=validate_new,
                on_rollback_start=request.on_rollback_start,
            )
        except ReplacePluginError as exc:
            source_restored = True
            if source_write_attempted:
                try:
                    source_restored = await port.restore_install_source(
                        original_entry=request.original_entry
                    )
                except Exception as restore_exc:  # pragma: no cover - port guard
                    source_restored = False
                    logger.error(
                        "Market install source rollback failed plugin_id={} err_type={}",
                        request.installed_plugin_id,
                        type(restore_exc).__name__,
                    )
            raise MarketReplacementTransactionError(
                replacement_error=exc,
                source_restored=source_restored,
            ) from exc


async def _materialize_package(
    port: PackageInstallPort,
    *,
    filename: str,
    content: bytes | None,
    package_path: str | None,
) -> tuple[dict[str, object], Path, str]:
    if package_path is not None:
        saved = await port.copy_package_file(
            filename=filename,
            package_path=package_path,
        )
    else:
        saved = await port.save_uploaded_bytes(
            filename=filename,
            content=content or b"",
        )
    saved_path = Path(_required_saved_string(saved, "path"))
    saved_name = _required_saved_string(saved, "name")
    return saved, saved_path, saved_name


def _prepare_market_detail(
    *,
    market_detail: dict[str, object],
    plugin_id: str,
    actual_sha256: str,
    unpack_result: dict[str, object],
    warnings: list[str],
) -> None:
    expected_plugin_id = market_detail.get("expected_plugin_toml_id")
    if (
        isinstance(expected_plugin_id, str)
        and expected_plugin_id
        and plugin_id
        and expected_plugin_id != plugin_id
    ):
        warnings.append(
            "plugin identity mismatch: Market declared "
            f"'{expected_plugin_id}' but the package contains plugin id "
            f"'{plugin_id}'; install proceeds but please verify the package source"
        )
    market_detail.pop("expected_plugin_toml_id", None)

    caller_sha_raw = market_detail.get("package_sha256")
    caller_sha = caller_sha_raw.lower() if isinstance(caller_sha_raw, str) else ""
    if caller_sha and caller_sha != actual_sha256:
        warnings.append(
            f"package_sha256 mismatch: market={caller_sha!r}, "
            f"actual={actual_sha256!r}; recording actual"
        )
    market_detail["package_sha256"] = actual_sha256

    unpacked_payload_hash = unpack_result.get("payload_hash")
    if isinstance(unpacked_payload_hash, str) and unpacked_payload_hash:
        caller_payload = market_detail.get("payload_hash")
        if (
            isinstance(caller_payload, str)
            and caller_payload
            and caller_payload.lower() != unpacked_payload_hash.lower()
        ):
            warnings.append("payload_hash mismatch between market and unpacked package")
        market_detail["payload_hash"] = unpacked_payload_hash


async def _cleanup_without_masking(
    port: PackageInstallPort,
    *,
    saved: dict[str, object] | None,
    target_dirs: list[Path],
    profile_dirs: list[Path],
    operation: str,
) -> None:
    try:
        await port.cleanup_failure(
            saved=saved,
            target_dirs=target_dirs,
            profile_dirs=profile_dirs,
        )
    except Exception as cleanup_exc:  # pragma: no cover - defensive port guard
        logger.warning(
            "{} cleanup failed without masking original error: {}",
            operation,
            cleanup_exc,
        )


def _required_saved_string(saved: dict[str, object], field: str) -> str:
    value = saved.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"saved package metadata is missing {field!r}")
    return value


__all__ = [
    "InstallationCoordinator",
    "install_result_entries",
    "install_result_profile_dirs",
    "install_result_target_dirs",
    "LocalImportPort",
    "LocalImportRequest",
    "LocalImportResult",
    "LocalReplacementPort",
    "LocalReplacementRequest",
    "MarketFreshInstallRequest",
    "MarketFreshInstallResult",
    "MarketInstallPort",
    "MarketReplacementInstallRequest",
    "MarketReplacementInstallResult",
    "MarketReplacementPlanChangedError",
    "MarketReplacementProfilePathError",
    "MarketReplacementTransactionError",
    "MarketReplacementTransactionPort",
    "MarketReplacementTransactionRequest",
    "single_install_target",
]
