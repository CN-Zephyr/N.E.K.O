from __future__ import annotations

import json

import pytest

from plugin.server.application.install_source.models import (
    LockEntry,
    LockFile,
    PluginEntry,
    PluginRegistrySnapshot,
)
from plugin.server.application.install_source.registry_preflight import (
    RegistryCutoverPreflightError,
    prepare_registry_cutover_preflight,
)
from plugin.server.domain.plugin_candidates import CandidateKey
from plugin.server.infrastructure.package_management.legacy_registry_preflight import (
    load_registry_cutover_preflight,
)
from plugin.server.infrastructure.plugin_selections import PluginSelection

TS = "2026-08-27T00:00:00.000000Z"


def _lock(*, schema_version: int = 2) -> LockFile:
    return LockFile(
        schema_version=schema_version,
        entries=(
            LockEntry(
                root_id="user",
                directory_name="private-directory",
                plugin_id="private-plugin-id",
                channel="manual",
                reason="user_requested",
                installed_at=TS,
                updated_at=TS,
                last_seen_at=TS,
            ),
        ),
        updated_at=TS,
        created_at=TS,
    )


def _selection(directory_name: str = "private-directory") -> PluginSelection:
    return PluginSelection(
        candidate=CandidateKey(root_id="user", directory_name=directory_name),
        candidate_source="manual",
        state_scope="legacy_shared",
        state_access_grant="user_authorized",
        release_chain_id=None,
        authorized_at=TS,
    )


def _selection_document(directory_name: str = "private-directory") -> dict:
    record = {
        "root_id": "user",
        "directory_name": directory_name,
        "candidate_source": "manual",
        "state_scope": "legacy_shared",
        "state_access_grant": "user_authorized",
        "release_chain_id": None,
        "authorized_at": TS,
    }
    return {
        "schema_version": 3,
        "selections": {"private-plugin-id": record},
        "state_owners": {"private-plugin-id": record},
    }


def test_preflight_builds_a_strict_in_memory_snapshot() -> None:
    selection = _selection()

    result = prepare_registry_cutover_preflight(
        _lock(),
        selections={"private-plugin-id": selection},
        state_owners={"private-plugin-id": selection},
        runtime_overrides={
            "private-plugin-id": {"enabled": False, "auto_start": True}
        },
        now=TS,
    )

    entry = result.snapshot.entry("private-plugin-id")
    assert result.snapshot.revision == 1
    assert entry is not None
    assert entry.selected_candidate is not None
    assert entry.enabled is False
    assert entry.auto_start is True
    assert entry.state_owner is not None
    assert result.shadow_comparison is None


@pytest.mark.parametrize("schema_version", [1, 3, True])
def test_preflight_requires_a_reconciled_v2_lock(schema_version: int) -> None:
    with pytest.raises(RegistryCutoverPreflightError) as exc_info:
        prepare_registry_cutover_preflight(
            _lock(schema_version=schema_version),
            now=TS,
        )

    assert exc_info.value.reason == "unsupported_lock_schema"


def test_preflight_fails_closed_on_migration_loss_without_private_details() -> None:
    private_value = "missing-secret-candidate"

    with pytest.raises(RegistryCutoverPreflightError) as exc_info:
        prepare_registry_cutover_preflight(
            _lock(),
            selections={"private-plugin-id": _selection(private_value)},
            now=TS,
        )

    error = exc_info.value
    assert error.reason == "migration_loss"
    assert error.details == {"loss_count": 1}
    assert private_value not in str(error)
    assert private_value not in repr(error.details)


def test_preflight_enforces_the_shadow_gate_with_aggregate_details() -> None:
    actual = PluginRegistrySnapshot.build(
        {"different-private-id": PluginEntry(plugin_id="different-private-id")},
        revision=8,
        updated_at=TS,
    )

    with pytest.raises(RegistryCutoverPreflightError) as exc_info:
        prepare_registry_cutover_preflight(
            _lock(),
            actual_registry=actual,
            now=TS,
        )

    error = exc_info.value
    assert error.reason == "shadow_mismatch"
    assert error.details == {
        "expected_plugin_count": 1,
        "actual_plugin_count": 1,
        "mismatch_counts": {
            "missing_plugins": 1,
            "unexpected_plugins": 1,
        },
    }
    assert "private-plugin-id" not in repr(error.details)
    assert "different-private-id" not in repr(error.details)


def test_preflight_accepts_an_authority_equivalent_registry() -> None:
    first = prepare_registry_cutover_preflight(_lock(), now=TS).snapshot
    equivalent = PluginRegistrySnapshot.build(
        first.plugins,
        revision=99,
        updated_at="2026-08-28T00:00:00.000000Z",
    )

    result = prepare_registry_cutover_preflight(
        _lock(),
        actual_registry=equivalent,
        now=TS,
    )

    assert result.shadow_comparison is not None
    assert result.shadow_comparison.matches is True


def test_explicit_path_loader_reads_valid_sidecars_without_writing(tmp_path) -> None:
    selections_path = tmp_path / "plugin_candidate_selections.json"
    runtime_path = tmp_path / "plugin_runtime_overrides.json"
    selection_bytes = json.dumps(_selection_document()).encode()
    runtime_bytes = json.dumps(
        {"private-plugin-id": {"enabled": True, "auto_start": False}}
    ).encode()
    selections_path.write_bytes(selection_bytes)
    runtime_path.write_bytes(runtime_bytes)

    result = load_registry_cutover_preflight(
        _lock(),
        selections_path=selections_path,
        runtime_overrides_path=runtime_path,
        now=TS,
    )

    entry = result.snapshot.entry("private-plugin-id")
    assert entry is not None and entry.enabled is True
    assert entry.auto_start is False
    assert selections_path.read_bytes() == selection_bytes
    assert runtime_path.read_bytes() == runtime_bytes
    assert not (tmp_path / "plugin_registry.json").exists()


def test_explicit_path_loader_treats_missing_sidecars_as_empty(tmp_path) -> None:
    result = load_registry_cutover_preflight(
        _lock(),
        selections_path=tmp_path / "missing-selections.json",
        runtime_overrides_path=tmp_path / "missing-overrides.json",
        now=TS,
    )

    entry = result.snapshot.entry("private-plugin-id")
    assert entry is not None
    assert entry.selected_candidate is None
    assert entry.enabled is None


@pytest.mark.parametrize(
    ("selection_document", "runtime_document", "authority"),
    [
        (
            {
                **_selection_document(),
                "unexpected": "must-block-cutover",
            },
            {},
            "candidate_selections",
        ),
        (
            None,
            {"private-plugin-id": {"enabled": True, "unknown": False}},
            "runtime_overrides",
        ),
    ],
)
def test_explicit_path_loader_rejects_partially_invalid_legacy_content(
    tmp_path,
    selection_document,
    runtime_document,
    authority: str,
) -> None:
    selections_path = tmp_path / "plugin_candidate_selections.json"
    runtime_path = tmp_path / "plugin_runtime_overrides.json"
    if selection_document is not None:
        selections_path.write_text(json.dumps(selection_document), encoding="utf-8")
    runtime_path.write_text(json.dumps(runtime_document), encoding="utf-8")

    with pytest.raises(RegistryCutoverPreflightError) as exc_info:
        load_registry_cutover_preflight(
            _lock(),
            selections_path=selections_path,
            runtime_overrides_path=runtime_path,
            now=TS,
        )

    assert exc_info.value.reason == "legacy_invalid_content"
    assert exc_info.value.details == {"authority": authority}


def test_explicit_path_loader_rejects_invalid_json(tmp_path) -> None:
    selections_path = tmp_path / "plugin_candidate_selections.json"
    selections_path.write_bytes(b"{not-json")

    with pytest.raises(RegistryCutoverPreflightError) as exc_info:
        load_registry_cutover_preflight(
            _lock(),
            selections_path=selections_path,
            runtime_overrides_path=tmp_path / "missing-overrides.json",
            now=TS,
        )

    assert exc_info.value.reason == "legacy_invalid_json"
    assert exc_info.value.details == {"authority": "candidate_selections"}


def test_explicit_path_loader_rejects_non_utf8_json(tmp_path) -> None:
    runtime_path = tmp_path / "plugin_runtime_overrides.json"
    runtime_path.write_bytes('{"private-plugin-id": true}'.encode("utf-16"))

    with pytest.raises(RegistryCutoverPreflightError) as exc_info:
        load_registry_cutover_preflight(
            _lock(),
            selections_path=tmp_path / "missing-selections.json",
            runtime_overrides_path=runtime_path,
            now=TS,
        )

    assert exc_info.value.reason == "legacy_invalid_json"
    assert exc_info.value.details == {"authority": "runtime_overrides"}
