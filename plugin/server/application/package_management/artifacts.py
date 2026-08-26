"""Managed local storage for uploaded runtime plugin package artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil
from typing import BinaryIO
import uuid


class PackageArtifactStore:
    def __init__(
        self,
        root: Path,
        *,
        allowed_suffixes: frozenset[str],
        max_bytes: int,
        copy_chunk_bytes: int,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.allowed_suffixes = allowed_suffixes
        self.max_bytes = max_bytes
        self.copy_chunk_bytes = copy_chunk_bytes

    def list(self) -> dict[str, object]:
        items = [
            self.metadata(path)
            for path in sorted(
                (
                    path
                    for path in self.root.glob("*")
                    if path.is_file() and self.has_allowed_suffix(path.name)
                ),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        ]
        return {
            "packages": items,
            "count": len(items),
            "target_dir": str(self.root),
        }

    def save_bytes(self, *, filename: str, content: bytes) -> dict[str, object]:
        if len(content) > self.max_bytes:
            raise self._file_too_large(len(content))
        safe_name, stem, suffix = self.filename_parts(filename)
        self.root.mkdir(parents=True, exist_ok=True)
        while True:
            dest = self._exclusive_destination(safe_name, stem, suffix)
            try:
                with dest.open("xb") as target:
                    target.write(content)
                return self.metadata(dest)
            except FileExistsError:
                continue
            except Exception:
                dest.unlink(missing_ok=True)
                raise

    def save_file(self, *, filename: str, source_file: BinaryIO) -> dict[str, object]:
        safe_name, stem, suffix = self.filename_parts(filename)
        self.root.mkdir(parents=True, exist_ok=True)
        source_file.seek(0)
        while True:
            dest = self._exclusive_destination(safe_name, stem, suffix)
            try:
                total_bytes = 0
                with dest.open("xb") as target:
                    while chunk := source_file.read(self.copy_chunk_bytes):
                        total_bytes += len(chunk)
                        if total_bytes > self.max_bytes:
                            raise self._file_too_large(total_bytes)
                        target.write(chunk)
                return self.metadata(dest)
            except FileExistsError:
                continue
            except Exception:
                dest.unlink(missing_ok=True)
                raise

    def copy_from(self, *, filename: str, package_path: str) -> dict[str, object]:
        source = Path(package_path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"package file not found: {package_path}")
        if source.stat().st_size > self.max_bytes:
            raise self._file_too_large(source.stat().st_size)

        safe_name, stem, suffix = self.filename_parts(filename or source.name)
        self.root.mkdir(parents=True, exist_ok=True)
        if source.parent == self.root and source.name == safe_name:
            return self.metadata(source)

        while True:
            dest = self._exclusive_destination(safe_name, stem, suffix)
            try:
                with source.open("rb") as src, dest.open("xb") as target:
                    shutil.copyfileobj(src, target)
                return self.metadata(dest)
            except FileExistsError:
                continue
            except Exception:
                dest.unlink(missing_ok=True)
                raise

    def discard(self, package: str) -> dict[str, object]:
        target = self.resolve(package)
        if target.parent != self.root:
            raise ValueError("only a directly uploaded plugin package can be discarded")
        target.unlink()
        return {"success": True, "removed": True, "name": target.name}

    def resolve(self, raw: str) -> Path:
        candidate = Path(raw).expanduser()
        if candidate.exists():
            resolved = self._require_within(candidate.resolve(), field=f"package '{raw}'")
            if resolved.is_file() and self.has_allowed_suffix(resolved.name):
                return resolved

        target_candidate = (self.root / raw).resolve()
        if target_candidate.exists():
            resolved = self._require_within(target_candidate, field=f"package '{raw}'")
            if resolved.is_file() and self.has_allowed_suffix(resolved.name):
                return resolved
        raise FileNotFoundError(f"package file not found: {raw}")

    def has_allowed_suffix(self, filename: str) -> bool:
        return filename.lower().endswith(tuple(self.allowed_suffixes))

    def filename_parts(self, filename: str) -> tuple[str, str, str]:
        safe_name = Path(filename).name
        if not safe_name:
            raise ValueError("Invalid filename")
        lower_name = safe_name.lower()
        for suffix in sorted(self.allowed_suffixes, key=len, reverse=True):
            if lower_name.endswith(suffix):
                return safe_name, safe_name[: -len(suffix)], suffix
        allowed = ", ".join(sorted(self.allowed_suffixes))
        raise ValueError(f"Unsupported file type. Allowed: {allowed}")

    @staticmethod
    def metadata(path: Path) -> dict[str, object]:
        file_stat = path.stat()
        return {
            "name": path.name,
            "path": str(path.resolve()),
            "size_bytes": file_stat.st_size,
            "modified_at": datetime.fromtimestamp(
                file_stat.st_mtime,
                tz=timezone.utc,
            ).isoformat(),
        }

    def _exclusive_destination(self, name: str, stem: str, suffix: str) -> Path:
        dest = self.root / name
        if dest.exists():
            dest = self.root / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"
        return dest

    def _require_within(self, path: Path, *, field: str) -> Path:
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"{field} must be inside {self.root}") from exc
        return path

    def _file_too_large(self, size: int) -> ValueError:
        return ValueError(
            f"File too large: {size} bytes "
            f"(max {self.max_bytes // (1024 * 1024)} MiB)"
        )


__all__ = ["PackageArtifactStore"]
