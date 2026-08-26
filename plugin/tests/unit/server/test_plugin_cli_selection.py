from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plugin.server.application.plugin_cli import service as module
from plugin.server.application.plugins import lifecycle_service as lifecycle_module
from plugin.server.application.plugins import registry_service as registry_module
from plugin.server.domain.plugin_candidates import CandidateKey
from plugin.server.domain.errors import ServerDomainError
from plugin.server.infrastructure import plugin_selections


pytestmark = pytest.mark.plugin_unit


@pytest.mark.asyncio
async def test_fresh_market_install_activates_through_lifecycle_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    target_dir = user_root / "demo"
    target_dir.mkdir(parents=True)
    manager = SimpleNamespace(
        builtin_root=builtin_root,
        user_root=user_root,
    )
    switched: list[tuple[str, CandidateKey]] = []
    service = module.PluginCliService()
    monkeypatch.setattr(service, "_require_install_source_manager", lambda: manager)
    async def _switch(_self, plugin_id: str, candidate: CandidateKey) -> None:
        switched.append((plugin_id, candidate))

    monkeypatch.setattr(
        lifecycle_module.PluginLifecycleService,
        "switch_plugin_candidate",
        _switch,
    )
    monkeypatch.setattr(
        registry_module.PluginRegistryService,
        "list_plugin_candidates",
        lambda _self, _plugin_id: _async_value(
            {
                "registered_candidate": {
                    "root_id": "builtin",
                    "directory_name": "demo",
                },
                "candidates": [{}, {}],
            }
        ),
    )

    await service._activate_fresh_install_candidate(
        plugin_id="demo",
        target_dir=target_dir,
    )

    assert switched == [
        (
            "demo",
            CandidateKey(root_id="user", directory_name="demo"),
        )
    ]


@pytest.mark.asyncio
async def test_fresh_market_activation_failure_propagates_for_install_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    target_dir = user_root / "demo"
    target_dir.mkdir(parents=True)
    manager = SimpleNamespace(
        builtin_root=builtin_root,
        user_root=user_root,
    )
    service = module.PluginCliService()
    monkeypatch.setattr(service, "_require_install_source_manager", lambda: manager)
    async def _fail_switch(
        _self,
        _plugin_id: str,
        _candidate: CandidateKey,
    ) -> None:
        raise ServerDomainError(
            code="PLUGIN_CANDIDATE_SWITCH_FAILED",
            message="switch failed",
            status_code=500,
        )

    monkeypatch.setattr(
        lifecycle_module.PluginLifecycleService,
        "switch_plugin_candidate",
        _fail_switch,
    )
    monkeypatch.setattr(
        registry_module.PluginRegistryService,
        "list_plugin_candidates",
        lambda _self, _plugin_id: _async_value(
            {
                "registered_candidate": {
                    "root_id": "builtin",
                    "directory_name": "demo",
                },
                "candidates": [{}, {}],
            }
        ),
    )

    with pytest.raises(ServerDomainError) as exc_info:
        await service._activate_fresh_install_candidate(
            plugin_id="demo",
            target_dir=target_dir,
        )

    assert exc_info.value.code == "PLUGIN_CANDIDATE_SWITCH_FAILED"


@pytest.mark.asyncio
async def test_fresh_install_keeps_candidate_pending_when_state_consent_is_required(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    target_dir = user_root / "demo-market"
    target_dir.mkdir(parents=True)
    manager = SimpleNamespace(builtin_root=builtin_root, user_root=user_root)
    service = module.PluginCliService()
    monkeypatch.setattr(service, "_require_install_source_manager", lambda: manager)

    async def _unexpected_switch(*_args, **_kwargs) -> None:
        raise AssertionError("installation must not silently grant shared-state access")

    monkeypatch.setattr(
        lifecycle_module.PluginLifecycleService,
        "switch_plugin_candidate",
        _unexpected_switch,
    )
    monkeypatch.setattr(
        registry_module.PluginRegistryService,
        "list_plugin_candidates",
        lambda _self, _plugin_id: _async_value(
            {
                "registered_candidate": {
                    "root_id": "builtin",
                    "directory_name": "demo",
                },
                "candidates": [
                    {
                        "key": {
                            "root_id": "builtin",
                            "directory_name": "demo",
                        },
                        "requires_shared_state_authorization": False,
                    },
                    {
                        "key": {
                            "root_id": "user",
                            "directory_name": "demo-market",
                        },
                        "requires_shared_state_authorization": True,
                    },
                ],
            }
        ),
    )

    activated = await service._activate_fresh_install_candidate(
        plugin_id="demo",
        target_dir=target_dir,
    )

    assert activated is False


def _write_candidate(root: Path, directory_name: str, plugin_id: str) -> Path:
    target_dir = root / directory_name
    target_dir.mkdir(parents=True)
    (target_dir / "plugin.toml").write_text(
        "\n".join(
            [
                "[plugin]",
                f"id = '{plugin_id}'",
                f"name = '{plugin_id}'",
                "type = 'plugin'",
                "entry = 'plugins.demo:Plugin'",
                "version = '1.0.0'",
            ]
        ),
        encoding="utf-8",
    )
    return target_dir


def _configure_real_candidate_inventory(
    monkeypatch: pytest.MonkeyPatch,
    *,
    builtin_root: Path,
    user_root: Path,
) -> None:
    monkeypatch.setattr(registry_module, "get_install_source_manager", lambda: None)
    monkeypatch.setattr(registry_module, "BUILTIN_PLUGIN_CONFIG_ROOT", builtin_root)
    monkeypatch.setattr(registry_module, "PLUGIN_CONFIG_ROOTS", (builtin_root, user_root))
    monkeypatch.setattr(registry_module, "_get_registered_plugin_snapshot_sync", lambda: {})
    monkeypatch.setattr(registry_module, "_list_running_plugin_ids_sync", lambda: set())


@pytest.mark.asyncio
async def test_deleted_candidate_owner_blocks_unrelated_same_id_fresh_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_id = "demo"
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    old = CandidateKey(root_id="user", directory_name="demo-a")
    # Reusing the deleted slot name must not make unrelated bytes inherit A's grant.
    target_dir = _write_candidate(user_root, "demo-a", plugin_id)
    manager = SimpleNamespace(builtin_root=builtin_root, user_root=user_root)
    _configure_real_candidate_inventory(
        monkeypatch,
        builtin_root=builtin_root,
        user_root=user_root,
    )
    monkeypatch.setattr(
        registry_module,
        "_get_registered_plugin_snapshot_sync",
        lambda: {
            plugin_id: {
                "id": plugin_id,
                "config_path": str(target_dir / "plugin.toml"),
            }
        },
    )
    plugin_selections.set_plugin_selection(
        plugin_id,
        old,
        candidate_source="manual",
        state_access_grant="user_authorized",
        authorized_at="2026-08-26T07:00:00Z",
    )
    assert plugin_selections.clear_plugin_selection_if_matches(plugin_id, old) is True

    async def _unexpected_switch(*_args, **_kwargs) -> None:
        raise AssertionError("unrelated code must not inherit deleted candidate state")

    monkeypatch.setattr(
        lifecycle_module.PluginLifecycleService,
        "switch_plugin_candidate",
        _unexpected_switch,
    )
    service = module.PluginCliService()
    monkeypatch.setattr(service, "_require_install_source_manager", lambda: manager)

    activated = await service._activate_fresh_install_candidate(
        plugin_id=plugin_id,
        target_dir=target_dir,
    )

    assert activated is False
    assert plugin_selections.get_plugin_selection(plugin_id) is None
    owner = plugin_selections.get_plugin_state_owner(plugin_id)
    assert owner is not None
    assert owner.candidate == old


@pytest.mark.asyncio
async def test_legacy_state_without_owner_blocks_same_id_fresh_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_id = "legacy_demo"
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    target_dir = _write_candidate(user_root, "legacy-new", plugin_id)
    manager = SimpleNamespace(builtin_root=builtin_root, user_root=user_root)
    _configure_real_candidate_inventory(
        monkeypatch,
        builtin_root=builtin_root,
        user_root=user_root,
    )
    monkeypatch.setattr(
        registry_module,
        "_get_registered_plugin_snapshot_sync",
        lambda: {
            plugin_id: {
                "id": plugin_id,
                "config_path": str(target_dir / "plugin.toml"),
            }
        },
    )
    monkeypatch.setattr(
        plugin_selections,
        "legacy_shared_state_exists",
        lambda candidate_plugin_id: candidate_plugin_id == plugin_id,
    )

    async def _unexpected_switch(*_args, **_kwargs) -> None:
        raise AssertionError("legacy state without an owner must fail closed")

    monkeypatch.setattr(
        lifecycle_module.PluginLifecycleService,
        "switch_plugin_candidate",
        _unexpected_switch,
    )
    service = module.PluginCliService()
    monkeypatch.setattr(service, "_require_install_source_manager", lambda: manager)

    activated = await service._activate_fresh_install_candidate(
        plugin_id=plugin_id,
        target_dir=target_dir,
    )

    assert activated is False
    assert plugin_selections.get_plugin_selection(plugin_id) is None
    assert plugin_selections.get_plugin_state_owner(plugin_id) is None


async def _async_value(value):
    return value
