"""Strava API client — CHỦ SỞ HỮU: agent `strava`. Chữ ký công khai (không đổi).

Mọi request tới `strava_api_base` mang header `Authorization: Bearer <token>` (sẵn sàng cho đổi base URL 2027).
Lỗi HTTP được quy về các exception bên dưới; 5xx / lỗi mạng thử lại 1 lần sau `_retry_delay` giây.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

_TIMEOUT_S = 30.0
_RATE_WINDOW_S = 900  # Strava reset usage mỗi 15 phút (:00, :15, :30, :45)


class StravaError(Exception):
    """Lỗi chung từ Strava (status, message)."""
    status: int | None = None


class StravaAuthError(StravaError):
    """401/403 — token hỏng hoặc athlete đã thu hồi quyền."""


class StravaNotFound(StravaError):
    """404 — hoạt động không tồn tại / không có quyền xem."""


class StravaRateLimited(StravaError):
    """429 — vượt rate limit; `retry_after` giây (ước tính)."""
    def __init__(self, msg: str = "rate limited", retry_after: int = 900):
        super().__init__(msg)
        self.retry_after = retry_after


@dataclass
class TokenResponse:
    access_token: str
    refresh_token: str
    expires_at: int            # epoch giây
    scope: str = ""
    athlete: dict | None = None  # chỉ có khi exchange_code


def _seconds_to_next_window(now: float | None = None) -> int:
    """Số giây tới mốc 15 phút kế tiếp (lúc Strava reset usage)."""
    now_i = int(now if now is not None else time.time())
    rest = _RATE_WINDOW_S - (now_i % _RATE_WINDOW_S)
    return rest if rest > 0 else _RATE_WINDOW_S


def _pair(value: str | None) -> tuple[int | None, int | None]:
    """'200,2000' -> (200, 2000); chịu lỗi với header thiếu/hỏng."""
    if not value:
        return None, None
    parts = [p.strip() for p in value.split(",")]
    out: list[int | None] = []
    for p in parts[:2]:
        try:
            out.append(int(p))
        except ValueError:
            out.append(None)
    while len(out) < 2:
        out.append(None)
    return out[0], out[1]


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        msg = data.get("message") or ""
        errors = data.get("errors")
        if isinstance(errors, list) and errors:
            detail = "; ".join(
                f"{e.get('resource', '')}.{e.get('field', '')}: {e.get('code', '')}" for e in errors if isinstance(e, dict)
            )
            msg = f"{msg} ({detail})" if msg else detail
        if msg:
            return msg
    text = (resp.text or "").strip()
    return text[:200] if text else f"HTTP {resp.status_code}"


def _make_error(resp: httpx.Response) -> StravaError:
    status = resp.status_code
    msg = f"Strava HTTP {status}: {_error_message(resp)}"
    err: StravaError
    if status in (401, 403):
        err = StravaAuthError(msg)
    elif status == 404:
        err = StravaNotFound(msg)
    elif status == 429:
        err = StravaRateLimited(msg, retry_after=_seconds_to_next_window())
    else:
        err = StravaError(msg)
    err.status = status
    return err


class StravaClient:
    _retry_delay: float = 1.0   # giây chờ trước khi thử lại 5xx / lỗi mạng (test đặt = 0)

    def __init__(self, settings: Settings | None = None, http: httpx.AsyncClient | None = None):
        self.settings = settings or get_settings()
        self._http = http
        self._owns_http = http is None
        self.last_rate_limit: dict | None = None   # {"limit_15m":..,"usage_15m":..,"limit_day":..,"usage_day":..}

    # ------------------------------------------------------------------ hạ tầng
    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(_TIMEOUT_S))
            self._owns_http = True
        return self._http

    @property
    def api_base(self) -> str:
        return self.settings.strava_api_base.rstrip("/")

    @property
    def oauth_base(self) -> str:
        return self.settings.strava_oauth_base.rstrip("/")

    def _record_rate_limit(self, resp: httpx.Response) -> None:
        limit = resp.headers.get("X-RateLimit-Limit")
        usage = resp.headers.get("X-RateLimit-Usage")
        if not limit and not usage:
            return
        l15, lday = _pair(limit)
        u15, uday = _pair(usage)
        self.last_rate_limit = {"limit_15m": l15, "limit_day": lday, "usage_15m": u15, "usage_day": uday}

    async def _send(self, method: str, url: str, *, token: str | None = None, params: dict | None = None,
                    data: dict | None = None) -> httpx.Response:
        """Gửi request, ghi rate limit; 5xx hoặc lỗi mạng thử lại 1 lần. Không ném theo status (trừ lỗi mạng)."""
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if params:
            params = {k: v for k, v in params.items() if v is not None}
        last_exc: Exception | None = None
        resp: httpx.Response | None = None
        for attempt in range(2):
            try:
                resp = await self.http.request(method, url, headers=headers, params=params or None, data=data)
            except httpx.HTTPError as e:   # timeout, connect error, ...
                last_exc = e
                log.warning("Strava %s %s lỗi mạng (lần %d): %s", method, url, attempt + 1, e)
                resp = None
            else:
                self._record_rate_limit(resp)
                if resp.status_code < 500:
                    return resp
                log.warning("Strava %s %s trả %d (lần %d)", method, url, resp.status_code, attempt + 1)
            if attempt == 0 and self._retry_delay > 0:
                await asyncio.sleep(self._retry_delay)
        if resp is not None:
            return resp
        err = StravaError(f"Không kết nối được Strava: {last_exc}")
        raise err from last_exc

    async def _request(self, method: str, url: str, *, token: str | None = None, params: dict | None = None,
                       data: dict | None = None) -> Any:
        resp = await self._send(method, url, token=token, params=params, data=data)
        if resp.status_code >= 400:
            raise _make_error(resp)
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as e:
            err = StravaError(f"Strava trả dữ liệu không phải JSON (HTTP {resp.status_code})")
            err.status = resp.status_code
            raise err from e

    def _parse_token(self, data: Any, *, with_athlete: bool) -> TokenResponse:
        if not isinstance(data, dict) or "access_token" not in data:
            raise StravaError("Phản hồi token từ Strava không hợp lệ")
        expires_at = data.get("expires_at")
        if expires_at is None:
            expires_at = int(time.time()) + int(data.get("expires_in") or 0)
        athlete = data.get("athlete") if with_athlete else None
        return TokenResponse(
            access_token=str(data["access_token"]),
            refresh_token=str(data.get("refresh_token") or ""),
            expires_at=int(expires_at),
            scope=str(data.get("scope") or ""),
            athlete=athlete if isinstance(athlete, dict) else None,
        )

    # ------------------------------------------------------------------ OAuth
    def authorize_url(self, state: str) -> str:
        params = {
            "client_id": self.settings.strava_client_id,
            "response_type": "code",
            "redirect_uri": self.settings.redirect_uri,
            "approval_prompt": "auto",
            "scope": self.settings.strava_scope,
            "state": state,
        }
        return f"{self.oauth_base}/authorize?{urlencode(params)}"

    async def exchange_code(self, code: str) -> TokenResponse:
        data = await self._request("POST", f"{self.oauth_base}/token", data={
            "client_id": self.settings.strava_client_id,
            "client_secret": self.settings.strava_client_secret,
            "code": code,
            "grant_type": "authorization_code",
        })
        return self._parse_token(data, with_athlete=True)

    async def refresh(self, refresh_token: str) -> TokenResponse:
        data = await self._request("POST", f"{self.oauth_base}/token", data={
            "client_id": self.settings.strava_client_id,
            "client_secret": self.settings.strava_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })
        return self._parse_token(data, with_athlete=False)

    async def revoke(self, access_token: str) -> None:
        """Thu hồi token. 4xx coi như đã thu hồi (token đã hỏng / athlete đã gỡ app); 5xx ném StravaError."""
        resp = await self._send("POST", f"{self.oauth_base}/revoke", data={"access_token": access_token})
        if resp.status_code == 404:
            # máy chủ cũ chưa có /revoke -> dùng endpoint deauthorize (bị bỏ từ 06/2027)
            resp = await self._send("POST", f"{self.oauth_base}/deauthorize", params={"access_token": access_token})
        if resp.status_code >= 500:
            raise _make_error(resp)
        if resp.status_code >= 400:
            log.info("Strava revoke trả %d — coi như token đã bị thu hồi", resp.status_code)

    # ------------------------------------------------------------------ API athlete
    async def get_activity(self, access_token: str, activity_id: int) -> dict:
        data = await self._request("GET", f"{self.api_base}/activities/{int(activity_id)}", token=access_token)
        return data if isinstance(data, dict) else {}

    async def list_activities(self, access_token: str, *, after: int | None = None, before: int | None = None,
                              page: int = 1, per_page: int = 100) -> list[dict]:
        data = await self._request("GET", f"{self.api_base}/athlete/activities", token=access_token,
                                   params={"after": after, "before": before, "page": page, "per_page": per_page})
        return list(data) if isinstance(data, list) else []

    async def list_all_activities(self, access_token: str, *, after: int | None = None,
                                  before: int | None = None, max_pages: int = 20) -> list[dict]:
        per_page = 100
        out: list[dict] = []
        for page in range(1, max(1, max_pages) + 1):
            batch = await self.list_activities(access_token, after=after, before=before, page=page, per_page=per_page)
            if not batch:
                break
            out.extend(batch)
            if len(batch) < per_page:
                break
        return out

    async def get_athlete(self, access_token: str) -> dict:
        data = await self._request("GET", f"{self.api_base}/athlete", token=access_token)
        return data if isinstance(data, dict) else {}

    async def get_athlete_clubs(self, access_token: str) -> list[dict]:
        data = await self._request("GET", f"{self.api_base}/athlete/clubs", token=access_token)
        return list(data) if isinstance(data, list) else []

    # ------------------------------------------------------------------ webhook subscription (dùng client_id/secret)
    def _app_creds(self) -> dict:
        return {"client_id": self.settings.strava_client_id, "client_secret": self.settings.strava_client_secret}

    async def list_subscriptions(self) -> list[dict]:
        data = await self._request("GET", f"{self.api_base}/push_subscriptions", params=self._app_creds())
        return list(data) if isinstance(data, list) else []

    async def create_subscription(self, callback_url: str, verify_token: str) -> dict:
        data = await self._request("POST", f"{self.api_base}/push_subscriptions", data={
            **self._app_creds(), "callback_url": callback_url, "verify_token": verify_token,
        })
        return data if isinstance(data, dict) else {}

    async def delete_subscription(self, subscription_id: int) -> None:
        await self._request("DELETE", f"{self.api_base}/push_subscriptions/{int(subscription_id)}",
                            params=self._app_creds())

    async def aclose(self) -> None:
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None
