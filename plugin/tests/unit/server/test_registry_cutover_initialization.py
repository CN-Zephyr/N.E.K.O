from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from plugin.server.application.install_source.manager import _serialize_lock
from plugin.server.application.install_source.models import (
    LockEntry,
    LockFile,
    PluginEntry,
    PluginRegistrySnapshot,
)
from plugin.server.application.install_source.registry_preflight import (
    RegistryCutoverPreflightError,
)
from plugin.server.infrastructure.package_management.json_registry import (
    JsonPluginRegistry,
)
from plugin.server.infrastructure.package_management.registry_cutover import (
    RegistryCutoverInitializationError,
    RegistryCutoverPaths,
    initialize_registry_cutover,
)

TS = "2026-08-27T00:00:00.000000Z"


class _FakeOperationLock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.active = False
        self.entries = 0

    @asynccontextmanager
    async def hold(self):
        async with self._lock:
            self.active = True
            self.entries += 1
            try:
                yield
            finally:
                self.active = False


def _lock(*, updated_at: str = TS) -> LockFile:
    return LockFile(
        schema_version=2,
        entries=(
            LockEntry(
                root_id="user",
                directory_name="demo",
                plugin_id="demo",
                channel="manual",
                reason="user_requested",
                installed_at=TS,
                updated_at=updated_at,
                last_seen_at=updated_at,
            ),
        ),
        updated_at=updated_at,
        created_at=TS,
    )


def _selection_document() -> dict[str, object]:
    record = {
        "root_id": "user",
        "directory_name": "demo",
        "candidate_source": "manual",
        "state_scope": "legacy_shared",
        "state_access_grant": "user_authorized",
        "release_chain_id": None,
        "authorized_at": TS,
    }
    return {
        "schema_version": 3,
        "selections": {"demo": record},
        "state_owners": {"demo": record},
    }


def _paths(tmp_path) -> RegistryCutoverPaths:
    return RegistryCutoverPaths(
        install_source=tmp_path / "legacy-lock" / "plugins.lock.json",
        candidate_selections=(
            tmp_path / "legacy-config" / "plugin_candidate_selections.json"
        ),
        runtime_overrides=(
            tmp_path / "runtime-config" / "plugin_runtime_overrides.json"
        ),
        registry=tmp_path / "runtime-config" / "plugin_registry.json",
    )


def _write_legacy_state(
    paths: RegistryCutoverPaths,
    lock: LockFile,
    *,
    include_sidecars: bool = True,
) -> dict[str, bytes]:
    payloads = {"install_source": _serialize_lock(lock)}
    paths.install_source.parent.mkdir(parents=True, exist_ok=True)
    paths.install_source.write_bytes(payloads["install_source"])
    if include_sidecars:
        payloads["candidate_selections"] = json.dumps(
            _selection_document(), sort_keys=True
        ).encode()
        payloads["runtime_overrides"] = json.dumps(
            {"demo": {"enabled": False, "auto_start": True}}, sort_keys=True
        ).encode()
        paths.candidate_selections.parent.mkdir(parents=True, exist_ok=True)
        paths.runtime_overrides.parent.mkdir(parents=True, exist_ok=True)
        paths.candidate_selections.write_bytes(payloads["candidate_selections"])
        paths.runtime_overrides.write_bytes(payloads["runtime_overrides"])
    return payloads


def _provider(lock: LockFile, operation_lock: _FakeOperationLock):
    def provide() -> LockFile:
        assert operation_lock.active is True
        return lock

    return provide


@pytest.mark.asyncio
async def test_cutover_initializes_under_operation_lock_and_backs_up_sources(
    tmp_path,
) -> None:
    paths = _paths(tmp_path)
    lock = _lock()
    payloads = _write_legacy_state(paths, lock)
    operation_lock = _FakeOperationLock()

    result = await initialize_registry_cutover(
        paths=paths,
        registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
        operation_lock=operation_lock,
        lock_snapshot_provider=_provider(lock, operation_lock),
        now=TS,
    )

    assert result.status == "initialized"
    assert result.snapshot.entry("demo") is not None
    assert result.snapshot.entry("demo").enabled is False
    assert operation_lock.entries == 1
    assert paths.install_source.read_bytes() == payloads["install_source"]
    assert paths.candidate_selections.read_bytes() == payloads["candidate_selections"]
    assert paths.runtime_overrides.read_bytes() == payloads["runtime_overrides"]
    assert paths.backup_manifest.is_file()
    assert paths.initial_registry_backup.is_file()
    assert paths.cutover_commit.is_file()
    assert (paths.backup_directory / "install_source.json").read_bytes() == payloads[
        "install_source"
    ]


@pytest.mark.asyncio
async def test_cutover_is_idempotent_when_registry_and_backups_exist(tmp_path) -> None:
    paths = _paths(tmp_path)
    lock = _lock()
    _write_legacy_state(paths, lock)
    operation_lock = _FakeOperationLock()
    kwargs = {
        "paths": paths,
        "operation_lock": operation_lock,
        "lock_snapshot_provider": _provider(lock, operation_lock),
        "now": TS,
    }

    first = await initialize_registry_cutover(
        registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
        **kwargs,
    )
    manifest_bytes = paths.backup_manifest.read_bytes()
    second = await initialize_registry_cutover(
        registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
        **kwargs,
    )

    assert first.status == "initialized"
    assert second.status == "resumed"
    assert second.snapshot == first.snapshot
    assert paths.backup_manifest.read_bytes() == manifest_bytes


@pytest.mark.asyncio
async def test_cutover_recovers_after_crash_immediately_after_registry_create(
    tmp_path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    lock = _lock()
    _write_legacy_state(paths, lock)
    operation_lock = _FakeOperationLock()
    registry = JsonPluginRegistry(paths.registry, clock=lambda: TS)
    original_initialize = registry.initialize

    def initialize_then_crash(snapshot):
        original_initialize(snapshot)
        raise RuntimeError("simulated process crash")

    monkeypatch.setattr(registry, "initialize", initialize_then_crash)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        await initialize_registry_cutover(
            paths=paths,
            registry=registry,
            operation_lock=operation_lock,
            lock_snapshot_provider=_provider(lock, operation_lock),
            now=TS,
        )

    assert paths.registry.is_file()
    assert paths.backup_manifest.is_file()
    recovered = await initialize_registry_cutover(
        paths=paths,
        registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
        operation_lock=operation_lock,
        lock_snapshot_provider=_provider(lock, operation_lock),
        now=TS,
    )
    assert recovered.status == "resumed"
    assert recovered.snapshot.revision == 1


@pytest.mark.asyncio
async def test_existing_mismatched_registry_blocks_before_backup(tmp_path) -> None:
    paths = _paths(tmp_path)
    lock = _lock()
    _write_legacy_state(paths, lock)
    operation_lock = _FakeOperationLock()
    registry = JsonPluginRegistry(paths.registry, clock=lambda: TS)
    registry.initialize(
        PluginRegistrySnapshot.build(
            {"other": PluginEntry(plugin_id="other")},
            updated_at=TS,
        )
    )

    with pytest.raises(RegistryCutoverPreflightError) as exc_info:
        await initialize_registry_cutover(
            paths=paths,
            registry=registry,
            operation_lock=operation_lock,
            lock_snapshot_provider=_provider(lock, operation_lock),
            now=TS,
        )

    assert exc_info.value.reason == "shadow_mismatch"
    assert not paths.backup_directory.exists()


@pytest.mark.asyncio
async def test_corrupt_existing_registry_enters_read_only_degrade_without_fallback(
    tmp_path,
) -> None:
    paths = _paths(tmp_path)
    lock = _lock()
    _write_legacy_state(paths, lock)
    corrupt_bytes = b"{not-valid-registry"
    paths.registry.parent.mkdir(parents=True, exist_ok=True)
    paths.registry.write_bytes(corrupt_bytes)
    operation_lock = _FakeOperationLock()

    with pytest.raises(RegistryCutoverInitializationError) as exc_info:
        await initialize_registry_cutover(
            paths=paths,
            registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
            operation_lock=operation_lock,
            lock_snapshot_provider=_provider(lock, operation_lock),
            now=TS,
        )

    assert exc_info.value.reason == "registry_read_only_degrade"
    assert exc_info.value.details == {"registry_error": "LOCK_FILE_CORRUPT"}
    assert paths.registry.read_bytes() == corrupt_bytes
    assert not paths.backup_directory.exists()


@pytest.mark.asyncio
async def test_stale_lock_snapshot_blocks_before_backup_or_registry(tmp_path) -> None:
    paths = _paths(tmp_path)
    disk_lock = _lock()
    stale_lock = _lock(updated_at="2026-08-27T01:00:00.000000Z")
    _write_legacy_state(paths, disk_lock)
    operation_lock = _FakeOperationLock()

    with pytest.raises(RegistryCutoverInitializationError) as exc_info:
        await initialize_registry_cutover(
            paths=paths,
            registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
            operation_lock=operation_lock,
            lock_snapshot_provider=_provider(stale_lock, operation_lock),
            now=TS,
        )

    assert exc_info.value.reason == "legacy_snapshot_stale"
    assert not paths.backup_directory.exists()
    assert not paths.registry.exists()


@pytest.mark.asyncio
async def test_backup_conflict_blocks_without_overwriting_existing_bytes(
    tmp_path,
) -> None:
    paths = _paths(tmp_path)
    lock = _lock()
    _write_legacy_state(paths, lock)
    conflict_path = paths.backup_directory / "install_source.json"
    conflict_path.parent.mkdir(parents=True, exist_ok=True)
    conflict_path.write_bytes(b"pre-existing-conflict")
    operation_lock = _FakeOperationLock()

    with pytest.raises(RegistryCutoverInitializationError) as exc_info:
        await initialize_registry_cutover(
            paths=paths,
            registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
            operation_lock=operation_lock,
            lock_snapshot_provider=_provider(lock, operation_lock),
            now=TS,
        )

    assert exc_info.value.reason == "backup_conflict"
    assert conflict_path.read_bytes() == b"pre-existing-conflict"
    assert not paths.registry.exists()


@pytest.mark.asyncio
async def test_partial_backup_failure_is_retryable(tmp_path, monkeypatch) -> None:
    from plugin.server.infrastructure.package_management import registry_cutover

    paths = _paths(tmp_path)
    lock = _lock()
    _write_legacy_state(paths, lock)
    operation_lock = _FakeOperationLock()
    real_atomic_write = registry_cutover.atomic_write_bytes
    calls = 0

    def fail_second_write(path, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated backup failure")
        real_atomic_write(path, payload)

    monkeypatch.setattr(registry_cutover, "atomic_write_bytes", fail_second_write)
    with pytest.raises(RegistryCutoverInitializationError) as exc_info:
        await initialize_registry_cutover(
            paths=paths,
            registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
            operation_lock=operation_lock,
            lock_snapshot_provider=_provider(lock, operation_lock),
            now=TS,
        )

    assert exc_info.value.reason == "backup_write_failed"
    assert (paths.backup_directory / "install_source.json").is_file()
    assert not paths.backup_manifest.exists()
    assert not paths.registry.exists()

    monkeypatch.setattr(registry_cutover, "atomic_write_bytes", real_atomic_write)
    result = await initialize_registry_cutover(
        paths=paths,
        registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
        operation_lock=operation_lock,
        lock_snapshot_provider=_provider(lock, operation_lock),
        now=TS,
    )
    assert result.status == "initialized"


@pytest.mark.asyncio
async def test_missing_optional_sidecars_are_recorded_and_idempotent(tmp_path) -> None:
    paths = _paths(tmp_path)
    lock = _lock()
    _write_legacy_state(paths, lock, include_sidecars=False)
    operation_lock = _FakeOperationLock()

    await initialize_registry_cutover(
        paths=paths,
        registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
        operation_lock=operation_lock,
        lock_snapshot_provider=_provider(lock, operation_lock),
        now=TS,
    )

    manifest = json.loads(paths.backup_manifest.read_text(encoding="utf-8"))
    assert manifest["authorities"]["candidate_selections"]["present"] is False
    assert manifest["authorities"]["runtime_overrides"]["present"] is False
    assert not (paths.backup_directory / "candidate_selections.json").exists()
    assert not (paths.backup_directory / "runtime_overrides.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authority", "invalid_payload"),
    [
        ("candidate_selections", b"{invalid-selection-json"),
        ("runtime_overrides", b'[{"not": "an object"}]'),
    ],
)
async def test_invalid_legacy_sidecar_is_backed_up_without_creating_registry(
    tmp_path,
    authority: str,
    invalid_payload: bytes,
) -> None:
    paths = _paths(tmp_path)
    lock = _lock()
    payloads = _write_legacy_state(paths, lock)
    authority_path = getattr(paths, authority)
    authority_path.write_bytes(invalid_payload)
    payloads[authority] = invalid_payload
    operation_lock = _FakeOperationLock()

    with pytest.raises(RegistryCutoverPreflightError) as exc_info:
        await initialize_registry_cutover(
            paths=paths,
            registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
            operation_lock=operation_lock,
            lock_snapshot_provider=_provider(lock, operation_lock),
            now=TS,
        )

    assert exc_info.value.reason in {"legacy_invalid_json", "legacy_invalid_content"}
    assert exc_info.value.details == {"authority": authority}
    manifest = json.loads(
        paths.failure_backup_manifest.read_text(encoding="utf-8")
    )
    assert manifest["preflight_reason"] == exc_info.value.reason
    assert manifest["failing_authority"] == authority
    assert set(manifest) == {
        "schema_version",
        "preflight_reason",
        "failing_authority",
        "authorities",
    }
    assert (
        paths.failure_backup_directory / "install_source.json"
    ).read_bytes() == payloads["install_source"]
    assert (
        paths.failure_backup_directory / "candidate_selections.json"
    ).read_bytes() == payloads["candidate_selections"]
    assert (
        paths.failure_backup_directory / "runtime_overrides.json"
    ).read_bytes() == payloads["runtime_overrides"]
    assert authority_path.read_bytes() == invalid_payload
    assert not paths.backup_directory.exists()
    assert not paths.registry.exists()


@pytest.mark.asyncio
async def test_failed_preflight_backup_is_idempotent_and_does_not_block_repair(
    tmp_path,
) -> None:
    paths = _paths(tmp_path)
    lock = _lock()
    _write_legacy_state(paths, lock)
    paths.candidate_selections.write_bytes(b"{invalid-selection-json")
    operation_lock = _FakeOperationLock()

    for _attempt in range(2):
        with pytest.raises(RegistryCutoverPreflightError):
            await initialize_registry_cutover(
                paths=paths,
                registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
                operation_lock=operation_lock,
                lock_snapshot_provider=_provider(lock, operation_lock),
                now=TS,
            )

    failure_manifest = paths.failure_backup_manifest.read_bytes()
    paths.candidate_selections.write_text(
        json.dumps(_selection_document(), sort_keys=True),
        encoding="utf-8",
    )

    result = await initialize_registry_cutover(
        paths=paths,
        registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
        operation_lock=operation_lock,
        lock_snapshot_provider=_provider(lock, operation_lock),
        now=TS,
    )

    assert result.status == "initialized"
    assert paths.failure_backup_manifest.read_bytes() == failure_manifest
    assert paths.registry.is_file()


@pytest.mark.asyncio
async def test_changed_corrupt_authority_never_overwrites_failure_backup(
    tmp_path,
) -> None:
    paths = _paths(tmp_path)
    lock = _lock()
    _write_legacy_state(paths, lock)
    first_invalid = b"{first-invalid-selection"
    paths.candidate_selections.write_bytes(first_invalid)
    operation_lock = _FakeOperationLock()
    with pytest.raises(RegistryCutoverPreflightError):
        await initialize_registry_cutover(
            paths=paths,
            registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
            operation_lock=operation_lock,
            lock_snapshot_provider=_provider(lock, operation_lock),
            now=TS,
        )
    backup_path = paths.failure_backup_directory / "candidate_selections.json"

    paths.candidate_selections.write_bytes(b"{second-invalid-selection")
    with pytest.raises(RegistryCutoverInitializationError) as exc_info:
        await initialize_registry_cutover(
            paths=paths,
            registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
            operation_lock=operation_lock,
            lock_snapshot_provider=_provider(lock, operation_lock),
            now=TS,
        )

    assert exc_info.value.reason == "failure_backup_conflict"
    assert backup_path.read_bytes() == first_invalid
    assert not paths.registry.exists()


@pytest.mark.asyncio
async def test_committed_cutover_resumes_after_registry_advances_without_legacy(
    tmp_path,
) -> None:
    paths = _paths(tmp_path)
    lock = _lock()
    _write_legacy_state(paths, lock)
    operation_lock = _FakeOperationLock()
    registry = JsonPluginRegistry(
        paths.registry,
        clock=lambda: "2026-08-27T02:00:00.000000Z",
    )
    await initialize_registry_cutover(
        paths=paths,
        registry=registry,
        operation_lock=operation_lock,
        lock_snapshot_provider=_provider(lock, operation_lock),
        now=TS,
    )
    registry.update(
        expected_revision=1,
        mutate=lambda snapshot: snapshot.with_entry(PluginEntry(plugin_id="new")),
    )
    paths.install_source.unlink()
    paths.candidate_selections.unlink()
    paths.runtime_overrides.unlink()

    def forbidden_legacy_provider() -> LockFile:
        raise AssertionError("committed cutover must not consult retired legacy state")

    resumed = await initialize_registry_cutover(
        paths=paths,
        registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
        operation_lock=operation_lock,
        lock_snapshot_provider=forbidden_legacy_provider,
        now=TS,
    )

    assert resumed.status == "resumed"
    assert resumed.snapshot.revision == 2
    assert resumed.snapshot.entry("new") is not None


@pytest.mark.asyncio
async def test_committed_cutover_rejects_tampered_backup_chain(tmp_path) -> None:
    paths = _paths(tmp_path)
    lock = _lock()
    _write_legacy_state(paths, lock)
    operation_lock = _FakeOperationLock()
    await initialize_registry_cutover(
        paths=paths,
        registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
        operation_lock=operation_lock,
        lock_snapshot_provider=_provider(lock, operation_lock),
        now=TS,
    )
    (paths.backup_directory / "install_source.json").write_bytes(b"tampered")

    with pytest.raises(RegistryCutoverInitializationError) as exc_info:
        await initialize_registry_cutover(
            paths=paths,
            registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
            operation_lock=operation_lock,
            lock_snapshot_provider=lambda: lock,
            now=TS,
        )

    assert exc_info.value.reason == "backup_conflict"


@pytest.mark.asyncio
async def test_committed_cutover_keeps_corrupt_registry_bytes_and_never_falls_back(
    tmp_path,
) -> None:
    paths = _paths(tmp_path)
    lock = _lock()
    _write_legacy_state(paths, lock)
    operation_lock = _FakeOperationLock()
    await initialize_registry_cutover(
        paths=paths,
        registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
        operation_lock=operation_lock,
        lock_snapshot_provider=_provider(lock, operation_lock),
        now=TS,
    )
    corrupt_bytes = b"{committed-but-corrupt"
    paths.registry.write_bytes(corrupt_bytes)

    def forbidden_legacy_provider() -> LockFile:
        raise AssertionError("committed cutover must not fall back to legacy state")

    with pytest.raises(RegistryCutoverInitializationError) as exc_info:
        await initialize_registry_cutover(
            paths=paths,
            registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
            operation_lock=operation_lock,
            lock_snapshot_provider=forbidden_legacy_provider,
            now=TS,
        )

    assert exc_info.value.reason == "registry_read_only_degrade"
    assert paths.registry.read_bytes() == corrupt_bytes


@pytest.mark.asyncio
async def test_concurrent_initializers_serialize_and_resume(tmp_path) -> None:
    paths = _paths(tmp_path)
    lock = _lock()
    _write_legacy_state(paths, lock)
    operation_lock = _FakeOperationLock()
    provider = _provider(lock, operation_lock)

    results = await asyncio.gather(
        initialize_registry_cutover(
            paths=paths,
            registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
            operation_lock=operation_lock,
            lock_snapshot_provider=provider,
            now=TS,
        ),
        initialize_registry_cutover(
            paths=paths,
            registry=JsonPluginRegistry(paths.registry, clock=lambda: TS),
            operation_lock=operation_lock,
            lock_snapshot_provider=provider,
            now=TS,
        ),
    )

    assert {result.status for result in results} == {"initialized", "resumed"}
    assert operation_lock.entries == 2


@pytest.mark.parametrize(
    ("field", "bad_name"),
    [
        ("install_source", "wrong-lock.json"),
        ("candidate_selections", "wrong-selections.json"),
        ("runtime_overrides", "wrong-overrides.json"),
        ("registry", "wrong-registry.json"),
    ],
)
def test_cutover_paths_reject_wrong_authority_filenames(
    tmp_path,
    field: str,
    bad_name: str,
) -> None:
    values = {
        "install_source": tmp_path / "plugins.lock.json",
        "candidate_selections": tmp_path / "plugin_candidate_selections.json",
        "runtime_overrides": tmp_path / "plugin_runtime_overrides.json",
        "registry": tmp_path / "plugin_registry.json",
    }
    values[field] = tmp_path / bad_name

    with pytest.raises(ValueError):
        RegistryCutoverPaths(**values)
