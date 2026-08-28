from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from plugin.server.application.install_source import (
    get_install_source_manager,
    set_global_manager,
)
from plugin.server.application.install_source.manager import (
    InstallSourceManager,
    _serialize_lock,
)
from plugin.server.application.install_source.models import LockFile
from plugin.server.application.install_source.scanner import PluginDirectoryScanner
from plugin.server.application.package_management.registry_startup import (
    initialize_plugin_registry_startup,
)
from plugin.server.infrastructure.package_management.install_source_facade import (
    RegistryInstallSourceFacade,
)
from plugin.server.infrastructure.plugin_registry_authority import (
    clear_plugin_registry_authority,
    get_published_plugin_registry,
    get_published_registry_snapshot_provider,
    is_plugin_registry_authority_configured,
)


pytestmark = pytest.mark.plugin_unit

TS = "2026-08-28T00:00:00.000000Z"


class _ConfigPaths:
    def __init__(self, root: Path) -> None:
        self.root = root

    def get_config_path(self, filename: str) -> Path:
        return self.root / "legacy-config" / filename

    def get_runtime_config_path(self, filename: str) -> Path:
        return self.root / "runtime-config" / filename


class _OperationLock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def hold(self):
        async with self._lock:
            yield


def _manager(tmp_path: Path) -> InstallSourceManager:
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    builtin_root.mkdir(exist_ok=True)
    user_root.mkdir(exist_ok=True)
    lock_path = tmp_path / "plugins.lock.json"
    lock_path.write_bytes(
        _serialize_lock(
            LockFile(
                schema_version=2,
                entries=(),
                updated_at=TS,
                created_at=TS,
            )
        )
    )
    manager = InstallSourceManager(
        lock_path=lock_path,
        builtin_root=builtin_root,
        user_root=user_root,
        scanner=PluginDirectoryScanner(builtin_root, user_root),
        clock=lambda: datetime(2026, 8, 28, tzinfo=UTC),
    )
    manager.load()
    return manager


@pytest.fixture(autouse=True)
def _reset_authorities():
    clear_plugin_registry_authority()
    set_global_manager(None)
    try:
        yield
    finally:
        clear_plugin_registry_authority()
        set_global_manager(None)


@pytest.mark.asyncio
async def test_success_publishes_registry_and_install_source_facade(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    legacy_bytes = manager.lock_path.read_bytes()

    result = await initialize_plugin_registry_startup(
        install_source_manager=manager,
        config_paths=_ConfigPaths(tmp_path),
        operation_lock=_OperationLock(),
        clock=lambda: TS,
    )

    assert result.mode == "registry"
    assert isinstance(result.install_source, RegistryInstallSourceFacade)
    assert get_install_source_manager() is result.install_source
    assert get_published_plugin_registry() is result.runtime.registry
    assert is_plugin_registry_authority_configured()
    assert manager.lock_path.read_bytes() == legacy_bytes


@pytest.mark.asyncio
async def test_first_preflight_failure_keeps_legacy_authority(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    config_paths = _ConfigPaths(tmp_path)
    selection_path = config_paths.get_config_path(
        "plugin_candidate_selections.json"
    )
    selection_path.parent.mkdir(parents=True)
    selection_path.write_bytes(b"{invalid-selection")

    result = await initialize_plugin_registry_startup(
        install_source_manager=manager,
        config_paths=config_paths,
        operation_lock=_OperationLock(),
        clock=lambda: TS,
    )

    assert result.mode == "legacy"
    assert result.error_reason == "legacy_invalid_json"
    assert get_install_source_manager() is manager
    assert not is_plugin_registry_authority_configured()
    assert get_published_plugin_registry() is None
    assert result.runtime.paths.failure_backup_manifest.is_file()
    assert not result.runtime.paths.registry.exists()


@pytest.mark.asyncio
async def test_existing_broken_registry_blocks_all_legacy_fallback(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    config_paths = _ConfigPaths(tmp_path)
    registry_path = config_paths.get_runtime_config_path("plugin_registry.json")
    registry_path.parent.mkdir(parents=True)
    registry_path.write_bytes(b"{broken-registry")

    result = await initialize_plugin_registry_startup(
        install_source_manager=manager,
        config_paths=config_paths,
        operation_lock=_OperationLock(),
        clock=lambda: TS,
    )

    assert result.mode == "blocked"
    assert result.error_reason == "registry_read_only_degrade"
    assert get_install_source_manager() is None
    assert get_published_plugin_registry() is None
    assert is_plugin_registry_authority_configured()
    provider = get_published_registry_snapshot_provider()
    assert provider is not None
    assert provider() is None
    assert registry_path.read_bytes() == b"{broken-registry"


@pytest.mark.asyncio
async def test_failed_reconcile_never_attempts_first_cutover(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    result = await initialize_plugin_registry_startup(
        install_source_manager=manager,
        config_paths=_ConfigPaths(tmp_path),
        operation_lock=_OperationLock(),
        clock=lambda: TS,
        reconciled=False,
    )

    assert result.mode == "legacy"
    assert result.error_reason == "legacy_reconcile_failed"
    assert get_install_source_manager() is manager
    assert not is_plugin_registry_authority_configured()
    assert not result.runtime.paths.registry.exists()


@pytest.mark.asyncio
async def test_failed_reconcile_after_registry_creation_is_blocked(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    config_paths = _ConfigPaths(tmp_path)
    registry_path = config_paths.get_runtime_config_path("plugin_registry.json")
    registry_path.parent.mkdir(parents=True)
    registry_path.write_bytes(b"existing-registry-boundary")

    result = await initialize_plugin_registry_startup(
        install_source_manager=manager,
        config_paths=config_paths,
        operation_lock=_OperationLock(),
        clock=lambda: TS,
        reconciled=False,
    )

    assert result.mode == "blocked"
    assert result.error_reason == "legacy_reconcile_failed"
    assert get_install_source_manager() is None
    assert is_plugin_registry_authority_configured()
    assert registry_path.read_bytes() == b"existing-registry-boundary"


@pytest.mark.asyncio
async def test_restart_resumes_advanced_registry_without_reactivating_legacy(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    config_paths = _ConfigPaths(tmp_path)
    first = await initialize_plugin_registry_startup(
        install_source_manager=manager,
        config_paths=config_paths,
        operation_lock=_OperationLock(),
        clock=lambda: TS,
    )
    facade = first.install_source
    assert isinstance(facade, RegistryInstallSourceFacade)
    target = manager.user_root / "demo"
    target.mkdir()
    (target / "plugin.toml").write_text(
        '[plugin]\nid = "demo"\nname = "Demo"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    facade.record_import(
        directory_path=target,
        package_filename="demo.neko-plugin",
        package_sha256="a" * 64,
    )
    assert first.runtime.registry.load().revision == 2
    legacy_bytes = manager.lock_path.read_bytes()
    clear_plugin_registry_authority()
    set_global_manager(None)

    resumed = await initialize_plugin_registry_startup(
        install_source_manager=manager,
        config_paths=config_paths,
        operation_lock=_OperationLock(),
        clock=lambda: TS,
    )

    assert resumed.mode == "registry"
    assert resumed.runtime.registry.load().revision == 2
    assert resumed.runtime.registry.load().entry("demo") is not None
    assert manager.lock_path.read_bytes() == legacy_bytes
