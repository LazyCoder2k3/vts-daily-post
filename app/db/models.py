"""Mô hình dữ liệu. Nguyên tắc: lưu hoạt động thô (Activity), chấm điểm tách rời (Evaluation) theo
version luật (RuleSet) -> đổi luật thì chấm lại, không mất dữ liệu.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (JSON, BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Athlete(Base):
    __tablename__ = "athletes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strava_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    firstname: Mapped[str] = mapped_column(String(100), default="")
    lastname: Mapped[str] = mapped_column(String(100), default="")
    display_name: Mapped[str] = mapped_column(String(200), default="")   # tên hiển thị trong bảng xếp hạng
    profile_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sex: Mapped[str | None] = mapped_column(String(1), nullable=True)      # 'M' | 'F' từ Strava (nếu có)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    telegram_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    club_member: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # None = chưa kiểm tra
    consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    deauthorized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    token: Mapped["OAuthToken | None"] = relationship(back_populates="athlete", uselist=False,
                                                       cascade="all, delete-orphan")
    activities: Mapped[list["Activity"]] = relationship(back_populates="athlete")

    @property
    def name(self) -> str:
        return self.display_name or f"{self.firstname} {self.lastname}".strip() or f"#{self.strava_id}"


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), primary_key=True)
    access_token: Mapped[str] = mapped_column(Text)        # đã mã hoá qua app.security nếu có khoá
    refresh_token: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[int] = mapped_column(Integer)        # epoch giây
    scope: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    athlete: Mapped[Athlete] = relationship(back_populates="token")


class LinkToken(Base):
    """Mã tạm nối Telegram user -> phiên OAuth (state)."""
    __tablename__ = "link_tokens"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Event(Base):
    """Một giải / đợt thi. Chỉ một event ở trạng thái 'active' tại một thời điểm."""
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Ho_Chi_Minh")
    status: Mapped[str] = mapped_column(String(16), default="active")   # draft | active | closed
    current_rules_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    rule_sets: Mapped[list["RuleSet"]] = relationship(back_populates="event", cascade="all, delete-orphan")


class RuleSet(Base):
    __tablename__ = "rule_sets"
    __table_args__ = (UniqueConstraint("event_id", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    yaml_text: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    event: Mapped[Event] = relationship(back_populates="rule_sets")


class Activity(Base):
    """Hoạt động thô từ Strava. id = strava activity id."""
    __tablename__ = "activities"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    sport_type: Mapped[str] = mapped_column(String(50), index=True)   # Run, TrailRun, Ride, Swim...
    type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    start_date: Mapped[datetime] = mapped_column(DateTime, index=True)        # UTC (naive)
    start_date_local: Mapped[datetime] = mapped_column(DateTime, index=True)  # giờ địa phương (naive)
    timezone: Mapped[str | None] = mapped_column(String(80), nullable=True)
    distance_m: Mapped[float] = mapped_column(Float, default=0.0)
    moving_s: Mapped[int] = mapped_column(Integer, default=0)
    elapsed_s: Mapped[int] = mapped_column(Integer, default=0)
    avg_speed: Mapped[float | None] = mapped_column(Float, nullable=True)      # m/s
    elevation_gain: Mapped[float | None] = mapped_column(Float, nullable=True)
    has_map: Mapped[bool] = mapped_column(Boolean, default=False)              # map.summary_polyline không rỗng
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    trainer: Mapped[bool] = mapped_column(Boolean, default=False)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    commute: Mapped[bool] = mapped_column(Boolean, default=False)
    private: Mapped[bool] = mapped_column(Boolean, default=False)
    visibility: Mapped[str | None] = mapped_column(String(30), nullable=True)
    device_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    upload_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)  # xoá sau raw_retention_days
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    athlete: Mapped[Athlete] = relationship(back_populates="activities")
    evaluations: Mapped[list["Evaluation"]] = relationship(back_populates="activity", cascade="all, delete-orphan")


class Evaluation(Base):
    """Kết quả chấm một hoạt động theo một event + version luật."""
    __tablename__ = "evaluations"
    __table_args__ = (UniqueConstraint("activity_id", "event_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    activity_id: Mapped[int] = mapped_column(ForeignKey("activities.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    rules_version: Mapped[int] = mapped_column(Integer)
    valid: Mapped[bool] = mapped_column(Boolean, index=True)
    reasons: Mapped[list] = mapped_column(JSON, default=list)        # mã lý do, vd ["NO_GPS_MAP"]
    converted_km: Mapped[float] = mapped_column(Float, default=0.0)  # km quy đổi (0 nếu không hợp lệ)
    raw_km: Mapped[float] = mapped_column(Float, default=0.0)
    sport_key: Mapped[str | None] = mapped_column(String(40), nullable=True)   # 'run' | 'ride' | 'swim'...
    pace_s: Mapped[float | None] = mapped_column(Float, nullable=True)         # giây / đơn vị pace
    overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    activity: Mapped[Activity] = relationship(back_populates="evaluations")


class Override(Base):
    """Can thiệp tay của admin với một hoạt động trong một event."""
    __tablename__ = "overrides"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    activity_id: Mapped[int] = mapped_column(BigInteger, index=True)
    event_id: Mapped[int] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(16))        # exclude | include | adjust_km
    value: Mapped[float | None] = mapped_column(Float, nullable=True)   # km cho adjust_km
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    by_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Post(Base):
    __tablename__ = "posts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    post_date: Mapped[date] = mapped_column(Date, index=True)
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(30), default="template")
    sent_to: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    action: Mapped[str] = mapped_column(String(60), index=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class WebhookEvent(Base):
    """Sự kiện webhook nhận từ Strava (idempotency + debug)."""
    __tablename__ = "webhook_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    object_type: Mapped[str] = mapped_column(String(20))
    object_id: Mapped[int] = mapped_column(BigInteger, index=True)
    aspect_type: Mapped[str] = mapped_column(String(20))
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    event_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AppSetting(Base):
    """Cài đặt runtime bật/tắt từ Telegram (ghi đè Settings mặc định)."""
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
