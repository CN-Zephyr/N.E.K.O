from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plugin.server.application.package_management.profile_removal import (
    PackageProfileRemovalRejectedError,
    PackageProfileRemovalTransactionError,
)
from plugin.server.application.plugins.operation_lock import (
    _operation_lock_is_held_by_current_task,
)
from plugin.server.domain.errors import ServerDomainError
from plugin.server.infrastructure.package_management import profile_removal_runtime


pytestmark = pytest.mark.plugin_unit


class _Coordinator:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        cleanup_deferred: bool = False,
    ) -> None:
        self.error = error
        self.cleanup_deferred = cleanup_deferred
        self.calls: list[dict[str, object]] = []

    async def remove_profile(self, **kwargs: object) -> object:
        assert _operation_lock_is_held_by_current_task() is True
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            cleanup_deferred=self.cleanup_deferred,
            cleanup_recorded=True if self.cleanup_deferred else None,
        )


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    coordinator: _Coordinator,
) -> SimpleNamespace:
    manager = SimpleNamespace(user_root=tmp_path / "plugins")
    profiles_root = tmp_path / "profiles"
    monkeypatch.setenv(
        "NEKO_PLUGIN_OPERATION_LOCK_PATH",
        str(tmp_path / "plugin-operation.lock"),
    )
    monkeypatch.setattr(
        profile_removal_runtime,
        "get_install_source_manager",
        lambda: manager,
    )
    monkeypatch.setattr(
        profile_removal_runtime,
        "get_user_package_profiles_root",
        lambda: profiles_root,
    )
    monkeypatch.setattr(
        profile_removal_runtime,
        "deferred_profile_cleanup_record_path",
        lambda: tmp_path / "profile-cleanup.json",
    )
    monkeypatch.setattr(
        profile_removal_runtime,
        "package_profile_removal_coordinator",
        coordinator,
    )
    return manager


def test_runtime_lists_only_retired_package_owned_profiles_without_local_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_root = tmp_path / "plugins"
    user_root.mkdir()
    (user_root / "code-still-present").mkdir()
    entries = [
        SimpleNamespace(
            plugin_id="zeta",
            root_id="user",
            directory_name="zeta-market",
            channel="market",
            removed=True,
            profile_installed=True,
            package_id="zeta-package",
            profile_dir=str(tmp_path / "private" / "zeta-package"),
        ),
        SimpleNamespace(
            plugin_id="alpha",
            root_id="user",
            directory_name="code-still-present",
            channel="imported",
            removed=True,
            profile_installed=True,
            package_id="alpha-package",
            profile_dir=str(tmp_path / "private" / "alpha-package"),
        ),
        SimpleNamespace(
            plugin_id="live",
            root_id="user",
            directory_name="live-market",
            channel="market",
            removed=False,
            profile_installed=True,
            package_id="live-package",
        ),
        SimpleNamespace(
            plugin_id="manual",
            root_id="user",
            directory_name="manual",
            channel="manual",
            removed=True,
            profile_installed=True,
            package_id="manual",
        ),
        SimpleNamespace(
            plugin_id="legacy",
            root_id="user",
            directory_name="legacy-market",
            channel="market",
            removed=True,
            profile_installed=None,
            package_id="legacy-package",
        ),
    ]
    manager = SimpleNamespace(
        user_root=user_root,
        list_entries=lambda *, include_removed=False: entries,
    )
    monkeypatch.setattr(
        profile_removal_runtime,
        "get_install_source_manager",
        lambda: manager,
    )

    result = profile_removal_runtime.list_retained_candidate_package_profiles()

    assert result == {
        "profiles": [
            {
                "plugin_id": "alpha",
                "candidate": {
                    "root_id": "user",
                    "directory_name": "code-still-present",
                },
                "source": "imported",
                "package_id": "alpha-package",
                "deletable": False,
                "blocked_reason": "candidate_code_present",
            },
            {
                "plugin_id": "zeta",
                "candidate": {
                    "root_id": "user",
                    "directory_name": "zeta-market",
                },
                "source": "market",
                "package_id": "zeta-package",
                "deletable": True,
                "blocked_reason": None,
            },
        ],
        "count": 2,
    }
    assert "profile_dir" not in repr(result)


def test_runtime_retained_profile_list_fails_closed_on_registry_read_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_read(*, include_removed: bool = False) -> list[object]:
        raise OSError("busy")

    monkeypatch.setattr(
        profile_removal_runtime,
        "get_install_source_manager",
        lambda: SimpleNamespace(user_root=tmp_path / "plugins", list_entries=fail_read),
    )

    with pytest.raises(ServerDomainError) as exc_info:
        profile_removal_runtime.list_retained_candidate_package_profiles()

    assert exc_info.value.code == "PLUGIN_PROFILE_LIST_FAILED"
    assert exc_info.value.status_code == 503
    assert exc_info.value.details["error_type"] == "OSError"


@pytest.mark.asyncio
async def test_runtime_holds_operation_lock_and_hides_local_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinator = _Coordinator(cleanup_deferred=True)
    manager = _configure(
        monkeypatch,
        tmp_path,
        coordinator=coordinator,
    )

    result = await profile_removal_runtime.remove_retired_candidate_package_profile(
        plugin_id="demo",
        root_id="user",
        directory_name="demo-market",
    )

    assert result == {
        "success": True,
        "plugin_id": "demo",
        "candidate": {
            "root_id": "user",
            "directory_name": "demo-market",
        },
        "package_profile_deleted": True,
        "cleanup_deferred": True,
        "cleanup_recorded": True,
        "message": "Plugin package profile deleted successfully",
    }
    [call] = coordinator.calls
    assert call["expected_plugin_id"] == "demo"
    assert call["candidate_dir"] == manager.user_root / "demo-market"
    assert "deleted_profile_dir" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected_code", "expected_status"),
    [
        ("candidate_not_registered", "PLUGIN_CANDIDATE_NOT_FOUND", 404),
        ("candidate_identity_mismatch", "PLUGIN_CANDIDATE_NOT_FOUND", 404),
        (
            "candidate_not_retired",
            "PLUGIN_PROFILE_DELETE_REQUIRES_RETIRED_CANDIDATE",
            409,
        ),
        (
            "candidate_code_present",
            "PLUGIN_PROFILE_DELETE_REQUIRES_RETIRED_CANDIDATE",
            409,
        ),
        ("profile_not_package_owned", "PLUGIN_PROFILE_DELETE_NOT_OWNED", 409),
        ("profile_missing_shared_or_unsafe", "PLUGIN_PROFILE_DELETE_UNSAFE", 409),
    ],
)
async def test_runtime_maps_rejections_to_stable_domain_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reason: str,
    expected_code: str,
    expected_status: int,
) -> None:
    _configure(
        monkeypatch,
        tmp_path,
        coordinator=_Coordinator(error=PackageProfileRemovalRejectedError(reason)),
    )

    with pytest.raises(ServerDomainError) as exc_info:
        await profile_removal_runtime.remove_retired_candidate_package_profile(
            plugin_id="demo",
            root_id="user",
            directory_name="demo-market",
        )

    assert exc_info.value.code == expected_code
    assert exc_info.value.status_code == expected_status
    assert exc_info.value.details["reason"] == reason


@pytest.mark.asyncio
async def test_runtime_reports_transaction_rollback_details(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cause = PermissionError("registry busy")
    _configure(
        monkeypatch,
        tmp_path,
        coordinator=_Coordinator(
            error=PackageProfileRemovalTransactionError(
                stage="commit",
                rollback_status="incomplete",
                cause=cause,
                rollback_errors=("restore_profile:PermissionError",),
            )
        ),
    )

    with pytest.raises(ServerDomainError) as exc_info:
        await profile_removal_runtime.remove_retired_candidate_package_profile(
            plugin_id="demo",
            root_id="user",
            directory_name="demo-market",
        )

    assert exc_info.value.code == "PLUGIN_PROFILE_DELETE_FAILED"
    assert exc_info.value.status_code == 500
    assert exc_info.value.details["reason"] == "commit"
    assert exc_info.value.details["rollback_status"] == "incomplete"
    assert exc_info.value.details["error_type"] == "PermissionError"


@pytest.mark.asyncio
async def test_runtime_fails_closed_when_persistence_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        "NEKO_PLUGIN_OPERATION_LOCK_PATH",
        str(tmp_path / "plugin-operation.lock"),
    )
    monkeypatch.setattr(
        profile_removal_runtime,
        "get_install_source_manager",
        lambda: None,
    )

    with pytest.raises(ServerDomainError) as exc_info:
        await profile_removal_runtime.remove_retired_candidate_package_profile(
            plugin_id="demo",
            root_id="user",
            directory_name="demo-market",
        )

    assert exc_info.value.code == "PLUGIN_PROFILE_DELETE_UNAVAILABLE"
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("root_id", "directory_name", "expected_code"),
    [
        ("builtin", "demo", "PLUGIN_PROFILE_DELETE_BUILTIN_FORBIDDEN"),
        ("user", "..", "PLUGIN_CANDIDATE_KEY_INVALID"),
        ("user", "nested/demo", "PLUGIN_CANDIDATE_KEY_INVALID"),
    ],
)
async def test_runtime_rejects_unsafe_candidate_keys_before_registry_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    root_id: str,
    directory_name: str,
    expected_code: str,
) -> None:
    monkeypatch.setenv(
        "NEKO_PLUGIN_OPERATION_LOCK_PATH",
        str(tmp_path / "plugin-operation.lock"),
    )
    registry_accessed = False

    def get_manager() -> None:
        nonlocal registry_accessed
        registry_accessed = True
        return None

    monkeypatch.setattr(
        profile_removal_runtime,
        "get_install_source_manager",
        get_manager,
    )

    with pytest.raises(ServerDomainError) as exc_info:
        await profile_removal_runtime.remove_retired_candidate_package_profile(
            plugin_id="demo",
            root_id=root_id,
            directory_name=directory_name,
        )

    assert exc_info.value.code == expected_code
    assert registry_accessed is False
