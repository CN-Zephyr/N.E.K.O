"""Public runtime boundary over the package-format library.

The package-format implementation remains in ``plugin.neko_plugin_cli.core``.
This service exposes only operations needed by the running application and
keeps HTTP/CLI response shaping outside the package-management boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import os
import re
import shutil
import stat
import tomllib
import uuid

from plugin.neko_plugin_cli.core.install import PackageInstaller
from plugin.neko_plugin_cli.core.models import (
    InstalledPlugin,
    InstallResult,
    PackageInspectResult,
)
from plugin.neko_plugin_cli.public import inspect_package, install_package
from plugin.server.application.install_source import get_install_source_manager
from plugin.server.domain.errors import ServerDomainError

from .install_plan import PluginInstallPlan, build_install_plan

_RETIREMENT_COMMIT_SUFFIX = ".committed"
_RETIREMENT_COMMIT_PAYLOAD = b"NEKO-CANDIDATE-RETIREMENT-V1\n"
_RETIREMENT_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class StagedCandidateRetirement:
    """One candidate directory moved outside the scanner-visible namespace."""

    original_dir: Path
    staged_dir: Path | None
    existed: bool


class PluginPackageService:
    """Inspect, verify, and plan installation of runtime plugin packages."""

    def inspect(self, package_path: Path) -> PackageInspectResult:
        return inspect_package(package_path)

    def verify(self, package_path: Path) -> PackageInspectResult:
        # Package inspection already performs the bounded archive validation
        # and payload verification implemented by the package-format library.
        return inspect_package(package_path)

    def read_installed_plugin_id(self, target_dir: Path) -> str:
        return _read_installed_plugin_toml_id(target_dir)

    def validate_existing_profile_ownership(
        self,
        *,
        profile_dir: Path,
        profiles_root: Path,
        package_id: str,
        plugin_ids: set[str],
    ) -> None:
        _validate_existing_profile_ownership(
            profile_dir=profile_dir,
            profiles_root=profiles_root,
            package_id=package_id,
            plugin_ids=plugin_ids,
        )

    def stage_candidate_retirement(self, target_dir: Path) -> StagedCandidateRetirement:
        """Atomically hide candidate code so a failed Registry write can restore it."""

        if not target_dir.exists():
            return StagedCandidateRetirement(
                original_dir=target_dir,
                staged_dir=None,
                existed=False,
            )
        if _is_link_or_reparse(target_dir):
            raise ValueError("candidate directory must not be a link or reparse point")
        if not target_dir.is_dir():
            raise NotADirectoryError(target_dir)

        staging_root = target_dir.parent / ".delete-backups"
        if _is_link_or_reparse(staging_root):
            raise ValueError("candidate retirement staging root must not be a link")
        staging_root.mkdir(parents=True, exist_ok=True)
        staged_dir = staging_root / f"{target_dir.name}.retiring.{uuid.uuid4().hex}"
        target_dir.rename(staged_dir)
        return StagedCandidateRetirement(
            original_dir=target_dir,
            staged_dir=staged_dir,
            existed=True,
        )

    def restore_candidate_retirement(self, staged: StagedCandidateRetirement) -> None:
        """Put staged code back after the Registry retirement failed."""

        if staged.staged_dir is None or not staged.staged_dir.exists():
            return
        if staged.original_dir.exists():
            raise FileExistsError(staged.original_dir)
        staged.staged_dir.rename(staged.original_dir)
        _remove_empty_directory(staged.staged_dir.parent)

    def finalize_candidate_retirement(self, staged: StagedCandidateRetirement) -> None:
        """Permanently clean code only after the Registry retirement committed."""

        if staged.staged_dir is None:
            return
        shutil.rmtree(staged.staged_dir)
        _retirement_commit_marker(staged.staged_dir).unlink(missing_ok=True)
        _remove_empty_directory(staged.staged_dir.parent)

    def mark_candidate_retirement_committed(
        self,
        staged: StagedCandidateRetirement,
    ) -> None:
        """Persist a cleanup-only marker after durable retirement commits."""

        if staged.staged_dir is None:
            return
        marker = _retirement_commit_marker(staged.staged_dir)
        with marker.open("xb") as file_obj:
            file_obj.write(_RETIREMENT_COMMIT_PAYLOAD)
            file_obj.flush()
            os.fsync(file_obj.fileno())

    def retry_candidate_retirement_cleanup(self, roots: tuple[Path, ...]) -> int:
        """Delete only hidden backups carrying our exact post-commit marker."""

        cleaned = 0
        for root in dict.fromkeys(path.resolve(strict=False) for path in roots):
            staging_root = root / ".delete-backups"
            if not staging_root.is_dir() or _is_link_or_reparse(staging_root):
                continue
            for marker in tuple(staging_root.glob(f".*{_RETIREMENT_COMMIT_SUFFIX}")):
                staged_dir = _staged_directory_from_marker(marker)
                if (
                    staged_dir is None
                    or staged_dir.parent != staging_root
                    or _is_link_or_reparse(marker)
                    or not marker.is_file()
                ):
                    continue
                try:
                    if marker.read_bytes() != _RETIREMENT_COMMIT_PAYLOAD:
                        continue
                    if staged_dir.exists():
                        if _is_link_or_reparse(staged_dir) or not staged_dir.is_dir():
                            continue
                        shutil.rmtree(staged_dir)
                    marker.unlink()
                except OSError:
                    continue
                cleaned += 1
            _remove_empty_directory(staging_root)
        return cleaned

    def plan_install(
        self,
        *,
        package_path: Path,
        plugins_root: Path,
    ) -> PluginInstallPlan:
        return build_install_plan(
            package_path=package_path,
            plugins_root=plugins_root,
        )

    def install(
        self,
        *,
        package_path: Path,
        plugins_root: Path,
        profiles_root: Path,
        on_conflict: str,
        use_staging: bool = True,
        forced_directory_name: str | None = None,
        install_result_factory: Callable[..., InstallResult] = InstallResult,
    ) -> InstallResult:
        if use_staging:
            return self._install_via_staging(
                package_path=package_path,
                plugins_root=plugins_root,
                profiles_root=profiles_root,
                on_conflict=on_conflict,
                forced_directory_name=forced_directory_name,
                install_result_factory=install_result_factory,
            )
        if forced_directory_name is not None:
            raise ValueError("forced_directory_name requires use_staging=True")
        return install_package(
            package_path,
            plugins_root=plugins_root,
            profiles_root=profiles_root,
            on_conflict=on_conflict,
        )

    def _install_via_staging(
        self,
        *,
        package_path: Path,
        plugins_root: Path,
        profiles_root: Path,
        on_conflict: str,
        forced_directory_name: str | None = None,
        install_result_factory: Callable[..., InstallResult] = InstallResult,
    ) -> InstallResult:
        """Extract into private staging trees, then promote into managed roots."""

        forced_directory_name = (
            _require_safe_directory_name(forced_directory_name)
            if forced_directory_name is not None
            else None
        )
        staging_token = uuid.uuid4().hex
        staging_plugins = plugins_root / f".neko_staging_{staging_token}"
        staging_profiles = profiles_root / f".neko_staging_{staging_token}"
        staging_plugins.mkdir(parents=True, exist_ok=True)
        staging_profiles.mkdir(parents=True, exist_ok=True)
        installer = PackageInstaller()
        promoted_plugins: list[InstalledPlugin] = []
        promoted_profile: Path | None = None
        profile_reused = False

        try:
            staged = install_package(
                package_path,
                plugins_root=staging_plugins,
                profiles_root=staging_profiles,
                on_conflict="fail",
            )

            for item in staged.installed_plugins:
                source_dir = Path(item.target_dir)
                desired_name = forced_directory_name or item.target_plugin_id
                desired = plugins_root / desired_name
                final_dir = installer.resolve_plugin_target_dir(desired)
                if source_dir.resolve() != final_dir.resolve():
                    final_dir.parent.mkdir(parents=True, exist_ok=True)
                    source_dir.rename(final_dir)
                promoted_plugins.append(
                    InstalledPlugin(
                        source_folder=item.source_folder,
                        target_plugin_id=final_dir.name,
                        target_dir=final_dir,
                        renamed=(final_dir.name != item.source_folder),
                    )
                )
                if not (final_dir / "plugin.toml").is_file():
                    raise ValueError(
                        f"promoted plugin is missing plugin.toml: {final_dir}"
                    )

            if staged.profile_dir is not None:
                source_profile = Path(staged.profile_dir)
                desired_profile = profiles_root / source_profile.name
                if _is_link_or_reparse(desired_profile):
                    raise ValueError(
                        "existing package profile path is a link or reparse point: "
                        f"{desired_profile.name}"
                    )
                if desired_profile.exists():
                    if not desired_profile.is_dir():
                        raise ValueError(
                            "existing package profile path is not a directory: "
                            f"{desired_profile.name}"
                        )
                    _validate_existing_profile_ownership(
                        profile_dir=desired_profile,
                        profiles_root=profiles_root,
                        package_id=staged.package_id,
                        plugin_ids={
                            _read_installed_plugin_toml_id(Path(item.target_dir))
                            for item in promoted_plugins
                        },
                    )
                    promoted_profile = desired_profile.resolve()
                    profile_reused = True
                else:
                    desired_profile = installer.resolve_target_dir(
                        desired_profile,
                        on_conflict=on_conflict,
                    )
                    if source_profile.resolve() != desired_profile.resolve():
                        desired_profile.parent.mkdir(parents=True, exist_ok=True)
                        source_profile.rename(desired_profile)
                    promoted_profile = desired_profile

            return install_result_factory(
                package_path=staged.package_path,
                package_type=staged.package_type,
                package_id=staged.package_id,
                plugins_root=plugins_root,
                profiles_root=profiles_root,
                installed_plugins=promoted_plugins,
                profile_dir=promoted_profile,
                profile_reused=profile_reused,
                metadata_found=staged.metadata_found,
                payload_hash=staged.payload_hash,
                payload_hash_verified=staged.payload_hash_verified,
                conflict_strategy=on_conflict,
            )
        except Exception:
            for item in promoted_plugins:
                shutil.rmtree(item.target_dir, ignore_errors=True)
            if promoted_profile is not None and not profile_reused:
                shutil.rmtree(promoted_profile, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(staging_plugins, ignore_errors=True)
            shutil.rmtree(staging_profiles, ignore_errors=True)


def _require_safe_directory_name(value: str) -> str:
    directory_name = value.strip()
    if (
        not directory_name
        or directory_name in {".", ".."}
        or "/" in directory_name
        or "\\" in directory_name
    ):
        raise ValueError(
            "forced_directory_name must be a safe plugin directory name, "
            f"got {value!r}"
        )
    return directory_name


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(file_attributes & reparse_attribute)


def _remove_empty_directory(path: Path) -> None:
    try:
        path.rmdir()
    except (FileNotFoundError, OSError):
        pass


def _retirement_commit_marker(staged_dir: Path) -> Path:
    return staged_dir.with_name(
        f".{staged_dir.name}{_RETIREMENT_COMMIT_SUFFIX}"
    )


def _staged_directory_from_marker(marker: Path) -> Path | None:
    name = marker.name
    if not name.startswith(".") or not name.endswith(_RETIREMENT_COMMIT_SUFFIX):
        return None
    staged_name = name[1 : -len(_RETIREMENT_COMMIT_SUFFIX)]
    prefix, separator, token = staged_name.rpartition(".retiring.")
    if (
        not separator
        or not prefix
        or prefix in {".", ".."}
        or _RETIREMENT_TOKEN_PATTERN.fullmatch(token) is None
    ):
        return None
    return marker.with_name(staged_name)


def _read_installed_plugin_toml_id(target_dir: Path) -> str:
    plugin_toml = target_dir / "plugin.toml"
    try:
        data = tomllib.loads(plugin_toml.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"installed plugin.toml not found: {plugin_toml}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"installed plugin.toml is invalid TOML: {plugin_toml}") from exc

    plugin_table = data.get("plugin")
    if not isinstance(plugin_table, dict):
        raise ValueError(f"installed plugin.toml missing [plugin] table: {plugin_toml}")
    plugin_id = plugin_table.get("id")
    if not isinstance(plugin_id, str) or not plugin_id.strip():
        raise ValueError(f"installed plugin.toml missing [plugin].id: {plugin_toml}")
    return plugin_id.strip()


def _validate_existing_profile_ownership(
    *,
    profile_dir: Path,
    profiles_root: Path,
    package_id: str,
    plugin_ids: set[str],
) -> None:
    manager = get_install_source_manager()
    if manager is None:
        raise ServerDomainError(
            code="INSTALL_SOURCE_NOT_READY",
            message="install source manager is not initialised",
            status_code=503,
            details={"hint": "wait for FastAPI lifespan startup to complete"},
        )
    owners = []
    resolved_profile = profile_dir.resolve(strict=False)
    for entry in manager.list_entries(include_removed=True):
        if entry.profile_installed is not True:
            continue
        recorded_key = entry.package_id
        if entry.profile_dir:
            recorded_profile = Path(entry.profile_dir).expanduser()
        elif recorded_key:
            recorded_profile = profiles_root / recorded_key
        else:
            continue
        if recorded_profile.resolve(strict=False) == resolved_profile:
            owners.append(entry)

    if bool(owners) and all(
        owner.plugin_id in plugin_ids and owner.package_id == package_id
        for owner in owners
    ):
        return
    raise ServerDomainError(
        code="PLUGIN_PACKAGE_PROFILE_OWNERSHIP_CONFLICT",
        message="existing package profile ownership does not match the incoming package",
        status_code=409,
        details={
            "package_id": package_id,
            "plugin_ids": sorted(plugin_ids),
            "recorded_plugin_ids": sorted(
                {owner.plugin_id for owner in owners if owner.plugin_id}
            ),
        },
    )


__all__ = ["PluginPackageService", "StagedCandidateRetirement"]
