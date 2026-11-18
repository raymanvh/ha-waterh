"""Minimal async client for the (unofficial) WaterH cloud API.

Only the endpoints the integration needs are implemented. See docs/API.md.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .const import API_BASE_URL, APP_VERSION

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Refresh a little before the server-side expiry.
TOKEN_EXPIRY_MARGIN = 300


class WaterHError(Exception):
    """Base error for the WaterH API."""


class WaterHConnectionError(WaterHError):
    """The API could not be reached or returned an unexpected response."""


class WaterHAuthError(WaterHError):
    """Credentials or tokens were rejected."""


class WaterHRateLimitError(WaterHError):
    """The API returned HTTP 429."""


@dataclass
class WaterHTokens:
    """OAuth tokens returned by /account/oauth/token/."""

    access_token: str
    refresh_token: str
    expires_at: float

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> WaterHTokens:
        try:
            return cls(
                access_token=data["access_token"],
                refresh_token=data["refresh_token"],
                expires_at=time.time() + float(data.get("expires_in", 3600)),
            )
        except (KeyError, TypeError, ValueError) as err:
            raise WaterHConnectionError(f"Unexpected token response: {err}") from err

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at - TOKEN_EXPIRY_MARGIN


@dataclass
class WaterHDrink:
    """One entry from /account/{id}/log."""

    id: str
    water_amount: int
    amount: int
    created: datetime
    source_id: str | None
    mac_address: str | None
    water_type: str | None
    temperature: float | None
    tds: int | None

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> WaterHDrink:
        return cls(
            id=str(item.get("id")),
            water_amount=int(item.get("water_amount") or 0),
            amount=int(item.get("amount") or 0),
            created=parse_api_datetime(item["date_created"]),
            source_id=item.get("source_id"),
            mac_address=item.get("mac_address"),
            water_type=item.get("water_type"),
            temperature=_to_float(item.get("temperature")),
            tds=item.get("tds"),
        )


def _to_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def parse_api_datetime(value: str) -> datetime:
    """Parse timestamps like '2026-11-18T00:22:40.000000Z' (always UTC)."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


TokenCallback = Callable[[WaterHTokens], Awaitable[None] | None]


class WaterHApiClient:
    """Async WaterH API client with automatic token refresh."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        client_id: str,
        client_secret: str,
        tokens: WaterHTokens | None = None,
        on_tokens_updated: TokenCallback | None = None,
        time_zone: str | None = None,
    ) -> None:
        self._session = session
        self._client_id = client_id
        self._client_secret = client_secret
        self._tokens = tokens
        self._on_tokens_updated = on_tokens_updated
        self._time_zone = time_zone
        self._refresh_lock = asyncio.Lock()

    @property
    def tokens(self) -> WaterHTokens | None:
        return self._tokens

    def _headers(self, auth: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "X-App-Version": APP_VERSION,
        }
        if self._time_zone:
            headers["Time-Zone"] = self._time_zone
        if auth and self._tokens:
            headers["Authorization"] = f"Bearer {self._tokens.access_token}"
        return headers

    async def _raw_request(
        self,
        method: str,
        path: str,
        *,
        auth: bool,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        url = f"{API_BASE_URL}{path}"
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                json=json,
                headers=self._headers(auth),
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    body = None
                return status, body
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise WaterHConnectionError(f"Error talking to WaterH: {err}") from err

    async def _token_request(self, payload: dict[str, Any]) -> WaterHTokens:
        payload = {
            **payload,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        status, body = await self._raw_request(
            "POST", "/account/oauth/token/", auth=False, json=payload
        )
        if status in (400, 401, 403):
            raise WaterHAuthError(_error_detail(body) or f"HTTP {status}")
        if status == 429:
            raise WaterHRateLimitError("Rate limited by WaterH")
        if status != 200 or not isinstance(body, dict):
            raise WaterHConnectionError(f"Token request failed: HTTP {status}")
        tokens = WaterHTokens.from_response(body)
        await self._set_tokens(tokens)
        return tokens

    async def _set_tokens(self, tokens: WaterHTokens) -> None:
        self._tokens = tokens
        if self._on_tokens_updated:
            result = self._on_tokens_updated(tokens)
            if asyncio.iscoroutine(result):
                await result

    async def async_login(self, email: str, password: str) -> WaterHTokens:
        """Log in with email + password (OAuth password grant)."""
        return await self._token_request(
            {"email": email, "password": password, "grant_type": "password"}
        )

    async def async_refresh(self) -> WaterHTokens:
        """Exchange the refresh token for a new access token."""
        if not self._tokens:
            raise WaterHAuthError("No refresh token available")
        return await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": self._tokens.refresh_token,
            }
        )

    async def _ensure_token(self) -> None:
        if not self._tokens:
            raise WaterHAuthError("Not logged in")
        if self._tokens.expired:
            async with self._refresh_lock:
                if self._tokens.expired:
                    await self.async_refresh()

    async def _authed_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        await self._ensure_token()
        status, body = await self._raw_request(
            method, path, auth=True, params=params, json=json
        )
        if status == 401:
            # Token revoked or expired early; try one refresh.
            async with self._refresh_lock:
                await self.async_refresh()
            status, body = await self._raw_request(
                method, path, auth=True, params=params, json=json
            )
            if status == 401:
                raise WaterHAuthError("Access token rejected after refresh")
        if status == 429:
            raise WaterHRateLimitError("Rate limited by WaterH")
        if status not in (200, 201, 204):
            detail = _error_detail(body)
            raise WaterHConnectionError(
                f"{method} {path} failed: HTTP {status}"
                + (f" ({detail})" if detail else "")
            )
        return body

    async def async_get_self(self) -> dict[str, Any]:
        """Return the logged-in user's profile (id, goal, ...)."""
        body = await self._authed_request("GET", "/account/self/")
        if not isinstance(body, dict) or "id" not in body:
            raise WaterHConnectionError("Unexpected /account/self/ response")
        return body

    async def async_get_log(
        self, user_id: int, start: datetime, end: datetime, period: str = "daily"
    ) -> list[WaterHDrink]:
        """Return drinks logged between start and end (inclusive window)."""
        body = await self._authed_request(
            "GET",
            f"/account/{user_id}/log",
            params={
                "period": period,
                "date_finished_from": int(start.timestamp()),
                "date_finished_to": int(end.timestamp()),
            },
        )
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise WaterHConnectionError("Unexpected log response")
        drinks: list[WaterHDrink] = []
        for item in body["data"]:
            try:
                drinks.append(WaterHDrink.from_api(item))
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.debug("Skipping unparseable log entry %s: %s", item, err)
        drinks.sort(key=lambda d: d.created)
        return drinks


    async def async_add_drink(
        self,
        user_id: int,
        amount_ml: int,
        water_type: str = "water",
        when: datetime | None = None,
    ) -> None:
        """Log a manual drink, the same way the app's "+" button does.

        Matches the app's request exactly (captured): source_id, mac_address,
        tds and temperature are omitted and the server marks the entry
        source "manual". Responds 201 with the stored entry.
        """
        created = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
        entry = {
            "amount": int(amount_ml),
            "date_created": format_api_datetime(created),
            "water_type": water_type,
        }
        await self._authed_request(
            "POST", f"/account/{user_id}/log", json={"data": [entry], "ble_data": []}
        )

    async def async_delete_drink(self, drink_id: str) -> None:
        """Delete a log entry by id."""
        await self._authed_request("DELETE", f"/v2/log/{drink_id}")

    async def async_set_goal(self, goal_ml: int) -> dict[str, Any]:
        """Set the daily goal (mL) on the user profile."""
        body = await self._authed_request(
            "PATCH", "/account/self/", json={"goal": int(goal_ml)}
        )
        return body if isinstance(body, dict) else {}


def format_api_datetime(value: datetime) -> str:
    """Format like the app's WaterLogDateAdapter: yyyy-MM-dd'T'HH:mm:ssXXX in UTC."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _error_detail(body: Any) -> str | None:
    if isinstance(body, dict):
        for key in ("error_description", "detail", "error", "non_field_errors"):
            if body.get(key):
                return str(body[key])
    return None
