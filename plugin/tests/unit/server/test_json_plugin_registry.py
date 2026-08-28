from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from plugin.server.application.install_source import InstallSourceError
from plugin.server.application.install_source.models import (
    PluginEntry,
    PluginRegistrySnapshot,
)
from plugin.server.infrastructure.package_management import (
    JsonPluginRegistry,
    RegistryNotInitializedError,
    RegistryRevisionConflict,
)
from plugin.server.infrastructure.package_management import atomic_files
from plugin.server.infrastructure.package_management import json_registry as store_module
from tests.fake_clock import patch_module_clock

pytestmark = pytest.mark.plugin_unit

TS = "2026-08-27T00:00:00.000000Z"
TS_LATER = "2026-08-27T12:00:00.000000Z"


def _empty_snapshot() -> PluginRegistrySnapshot:
    return PluginRegistrySnapshot.build(
        {},
        revision=1,
        updated_at=TS,
        created_at=TS,
    )


def _add_entry(plugin_id: str):
    def mutate(snapshot: PluginRegistrySnapshot) -> PluginRegistrySnapshot:
        return snapshot.with_entry(PluginEntry(plugin_id=plugin_id))

    return mutate


def test_registry_requires_explicit_initialization(tmp_path: Path) -> None:
    store = JsonPluginRegistry(tmp_path / "plugin_registry.json", clock=lambda: TS)

    with pytest.raises(RegistryNotInitializedError):
        store.load()

    initialized = store.initialize(_empty_snapshot())

    assert initialized == _empty_snapshot()
    assert store.load() == initialized


def test_registry_update_rejects_a_stale_expected_revision(tmp_path: Path) -> None:
    store = JsonPluginRegistry(tmp_path / "plugin_registry.json", clock=lambda: TS_LATER)
    store.initialize(_empty_snapshot())

    updated = store.update(expected_revision=1, mutate=_add_entry("alpha"))

    assert updated.revision == 2
    assert set(updated.plugins) == {"alpha"}
    with pytest.raises(RegistryRevisionConflict) as exc_info:
        store.update(expected_revision=1, mutate=_add_entry("beta"))
    assert exc_info.value.actual == 2
    assert set(store.load().plugins) == {"alpha"}


def test_registry_noop_update_does_not_advance_revision(tmp_path: Path) -> None:
    store = JsonPluginRegistry(tmp_path / "plugin_registry.json", clock=lambda: TS)
    initial = store.initialize(_empty_snapshot())

    current = store.update(expected_revision=1, mutate=lambda snapshot: snapshot)

    assert current == initial
    assert store.load().revision == 1


@pytest.mark.parametrize("expected_revision", [0, -1, True, "1"])
def test_registry_update_rejects_an_invalid_expected_revision(
    tmp_path: Path, expected_revision: object
) -> None:
    store = JsonPluginRegistry(tmp_path / "plugin_registry.json", clock=lambda: TS)
    store.initialize(_empty_snapshot())

    with pytest.raises(ValueError, match="positive integer"):
        store.update(  # type: ignore[arg-type]
            expected_revision=expected_revision,
            mutate=_add_entry("alpha"),
        )

    assert store.load() == _empty_snapshot()


def test_two_store_instances_cannot_lose_concurrent_plugin_updates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plugin_registry.json"
    first_store = JsonPluginRegistry(path, clock=lambda: TS_LATER)
    second_store = JsonPluginRegistry(path, clock=lambda: TS_LATER)
    first_store.initialize(_empty_snapshot())
    barrier = threading.Barrier(3)

    def update(store: JsonPluginRegistry, plugin_id: str) -> tuple[str, str]:
        barrier.wait()
        try:
            store.update(expected_revision=1, mutate=_add_entry(plugin_id))
        except RegistryRevisionConflict:
            return "conflict", plugin_id
        return "written", plugin_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(update, first_store, "alpha"),
            executor.submit(update, second_store, "beta"),
        ]
        barrier.wait()
        results = [future.result() for future in futures]

    assert sorted(status for status, _ in results) == ["conflict", "written"]
    current = first_store.load()
    assert current.revision == 2
    losing_plugin_id = next(
        plugin_id for status, plugin_id in results if status == "conflict"
    )

    merged = second_store.update(
        expected_revision=current.revision,
        mutate=_add_entry(losing_plugin_id),
    )

    assert merged.revision == 3
    assert set(merged.plugins) == {"alpha", "beta"}


def test_failed_atomic_replace_keeps_the_previous_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "plugin_registry.json"
    store = JsonPluginRegistry(path, clock=lambda: TS_LATER)
    store.initialize(_empty_snapshot())

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(store_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        store.update(expected_revision=1, mutate=_add_entry("alpha"))

    monkeypatch.undo()
    assert JsonPluginRegistry(path, clock=lambda: TS).load() == _empty_snapshot()
    assert not list(tmp_path.glob(".plugin_registry.json.*.tmp"))


def test_registry_retries_transient_windows_replace_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "plugin_registry.json"
    store = JsonPluginRegistry(path, clock=lambda: TS_LATER)
    store.initialize(_empty_snapshot())
    real_replace = store_module.os.replace
    replace_attempts = 0
    sleep_calls: list[float] = []

    def flaky_replace(source: Path, target: Path) -> None:
        nonlocal replace_attempts
        replace_attempts += 1
        if replace_attempts < 3:
            raise PermissionError("target is temporarily busy")
        real_replace(source, target)

    monkeypatch.setattr(store_module.os, "replace", flaky_replace)
    patch_module_clock(monkeypatch, atomic_files, sleep=sleep_calls.append)

    updated = store.update(expected_revision=1, mutate=_add_entry("alpha"))

    assert updated.revision == 2
    assert replace_attempts == 3
    assert sleep_calls == [0.05, 0.1]


def test_registry_preserves_replace_error_when_temp_cleanup_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "plugin_registry.json"
    store = JsonPluginRegistry(path, clock=lambda: TS_LATER)
    store.initialize(_empty_snapshot())
    replace_error = PermissionError("target remains busy")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise replace_error

    def fail_cleanup(_path: Path, *, missing_ok: bool = False) -> None:
        del missing_ok
        raise PermissionError("temporary file remains busy")

    monkeypatch.setattr(store_module.os, "replace", fail_replace)
    patch_module_clock(monkeypatch, atomic_files, sleep=lambda _delay: None)
    monkeypatch.setattr(Path, "unlink", fail_cleanup)

    with pytest.raises(PermissionError) as exc_info:
        store.update(expected_revision=1, mutate=_add_entry("alpha"))

    assert exc_info.value is replace_error


def test_store_rejects_future_schema_without_rewriting_it(tmp_path: Path) -> None:
    path = tmp_path / "plugin_registry.json"
    raw = {
        "schema_version": 2,
        "revision": 9,
        "updated_at": TS,
        "plugins": {},
        "future_field": {"must_survive": True},
    }
    original = json.dumps(raw).encode("utf-8")
    path.write_bytes(original)
    store = JsonPluginRegistry(path, clock=lambda: TS)

    with pytest.raises(InstallSourceError) as exc_info:
        store.load()

    assert exc_info.value.code == "UNSUPPORTED_REGISTRY_SCHEMA"
    assert path.read_bytes() == original
