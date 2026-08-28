from __future__ import annotations

from pathlib import Path

import pytest

from plugin.server.application.install_source.models import (
    CandidateRecord,
    CandidateRef,
    PluginEntry,
    PluginRegistrySnapshot,
    StateOwnership,
)
from plugin.server.domain.plugin_candidates import CandidateKey
from plugin.server.infrastructure import plugin_selections, runtime_overrides
from plugin.server.infrastructure.package_management.json_registry import (
    JsonPluginRegistry,
)
from plugin.server.infrastructure.plugin_registry_authority import (
    block_plugin_registry_authority,
    clear_plugin_registry_authority,
    publish_plugin_registry_authority,
)


pytestmark = pytest.mark.plugin_unit

TS = "2026-08-27T00:00:00.000000Z"


def _candidate(directory_name: str) -> CandidateRecord:
    return CandidateRecord(
        root_id="user",
        directory_name=directory_name,
        channel="manual",
        reason="user_requested",
        installed_at=TS,
        updated_at=TS,
        last_seen_at=TS,
    )


def _registry(tmp_path: Path) -> JsonPluginRegistry:
    old_ref = CandidateRef(root_id="user", directory_name="demo-old")
    registry = JsonPluginRegistry(
        tmp_path / "plugin_registry.json",
        clock=lambda: TS,
    )
    registry.initialize(
        PluginRegistrySnapshot.build(
            {
                "demo": PluginEntry(
                    plugin_id="demo",
                    candidates=(_candidate("demo-old"), _candidate("demo-new")),
                    selected_candidate=old_ref,
                    candidate_source="manual",
                    enabled=True,
                    state_owner=StateOwnership(
                        candidate=old_ref,
                        state_scope="legacy_shared",
                        state_access_grant="user_authorized",
                        authorized_at=TS,
                    ),
                ),
                "old-id": PluginEntry(
                    plugin_id="old-id",
                    enabled=False,
                    auto_start=True,
                ),
            },
            revision=1,
            updated_at=TS,
        )
    )
    return registry


def test_selection_apis_forward_to_registry_without_legacy_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    monkeypatch.setattr(
        plugin_selections,
        "_save_to_disk",
        lambda _store: pytest.fail("legacy selection file must be read-only"),
    )
    publish_plugin_registry_authority(registry)

    try:
        selected = CandidateKey(root_id="user", directory_name="demo-new")
        plugin_selections.set_plugin_selection(
            "demo",
            selected,
            candidate_source="manual",
            state_access_grant="user_authorized",
            authorized_at=TS,
        )

        assert plugin_selections.get_plugin_selection("demo") == selected
        owner = plugin_selections.get_plugin_state_owner("demo")
        assert owner is not None
        assert owner.candidate == selected
        revision = registry.load().revision

        assert not plugin_selections.clear_plugin_selection_if_matches(
            "demo",
            CandidateKey(root_id="user", directory_name="demo-old"),
        )
        assert registry.load().revision == revision
        assert plugin_selections.clear_plugin_selection_if_matches("demo", selected)
        assert plugin_selections.get_plugin_selection("demo") is None
        retained_owner = plugin_selections.get_plugin_state_owner("demo")
        assert retained_owner is not None
        assert retained_owner.candidate == selected
    finally:
        clear_plugin_registry_authority(expected=registry)


def test_runtime_intent_apis_forward_to_registry_without_legacy_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    monkeypatch.setattr(
        runtime_overrides,
        "_save_to_disk",
        lambda _overrides: pytest.fail("legacy override file must be read-only"),
    )
    publish_plugin_registry_authority(registry)

    try:
        runtime_overrides.set_runtime_override("demo", False, auto_start=False)
        assert runtime_overrides.get_runtime_override("demo") is False
        assert runtime_overrides.get_runtime_auto_start_override("demo") is False

        runtime_overrides.migrate_runtime_override(
            ("old-id",),
            "demo",
            True,
        )
        snapshot = registry.load()
        assert snapshot.entry("demo").enabled is True
        assert snapshot.entry("demo").auto_start is True
        assert snapshot.entry("old-id").enabled is None
        assert snapshot.entry("old-id").auto_start is None

        runtime_overrides.clear_runtime_override("demo")
        assert runtime_overrides.get_runtime_override("demo") is None
        assert runtime_overrides.get_runtime_auto_start_override("demo") is None
        assert registry.load().entry("demo").candidates
    finally:
        clear_plugin_registry_authority(expected=registry)


def test_blocked_authority_never_falls_back_to_legacy_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        plugin_selections,
        "_save_to_disk",
        lambda _store: pytest.fail("blocked authority must not write legacy selection"),
    )
    monkeypatch.setattr(
        runtime_overrides,
        "_save_to_disk",
        lambda _overrides: pytest.fail("blocked authority must not write legacy intent"),
    )
    clear_plugin_registry_authority()
    block_plugin_registry_authority()

    try:
        with pytest.raises(plugin_selections.PluginSelectionWriteError):
            plugin_selections.clear_plugin_selection("demo")
        with pytest.raises(runtime_overrides.RuntimeOverrideWriteError):
            runtime_overrides.set_runtime_override("demo", False)
        with pytest.raises(plugin_selections.PluginSelectionReadError):
            plugin_selections.load_plugin_selection_records()
        with pytest.raises(runtime_overrides.RuntimeOverrideReadError):
            runtime_overrides.load_runtime_overrides()
    finally:
        clear_plugin_registry_authority()
