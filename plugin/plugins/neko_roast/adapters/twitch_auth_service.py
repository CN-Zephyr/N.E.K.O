"""Twitch OAuth Device Code Flow with encrypted credential callbacks.

Twitch device codes are deliberately held only in this service instance. Access
and refresh tokens are handed to the injected credential store only after the
access token has been validated against Twitch.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


CredentialProvider = Callable[[], Awaitable[dict[str, Any] | None]]
CredentialSaver = Callable[[dict[str, Any]], Awaitable[bool]]
CredentialReloader = Callable[[], Awaitable[None]]
RequestJson = Callable[..., Awaitable[tuple[int, dict[str, Any]]]]

_DEVICE_URL = "https://id.twitch.tv/oauth2/device"
_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
_SCOPES = ("user:read:chat",)
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9]{8,80}$")


@dataclass(slots=True)
class _DeviceSession:
    client_id: str
    device_code: str
    user_code: str
    verification_uri: str
    expires_at: float
    expires_in: int
    interval: int


class TwitchAuthService:
    """One-shot Device Flow polling and token validation/refresh."""

    def __init__(
        self,
        *,
        credential_provider: CredentialProvider,
        credential_saver: CredentialSaver,
        credential_reloader: CredentialReloader,
        request_json: RequestJson | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._credential_provider = credential_provider
        self._credential_saver = credential_saver
        self._credential_reloader = credential_reloader
        self._request_json = request_json or _request_json
        self._clock = clock
        self._device_session: _DeviceSession | None = None

    async def start_device_authorization(self, client_id: Any) -> dict[str, Any]:
        normalized_client_id = _client_id(client_id)
        if not normalized_client_id:
            self._device_session = None
            return _error("invalid twitch client id")
        status, data = await self._request_json(
            "POST",
            _DEVICE_URL,
            data={"client_id": normalized_client_id, "scopes": " ".join(_SCOPES)},
        )
        device_code = _text(data.get("device_code"), limit=512)
        user_code = _text(data.get("user_code"), limit=64)
        verification_uri = _verification_uri(data.get("verification_uri"))
        expires_in = _positive_int(data.get("expires_in"), default=0, maximum=3600)
        interval = _positive_int(data.get("interval"), default=5, maximum=60)
        if status != 200 or not all((device_code, user_code, verification_uri, expires_in)):
            self._device_session = None
            return _error("twitch device authorization could not be started")
        self._device_session = _DeviceSession(
            client_id=normalized_client_id,
            device_code=device_code,
            user_code=user_code,
            verification_uri=verification_uri,
            expires_at=self._clock() + expires_in,
            expires_in=expires_in,
            interval=interval,
        )
        return {
            "platform": "twitch",
            "started": True,
            "pending": True,
            "user_code": user_code,
            "verification_uri": verification_uri,
            "expires_in": expires_in,
            "interval": interval,
        }

    async def check_device_authorization(self, client_id: Any) -> dict[str, Any]:
        normalized_client_id = _client_id(client_id)
        session = self._device_session
        if session is None or normalized_client_id != session.client_id:
            return _error("twitch device authorization is not active")
        if self._clock() >= session.expires_at:
            self._device_session = None
            return _error("twitch device authorization expired")
        status, data = await self._request_json(
            "POST",
            _TOKEN_URL,
            data={
                "client_id": session.client_id,
                "device_code": session.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        if status != 200:
            message = _oauth_error(data)
            if message in {"authorization_pending", "slow_down"}:
                return {
                    "platform": "twitch",
                    "pending": True,
                    "logged_in": False,
                    "message": message,
                    "interval": session.interval,
                }
            if message in {"expired_token", "invalid device code"}:
                self._device_session = None
            return _error("twitch authorization failed")
        credential = await self._validated_credential(session.client_id, data)
        if credential is None:
            self._device_session = None
            return _error("twitch access token validation failed")
        if not await self._credential_saver(credential):
            self._device_session = None
            return _error("twitch credential save failed")
        await self._credential_reloader()
        self._device_session = None
        return _public_status(credential, refreshed=False)

    async def check_credential(self, client_id: Any) -> dict[str, Any]:
        normalized_client_id = _client_id(client_id)
        if not normalized_client_id:
            return _error("invalid twitch client id")
        current = await self._credential_provider()
        access_token = _secret(current, "access_token")
        if not access_token:
            return _error("twitch authorization is required")
        validated = await self._validate_token(access_token, normalized_client_id)
        if validated is not None:
            merged = _merge_validated(current or {}, validated, clock=self._clock())
            return _public_status(merged, refreshed=False)
        refresh_token = _secret(current, "refresh_token")
        if not refresh_token:
            return _error("twitch authorization expired")
        status, token_data = await self._request_json(
            "POST",
            _TOKEN_URL,
            data={
                "client_id": normalized_client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        if status != 200:
            return _error("twitch token refresh failed")
        refreshed = await self._validated_credential(normalized_client_id, token_data)
        if refreshed is None:
            return _error("twitch refreshed token validation failed")
        if not await self._credential_saver(refreshed):
            return _error("twitch credential save failed")
        await self._credential_reloader()
        return _public_status(refreshed, refreshed=True)

    async def _validated_credential(self, client_id: str, token_data: dict[str, Any]) -> dict[str, Any] | None:
        access_token = _secret(token_data, "access_token")
        refresh_token = _secret(token_data, "refresh_token")
        if not access_token or not refresh_token:
            return None
        validated = await self._validate_token(access_token, client_id)
        if validated is None:
            return None
        return _merge_validated(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            validated,
            clock=self._clock(),
        )

    async def _validate_token(self, access_token: str, client_id: str) -> dict[str, Any] | None:
        status, data = await self._request_json(
            "GET",
            _VALIDATE_URL,
            headers={"Authorization": f"OAuth {access_token}"},
        )
        if status != 200 or _client_id(data.get("client_id")) != client_id:
            return None
        user_id = _text(data.get("user_id"), limit=64)
        login = _login(data.get("login"))
        scopes = _scopes(data.get("scopes"))
        if not user_id or not login or not set(_SCOPES).issubset(scopes):
            return None
        return {
            "client_id": client_id,
            "user_id": user_id,
            "login": login,
            "scopes": " ".join(sorted(scopes)),
            "expires_in": str(_positive_int(data.get("expires_in"), default=0, maximum=31_536_000)),
        }


async def _request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, url, headers=headers, data=data) as response:
            try:
                payload = await response.json(content_type=None)
            except Exception:
                payload = {}
            return response.status, payload if isinstance(payload, dict) else {}


def _merge_validated(current: dict[str, Any], validated: dict[str, Any], *, clock: float) -> dict[str, Any]:
    expires_in = _positive_int(validated.get("expires_in"), default=0, maximum=31_536_000)
    return {
        "access_token": _secret(current, "access_token"),
        "refresh_token": _secret(current, "refresh_token"),
        "client_id": _client_id(validated.get("client_id")),
        "user_id": _text(validated.get("user_id"), limit=64),
        "login": _login(validated.get("login")),
        "display_name": _text(current.get("display_name"), limit=80),
        "scopes": " ".join(sorted(_scopes(validated.get("scopes")))),
        "expires_at": str(int(clock + expires_in)),
    }


def _public_status(credential: dict[str, Any], *, refreshed: bool) -> dict[str, Any]:
    return {
        "platform": "twitch",
        "logged_in": True,
        "pending": False,
        "user_id": _text(credential.get("user_id"), limit=64),
        "login": _login(credential.get("login")),
        "display_name": _text(credential.get("display_name"), limit=80),
        "scopes": sorted(_scopes(credential.get("scopes"))),
        "expires_at": _text(credential.get("expires_at"), limit=24),
        "refreshed": refreshed is True,
    }


def _error(message: str) -> dict[str, Any]:
    return {
        "platform": "twitch",
        "logged_in": False,
        "pending": False,
        "message": message,
    }


def _client_id(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text if _CLIENT_ID_RE.fullmatch(text) else ""


def _secret(data: Any, key: str) -> str:
    if not isinstance(data, dict) or not isinstance(data.get(key), str):
        return ""
    return data[key].strip()


def _text(value: Any, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()[:limit]


def _login(value: Any) -> str:
    text = _text(value, limit=25).lower()
    return text if re.fullmatch(r"[a-z0-9_]{1,25}", text) else ""


def _scopes(value: Any) -> set[str]:
    if isinstance(value, str):
        items = value.split()
    elif isinstance(value, list):
        items = [item for item in value if isinstance(item, str)]
    else:
        items = []
    return {item.strip() for item in items if re.fullmatch(r"[a-z]+(?::[a-z]+)+", item.strip())}


def _positive_int(value: Any, *, default: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if 0 < number <= maximum else default


def _verification_uri(value: Any) -> str:
    text = _text(value, limit=200)
    return text if text in {"https://www.twitch.tv/activate", "https://twitch.tv/activate"} else ""


def _oauth_error(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    message = _text(data.get("message"), limit=80).lower()
    return message if message in {"authorization_pending", "slow_down", "expired_token", "invalid device code"} else ""
