from __future__ import annotations

import pytest

from plugin.server.domain.plugin_candidates import CandidateKey
from plugin.server.infrastructure import plugin_selections as selections


pytestmark = pytest.mark.plugin_unit


def test_selection_round_trip_is_sparse_and_stable(
    _isolate_plugin_candidate_selections,
) -> None:
    candidate = CandidateKey(root_id="user", directory_name="demo-market")

    selections.set_plugin_selection(
        "demo",
        candidate,
        candidate_source="imported",
        state_access_grant="user_authorized",
        authorized_at="2026-08-26T07:00:00Z",
    )
    selections.reset_cache_for_testing()

    assert selections.get_plugin_selection("demo") == candidate
    receipt = {
        "root_id": "user",
        "directory_name": "demo-market",
        "candidate_source": "imported",
        "state_scope": "legacy_shared",
        "state_access_grant": "user_authorized",
        "release_chain_id": None,
        "authorized_at": "2026-08-26T07:00:00Z",
    }
    assert _isolate_plugin_candidate_selections == {
        "schema_version": 3,
        "selections": {
            "demo": receipt,
        },
        "state_owners": {"demo": receipt},
    }


def test_invalid_entry_is_ignored_but_blocks_destructive_rewrite(
    _isolate_plugin_candidate_selections,
) -> None:
    _isolate_plugin_candidate_selections["selections"] = {
        "broken": {
            "root_id": "user",
            "directory_name": "../outside",
        }
    }
    selections.reset_cache_for_testing()

    assert selections.get_plugin_selection("broken") is None
    with pytest.raises(selections.PluginSelectionWriteError):
        selections.set_plugin_selection(
            "demo",
            CandidateKey(root_id="builtin", directory_name="demo"),
        )


def test_unsupported_schema_degrades_reads_without_overwriting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        selections,
        "_load_from_disk",
        lambda: (_ for _ in ()).throw(
            selections.PluginSelectionReadError("unsupported schema")
        ),
    )
    selections.reset_cache_for_testing()

    assert selections.get_plugin_selection("demo") is None


def test_conditional_clear_does_not_erase_a_newer_selection() -> None:
    old_candidate = CandidateKey(root_id="user", directory_name="demo-old")
    new_candidate = CandidateKey(root_id="user", directory_name="demo-new")
    selections.set_plugin_selection(
        "demo",
        new_candidate,
        candidate_source="manual",
        state_access_grant="user_authorized",
        authorized_at="2026-08-26T07:00:00Z",
    )

    assert selections.clear_plugin_selection_if_matches("demo", old_candidate) is False
    assert selections.get_plugin_selection("demo") == new_candidate

    assert selections.clear_plugin_selection_if_matches("demo", new_candidate) is True
    assert selections.get_plugin_selection("demo") is None
    owner = selections.get_plugin_state_owner("demo")
    assert owner is not None
    assert owner.candidate == new_candidate


def test_external_selection_without_state_grant_is_rejected() -> None:
    with pytest.raises(selections.PluginSelectionWriteError):
        selections.set_plugin_selection(
            "demo",
            CandidateKey(root_id="user", directory_name="demo-local"),
        )


def test_legacy_external_selection_loads_without_state_grant(
    _isolate_plugin_candidate_selections,
) -> None:
    _isolate_plugin_candidate_selections.clear()
    _isolate_plugin_candidate_selections.update(
        {
            "schema_version": 1,
            "selections": {
                "demo": {
                    "root_id": "user",
                    "directory_name": "demo-local",
                }
            },
        }
    )
    selections.reset_cache_for_testing()

    record = selections.get_plugin_selection_record("demo")

    assert record is not None
    assert record.candidate == CandidateKey(
        root_id="user",
        directory_name="demo-local",
    )
    assert record.has_state_access_grant is False


def test_v2_grant_is_promoted_to_owner_before_selection_clear(
    _isolate_plugin_candidate_selections,
) -> None:
    receipt = {
        "root_id": "user",
        "directory_name": "demo-imported",
        "candidate_source": "imported",
        "state_scope": "legacy_shared",
        "state_access_grant": "user_authorized",
        "release_chain_id": None,
        "authorized_at": "2026-08-26T07:00:00Z",
    }
    _isolate_plugin_candidate_selections.clear()
    _isolate_plugin_candidate_selections.update(
        {
            "schema_version": 2,
            "selections": {"demo": receipt},
        }
    )
    selections.reset_cache_for_testing()

    owner = selections.get_plugin_state_owner("demo")
    assert owner is not None
    assert owner.candidate.directory_name == "demo-imported"
    assert selections.clear_plugin_selection("demo") is None
    selections.reset_cache_for_testing()

    assert selections.get_plugin_selection("demo") is None
    retained_owner = selections.get_plugin_state_owner("demo")
    assert retained_owner is not None
    assert retained_owner.candidate.directory_name == "demo-imported"
    assert _isolate_plugin_candidate_selections["schema_version"] == 3
