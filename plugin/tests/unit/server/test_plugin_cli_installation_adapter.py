from __future__ import annotations

from pathlib import Path

import pytest

from plugin.server.application.package_management.plugin_cli_adapter import (
    PluginCliInstallationAdapter,
)

pytestmark = pytest.mark.plugin_unit


class _CleanupGateway:
    def __init__(self) -> None:
        self.rollback_calls: list[tuple[Path, bool]] = []

    def _rollback_install_source_best_effort(self, target_dir: Path) -> None:
        self.rollback_calls.append((target_dir, target_dir.exists()))


@pytest.mark.asyncio
async def test_failed_deployment_cleanup_removes_owned_artifacts_in_order(
    tmp_path: Path,
) -> None:
    target_dir = tmp_path / "plugins" / "demo"
    profile_dir = tmp_path / "profiles" / "package-demo"
    saved_package = tmp_path / "packages" / "demo.neko-plugin"
    for directory in (target_dir, profile_dir):
        directory.mkdir(parents=True)
        (directory / "created.txt").write_text("created", encoding="utf-8")
    saved_package.parent.mkdir(parents=True)
    saved_package.write_bytes(b"package")
    gateway = _CleanupGateway()

    await PluginCliInstallationAdapter(gateway).cleanup_failure(
        saved={"path": str(saved_package)},
        target_dirs=[target_dir],
        profile_dirs=[profile_dir],
    )

    assert gateway.rollback_calls == [(target_dir, True)]
    assert not target_dir.exists()
    assert not profile_dir.exists()
    assert not saved_package.exists()
