"""Startup authority switch for the unified plugin Registry."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from plugin.server.application.install_source import set_global_manager
from plugin.server.application.install_source.manager import InstallSourceManager
from plugin.server.infrastructure.package_management.install_source_facade import (
    RegistryInstallSourceFacade,
)
from plugin.server.infrastructure.package_management.registry_cutover import (
    RegistryCutoverOperationLock,
)
from plugin.server.infrastructure.package_management.registry_runtime import (
    PluginRegistryRuntime,
    RegistryConfigPaths,
    build_plugin_registry_runtime,
)
from plugin.server.infrastructure.plugin_registry_authority import (
    block_plugin_registry_authority,
    clear_plugin_registry_authority,
    publish_plugin_registry_authority,
)


@dataclass(frozen=True, slots=True)
class PluginRegistryStartupResult:
    """Persistence authority selected before runtime discovery starts."""

    mode: Literal["registry", "legacy", "blocked"]
    install_source: InstallSourceManager | RegistryInstallSourceFacade | None
    runtime: PluginRegistryRuntime
    error_reason: str | None = None


async def initialize_plugin_registry_startup(
    *,
    install_source_manager: InstallSourceManager,
    config_paths: RegistryConfigPaths,
    operation_lock: RegistryCutoverOperationLock,
    clock: Callable[[], str],
    reconciled: bool = True,
) -> PluginRegistryStartupResult:
    """Initialize, publish, or safely decline the Registry authority switch.

    A first preflight failure before Registry creation remains on the legacy
    manager.  Once a Registry or commit marker exists, every failure blocks the
    authority instead; falling back at that point could revive retired state.
    """

    clear_plugin_registry_authority()
    set_global_manager(None)
    runtime = build_plugin_registry_runtime(
        install_source_manager=install_source_manager,
        config_paths=config_paths,
        clock=clock,
    )
    if not reconciled:
        if runtime.paths.registry.exists() or runtime.paths.cutover_commit.exists():
            block_plugin_registry_authority()
            return PluginRegistryStartupResult(
                mode="blocked",
                install_source=None,
                runtime=runtime,
                error_reason="legacy_reconcile_failed",
            )
        set_global_manager(install_source_manager)
        return PluginRegistryStartupResult(
            mode="legacy",
            install_source=install_source_manager,
            runtime=runtime,
            error_reason="legacy_reconcile_failed",
        )
    try:
        await runtime.initialize(operation_lock=operation_lock)
        facade = RegistryInstallSourceFacade(
            legacy_manager=install_source_manager,
            registry=runtime.registry,
            clock=clock,
        )
        publish_plugin_registry_authority(runtime.registry)
        set_global_manager(facade)  # type: ignore[arg-type]
        return PluginRegistryStartupResult(
            mode="registry",
            install_source=facade,
            runtime=runtime,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        reason = getattr(exc, "reason", None) or getattr(exc, "code", None)
        error_reason = reason if isinstance(reason, str) else type(exc).__name__
        authority_started = (
            runtime.paths.registry.exists() or runtime.paths.cutover_commit.exists()
        )
        if authority_started:
            block_plugin_registry_authority()
            set_global_manager(None)
            return PluginRegistryStartupResult(
                mode="blocked",
                install_source=None,
                runtime=runtime,
                error_reason=error_reason,
            )
        clear_plugin_registry_authority()
        set_global_manager(install_source_manager)
        return PluginRegistryStartupResult(
            mode="legacy",
            install_source=install_source_manager,
            runtime=runtime,
            error_reason=error_reason,
        )


__all__ = [
    "PluginRegistryStartupResult",
    "initialize_plugin_registry_startup",
]
