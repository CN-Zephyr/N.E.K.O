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
from .package_service import PluginPackageService
from .artifacts import PackageArtifactStore
from .replacement import (
    ReplacePluginError,
    ReplacePluginResult,
    replace_plugin,
)

__all__ = [
    "InstallAction",
    "PackageType",
    "PackageArtifactStore",
    "PluginInstallPlan",
    "PluginPackageService",
    "REPLACEMENT_ACTIONS",
    "ReplacePluginError",
    "ReplacePluginResult",
    "backup_path_for",
    "build_install_plan",
    "confirmation_token",
    "is_manifestless_state_directory",
    "merge_directory_contents",
    "remove_directory",
    "replace_plugin",
    "restore_directory",
    "restore_manifest_adjacent_profiles",
]
