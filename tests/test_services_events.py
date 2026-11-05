"""Test app.services.events: event lifecycle, versioning luật, settings."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.db.models import AuditLog, Event, RuleSet
from app.services import events


async def test_create_event_closes_previous_active(db):
    async with db() as s:
        e1 = await events.create_event(s, "Giải 1", date(2027, 1, 1), date(2027, 1, 31), by=1000)
        assert e1.id and e1.status == "active" and e1.current_rules_id is not None
        assert await events.get_rules_version(s, e1) == 1

        e2 = await events.create_event(s, "Giải 2", date(2027, 2, 1), date(2027, 2, 28), by=1000)
        assert e1.status == "closed"
        assert e2.status == "active"
        active = await events.get_active_event(s)
        assert active is not None and active.id == e2.id

        assert [e.id for e in await events.list_events(s)] == [e2.id, e1.id]
        assert (await events.get_event(s, e1.id)).name == "Giải 1"
        assert await events.get_event(s, 9999) is None

        cfg = await events.get_rules(s, e2)
        assert (cfg.event.name, cfg.event.start, cfg.event.end) == ("Giải 2", date(2027, 2, 1), date(2027, 2, 28))
        # kế thừa luật mặc định
        assert cfg.sport_by_key("run") is not None and cfg.general.require_gps_map is True

        rs = await s.get(RuleSet, e2.current_rules_id)
        assert rs.version == 1 and rs.event_id == e2.id and "Giải 2" in rs.yaml_text

        logs = (await s.execute(select(AuditLog).where(AuditLog.action == "event.create"))).scalars().all()
        assert len(logs) == 2 and logs[-1].details["closed_events"] == [e1.id]


async def test_create_event_draft_and_base_rules(db):
    async with db() as s:
        e1 = await events.create_event(s, "Giải 1", date(2027, 1, 1), date(2027, 1, 31), by=1000)
        base = (await events.get_rules(s, e1)).with_value("sports.run.pace_max", "18:00")
        e2 = await events.create_event(s, "Giải nháp", date(2027, 3, 1), date(2027, 3, 31), by=1000,
                                       base=base, activate=False)
        assert e2.status == "draft" and e1.status == "active"
        cfg2 = await events.get_rules(s, e2)
        assert cfg2.sport_by_key("run").pace_max == "18:00"     # kế thừa base
        assert cfg2.event.name == "Giải nháp"

        with pytest.raises(ValueError):   # end < start -> ValidationError (ValueError)
            await events.create_event(s, "Sai", date(2027, 5, 10), date(2027, 5, 1), by=1000)


async def test_update_rules_creates_new_version_and_syncs_event(db):
    async with db() as s:
        e = await events.create_event(s, "Giải 1", date(2027, 1, 1), date(2027, 1, 31), by=1000)
        cfg = await events.get_rules(s, e)
        new_cfg = cfg.model_copy(update={"event": cfg.event.model_copy(update={
            "name": "Giải 1 (sửa)", "end": date(2027, 2, 5), "grace_days": 3})})
        rs = await events.update_rules(s, e, new_cfg, by=1000, note="gia hạn")
        assert rs.version == 2 and rs.note == "gia hạn"
        assert e.current_rules_id == rs.id
        assert e.name == "Giải 1 (sửa)" and e.end_date == date(2027, 2, 5)
        assert await events.get_rules_version(s, e) == 2
        assert (await events.get_rules(s, e)).event.grace_days == 3

        versions = (await s.execute(select(RuleSet.version).where(RuleSet.event_id == e.id)
                                    .order_by(RuleSet.version))).scalars().all()
        assert versions == [1, 2]
        log = (await s.execute(select(AuditLog).where(AuditLog.action == "rules.update"))).scalars().one()
        assert log.details == {"event_id": e.id, "version": 2, "note": "gia hạn"}


async def test_set_rule_value_returns_old_new(db):
    async with db() as s:
        e = await events.create_event(s, "Giải 1", date(2027, 1, 1), date(2027, 1, 31), by=1000)
        rs, old, new = await events.set_rule_value(s, e, "sports.run.pace_max", "18:00", by=1000)
        assert (rs.version, old, new) == (2, "17:00", "18:00")
        assert rs.note == "set sports.run.pace_max"
        assert (await events.get_rules(s, e)).sport_by_key("run").pace_max == "18:00"

        rs, old, new = await events.set_rule_value(s, e, "event.grace_days", "3", by=1000)
        assert (rs.version, old, new) == (3, 2, 3)

        rs, old, new = await events.set_rule_value(s, e, "general.daily_cap_km", "20", by=1000)
        assert rs.version == 4 and old is None and float(new) == 20.0

        rs, old, new = await events.set_rule_value(s, e, "excluded_types", "Yoga, Workout", by=1000)
        assert "VirtualRide" in old and new == ["Yoga", "Workout"]

        with pytest.raises(KeyError):
            await events.set_rule_value(s, e, "sports.bike.factor", "1", by=1000)
        with pytest.raises(ValueError):          # pace sai định dạng -> ValidationError
            await events.set_rule_value(s, e, "sports.run.pace_max", "abc", by=1000)
        assert await events.get_rules_version(s, e) == 5   # lỗi không tạo version mới


async def test_get_rules_fallback_without_ruleset(db):
    async with db() as s:
        e = Event(name="Giải cũ", start_date=date(2026, 9, 1), end_date=date(2026, 9, 30), status="closed",
                  timezone="Asia/Bangkok")
        s.add(e)
        await s.flush()
        assert await events.get_rules_version(s, e) == 0
        cfg = await events.get_rules(s, e)
        assert (cfg.event.name, cfg.event.start, cfg.event.end, cfg.event.timezone) == (
            "Giải cũ", date(2026, 9, 1), date(2026, 9, 30), "Asia/Bangkok")
        assert e.current_rules_id is None   # không lưu
        assert (await s.execute(select(RuleSet))).scalars().all() == []


async def test_set_event_status(db):
    async with db() as s:
        e1 = await events.create_event(s, "Giải 1", date(2027, 1, 1), date(2027, 1, 31), by=1000)
        e2 = await events.create_event(s, "Giải 2", date(2027, 2, 1), date(2027, 2, 28), by=1000)
        assert e1.status == "closed"
        await events.set_event_status(s, e1, "active", by=1000)
        assert e1.status == "active" and e2.status == "closed"
        assert (await events.get_active_event(s)).id == e1.id
        await events.set_event_status(s, e1, "closed", by=1000)
        assert await events.get_active_event(s) is None
        with pytest.raises(ValueError):
            await events.set_event_status(s, e1, "paused", by=1000)
        logs = (await s.execute(select(AuditLog).where(AuditLog.action == "event.status"))).scalars().all()
        assert len(logs) == 2


async def test_settings_and_setting_bool(db):
    async with db() as s:
        assert await events.get_setting(s, "x") is None
        assert await events.get_setting(s, "x", "dflt") == "dflt"
        await events.set_setting(s, "notify_rejected_dm", "0")
        assert await events.get_setting(s, "notify_rejected_dm") == "0"
        assert await events.setting_bool(s, "notify_rejected_dm", True) is False
        await events.set_setting(s, "notify_rejected_dm", "true")
        assert await events.setting_bool(s, "notify_rejected_dm", False) is True
        await events.set_setting(s, "notify_rejected_dm", "1")
        assert await events.setting_bool(s, "notify_rejected_dm", False) is True
        assert await events.setting_bool(s, "missing", True) is True
        assert await events.setting_bool(s, "missing", False) is False


async def test_audit_serializes_dates(db):
    async with db() as s:
        await events.audit(s, 1000, "test.action", {"d": date(2027, 1, 1), "n": 1, "l": [date(2027, 1, 2)]})
        log = (await s.execute(select(AuditLog))).scalars().one()
        assert log.actor_telegram_id == 1000
        assert log.details == {"d": "2027-01-01", "n": 1, "l": ["2027-01-02"]}
