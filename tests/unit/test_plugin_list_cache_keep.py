"""plugin_list_provider must not let a failed fetch wipe the last good cache.

Provider contract: list on success (empty list = no running plugin, clears the
cache), None on failure (keep the previous cache).
"""
import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import brain.task_executor as te
from brain.task_executor import DirectTaskExecutor


def _make_executor(cached, provider=None):
    # The cached list was just fetched and no change signal is wired, so it is
    # fresh: a failed refresh may keep it (a stale one is dropped, see
    # test_plugin_list_turn_cache).
    executor = object.__new__(DirectTaskExecutor)
    executor.plugin_list = list(cached)
    executor._external_plugin_provider = provider
    executor._plugin_list_change_token = None
    executor._plugin_list_fetched_at = te._monotonic()
    executor._plugin_list_fetched_token = None
    executor._short_desc_cache = {}
    executor._short_desc_prewarm_inflight = set()
    executor._short_desc_prewarm_tasks = set()
    return executor


GOOD = [{"id": "a", "short_description": "A"}]


@pytest.fixture(autouse=True)
def _no_prewarm():
    with patch.object(DirectTaskExecutor, "_schedule_short_desc_prewarm"):
        yield


async def test_external_failure_keeps_previous_cache():
    executor = _make_executor(GOOD, AsyncMock(return_value=None))
    result = await executor.plugin_list_provider(force_refresh=True)
    assert result == GOOD
    assert executor.plugin_list == GOOD


async def test_external_legit_empty_clears_cache():
    executor = _make_executor(GOOD, AsyncMock(return_value=[]))
    result = await executor.plugin_list_provider(force_refresh=True)
    assert result == []
    assert executor.plugin_list == []


async def test_external_success_replaces_cache():
    new = [{"id": "b", "short_description": "B"}]
    executor = _make_executor(GOOD, AsyncMock(return_value=new))
    result = await executor.plugin_list_provider(force_refresh=True)
    assert result == new
    assert executor.plugin_list == new


def _patch_fallback_http(handler):
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("proxy", None)
        kwargs.pop("trust_env", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    return patch("brain.task_executor.httpx.AsyncClient", side_effect=factory)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"detail": "boom"}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"unexpected": True}),
    ],
)
async def test_fallback_http_failure_keeps_previous_cache(response):
    executor = _make_executor(GOOD)
    with _patch_fallback_http(lambda request: response):
        result = await executor.plugin_list_provider(force_refresh=True)
    assert result == GOOD


async def test_fallback_http_transport_error_keeps_previous_cache():
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    executor = _make_executor(GOOD)
    with _patch_fallback_http(handler):
        result = await executor.plugin_list_provider(force_refresh=True)
    assert result == GOOD


async def test_fallback_http_legit_empty_clears_cache():
    executor = _make_executor(GOOD)
    with _patch_fallback_http(lambda request: httpx.Response(200, json={"plugins": []})):
        result = await executor.plugin_list_provider(force_refresh=True)
    assert result == []


async def test_fallback_http_success_replaces_cache():
    new = [{"id": "b"}]
    executor = _make_executor(GOOD)
    with _patch_fallback_http(lambda request: httpx.Response(200, json={"plugins": new})):
        result = await executor.plugin_list_provider(force_refresh=True)
    assert result == new


# ── Overlapping refreshes: an older request finishing last must not win ──
#
# /plugin/execute runs outside analyze_lock, so its force_refresh can overlap
# an analyze turn (or another direct execute). A starts first, B second; each
# response is released explicitly so the completion order is deterministic.

class _Gate:
    def __init__(self, results):
        self.results = list(results)  # per call, in start order; None = failure
        self.started = [asyncio.Event() for _ in self.results]
        self.release = [asyncio.Event() for _ in self.results]
        self.calls = 0

    async def wait(self):
        idx = self.calls
        self.calls += 1
        self.started[idx].set()
        await self.release[idx].wait()
        return idx, self.results[idx]


def _external_source(gate):
    async def provider(force_refresh):
        _, result = await gate.wait()
        return None if result is None else list(result)

    return provider, contextlib.nullcontext()


def _http_source(gate):
    async def handler(request):
        _, result = await gate.wait()
        if result is None:
            return httpx.Response(500, json={"detail": "boom"})
        return httpx.Response(200, json={"plugins": list(result)})

    return None, _patch_fallback_http(handler)


A_LIST = [{"id": "a"}]
B_LIST = [{"id": "b"}]


async def _race(monkeypatch, source, results):
    """Start A then B, finish B then A; return (executor, A's result, prewarms)."""
    now = {"t": 1000.0}
    monkeypatch.setattr(te, "_monotonic", lambda: now["t"])
    token = {"v": 0}

    def token_fn():
        token["v"] += 1  # every read differs: identifies which request published
        return token["v"]

    gate = _Gate(results)
    provider, ctx = source(gate)
    executor = _make_executor(GOOD, provider)
    executor._plugin_list_change_token = token_fn
    prewarms = []
    monkeypatch.setattr(
        executor, "_schedule_short_desc_prewarm",
        lambda plugins: prewarms.append(list(plugins)), raising=False,
    )
    with ctx:
        task_a = asyncio.create_task(executor.plugin_list_provider(force_refresh=True))
        await gate.started[0].wait()
        task_b = asyncio.create_task(executor.plugin_list_provider(force_refresh=True))
        await gate.started[1].wait()
        now["t"] = 1005.0
        gate.release[1].set()
        await task_b
        now["t"] = 1010.0
        gate.release[0].set()
        result_a = await task_a
    return executor, result_a, prewarms


SOURCES = pytest.mark.parametrize("source", [_external_source, _http_source], ids=["external", "http"])


@SOURCES
async def test_older_success_after_newer_success_is_discarded(monkeypatch, source):
    executor, result_a, prewarms = await _race(monkeypatch, source, [A_LIST, B_LIST])
    assert executor.plugin_list == B_LIST
    # A is not published, but its own caller gets what A fetched: start order
    # does not tell which response the server built later.
    assert result_a == A_LIST
    assert executor._plugin_list_fetched_at == 1005.0
    assert executor._plugin_list_fetched_token == 2  # B's pre-fetch token
    assert prewarms == [B_LIST]  # A did not prewarm


@SOURCES
async def test_older_success_after_newer_failure_publishes(monkeypatch, source):
    executor, result_a, prewarms = await _race(monkeypatch, source, [A_LIST, None])
    assert executor.plugin_list == A_LIST
    assert result_a == A_LIST
    assert executor._plugin_list_fetched_at == 1010.0
    assert executor._plugin_list_fetched_token == 1  # A's pre-fetch token
    assert prewarms == [A_LIST]


@SOURCES
async def test_newer_empty_success_is_not_overwritten(monkeypatch, source):
    executor, result_a, prewarms = await _race(monkeypatch, source, [A_LIST, []])
    assert executor.plugin_list == []
    # e.g. a plugin started between B's and A's server-side reads: A's caller
    # (an analyze turn) must still see the running plugin it fetched.
    assert result_a == A_LIST
    assert prewarms == [[]]
