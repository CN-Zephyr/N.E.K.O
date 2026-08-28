"""Runtime plugin package-management application boundary.

This package owns package inspection/install planning and, in later slices,
managed package-store mutations. HTTP routes, Market transport, developer
build/publish tooling, registry persistence, and runtime lifecycle do not
belong here.
"""

from .install_plan import (
    REPLACEMENT_ACTIONS,
    InstallAction,
    PackageType,
    PluginInstallPlan,
    build_install_plan,
    confirmation_token,
    is_manifestless_state_directory,
)
from .filesystem import (
    backup_path_for,
    merge_directory_contents,
    remove_directory,
    restore_directory,
    restore_manifest_adjacent_profiles,
)
from .package_service import PluginPackageService, StagedCandidateRetirement
from .profile_cleanup import (
    DEFERRED_PROFILE_CLEANUP_FILENAME,
    PackageProfileRegistryPort,
    PackageProfileService,
    StagedPackageProfile,
    finalize_staged_package_profile,
    record_deferred_profile_cleanup,
    restore_staged_package_profile,
    retry_deferred_profile_cleanup,
    stage_orphaned_package_profile,
)
from .profile_removal import (
    PackageProfileRemovalCoordinator,
    PackageProfileRemovalRegistryPort,
    PackageProfileRemovalRejectedError,
    PackageProfileRemovalResult,
    PackageProfileRemovalTransactionError,
)
from .candidate_removal import (
    CandidateRemovalCoordinator,
    CandidateRemovalResult,
    CandidateRemovalTransactionError,
    CandidateRetirementPort,
)
from .artifacts import PackageArtifactStore
from .replacement import (
    ReplacePluginError,
    ReplacePluginResult,
    replace_plugin,
)
from .coordinator import (
    InstallationCoordinator,
    LocalImportPort,
    LocalImportRequest,
    LocalImportResult,
    LocalReplacementPort,
    LocalReplacementRequest,
    MarketFreshInstallRequest,
    MarketFreshInstallResult,
    MarketInstallPort,
    MarketReplacementInstallRequest,
    MarketReplacementInstallResult,
    MarketReplacementPlanChangedError,
    MarketReplacementTransactionError,
    MarketReplacementTransactionPort,
    MarketReplacementTransactionRequest,
)

__all__ = [
    "InstallAction",
    "InstallationCoordinator",
    "CandidateRemovalCoordinator",
    "CandidateRemovalResult",
    "CandidateRemovalTransactionError",
    "CandidateRetirementPort",
    "DEFERRED_PROFILE_CLEANUP_FILENAME",
    "LocalImportPort",
    "LocalImportRequest",
    "LocalImportResult",
    "LocalReplacementPort",
    "LocalReplacementRequest",
    "MarketFreshInstallRequest",
    "MarketFreshInstallResult",
    "MarketInstallPort",
    "MarketReplacementInstallRequest",
    "MarketReplacementInstallResult",
    "MarketReplacementPlanChangedError",
    "MarketReplacementTransactionError",
    "MarketReplacementTransactionPort",
    "MarketReplacementTransactionRequest",
    "PackageType",
    "PackageArtifactStore",
    "PackageProfileRegistryPort",
    "PackageProfileRemovalCoordinator",
    "PackageProfileRemovalRegistryPort",
    "PackageProfileRemovalRejectedError",
    "PackageProfileRemovalResult",
    "PackageProfileRemovalTransactionError",
    "PackageProfileService",
    "PluginInstallPlan",
    "PluginPackageService",
    "StagedCandidateRetirement",
    "StagedPackageProfile",
    "REPLACEMENT_ACTIONS",
    "ReplacePluginError",
    "ReplacePluginResult",
    "backup_path_for",
    "build_install_plan",
    "confirmation_token",
    "finalize_staged_package_profile",
    "is_manifestless_state_directory",
    "merge_directory_contents",
    "remove_directory",
    "record_deferred_profile_cleanup",
    "replace_plugin",
    "restore_directory",
    "restore_manifest_adjacent_profiles",
    "restore_staged_package_profile",
    "retry_deferred_profile_cleanup",
    "stage_orphaned_package_profile",
]
