from __future__ import annotations

import asyncio
import re
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from plugin._types.plugin_types import SUPPORTED_PLUGIN_TYPES
from plugin.core.dependency import _topological_sort_plugins
from plugin.core.entry_points import describe_plugin_entry_directory_mismatch
from plugin.core.host import _import_plugin_module
from plugin.core.registry import (
    PluginContext,
    _build_plugin_meta,
    _check_plugin_dependency,
    _ensure_python_requirement_paths,
    _extract_entries_preview,
    _extract_plugin_ui_config,
    _find_missing_python_requirements,
    _parse_single_plugin_config,
    _prepare_plugin_import_roots,
)
from plugin.core.state import state
from plugin.logging_config import get_logger
from plugin.server.application.install_source import get_install_source_manager
from plugin.server.domain.plugin_candidates import (
    CandidateKey,
    PluginCandidate,
    PluginInventory,
    PluginResolution,
    inventory_without_candidate,
    requires_legacy_shared_state_authorization,
    resolve_plugin_candidate,
)
from plugin.server.domain.errors import ServerDomainError
from plugin.server.infrastructure.plugin_selections import (
    PluginSelection,
    get_plugin_selection,
    get_plugin_selection_record,
    get_plugin_state_owner,
)
from plugin.server.infrastructure import plugin_selections as plugin_selection_store
from plugin.settings import BUILTIN_PLUGIN_CONFIG_ROOT, PLUGIN_CONFIG_ROOTS

logger = get_logger("server.application.plugins.registry")
_PLUGIN_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

_MANAGED_META_KEYS = {
    "id",
    "name",
    "type",
    "plugin_type",
    "description",
    "short_description",
    "keywords",
    "passive",
    "version",
    "sdk_version",
    "sdk_recommended",
    "sdk_supported",
    "sdk_untested",
    "sdk_conflicts",
    "input_schema",
    "author",
    "dependencies",
    "i18n",
    "plugin_ui",
    "config_path",
    "entry_point",
    "runtime_enabled",
    "runtime_auto_start",
    "runtime_load_state",
    "runtime_load_error_type",
    "runtime_load_error_message",
    "runtime_load_error_phase",
    "entries_preview",
    "adapter_mode",
    "runtime_source_missing",
    "selected_candidate",
    "available_candidate_count",
    "selection_reason",
}


@dataclass(slots=True)
class PluginDiscoveryRecord:
    plugin_id: str
    config_path: Path
    plugin_type: str
    meta_payload: dict[str, object]


@dataclass(slots=True)
class PluginDiscoveryFailure:
    plugin_id: str | None
    config_path: Path
    error: str


@dataclass(slots=True)
class PluginDiscoverySnapshot:
    records: list[PluginDiscoveryRecord]
    failures: list[PluginDiscoveryFailure]
    config_paths: set[Path]
    inventory: PluginInventory
    resolutions: Mapping[str, PluginResolution]


@dataclass(slots=True)
class PluginInventoryScan:
    inventory: PluginInventory
    failures: list[PluginDiscoveryFailure]
    config_paths: set[Path]


def _get_registered_plugin_snapshot_sync() -> dict[str, dict[str, object]]:
    with state.acquire_plugins_read_lock():
        snapshot: dict[str, dict[str, object]] = {}
        for plugin_id, meta in state.plugins.items():
            if isinstance(plugin_id, str) and isinstance(meta, dict):
                snapshot[plugin_id] = dict(meta)
        return snapshot


def _list_running_plugin_ids_sync() -> set[str]:
    running: set[str] = set()
    with state.acquire_plugin_hosts_read_lock():
        for plugin_id, host_obj in state.plugin_hosts.items():
            if not isinstance(plugin_id, str):
                continue
            try:
                if hasattr(host_obj, "is_alive") and host_obj.is_alive():
                    running.add(plugin_id)
            except Exception:
                continue
    return running


def _get_running_plugin_config_path_sync(plugin_id: str) -> Path | None:
    with state.acquire_plugin_hosts_read_lock():
        host_obj = state.plugin_hosts.get(plugin_id)
    if host_obj is None:
        return None
    try:
        if not hasattr(host_obj, "is_alive") or not host_obj.is_alive():
            return None
    except Exception:
        return None
    raw_config_path = getattr(host_obj, "config_path", None)
    if not isinstance(raw_config_path, (str, Path)):
        return None
    return _resolve_config_path(Path(raw_config_path))


def _select_managed_fields(meta: dict[str, object]) -> dict[str, object]:
    return {
        key: meta[key]
        for key in _MANAGED_META_KEYS
        if key in meta
    }


def _resolve_meta_config_path(meta: dict[str, object] | None) -> Path | None:
    if not isinstance(meta, dict):
        return None

    config_path_obj = meta.get("config_path")
    if not isinstance(config_path_obj, str) or not config_path_obj:
        return None

    try:
        return Path(config_path_obj).resolve()
    except Exception:
        return Path(config_path_obj)


def _resolve_config_path(path: Path) -> Path:
    try:
        return path.resolve()
    except Exception:
        return path


def _plugin_config_roots() -> tuple[Path, ...]:
    """Use the install-source manager as the runtime root authority."""

    manager = get_install_source_manager()
    if manager is None:
        return tuple(PLUGIN_CONFIG_ROOTS)
    roots = (
        _resolve_config_path(manager.builtin_root),
        _resolve_config_path(manager.user_root),
    )
    return tuple(dict.fromkeys(roots))


def _find_existing_runtime_plugin_id_by_config_path(
    config_path: Path,
    existing_snapshot: dict[str, dict[str, object]],
) -> str | None:
    resolved_config_path = _resolve_config_path(config_path)
    for plugin_id, meta in existing_snapshot.items():
        meta_config_path = _resolve_meta_config_path(meta)
        if meta_config_path is not None and meta_config_path == resolved_config_path:
            return plugin_id
    return None


def _collect_registered_plugin_contexts_sync(
    registered_snapshot: dict[str, dict[str, object]],
) -> tuple[list[PluginContext], dict[str, PluginContext]]:
    plugin_contexts: list[PluginContext] = []
    pid_to_context: dict[str, PluginContext] = {}
    processed_paths: set[Path] = set()

    for plugin_id, meta in sorted(registered_snapshot.items()):
        config_path = _resolve_meta_config_path(meta)
        if config_path is None or not config_path.exists():
            continue
        try:
            ctx = _parse_single_plugin_config(config_path, processed_paths, logger)
        except Exception as exc:
            logger.debug(
                "registered plugin context skipped failed config {}: err_type={}, err={}",
                config_path,
                type(exc).__name__,
                str(exc),
            )
            continue
        if ctx is None or ctx.pid != plugin_id:
            continue
        plugin_contexts.append(ctx)
        pid_to_context[plugin_id] = ctx

    return plugin_contexts, pid_to_context


def _build_ordered_plugin_ids_sync(candidate_plugin_ids: set[str] | None = None) -> list[str]:
    registered_snapshot = _get_registered_plugin_snapshot_sync()
    if not registered_snapshot:
        return []
    plugin_contexts, pid_to_context = _collect_registered_plugin_contexts_sync(
        registered_snapshot
    )

    target_ids = set(candidate_plugin_ids) if candidate_plugin_ids is not None else set(registered_snapshot.keys())
    if not target_ids:
        return []

    ordered: list[str] = []
    seen: set[str] = set()
    if plugin_contexts:
        for runtime_plugin_id in _topological_sort_plugins(plugin_contexts, pid_to_context, logger):
            if runtime_plugin_id not in target_ids or runtime_plugin_id in seen:
                continue
            if runtime_plugin_id not in registered_snapshot:
                continue
            ordered.append(runtime_plugin_id)
            seen.add(runtime_plugin_id)

    for plugin_id in sorted(target_ids):
        if plugin_id in seen or plugin_id not in registered_snapshot:
            continue
        ordered.append(plugin_id)
        seen.add(plugin_id)

    return ordered


def _scan_plugin_inventory_sync(roots: tuple[Path, ...]) -> PluginInventoryScan:
    candidates: list[PluginCandidate] = []
    failures: list[PluginDiscoveryFailure] = []
    config_paths: set[Path] = set()
    manager = get_install_source_manager()
    builtin_root = _resolve_config_path(
        manager.builtin_root
        if manager is not None
        else BUILTIN_PLUGIN_CONFIG_ROOT
    )

    for root in roots:
        resolved_root = _resolve_config_path(root)
        if not resolved_root.exists():
            logger.info("No plugin config directory {}, skipping", resolved_root)
            continue

        found_toml_files = sorted(resolved_root.glob("*/plugin.toml"))
        logger.info(
            "Found {} plugin.toml files in {}: {}",
            len(found_toml_files),
            resolved_root,
            [str(path) for path in found_toml_files],
        )
        root_id = "builtin" if resolved_root == builtin_root else "user"

        for raw_config_path in found_toml_files:
            config_path = _resolve_config_path(raw_config_path)
            config_paths.add(config_path)
            source = "builtin" if root_id == "builtin" else "manual"
            source_plugin_id: str | None = None
            release_chain_id: str | None = None
            if manager is not None:
                try:
                    entry = manager.entry_for_directory(config_path.parent)
                except Exception as exc:
                    logger.debug(
                        "candidate source lookup failed for {}: err_type={}, err={}",
                        config_path,
                        type(exc).__name__,
                        str(exc),
                    )
                else:
                    if entry is not None:
                        source = entry.channel
                        entry_plugin_id = getattr(entry, "plugin_id", None)
                        if isinstance(entry_plugin_id, str) and entry_plugin_id:
                            source_plugin_id = entry_plugin_id
                        source_detail = getattr(entry, "source_detail", None)
                        market_id = getattr(source_detail, "plugin_market_id", None)
                        if source == "market" and isinstance(market_id, str):
                            release_chain_id = market_id.strip() or None

            plugin_id = source_plugin_id or config_path.parent.name
            version = ""
            valid = True
            error: str | None = None
            try:
                with config_path.open("rb") as file_obj:
                    manifest = tomllib.load(file_obj)
                plugin_section = manifest.get("plugin")
                if not isinstance(plugin_section, dict):
                    raise ValueError("plugin.toml is missing a [plugin] table")
                raw_plugin_id = plugin_section.get("id")
                if not isinstance(raw_plugin_id, str) or not _PLUGIN_ID_PATTERN.fullmatch(
                    raw_plugin_id.strip()
                ):
                    raise ValueError("plugin.toml contains an invalid [plugin].id")
                plugin_id = raw_plugin_id.strip()
                raw_version = plugin_section.get("version")
                if isinstance(raw_version, str):
                    version = raw_version.strip()
            except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
                valid = False
                error = str(exc)
                failures.append(
                    PluginDiscoveryFailure(
                        plugin_id=plugin_id or None,
                        config_path=config_path,
                        error=error,
                    )
                )

            candidates.append(
                PluginCandidate(
                    key=CandidateKey(
                        root_id=root_id,
                        directory_name=config_path.parent.name,
                    ),
                    plugin_id=plugin_id,
                    config_path=config_path,
                    version=version,
                    source=source,
                    release_chain_id=release_chain_id,
                    valid=valid,
                    error=error,
                )
            )

    return PluginInventoryScan(
        inventory=PluginInventory.build(candidates),
        failures=failures,
        config_paths=config_paths,
    )


def _resolve_inventory_sync(
    inventory: PluginInventory,
    *,
    transient_candidates: Mapping[str, CandidateKey] | None = None,
    desired_candidates: Mapping[str, CandidateKey] | None = None,
) -> dict[str, PluginResolution]:
    transient = transient_candidates or {}
    desired_overrides = desired_candidates or {}
    resolutions: dict[str, PluginResolution] = {}
    for plugin_id in inventory.plugin_ids:
        desired = (
            desired_overrides[plugin_id]
            if plugin_id in desired_overrides
            else get_plugin_selection(plugin_id)
        )
        state_owner = get_plugin_state_owner(plugin_id)
        transient_candidate = transient.get(plugin_id)
        resolution = resolve_plugin_candidate(
            inventory,
            plugin_id,
            desired_candidate=desired,
            transient_candidate=transient_candidate,
        )
        desired_item = next(
            (
                candidate
                for candidate in inventory.for_plugin(plugin_id)
                if candidate.key == desired
            ),
            None,
        )
        selection_record = get_plugin_selection_record(plugin_id)
        shared_state_exists = bool(
            transient_candidate is None
            and state_owner is None
            and selection_record is None
            and resolution.candidate is not None
            and resolution.candidate.source != "builtin"
            and plugin_selection_store.legacy_shared_state_exists(plugin_id)
        )
        desired_is_unauthorized_external = (
            desired_item is not None
            and desired_item.source != "builtin"
            and not _selection_grants_candidate_state_access(
                selection_record,
                desired_item,
            )
        )
        resolved_is_unauthorized_external = (
            resolution.candidate is not None
            and resolution.candidate.source != "builtin"
            and (
                desired is not None
                or state_owner is not None
                or shared_state_exists
            )
            and not _selection_allows_market_chain_state_access(
                selection_record,
                state_owner,
                resolution.candidate,
            )
        )
        if transient_candidate is None and (
            desired_is_unauthorized_external
            or resolved_is_unauthorized_external
        ):
            builtin = next(
                (
                    candidate
                    for candidate in inventory.for_plugin(plugin_id)
                    if candidate.valid and candidate.source == "builtin"
                ),
                None,
            )
            resolution = PluginResolution(
                plugin_id=plugin_id,
                candidate=builtin,
                reason="state_authorization_required",
                desired_candidate=desired,
                available_candidates=inventory.for_plugin(plugin_id),
            )
        resolutions[plugin_id] = resolution
    return resolutions


def _selection_grants_candidate_state_access(
    selection: PluginSelection | None,
    candidate: PluginCandidate,
) -> bool:
    if selection is None or selection.candidate != candidate.key:
        return False
    if not selection.has_state_access_grant:
        return False
    if selection.candidate_source != candidate.source:
        return False
    if candidate.source == "market":
        return (
            bool(candidate.release_chain_id)
            and selection.release_chain_id == candidate.release_chain_id
        )
    return selection.release_chain_id is None


def _selection_allows_market_chain_state_access(
    selection: PluginSelection | None,
    state_owner: PluginSelection | None,
    candidate: PluginCandidate,
) -> bool:
    if _selection_grants_candidate_state_access(selection, candidate):
        return True
    return bool(
        state_owner is not None
        and state_owner.has_state_access_grant
        and state_owner.candidate_source == "market"
        and candidate.source == "market"
        and candidate.release_chain_id
        and state_owner.release_chain_id == candidate.release_chain_id
    )


def _selection_state_owner_candidate(
    plugin_id: str,
    selection: PluginSelection | None,
) -> PluginCandidate | None:
    if selection is None:
        return None
    source = selection.candidate_source
    if source not in {"builtin", "manual", "imported", "market"}:
        source = "builtin" if selection.candidate.root_id == "builtin" else "manual"
    return PluginCandidate(
        key=selection.candidate,
        plugin_id=plugin_id,
        config_path=Path("."),
        version="",
        source=source,
        release_chain_id=selection.release_chain_id,
    )


def _build_selected_discovery_record_sync(
    resolution: PluginResolution,
    *,
    processed_paths: set[Path],
) -> PluginDiscoveryRecord:
    candidate = resolution.candidate
    if candidate is None:
        raise ValueError(f"plugin '{resolution.plugin_id}' has no effective candidate")
    ctx = _parse_single_plugin_config(candidate.config_path, processed_paths, logger)
    if ctx is None:
        raise ValueError("plugin config could not be parsed or validated")
    if ctx.pid != resolution.plugin_id:
        raise ValueError(
            f"plugin identity changed while scanning: expected '{resolution.plugin_id}', got '{ctx.pid}'"
        )
    return _build_discovery_record_from_context(
        ctx,
        candidate=candidate,
        resolution=resolution,
    )


def _discover_registry_snapshot_sync(
    roots: tuple[Path, ...],
) -> PluginDiscoverySnapshot:
    scan = _scan_plugin_inventory_sync(roots)
    resolutions = _resolve_inventory_sync(scan.inventory)
    processed_paths: set[Path] = set()
    records: list[PluginDiscoveryRecord] = []
    failures = list(scan.failures)

    for plugin_id, resolution in resolutions.items():
        if resolution.candidate is None:
            if resolution.reason == "ambiguous" and resolution.available_candidates:
                failures.append(
                    PluginDiscoveryFailure(
                        plugin_id=plugin_id,
                        config_path=resolution.available_candidates[0].config_path,
                        error=(
                            f"plugin '{plugin_id}' has multiple candidates and requires an explicit selection"
                        ),
                    )
                )
            continue
        try:
            records.append(
                _build_selected_discovery_record_sync(
                    resolution,
                    processed_paths=processed_paths,
                )
            )
        except Exception as exc:
            logger.warning(
                "plugin discovery failed for selected candidate {}: err_type={}, err={}",
                resolution.candidate.config_path,
                type(exc).__name__,
                str(exc),
            )
            failures.append(
                PluginDiscoveryFailure(
                    plugin_id=plugin_id,
                    config_path=resolution.candidate.config_path,
                    error=str(exc),
                )
            )

    return PluginDiscoverySnapshot(
        records=records,
        failures=failures,
        config_paths=scan.config_paths,
        inventory=scan.inventory,
        resolutions=resolutions,
    )


def _build_discovery_payload(
    ctx: PluginContext,
    *,
    plugin_id: str,
) -> dict[str, object]:
    plugin_type = str(ctx.pdata.get("type", "plugin") or "plugin")
    error_type: str | None = None
    error_message: str | None = None
    error_phase: str | None = None

    if not ctx.enabled:
        entries_preview = _extract_entries_preview(
            plugin_id,
            cls=type("DisabledPluginStub", (), {}),
            conf=ctx.conf,
            pdata=ctx.pdata,
        )
    else:
        entries_preview: list[dict[str, object]]
        entry_mismatch = describe_plugin_entry_directory_mismatch(
            ctx.entry,
            config_path=ctx.toml_path,
        )
        if entry_mismatch:
            error_type = "PluginEntryDirectoryMismatch"
            error_message = entry_mismatch
            error_phase = "entry_validation"
            entries_preview = _extract_entries_preview(
                plugin_id,
                cls=type("FailedPluginStub", (), {}),
                conf=ctx.conf,
                pdata=ctx.pdata,
            )
        else:
            dependency_errors: list[str] = []
            for dep in ctx.dependencies:
                satisfied, dep_error = _check_plugin_dependency(dep, logger, plugin_id)
                if not satisfied:
                    dependency_errors.append(str(dep_error or "dependency check failed"))
                    break
            if dependency_errors:
                error_type = "DependencyCheckFailed"
                error_message = dependency_errors[0]
                error_phase = "dependency_check"
                entries_preview = _extract_entries_preview(
                    plugin_id,
                    cls=type("FailedPluginStub", (), {}),
                    conf=ctx.conf,
                    pdata=ctx.pdata,
                )
            else:
                missing_requirements = _find_missing_python_requirements(
                    ctx.python_requirements,
                    search_paths=ctx.python_requirement_paths,
                )
                if missing_requirements:
                    error_type = "MissingPythonDependencies"
                    error_message = f"Unsatisfied Python dependencies: {missing_requirements}"
                    error_phase = "python_requirements"
                    entries_preview = _extract_entries_preview(
                        plugin_id,
                        cls=type("FailedPluginStub", (), {}),
                        conf=ctx.conf,
                        pdata=ctx.pdata,
                    )
                else:
                    # The startup loader installs vendor paths on sys.path before
                    # importing each plugin's entry module; do the same here so a
                    # plugin whose [project].dependencies live only under its own
                    # vendor/ directory does not get falsely recorded as
                    # ImportError/ModuleNotFoundError during a registry refresh.
                    _ensure_python_requirement_paths(
                        ctx.python_requirement_paths,
                        logger,
                        plugin_id,
                    )
                    try:
                        module_path, class_name = ctx.entry.split(":", 1)
                        module_obj = _import_plugin_module(module_path, ctx.toml_path, logger)
                        cls_obj = getattr(module_obj, class_name)
                        entries_preview = _extract_entries_preview(plugin_id, cls_obj, ctx.conf, ctx.pdata)
                    except (ImportError, ModuleNotFoundError, SyntaxError) as exc:
                        error_type = type(exc).__name__
                        error_message = str(exc)
                        error_phase = "import_module"
                        entries_preview = _extract_entries_preview(
                            plugin_id,
                            cls=type("FailedPluginStub", (), {}),
                            conf=ctx.conf,
                            pdata=ctx.pdata,
                        )
                    except AttributeError as exc:
                        error_type = "AttributeError"
                        error_message = f"Class not found for entry '{ctx.entry}': {exc}"
                        error_phase = "import_class"
                        entries_preview = _extract_entries_preview(
                            plugin_id,
                            cls=type("FailedPluginStub", (), {}),
                            conf=ctx.conf,
                            pdata=ctx.pdata,
                        )

    plugin_meta = _build_plugin_meta(
        plugin_id,
        ctx.pdata,
        sdk_supported_str=ctx.sdk_supported_str,
        sdk_recommended_str=ctx.sdk_recommended_str,
        sdk_untested_str=ctx.sdk_untested_str,
        sdk_conflicts_list=ctx.sdk_conflicts_list,
        dependencies=ctx.dependencies,
        plugin_ui=_extract_plugin_ui_config(ctx.conf, plugin_id=plugin_id, logger=logger),
    )
    payload = plugin_meta.model_dump(mode="python")
    payload["config_path"] = str(ctx.toml_path)
    payload["entry_point"] = ctx.entry
    payload["runtime_enabled"] = bool(ctx.enabled)
    payload["runtime_auto_start"] = bool(ctx.auto_start)
    payload["entries_preview"] = entries_preview
    payload["plugin_type"] = plugin_type
    if plugin_type == "adapter":
        adapter_conf = ctx.conf.get("adapter")
        if isinstance(adapter_conf, dict):
            payload["adapter_mode"] = str(adapter_conf.get("mode", "hybrid") or "hybrid")

    if error_type and error_message and error_phase:
        payload["runtime_load_state"] = "failed"
        payload["runtime_load_error_type"] = error_type
        payload["runtime_load_error_message"] = error_message
        payload["runtime_load_error_phase"] = error_phase
    else:
        payload.pop("runtime_load_state", None)
        payload.pop("runtime_load_error_type", None)
        payload.pop("runtime_load_error_message", None)
        payload.pop("runtime_load_error_phase", None)

    payload.pop("runtime_source_missing", None)
    return payload


def _build_discovery_record_from_context(
    ctx: PluginContext,
    *,
    candidate: PluginCandidate,
    resolution: PluginResolution,
) -> PluginDiscoveryRecord:
    payload = _build_discovery_payload(ctx, plugin_id=ctx.pid)
    payload["selected_candidate"] = {
        "root_id": candidate.key.root_id,
        "directory_name": candidate.key.directory_name,
        "source": candidate.source,
        "version": candidate.version,
    }
    payload["available_candidate_count"] = len(resolution.available_candidates)
    payload["selection_reason"] = resolution.reason
    return PluginDiscoveryRecord(
        plugin_id=ctx.pid,
        config_path=ctx.toml_path,
        plugin_type=str(ctx.pdata.get("type", "plugin") or "plugin"),
        meta_payload=payload,
    )


def _apply_discovery_record_sync(
    record: PluginDiscoveryRecord,
) -> tuple[str, dict[str, object]]:
    runtime_plugin_id = record.plugin_id
    if record.plugin_type not in SUPPORTED_PLUGIN_TYPES:
        raise ServerDomainError(
            code="PLUGIN_TYPE_UNSUPPORTED",
            message=f"Plugin '{record.plugin_id}' has an unsupported type '{record.plugin_type}'",
            status_code=400,
            details={"plugin_id": record.plugin_id},
        )
    payload = dict(record.meta_payload)
    payload["id"] = runtime_plugin_id

    with state.acquire_plugins_write_lock():
        current_meta = state.plugins.get(runtime_plugin_id)
        merged = dict(current_meta) if isinstance(current_meta, dict) else {}
        for key in _MANAGED_META_KEYS:
            if key in payload:
                merged[key] = payload[key]
            else:
                merged.pop(key, None)
        state.plugins[runtime_plugin_id] = merged
    state.invalidate_snapshot_cache("plugins")
    return runtime_plugin_id, payload


def _remove_stale_plugin_metadata_sync(
    stale_ids: set[str],
    *,
    running_ids: set[str],
) -> tuple[list[str], list[str]]:
    removed: list[str] = []
    kept_running: list[str] = []
    with state.acquire_plugins_write_lock():
        for plugin_id in sorted(stale_ids):
            raw_meta = state.plugins.get(plugin_id)
            if not isinstance(raw_meta, dict):
                continue
            if plugin_id in running_ids:
                raw_meta["runtime_source_missing"] = True
                state.plugins[plugin_id] = raw_meta
                kept_running.append(plugin_id)
                continue
            state.plugins.pop(plugin_id, None)
            removed.append(plugin_id)
    if removed or kept_running:
        state.invalidate_snapshot_cache("plugins")
    return removed, kept_running


def _collect_missing_plugin_ids_sync(existing_snapshot: dict[str, dict[str, object]]) -> set[str]:
    missing_ids: set[str] = set()
    for plugin_id, meta in existing_snapshot.items():
        config_path_obj = meta.get("config_path")
        if not isinstance(config_path_obj, str) or not config_path_obj:
            continue
        try:
            config_path = Path(config_path_obj).resolve()
        except Exception:
            config_path = Path(config_path_obj)
        if not config_path.exists():
            missing_ids.add(plugin_id)
    return missing_ids


def _collect_legacy_runtime_alias_ids_sync(
    existing_snapshot: dict[str, dict[str, object]],
    inventory: PluginInventory,
) -> set[str]:
    """Return metadata ids such as ``id_1`` whose manifest declares ``id``."""

    aliases: set[str] = set()
    for runtime_plugin_id, meta in existing_snapshot.items():
        config_path = _resolve_meta_config_path(meta)
        if config_path is None:
            continue
        candidate = inventory.by_config_path(config_path)
        if candidate is not None and candidate.plugin_id != runtime_plugin_id:
            aliases.add(runtime_plugin_id)
    return aliases


def _get_autostart_plugin_ids_sync() -> list[str]:
    candidates: set[str] = set()
    with state.acquire_plugins_read_lock():
        for plugin_id, raw_meta in state.plugins.items():
            if not isinstance(plugin_id, str) or not isinstance(raw_meta, dict):
                continue
            if raw_meta.get("runtime_enabled") is False:
                continue
            if raw_meta.get("runtime_auto_start") is False:
                continue
            if raw_meta.get("runtime_load_state") == "failed":
                continue
            if raw_meta.get("runtime_source_missing") is True:
                continue
            candidates.add(plugin_id)
    return _build_ordered_plugin_ids_sync(candidates)


def _normalize_plugin_id(plugin_id: str) -> str:
    normalized_plugin_id = plugin_id.strip()
    if not _PLUGIN_ID_PATTERN.fullmatch(normalized_plugin_id):
        raise ServerDomainError(
            code="PLUGIN_INVALID_ID",
            message="Invalid plugin id",
            status_code=400,
            details={"plugin_id": plugin_id},
        )
    return normalized_plugin_id


def _candidate_key_payload(candidate: CandidateKey | None) -> dict[str, str] | None:
    if candidate is None:
        return None
    return {
        "root_id": candidate.root_id,
        "directory_name": candidate.directory_name,
    }


class PluginRegistryService:
    async def refresh_registry(self) -> dict[str, object]:
        return await asyncio.to_thread(self._refresh_registry_sync)

    async def refresh_plugin(
        self,
        plugin_id: str,
        *,
        transient_candidate: CandidateKey | None = None,
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            self._refresh_plugin_sync,
            plugin_id,
            transient_candidate,
        )

    async def list_plugin_candidates(self, plugin_id: str) -> dict[str, object]:
        return await asyncio.to_thread(self._list_plugin_candidates_sync, plugin_id)

    async def validate_plugin_candidate(
        self,
        plugin_id: str,
        candidate_key: CandidateKey,
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            self._validate_plugin_candidate_sync,
            plugin_id,
            candidate_key,
        )

    async def plan_plugin_candidate_removal(
        self,
        plugin_id: str,
        candidate_key: CandidateKey,
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            self._plan_plugin_candidate_removal_sync,
            plugin_id,
            candidate_key,
        )

    async def list_autostart_plugin_ids(self) -> list[str]:
        return await asyncio.to_thread(_get_autostart_plugin_ids_sync)

    async def order_plugin_ids(self, plugin_ids: list[str]) -> list[str]:
        return await asyncio.to_thread(self._order_plugin_ids_sync, plugin_ids)

    def _refresh_registry_sync(self) -> dict[str, object]:
        roots = _plugin_config_roots()
        _prepare_plugin_import_roots(roots, logger)

        existing_snapshot = _get_registered_plugin_snapshot_sync()
        running_ids = _list_running_plugin_ids_sync()
        added: list[str] = []
        updated: list[str] = []
        unchanged: list[str] = []
        snapshot = _discover_registry_snapshot_sync(
            roots,
        )
        failed = [
            {
                "plugin_id": item.plugin_id or "",
                "config_path": str(item.config_path),
                "error": item.error,
            }
            for item in snapshot.failures
        ]

        for record in snapshot.records:
            try:
                registered_config_path = _resolve_meta_config_path(
                    existing_snapshot.get(record.plugin_id)
                )
                running_config_path = _get_running_plugin_config_path_sync(
                    record.plugin_id
                )
                if running_config_path is None:
                    running_config_path = registered_config_path
                if (
                    record.plugin_id in running_ids
                    and running_config_path is not None
                    and running_config_path != _resolve_config_path(record.config_path)
                ):
                    failed.append(
                        {
                            "plugin_id": record.plugin_id,
                            "config_path": str(record.config_path),
                            "error": (
                                "running plugin candidate can only change through "
                                "the lifecycle switch operation"
                            ),
                        }
                    )
                    continue
                previous_runtime_plugin_id = _find_existing_runtime_plugin_id_by_config_path(
                    record.config_path,
                    existing_snapshot,
                )
                previous_plugin_id = previous_runtime_plugin_id or record.plugin_id
                previous_managed = _select_managed_fields(existing_snapshot.get(previous_plugin_id, {}))
                resolved_id, payload = _apply_discovery_record_sync(record)
                current_managed = _select_managed_fields(payload)
                if resolved_id not in existing_snapshot:
                    added.append(resolved_id)
                elif previous_managed == current_managed:
                    unchanged.append(resolved_id)
                else:
                    updated.append(resolved_id)
            except ServerDomainError as exc:
                failed.append(
                    {
                        "plugin_id": record.plugin_id,
                        "config_path": str(record.config_path),
                        "error": exc.message,
                    }
                )
            except Exception as exc:
                logger.warning(
                    "refresh_registry failed for plugin {}: err_type={}, err={}",
                    record.plugin_id,
                    type(exc).__name__,
                    str(exc),
                )
                failed.append(
                    {
                        "plugin_id": record.plugin_id,
                        "config_path": str(record.config_path),
                        "error": str(exc),
                    }
                )

        stale_ids = _collect_missing_plugin_ids_sync(existing_snapshot)
        stale_ids.update(
            plugin_id
            for plugin_id, resolution in snapshot.resolutions.items()
            if resolution.reason == "ambiguous"
        )
        stale_ids.update(
            _collect_legacy_runtime_alias_ids_sync(
                existing_snapshot,
                snapshot.inventory,
            )
        )
        removed, removed_running = _remove_stale_plugin_metadata_sync(stale_ids, running_ids=running_ids)
        return {
            "success": not failed,
            "added": added,
            "updated": updated,
            "removed": removed,
            "removed_running": removed_running,
            "unchanged": unchanged,
            "failed": failed,
            "scanned_count": len(snapshot.inventory.candidates),
            "selected_count": len(snapshot.records),
        }

    def _refresh_plugin_sync(
        self,
        plugin_id: str,
        transient_candidate: CandidateKey | None = None,
    ) -> dict[str, object]:
        normalized_plugin_id = _normalize_plugin_id(plugin_id)

        roots = _plugin_config_roots()
        existing_snapshot = _get_registered_plugin_snapshot_sync()
        running_ids = _list_running_plugin_ids_sync()
        scan = _scan_plugin_inventory_sync(roots)
        existing_config_path = _resolve_meta_config_path(
            existing_snapshot.get(normalized_plugin_id)
        )
        legacy_registered_path = (
            existing_config_path
            if existing_config_path is not None
            and existing_config_path.exists()
            and scan.inventory.by_config_path(existing_config_path) is None
            else None
        )

        if legacy_registered_path is not None:
            _prepare_plugin_import_roots(roots, logger)
            try:
                ctx = _parse_single_plugin_config(
                    legacy_registered_path,
                    set(),
                    logger,
                )
                if ctx is None or ctx.pid != normalized_plugin_id:
                    raise ValueError("registered plugin identity no longer matches")
                raw_version = ctx.pdata.get("version")
                candidate = PluginCandidate(
                    key=CandidateKey(
                        root_id="user",
                        directory_name=legacy_registered_path.parent.name,
                    ),
                    plugin_id=ctx.pid,
                    config_path=legacy_registered_path,
                    version=raw_version.strip()
                    if isinstance(raw_version, str)
                    else "",
                    source="manual",
                )
                resolution = PluginResolution(
                    plugin_id=ctx.pid,
                    candidate=candidate,
                    reason="auto_single",
                    desired_candidate=get_plugin_selection(ctx.pid),
                    available_candidates=(candidate,),
                )
                record = _build_discovery_record_from_context(
                    ctx,
                    candidate=candidate,
                    resolution=resolution,
                )
            except Exception as exc:
                raise ServerDomainError(
                    code="PLUGIN_DISCOVERY_FAILED",
                    message=f"Plugin '{normalized_plugin_id}' configuration could not be parsed",
                    status_code=400,
                    details={"plugin_id": normalized_plugin_id},
                ) from exc
            previous_managed = _select_managed_fields(
                existing_snapshot.get(normalized_plugin_id, {})
            )
            resolved_id, payload = _apply_discovery_record_sync(record)
            current_managed = _select_managed_fields(payload)
            return {
                "success": True,
                "plugin_id": resolved_id,
                "original_plugin_id": normalized_plugin_id,
                "status": (
                    "unchanged"
                    if previous_managed == current_managed
                    else "updated"
                ),
                "config_path": str(legacy_registered_path),
                "selection_reason": resolution.reason,
            }

        logical_plugin_id = normalized_plugin_id
        if logical_plugin_id not in scan.inventory.plugin_ids:
            existing_config_path = _resolve_meta_config_path(
                existing_snapshot.get(normalized_plugin_id)
            )
            existing_candidate = (
                scan.inventory.by_config_path(existing_config_path)
                if existing_config_path is not None
                else None
            )
            if existing_candidate is not None:
                logical_plugin_id = existing_candidate.plugin_id

        if logical_plugin_id not in scan.inventory.plugin_ids:
            raise ServerDomainError(
                code="PLUGIN_CONFIG_NOT_FOUND",
                message=f"Plugin '{normalized_plugin_id}' configuration not found",
                status_code=404,
                details={"plugin_id": normalized_plugin_id},
            )

        resolution = _resolve_inventory_sync(
            scan.inventory,
            transient_candidates=(
                {logical_plugin_id: transient_candidate}
                if transient_candidate is not None
                else None
            ),
        )[logical_plugin_id]
        if resolution.candidate is None:
            raise ServerDomainError(
                code=(
                    "PLUGIN_SELECTION_REQUIRED"
                    if resolution.reason == "ambiguous"
                    else "PLUGIN_CANDIDATE_UNAVAILABLE"
                ),
                message=(
                    f"Plugin '{logical_plugin_id}' requires an explicit candidate selection"
                    if resolution.reason == "ambiguous"
                    else f"Plugin '{logical_plugin_id}' has no usable candidate"
                ),
                status_code=409,
                details={
                    "plugin_id": logical_plugin_id,
                    "selection_reason": resolution.reason,
                },
            )

        _prepare_plugin_import_roots(roots, logger)
        try:
            record = _build_selected_discovery_record_sync(
                resolution,
                processed_paths=set(),
            )
        except Exception as exc:
            raise ServerDomainError(
                code="PLUGIN_DISCOVERY_FAILED",
                message=f"Plugin '{logical_plugin_id}' configuration could not be parsed",
                status_code=400,
                details={"plugin_id": logical_plugin_id},
            ) from exc

        config_path = record.config_path
        registered_config_path = _resolve_meta_config_path(
            existing_snapshot.get(logical_plugin_id)
        )
        running_config_path = _get_running_plugin_config_path_sync(logical_plugin_id)
        if running_config_path is None:
            running_config_path = registered_config_path
        if (
            logical_plugin_id in running_ids
            and running_config_path is not None
            and running_config_path != _resolve_config_path(config_path)
        ):
            raise ServerDomainError(
                code="PLUGIN_CANDIDATE_SWITCH_REQUIRED",
                message=(
                    f"Running plugin '{logical_plugin_id}' can only change candidate "
                    "through the lifecycle switch operation"
                ),
                status_code=409,
                details={"plugin_id": logical_plugin_id},
            )
        previous_runtime_plugin_id = _find_existing_runtime_plugin_id_by_config_path(
            config_path,
            existing_snapshot,
        )
        previous_plugin_id = (
            logical_plugin_id
            if logical_plugin_id in existing_snapshot
            else previous_runtime_plugin_id or logical_plugin_id
        )
        previous_managed = _select_managed_fields(existing_snapshot.get(previous_plugin_id, {}))
        resolved_id, payload = _apply_discovery_record_sync(record)
        current_managed = _select_managed_fields(payload)
        status = "added"
        if previous_plugin_id in existing_snapshot:
            status = "unchanged" if previous_managed == current_managed else "updated"

        alias_ids = _collect_legacy_runtime_alias_ids_sync(
            existing_snapshot,
            scan.inventory,
        )
        _remove_stale_plugin_metadata_sync(alias_ids, running_ids=running_ids)

        return {
            "success": True,
            "plugin_id": resolved_id,
            "original_plugin_id": normalized_plugin_id,
            "status": status,
            "config_path": str(config_path),
            "selection_reason": resolution.reason,
        }

    def _list_plugin_candidates_sync(self, plugin_id: str) -> dict[str, object]:
        normalized_plugin_id = _normalize_plugin_id(plugin_id)
        scan = _scan_plugin_inventory_sync(_plugin_config_roots())
        if normalized_plugin_id not in scan.inventory.plugin_ids:
            raise ServerDomainError(
                code="PLUGIN_CONFIG_NOT_FOUND",
                message=f"Plugin '{normalized_plugin_id}' configuration not found",
                status_code=404,
                details={"plugin_id": normalized_plugin_id},
            )

        resolution = _resolve_inventory_sync(scan.inventory)[normalized_plugin_id]
        desired = get_plugin_selection(normalized_plugin_id)
        desired_record = get_plugin_selection_record(normalized_plugin_id)
        state_owner_record = get_plugin_state_owner(normalized_plugin_id)
        registered_meta = _get_registered_plugin_snapshot_sync().get(normalized_plugin_id)
        registered_config_path = _resolve_meta_config_path(registered_meta)
        registered_candidate = (
            scan.inventory.by_config_path(registered_config_path)
            if registered_config_path is not None
            else None
        )
        running_config_path = _get_running_plugin_config_path_sync(normalized_plugin_id)
        if (
            running_config_path is None
            and normalized_plugin_id in _list_running_plugin_ids_sync()
        ):
            running_config_path = registered_config_path
        running_candidate = (
            scan.inventory.by_config_path(running_config_path)
            if running_config_path is not None
            else None
        )
        state_owner_candidate = (
            _selection_state_owner_candidate(
                normalized_plugin_id,
                state_owner_record,
            )
            or _selection_state_owner_candidate(
                normalized_plugin_id,
                desired_record,
            )
            or (
                registered_candidate
                if registered_candidate is not None
                and registered_candidate.source == "builtin"
                else None
            )
        )
        shared_state_exists = bool(
            state_owner_candidate is None
            and any(
                candidate.source != "builtin"
                for candidate in resolution.available_candidates
            )
            and plugin_selection_store.legacy_shared_state_exists(
                normalized_plugin_id
            )
        )

        return {
            "plugin_id": normalized_plugin_id,
            "desired_candidate": _candidate_key_payload(desired),
            "effective_candidate": _candidate_key_payload(
                resolution.candidate.key if resolution.candidate is not None else None
            ),
            "registered_candidate": _candidate_key_payload(
                registered_candidate.key if registered_candidate is not None else None
            ),
            "running_candidate": _candidate_key_payload(
                running_candidate.key if running_candidate is not None else None
            ),
            "selection_reason": resolution.reason,
            "candidates": [
                {
                    "key": _candidate_key_payload(candidate.key),
                    "source": candidate.source,
                    "version": candidate.version,
                    "release_chain_id": candidate.release_chain_id,
                    "state_scope": "legacy_shared",
                    "requires_shared_state_authorization": (
                        not _selection_grants_candidate_state_access(
                            desired_record,
                            candidate,
                        )
                        and candidate.source != "builtin"
                        and (
                            (
                                state_owner_candidate is None
                                and shared_state_exists
                            )
                            or (
                                not _selection_allows_market_chain_state_access(
                                    desired_record,
                                    state_owner_record,
                                    candidate,
                                )
                                and requires_legacy_shared_state_authorization(
                                    state_owner_candidate,
                                    candidate,
                                )
                            )
                        )
                    ),
                    "valid": candidate.valid,
                    "error": candidate.error,
                    "selected": desired == candidate.key,
                    "effective": (
                        resolution.candidate is not None
                        and resolution.candidate.key == candidate.key
                    ),
                    "registered": (
                        registered_candidate is not None
                        and registered_candidate.key == candidate.key
                    ),
                    "running": (
                        running_candidate is not None
                        and running_candidate.key == candidate.key
                    ),
                }
                for candidate in resolution.available_candidates
            ],
        }

    def _validate_plugin_candidate_sync(
        self,
        plugin_id: str,
        candidate_key: CandidateKey,
    ) -> dict[str, object]:
        normalized_plugin_id = _normalize_plugin_id(plugin_id)
        roots = _plugin_config_roots()
        scan = _scan_plugin_inventory_sync(roots)
        candidate = next(
            (
                item
                for item in scan.inventory.for_plugin(normalized_plugin_id)
                if item.key == candidate_key
            ),
            None,
        )
        if candidate is None:
            raise ServerDomainError(
                code="PLUGIN_CANDIDATE_NOT_FOUND",
                message=f"Candidate for plugin '{normalized_plugin_id}' was not found",
                status_code=404,
                details={
                    "plugin_id": normalized_plugin_id,
                    "candidate": _candidate_key_payload(candidate_key),
                },
            )
        if not candidate.valid:
            raise ServerDomainError(
                code="PLUGIN_CANDIDATE_INVALID",
                message=f"Candidate for plugin '{normalized_plugin_id}' is invalid",
                status_code=400,
                details={
                    "plugin_id": normalized_plugin_id,
                    "candidate": _candidate_key_payload(candidate_key),
                    "error": candidate.error or "invalid plugin manifest",
                },
            )

        resolution = resolve_plugin_candidate(
            scan.inventory,
            normalized_plugin_id,
            desired_candidate=get_plugin_selection(normalized_plugin_id),
            transient_candidate=candidate_key,
        )
        _prepare_plugin_import_roots(roots, logger)
        try:
            record = _build_selected_discovery_record_sync(
                resolution,
                processed_paths=set(),
            )
        except Exception as exc:
            raise ServerDomainError(
                code="PLUGIN_CANDIDATE_VALIDATION_FAILED",
                message=f"Candidate for plugin '{normalized_plugin_id}' could not be loaded",
                status_code=400,
                details={
                    "plugin_id": normalized_plugin_id,
                    "candidate": _candidate_key_payload(candidate_key),
                    "error_type": type(exc).__name__,
                },
            ) from exc
        load_state = record.meta_payload.get("runtime_load_state")
        if load_state == "failed":
            raise ServerDomainError(
                code="PLUGIN_CANDIDATE_VALIDATION_FAILED",
                message=f"Candidate for plugin '{normalized_plugin_id}' could not be loaded",
                status_code=400,
                details={
                    "plugin_id": normalized_plugin_id,
                    "candidate": _candidate_key_payload(candidate_key),
                    "error_type": str(
                        record.meta_payload.get("runtime_load_error_type")
                        or "PluginLoadFailed"
                    ),
                    "phase": str(
                        record.meta_payload.get("runtime_load_error_phase") or "validation"
                    ),
                },
            )
        return {
            "plugin_id": normalized_plugin_id,
            "candidate": _candidate_key_payload(candidate.key),
            "source": candidate.source,
            "version": candidate.version,
            "release_chain_id": candidate.release_chain_id,
            "state_scope": "legacy_shared",
            "valid": True,
            "runtime_enabled": record.meta_payload.get("runtime_enabled") is not False,
        }

    def _plan_plugin_candidate_removal_sync(
        self,
        plugin_id: str,
        candidate_key: CandidateKey,
    ) -> dict[str, object]:
        """Resolve the post-removal candidate without touching disk or runtime."""

        normalized_plugin_id = _normalize_plugin_id(plugin_id)
        scan = _scan_plugin_inventory_sync(_plugin_config_roots())
        target = next(
            (
                candidate
                for candidate in scan.inventory.for_plugin(normalized_plugin_id)
                if candidate.key == candidate_key
            ),
            None,
        )
        if target is None:
            raise ServerDomainError(
                code="PLUGIN_CANDIDATE_NOT_FOUND",
                message=f"Candidate for plugin '{normalized_plugin_id}' was not found",
                status_code=404,
                details={
                    "plugin_id": normalized_plugin_id,
                    "candidate": _candidate_key_payload(candidate_key),
                },
            )

        remaining = inventory_without_candidate(
            scan.inventory,
            normalized_plugin_id,
            candidate_key,
        )
        if normalized_plugin_id in remaining.plugin_ids:
            resolution = _resolve_inventory_sync(
                remaining,
                desired_candidates={normalized_plugin_id: candidate_key},
            )[normalized_plugin_id]
        else:
            resolution = resolve_plugin_candidate(
                remaining,
                normalized_plugin_id,
                desired_candidate=candidate_key,
            )

        return {
            "plugin_id": normalized_plugin_id,
            "removed_candidate": _candidate_key_payload(candidate_key),
            "fallback_candidate": _candidate_key_payload(
                resolution.candidate.key if resolution.candidate is not None else None
            ),
            "fallback_reason": resolution.reason,
        }

    def _order_plugin_ids_sync(self, plugin_ids: list[str]) -> list[str]:
        return _build_ordered_plugin_ids_sync({plugin_id for plugin_id in plugin_ids if isinstance(plugin_id, str)})
