"""Explicit transaction for deleting one retired candidate's package profile."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .profile_cleanup import (
    PackageProfileRegistryPort,
    PackageProfileService,
    StagedPackageProfile,
)


class PackageProfileRemovalRegistryPort(PackageProfileRegistryPort, Protocol):
    """Exact candidate read and profile-ownership commit required here."""

    def mark_profile_removed(self, *, directory_path: Path) -> None: ...


@dataclass(frozen=True)
class PackageProfileRemovalResult:
    deleted_profile_dir: Path
    cleanup_deferred: bool = False
    cleanup_recorded: bool | None = None
    staged_path: Path | None = None


class PackageProfileRemovalRejectedError(RuntimeError):
    """The candidate cannot prove that its package profile is removable."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"package profile removal rejected: {reason}")
        self.reason = reason


class PackageProfileRemovalTransactionError(RuntimeError):
    def __init__(
        self,
        *,
        stage: str,
        rollback_status: str,
        cause: Exception,
        rollback_errors: tuple[str, ...] = (),
    ) -> None:
        super().__init__(f"package profile removal {stage} failed: {cause}")
        self.stage = stage
        self.rollback_status = rollback_status
        self.cause = cause
        self.rollback_errors = rollback_errors


class PackageProfileRemovalCoordinator:
    """Stage profile data, commit Registry ownership, then finalize cleanup.

    The caller must hold the candidate's serialized plugin-operation lock for
    the entire call.  This coordinator deliberately accepts only an already
    retired candidate whose Registry row proves package-owned profile data.
    """

    def __init__(self, profile_service: PackageProfileService) -> None:
        self.profile_service = profile_service

    async def remove_profile(
        self,
        *,
        expected_plugin_id: str,
        candidate_dir: Path,
        registry: PackageProfileRemovalRegistryPort,
        profiles_root: Path,
        deferred_cleanup_record_path: Path | Callable[[], Path] | None = None,
    ) -> PackageProfileRemovalResult:
        await self._read_retired_owned_entry(
            expected_plugin_id=expected_plugin_id,
            candidate_dir=candidate_dir,
            registry=registry,
        )

        try:
            staged_profile = await asyncio.to_thread(
                self.profile_service.stage,
                candidate_dir,
                registry=registry,
                profiles_root=profiles_root,
                include_removed=True,
                require_explicit_ownership=True,
            )
        except Exception as exc:
            raise PackageProfileRemovalTransactionError(
                stage="stage",
                rollback_status="not_needed",
                cause=exc,
            ) from exc
        if staged_profile is None:
            raise PackageProfileRemovalRejectedError("profile_missing_shared_or_unsafe")

        try:
            await asyncio.to_thread(
                registry.mark_profile_removed,
                directory_path=candidate_dir,
            )
        except Exception as exc:
            rollback_status, rollback_errors = await self._restore(staged_profile)
            raise PackageProfileRemovalTransactionError(
                stage="commit",
                rollback_status=rollback_status,
                cause=exc,
                rollback_errors=rollback_errors,
            ) from exc

        try:
            deleted_profile_dir = await asyncio.to_thread(
                self.profile_service.finalize,
                staged_profile,
            )
        except OSError:
            cleanup_recorded = await self._record_deferred(
                staged_profile,
                record_path=deferred_cleanup_record_path,
            )
            return PackageProfileRemovalResult(
                deleted_profile_dir=staged_profile.original_dir,
                cleanup_deferred=True,
                cleanup_recorded=cleanup_recorded,
                staged_path=staged_profile.staged_dir,
            )

        return PackageProfileRemovalResult(
            deleted_profile_dir=deleted_profile_dir or staged_profile.original_dir,
        )

    @staticmethod
    async def _read_retired_owned_entry(
        *,
        expected_plugin_id: str,
        candidate_dir: Path,
        registry: PackageProfileRemovalRegistryPort,
    ) -> None:
        try:
            entry = await asyncio.to_thread(
                registry.entry_for_directory,
                candidate_dir,
                include_removed=True,
            )
        except Exception as exc:
            raise PackageProfileRemovalTransactionError(
                stage="inspect",
                rollback_status="not_needed",
                cause=exc,
            ) from exc
        if entry is None:
            raise PackageProfileRemovalRejectedError("candidate_not_registered")
        if getattr(entry, "plugin_id", "") != expected_plugin_id:
            raise PackageProfileRemovalRejectedError("candidate_identity_mismatch")
        if getattr(entry, "removed", False) is not True:
            raise PackageProfileRemovalRejectedError("candidate_not_retired")
        if await asyncio.to_thread(candidate_dir.exists):
            raise PackageProfileRemovalRejectedError("candidate_code_present")
        if getattr(entry, "channel", "") not in {"imported", "market"}:
            raise PackageProfileRemovalRejectedError("candidate_not_package_managed")
        if getattr(entry, "profile_installed", None) is not True:
            raise PackageProfileRemovalRejectedError("profile_not_package_owned")

    async def _restore(
        self,
        staged_profile: StagedPackageProfile,
    ) -> tuple[str, tuple[str, ...]]:
        try:
            await asyncio.to_thread(self.profile_service.restore, staged_profile)
        except Exception as exc:
            return "incomplete", (f"restore_profile:{type(exc).__name__}",)
        return "completed", ()

    async def _record_deferred(
        self,
        staged_profile: StagedPackageProfile,
        *,
        record_path: Path | Callable[[], Path] | None,
    ) -> bool:
        if record_path is None:
            return False
        try:
            resolved_record_path = (
                await asyncio.to_thread(record_path)
                if callable(record_path)
                else record_path
            )
        except Exception:
            return False
        return await asyncio.to_thread(
            self.profile_service.record_deferred,
            staged_profile,
            record_path=resolved_record_path,
        )


__all__ = [
    "PackageProfileRemovalCoordinator",
    "PackageProfileRemovalRegistryPort",
    "PackageProfileRemovalRejectedError",
    "PackageProfileRemovalResult",
    "PackageProfileRemovalTransactionError",
]
