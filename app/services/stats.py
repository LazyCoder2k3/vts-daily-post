"""Thống kê / bảng xếp hạng — CHỦ SỞ HỮU: agent `services`. Chỉ tính Evaluation.valid=True và Activity.deleted_at IS NULL.

"Ngày" của một hoạt động = date(Activity.start_date_local). `until`: chỉ tính hoạt động có ngày địa phương <= until.
Xếp hạng: km quy đổi giảm dần, rồi số ngày giảm dần, rồi tên; rank 1..n (không đồng hạng).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Activity, Athlete, Evaluation, Event
from app.rules.schema import format_pace


@dataclass
class RankRow:
    athlete_id: int
    name: str
    km: float                 # km quy đổi
    days: int                 # số ngày có hoạt động hợp lệ
    activities: int
    rank: int = 0
    sex: str | None = None
    telegram_id: int | None = None


@dataclass
class AthleteSummary:
    athlete: Athlete
    total_km: float
    days: int
    rank: int | None
    total_athletes: int
    recent: list[tuple[Activity, Evaluation | None]] = field(default_factory=list)


@dataclass
class TeamTotals:
    sum_km: float
    active_athletes: int
    activities: int


# --------------------------------------------------------------------------- truy vấn nền
def _athlete_name(display_name: str | None, firstname: str | None, lastname: str | None, strava_id: int) -> str:
    return display_name or f"{firstname or ''} {lastname or ''}".strip() or f"#{strava_id}"


def _until_bound(until: date | None) -> datetime | None:
    return datetime.combine(until + timedelta(days=1), time.min) if until is not None else None


def _event_window(event: Event) -> tuple[datetime, datetime]:
    return datetime.combine(event.start_date, time.min), datetime.combine(event.end_date + timedelta(days=1), time.min)


def _valid_base(event: Event, *, until: date | None = None) -> Select:
    """SELECT ... FROM evaluations JOIN activities JOIN athletes WHERE hợp lệ + chưa xoá (+ until)."""
    q = (select(Evaluation, Activity, Athlete)
         .select_from(Evaluation)
         .join(Activity, Activity.id == Evaluation.activity_id)
         .join(Athlete, Athlete.id == Activity.athlete_id)
         .where(Evaluation.event_id == event.id, Evaluation.valid == True,  # noqa: E712
                Activity.deleted_at.is_(None)))
    bound = _until_bound(until)
    if bound is not None:
        q = q.where(Activity.start_date_local < bound)
    return q


def _aggregate(event: Event, *, until: date | None = None, day: date | None = None) -> Select:
    """Một dòng mỗi athlete: tổng km quy đổi, số ngày khác nhau, số hoạt động."""
    day_expr = func.date(Activity.start_date_local)
    q = (select(Athlete.id, Athlete.display_name, Athlete.firstname, Athlete.lastname, Athlete.strava_id,
                Athlete.sex, Athlete.telegram_id,
                func.coalesce(func.sum(Evaluation.converted_km), 0.0).label("km"),
                func.count(func.distinct(day_expr)).label("days"),
                func.count(Evaluation.id).label("activities"))
         .select_from(Evaluation)
         .join(Activity, Activity.id == Evaluation.activity_id)
         .join(Athlete, Athlete.id == Activity.athlete_id)
         .where(Evaluation.event_id == event.id, Evaluation.valid == True,  # noqa: E712
                Activity.deleted_at.is_(None))
         .group_by(Athlete.id))
    bound = _until_bound(until)
    if bound is not None:
        q = q.where(Activity.start_date_local < bound)
    if day is not None:
        lo = datetime.combine(day, time.min)
        q = q.where(Activity.start_date_local >= lo, Activity.start_date_local < lo + timedelta(days=1))
    return q


def _rank_rows(rows) -> list[RankRow]:
    out = [RankRow(athlete_id=r[0], name=_athlete_name(r[1], r[2], r[3], r[4]), km=float(r[7] or 0.0),
                   days=int(r[8] or 0), activities=int(r[9] or 0), sex=r[5], telegram_id=r[6])
           for r in rows]
    out.sort(key=lambda x: (-round(x.km, 6), -x.days, x.name.lower(), x.athlete_id))
    for i, r in enumerate(out, start=1):
        r.rank = i
    return out


# --------------------------------------------------------------------------- API
async def ranking(session: AsyncSession, event: Event, *, until: date | None = None, limit: int | None = None) -> list[RankRow]:
    rows = (await session.execute(_aggregate(event, until=until))).all()
    ranked = _rank_rows(rows)
    return ranked[:limit] if limit is not None else ranked


async def daily(session: AsyncSession, event: Event, day: date) -> list[RankRow]:
    """Km quy đổi hợp lệ theo start_date_local.date() == day, sắp xếp giảm dần."""
    rows = (await session.execute(_aggregate(event, day=day))).all()
    return _rank_rows(rows)


async def athlete_summary(session: AsyncSession, event: Event, athlete: Athlete, recent_n: int = 5) -> AthleteSummary:
    full = await ranking(session, event)
    mine = next((r for r in full if r.athlete_id == athlete.id), None)
    recent = await activity_log(session, event, athlete, n=recent_n)
    return AthleteSummary(
        athlete=athlete,
        total_km=mine.km if mine else 0.0,
        days=mine.days if mine else 0,
        rank=mine.rank if mine else None,
        total_athletes=len(full),
        recent=recent,
    )


async def team_totals(session: AsyncSession, event: Event, *, until: date | None = None) -> TeamTotals:
    q = (select(func.coalesce(func.sum(Evaluation.converted_km), 0.0),
                func.count(func.distinct(Activity.athlete_id)),
                func.count(Evaluation.id))
         .select_from(Evaluation)
         .join(Activity, Activity.id == Evaluation.activity_id)
         .where(Evaluation.event_id == event.id, Evaluation.valid == True,  # noqa: E712
                Activity.deleted_at.is_(None)))
    bound = _until_bound(until)
    if bound is not None:
        q = q.where(Activity.start_date_local < bound)
    km, athletes, acts = (await session.execute(q)).one()
    return TeamTotals(sum_km=float(km or 0.0), active_athletes=int(athletes or 0), activities=int(acts or 0))


async def activity_log(session: AsyncSession, event: Event, athlete: Athlete, n: int = 10
                       ) -> list[tuple[Activity, Evaluation | None]]:
    """n hoạt động gần nhất của athlete trong cửa sổ event (cả hợp lệ và không), kèm Evaluation của event hoặc None."""
    lo, hi = _event_window(event)
    q = (select(Activity, Evaluation)
         .outerjoin(Evaluation, (Evaluation.activity_id == Activity.id) & (Evaluation.event_id == event.id))
         .where(Activity.athlete_id == athlete.id, Activity.deleted_at.is_(None),
                Activity.start_date_local >= lo, Activity.start_date_local < hi)
         .order_by(Activity.start_date_local.desc(), Activity.id.desc())
         .limit(n))
    return [(a, ev) for a, ev in (await session.execute(q)).all()]


async def export_rows(session: AsyncSession, event: Event) -> list[dict]:
    """Mỗi hoạt động một dòng (cả hợp lệ và không), cho /export csv."""
    lo, hi = _event_window(event)
    q = (select(Activity, Evaluation, Athlete)
         .join(Athlete, Athlete.id == Activity.athlete_id)
         .outerjoin(Evaluation, (Evaluation.activity_id == Activity.id) & (Evaluation.event_id == event.id))
         .where(Activity.deleted_at.is_(None), Activity.start_date_local >= lo, Activity.start_date_local < hi)
         .order_by(Athlete.display_name, Athlete.id, Activity.start_date_local, Activity.id))
    rows: list[dict] = []
    for a, ev, ath in (await session.execute(q)).all():
        km = a.distance_m / 1000.0
        if ev is not None and ev.pace_s is not None:
            pace = format_pace(ev.pace_s)
        elif km > 0 and a.moving_s:
            pace = format_pace(a.moving_s / km)
        else:
            pace = ""
        rows.append({
            "activity_id": a.id,
            "athlete": ath.name,
            "strava_id": ath.strava_id,
            "telegram": ath.telegram_id,
            "sport_type": a.sport_type,
            "start_local": a.start_date_local.isoformat(),
            "distance_km": round(km, 3),
            "moving": a.moving_s,
            "elapsed": a.elapsed_s,
            "pace": pace,
            "has_map": a.has_map,
            "manual": a.manual,
            "trainer": a.trainer,
            "device": a.device_name or "",
            "valid": bool(ev.valid) if ev is not None else None,
            "reasons": ";".join(ev.reasons or []) if ev is not None else "",
            "converted_km": round(float(ev.converted_km), 3) if ev is not None else 0.0,
            "overridden": bool(ev.overridden) if ev is not None else False,
        })
    return rows


def longest_streak(days: set[date] | list[date]) -> int:
    """Chuỗi ngày liên tục dài nhất trong tập ngày."""
    s = set(days)
    best = 0
    for d in s:
        if d - timedelta(days=1) in s:
            continue   # không phải điểm đầu chuỗi
        n, cur = 1, d
        while cur + timedelta(days=1) in s:
            cur += timedelta(days=1)
            n += 1
        best = max(best, n)
    return best


async def streaks(session: AsyncSession, event: Event) -> dict[int, int]:
    """athlete_id -> chuỗi ngày liên tục dài nhất có hoạt động hợp lệ."""
    q = (select(Activity.athlete_id, func.date(Activity.start_date_local))
         .select_from(Evaluation)
         .join(Activity, Activity.id == Evaluation.activity_id)
         .where(Evaluation.event_id == event.id, Evaluation.valid == True,  # noqa: E712
                Activity.deleted_at.is_(None))
         .distinct())
    by_athlete: dict[int, set[date]] = {}
    for athlete_id, d in (await session.execute(q)).all():
        if d is None:
            continue
        dd = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
        by_athlete.setdefault(athlete_id, set()).add(dd)
    return {aid: longest_streak(ds) for aid, ds in by_athlete.items()}
