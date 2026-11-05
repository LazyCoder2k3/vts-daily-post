"""Nhận / chấm / lưu hoạt động — CHỦ SỞ HỮU: agent `services`.

Luồng: JSON Strava -> upsert Activity (thô) -> chọn event theo ngày địa phương -> engine.evaluate với
EvalContext (hoạt động cùng ngày, km đã tính) -> áp Override của admin -> upsert Evaluation -> thông báo.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, null, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import notify as notifier          # alias: tham số `notify: bool` của các hàm che tên module
from app.config import get_settings
from app.db.models import Activity, Athlete, Evaluation, Event, Override, WebhookEvent, utcnow
from app.rules import engine
from app.rules.schema import REASONS, ActivityData, EvalContext, RuleSetConfig, format_pace
from app.services import events as events_svc
from app.services import linking
from app.strava import tokens as strava_tokens
from app.strava.client import StravaClient, StravaNotFound

log = logging.getLogger(__name__)

OVERRIDE_ACTIONS = ("exclude", "include", "adjust_km")


@dataclass
class IngestResult:
    activity: Activity
    evaluation: Evaluation | None      # None nếu không có event active / ngoài mọi event
    created: bool                      # True = lần đầu thấy hoạt động này
    changed: bool                      # trạng thái valid/converted_km đổi so với trước


@dataclass
class ReevalSummary:
    total: int = 0
    changed: list[tuple[int, bool, bool]] = field(default_factory=list)   # (activity_id, valid cũ, valid mới)


# --------------------------------------------------------------------------- tiện ích
def _day_bounds(d: date) -> tuple[datetime, datetime]:
    lo = datetime.combine(d, time.min)
    return lo, lo + timedelta(days=1)


def _utc_to_local(dt: datetime, tz_name: str | None) -> datetime:
    """UTC naive -> giờ địa phương naive theo timezone của event."""
    try:
        tz = ZoneInfo(tz_name or "Asia/Ho_Chi_Minh")
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("Asia/Ho_Chi_Minh")
    return dt.replace(tzinfo=dt_timezone.utc).astimezone(tz).replace(tzinfo=None)


def _local_midnight_epoch(d: date, tz_name: str | None) -> int:
    try:
        tz = ZoneInfo(tz_name or "Asia/Ho_Chi_Minh")
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("Asia/Ho_Chi_Minh")
    return int(datetime.combine(d, time.min, tzinfo=tz).timestamp())


def _km(x: float) -> str:
    return f"{x:.2f}".replace(".", ",")


def _reason_text(reasons: list[str] | None) -> str:
    out = []
    for r in reasons or []:
        code, _, detail = str(r).partition(":")
        txt = REASONS.get(code, code)
        out.append(f"{txt} ({detail})" if detail else txt)
    return "; ".join(out) or "không hợp lệ"


def _apply_activity_data(activity: Activity, athlete: Athlete, ad: ActivityData, data: dict) -> None:
    activity.athlete_id = athlete.id
    activity.name = ad.name or ""
    activity.sport_type = ad.sport_type
    activity.type = ad.type
    activity.start_date = ad.start_date
    activity.start_date_local = ad.start_date_local
    activity.timezone = ad.timezone
    activity.distance_m = float(ad.distance_m or 0.0)
    activity.moving_s = int(ad.moving_s or 0)
    activity.elapsed_s = int(ad.elapsed_s or 0)
    activity.avg_speed = ad.avg_speed
    activity.elevation_gain = ad.elevation_gain
    activity.has_map = bool(ad.has_map)
    activity.manual = bool(ad.manual)
    activity.trainer = bool(ad.trainer)
    activity.flagged = bool(ad.flagged)
    activity.commute = bool(data.get("commute"))
    activity.private = bool(ad.private)
    activity.visibility = ad.visibility
    activity.device_name = ad.device_name
    activity.external_id = ad.external_id
    activity.upload_id = ad.upload_id
    activity.raw_json = data
    activity.deleted_at = None


async def _event_for_date(session: AsyncSession, local_date: date) -> Event | None:
    """Event active chứa ngày; nếu không, event bất kỳ (không draft) chứa ngày, mới nhất."""
    base = select(Event).where(Event.start_date <= local_date, Event.end_date >= local_date)
    res = await session.execute(base.where(Event.status == "active").order_by(Event.id.desc()).limit(1))
    ev = res.scalar_one_or_none()
    if ev is not None:
        return ev
    res = await session.execute(base.where(Event.status != "draft").order_by(Event.id.desc()).limit(1))
    return res.scalar_one_or_none()


async def _rules_and_version(session: AsyncSession, event: Event) -> tuple[RuleSetConfig, int]:
    return await events_svc.get_rules(session, event), await events_svc.get_rules_version(session, event)


async def _current_evaluation(session: AsyncSession, event_id: int, activity_id: int) -> Evaluation | None:
    res = await session.execute(
        select(Evaluation).where(Evaluation.event_id == event_id, Evaluation.activity_id == activity_id))
    return res.scalar_one_or_none()


# --------------------------------------------------------------------------- chấm
async def _evaluate_and_store(session: AsyncSession, event: Event, activity: Activity, athlete: Athlete,
                              rules: RuleSetConfig, version: int, *, earlier_only: bool = False
                              ) -> tuple[Evaluation, bool]:
    """Chấm `activity` theo `rules`, áp Override active, upsert Evaluation(activity, event).

    earlier_only=True (dùng khi chấm lại cả giải theo thứ tự thời gian): ngữ cảnh chỉ gồm các hoạt động
    cùng ngày bắt đầu TRƯỚC hoạt động này, để daily_cap/overlap nhất quán.
    Trả (evaluation, changed) — changed = valid hoặc converted_km đổi so với bản trước (bản mới -> True).
    """
    lo, hi = _day_bounds(activity.start_date_local.date())
    q = (select(Activity)
         .where(Activity.athlete_id == activity.athlete_id, Activity.id != activity.id,
                Activity.deleted_at.is_(None),
                Activity.start_date_local >= lo, Activity.start_date_local < hi))
    if earlier_only:
        q = q.where(Activity.start_date < activity.start_date)
    others = list((await session.execute(q.order_by(Activity.start_date, Activity.id))).scalars().all())

    counted = 0.0
    if others:
        res = await session.execute(
            select(func.coalesce(func.sum(Evaluation.converted_km), 0.0))
            .where(Evaluation.event_id == event.id, Evaluation.valid == True,  # noqa: E712
                   Evaluation.activity_id.in_([o.id for o in others])))
        counted = float(res.scalar() or 0.0)

    ctx = EvalContext(
        same_day_activities=[ActivityData.from_model(o) for o in others],
        counted_km_today=counted,
        now=_utc_to_local(activity.fetched_at or utcnow(), event.timezone),
    )
    result = engine.evaluate(ActivityData.from_model(activity), rules, ctx)

    valid, reasons, converted = bool(result.valid), list(result.reasons or []), float(result.converted_km or 0.0)
    overridden = False
    res = await session.execute(
        select(Override).where(Override.event_id == event.id, Override.activity_id == activity.id,
                               Override.active == True)  # noqa: E712
        .order_by(Override.id.desc()).limit(1))
    ov = res.scalar_one_or_none()
    if ov is not None:
        overridden = True
        if ov.action == "exclude":
            valid, reasons, converted = False, ["ADMIN_EXCLUDED"], 0.0
        elif ov.action == "include":
            sport = rules.sport_for(activity.sport_type)
            factor = sport.factor if sport is not None else 1.0
            valid, reasons, converted = True, [], float(result.raw_km) * factor
        elif ov.action == "adjust_km":
            valid, reasons, converted = True, [], float(ov.value or 0.0)

    evaluation = await _current_evaluation(session, event.id, activity.id)
    if evaluation is None:
        evaluation = Evaluation(activity_id=activity.id, event_id=event.id)
        session.add(evaluation)
        changed = True
    else:
        changed = (bool(evaluation.valid) != valid) or abs(float(evaluation.converted_km or 0.0) - converted) > 0.005
    evaluation.rules_version = version
    evaluation.valid = valid
    evaluation.reasons = reasons
    evaluation.converted_km = converted
    evaluation.raw_km = float(result.raw_km or 0.0)
    evaluation.sport_key = result.sport_key
    evaluation.pace_s = result.pace_s
    evaluation.overridden = overridden
    evaluation.evaluated_at = utcnow()
    await session.flush()
    return evaluation, changed


async def _notify_new(session: AsyncSession, athlete: Athlete, activity: Activity, ev: Evaluation) -> None:
    """Thông báo cho hoạt động MỚI theo cấu hình (AppSetting ghi đè Settings)."""
    s = get_settings()
    km = activity.distance_m / 1000.0
    when = activity.start_date_local.strftime("%d/%m %H:%M")
    name = activity.name or activity.sport_type
    if not ev.valid:
        if await events_svc.setting_bool(session, "notify_rejected_dm", s.notify_rejected_dm):
            await notifier.dm(athlete.telegram_id,
                              f"❌ Hoạt động '{name}' ({when}, {_km(km)} km) không được tính: "
                              f"{_reason_text(ev.reasons)}. Gõ /rules để xem luật.")
        return
    if await events_svc.setting_bool(session, "notify_valid_dm", s.notify_valid_dm):
        await notifier.dm(athlete.telegram_id,
                          f"✅ Hoạt động '{name}' ({when}, {_km(km)} km) được tính {_km(ev.converted_km)} km.")
    if await events_svc.setting_bool(session, "notify_valid_group", s.notify_valid_group):
        pace = ""
        if ev.pace_s is not None:
            unit = "/100m" if ev.sport_key == "swim" else "/km"
            pace = f" (pace {format_pace(ev.pace_s)}{unit})"
        await notifier.group(f"✅ {athlete.name} vừa hoàn thành {_km(km)} km {activity.sport_type}{pace}"
                             f" – {_km(ev.converted_km)} km quy đổi")


# --------------------------------------------------------------------------- nhận hoạt động
async def ingest_from_json(session: AsyncSession, athlete: Athlete, data: dict, *, notify: bool = True) -> IngestResult:
    """Lưu/ cập nhật Activity từ JSON Strava, chấm theo event active (hoặc event có cửa sổ chứa ngày hoạt động),
    áp override còn active, gửi thông báo qua app.notify theo cấu hình nếu notify=True."""
    ad = ActivityData.from_strava(data, athlete.id)
    activity = await session.get(Activity, ad.id)
    created = activity is None
    if activity is None:
        activity = Activity(id=ad.id, athlete_id=athlete.id, fetched_at=utcnow())
        session.add(activity)
    _apply_activity_data(activity, athlete, ad, data)
    await session.flush()

    event = await _event_for_date(session, ad.local_date)
    if event is None:
        return IngestResult(activity, None, created, False)
    rules, version = await _rules_and_version(session, event)
    evaluation, changed = await _evaluate_and_store(session, event, activity, athlete, rules, version)
    if notify and created:
        try:
            await _notify_new(session, athlete, activity, evaluation)
        except Exception as e:  # noqa: BLE001 - thông báo lỗi không được làm hỏng việc lưu
            log.warning("Không gửi được thông báo cho hoạt động %s: %s", activity.id, e)
    return IngestResult(activity, evaluation, created, changed)


async def ingest_activity(session: AsyncSession, client: StravaClient, athlete: Athlete, strava_activity_id: int,
                          *, notify: bool = True) -> IngestResult | None:
    """Lấy chi tiết từ Strava rồi gọi ingest_from_json. Trả None nếu 404."""
    token = await strava_tokens.get_valid_access_token(session, client, athlete)
    try:
        data = await client.get_activity(token, strava_activity_id)
    except StravaNotFound:
        log.info("Hoạt động %s không tồn tại / không có quyền xem", strava_activity_id)
        return None
    return await ingest_from_json(session, athlete, data, notify=notify)


async def mark_deleted(session: AsyncSession, strava_activity_id: int) -> Activity | None:
    activity = await session.get(Activity, strava_activity_id)
    if activity is None:
        return None
    activity.deleted_at = utcnow()
    await session.flush()
    return activity


async def handle_webhook_event(session: AsyncSession, client: StravaClient, event: WebhookEvent) -> None:
    """create/update -> ingest_activity; delete -> mark_deleted; athlete deauthorize -> linking.deauthorize.
    Ghi processed_at / error vào event. Không ném lỗi ra ngoài."""
    try:
        if event.object_type == "athlete":
            updates = (event.payload or {}).get("updates") or {}
            authorized = updates.get("authorized")
            if authorized is False or str(authorized).strip().lower() == "false":
                await linking.deauthorize(session, event.owner_id)
        elif event.object_type == "activity":
            athlete = await linking.get_athlete_by_strava(session, event.owner_id)
            if athlete is None or not athlete.active:
                log.info("Bỏ qua webhook %s/%s: athlete %s chưa kết nối hoặc đã gỡ",
                         event.aspect_type, event.object_id, event.owner_id)
            elif event.aspect_type == "delete":
                await mark_deleted(session, event.object_id)
            elif event.aspect_type in ("create", "update"):
                await ingest_activity(session, client, athlete, event.object_id,
                                      notify=(event.aspect_type == "create"))
        event.error = None
    except Exception as e:  # noqa: BLE001
        log.exception("Lỗi xử lý webhook %s %s/%s", event.object_type, event.aspect_type, event.object_id)
        event.error = str(e) or e.__class__.__name__
    finally:
        event.processed_at = utcnow()
        try:
            await session.flush()
        except Exception as e:  # noqa: BLE001
            log.error("Không ghi được trạng thái webhook %s: %s", event.id, e)


async def backfill_athlete(session: AsyncSession, client: StravaClient, athlete: Athlete, event: Event | None = None,
                           *, notify: bool = False) -> int:
    """Lấy mọi hoạt động trong cửa sổ event (start..end+grace) và ingest. Trả số hoạt động mới."""
    event = event or await events_svc.get_active_event(session)
    if event is None:
        return 0
    rules = await events_svc.get_rules(session, event)
    after = _local_midnight_epoch(event.start_date - timedelta(days=1), event.timezone)
    before = _local_midnight_epoch(event.end_date + timedelta(days=rules.event.grace_days + 1), event.timezone)
    token = await strava_tokens.get_valid_access_token(session, client, athlete)
    items = await client.list_all_activities(token, after=after, before=before)
    items = sorted(items, key=lambda d: (str(d.get("start_date") or ""), int(d.get("id") or 0)))
    created = 0
    for data in items:
        if "id" not in data:
            continue
        res = await ingest_from_json(session, athlete, data, notify=notify)
        if res.created:
            created += 1
    return created


async def reevaluate_event(session: AsyncSession, event: Event) -> ReevalSummary:
    """Chấm lại toàn bộ hoạt động trong cửa sổ event theo luật hiện hành (sau /rules set, /recheck)."""
    rules, version = await _rules_and_version(session, event)
    lo = datetime.combine(event.start_date, time.min)
    hi = datetime.combine(event.end_date + timedelta(days=rules.event.grace_days + 1), time.min)
    res = await session.execute(
        select(Activity).where(Activity.deleted_at.is_(None),
                               Activity.start_date_local >= lo, Activity.start_date_local < hi)
        .order_by(Activity.start_date, Activity.id))
    acts = list(res.scalars().all())
    summary = ReevalSummary(total=len(acts))
    athletes: dict[int, Athlete | None] = {}
    for a in acts:
        old = await _current_evaluation(session, event.id, a.id)
        old_valid = bool(old.valid) if old is not None else False
        if a.athlete_id not in athletes:
            athletes[a.athlete_id] = await session.get(Athlete, a.athlete_id)
        athlete = athletes[a.athlete_id]
        if athlete is None:
            continue
        ev, _ = await _evaluate_and_store(session, event, a, athlete, rules, version, earlier_only=True)
        if bool(ev.valid) != old_valid:
            summary.changed.append((a.id, old_valid, bool(ev.valid)))
    return summary


async def apply_override(session: AsyncSession, event: Event, activity_id: int, action: str, *, value: float | None,
                         note: str | None, by: int | None) -> Override:
    """exclude | include | adjust_km. Vô hiệu override cũ cùng activity, chấm lại hoạt động đó."""
    if action not in OVERRIDE_ACTIONS:
        raise ValueError(f"Hành động không hợp lệ: {action!r} (exclude | include | adjust_km)")
    if action == "adjust_km":
        if value is None:
            raise ValueError("adjust_km cần số km")
        value = float(value)
        if value < 0:
            raise ValueError("Số km phải >= 0")
    else:
        value = None
    res = await session.execute(
        select(Override).where(Override.event_id == event.id, Override.activity_id == activity_id,
                               Override.active == True))  # noqa: E712
    for old in res.scalars().all():
        old.active = False
    ov = Override(activity_id=activity_id, event_id=event.id, action=action, value=value, note=note,
                  by_telegram_id=by, active=True)
    session.add(ov)
    await session.flush()

    activity = await get_activity(session, activity_id)
    if activity is not None:
        athlete = await session.get(Athlete, activity.athlete_id)
        if athlete is not None:
            rules, version = await _rules_and_version(session, event)
            await _evaluate_and_store(session, event, activity, athlete, rules, version)
    await events_svc.audit(session, by, f"override.{action}", {
        "event_id": event.id, "activity_id": activity_id, "value": value, "note": note,
        "override_id": ov.id, "activity_found": activity is not None,
    })
    await session.flush()
    return ov


async def purge_raw_json(session: AsyncSession, older_than_days: int) -> int:
    cutoff = utcnow() - timedelta(days=older_than_days)
    # JSON(None) sẽ ghi chuỗi 'null' chứ không phải SQL NULL -> dùng null() để IS NOT NULL hoạt động đúng
    res = await session.execute(
        update(Activity).where(Activity.fetched_at < cutoff, Activity.raw_json.is_not(None))
        .values(raw_json=null()).execution_options(synchronize_session=False))
    await session.flush()
    return int(res.rowcount or 0)


async def get_activity(session: AsyncSession, activity_id: int) -> Activity | None:
    return await session.get(Activity, activity_id)
