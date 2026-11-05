"""Nối Telegram <-> Strava (OAuth) — CHỦ SỞ HỮU: agent `services`."""
from __future__ import annotations

import dataclasses
import logging
import secrets
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Activity, Athlete, Evaluation, LinkToken, OAuthToken, utcnow
from app.security import decrypt
from app.services import events
from app.strava import tokens as strava_tokens
from app.strava.client import StravaClient

log = logging.getLogger(__name__)

LINK_INVALID_MSG = "Liên kết không hợp lệ hoặc đã hết hạn"


async def create_link_token(session: AsyncSession, telegram_id: int, username: str | None, ttl_minutes: int = 30) -> str:
    token = secrets.token_urlsafe(24)
    session.add(LinkToken(token=token, telegram_id=telegram_id, telegram_username=username,
                          expires_at=utcnow() + timedelta(minutes=ttl_minutes)))
    await session.flush()
    return token


async def complete_link(session: AsyncSession, client: StravaClient, token: str, code: str, scope: str) -> Athlete:
    """Đổi code lấy token, upsert athlete + token, consent_at=now, đánh dấu LinkToken đã dùng.
    Kiểm tra club nếu settings.strava_club_id. Ném ValueError nếu token sai/hết hạn, StravaError nếu Strava lỗi."""
    now = utcnow()
    lt = await session.get(LinkToken, token) if token else None
    if lt is None or lt.used_at is not None or lt.expires_at < now:
        raise ValueError(LINK_INVALID_MSG)

    tr = await client.exchange_code(code)
    if scope and not tr.scope:
        # Strava trả scope qua query của callback, không nằm trong response đổi token
        tr = dataclasses.replace(tr, scope=scope)
    info = tr.athlete or {}
    if "id" not in info:
        raise ValueError("Strava không trả thông tin athlete")
    strava_id = int(info["id"])

    athlete = await get_athlete_by_strava(session, strava_id)
    created = athlete is None
    if athlete is None:
        athlete = Athlete(strava_id=strava_id)
        session.add(athlete)
    firstname = (info.get("firstname") or "").strip()
    lastname = (info.get("lastname") or "").strip()
    athlete.firstname = firstname
    athlete.lastname = lastname
    athlete.profile_url = info.get("profile") or info.get("profile_medium") or athlete.profile_url
    sex = info.get("sex")
    athlete.sex = sex[:1] if isinstance(sex, str) and sex else athlete.sex
    if not athlete.display_name:
        athlete.display_name = f"{firstname} {lastname}".strip()
    athlete.telegram_id = lt.telegram_id          # kể cả khi trước đó nối với Telegram khác -> chuyển sang mới
    athlete.telegram_username = lt.telegram_username
    athlete.consent_at = now
    athlete.active = True
    athlete.deauthorized_at = None
    await session.flush()

    await strava_tokens.store_token(session, athlete, tr)

    club_id = get_settings().strava_club_id
    if club_id:
        try:
            clubs = await client.get_athlete_clubs(tr.access_token)
            athlete.club_member = any(int(c.get("id", 0)) == int(club_id) for c in clubs)
        except Exception as e:  # noqa: BLE001 - không fatal
            log.warning("Không kiểm tra được club của athlete %s: %s", strava_id, e)
            athlete.club_member = None

    lt.used_at = now
    await events.audit(session, lt.telegram_id, "athlete.link", {
        "athlete_id": athlete.id, "strava_id": strava_id, "telegram_id": lt.telegram_id,
        "created": created, "scope": tr.scope, "club_member": athlete.club_member,
    })
    await session.flush()
    return athlete


async def get_athlete_by_telegram(session: AsyncSession, telegram_id: int) -> Athlete | None:
    """Athlete nối với Telegram này (active hay không). Ưu tiên bản active, nối gần nhất."""
    res = await session.execute(
        select(Athlete).where(Athlete.telegram_id == telegram_id)
        .order_by(Athlete.active.desc(), Athlete.consent_at.desc(), Athlete.id.desc()).limit(1))
    return res.scalar_one_or_none()


async def get_athlete_by_strava(session: AsyncSession, strava_id: int) -> Athlete | None:
    res = await session.execute(select(Athlete).where(Athlete.strava_id == strava_id))
    return res.scalar_one_or_none()


async def _delete_token(session: AsyncSession, athlete_id: int) -> OAuthToken | None:
    tok = await session.get(OAuthToken, athlete_id)
    if tok is not None:
        await session.execute(delete(OAuthToken).where(OAuthToken.athlete_id == athlete_id))
    return tok


async def _delete_activity_data(session: AsyncSession, athlete_id: int) -> int:
    """Xoá Activity (+Evaluation) của athlete. Trả số hoạt động đã xoá."""
    ids = select(Activity.id).where(Activity.athlete_id == athlete_id)
    await session.execute(delete(Evaluation).where(Evaluation.activity_id.in_(ids)))
    res = await session.execute(delete(Activity).where(Activity.athlete_id == athlete_id))
    return res.rowcount or 0


async def unlink(session: AsyncSession, client: StravaClient, athlete: Athlete, *, delete_data: bool = False) -> None:
    """Thu hồi token trên Strava (revoke), xoá OAuthToken, active=False; delete_data=True thì xoá cả hoạt động."""
    tok = await session.get(OAuthToken, athlete.id)
    revoked = False
    if tok is not None:
        try:
            await client.revoke(decrypt(tok.access_token))
            revoked = True
        except Exception as e:  # noqa: BLE001 - token có thể đã hỏng; vẫn gỡ liên kết
            log.warning("Không revoke được token của athlete %s: %s", athlete.strava_id, e)
        await _delete_token(session, athlete.id)
    athlete.active = False
    athlete.deauthorized_at = utcnow()
    deleted = 0
    if delete_data:
        deleted = await _delete_activity_data(session, athlete.id)
    await events.audit(session, athlete.telegram_id, "athlete.unlink", {
        "athlete_id": athlete.id, "strava_id": athlete.strava_id, "revoked": revoked,
        "delete_data": delete_data, "deleted_activities": deleted,
    })
    await session.flush()


async def deauthorize(session: AsyncSession, strava_id: int) -> Athlete | None:
    """Webhook athlete deauthorize: xoá token, active=False, deauthorized_at=now, xoá dữ liệu hoạt động."""
    athlete = await get_athlete_by_strava(session, strava_id)
    if athlete is None:
        return None
    await _delete_token(session, athlete.id)
    athlete.active = False
    athlete.deauthorized_at = utcnow()
    deleted = await _delete_activity_data(session, athlete.id)
    await events.audit(session, None, "athlete.deauthorize", {
        "athlete_id": athlete.id, "strava_id": strava_id, "deleted_activities": deleted,
    })
    await session.flush()
    return athlete


async def list_connected(session: AsyncSession) -> list[Athlete]:
    """Athlete active và còn token, sắp theo tên hiển thị."""
    res = await session.execute(
        select(Athlete).join(OAuthToken, OAuthToken.athlete_id == Athlete.id)
        .where(Athlete.active == True)  # noqa: E712
        .order_by(Athlete.display_name, Athlete.firstname, Athlete.id))
    return list(res.scalars().all())
