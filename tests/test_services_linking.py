"""Test app.services.linking: nối Telegram <-> Strava, unlink, deauthorize."""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import Activity, Athlete, AuditLog, Evaluation, LinkToken, OAuthToken
from app.services import activities, linking
from tests.services_helpers import (FakeClient, activity_json, fake_engine, fake_tokens,  # noqa: F401
                                    make_athlete, make_event)


async def test_complete_link_happy_path(db, fake_tokens):
    client = FakeClient()
    async with db() as s:
        token = await linking.create_link_token(s, 5001, "an_user")
        assert len(token) >= 24
        lt = await s.get(LinkToken, token)
        assert lt.telegram_id == 5001 and lt.used_at is None and lt.expires_at > lt.created_at

        ath = await linking.complete_link(s, client, token, "code123", "read,activity:read")
        assert ath.id is not None
        assert (ath.strava_id, ath.firstname, ath.lastname, ath.display_name) == (777, "An", "Nguyen", "An Nguyen")
        assert ath.profile_url == "https://img/p.jpg" and ath.sex == "M"
        assert ath.telegram_id == 5001 and ath.telegram_username == "an_user"
        assert ath.active is True and ath.consent_at is not None and ath.deauthorized_at is None
        assert ath.club_member is None   # không cấu hình club

        tok = await s.get(OAuthToken, ath.id)
        assert tok.access_token == "acc-code123" and tok.scope == "read,activity:read"
        assert (await s.get(LinkToken, token)).used_at is not None

        assert (await linking.get_athlete_by_telegram(s, 5001)).id == ath.id
        assert (await linking.get_athlete_by_strava(s, 777)).id == ath.id
        assert await linking.get_athlete_by_telegram(s, 42) is None
        assert [a.id for a in await linking.list_connected(s)] == [ath.id]

        log = (await s.execute(select(AuditLog).where(AuditLog.action == "athlete.link"))).scalars().one()
        assert log.details["strava_id"] == 777 and log.details["created"] is True

        # dùng lại token đã dùng -> lỗi
        with pytest.raises(ValueError, match="hết hạn"):
            await linking.complete_link(s, client, token, "code999", "read")

        # cùng Strava nối lại từ Telegram khác -> cập nhật telegram_id, không tạo athlete mới
        ath.display_name = "Tên tự đặt"
        token2 = await linking.create_link_token(s, 5002, "other")
        ath2 = await linking.complete_link(s, client, token2, "code456", "read,activity:read")
        assert ath2.id == ath.id and ath2.telegram_id == 5002 and ath2.display_name == "Tên tự đặt"
        assert (await s.execute(select(func.count(Athlete.id)))).scalar() == 1
        assert (await s.get(OAuthToken, ath.id)).access_token == "acc-code456"



async def test_complete_link_expired_or_unknown_token(db, fake_tokens):
    client = FakeClient()
    async with db() as s:
        expired = await linking.create_link_token(s, 5001, None, ttl_minutes=-1)
        with pytest.raises(ValueError, match="Liên kết không hợp lệ hoặc đã hết hạn"):
            await linking.complete_link(s, client, expired, "code", "read")
        with pytest.raises(ValueError):
            await linking.complete_link(s, client, "khong-ton-tai", "code", "read")
        with pytest.raises(ValueError):
            await linking.complete_link(s, client, "", "code", "read")
        assert client.calls == []   # không gọi Strava khi token sai
        assert await linking.get_athlete_by_strava(s, 777) is None


async def test_complete_link_club_check(db, fake_tokens, monkeypatch):
    monkeypatch.setattr(get_settings(), "strava_club_id", 42)
    async with db() as s:
        client = FakeClient()
        client.clubs = [{"id": 7, "name": "Khác"}, {"id": 42, "name": "VTS"}]
        ath = await linking.complete_link(s, client, await linking.create_link_token(s, 1, None), "c1", "read")
        assert ath.club_member is True

        client.clubs = [{"id": 7}]
        ath = await linking.complete_link(s, client, await linking.create_link_token(s, 1, None), "c2", "read")
        assert ath.club_member is False

        client.clubs_error = RuntimeError("strava down")
        ath = await linking.complete_link(s, client, await linking.create_link_token(s, 1, None), "c3", "read")
        assert ath.club_member is None and ath.active is True   # lỗi club không fatal


async def test_unlink_revokes_and_optionally_deletes_data(db, fake_tokens, fake_engine):
    client = FakeClient()
    async with db() as s:
        await make_event(s)
        ath = await make_athlete(s)
        await activities.ingest_from_json(s, ath, activity_json(101), notify=False)
        await activities.ingest_from_json(s, ath, activity_json(102, start_local="2027-01-06T06:00:00Z"), notify=False)

        await linking.unlink(s, client, ath, delete_data=False)
        assert client.revoked == ["acc-777"]
        assert await s.get(OAuthToken, ath.id) is None
        assert ath.active is False and ath.deauthorized_at is not None
        assert (await s.execute(select(func.count(Activity.id)))).scalar() == 2   # giữ dữ liệu
        assert await linking.list_connected(s) == []
        assert (await linking.get_athlete_by_telegram(s, 5001)).id == ath.id      # vẫn tra được

        # unlink lần nữa với delete_data -> xoá hoạt động + evaluation; không có token thì không revoke
        await linking.unlink(s, client, ath, delete_data=True)
        assert client.revoked == ["acc-777"]
        assert (await s.execute(select(func.count(Activity.id)))).scalar() == 0
        assert (await s.execute(select(func.count(Evaluation.id)))).scalar() == 0
        logs = (await s.execute(select(AuditLog).where(AuditLog.action == "athlete.unlink"))).scalars().all()
        assert len(logs) == 2 and logs[1].details["deleted_activities"] == 2


async def test_unlink_ignores_revoke_error(db, fake_tokens):
    class BadClient(FakeClient):
        async def revoke(self, access_token):
            raise RuntimeError("401")
    async with db() as s:
        ath = await make_athlete(s)
        await linking.unlink(s, BadClient(), ath)
        assert ath.active is False and await s.get(OAuthToken, ath.id) is None


async def test_deauthorize_deletes_everything(db, fake_tokens, fake_engine):
    async with db() as s:
        await make_event(s)
        ath = await make_athlete(s)
        await activities.ingest_from_json(s, ath, activity_json(101), notify=False)
        assert (await s.execute(select(func.count(Evaluation.id)))).scalar() == 1

        assert await linking.deauthorize(s, 123456) is None
        got = await linking.deauthorize(s, 777)
        assert got is not None and got.id == ath.id
        assert got.active is False and got.deauthorized_at is not None
        assert await s.get(OAuthToken, ath.id) is None
        assert (await s.execute(select(func.count(Activity.id)))).scalar() == 0
        assert (await s.execute(select(func.count(Evaluation.id)))).scalar() == 0
        log = (await s.execute(select(AuditLog).where(AuditLog.action == "athlete.deauthorize"))).scalars().one()
        assert log.details["deleted_activities"] == 1
