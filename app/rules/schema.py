"""Lược đồ bộ luật (YAML) + kiểu dữ liệu dùng chung cho rule engine.

- `RuleSetConfig.from_yaml(text)` / `.to_yaml()`
- `RuleSetConfig.with_value(path, value)` -> bản sao đã sửa một giá trị, vd "sports.run.pace_max" "18:00"
- `ActivityData`  : dạng phẳng của một hoạt động (không phụ thuộc ORM) để engine/test dùng
- `EvalContext`   : thông tin ngoài hoạt động (các hoạt động khác cùng ngày, km đã tính trong ngày)
- `EvalResult`    : kết quả chấm
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

PaceUnit = Literal["per_km", "per_100m"]


def parse_pace(value: str | int | float | None) -> float | None:
    """'4:00' -> 240.0 giây; '1:15' -> 75; số -> giây. None/'' -> None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if ":" in s:
        parts = s.split(":")
        if len(parts) == 2:
            m, sec = parts
            return int(m) * 60 + float(sec)
        if len(parts) == 3:
            h, m, sec = parts
            return int(h) * 3600 + int(m) * 60 + float(sec)
        raise ValueError(f"Pace không hợp lệ: {value!r}")
    return float(s)


def format_pace(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


class EventSection(BaseModel):
    name: str = "Giải chạy"
    start: date
    end: date
    timezone: str = "Asia/Ho_Chi_Minh"
    grace_days: int = 2          # hoạt động đồng bộ trễ sau ngày kết thúc vẫn tính nếu start_date còn trong giải
    target_km: float | None = None

    @model_validator(mode="after")
    def _check_dates(self):
        if self.end < self.start:
            raise ValueError("event.end phải >= event.start")
        return self


class GeneralRules(BaseModel):
    require_gps_map: bool = True
    reject_manual: bool = True
    reject_flagged: bool = True
    trainer_policy: Literal["reject", "allow"] = "reject"
    pace_basis: Literal["moving", "elapsed"] = "moving"
    overlap_policy: Literal["ignore", "reject_later"] = "reject_later"
    daily_cap_km: float | None = None          # tổng km quy đổi tối đa / người / ngày (None = không giới hạn)
    min_moving_ratio: float | None = None      # moving/elapsed tối thiểu (None = bỏ qua)
    allowed_visibility: list[str] | None = None  # None = không kiểm tra; vd ["everyone", "followers_only"]


class SportRule(BaseModel):
    key: str                                  # định danh ngắn: run | ride | swim ... (dùng trong /rules set)
    types: list[str]                          # sport_type Strava: Run, TrailRun, Walk, Hike, Ride, Swim...
    factor: float = 1.0                       # km quy đổi = km thực * factor
    min_distance_km: float = 0.0
    max_distance_km: float | None = None
    pace_unit: PaceUnit = "per_km"
    pace_min: str | None = None               # nhanh nhất cho phép, vd "4:00"
    pace_max: str | None = None               # chậm nhất cho phép, vd "17:00"
    require_gps_map: bool | None = None       # ghi đè general nếu đặt
    trainer_policy: Literal["reject", "allow"] | None = None

    @field_validator("pace_min", "pace_max")
    @classmethod
    def _valid_pace(cls, v):
        parse_pace(v)
        return v

    @field_validator("types")
    @classmethod
    def _norm_types(cls, v):
        return [t.strip() for t in v if t and t.strip()]

    @property
    def pace_min_s(self) -> float | None:
        return parse_pace(self.pace_min)

    @property
    def pace_max_s(self) -> float | None:
        return parse_pace(self.pace_max)


class RuleSetConfig(BaseModel):
    event: EventSection
    general: GeneralRules = Field(default_factory=GeneralRules)
    sports: list[SportRule]
    excluded_types: list[str] = Field(default_factory=list)
    awards: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_keys(self):
        keys = [s.key for s in self.sports]
        if len(keys) != len(set(keys)):
            raise ValueError("sports[].key phải duy nhất")
        return self

    # ----- YAML
    @classmethod
    def from_yaml(cls, text: str) -> "RuleSetConfig":
        data = yaml.safe_load(text) or {}
        return cls.model_validate(data)

    def to_yaml(self) -> str:
        data = self.model_dump(mode="json")
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)

    # ----- tra cứu
    def sport_for(self, sport_type: str) -> SportRule | None:
        st = (sport_type or "").strip().lower()
        for s in self.sports:
            if st in (t.lower() for t in s.types):
                return s
        return None

    def sport_by_key(self, key: str) -> SportRule | None:
        return next((s for s in self.sports if s.key == key), None)

    # ----- sửa một giá trị theo đường dẫn
    def with_value(self, path: str, value: Any) -> "RuleSetConfig":
        """Trả về bản sao đã đặt `path` = value.

        path dạng 'general.pace_basis', 'event.grace_days', 'sports.run.pace_max', 'excluded_types'.
        value là chuỗi từ Telegram: tự ép kiểu (true/false, số, 'a,b,c' cho list, 'null' cho None).
        """
        data = self.model_dump(mode="json")
        parts = path.split(".")
        node: Any = data
        for i, p in enumerate(parts[:-1]):
            if isinstance(node, list) and p.isdigit():
                node = node[int(p)]
            elif isinstance(node, list) and parts[i - 1] == "sports":
                match = next((s for s in node if s.get("key") == p), None)
                if match is None:
                    raise KeyError(f"Không có sport key '{p}'")
                node = match
            elif isinstance(node, dict):
                if p not in node:
                    raise KeyError(f"Không có mục '{p}'")
                node = node[p]
            else:
                raise KeyError(f"Đường dẫn không hợp lệ: {path}")
        last = parts[-1]
        if isinstance(node, list) and parts[-2] == "sports":
            raise KeyError("Hãy chỉ rõ trường: sports.<key>.<field>")
        if not isinstance(node, dict) or (last not in node and last != "__new__"):
            raise KeyError(f"Không có mục '{last}'")
        coerced = coerce_value(value, node.get(last))
        node[last] = coerced
        try:
            return RuleSetConfig.model_validate(copy.deepcopy(data))
        except ValidationError:
            # Trường đang null nhưng kiểu là list (vd allowed_visibility): thử tách chuỗi thành list
            if isinstance(coerced, str):
                node[last] = [x.strip() for x in coerced.strip("[]").split(",") if x.strip()]
                return RuleSetConfig.model_validate(copy.deepcopy(data))
            raise


def coerce_value(value: Any, current: Any) -> Any:
    """Ép chuỗi người dùng gõ về kiểu của giá trị hiện tại."""
    if not isinstance(value, str):
        return value
    v = value.strip()
    if v.lower() in ("null", "none", "~", ""):
        return None
    if isinstance(current, bool) or v.lower() in ("true", "false", "yes", "no", "on", "off"):
        return v.lower() in ("true", "yes", "on", "1")
    if isinstance(current, list):
        return [x.strip() for x in v.strip("[]").split(",") if x.strip()]
    if isinstance(current, (int, float)) and not isinstance(current, bool):
        try:
            return int(v) if isinstance(current, int) and v.lstrip("-").isdigit() else float(v)
        except ValueError:
            pass
    return v


# ----------------------------------------------------------------- dữ liệu cho engine
@dataclass
class ActivityData:
    """Hoạt động dạng phẳng. Tạo từ JSON Strava (`from_strava`) hoặc ORM (`from_model`)."""
    id: int
    athlete_id: int                      # id nội bộ (athletes.id) hoặc strava athlete id — chỉ cần nhất quán
    sport_type: str
    start_date: datetime                 # UTC naive
    start_date_local: datetime           # giờ địa phương naive
    distance_m: float
    moving_s: int
    elapsed_s: int
    name: str = ""
    has_map: bool = False
    manual: bool = False
    trainer: bool = False
    flagged: bool = False
    private: bool = False
    visibility: str | None = None
    device_name: str | None = None
    avg_speed: float | None = None
    elevation_gain: float | None = None
    timezone: str | None = None
    type: str | None = None
    external_id: str | None = None
    upload_id: int | None = None
    raw: dict | None = None

    @property
    def local_date(self) -> date:
        return self.start_date_local.date()

    @property
    def end_date_local(self) -> datetime:
        from datetime import timedelta
        return self.start_date_local + timedelta(seconds=self.elapsed_s or 0)

    @classmethod
    def from_strava(cls, d: dict, athlete_id: int) -> "ActivityData":
        def _dt(s: str | None) -> datetime:
            if not s:
                return datetime(1970, 1, 1)
            return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
        m = d.get("map") or {}
        return cls(
            id=int(d["id"]), athlete_id=athlete_id,
            sport_type=d.get("sport_type") or d.get("type") or "Unknown",
            type=d.get("type"), name=d.get("name") or "",
            start_date=_dt(d.get("start_date")), start_date_local=_dt(d.get("start_date_local")),
            timezone=d.get("timezone"),
            distance_m=float(d.get("distance") or 0.0),
            moving_s=int(d.get("moving_time") or 0), elapsed_s=int(d.get("elapsed_time") or 0),
            avg_speed=d.get("average_speed"), elevation_gain=d.get("total_elevation_gain"),
            has_map=bool(m.get("summary_polyline") or m.get("polyline")),
            manual=bool(d.get("manual")), trainer=bool(d.get("trainer")), flagged=bool(d.get("flagged")),
            private=bool(d.get("private")), visibility=d.get("visibility"), device_name=d.get("device_name"),
            external_id=d.get("external_id"), upload_id=d.get("upload_id"), raw=d,
        )

    @classmethod
    def from_model(cls, a) -> "ActivityData":  # a: app.db.models.Activity
        return cls(
            id=a.id, athlete_id=a.athlete_id, sport_type=a.sport_type, type=a.type, name=a.name,
            start_date=a.start_date, start_date_local=a.start_date_local, timezone=a.timezone,
            distance_m=a.distance_m, moving_s=a.moving_s, elapsed_s=a.elapsed_s, avg_speed=a.avg_speed,
            elevation_gain=a.elevation_gain, has_map=a.has_map, manual=a.manual, trainer=a.trainer,
            flagged=a.flagged, private=a.private, visibility=a.visibility, device_name=a.device_name,
            external_id=a.external_id, upload_id=a.upload_id, raw=a.raw_json,
        )


@dataclass
class EvalContext:
    """Thông tin ngoài hoạt động đang chấm (do services cung cấp)."""
    same_day_activities: list[ActivityData] = field(default_factory=list)  # cùng athlete, cùng ngày địa phương, trừ chính nó
    counted_km_today: float = 0.0        # km quy đổi đã hợp lệ trong ngày (trừ chính nó), cho daily_cap_km
    now: datetime | None = None          # giờ hiện tại (local) để xét grace_days; None = không xét


# Mã lý do loại — dùng thống nhất ở engine, bot, báo cáo
REASONS: dict[str, str] = {
    "OUTSIDE_EVENT_WINDOW": "ngoài thời gian giải",
    "AFTER_GRACE_PERIOD": "đồng bộ quá hạn sau khi giải kết thúc",
    "TYPE_EXCLUDED": "loại hoạt động không được tính",
    "TYPE_UNKNOWN": "loại hoạt động không có trong luật",
    "NO_GPS_MAP": "không có bản đồ GPS",
    "MANUAL_ENTRY": "nhập tay",
    "TRAINER": "máy tập / treadmill",
    "FLAGGED_BY_STRAVA": "bị Strava gắn cờ",
    "BELOW_MIN_DISTANCE": "chưa đủ quãng đường tối thiểu",
    "ABOVE_MAX_DISTANCE": "vượt quãng đường tối đa",
    "PACE_TOO_FAST": "pace nhanh bất thường",
    "PACE_TOO_SLOW": "pace quá chậm",
    "LOW_MOVING_RATIO": "thời gian dừng quá nhiều",
    "OVERLAP": "trùng thời gian với hoạt động khác",
    "DAILY_CAP": "vượt giới hạn km trong ngày",
    "DAILY_CAP_TRIMMED": "đã cắt theo giới hạn km trong ngày",
    "VISIBILITY": "chế độ hiển thị không được tính",
    "ADMIN_EXCLUDED": "ban tổ chức loại",
    "DELETED": "đã xoá trên Strava",
}


@dataclass
class EvalResult:
    valid: bool
    reasons: list[str]                   # mã trong REASONS (có thể kèm chi tiết sau dấu ':' vd 'PACE_TOO_FAST:3:41')
    converted_km: float                  # 0 nếu không hợp lệ
    raw_km: float
    sport_key: str | None = None
    pace_s: float | None = None          # giây / đơn vị pace của sport (per_km hoặc per_100m)

    def reason_text(self) -> str:
        out = []
        for r in self.reasons:
            code, _, detail = r.partition(":")
            txt = REASONS.get(code, code)
            out.append(f"{txt} ({detail})" if detail else txt)
        return "; ".join(out)
