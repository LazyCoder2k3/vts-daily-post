"""Test router FastAPI (app/api.py) qua httpx ASGITransport; services được monkeypatch."""
from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from app import deps
from app.api import router
from app.config import get_settings
from app.db.models import Athlete, WebhookEvent
from app.services import activities as activities_svc
from app.services import linking as linking_svc
from app.strava.client import StravaClient, StravaError


@pytest_asyncio.fixture
async def api(db):
    """Client HTTP nói chuyện với app FastAPI nhỏ chỉ include router; DB in-memory từ conftest."""
    app = FastAPI()
    app.include_router(router)
    deps.set_client(StravaClient(get_settings()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    deps.set_client(None)


async def _add_athlete(db, telegram_id: int | None = 777) -> Athlete:
    async with db() as s:
        a = Athlete(strava_id=4242, firstname="Thái", lastname="Nguyễn", telegram_id=telegram_id)
        s.add(a)
        await s.commit()
        return a


# --------------------------------------------------------------------------- health
async def test_health(api):
    r = await api.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "T" in body["time"]   # ISO 8601


# --------------------------------------------------------------------------- OAuth redirect
async def test_connect_redirects_to_strava(api):
    r = await api.get("/connect/tok123")
    assert r.status_code == 307
    loc = r.headers["location"]
    assert "strava.com/oauth/authorize" in loc
    assert "state=tok123" in loc
    assert "redirect_uri=https%3A%2F%2Frun.test%2Fauth%2Fcallback" in loc


async def test_auth_strava_redirect_and_missing_state(api):
    r = await api.get("/auth/strava", params={"state": "s1"})
    assert r.status_code == 307 and "state=s1" in r.headers["location"]
    r = await api.get("/auth/strava")
    assert r.status_code == 400
    assert "text/html" in r.headers["content-type"]


# --------------------------------------------------------------------------- OAuth callback
async def test_auth_callback_success_runs_backfill_and_dm(api, db, monkeypatch, sent_messages):
    athlete = await _add_athlete(db)
    seen: dict = {}

    async def fake_complete_link(session, client, token, code, scope):
        seen["link"] = (token, code, scope)
        return await session.get(Athlete, athlete.id)

    async def fake_backfill(session, client, a, event=None, *, notify=True):
        seen["backfill"] = (a.id, notify)
        return 3

    monkeypatch.setattr(linking_svc, "complete_link", fake_complete_link)
    monkeypatch.setattr(activities_svc, "backfill_athlete", fake_backfill)

    r = await api.get("/auth/callback", params={"code": "c0de", "state": "st4te", "scope": "read,activity:read"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Kết nối Strava thành công" in r.text
    assert "Thái Nguyễn" in r.text
    assert seen["link"] == ("st4te", "c0de", "read,activity:read")
    # background task đã chạy xong trước khi ASGITransport trả về
    assert seen["backfill"] == (athlete.id, False)
    assert sent_messages == [(777, "✅ Đã kết nối Strava (Thái Nguyễn). Đã nạp 3 hoạt động trong giải. Gõ /me để xem.")]


async def test_auth_callback_backfill_error_still_dms(api, db, monkeypatch, sent_messages):
    athlete = await _add_athlete(db)

    async def fake_complete_link(session, client, token, code, scope):
        return await session.get(Athlete, athlete.id)

    async def fake_backfill(session, client, a, event=None, *, notify=True):
        raise StravaError("Strava HTTP 500")

    monkeypatch.setattr(linking_svc, "complete_link", fake_complete_link)
    monkeypatch.setattr(activities_svc, "backfill_athlete", fake_backfill)
    r = await api.get("/auth/callback", params={"code": "c", "state": "s"})
    assert r.status_code == 200
    assert len(sent_messages) == 1 and sent_messages[0][0] == 777
    assert "Đã kết nối Strava" in sent_messages[0][1]


async def test_auth_callback_denied(api):
    r = await api.get("/auth/callback", params={"error": "access_denied", "state": "x"})
    assert r.status_code == 200
    assert "Bạn đã từ chối cấp quyền" in r.text


async def test_auth_callback_missing_code(api):
    r = await api.get("/auth/callback", params={"state": "x"})
    assert r.status_code == 400


async def test_auth_callback_expired_link(api, db, monkeypatch):
    async def fake_complete_link(session, client, token, code, scope):
        raise ValueError("token hết hạn")

    monkeypatch.setattr(linking_svc, "complete_link", fake_complete_link)
    r = await api.get("/auth/callback", params={"code": "c", "state": "old"})
    assert r.status_code == 400
    assert "Liên kết hết hạn" in r.text and "/connect" in r.text


async def test_auth_callback_strava_error(api, db, monkeypatch):
    async def fake_complete_link(session, client, token, code, scope):
        raise StravaError("Strava HTTP 503")

    monkeypatch.setattr(linking_svc, "complete_link", fake_complete_link)
    r = await api.get("/auth/callback", params={"code": "c", "state": "s"})
    assert r.status_code == 502
    assert "Strava" in r.text


# --------------------------------------------------------------------------- webhook verify
async def test_webhook_verify_ok(api):
    r = await api.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "vts-verify",
                                          "hub.challenge": "15f7d1a91c1f40f8a748fd134752feb3"})
    assert r.status_code == 200
    assert r.json() == {"hub.challenge": "15f7d1a91c1f40f8a748fd134752feb3"}


async def test_webhook_verify_wrong_token(api):
    r = await api.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "sai", "hub.challenge": "x"})
    assert r.status_code == 403
    r = await api.get("/webhook", params={"hub.mode": "subscribe", "hub.challenge": "x"})
    assert r.status_code == 403


# --------------------------------------------------------------------------- webhook event
PAYLOAD = {"object_type": "activity", "object_id": 1360128428, "aspect_type": "create", "owner_id": 134815,
           "event_time": 1516126040, "updates": {}, "subscription_id": 120475}


async def test_webhook_post_stores_event_and_dispatches(api, db, monkeypatch):
    handled: list[tuple[int, str, int]] = []

    async def fake_handle(session, client, event):
        assert isinstance(client, StravaClient)
        handled.append((event.id, event.aspect_type, event.object_id))
        assert event.payload == PAYLOAD
        event.processed_at = __import__("app.db.models", fromlist=["utcnow"]).utcnow()

    monkeypatch.setattr(activities_svc, "handle_webhook_event", fake_handle)
    r = await api.post("/webhook", json=PAYLOAD)
    assert r.status_code == 200 and r.json() == {"ok": True}

    async with db() as s:
        events = (await s.execute(select(WebhookEvent))).scalars().all()
    assert len(events) == 1
    ev = events[0]
    assert (ev.object_type, ev.object_id, ev.aspect_type, ev.owner_id, ev.event_time) == \
        ("activity", 1360128428, "create", 134815, 1516126040)
    assert ev.payload == PAYLOAD
    assert ev.processed_at is not None            # handler đã chạy và commit
    assert handled == [(ev.id, "create", 1360128428)]


async def test_webhook_post_handler_error_is_recorded(api, db, monkeypatch):
    async def fake_handle(session, client, event):
        raise RuntimeError("nổ")

    monkeypatch.setattr(activities_svc, "handle_webhook_event", fake_handle)
    r = await api.post("/webhook", json={**PAYLOAD, "aspect_type": "update"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    async with db() as s:
        ev = (await s.execute(select(WebhookEvent))).scalars().one()
    assert ev.error and "nổ" in ev.error
    assert ev.processed_at is None


async def test_webhook_post_bad_body(api, db, monkeypatch):
    called = []

    async def fake_handle(session, client, event):
        called.append(event)

    monkeypatch.setattr(activities_svc, "handle_webhook_event", fake_handle)
    r = await api.post("/webhook", content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 200 and r.json() == {"ok": False}
    r = await api.post("/webhook", json=["list", "khong", "phai", "object"])
    assert r.status_code == 200 and r.json() == {"ok": False}
    r = await api.post("/webhook", json={"object_type": "activity"})   # thiếu trường
    assert r.status_code == 200 and r.json() == {"ok": False}
    assert called == []
    async with db() as s:
        assert (await s.execute(select(WebhookEvent))).scalars().all() == []


async def test_webhook_post_athlete_deauthorize(api, db, monkeypatch):
    got = []

    async def fake_handle(session, client, event):
        got.append((event.object_type, event.aspect_type, event.payload.get("updates")))

    monkeypatch.setattr(activities_svc, "handle_webhook_event", fake_handle)
    body = {"object_type": "athlete", "object_id": 134815, "aspect_type": "update", "owner_id": 134815,
            "event_time": 1516126040, "updates": {"authorized": "false"}, "subscription_id": 120475}
    r = await api.post("/webhook", content=json.dumps(body), headers={"content-type": "application/json"})
    assert r.json() == {"ok": True}
    assert got == [("athlete", "update", {"authorized": "false"})]
