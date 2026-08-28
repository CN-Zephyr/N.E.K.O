"""Revisioned JSON store for the future unified plugin Registry.

This adapter is intentionally not wired into runtime readers yet. Every update
holds a short cross-process lock, re-reads the latest disk snapshot, checks an
expected revision, applies one pure mutation, and publishes with atomic replace.
Downloads, extraction, imports, and lifecycle operations must stay outside this
critical section.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from plugin.server.application.install_source.registry_codec import (
    parse_registry,
    serialize_registry,
)
from plugin.server.application.install_source.manager import InstallSourceError
from plugin.server.application.install_source.models import (
    REGISTRY_SCHEMA_VERSION,
    PluginRegistrySnapshot,
)

from .atomic_files import atomic_write_bytes
from .locks import registry_file_lock


class RegistryNotInitializedError(FileNotFoundError):
    """The Registry file does not exist yet."""


class RegistryRevisionConflict(RuntimeError):
    """The caller planned against an obsolete Registry revision."""

    def __init__(self, *, expected: int, actual: int) -> None:
        super().__init__(
            f"plugin registry revision changed: expected={expected} actual={actual}"
        )
        self.expected = expected
        self.actual = actual


RegistryMutation = Callable[[PluginRegistrySnapshot], PluginRegistrySnapshot]


class JsonPluginRegistry:
    """Strict, atomic, compare-and-set JSON Registry store."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], str] | None = None,
        lock_timeout: float = 10.0,
    ) -> None:
        self.path = path.resolve()
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self._clock = clock or _utc_iso_now
        self._lock_timeout = lock_timeout
        self._snapshot: PluginRegistrySnapshot | None = None

    @property
    def snapshot(self) -> PluginRegistrySnapshot | None:
        return self._snapshot

    def load(self) -> PluginRegistrySnapshot:
        snapshot = self._read_disk()
        self._snapshot = snapshot
        return snapshot

    def initialize(
        self,
        snapshot: PluginRegistrySnapshot,
    ) -> PluginRegistrySnapshot:
        """Create the Registry once, or return the already-created snapshot."""

        with registry_file_lock(self.lock_path, timeout=self._lock_timeout):
            if self.path.exists():
                current = self._read_disk()
                self._snapshot = current
                return current
            payload, validated = self._validated_payload(snapshot)
            self._atomic_write(payload)
            self._snapshot = validated
            return validated

    def update(
        self,
        *,
        expected_revision: int,
        mutate: RegistryMutation,
    ) -> PluginRegistrySnapshot:
        """Apply one CAS mutation after re-reading under the file lock."""

        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer")

        with registry_file_lock(self.lock_path, timeout=self._lock_timeout):
            current = self._read_disk()
            if current.revision != expected_revision:
                raise RegistryRevisionConflict(
                    expected=expected_revision,
                    actual=current.revision,
                )

            proposed = mutate(current)
            if not isinstance(proposed, PluginRegistrySnapshot):
                raise TypeError("registry mutation must return PluginRegistrySnapshot")
            if proposed.schema_version != REGISTRY_SCHEMA_VERSION:
                raise InstallSourceError(
                    "UNSUPPORTED_REGISTRY_SCHEMA",
                    "registry mutation returned an unsupported schema",
                    details={
                        "expected_schema_version": REGISTRY_SCHEMA_VERSION,
                        "actual_schema_version": proposed.schema_version,
                    },
                )
            if proposed == current:
                self._snapshot = current
                return current

            updated = PluginRegistrySnapshot.build(
                proposed.plugins,
                revision=current.revision + 1,
                updated_at=self._clock(),
                created_at=current.created_at,
            )
            payload, validated = self._validated_payload(updated)
            self._atomic_write(payload)
            self._snapshot = validated
            return validated

    def _read_disk(self) -> PluginRegistrySnapshot:
        if not self.path.is_file():
            raise RegistryNotInitializedError(self.path)
        try:
            raw = json.loads(self.path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InstallSourceError(
                "LOCK_FILE_CORRUPT",
                f"cannot read plugin registry: {exc}",
                details={"reason": "registry_read_failed"},
            ) from exc
        if not isinstance(raw, dict):
            raise InstallSourceError(
                "LOCK_FILE_CORRUPT",
                "plugin registry root is not an object",
                details={"reason": "registry_not_object"},
            )
        schema_version = raw.get("schema_version")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise InstallSourceError(
                "LOCK_FILE_CORRUPT",
                f"plugin registry has invalid schema_version: {schema_version!r}",
                details={"reason": "invalid_schema_version"},
            )
        return parse_registry(
            raw,
            now=self._clock(),
            schema_version=schema_version,
        )

    def _validated_payload(
        self, snapshot: PluginRegistrySnapshot
    ) -> tuple[bytes, PluginRegistrySnapshot]:
        payload = serialize_registry(snapshot)
        raw = json.loads(payload)
        validated = parse_registry(
            raw,
            now=self._clock(),
            schema_version=REGISTRY_SCHEMA_VERSION,
        )
        canonical_payload = serialize_registry(validated)
        return canonical_payload, validated

    def _atomic_write(self, payload: bytes) -> None:
        atomic_write_bytes(self.path, payload, replace=os.replace)


def _utc_iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


__all__ = [
    "JsonPluginRegistry",
    "RegistryNotInitializedError",
    "RegistryRevisionConflict",
]
