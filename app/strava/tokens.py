"""Quản lý token theo athlete — CHỦ SỞ HỮU: agent `strava`.

async def get_valid_access_token(session, client, athlete) -> str
    Giải mã token từ DB; nếu hết hạn trong <5 phút thì refresh, lưu lại (mã hoá). Ném StravaAuthError nếu không thể.
async def store_token(session, athlete, token: TokenResponse) -> OAuthToken
async def refresh_expiring(session, client, within_s: int = 1800) -> int   # refresh hàng loạt, trả số token đã làm
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Athlete, OAuthToken
from app.security import decrypt, encrypt
from app.strava.client import StravaAuthError, StravaClient, StravaError, StravaRateLimited, TokenResponse

log = logging.getLogger(__name__)

REFRESH_MARGIN_S = 300   # refresh khi còn dưới 5 phút


def _apply(tok: OAuthToken, token: TokenResponse) -> None:
    tok.access_token = encrypt(token.access_token)
    if token.refresh_token:                 # Strava có thể trả refresh_token mới; giữ cũ nếu không có
        tok.refresh_token = encrypt(token.refresh_token)
    tok.expires_at = int(token.expires_at)
    if token.scope:                         # phản hồi refresh không có scope -> giữ scope đã cấp
        tok.scope = token.scope


async def store_token(session: AsyncSession, athlete: Athlete, token: TokenResponse) -> OAuthToken:
    """Upsert OAuthToken cho athlete (access/refresh được mã hoá qua app.security)."""
    if athlete.id is None:
        session.add(athlete)
        await session.flush()
    tok = await session.get(OAuthToken, athlete.id)
    if tok is None:
        tok = OAuthToken(athlete_id=athlete.id, access_token="", refresh_token="", expires_at=0, scope="")
        session.add(tok)
    _apply(tok, token)
    await session.flush()
    return tok


async def _refresh_into(client: StravaClient, tok: OAuthToken) -> TokenResponse:
    """Gọi Strava refresh với refresh_token đã giải mã; 4xx -> StravaAuthError."""
    refresh_token = decrypt(tok.refresh_token)
    if not refresh_token:
        raise StravaAuthError("Athlete không có refresh_token")
    try:
        return await client.refresh(refresh_token)
    except StravaAuthError:
        raise
    except StravaRateLimited:
        raise
    except StravaError as e:
        if e.status is not None and 400 <= e.status < 500:
            err = StravaAuthError(f"Không refresh được token Strava: {e}")
            err.status = e.status
            raise err from e
        raise


async def get_valid_access_token(session: AsyncSession, client: StravaClient, athlete: Athlete) -> str:
    if athlete.id is None:
        raise StravaAuthError(f"Athlete {athlete.name} chưa được lưu / chưa có token")
    tok = await session.get(OAuthToken, athlete.id)
    if tok is None:
        raise StravaAuthError(f"Athlete {athlete.name} chưa kết nối Strava (không có token)")
    now = int(time.time())
    if tok.expires_at - now >= REFRESH_MARGIN_S:
        return decrypt(tok.access_token)
    new = await _refresh_into(client, tok)
    await store_token(session, athlete, new)
    return new.access_token


async def refresh_expiring(session: AsyncSession, client: StravaClient, within_s: int = 1800) -> int:
    """Refresh mọi token của athlete đang active sắp hết hạn trong `within_s` giây. Lỗi từng token chỉ log."""
    now = int(time.time())
    stmt = (
        select(OAuthToken)
        .join(Athlete, Athlete.id == OAuthToken.athlete_id)
        .where(Athlete.active.is_(True), OAuthToken.expires_at <= now + int(within_s))
        .order_by(OAuthToken.expires_at)
    )
    tokens = (await session.execute(stmt)).scalars().all()
    done = 0
    for tok in tokens:
        try:
            new = await _refresh_into(client, tok)
            _apply(tok, new)
            await session.flush()
            done += 1
        except StravaRateLimited as e:
            log.warning("Strava rate limit khi refresh token athlete_id=%s, dừng đợt này (thử lại sau %ss)",
                        tok.athlete_id, e.retry_after)
            break
        except Exception as e:  # noqa: BLE001
            log.warning("Không refresh được token athlete_id=%s: %s", tok.athlete_id, e)
    return done
