"""plugin_list_provider must not let a failed fetch wipe the last good cache.

Provider contract: list on success (empty list = no running plugin, clears the
cache), None on failure (keep the previous cache).
"""
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
