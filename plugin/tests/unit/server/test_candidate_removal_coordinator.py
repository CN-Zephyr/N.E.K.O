from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plugin.server.application.package_management.candidate_removal import (
    CandidateRemovalCoordinator,
    CandidateRemovalTransactionError,
)
from plugin.server.application.package_management.package_service import (
    PluginPackageService,
    StagedCandidateRetirement,
)
from plugin.server.application.package_management.profile_cleanup import (
    PackageProfileService,
)
from plugin.server.infrastructure.package_management.removal_runtime import (
    build_candidate_removal_coordinator,
)


pytestmark = pytest.mark.plugin_unit


class _Provenance:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail
        self.calls: list[Path] = []

    def mark_removed(self, *, directory_path: Path) -> None:
        self.calls.append(directory_path)
        assert directory_path.exists() is False
        assert any((directory_path.parent / ".delete-backups").iterdir())
        if self.fail is not None:
            raise self.fail


class _ProfileRegistry:
    def __init__(self, *, profile_dir: Path) -> None:
        self.entry = SimpleNamespace(
            channel="imported",
            package_id="demo_package",
            plugin_id="demo",
            profile_dir=str(profile_dir),
            profile_installed=True,
            root_id="user",
            directory_name="demo",
        )

    def entry_for_directory(
        self,
        _directory_path: Path,
        *,
        include_removed: bool = False,
    ) -> object:
        assert include_removed is False
        return self.entry

    def list_entries(self) -> list[object]:
        return [self.entry]


def _candidate(tmp_path: Path) -> Path:
    target = tmp_path / "plugins" / "demo"
    target.mkdir(parents=True)
    (target / "plugin.toml").write_text("[plugin]\nid='demo'\n", encoding="utf-8")
    return target


def _profile(tmp_path: Path) -> tuple[Path, Path, _ProfileRegistry]:
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / "demo_package"
    profile_dir.mkdir(parents=True)
    (profile_dir / "settings.toml").write_text("value = true\n", encoding="utf-8")
    return profiles_root, profile_dir, _ProfileRegistry(profile_dir=profile_dir)


def test_production_composition_builds_isolated_removal_services() -> None:
    first = build_candidate_removal_coordinator()
    second = build_candidate_removal_coordinator()

    assert isinstance(first.package_service, PluginPackageService)
    assert isinstance(first.profile_service, PackageProfileService)
    assert first is not second
    assert first.package_service is not second.package_service
    assert first.profile_service is not second.profile_service


@pytest.mark.asyncio
async def test_candidate_removal_stages_then_retires_then_cleans(
    tmp_path: Path,
) -> None:
    target = _candidate(tmp_path)
    provenance = _Provenance()
    coordinator = CandidateRemovalCoordinator(PluginPackageService())

    result = await coordinator.remove_candidate(
        target_dir=target,
        provenance=provenance,
    )

    assert result.deleted_from_disk is True
    assert result.cleanup_deferred is False
    assert result.staged_path is None
    assert provenance.calls == [target]
    assert target.exists() is False
    assert (target.parent / ".delete-backups").exists() is False


@pytest.mark.asyncio
async def test_registry_retire_failure_restores_exact_candidate_directory(
    tmp_path: Path,
) -> None:
    target = _candidate(tmp_path)
    original_bytes = (target / "plugin.toml").read_bytes()
    provenance = _Provenance(fail=PermissionError("registry busy"))
    coordinator = CandidateRemovalCoordinator(PluginPackageService())

    with pytest.raises(CandidateRemovalTransactionError) as exc_info:
        await coordinator.remove_candidate(
            target_dir=target,
            provenance=provenance,
        )

    assert exc_info.value.stage == "retire"
    assert exc_info.value.rollback_status == "completed"
    assert provenance.calls == [target]
    assert (target / "plugin.toml").read_bytes() == original_bytes
    assert (target.parent / ".delete-backups").exists() is False


@pytest.mark.asyncio
async def test_registry_retire_failure_reports_incomplete_package_restore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = _candidate(tmp_path)
    package_service = PluginPackageService()
    provenance = _Provenance(fail=PermissionError("registry busy"))

    def _fail_restore(_staged: StagedCandidateRetirement) -> None:
        raise PermissionError("restore busy")

    monkeypatch.setattr(package_service, "restore_candidate_retirement", _fail_restore)
    coordinator = CandidateRemovalCoordinator(package_service)

    with pytest.raises(CandidateRemovalTransactionError) as exc_info:
        await coordinator.remove_candidate(
            target_dir=target,
            provenance=provenance,
        )

    assert exc_info.value.stage == "retire"
    assert exc_info.value.rollback_status == "incomplete"
    assert target.exists() is False
    assert any((target.parent / ".delete-backups").iterdir())


@pytest.mark.asyncio
async def test_cleanup_failure_keeps_retired_code_hidden_for_deferred_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = _candidate(tmp_path)
    package_service = PluginPackageService()
    provenance = _Provenance()

    def _fail_cleanup(_staged: StagedCandidateRetirement) -> None:
        raise PermissionError("cleanup busy")

    monkeypatch.setattr(package_service, "finalize_candidate_retirement", _fail_cleanup)
    coordinator = CandidateRemovalCoordinator(package_service)

    result = await coordinator.remove_candidate(
        target_dir=target,
        provenance=provenance,
    )

    assert result.deleted_from_disk is True
    assert result.cleanup_deferred is True
    assert result.staged_path is not None
    assert result.staged_path.is_dir()
    assert result.staged_path.parent.name == ".delete-backups"
    marker = result.staged_path.with_name(f".{result.staged_path.name}.committed")
    assert marker.is_file()
    assert target.exists() is False

    assert await coordinator.retry_deferred_cleanup((target.parent,)) == 1
    assert result.staged_path.exists() is False
    assert marker.exists() is False
    assert result.staged_path.parent.exists() is False


@pytest.mark.asyncio
async def test_retry_never_deletes_uncommitted_hidden_candidate(tmp_path: Path) -> None:
    target = _candidate(tmp_path)
    package_service = PluginPackageService()
    staged = package_service.stage_candidate_retirement(target)
    assert staged.staged_dir is not None
    coordinator = CandidateRemovalCoordinator(package_service)

    assert await coordinator.retry_deferred_cleanup((target.parent,)) == 0
    assert staged.staged_dir.is_dir()
    assert target.exists() is False

    package_service.restore_candidate_retirement(staged)
    assert target.is_dir()


@pytest.mark.asyncio
async def test_candidate_removal_commits_code_and_package_profile_together(
    tmp_path: Path,
) -> None:
    target = _candidate(tmp_path)
    profiles_root, profile_dir, profile_registry = _profile(tmp_path)
    provenance = _Provenance()
    coordinator = CandidateRemovalCoordinator(
        PluginPackageService(),
        PackageProfileService(),
    )

    result = await coordinator.remove_candidate(
        target_dir=target,
        provenance=provenance,
        profile_registry=profile_registry,
        profiles_root=profiles_root,
        deferred_profile_cleanup_record_path=tmp_path / "profile-cleanup.json",
    )

    assert result.deleted_from_disk is True
    assert result.deleted_profile_dir == profile_dir
    assert result.profile_cleanup_deferred is False
    assert target.exists() is False
    assert profile_dir.exists() is False
    assert list(profiles_root.glob(".demo_package.deleting-*")) == []


@pytest.mark.asyncio
async def test_candidate_stage_failure_restores_profile_before_reporting_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = _candidate(tmp_path)
    profiles_root, profile_dir, profile_registry = _profile(tmp_path)
    original_profile = (profile_dir / "settings.toml").read_bytes()
    package_service = PluginPackageService()

    def _fail_candidate_stage(_target_dir: Path) -> StagedCandidateRetirement:
        raise PermissionError("candidate busy")

    monkeypatch.setattr(
        package_service,
        "stage_candidate_retirement",
        _fail_candidate_stage,
    )
    coordinator = CandidateRemovalCoordinator(
        package_service,
        PackageProfileService(),
    )

    with pytest.raises(CandidateRemovalTransactionError) as exc_info:
        await coordinator.remove_candidate(
            target_dir=target,
            provenance=_Provenance(),
            profile_registry=profile_registry,
            profiles_root=profiles_root,
        )

    assert exc_info.value.stage == "stage"
    assert exc_info.value.rollback_status == "completed"
    assert exc_info.value.rollback_errors == ()
    assert target.is_dir()
    assert (profile_dir / "settings.toml").read_bytes() == original_profile
    assert list(profiles_root.glob(".demo_package.deleting-*")) == []


@pytest.mark.asyncio
async def test_registry_retire_failure_restores_candidate_and_profile(
    tmp_path: Path,
) -> None:
    target = _candidate(tmp_path)
    profiles_root, profile_dir, profile_registry = _profile(tmp_path)
    original_candidate = (target / "plugin.toml").read_bytes()
    original_profile = (profile_dir / "settings.toml").read_bytes()
    coordinator = CandidateRemovalCoordinator(
        PluginPackageService(),
        PackageProfileService(),
    )

    with pytest.raises(CandidateRemovalTransactionError) as exc_info:
        await coordinator.remove_candidate(
            target_dir=target,
            provenance=_Provenance(fail=PermissionError("registry busy")),
            profile_registry=profile_registry,
            profiles_root=profiles_root,
        )

    assert exc_info.value.stage == "retire"
    assert exc_info.value.rollback_status == "completed"
    assert (target / "plugin.toml").read_bytes() == original_candidate
    assert (profile_dir / "settings.toml").read_bytes() == original_profile


@pytest.mark.asyncio
async def test_profile_cleanup_failure_is_recorded_and_retried_by_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = _candidate(tmp_path)
    profiles_root, profile_dir, profile_registry = _profile(tmp_path)
    record_path = tmp_path / "profile-cleanup.json"
    profile_service = PackageProfileService()

    def _fail_profile_cleanup(_staged: object) -> None:
        raise PermissionError("profile busy")

    monkeypatch.setattr(profile_service, "finalize", _fail_profile_cleanup)
    coordinator = CandidateRemovalCoordinator(
        PluginPackageService(),
        profile_service,
    )

    result = await coordinator.remove_candidate(
        target_dir=target,
        provenance=_Provenance(),
        profile_registry=profile_registry,
        profiles_root=profiles_root,
        deferred_profile_cleanup_record_path=lambda: record_path,
    )

    assert result.deleted_from_disk is True
    assert result.deleted_profile_dir is None
    assert result.profile_cleanup_deferred is True
    assert result.profile_cleanup_recorded is True
    assert profile_dir.exists() is False
    staged_profiles = list(profiles_root.glob(".demo_package.deleting-*"))
    assert len(staged_profiles) == 1
    assert record_path.is_file()

    assert (
        await coordinator.retry_deferred_profile_cleanup(record_path=record_path) == 1
    )
    assert staged_profiles[0].exists() is False
    assert record_path.exists() is False


@pytest.mark.asyncio
async def test_candidate_stage_failure_reports_incomplete_profile_restore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = _candidate(tmp_path)
    profiles_root, profile_dir, profile_registry = _profile(tmp_path)
    package_service = PluginPackageService()
    profile_service = PackageProfileService()

    def _fail_candidate_stage(_target_dir: Path) -> StagedCandidateRetirement:
        raise PermissionError("candidate busy")

    def _fail_profile_restore(_staged: object) -> None:
        raise PermissionError("profile restore busy")

    monkeypatch.setattr(
        package_service,
        "stage_candidate_retirement",
        _fail_candidate_stage,
    )
    monkeypatch.setattr(profile_service, "restore", _fail_profile_restore)
    coordinator = CandidateRemovalCoordinator(package_service, profile_service)

    with pytest.raises(CandidateRemovalTransactionError) as exc_info:
        await coordinator.remove_candidate(
            target_dir=target,
            provenance=_Provenance(),
            profile_registry=profile_registry,
            profiles_root=profiles_root,
        )

    assert exc_info.value.stage == "stage"
    assert exc_info.value.rollback_status == "incomplete"
    assert exc_info.value.rollback_errors == ("restore_profile:PermissionError",)
    assert target.is_dir()
    assert profile_dir.exists() is False
    assert len(list(profiles_root.glob(".demo_package.deleting-*"))) == 1


@pytest.mark.asyncio
async def test_unavailable_deferred_record_path_keeps_profile_hidden(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = _candidate(tmp_path)
    profiles_root, profile_dir, profile_registry = _profile(tmp_path)
    profile_service = PackageProfileService()

    def _fail_profile_cleanup(_staged: object) -> None:
        raise PermissionError("profile busy")

    def _unavailable_record_path() -> Path:
        raise RuntimeError("config root unavailable")

    monkeypatch.setattr(profile_service, "finalize", _fail_profile_cleanup)
    coordinator = CandidateRemovalCoordinator(
        PluginPackageService(),
        profile_service,
    )

    result = await coordinator.remove_candidate(
        target_dir=target,
        provenance=_Provenance(),
        profile_registry=profile_registry,
        profiles_root=profiles_root,
        deferred_profile_cleanup_record_path=_unavailable_record_path,
    )

    assert result.deleted_from_disk is True
    assert result.profile_cleanup_deferred is True
    assert result.profile_cleanup_recorded is False
    assert profile_dir.exists() is False
    assert len(list(profiles_root.glob(".demo_package.deleting-*"))) == 1
