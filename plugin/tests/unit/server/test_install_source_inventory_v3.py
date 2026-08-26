"""Schema v3 inventory: codec round-trip, determinism, and v2 → v3 migration.

The v3 inventory is the single durable authority that replaces the
``plugins.lock.json`` + ``plugin_candidate_selections.json`` pair. These tests
pin the contracts the later cutover depends on:

* parse ∘ serialize is the identity on a v3 snapshot, and serialization is
  byte-deterministic for an unchanged snapshot;
* every representable v2 field survives migration (checked both by explicit
  assertions and by ``describe_migration_losses``);
* the two entry invariants hold on both the read and the migration path — a
  selection must name a live candidate, and a state-ownership receipt is kept
  for a soft-removed candidate but dropped when no row exists at all.

Pure data tests: no filesystem, no clock, no scanner, no network.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings, strategies as st

from plugin.server.application.install_source.inventory_codec import (
    parse_inventory,
    serialize_inventory,
)
from plugin.server.application.install_source.migration import (
    describe_migration_losses,
    migrate_lock_to_inventory,
)
from plugin.server.application.install_source.models import (
    CandidateRecord,
    CandidateRef,
    LockEntry,
    LockFile,
    PluginEntry,
    PluginInventory,
    SourceDetailImported,
    SourceDetailMarket,
    StateOwnership,
)
from plugin.server.domain.plugin_candidates import CandidateKey
from plugin.server.infrastructure.plugin_selections import PluginSelection

TS = "2026-08-27T00:00:00.000000Z"
TS_LATER = "2026-08-27T12:00:00.000000Z"

def _market_detail(version: str = "2.0.0") -> SourceDetailMarket:
    return SourceDetailMarket(
        plugin_market_id="market-abc",
        version=version,
        package_url="https://market.example/pkg.neko-plugin",
        package_sha256="a" * 64,
        payload_hash="b" * 64,
        channel="stable",
        published_at=TS,
    )


def _lock_entry(
    *,
    plugin_id: str,
    root_id: str = "user",
    directory_name: str = "demo",
    channel: str = "market",
    removed: bool = False,
    source_detail=None,
) -> LockEntry:
    return LockEntry(
        root_id=root_id,  # type: ignore[arg-type]
        directory_name=directory_name,
        plugin_id=plugin_id,
        channel=channel,  # type: ignore[arg-type]
        reason="user_requested",
        installed_at=TS,
        updated_at=TS,
        last_seen_at=TS,
        removed=removed,
        removed_at=TS_LATER if removed else None,
        source_detail=source_detail,
        package_id="pkg-1",
        profile_dir="profiles/demo",
        profile_installed=True,
    )


def _selection(root_id: str, directory_name: str, **kwargs) -> PluginSelection:
    return PluginSelection(
        candidate=CandidateKey(root_id=root_id, directory_name=directory_name),  # type: ignore[arg-type]
        candidate_source=kwargs.get("candidate_source", "market"),
        state_scope=kwargs.get("state_scope", "legacy_shared"),
        state_access_grant=kwargs.get("state_access_grant", "trusted_market_chain"),
        release_chain_id=kwargs.get("release_chain_id", "market-abc"),
        authorized_at=kwargs.get("authorized_at", TS),
    )


def _roundtrip(inventory: PluginInventory) -> PluginInventory:
    return parse_inventory(
        json.loads(serialize_inventory(inventory)), now=TS, schema_version=3
    )


# --- Codec -------------------------------------------------------------------


def test_codec_roundtrip_preserves_a_full_snapshot() -> None:
    builtin = CandidateRecord(
        root_id="builtin",
        directory_name="demo",
        channel="builtin",
        reason="user_requested",
        installed_at=TS,
        updated_at=TS,
        last_seen_at=TS,
    )
    market = CandidateRecord.from_lock_entry(
        _lock_entry(plugin_id="demo", source_detail=_market_detail())
    )
    ref = CandidateRef(root_id="user", directory_name="demo")
    inventory = PluginInventory.build(
        {
            "demo": PluginEntry(
                plugin_id="demo",
                candidates=(builtin, market),
                selected_candidate=ref,
                candidate_source="market",
                enabled=True,
                state_owner=StateOwnership(
                    candidate=ref,
                    state_scope="legacy_shared",
                    state_access_grant="trusted_market_chain",
                    release_chain_id="market-abc",
                    authorized_at=TS,
                ),
            )
        },
        revision=9,
        updated_at=TS_LATER,
        created_at=TS,
    )

    assert _roundtrip(inventory) == inventory


def test_codec_serialization_is_byte_deterministic() -> None:
    entry = PluginEntry(
        plugin_id="demo",
        candidates=(
            CandidateRecord.from_lock_entry(
                _lock_entry(plugin_id="demo", directory_name="b")
            ),
            CandidateRecord.from_lock_entry(
                _lock_entry(plugin_id="demo", directory_name="a")
            ),
        ),
    )
    inventory = PluginInventory.build({"demo": entry}, updated_at=TS)

    first = serialize_inventory(inventory)
    assert first == serialize_inventory(_roundtrip(inventory))


def test_codec_rejects_a_non_object_plugins_field() -> None:
    from plugin.server.application.install_source import InstallSourceError

    with pytest.raises(InstallSourceError) as excinfo:
        parse_inventory({"plugins": []}, now=TS, schema_version=3)
    assert excinfo.value.code == "LOCK_FILE_CORRUPT"


def test_codec_tolerates_a_missing_plugins_field() -> None:
    inventory = parse_inventory({"revision": 3}, now=TS, schema_version=3)
    assert inventory.plugins == {}
    assert inventory.revision == 3


@pytest.mark.parametrize("bad_revision", [0, -1, "5", True, None])
def test_codec_degrades_an_unusable_revision_to_one(bad_revision: object) -> None:
    inventory = parse_inventory(
        {"revision": bad_revision}, now=TS, schema_version=3
    )
    assert inventory.revision == 1


def test_codec_drops_a_selection_that_names_a_removed_candidate() -> None:
    removed = CandidateRecord.from_lock_entry(
        _lock_entry(plugin_id="demo", removed=True)
    )
    inventory = PluginInventory.build(
        {
            "demo": PluginEntry(
                plugin_id="demo",
                candidates=(removed,),
                selected_candidate=CandidateRef(
                    root_id="user", directory_name="demo"
                ),
                candidate_source="market",
            )
        },
        updated_at=TS,
    )

    parsed = _roundtrip(inventory)

    # The row survives; the selection does not, so the pure Resolver re-picks
    # instead of the inventory resurrecting retired code.
    assert parsed.entry("demo").candidates == (removed,)
    assert parsed.entry("demo").selected_candidate is None


def test_codec_keeps_the_ownership_receipt_for_a_removed_candidate() -> None:
    ref = CandidateRef(root_id="user", directory_name="demo")
    removed = CandidateRecord.from_lock_entry(
        _lock_entry(plugin_id="demo", removed=True)
    )
    inventory = PluginInventory.build(
        {
            "demo": PluginEntry(
                plugin_id="demo",
                candidates=(removed,),
                state_owner=StateOwnership(candidate=ref, state_scope="legacy_shared"),
            )
        },
        updated_at=TS,
    )

    owner = _roundtrip(inventory).entry("demo").state_owner
    assert owner is not None and owner.candidate == ref


def test_codec_fails_closed_on_an_ownership_receipt_with_no_row() -> None:
    raw = {
        "schema_version": 3,
        "revision": 1,
        "updated_at": TS,
        "plugins": {
            "demo": {
                "candidates": [],
                "selected_candidate": None,
                "candidate_source": None,
                "enabled": True,
                "state_owner": {
                    "candidate": {"root_id": "user", "directory_name": "ghost"},
                    "state_scope": "legacy_shared",
                    "state_access_grant": "user_authorized",
                    "release_chain_id": None,
                    "authorized_at": TS,
                },
            }
        },
    }

    parsed = parse_inventory(raw, now=TS, schema_version=3)
    assert parsed.entry("demo").state_owner is None


def test_codec_dedups_a_repeated_primary_key_keeping_the_later_row() -> None:
    raw = {
        "schema_version": 3,
        "revision": 1,
        "updated_at": TS,
        "plugins": {
            "demo": {
                "candidates": [
                    {
                        "root_id": "user",
                        "directory_name": "demo",
                        "channel": "manual",
                        "reason": "user_requested",
                        "installed_at": TS,
                        "updated_at": TS,
                        "last_seen_at": TS,
                        "removed": False,
                        "source_detail": None,
                    },
                    {
                        "root_id": "user",
                        "directory_name": "demo",
                        "channel": "market",
                        "reason": "user_requested",
                        "installed_at": TS,
                        "updated_at": TS_LATER,
                        "last_seen_at": TS_LATER,
                        "removed": False,
                        "source_detail": None,
                    },
                ],
            }
        },
    }

    candidates = parse_inventory(raw, now=TS, schema_version=3).entry("demo").candidates
    assert len(candidates) == 1
    assert candidates[0].last_seen_at == TS_LATER


# --- Migration ---------------------------------------------------------------


def test_migration_groups_candidates_and_carries_selection_and_owner() -> None:
    lock = LockFile(
        schema_version=2,
        entries=(
            _lock_entry(
                plugin_id="demo",
                root_id="builtin",
                directory_name="demo",
                channel="builtin",
            ),
            _lock_entry(
                plugin_id="demo",
                directory_name="demo-market",
                source_detail=_market_detail(),
            ),
        ),
        updated_at=TS_LATER,
        created_at=TS,
    )
    selections = {"demo": _selection("user", "demo-market")}
    owners = {"demo": _selection("user", "demo-market")}

    inventory = migrate_lock_to_inventory(
        lock, selections=selections, state_owners=owners, now=TS_LATER
    )

    entry = inventory.entry("demo")
    assert inventory.schema_version == 3
    assert inventory.revision == 1
    assert inventory.created_at == TS
    assert inventory.updated_at == TS_LATER
    assert {c.primary_key for c in entry.candidates} == {
        ("builtin", "demo"),
        ("user", "demo-market"),
    }
    assert entry.selected_candidate.primary_key == ("user", "demo-market")
    assert entry.candidate_source == "market"
    assert entry.state_owner.state_access_grant == "trusted_market_chain"
    assert entry.enabled is True

    assert describe_migration_losses(
        lock, inventory, selections=selections, state_owners=owners
    ) == []


def test_migration_preserves_every_market_provenance_field() -> None:
    detail = _market_detail(version="3.1.4")
    lock = LockFile(
        schema_version=2,
        entries=(_lock_entry(plugin_id="demo", source_detail=detail),),
        updated_at=TS,
        created_at=TS,
    )

    inventory = migrate_lock_to_inventory(lock, now=TS)
    candidate = inventory.entry("demo").candidates[0]

    assert candidate.source_detail == detail
    assert candidate.package_id == "pkg-1"
    assert candidate.profile_dir == "profiles/demo"
    assert candidate.profile_installed is True
    # And it survives a durable write.
    assert _roundtrip(inventory) == inventory


def test_migration_keeps_a_row_whose_plugin_id_is_not_known_yet() -> None:
    lock = LockFile(
        schema_version=2,
        entries=(_lock_entry(plugin_id="", directory_name="pending"),),
        updated_at=TS,
        created_at=TS,
    )

    inventory = migrate_lock_to_inventory(lock, now=TS)

    # v2's placeholder rule: the directory name stands in until the scanner
    # resolves the real id, so provenance is not thrown away.
    assert "pending" in inventory.plugins
    assert inventory.entry("pending").candidates[0].primary_key == ("user", "pending")
    assert describe_migration_losses(lock, inventory) == []


def test_migration_drops_a_selection_whose_candidate_is_soft_removed() -> None:
    lock = LockFile(
        schema_version=2,
        entries=(_lock_entry(plugin_id="demo", removed=True),),
        updated_at=TS,
        created_at=TS,
    )
    selections = {"demo": _selection("user", "demo")}

    inventory = migrate_lock_to_inventory(lock, selections=selections, now=TS)

    assert inventory.entry("demo").selected_candidate is None
    losses = describe_migration_losses(lock, inventory, selections=selections)
    assert len(losses) == 1 and "selection for 'demo'" in losses[0]


def test_migration_keeps_the_ownership_receipt_for_a_soft_removed_candidate() -> None:
    lock = LockFile(
        schema_version=2,
        entries=(_lock_entry(plugin_id="demo", removed=True),),
        updated_at=TS,
        created_at=TS,
    )
    owners = {"demo": _selection("user", "demo")}

    inventory = migrate_lock_to_inventory(lock, state_owners=owners, now=TS)

    owner = inventory.entry("demo").state_owner
    assert owner is not None
    assert owner.candidate.primary_key == ("user", "demo")
    assert describe_migration_losses(lock, inventory, state_owners=owners) == []


def test_migration_fails_closed_on_an_owner_with_no_lock_row() -> None:
    lock = LockFile(schema_version=2, entries=(), updated_at=TS, created_at=TS)
    owners = {"demo": _selection("user", "ghost")}

    inventory = migrate_lock_to_inventory(lock, state_owners=owners, now=TS)

    assert inventory.entry("demo").state_owner is None
    losses = describe_migration_losses(lock, inventory, state_owners=owners)
    assert len(losses) == 1 and "state owner for 'demo'" in losses[0]


def test_migration_is_deterministic_and_repeatable() -> None:
    lock = LockFile(
        schema_version=2,
        entries=(
            _lock_entry(plugin_id="demo", directory_name="b"),
            _lock_entry(plugin_id="demo", directory_name="a"),
        ),
        updated_at=TS,
        created_at=TS,
    )
    selections = {"demo": _selection("user", "a")}

    first = migrate_lock_to_inventory(lock, selections=selections, now=TS)
    second = migrate_lock_to_inventory(lock, selections=selections, now=TS)

    assert first == second
    assert serialize_inventory(first) == serialize_inventory(second)


def test_migration_of_an_empty_lock_yields_an_empty_inventory() -> None:
    lock = LockFile(schema_version=1, entries=(), updated_at=TS, created_at=None)

    inventory = migrate_lock_to_inventory(lock, now=TS)

    assert inventory.plugins == {}
    assert inventory.revision == 1
    assert inventory.created_at is None
    assert _roundtrip(inventory) == inventory


# --- Property ----------------------------------------------------------------


@settings(max_examples=120, deadline=None)
@given(
    rows=st.lists(
        st.tuples(
            st.sampled_from(["builtin", "user"]),
            st.text(alphabet="abcdef-", min_size=1, max_size=6),
            st.sampled_from(["builtin", "manual", "imported", "market"]),
            st.booleans(),
        ),
        min_size=0,
        max_size=6,
    ),
    revision=st.integers(min_value=1, max_value=10_000),
)
def test_property_codec_roundtrip_is_the_identity(rows, revision: int) -> None:
    """Any inventory built from arbitrary rows survives parse ∘ serialize."""

    by_key: dict[tuple[str, str], CandidateRecord] = {}
    for root_id, directory_name, channel, removed in rows:
        record = CandidateRecord(
            root_id=root_id,  # type: ignore[arg-type]
            directory_name=directory_name,
            channel=channel,  # type: ignore[arg-type]
            reason="user_requested",
            installed_at=TS,
            updated_at=TS,
            last_seen_at=TS,
            removed=removed,
            removed_at=TS_LATER if removed else None,
            source_detail=_market_detail() if channel == "market" else None,
        )
        by_key[record.primary_key] = record

    candidates = tuple(
        sorted(by_key.values(), key=lambda c: (c.root_id, c.directory_name))
    )
    live = [c for c in candidates if not c.removed]
    selected = (
        CandidateRef(root_id=live[0].root_id, directory_name=live[0].directory_name)
        if live
        else None
    )
    inventory = PluginInventory.build(
        {
            "demo": PluginEntry(
                plugin_id="demo",
                candidates=candidates,
                selected_candidate=selected,
                candidate_source=live[0].channel if live else None,
            )
        },
        revision=revision,
        updated_at=TS,
        created_at=TS,
    )

    assert _roundtrip(inventory) == inventory
