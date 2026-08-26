"""Compatibility imports for the former Plugin CLI install-plan module.

New runtime code must import from ``application.package_management``. This
module remains temporarily so #2958 tests and external development tooling do
not need to change in the boundary-only slice.
"""

from plugin.server.application.package_management.install_plan import (
    REPLACEMENT_ACTIONS,
    InstallAction,
    PackageType,
    PluginInstallPlan,
    build_install_plan,
    confirmation_token,
    is_manifestless_state_directory,
)

__all__ = [
    "InstallAction",
    "PackageType",
    "PluginInstallPlan",
    "REPLACEMENT_ACTIONS",
    "build_install_plan",
    "confirmation_token",
    "is_manifestless_state_directory",
]
