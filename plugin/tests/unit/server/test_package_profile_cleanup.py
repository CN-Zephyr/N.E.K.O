from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plugin.server.application.package_management.profile_cleanup import (
    StagedPackageProfile,
    finalize_staged_package_profile,
    load_deferred_profile_cleanup_paths,
    record_deferred_profile_cleanup,
    retry_deferred_profile_cleanup,
    stage_orphaned_package_profile,
)


pytestmark = pytest.mark.plugin_unit


class _ProfileRegistry:
    def __init__(
        self,
        *,
        package_id: str,
        profile_dir: str = "",
        profile_installed: bool | None = None,
        channel: str = "imported",
        root_id: str = "user",
        active_package_ids: tuple[str, ...] = (),
        active_profile_dirs: tuple[str, ...] = (),
        active_root_ids: tuple[str, ...] = (),
        active_directory_names: tuple[str, ...] = (),
        active_channels: tuple[str, ...] = (),
        list_entries_error: Exception | None = None,
    ) -> None:
        self.package_id = package_id
        self.profile_dir = profile_dir
        self.profile_installed = profile_installed
        self.channel = channel
        self.root_id = root_id
        self.active_package_ids = active_package_ids
        self.active_profile_dirs = active_profile_dirs
        self.active_root_ids = active_root_ids
        self.active_directory_names = active_directory_names
        self.active_channels = active_channels
        self.list_entries_error = list_entries_error

    def entry_for_directory(
        self,
        directory_path: Path,
        *,
        include_removed: bool = False,
    ) -> SimpleNamespace:
        assert include_removed is False
        return SimpleNamespace(
            package_id=self.package_id,
            plugin_id=directory_path.name,
            profile_dir=self.profile_dir,
            profile_installed=self.profile_installed,
            channel=self.channel,
            root_id=self.root_id,
            directory_name=directory_path.name,
        )

    def list_entries(self) -> list[SimpleNamespace]:
        if self.list_entries_error is not None:
            raise self.list_entries_error
        return [
            SimpleNamespace(
                package_id=package_id,
                plugin_id=f"other_{index}",
                profile_dir=(
                    self.active_profile_dirs[index]
                    if index < len(self.active_profile_dirs)
                    else ""
                ),
                profile_installed=None,
                channel=(
                    self.active_channels[index]
                    if index < len(self.active_channels)
                    else "imported"
                ),
                root_id=(
                    self.active_root_ids[index]
                    if index < len(self.active_root_ids)
                    else "user"
                ),
                directory_name=(
                    self.active_directory_names[index]
                    if index < len(self.active_directory_names)
                    else f"other_{index}"
                ),
            )
            for index, package_id in enumerate(self.active_package_ids)
        ]


def _stage(
    plugin_dir: Path,
    *,
    registry: _ProfileRegistry,
    profiles_root: Path,
) -> StagedPackageProfile | None:
    return stage_orphaned_package_profile(
        plugin_dir,
        registry=registry,
        profiles_root=profiles_root,
    )


def test_keeps_profile_shared_by_another_bundle_plugin(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "first_bundle_plugin"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / "shared_bundle"
    profile_dir.mkdir(parents=True)
    registry = _ProfileRegistry(
        package_id="shared_bundle",
        active_package_ids=("shared_bundle",),
    )

    assert _stage(plugin_dir, registry=registry, profiles_root=profiles_root) is None
    assert profile_dir.is_dir()


def test_legacy_empty_package_id_uses_plugin_directory_name(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "legacy_plugin"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / plugin_dir.name
    profile_dir.mkdir(parents=True)

    staged = _stage(
        plugin_dir,
        registry=_ProfileRegistry(package_id=""),
        profiles_root=profiles_root,
    )

    assert staged is not None
    assert staged.original_dir == profile_dir
    assert finalize_staged_package_profile(staged) == profile_dir
    assert profile_dir.exists() is False


@pytest.mark.parametrize("current_package_id", ["", "shared_pkg"])
def test_keeps_profile_when_another_package_row_has_no_package_id(
    tmp_path: Path,
    current_package_id: str,
) -> None:
    plugin_dir = tmp_path / (current_package_id or "legacy_bundle")
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / plugin_dir.name
    profile_dir.mkdir(parents=True)
    (profile_dir / "sibling.toml").write_text("keep = true\n", encoding="utf-8")
    registry = _ProfileRegistry(
        package_id=current_package_id,
        profile_dir=str(profile_dir) if current_package_id else "",
        active_package_ids=("",),
        active_directory_names=("legacy_sibling",),
    )

    assert _stage(plugin_dir, registry=registry, profiles_root=profiles_root) is None
    assert (profile_dir / "sibling.toml").is_file()


def test_identifiable_other_package_does_not_block_legacy_cleanup(
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "legacy_plugin"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / plugin_dir.name
    profile_dir.mkdir(parents=True)
    registry = _ProfileRegistry(
        package_id="",
        active_package_ids=("other_package",),
        active_directory_names=("other_plugin",),
    )

    staged = _stage(plugin_dir, registry=registry, profiles_root=profiles_root)

    assert staged is not None
    assert profile_dir.exists() is False


@pytest.mark.parametrize(
    ("channel", "profile_installed"),
    [("manual", None), ("imported", False)],
)
def test_only_package_installer_owned_profiles_are_staged(
    tmp_path: Path,
    channel: str,
    profile_installed: bool | None,
) -> None:
    plugin_dir = tmp_path / "demo_plugin"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / "demo_package"
    profile_dir.mkdir(parents=True)
    registry = _ProfileRegistry(
        package_id="demo_package",
        channel=channel,
        profile_installed=profile_installed,
    )

    assert _stage(plugin_dir, registry=registry, profiles_root=profiles_root) is None
    assert profile_dir.is_dir()


def test_same_profile_in_another_root_is_still_shared(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "same_name"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / "shared_package"
    profile_dir.mkdir(parents=True)
    registry = _ProfileRegistry(
        package_id="shared_package",
        profile_dir=str(profile_dir),
        root_id="user",
        active_package_ids=("shared_package",),
        active_profile_dirs=(str(profile_dir),),
        active_root_ids=("builtin",),
        active_directory_names=(plugin_dir.name,),
    )

    assert _stage(plugin_dir, registry=registry, profiles_root=profiles_root) is None
    assert profile_dir.is_dir()


def test_manual_sibling_does_not_claim_package_profile(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "imported_plugin"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / "shared_package"
    profile_dir.mkdir(parents=True)
    registry = _ProfileRegistry(
        package_id="shared_package",
        profile_dir=str(profile_dir),
        active_package_ids=("shared_package",),
        active_profile_dirs=(str(profile_dir),),
        active_channels=("manual",),
    )

    staged = _stage(plugin_dir, registry=registry, profiles_root=profiles_root)

    assert staged is not None
    assert finalize_staged_package_profile(staged) == profile_dir


@pytest.mark.parametrize("configured_root_changed", [False, True])
def test_recorded_custom_profile_root_remains_authoritative(
    tmp_path: Path,
    configured_root_changed: bool,
) -> None:
    plugin_dir = tmp_path / "custom_profile_plugin"
    recorded_root = tmp_path / "old_profiles"
    profiles_root = (
        tmp_path / "current_profiles" if configured_root_changed else recorded_root
    )
    profile_dir = recorded_root / "custom" / "custom_package"
    profile_dir.mkdir(parents=True)
    registry = _ProfileRegistry(
        package_id="custom_package",
        profile_dir=str(profile_dir),
        profile_installed=True,
    )

    staged = _stage(plugin_dir, registry=registry, profiles_root=profiles_root)

    assert staged is not None
    assert finalize_staged_package_profile(staged) == profile_dir
    assert profile_dir.exists() is False


def test_same_package_id_at_different_custom_root_is_not_shared(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "first_plugin"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / "first" / "shared_package"
    other_profile_dir = profiles_root / "second" / "shared_package"
    profile_dir.mkdir(parents=True)
    other_profile_dir.mkdir(parents=True)
    registry = _ProfileRegistry(
        package_id="shared_package",
        profile_dir=str(profile_dir),
        active_package_ids=("shared_package",),
        active_profile_dirs=(str(other_profile_dir),),
    )

    staged = _stage(plugin_dir, registry=registry, profiles_root=profiles_root)

    assert staged is not None
    assert finalize_staged_package_profile(staged) == profile_dir
    assert other_profile_dir.is_dir()


@pytest.mark.parametrize("symlink_target", ["profile", "ancestor"])
def test_refuses_symlinked_recorded_profile_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    symlink_target: str,
) -> None:
    plugin_dir = tmp_path / "symlinked_plugin"
    profiles_root = tmp_path / "profiles"
    ancestor = tmp_path / "old_profiles"
    profile_dir = ancestor / "symlinked_package"
    simulated_link = profile_dir if symlink_target == "profile" else ancestor
    original_is_symlink = Path.is_symlink

    def _is_symlink(path: Path) -> bool:
        return path == simulated_link or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", _is_symlink)
    registry = _ProfileRegistry(
        package_id="symlinked_package",
        profile_dir=str(profile_dir),
        profile_installed=True,
    )

    assert _stage(plugin_dir, registry=registry, profiles_root=profiles_root) is None


def test_registry_listing_failure_keeps_profile(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "demo_plugin"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / "demo_package"
    profile_dir.mkdir(parents=True)
    registry = _ProfileRegistry(
        package_id="demo_package",
        list_entries_error=RuntimeError("registry unavailable"),
    )

    assert _stage(plugin_dir, registry=registry, profiles_root=profiles_root) is None
    assert profile_dir.is_dir()


def test_profile_staging_failure_is_propagated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "demo_plugin"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / "demo_package"
    profile_dir.mkdir(parents=True)

    def _fail_to_stage(self: Path, target: Path) -> Path:
        assert self == profile_dir
        assert target.name.startswith(".demo_package.deleting-")
        raise PermissionError("profile is in use")

    monkeypatch.setattr(Path, "replace", _fail_to_stage)

    with pytest.raises(PermissionError, match="profile is in use"):
        _stage(
            plugin_dir,
            registry=_ProfileRegistry(package_id="demo_package"),
            profiles_root=profiles_root,
        )

    assert profile_dir.is_dir()


@pytest.mark.parametrize("package_id", ["demo_package", "com.example.demo"])
def test_deferred_profile_cleanup_is_persisted_and_retried(
    tmp_path: Path,
    package_id: str,
) -> None:
    record_path = tmp_path / "package_profile_cleanup.json"
    staged_dir = tmp_path / "profiles" / f".{package_id}.deleting-{'a' * 32}"
    staged_dir.mkdir(parents=True)
    (staged_dir / "config.toml").write_text("value = true\n", encoding="utf-8")
    staged = StagedPackageProfile(
        original_dir=tmp_path / "profiles" / package_id,
        staged_dir=staged_dir,
    )

    assert record_deferred_profile_cleanup(staged, record_path=record_path) is True
    assert retry_deferred_profile_cleanup(record_path=record_path) == 1
    assert staged_dir.exists() is False
    assert record_path.exists() is False


def test_unreadable_deferred_cleanup_record_is_never_overwritten(
    tmp_path: Path,
) -> None:
    record_path = tmp_path / "package_profile_cleanup.json"
    record_path.write_text("{ not json", encoding="utf-8")
    staged = StagedPackageProfile(
        original_dir=tmp_path / "profiles" / "demo_package",
        staged_dir=tmp_path / "profiles" / f".demo_package.deleting-{'a' * 32}",
    )

    assert load_deferred_profile_cleanup_paths(record_path) is None
    assert record_deferred_profile_cleanup(staged, record_path=record_path) is False
    assert retry_deferred_profile_cleanup(record_path=record_path) == 0
    assert record_path.read_text(encoding="utf-8") == "{ not json"
