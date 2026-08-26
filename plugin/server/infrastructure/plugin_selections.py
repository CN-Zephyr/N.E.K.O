"""Durable code selection and legacy-shared-state ownership receipts."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from plugin.core.plugin_layout import resolve_plugin_layout
from plugin.logging_config import get_logger
from plugin.server.domain.plugin_candidates import (
    CandidateKey,
    CandidateSource,
    StateAccessGrant,
)

logger = get_logger("server.infrastructure.plugin_selections")

SELECTIONS_FILENAME = "plugin_candidate_selections.json"
SELECTIONS_SCHEMA_VERSION = 3

_cache_lock = threading.Lock()
_cache: PluginSelectionStore | None = None
_cache_write_blocked_by_invalid_content = False


@dataclass(frozen=True, slots=True)
class PluginSelection:
    """One exact candidate and its grant for the logical id's shared state."""

    candidate: CandidateKey
    candidate_source: CandidateSource | None
    state_scope: str | None
    state_access_grant: StateAccessGrant | None
    release_chain_id: str | None = None
    authorized_at: str | None = None

    @property
    def has_state_access_grant(self) -> bool:
        if self.candidate.root_id == "builtin":
            return self.state_access_grant == "builtin"
        return (
            self.state_scope == "legacy_shared"
            and self.state_access_grant
            in {"initial_identity", "trusted_market_chain", "user_authorized"}
        )


@dataclass(frozen=True, slots=True)
class PluginSelectionStore:
    """Atomic snapshot of code choices and non-deletable state ownership."""

    selections: dict[str, PluginSelection]
    state_owners: dict[str, PluginSelection]


class PluginSelectionPersistenceError(OSError):
    """Base error for durable candidate-selection storage failures."""


class PluginSelectionReadError(PluginSelectionPersistenceError):
    """The persisted candidate selections could not be read safely."""


class PluginSelectionWriteError(PluginSelectionPersistenceError):
    """Candidate selections could not be committed to durable storage."""


def _coerce_records(
    raw_records: object,
    *,
    legacy_keys_only: bool,
) -> tuple[dict[str, PluginSelection], bool]:
    if not isinstance(raw_records, Mapping):
        return {}, True

    invalid_content_found = False
    result: dict[str, PluginSelection] = {}
    for plugin_id, value in raw_records.items():
        if not isinstance(plugin_id, str) or not plugin_id or not isinstance(value, Mapping):
            invalid_content_found = True
            continue
        allowed_keys = {"root_id", "directory_name"}
        if not legacy_keys_only:
            allowed_keys.update(
                {
                    "candidate_source",
                    "state_scope",
                    "state_access_grant",
                    "release_chain_id",
                    "authorized_at",
                }
            )
        if set(value) != allowed_keys:
            invalid_content_found = True
            continue
        root_id = value.get("root_id")
        directory_name = value.get("directory_name")
        if (
            root_id not in {"builtin", "user"}
            or not isinstance(directory_name, str)
            or not _is_safe_directory_name(directory_name)
        ):
            invalid_content_found = True
            continue
        candidate = CandidateKey(root_id=root_id, directory_name=directory_name)
        if legacy_keys_only:
            result[plugin_id] = PluginSelection(
                candidate=candidate,
                candidate_source="builtin" if root_id == "builtin" else None,
                state_scope="legacy_shared" if root_id == "builtin" else None,
                state_access_grant="builtin" if root_id == "builtin" else None,
            )
            continue

        candidate_source = value.get("candidate_source")
        state_scope = value.get("state_scope")
        state_access_grant = value.get("state_access_grant")
        release_chain_id = value.get("release_chain_id")
        authorized_at = value.get("authorized_at")
        if not _valid_state_receipt_fields(
            root_id=root_id,
            candidate_source=candidate_source,
            state_scope=state_scope,
            state_access_grant=state_access_grant,
            release_chain_id=release_chain_id,
            authorized_at=authorized_at,
        ):
            invalid_content_found = True
            continue
        result[plugin_id] = PluginSelection(
            candidate=candidate,
            candidate_source=candidate_source,
            state_scope=state_scope,
            state_access_grant=state_access_grant,
            release_chain_id=release_chain_id or None,
            authorized_at=authorized_at or None,
        )
    return result, invalid_content_found


def _coerce_selections_with_status(
    raw: object,
) -> tuple[PluginSelectionStore, bool]:
    if not isinstance(raw, Mapping):
        raise PluginSelectionReadError(
            f"{SELECTIONS_FILENAME} must contain a JSON object"
        )
    schema_version = raw.get("schema_version")
    if schema_version not in {1, 2, SELECTIONS_SCHEMA_VERSION}:
        raise PluginSelectionReadError(
            f"{SELECTIONS_FILENAME} has an unsupported schema version"
        )

    expected_top_level = {"schema_version", "selections"}
    if schema_version == SELECTIONS_SCHEMA_VERSION:
        expected_top_level.add("state_owners")
    invalid_content_found = set(raw) != expected_top_level
    selections, invalid_selections = _coerce_records(
        raw.get("selections"),
        legacy_keys_only=schema_version == 1,
    )
    invalid_content_found = invalid_content_found or invalid_selections

    if schema_version == SELECTIONS_SCHEMA_VERSION:
        state_owners, invalid_owners = _coerce_records(
            raw.get("state_owners"),
            legacy_keys_only=False,
        )
        invalid_content_found = invalid_content_found or invalid_owners
    else:
        # v1/v2 had no independent owner ledger. Promote valid grants so
        # clearing code selection cannot erase data-ownership history.
        state_owners = {
            plugin_id: selection
            for plugin_id, selection in selections.items()
            if selection.has_state_access_grant
        }

    return PluginSelectionStore(selections, state_owners), invalid_content_found


def _load_from_disk() -> PluginSelectionStore:
    global _cache_write_blocked_by_invalid_content
    try:
        from utils.config_manager import get_config_manager

        raw = get_config_manager().load_json_config(SELECTIONS_FILENAME)
    except FileNotFoundError:
        _cache_write_blocked_by_invalid_content = False
        return PluginSelectionStore({}, {})
    except Exception as exc:
        logger.error(
            "Failed to load plugin candidate selections from {}: {}",
            SELECTIONS_FILENAME,
            exc,
        )
        raise PluginSelectionReadError(
            f"Failed to load plugin candidate selections from {SELECTIONS_FILENAME}"
        ) from exc

    store, invalid_content_found = _coerce_selections_with_status(raw)
    _cache_write_blocked_by_invalid_content = invalid_content_found
    return store


def _record_payload(selection: PluginSelection) -> dict[str, object]:
    return {
        "root_id": selection.candidate.root_id,
        "directory_name": selection.candidate.directory_name,
        "candidate_source": selection.candidate_source,
        "state_scope": selection.state_scope,
        "state_access_grant": selection.state_access_grant,
        "release_chain_id": selection.release_chain_id,
        "authorized_at": selection.authorized_at,
    }


def _save_to_disk(store: PluginSelectionStore) -> None:
    payload = {
        "schema_version": SELECTIONS_SCHEMA_VERSION,
        "selections": {
            plugin_id: _record_payload(selection)
            for plugin_id, selection in sorted(store.selections.items())
        },
        "state_owners": {
            plugin_id: _record_payload(owner)
            for plugin_id, owner in sorted(store.state_owners.items())
        },
    }
    try:
        from utils.config_manager import get_config_manager

        get_config_manager().save_json_config(SELECTIONS_FILENAME, payload)
    except Exception as exc:
        logger.error(
            "Failed to persist plugin candidate selections to {}: {}",
            SELECTIONS_FILENAME,
            exc,
        )
        raise PluginSelectionWriteError(
            f"Failed to persist plugin candidate selections to {SELECTIONS_FILENAME}"
        ) from exc


def _get_store() -> PluginSelectionStore:
    global _cache
    if _cache is None:
        _cache = _load_from_disk()
    return _cache


def load_plugin_selections() -> dict[str, CandidateKey]:
    """Return an immutable-value snapshot, loading it on first access."""

    with _cache_lock:
        return {
            plugin_id: selection.candidate
            for plugin_id, selection in _get_store().selections.items()
        }


def load_plugin_selection_records() -> dict[str, PluginSelection]:
    with _cache_lock:
        return dict(_get_store().selections)


def get_plugin_selection(plugin_id: str) -> CandidateKey | None:
    if not plugin_id:
        return None
    try:
        return load_plugin_selections().get(plugin_id)
    except PluginSelectionReadError as exc:
        logger.warning(
            "Ignoring unreadable plugin candidate selections for plugin {}: {}",
            plugin_id,
            exc,
        )
        return None


def get_plugin_selection_record(plugin_id: str) -> PluginSelection | None:
    if not plugin_id:
        return None
    try:
        return load_plugin_selection_records().get(plugin_id)
    except PluginSelectionReadError as exc:
        logger.warning(
            "Ignoring unreadable plugin candidate selections for plugin {}: {}",
            plugin_id,
            exc,
        )
        return None


def get_plugin_state_owner(plugin_id: str) -> PluginSelection | None:
    """Return the last committed state owner, independent of code selection."""

    if not plugin_id:
        return None
    try:
        with _cache_lock:
            return _get_store().state_owners.get(plugin_id)
    except PluginSelectionReadError as exc:
        logger.warning(
            "Ignoring unreadable plugin state owner for plugin {}: {}",
            plugin_id,
            exc,
        )
        return None


def set_plugin_selection(
    plugin_id: str,
    candidate: CandidateKey,
    *,
    candidate_source: CandidateSource | None = None,
    state_access_grant: StateAccessGrant | None = None,
    release_chain_id: str | None = None,
    authorized_at: str | None = None,
) -> None:
    """Atomically commit code intent and the corresponding state owner."""

    if not plugin_id:
        return
    global _cache
    with _cache_lock:
        store = _get_store()
        if candidate.root_id == "builtin":
            candidate_source = "builtin"
            state_access_grant = "builtin"
            release_chain_id = None
            authorized_at = None
        if not _valid_state_receipt_fields(
            root_id=candidate.root_id,
            candidate_source=candidate_source,
            state_scope="legacy_shared",
            state_access_grant=state_access_grant,
            release_chain_id=release_chain_id,
            authorized_at=authorized_at,
        ):
            raise PluginSelectionWriteError(
                "Refusing to persist a candidate without a valid shared-state grant"
            )
        selection = PluginSelection(
            candidate=candidate,
            candidate_source=candidate_source,
            state_scope="legacy_shared",
            state_access_grant=state_access_grant,
            release_chain_id=release_chain_id or None,
            authorized_at=authorized_at or None,
        )
        if (
            store.selections.get(plugin_id) == selection
            and store.state_owners.get(plugin_id) == selection
        ):
            return
        _ensure_write_allowed()
        updated = PluginSelectionStore(
            selections={**store.selections, plugin_id: selection},
            state_owners={**store.state_owners, plugin_id: selection},
        )
        _save_to_disk(updated)
        _cache = updated


def clear_plugin_selection(plugin_id: str) -> None:
    if not plugin_id:
        return
    global _cache
    with _cache_lock:
        store = _get_store()
        if plugin_id not in store.selections:
            return
        _ensure_write_allowed()
        selections = dict(store.selections)
        selections.pop(plugin_id, None)
        updated = PluginSelectionStore(selections, dict(store.state_owners))
        _save_to_disk(updated)
        _cache = updated


def clear_plugin_selection_if_matches(
    plugin_id: str,
    candidate: CandidateKey,
) -> bool:
    """Clear code intent while deliberately retaining state ownership."""

    if not plugin_id:
        return False
    global _cache
    with _cache_lock:
        store = _get_store()
        current = store.selections.get(plugin_id)
        if current is None or current.candidate != candidate:
            return False
        _ensure_write_allowed()
        selections = dict(store.selections)
        selections.pop(plugin_id, None)
        updated = PluginSelectionStore(selections, dict(store.state_owners))
        _save_to_disk(updated)
        _cache = updated
        return True


def legacy_shared_state_exists(plugin_id: str) -> bool:
    """Probe only layout paths; never open or enumerate user state contents."""

    layout = resolve_plugin_layout(plugin_id, Path("."))
    return any(
        path.exists()
        for path in (layout.config_path, layout.data_dir, layout.cache_dir)
    )


def reset_cache_for_testing() -> None:
    global _cache, _cache_write_blocked_by_invalid_content
    with _cache_lock:
        _cache = None
        _cache_write_blocked_by_invalid_content = False


def _ensure_write_allowed() -> None:
    if _cache_write_blocked_by_invalid_content:
        raise PluginSelectionWriteError(
            f"Refusing to overwrite {SELECTIONS_FILENAME} because it contains invalid content"
        )


def _valid_state_receipt_fields(
    *,
    root_id: object,
    candidate_source: object,
    state_scope: object,
    state_access_grant: object,
    release_chain_id: object,
    authorized_at: object,
) -> bool:
    return not (
        candidate_source not in {"builtin", "manual", "imported", "market"}
        or state_scope != "legacy_shared"
        or state_access_grant
        not in {"builtin", "initial_identity", "trusted_market_chain", "user_authorized"}
        or (release_chain_id is not None and not isinstance(release_chain_id, str))
        or (authorized_at is not None and not isinstance(authorized_at, str))
        or (state_access_grant == "user_authorized" and not authorized_at)
        or (state_access_grant == "builtin") != (root_id == "builtin")
        or (candidate_source == "builtin") != (root_id == "builtin")
        or (candidate_source == "market") != bool(release_chain_id)
        or (
            state_access_grant == "trusted_market_chain"
            and candidate_source != "market"
        )
        or (state_access_grant != "user_authorized" and authorized_at is not None)
    )


def _is_safe_directory_name(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
    )


__all__ = [
    "PluginSelection",
    "PluginSelectionPersistenceError",
    "PluginSelectionReadError",
    "PluginSelectionStore",
    "PluginSelectionWriteError",
    "SELECTIONS_FILENAME",
    "clear_plugin_selection",
    "clear_plugin_selection_if_matches",
    "get_plugin_selection",
    "get_plugin_selection_record",
    "get_plugin_state_owner",
    "legacy_shared_state_exists",
    "load_plugin_selection_records",
    "load_plugin_selections",
    "reset_cache_for_testing",
    "set_plugin_selection",
]
