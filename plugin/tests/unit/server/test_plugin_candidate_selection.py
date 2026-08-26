from __future__ import annotations

from pathlib import Path

import pytest

from plugin.server.domain.plugin_candidates import (
    CandidateKey,
    CandidateRootId,
    CandidateSource,
    PluginCandidate,
    PluginInventory,
    requires_legacy_shared_state_authorization,
    resolve_plugin_candidate,
)


pytestmark = pytest.mark.plugin_unit


def _candidate(
    root_id: CandidateRootId,
    directory_name: str,
    *,
    source: CandidateSource,
    valid: bool = True,
    release_chain_id: str | None = None,
) -> PluginCandidate:
    return PluginCandidate(
        key=CandidateKey(root_id=root_id, directory_name=directory_name),
        plugin_id="demo",
        config_path=Path(root_id) / directory_name / "plugin.toml",
        version="1.0.0",
        source=source,
        release_chain_id=release_chain_id,
        valid=valid,
        error=None if valid else "invalid manifest",
    )


def test_explicit_selection_wins_over_builtin_and_scan_order() -> None:
    builtin = _candidate("builtin", "demo", source="builtin")
    imported = _candidate("user", "demo-local", source="imported")
    inventory = PluginInventory.build((imported, builtin))

    resolved = resolve_plugin_candidate(
        inventory,
        "demo",
        desired_candidate=imported.key,
    )

    assert resolved.candidate == imported
    assert resolved.reason == "explicit_selection"
    assert inventory.candidates == (builtin, imported)


def test_missing_desired_candidate_falls_back_without_erasing_intent() -> None:
    desired = CandidateKey(root_id="user", directory_name="demo-market")
    builtin = _candidate("builtin", "demo", source="builtin")
    inventory = PluginInventory.build((builtin,))

    resolved = resolve_plugin_candidate(
        inventory,
        "demo",
        desired_candidate=desired,
    )

    assert resolved.candidate == builtin
    assert resolved.reason == "fallback_builtin"
    assert resolved.desired_candidate == desired
    assert resolved.is_fallback is True


def test_missing_desired_candidate_can_fall_back_to_single_market_receipt() -> None:
    market = _candidate("user", "demo-market", source="market")
    inventory = PluginInventory.build((market,))

    resolved = resolve_plugin_candidate(
        inventory,
        "demo",
        desired_candidate=CandidateKey(root_id="builtin", directory_name="demo"),
    )

    assert resolved.candidate == market
    assert resolved.reason == "fallback_market"


def test_missing_desired_candidate_never_silently_falls_back_to_imported() -> None:
    imported = _candidate("user", "demo-local", source="imported")
    inventory = PluginInventory.build((imported,))

    resolved = resolve_plugin_candidate(
        inventory,
        "demo",
        desired_candidate=CandidateKey(root_id="builtin", directory_name="demo"),
    )

    assert resolved.candidate is None
    assert resolved.reason == "desired_missing"


def test_canonical_directory_breaks_legacy_suffix_ambiguity_deterministically() -> None:
    canonical = _candidate("user", "demo", source="manual")
    legacy_suffix = _candidate("user", "demo_1", source="manual")

    forward = resolve_plugin_candidate(
        PluginInventory.build((canonical, legacy_suffix)),
        "demo",
    )
    reverse = resolve_plugin_candidate(
        PluginInventory.build((legacy_suffix, canonical)),
        "demo",
    )

    assert forward.candidate == canonical
    assert reverse.candidate == canonical
    assert forward.reason == reverse.reason == "auto_canonical_directory"


def test_multiple_noncanonical_manual_candidates_require_a_choice() -> None:
    inventory = PluginInventory.build(
        (
            _candidate("user", "demo-a", source="manual"),
            _candidate("user", "demo-b", source="imported"),
        )
    )

    resolved = resolve_plugin_candidate(inventory, "demo")

    assert resolved.candidate is None
    assert resolved.reason == "ambiguous"


def test_invalid_explicit_candidate_uses_safe_builtin_fallback() -> None:
    invalid = _candidate("user", "demo-local", source="imported", valid=False)
    builtin = _candidate("builtin", "demo", source="builtin")
    inventory = PluginInventory.build((invalid, builtin))

    resolved = resolve_plugin_candidate(
        inventory,
        "demo",
        desired_candidate=invalid.key,
    )

    assert resolved.candidate == builtin
    assert resolved.reason == "fallback_builtin"


def test_imported_candidate_cannot_implicitly_inherit_builtin_state() -> None:
    builtin = _candidate("builtin", "demo", source="builtin")
    imported = _candidate("user", "demo-local", source="imported")

    assert requires_legacy_shared_state_authorization(builtin, imported) is True


def test_market_release_chain_can_reuse_its_own_state() -> None:
    previous = _candidate(
        "user",
        "demo-market-old",
        source="market",
        release_chain_id="market.demo",
    )
    target = _candidate(
        "user",
        "demo-market-new",
        source="market",
        release_chain_id="market.demo",
    )
    unrelated = _candidate(
        "user",
        "demo-market-other",
        source="market",
        release_chain_id="market.other",
    )

    assert requires_legacy_shared_state_authorization(previous, target) is False
    assert requires_legacy_shared_state_authorization(previous, unrelated) is True
