"""Test app.services.stats: ranking, daily, athlete_summary, team_totals, activity_log, export_rows, streaks."""
from __future__ import annotations

from datetime import date

from app.services import activities, stats
from tests.services_helpers import activity_json, fake_engine, make_athlete, make_event  # noqa: F401


async def _seed(s):
    """3 athlete, nhiều ngày, có hoạt động không hợp lệ, đã xoá, Ride quy đổi."""
    ev = await make_event(s)
    A = await make_athlete(s, strava_id=1, telegram_id=11, name="An Nguyen", sex="M")
    B = await make_athlete(s, strava_id=2, telegram_id=22, name="Bình Lê", sex="F")
    C = await make_athlete(s, strava_id=3, telegram_id=33, name="Chi Trần", sex="F")

    async def ing(ath, aid, **kw):
        return await activities.ingest_from_json(s, ath, activity_json(aid, **kw), notify=False)

    # A: 5 + 6 + 7 (chuỗi 3 ngày) + 2 = 20 km, 4 ngày
    await ing(A, 11, km=5.0, start_local="2027-01-05T06:00:00Z")
    await ing(A, 12, km=6.0, start_local="2027-01-06T06:00:00Z")
    await ing(A, 13, km=7.0, start_local="2027-01-07T06:00:00Z")
    await ing(A, 14, km=2.0, start_local="2027-01-10T06:00:00Z")
    # B: 10 (05/01) + không hợp lệ (06/01) + Ride 20 km -> 5 km (20/01) = 15 km, 2 ngày
    await ing(B, 21, km=10.0, start_local="2027-01-05T06:00:00Z")
    await ing(B, 22, km=8.0, start_local="2027-01-06T06:00:00Z", has_map=False)
    await ing(B, 23, km=20.0, sport="Ride", start_local="2027-01-20T06:00:00Z", moving=3600)
    # C: 3 km (06/01) + 10 km đã xoá (07/01) + VirtualRide không hợp lệ (08/01) = 3 km, 1 ngày
    await ing(C, 31, km=3.0, start_local="2027-01-06T06:00:00Z")
    await ing(C, 32, km=10.0, start_local="2027-01-07T06:00:00Z")
    await activities.mark_deleted(s, 32)
    await ing(C, 33, km=15.0, sport="VirtualRide", start_local="2027-01-08T06:00:00Z")
    return ev, A, B, C


async def test_ranking_order_days_until_limit(db, fake_engine):
    async with db() as s:
        ev, A, B, C = await _seed(s)
        rows = await stats.ranking(s, ev)
        assert [(r.name, r.km, r.days, r.activities, r.rank) for r in rows] == [
            ("An Nguyen", 20.0, 4, 4, 1), ("Bình Lê", 15.0, 2, 2, 2), ("Chi Trần", 3.0, 1, 1, 3)]
        assert rows[1].sex == "F" and rows[1].telegram_id == 22 and rows[0].athlete_id == A.id

        until = await stats.ranking(s, ev, until=date(2027, 1, 5))
        assert [(r.name, r.km, r.rank) for r in until] == [("Bình Lê", 10.0, 1), ("An Nguyen", 5.0, 2)]
        until6 = await stats.ranking(s, ev, until=date(2027, 1, 6))
        assert [(r.name, r.km, r.days) for r in until6] == [("An Nguyen", 11.0, 2), ("Bình Lê", 10.0, 1), ("Chi Trần", 3.0, 1)]

        top2 = await stats.ranking(s, ev, limit=2)
        assert [r.rank for r in top2] == [1, 2] and len(top2) == 2
        assert await stats.ranking(s, ev, until=date(2026, 12, 31)) == []


async def test_ranking_tiebreak_days_then_name(db, fake_engine):
    async with db() as s:
        ev = await make_event(s)
        X = await make_athlete(s, strava_id=1, telegram_id=1, name="Zed")
        Y = await make_athlete(s, strava_id=2, telegram_id=2, name="Yen")
        Z = await make_athlete(s, strava_id=3, telegram_id=3, name="Anh")
        for ath, aid, days in ((X, 1, ["05", "06"]), (Y, 10, ["05"]), (Z, 20, ["05"])):
            for i, d in enumerate(days):
                await activities.ingest_from_json(
                    s, ath, activity_json(aid + i, km=10.0 / len(days), start_local=f"2027-01-{d}T06:00:00Z"), notify=False)
        rows = await stats.ranking(s, ev)
        assert [(r.name, r.km, r.days, r.rank) for r in rows] == [("Zed", 10.0, 2, 1), ("Anh", 10.0, 1, 2), ("Yen", 10.0, 1, 3)]


async def test_daily(db, fake_engine):
    async with db() as s:
        ev, A, B, C = await _seed(s)
        rows = await stats.daily(s, ev, date(2027, 1, 6))
        assert [(r.name, r.km, r.days, r.rank) for r in rows] == [("An Nguyen", 6.0, 1, 1), ("Chi Trần", 3.0, 1, 2)]
        rows = await stats.daily(s, ev, date(2027, 1, 5))
        assert [(r.name, r.km) for r in rows] == [("Bình Lê", 10.0), ("An Nguyen", 5.0)]
        assert [(r.name, r.km) for r in await stats.daily(s, ev, date(2027, 1, 7))] == [("An Nguyen", 7.0)]   # C đã xoá
        assert await stats.daily(s, ev, date(2027, 1, 8)) == []   # VirtualRide không hợp lệ
        assert [(r.name, r.km) for r in await stats.daily(s, ev, date(2027, 1, 20))] == [("Bình Lê", 5.0)]


async def test_team_totals(db, fake_engine):
    async with db() as s:
        ev, *_ = await _seed(s)
        t = await stats.team_totals(s, ev)
        assert (round(t.sum_km, 3), t.active_athletes, t.activities) == (38.0, 3, 7)
        t5 = await stats.team_totals(s, ev, until=date(2027, 1, 5))
        assert (t5.sum_km, t5.active_athletes, t5.activities) == (15.0, 2, 2)
        empty = await stats.team_totals(s, ev, until=date(2026, 1, 1))
        assert (empty.sum_km, empty.active_athletes, empty.activities) == (0.0, 0, 0)


async def test_athlete_summary_and_activity_log(db, fake_engine):
    async with db() as s:
        ev, A, B, C = await _seed(s)
        sm = await stats.athlete_summary(s, ev, B, recent_n=2)
        assert (sm.total_km, sm.days, sm.rank, sm.total_athletes) == (15.0, 2, 2, 3)
        assert sm.athlete.id == B.id
        assert [(a.id, e.valid) for a, e in sm.recent] == [(23, True), (22, False)]   # gần nhất trước, kể cả invalid
        assert sm.recent[1][1].reasons == ["NO_GPS_MAP"]

        full = await stats.activity_log(s, ev, B, n=10)
        assert [a.id for a, _ in full] == [23, 22, 21]
        logC = await stats.activity_log(s, ev, C)
        assert [a.id for a, _ in logC] == [33, 31]   # 32 đã xoá không hiện
        assert logC[0][1] is not None and logC[0][1].valid is False

        # athlete chưa có gì
        D = await make_athlete(s, strava_id=4, telegram_id=44, name="Dũng")
        smD = await stats.athlete_summary(s, ev, D)
        assert (smD.total_km, smD.days, smD.rank, smD.total_athletes, smD.recent) == (0.0, 0, None, 3, [])

        # hoạt động chưa chấm (ngoài cửa sổ event không hiện; trong cửa sổ nhưng chưa có Evaluation -> None)
        from app.db.models import Activity
        from datetime import datetime
        s.add(Activity(id=999, athlete_id=D.id, sport_type="Run", start_date=datetime(2027, 1, 11), start_date_local=datetime(2027, 1, 11, 7),
                       distance_m=1000, moving_s=400, elapsed_s=400))
        await s.flush()
        logD = await stats.activity_log(s, ev, D)
        assert len(logD) == 1 and logD[0][1] is None


async def test_export_rows(db, fake_engine):
    async with db() as s:
        ev, A, B, C = await _seed(s)
        rows = await stats.export_rows(s, ev)
        assert len(rows) == 9                      # 4 + 3 + 2 (bỏ đã xoá)
        assert set(rows[0]) == {"activity_id", "athlete", "strava_id", "telegram", "sport_type", "start_local",
                                "distance_km", "moving", "elapsed", "pace", "has_map", "manual", "trainer", "device",
                                "valid", "reasons", "converted_km", "overridden"}
        by_id = {r["activity_id"]: r for r in rows}
        r22 = by_id[22]
        assert r22["athlete"] == "Bình Lê" and r22["strava_id"] == 2 and r22["telegram"] == 22
        assert r22["valid"] is False and r22["reasons"] == "NO_GPS_MAP" and r22["converted_km"] == 0.0
        assert r22["has_map"] is False and r22["distance_km"] == 8.0 and r22["start_local"] == "2027-01-06T06:00:00"
        r23 = by_id[23]
        assert r23["sport_type"] == "Ride" and r23["valid"] is True and r23["converted_km"] == 5.0 and r23["pace"] == "3:00"
        assert by_id[11]["pace"] == "6:00" and by_id[11]["device"] == "Garmin" and by_id[11]["overridden"] is False
        assert [r["athlete"] for r in rows[:4]] == ["An Nguyen"] * 4   # sắp theo tên rồi thời gian
        assert [r["activity_id"] for r in rows[:4]] == [11, 12, 13, 14]

        await activities.apply_override(s, ev, 22, "include", value=None, note="ok", by=1000)
        rows = await stats.export_rows(s, ev)
        r22 = {r["activity_id"]: r for r in rows}[22]
        assert r22["valid"] is True and r22["overridden"] is True and r22["converted_km"] == 8.0 and r22["reasons"] == ""


async def test_streaks(db, fake_engine):
    async with db() as s:
        ev, A, B, C = await _seed(s)
        st = await stats.streaks(s, ev)
        assert st == {A.id: 3, B.id: 1, C.id: 1}
        # A thêm 08/01 và 09/01 -> chuỗi 05..10 = 6 ngày
        for aid, d in ((15, "08"), (16, "09")):
            await activities.ingest_from_json(s, A, activity_json(aid, km=1.5, start_local=f"2027-01-{d}T06:00:00Z"), notify=False)
        assert (await stats.streaks(s, ev))[A.id] == 6
        # hai hoạt động cùng ngày không tính thêm
        await activities.ingest_from_json(s, A, activity_json(17, km=1.5, start_local="2027-01-09T18:00:00Z"), notify=False)
        assert (await stats.streaks(s, ev))[A.id] == 6
        assert stats.longest_streak([]) == 0
        assert stats.longest_streak([date(2027, 1, 1), date(2027, 1, 3), date(2027, 1, 4)]) == 2
