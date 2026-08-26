"""Filesystem primitives for safe runtime plugin package transactions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
import shutil
import stat

_MANIFEST_ADJACENT_PROFILE_NAMES = {
    "profiles.toml": "profiles.toml",
    "profiles": "profiles",
}


def backup_path_for(target_dir: Path, *, backup_root: Path | None = None) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%f")
    root = backup_root or target_dir.parent / ".upgrade-backups"
    return root / f"{target_dir.name}.bak.{timestamp}"


async def restore_directory(backup_dir: Path, target_dir: Path) -> None:
    if not backup_dir.exists():
        return
    await remove_directory(target_dir)
    await asyncio.to_thread(backup_dir.rename, target_dir)


async def remove_directory(target_dir: Path) -> None:
    if not target_dir.exists():
        return
    await asyncio.to_thread(shutil.rmtree, target_dir)


async def merge_directory_contents(source_dir: Path, target_dir: Path) -> None:
    if not source_dir.exists():
        return
    if not source_dir.is_dir():
        raise NotADirectoryError(source_dir)
    await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.copytree, source_dir, target_dir, dirs_exist_ok=True)


def assert_preserved_tree_has_no_links_or_reparse_points(source: Path) -> None:
    pending = [source]
    while pending:
        current = pending.pop()
        metadata = current.lstat()
        file_attributes = getattr(metadata, "st_file_attributes", 0)
        reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(metadata.st_mode) or bool(file_attributes & reparse_attribute):
            raise OSError(
                "links and reparse points are not supported for preserved "
                f"plugin state: {current.name}"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            continue
        with os.scandir(current) as entries:
            pending.extend(Path(entry.path) for entry in entries)


def canonical_profile_sources(sources: list[Path]) -> dict[str, Path]:
    sources_by_name: dict[str, Path] = {}
    for source in sources:
        canonical_name = _MANIFEST_ADJACENT_PROFILE_NAMES.get(source.name.casefold())
        if canonical_name is None:
            continue
        if canonical_name in sources_by_name:
            raise OSError(f"multiple legacy profile paths map to {canonical_name}")
        sources_by_name[canonical_name] = source
    return sources_by_name


async def restore_manifest_adjacent_profiles(
    backup_dir: Path,
    target_dir: Path,
) -> None:
    sources = await asyncio.to_thread(lambda: list(backup_dir.iterdir()))
    sources_by_name = canonical_profile_sources(sources)

    for canonical_name, source in sources_by_name.items():
        await asyncio.to_thread(
            assert_preserved_tree_has_no_links_or_reparse_points,
            source,
        )
        target = target_dir / canonical_name
        if source.is_dir():
            await merge_directory_contents(source, target)
            continue
        if not source.is_file():
            raise OSError(f"unsupported profile path: {source}")
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copy2, source, target)


__all__ = [
    "assert_preserved_tree_has_no_links_or_reparse_points",
    "backup_path_for",
    "canonical_profile_sources",
    "merge_directory_contents",
    "remove_directory",
    "restore_directory",
    "restore_manifest_adjacent_profiles",
]
