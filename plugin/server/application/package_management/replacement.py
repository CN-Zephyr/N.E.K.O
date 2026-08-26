"""Transactional replacement of managed plugin package directories."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from plugin.core.plugin_layout import PluginLayout
from plugin.logging_config import get_logger
from plugin.server.infrastructure.config_paths import ensure_plugin_layout_runtime_config

from . import filesystem

logger = get_logger("server.application.package_management.replacement")


@dataclass(frozen=True, slots=True)
class ReplacePluginResult:
    restarted: bool
    rollback_status: str
    install_result: dict[str, object]
    backup_dir: Path


class ReplacePluginError(RuntimeError):
    def __init__(self, *, stage: str, rollback_status: str, cause: Exception) -> None:
        super().__init__(f"{stage} failed: {cause}")
        self.stage = stage
        self.rollback_status = rollback_status
        self.cause = cause


async def run_rollback(
    *,
    plugin_id: str,
    target_dir: Path,
    backup_dir: Path,
    restart: bool,
    start: Callable[[str], Awaitable[None]],
) -> bool:
    restored = True
    try:
        await filesystem.restore_directory(backup_dir, target_dir)
    except Exception as exc:
        restored = False
        logger.error(
            "plugin directory rollback failed plugin_id={} err_type={}",
            plugin_id,
            type(exc).__name__,
        )
    if restart:
        try:
            await start(plugin_id)
        except Exception as exc:
            restored = False
            logger.error(
                "plugin rollback restart failed plugin_id={} err_type={}",
                plugin_id,
                type(exc).__name__,
            )
    return restored


async def rollback_targets(
    *,
    targets: tuple[Path, ...],
    backups: dict[Path, Path],
    preexisting_targets: frozenset[Path],
    remove_created_targets: bool,
) -> bool:
    restored = True
    for target in reversed(targets):
        backup = backups.get(target)
        if backup is None:
            if remove_created_targets and target not in preexisting_targets:
                try:
                    await filesystem.remove_directory(target)
                except Exception as exc:
                    restored = False
                    logger.error(
                        "plugin replacement created-target cleanup failed "
                        "target={} err_type={}",
                        target.name,
                        type(exc).__name__,
                    )
            continue
        try:
            await filesystem.remove_directory(target)
            await filesystem.restore_directory(backup, target)
        except Exception as exc:
            restored = False
            logger.error(
                "plugin replacement target rollback failed target={} err_type={}",
                target.name,
                type(exc).__name__,
            )
    return restored


def _notify_rollback_start(callback: Callable[[], None] | None) -> None:
    if callback is None:
        return
    try:
        callback()
    except Exception as exc:
        logger.warning(
            "plugin replacement rollback observer failed err_type={}",
            type(exc).__name__,
        )


async def replace_plugin(
    *,
    layout: PluginLayout,
    install_new: Callable[[], Awaitable[dict[str, object]]],
    validate_new: Callable[[], Awaitable[None]],
    is_running: Callable[[str], Awaitable[bool]],
    stop: Callable[[str], Awaitable[None]],
    start: Callable[[str], Awaitable[None]],
    cleanup_backup: Callable[[Path], Awaitable[None]],
    additional_targets: tuple[Path, ...] = (),
    preserve_targets: tuple[Path, ...] = (),
    initialize_runtime_config: bool = True,
    validate_backup: Callable[[Path], Awaitable[None]] | None = None,
    on_rollback_start: Callable[[], None] | None = None,
) -> ReplacePluginResult:
    plugin_id = layout.plugin_id
    target_dir = layout.installed_dir
    if not plugin_id:
        raise ValueError("plugin replacement requires a plugin id")
    if not target_dir.is_dir():
        raise FileNotFoundError(
            f"installed plugin directory is missing: {target_dir.name}"
        )
    targets = (target_dir, *additional_targets)
    if any(target not in targets for target in preserve_targets):
        raise ValueError("preserve targets must also be replacement targets")

    if initialize_runtime_config:
        await asyncio.to_thread(ensure_plugin_layout_runtime_config, layout)
    was_running = await is_running(plugin_id)
    if was_running:
        await stop(plugin_id)

    preexisting_targets = frozenset(target for target in targets if target.exists())
    backups: dict[Path, Path] = {}
    backup_dir = filesystem.backup_path_for(target_dir)
    try:
        for target in targets:
            if not target.exists():
                continue
            if not target.is_dir():
                raise NotADirectoryError(target)
            backup = (
                backup_dir
                if target == target_dir
                else filesystem.backup_path_for(target)
            )
            await asyncio.to_thread(backup.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(target.rename, backup)
            backups[target] = backup
    except Exception as exc:
        _notify_rollback_start(on_rollback_start)
        recovered = await rollback_targets(
            targets=targets,
            backups=backups,
            preexisting_targets=preexisting_targets,
            remove_created_targets=False,
        )
        if was_running:
            try:
                await start(plugin_id)
            except Exception as restart_exc:
                recovered = False
                logger.error(
                    "plugin restart after backup failure failed "
                    "plugin_id={} err_type={}",
                    plugin_id,
                    type(restart_exc).__name__,
                )
        raise ReplacePluginError(
            stage="backup",
            rollback_status="completed" if recovered else "incomplete",
            cause=exc,
        ) from exc

    stage = "backup_validation"
    try:
        if validate_backup is not None:
            await validate_backup(backups[target_dir])
        stage = "install"
        install_result = await install_new()
        stage = "validate"
        await validate_new()
        stage = "preserve"
        for target in preserve_targets:
            backup = backups.get(target)
            if backup is not None:
                await filesystem.merge_directory_contents(backup, target)
        await filesystem.restore_manifest_adjacent_profiles(backup_dir, target_dir)
        if was_running:
            stage = "restart"
            await start(plugin_id)
        stage = "cleanup"
        for backup in backups.values():
            try:
                await cleanup_backup(backup)
            except Exception as exc:
                logger.warning(
                    "plugin backup cleanup failed plugin_id={} err_type={}",
                    plugin_id,
                    type(exc).__name__,
                )
        return ReplacePluginResult(
            restarted=was_running,
            rollback_status="not_needed",
            install_result=install_result,
            backup_dir=backup_dir,
        )
    except Exception as exc:
        _notify_rollback_start(on_rollback_start)
        restored = await rollback_targets(
            targets=targets,
            backups=backups,
            preexisting_targets=preexisting_targets,
            remove_created_targets=True,
        )
        if was_running:
            try:
                await start(plugin_id)
            except Exception as restart_exc:
                restored = False
                logger.error(
                    "plugin rollback restart failed plugin_id={} err_type={}",
                    plugin_id,
                    type(restart_exc).__name__,
                )
        raise ReplacePluginError(
            stage=stage,
            rollback_status="completed" if restored else "incomplete",
            cause=exc,
        ) from exc


__all__ = [
    "ReplacePluginError",
    "ReplacePluginResult",
    "replace_plugin",
    "rollback_targets",
    "run_rollback",
]
