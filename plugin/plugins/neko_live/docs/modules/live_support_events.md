# live_support_events Module

## Purpose

`live_support_events` builds the NEKO reply request for Gift, Super Chat, and guard events received from the EventBus support lane. It exists so verified support events no longer fall through as ordinary danmaku or signal-only skipped results.

The module asks for one short appreciative line. It must not ask viewers for more gifts, SC, or guards; it must not create a ceremony, ranking, or reward promise.

## Owner And Contracts

- Module owner: `plugin.plugins.neko_live.modules.live_support_events.LiveSupportEventsModule`
- Input contract: a `LiveEvent` whose authoritative outer `type` is `gift`, `super_chat`, or `guard`; provider `raw` data may enrich fields but cannot downgrade that verified outer type.
- Output contract: returns an `InteractionRequest` for the normal pipeline and dispatcher path.
- Metadata contract: request metadata exposes `support_event_type`, `support_event_tier`, and `support_event_label`.

## Data Flow

The provider ingest publishes a normalized `LiveEvent` to EventBus. `live_support_events` subscribes to `gift`, `super_chat`, and `guard`, projects only public support fields, preserves the event `trace_id`, and calls `ctx.handle_live_payload(payload)` without waiting for the ordinary danmaku selection window.

Before that call, verified support events enter one session-scoped scheduler. The scheduler serializes support replies, orders only pending items by fixed priority, merges `COMBO_SEND` updates, and deduplicates provider deliveries by a validated `provider_event_id`. It never interrupts a request or TTS line that has already started.

`core/pipeline_routing.py` detects support event types before first-appearance or repeat-danmaku routing and selects `response_module_id="live_support_events"`.

`core/pipeline_requests.py` calls `ctx.live_support_events.build_request(event, identity, profile)`. The resulting request reuses recent context, viewer preference prompts, and live-event context, but sets `allow_avatar_image=False`.

## Safety Boundary

This module does not push messages directly. Support-event replies still pass through identity/profile preparation, pipeline steps, `safety_guard`, `neko_dispatcher`, audit records, `dry_run`, and runtime timeline projection.

Raw Bilibili payloads are not exposed. `ViewerEvent.to_dict()` only projects support summary fields such as gift name, gift count, coin totals, and guard level.

Ordinary danmaku is never promoted to this module from text alone. Text that merely claims a gift or support action remains unverified danmaku and is blocked from thanks-style confirmation by the danmaku/output guards.

## Scheduling Contract

- Milestone: Super Chat and Guard events.
- High: verified Bilibili gold gifts with `gift_value >= 10000`.
- Medium: verified Bilibili gold gifts with `1000 <= gift_value < 10000`.
- Light: silver, free, unknown, and lower-value gifts.
- Priority changes the next pending support event only. Active Pipeline or TTS work is not cancelled for priority.
- Equal priorities remain FIFO by local submission sequence.
- `provider_event_id` is the authoritative dedupe key when present. An event removed from the pending queue by a higher priority releases its provider ID (and combo tombstone, when applicable), because it was never dispatched and must remain retryable. `COMBO_SEND` is stateful: an identical delivery is ignored, while a monotonic count/value update with the same provider ID is allowed to advance the active combo. The short content fingerprint remains only an ingest fallback for callbacks without an event ID.
- `COMBO_SEND` updates share `(room, viewer, combo_id)` state, keep the maximum observed count/value, and finalize once on explicit end or after one second without growth. Identity fields from the first packet are immutable; conflicting updates fail closed. Active combos and timer tasks are bounded, while finalized combo keys stay in a bounded 10-minute/4,096-entry tombstone cache.
- Queue pressure admits a higher-priority event by removing the oldest pending event from the lowest available lower tier; this includes allowing a milestone to replace a pending high-value gift. Light events aggregate only when they have no authoritative provider event ID and their room, viewer, gift, coin type, and provider event type all match. Identified events remain individually retryable instead of entering an aggregate whose dedupe ownership cannot be recovered after eviction. No priority may exceed the hard pending limit (maximum 100); when no compatible aggregate or lower-priority victim exists, the newest event is rejected and reflected in aggregate overflow/drop counters.
- Dispatch is retried once, then recorded as `support.dispatch_failed`; subsequent support events continue normally. Audit-store failures are isolated from scheduling so an unavailable diagnostic side channel cannot strand the support queue.
- Starting, changing, or ending a live session clears queue, combo timers, finalized keys, and processed IDs. Cancelled workers remain tracked until `wait_idle()`/`close()` confirms they have exited; after `close()` the scheduler is sealed and rejects any late submission through a stale reference.

## Trusted Support Event Ledger

### Purpose

The in-memory `SupportEventScheduler` is augmented by a persistent `SupportLedger` that records every successful dispatch as a de-sensitized, cross-platform entry. This enables dashboard summary queries and developer sandbox audit without exposing raw payloads.

### `on_dispatched` Callback

`SupportEventScheduler` accepts an optional `on_dispatched` callback parameter:

```python
class SupportEventScheduler:
    def __init__(
        self,
        *,
        dispatch: Callable[[dict[str, Any]], Awaitable[None]],
        on_dispatched: Callable[[dict[str, Any]], Awaitable[None]] | None = None,  # ← 新增
        ...
    )
```

The callback fires immediately after a successful `dispatch` (no exception), with the same payload dict. It runs in the scheduler's worker task context but **must not block** — the scheduler does not await it before processing the next queue item. The ledger uses this seam as its write trigger.

### `SupportLedger` Lifecycle

| Phase | Action |
|-------|--------|
| `setup()` | Initialize `SupportLedger(data_dir)`, attach it as `on_dispatched` to the scheduler |
| `teardown()` | Call `ledger.flush_and_close()`, then `ledger = None` |
| `reset()` | Call `ledger.close_and_clear()` (drops current session's un-flushed buffer, keeps on-disk data) |

```python
class LiveSupportEventsModule(BaseModule):
    async def setup(self, ctx):
        ...
        self._ledger = SupportLedger(data_dir=ctx.data_path("support_ledger"))
        self._scheduler = SupportEventScheduler(
            dispatch=self._handle_payload,
            on_dispatched=self._ledger.record,  # ← 接入账本
            ...
        )
```

### Degradation on Write Failure

- `SupportLedger.record()` catches all exceptions internally and records them as audit warnings
- A write failure **never blocks** the scheduler or dispatcher
- After 10 consecutive write failures, the ledger stops retrying until the next `setup()`, and emits one final `audit.record("support_ledger_failed", ..., level="error")`
- Dashboard reflects this as `ledger_unavailable: true` in the module status

### Query Interface

Two query methods exposed on `LiveSupportEventsModule`:

```python
def get_ledger_summary(self, room_id: int, session_generation: int | None = None) -> SupportLedgerSummary:
    """Aggregated per-room summary (total gifts/SC/guard, top users, total CNY value).
       Uses control.json pre-aggregated cache; falls back to file scan if cache is stale."""

def get_ledger_entries(
    self,
    room_id: int,
    event_type: str | None = None,
    uid: str | None = None,
    session_generation: int | None = None,
    limit: int = 50,
) -> list[SupportLedgerEntry]:
    """Paginated detail query, de-sensitized (no provider_event_id, trace_id, or raw payload)."""
```

These are also exposed as a developer-mode `@plugin_entry` (`query_support_ledger`) for chat-tool use.

### Lifecycle Relationship

| Event | Scheduler behavior | Ledger behavior |
|-------|-------------------|-----------------|
| `setup()` | Create scheduler | Create ledger, attach `on_dispatched` |
| start listening | Queue/combo/processed IDs cleared | Buffer stays; on-disk data preserved |
| stop listening | Same as start | Same as start |
| `reset()` | Queue/combo/processed IDs cleared | `close_and_clear()` → flush buffer, stop timers |
| `close()` | Scheduler sealed | `flush_and_close()` → flush buffer, seal ledger |
| `teardown()` | Scheduler destroyed | Ledger destroyed |

### Cross-Platform Value Normalization

`SupportLedger._to_entry()` computes `value_cny` at write time:

| Source | Rule |
|--------|------|
| Bilibili gold | `value_cny = gift_value / 1000` |
| Bilibili silver | `value_cny = 0.0` |
| Other providers | Provider ingest layer supplies pre-computed `value_cny` |

This ensures `SupportLedgerSummary.total_cny` is a meaningful cross-platform total.

## Limitations

- Entry/follow events are still out of scope.
- The module only produces short thanks-style replies; it does not implement contribution rankings, reward logic, or privileged viewer treatment.
- The first fixed monetary thresholds currently use Bilibili's normalized `gold` coin totals. Other providers remain light unless their typed bridge supplies an equivalent verified coin contract.
- The persistent ledger is append-only and bounded (100k entries / 30 days / 50 rooms). It does not support cross-room aggregation, lifetime statistics, or financial reconciliation.

## Testing

Run:

```powershell
uv run pytest plugin/plugins/neko_live/tests/test_runtime_live_controls.py::test_handle_live_payload_routes_gift_to_support_events plugin/plugins/neko_live/tests/test_runtime_live_controls.py::test_handle_live_payload_routes_support_events_through_pipeline -q
uv run pytest plugin/plugins/neko_live/tests/test_live_events.py plugin/plugins/neko_live/tests/test_bili_listener_lifecycle.py -q
uv run pytest plugin/plugins/neko_live/tests/test_live_support_scheduler.py -q
```

The broader solo-stream simulation covers Gift and SC flowing through `live_support_events` together with ordinary danmaku and hosting routes.
