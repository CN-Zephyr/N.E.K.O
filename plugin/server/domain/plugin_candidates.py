"""Pure plugin-candidate inventory and selection rules.

The logical plugin id remains the runtime and user-facing identity.  A
``PluginCandidate`` identifies one installed code location for that identity;
it is deliberately independent from runtime enablement and process state.

This module performs no filesystem or persistence I/O.  Callers build an
immutable inventory, load an optional desired candidate, and resolve exactly
one effective candidate (or an explicit unavailable result).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Literal, Mapping


CandidateRootId = Literal["builtin", "user"]
CandidateSource = Literal["builtin", "manual", "imported", "market"]
StateAccessGrant = Literal[
    "builtin",
    "initial_identity",
    "trusted_market_chain",
    "user_authorized",
]
ResolutionReason = Literal[
    "explicit_selection",
    "transient_selection",
    "auto_single",
    "auto_canonical_directory",
    "auto_builtin",
    "auto_market",
    "fallback_builtin",
    "fallback_market",
    "desired_missing",
    "desired_invalid",
    "state_authorization_required",
    "ambiguous",
    "no_candidate",
]


@dataclass(frozen=True, slots=True, order=True)
class CandidateKey:
    """Stable installation-slot key; versions may change in place."""

    root_id: CandidateRootId
    directory_name: str


@dataclass(frozen=True, slots=True)
class PluginCandidate:
    """One installed code candidate for a logical plugin id."""

    key: CandidateKey
    plugin_id: str
    config_path: Path
    version: str
    source: CandidateSource
    release_chain_id: str | None = None
    valid: bool = True
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PluginInventory:
    """Immutable, deterministically ordered candidate snapshot."""

    candidates: tuple[PluginCandidate, ...]
    _by_plugin_id: Mapping[str, tuple[PluginCandidate, ...]]

    @classmethod
    def build(cls, candidates: Iterable[PluginCandidate]) -> "PluginInventory":
        ordered = tuple(sorted(candidates, key=_candidate_sort_key))
        grouped: dict[str, list[PluginCandidate]] = {}
        for candidate in ordered:
            grouped.setdefault(candidate.plugin_id, []).append(candidate)
        frozen_groups = MappingProxyType(
            {plugin_id: tuple(items) for plugin_id, items in sorted(grouped.items())}
        )
        return cls(candidates=ordered, _by_plugin_id=frozen_groups)

    @property
    def plugin_ids(self) -> tuple[str, ...]:
        return tuple(self._by_plugin_id)

    def for_plugin(self, plugin_id: str) -> tuple[PluginCandidate, ...]:
        return self._by_plugin_id.get(plugin_id, ())

    def by_config_path(self, config_path: Path) -> PluginCandidate | None:
        resolved_target = _resolve_path(config_path)
        for candidate in self.candidates:
            if _resolve_path(candidate.config_path) == resolved_target:
                return candidate
        return None


@dataclass(frozen=True, slots=True)
class PluginResolution:
    """Effective candidate plus the reason used to select it."""

    plugin_id: str
    candidate: PluginCandidate | None
    reason: ResolutionReason
    desired_candidate: CandidateKey | None
    available_candidates: tuple[PluginCandidate, ...]

    @property
    def is_fallback(self) -> bool:
        return self.reason in {"fallback_builtin", "fallback_market"}


def resolve_plugin_candidate(
    inventory: PluginInventory,
    plugin_id: str,
    *,
    desired_candidate: CandidateKey | None = None,
    transient_candidate: CandidateKey | None = None,
) -> PluginResolution:
    """Resolve at most one candidate without mutating inventory or intent."""

    candidates = inventory.for_plugin(plugin_id)
    valid_candidates = tuple(candidate for candidate in candidates if candidate.valid)

    if transient_candidate is not None:
        overridden = _find_candidate(valid_candidates, transient_candidate)
        if overridden is not None:
            return _resolution(
                plugin_id,
                overridden,
                "transient_selection",
                desired_candidate,
                candidates,
            )

    if desired_candidate is not None:
        desired = _find_candidate(candidates, desired_candidate)
        if desired is not None and desired.valid:
            return _resolution(
                plugin_id,
                desired,
                "explicit_selection",
                desired_candidate,
                candidates,
            )

        fallback, fallback_reason = _select_safe_fallback(valid_candidates, plugin_id)
        if fallback is not None and fallback_reason is not None:
            return _resolution(
                plugin_id,
                fallback,
                fallback_reason,
                desired_candidate,
                candidates,
            )
        return _resolution(
            plugin_id,
            None,
            "desired_invalid" if desired is not None else "desired_missing",
            desired_candidate,
            candidates,
        )

    if not valid_candidates:
        return _resolution(
            plugin_id,
            None,
            "no_candidate",
            None,
            candidates,
        )
    if len(valid_candidates) == 1:
        return _resolution(
            plugin_id,
            valid_candidates[0],
            "auto_single",
            None,
            candidates,
        )

    canonical = tuple(
        candidate
        for candidate in valid_candidates
        if candidate.key.directory_name == plugin_id
    )
    if len(canonical) == 1:
        return _resolution(
            plugin_id,
            canonical[0],
            "auto_canonical_directory",
            None,
            candidates,
        )

    builtin = tuple(
        candidate for candidate in valid_candidates if candidate.source == "builtin"
    )
    if len(builtin) == 1:
        return _resolution(
            plugin_id,
            builtin[0],
            "auto_builtin",
            None,
            candidates,
        )
    if not builtin:
        market = tuple(
            candidate for candidate in valid_candidates if candidate.source == "market"
        )
        if len(market) == 1:
            return _resolution(
                plugin_id,
                market[0],
                "auto_market",
                None,
                candidates,
            )

    return _resolution(
        plugin_id,
        None,
        "ambiguous",
        None,
        candidates,
    )


def requires_legacy_shared_state_authorization(
    previous: PluginCandidate | None,
    target: PluginCandidate,
) -> bool:
    """Return whether a candidate switch needs explicit data inheritance consent.

    The current runtime layout is keyed only by logical plugin id, so switching
    code candidates also grants access to that id's existing config/data/cache.
    Builtins are trusted by the application.  Market packages may inherit
    automatically only when both sides carry the same non-empty market identity.
    A first candidate establishes a new logical identity and has no predecessor
    whose state it could inherit through this switch operation.
    """

    if target.source == "builtin" or previous is None:
        return False
    return not (
        previous.source == "market"
        and target.source == "market"
        and bool(previous.release_chain_id)
        and previous.release_chain_id == target.release_chain_id
    )


def state_access_grant_for_switch(
    previous: PluginCandidate | None,
    target: PluginCandidate,
    *,
    user_authorized: bool,
) -> StateAccessGrant:
    if target.source == "builtin":
        return "builtin"
    if previous is None:
        return "initial_identity"
    if not requires_legacy_shared_state_authorization(previous, target):
        return "trusted_market_chain"
    if user_authorized:
        return "user_authorized"
    raise ValueError("legacy shared state authorization is required")


def _select_safe_fallback(
    candidates: tuple[PluginCandidate, ...],
    plugin_id: str,
) -> tuple[PluginCandidate | None, ResolutionReason | None]:
    builtin = tuple(
        candidate for candidate in candidates if candidate.source == "builtin"
    )
    selected_builtin = _select_unambiguous(builtin, plugin_id)
    if selected_builtin is not None:
        return selected_builtin, "fallback_builtin"

    market = tuple(
        candidate for candidate in candidates if candidate.source == "market"
    )
    selected_market = _select_unambiguous(market, plugin_id)
    if selected_market is not None:
        return selected_market, "fallback_market"
    return None, None


def _select_unambiguous(
    candidates: tuple[PluginCandidate, ...],
    plugin_id: str,
) -> PluginCandidate | None:
    if len(candidates) == 1:
        return candidates[0]
    canonical = tuple(
        candidate
        for candidate in candidates
        if candidate.key.directory_name == plugin_id
    )
    return canonical[0] if len(canonical) == 1 else None


def _find_candidate(
    candidates: tuple[PluginCandidate, ...],
    key: CandidateKey,
) -> PluginCandidate | None:
    return next((candidate for candidate in candidates if candidate.key == key), None)


def _resolution(
    plugin_id: str,
    candidate: PluginCandidate | None,
    reason: ResolutionReason,
    desired_candidate: CandidateKey | None,
    candidates: tuple[PluginCandidate, ...],
) -> PluginResolution:
    return PluginResolution(
        plugin_id=plugin_id,
        candidate=candidate,
        reason=reason,
        desired_candidate=desired_candidate,
        available_candidates=candidates,
    )


def _candidate_sort_key(candidate: PluginCandidate) -> tuple[int, str, str, str]:
    return (
        0 if candidate.key.root_id == "builtin" else 1,
        candidate.key.directory_name.casefold(),
        candidate.key.directory_name,
        str(candidate.config_path),
    )


def _resolve_path(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


__all__ = [
    "CandidateKey",
    "CandidateRootId",
    "CandidateSource",
    "StateAccessGrant",
    "PluginCandidate",
    "PluginInventory",
    "PluginResolution",
    "ResolutionReason",
    "requires_legacy_shared_state_authorization",
    "resolve_plugin_candidate",
    "state_access_grant_for_switch",
]
