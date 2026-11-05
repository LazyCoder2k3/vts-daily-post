"""Định dạng văn bản Telegram dùng chung (member + admin). Không phụ thuộc aiogram."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from app.rules.schema import format_pace

MEDALS = ["🥇", "🥈", "🥉"]


def fmt_km(x: float, nd: int = 2) -> str:
    return f"{x:.{nd}f}".replace(".", ",")


def fmt_duration(seconds: int | float) -> str:
    s = int(seconds or 0)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fmt_pace(pace_s: float | None, unit: str = "per_km") -> str:
    if pace_s is None:
        return "-"
    return format_pace(pace_s) + ("/100m" if unit == "per_100m" else "/km")


def fmt_date(d: date | datetime) -> str:
    return f"{d.day:02d}/{d.month:02d}"


def fmt_datetime(d: datetime) -> str:
    return f"{d.hour:02d}:{d.minute:02d} {d.day:02d}/{d.month:02d}"


def medal(i: int) -> str:
    return MEDALS[i] if i < 3 else f"{i + 1}."


def ranking_text(rows, title: str, *, limit: int = 20, show_days: bool = True) -> str:
    """rows: list[app.services.stats.RankRow]."""
    lines = [title]
    if not rows:
        lines.append("(chưa có hoạt động hợp lệ)")
        return "\n".join(lines)
    for i, r in enumerate(rows[:limit]):
        extra = f" · {r.days} ngày" if show_days else ""
        lines.append(f"{medal(i)} {r.name} – {fmt_km(r.km)} km{extra}")
    if len(rows) > limit:
        lines.append(f"… và {len(rows) - limit} người khác")
    return "\n".join(lines)


def activity_line(a, ev=None) -> str:
    """a: Activity, ev: Evaluation|None -> '✅ 05/11 06:12 Run 5,20 km 6:05/km' hoặc '❌ … (lý do)'."""
    from app.rules.schema import REASONS
    km = fmt_km(a.distance_m / 1000)
    pace = "-"
    if ev is not None and ev.pace_s is not None:
        unit = "per_100m" if (ev.sport_key == "swim") else "per_km"
        pace = fmt_pace(ev.pace_s, unit)
    head = f"{fmt_datetime(a.start_date_local)} {a.sport_type} {km} km {pace}"
    if ev is None:
        return f"⚪ {head} (chưa chấm)"
    if ev.valid:
        conv = f" → {fmt_km(ev.converted_km)} km quy đổi" if abs(ev.converted_km - a.distance_m / 1000) > 0.005 else ""
        return f"✅ {head}{conv}"
    reasons = []
    for r in ev.reasons or []:
        code, _, detail = r.partition(":")
        t = REASONS.get(code, code)
        reasons.append(f"{t} ({detail})" if detail else t)
    return f"❌ {head} — {'; '.join(reasons) or 'không hợp lệ'}"


def vn_today(tz_name: str = "Asia/Ho_Chi_Minh") -> date:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(tz_name)).date()


def yesterday(tz_name: str = "Asia/Ho_Chi_Minh") -> date:
    return vn_today(tz_name) - timedelta(days=1)
