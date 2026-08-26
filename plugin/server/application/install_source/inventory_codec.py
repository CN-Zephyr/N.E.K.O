"""Schema v3 inventory codec: ``dict`` ↔ :class:`PluginInventory`.

The v3 document keeps every v1/v2 provenance field but regroups the flat
``entries`` list under its logical PluginId and folds in the selection and
state-ownership receipts that v2 stored in ``plugin_candidate_selections.json``.

Parsing is tolerant in the same way :func:`_parse_lock` is: a malformed
sub-field degrades to a safe default with a WARN rather than failing the whole
read. Only a structurally impossible document (``plugins`` present but not an
object) raises ``LOCK_FILE_CORRUPT``, which lets the caller back the file up
and re-seed.

Serialization is deterministic — plugins sorted by PluginId, candidates sorted
by ``(root_id, directory_name)`` — so an unchanged inventory re-serializes to
identical bytes.

This module deliberately depends on :mod:`manager` for the shared low-level
helpers rather than duplicating them. ``manager`` must therefore import this
module lazily (function-local) if it ever needs it, to avoid an import cycle.
"""

from __future__ import annotations

import json
from typing import Any

from plugin.logging_config import get_logger
from plugin.server.application.install_source.manager import (
    _LEGAL_CHANNELS,
    _LEGAL_REASONS,
    _LEGAL_ROOT_IDS,
    InstallSourceError,
    _normalize_ts,
    _parse_source_detail,
    _serialize_source_detail_for_json,
)
from plugin.server.application.install_source.models import (
    INVENTORY_SCHEMA_VERSION,
    CandidateRecord,
    CandidateRef,
    PluginEntry,
    PluginInventory,
    StateOwnership,
)

logger = get_logger("server.application.install_source.inventory_codec")

def parse_inventory(
    data: dict[str, Any],
    *,
    now: str,
    schema_version: int,
) -> PluginInventory:
    """Build a :class:`PluginInventory` from an already-decoded v3 document.

    ``data`` is the JSON object; ``schema_version`` is what the caller read
    from it (kept rather than pinned so a forward version survives a
    best-effort read). Raises :class:`InstallSourceError` with
    ``LOCK_FILE_CORRUPT`` only when ``plugins`` is present but not an object.
    """

    raw_plugins = data.get("plugins")
    if raw_plugins is None:
        raw_plugins = {}
    elif not isinstance(raw_plugins, dict):
        raise InstallSourceError(
            "LOCK_FILE_CORRUPT",
            "plugins.lock.json 'plugins' field is not an object "
            f"(got {type(raw_plugins).__name__})",
            details={"reason": "plugins_not_object"},
        )

    revision = _parse_revision(data.get("revision"))
    updated_at = _normalize_ts(data.get("updated_at"), now=now)
    raw_created_at = data.get("created_at")
    created_at = (
        _normalize_ts(raw_created_at, now=now) if raw_created_at is not None else None
    )

    entries: dict[str, PluginEntry] = {}
    for plugin_id, raw_entry in raw_plugins.items():
        if not isinstance(plugin_id, str) or not plugin_id:
            logger.warning("inventory: dropping entry with non-string PluginId")
            continue
        entry = _parse_entry(plugin_id, raw_entry, now=now)
        if entry is not None:
            entries[plugin_id] = entry

    return PluginInventory.build(
        entries,
        revision=revision,
        updated_at=updated_at,
        created_at=created_at,
        schema_version=schema_version,
    )


def _parse_revision(raw: Any) -> int:
    """Coerce ``revision`` to a positive int; anything else degrades to 1."""

    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        if raw is not None:
            logger.warning("inventory: invalid revision=%r, treating as 1", raw)
        return 1
    return raw


def _parse_entry(
    plugin_id: str,
    raw: Any,
    *,
    now: str,
) -> PluginEntry | None:
    """Parse one PluginEntry; ``None`` when the object is unusable."""

    if not isinstance(raw, dict):
        logger.warning(
            "inventory: entry for plugin_id=%r is not an object, dropping", plugin_id
        )
        return None

    raw_candidates = raw.get("candidates")
    if raw_candidates is None:
        raw_candidates = []
    elif not isinstance(raw_candidates, list):
        logger.warning(
            "inventory: 'candidates' for plugin_id=%r is not a list, treating as empty",
            plugin_id,
        )
        raw_candidates = []

    candidates: dict[tuple[str, str], CandidateRecord] = {}
    for raw_candidate in raw_candidates:
        candidate = _parse_candidate(raw_candidate, plugin_id=plugin_id, now=now)
        if candidate is None:
            continue
        # Same primary-key dedup rule as v2: keep the later-seen row.
        existing = candidates.get(candidate.primary_key)
        if existing is None or candidate.last_seen_at >= existing.last_seen_at:
            candidates[candidate.primary_key] = candidate

    ordered = tuple(
        sorted(candidates.values(), key=lambda c: (c.root_id, c.directory_name))
    )
    return _finish_entry(plugin_id, raw, ordered, now=now)


def _finish_entry(
    plugin_id: str,
    raw: dict[str, Any],
    candidates: tuple[CandidateRecord, ...],
    *,
    now: str,
) -> PluginEntry:
    """Attach selection / ownership / intent, enforcing the entry invariants."""

    selected = _parse_ref(raw.get("selected_candidate"), plugin_id=plugin_id)
    live_keys = {c.primary_key for c in candidates if not c.removed}
    if selected is not None and selected.primary_key not in live_keys:
        # A selection pointing at missing or retired code must not resurrect
        # it; drop the selection and let the pure Resolver pick again.
        logger.warning(
            "inventory: plugin_id=%r selects %s which is absent or removed; "
            "dropping selection",
            plugin_id,
            selected.primary_key,
        )
        selected = None

    candidate_source = raw.get("candidate_source")
    if candidate_source is not None and candidate_source not in _LEGAL_CHANNELS:
        logger.warning(
            "inventory: plugin_id=%r has illegal candidate_source=%r, dropping",
            plugin_id,
            candidate_source,
        )
        candidate_source = None

    raw_enabled = raw.get("enabled", True)
    enabled = raw_enabled if isinstance(raw_enabled, bool) else True

    # StateOwnership may legitimately name a removed candidate: losing the
    # code must not drop the data-ownership receipt.
    all_keys = {c.primary_key for c in candidates}
    owner = _parse_owner(
        raw.get("state_owner"), plugin_id=plugin_id, known_keys=all_keys, now=now
    )

    return PluginEntry(
        plugin_id=plugin_id,
        candidates=candidates,
        selected_candidate=selected,
        candidate_source=candidate_source,
        enabled=enabled,
        state_owner=owner,
    )


def _parse_candidate(
    raw: Any,
    *,
    plugin_id: str,
    now: str,
) -> CandidateRecord | None:
    """Parse one candidate row; ``None`` when the primary key is unusable."""

    if not isinstance(raw, dict):
        logger.warning(
            "inventory: candidate for plugin_id=%r is not an object, dropping",
            plugin_id,
        )
        return None

    root_id = raw.get("root_id")
    directory_name = raw.get("directory_name")
    if root_id not in _LEGAL_ROOT_IDS or not isinstance(directory_name, str) or not directory_name:
        logger.warning(
            "inventory: candidate for plugin_id=%r has an unusable primary key "
            "(root_id=%r, directory_name=%r), dropping",
            plugin_id,
            root_id,
            directory_name,
        )
        return None

    channel = raw.get("channel")
    if channel not in _LEGAL_CHANNELS:
        channel = "builtin" if root_id == "builtin" else "manual"

    reason = raw.get("reason")
    if reason not in _LEGAL_REASONS:
        reason = "user_requested"

    installed_at = _normalize_ts(raw.get("installed_at"), now=now)
    updated_at = _normalize_ts(raw.get("updated_at"), now=now)
    last_seen_at = _normalize_ts(raw.get("last_seen_at"), now=now)

    removed = bool(raw.get("removed", False))
    raw_removed_at = raw.get("removed_at")
    removed_at = (
        _normalize_ts(raw_removed_at, now=now)
        if removed and raw_removed_at is not None
        else None
    )

    return _finish_candidate(
        raw,
        root_id=root_id,
        directory_name=directory_name,
        channel=channel,
        reason=reason,
        installed_at=installed_at,
        updated_at=updated_at,
        last_seen_at=last_seen_at,
        removed=removed,
        removed_at=removed_at,
        now=now,
    )


def _finish_candidate(
    raw: dict[str, Any],
    *,
    root_id: str,
    directory_name: str,
    channel: str,
    reason: str,
    installed_at: str,
    updated_at: str,
    last_seen_at: str,
    removed: bool,
    removed_at: str | None,
    now: str,
) -> CandidateRecord:
    """Fill in the package-profile fields and channel-driven source detail."""

    source_detail = _parse_source_detail(
        channel,
        raw.get("source_detail"),
        key=(root_id, directory_name),
        installed_at=installed_at,
        now=now,
    )

    raw_package_id = raw.get("package_id")
    package_id = raw_package_id if isinstance(raw_package_id, str) else ""

    raw_profile_dir = raw.get("profile_dir")
    profile_dir = raw_profile_dir if isinstance(raw_profile_dir, str) else ""

    raw_profile_installed = raw.get("profile_installed")
    if raw_profile_installed is None:
        profile_installed: bool | None = None
    elif isinstance(raw_profile_installed, bool):
        profile_installed = raw_profile_installed
    else:
        logger.warning(
            "inventory: illegal profile_installed=%r for key=%s, treating as false",
            raw_profile_installed,
            (root_id, directory_name),
        )
        profile_installed = False

    return CandidateRecord(
        root_id=root_id,  # type: ignore[arg-type]
        directory_name=directory_name,
        channel=channel,  # type: ignore[arg-type]
        reason=reason,  # type: ignore[arg-type]
        installed_at=installed_at,
        updated_at=updated_at,
        last_seen_at=last_seen_at,
        removed=removed,
        removed_at=removed_at,
        source_detail=source_detail,
        package_id=package_id,
        profile_dir=profile_dir,
        profile_installed=profile_installed,
    )


def _parse_ref(raw: Any, *, plugin_id: str) -> CandidateRef | None:
    """Parse a ``{root_id, directory_name}`` pointer."""

    if raw is None:
        return None
    if not isinstance(raw, dict):
        logger.warning(
            "inventory: candidate reference for plugin_id=%r is not an object, dropping",
            plugin_id,
        )
        return None

    root_id = raw.get("root_id")
    directory_name = raw.get("directory_name")
    if root_id not in _LEGAL_ROOT_IDS or not isinstance(directory_name, str) or not directory_name:
        logger.warning(
            "inventory: candidate reference for plugin_id=%r is unusable "
            "(root_id=%r, directory_name=%r), dropping",
            plugin_id,
            root_id,
            directory_name,
        )
        return None
    return CandidateRef(root_id=root_id, directory_name=directory_name)  # type: ignore[arg-type]


def _parse_owner(
    raw: Any,
    *,
    plugin_id: str,
    known_keys: set[tuple[str, str]],
    now: str,
) -> StateOwnership | None:
    """Parse the state-ownership receipt.

    Fails closed: an owner naming a candidate this entry has never seen is
    dropped rather than trusted, because granting data access to unverified
    code is worse than re-authorising later.
    """

    if raw is None:
        return None
    if not isinstance(raw, dict):
        logger.warning(
            "inventory: state_owner for plugin_id=%r is not an object, dropping",
            plugin_id,
        )
        return None

    ref = _parse_ref(raw.get("candidate"), plugin_id=plugin_id)
    if ref is None:
        return None
    if ref.primary_key not in known_keys:
        logger.warning(
            "inventory: state_owner %s for plugin_id=%r is not a known candidate; "
            "dropping receipt (fail closed)",
            ref.primary_key,
            plugin_id,
        )
        return None

    return _finish_owner(raw, ref, now=now)


def _finish_owner(
    raw: dict[str, Any],
    ref: CandidateRef,
    *,
    now: str,
) -> StateOwnership:
    """Coerce the receipt's optional string fields."""

    def _opt_str(key: str) -> str | None:
        value = raw.get(key)
        return value if isinstance(value, str) and value else None

    raw_authorized_at = raw.get("authorized_at")
    authorized_at = (
        _normalize_ts(raw_authorized_at, now=now)
        if isinstance(raw_authorized_at, str) and raw_authorized_at
        else None
    )

    return StateOwnership(
        candidate=ref,
        state_scope=_opt_str("state_scope"),
        state_access_grant=_opt_str("state_access_grant"),
        release_chain_id=_opt_str("release_chain_id"),
        authorized_at=authorized_at,
    )


def serialize_inventory(inventory: PluginInventory) -> bytes:
    """Serialize a :class:`PluginInventory` to deterministic UTF-8 JSON bytes.

    ``schema_version`` is pinned to :data:`INVENTORY_SCHEMA_VERSION` — once we
    write through this function the v3 layout is the truth on disk.
    """

    out: dict[str, Any] = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "revision": inventory.revision,
    }
    if inventory.created_at is not None:
        out["created_at"] = inventory.created_at
    out["updated_at"] = inventory.updated_at

    out["plugins"] = {
        plugin_id: _serialize_entry(inventory.plugins[plugin_id])
        for plugin_id in sorted(inventory.plugins)
    }

    return json.dumps(out, ensure_ascii=False, indent=2, sort_keys=False).encode("utf-8")


def _serialize_entry(entry: PluginEntry) -> dict[str, Any]:
    """Serialize one PluginEntry with a fixed field order."""

    out: dict[str, Any] = {
        "candidates": [
            _serialize_candidate(candidate)
            for candidate in sorted(
                entry.candidates, key=lambda c: (c.root_id, c.directory_name)
            )
        ],
        "selected_candidate": _serialize_ref(entry.selected_candidate),
        "candidate_source": entry.candidate_source,
        "enabled": entry.enabled,
        "state_owner": _serialize_owner(entry.state_owner),
    }
    return out


def _serialize_candidate(candidate: CandidateRecord) -> dict[str, Any]:
    """Serialize one candidate, mirroring the v2 entry field order."""

    out: dict[str, Any] = {
        "root_id": candidate.root_id,
        "directory_name": candidate.directory_name,
        "channel": candidate.channel,
        "reason": candidate.reason,
        "installed_at": candidate.installed_at,
        "updated_at": candidate.updated_at,
        "last_seen_at": candidate.last_seen_at,
        "removed": candidate.removed,
    }
    if candidate.package_id:
        out["package_id"] = candidate.package_id
    if candidate.profile_dir:
        out["profile_dir"] = candidate.profile_dir
    if candidate.profile_installed is not None:
        out["profile_installed"] = candidate.profile_installed
    if candidate.removed:
        out["removed_at"] = candidate.removed_at
    out["source_detail"] = _serialize_source_detail_for_json(candidate.source_detail)
    return out


def _serialize_ref(ref: CandidateRef | None) -> dict[str, Any] | None:
    if ref is None:
        return None
    return {"root_id": ref.root_id, "directory_name": ref.directory_name}


def _serialize_owner(owner: StateOwnership | None) -> dict[str, Any] | None:
    if owner is None:
        return None
    return {
        "candidate": _serialize_ref(owner.candidate),
        "state_scope": owner.state_scope,
        "state_access_grant": owner.state_access_grant,
        "release_chain_id": owner.release_chain_id,
        "authorized_at": owner.authorized_at,
    }


__all__ = ["parse_inventory", "serialize_inventory"]
