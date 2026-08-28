"""Crash-recoverable one-time initialization for ``plugin_registry.json``.

This module is deliberately not wired into server startup yet.  Callers must
provide the existing cross-process plugin operation lock and a snapshot
provider.  All legacy bytes are captured under that lock, backed up without
modifying their sources, and only then used to atomically initialize or verify
the Registry.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import AsyncContextManager, Literal, Mapping, Protocol

from plugin.server.application.install_source.manager import (
    InstallSourceError,
    _serialize_lock,
)
from plugin.server.application.install_source.models import (
    LockFile,
    PluginRegistrySnapshot,
)
from plugin.server.application.install_source.registry_preflight import (
    RegistryCutoverPreflightError,
)
from plugin.server.application.install_source.registry_codec import (
    parse_registry,
    serialize_registry,
)
from plugin.server.application.install_source.registry_shadow import (
    compare_registry_snapshots,
)

from .atomic_files import atomic_write_bytes
from .json_registry import JsonPluginRegistry
from .legacy_registry_preflight import (
    prepare_registry_cutover_preflight_from_bytes,
)

_BACKUP_SCHEMA_VERSION = 1
_BACKUP_DIRECTORY_NAME = ".plugin-registry-v1-cutover-backup"
_FAILURE_BACKUP_DIRECTORY_NAME = ".plugin-registry-v1-cutover-failure-backup"
_BACKUP_MANIFEST_NAME = "manifest.json"
_INITIAL_REGISTRY_NAME = "initial_registry.json"
_CUTOVER_COMMIT_NAME = "cutover_committed.json"


class RegistryCutoverOperationLock(Protocol):
    """Narrow view of the existing async plugin operation lock."""

    def hold(self) -> AsyncContextManager[None]: ...


class RegistryCutoverInitializationError(RuntimeError):
    """The one-time cutover initialization cannot safely continue."""

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
class RegistryCutoverPaths:
    """Validated locations for the three legacy authorities and Registry."""

    install_source: Path
    candidate_selections: Path
    runtime_overrides: Path
    registry: Path

    def __post_init__(self) -> None:
        resolved = {
            "install_source": self.install_source.resolve(strict=False),
            "candidate_selections": self.candidate_selections.resolve(strict=False),
            "runtime_overrides": self.runtime_overrides.resolve(strict=False),
            "registry": self.registry.resolve(strict=False),
        }
        expected_names = {
            "install_source": "plugins.lock.json",
            "candidate_selections": "plugin_candidate_selections.json",
            "runtime_overrides": "plugin_runtime_overrides.json",
            "registry": "plugin_registry.json",
        }
        for authority, path in resolved.items():
            if path.name != expected_names[authority]:
                raise ValueError(
                    f"{authority} path must end with {expected_names[authority]!r}"
                )
            object.__setattr__(self, authority, path)
        if len(set(resolved.values())) != len(resolved):
            raise ValueError("Registry cutover paths must be distinct")

    @property
    def backup_directory(self) -> Path:
        return self.registry.parent / _BACKUP_DIRECTORY_NAME

    @property
    def backup_manifest(self) -> Path:
        return self.backup_directory / _BACKUP_MANIFEST_NAME

    @property
    def initial_registry_backup(self) -> Path:
        return self.backup_directory / _INITIAL_REGISTRY_NAME

    @property
    def cutover_commit(self) -> Path:
        return self.backup_directory / _CUTOVER_COMMIT_NAME

    @property
    def failure_backup_directory(self) -> Path:
        return self.registry.parent / _FAILURE_BACKUP_DIRECTORY_NAME

    @property
    def failure_backup_manifest(self) -> Path:
        return self.failure_backup_directory / _BACKUP_MANIFEST_NAME


@dataclass(frozen=True, slots=True)
class RegistryCutoverInitializationResult:
    """Successful atomic initialization or recovery of an existing Registry."""

    status: Literal["initialized", "resumed"]
    snapshot: PluginRegistrySnapshot
    backup_manifest: Path


@dataclass(frozen=True, slots=True)
class _CapturedLegacyState:
    lock: bytes
    selections: bytes | None
    runtime_overrides: bytes | None


async def initialize_registry_cutover(
    *,
    paths: RegistryCutoverPaths,
    registry: JsonPluginRegistry,
    operation_lock: RegistryCutoverOperationLock,
    lock_snapshot_provider: Callable[[], LockFile],
    now: str,
) -> RegistryCutoverInitializationResult:
    """Initialize or recover the Registry under the plugin operation lock."""

    if registry.path != paths.registry:
        raise ValueError("JsonPluginRegistry path does not match cutover paths")

    async with operation_lock.hold():
        return await asyncio.to_thread(
            _initialize_registry_cutover_locked,
            paths=paths,
            registry=registry,
            lock_snapshot_provider=lock_snapshot_provider,
            now=now,
        )


def _initialize_registry_cutover_locked(
    *,
    paths: RegistryCutoverPaths,
    registry: JsonPluginRegistry,
    lock_snapshot_provider: Callable[[], LockFile],
    now: str,
) -> RegistryCutoverInitializationResult:
    _require_safe_backup_location(paths)
    committed = _resume_committed_cutover(paths, registry, now=now)
    if committed is not None:
        return committed

    lock = lock_snapshot_provider()
    captured = _capture_legacy_state(paths)
    _require_current_canonical_lock(lock, captured.lock)

    existing_registry: PluginRegistrySnapshot | None = None
    if paths.registry.exists():
        try:
            existing_registry = registry.load()
        except InstallSourceError as exc:
            raise RegistryCutoverInitializationError(
                "registry_read_only_degrade",
                "existing Registry cannot be used safely; refusing legacy fallback",
                details={"registry_error": exc.code},
            ) from exc
    try:
        preflight = prepare_registry_cutover_preflight_from_bytes(
            lock,
            selections_bytes=captured.selections,
            runtime_overrides_bytes=captured.runtime_overrides,
            actual_registry=existing_registry,
            now=now,
        )
    except RegistryCutoverPreflightError as exc:
        if exc.reason in {"legacy_invalid_json", "legacy_invalid_content"}:
            _ensure_failed_preflight_backup(paths, captured, exc)
        raise
    manifest_payload = _ensure_backup_set(paths, captured)

    existed_before_initialize = existing_registry is not None
    initialized = registry.initialize(preflight.snapshot)
    # ``initialize`` can observe a file created after the first existence
    # check.  Always re-run the hard gate on the exact snapshot it returned.
    comparison = compare_registry_snapshots(preflight.snapshot, initialized)
    if not comparison.matches:
        raise RegistryCutoverPreflightError(
            "shadow_mismatch",
            "initialized Registry does not match the legacy authority",
            details={
                "expected_plugin_count": comparison.expected_plugin_count,
                "actual_plugin_count": comparison.actual_plugin_count,
                "mismatch_counts": dict(comparison.mismatch_counts),
            },
        )

    initial_registry_payload = serialize_registry(initialized)
    _create_or_verify_backup(
        paths.initial_registry_backup,
        initial_registry_payload,
        authority="initial_registry",
    )
    commit_payload = _canonical_json(
        {
            "schema_version": _BACKUP_SCHEMA_VERSION,
            "backup_manifest_sha256": _sha256(manifest_payload),
            "initial_registry_sha256": _sha256(initial_registry_payload),
        }
    )
    _create_or_verify_backup(
        paths.cutover_commit,
        commit_payload,
        authority="cutover_commit",
    )

    return RegistryCutoverInitializationResult(
        status="resumed" if existed_before_initialize else "initialized",
        snapshot=initialized,
        backup_manifest=paths.backup_manifest,
    )


def _capture_legacy_state(paths: RegistryCutoverPaths) -> _CapturedLegacyState:
    return _CapturedLegacyState(
        lock=_read_required(paths.install_source, authority="install_source"),
        selections=_read_optional(
            paths.candidate_selections,
            authority="candidate_selections",
        ),
        runtime_overrides=_read_optional(
            paths.runtime_overrides,
            authority="runtime_overrides",
        ),
    )


def _read_required(path: Path, *, authority: str) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise RegistryCutoverInitializationError(
            "legacy_authority_missing",
            f"required legacy {authority} authority is missing",
            details={"authority": authority},
        ) from exc
    except OSError as exc:
        raise RegistryCutoverInitializationError(
            "legacy_read_failed",
            f"cannot read legacy {authority} authority",
            details={"authority": authority},
        ) from exc


def _read_optional(path: Path, *, authority: str) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RegistryCutoverInitializationError(
            "legacy_read_failed",
            f"cannot read legacy {authority} authority",
            details={"authority": authority},
        ) from exc


def _require_current_canonical_lock(lock: LockFile, raw: bytes) -> None:
    if lock.schema_version != 2:
        raise RegistryCutoverPreflightError(
            "unsupported_lock_schema",
            "cutover requires a reconciled plugins.lock.json schema v2 snapshot",
            details={"actual_schema_version": lock.schema_version},
        )
    if _serialize_lock(lock) != raw:
        raise RegistryCutoverInitializationError(
            "legacy_snapshot_stale",
            "reconciled lock snapshot does not match canonical disk bytes",
            details={"authority": "install_source"},
        )


def _ensure_backup_set(
    paths: RegistryCutoverPaths,
    captured: _CapturedLegacyState,
) -> bytes:
    authorities = {
        "install_source": ("install_source.json", captured.lock),
        "candidate_selections": ("candidate_selections.json", captured.selections),
        "runtime_overrides": ("runtime_overrides.json", captured.runtime_overrides),
    }
    manifest = {
        "schema_version": _BACKUP_SCHEMA_VERSION,
        "authorities": {
            authority: {
                "backup_file": backup_name,
                "present": payload is not None,
                "sha256": _sha256(payload) if payload is not None else None,
            }
            for authority, (backup_name, payload) in authorities.items()
        },
    }
    manifest_payload = _canonical_json(manifest)

    for authority, (backup_name, payload) in authorities.items():
        backup_path = paths.backup_directory / backup_name
        if payload is None:
            if backup_path.exists():
                raise _backup_conflict(authority)
            continue
        _create_or_verify_backup(backup_path, payload, authority=authority)

    _create_or_verify_backup(
        paths.backup_manifest,
        manifest_payload,
        authority="backup_manifest",
    )
    return manifest_payload


def _ensure_failed_preflight_backup(
    paths: RegistryCutoverPaths,
    captured: _CapturedLegacyState,
    error: RegistryCutoverPreflightError,
) -> None:
    """Preserve one corrupt legacy capture without modifying its authorities."""

    _require_plain_directory_path(
        paths.failure_backup_directory,
        reason="failure_backup_path_unsafe",
    )
    authorities = {
        "install_source": ("install_source.json", captured.lock),
        "candidate_selections": ("candidate_selections.json", captured.selections),
        "runtime_overrides": ("runtime_overrides.json", captured.runtime_overrides),
    }
    failing_authority = error.details.get("authority")
    manifest = {
        "schema_version": _BACKUP_SCHEMA_VERSION,
        "preflight_reason": error.reason,
        "failing_authority": (
            failing_authority if isinstance(failing_authority, str) else None
        ),
        "authorities": {
            authority: {
                "backup_file": backup_name,
                "present": payload is not None,
                "sha256": _sha256(payload) if payload is not None else None,
            }
            for authority, (backup_name, payload) in authorities.items()
        },
    }
    for authority, (backup_name, payload) in authorities.items():
        backup_path = paths.failure_backup_directory / backup_name
        if payload is None:
            if backup_path.exists():
                raise RegistryCutoverInitializationError(
                    "failure_backup_conflict",
                    "existing failed-cutover backup conflicts with captured authority",
                    details={"authority": authority},
                )
            continue
        _create_or_verify_failure_backup(
            backup_path,
            payload,
            authority=authority,
        )
    _create_or_verify_failure_backup(
        paths.failure_backup_manifest,
        _canonical_json(manifest),
        authority="failure_backup_manifest",
    )


def _resume_committed_cutover(
    paths: RegistryCutoverPaths,
    registry: JsonPluginRegistry,
    *,
    now: str,
) -> RegistryCutoverInitializationResult | None:
    try:
        commit_payload = paths.cutover_commit.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RegistryCutoverInitializationError(
            "cutover_commit_invalid",
            "cannot read Registry cutover commit marker",
        ) from exc

    commit = _decode_exact_object(
        commit_payload,
        expected_keys={
            "schema_version",
            "backup_manifest_sha256",
            "initial_registry_sha256",
        },
        reason="cutover_commit_invalid",
    )
    if commit.get("schema_version") != _BACKUP_SCHEMA_VERSION:
        raise RegistryCutoverInitializationError(
            "cutover_commit_invalid",
            "Registry cutover commit marker has an unsupported schema",
        )

    manifest_payload = _read_backup_file(
        paths.backup_manifest,
        authority="backup_manifest",
    )
    initial_registry_payload = _read_backup_file(
        paths.initial_registry_backup,
        authority="initial_registry",
    )
    if not _matches_sha256(
        manifest_payload,
        commit.get("backup_manifest_sha256"),
    ) or not _matches_sha256(
        initial_registry_payload,
        commit.get("initial_registry_sha256"),
    ):
        raise RegistryCutoverInitializationError(
            "cutover_commit_invalid",
            "Registry cutover commit hashes do not match their backups",
        )

    _verify_backup_manifest(paths, manifest_payload)
    initial_registry = _parse_initial_registry(initial_registry_payload, now=now)
    if not paths.registry.exists():
        raise RegistryCutoverInitializationError(
            "registry_read_only_degrade",
            "committed Registry is missing; refusing legacy fallback",
            details={"registry_error": "REGISTRY_MISSING"},
        )
    try:
        snapshot = registry.load()
    except InstallSourceError as exc:
        raise RegistryCutoverInitializationError(
            "registry_read_only_degrade",
            "committed Registry cannot be used safely; refusing legacy fallback",
            details={"registry_error": exc.code},
        ) from exc
    if (
        snapshot.revision < initial_registry.revision
        or snapshot.created_at != initial_registry.created_at
    ):
        raise RegistryCutoverInitializationError(
            "registry_read_only_degrade",
            "committed Registry does not match its initialization lineage",
            details={"registry_error": "REGISTRY_LINEAGE_MISMATCH"},
        )
    return RegistryCutoverInitializationResult(
        status="resumed",
        snapshot=snapshot,
        backup_manifest=paths.backup_manifest,
    )


def _verify_backup_manifest(paths: RegistryCutoverPaths, payload: bytes) -> None:
    manifest = _decode_exact_object(
        payload,
        expected_keys={"schema_version", "authorities"},
        reason="backup_manifest_invalid",
    )
    if manifest.get("schema_version") != _BACKUP_SCHEMA_VERSION:
        raise RegistryCutoverInitializationError(
            "backup_manifest_invalid",
            "Registry cutover backup manifest has an unsupported schema",
        )
    authorities = manifest.get("authorities")
    expected_files = {
        "install_source": "install_source.json",
        "candidate_selections": "candidate_selections.json",
        "runtime_overrides": "runtime_overrides.json",
    }
    if not isinstance(authorities, Mapping) or set(authorities) != set(expected_files):
        raise RegistryCutoverInitializationError(
            "backup_manifest_invalid",
            "Registry cutover backup manifest has invalid authorities",
        )
    for authority, backup_name in expected_files.items():
        record = authorities.get(authority)
        if not isinstance(record, Mapping) or set(record) != {
            "backup_file",
            "present",
            "sha256",
        }:
            raise RegistryCutoverInitializationError(
                "backup_manifest_invalid",
                "Registry cutover backup manifest has an invalid authority record",
                details={"authority": authority},
            )
        present = record.get("present")
        if record.get("backup_file") != backup_name or not isinstance(present, bool):
            raise RegistryCutoverInitializationError(
                "backup_manifest_invalid",
                "Registry cutover backup manifest has invalid authority metadata",
                details={"authority": authority},
            )
        backup_path = paths.backup_directory / backup_name
        if not present:
            if record.get("sha256") is not None or backup_path.exists():
                raise _backup_conflict(authority)
            continue
        backup_payload = _read_backup_file(backup_path, authority=authority)
        if not _matches_sha256(backup_payload, record.get("sha256")):
            raise _backup_conflict(authority)


def _decode_exact_object(
    payload: bytes,
    *,
    expected_keys: set[str],
    reason: str,
) -> Mapping[str, object]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryCutoverInitializationError(
            reason,
            "Registry cutover metadata is not valid UTF-8 JSON",
        ) from exc
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise RegistryCutoverInitializationError(
            reason,
            "Registry cutover metadata has an invalid shape",
        )
    return value


def _read_backup_file(path: Path, *, authority: str) -> bytes:
    _require_plain_backup_path(path, authority=authority)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RegistryCutoverInitializationError(
            "backup_read_failed",
            "cannot read required Registry cutover backup",
            details={"authority": authority},
        ) from exc


def _matches_sha256(payload: bytes, expected: object) -> bool:
    return (
        isinstance(expected, str)
        and len(expected) == 64
        and _sha256(payload) == expected
    )


def _parse_initial_registry(payload: bytes, *, now: str) -> PluginRegistrySnapshot:
    try:
        raw = json.loads(payload.decode("utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("initial Registry backup root is not an object")
        schema_version = raw.get("schema_version")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise TypeError("initial Registry backup schema is invalid")
        return parse_registry(raw, now=now, schema_version=schema_version)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, InstallSourceError) as exc:
        raise RegistryCutoverInitializationError(
            "cutover_commit_invalid",
            "initial Registry backup is not a valid Registry snapshot",
        ) from exc


def _require_safe_backup_location(paths: RegistryCutoverPaths) -> None:
    backup_directory = paths.backup_directory
    _require_plain_directory_path(backup_directory, reason="backup_path_unsafe")


def _require_plain_directory_path(path: Path, *, reason: str) -> None:
    if path.resolve(strict=False) != path or path.is_symlink():
        raise RegistryCutoverInitializationError(
            reason,
            "Registry cutover backup directory must not redirect through a link",
        )


def _require_plain_backup_path(path: Path, *, authority: str) -> None:
    if path.is_symlink():
        raise RegistryCutoverInitializationError(
            "backup_path_unsafe",
            "Registry cutover backup file must not be a symbolic link",
            details={"authority": authority},
        )


def _create_or_verify_backup(
    path: Path,
    payload: bytes,
    *,
    authority: str,
) -> None:
    _require_plain_backup_path(path, authority=authority)
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        try:
            atomic_write_bytes(path, payload)
        except OSError as exc:
            raise RegistryCutoverInitializationError(
                "backup_write_failed",
                "cannot create Registry cutover backup",
                details={"authority": authority},
            ) from exc
        return
    except OSError as exc:
        raise RegistryCutoverInitializationError(
            "backup_read_failed",
            "cannot verify Registry cutover backup",
            details={"authority": authority},
        ) from exc
    if existing != payload:
        raise _backup_conflict(authority)


def _create_or_verify_failure_backup(
    path: Path,
    payload: bytes,
    *,
    authority: str,
) -> None:
    _require_plain_backup_path(path, authority=authority)
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        try:
            atomic_write_bytes(path, payload)
        except OSError as exc:
            raise RegistryCutoverInitializationError(
                "failure_backup_write_failed",
                "cannot create failed-cutover legacy backup",
                details={"authority": authority},
            ) from exc
        return
    except OSError as exc:
        raise RegistryCutoverInitializationError(
            "failure_backup_read_failed",
            "cannot verify failed-cutover legacy backup",
            details={"authority": authority},
        ) from exc
    if existing != payload:
        raise RegistryCutoverInitializationError(
            "failure_backup_conflict",
            "existing failed-cutover backup conflicts with captured authority",
            details={"authority": authority},
        )


def _backup_conflict(authority: str) -> RegistryCutoverInitializationError:
    return RegistryCutoverInitializationError(
        "backup_conflict",
        "existing Registry cutover backup does not match legacy authority",
        details={"authority": authority},
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


__all__ = [
    "RegistryCutoverInitializationError",
    "RegistryCutoverInitializationResult",
    "RegistryCutoverOperationLock",
    "RegistryCutoverPaths",
    "initialize_registry_cutover",
]
