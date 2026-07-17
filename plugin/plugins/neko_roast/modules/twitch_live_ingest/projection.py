"""Project TwitchIO events into NEKO Live's credential-free envelope."""

from __future__ import annotations

import time
from typing import Any

from ...core.contracts import LiveEvent
from .room_ref import parse_twitch_room_ref


def project_chat_message(message: Any, *, room_ref: Any, ts: float | None = None) -> LiveEvent | None:
    parsed = parse_twitch_room_ref(room_ref)
    chatter = getattr(message, "chatter", None)
    chatter_id = _numeric_id(getattr(chatter, "id", None))
    login = _login(getattr(chatter, "name", None))
    nickname = _text(getattr(chatter, "display_name", None), 80) or login
    text = _text(getattr(message, "text", None), 500)
    if not parsed.ok or not chatter_id or not login or not text:
        return None
    uid = f"twitch:{chatter_id}"
    payload = {
        "event_type": "danmaku",
        "uid": uid,
        "nickname": nickname,
        "chatter_login": login,
        "danmaku_text": text,
        "text": text,
        "message_id": _text(getattr(message, "id", None), 80),
        "room_ref": parsed.room_ref,
    }
    return LiveEvent(
        type="danmaku",
        uid=uid,
        payload=payload,
        source="live",
        ts=float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else time.time(),
        raw=None,
    )


def _numeric_id(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text if text.isascii() and text.isdigit() and len(text) <= 32 else ""


def _login(value: Any) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    return text if 0 < len(text) <= 25 and text.isascii() and text.replace("_", "").isalnum() else ""


def _text(value: Any, limit: int) -> str:
    return " ".join(value.split()).strip()[:limit] if isinstance(value, str) else ""
