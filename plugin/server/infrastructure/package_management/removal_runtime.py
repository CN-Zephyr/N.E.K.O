"""Production composition root for candidate-removal filesystem services."""

from __future__ import annotations

from pathlib import Path

from plugin.server.application.package_management.candidate_removal import (
    CandidateRemovalCoordinator,
)
from plugin.server.application.package_management.package_service import (
    PluginPackageService,
)
from plugin.server.application.package_management.profile_cleanup import (
    DEFERRED_PROFILE_CLEANUP_FILENAME,
    PackageProfileService,
)
from plugin.server.application.package_management.profile_removal import (
    PackageProfileRemovalCoordinator,
)
from plugin.settings import get_user_plugin_config_root


def build_candidate_removal_coordinator() -> CandidateRemovalCoordinator:
    """Assemble the production candidate/profile retirement transaction."""
    return CandidateRemovalCoordinator(
        PluginPackageService(),
        PackageProfileService(),
    )


candidate_removal_coordinator = build_candidate_removal_coordinator()


def build_package_profile_removal_coordinator() -> PackageProfileRemovalCoordinator:
    """Assemble the explicit retired-candidate profile transaction."""
    return PackageProfileRemovalCoordinator(PackageProfileService())


def deferred_profile_cleanup_record_path() -> Path:
    """Resolve the durable queue used for post-commit profile cleanup."""
    return (
        get_user_plugin_config_root().expanduser().resolve().parent
        / DEFERRED_PROFILE_CLEANUP_FILENAME
    )


__all__ = [
    "build_candidate_removal_coordinator",
    "build_package_profile_removal_coordinator",
    "candidate_removal_coordinator",
    "deferred_profile_cleanup_record_path",
]
