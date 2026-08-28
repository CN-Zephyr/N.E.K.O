"""Registry schema v1 codec: ``dict`` ↔ :class:`PluginRegistrySnapshot`.

The Registry document keeps every v1/v2 provenance field but regroups the flat
``entries`` list under its logical PluginId and folds in the selection and
state-ownership receipts that v2 stored in ``plugin_candidate_selections.json``.

Unlike the tolerant legacy lock reader, Registry v1 fails the entire read when
any durable field is malformed.  Migration is where legacy recovery belongs;
once the canonical Registry exists, silently dropping a candidate, override,
Selection, or StateOwner could change desired state or data authority.

Serialization is deterministic — plugins sorted by PluginId, candidates sorted
by ``(root_id, directory_name)`` — so an unchanged inventory re-serializes to
identical bytes.

This module deliberately depends on :mod:`manager` for the shared low-level
helpers rather than duplicating them. ``manager`` must therefore import this
module lazily (function-local) if it ever needs it, to avoid an import cycle.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

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
    REGISTRY_SCHEMA_VERSION,
    CandidateRecord,
    CandidateRef,
    PluginEntry,
    PluginRegistrySnapshot,
    StateOwnership,
)

def parse_registry(
    data: dict[str, Any],
    *,
    now: str,
    schema_version: int,
) -> PluginRegistrySnapshot:
    """Build a strict registry snapshot from an already-decoded document.

    Future or legacy schema versions are rejected before parsing so this codec
    can never best-effort read a document and later downgrade-write it.
    """

    if schema_version != REGISTRY_SCHEMA_VERSION:
        raise InstallSourceError(
            "UNSUPPORTED_REGISTRY_SCHEMA",
            "plugin registry schema is not supported: "
            f"expected={REGISTRY_SCHEMA_VERSION} actual={schema_version}",
            details={
                "expected_schema_version": REGISTRY_SCHEMA_VERSION,
                "actual_schema_version": schema_version,
            },
        )

    raw_plugins = data.get("plugins")
    if not isinstance(raw_plugins, dict):
        raise InstallSourceError(
            "LOCK_FILE_CORRUPT",
            "plugin registry 'plugins' field is not an object "
            f"(got {type(raw_plugins).__name__})",
            details={"reason": "plugins_not_object"},
        )

    revision = _parse_revision(data.get("revision"))
    updated_at = _parse_timestamp(
        data.get("updated_at"), field="updated_at", now=now
    )
    raw_created_at = data.get("created_at")
    created_at = (
        _parse_timestamp(raw_created_at, field="created_at", now=now)
        if raw_created_at is not None
        else None
    )

    entries: dict[str, PluginEntry] = {}
    for plugin_id, raw_entry in raw_plugins.items():
        if not isinstance(plugin_id, str) or not plugin_id:
            raise _corrupt(
                "plugin registry contains an invalid PluginId",
                reason="invalid_plugin_id",
            )
        entries[plugin_id] = _parse_entry(plugin_id, raw_entry, now=now)

    return PluginRegistrySnapshot.build(
        entries,
        revision=revision,
        updated_at=updated_at,
        created_at=created_at,
        schema_version=schema_version,
    )


def _parse_revision(raw: Any) -> int:
    """Read a positive CAS revision or reject the snapshot."""

    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise InstallSourceError(
            "LOCK_FILE_CORRUPT",
            f"plugin registry has invalid revision: {raw!r}",
            details={"reason": "invalid_revision", "revision": raw},
        )
    return raw


def _corrupt(message: str, *, reason: str, **details: Any) -> InstallSourceError:
    return InstallSourceError(
        "LOCK_FILE_CORRUPT",
        message,
        details={"reason": reason, **details},
    )


def _parse_timestamp(raw: Any, *, field: str, now: str) -> str:
    """Validate an ISO timestamp, then reuse the legacy canonicalizer."""

    if not isinstance(raw, str) or not raw:
        raise _corrupt(
            f"plugin registry has an invalid {field} timestamp",
            reason="invalid_timestamp",
            field=field,
        )
    try:
        datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, OverflowError) as exc:
        raise _corrupt(
            f"plugin registry has an invalid {field} timestamp",
            reason="invalid_timestamp",
            field=field,
        ) from exc
    return _normalize_ts(raw, now=now)


def _parse_entry(
    plugin_id: str,
    raw: Any,
    *,
    now: str,
) -> PluginEntry:
    """Parse one PluginEntry or reject the complete Registry snapshot."""

    if not isinstance(raw, dict):
        raise _corrupt(
            f"plugin registry entry for {plugin_id!r} is not an object",
            reason="entry_not_object",
            plugin_id=plugin_id,
        )

    raw_candidates = raw.get("candidates")
    if not isinstance(raw_candidates, list):
        raise _corrupt(
            f"plugin registry candidates for {plugin_id!r} is not a list",
            reason="candidates_not_list",
            plugin_id=plugin_id,
        )

    candidates: dict[tuple[str, str], CandidateRecord] = {}
    for raw_candidate in raw_candidates:
        candidate = _parse_candidate(raw_candidate, plugin_id=plugin_id, now=now)
        if candidate.primary_key in candidates:
            raise _corrupt(
                f"plugin registry contains duplicate candidate "
                f"{candidate.primary_key} for {plugin_id!r}",
                reason="duplicate_candidate",
                plugin_id=plugin_id,
                candidate=candidate.primary_key,
            )
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

    raw_selected = raw.get("selected_candidate")
    selected = _parse_ref(raw_selected, plugin_id=plugin_id)
    if raw_selected is not None and selected is None:
        raise InstallSourceError(
            "LOCK_FILE_CORRUPT",
            f"plugin registry has an invalid selection for {plugin_id!r}",
            details={"reason": "invalid_selection", "plugin_id": plugin_id},
        )
    live_keys = {c.primary_key for c in candidates if not c.removed}
    if selected is not None and selected.primary_key not in live_keys:
        raise InstallSourceError(
            "LOCK_FILE_CORRUPT",
            f"plugin registry selection for {plugin_id!r} names missing or "
            f"removed candidate {selected.primary_key}",
            details={
                "reason": "selection_not_live",
                "plugin_id": plugin_id,
                "candidate": selected.primary_key,
            },
        )

    candidate_source = raw.get("candidate_source")
    if candidate_source is not None and candidate_source not in _LEGAL_CHANNELS:
        raise _corrupt(
            f"plugin registry has an invalid candidate_source for {plugin_id!r}",
            reason="invalid_candidate_source",
            plugin_id=plugin_id,
        )

    raw_enabled = raw.get("enabled")
    if "enabled" in raw and raw_enabled is not None and not isinstance(raw_enabled, bool):
        raise _corrupt(
            f"plugin registry has an invalid enabled override for {plugin_id!r}",
            reason="invalid_runtime_intent",
            plugin_id=plugin_id,
            field="enabled",
        )
    enabled = raw_enabled if isinstance(raw_enabled, bool) else None
    raw_auto_start = raw.get("auto_start")
    if (
        "auto_start" in raw
        and raw_auto_start is not None
        and not isinstance(raw_auto_start, bool)
    ):
        raise _corrupt(
            f"plugin registry has an invalid auto_start override for {plugin_id!r}",
            reason="invalid_runtime_intent",
            plugin_id=plugin_id,
            field="auto_start",
        )
    auto_start = raw_auto_start if isinstance(raw_auto_start, bool) else None

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
        auto_start=auto_start,
        state_owner=owner,
    )


def _parse_candidate(
    raw: Any,
    *,
    plugin_id: str,
    now: str,
) -> CandidateRecord:
    """Parse one candidate row or reject the complete Registry snapshot."""

    if not isinstance(raw, dict):
        raise _corrupt(
            f"plugin registry candidate for {plugin_id!r} is not an object",
            reason="candidate_not_object",
            plugin_id=plugin_id,
        )

    root_id = raw.get("root_id")
    directory_name = raw.get("directory_name")
    if (
        root_id not in _LEGAL_ROOT_IDS
        or not isinstance(directory_name, str)
        or not directory_name
    ):
        raise _corrupt(
            f"plugin registry candidate for {plugin_id!r} has an invalid primary key",
            reason="invalid_candidate_key",
            plugin_id=plugin_id,
        )

    channel = raw.get("channel")
    if channel not in _LEGAL_CHANNELS:
        raise _corrupt(
            f"plugin registry candidate for {plugin_id!r} has an invalid channel",
            reason="invalid_candidate_channel",
            plugin_id=plugin_id,
        )

    reason = raw.get("reason")
    if reason not in _LEGAL_REASONS:
        raise _corrupt(
            f"plugin registry candidate for {plugin_id!r} has an invalid reason",
            reason="invalid_candidate_reason",
            plugin_id=plugin_id,
        )

    installed_at = _parse_timestamp(
        raw.get("installed_at"), field="candidate.installed_at", now=now
    )
    updated_at = _parse_timestamp(
        raw.get("updated_at"), field="candidate.updated_at", now=now
    )
    last_seen_at = _parse_timestamp(
        raw.get("last_seen_at"), field="candidate.last_seen_at", now=now
    )

    removed = raw.get("removed")
    if not isinstance(removed, bool):
        raise _corrupt(
            f"plugin registry candidate for {plugin_id!r} has an invalid removed flag",
            reason="invalid_candidate_removed",
            plugin_id=plugin_id,
        )
    raw_removed_at = raw.get("removed_at")
    if not removed and raw_removed_at is not None:
        raise _corrupt(
            f"plugin registry candidate for {plugin_id!r} has removed_at while live",
            reason="invalid_candidate_removed_at",
            plugin_id=plugin_id,
        )
    removed_at = (
        _parse_timestamp(
            raw_removed_at, field="candidate.removed_at", now=now
        )
        if removed and raw_removed_at is not None
        else None
    )

    return _finish_candidate(
        raw,
        plugin_id=plugin_id,
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
    plugin_id: str,
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

    raw_source_detail = raw.get("source_detail")
    _validate_source_detail(
        channel,
        raw_source_detail,
        plugin_id=plugin_id,
        now=now,
    )
    source_detail = _parse_source_detail(
        channel,
        raw_source_detail,
        key=(root_id, directory_name),
        installed_at=installed_at,
        now=now,
    )

    raw_package_id = raw.get("package_id")
    if raw_package_id is not None and not isinstance(raw_package_id, str):
        raise _corrupt(
            "plugin registry candidate has an invalid package_id",
            reason="invalid_candidate_package_id",
            plugin_id=plugin_id,
        )
    package_id = raw_package_id if isinstance(raw_package_id, str) else ""

    raw_profile_dir = raw.get("profile_dir")
    if raw_profile_dir is not None and not isinstance(raw_profile_dir, str):
        raise _corrupt(
            "plugin registry candidate has an invalid profile_dir",
            reason="invalid_candidate_profile_dir",
            plugin_id=plugin_id,
        )
    profile_dir = raw_profile_dir if isinstance(raw_profile_dir, str) else ""

    raw_profile_installed = raw.get("profile_installed")
    if raw_profile_installed is None:
        profile_installed: bool | None = None
    elif isinstance(raw_profile_installed, bool):
        profile_installed = raw_profile_installed
    else:
        raise _corrupt(
            "plugin registry candidate has an invalid profile_installed flag",
            reason="invalid_candidate_profile_installed",
            plugin_id=plugin_id,
        )

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


def _validate_source_detail(
    channel: str,
    raw: Any,
    *,
    plugin_id: str,
    now: str,
) -> None:
    """Reject malformed source evidence before the tolerant legacy parser runs."""

    if channel in {"builtin", "manual"}:
        if raw is not None:
            raise _corrupt(
                "plugin registry candidate has source_detail for a channel "
                "that cannot carry it",
                reason="invalid_source_detail",
                plugin_id=plugin_id,
            )
        return
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise _corrupt(
            "plugin registry candidate has a non-object source_detail",
            reason="invalid_source_detail",
            plugin_id=plugin_id,
        )

    if channel == "market":
        for field in (
            "plugin_market_id",
            "version",
            "package_url",
            "package_sha256",
        ):
            if not isinstance(raw.get(field), str):
                raise _corrupt(
                    f"plugin registry market source_detail has invalid {field}",
                    reason="invalid_source_detail",
                    plugin_id=plugin_id,
                    field=field,
                )
        for field in ("payload_hash", "previous_version"):
            if raw.get(field) is not None and not isinstance(raw.get(field), str):
                raise _corrupt(
                    f"plugin registry market source_detail has invalid {field}",
                    reason="invalid_source_detail",
                    plugin_id=plugin_id,
                    field=field,
                )
        if raw.get("channel") not in {"stable", "beta"}:
            raise _corrupt(
                "plugin registry market source_detail has invalid channel",
                reason="invalid_source_detail",
                plugin_id=plugin_id,
                field="channel",
            )
        _parse_timestamp(
            raw.get("published_at"),
            field="source_detail.published_at",
            now=now,
        )
        return

    for field in ("package_filename", "package_sha256"):
        if not isinstance(raw.get(field), str):
            raise _corrupt(
                f"plugin registry imported source_detail has invalid {field}",
                reason="invalid_source_detail",
                plugin_id=plugin_id,
                field=field,
            )

def _parse_ref(raw: Any, *, plugin_id: str) -> CandidateRef | None:
    """Parse a ``{root_id, directory_name}`` pointer."""

    if raw is None:
        return None
    if not isinstance(raw, dict):
        return None

    root_id = raw.get("root_id")
    directory_name = raw.get("directory_name")
    if (
        root_id not in _LEGAL_ROOT_IDS
        or not isinstance(directory_name, str)
        or not directory_name
    ):
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

    Fails closed: an owner naming a candidate this entry has never seen rejects
    the Registry, because granting data access to unverified code is worse than
    requiring explicit recovery.
    """

    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise InstallSourceError(
            "LOCK_FILE_CORRUPT",
            f"plugin registry state owner for {plugin_id!r} is not an object",
            details={"reason": "invalid_state_owner", "plugin_id": plugin_id},
        )

    ref = _parse_ref(raw.get("candidate"), plugin_id=plugin_id)
    if ref is None:
        raise InstallSourceError(
            "LOCK_FILE_CORRUPT",
            f"plugin registry state owner for {plugin_id!r} has no valid candidate",
            details={"reason": "invalid_state_owner", "plugin_id": plugin_id},
        )
    if ref.primary_key not in known_keys:
        raise InstallSourceError(
            "LOCK_FILE_CORRUPT",
            f"plugin registry state owner for {plugin_id!r} names unknown "
            f"candidate {ref.primary_key}",
            details={
                "reason": "state_owner_not_known",
                "plugin_id": plugin_id,
                "candidate": ref.primary_key,
            },
        )

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
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise _corrupt(
                f"plugin registry state owner has an invalid {key}",
                reason="invalid_state_owner",
                field=key,
            )
        return value

    raw_authorized_at = raw.get("authorized_at")
    authorized_at = (
        _parse_timestamp(
            raw_authorized_at, field="state_owner.authorized_at", now=now
        )
        if raw_authorized_at is not None
        else None
    )

    return StateOwnership(
        candidate=ref,
        state_scope=_opt_str("state_scope"),
        state_access_grant=_opt_str("state_access_grant"),
        release_chain_id=_opt_str("release_chain_id"),
        authorized_at=authorized_at,
    )


def serialize_registry(inventory: PluginRegistrySnapshot) -> bytes:
    """Serialize a registry snapshot to deterministic UTF-8 JSON bytes.

    Refuse non-current snapshots so a future schema can never be downgraded.
    """

    if inventory.schema_version != REGISTRY_SCHEMA_VERSION:
        raise InstallSourceError(
            "UNSUPPORTED_REGISTRY_SCHEMA",
            "refusing to serialize unsupported plugin registry schema: "
            f"{inventory.schema_version}",
            details={
                "expected_schema_version": REGISTRY_SCHEMA_VERSION,
                "actual_schema_version": inventory.schema_version,
            },
        )
    if (
        isinstance(inventory.revision, bool)
        or not isinstance(inventory.revision, int)
        or inventory.revision < 1
    ):
        raise InstallSourceError(
            "LOCK_FILE_CORRUPT",
            f"refusing to serialize invalid registry revision: {inventory.revision!r}",
            details={"reason": "invalid_revision"},
        )

    out: dict[str, Any] = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "revision": inventory.revision,
    }
    if inventory.created_at is not None:
        out["created_at"] = inventory.created_at
    out["updated_at"] = inventory.updated_at

    plugin_ids = tuple(inventory.plugins)
    if any(not isinstance(plugin_id, str) or not plugin_id for plugin_id in plugin_ids):
        raise _corrupt(
            "plugin registry snapshot contains an invalid PluginId",
            reason="invalid_plugin_id",
        )

    serialized_plugins: dict[str, Any] = {}
    for plugin_id in sorted(plugin_ids):
        entry = inventory.plugins[plugin_id]
        if plugin_id != entry.plugin_id:
            raise _corrupt(
                "plugin registry snapshot has an invalid or mismatched PluginId",
                reason="invalid_plugin_id",
                plugin_id=plugin_id,
            )
        serialized_plugins[plugin_id] = _serialize_entry(entry)
    out["plugins"] = serialized_plugins

    return json.dumps(out, ensure_ascii=False, indent=2, sort_keys=False).encode("utf-8")


def _serialize_entry(entry: PluginEntry) -> dict[str, Any]:
    """Serialize one PluginEntry with a fixed field order."""

    if entry.enabled is not None and not isinstance(entry.enabled, bool):
        raise TypeError("PluginEntry.enabled must be bool or None")
    if entry.auto_start is not None and not isinstance(entry.auto_start, bool):
        raise TypeError("PluginEntry.auto_start must be bool or None")

    out: dict[str, Any] = {
        "candidates": [
            _serialize_candidate(candidate)
            for candidate in sorted(
                entry.candidates, key=lambda c: (c.root_id, c.directory_name)
            )
        ],
        "selected_candidate": _serialize_ref(entry.selected_candidate),
        "candidate_source": entry.candidate_source,
    }
    if entry.enabled is not None:
        out["enabled"] = entry.enabled
    if entry.auto_start is not None:
        out["auto_start"] = entry.auto_start
    out["state_owner"] = _serialize_owner(entry.state_owner)
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


__all__ = ["parse_registry", "serialize_registry"]
