"""Pure selection of one runtime candidate per logical plugin ID."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from plugin.server.application.plugins.inventory_store import InventoryResolution


RootId = Literal["builtin", "user"]
ResolutionStatus = Literal["selected", "deleted", "blocked"]


@dataclass(frozen=True, slots=True)
class PluginCandidate:
    logical_plugin_id: str
    root_id: RootId
    directory_name: str
    config_path: Path


@dataclass(frozen=True, slots=True)
class PluginResolution:
    logical_plugin_id: str
    status: ResolutionStatus
    selected: PluginCandidate | None
    rejected: tuple[PluginCandidate, ...]
    reason: str


def resolve_plugin_candidates(
    candidates: list[PluginCandidate],
    *,
    inventory: InventoryResolution,
) -> tuple[PluginResolution, ...]:
    """Resolve candidates without importing plugin code or touching disk."""

    grouped: dict[str, list[PluginCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.logical_plugin_id.casefold(), []).append(candidate)

    resolutions: list[PluginResolution] = []
    for canonical_plugin_id in sorted(grouped):
        group = sorted(
            grouped[canonical_plugin_id],
            key=lambda candidate: (
                0 if candidate.root_id == "builtin" else 1,
                candidate.directory_name,
                str(candidate.config_path),
            ),
        )
        if not inventory.authoritative:
            resolutions.append(
                PluginResolution(
                    logical_plugin_id=canonical_plugin_id,
                    status="blocked",
                    selected=None,
                    rejected=tuple(group),
                    reason="plugin_inventory_unavailable",
                )
            )
            continue
        plugin_id_spellings = {candidate.logical_plugin_id for candidate in group}
        if len(plugin_id_spellings) != 1:
            resolutions.append(
                PluginResolution(
                    logical_plugin_id=canonical_plugin_id,
                    status="blocked",
                    selected=None,
                    rejected=tuple(group),
                    reason="logical_plugin_id_case_collision",
                )
            )
            continue
        plugin_id = group[0].logical_plugin_id
        if canonical_plugin_id in inventory.deleted_plugin_ids:
            resolutions.append(
                PluginResolution(
                    logical_plugin_id=plugin_id,
                    status="deleted",
                    selected=None,
                    rejected=tuple(group),
                    reason="user_deleted",
                )
            )
            continue

        claimed_directory = inventory.active_user_directories.get(canonical_plugin_id)
        if claimed_directory is not None:
            user_candidates = [
                candidate for candidate in group if candidate.root_id == "user"
            ]
            claimed = [
                candidate
                for candidate in user_candidates
                if candidate.directory_name == claimed_directory
            ]
            if len(claimed) == 1 and len(user_candidates) == 1:
                selected = claimed[0]
                resolutions.append(
                    PluginResolution(
                        logical_plugin_id=plugin_id,
                        status="selected",
                        selected=selected,
                        rejected=tuple(item for item in group if item != selected),
                        reason="explicit_user_installation",
                    )
                )
                continue

            if len(claimed) == 1 and len(user_candidates) > 1:
                resolutions.append(
                    PluginResolution(
                        logical_plugin_id=plugin_id,
                        status="blocked",
                        selected=None,
                        rejected=tuple(group),
                        reason="unexpected_user_installation_candidates",
                    )
                )
                continue

            builtin = [candidate for candidate in group if candidate.root_id == "builtin"]
            if len(claimed) == 0 and len(builtin) == 1:
                selected = builtin[0]
                resolutions.append(
                    PluginResolution(
                        logical_plugin_id=plugin_id,
                        status="selected",
                        selected=selected,
                        rejected=tuple(item for item in group if item != selected),
                        reason="missing_user_installation_fallback_builtin",
                    )
                )
                continue

            resolutions.append(
                PluginResolution(
                    logical_plugin_id=plugin_id,
                    status="blocked",
                    selected=None,
                    rejected=tuple(group),
                    reason="claimed_user_installation_missing_or_ambiguous",
                )
            )
            continue

        builtin = [candidate for candidate in group if candidate.root_id == "builtin"]
        users = [candidate for candidate in group if candidate.root_id == "user"]
        if len(builtin) == 1:
            selected = builtin[0]
            resolutions.append(
                PluginResolution(
                    logical_plugin_id=plugin_id,
                    status="selected",
                    selected=selected,
                    rejected=tuple(item for item in group if item != selected),
                    reason="builtin_default",
                )
            )
            continue
        if not builtin and len(users) == 1:
            resolutions.append(
                PluginResolution(
                    logical_plugin_id=plugin_id,
                    status="selected",
                    selected=users[0],
                    rejected=(),
                    reason="single_legacy_user_installation",
                )
            )
            continue

        resolutions.append(
            PluginResolution(
                logical_plugin_id=plugin_id,
                status="blocked",
                selected=None,
                rejected=tuple(group),
                reason="multiple_unclaimed_installations",
            )
        )

    return tuple(resolutions)
