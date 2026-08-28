from __future__ import annotations

import asyncio

from plugin.logging_config import get_logger
from plugin.server.domain.errors import ServerDomainError

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


# Compatibility lifecycle aliases retained for the Market replacement adapter.
stop_plugin_for_upgrade = stop_plugin_for_replace
start_plugin_after_upgrade = start_plugin_after_replace
