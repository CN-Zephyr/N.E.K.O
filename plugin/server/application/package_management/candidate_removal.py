"""Coordinate candidate code retirement with its durable provenance record."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Protocol

from .package_service import PluginPackageService, StagedCandidateRetirement
from .profile_cleanup import (
    PackageProfileRegistryPort,
    PackageProfileService,
    StagedPackageProfile,
)


class CandidateRetirementPort(Protocol):
    """Narrow Registry/install-source mutation used by candidate removal."""

    def mark_removed(self, *, directory_path: Path) -> None: ...


@dataclass(frozen=True)
class CandidateRemovalResult:
    deleted_from_disk: bool
    cleanup_deferred: bool = False
    staged_path: Path | None = None
    deleted_profile_dir: Path | None = None
    profile_cleanup_deferred: bool = False
    profile_cleanup_recorded: bool | None = None


class CandidateRemovalTransactionError(RuntimeError):
    def __init__(
        self,
        *,
        stage: str,
        rollback_status: str,
        cause: Exception,
        rollback_errors: tuple[str, ...] = (),
    ) -> None:
        super().__init__(f"candidate removal {stage} failed: {cause}")
        self.stage = stage
        self.rollback_status = rollback_status
        self.cause = cause
        self.rollback_errors = rollback_errors


class CandidateRemovalCoordinator:
    """Stage code, commit Registry retirement, then clean the hidden backup."""

    def __init__(
        self,
        package_service: PluginPackageService,
        profile_service: PackageProfileService | None = None,
    ) -> None:
        self.package_service = package_service
        self.profile_service = profile_service

    async def remove_candidate(
        self,
        *,
        target_dir: Path,
        provenance: CandidateRetirementPort | None,
        profile_registry: PackageProfileRegistryPort | None = None,
        profiles_root: Path | None = None,
        deferred_profile_cleanup_record_path: Path | Callable[[], Path] | None = None,
    ) -> CandidateRemovalResult:
        staged_profile: StagedPackageProfile | None = None
        if self.profile_service is not None and profiles_root is not None:
            try:
                staged_profile = await asyncio.to_thread(
                    self.profile_service.stage,
                    target_dir,
                    registry=profile_registry,
                    profiles_root=profiles_root,
                )
            except Exception as exc:
                raise CandidateRemovalTransactionError(
                    stage="profile_stage",
                    rollback_status="not_needed",
                    cause=exc,
                ) from exc

        try:
            staged = await asyncio.to_thread(
                self.package_service.stage_candidate_retirement,
                target_dir,
            )
        except Exception as exc:
            rollback_status, rollback_errors = await self._restore_staged(
                candidate=None,
                profile=staged_profile,
            )
            raise CandidateRemovalTransactionError(
                stage="stage",
                rollback_status=rollback_status,
                cause=exc,
                rollback_errors=rollback_errors,
            ) from exc

        try:
            if provenance is not None:
                await asyncio.to_thread(
                    provenance.mark_removed,
                    directory_path=target_dir,
                )
        except Exception as exc:
            rollback_status, rollback_errors = await self._restore_staged(
                candidate=staged,
                profile=staged_profile,
            )
            raise CandidateRemovalTransactionError(
                stage="retire",
                rollback_status=rollback_status,
                cause=exc,
                rollback_errors=rollback_errors,
            ) from exc

        try:
            await asyncio.to_thread(
                self.package_service.mark_candidate_retirement_committed,
                staged,
            )
        except OSError:
            # The Registry commit is already authoritative. Immediate cleanup
            # remains safe; if it also fails, the hidden backup is retained but
            # will not be auto-deleted without an exact commit marker.
            pass

        cleanup_deferred = False
        staged_path: Path | None = None
        try:
            await asyncio.to_thread(
                self.package_service.finalize_candidate_retirement,
                staged,
            )
        except OSError:
            cleanup_deferred = True
            staged_path = staged.staged_dir

        deleted_profile_dir: Path | None = None
        profile_cleanup_deferred = False
        profile_cleanup_recorded: bool | None = None
        if self.profile_service is not None and staged_profile is not None:
            try:
                deleted_profile_dir = await asyncio.to_thread(
                    self.profile_service.finalize,
                    staged_profile,
                )
            except OSError:
                profile_cleanup_deferred = True
                if deferred_profile_cleanup_record_path is not None:
                    try:
                        record_path = (
                            await asyncio.to_thread(
                                deferred_profile_cleanup_record_path
                            )
                            if callable(deferred_profile_cleanup_record_path)
                            else deferred_profile_cleanup_record_path
                        )
                    except Exception:
                        profile_cleanup_recorded = False
                    else:
                        profile_cleanup_recorded = await asyncio.to_thread(
                            self.profile_service.record_deferred,
                            staged_profile,
                            record_path=record_path,
                        )
                else:
                    profile_cleanup_recorded = False

        return CandidateRemovalResult(
            deleted_from_disk=staged.existed,
            cleanup_deferred=cleanup_deferred,
            staged_path=staged_path,
            deleted_profile_dir=deleted_profile_dir,
            profile_cleanup_deferred=profile_cleanup_deferred,
            profile_cleanup_recorded=profile_cleanup_recorded,
        )

    async def retry_deferred_cleanup(self, roots: tuple[Path, ...]) -> int:
        return await asyncio.to_thread(
            self.package_service.retry_candidate_retirement_cleanup,
            roots,
        )

    async def retry_deferred_profile_cleanup(self, *, record_path: Path) -> int:
        if self.profile_service is None:
            return 0
        return await asyncio.to_thread(
            self.profile_service.retry_deferred,
            record_path=record_path,
        )

    async def _restore_staged(
        self,
        *,
        candidate: StagedCandidateRetirement | None,
        profile: StagedPackageProfile | None,
    ) -> tuple[str, tuple[str, ...]]:
        rollback_needed = False
        rollback_errors: list[str] = []

        if candidate is not None and candidate.existed:
            rollback_needed = True
            try:
                await asyncio.to_thread(
                    self.package_service.restore_candidate_retirement,
                    candidate,
                )
            except Exception as exc:
                rollback_errors.append(f"restore_candidate:{type(exc).__name__}")

        if self.profile_service is not None and profile is not None:
            rollback_needed = True
            try:
                await asyncio.to_thread(self.profile_service.restore, profile)
            except Exception as exc:
                rollback_errors.append(f"restore_profile:{type(exc).__name__}")

        if rollback_errors:
            return "incomplete", tuple(rollback_errors)
        if rollback_needed:
            return "completed", ()
        return "not_needed", ()


__all__ = [
    "CandidateRemovalCoordinator",
    "CandidateRemovalResult",
    "CandidateRemovalTransactionError",
    "CandidateRetirementPort",
]
