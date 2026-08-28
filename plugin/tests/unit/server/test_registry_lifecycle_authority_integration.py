from __future__ import annotations

import copy
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
from plugin.server.application.install_source.models import (
    CandidateRecord,
    CandidateRef,
    LockFile,
    PluginEntry,
    PluginRegistrySnapshot,
    SourceDetailMarket,
    StateOwnership,
)
from plugin.server.application.install_source.scanner import PluginDirectoryScanner
from plugin.server.application.plugins import lifecycle_service as lifecycle_module
from plugin.server.application.plugins import registry_service as registry_module
from plugin.server.domain.plugin_candidates import CandidateKey
from plugin.server.domain.errors import ServerDomainError
from plugin.server.infrastructure.package_management.install_source_facade import (
    RegistryInstallSourceFacade,
)
from plugin.server.infrastructure.package_management.json_registry import (
    JsonPluginRegistry,
)
from plugin.server.infrastructure.plugin_registry_authority import (
    clear_plugin_registry_authority,
    publish_plugin_registry_authority,
)


pytestmark = pytest.mark.plugin_unit
TS = "2026-08-28T00:00:00.000000Z"


class _CandidateHost:
    def __init__(self, candidate: CandidateKey) -> None:
        self.candidate = candidate

    async def start(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def shutdown(self, *_args: object, **_kwargs: object) -> None:
        return None

    def is_alive(self) -> bool:
        return True


def _record(
    root_id: str,
    directory_name: str,
    channel: str,
) -> CandidateRecord:
    market_detail = (
        SourceDetailMarket(
            plugin_market_id="market.demo",
            version="1.0.0",
            package_url="https://example.invalid/demo.neko-plugin",
            package_sha256="a" * 64,
            payload_hash=None,
            channel="stable",
            published_at=TS,
        )
        if channel == "market"
        else None
    )
    return CandidateRecord(
        root_id=root_id,  # type: ignore[arg-type]
        directory_name=directory_name,
        channel=channel,  # type: ignore[arg-type]
        reason="user_requested",
        installed_at=TS,
        updated_at=TS,
        last_seen_at=TS,
        source_detail=market_detail,
        package_id=f"package-{directory_name}",
        profile_installed=False,
    )


def _write_plugin(root: Path, directory_name: str, plugin_id: str) -> Path:
    directory = root / directory_name
    directory.mkdir(parents=True)
    (directory / "plugin.toml").write_text(
        f'[plugin]\nid = "{plugin_id}"\nentry = "tests.fake:Plugin"\n',
        encoding="utf-8",
    )
    return directory


@pytest.mark.asyncio
async def test_delete_last_candidate_keeps_registry_tombstone_authoritative(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_id = "demo"
    candidate = CandidateKey(root_id="user", directory_name="demo-market")
    candidate_ref = CandidateRef(
        root_id=candidate.root_id,
        directory_name=candidate.directory_name,
    )
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    candidate_dir = _write_plugin(
        user_root,
        candidate.directory_name,
        plugin_id,
    )
    registry = JsonPluginRegistry(
        tmp_path / "plugin_registry.json",
        clock=lambda: TS,
    )
    registry.initialize(
        PluginRegistrySnapshot.build(
            {
                plugin_id: PluginEntry(
                    plugin_id=plugin_id,
                    candidates=(
                        _record("user", candidate.directory_name, "market"),
                    ),
                    selected_candidate=candidate_ref,
                    candidate_source="market",
                    enabled=True,
                    auto_start=True,
                    state_owner=StateOwnership(
                        candidate=candidate_ref,
                        state_scope="legacy_shared",
                        state_access_grant="initial_identity",
                        release_chain_id="market.demo",
                    ),
                )
            },
            revision=1,
            updated_at=TS,
            created_at=TS,
        )
    )
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
    legacy_bytes = lock_path.read_bytes()
    legacy_manager = InstallSourceManager(
        lock_path=lock_path,
        builtin_root=builtin_root,
        user_root=user_root,
        scanner=PluginDirectoryScanner(builtin_root, user_root),
        clock=lambda: datetime(2026, 8, 28, tzinfo=UTC),
    )
    legacy_manager.load()
    facade = RegistryInstallSourceFacade(
        legacy_manager=legacy_manager,
        registry=registry,
        clock=lambda: TS,
    )
    previous_manager = get_install_source_manager()
    plugins_backup = copy.deepcopy(lifecycle_module.state.plugins)
    hosts_backup = dict(lifecycle_module.state.plugin_hosts)

    monkeypatch.setenv(
        "NEKO_PLUGIN_OPERATION_LOCK_PATH",
        str(tmp_path / "plugin-operation.lock"),
    )
    monkeypatch.setattr(
        lifecycle_module,
        "PLUGIN_CONFIG_ROOTS",
        (builtin_root, user_root),
    )
    monkeypatch.setattr(
        registry_module,
        "PLUGIN_CONFIG_ROOTS",
        (builtin_root, user_root),
    )
    monkeypatch.setattr(lifecycle_module, "emit_lifecycle_event", lambda _event: None)

    clear_plugin_registry_authority()
    publish_plugin_registry_authority(registry)
    set_global_manager(facade)  # type: ignore[arg-type]
    try:
        with lifecycle_module.state.acquire_plugins_write_lock():
            lifecycle_module.state.plugins.clear()
            lifecycle_module.state.plugins[plugin_id] = {
                "id": plugin_id,
                "config_path": str(candidate_dir / "plugin.toml"),
                "selected_candidate": lifecycle_module._candidate_key_payload(candidate),
                "available_candidate_count": 1,
            }
        with lifecycle_module.state.acquire_plugin_hosts_write_lock():
            lifecycle_module.state.plugin_hosts.clear()

        result = await lifecycle_module.PluginLifecycleService().delete_plugin(plugin_id)

        entry = registry.load().entry(plugin_id)
        assert entry is not None
        assert entry.selected_candidate is None
        assert entry.candidate_source is None
        assert entry.state_owner is not None
        assert entry.state_owner.candidate == candidate_ref
        assert entry.enabled is None
        assert entry.auto_start is None
        assert entry.live_candidates() == ()
        retired = entry.candidate_for(candidate_ref)
        assert retired is not None
        assert retired.removed is True
        assert candidate_dir.exists() is False
        assert facade.list_entries() == []
        assert len(facade.list_entries(include_removed=True)) == 1
        assert lock_path.read_bytes() == legacy_bytes
        assert result["fallback_candidate"] is None
        assert result["fallback_started"] is False
        with lifecycle_module.state.acquire_plugins_read_lock():
            assert plugin_id not in lifecycle_module.state.plugins

        # Simulate leftover/reintroduced bytes at the exact retired slot. A
        # fresh Registry projection must ignore them instead of falling back to
        # the directory scanner and silently resurrecting the plugin.
        _write_plugin(user_root, candidate.directory_name, plugin_id)

        def reject_full_scan(_roots: tuple[Path, ...]) -> registry_module.PluginInventoryScan:
            raise AssertionError("retired Registry candidate must not trigger a full scan")

        monkeypatch.setattr(
            registry_module,
            "_scan_plugin_inventory_sync",
            reject_full_scan,
        )
        restarted_service = registry_module.PluginRegistryService(
            registry_snapshot_provider=registry.load,
        )
        refresh = await restarted_service.refresh_registry()

        assert refresh["success"] is True
        assert refresh["added"] == []
        assert refresh["selected_count"] == 0
        assert refresh["scanned_count"] == 0
        with lifecycle_module.state.acquire_plugins_read_lock():
            assert plugin_id not in lifecycle_module.state.plugins
        with pytest.raises(ServerDomainError) as exc_info:
            await restarted_service.list_plugin_candidates(plugin_id)
        assert exc_info.value.code == "PLUGIN_CONFIG_NOT_FOUND"
        assert registry.load().entry(plugin_id) == entry
        assert lock_path.read_bytes() == legacy_bytes
    finally:
        clear_plugin_registry_authority(expected=registry)
        set_global_manager(previous_manager)
        with lifecycle_module.state.acquire_plugins_write_lock():
            lifecycle_module.state.plugins.clear()
            lifecycle_module.state.plugins.update(plugins_backup)
        with lifecycle_module.state.acquire_plugin_hosts_write_lock():
            lifecycle_module.state.plugin_hosts.clear()
            lifecycle_module.state.plugin_hosts.update(hosts_backup)


@pytest.mark.asyncio
async def test_delete_selected_running_candidate_commits_registry_fallback_before_retire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_id = "demo"
    builtin = CandidateKey(root_id="builtin", directory_name=plugin_id)
    market = CandidateKey(root_id="user", directory_name="demo-market")
    builtin_ref = CandidateRef(
        root_id=builtin.root_id,
        directory_name=builtin.directory_name,
    )
    market_ref = CandidateRef(
        root_id=market.root_id,
        directory_name=market.directory_name,
    )
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    builtin_dir = _write_plugin(builtin_root, builtin.directory_name, plugin_id)
    market_dir = _write_plugin(user_root, market.directory_name, plugin_id)
    registry = JsonPluginRegistry(
        tmp_path / "plugin_registry.json",
        clock=lambda: TS,
    )
    registry.initialize(
        PluginRegistrySnapshot.build(
            {
                plugin_id: PluginEntry(
                    plugin_id=plugin_id,
                    candidates=(
                        _record("builtin", builtin.directory_name, "builtin"),
                        _record("user", market.directory_name, "market"),
                    ),
                    selected_candidate=market_ref,
                    candidate_source="market",
                    enabled=True,
                    auto_start=True,
                    state_owner=StateOwnership(
                        candidate=market_ref,
                        state_scope="legacy_shared",
                        state_access_grant="initial_identity",
                        release_chain_id="market.demo",
                    ),
                )
            },
            revision=1,
            updated_at=TS,
            created_at=TS,
        )
    )
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
    legacy_bytes = lock_path.read_bytes()
    legacy_manager = InstallSourceManager(
        lock_path=lock_path,
        builtin_root=builtin_root,
        user_root=user_root,
        scanner=PluginDirectoryScanner(builtin_root, user_root),
        clock=lambda: datetime(2026, 8, 28, tzinfo=UTC),
    )
    legacy_manager.load()
    facade = RegistryInstallSourceFacade(
        legacy_manager=legacy_manager,
        registry=registry,
        clock=lambda: TS,
    )
    previous_manager = get_install_source_manager()
    plugins_backup = copy.deepcopy(lifecycle_module.state.plugins)
    hosts_backup = dict(lifecycle_module.state.plugin_hosts)
    events: list[str] = []

    async def _list_candidates(_plugin_id: str) -> dict[str, object]:
        return {
            "plugin_id": plugin_id,
            "effective_candidate": lifecycle_module._candidate_key_payload(market),
            "registered_candidate": lifecycle_module._candidate_key_payload(market),
            "running_candidate": lifecycle_module._candidate_key_payload(market),
            "candidates": [
                {
                    "key": lifecycle_module._candidate_key_payload(builtin),
                    "source": "builtin",
                },
                {
                    "key": lifecycle_module._candidate_key_payload(market),
                    "source": "market",
                    "release_chain_id": "market.demo",
                },
            ],
        }

    async def _validate(_plugin_id: str, candidate: CandidateKey) -> dict[str, object]:
        assert candidate == builtin
        events.append("validate")
        return {"valid": True, "source": "builtin", "runtime_enabled": True}

    async def _refresh_plugin(
        _plugin_id: str,
        *,
        transient_candidate: CandidateKey | None = None,
    ) -> dict[str, object]:
        assert transient_candidate == builtin
        events.append("apply_builtin")
        with lifecycle_module.state.acquire_plugins_write_lock():
            lifecycle_module.state.plugins[plugin_id] = {
                "id": plugin_id,
                "config_path": str(builtin_dir / "plugin.toml"),
                "selected_candidate": lifecycle_module._candidate_key_payload(builtin),
                "available_candidate_count": 2,
            }
        return {"success": True, "plugin_id": plugin_id}

    async def _plan_removal(
        _plugin_id: str,
        candidate: CandidateKey,
    ) -> dict[str, object]:
        assert candidate == market
        events.append("plan")
        return {
            "plugin_id": plugin_id,
            "removed_candidate": lifecycle_module._candidate_key_payload(market),
            "fallback_candidate": lifecycle_module._candidate_key_payload(builtin),
            "fallback_reason": "fallback_builtin",
        }

    async def _refresh_registry() -> dict[str, object]:
        events.append("refresh")
        return {"success": True}

    service = lifecycle_module.PluginLifecycleService()

    async def _stop(_plugin_id: str, **_kwargs: object) -> dict[str, object]:
        events.append("stop_market")
        with lifecycle_module.state.acquire_plugin_hosts_write_lock():
            lifecycle_module.state.plugin_hosts.pop(plugin_id, None)
        return {"success": True, "plugin_id": plugin_id}

    async def _start(_plugin_id: str, **_kwargs: object) -> dict[str, object]:
        events.append("start_builtin")
        with lifecycle_module.state.acquire_plugin_hosts_write_lock():
            lifecycle_module.state.plugin_hosts[plugin_id] = _CandidateHost(builtin)
        return {"success": True, "plugin_id": plugin_id}

    monkeypatch.setattr(
        lifecycle_module.plugin_registry_service,
        "list_plugin_candidates",
        _list_candidates,
    )
    monkeypatch.setattr(
        lifecycle_module.plugin_registry_service,
        "validate_plugin_candidate",
        _validate,
    )
    monkeypatch.setattr(
        lifecycle_module.plugin_registry_service,
        "refresh_plugin",
        _refresh_plugin,
    )
    monkeypatch.setattr(
        lifecycle_module.plugin_registry_service,
        "plan_plugin_candidate_removal",
        _plan_removal,
    )
    monkeypatch.setattr(
        lifecycle_module.plugin_registry_service,
        "refresh_registry",
        _refresh_registry,
    )
    monkeypatch.setattr(service, "stop_plugin", _stop)
    monkeypatch.setattr(service, "start_plugin", _start)
    monkeypatch.setattr(lifecycle_module, "PLUGIN_CONFIG_ROOTS", (builtin_root, user_root))
    monkeypatch.setattr(lifecycle_module, "emit_lifecycle_event", lambda _event: None)

    clear_plugin_registry_authority()
    publish_plugin_registry_authority(registry)
    set_global_manager(facade)  # type: ignore[arg-type]
    try:
        with lifecycle_module.state.acquire_plugins_write_lock():
            lifecycle_module.state.plugins.clear()
            lifecycle_module.state.plugins[plugin_id] = {
                "id": plugin_id,
                "config_path": str(market_dir / "plugin.toml"),
                "selected_candidate": lifecycle_module._candidate_key_payload(market),
                "available_candidate_count": 2,
            }
        with lifecycle_module.state.acquire_plugin_hosts_write_lock():
            lifecycle_module.state.plugin_hosts.clear()
            lifecycle_module.state.plugin_hosts[plugin_id] = _CandidateHost(market)

        result = await service.delete_plugin(plugin_id)

        entry = registry.load().entry(plugin_id)
        assert entry is not None
        assert entry.selected_candidate == builtin_ref
        assert entry.candidate_source == "builtin"
        assert entry.state_owner is not None
        assert entry.state_owner.candidate == builtin_ref
        assert entry.enabled is True
        assert entry.auto_start is True
        assert entry.candidate_for(market_ref).removed is True
        assert entry.candidate_for(builtin_ref).removed is False
        assert market_dir.exists() is False
        assert builtin_dir.is_dir()
        assert lock_path.read_bytes() == legacy_bytes
        assert result["fallback_candidate"] == lifecycle_module._candidate_key_payload(builtin)
        assert result["fallback_started"] is True
        assert events == [
            "plan",
            "validate",
            "stop_market",
            "apply_builtin",
            "start_builtin",
            "refresh",
        ]
    finally:
        clear_plugin_registry_authority(expected=registry)
        set_global_manager(previous_manager)
        with lifecycle_module.state.acquire_plugins_write_lock():
            lifecycle_module.state.plugins.clear()
            lifecycle_module.state.plugins.update(plugins_backup)
        with lifecycle_module.state.acquire_plugin_hosts_write_lock():
            lifecycle_module.state.plugin_hosts.clear()
            lifecycle_module.state.plugin_hosts.update(hosts_backup)


@pytest.mark.asyncio
async def test_failed_candidate_start_restores_runtime_without_mutating_registry_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_plugin_candidate_selections: dict[str, object],
) -> None:
    plugin_id = "demo"
    builtin = CandidateKey(root_id="builtin", directory_name=plugin_id)
    market = CandidateKey(root_id="user", directory_name="demo-market")
    builtin_ref = CandidateRef(
        root_id=builtin.root_id,
        directory_name=builtin.directory_name,
    )
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    builtin_dir = _write_plugin(builtin_root, builtin.directory_name, plugin_id)
    market_dir = _write_plugin(user_root, market.directory_name, plugin_id)
    registry = JsonPluginRegistry(
        tmp_path / "plugin_registry.json",
        clock=lambda: TS,
    )
    registry.initialize(
        PluginRegistrySnapshot.build(
            {
                plugin_id: PluginEntry(
                    plugin_id=plugin_id,
                    candidates=(
                        _record("builtin", builtin.directory_name, "builtin"),
                        _record("user", market.directory_name, "market"),
                    ),
                    selected_candidate=builtin_ref,
                    candidate_source="builtin",
                    enabled=True,
                    auto_start=True,
                    state_owner=StateOwnership(
                        candidate=builtin_ref,
                        state_scope="legacy_shared",
                        state_access_grant="builtin",
                    ),
                )
            },
            revision=1,
            updated_at=TS,
            created_at=TS,
        )
    )
    plugins_backup = copy.deepcopy(lifecycle_module.state.plugins)
    hosts_backup = dict(lifecycle_module.state.plugin_hosts)
    old_meta = {
        "id": plugin_id,
        "config_path": str(builtin_dir / "plugin.toml"),
        "selected_candidate": lifecycle_module._candidate_key_payload(builtin),
        "available_candidate_count": 2,
    }
    starts: list[CandidateKey] = []

    async def _list_candidates(_plugin_id: str) -> dict[str, object]:
        return {
            "plugin_id": plugin_id,
            "effective_candidate": lifecycle_module._candidate_key_payload(builtin),
            "registered_candidate": lifecycle_module._candidate_key_payload(builtin),
            "running_candidate": lifecycle_module._candidate_key_payload(builtin),
            "candidates": [
                {
                    "key": lifecycle_module._candidate_key_payload(builtin),
                    "source": "builtin",
                },
                {
                    "key": lifecycle_module._candidate_key_payload(market),
                    "source": "market",
                    "release_chain_id": "market.demo",
                },
            ],
        }

    async def _validate(_plugin_id: str, candidate: CandidateKey) -> dict[str, object]:
        assert candidate == market
        return {
            "valid": True,
            "source": "market",
            "release_chain_id": "market.demo",
            "runtime_enabled": True,
        }

    async def _refresh_plugin(
        _plugin_id: str,
        *,
        transient_candidate: CandidateKey | None = None,
    ) -> dict[str, object]:
        assert transient_candidate == market
        with lifecycle_module.state.acquire_plugins_write_lock():
            lifecycle_module.state.plugins[plugin_id] = {
                "id": plugin_id,
                "config_path": str(market_dir / "plugin.toml"),
                "selected_candidate": lifecycle_module._candidate_key_payload(market),
                "available_candidate_count": 2,
            }
        return {"success": True, "plugin_id": plugin_id}

    service = lifecycle_module.PluginLifecycleService()

    async def _stop(_plugin_id: str, **_kwargs: object) -> dict[str, object]:
        with lifecycle_module.state.acquire_plugin_hosts_write_lock():
            lifecycle_module.state.plugin_hosts.pop(plugin_id, None)
        return {"success": True, "plugin_id": plugin_id}

    async def _start(_plugin_id: str, **_kwargs: object) -> dict[str, object]:
        with lifecycle_module.state.acquire_plugins_read_lock():
            candidate = lifecycle_module._candidate_key_from_payload(
                lifecycle_module.state.plugins[plugin_id].get("selected_candidate")
            )
        assert candidate is not None
        starts.append(candidate)
        if candidate == market:
            raise ServerDomainError(
                code="PLUGIN_START_FAILED",
                message="target failed",
                status_code=500,
            )
        with lifecycle_module.state.acquire_plugin_hosts_write_lock():
            lifecycle_module.state.plugin_hosts[plugin_id] = _CandidateHost(builtin)
        return {"success": True, "plugin_id": plugin_id}

    monkeypatch.setattr(
        lifecycle_module.plugin_registry_service,
        "list_plugin_candidates",
        _list_candidates,
    )
    monkeypatch.setattr(
        lifecycle_module.plugin_registry_service,
        "validate_plugin_candidate",
        _validate,
    )
    monkeypatch.setattr(
        lifecycle_module.plugin_registry_service,
        "refresh_plugin",
        _refresh_plugin,
    )
    monkeypatch.setattr(service, "stop_plugin", _stop)
    monkeypatch.setattr(service, "start_plugin", _start)
    monkeypatch.setattr(lifecycle_module, "emit_lifecycle_event", lambda _event: None)

    clear_plugin_registry_authority()
    publish_plugin_registry_authority(registry)
    try:
        with lifecycle_module.state.acquire_plugins_write_lock():
            lifecycle_module.state.plugins.clear()
            lifecycle_module.state.plugins[plugin_id] = dict(old_meta)
        with lifecycle_module.state.acquire_plugin_hosts_write_lock():
            lifecycle_module.state.plugin_hosts.clear()
            lifecycle_module.state.plugin_hosts[plugin_id] = _CandidateHost(builtin)

        with pytest.raises(ServerDomainError) as exc_info:
            await service.switch_plugin_candidate(
                plugin_id,
                market,
                allow_legacy_shared_state=True,
            )

        assert exc_info.value.code == "PLUGIN_CANDIDATE_SWITCH_FAILED"
        assert exc_info.value.details["rollback_status"] == "completed"
        assert starts == [market, builtin]
        entry = registry.load().entry(plugin_id)
        assert entry is not None
        assert entry.selected_candidate == builtin_ref
        assert entry.candidate_source == "builtin"
        assert entry.state_owner is not None
        assert entry.state_owner.candidate == builtin_ref
        assert entry.enabled is True
        assert entry.auto_start is True
        assert registry.load().revision == 1
        assert _isolate_plugin_candidate_selections["selections"] == {}
        assert _isolate_plugin_candidate_selections["state_owners"] == {}
        with lifecycle_module.state.acquire_plugins_read_lock():
            assert lifecycle_module.state.plugins[plugin_id] == old_meta
        with lifecycle_module.state.acquire_plugin_hosts_read_lock():
            restored_host = lifecycle_module.state.plugin_hosts[plugin_id]
        assert restored_host.candidate == builtin
    finally:
        clear_plugin_registry_authority(expected=registry)
        with lifecycle_module.state.acquire_plugins_write_lock():
            lifecycle_module.state.plugins.clear()
            lifecycle_module.state.plugins.update(plugins_backup)
        with lifecycle_module.state.acquire_plugin_hosts_write_lock():
            lifecycle_module.state.plugin_hosts.clear()
            lifecycle_module.state.plugin_hosts.update(hosts_backup)
