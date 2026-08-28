from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
import hashlib
import zipfile
from pathlib import Path
from typing import Any, BinaryIO, Literal

from plugin.logging_config import get_logger
from plugin.neko_plugin_cli.core.models import InstallResult
from plugin.neko_plugin_cli.public import (
    analyze_bundle_plugins,
    build_bundle,
    build_plugin,
)
from plugin.server.application.install_source import (
    InstallSourceError,
    InstallSourceManager,
    classify_plugin_path,
    get_install_source_manager,
)
from plugin.server.application.plugin_cli.paths import PluginCliPathPolicy
from plugin.server.application.package_management.install_plan import (
    REPLACEMENT_ACTIONS,
    PluginInstallPlan,
)
from plugin.server.application.package_management.package_service import (
    PluginPackageService,
)
from plugin.server.application.package_management.coordinator import (
    InstallationCoordinator,
    LocalImportRequest,
    LocalReplacementRequest,
    MarketFreshInstallRequest,
    MarketReplacementInstallRequest,
    install_result_entries,
    install_result_profile_dirs,
    install_result_target_dirs,
    single_install_target,
)
from plugin.server.application.package_management.artifacts import PackageArtifactStore
from plugin.server.application.package_management.plugin_cli_adapter import (
    PluginCliInstallationAdapter,
)
from plugin.server.application.package_management.replacement import ReplacePluginError
from plugin.server.application.plugins.operation_lock import serialized_plugin_operation
from plugin.server.application.plugin_cli.source_resolver import (
    PluginSourceResolver,
    ResolvedPluginSource,
)
from plugin.server.domain.plugin_candidates import CandidateKey
from plugin.server.domain.errors import ServerDomainError
from plugin.settings import (
    BUILTIN_PLUGIN_CONFIG_ROOT,
    USER_PACKAGE_PROFILES_ROOT,
    USER_PLUGIN_CONFIG_ROOT,
    USER_PLUGIN_PACKAGES_ROOT,
)

_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
# Deprecated compatibility anchors. Package-management code below resolves
# roots through PluginCliPathPolicy.from_settings() for each operation.
_RUNTIME_PLUGINS_ROOT = BUILTIN_PLUGIN_CONFIG_ROOT
_INSTALL_PLUGINS_ROOT = USER_PLUGIN_CONFIG_ROOT
_INSTALL_PROFILES_ROOT = USER_PACKAGE_PROFILES_ROOT
_TARGET_ROOT = USER_PLUGIN_PACKAGES_ROOT

# Allowed extensions for uploaded plugin packages
_ALLOWED_UPLOAD_SUFFIXES = frozenset({".neko-plugin", ".neko-bundle"})
# Maximum upload size (500 MiB)
_UPLOAD_MAX_BYTES = 500 * 1024 * 1024
_UPLOAD_COPY_CHUNK_BYTES = 1024 * 1024

logger = get_logger("server.application.plugin_cli")

_PACKAGE_ERROR_PATTERNS = (
    (
        "PLUGIN_PACKAGE_NESTED_ROOT",
        (("extra parent folder",), ("manifest.toml is nested",)),
    ),
    (
        "PLUGIN_PACKAGE_MANIFEST_MISSING",
        (
            ("required file 'manifest.toml' not found",),
            ("package manifest.toml is missing",),
        ),
    ),
    (
        "PLUGIN_PACKAGE_PLUGIN_MANIFEST_MISSING",
        (("missing the required 'plugin.toml'",),),
    ),
    (
        "PLUGIN_PACKAGE_PLUGIN_MANIFEST_INVALID",
        (("plugin.toml", "invalid toml"),),
    ),
    (
        "PLUGIN_PACKAGE_IDENTITY_MISMATCH",
        (("does not match plugin.toml id",), ("plugin identity mismatch",)),
    ),
    (
        "PLUGIN_PACKAGE_HASH_MISMATCH",
        (("payload hash mismatch",), ("content verification hash",)),
    ),
)


def _validate_existing_profile_ownership(
    *,
    profile_dir: Path,
    profiles_root: Path,
    package_id: str,
    plugin_ids: set[str],
) -> None:
    """Compatibility facade for the extracted package ownership guard."""

    PluginPackageService().validate_existing_profile_ownership(
        profile_dir=profile_dir,
        profiles_root=profiles_root,
        package_id=package_id,
        plugin_ids=plugin_ids,
    )


def _classify_package_error(exc: Exception) -> str | None:
    if isinstance(exc, ServerDomainError) and exc.code.startswith("PLUGIN_PACKAGE_"):
        return exc.code
    if isinstance(exc, zipfile.BadZipFile):
        return "PLUGIN_PACKAGE_INVALID_ARCHIVE"

    message = str(exc).lower()
    for code, alternatives in _PACKAGE_ERROR_PATTERNS:
        if any(
            all(fragment in message for fragment in fragments)
            for fragments in alternatives
        ):
            return code
    if any(
        fragment in message
        for fragment in (
            "too many entries",
            "package archive expands to",
            "single-member limit",
            "compression ratio",
            "-byte read limit",
            "equivalent on common filesystems",
            "file/directory path conflict",
        )
    ):
        return "PLUGIN_PACKAGE_INVALID_ARCHIVE"
    return None


def _replacement_error_details(
    exc: ReplacePluginError,
) -> dict[str, object]:
    details: dict[str, object] = {
        "stage": exc.stage,
        "rollback_status": exc.rollback_status,
    }
    cause_code = _classify_package_error(exc.cause)
    if cause_code:
        details["cause_code"] = cause_code
    return details


def _require_within(path: Path, root: Path, *, field: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{field} must be inside {root}") from exc
    return resolved


def _require_safe_directory_name(value: str, *, field: str) -> str:
    directory_name = value.strip()
    if (
        not directory_name
        or directory_name in {".", ".."}
        or "/" in directory_name
        or "\\" in directory_name
    ):
        raise ValueError(f"{field} must be a safe plugin directory name, got {value!r}")
    return directory_name


class PluginCliService:
    async def list_local_plugins(self) -> dict[str, object]:
        return await asyncio.to_thread(self._list_local_plugins_sync)

    async def list_local_packages(self) -> dict[str, object]:
        return await asyncio.to_thread(self._list_local_packages_sync)

    async def build(
        self,
        *,
        mode: str = "selected",
        plugin: str | None = None,
        plugins: list[str] | None = None,
        plugin_ref: dict[str, Any] | None = None,
        plugin_refs: list[dict[str, Any]] | None = None,
        out: str | None = None,
        target_dir: str | None = None,
        keep_staging: bool = False,
        bundle_id: str | None = None,
        package_name: str | None = None,
        package_description: str | None = None,
        version: str | None = None,
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            self._build_sync,
            mode=mode,
            plugin=plugin,
            plugins=plugins,
            plugin_ref=plugin_ref,
            plugin_refs=plugin_refs,
            out=out,
            target_dir=target_dir,
            keep_staging=keep_staging,
            bundle_id=bundle_id,
            package_name=package_name,
            package_description=package_description,
            version=version,
        )

    async def inspect(self, *, package: str) -> dict[str, object]:
        return await asyncio.to_thread(self._inspect_sync, package=package)

    async def verify(self, *, package: str) -> dict[str, object]:
        return await asyncio.to_thread(self._verify_sync, package=package)

    async def plan_install(
        self,
        *,
        package: str,
        plugins_root: str | None = None,
        profiles_root: str | None = None,
        _allow_external_profiles_root: bool = False,
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            self._plan_install_sync,
            package=package,
            plugins_root=plugins_root,
            profiles_root=profiles_root,
            _allow_external_profiles_root=_allow_external_profiles_root,
        )

    @serialized_plugin_operation
    async def install(
        self,
        *,
        package: str,
        plugins_root: str | None = None,
        profiles_root: str | None = None,
        on_conflict: str = "fail",
        use_staging: bool = True,
        forced_directory_name: str | None = None,
        install_source: Literal["imported"] | None = None,
        confirm_upgrade: bool = False,
        confirmation_token: str | None = None,
        _allow_external_profiles_root: bool = False,
    ) -> dict[str, object]:
        plan_dict = await self.plan_install(
            package=package,
            plugins_root=plugins_root,
            profiles_root=profiles_root,
            _allow_external_profiles_root=_allow_external_profiles_root,
        )
        action = str(plan_dict["action"])
        if action == "blocked":
            raise ServerDomainError(
                code="PLUGIN_INSTALL_BLOCKED",
                message="plugin package cannot be installed safely",
                status_code=409,
                details=plan_dict,
            )
        if action == "install":
            result = await asyncio.to_thread(
                self._install_sync,
                package=package,
                plugins_root=plugins_root,
                profiles_root=profiles_root,
                on_conflict=on_conflict,
                use_staging=use_staging,
                forced_directory_name=forced_directory_name,
                _allow_external_profiles_root=_allow_external_profiles_root,
            )
            return await self._record_requested_install_source(
                install_result=result,
                package=package,
                source=install_source,
            )

        if not confirm_upgrade or not confirmation_token:
            raise ServerDomainError(
                code="PLUGIN_UPGRADE_CONFIRMATION_REQUIRED",
                message="plugin replacement requires explicit confirmation",
                status_code=409,
                details=plan_dict,
            )
        if confirmation_token != str(plan_dict["confirmation_token"]):
            raise ServerDomainError(
                code="PLUGIN_UPGRADE_PLAN_CHANGED",
                message="installed plugin changed after replacement confirmation",
                status_code=409,
                details=plan_dict,
            )

        policy = self._path_policy()
        target_root = (
            _require_within(
                Path(plugins_root).expanduser().resolve(),
                policy.user_plugins_root,
                field="plugins_root",
            )
            if plugins_root
            else policy.user_plugins_root
        )
        directory_name = _require_safe_directory_name(
            str(plan_dict["directory_name"]),
            field="directory_name",
        )
        target_dir = target_root / directory_name
        profiles_root_path = (
            Path(profiles_root).expanduser().resolve()
            if profiles_root and _allow_external_profiles_root
            else (
                _require_within(
                    Path(profiles_root).expanduser().resolve(),
                    policy.package_profiles_root,
                    field="profiles_root",
                )
                if profiles_root
                else policy.package_profiles_root
            )
        )
        _require_safe_directory_name(
            str(plan_dict["package_id"]),
            field="package_id",
        )
        installed_package_id = _require_safe_directory_name(
            str(plan_dict["installed_package_id"] or plan_dict["package_id"]),
            field="installed_package_id",
        )
        profile_dir = profiles_root_path / installed_package_id
        plan = self._apply_installed_package_identity(
            self._package_service().plan_install(
                package_path=self._resolve_package_path(package),
                plugins_root=target_root,
            ),
            target_root=target_root,
            profiles_root=profiles_root_path,
        )
        if (
            plan.action not in REPLACEMENT_ACTIONS
            or plan.confirmation_token != confirmation_token
        ):
            raise ServerDomainError(
                code="PLUGIN_UPGRADE_PLAN_CHANGED",
                message="installed plugin changed after replacement confirmation",
                status_code=409,
                details=asdict(plan),
            )

        port = PluginCliInstallationAdapter(self)
        try:
            result = await InstallationCoordinator(
                local_replacement=port
            ).replace_local(
                LocalReplacementRequest(
                    plugin_id=plan.plugin_id,
                    directory_name=plan.directory_name,
                    target_dir=target_dir,
                    profile_dir=profile_dir,
                    manifestless_state=plan.manifestless_state,
                    package=package,
                    plugins_root=plugins_root,
                    profiles_root=profiles_root,
                    use_staging=use_staging,
                    forced_directory_name=forced_directory_name,
                    allow_external_profiles_root=_allow_external_profiles_root,
                )
            )
        except ReplacePluginError as exc:
            raise ServerDomainError(
                code="PLUGIN_UPGRADE_ROLLED_BACK",
                message="plugin upgrade failed and rollback was attempted",
                status_code=500,
                details=_replacement_error_details(exc),
            ) from exc

        response = {
            **result.install_result,
            # Compatibility response for the existing Package Manager UI.
            # The shared file transaction itself is version-agnostic replace.
            "operation": plan.action,
            "restarted": result.restarted,
            "rollback_status": result.rollback_status,
        }
        return await self._record_requested_install_source(
            install_result=response,
            package=package,
            source=install_source,
        )

    async def _record_requested_install_source(
        self,
        *,
        install_result: dict[str, object],
        package: str,
        source: Literal["imported"] | None,
    ) -> dict[str, object]:
        if source is None:
            return install_result

        try:
            package_path = self._resolve_package_path(package)
            package_sha256 = await asyncio.to_thread(self._sha256_file, package_path)
        except Exception as exc:
            logger.warning(
                "prepare install source failed: err_type={}, err={}",
                type(exc).__name__,
                str(exc),
            )
            return {
                **install_result,
                "install_source_warning": f"install_source_prepare_failed: {exc}",
            }
        warning = await self._record_install_source_best_effort(
            install_result=install_result,
            package_filename=package_path.name,
            package_sha256=package_sha256,
            override=None,
        )
        if warning is None:
            return install_result
        return {**install_result, "install_source_warning": warning}

    async def analyze(
        self,
        *,
        plugins: list[str],
        plugin_refs: list[dict[str, Any]] | None = None,
        current_sdk_version: str | None = None,
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            self._analyze_sync,
            plugins=plugins,
            plugin_refs=plugin_refs,
            current_sdk_version=current_sdk_version,
        )

    # ── Upload & Download ──────────────────────────────────────────────

    async def save_uploaded_package(self, *, filename: str, content: bytes) -> dict[str, object]:
        """Save an uploaded package file to the target directory.

        Returns metadata about the saved file including its server-side path,
        which can be passed to ``install`` or ``inspect``.
        """
        return await asyncio.to_thread(self._save_uploaded_package_sync, filename=filename, content=content)

    async def save_uploaded_file(self, *, filename: str, source_file: BinaryIO) -> dict[str, object]:
        """Stream an uploaded package into the managed artifacts directory."""
        return await asyncio.to_thread(
            self._save_uploaded_file_sync,
            filename=filename,
            source_file=source_file,
        )

    @serialized_plugin_operation
    async def discard_uploaded_package(self, *, package: str) -> dict[str, object]:
        """Remove one upload owned by an abandoned Plugin Center workflow."""
        return await asyncio.to_thread(self._discard_uploaded_package_sync, package=package)

    @serialized_plugin_operation
    async def upload_and_install(
        self,
        *,
        filename: str,
        content: bytes | None = None,
        package_path: str | None = None,
        profiles_root: str | None = None,
        _allow_external_profiles_root: bool = False,
        on_conflict: str = "fail",
        install_source_override: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        """Upload, unpack, and atomically record the install source (design §3.3).

        ``install_source_override`` lets the caller pin the lock entry to
        ``channel="market"`` and mode (``install`` / ``upgrade`` / ``reinstall``)
        in a single call. When ``None`` this method is exactly equivalent to
        :meth:`upload_and_unpack` (no lock write).

        ``install_source_override`` schema (design §3.3.1):

        ```
        {
            "channel": "market",
            "mode": "install" | "upgrade" | "reinstall",
            "market_detail": {
                "plugin_market_id": str,
                "version": str,
                "package_url": str,
                "channel": str,            # "stable" | "beta"
                "package_sha256": str,     # 64-hex from caller; we re-verify
                "payload_hash": str | None,
                "published_at": str,       # ISO 8601
            },
        }
        ```

        Returns a dict with ``upload`` / ``unpack`` / ``install`` keys; the
        ``install`` dict mirrors :class:`SourceDetailMarket` fields. When
        warnings accrue (e.g. mismatched sha256, missing market_detail
        keys, fall back to imported channel) they are joined into an
        ``install_source_warning`` string in the return value (Req 3.4 / R10.5).

        Failure semantics (Req 3.6 / design §10.1):

        * Any exception from the save / unpack / record steps cleans up
          the saved package file and the unpacked directory before
          re-raising. The lock is never left with a half-written entry.
        * ``record_market_*`` raising :class:`InstallSourceError` with
          ``code="lock_write_failed"`` propagates verbatim so the caller
          (Bridge ``_execute_install``) can map it to the right user-facing
          error code.
        """

        if content is None and package_path is None:
            raise ValueError("upload_and_install requires content or package_path")
        if content is not None and package_path is not None:
            raise ValueError("upload_and_install accepts content or package_path, not both")

        if install_source_override is None:
            port = PluginCliInstallationAdapter(self)
            result = await InstallationCoordinator(
                port
            ).install_local(
                LocalImportRequest(
                    filename=filename,
                    content=content,
                    package_path=package_path,
                    profiles_root=profiles_root,
                    allow_external_profiles_root=_allow_external_profiles_root,
                    on_conflict=on_conflict,
                )
            )
            return result.to_api_dict()

        channel = install_source_override.get("channel")
        if channel != "market":
            raise ValueError(
                f"unsupported install_source_override channel: {channel!r}"
            )

        install_mode = install_source_override.get("mode") or "install"
        if install_mode == "install":
            market_detail_raw = install_source_override.get("market_detail") or {}
            forced_directory_name_raw = install_source_override.get("directory_name")
            return await self.install_market_fresh(
                MarketFreshInstallRequest(
                    filename=filename,
                    content=content,
                    package_path=package_path,
                    profiles_root=profiles_root,
                    allow_external_profiles_root=_allow_external_profiles_root,
                    on_conflict=on_conflict,
                    forced_directory_name=(
                        forced_directory_name_raw
                        if isinstance(forced_directory_name_raw, str)
                        else None
                    ),
                    market_detail=dict(market_detail_raw),
                )
            )
        if install_mode not in {"upgrade", "reinstall"}:
            raise ValueError(f"unsupported Market install mode: {install_mode!r}")
        market_detail_raw = install_source_override.get("market_detail") or {}
        forced_directory_name_raw = install_source_override.get("directory_name")
        return await self.install_market_replacement(
            MarketReplacementInstallRequest(
                filename=filename,
                mode=install_mode,
                content=content,
                package_path=package_path,
                profiles_root=profiles_root,
                allow_external_profiles_root=_allow_external_profiles_root,
                on_conflict=on_conflict,
                forced_directory_name=(
                    forced_directory_name_raw
                    if isinstance(forced_directory_name_raw, str)
                    else None
                ),
                market_detail=dict(market_detail_raw),
            )
        )

    async def install_market_replacement(
        self,
        request: MarketReplacementInstallRequest,
    ) -> dict[str, object]:
        """Deploy one prepared Market replacement through the coordinator."""

        port = PluginCliInstallationAdapter(self)
        result = await InstallationCoordinator(
            market_install=port
        ).install_market_replacement(request)
        return result.to_api_dict()

    async def install_market_fresh(
        self,
        request: MarketFreshInstallRequest,
    ) -> dict[str, object]:
        """Deploy one prepared fresh Market package through the coordinator."""

        port = PluginCliInstallationAdapter(self)
        result = await InstallationCoordinator(
            market_install=port
        ).install_market_fresh(request)
        return result.to_api_dict()

    @staticmethod
    def _extract_unpack_entries(unpack_result: dict[str, object]) -> list[dict[str, object]]:
        """Compatibility facade for the package-management result parser."""

        return install_result_entries(unpack_result)

    @staticmethod
    def _extract_unpack_target_dirs(unpack_result: dict[str, object]) -> list[Path]:
        """Return every target dir created by the unpack operation."""

        return install_result_target_dirs(unpack_result)

    @staticmethod
    def _extract_unpack_profile_dirs(unpack_result: dict[str, object]) -> list[Path]:
        """Return promoted profile dirs created by the unpack operation."""

        return install_result_profile_dirs(unpack_result)

    @staticmethod
    def _extract_unpack_target(
        unpack_result: dict[str, object],
    ) -> tuple[Path, str]:
        """Pull the single Market plugin's target dir + plugin id from a dump.

        The CLI returns potentially many ``unpacked_plugins`` for bundles,
        but Market install-source metadata and rollback are single-plugin
        flows. Reject multi-plugin Market packages before recording any lock
        entry so extra unpacked plugins cannot become untracked installs.
        """

        return single_install_target(unpack_result)

    @staticmethod
    def _read_installed_plugin_toml_id(target_dir: Path) -> str:
        return PluginPackageService().read_installed_plugin_id(target_dir)

    async def _activate_fresh_install_candidate(
        self,
        *,
        plugin_id: str,
        target_dir: Path,
    ) -> bool:
        """Activate a fresh Market candidate through the lifecycle transaction."""

        manager = self._require_install_source_manager()
        root_id, directory_name = classify_plugin_path(
            target_dir,
            builtin_root=manager.builtin_root,
            user_root=manager.user_root,
        )
        from plugin.server.application.plugins.lifecycle_service import (
            PluginLifecycleService,
        )
        from plugin.server.application.plugins.registry_service import (
            PluginRegistryService,
        )

        candidate_state = await PluginRegistryService().list_plugin_candidates(
            plugin_id
        )
        candidates = candidate_state.get("candidates")

        target_key = CandidateKey(root_id=root_id, directory_name=directory_name)
        target_item = next(
            (
                item
                for item in candidates
                if isinstance(item, dict)
                and item.get("key")
                == {
                    "root_id": target_key.root_id,
                    "directory_name": target_key.directory_name,
                }
            ),
            None,
        ) if isinstance(candidates, list) else None
        if (
            isinstance(target_item, dict)
            and target_item.get("requires_shared_state_authorization") is True
        ):
            # Keep the installed candidate, but do not silently grant it the
            # existing logical plugin's config/data/cache.  The candidates UI
            # performs the explicit, audited selection transaction.
            return False

        registered_candidate = candidate_state.get("registered_candidate")
        if (
            isinstance(candidates, list)
            and len(candidates) == 1
            and registered_candidate is None
        ):
            from plugin.server.infrastructure import plugin_selections

            state_owner = await asyncio.to_thread(
                plugin_selections.get_plugin_state_owner,
                plugin_id,
            )
            shared_state_exists = await asyncio.to_thread(
                plugin_selections.legacy_shared_state_exists,
                plugin_id,
            )
            if state_owner is None and not shared_state_exists:
                source = target_item.get("source") if isinstance(target_item, dict) else None
                release_chain_id = (
                    target_item.get("release_chain_id")
                    if isinstance(target_item, dict)
                    else None
                )
                if source in {"manual", "imported", "market"} and (
                    source != "market"
                    or isinstance(release_chain_id, str)
                    and bool(release_chain_id)
                ):
                    # The installer has proven that this logical id had no
                    # prior state before committing its first ownership receipt.
                    # No plugin import/start is needed merely to establish that
                    # durable boundary.
                    await asyncio.to_thread(
                        plugin_selections.set_plugin_selection,
                        plugin_id,
                        target_key,
                        candidate_source=source,
                        state_access_grant="initial_identity",
                        release_chain_id=(
                            release_chain_id if isinstance(release_chain_id, str) else None
                        ),
                    )
                    return True

        await PluginLifecycleService().switch_plugin_candidate(
            plugin_id,
            target_key,
        )
        return True

    async def _record_imported_for_unpack(
        self,
        *,
        target_dir: Path,
        saved_filename: str,
        actual_sha256: str,
        package_id: str,
        profile_dir: str,
    ) -> dict[str, Any]:
        """Fall back to recording the install as ``channel="imported"``.

        Used when ``market_detail`` lacks the required keys; the user
        still gets a working plugin and we still record source-truth, just
        without the Market-side evidence.
        """

        mgr = self._require_install_source_manager()

        def _record() -> None:
            mgr.record_import(
                directory_path=target_dir,
                package_filename=saved_filename,
                package_sha256=actual_sha256,
                package_id=package_id,
                profile_dir=profile_dir,
            )

        await asyncio.to_thread(_record)
        # Build a minimal install_dict mirroring the imported entry shape
        # (no version / channel for imported channel by design).
        return {
            "channel": "imported",
            "directory_name": target_dir.name,
            "plugin_id": target_dir.name,
            "package_filename": saved_filename,
            "package_sha256": actual_sha256,
        }

    @staticmethod
    def _rollback_install_source_best_effort(target_dir: Path) -> None:
        manager = get_install_source_manager()
        if manager is None:
            return
        try:
            manager.mark_removed(
                directory_path=target_dir,
                reason="install_rollback",
            )
        except Exception as exc:
            logger.warning(
                "upload_and_install: failed to roll back install source for {}: {}",
                target_dir,
                exc,
            )

    @staticmethod
    def _require_install_source_manager() -> InstallSourceManager:
        """Resolve the global manager or raise a clear configuration error.

        The manager is published by ``StartupReconciler`` during FastAPI
        lifespan startup; if a caller hits the market install path before
        that has run we want a meaningful error rather than ``AttributeError``
        on ``None.record_market_install``.
        """

        mgr = get_install_source_manager()
        if mgr is None:
            raise ServerDomainError(
                code="INSTALL_SOURCE_NOT_READY",
                message="install source manager is not initialised",
                status_code=503,
                details={"hint": "wait for FastAPI lifespan startup to complete"},
            )
        return mgr

    def resolve_download_path(self, package: str) -> Path:
        """Resolve and validate a package path for download.

        Returns the absolute path to the package file.  Raises if the file
        does not exist or is outside the target directory.
        """
        try:
            return self._resolve_package_path(package)
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="download") from exc

    # ── Sync helpers ───────────────────────────────────────────────────

    @staticmethod
    def _path_policy() -> PluginCliPathPolicy:
        return PluginCliPathPolicy.from_settings()

    @staticmethod
    def _package_service() -> PluginPackageService:
        return PluginPackageService()

    def _artifact_store(self) -> PackageArtifactStore:
        return PackageArtifactStore(
            self._path_policy().package_artifacts_root,
            allowed_suffixes=_ALLOWED_UPLOAD_SUFFIXES,
            max_bytes=_UPLOAD_MAX_BYTES,
            copy_chunk_bytes=_UPLOAD_COPY_CHUNK_BYTES,
        )

    def _resolver(self) -> PluginSourceResolver:
        return PluginSourceResolver(self._path_policy())

    def _list_local_plugins_sync(self) -> dict[str, object]:
        try:
            sources = self._resolver().list_plugins()
            plugins = [source.directory_name for source in sources]
            plugin_refs = [
                {
                    "root_id": source.root_id,
                    "directory_name": source.directory_name,
                    "plugin_id": source.plugin_id,
                    "label": (
                        f"{source.plugin_id} ({source.root_id}/{source.directory_name})"
                        if source.plugin_id and source.plugin_id != source.directory_name
                        else f"{source.root_id}/{source.directory_name}"
                    ),
                }
                for source in sources
            ]
            return {"plugins": plugins, "plugin_refs": plugin_refs, "count": len(sources)}
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="list_plugins") from exc

    def _list_local_packages_sync(self) -> dict[str, object]:
        try:
            result = self._artifact_store().list()
            for item in result["packages"]:
                if isinstance(item, dict):
                    item["suffix"] = Path(str(item["name"])).suffix
            return result
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="list_packages") from exc

    def _build_sync(
        self,
        *,
        mode: str,
        plugin: str | None,
        plugins: list[str] | None,
        plugin_ref: dict[str, Any] | None,
        plugin_refs: list[dict[str, Any]] | None,
        out: str | None,
        target_dir: str | None,
        keep_staging: bool,
        bundle_id: str | None,
        package_name: str | None,
        package_description: str | None,
        version: str | None,
    ) -> dict[str, object]:
        try:
            policy = self._path_policy()
            target_root = policy.package_artifacts_root
            sources = self._resolve_plugin_sources(
                mode=mode,
                plugin=plugin,
                plugins=plugins or [],
                plugin_ref=plugin_ref,
                plugin_refs=plugin_refs or [],
            )
            plugin_dirs = [source.plugin_dir for source in sources]
            resolved_target_dir = Path(target_dir).expanduser().resolve() if target_dir else target_root
            _require_within(resolved_target_dir, target_root, field="target_dir")
            resolved_target_dir.mkdir(parents=True, exist_ok=True)

            if out and mode != "bundle" and len(plugin_dirs) != 1:
                raise ValueError("'out' can only be used when building a single plugin")

            if mode == "bundle":
                resolved_bundle_id = bundle_id or "__".join(sorted(item.directory_name for item in sources))
                output_path = (
                    _require_within(Path(out).expanduser().resolve(), target_root, field="out")
                    if out
                    else _require_within(
                        (resolved_target_dir / f"{resolved_bundle_id}.neko-bundle").resolve(),
                        target_root,
                        field="out",
                    )
                )
                result = build_bundle(
                    plugin_dirs,
                    output_path,
                    bundle_id=resolved_bundle_id,
                    package_name=package_name,
                    package_description=package_description,
                    version=version or "0.1.0",
                    keep_staging=keep_staging,
                )
                built = [result.model_dump(mode="json")]
                return {
                    "built": built,
                    "built_count": len(built),
                    "failed": [],
                    "failed_count": 0,
                    "ok": True,
                }

            built: list[dict[str, object]] = []
            failed: list[dict[str, object]] = []
            output_stems = self._output_stems_for_sources(sources)
            for source, plugin_dir in zip(sources, plugin_dirs, strict=True):
                output_path = (
                    _require_within(Path(out).expanduser().resolve(), target_root, field="out")
                    if out
                    else resolved_target_dir / f"{output_stems[source]}.neko-plugin"
                )
                try:
                    result = build_plugin(
                        plugin_dir,
                        output_path,
                        keep_staging=keep_staging,
                    )
                    built.append(result.model_dump(mode="json"))
                except Exception as exc:
                    failed.append({"plugin": f"{source.root_id}/{source.directory_name}", "error": str(exc)})

            return {
                "built": built,
                "built_count": len(built),
                "failed": failed,
                "failed_count": len(failed),
                "ok": not failed,
            }
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="build") from exc

    def _inspect_sync(self, *, package: str) -> dict[str, object]:
        try:
            result = self._package_service().inspect(
                self._resolve_package_path(package)
            )
            return result.model_dump(mode="json")
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="inspect") from exc

    def _verify_sync(self, *, package: str) -> dict[str, object]:
        try:
            result = self._package_service().verify(
                self._resolve_package_path(package)
            )
            payload_hash_verified = result.payload_hash_verified
            return {
                **result.model_dump(mode="json"),
                "ok": payload_hash_verified is True,
            }
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="verify") from exc

    def _plan_install_sync(
        self,
        *,
        package: str,
        plugins_root: str | None,
        profiles_root: str | None,
        _allow_external_profiles_root: bool = False,
    ) -> dict[str, object]:
        try:
            policy = self._path_policy()
            target_root = (
                _require_within(
                    Path(plugins_root).expanduser().resolve(),
                    policy.user_plugins_root,
                    field="plugins_root",
                )
                if plugins_root
                else policy.user_plugins_root
            )
            profiles_root_path = (
                Path(profiles_root).expanduser().resolve()
                if profiles_root and _allow_external_profiles_root
                else (
                    _require_within(
                        Path(profiles_root).expanduser().resolve(),
                        policy.package_profiles_root,
                        field="profiles_root",
                    )
                    if profiles_root
                    else policy.package_profiles_root
                )
            )
            plan = self._apply_installed_package_identity(
                self._package_service().plan_install(
                    package_path=self._resolve_package_path(package),
                    plugins_root=target_root,
                ),
                target_root=target_root,
                profiles_root=profiles_root_path,
            )
            return asdict(plan)
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="install-plan") from exc

    def _apply_installed_package_identity(
        self,
        plan: PluginInstallPlan,
        *,
        target_root: Path,
        profiles_root: Path,
    ) -> PluginInstallPlan:
        if plan.action not in REPLACEMENT_ACTIONS:
            return plan

        target_dir = target_root / plan.directory_name
        manager = get_install_source_manager()
        installed_package_id = (
            manager.package_id_for_directory(target_dir) if manager is not None else ""
        )
        if not installed_package_id:
            # Legacy rows predate package identity tracking. Directory
            # existence cannot prove ownership because stale or unrelated
            # profile trees may share the incoming name. Historical official
            # single-plugin packages used plugin_id as package_id, so use that
            # conservative baseline and fail closed on any ambiguous rename.
            installed_package_id = plan.plugin_id
        if installed_package_id != plan.package_id:
            return replace(
                plan,
                action="blocked",
                confirmation_token="",
                reason="package_id_change",
                installed_package_id=installed_package_id,
            )
        return replace(plan, installed_package_id=installed_package_id)

    def _install_sync(
        self,
        *,
        package: str,
        plugins_root: str | None,
        profiles_root: str | None,
        on_conflict: str,
        use_staging: bool = True,
        forced_directory_name: str | None = None,
        _allow_external_profiles_root: bool = False,
    ) -> dict[str, object]:
        try:
            policy = self._path_policy()
            install_plugins_root = policy.user_plugins_root
            install_profiles_root = policy.package_profiles_root
            plugins_root_path = (
                _require_within(Path(plugins_root).expanduser().resolve(), install_plugins_root, field="plugins_root")
                if plugins_root
                else install_plugins_root
            )
            profiles_root_path = (
                Path(profiles_root).expanduser().resolve()
                if profiles_root and _allow_external_profiles_root
                else (
                    _require_within(
                        Path(profiles_root).expanduser().resolve(),
                        install_profiles_root,
                        field="profiles_root",
                    )
                    if profiles_root
                    else install_profiles_root
                )
            )
            package_path = self._resolve_package_path(package)
            result = self._package_service().install(
                package_path=package_path,
                plugins_root=plugins_root_path,
                profiles_root=profiles_root_path,
                on_conflict=on_conflict,
                use_staging=use_staging,
                forced_directory_name=forced_directory_name,
                install_result_factory=InstallResult,
            )
            return result.model_dump(mode="json")
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="install") from exc

    def _install_via_staging_sync(
        self,
        *,
        package: Path,
        plugins_root: Path,
        profiles_root: Path,
        on_conflict: str,
        forced_directory_name: str | None = None,
    ) -> InstallResult:
        """Compatibility facade for the extracted package staging service."""

        return self._package_service().install(
            package_path=package,
            plugins_root=plugins_root,
            profiles_root=profiles_root,
            on_conflict=on_conflict,
            use_staging=True,
            forced_directory_name=forced_directory_name,
            install_result_factory=InstallResult,
        )

    @staticmethod
    def _sha256_file(path: str | Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest().lower()

    @staticmethod
    def _package_ref_from_path(*, filename: str, package_path: str) -> dict[str, object]:
        resolved = Path(package_path).expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"package file not found: {package_path}")
        return {
            "name": filename,
            "path": str(resolved),
            "size": resolved.stat().st_size,
        }

    def _analyze_sync(
        self,
        *,
        plugins: list[str],
        plugin_refs: list[dict[str, Any]] | None,
        current_sdk_version: str | None,
    ) -> dict[str, object]:
        try:
            plugin_dirs = [
                source.plugin_dir
                for source in self._resolver().resolve_many(
                    refs=plugin_refs or [],
                    specifiers=plugins,
                )
            ]
            result = analyze_bundle_plugins(
                plugin_dirs,
                current_sdk_version=current_sdk_version,
            )
            return result.model_dump(mode="json")
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="analyze") from exc

    @staticmethod
    def _has_allowed_upload_suffix(filename: str) -> bool:
        return PackageArtifactStore(
            Path("."),
            allowed_suffixes=_ALLOWED_UPLOAD_SUFFIXES,
            max_bytes=_UPLOAD_MAX_BYTES,
            copy_chunk_bytes=_UPLOAD_COPY_CHUNK_BYTES,
        ).has_allowed_suffix(filename)

    @staticmethod
    def _upload_filename_parts(filename: str) -> tuple[str, str, str]:
        return PackageArtifactStore(
            Path("."),
            allowed_suffixes=_ALLOWED_UPLOAD_SUFFIXES,
            max_bytes=_UPLOAD_MAX_BYTES,
            copy_chunk_bytes=_UPLOAD_COPY_CHUNK_BYTES,
        ).filename_parts(filename)

    @staticmethod
    def _upload_metadata(path: Path) -> dict[str, object]:
        return PackageArtifactStore.metadata(path)

    def _save_uploaded_package_sync(self, *, filename: str, content: bytes) -> dict[str, object]:
        try:
            return self._artifact_store().save_bytes(
                filename=filename,
                content=content,
            )
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="upload") from exc

    def _discard_uploaded_package_sync(self, *, package: str) -> dict[str, object]:
        """Remove one direct upload using the existing package-path policy."""
        try:
            return self._artifact_store().discard(package)
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="discard-upload") from exc

    def _save_uploaded_file_sync(self, *, filename: str, source_file: BinaryIO) -> dict[str, object]:
        """Copy an incoming upload in bounded chunks and enforce the size limit."""
        try:
            return self._artifact_store().save_file(
                filename=filename,
                source_file=source_file,
            )
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="upload") from exc

    def _save_package_file_sync(self, *, filename: str, package_path: str) -> dict[str, object]:
        """Copy an existing package into the managed package artifacts root."""

        return self._artifact_store().copy_from(
            filename=filename,
            package_path=package_path,
        )

    def _resolve_plugin_sources(
        self,
        *,
        mode: str,
        plugin: str | None,
        plugins: list[str],
        plugin_ref: dict[str, Any] | None,
        plugin_refs: list[dict[str, Any]],
    ) -> list[ResolvedPluginSource]:
        resolver = self._resolver()
        if mode == "all":
            sources = resolver.list_plugins()
            if not sources:
                roots = ", ".join(f"{root_id}={root}" for root_id, root in self._path_policy().build_source_roots)
                raise FileNotFoundError(f"No plugin.toml files found under builtin or user plugin roots ({roots})")
            return sources

        if mode == "single":
            if plugin_ref is not None:
                return [resolver.resolve_plugin_ref(plugin_ref)]
            if plugin:
                return [resolver.resolve_string(plugin)]
            raise ValueError("Please provide plugin_ref or plugin when mode=single")

        if mode in {"selected", "bundle"}:
            if plugin_refs:
                return [resolver.resolve_plugin_ref(item) for item in plugin_refs]
            if plugins:
                return [resolver.resolve_string(item) for item in plugins]
            raise ValueError(f"Please provide plugin_refs or plugins when mode={mode}")

        raise ValueError("Unsupported build mode")

    @staticmethod
    def _output_stems_for_sources(sources: list[ResolvedPluginSource]) -> dict[ResolvedPluginSource, str]:
        counts: dict[str, int] = {}
        for source in sources:
            counts[source.directory_name] = counts.get(source.directory_name, 0) + 1
        return {
            source: (
                source.directory_name
                if counts[source.directory_name] == 1
                else f"{source.root_id}_{source.directory_name}"
            )
            for source in sources
        }

    def _resolve_package_path(self, raw: str) -> Path:
        return self._artifact_store().resolve(raw)

    async def _record_install_source_best_effort(
        self,
        *,
        install_result: dict,
        package_filename: str,
        package_sha256: str,
        override: dict | None,
    ) -> str | None:
        """Best-effort record the install source in the lock file (design §7.3).

        Returns ``None`` on success or a short human-readable warning
        string on failure (to be surfaced as ``install_source_warning``
        per Req 9.6 / 10.8). This helper intentionally never raises: a
        broken install-source subsystem must not mask a successful
        plugin install.
        """
        try:
            from plugin.server.application.install_source import (
                get_install_source_manager,
            )
        except Exception as exc:
            return f"install_source_import_failed: {exc}"

        mgr = get_install_source_manager()
        if mgr is None:
            return "install_source_manager_unavailable"
        if mgr.is_degraded:
            return f"install_source_manager_degraded: {mgr.degrade_reason}"

        try:
            await asyncio.to_thread(
                _record_install_source_for_install_result,
                mgr,
                install_result,
                package_filename,
                package_sha256,
                override,
            )
            return None
        except Exception as exc:
            logger.warning(
                "record_install_source failed: err_type={}, err={}",
                type(exc).__name__,
                str(exc),
            )
            # Design §13 Fix 12: for BUILTIN_CHANNEL_LOCKED errors,
            # surface a specifically-shaped warning so ops can grep for
            # internal bug triggers.
            try:
                from plugin.server.application.install_source import InstallSourceError

                if isinstance(exc, InstallSourceError):
                    if exc.code == "BUILTIN_CHANNEL_LOCKED":
                        details = exc.details
                        return (
                            "internal_error: attempted to mutate builtin channel, "
                            f"plugin_id={details.get('plugin_id', '')} "
                            f"directory={details.get('directory_name', '')}"
                        )
                    return f"{exc.code}: {exc.message}"
            except Exception:
                pass  # classification failed; use generic fallback below
            return f"unexpected: {exc}"

    def _domain_error_from_exception(self, exc: Exception, *, action: str) -> ServerDomainError:
        if isinstance(exc, ServerDomainError):
            return exc
        package_error_code = _classify_package_error(exc)
        if package_error_code:
            status_code = 400
            code = package_error_code
        elif isinstance(exc, FileNotFoundError):
            status_code = 404
            code = "PLUGIN_CLI_NOT_FOUND"
        elif isinstance(exc, FileExistsError):
            status_code = 409
            code = "PLUGIN_CLI_CONFLICT"
        elif isinstance(exc, ValueError):
            status_code = 400
            code = "PLUGIN_CLI_INVALID_REQUEST"
        else:
            status_code = 500
            code = "PLUGIN_CLI_INTERNAL_ERROR"

        logger.warning(
            "plugin cli action failed: action={}, err_type={}, err={}",
            action,
            type(exc).__name__,
            str(exc),
        )
        return ServerDomainError(
            code=code,
            message=str(exc),
            status_code=status_code,
            details={"action": action, "error_type": type(exc).__name__},
        )


def _record_install_source_for_install_result(
    mgr,
    install_result: dict,
    package_filename: str,
    package_sha256: str,
    override: dict | None,
) -> None:
    """Walk ``install_result["installed_plugins"]`` and call the appropriate
    ``record_*`` method on ``mgr`` for each one (design §7.3).

    Raises :class:`InstallSourceError` with code ``"UNSUPPORTED_OVERRIDE"``
    when the caller supplies an ``override`` whose ``channel`` is not one
    of the supported values. Other ``InstallSourceError`` codes (e.g.
    ``PATH_OUTSIDE_ROOTS``, ``BUILTIN_CHANNEL_LOCKED``) propagate from
    the manager.
    """
    from plugin.server.application.install_source import InstallSourceError

    installed_plugins = install_result.get("installed_plugins", [])
    package_id = str(install_result.get("package_id") or "")
    profile_dir = str(install_result.get("profile_dir") or "")
    for installed in installed_plugins:
        target_dir = Path(installed["target_dir"])
        if override is None:
            mgr.record_import(
                directory_path=target_dir,
                package_filename=package_filename,
                package_sha256=package_sha256,
                package_id=package_id,
                profile_dir=profile_dir,
            )
        elif override.get("channel") == "market":
            detail = override.get("market_detail", {})
            mgr.record_market(
                directory_path=target_dir,
                plugin_market_id=detail.get("plugin_market_id", ""),
                version=detail.get("version", ""),
                package_url=detail.get("package_url", ""),
                package_id=package_id,
                profile_dir=profile_dir,
            )
        else:
            raise InstallSourceError(
                "UNSUPPORTED_OVERRIDE",
                f"unsupported override channel={override.get('channel')}",
                details={"override": override},
            )
