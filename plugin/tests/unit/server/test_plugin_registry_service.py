from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest

from plugin.server.application.plugins import registry_service as module
from plugin.server.application.install_source.models import (
    CandidateRecord,
    CandidateRef,
    Channel,
    PluginEntry,
    PluginRegistrySnapshot,
    RootId,
    StateOwnership,
)
from plugin.server.domain.plugin_candidates import CandidateKey
from plugin.server.infrastructure import plugin_selections, runtime_overrides


pytestmark = pytest.mark.plugin_unit

_REGISTRY_TS = "2026-08-27T00:00:00.000000Z"


class _AliveHost:
    def is_alive(self) -> bool:
        return True


def _write_plugin_fixture(tmp_path: Path, plugin_id: str) -> Path:
    root = tmp_path / "plugins"
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "__init__.py").write_text(
        "\n".join(
            [
                "from plugin.sdk.plugin.decorators import plugin_entry",
                "",
                "class DemoPlugin:",
                "    @plugin_entry(id='ping', name='Ping', description='Ping tool')",
                "    async def ping(self):",
                "        return {'ok': True}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (plugin_dir / "plugin.toml").write_text(
        "\n".join(
            [
                "[plugin]",
                f"id = '{plugin_id}'",
                f"name = '{plugin_id}'",
                "type = 'plugin'",
                f"entry = 'plugins.{plugin_id}:DemoPlugin'",
                "version = '0.1.0'",
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return root


def _write_ordered_plugin_fixture(
    root: Path,
    plugin_id: str,
    *,
    dependencies_block: list[str] | None = None,
) -> Path:
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.toml").write_text(
        "\n".join(
            [
                "[plugin]",
                f"id = '{plugin_id}'",
                f"name = '{plugin_id}'",
                "type = 'plugin'",
                f"entry = '{plugin_id}.module:Plugin'",
                "version = '0.1.0'",
                *(dependencies_block or []),
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return plugin_dir / "plugin.toml"


def _write_package_plugin_fixture(
    root: Path,
    directory_name: str,
    *,
    plugin_id: str | None = None,
    entry_package: str | None = None,
    source: str | None = None,
) -> Path:
    resolved_plugin_id = plugin_id or directory_name
    resolved_entry_package = entry_package or directory_name
    plugin_dir = root / directory_name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "__init__.py").write_text(
        source
        or "\n".join(
            [
                "class DemoPlugin:",
                "    pass",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (plugin_dir / "plugin.toml").write_text(
        "\n".join(
            [
                "[plugin]",
                f"id = '{resolved_plugin_id}'",
                f"name = '{resolved_plugin_id}'",
                "type = 'plugin'",
                f"entry = 'plugins.{resolved_entry_package}:DemoPlugin'",
                "version = '0.1.0'",
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return plugin_dir / "plugin.toml"


def _registry_candidate(
    root_id: RootId,
    directory_name: str,
    *,
    channel: Channel,
) -> CandidateRecord:
    return CandidateRecord(
        root_id=root_id,
        directory_name=directory_name,
        channel=channel,
        reason="user_requested",
        installed_at=_REGISTRY_TS,
        updated_at=_REGISTRY_TS,
        last_seen_at=_REGISTRY_TS,
    )


def test_registry_entry_with_only_retired_candidates_is_not_stale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    monkeypatch.setattr(module, "BUILTIN_PLUGIN_CONFIG_ROOT", builtin_root)
    removed = replace(
        _registry_candidate("user", "retired-demo", channel="manual"),
        removed=True,
        removed_at=_REGISTRY_TS,
    )

    scan = module._scan_registry_plugin_inventory_sync(
        "demo",
        PluginEntry(plugin_id="demo", candidates=(removed,)),
        (builtin_root, user_root),
    )

    assert scan is not None
    assert scan.inventory.candidates == ()
    assert scan.failures == []
    assert scan.config_paths == set()


@pytest.mark.asyncio
async def test_refresh_registry_syncs_metadata_and_marks_missing_running_plugin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_plugin_fixture(tmp_path, "demo_plugin")

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["stale_plugin"] = {
                "id": "stale_plugin",
                "name": "stale_plugin",
                "config_path": str((tmp_path / "plugins" / "stale_plugin" / "plugin.toml").resolve()),
            }
            module.state.plugins["running_removed"] = {
                "id": "running_removed",
                "name": "running_removed",
                "config_path": str((tmp_path / "plugins" / "running_removed" / "plugin.toml").resolve()),
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts["running_removed"] = _AliveHost()

        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

        service = module.PluginRegistryService()
        result = await service.refresh_registry()

        assert result["success"] is True
        assert result["added"] == ["demo_plugin"]
        assert result["removed"] == ["stale_plugin"]
        assert result["removed_running"] == ["running_removed"]

        with module.state.acquire_plugins_read_lock():
            demo_meta = dict(module.state.plugins["demo_plugin"])
            running_removed = dict(module.state.plugins["running_removed"])

        assert demo_meta["runtime_enabled"] is True
        assert demo_meta["runtime_auto_start"] is False
        assert [entry["id"] for entry in demo_meta["entries_preview"]] == ["ping"]
        assert running_removed["runtime_source_missing"] is True
        assert "stale_plugin" not in module.state.plugins
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_refresh_registry_applies_user_auto_start_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_plugin_fixture(tmp_path, "remembered_plugin")
    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        runtime_overrides.set_runtime_override(
            "remembered_plugin",
            True,
            auto_start=True,
        )
        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

        await module.PluginRegistryService().refresh_registry()

        with module.state.acquire_plugins_read_lock():
            plugin_meta = dict(module.state.plugins["remembered_plugin"])
        assert plugin_meta["runtime_enabled"] is True
        assert plugin_meta["runtime_auto_start"] is True
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "load_overrides",
    [
        lambda: (_ for _ in ()).throw(
            runtime_overrides.RuntimeOverrideReadError("invalid json")
        ),
        lambda: runtime_overrides._coerce_overrides(
            {
                "manifest_plugin": {
                    "enabled": False,
                    "auto_start": "yes",
                }
            }
        ),
    ],
    ids=("unreadable-file", "invalid-plugin-entry"),
)
async def test_refresh_registry_uses_manifest_defaults_when_overrides_are_invalid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    load_overrides,
) -> None:
    root = _write_plugin_fixture(tmp_path, "manifest_plugin")
    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    monkeypatch.setattr(
        runtime_overrides,
        "_load_from_disk",
        load_overrides,
    )
    runtime_overrides.reset_cache_for_testing()
    monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

    try:
        await module.PluginRegistryService().refresh_registry()

        with module.state.acquire_plugins_read_lock():
            plugin_meta = dict(module.state.plugins["manifest_plugin"])
        assert plugin_meta["runtime_enabled"] is True
        assert plugin_meta["runtime_auto_start"] is False
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_refresh_plugin_returns_updated_status_for_existing_plugin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_plugin_fixture(tmp_path, "refresh_me")

    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["refresh_me"] = {
                "id": "refresh_me",
                "name": "Old Name",
                "config_path": str((root / "refresh_me" / "plugin.toml").resolve()),
                "runtime_enabled": True,
                "runtime_auto_start": True,
                "entries_preview": [],
            }

        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

        service = module.PluginRegistryService()
        payload = await service.refresh_plugin("refresh_me")

        assert payload["success"] is True
        assert payload["plugin_id"] == "refresh_me"
        assert payload["status"] == "updated"

        with module.state.acquire_plugins_read_lock():
            refreshed = dict(module.state.plugins["refresh_me"])
        assert refreshed["name"] == "refresh_me"
        assert [entry["id"] for entry in refreshed["entries_preview"]] == ["ping"]
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_refresh_plugin_checks_python_requirements_against_vendor_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_plugin_fixture(tmp_path, "vendor_refresh")
    plugin_dir = root / "vendor_refresh"
    vendor_dir = plugin_dir / "vendor"
    vendor_dir.mkdir()
    (plugin_dir / "pyproject.toml").write_text(
        '[project]\ndependencies = ["demo-lib>=2"]\n',
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    def _fake_find_missing(requirements, *, search_paths=None):
        seen["requirements"] = list(requirements)
        seen["search_paths"] = list(search_paths or [])
        return []

    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)
    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["vendor_refresh"] = {
                "id": "vendor_refresh",
                "name": "Vendor Refresh",
                "config_path": str((plugin_dir / "plugin.toml").resolve()),
            }

        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))
        monkeypatch.setattr(module, "_find_missing_python_requirements", _fake_find_missing)

        payload = await module.PluginRegistryService().refresh_plugin("vendor_refresh")

        assert payload["success"] is True
        assert seen["requirements"] == ["demo-lib>=2"]
        assert seen["search_paths"] == [vendor_dir]
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_refresh_registry_keeps_existing_metadata_when_config_parse_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    plugin_dir = root / "broken_plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    config_path = plugin_dir / "plugin.toml"
    config_path.write_text("[plugin\nid='broken_plugin'\n", encoding="utf-8")

    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["broken_plugin"] = {
                "id": "broken_plugin",
                "name": "Broken Plugin",
                "config_path": str(config_path.resolve()),
                "runtime_enabled": True,
                "runtime_auto_start": False,
            }

        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

        service = module.PluginRegistryService()
        result = await service.refresh_registry()

        assert result["success"] is False
        assert result["removed"] == []
        assert result["removed_running"] == []
        assert len(result["failed"]) == 1
        assert result["failed"][0]["config_path"] == str(config_path.resolve())

        with module.state.acquire_plugins_read_lock():
            preserved = dict(module.state.plugins["broken_plugin"])
        assert preserved["name"] == "Broken Plugin"
        assert "runtime_source_missing" not in preserved
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_refresh_registry_marks_syntax_error_plugin_failed_without_aborting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    _write_package_plugin_fixture(root, "healthy_plugin")
    _write_package_plugin_fixture(
        root,
        "broken_plugin",
        source="def broken(:\n    pass\n",
    )

    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()

        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

        result = await module.PluginRegistryService().refresh_registry()

        assert result["success"] is True
        with module.state.acquire_plugins_read_lock():
            healthy = dict(module.state.plugins["healthy_plugin"])
            broken = dict(module.state.plugins["broken_plugin"])

        assert healthy.get("runtime_load_state") != "failed"
        assert broken["runtime_load_state"] == "failed"
        assert broken["runtime_load_error_type"] == "SyntaxError"
        assert broken["runtime_load_error_phase"] == "import_module"
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_refresh_registry_marks_entry_directory_mismatch_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    _write_package_plugin_fixture(
        root,
        "repo_file_manager",
        plugin_id="file_manager",
        entry_package="file_manager",
    )

    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()

        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

        result = await module.PluginRegistryService().refresh_registry()

        assert result["success"] is True
        with module.state.acquire_plugins_read_lock():
            plugin_meta = dict(module.state.plugins["file_manager"])

        assert plugin_meta["runtime_load_state"] == "failed"
        assert plugin_meta["runtime_load_error_type"] == "PluginEntryDirectoryMismatch"
        assert "repo_file_manager" in plugin_meta["runtime_load_error_message"]
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_refresh_registry_prioritizes_entry_directory_mismatch_before_requirements(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    config_path = _write_package_plugin_fixture(
        root,
        "repo_file_manager",
        plugin_id="file_manager",
        entry_package="file_manager",
    )
    (config_path.parent / "pyproject.toml").write_text(
        '[project]\ndependencies = ["definitely-missing-lib>=1"]\n',
        encoding="utf-8",
    )
    requirements_checked = False

    def _fake_find_missing(requirements, *, search_paths=None):
        nonlocal requirements_checked
        requirements_checked = True
        return ["definitely-missing-lib>=1"]

    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()

        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))
        monkeypatch.setattr(module, "_find_missing_python_requirements", _fake_find_missing)

        result = await module.PluginRegistryService().refresh_registry()

        assert result["success"] is True
        assert requirements_checked is False
        with module.state.acquire_plugins_read_lock():
            plugin_meta = dict(module.state.plugins["file_manager"])

        assert plugin_meta["runtime_load_state"] == "failed"
        assert plugin_meta["runtime_load_error_type"] == "PluginEntryDirectoryMismatch"
        assert plugin_meta["runtime_load_error_phase"] == "entry_validation"
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_list_autostart_plugin_ids_uses_dependency_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    provider_config = _write_ordered_plugin_fixture(root, "provider")
    consumer_config = _write_ordered_plugin_fixture(
        root,
        "consumer",
        dependencies_block=[
            "",
            "dependencies = ['provider']",
        ],
    )

    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["consumer"] = {
                "id": "consumer",
                "type": "plugin",
                "config_path": str(consumer_config.resolve()),
                "runtime_enabled": True,
                "runtime_auto_start": True,
            }
            module.state.plugins["provider"] = {
                "id": "provider",
                "type": "plugin",
                "config_path": str(provider_config.resolve()),
                "runtime_enabled": True,
                "runtime_auto_start": True,
            }

        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

        service = module.PluginRegistryService()
        ordered = await service.list_autostart_plugin_ids()

        assert ordered == ["provider", "consumer"]
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_refresh_plugin_marks_missing_simple_plugin_dependency_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_plugin_fixture(tmp_path, "consumer")
    config_path = root / "consumer" / "plugin.toml"
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'consumer'",
                "name = 'consumer'",
                "type = 'plugin'",
                "entry = 'consumer_entry:DemoPlugin'",
                "version = '0.1.0'",
                "dependencies = ['missing_provider']",
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = false",
                "",
            ]
        ),
        encoding="utf-8",
    )

    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)
    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["consumer"] = {
                "id": "consumer",
                "name": "consumer",
                "config_path": str(config_path.resolve()),
                "runtime_enabled": True,
                "runtime_auto_start": False,
            }

        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

        payload = await module.PluginRegistryService().refresh_plugin("consumer")

        assert payload["success"] is True
        with module.state.acquire_plugins_read_lock():
            refreshed = dict(module.state.plugins["consumer"])
        assert refreshed["runtime_load_state"] == "failed"
        assert refreshed["runtime_load_error_type"] == "DependencyCheckFailed"
        assert refreshed["runtime_load_error_phase"] == "dependency_check"
        assert "missing_provider" in refreshed["runtime_load_error_message"]
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_refresh_registry_groups_duplicate_declared_ids_and_registers_one_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    first_dir = root / "demo"
    second_dir = root / "demo_1"
    first_dir.mkdir(parents=True, exist_ok=True)
    second_dir.mkdir(parents=True, exist_ok=True)

    (tmp_path / "demo_entry.py").write_text(
        "\n".join(
            [
                "class DemoPlugin:",
                "    pass",
                "",
            ]
        ),
        encoding="utf-8",
    )
    for plugin_dir in (first_dir, second_dir):
        (plugin_dir / "plugin.toml").write_text(
            "\n".join(
                [
                    "[plugin]",
                    "id = 'demo'",
                    "name = 'demo'",
                    "type = 'plugin'",
                    "entry = 'demo_entry:DemoPlugin'",
                    "version = '0.1.0'",
                    "",
                    "[plugin_runtime]",
                    "enabled = true",
                    "auto_start = false",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()

        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

        service = module.PluginRegistryService()
        result = await service.refresh_registry()
        second_result = await service.refresh_registry()
        refreshed = await service.refresh_plugin("demo")

        assert result["success"] is True
        assert result["failed"] == []
        assert result["added"] == ["demo"]
        assert result["scanned_count"] == 2
        assert result["selected_count"] == 1
        assert second_result["success"] is True
        assert second_result["failed"] == []
        assert second_result["added"] == []
        assert second_result["unchanged"] == ["demo"]
        assert refreshed["success"] is True
        assert refreshed["plugin_id"] == "demo"
        assert refreshed["status"] == "unchanged"

        with module.state.acquire_plugins_read_lock():
            plugin_meta = dict(module.state.plugins["demo"])

        assert set(module.state.plugins) == {"demo"}
        assert Path(plugin_meta["config_path"]).parent.name == "demo"
        assert plugin_meta["id"] == "demo"
        assert plugin_meta["available_candidate_count"] == 2
        assert plugin_meta["selection_reason"] == "auto_canonical_directory"
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_refresh_registry_imports_only_the_resolved_candidate_and_persists_choice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    builtin_config = _write_package_plugin_fixture(
        builtin_root,
        "demo",
    )
    user_config = _write_package_plugin_fixture(
        user_root,
        "demo",
    )
    imported_paths: list[Path] = []

    class _DemoPlugin:
        pass

    def _fake_import(_module_path: str, config_path: Path, _logger):
        imported_paths.append(config_path.resolve())
        return type("_Module", (), {"DemoPlugin": _DemoPlugin})

    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)
    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
        monkeypatch.setattr(module, "BUILTIN_PLUGIN_CONFIG_ROOT", builtin_root)
        monkeypatch.setattr(
            module,
            "PLUGIN_CONFIG_ROOTS",
            (builtin_root, user_root),
        )
        monkeypatch.setattr(module, "_import_plugin_module", _fake_import)

        first = await module.PluginRegistryService().refresh_registry()

        assert first["success"] is True
        assert first["scanned_count"] == 2
        assert first["selected_count"] == 1
        assert imported_paths == [builtin_config.resolve()]
        with module.state.acquire_plugins_read_lock():
            builtin_meta = dict(module.state.plugins["demo"])
        assert builtin_meta["selected_candidate"]["root_id"] == "builtin"
        assert builtin_meta["selected_candidate"]["source"] == "builtin"

        plugin_selections.set_plugin_selection(
            "demo",
            CandidateKey(root_id="user", directory_name="demo"),
            candidate_source="manual",
            state_access_grant="user_authorized",
            authorized_at="2026-08-26T07:00:00Z",
        )
        plugin_selections.reset_cache_for_testing()
        imported_paths.clear()

        second = await module.PluginRegistryService().refresh_registry()

        assert second["success"] is True
        assert imported_paths == [user_config.resolve()]
        with module.state.acquire_plugins_read_lock():
            user_meta = dict(module.state.plugins["demo"])
        assert Path(user_meta["config_path"]).resolve() == user_config.resolve()
        assert user_meta["selection_reason"] == "explicit_selection"
        assert user_meta["selected_candidate"]["root_id"] == "user"
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_plan_selected_candidate_removal_resolves_builtin_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    _write_package_plugin_fixture(builtin_root, "demo")
    _write_package_plugin_fixture(user_root, "demo-market", plugin_id="demo")
    selected = CandidateKey(root_id="user", directory_name="demo-market")

    monkeypatch.setattr(module, "BUILTIN_PLUGIN_CONFIG_ROOT", builtin_root)
    monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (builtin_root, user_root))
    plugin_selections.set_plugin_selection(
        "demo",
        selected,
        candidate_source="manual",
        state_access_grant="user_authorized",
        authorized_at="2026-08-26T07:00:00Z",
    )

    plan = await module.PluginRegistryService().plan_plugin_candidate_removal(
        "demo",
        selected,
    )

    assert plan == {
        "plugin_id": "demo",
        "removed_candidate": {
            "root_id": "user",
            "directory_name": "demo-market",
        },
        "fallback_candidate": {
            "root_id": "builtin",
            "directory_name": "demo",
        },
        "fallback_reason": "fallback_builtin",
    }
    assert plugin_selections.get_plugin_selection("demo") == selected


@pytest.mark.asyncio
async def test_registry_snapshot_fast_path_matches_legacy_candidate_queries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    _write_package_plugin_fixture(builtin_root, "demo")
    _write_package_plugin_fixture(user_root, "demo-market", plugin_id="demo")
    selected = CandidateKey(root_id="user", directory_name="demo-market")

    monkeypatch.setattr(module, "BUILTIN_PLUGIN_CONFIG_ROOT", builtin_root)
    monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (builtin_root, user_root))
    plugin_selections.set_plugin_selection(
        "demo",
        selected,
        candidate_source="manual",
        state_access_grant="user_authorized",
        authorized_at="2026-08-26T07:00:00Z",
    )
    legacy_service = module.PluginRegistryService()
    expected_candidates = await legacy_service.list_plugin_candidates("demo")
    expected_plan = await legacy_service.plan_plugin_candidate_removal(
        "demo",
        selected,
    )

    selected_ref = CandidateRef(root_id="user", directory_name="demo-market")
    snapshot = PluginRegistrySnapshot.build(
        {
            "demo": PluginEntry(
                plugin_id="demo",
                candidates=(
                    _registry_candidate("builtin", "demo", channel="builtin"),
                    _registry_candidate("user", "demo-market", channel="manual"),
                ),
                selected_candidate=selected_ref,
                candidate_source="manual",
                state_owner=StateOwnership(
                    candidate=selected_ref,
                    state_scope="legacy_shared",
                    state_access_grant="user_authorized",
                    authorized_at="2026-08-26T07:00:00Z",
                ),
            )
        },
        revision=7,
        updated_at=_REGISTRY_TS,
    )
    # Prove the injected Registry is the authority for the fast view rather
    # than accidentally consulting the legacy selection cache.
    plugin_selections.set_plugin_selection(
        "demo",
        CandidateKey(root_id="builtin", directory_name="demo"),
    )

    def reject_full_scan(_roots: tuple[Path, ...]) -> module.PluginInventoryScan:
        raise AssertionError("Registry fast path unexpectedly performed a full scan")

    monkeypatch.setattr(module, "_scan_plugin_inventory_sync", reject_full_scan)
    registry_service = module.PluginRegistryService(
        registry_snapshot_provider=lambda: snapshot
    )

    actual_candidates = await registry_service.list_plugin_candidates("demo")
    actual_plan = await registry_service.plan_plugin_candidate_removal(
        "demo",
        selected,
    )

    assert actual_candidates == expected_candidates
    assert actual_plan == expected_plan


@pytest.mark.asyncio
async def test_registry_snapshot_stale_candidate_fails_closed_without_full_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    _write_package_plugin_fixture(user_root, "demo")
    snapshot = PluginRegistrySnapshot.build(
        {
            "demo": PluginEntry(
                plugin_id="demo",
                candidates=(
                    _registry_candidate("user", "missing-demo", channel="manual"),
                ),
            )
        },
        revision=8,
        updated_at=_REGISTRY_TS,
    )

    monkeypatch.setattr(module, "BUILTIN_PLUGIN_CONFIG_ROOT", builtin_root)
    monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (builtin_root, user_root))
    scan_calls = 0

    def count_full_scan(_roots: tuple[Path, ...]) -> module.PluginInventoryScan:
        nonlocal scan_calls
        scan_calls += 1
        raise AssertionError("authoritative Registry must not fall back to scanning")

    monkeypatch.setattr(module, "_scan_plugin_inventory_sync", count_full_scan)
    service = module.PluginRegistryService(registry_snapshot_provider=lambda: snapshot)

    with pytest.raises(module.ServerDomainError) as exc_info:
        await service.list_plugin_candidates("demo")

    assert scan_calls == 0
    assert exc_info.value.code == "PLUGIN_REGISTRY_STALE"
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_configured_registry_provider_must_be_initialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_full_scan(_roots: tuple[Path, ...]) -> module.PluginInventoryScan:
        raise AssertionError("uninitialized Registry must not fall back to scanning")

    monkeypatch.setattr(module, "_scan_plugin_inventory_sync", reject_full_scan)
    service = module.PluginRegistryService(registry_snapshot_provider=lambda: None)

    with pytest.raises(module.ServerDomainError) as exc_info:
        await service.list_plugin_candidates("demo")

    assert exc_info.value.code == "PLUGIN_REGISTRY_NOT_READY"
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_configured_registry_read_failure_fails_closed_without_full_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_full_scan(_roots: tuple[Path, ...]) -> module.PluginInventoryScan:
        raise AssertionError("unreadable Registry must not fall back to scanning")

    def fail_read() -> PluginRegistrySnapshot:
        raise OSError("registry unavailable")

    monkeypatch.setattr(module, "_scan_plugin_inventory_sync", reject_full_scan)
    service = module.PluginRegistryService(registry_snapshot_provider=fail_read)

    with pytest.raises(module.ServerDomainError) as exc_info:
        await service.list_plugin_candidates("demo")

    assert exc_info.value.code == "PLUGIN_REGISTRY_UNAVAILABLE"
    assert exc_info.value.status_code == 503
    assert exc_info.value.details == {
        "authority": "plugin_registry",
        "error_type": "OSError",
    }


@pytest.mark.asyncio
async def test_authoritative_registry_refresh_never_performs_full_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    _write_package_plugin_fixture(user_root, "demo")
    snapshot = PluginRegistrySnapshot.build(
        {
            "demo": PluginEntry(
                plugin_id="demo",
                candidates=(
                    _registry_candidate("user", "demo", channel="manual"),
                ),
            )
        },
        revision=9,
        updated_at=_REGISTRY_TS,
    )
    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    class _DemoPlugin:
        pass

    def reject_full_scan(_roots: tuple[Path, ...]) -> module.PluginInventoryScan:
        raise AssertionError("authoritative Registry refresh performed a full scan")

    monkeypatch.setattr(module, "BUILTIN_PLUGIN_CONFIG_ROOT", builtin_root)
    monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (builtin_root, user_root))
    monkeypatch.setattr(module, "_scan_plugin_inventory_sync", reject_full_scan)
    monkeypatch.setattr(
        module,
        "_import_plugin_module",
        lambda *_args, **_kwargs: type("_Module", (), {"DemoPlugin": _DemoPlugin}),
    )

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
        service = module.PluginRegistryService(
            registry_snapshot_provider=lambda: snapshot
        )
        result = await service.refresh_registry()
        refreshed = await service.refresh_plugin("demo")
        validated = await service.validate_plugin_candidate(
            "demo",
            CandidateKey(root_id="user", directory_name="demo"),
        )

        assert result["success"] is True
        assert result["scanned_count"] == 1
        assert result["selected_count"] == 1
        assert refreshed["plugin_id"] == "demo"
        assert validated["candidate"] == {
            "root_id": "user",
            "directory_name": "demo",
        }
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_restart_rejects_legacy_external_selection_without_state_grant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_plugin_candidate_selections: dict[str, object],
) -> None:
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    builtin_config = _write_package_plugin_fixture(builtin_root, "demo")
    _write_package_plugin_fixture(user_root, "demo")
    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    class _DemoPlugin:
        pass

    monkeypatch.setattr(module, "BUILTIN_PLUGIN_CONFIG_ROOT", builtin_root)
    monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (builtin_root, user_root))
    monkeypatch.setattr(
        module,
        "_import_plugin_module",
        lambda *_args, **_kwargs: type("_Module", (), {"DemoPlugin": _DemoPlugin}),
    )
    _isolate_plugin_candidate_selections.clear()
    _isolate_plugin_candidate_selections.update(
        {
            "schema_version": 1,
            "selections": {
                "demo": {"root_id": "user", "directory_name": "demo"}
            },
        }
    )
    plugin_selections.reset_cache_for_testing()

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()

        result = await module.PluginRegistryService().refresh_registry()

        assert result["success"] is True
        with module.state.acquire_plugins_read_lock():
            metadata = dict(module.state.plugins["demo"])
        assert Path(metadata["config_path"]).resolve() == builtin_config.resolve()
        assert metadata["selection_reason"] == "state_authorization_required"
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_refresh_registry_does_not_treat_running_candidate_as_override_or_switch_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    builtin_config = _write_package_plugin_fixture(builtin_root, "demo")
    user_config = _write_package_plugin_fixture(user_root, "demo")
    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    class _DemoPlugin:
        pass

    monkeypatch.setattr(module, "BUILTIN_PLUGIN_CONFIG_ROOT", builtin_root)
    monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (builtin_root, user_root))
    monkeypatch.setattr(
        module,
        "_import_plugin_module",
        lambda *_args, **_kwargs: type("_Module", (), {"DemoPlugin": _DemoPlugin}),
    )

    try:
        plugin_selections.set_plugin_selection(
            "demo",
            CandidateKey(root_id="user", directory_name="demo"),
            candidate_source="manual",
            state_access_grant="user_authorized",
            authorized_at="2026-08-26T07:00:00Z",
        )
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["demo"] = {
                "id": "demo",
                "config_path": str(builtin_config.resolve()),
                "selected_candidate": {
                    "root_id": "builtin",
                    "directory_name": "demo",
                },
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts["demo"] = _AliveHost()

        candidates = await module.PluginRegistryService().list_plugin_candidates("demo")
        result = await module.PluginRegistryService().refresh_registry()

        assert candidates["effective_candidate"] == {
            "root_id": "user",
            "directory_name": "demo",
        }
        assert candidates["running_candidate"] == {
            "root_id": "builtin",
            "directory_name": "demo",
        }
        assert result["success"] is False
        assert result["failed"] == [
            {
                "plugin_id": "demo",
                "config_path": str(user_config.resolve()),
                "error": (
                    "running plugin candidate can only change through "
                    "the lifecycle switch operation"
                ),
            }
        ]
        with module.state.acquire_plugins_read_lock():
            assert Path(module.state.plugins["demo"]["config_path"]).resolve() == builtin_config.resolve()
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup
