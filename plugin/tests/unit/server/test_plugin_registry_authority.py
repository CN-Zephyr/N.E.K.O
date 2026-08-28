from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from plugin.server.application.install_source.models import (
    PluginEntry,
    PluginRegistrySnapshot,
)
from plugin.server.application.plugins.registry_service import PluginRegistryService
from plugin.server.infrastructure.package_management.json_registry import (
    JsonPluginRegistry,
)
from plugin.server.infrastructure.plugin_registry_authority import (
    block_plugin_registry_authority,
    clear_plugin_registry_authority,
    get_published_plugin_registry,
    get_published_registry_snapshot_provider,
    is_plugin_registry_authority_configured,
    publish_plugin_registry_authority,
    update_plugin_registry,
)


pytestmark = pytest.mark.plugin_unit

TS = "2026-08-27T00:00:00.000000Z"


def _initialized_registry(tmp_path: Path) -> JsonPluginRegistry:
    registry = JsonPluginRegistry(
        tmp_path / "plugin_registry.json",
        clock=lambda: TS,
    )
    registry.initialize(
        PluginRegistrySnapshot.build({}, revision=1, updated_at=TS)
    )
    return registry


def test_uninitialized_registry_cannot_be_published(tmp_path: Path) -> None:
    clear_plugin_registry_authority()
    registry = JsonPluginRegistry(tmp_path / "plugin_registry.json")

    with pytest.raises(ValueError, match="uninitialized"):
        publish_plugin_registry_authority(registry)

    assert get_published_plugin_registry() is None


def test_blocked_authority_configures_fail_closed_provider() -> None:
    clear_plugin_registry_authority()
    block_plugin_registry_authority()

    try:
        provider = get_published_registry_snapshot_provider()
        assert is_plugin_registry_authority_configured()
        assert get_published_plugin_registry() is None
        assert provider is not None
        assert provider() is None
    finally:
        clear_plugin_registry_authority()

    assert not is_plugin_registry_authority_configured()


def test_publication_is_visible_to_services_created_before_cutover(
    tmp_path: Path,
) -> None:
    clear_plugin_registry_authority()
    service = PluginRegistryService()
    assert service._configured_registry_snapshot_provider() is None
    registry = _initialized_registry(tmp_path)

    try:
        publish_plugin_registry_authority(registry)

        provider = service._configured_registry_snapshot_provider()
        assert provider is not None
        assert provider() == registry.snapshot
        assert get_published_registry_snapshot_provider() is not None
    finally:
        clear_plugin_registry_authority(expected=registry)

    assert service._configured_registry_snapshot_provider() is None


def test_published_provider_reloads_commits_from_another_process_view(
    tmp_path: Path,
) -> None:
    clear_plugin_registry_authority()
    published = _initialized_registry(tmp_path)
    external = JsonPluginRegistry(published.path, clock=lambda: TS)

    try:
        publish_plugin_registry_authority(published)
        provider = get_published_registry_snapshot_provider()
        assert provider is not None

        update_plugin_registry(
            external,
            lambda snapshot: snapshot.with_entry(PluginEntry(plugin_id="external")),
        )

        observed = provider()
        assert observed is not None
        assert observed.revision == 2
        assert observed.entry("external") is not None
    finally:
        clear_plugin_registry_authority(expected=published)


def test_expected_registry_prevents_stale_shutdown_from_clearing_new_authority(
    tmp_path: Path,
) -> None:
    clear_plugin_registry_authority()
    previous = _initialized_registry(tmp_path / "previous")
    current = _initialized_registry(tmp_path / "current")
    publish_plugin_registry_authority(previous)
    publish_plugin_registry_authority(current)

    try:
        assert not clear_plugin_registry_authority(expected=previous)
        assert get_published_plugin_registry() is current
    finally:
        clear_plugin_registry_authority(expected=current)


def test_retrying_updates_merge_different_plugin_ids_after_cas_conflict(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plugin_registry.json"
    first = JsonPluginRegistry(path, clock=lambda: TS)
    second = JsonPluginRegistry(path, clock=lambda: TS)
    first.initialize(PluginRegistrySnapshot.build({}, revision=1, updated_at=TS))
    barrier = threading.Barrier(2)

    def synchronize_first_load(store: JsonPluginRegistry) -> None:
        original_load = store.load
        first_call = True

        def load():
            nonlocal first_call
            snapshot = original_load()
            if first_call:
                first_call = False
                barrier.wait()
            return snapshot

        store.load = load  # type: ignore[method-assign]

    synchronize_first_load(first)
    synchronize_first_load(second)

    def add(store: JsonPluginRegistry, plugin_id: str) -> None:
        update_plugin_registry(
            store,
            lambda snapshot: snapshot.with_entry(PluginEntry(plugin_id=plugin_id)),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(add, first, "alpha"),
            executor.submit(add, second, "beta"),
        ]
        for future in futures:
            future.result()

    snapshot = JsonPluginRegistry(path, clock=lambda: TS).load()
    assert snapshot.revision == 3
    assert set(snapshot.plugins) == {"alpha", "beta"}
