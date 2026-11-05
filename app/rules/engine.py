"""Rule engine — CHỦ SỞ HỮU: agent `rules`. Chữ ký công khai (không đổi):

evaluate(activity: ActivityData, rules: RuleSetConfig, ctx: EvalContext | None = None) -> EvalResult
load_default_rules() -> RuleSetConfig

Nguyên tắc chấm:
- Thu thập TẤT CẢ lý do áp dụng (không dừng ở lý do đầu).
- `valid` = không có lý do "cứng". Lý do "mềm" (SOFT_REASONS) chỉ ghi chú, không loại hoạt động.
- Mã lý do theo `schema.REASONS`, chi tiết đi sau dấu ':' (vd 'PACE_TOO_FAST:3:41').
- Khi không khớp SportRule nào: bỏ qua các kiểm tra cần ngưỡng (quãng đường, pace, tỉ lệ di chuyển),
  vẫn chạy kiểm tra cờ (map, manual, trainer, flagged, visibility) và chồng thời gian.
- Giới hạn km/ngày chỉ áp dụng khi hoạt động đã hợp lệ theo mọi kiểm tra khác.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from app.rules.schema import (
    ActivityData,
    EvalContext,
    EvalResult,
    GeneralRules,
    RuleSetConfig,
    SportRule,
    format_pace,
)

DEFAULTS_PATH = Path(__file__).with_name("defaults.yaml")

#: Lý do "mềm": được ghi vào reasons nhưng không làm hoạt động mất hiệu lực.
SOFT_REASONS: frozenset[str] = frozenset({"DAILY_CAP_TRIMMED"})


def load_default_rules() -> RuleSetConfig:
    """Đọc bộ luật mặc định từ defaults.yaml."""
    return RuleSetConfig.from_yaml(DEFAULTS_PATH.read_text(encoding="utf-8"))


# ----------------------------------------------------------------- tiện ích lý do
def reason_code(reason: str) -> str:
    """'PACE_TOO_FAST:3:41' -> 'PACE_TOO_FAST'."""
    return reason.partition(":")[0]


def is_hard_reason(reason: str) -> bool:
    """Lý do cứng = làm hoạt động không hợp lệ."""
    return reason_code(reason) not in SOFT_REASONS


def _has_hard(reasons: list[str]) -> bool:
    return any(is_hard_reason(r) for r in reasons)


def _km(value: float) -> str:
    return f"{value:.2f}"


def _override(sport_value, general_value):
    """Giá trị của sport ghi đè general nếu khác None."""
    return general_value if sport_value is None else sport_value


# ----------------------------------------------------------------- các nhóm kiểm tra
def _check_event_window(activity: ActivityData, rules: RuleSetConfig, ctx: EvalContext) -> list[str]:
    """1. Ngày địa phương phải nằm trong [start, end]; đồng bộ sau end + grace_days bị loại."""
    reasons: list[str] = []
    ev = rules.event
    local_date = activity.local_date
    if not (ev.start <= local_date <= ev.end):
        reasons.append(f"OUTSIDE_EVENT_WINDOW:{local_date.isoformat()}")
    if ctx.now is not None and ctx.now.date() > ev.end + timedelta(days=ev.grace_days):
        reasons.append("AFTER_GRACE_PERIOD")
    return reasons


def activity_type_label(activity: ActivityData) -> str:
    """Nhãn loại dùng để tra luật/hiển thị: sport_type, thiếu thì type, thiếu nữa là 'Unknown'."""
    return (activity.sport_type or activity.type or "").strip() or "Unknown"


def resolve_sport(activity: ActivityData, rules: RuleSetConfig) -> tuple[SportRule | None, list[str]]:
    """2. Tìm SportRule: thử sport_type, không khớp thì thử type (loại gộp của Strava, vd TrailRun -> Run).

    Loại nằm trong excluded_types -> TYPE_EXCLUDED (không thêm TYPE_UNKNOWN nữa).
    """
    label = activity_type_label(activity)
    excluded = {t.lower() for t in rules.excluded_types}
    if label.lower() in excluded:
        return rules.sport_for(label), [f"TYPE_EXCLUDED:{label}"]
    sport = rules.sport_for(label)
    legacy = (activity.type or "").strip()
    if sport is None and legacy and legacy.lower() not in excluded:
        sport = rules.sport_for(legacy)
    if sport is None:
        return None, [f"TYPE_UNKNOWN:{label}"]
    return sport, []


def _check_flags(activity: ActivityData, general: GeneralRules, sport: SportRule | None) -> list[str]:
    """3. Cờ Strava: flagged, manual, trainer, bản đồ GPS, visibility."""
    reasons: list[str] = []
    if activity.flagged and general.reject_flagged:
        reasons.append("FLAGGED_BY_STRAVA")
    if activity.manual and general.reject_manual:
        reasons.append("MANUAL_ENTRY")
    trainer_policy = _override(sport.trainer_policy if sport else None, general.trainer_policy)
    if activity.trainer and trainer_policy == "reject":
        reasons.append("TRAINER")
    require_map = _override(sport.require_gps_map if sport else None, general.require_gps_map)
    if require_map and not activity.has_map:
        reasons.append("NO_GPS_MAP")
    if general.allowed_visibility is not None and activity.visibility is not None:
        allowed = {v.strip().lower() for v in general.allowed_visibility}
        if activity.visibility.strip().lower() not in allowed:
            reasons.append(f"VISIBILITY:{activity.visibility}")
    return reasons


def _check_distance(raw_km: float, sport: SportRule) -> list[str]:
    """4. Quãng đường tối thiểu / tối đa theo sport."""
    reasons: list[str] = []
    if raw_km < sport.min_distance_km:
        reasons.append(f"BELOW_MIN_DISTANCE:{_km(raw_km)}")
    if sport.max_distance_km is not None and raw_km > sport.max_distance_km:
        reasons.append(f"ABOVE_MAX_DISTANCE:{_km(raw_km)}")
    return reasons


def compute_pace(activity: ActivityData, sport: SportRule, pace_basis: str = "moving") -> float | None:
    """5. Pace (giây / đơn vị của sport). Quãng đường 0 -> None."""
    if activity.distance_m <= 0:
        return None
    seconds = activity.elapsed_s if pace_basis == "elapsed" else activity.moving_s
    if sport.pace_unit == "per_100m":
        return seconds / (activity.distance_m / 100.0)
    return seconds / (activity.distance_m / 1000.0)


def _check_pace(pace_s: float | None, sport: SportRule) -> list[str]:
    """5. Pace nằm trong [pace_min, pace_max] của sport."""
    if pace_s is None:
        return []
    reasons: list[str] = []
    pace_min, pace_max = sport.pace_min_s, sport.pace_max_s
    if pace_min is not None and pace_s < pace_min:
        reasons.append(f"PACE_TOO_FAST:{format_pace(pace_s)}")
    if pace_max is not None and pace_s > pace_max:
        reasons.append(f"PACE_TOO_SLOW:{format_pace(pace_s)}")
    return reasons


def _check_moving_ratio(activity: ActivityData, general: GeneralRules) -> list[str]:
    """6. moving/elapsed tối thiểu (thời gian dừng quá nhiều)."""
    ratio_min = general.min_moving_ratio
    if ratio_min is None or activity.elapsed_s <= 0:
        return []
    ratio = activity.moving_s / activity.elapsed_s
    if ratio < ratio_min:
        return [f"LOW_MOVING_RATIO:{ratio:.2f}"]
    return []


def _utc_interval(a: ActivityData) -> tuple[datetime, datetime]:
    return a.start_date, a.start_date + timedelta(seconds=a.elapsed_s or 0)


def _check_overlap(activity: ActivityData, general: GeneralRules, ctx: EvalContext) -> list[str]:
    """7. Chồng thời gian với hoạt động tải lên trước (id nhỏ hơn) của cùng athlete."""
    if general.overlap_policy != "reject_later":
        return []
    start, end = _utc_interval(activity)
    if end <= start:
        return []
    reasons: list[str] = []
    for other in sorted(ctx.same_day_activities, key=lambda o: o.id):
        if other.id >= activity.id or other.athlete_id != activity.athlete_id:
            continue
        o_start, o_end = _utc_interval(other)
        if o_start < end and start < o_end:
            reasons.append(f"OVERLAP:{other.id}")
    return reasons


def _apply_daily_cap(converted: float, general: GeneralRules, ctx: EvalContext) -> tuple[float, list[str]]:
    """8. Giới hạn km quy đổi / ngày: hết hạn mức -> DAILY_CAP; vượt một phần -> cắt (lý do mềm)."""
    cap = general.daily_cap_km
    if cap is None:
        return converted, []
    remaining = round(cap - ctx.counted_km_today, 3)
    if remaining <= 0:
        return converted, [f"DAILY_CAP:{_km(cap)}"]
    if converted > remaining:
        return remaining, [f"DAILY_CAP_TRIMMED:{_km(remaining)}"]
    return converted, []


# ----------------------------------------------------------------- điểm vào
def evaluate(activity: ActivityData, rules: RuleSetConfig, ctx: EvalContext | None = None) -> EvalResult:
    """Chấm một hoạt động theo bộ luật; trả về mọi lý do áp dụng và km quy đổi."""
    ctx = ctx or EvalContext()
    general = rules.general
    raw_km = activity.distance_m / 1000.0
    reasons: list[str] = []

    reasons += _check_event_window(activity, rules, ctx)
    sport, type_reasons = resolve_sport(activity, rules)
    reasons += type_reasons
    reasons += _check_flags(activity, general, sport)

    pace_s: float | None = None
    if sport is not None:
        reasons += _check_distance(raw_km, sport)
        pace_s = compute_pace(activity, sport, general.pace_basis)
        reasons += _check_pace(pace_s, sport)
        reasons += _check_moving_ratio(activity, general)

    reasons += _check_overlap(activity, general, ctx)

    factor = sport.factor if sport is not None else 1.0
    converted = round(raw_km * factor, 3)
    if not _has_hard(reasons):
        converted, cap_reasons = _apply_daily_cap(converted, general, ctx)
        reasons += cap_reasons

    valid = not _has_hard(reasons)
    return EvalResult(
        valid=valid,
        reasons=reasons,
        converted_km=converted if valid else 0.0,
        raw_km=round(raw_km, 3),
        sport_key=sport.key if sport is not None else None,
        pace_s=pace_s,
    )
