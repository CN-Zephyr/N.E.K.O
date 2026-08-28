from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.requests import Request

from plugin.server import http_app


pytestmark = pytest.mark.unit


class _App:
    def __init__(self) -> None:
        self.routers: list[object] = []

    def include_router(self, router: object) -> None:
        self.routers.append(router)


def _request(url: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": url.split(":", 1)[0],
            "path": "/api_key",
            "query_string": b"",
            "headers": [(b"host", url.split("//", 1)[1].encode("ascii"))],
            "server": ("testserver", 80),
        }
    )


def test_model_settings_redirect_uses_client_visible_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEKO_MAIN_SERVER_PUBLIC_ORIGIN", raising=False)

    assert http_app._model_settings_url(
        _request("http://192.168.1.25:48916"), 48911
    ) == "http://192.168.1.25:48911/api_key"


def test_model_settings_redirect_honors_public_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "NEKO_MAIN_SERVER_PUBLIC_ORIGIN", "https://neko.example.com:8443/"
    )

    assert http_app._model_settings_url(
        _request("http://127.0.0.1:48916"), 48911
    ) == "https://neko.example.com:8443/api_key"


def test_optional_router_does_not_swallow_import_attribute_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _App()

    def _import_module(_module_name: str) -> object:
        raise AttributeError("inner module bug")

    monkeypatch.setattr(http_app.importlib, "import_module", _import_module)

    with pytest.raises(AttributeError, match="inner module bug"):
        http_app._include_optional_router(
            app,
            module_name="plugin.plugins.optional_routes",
            label="optional routes",
        )

    assert app.routers == []


@pytest.mark.asyncio
async def test_persistence_reconciles_before_registry_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugin.server.application import install_source
    from plugin.server.application.package_management import registry_startup
    from utils import config_manager

    calls: list[str] = []
    manager = object()
    config_paths = object()

    class _Reconciler:
        def __init__(self, actual_manager: object) -> None:
            assert actual_manager is manager

        async def run(self) -> bool:
            calls.append("reconcile")
            return True

    async def _initialize(**kwargs):
        assert kwargs["install_source_manager"] is manager
        assert kwargs["config_paths"] is config_paths
        assert kwargs["reconciled"] is True
        assert calls == ["reconcile"]
        calls.append("registry")
        return SimpleNamespace(mode="registry", error_reason=None)

    monkeypatch.setattr(install_source, "build_install_source_manager", lambda: manager)
    monkeypatch.setattr(install_source, "StartupReconciler", _Reconciler)
    monkeypatch.setattr(
        registry_startup,
        "initialize_plugin_registry_startup",
        _initialize,
    )
    monkeypatch.setattr(
        config_manager,
        "get_config_manager",
        lambda *, migrate: config_paths if migrate is False else None,
    )

    result = await http_app._initialize_plugin_persistence()

    assert result.mode == "registry"
    assert calls == ["reconcile", "registry"]


@pytest.mark.asyncio
async def test_lifespan_selects_persistence_before_runtime_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugin.server.routes import market_bridge

    calls: list[str] = []

    class _Thread:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            calls.append("watchdog")

    async def _persistence() -> object:
        calls.append("persistence")
        return SimpleNamespace(mode="registry", error_reason=None)

    async def _lifecycle_startup() -> None:
        calls.append("lifecycle_startup")

    async def _lifecycle_shutdown() -> None:
        calls.append("lifecycle_shutdown")

    monkeypatch.setattr(http_app, "_can_register_faulthandler_signal", lambda: False)
    monkeypatch.setattr(http_app.threading, "Thread", _Thread)
    monkeypatch.setattr(http_app, "_EMBEDDED_BY_AGENT", False)
    monkeypatch.setattr(http_app, "_initialize_plugin_persistence", _persistence)
    monkeypatch.setattr(http_app, "lifecycle_startup", _lifecycle_startup)
    monkeypatch.setattr(http_app, "lifecycle_shutdown", _lifecycle_shutdown)
    monkeypatch.setattr(
        http_app,
        "_clear_plugin_persistence_authority",
        lambda: calls.append("clear"),
    )
    monkeypatch.setattr(
        market_bridge,
        "write_bridge_token_file",
        lambda _path: calls.append("bridge_token"),
    )

    async with http_app.plugin_server_lifespan(_App()):
        calls.append("serving")

    assert calls == [
        "watchdog",
        "persistence",
        "lifecycle_startup",
        "bridge_token",
        "serving",
        "lifecycle_shutdown",
        "clear",
    ]


@pytest.mark.asyncio
async def test_lifespan_clears_persistence_when_lifecycle_startup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _Thread:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            return None

    async def _persistence() -> object:
        calls.append("persistence")
        return SimpleNamespace(mode="registry", error_reason=None)

    async def _startup() -> None:
        calls.append("lifecycle_startup")
        raise RuntimeError("startup failed")

    async def _shutdown() -> None:
        calls.append("lifecycle_shutdown")

    monkeypatch.setattr(http_app, "_can_register_faulthandler_signal", lambda: False)
    monkeypatch.setattr(http_app.threading, "Thread", _Thread)
    monkeypatch.setattr(http_app, "_EMBEDDED_BY_AGENT", False)
    monkeypatch.setattr(http_app, "_initialize_plugin_persistence", _persistence)
    monkeypatch.setattr(http_app, "lifecycle_startup", _startup)
    monkeypatch.setattr(http_app, "lifecycle_shutdown", _shutdown)
    monkeypatch.setattr(
        http_app,
        "_clear_plugin_persistence_authority",
        lambda: calls.append("clear"),
    )

    with pytest.raises(RuntimeError, match="startup failed"):
        async with http_app.plugin_server_lifespan(_App()):
            pytest.fail("lifespan must not yield after startup failure")

    assert calls == [
        "persistence",
        "lifecycle_startup",
        "lifecycle_shutdown",
        "clear",
    ]


@pytest.mark.asyncio
async def test_lifespan_blocks_authority_when_persistence_selection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _Thread:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            return None

    async def _persistence() -> object:
        calls.append("persistence")
        raise RuntimeError("cannot select authority")

    async def _startup() -> None:
        calls.append("lifecycle_startup")

    async def _shutdown() -> None:
        calls.append("lifecycle_shutdown")

    monkeypatch.setattr(http_app, "_can_register_faulthandler_signal", lambda: False)
    monkeypatch.setattr(http_app.threading, "Thread", _Thread)
    monkeypatch.setattr(http_app, "_EMBEDDED_BY_AGENT", False)
    monkeypatch.setattr(http_app, "_initialize_plugin_persistence", _persistence)
    monkeypatch.setattr(http_app, "lifecycle_startup", _startup)
    monkeypatch.setattr(http_app, "lifecycle_shutdown", _shutdown)
    monkeypatch.setattr(
        http_app,
        "_block_plugin_persistence_authority",
        lambda: calls.append("block"),
    )
    monkeypatch.setattr(
        http_app,
        "_clear_plugin_persistence_authority",
        lambda: calls.append("clear"),
    )

    async with http_app.plugin_server_lifespan(_App()):
        calls.append("serving")

    assert calls == [
        "persistence",
        "block",
        "lifecycle_startup",
        "serving",
        "lifecycle_shutdown",
        "clear",
    ]
