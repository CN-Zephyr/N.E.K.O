from __future__ import annotations

from plugin.server.application.install_source.models import (
    CandidateRecord,
    CandidateRef,
    LockEntry,
    LockFile,
    PluginEntry,
    PluginRegistrySnapshot,
    SourceDetailMarket,
    StateOwnership,
)
from plugin.server.application.install_source.registry_migration import (
    migrate_legacy_state_to_registry,
)
from plugin.server.application.install_source.registry_shadow import (
    compare_legacy_state_to_registry,
    compare_registry_snapshots,
)

TS = "2026-08-27T00:00:00.000000Z"


def _candidate(
    directory_name: str,
    *,
    package_url: str = "https://packages.invalid/private-token",
) -> CandidateRecord:
    return CandidateRecord(
        root_id="user",
        directory_name=directory_name,
        channel="market",
        reason="user_requested",
        installed_at=TS,
        updated_at=TS,
        last_seen_at=TS,
        source_detail=SourceDetailMarket(
            plugin_market_id="private-market-id",
            version="1.0.0",
            package_url=package_url,
            package_sha256="a" * 64,
            payload_hash=None,
            channel="stable",
            published_at=TS,
        ),
    )


def _snapshot(
    plugins: dict[str, PluginEntry],
    *,
    revision: int,
) -> PluginRegistrySnapshot:
    return PluginRegistrySnapshot.build(
        plugins,
        revision=revision,
        updated_at=TS,
    )


def test_shadow_comparison_ignores_snapshot_metadata_when_authority_matches() -> None:
    entry = PluginEntry(
        plugin_id="demo",
        candidates=(_candidate("demo"),),
    )

    result = compare_registry_snapshots(
        _snapshot({"demo": entry}, revision=1),
        PluginRegistrySnapshot.build(
            {"demo": entry},
            revision=99,
            updated_at="2026-08-28T00:00:00.000000Z",
            created_at="2026-08-20T00:00:00.000000Z",
        ),
    )

    assert result.matches is True
    assert dict(result.mismatch_counts) == {}


def test_shadow_comparison_reports_only_aggregate_mismatch_categories() -> None:
    expected_ref = CandidateRef(root_id="user", directory_name="private-dir")
    actual_ref = CandidateRef(root_id="user", directory_name="different-private-dir")
    expected = _snapshot(
        {
            "private-plugin-id": PluginEntry(
                plugin_id="private-plugin-id",
                candidates=(_candidate("private-dir"),),
                selected_candidate=expected_ref,
                candidate_source="market",
                enabled=True,
                state_owner=StateOwnership(
                    candidate=expected_ref,
                    state_scope="legacy_shared",
                    state_access_grant="user_authorized",
                    authorized_at=TS,
                ),
            ),
            "missing-private-plugin": PluginEntry(
                plugin_id="missing-private-plugin"
            ),
        },
        revision=4,
    )
    actual = _snapshot(
        {
            "private-plugin-id": PluginEntry(
                plugin_id="private-plugin-id",
                candidates=(
                    _candidate(
                        "different-private-dir",
                        package_url="https://secret.invalid/bearer-value",
                    ),
                ),
                selected_candidate=actual_ref,
                candidate_source="manual",
                enabled=False,
            ),
            "unexpected-private-plugin": PluginEntry(
                plugin_id="unexpected-private-plugin"
            ),
        },
        revision=5,
    )

    result = compare_registry_snapshots(expected, actual)

    assert result.matches is False
    assert result.expected_plugin_count == 2
    assert result.actual_plugin_count == 2
    assert dict(result.mismatch_counts) == {
        "candidates": 1,
        "missing_plugins": 1,
        "runtime_intent": 1,
        "selection": 1,
        "state_owner": 1,
        "unexpected_plugins": 1,
    }
    diagnostic = repr(result)
    for private_value in (
        "private-plugin-id",
        "missing-private-plugin",
        "unexpected-private-plugin",
        "private-dir",
        "bearer-value",
        "private-market-id",
    ):
        assert private_value not in diagnostic


def test_legacy_shadow_projection_matches_migrated_registry() -> None:
    lock = LockFile(
        schema_version=2,
        entries=(
            LockEntry(
                root_id="user",
                directory_name="demo",
                plugin_id="demo",
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
    actual = migrate_legacy_state_to_registry(lock, now=TS)

    result = compare_legacy_state_to_registry(lock, actual, now=TS)

    assert result.matches is True
    assert result.expected_plugin_count == 1
    assert result.actual_plugin_count == 1
    assert dict(result.mismatch_counts) == {}
