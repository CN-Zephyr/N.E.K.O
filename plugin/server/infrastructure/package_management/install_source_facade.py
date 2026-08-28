"""Compatibility facade from the legacy install-source API to Registry v1.

After cutover, existing callers may keep their ``InstallSourceManager``-shaped
API, but every read and mutation is projected from the unified Registry.  The
legacy ``plugins.lock.json`` remains a read-only migration backup.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from plugin.server.application.install_source.manager import (
    InstallSourceError,
    InstallSourceManager,
    _DEFAULT_INSTALL_SOURCE,
    _serialize_source_detail_for_json,
    classify_plugin_path,
)
from plugin.server.application.install_source.models import (
    CandidateRecord,
    CandidateRef,
    LockEntry,
    LockFile,
    PluginEntry,
    PluginRegistrySnapshot,
    RootId,
    SourceDetailImported,
    SourceDetailMarket,
)
from plugin.server.application.install_source.scanner import PluginDirectoryScanner
from plugin.server.infrastructure.plugin_registry_authority import (
    update_plugin_registry,
)

from .json_registry import JsonPluginRegistry


class RegistryInstallSourceFacade:
    """Preserve the #2958 install-source surface with Registry as sole writer."""

    def __init__(
        self,
        *,
        legacy_manager: InstallSourceManager,
        registry: JsonPluginRegistry,
        clock: Callable[[], str],
    ) -> None:
        if registry.snapshot is None:
            raise ValueError("install-source facade requires an initialized Registry")
        self.builtin_root = legacy_manager.builtin_root
        self.user_root = legacy_manager.user_root
        self.scanner = legacy_manager.scanner
        self.lock_path = legacy_manager.lock_path
        self._legacy_manager = legacy_manager
        self._registry = registry
        self._clock = clock

    @property
    def is_degraded(self) -> bool:
        try:
            self._registry.load()
        except Exception:
            return True
        return False

    @property
    def degrade_reason(self) -> str | None:
        return "registry_read_failed" if self.is_degraded else None

    @property
    def current_updated_at(self) -> str:
        return self._registry.load().updated_at

    def snapshot(self) -> LockFile:
        """Project one current Registry revision into the legacy frozen view."""

        snapshot = self._registry.load()
        entries = tuple(
            candidate.to_lock_entry(plugin_id)
            for plugin_id, entry in sorted(snapshot.plugins.items())
            for candidate in sorted(
                entry.candidates,
                key=lambda item: (item.root_id, item.directory_name),
            )
        )
        return LockFile(
            schema_version=2,
            entries=entries,
            updated_at=snapshot.updated_at,
            created_at=snapshot.created_at,
        )

    def list_entries(self, *, include_removed: bool = False) -> list[LockEntry]:
        entries = self.snapshot().entries
        if include_removed:
            return list(entries)
        return [entry for entry in entries if not entry.removed]

    def entry_for_directory(
        self,
        directory_path: Path,
        *,
        include_removed: bool = False,
    ) -> LockEntry | None:
        key = self._classify(directory_path)
        entry = self._entry_by_key(self.snapshot(), key)
        if entry is None or (entry.removed and not include_removed):
            return None
        return entry

    def package_id_for_directory(
        self,
        directory_path: Path,
        *,
        include_removed: bool = False,
    ) -> str:
        entry = self.entry_for_directory(
            directory_path,
            include_removed=include_removed,
        )
        return entry.package_id if entry is not None else ""

    def profile_dir_for_directory(
        self,
        directory_path: Path,
        *,
        include_removed: bool = False,
    ) -> str:
        entry = self.entry_for_directory(
            directory_path,
            include_removed=include_removed,
        )
        return entry.profile_dir if entry is not None else ""

    def find_active_market_entry(self, plugin_ref: str) -> LockEntry | None:
        if not plugin_ref:
            return None
        for entry in self.snapshot().entries:
            if entry.removed or entry.channel != "market":
                continue
            if entry.plugin_id == plugin_ref:
                return entry
            detail = entry.source_detail
            if (
                isinstance(detail, SourceDetailMarket)
                and detail.plugin_market_id == plugin_ref
            ):
                return entry
        return None

    def to_api_view(
        self,
        plugin_id: str,
        *,
        directory_path: Path | None = None,
    ) -> dict[str, Any]:
        snapshot = self.snapshot()
        entry: LockEntry | None = None
        if directory_path is not None:
            try:
                entry = self._entry_by_key(snapshot, self._classify(directory_path))
            except InstallSourceError:
                entry = None
        if entry is None:
            candidates = [
                item for item in snapshot.entries if item.plugin_id == plugin_id
            ]
            if not candidates:
                candidates = [
                    item
                    for item in snapshot.entries
                    if not item.plugin_id and item.directory_name == plugin_id
                ]
            if candidates:
                active = [item for item in candidates if not item.removed]
                entry = max(active or candidates, key=lambda item: item.updated_at)
        if entry is None or entry.removed:
            return _DEFAULT_INSTALL_SOURCE.copy()
        return {
            "source": entry.channel,
            "reason": entry.reason,
            "installed_at": entry.installed_at,
            "source_detail": _serialize_source_detail_for_json(entry.source_detail),
        }

    def record_import(
        self,
        *,
        directory_path: Path,
        package_filename: str,
        package_sha256: str,
        package_id: str = "",
        profile_dir: str = "",
    ) -> None:
        root_id, directory_name = self._classify(directory_path)
        plugin_id = PluginDirectoryScanner._load_plugin_id(directory_path)
        self._reject_builtin(root_id, directory_name, plugin_id, channel="imported")
        now = self._clock()
        detail = SourceDetailImported(
            package_filename=package_filename,
            package_sha256=package_sha256,
        )

        def build(existing: CandidateRecord | None) -> CandidateRecord:
            if existing is None:
                return CandidateRecord(
                    root_id=root_id,
                    directory_name=directory_name,
                    channel="imported",
                    reason="user_requested",
                    installed_at=now,
                    updated_at=now,
                    last_seen_at=now,
                    source_detail=detail,
                    package_id=package_id,
                    profile_dir=profile_dir,
                    profile_installed=bool(profile_dir),
                )
            return replace(
                existing,
                channel="imported",
                source_detail=detail,
                updated_at=now,
                last_seen_at=now,
                removed=False,
                removed_at=None,
                package_id=package_id or existing.package_id,
                profile_dir=profile_dir
                or (
                    existing.profile_dir
                    if not existing.removed and existing.profile_installed is True
                    else ""
                ),
                profile_installed=(
                    True
                    if profile_dir
                    else existing.profile_installed
                    if not existing.removed
                    else False
                ),
            )

        self._upsert_candidate(
            root_id=root_id,
            directory_name=directory_name,
            plugin_id=plugin_id,
            build=build,
        )

    def record_market(
        self,
        *,
        directory_path: Path,
        plugin_market_id: str,
        version: str,
        package_url: str,
        package_id: str = "",
        profile_dir: str = "",
    ) -> None:
        root_id, directory_name = self._classify(directory_path)
        plugin_id = PluginDirectoryScanner._load_plugin_id(directory_path)
        self._reject_builtin(root_id, directory_name, plugin_id, channel="market")
        now = self._clock()

        def build(existing: CandidateRecord | None) -> CandidateRecord:
            previous_version = None
            if (
                existing is not None
                and isinstance(existing.source_detail, SourceDetailMarket)
                and existing.source_detail.version
                and existing.source_detail.version != version
            ):
                previous_version = existing.source_detail.version
            detail = SourceDetailMarket(
                plugin_market_id=plugin_market_id,
                version=version,
                package_url=package_url,
                package_sha256="",
                payload_hash=None,
                channel="stable",
                published_at=now,
                previous_version=previous_version,
            )
            if existing is None:
                return CandidateRecord(
                    root_id=root_id,
                    directory_name=directory_name,
                    channel="market",
                    reason="user_requested",
                    installed_at=now,
                    updated_at=now,
                    last_seen_at=now,
                    source_detail=detail,
                    package_id=package_id,
                    profile_dir=profile_dir,
                    profile_installed=bool(profile_dir),
                )
            return replace(
                existing,
                channel="market",
                source_detail=detail,
                updated_at=now,
                last_seen_at=now,
                removed=False,
                removed_at=None,
                package_id=package_id or existing.package_id,
                profile_dir=profile_dir,
                profile_installed=bool(profile_dir),
            )

        self._upsert_candidate(
            root_id=root_id,
            directory_name=directory_name,
            plugin_id=plugin_id,
            build=build,
        )

    def record_market_install(
        self,
        *,
        root_id: RootId,
        directory_name: str,
        plugin_id: str,
        market_detail: dict[str, Any],
        package_id: str = "",
        profile_dir: str = "",
    ) -> tuple[LockEntry, list[str]]:
        return self._record_market_common(
            root_id=root_id,
            directory_name=directory_name,
            plugin_id=plugin_id,
            market_detail=market_detail,
            is_upgrade=False,
            package_id=package_id,
            profile_dir=profile_dir,
        )

    def record_market_upgrade(
        self,
        *,
        root_id: RootId,
        directory_name: str,
        plugin_id: str,
        market_detail: dict[str, Any],
        package_id: str = "",
        profile_dir: str = "",
    ) -> tuple[LockEntry, list[str]]:
        return self._record_market_common(
            root_id=root_id,
            directory_name=directory_name,
            plugin_id=plugin_id,
            market_detail=market_detail,
            is_upgrade=True,
            package_id=package_id,
            profile_dir=profile_dir,
        )

    def _record_market_common(
        self,
        *,
        root_id: RootId,
        directory_name: str,
        plugin_id: str,
        market_detail: dict[str, Any],
        is_upgrade: bool,
        package_id: str,
        profile_dir: str,
    ) -> tuple[LockEntry, list[str]]:
        self._reject_builtin(root_id, directory_name, plugin_id, channel="market")
        now = self._clock()
        result: CandidateRecord | None = None
        warnings: list[str] = []

        def build(existing: CandidateRecord | None) -> CandidateRecord:
            nonlocal result, warnings
            previous_version = None
            installed_at = now
            if existing is not None and is_upgrade and not existing.removed:
                installed_at = existing.installed_at
                if isinstance(existing.source_detail, SourceDetailMarket):
                    previous_version = existing.source_detail.version
            detail, warnings = self._legacy_manager._build_market_detail_with_warnings(
                market_detail,
                previous_version=previous_version,
                now=now,
            )
            result = CandidateRecord(
                root_id=root_id,
                directory_name=directory_name,
                channel="market",
                reason="user_requested",
                installed_at=installed_at,
                updated_at=now,
                last_seen_at=now,
                source_detail=detail,
                package_id=package_id or (existing.package_id if existing else ""),
                profile_dir=profile_dir
                or (
                    existing.profile_dir
                    if is_upgrade
                    and existing is not None
                    and existing.profile_installed is True
                    else ""
                ),
                profile_installed=(
                    True
                    if profile_dir
                    else existing.profile_installed
                    if is_upgrade and existing is not None
                    else False
                ),
            )
            return result

        self._upsert_candidate(
            root_id=root_id,
            directory_name=directory_name,
            plugin_id=plugin_id,
            build=build,
        )
        if result is None:  # pragma: no cover - mutation invariant
            raise RuntimeError("market candidate mutation did not produce an entry")
        return result.to_lock_entry(plugin_id), warnings

    def restore_entry_for_rollback(self, entry: LockEntry) -> None:
        restored = CandidateRecord.from_lock_entry(entry)
        self._upsert_candidate(
            root_id=entry.root_id,
            directory_name=entry.directory_name,
            plugin_id=entry.plugin_id,
            build=lambda _existing: restored,
        )

    def mark_removed(
        self,
        *,
        directory_path: Path,
        reason: str = "user_delete",
    ) -> None:
        _ = reason
        root_id, directory_name = self._classify(directory_path)
        self._reject_builtin(root_id, directory_name, "", channel="removed")
        now = self._clock()
        key = (root_id, directory_name)

        def mutate(snapshot: PluginRegistrySnapshot) -> PluginRegistrySnapshot:
            located = self._locate_candidate(snapshot, key)
            if located is None:
                return snapshot
            plugin_id, entry, candidate = located
            if candidate.removed:
                return snapshot
            removed = replace(
                candidate,
                removed=True,
                removed_at=now,
                updated_at=now,
            )
            selected_matches = (
                entry.selected_candidate is not None
                and entry.selected_candidate.primary_key == key
            )
            updated_entry = replace(
                entry,
                candidates=self._replace_candidate(entry, removed),
                selected_candidate=(
                    None if selected_matches else entry.selected_candidate
                ),
                candidate_source=None if selected_matches else entry.candidate_source,
            )
            return snapshot.with_entry(updated_entry)

        self._update(mutate, operation="mark removed")

    def mark_profile_removed(
        self,
        *,
        directory_path: Path,
    ) -> None:
        """Clear package-profile ownership for one exact Registry candidate."""

        root_id, directory_name = self._classify(directory_path)
        self._reject_builtin(
            root_id,
            directory_name,
            "",
            channel="profile_removed",
        )
        now = self._clock()
        key = (root_id, directory_name)

        def mutate(snapshot: PluginRegistrySnapshot) -> PluginRegistrySnapshot:
            located = self._locate_candidate(snapshot, key)
            if located is None:
                return snapshot
            _plugin_id, entry, candidate = located
            if not candidate.profile_dir and candidate.profile_installed is False:
                return snapshot
            updated = replace(
                candidate,
                profile_dir="",
                profile_installed=False,
                updated_at=now,
            )
            return snapshot.with_entry(
                replace(
                    entry,
                    candidates=self._replace_candidate(entry, updated),
                )
            )

        self._update(mutate, operation="mark profile removed")

    def _upsert_candidate(
        self,
        *,
        root_id: RootId,
        directory_name: str,
        plugin_id: str,
        build: Callable[[CandidateRecord | None], CandidateRecord],
    ) -> None:
        key = (root_id, directory_name)

        def mutate(snapshot: PluginRegistrySnapshot) -> PluginRegistrySnapshot:
            located = self._locate_candidate(snapshot, key)
            owner_id = located[0] if located is not None else None
            target_id = plugin_id or owner_id or directory_name
            if owner_id is not None and owner_id != target_id:
                raise InstallSourceError(
                    "PLUGIN_ID_CONFLICT",
                    "candidate is already owned by another logical plugin id",
                    details={"root_id": root_id, "directory_name": directory_name},
                )
            entry = snapshot.entry(target_id) or PluginEntry(plugin_id=target_id)
            existing = located[2] if located is not None else None
            candidate = build(existing)
            return snapshot.with_entry(
                replace(
                    entry,
                    candidates=self._replace_candidate(entry, candidate),
                )
            )

        self._update(mutate, operation="upsert candidate")

    def _update(
        self,
        mutate: Callable[[PluginRegistrySnapshot], PluginRegistrySnapshot],
        *,
        operation: str,
    ) -> None:
        try:
            update_plugin_registry(self._registry, mutate)
        except InstallSourceError:
            raise
        except Exception as exc:
            raise InstallSourceError(
                "lock_write_failed",
                f"failed to {operation} in plugin Registry: {exc}",
            ) from exc

    def _classify(self, directory_path: Path) -> tuple[RootId, str]:
        return classify_plugin_path(
            directory_path,
            builtin_root=self.builtin_root,
            user_root=self.user_root,
        )

    @staticmethod
    def _entry_by_key(
        snapshot: LockFile,
        key: tuple[str, str],
    ) -> LockEntry | None:
        return next(
            (entry for entry in snapshot.entries if entry.primary_key == key), None
        )

    @staticmethod
    def _locate_candidate(
        snapshot: PluginRegistrySnapshot,
        key: tuple[str, str],
    ) -> tuple[str, PluginEntry, CandidateRecord] | None:
        for plugin_id, entry in snapshot.plugins.items():
            candidate = next(
                (item for item in entry.candidates if item.primary_key == key),
                None,
            )
            if candidate is not None:
                return plugin_id, entry, candidate
        return None

    @staticmethod
    def _replace_candidate(
        entry: PluginEntry,
        candidate: CandidateRecord,
    ) -> tuple[CandidateRecord, ...]:
        kept = [
            item
            for item in entry.candidates
            if item.primary_key != candidate.primary_key
        ]
        kept.append(candidate)
        return tuple(sorted(kept, key=lambda item: item.primary_key))

    @staticmethod
    def _reject_builtin(
        root_id: RootId,
        directory_name: str,
        plugin_id: str,
        *,
        channel: str,
    ) -> None:
        if root_id != "builtin":
            return
        raise InstallSourceError(
            "BUILTIN_CHANNEL_LOCKED",
            f"builtin plugin {directory_name} cannot be set to channel={channel}",
            details={
                "directory_name": directory_name,
                "plugin_id": plugin_id,
                "target_channel": channel,
            },
        )


def build_registry_install_source_facade(
    *,
    legacy_manager: InstallSourceManager,
    registry: JsonPluginRegistry,
    clock: Callable[[], str],
) -> RegistryInstallSourceFacade:
    return RegistryInstallSourceFacade(
        legacy_manager=legacy_manager,
        registry=registry,
        clock=clock,
    )


__all__ = [
    "RegistryInstallSourceFacade",
    "build_registry_install_source_facade",
]
