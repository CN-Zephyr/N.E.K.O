from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from plugin.server.application.install_source.manager import (
    InstallSourceManager,
    _serialize_lock,
)
from plugin.server.application.install_source.models import LockFile
from plugin.server.infrastructure.package_management.registry_runtime import (
    build_plugin_registry_runtime,
)


pytestmark = pytest.mark.plugin_unit

TS = "2026-08-27T00:00:00.000000Z"


class _NoopScanner:
    def scan(self):
        return []


class _ConfigPaths:
    def __init__(self, *, legacy: Path, runtime: Path) -> None:
        self.legacy = legacy
        self.runtime = runtime
        self.calls: list[tuple[str, str]] = []

    def get_config_path(self, filename: str) -> Path:
        self.calls.append(("legacy", filename))
        return self.legacy / filename

    def get_runtime_config_path(self, filename: str) -> Path:
        self.calls.append(("runtime", filename))
        return self.runtime / filename


class _OperationLock:
    def __init__(self) -> None:
        self.entries = 0
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def hold(self):
        async with self._lock:
            self.entries += 1
            yield


def _manager(tmp_path: Path, lock: LockFile) -> InstallSourceManager:
    lock_path = tmp_path / "packages" / "plugins.lock.json"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_bytes(_serialize_lock(lock))
    manager = InstallSourceManager(
        lock_path=lock_path,
        builtin_root=tmp_path / "builtin",
        user_root=tmp_path / "user",
        scanner=_NoopScanner(),
        clock=lambda: datetime(2026, 8, 27, tzinfo=UTC),
    )
    manager.load()
    return manager


def _empty_lock() -> LockFile:
    return LockFile(
        schema_version=2,
        entries=(),
        updated_at=TS,
        created_at=TS,
    )


def test_factory_preserves_legacy_fallback_and_runtime_write_paths(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _empty_lock())
    config_paths = _ConfigPaths(
        legacy=tmp_path / "project-config",
        runtime=tmp_path / "user-config",
    )

    runtime = build_plugin_registry_runtime(
        install_source_manager=manager,
        config_paths=config_paths,
        clock=lambda: TS,
    )

    assert runtime.paths.install_source == manager.lock_path.resolve()
    assert runtime.paths.candidate_selections == (
        tmp_path / "project-config" / "plugin_candidate_selections.json"
    ).resolve()
    assert runtime.paths.runtime_overrides == (
        tmp_path / "project-config" / "plugin_runtime_overrides.json"
    ).resolve()
    assert runtime.paths.registry == (
        tmp_path / "user-config" / "plugin_registry.json"
    ).resolve()
    assert config_paths.calls == [
        ("legacy", "plugin_candidate_selections.json"),
        ("legacy", "plugin_runtime_overrides.json"),
        ("runtime", "plugin_registry.json"),
    ]
    assert runtime.snapshot_provider() is None
    assert not runtime.paths.registry.exists()


@pytest.mark.asyncio
async def test_initialize_exposes_snapshot_only_after_cutover_commits(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _empty_lock())
    config_paths = _ConfigPaths(
        legacy=tmp_path / "legacy-config",
        runtime=tmp_path / "runtime-config",
    )
    runtime = build_plugin_registry_runtime(
        install_source_manager=manager,
        config_paths=config_paths,
        clock=lambda: TS,
    )
    operation_lock = _OperationLock()

    assert runtime.snapshot_provider() is None

    result = await runtime.initialize(operation_lock=operation_lock)

    assert result.status == "initialized"
    assert operation_lock.entries == 1
    assert runtime.snapshot_provider() == result.snapshot
    assert runtime.paths.registry.is_file()
    assert runtime.paths.cutover_commit.is_file()


@pytest.mark.asyncio
async def test_failed_initialize_does_not_publish_a_snapshot(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _empty_lock())
    manager.lock_path.write_bytes(b"not-the-canonical-manager-snapshot")
    runtime = build_plugin_registry_runtime(
        install_source_manager=manager,
        config_paths=_ConfigPaths(
            legacy=tmp_path / "legacy-config",
            runtime=tmp_path / "runtime-config",
        ),
        clock=lambda: TS,
    )

    with pytest.raises(RuntimeError):
        await runtime.initialize(operation_lock=_OperationLock())

    assert runtime.snapshot_provider() is None
    assert not runtime.paths.registry.exists()
