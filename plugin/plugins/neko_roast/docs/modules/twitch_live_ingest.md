# twitch_live_ingest

`twitch_live_ingest` is NEKO Live's first read-only Twitch slice. It uses
`twitchio==3.2.2` for Helix and EventSub WebSocket delivery, while NEKO owns the
client-secret-free Device Code Flow and encrypted token persistence.

## Supported flow

1. Create a Twitch Developer application and copy its Client ID. A Client Secret
   is not requested or stored.
2. Start device authorization, open `https://www.twitch.tv/activate`, enter the
   displayed user code, and manually check authorization in the panel.
3. Enter any target channel login or a canonical
   `https://www.twitch.tv/<login>` URL. The authorized account and target channel
   are independent.
4. Query the channel and start listening. Helix supplies online/offline metadata;
   EventSub `channel.chat.message` supplies read-only chat events.

Only the `user:read:chat` scope is requested. Access and refresh tokens are
stored in the `twitch` namespace of `CredentialStore`, encrypted at rest. Device
codes remain in memory. TwitchIO is always started with
`load_tokens=False`, `save_tokens=False`, and `with_adapter=False`, so it never
creates `.tio.tokens.json` or starts its OAuth web adapter. A TwitchIO token
refresh callback must replace both rotated tokens through the encrypted store;
if that save fails, the listener stops in `auth_required` state.

## Public event boundary

The provider subscribes only to `channel.chat.message`. A message is projected
to `LiveEvent(type="danmaku")` with bounded public fields: Twitch-prefixed user
ID, display name, channel login, text, message ID, and normalized room reference.
The TwitchIO object and raw EventSub payload are not retained. The shared
EventBus, pipeline, safety guard, and dispatcher remain unchanged.

## Deliberately out of scope

- Twitch homepage, discovery, recommendations, or followed-channel feeds
- bits, subscriptions, raids, redemptions, or other EventSub types
- sending chat messages or any other Twitch write operation
- background Device Flow polling
- bundled Client ID or Client Secret

Rollback is provider-local: stop/unregister `twitch_live_ingest` and
`twitch_identity`; the Bilibili and Douyin providers and the shared pipeline do
not need to change. Encrypted `twitch_credential.*` files can be removed through
the Twitch logout action.
