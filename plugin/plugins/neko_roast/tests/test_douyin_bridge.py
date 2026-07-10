from types import SimpleNamespace

import pytest

from plugin.plugins.neko_roast.core.runtime_douyin_auth import normalize_cookie
from plugin.plugins.neko_roast.modules.douyin_live_ingest import DouyinLiveIngestModule
from plugin.plugins.neko_roast.modules.douyin_live_ingest.bridge_adapter import (
    DouyinLiveBridgeAdapter,
)
from plugin.plugins.neko_roast.modules.douyin_live_ingest.room_ref import (
    parse_douyin_room_ref,
)
from plugin.plugins.neko_roast.modules.live_bridge.process_supervisor import (
    BridgeProcessSupervisor,
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


@pytest.mark.asyncio
async def test_missing_bundled_bridge_degrades_without_starting_process(tmp_path) -> None:
    supervisor = BridgeProcessSupervisor(
        executable_path=tmp_path / "missing.exe",
        args_factory=lambda port: ["--port", str(port)],
    )

    state = await supervisor.start()

    assert state.ok is False
    assert state.last_error == "bundled bridge executable is missing"
