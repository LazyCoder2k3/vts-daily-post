"""Quản lý giải và luật — CHỦ SỞ HỮU: agent `services`.

- Event: chỉ một 'active' tại một thời điểm (create_event/set_event_status tự đóng event active khác).
- RuleSet: mỗi lần đổi luật tạo version mới (không sửa tại chỗ) và trỏ Event.current_rules_id vào đó.
- AppSetting: cài đặt runtime (chuỗi) bật/tắt từ Telegram, ghi đè Settings mặc định.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppSetting, AuditLog, Event, RuleSet
from app.rules import engine
from app.rules.schema import RuleSetConfig

EVENT_STATUSES = ("draft", "active", "closed")


# --------------------------------------------------------------------------- event
async def get_active_event(session: AsyncSession) -> Event | None:
    res = await session.execute(
        select(Event).where(Event.status == "active").order_by(Event.id.desc()).limit(1))
    return res.scalar_one_or_none()


async def get_event(session: AsyncSession, event_id: int) -> Event | None:
    return await session.get(Event, event_id)


async def list_events(session: AsyncSession) -> list[Event]:
    res = await session.execute(select(Event).order_by(Event.id.desc()))
    return list(res.scalars().all())


# --------------------------------------------------------------------------- rules
def _with_event_fields(cfg: RuleSetConfig, *, name: str, start: date, end: date,
                       timezone: str | None = None) -> RuleSetConfig:
    """Bản sao cfg với event.name/start/end (và timezone nếu có) = tham số, có kiểm tra (end >= start)."""
    data = cfg.model_dump(mode="json")
    data["event"].update({"name": name, "start": start.isoformat(), "end": end.isoformat()})
    if timezone:
        data["event"]["timezone"] = timezone
    return RuleSetConfig.model_validate(data)


async def _current_ruleset(session: AsyncSession, event: Event) -> RuleSet | None:
    if event.current_rules_id is None:
        return None
    return await session.get(RuleSet, event.current_rules_id)


async def get_rules(session: AsyncSession, event: Event) -> RuleSetConfig:
    """Luật hiện hành của event; chưa có RuleSet -> luật mặc định gắn tên/ngày của event (không lưu)."""
    rs = await _current_ruleset(session, event)
    if rs is not None:
        return RuleSetConfig.from_yaml(rs.yaml_text)
    cfg = engine.load_default_rules()
    return _with_event_fields(cfg, name=event.name, start=event.start_date, end=event.end_date,
                              timezone=event.timezone)


async def get_rules_version(session: AsyncSession, event: Event) -> int:
    rs = await _current_ruleset(session, event)
    return rs.version if rs is not None else 0


async def _close_other_active(session: AsyncSession, keep_id: int | None) -> list[int]:
    q = select(Event).where(Event.status == "active")
    if keep_id is not None:
        q = q.where(Event.id != keep_id)
    closed: list[int] = []
    for ev in (await session.execute(q)).scalars().all():
        ev.status = "closed"
        closed.append(ev.id)
    return closed


async def create_event(session: AsyncSession, name: str, start: date, end: date, *, by: int | None,
                       base: RuleSetConfig | None = None, activate: bool = True) -> Event:
    """Tạo event + RuleSet version 1 (kế thừa `base` hoặc luật mặc định). activate=True: đóng event active khác."""
    cfg = _with_event_fields(base or engine.load_default_rules(), name=name, start=start, end=end)
    closed: list[int] = []
    if activate:
        closed = await _close_other_active(session, None)
    event = Event(name=name, start_date=start, end_date=end, timezone=cfg.event.timezone,
                  status="active" if activate else "draft", created_by=by)
    session.add(event)
    await session.flush()
    rs = RuleSet(event_id=event.id, version=1, yaml_text=cfg.to_yaml(), note="khởi tạo", created_by=by)
    session.add(rs)
    await session.flush()
    event.current_rules_id = rs.id
    await audit(session, by, "event.create", {
        "event_id": event.id, "name": name, "start": start, "end": end,
        "activate": activate, "closed_events": closed, "rules_id": rs.id,
    })
    await session.flush()
    return event


async def update_rules(session: AsyncSession, event: Event, cfg: RuleSetConfig, *, by: int | None,
                       note: str | None = None) -> RuleSet:
    """Lưu cfg thành version mới, đồng bộ tên/ngày/timezone của event từ cfg.event."""
    res = await session.execute(select(func.max(RuleSet.version)).where(RuleSet.event_id == event.id))
    version = int(res.scalar() or 0) + 1
    rs = RuleSet(event_id=event.id, version=version, yaml_text=cfg.to_yaml(), note=note, created_by=by)
    session.add(rs)
    await session.flush()
    event.name = cfg.event.name
    event.start_date = cfg.event.start
    event.end_date = cfg.event.end
    event.timezone = cfg.event.timezone
    event.current_rules_id = rs.id
    await audit(session, by, "rules.update", {"event_id": event.id, "version": version, "note": note})
    await session.flush()
    return rs


def _value_at(data: Any, path: str) -> Any:
    """Lấy giá trị theo đường dẫn 'a.b.c'; trong list tìm theo chỉ số hoặc theo 'key' (sports.<key>)."""
    node = data
    for p in path.split("."):
        if isinstance(node, list):
            if p.isdigit() and int(p) < len(node):
                node = node[int(p)]
            else:
                node = next((x for x in node if isinstance(x, dict) and x.get("key") == p), None)
        elif isinstance(node, dict):
            node = node.get(p)
        else:
            return None
        if node is None:
            return None
    return node


async def set_rule_value(session: AsyncSession, event: Event, path: str, value: str, *, by: int | None
                         ) -> tuple[RuleSet, object, object]:
    """Đặt một giá trị theo đường dẫn -> version mới. KeyError/ValidationError lan ra để bot báo lỗi.
    Trả (ruleset, giá trị cũ, giá trị mới)."""
    cfg_old = await get_rules(session, event)
    cfg_new = cfg_old.with_value(path, value)
    old = _value_at(cfg_old.model_dump(mode="json"), path)
    new = _value_at(cfg_new.model_dump(mode="json"), path)
    rs = await update_rules(session, event, cfg_new, by=by, note=f"set {path}")
    return rs, old, new


async def set_event_status(session: AsyncSession, event: Event, status: str, *, by: int | None) -> Event:
    if status not in EVENT_STATUSES:
        raise ValueError(f"Trạng thái không hợp lệ: {status!r} (draft | active | closed)")
    closed: list[int] = []
    if status == "active":
        closed = await _close_other_active(session, event.id)
    old = event.status
    event.status = status
    await audit(session, by, "event.status", {"event_id": event.id, "from": old, "to": status,
                                              "closed_events": closed})
    await session.flush()
    return event


# --------------------------------------------------------------------------- audit
def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


async def audit(session: AsyncSession, actor: int | None, action: str, details: dict | None = None) -> None:
    session.add(AuditLog(actor_telegram_id=actor, action=action, details=_jsonable(details) if details else None))
    await session.flush()


# --------------------------------------------------------------------------- settings
async def get_setting(session: AsyncSession, key: str, default: str | None = None) -> str | None:
    row = await session.get(AppSetting, key)
    return row.value if row is not None else default


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=str(value)))
    else:
        row.value = str(value)
    await session.flush()


async def setting_bool(session: AsyncSession, key: str, default: bool) -> bool:
    """Đọc cài đặt bool: '1'/'true'/'yes'/'on' -> True; '0'/'false'/... -> False; chưa đặt -> default."""
    raw = await get_setting(session, key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
