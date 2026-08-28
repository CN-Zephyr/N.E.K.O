"""Privacy-safe shadow comparison for the Registry cutover.

The cutover runner can build an expected Registry snapshot from the three
legacy authorities, load a proposed ``plugin_registry.json``, and compare the
two without logging paths, package URLs, plugin ids, or state receipts.  This
module is deliberately pure: it performs no filesystem I/O and never mutates
either snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping

from plugin.server.application.install_source.models import (
    LockFile,
    PluginEntry,
    PluginRegistrySnapshot,
)
from plugin.server.application.install_source.registry_migration import (
    migrate_legacy_state_to_registry,
)

if TYPE_CHECKING:
    from plugin.server.infrastructure.plugin_selections import PluginSelection


@dataclass(frozen=True, slots=True)
class RegistryShadowComparison:
    """Aggregate-only comparison result safe for operational logs."""

    matches: bool
    expected_plugin_count: int
    actual_plugin_count: int
    mismatch_counts: Mapping[str, int]

    @classmethod
    def build(
        cls,
        *,
        expected_plugin_count: int,
        actual_plugin_count: int,
        mismatch_counts: Mapping[str, int],
    ) -> "RegistryShadowComparison":
        normalized = {
            category: count
            for category, count in sorted(mismatch_counts.items())
            if count > 0
        }
        return cls(
            matches=not normalized,
            expected_plugin_count=expected_plugin_count,
            actual_plugin_count=actual_plugin_count,
            mismatch_counts=MappingProxyType(normalized),
        )


def compare_registry_snapshots(
    expected: PluginRegistrySnapshot,
    actual: PluginRegistrySnapshot,
) -> RegistryShadowComparison:
    """Compare authority fields while ignoring Registry metadata revisions."""

    expected_ids = set(expected.plugins)
    actual_ids = set(actual.plugins)
    mismatch_counts: dict[str, int] = {}
    _record_count(mismatch_counts, "missing_plugins", len(expected_ids - actual_ids))
    _record_count(
        mismatch_counts,
        "unexpected_plugins",
        len(actual_ids - expected_ids),
    )

    for plugin_id in expected_ids & actual_ids:
        expected_entry = expected.plugins[plugin_id]
        actual_entry = actual.plugins[plugin_id]
        _compare_entry(expected_entry, actual_entry, mismatch_counts)

    return RegistryShadowComparison.build(
        expected_plugin_count=len(expected_ids),
        actual_plugin_count=len(actual_ids),
        mismatch_counts=mismatch_counts,
    )


def compare_legacy_state_to_registry(
    lock: LockFile,
    actual: PluginRegistrySnapshot,
    *,
    selections: Mapping[str, "PluginSelection"] | None = None,
    state_owners: Mapping[str, "PluginSelection"] | None = None,
    runtime_overrides: Mapping[str, object] | None = None,
    now: str,
) -> RegistryShadowComparison:
    """Project legacy state in memory and compare it to a Registry snapshot."""

    expected = migrate_legacy_state_to_registry(
        lock,
        selections=selections,
        state_owners=state_owners,
        runtime_overrides=runtime_overrides,
        now=now,
    )
    return compare_registry_snapshots(expected, actual)


def _compare_entry(
    expected: PluginEntry,
    actual: PluginEntry,
    counts: dict[str, int],
) -> None:
    if expected.candidates != actual.candidates:
        _record_count(counts, "candidates", 1)
    if (
        expected.selected_candidate != actual.selected_candidate
        or expected.candidate_source != actual.candidate_source
    ):
        _record_count(counts, "selection", 1)
    if (
        expected.enabled != actual.enabled
        or expected.auto_start != actual.auto_start
    ):
        _record_count(counts, "runtime_intent", 1)
    if expected.state_owner != actual.state_owner:
        _record_count(counts, "state_owner", 1)


def _record_count(counts: dict[str, int], category: str, amount: int) -> None:
    if amount > 0:
        counts[category] = counts.get(category, 0) + amount


__all__ = [
    "RegistryShadowComparison",
    "compare_legacy_state_to_registry",
    "compare_registry_snapshots",
]
