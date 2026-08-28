from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from plugin.server.application.install_source.manager import (
    InstallSourceError,
    InstallSourceManager,
    _serialize_lock,
)
from plugin.server.application.install_source.models import (
    CandidateRef,
    LockFile,
    PluginRegistrySnapshot,
    StateOwnership,
)
from plugin.server.application.install_source.scanner import PluginDirectoryScanner
from plugin.server.application.package_management.profile_cleanup import (
    PackageProfileService,
)
from plugin.server.application.package_management.profile_removal import (
    PackageProfileRemovalCoordinator,
)
from plugin.server.infrastructure.package_management.install_source_facade import (
    RegistryInstallSourceFacade,
)
from plugin.server.infrastructure.package_management.json_registry import (
    JsonPluginRegistry,
)


pytestmark = pytest.mark.plugin_unit

TS = "2026-08-28T00:00:00.000000Z"
SHA = "a" * 64


def _write_plugin(root: Path, directory_name: str, plugin_id: str) -> Path:
    target = root / directory_name
    target.mkdir(parents=True)
    (target / "plugin.toml").write_text(
        f'[plugin]\nid = "{plugin_id}"\nname = "Demo"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    return target


def _build(
    tmp_path: Path,
) -> tuple[RegistryInstallSourceFacade, JsonPluginRegistry, bytes]:
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    builtin_root.mkdir()
    user_root.mkdir()
    lock_path = tmp_path / "plugins.lock.json"
    lock = LockFile(
        schema_version=2,
        entries=(),
        updated_at=TS,
        created_at=TS,
    )
    lock_path.write_bytes(_serialize_lock(lock))
    manager = InstallSourceManager(
        lock_path=lock_path,
        builtin_root=builtin_root,
        user_root=user_root,
        scanner=PluginDirectoryScanner(builtin_root, user_root),
        clock=lambda: datetime(2026, 8, 28, tzinfo=UTC),
    )
    manager.load()
    registry = JsonPluginRegistry(
        tmp_path / "plugin_registry.json",
        clock=lambda: TS,
    )
    registry.initialize(
        PluginRegistrySnapshot.build(
            {},
            revision=1,
            updated_at=TS,
            created_at=TS,
        )
    )
    return (
        RegistryInstallSourceFacade(
            legacy_manager=manager,
            registry=registry,
            clock=lambda: TS,
        ),
        registry,
        lock_path.read_bytes(),
    )


def test_import_and_read_views_use_registry_without_touching_legacy_lock(
    tmp_path: Path,
) -> None:
    facade, registry, legacy_bytes = _build(tmp_path)
    target = _write_plugin(facade.user_root, "demo-dir", "demo")

    facade.record_import(
        directory_path=target,
        package_filename="demo.neko-plugin",
        package_sha256=SHA,
        package_id="demo-package",
        profile_dir=str(tmp_path / "profiles" / "demo-package"),
    )

    [entry] = facade.list_entries()
    assert entry.plugin_id == "demo"
    assert entry.channel == "imported"
    assert entry.source_detail.package_filename == "demo.neko-plugin"
    assert facade.entry_for_directory(target) == entry
    assert facade.package_id_for_directory(target) == "demo-package"
    assert facade.profile_dir_for_directory(target).endswith("demo-package")
    assert facade.to_api_view("demo", directory_path=target)["source"] == "imported"
    assert facade.snapshot().updated_at == registry.load().updated_at
    assert facade.lock_path.read_bytes() == legacy_bytes


def test_market_install_upgrade_and_rollback_preserve_v2_contract(
    tmp_path: Path,
) -> None:
    facade, registry, legacy_bytes = _build(tmp_path)
    _write_plugin(facade.user_root, "demo", "demo")
    first_detail = {
        "plugin_market_id": "market-demo",
        "version": "1.0.0",
        "package_url": "https://example.invalid/demo-1.neko-plugin",
        "package_sha256": SHA,
        "payload_hash": None,
        "channel": "stable",
        "published_at": TS,
    }
    original, warnings = facade.record_market_install(
        root_id="user",
        directory_name="demo",
        plugin_id="demo",
        market_detail=first_detail,
        package_id="demo-package",
    )
    assert warnings == []
    assert original.source_detail.previous_version is None

    upgraded, warnings = facade.record_market_upgrade(
        root_id="user",
        directory_name="demo",
        plugin_id="demo",
        market_detail={**first_detail, "version": "2.0.0"},
        package_id="demo-package",
    )
    assert warnings == []
    assert upgraded.installed_at == original.installed_at
    assert upgraded.source_detail.previous_version == "1.0.0"
    assert facade.find_active_market_entry("market-demo") == upgraded

    facade.restore_entry_for_rollback(original)

    restored = facade.entry_for_directory(facade.user_root / "demo")
    assert restored == original
    assert registry.load().revision == 4
    assert facade.lock_path.read_bytes() == legacy_bytes


def test_mark_removed_atomically_clears_matching_selection_but_retains_owner(
    tmp_path: Path,
) -> None:
    facade, registry, legacy_bytes = _build(tmp_path)
    target = _write_plugin(facade.user_root, "demo", "demo")
    facade.record_import(
        directory_path=target,
        package_filename="demo.neko-plugin",
        package_sha256=SHA,
    )
    current = registry.load()
    ref = CandidateRef(root_id="user", directory_name="demo")
    registry.update(
        expected_revision=current.revision,
        mutate=lambda snapshot: snapshot.with_entry(
            replace(
                snapshot.entry("demo"),
                selected_candidate=ref,
                candidate_source="imported",
                state_owner=StateOwnership(
                    candidate=ref,
                    state_scope="legacy_shared",
                    state_access_grant="user_authorized",
                    authorized_at=TS,
                ),
            )
        ),
    )

    facade.mark_removed(directory_path=target)

    entry = registry.load().entry("demo")
    assert entry.selected_candidate is None
    assert entry.candidate_source is None
    assert entry.state_owner is not None
    assert entry.state_owner.candidate == ref
    assert entry.candidates[0].removed is True
    assert facade.list_entries() == []
    assert len(facade.list_entries(include_removed=True)) == 1
    revision = registry.load().revision
    facade.mark_removed(directory_path=target)
    assert registry.load().revision == revision
    assert facade.lock_path.read_bytes() == legacy_bytes


def test_mark_profile_removed_updates_removed_candidate_without_losing_audit(
    tmp_path: Path,
) -> None:
    facade, registry, legacy_bytes = _build(tmp_path)
    target = _write_plugin(facade.user_root, "demo", "demo")
    profile_dir = tmp_path / "profiles" / "demo-package"
    facade.record_import(
        directory_path=target,
        package_filename="demo.neko-plugin",
        package_sha256=SHA,
        package_id="demo-package",
        profile_dir=str(profile_dir),
    )
    facade.mark_removed(directory_path=target)
    before = facade.entry_for_directory(target, include_removed=True)
    assert before is not None
    revision_before = registry.load().revision

    facade.mark_profile_removed(directory_path=target)

    after = facade.entry_for_directory(target, include_removed=True)
    assert after is not None
    assert after.removed is True
    assert after.removed_at == before.removed_at
    assert after.installed_at == before.installed_at
    assert after.source_detail == before.source_detail
    assert after.package_id == "demo-package"
    assert after.profile_dir == ""
    assert after.profile_installed is False
    assert registry.load().revision == revision_before + 1
    facade.mark_profile_removed(directory_path=target)
    assert registry.load().revision == revision_before + 1
    assert facade.lock_path.read_bytes() == legacy_bytes


@pytest.mark.asyncio
async def test_profile_removal_transaction_commits_to_registry_facade(
    tmp_path: Path,
) -> None:
    facade, registry, legacy_bytes = _build(tmp_path)
    target = _write_plugin(facade.user_root, "demo", "demo")
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / "demo-package"
    profile_dir.mkdir(parents=True)
    (profile_dir / "settings.toml").write_text("value = true\n", encoding="utf-8")
    facade.record_import(
        directory_path=target,
        package_filename="demo.neko-plugin",
        package_sha256=SHA,
        package_id="demo-package",
        profile_dir=str(profile_dir),
    )
    facade.mark_removed(directory_path=target)
    target.rename(tmp_path / "retired-demo-code")
    revision_before = registry.load().revision
    coordinator = PackageProfileRemovalCoordinator(PackageProfileService())

    result = await coordinator.remove_profile(
        expected_plugin_id="demo",
        candidate_dir=target,
        registry=facade,
        profiles_root=profiles_root,
    )

    entry = facade.entry_for_directory(target, include_removed=True)
    assert entry is not None
    assert entry.profile_installed is False
    assert entry.profile_dir == ""
    assert entry.removed is True
    assert result.deleted_profile_dir == profile_dir
    assert profile_dir.exists() is False
    assert registry.load().revision == revision_before + 1
    assert facade.lock_path.read_bytes() == legacy_bytes


def test_facade_preserves_builtin_and_identity_conflict_guards(tmp_path: Path) -> None:
    facade, _registry, _legacy_bytes = _build(tmp_path)
    builtin = _write_plugin(facade.builtin_root, "builtin-demo", "demo")

    with pytest.raises(InstallSourceError) as builtin_error:
        facade.record_import(
            directory_path=builtin,
            package_filename="demo.neko-plugin",
            package_sha256=SHA,
        )
    assert builtin_error.value.code == "BUILTIN_CHANNEL_LOCKED"

    target = _write_plugin(facade.user_root, "shared-dir", "first-id")
    facade.record_import(
        directory_path=target,
        package_filename="first.neko-plugin",
        package_sha256=SHA,
    )
    (target / "plugin.toml").write_text(
        '[plugin]\nid = "second-id"\nname = "Demo"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )

    with pytest.raises(InstallSourceError) as identity_error:
        facade.record_import(
            directory_path=target,
            package_filename="second.neko-plugin",
            package_sha256=SHA,
        )
    assert identity_error.value.code == "PLUGIN_ID_CONFLICT"


def test_facade_reports_registry_corruption_without_falling_back_to_legacy(
    tmp_path: Path,
) -> None:
    facade, registry, legacy_bytes = _build(tmp_path)
    registry.path.write_bytes(b"not-json")

    assert facade.is_degraded is True
    assert facade.degrade_reason == "registry_read_failed"
    with pytest.raises(InstallSourceError):
        facade.snapshot()
    assert facade.lock_path.read_bytes() == legacy_bytes
