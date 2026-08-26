"""Deterministic v2 → v3 migration for the plugin inventory.

v2 keeps two durable authorities:

* ``plugins.lock.json`` — a flat ``entries`` list of per-directory provenance;
* ``plugin_candidate_selections.json`` — ``selections`` (which candidate is
  chosen) plus ``state_owners`` (who may read the logical id's shared state).

v3 folds both into one :class:`PluginInventory` grouped by logical PluginId.
This module performs only the pure data transform: no filesystem access, no
clock, no scanner. The caller supplies already-parsed inputs and the timestamp
to stamp, which keeps the migration deterministic and independently testable.

Deliberate narrowings, both logged:

* An entry whose ``plugin_id`` is still ``""`` is grouped under its
  ``directory_name``, following the v2 placeholder convention (Req 4.3), so its
  provenance survives until the scanner learns the real id.
* A ``state_owners`` receipt naming a candidate that has no row at all is
  dropped rather than trusted. v2 soft-deletes rows instead of erasing them, so
  in practice the row is present with ``removed=True`` and the receipt is kept;
  a genuinely absent row means we cannot verify what we would be granting, and
  the codec fails closed on read the same way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

from plugin.logging_config import get_logger
from plugin.server.application.install_source.models import (
    CandidateRecord,
    CandidateRef,
    LockFile,
    PluginEntry,
    PluginInventory,
    StateOwnership,
)

if TYPE_CHECKING:
    from plugin.server.infrastructure.plugin_selections import PluginSelection

logger = get_logger("server.application.install_source.migration")

def migrate_lock_to_inventory(
    lock: LockFile,
    *,
    selections: Mapping[str, "PluginSelection"] | None = None,
    state_owners: Mapping[str, "PluginSelection"] | None = None,
    now: str,
) -> PluginInventory:
    """Fold a v2 lock plus its selection receipts into a v3 inventory.

    Args:
        lock: parsed ``plugins.lock.json`` (schema v1 or v2).
        selections: ``PluginSelectionStore.selections`` — the chosen candidate
            per logical id. ``None`` is treated as empty.
        state_owners: ``PluginSelectionStore.state_owners`` — the data-access
            receipt per logical id. ``None`` is treated as empty.
        now: timestamp to stamp on the resulting snapshot.

    Returns:
        A :class:`PluginInventory` at ``revision=1`` preserving every
        representable v2 field. ``created_at`` is carried over from the lock so
        the original First_Startup stamp is not lost.

    The transform is idempotent in the sense that re-running it on the same
    inputs yields an equal snapshot; it is not meant to be run against an
    already-migrated v3 document.
    """

    selections = selections or {}
    state_owners = state_owners or {}

    grouped = _group_candidates(lock)

    plugin_ids = set(grouped) | set(selections) | set(state_owners)
    entries: dict[str, PluginEntry] = {}
    for plugin_id in sorted(plugin_ids):
        entries[plugin_id] = _build_entry(
            plugin_id,
            candidates=grouped.get(plugin_id, ()),
            selection=selections.get(plugin_id),
            owner=state_owners.get(plugin_id),
        )

    return PluginInventory.build(
        entries,
        revision=1,
        updated_at=lock.updated_at or now,
        created_at=lock.created_at,
    )


def _group_candidates(lock: LockFile) -> dict[str, tuple[CandidateRecord, ...]]:
    """Group v2 entries by logical PluginId, keeping v2's placeholder rule."""

    grouped: dict[str, list[CandidateRecord]] = {}
    for entry in lock.entries:
        key = entry.plugin_id
        if not key:
            # Req 4.3: directory_name stands in for plugin_id until the
            # scanner reads the real id. Keep the row so provenance survives.
            key = entry.directory_name
            logger.info(
                "migration: entry %s has no plugin_id yet; grouping under its "
                "directory name until the scanner resolves it",
                entry.primary_key,
            )
        grouped.setdefault(key, []).append(CandidateRecord.from_lock_entry(entry))

    return {
        plugin_id: tuple(
            sorted(records, key=lambda c: (c.root_id, c.directory_name))
        )
        for plugin_id, records in grouped.items()
    }


def _build_entry(
    plugin_id: str,
    *,
    candidates: tuple[CandidateRecord, ...],
    selection: "PluginSelection | None",
    owner: "PluginSelection | None",
) -> PluginEntry:
    """Assemble one PluginEntry, enforcing the v3 entry invariants."""

    live_keys = {c.primary_key for c in candidates if not c.removed}
    all_keys = {c.primary_key for c in candidates}

    selected_ref: CandidateRef | None = None
    candidate_source = None
    if selection is not None:
        ref = CandidateRef(
            root_id=selection.candidate.root_id,
            directory_name=selection.candidate.directory_name,
        )
        if ref.primary_key in live_keys:
            selected_ref = ref
            candidate_source = selection.candidate_source
        else:
            logger.warning(
                "migration: plugin_id=%r selected %s which has no live row; "
                "dropping the selection so the Resolver picks again",
                plugin_id,
                ref.primary_key,
            )

    return PluginEntry(
        plugin_id=plugin_id,
        candidates=candidates,
        selected_candidate=selected_ref,
        candidate_source=candidate_source,
        # v2 has no durable enable intent; runtime overrides stay outside the
        # inventory, so every migrated entry starts as "intended to run".
        enabled=True,
        state_owner=_build_owner(plugin_id, owner=owner, known_keys=all_keys),
    )


def _build_owner(
    plugin_id: str,
    *,
    owner: "PluginSelection | None",
    known_keys: set[tuple[str, str]],
) -> StateOwnership | None:
    """Carry the data-access receipt over, failing closed when unverifiable.

    A receipt naming a soft-removed row is kept: losing the code must not drop
    the record of who owns the data. A receipt naming a row that is absent
    entirely is dropped, matching the codec's read-time rule.
    """

    if owner is None:
        return None

    ref = CandidateRef(
        root_id=owner.candidate.root_id,
        directory_name=owner.candidate.directory_name,
    )
    if ref.primary_key not in known_keys:
        logger.warning(
            "migration: state owner %s for plugin_id=%r has no row in the lock; "
            "dropping the receipt (fail closed)",
            ref.primary_key,
            plugin_id,
        )
        return None

    return StateOwnership(
        candidate=ref,
        state_scope=owner.state_scope,
        state_access_grant=owner.state_access_grant,
        release_chain_id=owner.release_chain_id,
        authorized_at=owner.authorized_at,
    )


def describe_migration_losses(
    lock: LockFile,
    inventory: PluginInventory,
    *,
    selections: Mapping[str, "PluginSelection"] | None = None,
    state_owners: Mapping[str, "PluginSelection"] | None = None,
) -> list[str]:
    """Return a human-readable list of everything the migration did not carry.

    Empty means the migration was lossless for every representable field. The
    caller is expected to log this and refuse to cut over on anything it did
    not already decide to accept.
    """

    selections = selections or {}
    state_owners = state_owners or {}
    losses: list[str] = []

    migrated_rows: set[tuple[str, str]] = set()
    for entry in inventory.plugins.values():
        migrated_rows.update(c.primary_key for c in entry.candidates)

    for lock_entry in lock.entries:
        if lock_entry.primary_key not in migrated_rows:
            losses.append(f"candidate row {lock_entry.primary_key} was not migrated")

    for plugin_id, selection in selections.items():
        entry = inventory.entry(plugin_id)
        expected = (selection.candidate.root_id, selection.candidate.directory_name)
        actual = (
            entry.selected_candidate.primary_key
            if entry is not None and entry.selected_candidate is not None
            else None
        )
        if actual != expected:
            losses.append(
                f"selection for {plugin_id!r} became {actual} (was {expected})"
            )

    for plugin_id, owner in state_owners.items():
        entry = inventory.entry(plugin_id)
        expected = (owner.candidate.root_id, owner.candidate.directory_name)
        actual = (
            entry.state_owner.candidate.primary_key
            if entry is not None and entry.state_owner is not None
            else None
        )
        if actual != expected:
            losses.append(
                f"state owner for {plugin_id!r} became {actual} (was {expected})"
            )

    if inventory.created_at != lock.created_at:
        losses.append("created_at was not preserved")

    return losses


__all__ = ["migrate_lock_to_inventory", "describe_migration_losses"]
