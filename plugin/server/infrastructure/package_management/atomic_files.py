"""Small fsync + atomic-replace primitives shared by Registry storage."""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path


def atomic_write_bytes(
    path: Path,
    payload: bytes,
    *,
    replace: Callable[[Path, Path], None] | None = None,
) -> None:
    """Replace ``path`` with ``payload`` without exposing a partial file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        replace_file = replace or os.replace
        last_permission_error: PermissionError | None = None
        for retry_delay_ms in (0, 50, 100, 200):
            if retry_delay_ms:
                time.sleep(retry_delay_ms / 1000.0)
            try:
                replace_file(temp_path, path)
                break
            except PermissionError as exc:
                last_permission_error = exc
        else:
            assert last_permission_error is not None
            raise last_permission_error
        temp_path = None
        fsync_directory(path.parent)
    except BaseException:
        if temp_path is not None:
            # Cleanup is best-effort: a second Windows sharing violation must
            # never hide the original write/replace failure from the caller.
            with suppress(OSError):
                temp_path.unlink(missing_ok=True)
        raise


def fsync_directory(directory: Path) -> None:
    """Persist a directory entry update where the platform supports it."""

    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["atomic_write_bytes", "fsync_directory"]
