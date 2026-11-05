"""Đối soát định kỳ với Strava (bù webhook bị lỡ, phát hiện hoạt động đã xoá) + refresh token hàng loạt.

reconcile_all(client) -> {"athletes", "new", "deleted", "errors": [str], "tokens_refreshed"}
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import notify
from app.config import get_settings
from app.db.base import session_scope
from app.db.models import Activity, Athlete
from app.services import activities, linking
from app.strava import tokens as strava_tokens
from app.strava.client import StravaAuthError, StravaClient, StravaRateLimited

log = logging.getLogger(__name__)


async def _reconcile_athlete(session, client: StravaClient, athlete: Athlete, after_dt: datetime) -> tuple[int, int]:
    """Đồng bộ một athlete trong session đã mở. Trả (số hoạt động mới, số hoạt động phát hiện đã xoá)."""
    token = await strava_tokens.get_valid_access_token(session, client, athlete)
    data = await client.list_all_activities(token, after=int(after_dt.replace(tzinfo=timezone.utc).timestamp()))

    created = 0
    seen: set[int] = set()
    for js in data:
        try:
            seen.add(int(js["id"]))
        except (KeyError, TypeError, ValueError):
            continue
        res = await activities.ingest_from_json(session, athlete, js, notify=False)
        if res is not None and getattr(res, "created", False):
            created += 1

    deleted = 0
    q = select(Activity.id).where(Activity.athlete_id == athlete.id,
                                  Activity.start_date >= after_dt,
                                  Activity.deleted_at.is_(None))
    known = [int(x) for x in (await session.execute(q)).scalars().all()]
    for aid in known:
        if aid not in seen:
            await activities.mark_deleted(session, aid)
            deleted += 1
    return created, deleted


async def _deactivate(athlete_id: int) -> None:
    async with session_scope() as s:
        a = await s.get(Athlete, athlete_id)
        if a is not None:
            a.active = False


async def reconcile_all(client: StravaClient) -> dict:
    settings = get_settings()
    result: dict = {"athletes": 0, "new": 0, "deleted": 0, "errors": [], "tokens_refreshed": 0}
    after_dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=settings.reconcile_lookback_days)

    async with session_scope() as s:
        roster = list(await linking.list_connected(s))
    result["athletes"] = len(roster)

    for item in roster:
        name = getattr(item, "name", None) or f"#{getattr(item, 'strava_id', item.id)}"
        try:
            async with session_scope() as s:
                athlete = await s.get(Athlete, item.id) or item
                created, deleted = await _reconcile_athlete(s, client, athlete, after_dt)
            result["new"] += created
            result["deleted"] += deleted
        except StravaAuthError as e:
            msg = f"{name}: mất quyền truy cập Strava ({e}) — đã tắt theo dõi, cần /connect lại."
            log.warning("[reconcile] %s", msg)
            result["errors"].append(msg)
            try:
                await _deactivate(item.id)
            except Exception as e2:  # noqa: BLE001
                log.exception("[reconcile] Không tắt được athlete %s: %s", item.id, e2)
            await notify.admins(f"⚠️ {msg}")
        except StravaRateLimited as e:
            msg = f"Strava giới hạn tần suất (thử lại sau ~{e.retry_after}s), dừng đối soát tại {name}."
            log.warning("[reconcile] %s", msg)
            result["errors"].append(msg)
            break
        except Exception as e:  # noqa: BLE001
            log.exception("[reconcile] Lỗi với %s", name)
            result["errors"].append(f"{name}: {type(e).__name__}: {e}")

    try:
        async with session_scope() as s:
            result["tokens_refreshed"] = await strava_tokens.refresh_expiring(s, client, within_s=3600 * 2)
    except StravaRateLimited as e:
        result["errors"].append(f"Refresh token: Strava giới hạn tần suất (thử lại sau ~{e.retry_after}s).")
    except Exception as e:  # noqa: BLE001
        log.exception("[reconcile] Lỗi refresh token hàng loạt")
        result["errors"].append(f"Refresh token: {type(e).__name__}: {e}")

    log.info("[reconcile] %d VĐV, +%d mới, %d đã xoá, %d token refresh, %d lỗi",
             result["athletes"], result["new"], result["deleted"], result["tokens_refreshed"], len(result["errors"]))
    if result["errors"]:
        lines = "\n".join(f"• {e}" for e in result["errors"][:10])
        more = f"\n… và {len(result['errors']) - 10} lỗi khác" if len(result["errors"]) > 10 else ""
        await notify.admins(f"⚠️ Đối soát Strava có {len(result['errors'])} lỗi "
                            f"({result['athletes']} VĐV, +{result['new']} mới, {result['deleted']} xoá):\n{lines}{more}")
    return result
