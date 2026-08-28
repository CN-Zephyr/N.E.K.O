from __future__ import annotations

import pytest

from plugin.server.routes import plugins as module


pytestmark = pytest.mark.plugin_unit


@pytest.mark.asyncio
async def test_start_plugin_endpoint_ensures_messaging_before_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _ensure_messaging() -> None:
        calls.append("ensure")

    async def _start_plugin(plugin_id: str, *, persist_user_intent: bool = False) -> dict[str, object]:
        calls.append(f"start:{plugin_id}:{persist_user_intent}")
        return {"success": True, "plugin_id": plugin_id}

    monkeypatch.setattr(module, "ensure_plugin_messaging_started", _ensure_messaging, raising=False)
    monkeypatch.setattr(module.lifecycle_service, "start_plugin", _start_plugin)

    result = await module.start_plugin_endpoint("sample_plugin", _="test")

    assert result == {"success": True, "plugin_id": "sample_plugin"}
    assert calls == ["ensure", "start:sample_plugin:True"]


@pytest.mark.asyncio
async def test_candidate_routes_expose_inventory_and_controlled_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listed = {
        "plugin_id": "sample_plugin",
        "candidates": [
            {
                "key": {"root_id": "builtin", "directory_name": "sample_plugin"},
                "source": "builtin",
                "version": "1.0.0",
                "valid": True,
            }
        ],
    }
    switched: list[tuple[str, object, bool]] = []

    async def _list(plugin_id: str) -> dict[str, object]:
        assert plugin_id == "sample_plugin"
        return listed

    async def _switch(
        plugin_id: str,
        candidate: object,
        *,
        allow_legacy_shared_state: bool,
    ) -> dict[str, object]:
        switched.append((plugin_id, candidate, allow_legacy_shared_state))
        return {"success": True, "plugin_id": plugin_id}

    monkeypatch.setattr(module.registry_service, "list_plugin_candidates", _list)
    monkeypatch.setattr(module.lifecycle_service, "switch_plugin_candidate", _switch)

    assert await module.list_plugin_candidates_endpoint("sample_plugin") == listed
    payload = module.PluginCandidateSelectionRequest(
        root_id="user",
        directory_name="sample-market",
        allow_legacy_shared_state=True,
    )
    result = await module.select_plugin_candidate_endpoint(
        "sample_plugin",
        payload,
        _="test",
    )

    assert result == {"success": True, "plugin_id": "sample_plugin"}
    assert switched == [
        (
            "sample_plugin",
            module.CandidateKey(root_id="user", directory_name="sample-market"),
            True,
        )
    ]


@pytest.mark.asyncio
async def test_package_profile_delete_route_requires_exact_confirmed_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str]] = []

    async def remove_profile(**kwargs: str) -> dict[str, object]:
        calls.append(kwargs)
        return {"success": True, "package_profile_deleted": True}

    monkeypatch.setattr(
        module,
        "remove_retired_candidate_package_profile",
        remove_profile,
    )
    payload = module.PluginPackageProfileRemovalRequest(
        root_id="user",
        directory_name="sample-market",
        confirm_delete=True,
    )

    result = await module.delete_plugin_package_profile_endpoint(
        "sample_plugin",
        payload,
        _="test",
    )

    assert result == {"success": True, "package_profile_deleted": True}
    assert calls == [
        {
            "plugin_id": "sample_plugin",
            "root_id": "user",
            "directory_name": "sample-market",
        }
    ]

    with pytest.raises(ValueError):
        module.PluginPackageProfileRemovalRequest(
            root_id="user",
            directory_name="sample-market",
            confirm_delete=False,
        )


@pytest.mark.asyncio
async def test_retained_package_profile_list_route_uses_safe_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"profiles": [], "count": 0}
    monkeypatch.setattr(
        module,
        "list_retained_candidate_package_profiles",
        lambda: expected,
    )

    result = await module.list_retained_package_profiles_endpoint(_="test")

    assert result is expected
