"""Production adapter for the explicit retired-candidate profile operation."""

from __future__ import annotations

from pathlib import Path

from plugin.server.application.install_source import get_install_source_manager
from plugin.server.application.package_management.profile_removal import (
    PackageProfileRemovalRejectedError,
    PackageProfileRemovalTransactionError,
)
from plugin.server.application.plugins.operation_lock import (
    serialized_plugin_operation,
)
from plugin.server.domain.errors import ServerDomainError
from plugin.settings import get_user_package_profiles_root

from .removal_runtime import (
    build_package_profile_removal_coordinator,
    deferred_profile_cleanup_record_path,
)

package_profile_removal_coordinator = build_package_profile_removal_coordinator()


def _candidate_key_is_valid(root_id: str, directory_name: str) -> bool:
    return (
        root_id == "user"
        and bool(directory_name)
        and directory_name not in {".", ".."}
        and "/" not in directory_name
        and "\\" not in directory_name
    )


def _domain_error(
    *,
    code: str,
    message: str,
    status_code: int,
    plugin_id: str,
    reason: str,
    details: dict[str, object] | None = None,
) -> ServerDomainError:
    return ServerDomainError(
        code=code,
        message=message,
        status_code=status_code,
        details={
            "plugin_id": plugin_id,
            "reason": reason,
            **(details or {}),
        },
    )


def _validate_candidate_key(plugin_id: str, root_id: str, directory_name: str) -> None:
    if root_id != "user":
        raise _domain_error(
            code="PLUGIN_PROFILE_DELETE_BUILTIN_FORBIDDEN",
            message="Builtin plugin package profiles cannot be deleted",
            status_code=403,
            plugin_id=plugin_id,
            reason="builtin_candidate",
        )
    if not _candidate_key_is_valid(root_id, directory_name):
        raise _domain_error(
            code="PLUGIN_CANDIDATE_KEY_INVALID",
            message="Invalid plugin candidate directory name",
            status_code=400,
            plugin_id=plugin_id,
            reason="invalid_directory_name",
        )


def list_retained_candidate_package_profiles() -> dict[str, object]:
    """List exact retired candidates whose package profile remains owned.

    This is a discovery projection for the UI, not a deletion planner.  The
    destructive coordinator rechecks profile ownership, sharing, symlinks and
    paths under the cross-process operation lock immediately before removal.
    Local filesystem paths are deliberately omitted from the response.
    """

    registry = get_install_source_manager()
    if registry is None:
        raise _domain_error(
            code="PLUGIN_PROFILE_DELETE_UNAVAILABLE",
            message="Plugin persistence is unavailable",
            status_code=503,
            plugin_id="",
            reason="persistence_unavailable",
        )

    try:
        entries = registry.list_entries(include_removed=True)
    except Exception as exc:
        raise _domain_error(
            code="PLUGIN_PROFILE_LIST_FAILED",
            message="Failed to read retained plugin package profiles",
            status_code=503,
            plugin_id="",
            reason="persistence_read_failed",
            details={"error_type": type(exc).__name__},
        ) from exc

    items: list[dict[str, object]] = []
    for entry in entries:
        plugin_id = str(getattr(entry, "plugin_id", "") or "")
        root_id = str(getattr(entry, "root_id", "") or "")
        directory_name = str(getattr(entry, "directory_name", "") or "")
        channel = str(getattr(entry, "channel", "") or "")
        if (
            not plugin_id
            or not _candidate_key_is_valid(root_id, directory_name)
            or getattr(entry, "removed", False) is not True
            or channel not in {"imported", "market"}
            or getattr(entry, "profile_installed", None) is not True
        ):
            continue

        candidate_code_present = (Path(registry.user_root) / directory_name).exists()
        items.append(
            {
                "plugin_id": plugin_id,
                "candidate": {
                    "root_id": "user",
                    "directory_name": directory_name,
                },
                "source": channel,
                "package_id": str(getattr(entry, "package_id", "") or plugin_id),
                "deletable": not candidate_code_present,
                "blocked_reason": (
                    "candidate_code_present" if candidate_code_present else None
                ),
            }
        )

    items.sort(
        key=lambda item: (
            str(item["plugin_id"]).casefold(),
            str(item["candidate"]["directory_name"]).casefold(),  # type: ignore[index]
        )
    )
    return {"profiles": items, "count": len(items)}


def _map_rejection(plugin_id: str, reason: str) -> ServerDomainError:
    if reason in {"candidate_not_registered", "candidate_identity_mismatch"}:
        return _domain_error(
            code="PLUGIN_CANDIDATE_NOT_FOUND",
            message="The requested plugin candidate does not exist",
            status_code=404,
            plugin_id=plugin_id,
            reason=reason,
        )
    if reason in {"candidate_not_retired", "candidate_code_present"}:
        return _domain_error(
            code="PLUGIN_PROFILE_DELETE_REQUIRES_RETIRED_CANDIDATE",
            message="Delete the plugin candidate code before deleting its package profile",
            status_code=409,
            plugin_id=plugin_id,
            reason=reason,
        )
    if reason in {"candidate_not_package_managed", "profile_not_package_owned"}:
        return _domain_error(
            code="PLUGIN_PROFILE_DELETE_NOT_OWNED",
            message="This candidate has no package-owned profile to delete",
            status_code=409,
            plugin_id=plugin_id,
            reason=reason,
        )
    return _domain_error(
        code="PLUGIN_PROFILE_DELETE_UNSAFE",
        message="The package profile cannot be deleted safely",
        status_code=409,
        plugin_id=plugin_id,
        reason=reason,
    )


@serialized_plugin_operation
async def remove_retired_candidate_package_profile(
    *,
    plugin_id: str,
    root_id: str,
    directory_name: str,
) -> dict[str, object]:
    """Delete one exact retired candidate's proven package-owned profile."""

    _validate_candidate_key(plugin_id, root_id, directory_name)
    registry = get_install_source_manager()
    if registry is None:
        raise _domain_error(
            code="PLUGIN_PROFILE_DELETE_UNAVAILABLE",
            message="Plugin persistence is unavailable",
            status_code=503,
            plugin_id=plugin_id,
            reason="persistence_unavailable",
        )

    candidate_dir = Path(registry.user_root) / directory_name
    try:
        result = await package_profile_removal_coordinator.remove_profile(
            expected_plugin_id=plugin_id,
            candidate_dir=candidate_dir,
            registry=registry,
            profiles_root=get_user_package_profiles_root(),
            deferred_cleanup_record_path=deferred_profile_cleanup_record_path,
        )
    except PackageProfileRemovalRejectedError as exc:
        raise _map_rejection(plugin_id, exc.reason) from exc
    except PackageProfileRemovalTransactionError as exc:
        raise _domain_error(
            code="PLUGIN_PROFILE_DELETE_FAILED",
            message="Failed to delete the plugin package profile",
            status_code=500,
            plugin_id=plugin_id,
            reason=exc.stage,
            details={
                "rollback_status": exc.rollback_status,
                "rollback_errors": list(exc.rollback_errors),
                "error_type": type(exc.cause).__name__,
            },
        ) from exc

    return {
        "success": True,
        "plugin_id": plugin_id,
        "candidate": {
            "root_id": root_id,
            "directory_name": directory_name,
        },
        "package_profile_deleted": True,
        "cleanup_deferred": result.cleanup_deferred,
        "cleanup_recorded": result.cleanup_recorded,
        "message": "Plugin package profile deleted successfully",
    }


__all__ = [
    "list_retained_candidate_package_profiles",
    "package_profile_removal_coordinator",
    "remove_retired_candidate_package_profile",
]
