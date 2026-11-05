"""Test tích hợp xuyên module với engine THẬT + services THẬT + composer THẬT:
giải -> nạp hoạt động Strava (JSON) -> chấm -> thống kê -> đổi luật & chấm lại -> override -> bài đăng sáng.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.db.models import Athlete
from app.jobs import daily_post
from app.services import activities, events, stats
from tests.services_helpers import activity_json

START, END = date(2027, 1, 1), date(2027, 1, 31)


async def _athlete(session, strava_id: int, name: str, tg: int) -> Athlete:
    a = Athlete(strava_id=strava_id, firstname=name, lastname="", display_name=name, telegram_id=tg, active=True)
    session.add(a)
    await session.flush()
    return a


@pytest.mark.asyncio
async def test_full_flow(db, sent_messages):
    async with db() as s:
        ev = await events.create_event(s, "Giải thử", START, END, by=1000)
        an = await _athlete(s, 1, "An", 11)
        binh = await _athlete(s, 2, "Bình", 12)
        await s.commit()

        # An: 5 km pace 6:00 có map -> hợp lệ; 3 km pace 3:30 -> quá nhanh; đạp 8 km -> 2 km quy đổi
        r1 = await activities.ingest_from_json(s, an, activity_json(101, start_local="2027-01-05T06:00:00Z", km=5))
        r2 = await activities.ingest_from_json(s, an, activity_json(102, start_local="2027-01-06T06:00:00Z", km=3, moving=630))
        r3 = await activities.ingest_from_json(s, an, activity_json(103, start_local="2027-01-06T18:00:00Z", km=8,
                                                                    sport="Ride", moving=1600))
        # Bình: không có map -> loại; 10 km hợp lệ
        r4 = await activities.ingest_from_json(s, binh, activity_json(201, start_local="2027-01-05T06:00:00Z", km=4, has_map=False))
        r5 = await activities.ingest_from_json(s, binh, activity_json(202, start_local="2027-01-07T06:00:00Z", km=10))
        await s.commit()

    assert r1.evaluation.valid and r1.evaluation.converted_km == 5.0
    assert not r2.evaluation.valid and any(x.startswith("PACE_TOO_FAST") for x in r2.evaluation.reasons)
    assert r3.evaluation.valid and r3.evaluation.converted_km == 2.0 and r3.evaluation.sport_key == "ride"
    assert not r4.evaluation.valid and "NO_GPS_MAP" in r4.evaluation.reasons
    assert r5.evaluation.valid
    # DM báo loại cho 2 hoạt động không hợp lệ (mặc định notify_rejected_dm=True)
    rejected = [t for cid, t in sent_messages if t.startswith("❌")]
    assert len(rejected) == 2 and any("nhanh" in t for t in rejected) and any("GPS" in t for t in rejected)

    async with db() as s:
        ev = await events.get_active_event(s)
        rows = await stats.ranking(s, ev)
        assert [(r.name, round(r.km, 2)) for r in rows] == [("Bình", 10.0), ("An", 7.0)]
        assert rows[0].rank == 1 and rows[1].days == 2
        d6 = await stats.daily(s, ev, date(2027, 1, 6))
        assert [(r.name, r.km) for r in d6] == [("An", 2.0)]
        totals = await stats.team_totals(s, ev)
        assert round(totals.sum_km, 2) == 17.0 and totals.active_athletes == 2

        # Đổi luật: cho phép pace nhanh tới 3:00 -> hoạt động 102 trở thành hợp lệ sau /recheck
        rs, old, new = await events.set_rule_value(s, ev, "sports.run.pace_min", "3:00", by=1000)
        assert (old, new) == ("4:00", "3:00") and rs.version == 2
        summary = await activities.reevaluate_event(s, ev)
        assert (102, False, True) in summary.changed
        rows = await stats.ranking(s, ev)
        assert [(r.name, round(r.km, 2)) for r in rows] == [("An", 10.0), ("Bình", 10.0)]

        # Admin include hoạt động không map của Bình -> +4 km
        await activities.apply_override(s, ev, 201, "include", value=None, note="BTC xác nhận", by=1000)
        rows = await stats.ranking(s, ev)
        assert [(r.name, round(r.km, 2)) for r in rows] == [("Bình", 14.0), ("An", 10.0)]

        export = await stats.export_rows(s, ev)
        assert len(export) == 5 and {r["activity_id"] for r in export} == {101, 102, 103, 201, 202}
        await s.commit()

    # Bài đăng sáng 08/01 (báo cáo ngày 07/01): top tổng và top ngày có tên, không gửi vì dry_run
    sent_before = len(sent_messages)
    text = await daily_post.run_daily_post(date(2027, 1, 8), dry_run=True)
    assert "Bình" in text and "An" in text and "#VTSRunningClub" in text
    assert "10,0 km" in text  # Bình chạy 10 km ngày 07/01
    assert len(sent_messages) == sent_before

    # Gửi thật: 2 tin cho admin (chú thích + bài), không gửi nhóm vì auto_post_group=False
    text2 = await daily_post.run_daily_post(date(2027, 1, 8))
    new = sent_messages[sent_before:]
    assert text2 and [cid for cid, _ in new] == [1000, 1000]
    assert any(t.startswith("📋") for _, t in new)
