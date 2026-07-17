from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugin.plugins.neko_roast.modules.twitch_identity import TwitchIdentityModule
from plugin.plugins.neko_roast.modules.twitch_live_ingest import TwitchLiveIngestModule
from plugin.plugins.neko_roast.modules.twitch_live_ingest.helix import lookup_channel_status
from plugin.plugins.neko_roast.modules.twitch_live_ingest.projection import project_chat_message


class _AsyncItems:
    def __init__(self, items: list[Any]) -> None:
        self.items = items

    def __aiter__(self):
        async def iterate():
            for item in self.items:
                yield item

        return iterate()


class _HelixClient:
    def __init__(self, *, users: list[Any], streams: list[Any]) -> None:
        self.users = users
        self.streams = streams
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def fetch_users(self, **kwargs: Any) -> list[Any]:
        self.calls.append(("users", kwargs))
        return self.users

    def fetch_streams(self, **kwargs: Any) -> _AsyncItems:
        self.calls.append(("streams", kwargs))
        return _AsyncItems(self.streams)


@pytest.mark.asyncio
async def test_helix_lookup_returns_live_channel_metadata_with_user_token_owner() -> None:
    client = _HelixClient(
        users=[SimpleNamespace(id="100", name="target_channel", display_name="Target Channel")],
        streams=[SimpleNamespace(title="Building a tiny robot", user_name="Target Channel", type="live")],
    )

    status = await lookup_channel_status(client, "TARGET_CHANNEL", token_for="42")

    assert status.ok is True
    assert status.room_id == 100
    assert status.title == "Building a tiny robot"
    assert status.anchor_name == "Target Channel"
    assert status.live_status == "live"
    assert client.calls == [
        ("users", {"logins": ["target_channel"], "token_for": "42"}),
        ("streams", {"user_ids": ["100"], "type": "live", "token_for": "42", "max_results": 1}),
    ]


@pytest.mark.asyncio
async def test_helix_lookup_distinguishes_offline_and_missing_channels() -> None:
    offline = _HelixClient(
        users=[SimpleNamespace(id="100", name="target_channel", display_name="Target Channel")],
        streams=[],
    )
    missing = _HelixClient(users=[], streams=[])

    offline_status = await lookup_channel_status(offline, "target_channel", token_for="42")
    missing_status = await lookup_channel_status(missing, "missing_channel", token_for="42")

    assert offline_status.ok is True
    assert offline_status.live_status == "offline"
    assert offline_status.anchor_name == "Target Channel"
    assert missing_status.ok is False
    assert missing_status.live_status == "unknown"
    assert missing_status.message == "twitch channel was not found"


def test_chat_message_projection_contains_only_pipeline_fields() -> None:
    message = SimpleNamespace(
        id="message-1",
        text="hello NEKO",
        chatter=SimpleNamespace(id="200", name="viewer_login", display_name="Viewer Name"),
        broadcaster=SimpleNamespace(id="100", name="target_channel", display_name="Target Channel"),
        access_token="must-not-leak",
    )

    event = project_chat_message(message, room_ref="target_channel", ts=123.5)

    assert event.type == "danmaku"
    assert event.uid == "twitch:200"
    assert event.source == "live"
    assert event.ts == 123.5
    assert event.raw is None
    assert event.payload == {
        "event_type": "danmaku",
        "uid": "twitch:200",
        "nickname": "Viewer Name",
        "chatter_login": "viewer_login",
        "danmaku_text": "hello NEKO",
        "text": "hello NEKO",
        "message_id": "message-1",
        "room_ref": "target_channel",
    }
    assert "must-not-leak" not in str(event.to_dict())


@pytest.mark.asyncio
async def test_module_normalize_and_identity_keep_twitch_uid_namespace() -> None:
    module = TwitchLiveIngestModule()
    identity_module = TwitchIdentityModule()

    viewer = module.normalize(
        {
            "uid": "twitch:200",
            "nickname": "Viewer Name",
            "chatter_login": "viewer_login",
            "danmaku_text": "hello NEKO",
            "event_type": "danmaku",
        }
    )
    identity = await identity_module.resolve(viewer)

    assert viewer.uid == "twitch:200"
    assert viewer.source == "live_danmaku"
    assert viewer.danmaku_text == "hello NEKO"
    assert identity.uid == "twitch:200"
    assert identity.source_url == "https://www.twitch.tv/viewer_login"
