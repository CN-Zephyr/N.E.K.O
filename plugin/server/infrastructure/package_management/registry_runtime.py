"""Production path composition for the unified plugin Registry.

This module only constructs and initializes the Registry runtime.  Publishing
its snapshot to runtime discovery is a separate startup action so callers
cannot accidentally expose a partially migrated authority.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from plugin.server.application.install_source.manager import InstallSourceManager
from plugin.server.application.install_source.models import (
    LockFile,
    PluginRegistrySnapshot,
)
from plugin.server.infrastructure.plugin_selections import SELECTIONS_FILENAME
from plugin.server.infrastructure.runtime_overrides import OVERRIDES_FILENAME

from .json_registry import JsonPluginRegistry
from .registry_cutover import (
    RegistryCutoverInitializationResult,
    RegistryCutoverOperationLock,
    RegistryCutoverPaths,
    initialize_registry_cutover,
)

REGISTRY_FILENAME = "plugin_registry.json"


class RegistryConfigPaths(Protocol):
    """Narrow ConfigManager view needed to resolve legacy and runtime paths."""

    def get_config_path(self, filename: str) -> Path: ...

    def get_runtime_config_path(self, filename: str) -> Path: ...


@dataclass(frozen=True, slots=True)
class PluginRegistryRuntime:
    """One Registry store plus the exact legacy authorities it supersedes."""

    paths: RegistryCutoverPaths
    registry: JsonPluginRegistry
    lock_snapshot_provider: Callable[[], LockFile]
    clock: Callable[[], str]

    def snapshot_provider(self) -> PluginRegistrySnapshot | None:
        """Return only a snapshot loaded or committed by this runtime."""

        return self.registry.snapshot

    async def initialize(
        self,
        *,
        operation_lock: RegistryCutoverOperationLock,
    ) -> RegistryCutoverInitializationResult:
        """Run the cutover transaction without publishing runtime authority."""

        return await initialize_registry_cutover(
            paths=self.paths,
            registry=self.registry,
            operation_lock=operation_lock,
            lock_snapshot_provider=self.lock_snapshot_provider,
            now=self.clock(),
        )


def build_plugin_registry_runtime(
    *,
    install_source_manager: InstallSourceManager,
    config_paths: RegistryConfigPaths,
    clock: Callable[[], str],
    registry_lock_timeout: float = 10.0,
) -> PluginRegistryRuntime:
    """Build the production Registry paths without reading authority contents.

    Legacy sidecars deliberately use ``get_config_path`` because their current
    read authority may still be the project-config fallback.  The new Registry
    always uses ``get_runtime_config_path`` so its only write location is the
    user runtime config directory.
    """

    paths = RegistryCutoverPaths(
        install_source=install_source_manager.lock_path,
        candidate_selections=Path(
            config_paths.get_config_path(SELECTIONS_FILENAME)
        ),
        runtime_overrides=Path(config_paths.get_config_path(OVERRIDES_FILENAME)),
        registry=Path(config_paths.get_runtime_config_path(REGISTRY_FILENAME)),
    )
    registry = JsonPluginRegistry(
        paths.registry,
        clock=clock,
        lock_timeout=registry_lock_timeout,
    )
    return PluginRegistryRuntime(
        paths=paths,
        registry=registry,
        lock_snapshot_provider=install_source_manager.snapshot,
        clock=clock,
    )


__all__ = [
    "PluginRegistryRuntime",
    "REGISTRY_FILENAME",
    "RegistryConfigPaths",
    "build_plugin_registry_runtime",
]
