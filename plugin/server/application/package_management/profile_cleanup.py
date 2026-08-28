"""Safe retirement of package-owned plugin profiles.

This module owns the filesystem policy for package profiles during candidate
removal.  Runtime lifecycle coordinates the transaction, but it must not
decide which profile belongs to a package, whether another installed
candidate still shares it, or how deferred cleanup is persisted.

The install-source object is deliberately represented by a narrow protocol.
It is a compatibility adapter for the current directory-keyed registry and
can later be replaced by the V2 registry without pulling lifecycle concerns
back into this module.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from plugin.logging_config import get_logger

logger = get_logger("server.application.package_management.profile_cleanup")

DEFERRED_PROFILE_CLEANUP_FILENAME = "package_profile_cleanup.json"
_STAGING_NAME_PATTERN = re.compile(r"^\.[A-Za-z0-9._-]+\.deleting-[0-9a-f]{32}$")


class PackageProfileRegistryPort(Protocol):
    """Legacy registry reads needed to prove profile ownership and sharing."""

    def entry_for_directory(
        self,
        directory_path: Path,
        *,
        include_removed: bool = False,
    ) -> object | None: ...

    def list_entries(self) -> object: ...


@dataclass(frozen=True)
class StagedPackageProfile:
    """A package profile moved aside until candidate retirement commits."""

    original_dir: Path
    staged_dir: Path


def profile_path_from_entry(entry: object, profiles_root: Path) -> Path | None:
    if getattr(entry, "channel", "") not in {"imported", "market"}:
        return None
    if getattr(entry, "profile_installed", None) is False:
        return None
    package_id = str(
        getattr(entry, "package_id", "") or getattr(entry, "plugin_id", "")
    )
    if not package_id:
        return None
    raw_profile_dir = str(getattr(entry, "profile_dir", "") or "")
    candidate = (
        Path(raw_profile_dir).expanduser()
        if raw_profile_dir
        else profiles_root / package_id
    )
    if path_has_symlink_ancestor(candidate):
        return None
    try:
        profile_dir = candidate.resolve()
    except Exception:
        return None
    if profile_dir.name != package_id:
        return None
    # A recorded profile location remains valid after the configured profile
    # root changes. Legacy fallback paths are still constrained to that root.
    if not raw_profile_dir and (
        profile_dir != profiles_root and profiles_root not in profile_dir.parents
    ):
        return None
    return profile_dir


def path_has_symlink_ancestor(path: Path) -> bool:
    """Reject a path when resolving it would traverse a symlink."""
    return any(candidate.is_symlink() for candidate in (path, *path.parents))


def has_other_entry_without_package_id(
    active_entries: object,
    current_primary_key: tuple[str, str],
) -> bool:
    """Report whether another installed row also predates package id tracking."""
    for entry in active_entries or ():
        if getattr(entry, "channel", "") not in {"imported", "market"}:
            continue
        key = (getattr(entry, "root_id", ""), getattr(entry, "directory_name", ""))
        if key == current_primary_key:
            continue
        if not str(getattr(entry, "package_id", "") or ""):
            return True
    return False


def load_deferred_profile_cleanup_paths(record_path: Path) -> list[str] | None:
    """Return pending paths, or ``None`` when an existing record is unusable.

    Callers must not overwrite a record they could not read: doing so would
    drop staging directories that no other durable state tracks.
    """
    try:
        raw = json.loads(record_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError, TypeError) as exc:
        logger.error(
            "failed to read deferred profile cleanup record {}: {}",
            record_path,
            exc,
        )
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("staged_paths"), list):
        logger.error("invalid deferred profile cleanup record: {}", record_path)
        return None
    return [path for path in raw["staged_paths"] if isinstance(path, str) and path]


def save_deferred_profile_cleanup_paths(record_path: Path, paths: list[str]) -> None:
    if not paths:
        try:
            record_path.unlink()
        except FileNotFoundError:
            pass
        return
    record_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = record_path.with_name(
        f".{record_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary_path.write_text(
            json.dumps({"schema_version": 1, "staged_paths": paths}),
            encoding="utf-8",
        )
        temporary_path.replace(record_path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def record_deferred_profile_cleanup(
    staged_profile: StagedPackageProfile,
    *,
    record_path: Path,
) -> bool:
    try:
        paths = load_deferred_profile_cleanup_paths(record_path)
        if paths is None:
            return False
        staged_path = str(staged_profile.staged_dir)
        if staged_path not in paths:
            paths.append(staged_path)
        save_deferred_profile_cleanup_paths(record_path, paths)
        return True
    except Exception as exc:
        logger.error(
            "failed to persist deferred profile cleanup for {}: {}",
            staged_profile.staged_dir,
            exc,
        )
        return False


def is_safe_deferred_profile_cleanup_path(path: Path) -> bool:
    return (
        path.is_absolute()
        and _STAGING_NAME_PATTERN.fullmatch(path.name) is not None
        and not path_has_symlink_ancestor(path)
    )


def retry_deferred_profile_cleanup(*, record_path: Path) -> int:
    """Retry profile cleanup jobs persisted after transient deletion failures."""
    paths = load_deferred_profile_cleanup_paths(record_path)
    if not paths:
        return 0

    remaining_paths: list[str] = []
    cleaned = 0
    for raw_path in paths:
        staged_path = Path(raw_path).expanduser()
        if not is_safe_deferred_profile_cleanup_path(staged_path):
            logger.error(
                "refusing unsafe deferred profile cleanup path: {}", staged_path
            )
            remaining_paths.append(raw_path)
            continue
        try:
            shutil.rmtree(staged_path)
        except FileNotFoundError:
            cleaned += 1
        except OSError as exc:
            logger.warning(
                "deferred profile cleanup still pending for {}: {}",
                staged_path,
                exc,
            )
            remaining_paths.append(raw_path)
        else:
            cleaned += 1
    try:
        save_deferred_profile_cleanup_paths(record_path, remaining_paths)
    except OSError as exc:
        logger.error(
            "failed to update deferred profile cleanup record {}: {}",
            record_path,
            exc,
        )
    return cleaned


def stage_orphaned_package_profile(
    plugin_dir: Path,
    *,
    registry: PackageProfileRegistryPort | None,
    profiles_root: Path,
    include_removed: bool = False,
    require_explicit_ownership: bool = False,
) -> StagedPackageProfile | None:
    """Stage an unshared package profile while candidate removal is pending.

    Moving the profile out of its package location prevents a concurrent
    reinstall from seeing it, but preserves it until executable deletion has
    succeeded. A failed candidate retirement can therefore restore the exact
    persisted configuration.
    """
    if registry is None:
        return None

    try:
        current_entry = registry.entry_for_directory(
            plugin_dir,
            include_removed=include_removed,
        )
        active_entries = registry.list_entries()
    except Exception as exc:
        logger.warning(
            "failed to inspect package-profile ownership for plugin_dir={}: {}",
            plugin_dir,
            exc,
        )
        return None

    # Only package installers own package profiles. A scanner-created manual
    # entry with no profile record must never infer ownership from a matching
    # directory name.
    if current_entry is None or getattr(current_entry, "channel", "") not in {
        "imported",
        "market",
    }:
        return None
    if (
        require_explicit_ownership
        and getattr(current_entry, "profile_installed", None) is not True
    ):
        return None

    current_primary_key = (
        getattr(current_entry, "root_id", ""),
        getattr(current_entry, "directory_name", ""),
    )
    recorded_package_id = str(getattr(current_entry, "package_id", "") or "")
    if has_other_entry_without_package_id(active_entries, current_primary_key):
        # Legacy bundle rows without package ids cannot prove that another
        # installed member does not share this profile. Keep data rather than
        # infer ownership and risk deleting a sibling's configuration.
        logger.warning(
            "skipping profile cleanup while an installation without a "
            "recorded package id may share this profile: {}",
            plugin_dir,
        )
        return None

    package_id = recorded_package_id or str(
        getattr(current_entry, "plugin_id", "") or ""
    )
    if not package_id:
        return None
    recorded_profile_dir = str(getattr(current_entry, "profile_dir", "") or "")
    if getattr(current_entry, "profile_installed", None) is False:
        return None

    try:
        resolved_profiles_root = profiles_root.resolve()
        profile_candidate = (
            Path(recorded_profile_dir).expanduser()
            if recorded_profile_dir
            else resolved_profiles_root / package_id
        )
        if path_has_symlink_ancestor(profile_candidate):
            logger.warning(
                "refusing to remove symlinked package profile path: {}",
                profile_candidate,
            )
            return None
        current_profile_dir = profile_candidate.resolve()
    except Exception as exc:
        logger.warning(
            "failed to resolve package profile for plugin_dir={}: {}",
            plugin_dir,
            exc,
        )
        return None

    if current_profile_dir.name != package_id or (
        not recorded_profile_dir
        and (
            current_profile_dir != resolved_profiles_root
            and resolved_profiles_root not in current_profile_dir.parents
        )
    ):
        logger.warning(
            "refusing to remove unsafe package profile path: {}",
            current_profile_dir,
        )
        return None

    for entry in active_entries:
        if (
            getattr(entry, "root_id", ""),
            getattr(entry, "directory_name", ""),
        ) == current_primary_key:
            continue
        if (
            profile_path_from_entry(entry, resolved_profiles_root)
            == current_profile_dir
        ):
            return None

    staged_profile_dir = current_profile_dir.with_name(
        f".{current_profile_dir.name}.deleting-{uuid.uuid4().hex}"
    )
    try:
        current_profile_dir.replace(staged_profile_dir)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.error(
            "failed to stage package profile {}: {}",
            current_profile_dir,
            exc,
        )
        raise
    return StagedPackageProfile(
        original_dir=current_profile_dir,
        staged_dir=staged_profile_dir,
    )


def restore_staged_package_profile(staged_profile: StagedPackageProfile) -> None:
    """Restore a profile after candidate retirement failed."""
    if not staged_profile.staged_dir.exists():
        return
    staged_profile.staged_dir.replace(staged_profile.original_dir)


def finalize_staged_package_profile(
    staged_profile: StagedPackageProfile,
) -> Path | None:
    """Permanently remove a profile only after candidate retirement commits."""
    try:
        shutil.rmtree(staged_profile.staged_dir)
    except FileNotFoundError:
        return None
    return staged_profile.original_dir


class PackageProfileService:
    """Injectable filesystem boundary for package-owned profile retirement."""

    def stage(
        self,
        plugin_dir: Path,
        *,
        registry: PackageProfileRegistryPort | None,
        profiles_root: Path,
        include_removed: bool = False,
        require_explicit_ownership: bool = False,
    ) -> StagedPackageProfile | None:
        return stage_orphaned_package_profile(
            plugin_dir,
            registry=registry,
            profiles_root=profiles_root,
            include_removed=include_removed,
            require_explicit_ownership=require_explicit_ownership,
        )

    def restore(self, staged_profile: StagedPackageProfile) -> None:
        restore_staged_package_profile(staged_profile)

    def finalize(self, staged_profile: StagedPackageProfile) -> Path | None:
        return finalize_staged_package_profile(staged_profile)

    def record_deferred(
        self,
        staged_profile: StagedPackageProfile,
        *,
        record_path: Path,
    ) -> bool:
        return record_deferred_profile_cleanup(
            staged_profile,
            record_path=record_path,
        )

    def retry_deferred(self, *, record_path: Path) -> int:
        return retry_deferred_profile_cleanup(record_path=record_path)


__all__ = [
    "DEFERRED_PROFILE_CLEANUP_FILENAME",
    "PackageProfileService",
    "PackageProfileRegistryPort",
    "StagedPackageProfile",
    "finalize_staged_package_profile",
    "has_other_entry_without_package_id",
    "is_safe_deferred_profile_cleanup_path",
    "load_deferred_profile_cleanup_paths",
    "path_has_symlink_ancestor",
    "profile_path_from_entry",
    "record_deferred_profile_cleanup",
    "restore_staged_package_profile",
    "retry_deferred_profile_cleanup",
    "save_deferred_profile_cleanup_paths",
    "stage_orphaned_package_profile",
]
