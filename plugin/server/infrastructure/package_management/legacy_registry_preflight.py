"""Explicit-path, read-only loader for Registry cutover preflight inputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from plugin.server.application.install_source.models import (
    LockFile,
    PluginRegistrySnapshot,
)
from plugin.server.application.install_source.registry_preflight import (
    RegistryCutoverPreflightError,
    RegistryCutoverPreflightResult,
    prepare_registry_cutover_preflight,
)
from plugin.server.infrastructure.plugin_selections import (
    PluginSelectionReadError,
    PluginSelectionStore,
    _coerce_selections_with_status,
)
from plugin.server.infrastructure.runtime_overrides import (
    RuntimeOverrideReadError,
    _coerce_overrides_with_status,
)


def load_registry_cutover_preflight(
    lock: LockFile,
    *,
    selections_path: Path,
    runtime_overrides_path: Path,
    actual_registry: PluginRegistrySnapshot | None = None,
    now: str,
) -> RegistryCutoverPreflightResult:
    """Read legacy sidecar files without caches or global config mutation.

    Missing sidecars mean no explicit selection/owner or runtime override.
    Existing but malformed or partially invalid files block cutover in full;
    the legacy readers' best-effort subsets are never accepted as authority.
    """

    selection_bytes = _read_optional_bytes(
        selections_path,
        authority="candidate_selections",
    )
    runtime_bytes = _read_optional_bytes(
        runtime_overrides_path,
        authority="runtime_overrides",
    )

    return prepare_registry_cutover_preflight_from_bytes(
        lock,
        selections_bytes=selection_bytes,
        runtime_overrides_bytes=runtime_bytes,
        actual_registry=actual_registry,
        now=now,
    )


def prepare_registry_cutover_preflight_from_bytes(
    lock: LockFile,
    *,
    selections_bytes: bytes | None,
    runtime_overrides_bytes: bytes | None,
    actual_registry: PluginRegistrySnapshot | None = None,
    now: str,
) -> RegistryCutoverPreflightResult:
    """Strictly parse a single captured view of both legacy sidecars."""

    selection_document = _decode_optional_json_object(
        selections_bytes,
        authority="candidate_selections",
    )
    runtime_document = _decode_optional_json_object(
        runtime_overrides_bytes,
        authority="runtime_overrides",
    )

    selection_store = _strict_selection_store(selection_document)
    runtime_overrides = _strict_runtime_overrides(runtime_document)
    return prepare_registry_cutover_preflight(
        lock,
        selections=selection_store.selections,
        state_owners=selection_store.state_owners,
        runtime_overrides=runtime_overrides,
        actual_registry=actual_registry,
        now=now,
    )


def _read_optional_bytes(
    path: Path,
    *,
    authority: str,
) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RegistryCutoverPreflightError(
            "legacy_read_failed",
            f"cannot read legacy {authority} authority",
            details={"authority": authority},
        ) from exc



def _decode_optional_json_object(
    raw: bytes | None,
    *,
    authority: str,
) -> Mapping[str, object] | None:
    if raw is None:
        return None
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryCutoverPreflightError(
            "legacy_invalid_json",
            f"legacy {authority} authority is not valid UTF-8 JSON",
            details={"authority": authority},
        ) from exc
    if not isinstance(document, Mapping):
        raise RegistryCutoverPreflightError(
            "legacy_invalid_content",
            f"legacy {authority} authority must contain a JSON object",
            details={"authority": authority},
        )
    return document


def _strict_selection_store(
    document: Mapping[str, object] | None,
) -> PluginSelectionStore:
    if document is None:
        return PluginSelectionStore({}, {})
    try:
        store, invalid_content = _coerce_selections_with_status(document)
    except PluginSelectionReadError as exc:
        raise RegistryCutoverPreflightError(
            "legacy_invalid_content",
            "legacy candidate selection authority is not supported",
            details={"authority": "candidate_selections"},
        ) from exc
    if invalid_content:
        raise RegistryCutoverPreflightError(
            "legacy_invalid_content",
            "legacy candidate selection authority contains invalid content",
            details={"authority": "candidate_selections"},
        )
    return store


def _strict_runtime_overrides(
    document: Mapping[str, object] | None,
) -> dict[str, object]:
    if document is None:
        return {}
    try:
        overrides, invalid_content = _coerce_overrides_with_status(
            document,
            log_invalid=False,
        )
    except RuntimeOverrideReadError as exc:
        raise RegistryCutoverPreflightError(
            "legacy_invalid_content",
            "legacy runtime override authority is not supported",
            details={"authority": "runtime_overrides"},
        ) from exc
    if invalid_content:
        raise RegistryCutoverPreflightError(
            "legacy_invalid_content",
            "legacy runtime override authority contains invalid content",
            details={"authority": "runtime_overrides"},
        )
    return dict(overrides)


__all__ = [
    "load_registry_cutover_preflight",
    "prepare_registry_cutover_preflight_from_bytes",
]
