"""Trợ giúp chung cho tests/test_services_*.py: JSON hoạt động giả, engine giả, client Strava giả, fixture."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.db.models import Athlete, OAuthToken, utcnow
from app.rules.schema import EvalResult
from app.strava.client import StravaNotFound, TokenResponse

LOCAL_OFFSET_H = 7   # giờ VN


def activity_json(aid: int, *, start_local: str = "2027-01-05T06:00:00Z", km: float = 5.0, sport: str = "Run",
                  has_map: bool = True, moving: int | None = None, elapsed: int | None = None,
                  name: str = "Morning Run", **extra) -> dict:
    """JSON kiểu Strava (DetailedActivity/SummaryActivity) đủ trường cho ActivityData.from_strava."""
    local = datetime.fromisoformat(start_local.replace("Z", ""))
    utc = local - timedelta(hours=LOCAL_OFFSET_H)
    moving = moving if moving is not None else int(km * 360)   # pace 6:00/km
    elapsed = elapsed if elapsed is not None else moving + 30
    d = {
        "id": aid, "name": name, "sport_type": sport, "type": sport,
        "start_date": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "start_date_local": local.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timezone": "(GMT+07:00) Asia/Bangkok",
        "distance": km * 1000.0, "moving_time": moving, "elapsed_time": elapsed,
        "average_speed": (km * 1000.0 / moving) if moving else 0.0, "total_elevation_gain": 10.0,
        "map": {"id": f"a{aid}", "summary_polyline": "abc_polyline" if has_map else ""},
        "manual": False, "trainer": False, "flagged": False, "private": False, "visibility": "everyone",
        "device_name": "Garmin", "external_id": f"ext{aid}", "upload_id": aid * 10, "commute": False,
        "athlete": {"id": 777},
    }
    d.update(extra)
    return d


def epoch_of(d: dict) -> int:
    return int(datetime.fromisoformat(d["start_date"].replace("Z", "+00:00")).timestamp())


# ----------------------------------------------------------------- engine giả
def fake_evaluate(activity, rules, ctx=None) -> EvalResult:
    """Luật giả: hợp lệ nếu có bản đồ, >= 1 km và không phải VirtualRide; Ride quy đổi 0.25."""
    raw_km = activity.distance_m / 1000.0
    reasons: list[str] = []
    if activity.sport_type == "VirtualRide":
        reasons.append("TYPE_EXCLUDED:VirtualRide")
    if not activity.has_map:
        reasons.append("NO_GPS_MAP")
    if raw_km < 1.0:
        reasons.append("BELOW_MIN_DISTANCE")
    factor = 0.25 if activity.sport_type == "Ride" else 1.0
    key = "ride" if activity.sport_type == "Ride" else "run"
    valid = not reasons
    pace = (activity.moving_s / raw_km) if raw_km else None
    return EvalResult(valid=valid, reasons=reasons, converted_km=raw_km * factor if valid else 0.0,
                      raw_km=raw_km, sport_key=key, pace_s=pace)


def accept_all(activity, rules, ctx=None) -> EvalResult:
    raw_km = activity.distance_m / 1000.0
    return EvalResult(valid=True, reasons=[], converted_km=raw_km, raw_km=raw_km, sport_key="run",
                      pace_s=(activity.moving_s / raw_km) if raw_km else None)


# ----------------------------------------------------------------- client Strava giả
class FakeClient:
    def __init__(self):
        self.activities: dict[int, dict] = {}
        self.clubs: list[dict] = []
        self.clubs_error: Exception | None = None
        self.revoked: list[str] = []
        self.fail_get: Exception | None = None
        self.athlete = {"id": 777, "firstname": "An", "lastname": "Nguyen", "profile": "https://img/p.jpg", "sex": "M"}
        self.calls: list[tuple] = []

    async def exchange_code(self, code: str) -> TokenResponse:
        self.calls.append(("exchange_code", code))
        return TokenResponse(access_token=f"acc-{code}", refresh_token=f"ref-{code}",
                             expires_at=int(utcnow().timestamp()) + 21600, scope="", athlete=dict(self.athlete))

    async def get_activity(self, access_token: str, activity_id: int) -> dict:
        self.calls.append(("get_activity", access_token, activity_id))
        if self.fail_get is not None:
            raise self.fail_get
        if activity_id not in self.activities:
            raise StravaNotFound(f"activity {activity_id} not found")
        return dict(self.activities[activity_id])

    async def list_all_activities(self, access_token: str, *, after=None, before=None, max_pages: int = 20) -> list[dict]:
        self.calls.append(("list_all_activities", after, before))
        out = []
        for d in self.activities.values():
            t = epoch_of(d)
            if after is not None and t <= after:
                continue
            if before is not None and t >= before:
                continue
            out.append(dict(d))
        out.sort(key=epoch_of, reverse=True)   # Strava trả mới nhất trước
        return out

    async def get_athlete_clubs(self, access_token: str) -> list[dict]:
        if self.clubs_error is not None:
            raise self.clubs_error
        return list(self.clubs)

    async def revoke(self, access_token: str) -> None:
        self.revoked.append(access_token)


# ----------------------------------------------------------------- fixture
@pytest.fixture
def fake_engine(monkeypatch):
    """Thay app.rules.engine.evaluate bằng luật giả; trả module để test đổi luật giữa chừng."""
    import app.rules.engine as eng
    monkeypatch.setattr(eng, "evaluate", fake_evaluate)
    return eng


@pytest.fixture
def fake_tokens(monkeypatch):
    """app.strava.tokens: get_valid_access_token -> 'tok'; store_token ghi OAuthToken thô."""
    import app.strava.tokens as tok

    async def get_valid(session, client, athlete) -> str:
        return "tok"

    async def store(session, athlete, tr):
        row = await session.get(OAuthToken, athlete.id)
        if row is None:
            row = OAuthToken(athlete_id=athlete.id, access_token=tr.access_token, refresh_token=tr.refresh_token,
                             expires_at=tr.expires_at, scope=tr.scope or "")
            session.add(row)
        else:
            row.access_token, row.refresh_token = tr.access_token, tr.refresh_token
            row.expires_at, row.scope = tr.expires_at, tr.scope or ""
        await session.flush()
        return row

    monkeypatch.setattr(tok, "get_valid_access_token", get_valid)
    monkeypatch.setattr(tok, "store_token", store)
    return tok


async def make_athlete(session, *, strava_id: int = 777, telegram_id: int | None = 5001, name: str = "An Nguyen",
                       with_token: bool = True, sex: str | None = "M") -> Athlete:
    first, _, last = name.partition(" ")
    a = Athlete(strava_id=strava_id, firstname=first, lastname=last, display_name=name, telegram_id=telegram_id,
                telegram_username=f"u{telegram_id}" if telegram_id else None, sex=sex, consent_at=utcnow(), active=True)
    session.add(a)
    await session.flush()
    if with_token:
        session.add(OAuthToken(athlete_id=a.id, access_token=f"acc-{strava_id}", refresh_token="ref",
                               expires_at=int(utcnow().timestamp()) + 21600, scope="read,activity:read"))
        await session.flush()
    return a


async def make_event(session, name: str = "Giải Tết 2027", start: date = date(2027, 1, 1), end: date = date(2027, 1, 31),
                     by: int = 1000):
    from app.services import events
    return await events.create_event(session, name, start, end, by=by)
