"""Bài đăng sáng: tổng hợp từ DB -> composer -> lưu Post -> gửi admin (và nhóm nếu bật).

run_daily_post(day=None, *, dry_run=False, send_group=None) -> str (nội dung bài; "" nếu không có gì để đăng)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from sqlalchemy import select

from app import composer, notify
from app.bot.format import fmt_date, vn_today
from app.config import get_settings
from app.db.base import session_scope
from app.db.models import Event, Post
from app.rules.schema import RuleSetConfig, SportRule
from app.services import events, stats

log = logging.getLogger(__name__)

SPORT_LABELS = {
    "run": "Chạy/đi bộ", "walk": "Đi bộ", "hike": "Leo núi", "ride": "Đạp xe", "swim": "Bơi",
    "trail": "Chạy trail", "row": "Chèo thuyền",
}


def _num(x: float) -> str:
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def _sport_summary(sp: SportRule) -> str:
    label = SPORT_LABELS.get(sp.key, sp.key.capitalize())
    if sp.factor and abs(sp.factor - 1.0) > 1e-9:
        label += f" ×{_num(sp.factor)}"
    unit = "100m" if sp.pace_unit == "per_100m" else "km"
    parts: list[str] = []
    if sp.pace_min and sp.pace_max:
        parts.append(f"pace {sp.pace_min}–{sp.pace_max}/{unit}")
    elif sp.pace_max:
        parts.append(f"pace chậm nhất {sp.pace_max}/{unit}")
    elif sp.pace_min:
        parts.append(f"pace nhanh nhất {sp.pace_min}/{unit}")
    if sp.min_distance_km:
        parts.append(f"tối thiểu {_num(sp.min_distance_km)} km")
    if sp.max_distance_km:
        parts.append(f"tối đa {_num(sp.max_distance_km)} km")
    return f"{label}: {', '.join(parts)}" if parts else label


def rules_summary(cfg: RuleSetConfig) -> str | None:
    """Một dòng tóm tắt luật tính km từ cfg.sports, vd:
    'Chạy/đi bộ: pace 4:00–17:00/km, tối thiểu 1 km; Đạp xe ×0,25: pace 1:30–10:00/km, tối thiểu 4 km.'"""
    if not cfg.sports:
        return None
    text = "; ".join(_sport_summary(sp) for sp in cfg.sports)
    return text + "." if not text.endswith(".") else text


def composer_cfg(settings, event: Event, rules: RuleSetConfig) -> dict:
    """cfg cho app.composer từ Settings + Event + RuleSetConfig."""
    return {
        "start": event.start_date,
        "end": event.end_date,
        "event_name": (rules.event.name or event.name or "").strip() or event.name,
        "target_km": rules.event.target_km,
        "hashtags": settings.hashtags,
        "show_daily_km": settings.show_daily_km,
        "llm_provider": settings.llm_provider,
        "anthropic_key": settings.anthropic_api_key,
        "anthropic_model": settings.anthropic_model,
        "gemini_key": settings.gemini_api_key,
        "gemini_model": settings.gemini_model,
        "founded_year": settings.founded_year,
    }


async def _pick_event(session, day: date) -> Event | None:
    """Event active; nếu không có -> event mới nhất còn trong hạn đăng (end_date >= day - 2)."""
    event = await events.get_active_event(session)
    if event is not None:
        return event
    cands = [e for e in await events.list_events(session) if e.end_date >= day - timedelta(days=2)]
    if not cands:
        return None
    return max(cands, key=lambda e: (e.end_date, e.id))


async def _recent_posts(session, limit: int = 5) -> list[str]:
    """Nội dung 5 bài gần nhất, cũ -> mới (để LLM tránh lặp)."""
    res = await session.execute(select(Post).order_by(Post.post_date.desc(), Post.id.desc()).limit(limit))
    posts = list(res.scalars().all())
    return [p.text for p in reversed(posts)]


def _awards_value(awards: dict, key: str):
    v = awards.get(key) if isinstance(awards, dict) else None
    if v in (None, "", 0, False):
        return None
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


async def run_daily_post(day: date | None = None, *, dry_run: bool = False, send_group: bool | None = None) -> str:
    settings = get_settings()
    day = day or vn_today(settings.timezone)
    report_day = day - timedelta(days=1)

    async with session_scope() as s:
        event = await _pick_event(s, day)
        if event is None:
            log.info("[daily_post] Không có giải nào phù hợp để đăng bài ngày %s.", day)
            return ""
        if day <= event.start_date:
            log.info("[daily_post] %s: giải '%s' chưa có ngày chạy nào (bắt đầu %s), bỏ qua.",
                     day, event.name, event.start_date)
            return ""
        if day > event.end_date + timedelta(days=1):
            log.info("[daily_post] %s: giải '%s' đã kết thúc từ %s, bỏ qua.", day, event.name, event.end_date)
            return ""

        rules = await events.get_rules(s, event)
        rank = await stats.ranking(s, event, until=report_day)
        rows = [{"nick": r.name, "total": float(r.km)} for r in rank]
        daily_rows = [r for r in await stats.daily(s, event, report_day) if r.km > 0]
        daily = {
            "top": [(r.name, float(r.km)) for r in daily_rows[:3]],
            "active": len(daily_rows),
            "sum": float(sum(r.km for r in daily_rows)),
        }
        history = await _recent_posts(s)

        cfg = composer_cfg(settings, event, rules)
        ctx = composer.build_context(day, rows, daily, report_day, cfg)
        ctx["rules_summary"] = rules_summary(rules)
        ctx["streak_days"] = _awards_value(rules.awards, "streak_days")
        ctx["female_target_km"] = _awards_value(rules.awards, "female_target_km")

        draft = composer.compose(ctx)
        text, source = await asyncio.to_thread(composer.rewrite_with_llm, ctx, draft, history, cfg)

        if dry_run:
            return text

        if send_group is None:
            to_group = await events.setting_bool(s, "auto_post_group", settings.auto_post_group)
        else:
            to_group = bool(send_group)
        to_group = to_group and settings.group_chat_id is not None
        sent_to = "admins" + (",group" if to_group else "")
        s.add(Post(event_id=event.id, post_date=day, text=text, source=source, sent_to=sent_to))

    await notify.admins(f"📋 Bài đăng {fmt_date(day)} (nguồn: {source})")
    await notify.admins(text)
    if to_group:
        ok = await notify.group(text)
        log.info("[daily_post] Đã gửi bài %s vào nhóm: %s", day, ok)
    else:
        log.info("[daily_post] Đã gửi bài %s cho admin (chưa gửi nhóm).", day)
    return text
