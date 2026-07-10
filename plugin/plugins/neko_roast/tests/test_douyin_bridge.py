import asyncio
import time
from types import SimpleNamespace

import pytest

from plugin.plugins.neko_roast.core.pipeline_routing import support_event_type
from plugin.plugins.neko_roast.core.runtime_douyin_auth import normalize_cookie
from plugin.plugins.neko_roast.modules.douyin_live_ingest import DouyinLiveIngestModule
from plugin.plugins.neko_roast.modules.douyin_live_ingest.bridge_adapter import (
    DouyinLiveBridgeAdapter,
)
from plugin.plugins.neko_roast.modules.douyin_live_ingest.event_model import platform_uid
from plugin.plugins.neko_roast.modules.douyin_live_ingest.room_ref import (
    parse_douyin_room_ref,
)
from plugin.plugins.neko_roast.modules.live_bridge import (
    LiveBridgeStartRequest,
    LiveBridgeTransport,
)
from plugin.plugins.neko_roast.modules.live_bridge import process_supervisor as supervisor_module
from plugin.plugins.neko_roast.modules.live_bridge.process_supervisor import (
    BridgeProcessSupervisor,
    cleanup_stale_windows_processes,
)


class _Bus:
    def __init__(self) -> None:
        self.events = []

    def publish(self, event_type, event) -> None:
        self.events.append((event_type, event))


def test_cookie_normalization_accepts_cookie_header_only() -> None:
    assert normalize_cookie("Cookie: sessionid=abc; ttwid=xyz") == (
        "sessionid=abc; ttwid=xyz"
    )

    with pytest.raises(ValueError, match="unsupported header"):
        normalize_cookie("Cookie: sessionid=abc\nAuthorization: Bearer secret")


def test_room_reference_accepts_supported_url_and_rejects_other_hosts() -> None:
    parsed = parse_douyin_room_ref("https://live.douyin.com/123456")

    assert parsed.ok is True
    assert parsed.room_ref == "123456"
    assert parse_douyin_room_ref("https://example.com/123456").ok is False


def test_bridge_adapter_keeps_only_public_event_fields() -> None:
    adapter = DouyinLiveBridgeAdapter()

    payloads = adapter.map_message(
        {
            "method": "WebcastChatMessage",
            "user": {"id": "42", "nickname": "viewer"},
            "content": "hello",
            "cookie": "sessionid=secret",
        },
        room_ref="123456",
    )

    assert payloads == [
        {
            "event_type": "danmaku",
            "room_ref": "123456",
            "uid": "42",
            "nickname": "viewer",
            "text": "hello",
            "avatar_url": "",
            "gift_name": "",
            "gift_count": 0,
            "gift_value": 0,
            "room_id": 0,
        }
    ]


def test_routable_event_is_published_without_raw_credentials() -> None:
    bus = _Bus()
    module = DouyinLiveIngestModule()
    module.ctx = SimpleNamespace(
        event_bus=bus,
        config=SimpleNamespace(live_mode="co_stream"),
    )
    module._room_ref = "123456"

    event = module.publish_provider_event(
        {
            "event_type": "chat",
            "uid": "42",
            "text": "hello",
            "room_ref": "123456",
            "cookie": "sessionid=secret",
        },
        ts=1.0,
    )

    assert event is not None
    assert event.type == "danmaku"
    assert event.uid == "douyin:42"
    assert event.payload["text"] == "hello"
    assert "cookie" not in event.payload
    assert bus.events == [("danmaku", event)]


def test_support_event_keeps_live_source_and_routes_by_event_type() -> None:
    module = DouyinLiveIngestModule()
    module.ctx = SimpleNamespace(config=SimpleNamespace(live_mode="co_stream"))

    event = module.normalize(
        {"event_type": "gift", "uid": "viewer-token-42", "gift_name": "rose"}
    )

    assert event.uid == "douyin:viewer-token-42"
    assert event.source == "live_danmaku"
    assert support_event_type(event) == "gift"


def test_platform_uid_accepts_opaque_ids_but_rejects_credential_shapes() -> None:
    assert platform_uid("signature-viewer") == "douyin:signature-viewer"
    assert platform_uid("sessionid=secret") == ""


@pytest.mark.asyncio
async def test_missing_bundled_bridge_degrades_without_starting_process(tmp_path) -> None:
    supervisor = BridgeProcessSupervisor(
        executable_path=tmp_path / "missing.exe",
        args_factory=lambda port: ["--port", str(port)],
    )

    state = await supervisor.start()

    assert state.ok is False
    assert state.last_error == "bundled bridge executable is missing"


@pytest.mark.asyncio
async def test_bridge_port_wait_does_not_block_async_runtime(tmp_path) -> None:
    executable = tmp_path / "bridge.exe"
    executable.write_bytes(b"")

    class _Process:
        pid = 123

        def poll(self):
            return None

    def wait_for_port(_port: int, _timeout: float) -> bool:
        time.sleep(0.25)
        return True

    supervisor = BridgeProcessSupervisor(
        executable_path=executable,
        args_factory=lambda port: ["--port", str(port)],
        process_factory=lambda *_args, **_kwargs: _Process(),
        port_factory=lambda: 12345,
        port_waiter=wait_for_port,
    )

    task = asyncio.create_task(supervisor.start())
    started = time.monotonic()
    await asyncio.sleep(0.02)

    assert time.monotonic() - started < 0.15
    assert (await task).ok is True


def test_stale_cleanup_targets_only_recorded_owned_pid(tmp_path, monkeypatch) -> None:
    executable = tmp_path / "douyinLive.exe"
    marker = tmp_path / "bridge.pid"
    marker.write_text("321", encoding="ascii")
    calls = []

    monkeypatch.setattr(supervisor_module.os, "name", "nt")
    monkeypatch.setattr(supervisor_module, "_ownership_marker_path", lambda _path: marker)
    monkeypatch.setattr(supervisor_module.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    cleanup_stale_windows_processes(executable)

    assert len(calls) == 1
    assert calls[0][1]["env"]["NEKO_BRIDGE_PROCESS_ID"] == "321"
    assert "ProcessId -eq $ownedPid" in calls[0][0][0][-1]
    assert not marker.exists()


@pytest.mark.asyncio
async def test_bridge_transport_enables_ping_timeout() -> None:
    connect_kwargs = {}

    class _Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Future()

    def connect_factory(_url, **kwargs):
        connect_kwargs.update(kwargs)
        return _Socket()

    adapter = SimpleNamespace(
        adapter_id="test",
        bridge_url=lambda _room_ref: "ws://127.0.0.1:12345/ws",
        map_message=lambda _message, room_ref: [],
    )
    transport = LiveBridgeTransport(connect_factory=connect_factory)

    state = await transport.start(LiveBridgeStartRequest(room_ref="123", adapter=adapter))

    assert state.state == "connected"
    assert connect_kwargs["ping_interval"] == 20
    assert connect_kwargs["ping_timeout"] == 20
    await transport.stop()
