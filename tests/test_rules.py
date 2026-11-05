"""Test rule engine: evaluate() với bộ luật mặc định (defaults.yaml) và các biến thể qua with_value()."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.rules.engine import (
    SOFT_REASONS,
    compute_pace,
    evaluate,
    is_hard_reason,
    load_default_rules,
    reason_code,
)
from app.rules.schema import REASONS, ActivityData, EvalContext, RuleSetConfig

# Giờ địa phương Asia/Ho_Chi_Minh = UTC+7. Ngày 2027-01-11 nằm trong cửa sổ defaults.yaml (01-01..01-31).
LOCAL_START = datetime(2027, 1, 11, 6, 0, 0)
UTC_OFFSET = timedelta(hours=7)


def make_activity(**overrides) -> ActivityData:
    """Hoạt động mặc định HỢP LỆ: Run 5 km, pace 6:00/km, có bản đồ, trong cửa sổ giải."""
    start_local = overrides.pop("start_date_local", LOCAL_START)
    start_utc = overrides.pop("start_date", start_local - UTC_OFFSET)
    data = dict(
        id=1001,
        athlete_id=1,
        sport_type="Run",
        type="Run",
        start_date=start_utc,
        start_date_local=start_local,
        distance_m=5000.0,
        moving_s=1800,
        elapsed_s=1850,
        name="Morning Run",
        has_map=True,
        visibility="everyone",
    )
    data.update(overrides)
    return ActivityData(**data)


def codes(result) -> list[str]:
    return [reason_code(r) for r in result.reasons]


@pytest.fixture(autouse=True)
def _settings():
    """Rule engine thuần, không cần Settings: ghi đè fixture autouse của conftest để test độc lập với .env."""
    yield None


@pytest.fixture
def rules() -> RuleSetConfig:
    return load_default_rules()


# ----------------------------------------------------------------- bộ luật mặc định
def test_load_default_rules(rules):
    assert rules.event.start.isoformat() == "2027-01-01"
    assert rules.event.end.isoformat() == "2027-01-31"
    assert rules.event.grace_days == 2
    assert [s.key for s in rules.sports] == ["run", "ride", "swim"]
    assert "VirtualRide" in rules.excluded_types


def test_daily_cap_trimmed_is_soft_reason():
    assert "DAILY_CAP_TRIMMED" in REASONS
    assert "DAILY_CAP_TRIMMED" in SOFT_REASONS
    assert not is_hard_reason("DAILY_CAP_TRIMMED:2.50")
    assert is_hard_reason("DAILY_CAP:10.00")
    assert is_hard_reason("NO_GPS_MAP")


# ----------------------------------------------------------------- run cơ bản
def test_valid_run(rules):
    r = evaluate(make_activity(), rules)
    assert r.valid is True
    assert r.reasons == []
    assert r.sport_key == "run"
    assert r.raw_km == 5.0
    assert r.converted_km == 5.0
    assert r.pace_s == pytest.approx(360.0)


def test_pace_too_fast(rules):
    r = evaluate(make_activity(moving_s=1050, elapsed_s=1050), rules)  # 3:30/km
    assert r.valid is False
    assert r.reasons == ["PACE_TOO_FAST:3:30"]
    assert r.converted_km == 0.0


def test_pace_too_slow(rules):
    r = evaluate(make_activity(moving_s=5400, elapsed_s=5400), rules)  # 18:00/km
    assert r.valid is False
    assert r.reasons == ["PACE_TOO_SLOW:18:00"]


def test_no_gps_map(rules):
    r = evaluate(make_activity(has_map=False), rules)
    assert r.valid is False
    assert r.reasons == ["NO_GPS_MAP"]


def test_manual_entry(rules):
    r = evaluate(make_activity(manual=True), rules)
    assert r.valid is False
    assert r.reasons == ["MANUAL_ENTRY"]


def test_flagged_by_strava(rules):
    r = evaluate(make_activity(flagged=True), rules)
    assert r.reasons == ["FLAGGED_BY_STRAVA"]
    assert evaluate(make_activity(flagged=True), rules.with_value("general.reject_flagged", "false")).valid


def test_trainer_reject_then_allow(rules):
    act = make_activity(trainer=True)  # vẫn có map để chỉ kiểm tra chính sách trainer
    assert evaluate(act, rules).reasons == ["TRAINER"]

    allowed = rules.with_value("general.trainer_policy", "allow")
    r = evaluate(act, allowed)
    assert r.valid is True
    assert r.reasons == []


def test_sport_trainer_policy_overrides_general(rules):
    variant = rules.with_value("sports.run.trainer_policy", "allow")
    assert evaluate(make_activity(trainer=True), variant).valid is True
    # ride không ghi đè -> vẫn theo general = reject
    ride = make_activity(sport_type="Ride", type="Ride", trainer=True, distance_m=8000, moving_s=1600, elapsed_s=1600)
    assert "TRAINER" in evaluate(ride, variant).reasons


def test_walk_counts_as_run_group(rules):
    r = evaluate(make_activity(sport_type="Walk", type="Walk", moving_s=3600, elapsed_s=3600), rules)  # 12:00/km
    assert r.valid is True
    assert r.sport_key == "run"
    assert r.converted_km == 5.0


def test_type_matching_is_case_insensitive(rules):
    r = evaluate(make_activity(sport_type="trailrun", type=None), rules)
    assert r.valid is True
    assert r.sport_key == "run"


def test_type_fallback_to_legacy_type(rules):
    # sport_type trống -> dùng type
    assert evaluate(make_activity(sport_type="", type="Run"), rules).sport_key == "run"
    # sport_type lạ nhưng type gộp là Ride -> tính theo ride
    r = evaluate(make_activity(sport_type="Velomobile", type="Ride", distance_m=8000, moving_s=1600, elapsed_s=1600), rules)
    assert r.sport_key == "ride"
    assert r.valid is True


# ----------------------------------------------------------------- ride / swim
def test_ride_8km_converts_to_2km(rules):
    r = evaluate(make_activity(sport_type="Ride", type="Ride", distance_m=8000, moving_s=1600, elapsed_s=1600), rules)
    assert r.valid is True
    assert r.sport_key == "ride"
    assert r.raw_km == 8.0
    assert r.converted_km == 2.0
    assert r.pace_s == pytest.approx(200.0)  # 3:20/km


def test_ride_3km_below_min_distance(rules):
    r = evaluate(make_activity(sport_type="Ride", type="Ride", distance_m=3000, moving_s=600, elapsed_s=600), rules)
    assert r.valid is False
    assert r.reasons == ["BELOW_MIN_DISTANCE:3.00"]


def test_swim_1km_in_25_minutes(rules):
    r = evaluate(make_activity(sport_type="Swim", type="Swim", distance_m=1000, moving_s=1500, elapsed_s=1500), rules)
    assert r.valid is True
    assert r.sport_key == "swim"
    assert r.pace_s == pytest.approx(150.0)  # 2:30 / 100m
    assert r.converted_km == 4.0


def test_swim_too_slow(rules):
    r = evaluate(make_activity(sport_type="Swim", type="Swim", distance_m=1000, moving_s=3000, elapsed_s=3000), rules)
    assert r.valid is False
    assert r.reasons == ["PACE_TOO_SLOW:5:00"]


def test_swim_too_fast(rules):
    r = evaluate(make_activity(sport_type="Swim", type="Swim", distance_m=1000, moving_s=600, elapsed_s=600), rules)
    assert r.reasons == ["PACE_TOO_FAST:1:00"]


# ----------------------------------------------------------------- loại hoạt động
def test_virtual_ride_excluded(rules):
    r = evaluate(make_activity(sport_type="VirtualRide", type="VirtualRide", distance_m=20000, moving_s=3000, elapsed_s=3000), rules)
    assert r.valid is False
    assert r.reasons == ["TYPE_EXCLUDED:VirtualRide"]
    assert r.sport_key is None
    assert r.converted_km == 0.0


def test_unknown_type_golf(rules):
    r = evaluate(make_activity(sport_type="Golf", type="Golf", has_map=True), rules)
    assert r.valid is False
    assert r.reasons == ["TYPE_UNKNOWN:Golf"]
    assert r.sport_key is None
    assert r.pace_s is None
    assert r.raw_km == 5.0


def test_unknown_type_still_checks_flags(rules):
    r = evaluate(make_activity(sport_type="Golf", type="Golf", has_map=False, manual=True), rules)
    assert set(codes(r)) == {"TYPE_UNKNOWN", "NO_GPS_MAP", "MANUAL_ENTRY"}


# ----------------------------------------------------------------- cửa sổ giải
def test_outside_event_window(rules):
    before = make_activity(start_date_local=datetime(2026, 12, 31, 20, 0))
    r = evaluate(before, rules)
    assert r.valid is False
    assert r.reasons == ["OUTSIDE_EVENT_WINDOW:2026-12-31"]

    after = make_activity(start_date_local=datetime(2027, 2, 1, 5, 0))
    assert evaluate(after, rules).reasons == ["OUTSIDE_EVENT_WINDOW:2027-02-01"]

    # biên: ngày đầu và ngày cuối đều hợp lệ
    assert evaluate(make_activity(start_date_local=datetime(2027, 1, 1, 5, 0)), rules).valid
    assert evaluate(make_activity(start_date_local=datetime(2027, 1, 31, 23, 30)), rules).valid


def test_after_grace_period(rules):
    act = make_activity(start_date_local=datetime(2027, 1, 31, 18, 0))
    # end 01-31 + grace 2 = 02-02: còn trong hạn
    assert evaluate(act, rules, EvalContext(now=datetime(2027, 2, 2, 23, 59))).valid is True
    r = evaluate(act, rules, EvalContext(now=datetime(2027, 2, 3, 0, 1)))
    assert r.valid is False
    assert r.reasons == ["AFTER_GRACE_PERIOD"]
    # không truyền now -> không xét
    assert evaluate(act, rules, EvalContext()).valid is True
    # grace_days đổi qua with_value
    assert evaluate(act, rules.with_value("event.grace_days", "5"), EvalContext(now=datetime(2027, 2, 3))).valid


# ----------------------------------------------------------------- chồng thời gian
def test_overlap_rejects_later_upload_keeps_earlier(rules):
    first = make_activity(id=100, start_date_local=datetime(2027, 1, 11, 6, 0), elapsed_s=1800, moving_s=1800)
    second = make_activity(id=200, start_date_local=datetime(2027, 1, 11, 6, 20), elapsed_s=1800, moving_s=1800)

    r_second = evaluate(second, rules, EvalContext(same_day_activities=[first]))
    assert r_second.valid is False
    assert r_second.reasons == ["OVERLAP:100"]

    r_first = evaluate(first, rules, EvalContext(same_day_activities=[second]))
    assert r_first.valid is True
    assert r_first.reasons == []


def test_overlap_not_triggered_when_disjoint_or_other_athlete_or_ignored(rules):
    first = make_activity(id=100, start_date_local=datetime(2027, 1, 11, 6, 0), elapsed_s=1800, moving_s=1800)
    later_disjoint = make_activity(id=200, start_date_local=datetime(2027, 1, 11, 6, 30), elapsed_s=1800, moving_s=1800)
    assert evaluate(later_disjoint, rules, EvalContext(same_day_activities=[first])).valid is True

    overlapping = make_activity(id=200, start_date_local=datetime(2027, 1, 11, 6, 20), elapsed_s=1800, moving_s=1800)
    other_athlete = make_activity(id=100, athlete_id=2, start_date_local=datetime(2027, 1, 11, 6, 0), elapsed_s=1800, moving_s=1800)
    assert evaluate(overlapping, rules, EvalContext(same_day_activities=[other_athlete])).valid is True

    ignored = rules.with_value("general.overlap_policy", "ignore")
    assert evaluate(overlapping, ignored, EvalContext(same_day_activities=[first])).valid is True


# ----------------------------------------------------------------- giới hạn ngày
def test_daily_cap_rejects_when_quota_used_up(rules):
    capped = rules.with_value("general.daily_cap_km", "10")
    r = evaluate(make_activity(), capped, EvalContext(counted_km_today=10.0))
    assert r.valid is False
    assert codes(r) == ["DAILY_CAP"]
    assert r.converted_km == 0.0


def test_daily_cap_trims_partially_but_stays_valid(rules):
    capped = rules.with_value("general.daily_cap_km", "10")
    r = evaluate(make_activity(), capped, EvalContext(counted_km_today=7.5))
    assert r.valid is True
    assert r.reasons == ["DAILY_CAP_TRIMMED:2.50"]
    assert r.converted_km == 2.5
    assert r.raw_km == 5.0


def test_daily_cap_not_applied_when_under_quota_or_invalid(rules):
    capped = rules.with_value("general.daily_cap_km", "10")
    r = evaluate(make_activity(), capped, EvalContext(counted_km_today=3.0))
    assert r.valid is True
    assert r.reasons == []
    assert r.converted_km == 5.0
    # hoạt động đã không hợp lệ -> không thêm lý do cắt
    r2 = evaluate(make_activity(has_map=False), capped, EvalContext(counted_km_today=7.5))
    assert r2.reasons == ["NO_GPS_MAP"]
    assert r2.converted_km == 0.0


# ----------------------------------------------------------------- visibility
def test_visibility_filter(rules):
    variant = rules.with_value("general.allowed_visibility", ["everyone", "followers_only"])
    r = evaluate(make_activity(visibility="only_me"), variant)
    assert r.valid is False
    assert r.reasons == ["VISIBILITY:only_me"]
    assert evaluate(make_activity(visibility="followers_only"), variant).valid is True
    # visibility None (không có dữ liệu) -> không xét
    assert evaluate(make_activity(visibility=None), variant).valid is True
    # mặc định allowed_visibility=null -> không xét
    assert evaluate(make_activity(visibility="only_me"), rules).valid is True


# ----------------------------------------------------------------- pace_basis / ghi đè sport
def test_pace_basis_elapsed_changes_result(rules):
    act = make_activity(moving_s=1800, elapsed_s=6000)  # 6:00/km moving, 20:00/km elapsed
    r_moving = evaluate(act, rules)
    assert r_moving.valid is True
    assert r_moving.pace_s == pytest.approx(360.0)

    r_elapsed = evaluate(act, rules.with_value("general.pace_basis", "elapsed"))
    assert r_elapsed.valid is False
    assert r_elapsed.reasons == ["PACE_TOO_SLOW:20:00"]
    assert r_elapsed.pace_s == pytest.approx(1200.0)


def test_sport_require_gps_map_overrides_general(rules):
    variant = rules.with_value("sports.run.require_gps_map", "false")
    assert evaluate(make_activity(has_map=False), variant).valid is True
    ride = make_activity(sport_type="Ride", type="Ride", has_map=False, distance_m=8000, moving_s=1600, elapsed_s=1600)
    assert evaluate(ride, variant).reasons == ["NO_GPS_MAP"]


def test_min_moving_ratio(rules):
    act = make_activity(moving_s=1800, elapsed_s=3600)
    assert evaluate(act, rules).valid is True  # mặc định null -> bỏ qua
    r = evaluate(act, rules.with_value("general.min_moving_ratio", "0.8"))
    assert r.valid is False
    assert r.reasons == ["LOW_MOVING_RATIO:0.50"]


def test_max_distance(rules):
    variant = rules.with_value("sports.run.max_distance_km", "42.5")
    r = evaluate(make_activity(distance_m=50000, moving_s=18000, elapsed_s=18000), variant)
    assert r.reasons == ["ABOVE_MAX_DISTANCE:50.00"]


def test_zero_distance_has_no_pace(rules):
    r = evaluate(make_activity(distance_m=0, moving_s=600, elapsed_s=600), rules)
    assert r.pace_s is None
    assert r.reasons == ["BELOW_MIN_DISTANCE:0.00"]
    assert r.valid is False


def test_compute_pace_helper(rules):
    run = rules.sport_by_key("run")
    swim = rules.sport_by_key("swim")
    assert compute_pace(make_activity(), run) == pytest.approx(360.0)
    assert compute_pace(make_activity(moving_s=1800, elapsed_s=3600), run, "elapsed") == pytest.approx(720.0)
    assert compute_pace(make_activity(distance_m=1000, moving_s=1500), swim) == pytest.approx(150.0)


# ----------------------------------------------------------------- nhiều lý do, văn bản
def test_collects_multiple_reasons(rules):
    act = make_activity(manual=True, has_map=False, trainer=True, distance_m=500, moving_s=60, elapsed_s=60)
    r = evaluate(act, rules)
    assert r.valid is False
    assert r.reasons == [
        "MANUAL_ENTRY",
        "TRAINER",
        "NO_GPS_MAP",
        "BELOW_MIN_DISTANCE:0.50",
        "PACE_TOO_FAST:2:00",
    ]
    assert r.converted_km == 0.0


def test_reason_text_is_vietnamese(rules):
    r = evaluate(make_activity(has_map=False, moving_s=1050, elapsed_s=1050), rules)
    text = r.reason_text()
    assert text == "không có bản đồ GPS; pace nhanh bất thường (3:30)"

    excluded = evaluate(make_activity(sport_type="VirtualRide", type="VirtualRide"), rules)
    assert excluded.reason_text() == "loại hoạt động không được tính (VirtualRide)"

    trimmed = evaluate(make_activity(), rules.with_value("general.daily_cap_km", "7"), EvalContext(counted_km_today=5))
    assert trimmed.reason_text() == "đã cắt theo giới hạn km trong ngày (2.00)"
