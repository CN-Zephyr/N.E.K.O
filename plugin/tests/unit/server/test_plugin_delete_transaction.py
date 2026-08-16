from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
import shutil
import threading
from typing import Any

import pytest

from plugin.server.application.plugins import lifecycle_service as module
from plugin.server.application.plugins.mutation_guard import plugin_mutation_guard
from plugin.server.domain.errors import ServerDomainError


def _snapshot_tree(root: Path) -> dict[str, tuple[str, bytes]]:
    snapshot: dict[str, tuple[str, bytes]] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot[relative] = ("directory", b"")
        else:
            snapshot[relative] = ("file", path.read_bytes())
    return snapshot


def _write_plugin_tree(plugin_dir: Path, plugin_id: str) -> None:
    files = {
        "plugin.toml": (
            f"[plugin]\nid='{plugin_id}'\nentry='tests.fake:Plugin'\n".encode()
        ),
        "code/main.py": b"PLUGIN_VERSION = 'old'\n",
        "assets/model.bin": b"package-owned-model",
        "config/settings.json": b'{"theme":"dark"}',
        "data/user.db": b"persistent-user-database",
        "cache/index.bin": b"runtime-cache",
    }
    for relative_path, contents in files.items():
        path = plugin_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)


class _SourceManager:
    def __init__(self, plugin_dir: Path) -> None:
        self._state = {str(plugin_dir): "imported"}
        self.restore_calls = 0

    @property
    def state(self) -> dict[str, str]:
        return dict(self._state)

    def snapshot(self) -> dict[str, str]:
        return dict(self._state)

    def mark_removed(self, *, directory_path: Path, reason: str) -> None:
        assert reason == "user_overlay_removed"
        self._state.pop(str(directory_path), None)

    def restore_snapshot_for_rollback(self, snapshot: dict[str, str]) -> None:
        self.restore_calls += 1
        self._state = dict(snapshot)


class _TrackingMutationGuard:
    def __init__(
        self,
        factory: Callable[[], Any],
        exit_started: threading.Event,
    ) -> None:
        self._delegate = factory()
        self._exit_started = exit_started

    async def __aenter__(self) -> None:
        await self._delegate.__aenter__()

    async def __aexit__(self, *args: object) -> bool:
        self._exit_started.set()
        return await self._delegate.__aexit__(*args)


def _prepare_delete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    plugin_id: str,
    running: bool,
) -> tuple[Path, _SourceManager, dict[str, str], dict[str, bool], list[str]]:
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    plugin_dir = user_root / plugin_id
    builtin_root.mkdir()
    _write_plugin_tree(plugin_dir, plugin_id)

    source_manager = _SourceManager(plugin_dir)
    inventory = {plugin_id: plugin_id}
    runtime = {"running": running}
    lifecycle_calls: list[str] = []

    monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (builtin_root, user_root))
    monkeypatch.setattr(
        module,
        "_get_plugin_meta_sync",
        lambda requested_id: {
            "id": requested_id,
            "config_path": str(plugin_dir / "plugin.toml"),
        },
    )
    monkeypatch.setattr(
        module,
        "_resolve_plugin_dir_sync",
        lambda _plugin_id, _plugin_meta: plugin_dir,
    )
    monkeypatch.setattr(module, "_path_within_plugin_roots_sync", lambda _path: True)
    monkeypatch.setattr(module, "_plugin_root_kind_sync", lambda _path: "user")
    monkeypatch.setattr(module, "_builtin_plugin_exists_sync", lambda _plugin_id: False)
    monkeypatch.setattr(module, "get_install_source_manager", lambda: source_manager)
    monkeypatch.setattr(module, "capture_inventory_snapshot", lambda: dict(inventory))

    def _remove_inventory(requested_id: str) -> None:
        inventory.pop(requested_id, None)

    def _restore_inventory(snapshot: dict[str, str]) -> None:
        inventory.clear()
        inventory.update(snapshot)

    monkeypatch.setattr(module, "remove_user_installation", _remove_inventory)
    monkeypatch.setattr(module, "restore_inventory_snapshot", _restore_inventory)
    monkeypatch.setattr(module, "_plugin_is_running_sync", lambda _plugin_id: runtime["running"])

    async def _stop(_plugin_id: str) -> dict[str, object]:
        lifecycle_calls.append("stop")
        runtime["running"] = False
        return {"success": True}

    async def _start(_plugin_id: str, **_kwargs: object) -> dict[str, object]:
        lifecycle_calls.append("start")
        runtime["running"] = True
        return {"success": True}

    async def _refresh() -> dict[str, object]:
        return {"success": True}

    monkeypatch.setattr(module.PluginLifecycleService, "stop_plugin", staticmethod(_stop))
    monkeypatch.setattr(module.PluginLifecycleService, "start_plugin", staticmethod(_start))
    monkeypatch.setattr(module.plugin_registry_service, "refresh_registry", _refresh)
    monkeypatch.setattr(module, "_pop_plugin_host_sync", lambda _plugin_id: None)
    monkeypatch.setattr(module, "_remove_event_handlers_sync", lambda _plugin_id: None)
    monkeypatch.setattr(module, "_remove_plugin_metadata_sync", lambda _plugin_id: None)
    monkeypatch.setattr(module, "clear_runtime_override", lambda _plugin_id: None)
    monkeypatch.setattr(module, "emit_lifecycle_event", lambda _event: None)

    return plugin_dir, source_manager, inventory, runtime, lifecycle_calls


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_delete_partial_filesystem_failure_restores_exact_original_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_id = "partial_delete_demo"
    plugin_dir, source_manager, inventory, runtime, lifecycle_calls = _prepare_delete(
        monkeypatch,
        tmp_path,
        plugin_id=plugin_id,
        running=True,
    )
    before = _snapshot_tree(plugin_dir)
    source_before = source_manager.state

    def _fail_after_removing_one_child(target: Path) -> bool:
        shutil.rmtree(target / "code")
        raise OSError("injected failure after one child was removed")

    monkeypatch.setattr(
        module,
        "_delete_plugin_directory_sync",
        _fail_after_removing_one_child,
    )

    with pytest.raises(ServerDomainError) as exc_info:
        await module.PluginLifecycleService().delete_plugin(plugin_id)

    assert exc_info.value.code == "PLUGIN_DELETE_FAILED"
    assert plugin_dir.is_dir()
    assert _snapshot_tree(plugin_dir) == before
    assert inventory == {plugin_id: plugin_id}
    assert source_manager.state == source_before
    assert source_manager.restore_calls == 1
    assert runtime["running"] is True
    assert lifecycle_calls == ["stop", "start"]


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_delete_cancel_during_filesystem_move_holds_guard_and_restores_everything(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_id = "cancel_delete_demo"
    plugin_dir, source_manager, inventory, runtime, lifecycle_calls = _prepare_delete(
        monkeypatch,
        tmp_path,
        plugin_id=plugin_id,
        running=True,
    )
    before = _snapshot_tree(plugin_dir)
    source_before = source_manager.state
    backup_dir = plugin_dir.parent / ".delete-backups" / plugin_id
    worker_entered = asyncio.Event()
    release_worker = threading.Event()
    guard_exit_started = threading.Event()
    loop = asyncio.get_running_loop()

    def _move_then_block(target: Path) -> bool:
        backup_dir.parent.mkdir(parents=True, exist_ok=True)
        target.rename(backup_dir)
        loop.call_soon_threadsafe(worker_entered.set)
        release_worker.wait()
        return True

    original_guard_factory = plugin_mutation_guard
    monkeypatch.setattr(
        module,
        "plugin_mutation_guard",
        lambda: _TrackingMutationGuard(original_guard_factory, guard_exit_started),
    )
    monkeypatch.setattr(module, "_delete_plugin_directory_sync", _move_then_block)

    delete_task = asyncio.create_task(
        module.PluginLifecycleService().delete_plugin(plugin_id),
        name="delete-cancel-in-flight",
    )
    await worker_entered.wait()
    delete_task.cancel()

    guard_released_at_cancel_checkpoint: list[bool] = []
    checkpoint_reached = asyncio.Event()

    def _cancellation_checkpoint() -> None:
        guard_released_at_cancel_checkpoint.append(guard_exit_started.is_set())
        delete_task.cancel()
        release_worker.set()
        checkpoint_reached.set()

    loop.call_soon(_cancellation_checkpoint)
    await checkpoint_reached.wait()

    with pytest.raises(asyncio.CancelledError):
        await delete_task

    assert guard_released_at_cancel_checkpoint == [False]
    assert plugin_dir.is_dir()
    assert not backup_dir.exists()
    assert _snapshot_tree(plugin_dir) == before
    assert inventory == {plugin_id: plugin_id}
    assert source_manager.state == source_before
    assert source_manager.restore_calls == 1
    assert runtime["running"] is True
    assert lifecycle_calls == ["stop", "start"]

    guard_reacquired = False
    async with original_guard_factory():
        guard_reacquired = True
    assert guard_reacquired is True


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_delete_success_preserves_mutable_state_and_cleans_transaction_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_id = "successful_delete_demo"
    plugin_dir, source_manager, inventory, runtime, lifecycle_calls = _prepare_delete(
        monkeypatch,
        tmp_path,
        plugin_id=plugin_id,
        running=False,
    )
    expected_state = {
        name: _snapshot_tree(plugin_dir / name)
        for name in ("config", "data", "cache")
    }

    result = await module.PluginLifecycleService().delete_plugin(plugin_id)

    assert result["success"] is True
    assert result["deleted_from_disk"] is True
    assert result["user_data_preserved"] is True
    for name, snapshot in expected_state.items():
        state_dir = plugin_dir / name
        assert state_dir.is_dir()
        assert _snapshot_tree(state_dir) == snapshot
    assert not (plugin_dir / "plugin.toml").exists()
    assert not (plugin_dir / "code").exists()
    assert not (plugin_dir / "assets").exists()
    assert inventory == {}
    assert source_manager.state == {}
    assert runtime["running"] is False
    assert lifecycle_calls == []
    backup_root = plugin_dir.parent / ".delete-backups"
    assert not backup_root.exists() or not any(backup_root.iterdir())
