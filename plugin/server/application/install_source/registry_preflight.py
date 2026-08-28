"""Read-only safety gate for the unified Registry cutover.

The preflight consumes an already-reconciled legacy v2 ``LockFile`` plus the
typed selection/state-owner and runtime-override snapshots.  It builds and
strictly round-trips the candidate Registry entirely in memory.  No file is
created and no runtime provider is published here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping

from plugin.server.application.install_source.models import (
    REGISTRY_SCHEMA_VERSION,
    LockFile,
    PluginRegistrySnapshot,
)
from plugin.server.application.install_source.registry_codec import (
    parse_registry,
    serialize_registry,
)
from plugin.server.application.install_source.registry_migration import (
    describe_migration_losses,
    migrate_legacy_state_to_registry,
)
from plugin.server.application.install_source.registry_shadow import (
    RegistryShadowComparison,
    compare_registry_snapshots,
)

if TYPE_CHECKING:
    from plugin.server.infrastructure.plugin_selections import PluginSelection


class RegistryCutoverPreflightError(RuntimeError):
    """The legacy state cannot safely become the canonical Registry."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = MappingProxyType(dict(details or {}))


@dataclass(frozen=True, slots=True)
class RegistryCutoverPreflightResult:
    """A validated in-memory candidate and its optional shadow result."""

    snapshot: PluginRegistrySnapshot
    shadow_comparison: RegistryShadowComparison | None = None


def prepare_registry_cutover_preflight(
    lock: LockFile,
    *,
    selections: Mapping[str, "PluginSelection"] | None = None,
    state_owners: Mapping[str, "PluginSelection"] | None = None,
    runtime_overrides: Mapping[str, object] | None = None,
    actual_registry: PluginRegistrySnapshot | None = None,
    now: str,
) -> RegistryCutoverPreflightResult:
    """Build a strict Registry candidate or fail closed without writing.

    ``lock`` must be the reconciled v2 snapshot already owned by the legacy
    manager.  Accepting the manager snapshot avoids reparsing the tolerant
    legacy JSON format during cutover and makes the exact reconciliation point
    an explicit caller responsibility.
    """

    if isinstance(lock.schema_version, bool) or lock.schema_version != 2:
        raise RegistryCutoverPreflightError(
            "unsupported_lock_schema",
            "cutover requires a reconciled plugins.lock.json schema v2 snapshot",
            details={"actual_schema_version": lock.schema_version},
        )

    selections = selections or {}
    state_owners = state_owners or {}
    runtime_overrides = runtime_overrides or {}
    candidate = migrate_legacy_state_to_registry(
        lock,
        selections=selections,
        state_owners=state_owners,
        runtime_overrides=runtime_overrides,
        now=now,
    )

    losses = describe_migration_losses(
        lock,
        candidate,
        selections=selections,
        state_owners=state_owners,
        runtime_overrides=runtime_overrides,
    )
    if losses:
        raise RegistryCutoverPreflightError(
            "migration_loss",
            "legacy state cannot be represented without migration loss",
            details={"loss_count": len(losses)},
        )

    # A serialize/strict-parse round trip proves the candidate satisfies the
    # durable Registry codec, not merely the looser in-memory dataclass types.
    canonical = parse_registry(
        json.loads(serialize_registry(candidate)),
        now=now,
        schema_version=REGISTRY_SCHEMA_VERSION,
    )

    comparison: RegistryShadowComparison | None = None
    if actual_registry is not None:
        comparison = compare_registry_snapshots(canonical, actual_registry)
        if not comparison.matches:
            raise RegistryCutoverPreflightError(
                "shadow_mismatch",
                "candidate Registry does not match the legacy authority",
                details={
                    "expected_plugin_count": comparison.expected_plugin_count,
                    "actual_plugin_count": comparison.actual_plugin_count,
                    "mismatch_counts": dict(comparison.mismatch_counts),
                },
            )

    return RegistryCutoverPreflightResult(
        snapshot=canonical,
        shadow_comparison=comparison,
    )


__all__ = [
    "RegistryCutoverPreflightError",
    "RegistryCutoverPreflightResult",
    "prepare_registry_cutover_preflight",
]
