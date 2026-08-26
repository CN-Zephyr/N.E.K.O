from __future__ import annotations

import asyncio
from pathlib import Path

from plugin.logging_config import get_logger
from plugin.server.application.package_management import filesystem as package_filesystem
from plugin.server.application.package_management.replacement import (
    ReplacePluginError,
    ReplacePluginResult,
    replace_plugin,
    rollback_targets as _rollback_targets,
    run_rollback,
)
from plugin.server.domain.errors import ServerDomainError

# Compatibility test seam for callers that historically patched
# ``upgrade_support.shutil``. The actual filesystem owner is now the dedicated
# package-management module.
shutil = package_filesystem.shutil

logger = get_logger("server.application.plugins.upgrade_support")

async def plugin_is_running(plugin_id: str) -> bool:
    if not plugin_id:
        return False
    try:
        from plugin.server.application.plugins.lifecycle_service import _plugin_is_running_sync

        return await asyncio.to_thread(_plugin_is_running_sync, plugin_id)
    except Exception as exc:  # pragma: no cover - defensive host-registry boundary
        logger.warning(
            "lifecycle running-state probe failed plugin_id={} err_type={}",
            plugin_id,
            type(exc).__name__,
        )
        raise


async def stop_plugin_for_replace(plugin_id: str) -> None:
    if not plugin_id:
        return
    from plugin.server.application.plugins.lifecycle_service import PluginLifecycleService

    try:
        await PluginLifecycleService().stop_plugin(plugin_id)
    except ServerDomainError as exc:
        if getattr(exc, "code", None) == "PLUGIN_NOT_RUNNING":
            return
        raise


async def start_plugin_after_replace(plugin_id: str, *, strict: bool) -> bool:
    if not plugin_id:
        return False
    from plugin.server.application.plugins.lifecycle_service import PluginLifecycleService

    try:
        await PluginLifecycleService().start_plugin(plugin_id)
        return True
    except Exception as exc:
        logger.error(
            "lifecycle restart failed plugin_id={} err_type={}",
            plugin_id,
            type(exc).__name__,
        )
        if strict:
            raise
        return False


# Market keeps the established names until its Day 3 adapter switches to the
# shared replace transaction.
stop_plugin_for_upgrade = stop_plugin_for_replace
start_plugin_after_upgrade = start_plugin_after_replace


def backup_path_for(target_dir: Path, *, backup_root: Path | None = None) -> Path:
    return package_filesystem.backup_path_for(target_dir, backup_root=backup_root)


async def restore_directory(backup_dir: Path, target_dir: Path) -> None:
    await package_filesystem.restore_directory(backup_dir, target_dir)


async def remove_directory(target_dir: Path) -> None:
    await package_filesystem.remove_directory(target_dir)


async def merge_directory_contents(source_dir: Path, target_dir: Path) -> None:
    await package_filesystem.merge_directory_contents(source_dir, target_dir)


def _assert_preserved_tree_has_no_links_or_reparse_points(source: Path) -> None:
    package_filesystem.assert_preserved_tree_has_no_links_or_reparse_points(source)


def _canonical_profile_sources(sources: list[Path]) -> dict[str, Path]:
    return package_filesystem.canonical_profile_sources(sources)


async def _restore_manifest_adjacent_profiles(backup_dir: Path, target_dir: Path) -> None:
    await package_filesystem.restore_manifest_adjacent_profiles(
        backup_dir,
        target_dir,
    )
