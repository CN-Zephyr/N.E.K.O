from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import threading

import pytest

from plugin.core.plugin_layout import resolve_plugin_layout
from plugin.neko_plugin_cli.public import pack_plugin
from plugin.server.application.plugin_cli.service import PluginCliService
from plugin.server.application.plugins import mutation_guard, upgrade_support
from plugin.server.application.plugins.mutation_guard import plugin_mutation_guard
from plugin.server.application.plugins.registry_service import PluginRegistryService
from plugin.server.domain.errors import ServerDomainError


pytestmark = pytest.mark.plugin_unit


async def _async_none() -> None:
    return None


async def _async_false() -> bool:
    return False


async def _event_loop_checkpoint() -> None:
    checkpoint = asyncio.Event()
    asyncio.get_running_loop().call_soon(checkpoint.set)
    await checkpoint.wait()


def _make_plugin(
    root: Path,
    *,
    plugin_id: str,
    version: str,
    implementation: bytes,
) -> Path:
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        "\n".join(
            (
                "[plugin]",
                f'id = "{plugin_id}"',
                f'name = "{plugin_id}"',
                f'version = "{version}"',
                'type = "plugin"',
                f'entry = "{plugin_id}:Plugin"',
                "",
            )
        ),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_bytes(implementation)
    return plugin_dir


@pytest.mark.asyncio
async def test_cancelled_package_snapshot_finishes_and_is_removed_before_guard_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.neko-plugin"
    source.write_bytes(b"source-package")
    snapshot = tmp_path / "snapshot.neko-plugin"
    worker_entered = asyncio.Event()
    worker_finished = asyncio.Event()
    release_worker = threading.Event()
    contender_acquired = asyncio.Event()
    release_contender = asyncio.Event()
    loop = asyncio.get_running_loop()
    service = PluginCliService()

    def make_snapshot(_package_path: Path) -> Path:
        loop.call_soon_threadsafe(worker_entered.set)
        release_worker.wait()
        snapshot.write_bytes(b"snapshot-written-after-cancel")
        loop.call_soon_threadsafe(worker_finished.set)
        return snapshot

    monkeypatch.setattr(service, "_snapshot_package_for_operation", make_snapshot)

    async def run_snapshot() -> Path:
        async with plugin_mutation_guard():
            return await service._snapshot_package_mutation(source)

    async def contend_for_guard() -> None:
        async with plugin_mutation_guard():
            contender_acquired.set()
            await release_contender.wait()

    operation_task = asyncio.create_task(run_snapshot())
    contender_task: asyncio.Task[None] | None = None
    try:
        await worker_entered.wait()
        operation_task.cancel()
        await _event_loop_checkpoint()
        contender_task = asyncio.create_task(contend_for_guard())
        await _event_loop_checkpoint()

        assert not operation_task.done()
        assert not contender_acquired.is_set()

        operation_task.cancel()
        await _event_loop_checkpoint()
        assert not operation_task.done()
        assert not contender_acquired.is_set()

        release_worker.set()
        await worker_finished.wait()
        with pytest.raises(asyncio.CancelledError):
            await operation_task

        assert not snapshot.exists()
        await contender_acquired.wait()
        release_contender.set()
        await contender_task
        async with plugin_mutation_guard():
            pass
    finally:
        release_worker.set()
        release_contender.set()
        tasks = [operation_task]
        if contender_task is not None:
            tasks.append(contender_task)
        await asyncio.gather(*tasks, return_exceptions=True)


def _patch_plugin_roots(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tmp_path: Path,
    user_root: Path,
    packages_root: Path,
) -> None:
    from plugin import settings

    builtin_root = tmp_path / "builtin"
    profiles_root = tmp_path / "profiles"
    builtin_root.mkdir()
    packages_root.mkdir(exist_ok=True)
    monkeypatch.setattr(settings, "BUILTIN_PLUGIN_CONFIG_ROOT", builtin_root)
    monkeypatch.setattr(settings, "USER_PLUGIN_CONFIG_ROOT", user_root)
    monkeypatch.setattr(settings, "USER_PLUGIN_PACKAGES_ROOT", packages_root)
    monkeypatch.setattr(settings, "USER_PACKAGE_PROFILES_ROOT", profiles_root)
    monkeypatch.setenv(
        "NEKO_PLUGIN_INSTALLATIONS_PATH",
        str(tmp_path / "plugin-installations.json"),
    )
    monkeypatch.setenv("NEKO_STORAGE_SELECTED_ROOT", str(tmp_path / "runtime"))


@pytest.mark.asyncio
async def test_committed_replacement_cleanup_cancellation_keeps_new_version_and_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during final cleanup must never reopen the rollback window."""

    target = tmp_path / "plugins" / "demo"
    target.mkdir(parents=True)
    (target / "plugin.toml").write_bytes(b"old-version\n")
    cleanup_deleted = asyncio.Event()
    allow_cleanup_finish = threading.Event()
    committed = asyncio.Event()
    early_guard_releases: list[bool] = []
    loop = asyncio.get_running_loop()

    async def install_new() -> dict[str, object]:
        target.mkdir()
        (target / "plugin.toml").write_bytes(b"new-version\n")
        return {"installed": True}

    async def commit(result: dict[str, object]) -> dict[str, object]:
        committed.set()
        return result

    def blocking_cleanup(backup: Path) -> None:
        shutil.rmtree(backup)
        loop.call_soon_threadsafe(cleanup_deleted.set)
        assert allow_cleanup_finish.wait(timeout=10)

    async def cleanup_backup(backup: Path) -> None:
        await asyncio.to_thread(blocking_cleanup, backup)

    original_cancellation_safe = upgrade_support.await_cancellation_safe

    async def observed_cancellation_safe(operation):  # type: ignore[no-untyped-def]
        # The corrected implementation waits for the already-running cleanup
        # task. The old implementation reaches this helper only with a newly
        # created rollback coroutine after the backup has already disappeared.
        if isinstance(operation, asyncio.Task):
            allow_cleanup_finish.set()
        return await original_cancellation_safe(operation)

    original_release = mutation_guard._MUTATION_LOCK.release

    def observed_release() -> None:
        released_before_cleanup_finished = not allow_cleanup_finish.is_set()
        early_guard_releases.append(released_before_cleanup_finished)
        # Let the old implementation terminate instead of leaking its worker
        # when this assertion exposes the early release.
        allow_cleanup_finish.set()
        original_release()

    monkeypatch.setattr(
        upgrade_support,
        "await_cancellation_safe",
        observed_cancellation_safe,
    )
    monkeypatch.setattr(mutation_guard._MUTATION_LOCK, "release", observed_release)

    async def replace_under_guard() -> None:
        async with plugin_mutation_guard():
            await upgrade_support.replace_plugin(
                layout=resolve_plugin_layout("demo", target),
                install_new=install_new,
                validate_new=_async_none,
                is_running=lambda _plugin_id: _async_false(),
                stop=lambda _plugin_id: _async_none(),
                start=lambda _plugin_id: _async_none(),
                cleanup_backup=cleanup_backup,
                commit=commit,
            )

    operation = asyncio.create_task(replace_under_guard())
    await cleanup_deleted.wait()
    assert committed.is_set()

    operation.cancel()
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation

    assert early_guard_releases == [False]
    assert (target / "plugin.toml").read_bytes() == b"new-version\n"
    assert not any((target.parent / ".upgrade-backups").glob("*"))

    async with plugin_mutation_guard():
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_input", ("target", "package"))
async def test_upgrade_revalidates_confirmed_bytes_after_backup_before_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_input: str,
) -> None:
    """The bytes used for replacement must still match the confirmed plan."""

    plugin_id = f"confirmed_{changed_input}_demo"
    user_root = tmp_path / "user-plugins"
    packages_root = tmp_path / "packages"
    old_source = _make_plugin(
        tmp_path / "old-source",
        plugin_id=plugin_id,
        version="1.0.0",
        implementation=b"old implementation\n",
    )
    new_source = _make_plugin(
        tmp_path / "new-source",
        plugin_id=plugin_id,
        version="2.0.0",
        implementation=b"confirmed implementation\n",
    )
    changed_package_source = _make_plugin(
        tmp_path / "changed-package-source",
        plugin_id=plugin_id,
        version="2.0.0",
        implementation=b"changed package implementation\n",
    )
    target = user_root / plugin_id
    shutil.copytree(old_source, target)
    _patch_plugin_roots(
        monkeypatch,
        tmp_path=tmp_path,
        user_root=user_root,
        packages_root=packages_root,
    )
    package = packages_root / f"{plugin_id}.neko-plugin"
    changed_package = packages_root / f"{plugin_id}-changed.neko-plugin"
    pack_plugin(new_source, package)
    pack_plugin(changed_package_source, changed_package)

    service = PluginCliService()
    confirmed_plan = await service.plan_install(package=str(package))
    assert confirmed_plan["action"] == "upgrade"

    replacement_reached = asyncio.Event()
    release_replacement = asyncio.Event()
    release_package_snapshot = threading.Event()
    running_probe_calls = 0

    async def block_after_second_plan(_plugin_id: str) -> bool:
        nonlocal running_probe_calls
        running_probe_calls += 1
        if changed_input == "target" and running_probe_calls == 1:
            replacement_reached.set()
            await release_replacement.wait()
        return False

    monkeypatch.setattr(
        upgrade_support,
        "plugin_is_running",
        block_after_second_plan,
    )
    if changed_input == "package":
        original_snapshot_package = service._snapshot_package_for_operation
        loop = asyncio.get_running_loop()

        def block_package_snapshot(source: Path) -> Path:
            loop.call_soon_threadsafe(replacement_reached.set)
            assert release_package_snapshot.wait(timeout=10)
            return original_snapshot_package(source)

        monkeypatch.setattr(
            service,
            "_snapshot_package_for_operation",
            block_package_snapshot,
        )
    original_install_sync = service._install_sync
    install_calls = 0

    def observed_install_sync(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal install_calls
        install_calls += 1
        return original_install_sync(**kwargs)

    monkeypatch.setattr(service, "_install_sync", observed_install_sync)
    operation = asyncio.create_task(
        service.install(
            package=str(package),
            confirm_upgrade=True,
            confirmation_token=str(confirmed_plan["confirmation_token"]),
            activate_installation=False,
        )
    )
    await replacement_reached.wait()

    if changed_input == "target":
        expected_target_bytes = b"developer edit after confirmation\n"
        (target / "__init__.py").write_bytes(expected_target_bytes)
        release_replacement.set()
    else:
        expected_target_bytes = b"old implementation\n"
        shutil.copyfile(changed_package, package)
        release_package_snapshot.set()

    with pytest.raises(ServerDomainError) as exc_info:
        await operation

    assert exc_info.value.code == "PLUGIN_UPGRADE_PLAN_CHANGED"
    assert install_calls == 0
    assert (target / "__init__.py").read_bytes() == expected_target_bytes
    assert not any((user_root / ".upgrade-backups").glob("*"))


def _write_replacement_journal(
    journal_root: Path,
    *,
    schema_version: int,
    operation_id: str,
    phase: str,
    target: Path,
    backup: Path,
) -> Path:
    journal_root.mkdir(parents=True, exist_ok=True)
    journal_path = journal_root / f"{operation_id}.json"
    journal_path.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "operation_id": operation_id,
                "plugin_id": "journal_demo",
                "phase": phase,
                "targets": [
                    {
                        "target": str(target),
                        "backup": str(backup),
                        "preexisting": True,
                        "moved": True,
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return journal_path


def test_recovery_restores_clear_precommit_journal_idempotently(
    tmp_path: Path,
) -> None:
    target = tmp_path / "plugins" / "journal_demo"
    backup = tmp_path / "plugins" / ".upgrade-backups" / "journal_demo.bak.20260816"
    backup.mkdir(parents=True)
    (backup / "plugin.toml").write_bytes(b"old version\n")
    journal_root = tmp_path / "journals"
    _write_replacement_journal(
        journal_root,
        schema_version=1,
        operation_id="clear-precommit",
        phase="precommit",
        target=target,
        backup=backup,
    )

    recovery = getattr(
        upgrade_support,
        "recover_incomplete_plugin_replacements",
        None,
    )
    assert callable(recovery), "replacement journal recovery API is missing"
    first = recovery(journal_root=journal_root)

    assert first.recovered_operation_ids == ("clear-precommit",)
    assert first.manual_recovery_operation_ids == ()
    assert (target / "plugin.toml").read_bytes() == b"old version\n"
    assert not backup.exists()

    second = recovery(journal_root=journal_root)
    assert second.recovered_operation_ids == ()
    assert second.manual_recovery_operation_ids == ()
    assert (target / "plugin.toml").read_bytes() == b"old version\n"


@pytest.mark.parametrize(
    ("schema_version", "phase", "target_exists"),
    (
        (1, "precommit", True),
        (1, "commit_started", False),
        (99, "precommit", False),
    ),
)
def test_recovery_never_overwrites_ambiguous_or_future_journal(
    tmp_path: Path,
    schema_version: int,
    phase: str,
    target_exists: bool,
) -> None:
    operation_id = f"manual-{schema_version}-{phase}-{target_exists}"
    target = tmp_path / "plugins" / "journal_demo"
    backup = tmp_path / "plugins" / ".upgrade-backups" / "journal_demo.bak.20260816"
    backup.mkdir(parents=True)
    (backup / "plugin.toml").write_bytes(b"old version\n")
    if target_exists:
        target.mkdir(parents=True)
        (target / "plugin.toml").write_bytes(b"new or external version\n")
    journal_root = tmp_path / "journals"
    journal_path = _write_replacement_journal(
        journal_root,
        schema_version=schema_version,
        operation_id=operation_id,
        phase=phase,
        target=target,
        backup=backup,
    )
    journal_before = journal_path.read_bytes()

    recovery = getattr(
        upgrade_support,
        "recover_incomplete_plugin_replacements",
        None,
    )
    assert callable(recovery), "replacement journal recovery API is missing"
    result = recovery(journal_root=journal_root)

    assert result.recovered_operation_ids == ()
    assert result.manual_recovery_operation_ids == (operation_id,)
    assert (backup / "plugin.toml").read_bytes() == b"old version\n"
    if target_exists:
        assert (target / "plugin.toml").read_bytes() == b"new or external version\n"
    else:
        assert not target.exists()
    assert journal_path.read_bytes() == journal_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "expected_blocked"),
    (("precommit", frozenset()), ("commit_started", frozenset({"journal_demo"}))),
)
@pytest.mark.parametrize("entrypoint", ("registry", "plugin"))
async def test_registry_recovers_or_blocks_replacement_before_scanning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    expected_blocked: frozenset[str],
    entrypoint: str,
) -> None:
    user_root = tmp_path / "plugins"
    packages_root = tmp_path / "packages"
    user_root.mkdir()
    _patch_plugin_roots(
        monkeypatch,
        tmp_path=tmp_path,
        user_root=user_root,
        packages_root=packages_root,
    )
    target = user_root / "journal_demo"
    backup = user_root / ".upgrade-backups" / "journal_demo.bak.20260816"
    backup.mkdir(parents=True)
    (backup / "plugin.toml").write_bytes(b"old version\n")
    journal_root = user_root / ".upgrade-backups" / ".transactions"
    _write_replacement_journal(
        journal_root,
        schema_version=1,
        operation_id=f"registry-{phase}",
        phase=phase,
        target=target,
        backup=backup,
    )

    observed: list[frozenset[str]] = []
    service = PluginRegistryService()

    def scan(
        only_plugin_id: str | None = None,
        *,
        blocked_recovery_plugin_ids: frozenset[str],
    ) -> dict[str, object]:
        observed.append(blocked_recovery_plugin_ids)
        assert only_plugin_id == ("journal_demo" if entrypoint == "plugin" else None)
        if phase == "precommit":
            assert (target / "plugin.toml").read_bytes() == b"old version\n"
        return {"success": True}

    monkeypatch.setattr(service, "_refresh_registry_sync", scan)
    if entrypoint == "plugin":
        monkeypatch.setattr(
            service,
            "_refresh_plugin_sync",
            lambda plugin_id, *, blocked_recovery_plugin_ids: scan(
                plugin_id,
                blocked_recovery_plugin_ids=blocked_recovery_plugin_ids,
            ),
        )
        result = await service.refresh_plugin("journal_demo")
    else:
        result = await service.refresh_registry()

    assert result == {"success": True}
    assert observed == [expected_blocked]
