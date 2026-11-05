"""Test app.services.activities: ingest, thông báo, webhook, backfill, override, reevaluate, purge."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db.models import Activity, AuditLog, Evaluation, OAuthToken, Override, WebhookEvent, utcnow
from app.services import activities, events, stats
from tests.services_helpers import (FakeClient, accept_all, activity_json, fake_engine, fake_evaluate,  # noqa: F401
                                    fake_tokens, make_athlete, make_event)


async def _count(s, col):
    return (await s.execute(select(func.count(col)))).scalar()


# ------------------------------------------------------------------ ingest_from_json
async def test_ingest_creates_activity_and_evaluation(db, fake_engine, sent_messages):
    async with db() as s:
        ev = await make_event(s)
        ath = await make_athlete(s)
        data = activity_json(101, km=5.0)
        r = await activities.ingest_from_json(s, ath, data)
        assert r.created is True and r.changed is True
        a, e = r.activity, r.evaluation
        assert a.id == 101 and a.athlete_id == ath.id and a.sport_type == "Run" and a.has_map is True
        assert a.distance_m == 5000.0 and a.moving_s == 1800 and a.raw_json["id"] == 101
        assert a.start_date_local.hour == 6 and a.start_date.hour == 23   # UTC = local - 7h
        assert e is not None and e.event_id == ev.id and e.activity_id == 101
        assert e.valid is True and e.converted_km == 5.0 and e.raw_km == 5.0 and e.rules_version == 1
        assert e.reasons == [] and e.overridden is False and e.sport_key == "run" and e.pace_s == 360.0
        assert sent_messages == []   # hợp lệ: mặc định không DM, không báo nhóm


async def test_ingest_invalid_sends_dm(db, fake_engine, sent_messages):
    async with db() as s:
        await make_event(s)
        ath = await make_athlete(s, telegram_id=5001)
        r = await activities.ingest_from_json(s, ath, activity_json(102, has_map=False, name="Chạy máy"))
        assert r.evaluation.valid is False and r.evaluation.reasons == ["NO_GPS_MAP"] and r.evaluation.converted_km == 0
        assert len(sent_messages) == 1
        chat_id, text = sent_messages[0]
        assert chat_id == 5001
        assert text.startswith("❌ Hoạt động 'Chạy máy' (05/01 06:00, 5,00 km) không được tính: không có bản đồ GPS")
        assert "/rules" in text


async def test_ingest_notify_false_or_setting_off_no_dm(db, fake_engine, sent_messages):
    async with db() as s:
        await make_event(s)
        ath = await make_athlete(s)
        await activities.ingest_from_json(s, ath, activity_json(103, has_map=False), notify=False)
        assert sent_messages == []
        await events.set_setting(s, "notify_rejected_dm", "0")
        await activities.ingest_from_json(s, ath, activity_json(104, has_map=False))
        assert sent_messages == []
        # athlete không có telegram -> không gửi được, không lỗi
        await events.set_setting(s, "notify_rejected_dm", "1")
        ath2 = await make_athlete(s, strava_id=778, telegram_id=None, name="Bình Lê")
        await activities.ingest_from_json(s, ath2, activity_json(105, has_map=False))
        assert sent_messages == []


async def test_ingest_valid_notifications_when_enabled(db, fake_engine, sent_messages):
    async with db() as s:
        await make_event(s)
        ath = await make_athlete(s, telegram_id=5001, name="An Nguyen")
        await events.set_setting(s, "notify_valid_dm", "1")
        await events.set_setting(s, "notify_valid_group", "true")
        await activities.ingest_from_json(s, ath, activity_json(106, km=6.2, sport="Run"))
        assert len(sent_messages) == 2
        assert sent_messages[0][0] == 5001 and sent_messages[0][1].startswith("✅") and "được tính 6,20 km" in sent_messages[0][1]
        assert sent_messages[1][0] == -100500
        assert sent_messages[1][1] == "✅ An Nguyen vừa hoàn thành 6,20 km Run (pace 6:00/km) – 6,20 km quy đổi"
        # lần 2 cùng id: không phải hoạt động mới -> không báo lại
        await activities.ingest_from_json(s, ath, activity_json(106, km=6.2))
        assert len(sent_messages) == 2


async def test_ingest_same_id_updates_without_duplicate(db, fake_engine, sent_messages):
    async with db() as s:
        await make_event(s)
        ath = await make_athlete(s)
        r1 = await activities.ingest_from_json(s, ath, activity_json(107, name="Lần 1"))
        fetched = r1.activity.fetched_at
        r2 = await activities.ingest_from_json(s, ath, activity_json(107, name="Lần 2", km=5.0))
        assert r2.created is False and r2.changed is False
        assert r2.activity.name == "Lần 2" and r2.activity.fetched_at == fetched
        assert await _count(s, Activity.id) == 1 and await _count(s, Evaluation.id) == 1

        # đổi thành không hợp lệ -> changed=True, valid đổi, nhưng không DM (không phải hoạt động mới)
        r3 = await activities.ingest_from_json(s, ath, activity_json(107, name="Lần 3", has_map=False))
        assert r3.created is False and r3.changed is True and r3.evaluation.valid is False
        assert await _count(s, Evaluation.id) == 1 and sent_messages == []

        # hoạt động từng bị xoá, ingest lại -> hồi phục
        await activities.mark_deleted(s, 107)
        r4 = await activities.ingest_from_json(s, ath, activity_json(107, name="Lần 4"))
        assert r4.activity.deleted_at is None and r4.evaluation.valid is True


async def test_ingest_outside_any_event_has_no_evaluation(db, fake_engine, sent_messages):
    async with db() as s:
        ath = await make_athlete(s)
        r = await activities.ingest_from_json(s, ath, activity_json(108))   # chưa có event nào
        assert r.created is True and r.evaluation is None and r.changed is False
        ev = await make_event(s)
        r = await activities.ingest_from_json(s, ath, activity_json(109, start_local="2026-06-01T06:00:00Z"))
        assert r.evaluation is None and sent_messages == []
        # event đã đóng nhưng chứa ngày hoạt động -> vẫn chấm vào event đó
        await events.set_event_status(s, ev, "closed", by=1000)
        r = await activities.ingest_from_json(s, ath, activity_json(110))
        assert r.evaluation is not None and r.evaluation.event_id == ev.id
        # event draft không nhận
        e2 = await events.create_event(s, "Nháp", ev.start_date, ev.end_date, by=1000, activate=False)
        r = await activities.ingest_from_json(s, ath, activity_json(111, start_local="2027-01-09T06:00:00Z"))
        assert r.evaluation.event_id == ev.id and e2.status == "draft"


async def test_evaluate_context_same_day(db, fake_engine, monkeypatch):
    """EvalContext nhận hoạt động khác cùng ngày + km đã tính; không gồm chính nó / hoạt động đã xoá."""
    seen = {}

    def spy(activity, rules, ctx=None):
        seen[activity.id] = ([a.id for a in ctx.same_day_activities], ctx.counted_km_today, ctx.now)
        return fake_evaluate(activity, rules, ctx)
    monkeypatch.setattr(fake_engine, "evaluate", spy)
    async with db() as s:
        await make_event(s)
        ath = await make_athlete(s)
        await activities.ingest_from_json(s, ath, activity_json(201, km=3.0, start_local="2027-01-05T06:00:00Z"), notify=False)
        await activities.ingest_from_json(s, ath, activity_json(202, km=2.0, start_local="2027-01-05T18:00:00Z"), notify=False)
        await activities.ingest_from_json(s, ath, activity_json(203, km=4.0, start_local="2027-01-06T06:00:00Z"), notify=False)
        assert seen[201] == ([], 0.0, seen[201][2]) and seen[201][2] is not None
        assert seen[202][0] == [201] and seen[202][1] == 3.0
        assert seen[203][0] == []
        await activities.mark_deleted(s, 201)
        await activities.ingest_from_json(s, ath, activity_json(202, km=2.0, start_local="2027-01-05T18:00:00Z"), notify=False)
        assert seen[202] == ([], 0.0, seen[202][2])


# ------------------------------------------------------------------ ingest_activity / webhook
async def test_ingest_activity_fetches_and_handles_404(db, fake_engine, fake_tokens):
    client = FakeClient()
    async with db() as s:
        await make_event(s)
        ath = await make_athlete(s)
        client.activities[301] = activity_json(301)
        r = await activities.ingest_activity(s, client, ath, 301, notify=False)
        assert r is not None and r.created and r.evaluation.valid
        assert ("get_activity", "tok", 301) in client.calls
        assert await activities.ingest_activity(s, client, ath, 999, notify=False) is None
        assert await activities.get_activity(s, 301) is not None
        assert await activities.get_activity(s, 999) is None


async def test_handle_webhook_create_update_delete(db, fake_engine, fake_tokens, sent_messages):
    client = FakeClient()
    async with db() as s:
        ev = await make_event(s)
        ath = await make_athlete(s, telegram_id=5001)
        client.activities[401] = activity_json(401, has_map=False)
        client.activities[402] = activity_json(402, start_local="2027-01-06T06:00:00Z")

        w = WebhookEvent(object_type="activity", object_id=401, aspect_type="create", owner_id=777)
        s.add(w)
        await activities.handle_webhook_event(s, client, w)
        assert w.processed_at is not None and w.error is None
        assert (await activities.get_activity(s, 401)).athlete_id == ath.id
        assert len(sent_messages) == 1 and sent_messages[0][1].startswith("❌")   # create -> notify

        client.activities[401] = activity_json(401, has_map=True)
        w2 = WebhookEvent(object_type="activity", object_id=401, aspect_type="update", owner_id=777)
        s.add(w2)
        await activities.handle_webhook_event(s, client, w2)
        assert w2.error is None and len(sent_messages) == 1   # update -> không notify
        assert (await s.execute(select(Evaluation).where(Evaluation.activity_id == 401))).scalar_one().valid is True

        w3 = WebhookEvent(object_type="activity", object_id=402, aspect_type="create", owner_id=777)
        s.add(w3)
        await activities.handle_webhook_event(s, client, w3)
        assert len(await stats.ranking(s, ev)) == 1 and (await stats.ranking(s, ev))[0].km == 10.0

        w4 = WebhookEvent(object_type="activity", object_id=402, aspect_type="delete", owner_id=777)
        s.add(w4)
        await activities.handle_webhook_event(s, client, w4)
        assert (await activities.get_activity(s, 402)).deleted_at is not None and w4.error is None
        assert (await stats.ranking(s, ev))[0].km == 5.0

        # athlete lạ / đã gỡ -> bỏ qua nhưng vẫn processed
        w5 = WebhookEvent(object_type="activity", object_id=403, aspect_type="create", owner_id=123)
        s.add(w5)
        await activities.handle_webhook_event(s, client, w5)
        assert w5.processed_at is not None and w5.error is None and await activities.get_activity(s, 403) is None
        assert await _count(s, Activity.id) == 2

        # 404 từ Strava -> không lỗi
        w6 = WebhookEvent(object_type="activity", object_id=404, aspect_type="create", owner_id=777)
        s.add(w6)
        await activities.handle_webhook_event(s, client, w6)
        assert w6.processed_at is not None and w6.error is None


async def test_handle_webhook_records_error(db, fake_engine, fake_tokens):
    client = FakeClient()
    client.fail_get = RuntimeError("boom")
    async with db() as s:
        await make_event(s)
        await make_athlete(s)
        w = WebhookEvent(object_type="activity", object_id=501, aspect_type="create", owner_id=777)
        s.add(w)
        await activities.handle_webhook_event(s, client, w)    # không ném
        assert w.error == "boom" and w.processed_at is not None


async def test_handle_webhook_deauthorize(db, fake_engine, fake_tokens):
    client = FakeClient()
    async with db() as s:
        await make_event(s)
        ath = await make_athlete(s)
        await activities.ingest_from_json(s, ath, activity_json(601), notify=False)
        w = WebhookEvent(object_type="athlete", object_id=777, aspect_type="update", owner_id=777,
                         payload={"updates": {"authorized": "false"}})
        s.add(w)
        await activities.handle_webhook_event(s, client, w)
        assert w.error is None and w.processed_at is not None
        assert ath.active is False and ath.deauthorized_at is not None
        assert await s.get(OAuthToken, ath.id) is None
        assert await _count(s, Activity.id) == 0 and await _count(s, Evaluation.id) == 0
        # athlete update khác (vd đổi tên) -> không làm gì
        ath2 = await make_athlete(s, strava_id=778, telegram_id=5002, name="Bình Lê")
        w2 = WebhookEvent(object_type="athlete", object_id=778, aspect_type="update", owner_id=778,
                          payload={"updates": {"title": "x"}})
        s.add(w2)
        await activities.handle_webhook_event(s, client, w2)
        assert ath2.active is True and w2.processed_at is not None
        # webhook activity của athlete đã gỡ -> bỏ qua
        client.activities[602] = activity_json(602)
        w3 = WebhookEvent(object_type="activity", object_id=602, aspect_type="create", owner_id=777)
        s.add(w3)
        await activities.handle_webhook_event(s, client, w3)
        assert await activities.get_activity(s, 602) is None and w3.error is None


# ------------------------------------------------------------------ backfill
async def test_backfill_counts_created_in_window(db, fake_engine, fake_tokens, sent_messages):
    client = FakeClient()
    client.activities = {
        701: activity_json(701, start_local="2027-01-05T06:00:00Z"),
        702: activity_json(702, start_local="2027-01-06T06:00:00Z", has_map=False),
        703: activity_json(703, start_local="2027-02-01T06:00:00Z"),   # sau end, trong grace (2 ngày) -> vẫn tải
        704: activity_json(704, start_local="2026-12-20T06:00:00Z"),   # ngoài cửa sổ
        705: activity_json(705, start_local="2027-02-05T06:00:00Z"),   # ngoài cửa sổ
    }
    async with db() as s:
        ath = await make_athlete(s)
        assert await activities.backfill_athlete(s, client, ath) == 0   # chưa có event
        ev = await make_event(s)
        n = await activities.backfill_athlete(s, client, ath)
        assert n == 3
        assert sorted(a.id for a in (await s.execute(select(Activity))).scalars()) == [701, 702, 703]
        assert sent_messages == []   # mặc định không thông báo khi backfill
        _, after, before = [c for c in client.calls if c[0] == "list_all_activities"][-1]
        assert after < before and after is not None
        # 703 ngoài ngày thi -> không có evaluation; 701/702 có
        assert await _count(s, Evaluation.id) == 2
        assert await activities.backfill_athlete(s, client, ath, ev) == 0   # không tạo trùng
        assert await _count(s, Activity.id) == 3


# ------------------------------------------------------------------ override + stats
async def test_apply_override_changes_stats(db, fake_engine):
    async with db() as s:
        ev = await make_event(s)
        ath = await make_athlete(s)
        await activities.ingest_from_json(s, ath, activity_json(801, km=5.0), notify=False)
        await activities.ingest_from_json(s, ath, activity_json(802, km=4.0, has_map=False,
                                                                start_local="2027-01-06T06:00:00Z"), notify=False)
        assert (await stats.ranking(s, ev))[0].km == 5.0

        ov = await activities.apply_override(s, ev, 801, "exclude", value=None, note="gian lận", by=1000)
        assert ov.id and ov.active and ov.action == "exclude"
        e801 = (await s.execute(select(Evaluation).where(Evaluation.activity_id == 801))).scalar_one()
        assert e801.valid is False and e801.reasons == ["ADMIN_EXCLUDED"] and e801.converted_km == 0 and e801.overridden
        assert await stats.ranking(s, ev) == []

        await activities.apply_override(s, ev, 802, "include", value=None, note=None, by=1000)
        e802 = (await s.execute(select(Evaluation).where(Evaluation.activity_id == 802))).scalar_one()
        assert e802.valid is True and e802.reasons == [] and e802.converted_km == 4.0 and e802.overridden
        assert (await stats.ranking(s, ev))[0].km == 4.0

        await activities.apply_override(s, ev, 801, "adjust_km", value=3.0, note=None, by=1000)
        e801 = (await s.execute(select(Evaluation).where(Evaluation.activity_id == 801))).scalar_one()
        assert e801.valid is True and e801.converted_km == 3.0
        assert (await stats.ranking(s, ev))[0].km == 7.0
        # chỉ một override active cho (event, activity)
        active = (await s.execute(select(Override).where(Override.activity_id == 801, Override.active == True))).scalars().all()  # noqa: E712
        assert len(active) == 1 and active[0].action == "adjust_km" and active[0].value == 3.0
        assert await _count(s, Override.id) == 3

        # override vẫn giữ sau khi ingest lại / chấm lại
        await activities.ingest_from_json(s, ath, activity_json(801, km=5.0), notify=False)
        assert (await stats.ranking(s, ev))[0].km == 7.0
        await activities.reevaluate_event(s, ev)
        assert (await stats.ranking(s, ev))[0].km == 7.0

        # include với Ride dùng factor của luật (0.25)
        await activities.ingest_from_json(s, ath, activity_json(803, km=8.0, sport="Ride", has_map=False,
                                                                start_local="2027-01-07T06:00:00Z"), notify=False)
        await activities.apply_override(s, ev, 803, "include", value=None, note=None, by=1000)
        e803 = (await s.execute(select(Evaluation).where(Evaluation.activity_id == 803))).scalar_one()
        assert e803.valid and e803.converted_km == 2.0

        # hoạt động chưa có trong DB: override vẫn lưu, không lỗi
        ov2 = await activities.apply_override(s, ev, 999, "exclude", value=None, note=None, by=1000)
        assert ov2.id is not None

        with pytest.raises(ValueError):
            await activities.apply_override(s, ev, 801, "ban", value=None, note=None, by=1000)
        with pytest.raises(ValueError):
            await activities.apply_override(s, ev, 801, "adjust_km", value=None, note=None, by=1000)
        logs = (await s.execute(select(AuditLog).where(AuditLog.action.like("override.%")))).scalars().all()
        assert [l.action for l in logs] == ["override.exclude", "override.include", "override.adjust_km",
                                            "override.include", "override.exclude"]


# ------------------------------------------------------------------ reevaluate
async def test_reevaluate_event_reports_changed(db, fake_engine, monkeypatch):
    async with db() as s:
        ev = await make_event(s)
        ath = await make_athlete(s)
        await activities.ingest_from_json(s, ath, activity_json(901, km=5.0), notify=False)
        await activities.ingest_from_json(s, ath, activity_json(902, km=4.0, has_map=False,
                                                                start_local="2027-01-06T06:00:00Z"), notify=False)
        await activities.ingest_from_json(s, ath, activity_json(903, km=4.0,
                                                                start_local="2027-02-10T06:00:00Z"), notify=False)  # ngoài cửa sổ
        await activities.mark_deleted(s, 901)
        await activities.ingest_from_json(s, ath, activity_json(904, km=2.0,
                                                                start_local="2027-01-05T18:00:00Z"), notify=False)

        summary = await activities.reevaluate_event(s, ev)
        assert summary.total == 2 and summary.changed == []       # 901 đã xoá, 903 ngoài cửa sổ

        await events.set_rule_value(s, ev, "general.require_gps_map", "false", by=1000)
        monkeypatch.setattr(fake_engine, "evaluate", accept_all)
        summary = await activities.reevaluate_event(s, ev)
        assert summary.total == 2 and summary.changed == [(902, False, True)]
        e902 = (await s.execute(select(Evaluation).where(Evaluation.activity_id == 902))).scalar_one()
        assert e902.valid is True and e902.rules_version == 2

        monkeypatch.setattr(fake_engine, "evaluate", fake_evaluate)
        summary = await activities.reevaluate_event(s, ev)
        assert summary.changed == [(902, True, False)]

        # hoạt động chưa từng chấm (ingest khi chưa có event) -> được chấm, báo đổi nếu hợp lệ
        a = Activity(id=905, athlete_id=ath.id, name="x", sport_type="Run", start_date=datetime(2027, 1, 9, 23),
                     start_date_local=datetime(2027, 1, 10, 6),
                     distance_m=3000, moving_s=1000, elapsed_s=1000, has_map=True)
        s.add(a)
        await s.flush()
        summary = await activities.reevaluate_event(s, ev)
        assert summary.total == 3 and summary.changed == [(905, False, True)]


# ------------------------------------------------------------------ purge / mark_deleted
async def test_purge_raw_json_and_mark_deleted(db, fake_engine):
    async with db() as s:
        await make_event(s)
        ath = await make_athlete(s)
        r1 = await activities.ingest_from_json(s, ath, activity_json(1001), notify=False)
        r2 = await activities.ingest_from_json(s, ath, activity_json(1002, start_local="2027-01-06T06:00:00Z"), notify=False)
        r1.activity.fetched_at = utcnow() - timedelta(days=10)
        await s.flush()
        assert await activities.purge_raw_json(s, 7) == 1
        await s.refresh(r1.activity)
        await s.refresh(r2.activity)
        assert r1.activity.raw_json is None and r2.activity.raw_json is not None
        assert r1.activity.distance_m == 5000.0   # số liệu đã chấm vẫn giữ
        assert await activities.purge_raw_json(s, 7) == 0

        assert await activities.mark_deleted(s, 424242) is None
        a = await activities.mark_deleted(s, 1001)
        assert a is not None and a.deleted_at is not None
