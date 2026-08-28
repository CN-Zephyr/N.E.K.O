"""Process-local publication bridge for the durable plugin Registry.

Existing routes and lifecycle services are constructed at module import time.
This bridge lets those long-lived objects observe the Registry only after the
startup cutover has committed, without rebuilding a second service graph.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from plugin.server.application.install_source.models import PluginRegistrySnapshot

from .package_management.json_registry import (
    JsonPluginRegistry,
    RegistryMutation,
    RegistryRevisionConflict,
)

RegistrySnapshotProvider = Callable[[], PluginRegistrySnapshot | None]

_authority_lock = threading.RLock()
_published_registry: JsonPluginRegistry | None = None
_authority_blocked = False
_MAX_UPDATE_ATTEMPTS = 8


def publish_plugin_registry_authority(registry: JsonPluginRegistry) -> None:
    """Publish a fully initialized Registry as the process authority."""

    if registry.snapshot is None:
        raise ValueError("cannot publish an uninitialized plugin Registry")
    global _authority_blocked, _published_registry
    with _authority_lock:
        _published_registry = registry
        _authority_blocked = False


def block_plugin_registry_authority() -> None:
    """Configure fail-closed authority without exposing a usable snapshot."""

    global _authority_blocked, _published_registry
    with _authority_lock:
        _published_registry = None
        _authority_blocked = True


def clear_plugin_registry_authority(
    *,
    expected: JsonPluginRegistry | None = None,
) -> bool:
    """Clear the bridge, optionally only when it still owns ``expected``."""

    global _authority_blocked, _published_registry
    with _authority_lock:
        if expected is not None and _published_registry is not expected:
            return False
        changed = _published_registry is not None or _authority_blocked
        _published_registry = None
        _authority_blocked = False
        return changed


def get_published_plugin_registry() -> JsonPluginRegistry | None:
    with _authority_lock:
        return _published_registry


def is_plugin_registry_authority_configured() -> bool:
    with _authority_lock:
        return _published_registry is not None or _authority_blocked


def update_plugin_registry(
    registry: JsonPluginRegistry,
    mutate: RegistryMutation,
) -> PluginRegistrySnapshot:
    """Apply one mutation with bounded CAS retries against ``registry``."""

    last_conflict: RegistryRevisionConflict | None = None
    for _attempt in range(_MAX_UPDATE_ATTEMPTS):
        current = registry.load()
        try:
            return registry.update(
                expected_revision=current.revision,
                mutate=mutate,
            )
        except RegistryRevisionConflict as exc:
            last_conflict = exc
    if last_conflict is None:  # pragma: no cover - loop invariant
        raise RuntimeError("plugin Registry update retry loop did not run")
    raise last_conflict


def update_published_plugin_registry(
    mutate: RegistryMutation,
) -> PluginRegistrySnapshot | None:
    """Apply a retrying CAS mutation, or return ``None`` before cutover."""

    registry = get_published_plugin_registry()
    if registry is None:
        return None
    return update_plugin_registry(registry, mutate)


def _published_snapshot() -> PluginRegistrySnapshot | None:
    registry = get_published_plugin_registry()
    # The Registry file is shared by more than one plugin-server process.  A
    # cached in-process snapshot would hide commits made by another process and
    # could make lifecycle decisions from stale Selection/StateOwner data.
    return registry.load() if registry is not None else None


def get_published_registry_snapshot_provider() -> RegistrySnapshotProvider | None:
    """Return a dynamic provider only after authority publication."""

    with _authority_lock:
        if _published_registry is None and not _authority_blocked:
            return None
    return _published_snapshot


__all__ = [
    "RegistrySnapshotProvider",
    "block_plugin_registry_authority",
    "clear_plugin_registry_authority",
    "get_published_plugin_registry",
    "get_published_registry_snapshot_provider",
    "is_plugin_registry_authority_configured",
    "publish_plugin_registry_authority",
    "update_plugin_registry",
    "update_published_plugin_registry",
]
