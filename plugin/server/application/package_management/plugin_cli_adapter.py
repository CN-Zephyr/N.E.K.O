"""Plugin CLI compatibility adapter for package installation transactions.

The coordinator owns transaction ordering. This adapter translates the
existing Plugin CLI service surface into its narrow ports and owns best-effort
cleanup of package artifacts created by a failed deployment.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
from typing import Protocol

from plugin.core.plugin_layout import resolve_plugin_layout
from plugin.logging_config import get_logger
from plugin.server.application.install_source import (
    InstallSourceManager,
    classify_plugin_path,
)
from plugin.server.application.package_management import filesystem
from plugin.server.application.package_management import replacement
from plugin.server.application.package_management.coordinator import (
    LocalReplacementRequest,
)
from plugin.server.application.package_management.install_plan import (
    is_manifestless_state_directory,
)
from plugin.server.application.plugins import upgrade_support

logger = get_logger("server.application.package_management.plugin_cli_adapter")


class PluginCliDeploymentGateway(Protocol):
    """The compatibility callbacks that remain owned by Plugin CLI."""

    async def save_uploaded_package(
        self,
        *,
        filename: str,
        content: bytes,
    ) -> dict[str, object]: ...

    def _save_package_file_sync(
        self,
        *,
        filename: str,
        package_path: str,
    ) -> dict[str, object]: ...

    def _sha256_file(self, path: str | Path) -> str: ...

    async def install(
        self,
        *,
        package: str,
        profiles_root: str | None,
        on_conflict: str,
        use_staging: bool,
        forced_directory_name: str | None,
        _allow_external_profiles_root: bool,
    ) -> dict[str, object]: ...

    def _install_sync(
        self,
        *,
        package: str,
        plugins_root: str | None,
        profiles_root: str | None,
        on_conflict: str,
        use_staging: bool,
        forced_directory_name: str | None,
        _allow_external_profiles_root: bool,
    ) -> dict[str, object]: ...

    async def _record_install_source_best_effort(
        self,
        *,
        install_result: dict[str, object],
        package_filename: str,
        package_sha256: str,
        override: dict[str, object] | None,
    ) -> str | None: ...

    def _read_installed_plugin_toml_id(self, target_dir: Path) -> str: ...

    async def _record_imported_for_unpack(
        self,
        *,
        target_dir: Path,
        saved_filename: str,
        actual_sha256: str,
        package_id: str,
        profile_dir: str,
    ) -> dict[str, object]: ...

    def _require_install_source_manager(self) -> InstallSourceManager: ...

    async def _activate_fresh_install_candidate(
        self,
        *,
        plugin_id: str,
        target_dir: Path,
    ) -> bool: ...

    def _rollback_install_source_best_effort(self, target_dir: Path) -> None: ...


class PluginCliInstallationAdapter:
    """Adapt the current Plugin CLI facade to coordinator transactions."""

    def __init__(self, gateway: PluginCliDeploymentGateway) -> None:
        self._gateway = gateway

    async def save_uploaded_bytes(
        self,
        *,
        filename: str,
        content: bytes,
    ) -> dict[str, object]:
        return await self._gateway.save_uploaded_package(
            filename=filename,
            content=content,
        )

    async def copy_package_file(
        self,
        *,
        filename: str,
        package_path: str,
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            self._gateway._save_package_file_sync,
            filename=filename,
            package_path=package_path,
        )

    async def sha256_file(self, *, path: Path) -> str:
        return await asyncio.to_thread(self._gateway._sha256_file, path)

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
        return await self._gateway.install(
            package=package,
            profiles_root=profiles_root,
            on_conflict=on_conflict,
            use_staging=use_staging,
            forced_directory_name=forced_directory_name,
            _allow_external_profiles_root=allow_external_profiles_root,
        )

    async def record_import_source(
        self,
        *,
        install_result: dict[str, object],
        package_filename: str,
        package_sha256: str,
    ) -> str | None:
        return await self._gateway._record_install_source_best_effort(
            install_result=install_result,
            package_filename=package_filename,
            package_sha256=package_sha256,
            override=None,
        )

    async def deploy_local_replacement(
        self,
        request: LocalReplacementRequest,
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            self._gateway._install_sync,
            package=request.package,
            plugins_root=request.plugins_root,
            profiles_root=request.profiles_root,
            on_conflict="fail",
            use_staging=request.use_staging,
            forced_directory_name=request.forced_directory_name,
            _allow_external_profiles_root=request.allow_external_profiles_root,
        )

    async def read_installed_plugin_id(self, *, target_dir: Path) -> str:
        return await asyncio.to_thread(
            self._gateway._read_installed_plugin_toml_id,
            target_dir,
        )

    async def validate_manifestless_backup(self, *, backup_dir: Path) -> bool:
        return await asyncio.to_thread(is_manifestless_state_directory, backup_dir)

    async def replace_local_package(
        self,
        *,
        request: LocalReplacementRequest,
        install_new,
        validate_new,
        validate_backup,
    ) -> replacement.ReplacePluginResult:
        async def start(plugin_id: str) -> None:
            await upgrade_support.start_plugin_after_replace(plugin_id, strict=True)

        return await replacement.replace_plugin(
            layout=resolve_plugin_layout(request.plugin_id, request.target_dir),
            install_new=install_new,
            validate_new=validate_new,
            is_running=upgrade_support.plugin_is_running,
            stop=upgrade_support.stop_plugin_for_replace,
            start=start,
            cleanup_backup=filesystem.remove_directory,
            additional_targets=(request.profile_dir,),
            preserve_targets=(
                (request.target_dir, request.profile_dir)
                if request.manifestless_state
                else (request.profile_dir,)
            ),
            initialize_runtime_config=not request.manifestless_state,
            validate_backup=validate_backup,
        )

    async def record_imported_fallback(
        self,
        *,
        target_dir: Path,
        package_filename: str,
        package_sha256: str,
        package_id: str,
        profile_dir: str,
    ) -> dict[str, object]:
        return await self._gateway._record_imported_for_unpack(
            target_dir=target_dir,
            saved_filename=package_filename,
            actual_sha256=package_sha256,
            package_id=package_id,
            profile_dir=profile_dir,
        )

    async def record_market_install(
        self,
        *,
        target_dir: Path,
        plugin_id: str,
        market_detail: dict[str, object],
        package_id: str,
        profile_dir: str,
    ) -> tuple[dict[str, object], list[str]]:
        manager = self._gateway._require_install_source_manager()
        root_id, directory_name = classify_plugin_path(
            target_dir,
            builtin_root=manager.builtin_root,
            user_root=manager.user_root,
        )

        def record():
            return manager.record_market_install(
                root_id=root_id,
                directory_name=directory_name,
                plugin_id=plugin_id,
                market_detail=market_detail,
                package_id=package_id,
                profile_dir=profile_dir,
            )

        entry, warnings = await asyncio.to_thread(record)
        return _market_entry_to_install_dict(entry), warnings

    async def record_market_upgrade(
        self,
        *,
        target_dir: Path,
        plugin_id: str,
        market_detail: dict[str, object],
        package_id: str,
        profile_dir: str,
    ) -> tuple[dict[str, object], list[str]]:
        manager = self._gateway._require_install_source_manager()
        root_id, directory_name = classify_plugin_path(
            target_dir,
            builtin_root=manager.builtin_root,
            user_root=manager.user_root,
        )

        def record():
            return manager.record_market_upgrade(
                root_id=root_id,
                directory_name=directory_name,
                plugin_id=plugin_id,
                market_detail=market_detail,
                package_id=package_id,
                profile_dir=profile_dir,
            )

        entry, warnings = await asyncio.to_thread(record)
        return _market_entry_to_install_dict(entry), warnings

    async def activate_fresh_candidate(
        self,
        *,
        plugin_id: str,
        target_dir: Path,
    ) -> bool:
        return await self._gateway._activate_fresh_install_candidate(
            plugin_id=plugin_id,
            target_dir=target_dir,
        )

    async def cleanup_failure(
        self,
        *,
        saved: dict[str, object] | None,
        target_dirs: list[Path],
        profile_dirs: list[Path],
    ) -> None:
        await asyncio.to_thread(
            self._cleanup_failure_sync,
            saved=saved,
            target_dirs=target_dirs,
            profile_dirs=profile_dirs,
        )

    def _cleanup_failure_sync(
        self,
        *,
        saved: dict[str, object] | None,
        target_dirs: list[Path],
        profile_dirs: list[Path],
    ) -> None:
        for target_dir in target_dirs:
            self._gateway._rollback_install_source_best_effort(target_dir)
            _remove_failed_directory(target_dir)
        for profile_dir in profile_dirs:
            _remove_failed_directory(profile_dir)
        if saved is None:
            return
        saved_path = saved.get("path")
        if not isinstance(saved_path, str) or not saved_path:
            return
        try:
            Path(saved_path).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "upload_and_install: failed to clean up saved package {}: {}",
                saved_path,
                exc,
            )


def _remove_failed_directory(target_dir: Path) -> None:
    try:
        shutil.rmtree(target_dir, ignore_errors=True)
    except OSError as exc:  # pragma: no cover - ignore_errors=True suppresses
        logger.warning(
            "upload_and_install: failed to clean unpacked directory {}: {}",
            target_dir,
            exc,
        )


def _market_entry_to_install_dict(entry: object) -> dict[str, object]:
    install_dict: dict[str, object] = {
        "channel": getattr(entry, "channel", ""),
        "directory_name": getattr(entry, "directory_name", ""),
        "plugin_id": getattr(entry, "plugin_id", ""),
    }
    source_detail = getattr(entry, "source_detail", None)
    if source_detail is not None and hasattr(source_detail, "version"):
        install_dict.update(
            {
                "version": getattr(source_detail, "version", ""),
                "package_sha256": getattr(source_detail, "package_sha256", ""),
                "payload_hash": getattr(source_detail, "payload_hash", None),
                "published_at": getattr(source_detail, "published_at", ""),
                "previous_version": getattr(source_detail, "previous_version", None),
            }
        )
    return install_dict


__all__ = ["PluginCliDeploymentGateway", "PluginCliInstallationAdapter"]
