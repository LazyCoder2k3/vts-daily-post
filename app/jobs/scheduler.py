"""Lịch nền (APScheduler 3, AsyncIOScheduler) — chạy cùng process với FastAPI + bot.

Jobs (id):
  reconcile       mỗi settings.reconcile_interval_hours giờ  — app.jobs.reconcile.reconcile_all
  daily_post      CronTrigger settings.daily_post_time (HH:MM) — app.jobs.daily_post.run_daily_post
  backup          02:15 hằng ngày                              — app.jobs.backup.run_backup_async
  purge_raw       03:00 hằng ngày                              — activities.purge_raw_json(raw_retention_days)
  refresh_tokens  mỗi 60 phút                                  — strava_tokens.refresh_expiring

start_scheduler() -> AsyncIOScheduler (cần event loop đang chạy); shutdown_scheduler().
`scheduler` ở mức module để /status đọc get_jobs()/next_run_time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import notify
from app.config import get_settings
from app.db.base import session_scope
from app.deps import get_client
from app.jobs.backup import run_backup_async
from app.jobs.daily_post import run_daily_post
from app.jobs.reconcile import reconcile_all
from app.services import activities
from app.strava import tokens as strava_tokens
from app.strava.client import StravaRateLimited

log = logging.getLogger(__name__)

scheduler: AsyncIOScheduler | None = None

JOB_IDS = ("reconcile", "daily_post", "backup", "purge_raw", "refresh_tokens")


def _wrap(name: str, fn: Callable[[], Awaitable[object]]) -> Callable[[], Awaitable[None]]:
    """Bắt mọi lỗi của job: log + báo admin (rate limit chỉ log, tránh spam)."""
    async def job() -> None:
        log.info("[job:%s] bắt đầu", name)
        try:
            out = await fn()
            log.info("[job:%s] xong: %s", name, out if out is not None else "ok")
        except StravaRateLimited as e:
            log.warning("[job:%s] Strava giới hạn tần suất, thử lại sau ~%ss", name, e.retry_after)
        except Exception as e:  # noqa: BLE001
            log.exception("[job:%s] lỗi", name)
            try:
                await notify.admins(f"⚠️ Job {name} lỗi: {type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                pass
    job.__name__ = f"job_{name}"
    return job


async def job_reconcile() -> dict:
    return await reconcile_all(get_client())


async def job_daily_post() -> str:
    text = await run_daily_post()
    return f"{len(text)} ký tự" if text else "không có bài"


async def job_backup() -> str | None:
    return await run_backup_async()


async def job_purge_raw() -> int:
    days = get_settings().raw_retention_days
    async with session_scope() as s:
        return await activities.purge_raw_json(s, days)


async def job_refresh_tokens() -> int:
    async with session_scope() as s:
        return await strava_tokens.refresh_expiring(s, get_client())


def _parse_hhmm(value: str, default: tuple[int, int] = (6, 30)) -> tuple[int, int]:
    try:
        hh, mm = value.strip().split(":")
        hh, mm = int(hh), int(mm)
        if 0 <= hh < 24 and 0 <= mm < 60:
            return hh, mm
    except Exception:  # noqa: BLE001
        pass
    log.warning("[scheduler] DAILY_POST_TIME=%r không hợp lệ, dùng %02d:%02d", value, *default)
    return default


def start_scheduler() -> AsyncIOScheduler:
    global scheduler
    if scheduler is not None and scheduler.running:
        return scheduler
    settings = get_settings()
    tz = ZoneInfo(settings.timezone)
    sched = AsyncIOScheduler(timezone=tz)
    common = dict(coalesce=True, max_instances=1, misfire_grace_time=600, replace_existing=True)

    hours = max(1, int(settings.reconcile_interval_hours or 3))
    sched.add_job(_wrap("reconcile", job_reconcile), IntervalTrigger(hours=hours, timezone=tz),
                  id="reconcile", name="Đối soát Strava",
                  next_run_time=datetime.now(tz) + timedelta(minutes=5), **common)

    hh, mm = _parse_hhmm(settings.daily_post_time)
    sched.add_job(_wrap("daily_post", job_daily_post), CronTrigger(hour=hh, minute=mm, timezone=tz),
                  id="daily_post", name="Bài đăng sáng", **{**common, "misfire_grace_time": 3600})

    sched.add_job(_wrap("backup", job_backup), CronTrigger(hour=2, minute=15, timezone=tz),
                  id="backup", name="Backup DB", **{**common, "misfire_grace_time": 3600})

    sched.add_job(_wrap("purge_raw", job_purge_raw), CronTrigger(hour=3, minute=0, timezone=tz),
                  id="purge_raw", name="Xoá raw_json cũ", **{**common, "misfire_grace_time": 3600})

    sched.add_job(_wrap("refresh_tokens", job_refresh_tokens), IntervalTrigger(minutes=60, timezone=tz),
                  id="refresh_tokens", name="Refresh token Strava", **common)

    sched.start()
    scheduler = sched
    log.info("[scheduler] Đã khởi động %d job (tz=%s): %s", len(sched.get_jobs()), settings.timezone,
             ", ".join(j.id for j in sched.get_jobs()))
    return sched


def shutdown_scheduler() -> None:
    global scheduler
    if scheduler is not None:
        try:
            if scheduler.running:
                scheduler.shutdown(wait=False)
        except Exception as e:  # noqa: BLE001
            log.warning("[scheduler] Lỗi khi dừng: %s", e)
        scheduler = None


def jobs_status() -> list[dict]:
    """Cho /status: [{"id", "name", "next_run"}]."""
    if scheduler is None:
        return []
    out = []
    for j in scheduler.get_jobs():
        nrt = getattr(j, "next_run_time", None)
        out.append({"id": j.id, "name": j.name, "next_run": nrt.strftime("%H:%M %d/%m") if nrt else None})
    return out
