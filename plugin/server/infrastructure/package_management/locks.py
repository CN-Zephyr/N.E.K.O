"""Short cross-process file lock for plugin Registry mutations."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import portalocker


@contextmanager
def registry_file_lock(lock_path: Path, *, timeout: float = 10.0) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(str(lock_path), mode="a+b", timeout=timeout):
        yield


__all__ = ["registry_file_lock"]
