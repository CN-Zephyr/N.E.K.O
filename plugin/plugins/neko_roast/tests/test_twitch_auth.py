from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugin.plugins.neko_roast.adapters.twitch_auth_service import TwitchAuthService
from plugin.plugins.neko_roast.core import runtime_twitch_auth


class _Store:
    def __init__(self, credential: dict[str, Any] | None = None, *, save_ok: bool = True) -> None:
        self.credential = credential
        self.save_ok = save_ok
        self.saved: list[dict[str, Any]] = []

    async def load(self) -> dict[str, Any] | None:
        return dict(self.credential) if self.credential else None

    async def save(self, payload: dict[str, Any]) -> bool:
        self.saved.append(dict(payload))
        if self.save_ok:
            self.credential = dict(payload)
        return self.save_ok


class _Http:
    def __init__(self, responses: list[tuple[int, dict[str, Any]]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append({"method": method, "url": url, "headers": headers or {}, "data": data or {}})
        return self.responses.pop(0)


def _service(store: _Store, http: _Http) -> TwitchAuthService:
    async def reload() -> None:
        return None

    return TwitchAuthService(
        credential_provider=store.load,
        credential_saver=store.save,
        credential_reloader=reload,
        request_json=http,
        clock=lambda: 1_700_000_000.0,
    )


@pytest.mark.asyncio
async def test_device_authorization_stays_in_memory_and_pending_check_is_public() -> None:
    store = _Store()
    http = _Http(
        [
            (
                200,
                {
                    "device_code": "secret-device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://www.twitch.tv/activate",
                    "expires_in": 900,
                    "interval": 5,
                },
            ),
            (400, {"message": "authorization_pending"}),
        ]
    )
    service = _service(store, http)

    started = await service.start_device_authorization("clientid123")
    pending = await service.check_device_authorization("clientid123")

    assert started == {
        "platform": "twitch",
        "started": True,
        "pending": True,
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://www.twitch.tv/activate",
        "expires_in": 900,
        "interval": 5,
    }
    assert "device_code" not in started
    assert pending["pending"] is True
    assert pending["logged_in"] is False
    assert "secret-device-code" not in str(pending)
    assert store.saved == []
    assert http.calls[1]["data"]["device_code"] == "secret-device-code"


@pytest.mark.asyncio
async def test_device_authorization_success_validates_then_encrypts_tokens() -> None:
    store = _Store()
    http = _Http(
        [
            (
                200,
                {
                    "device_code": "secret-device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://www.twitch.tv/activate",
                    "expires_in": 900,
                    "interval": 5,
                },
            ),
            (200, {"access_token": "new-access", "refresh_token": "new-refresh", "scope": ["user:read:chat"]}),
            (
                200,
                {
                    "client_id": "clientid123",
                    "login": "account_login",
                    "user_id": "42",
                    "scopes": ["user:read:chat"],
                    "expires_in": 14400,
                },
            ),
        ]
    )
    service = _service(store, http)

    await service.start_device_authorization("clientid123")
    result = await service.check_device_authorization("clientid123")

    assert result["logged_in"] is True
    assert result["login"] == "account_login"
    assert result["user_id"] == "42"
    assert result["scopes"] == ["user:read:chat"]
    assert "access_token" not in result
    assert store.saved[0]["access_token"] == "new-access"
    assert store.saved[0]["refresh_token"] == "new-refresh"
    assert store.saved[0]["expires_at"] == "1700014400"


@pytest.mark.asyncio
async def test_invalid_access_token_refreshes_and_replaces_one_time_refresh_token() -> None:
    old = {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "client_id": "clientid123",
        "user_id": "42",
        "login": "account_login",
        "display_name": "Account Login",
        "scopes": "user:read:chat",
        "expires_at": "1",
    }
    store = _Store(old)
    http = _Http(
        [
            (401, {"status": 401, "message": "invalid access token"}),
            (200, {"access_token": "fresh-access", "refresh_token": "fresh-refresh", "scope": ["user:read:chat"]}),
            (
                200,
                {
                    "client_id": "clientid123",
                    "login": "account_login",
                    "user_id": "42",
                    "scopes": ["user:read:chat"],
                    "expires_in": 3600,
                },
            ),
        ]
    )
    service = _service(store, http)

    result = await service.check_credential("clientid123")

    assert result["logged_in"] is True
    assert result["refreshed"] is True
    assert store.credential is not None
    assert store.credential["access_token"] == "fresh-access"
    assert store.credential["refresh_token"] == "fresh-refresh"
    assert "old-refresh" not in str(result)


@pytest.mark.asyncio
async def test_failed_refresh_save_keeps_cached_old_credential_and_returns_no_secret() -> None:
    old = {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "client_id": "clientid123",
        "user_id": "42",
        "login": "account_login",
        "display_name": "Account Login",
        "scopes": "user:read:chat",
        "expires_at": "1",
    }
    store = _Store(old, save_ok=False)
    http = _Http(
        [
            (401, {"message": "invalid access token"}),
            (200, {"access_token": "fresh-access", "refresh_token": "fresh-refresh", "scope": ["user:read:chat"]}),
            (
                200,
                {
                    "client_id": "clientid123",
                    "login": "account_login",
                    "user_id": "42",
                    "scopes": ["user:read:chat"],
                    "expires_in": 3600,
                },
            ),
        ]
    )
    service = _service(store, http)

    result = await service.check_credential("clientid123")

    assert result["logged_in"] is False
    assert result["message"] == "twitch credential save failed"
    assert store.credential == old
    assert "fresh-access" not in str(result)
    assert "fresh-refresh" not in str(result)


@pytest.mark.asyncio
async def test_runtime_twitch_store_is_namespaced_encrypted_and_logout_clears_cache(tmp_path: Path) -> None:
    plugin = SimpleNamespace(data_path=lambda: str(tmp_path))
    audit = SimpleNamespace(record=lambda *_args, **_kwargs: None)
    store = runtime_twitch_auth.create_credential_store(plugin, audit)
    runtime = SimpleNamespace(
        twitch_credential_store=store,
        twitch_credential=None,
        audit=audit,
    )
    payload = {
        "access_token": "secret-access",
        "refresh_token": "secret-refresh",
        "client_id": "clientid123",
        "user_id": "42",
        "login": "account_login",
        "display_name": "Account Login",
        "scopes": "user:read:chat",
        "expires_at": "1700014400",
    }

    assert await store.save(payload) is True
    await runtime_twitch_auth.reload_credential(runtime)

    encrypted = (tmp_path / "twitch_credential.enc").read_bytes()
    assert b"secret-access" not in encrypted
    assert b"secret-refresh" not in encrypted
    assert runtime.twitch_credential == payload

    result = await runtime_twitch_auth.logout(runtime)

    assert result["logged_out"] is True
    assert runtime.twitch_credential is None
    assert not (tmp_path / "twitch_credential.enc").exists()
    assert not (tmp_path / "twitch_credential.key").exists()
