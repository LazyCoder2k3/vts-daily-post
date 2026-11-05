"""Test StravaClient (respx mock httpx) và app.strava.tokens (DB in-memory, client giả)."""
from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from app.config import get_settings
from app.db.models import Athlete, OAuthToken
from app.security import decrypt, generate_key
from app.strava import tokens as tokens_mod
from app.strava.client import (StravaAuthError, StravaClient, StravaError, StravaNotFound, StravaRateLimited,
                               TokenResponse)

OAUTH = "https://www.strava.com/oauth"
API = "https://www.strava.com/api/v3"


def _form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


@pytest.fixture
def client():
    c = StravaClient(get_settings())
    c._retry_delay = 0   # không chờ 1 giây khi test retry 5xx
    return c


# --------------------------------------------------------------------------- OAuth
def test_authorize_url(client):
    url = client.authorize_url("abc123")
    parsed = urlparse(url)
    assert url.startswith(f"{OAUTH}/authorize?")
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert q == {
        "client_id": "1",
        "response_type": "code",
        "redirect_uri": "https://run.test/auth/callback",
        "approval_prompt": "auto",
        "scope": "read,activity:read",
        "state": "abc123",
    }


async def test_exchange_code(client, respx_mock):
    route = respx_mock.post(f"{OAUTH}/token").mock(return_value=httpx.Response(200, json={
        "token_type": "Bearer", "access_token": "at1", "refresh_token": "rt1",
        "expires_at": 1_900_000_000, "expires_in": 21600,
        "athlete": {"id": 42, "firstname": "An", "lastname": "Nguyễn"},
    }))
    tok = await client.exchange_code("thecode")
    assert route.called
    form = _form(route.calls.last.request)
    assert form == {"client_id": "1", "client_secret": "secret", "code": "thecode", "grant_type": "authorization_code"}
    assert tok == TokenResponse(access_token="at1", refresh_token="rt1", expires_at=1_900_000_000, scope="",
                                athlete={"id": 42, "firstname": "An", "lastname": "Nguyễn"})
    await client.aclose()


async def test_refresh(client, respx_mock):
    route = respx_mock.post(f"{OAUTH}/token").mock(return_value=httpx.Response(200, json={
        "access_token": "at2", "refresh_token": "rt2", "expires_at": 1_900_000_500,
    }))
    tok = await client.refresh("rt1")
    form = _form(route.calls.last.request)
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "rt1"
    assert tok.access_token == "at2" and tok.refresh_token == "rt2" and tok.expires_at == 1_900_000_500
    assert tok.athlete is None


async def test_revoke_ok_and_fallback_and_5xx(client, respx_mock):
    revoke = respx_mock.post(f"{OAUTH}/revoke").mock(return_value=httpx.Response(200, json={}))
    await client.revoke("at")
    assert _form(revoke.calls.last.request) == {"access_token": "at"}

    # 404 -> thử deauthorize; 4xx khác coi như đã thu hồi (không ném)
    revoke.mock(return_value=httpx.Response(404))
    deauth = respx_mock.post(f"{OAUTH}/deauthorize").mock(return_value=httpx.Response(401, json={"message": "x"}))
    await client.revoke("at")
    assert deauth.called
    assert deauth.calls.last.request.url.params["access_token"] == "at"

    # 5xx -> StravaError (sau 1 lần thử lại)
    revoke.mock(return_value=httpx.Response(503))
    with pytest.raises(StravaError):
        await client.revoke("at")


# --------------------------------------------------------------------------- API + lỗi
async def test_get_activity_bearer_and_rate_limit(client, respx_mock):
    route = respx_mock.get(f"{API}/activities/555").mock(return_value=httpx.Response(
        200, json={"id": 555, "name": "Morning Run"},
        headers={"X-RateLimit-Limit": "200,2000", "X-RateLimit-Usage": "37,412"},
    ))
    data = await client.get_activity("tok", 555)
    assert data["id"] == 555
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok"
    assert client.last_rate_limit == {"limit_15m": 200, "limit_day": 2000, "usage_15m": 37, "usage_day": 412}


async def test_rate_limit_header_tolerant(client, respx_mock):
    respx_mock.get(f"{API}/athlete").mock(return_value=httpx.Response(
        200, json={"id": 1}, headers={"X-RateLimit-Limit": "200", "X-RateLimit-Usage": "abc,5"}))
    await client.get_athlete("tok")
    assert client.last_rate_limit == {"limit_15m": 200, "limit_day": None, "usage_15m": None, "usage_day": 5}


async def test_401_auth_error(client, respx_mock):
    respx_mock.get(f"{API}/athlete").mock(return_value=httpx.Response(
        401, json={"message": "Authorization Error", "errors": [{"resource": "Athlete", "field": "access_token",
                                                                  "code": "invalid"}]}))
    with pytest.raises(StravaAuthError) as ei:
        await client.get_athlete("bad")
    assert ei.value.status == 401
    assert "Authorization Error" in str(ei.value)


async def test_403_auth_error(client, respx_mock):
    respx_mock.get(f"{API}/activities/1").mock(return_value=httpx.Response(403, json={"message": "Forbidden"}))
    with pytest.raises(StravaAuthError):
        await client.get_activity("tok", 1)


async def test_404_not_found(client, respx_mock):
    respx_mock.get(f"{API}/activities/999").mock(return_value=httpx.Response(404, json={"message": "Record Not Found"}))
    with pytest.raises(StravaNotFound) as ei:
        await client.get_activity("tok", 999)
    assert ei.value.status == 404


async def test_429_rate_limited(client, respx_mock):
    respx_mock.get(f"{API}/athlete/activities").mock(return_value=httpx.Response(
        429, json={"message": "Rate Limit Exceeded"},
        headers={"X-RateLimit-Limit": "200,2000", "X-RateLimit-Usage": "201,900"}))
    with pytest.raises(StravaRateLimited) as ei:
        await client.list_activities("tok")
    assert 0 < ei.value.retry_after <= 900
    assert ei.value.status == 429
    assert client.last_rate_limit["usage_15m"] == 201


async def test_5xx_retry_once_then_success(client, respx_mock):
    route = respx_mock.get(f"{API}/athlete").mock(side_effect=[
        httpx.Response(502, text="Bad Gateway"),
        httpx.Response(200, json={"id": 7}),
    ])
    assert await client.get_athlete("tok") == {"id": 7}
    assert route.call_count == 2


async def test_5xx_twice_raises(client, respx_mock):
    route = respx_mock.get(f"{API}/athlete").mock(return_value=httpx.Response(500, json={"message": "boom"}))
    with pytest.raises(StravaError) as ei:
        await client.get_athlete("tok")
    assert route.call_count == 2
    assert ei.value.status == 500
    assert not isinstance(ei.value, (StravaAuthError, StravaNotFound, StravaRateLimited))


async def test_network_error_retry_then_raise(client, respx_mock):
    route = respx_mock.get(f"{API}/athlete").mock(side_effect=httpx.ConnectTimeout("timeout"))
    with pytest.raises(StravaError):
        await client.get_athlete("tok")
    assert route.call_count == 2


async def test_list_activities_params(client, respx_mock):
    route = respx_mock.get(f"{API}/athlete/activities").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    out = await client.list_activities("tok", after=100, page=2, per_page=50)
    assert out == [{"id": 1}]
    params = route.calls.last.request.url.params
    assert params["after"] == "100" and params["page"] == "2" and params["per_page"] == "50"
    assert "before" not in params


async def test_list_all_activities_two_pages(client, respx_mock):
    def responder(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        per_page = int(request.url.params["per_page"])
        assert request.url.params["after"] == "1000"
        if page == 1:
            return httpx.Response(200, json=[{"id": i} for i in range(per_page)])
        if page == 2:
            return httpx.Response(200, json=[{"id": 9001}, {"id": 9002}])
        raise AssertionError("không được gọi trang 3")

    route = respx_mock.get(f"{API}/athlete/activities").mock(side_effect=responder)
    out = await client.list_all_activities("tok", after=1000)
    assert route.call_count == 2
    assert len(out) == 102 and out[-1] == {"id": 9002}


async def test_list_all_activities_max_pages(client, respx_mock):
    route = respx_mock.get(f"{API}/athlete/activities").mock(
        return_value=httpx.Response(200, json=[{"id": i} for i in range(100)]))
    out = await client.list_all_activities("tok", max_pages=3)
    assert route.call_count == 3 and len(out) == 300


async def test_get_athlete_clubs(client, respx_mock):
    respx_mock.get(f"{API}/athlete/clubs").mock(return_value=httpx.Response(200, json=[{"id": 5, "name": "VTS"}]))
    assert await client.get_athlete_clubs("tok") == [{"id": 5, "name": "VTS"}]


# --------------------------------------------------------------------------- webhook subscription
async def test_create_subscription(client, respx_mock):
    route = respx_mock.post(f"{API}/push_subscriptions").mock(return_value=httpx.Response(201, json={"id": 123}))
    out = await client.create_subscription("https://run.test/webhook", "vts-verify")
    assert out == {"id": 123}
    form = _form(route.calls.last.request)
    assert form == {"client_id": "1", "client_secret": "secret",
                    "callback_url": "https://run.test/webhook", "verify_token": "vts-verify"}
    assert "Authorization" not in route.calls.last.request.headers


async def test_list_and_delete_subscription(client, respx_mock):
    lst = respx_mock.get(f"{API}/push_subscriptions").mock(return_value=httpx.Response(200, json=[{"id": 123}]))
    assert await client.list_subscriptions() == [{"id": 123}]
    p = lst.calls.last.request.url.params
    assert p["client_id"] == "1" and p["client_secret"] == "secret"

    dele = respx_mock.delete(f"{API}/push_subscriptions/123").mock(return_value=httpx.Response(204))
    assert await client.delete_subscription(123) is None
    assert dele.calls.last.request.url.params["client_id"] == "1"


async def test_aclose_closes_own_http(client):
    http = client.http   # tạo lazily
    assert isinstance(http, httpx.AsyncClient)
    await client.aclose()
    assert http.is_closed and client._http is None

    own = httpx.AsyncClient()
    c2 = StravaClient(get_settings(), http=own)
    await c2.aclose()
    assert not own.is_closed   # không đóng http do người khác truyền vào
    await own.aclose()


# --------------------------------------------------------------------------- tokens
class FakeClient:
    """Client giả: chỉ có refresh()."""
    def __init__(self, result: TokenResponse | None = None, exc: Exception | None = None):
        self.result = result
        self.exc = exc
        self.calls: list[str] = []

    async def refresh(self, refresh_token: str) -> TokenResponse:
        self.calls.append(refresh_token)
        if self.exc:
            raise self.exc
        assert self.result is not None
        return self.result


async def _make_athlete(db, strava_id: int = 100, active: bool = True) -> Athlete:
    async with db() as s:
        a = Athlete(strava_id=strava_id, firstname="An", lastname="Nguyễn", active=active)
        s.add(a)
        await s.commit()
        return a


async def test_store_and_get_token_fresh(db):
    a = await _make_athlete(db)
    now = int(time.time())
    async with db() as s:
        athlete = await s.get(Athlete, a.id)
        tok = await tokens_mod.store_token(s, athlete, TokenResponse("at", "rt", now + 6 * 3600, scope="read,activity:read"))
        await s.commit()
    assert tok.athlete_id == a.id and tok.scope == "read,activity:read"

    fake = FakeClient()
    async with db() as s:
        athlete = await s.get(Athlete, a.id)
        assert await tokens_mod.get_valid_access_token(s, fake, athlete) == "at"
    assert fake.calls == []   # còn hạn -> không refresh


async def test_get_valid_access_token_refreshes_when_expiring(db):
    a = await _make_athlete(db)
    now = int(time.time())
    fake = FakeClient(TokenResponse("new_at", "new_rt", now + 21600))   # refresh không có scope
    async with db() as s:
        athlete = await s.get(Athlete, a.id)
        await tokens_mod.store_token(s, athlete, TokenResponse("old_at", "old_rt", now + 120, scope="read"))
        got = await tokens_mod.get_valid_access_token(s, fake, athlete)
        await s.commit()
    assert got == "new_at"
    assert fake.calls == ["old_rt"]
    async with db() as s:
        tok = await s.get(OAuthToken, a.id)
        assert tok.access_token == "new_at" and tok.refresh_token == "new_rt"
        assert tok.expires_at == now + 21600
        assert tok.scope == "read"   # giữ scope cũ


async def test_store_token_encrypts_when_key_set(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "token_encryption_key", generate_key())
    a = await _make_athlete(db)
    now = int(time.time())
    async with db() as s:
        athlete = await s.get(Athlete, a.id)
        await tokens_mod.store_token(s, athlete, TokenResponse("secret_at", "secret_rt", now + 6000))
        await s.commit()
    async with db() as s:
        tok = await s.get(OAuthToken, a.id)
        assert tok.access_token.startswith("enc:") and tok.refresh_token.startswith("enc:")
        assert decrypt(tok.access_token) == "secret_at"
        athlete = await s.get(Athlete, a.id)
        assert await tokens_mod.get_valid_access_token(s, FakeClient(), athlete) == "secret_at"


async def test_get_valid_access_token_errors(db):
    a = await _make_athlete(db)
    async with db() as s:
        athlete = await s.get(Athlete, a.id)
        with pytest.raises(StravaAuthError):
            await tokens_mod.get_valid_access_token(s, FakeClient(), athlete)   # chưa có token

    now = int(time.time())
    bad = StravaError("Strava HTTP 400: Bad Request")
    bad.status = 400
    fake = FakeClient(exc=bad)
    async with db() as s:
        athlete = await s.get(Athlete, a.id)
        await tokens_mod.store_token(s, athlete, TokenResponse("old_at", "old_rt", now - 10))
        await s.commit()
        with pytest.raises(StravaAuthError):
            await tokens_mod.get_valid_access_token(s, fake, athlete)   # refresh 4xx -> AuthError

    boom = StravaError("Strava HTTP 503")
    boom.status = 503
    async with db() as s:
        athlete = await s.get(Athlete, a.id)
        with pytest.raises(StravaError) as ei:
            await tokens_mod.get_valid_access_token(s, FakeClient(exc=boom), athlete)
        assert not isinstance(ei.value, StravaAuthError)   # 5xx giữ nguyên StravaError


async def test_refresh_expiring(db):
    now = int(time.time())
    a1 = await _make_athlete(db, 1)              # sắp hết hạn, active -> refresh
    a2 = await _make_athlete(db, 2)              # còn lâu -> bỏ qua
    a3 = await _make_athlete(db, 3, active=False)  # inactive -> bỏ qua
    a4 = await _make_athlete(db, 4)              # sắp hết hạn nhưng refresh lỗi -> chỉ log
    async with db() as s:
        for a, exp, rt in ((a1, now + 600, "rt1"), (a2, now + 20000, "rt2"), (a3, now + 10, "rt3"), (a4, now - 5, "rt4")):
            await tokens_mod.store_token(s, await s.get(Athlete, a.id), TokenResponse(f"at{a.strava_id}", rt, exp))
        await s.commit()

    class Fake:
        def __init__(self):
            self.calls = []

        async def refresh(self, refresh_token):
            self.calls.append(refresh_token)
            if refresh_token == "rt4":
                e = StravaError("Strava HTTP 400: invalid")
                e.status = 400
                raise e
            return TokenResponse("fresh_" + refresh_token, refresh_token, now + 21600)

    fake = Fake()
    async with db() as s:
        assert await tokens_mod.refresh_expiring(s, fake, within_s=1800) == 1
        await s.commit()
    assert sorted(fake.calls) == ["rt1", "rt4"]
    async with db() as s:
        assert (await s.get(OAuthToken, a1.id)).access_token == "fresh_rt1"
        assert (await s.get(OAuthToken, a2.id)).access_token == "at2"
        assert (await s.get(OAuthToken, a3.id)).access_token == "at3"
        assert (await s.get(OAuthToken, a4.id)).access_token == "at4"
