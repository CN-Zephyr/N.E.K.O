"""Concrete Market replacement adapter for the installation coordinator."""

from __future__ import annotations

import asyncio
import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from plugin.core.plugin_layout import resolve_plugin_layout
from plugin.logging_config import get_logger
from plugin.server.application.package_management.coordinator import (
    MarketReplacementInstallRequest,
)
from plugin.server.application.package_management.filesystem import remove_directory
from plugin.server.application.package_management.profile_cleanup import (
    path_has_symlink_ancestor,
)
from plugin.server.application.package_management.replacement import (
    ReplacePluginResult,
    replace_plugin,
)
from plugin.server.application.plugins.operation_lock import serialized_plugin_operation
from plugin.server.application.plugins.upgrade_support import (
    plugin_is_running,
    start_plugin_after_upgrade,
    stop_plugin_for_upgrade,
)

logger = get_logger("server.infrastructure.package_management.market_replacement")


class MarketDeploymentService(Protocol):
    async def install_market_replacement(
        self,
        request: MarketReplacementInstallRequest,
    ) -> dict[str, object]: ...


class MarketInstallSourceManager(Protocol):
    def find_active_market_entry(self, plugin_id: str) -> object | None: ...


@serialized_plugin_operation
async def _run_serialized_market_replacement(
    operation: Callable[[], Awaitable[ReplacePluginResult]],
) -> ReplacePluginResult:
    """Run snapshot revalidation and replacement under the operation lock."""
    return await operation()


class MarketReplacementAdapter:
    """Bind package replacement, provenance, and runtime lifecycle capabilities."""

    def __init__(
        self,
        manager: MarketInstallSourceManager,
        *,
        deployment_service: MarketDeploymentService,
    ) -> None:
        self._manager = manager
        self._deployment_service = deployment_service

    async def run_serialized(
        self,
        operation: Callable[[], Awaitable[ReplacePluginResult]],
    ) -> ReplacePluginResult:
        return await _run_serialized_market_replacement(operation)

    async def snapshot_matches(
        self,
        *,
        expected_plugin_id: str,
        original_entry: object,
        original_entry_fingerprint: tuple[object, ...],
        installed_package_id: str,
    ) -> bool:
        active_entry = self._manager.find_active_market_entry(expected_plugin_id)
        if active_entry is None:
            return False
        return (
            getattr(active_entry, "plugin_id", None)
            == getattr(original_entry, "plugin_id", None)
            and getattr(active_entry, "directory_name", None)
            == getattr(original_entry, "directory_name", None)
            and (
                getattr(active_entry, "package_id", "")
                or getattr(active_entry, "plugin_id", "")
            )
            == installed_package_id
            and market_entry_fingerprint(active_entry)
            == original_entry_fingerprint
        )

    async def deploy_replacement(
        self,
        request: MarketReplacementInstallRequest,
    ) -> dict[str, object]:
        return await self._deployment_service.install_market_replacement(request)

    async def resolve_profile_dir(
        self,
        *,
        original_entry: object,
        installed_package_id: str,
        default_profiles_root: Path,
    ) -> Path:
        return await asyncio.to_thread(
            _resolve_market_profile_dir,
            original_entry,
            installed_package_id,
            default_profiles_root,
        )

    async def read_installed_plugin_id(self, *, target_dir: Path) -> str:
        return await asyncio.to_thread(
            _read_plugin_toml_id,
            target_dir / "plugin.toml",
        )

    async def replace_plugin(
        self,
        *,
        plugin_id: str,
        target_dir: Path,
        profile_dir: Path,
        install_new: Callable[[], Awaitable[dict[str, object]]],
        validate_new: Callable[[], Awaitable[None]],
        on_rollback_start: Callable[[], None] | None,
    ) -> ReplacePluginResult:
        async def start(candidate_plugin_id: str) -> None:
            await start_plugin_after_upgrade(candidate_plugin_id, strict=True)

        return await replace_plugin(
            layout=resolve_plugin_layout(plugin_id, target_dir),
            install_new=install_new,
            validate_new=validate_new,
            is_running=plugin_is_running,
            stop=stop_plugin_for_upgrade,
            start=start,
            cleanup_backup=_async_remove_dir,
            additional_targets=(profile_dir,),
            preserve_targets=(profile_dir,),
            on_rollback_start=on_rollback_start,
        )

    async def restore_install_source(self, *, original_entry: object) -> bool:
        restore_source = getattr(self._manager, "restore_entry_for_rollback", None)
        if not callable(restore_source):
            return True
        try:
            await asyncio.to_thread(restore_source, original_entry)
        except Exception as restore_exc:
            logger.error(
                "market install source rollback failed plugin_id={} err={}",
                getattr(original_entry, "plugin_id", ""),
                restore_exc,
            )
            return False
        return True


def market_entry_fingerprint(entry: object) -> tuple[object, ...]:
    """Identify the exact install-source snapshot an upgrade was planned against."""
    source_detail = getattr(entry, "source_detail", None)
    return (
        getattr(entry, "root_id", ""),
        getattr(entry, "directory_name", ""),
        getattr(entry, "plugin_id", ""),
        getattr(entry, "package_id", ""),
        getattr(entry, "profile_dir", ""),
        getattr(entry, "profile_installed", None),
        getattr(entry, "installed_at", ""),
        getattr(entry, "updated_at", ""),
        getattr(source_detail, "version", ""),
        getattr(source_detail, "package_sha256", ""),
    )


def _resolve_market_profile_dir(
    entry: object,
    installed_package_id: str,
    default_profiles_root: Path,
) -> Path:
    recorded_profile_dir = str(getattr(entry, "profile_dir", "") or "")
    profile_candidate = (
        Path(recorded_profile_dir).expanduser()
        if recorded_profile_dir
        else default_profiles_root / installed_package_id
    )
    if path_has_symlink_ancestor(profile_candidate):
        raise ValueError(
            f"recorded package profile path contains a symlink: {profile_candidate}"
        )
    try:
        profile_dir = profile_candidate.resolve()
    except OSError as exc:
        raise ValueError(
            f"cannot resolve recorded package profile path: {profile_candidate}"
        ) from exc
    if profile_dir.name != installed_package_id:
        raise ValueError(
            "recorded package profile path does not match package id: "
            f"{profile_dir}"
        )
    return profile_dir


def _read_plugin_toml_id(manifest: Path) -> str | None:
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("Failed to read plugin manifest {}: {}", manifest, exc)
        return None

    plugin_table = data.get("plugin")
    if not isinstance(plugin_table, dict):
        return None
    plugin_id = plugin_table.get("id")
    if not isinstance(plugin_id, str) or not plugin_id.strip():
        return None
    return plugin_id.strip()


async def _async_remove_dir(target_dir: Path) -> None:
    """Async best-effort cleanup for a replacement backup."""
    try:
        await remove_directory(target_dir)
    except Exception as exc:  # pragma: no cover - platform-specific cleanup failure
        logger.warning("backup cleanup failed for {}: {}", target_dir, exc)
