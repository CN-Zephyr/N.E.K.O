"""TwitchIO client configured for NEKO-owned token persistence."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import twitchio


class NekoTwitchClient(twitchio.Client):
    def __init__(
        self,
        *,
        client_id: str,
        on_message: Callable[[Any], Awaitable[None]],
        on_chat_notification: Callable[[Any], Awaitable[None]],
        on_token_refreshed: Callable[[Any], Awaitable[None]],
    ) -> None:
        super().__init__(
            client_id=client_id,
            client_secret="",
            fetch_client_user=False,
        )
        self._neko_on_message = on_message
        self._neko_on_chat_notification = on_chat_notification
        self._neko_on_token_refreshed = on_token_refreshed

    async def event_message(self, payload: Any) -> None:
        await self._neko_on_message(payload)

    async def event_chat_notification(self, payload: Any) -> None:
        await self._neko_on_chat_notification(payload)

    async def event_token_refreshed(self, payload: Any) -> None:
        await self._neko_on_token_refreshed(payload)


def create_twitch_client(**kwargs: Any) -> NekoTwitchClient:
    return NekoTwitchClient(**kwargs)
