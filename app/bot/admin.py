"""Lệnh admin Telegram — CHỦ SỞ HỮU: agent `admin`.

- `router` chỉ nhận tin từ admin (IsAdmin) cho cả message và callback query.
- Văn bản thuần, tiếng Việt. Mọi handler mở `session_scope()`, lấy StravaClient qua `app.deps.get_client()`.
- Hành động nguy hiểm (áp dụng luật, reset, broadcast, unlink) đi qua nút xác nhận inline; yêu cầu chờ
  được giữ trong `_pending` (bộ nhớ tiến trình, hết hạn sau PENDING_TTL).
- Các hàm jobs (daily_post, reconcile, backup) import LAZY trong handler.
"""
from __future__ import annotations

import csv
import inspect
import io
import logging
import secrets
import shlex
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from pydantic import ValidationError
from sqlalchemy import func, select

from app import deps, notify
from app.bot import format as fmt
from app.bot.guards import IsAdmin
from app.config import get_settings
from app.db.base import session_scope
from app.db.models import Athlete, Evaluation, Event, Override, WebhookEvent
from app.rules.engine import load_default_rules
from app.rules.schema import REASONS, EventSection, GeneralRules, RuleSetConfig, SportRule
from app.services import activities, events, linking, stats

log = logging.getLogger(__name__)

router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())   # CallbackQuery cũng có .from_user

TOGGLE_KEYS: tuple[str, ...] = ("notify_rejected_dm", "notify_valid_dm", "notify_valid_group", "auto_post_group")
TEXT_LIMIT = 3500            # YAML dài hơn -> gửi file
TG_MAX = 4000                # cắt tin dài
PENDING_TTL = timedelta(minutes=15)
IMPORT_TTL = timedelta(minutes=10)

# id ngắn -> {"kind": rules_set|rules_reset|broadcast|unlink, "admin_id": int, "created_at": datetime, ...}
_pending: dict[str, dict[str, Any]] = {}
# admin telegram id -> thời điểm gõ /rules import (chờ file .yaml)
_awaiting_import: dict[int, datetime] = {}

ADMIN_HELP = """Lệnh admin:
/status – tình trạng bot: giải, athlete, webhook, rate limit, toggle
/event – xem giải | new "Tên" YYYY-MM-DD YYYY-MM-DD [draft] | activate <id> | close <id>
/rules – xem luật | set <path> <value> | import | export | reset
/recheck – chấm lại toàn bộ hoạt động theo luật hiện hành
/exclude <activity_id> [lý do] – loại một hoạt động
/include <activity_id> [lý do] – tính lại hoạt động đã bị loại
/adjust <activity_id> <km> [lý do] – sửa km quy đổi của hoạt động
/activity <id> – chi tiết hoạt động + kết quả chấm + override
/athletes – danh sách athlete đã kết nối
/backfill <strava_id|all> – tải lại hoạt động trong cửa sổ giải
/export – xuất CSV mọi hoạt động của giải
/post [dry|group] – soạn bài sáng ngay (dry: chỉ xem, group: gửi thẳng nhóm)
/settings [set <key> 0|1] – toggle thông báo / tự đăng nhóm
/broadcast <text> – nhắn riêng tới mọi runner đã kết nối (có xác nhận)
/reconcile – đối soát Strava ngay
/backup – sao lưu DB
/webhook list|subscribe|delete <id> – quản lý webhook Strava
/unlink_athlete <strava_id> [delete] – ngắt kết nối một runner (có xác nhận)"""


# =================================================================== tiện ích chung
def _args(command: CommandObject | None) -> list[str]:
    """Tách args bằng shlex để hỗ trợ "tên có dấu cách"; lỗi ngoặc kép -> tách theo khoảng trắng."""
    raw = (command.args if command is not None else None) or ""
    raw = raw.strip()
    if not raw:
        return []
    try:
        return shlex.split(raw)
    except ValueError:
        return raw.split()


def _raw_args(command: CommandObject | None) -> str:
    return ((command.args if command is not None else None) or "").strip()


def _split_text(text: str, limit: int = TG_MAX) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:                       # dòng đơn quá dài
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit and cur:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


async def _reply(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    chunks = _split_text(text)
    for i, chunk in enumerate(chunks):
        await message.answer(chunk, reply_markup=reply_markup if i == len(chunks) - 1 else None)


async def _send_file(message: Message, data: bytes, filename: str, caption: str | None = None) -> None:
    await message.answer_document(BufferedInputFile(data, filename=filename), caption=caption)


def admin_cmd(fn):
    """Bắt mọi lỗi bất ngờ: log + trả '⚠️ Lỗi: ...' cho admin (admin cần thấy lỗi thật)."""
    @wraps(fn)
    async def wrapper(message: Message, *args, **kwargs):
        try:
            return await fn(message, *args, **kwargs)
        except Exception as e:  # noqa: BLE001
            log.exception("Lệnh admin %s lỗi", fn.__name__)
            try:
                await message.answer(f"⚠️ Lỗi: {str(e)[:300]}")
            except Exception:  # noqa: BLE001
                log.exception("Không gửi được báo lỗi")
    return wrapper


def _uid(message: Message) -> int | None:
    return message.from_user.id if message.from_user is not None else None


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().timezone)
    except Exception:  # noqa: BLE001
        return ZoneInfo("Asia/Ho_Chi_Minh")


def _local(dt: datetime | None) -> str:
    """datetime UTC naive (DB) -> 'HH:MM dd/mm/yyyy' giờ địa phương."""
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    loc = dt.astimezone(_tz())
    return f"{loc.hour:02d}:{loc.minute:02d} {loc.day:02d}/{loc.month:02d}/{loc.year}"


def _d(d: date | None) -> str:
    return f"{d.day:02d}/{d.month:02d}/{d.year}" if d else "-"


def _parse_date(s: str) -> date:
    s = s.strip()
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    raise ValueError(f"Ngày không hợp lệ: {s!r} (dùng YYYY-MM-DD)")


def _parse_float(s: str) -> float:
    return float(s.strip().replace(",", "."))


def _yesno(v: bool | None) -> str:
    if v is None:
        return "?"
    return "có" if v else "không"


def _fmt_val(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return "[" + ", ".join(str(x) for x in v) + "]"
    return str(v)


def _fmt_dict(d: dict | None) -> str:
    if not d:
        return "(trống)"
    return "\n".join(f"{k}: {_fmt_val(v)}" for k, v in d.items())


def _event_title(ev: Event) -> str:
    return f"#{ev.id} «{ev.name}» {_d(ev.start_date)} → {_d(ev.end_date)}"


async def _require_event(s, message: Message) -> Event | None:
    ev = await events.get_active_event(s)
    if ev is None:
        await message.answer("Chưa có giải nào đang active. Dùng /event để xem hoặc /event new … để tạo.")
    return ev


async def _setting_bool(s, key: str) -> bool:
    """Toggle runtime: AppSetting ghi đè Settings mặc định."""
    default = bool(getattr(get_settings(), key, False))
    fn = getattr(events, "setting_bool", None)
    if fn is not None:
        return bool(await fn(s, key, default))
    raw = await events.get_setting(s, key, None)          # dự phòng nếu services chưa có setting_bool
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _parse_bool(s: str) -> bool:
    v = s.strip().lower()
    if v in ("1", "true", "yes", "on", "bật"):
        return True
    if v in ("0", "false", "no", "off", "tắt"):
        return False
    raise ValueError(f"Giá trị phải là 0 hoặc 1, nhận {s!r}")


# ------------------------------------------------------------------ pending xác nhận
def _prune_pending() -> None:
    now = datetime.utcnow()
    for k in [k for k, v in _pending.items() if now - v["created_at"] > PENDING_TTL]:
        _pending.pop(k, None)
    for k in [k for k, t in _awaiting_import.items() if now - t > IMPORT_TTL]:
        _awaiting_import.pop(k, None)


def _add_pending(kind: str, admin_id: int | None, **payload: Any) -> str:
    _prune_pending()
    pid = secrets.token_hex(4)
    while pid in _pending:
        pid = secrets.token_hex(4)
    _pending[pid] = {"kind": kind, "admin_id": admin_id, "created_at": datetime.utcnow(), **payload}
    return pid


def _confirm_kb(prefix: str, pid: str, ok: str = "✅ Áp dụng", cancel: str = "❌ Huỷ") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=ok, callback_data=f"{prefix}:apply:{pid}"),
        InlineKeyboardButton(text=cancel, callback_data=f"{prefix}:cancel:{pid}"),
    ]])


# ------------------------------------------------------------------ luật: tra cứu / gợi ý
def _get_path(cfg: RuleSetConfig, path: str) -> Any:
    """Đọc giá trị theo đường dẫn cùng cú pháp với RuleSetConfig.with_value."""
    node: Any = cfg.model_dump(mode="json")
    parts = path.split(".")
    for i, p in enumerate(parts):
        if isinstance(node, list):
            if p.isdigit():
                node = node[int(p)]
            elif i > 0 and parts[i - 1] == "sports":
                match = next((x for x in node if x.get("key") == p), None)
                if match is None:
                    raise KeyError(f"Không có sport key '{p}'")
                node = match
            else:
                raise KeyError(f"Đường dẫn không hợp lệ: {path}")
        elif isinstance(node, dict):
            if p not in node:
                raise KeyError(f"Không có mục '{p}'")
            node = node[p]
        else:
            raise KeyError(f"Đường dẫn không hợp lệ: {path}")
    return node


def _path_hint(cfg: RuleSetConfig | None) -> str:
    keys = ", ".join(s.key for s in cfg.sports) if cfg else "run, ride, swim"
    return (
        "Đường dẫn hợp lệ:\n"
        f"• general.<field>: {', '.join(GeneralRules.model_fields)}\n"
        f"• event.<field>: {', '.join(EventSection.model_fields)}\n"
        f"• sports.<key>.<field> — key: {keys}; field: {', '.join(k for k in SportRule.model_fields if k != 'key')}\n"
        "• excluded_types (danh sách, cách nhau bằng dấu phẩy)\n"
        "Ví dụ: /rules set sports.run.pace_max 18:00 · /rules set general.daily_cap_km null"
    )


def _err_text(e: Exception) -> str:
    if isinstance(e, ValidationError):
        parts = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err.get("loc", ()))
            msg = err.get("msg", "")
            parts.append(f"{loc}: {msg}" if loc else msg)
        return "; ".join(parts) or str(e)
    if isinstance(e, KeyError):
        return str(e.args[0]) if e.args else str(e)
    return str(e)


async def _rules_header(s, event: Event) -> tuple[RuleSetConfig, int]:
    cfg = await events.get_rules(s, event)
    version = await events.get_rules_version(s, event)
    return cfg, version


async def _load_evaluation(s, activity_id: int, event_id: int | None) -> Evaluation | None:
    """Evaluation của hoạt động trong event (None event -> bản mới nhất bất kỳ)."""
    q = select(Evaluation).where(Evaluation.activity_id == activity_id)
    if event_id is not None:
        q = q.where(Evaluation.event_id == event_id)
    q = q.order_by(Evaluation.evaluated_at.desc()).limit(1)
    return (await s.execute(q)).scalars().first()


async def _load_override(s, activity_id: int, event_id: int | None) -> Override | None:
    q = select(Override).where(Override.activity_id == activity_id, Override.active.is_(True))
    if event_id is not None:
        q = q.where(Override.event_id == event_id)
    q = q.order_by(Override.created_at.desc()).limit(1)
    return (await s.execute(q)).scalars().first()


# =================================================================== /admin
@router.message(Command("admin"))
@admin_cmd
async def cmd_admin(message: Message) -> None:
    await _reply(message, ADMIN_HELP)


# =================================================================== /status
@router.message(Command("status"))
@admin_cmd
async def cmd_status(message: Message) -> None:
    settings = get_settings()
    client = deps.get_client()
    lines: list[str] = ["📊 Tình trạng bot"]
    async with session_scope() as s:
        # Giải
        try:
            ev = await events.get_active_event(s)
            if ev is None:
                lines.append("🏁 Giải: (không có giải active)")
            else:
                ver = await events.get_rules_version(s, ev)
                lines.append(f"🏁 Giải: {_event_title(ev)} · luật v{ver}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"🏁 Giải: không kiểm tra được ({str(e)[:80]})")
        # Athlete
        try:
            aths = await linking.list_connected(s)
            with_tg = sum(1 for a in aths if a.telegram_id)
            lines.append(f"👥 Athlete đã kết nối: {len(aths)} (có Telegram: {with_tg})")
        except Exception as e:  # noqa: BLE001
            lines.append(f"👥 Athlete: không kiểm tra được ({str(e)[:80]})")
        # Webhook subscription
        try:
            subs = await client.list_subscriptions()
            if not subs:
                lines.append("🔗 Webhook: ⚠️ chưa đăng ký (dùng /webhook subscribe)")
            else:
                for sub in subs:
                    cb = sub.get("callback_url", "?")
                    ok = "khớp" if cb.rstrip("/") == settings.webhook_url.rstrip("/") else "KHÁC cấu hình!"
                    lines.append(f"🔗 Webhook: #{sub.get('id')} → {cb} ({ok})")
        except Exception as e:  # noqa: BLE001
            lines.append(f"🔗 Webhook: không kiểm tra được ({str(e)[:80]})")
        # Rate limit
        rl = getattr(client, "last_rate_limit", None)
        if rl:
            lines.append(f"📈 Rate limit: 15 phút {rl.get('usage_15m', '?')}/{rl.get('limit_15m', '?')} · "
                         f"ngày {rl.get('usage_day', '?')}/{rl.get('limit_day', '?')}")
        else:
            lines.append("📈 Rate limit: chưa có dữ liệu (chưa gọi API)")
        # Webhook events
        try:
            last = (await s.execute(select(WebhookEvent).order_by(WebhookEvent.received_at.desc()).limit(1))
                    ).scalars().first()
            since = datetime.utcnow() - timedelta(hours=24)
            n_err = (await s.execute(select(func.count()).select_from(WebhookEvent)
                                     .where(WebhookEvent.error.is_not(None), WebhookEvent.received_at >= since))
                     ).scalar_one()
            last_txt = f"{_local(last.received_at)} ({last.object_type} {last.aspect_type})" if last else "chưa có"
            lines.append(f"📨 Webhook event gần nhất: {last_txt} · lỗi 24h: {n_err}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"📨 Webhook event: không kiểm tra được ({str(e)[:80]})")
        # Toggle
        try:
            toggles = []
            for key in TOGGLE_KEYS:
                toggles.append(f"{key}={1 if await _setting_bool(s, key) else 0}")
            lines.append("⚙️ " + " · ".join(toggles))
        except Exception as e:  # noqa: BLE001
            lines.append(f"⚙️ Toggle: không kiểm tra được ({str(e)[:80]})")
    lines.append(f"🌐 public_base_url: {settings.public_base_url}")
    lines.append(f"📣 group_chat_id: {settings.group_chat_id if settings.group_chat_id else '(chưa đặt)'} · "
                 f"giờ đăng bài: {settings.daily_post_time} {settings.timezone}")
    await _reply(message, "\n".join(lines))


# =================================================================== /event
@router.message(Command("event"))
@admin_cmd
async def cmd_event(message: Message, command: CommandObject | None = None) -> None:
    args = _args(command)
    uid = _uid(message)
    async with session_scope() as s:
        if not args or args[0] == "list":
            evs = await events.list_events(s)
            if not evs:
                await message.answer('Chưa có giải nào. Tạo: /event new "Tên giải" 2027-01-01 2027-01-31 [draft]')
                return
            lines = ["Danh sách giải (★ = active):"]
            for ev in evs:
                star = "★" if ev.status == "active" else "•"
                lines.append(f"{star} {_event_title(ev)} · {ev.status}")
            await _reply(message, "\n".join(lines))
            return

        sub = args[0].lower()
        if sub == "new":
            if len(args) < 4:
                await message.answer('Cú pháp: /event new "Tên giải" YYYY-MM-DD YYYY-MM-DD [draft]')
                return
            name = args[1].strip()
            start, end = _parse_date(args[2]), _parse_date(args[3])
            if end < start:
                await message.answer("Ngày kết thúc phải >= ngày bắt đầu.")
                return
            draft = any(a.lower().lstrip("-") == "draft" for a in args[4:])
            ev = await events.create_event(s, name, start, end, by=uid, activate=not draft)
            try:
                ver = await events.get_rules_version(s, ev)
            except Exception:  # noqa: BLE001
                ver = 1
            status = "active" if not draft else "draft (chưa kích hoạt: /event activate %s)" % ev.id
            await message.answer(
                f"✅ Đã tạo giải {_event_title(ev)} · {status} · luật v{ver} (kế thừa mặc định).\n"
                "Xem/chỉnh luật: /rules · /rules set event.target_km 45 · /rules import (gửi file YAML)."
            )
            return

        if sub in ("activate", "close"):
            if len(args) < 2 or not args[1].isdigit():
                await message.answer(f"Cú pháp: /event {sub} <id>")
                return
            ev = await events.get_event(s, int(args[1]))
            if ev is None:
                await message.answer(f"Không có giải #{args[1]}.")
                return
            new_status = "active" if sub == "activate" else "closed"
            ev = await events.set_event_status(s, ev, new_status, by=uid)
            await message.answer(f"✅ Giải {_event_title(ev)} → {ev.status}.")
            return

        await message.answer('Cú pháp: /event | /event new "Tên" YYYY-MM-DD YYYY-MM-DD [draft] | '
                             '/event activate <id> | /event close <id>')


# =================================================================== /rules
@router.message(Command("rules"))
@admin_cmd
async def cmd_rules(message: Message, command: CommandObject | None = None) -> None:
    args = _args(command)
    uid = _uid(message)
    sub = args[0].lower() if args else "show"
    async with session_scope() as s:
        ev = await _require_event(s, message)
        if ev is None:
            return

        if sub in ("show", "view"):
            cfg, ver = await _rules_header(s, ev)
            yaml_text = cfg.to_yaml()
            head = f"Luật giải {_event_title(ev)} · v{ver}"
            if len(yaml_text) <= TEXT_LIMIT:
                await _reply(message, f"{head}\n\n{yaml_text}\nSửa: /rules set <path> <value> · /rules import")
            else:
                await _send_file(message, yaml_text.encode("utf-8"), f"rules-v{ver}.yaml", caption=head)
            return

        if sub == "export":
            cfg, ver = await _rules_header(s, ev)
            await _send_file(message, cfg.to_yaml().encode("utf-8"), f"rules-v{ver}.yaml",
                             caption=f"Luật giải {_event_title(ev)} · v{ver}")
            return

        if sub == "set":
            if len(args) < 3:
                await message.answer("Cú pháp: /rules set <path> <value>\n" + _path_hint(None))
                return
            path = args[1]
            value = " ".join(args[2:])
            cfg, ver = await _rules_header(s, ev)
            try:
                old = _get_path(cfg, path)
                new_cfg = cfg.with_value(path, value)
                new = _get_path(new_cfg, path)
            except (KeyError, IndexError, ValueError, ValidationError) as e:   # ValidationError là ValueError
                await _reply(message, f"❌ Không đặt được '{path}': {_err_text(e)}\n\n{_path_hint(cfg)}")
                return
            if _fmt_val(old) == _fmt_val(new):
                await message.answer(f"{path} đã là {_fmt_val(new)} — không có gì thay đổi.")
                return
            pid = _add_pending("rules_set", uid, event_id=ev.id, path=path, value=value)
            await message.answer(
                f"Xem trước (chưa lưu) — luật v{ver} → v{ver + 1}:\n{path}: {_fmt_val(old)} → {_fmt_val(new)}",
                reply_markup=_confirm_kb("rules", pid),
            )
            return

        if sub == "import":
            _prune_pending()
            if uid is not None:
                _awaiting_import[uid] = datetime.utcnow()
            await message.answer(f"Gửi file .yaml ngay sau tin này (trong {int(IMPORT_TTL.total_seconds() // 60)} phút). "
                                 "Luật sẽ thành version mới; nhớ /recheck sau khi nhập.")
            return

        if sub == "reset":
            cfg, ver = await _rules_header(s, ev)
            pid = _add_pending("rules_reset", uid, event_id=ev.id)
            await message.answer(
                f"Đặt lại luật giải {_event_title(ev)} về mặc định (defaults.yaml), giữ tên/ngày giải hiện tại?\n"
                f"Luật v{ver} vẫn được lưu lịch sử; bản mới là v{ver + 1}.",
                reply_markup=_confirm_kb("rules", pid, ok="✅ Đặt lại"),
            )
            return

        await message.answer("Cú pháp: /rules | /rules set <path> <value> | /rules import | /rules export | /rules reset\n"
                             + _path_hint(None))


@router.message(F.document)
@admin_cmd
async def on_document(message: Message) -> None:
    """File .yaml gửi sau /rules import."""
    uid = _uid(message)
    if uid is None or uid not in _awaiting_import:
        return
    started = _awaiting_import.pop(uid)
    if datetime.utcnow() - started > IMPORT_TTL:
        await message.answer("Hết thời gian chờ file. Gõ /rules import rồi gửi lại.")
        return
    doc = message.document
    if doc.file_size and doc.file_size > 512_000:
        await message.answer("File quá lớn (>500 KB). Luật YAML chỉ vài KB.")
        return
    buf = await message.bot.download(doc)
    data = buf.read() if hasattr(buf, "read") else bytes(buf)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        await message.answer("File không phải UTF-8. Hãy lưu lại dạng UTF-8 rồi gửi lại.")
        return
    try:
        cfg = RuleSetConfig.from_yaml(text)
    except Exception as e:  # noqa: BLE001  (yaml.YAMLError, ValidationError…)
        await _reply(message, f"❌ File YAML không hợp lệ: {_err_text(e)[:1500]}\nGõ /rules export để lấy mẫu đúng.")
        return
    async with session_scope() as s:
        ev = await _require_event(s, message)
        if ev is None:
            return
        rs = await events.update_rules(s, ev, cfg, by=uid, note=f"import {doc.file_name or ''}".strip())
        note = ""
        if cfg.event.start != ev.start_date or cfg.event.end != ev.end_date:
            note = (f"\n⚠️ event.start/end trong file ({cfg.event.start} → {cfg.event.end}) khác giải "
                    f"({ev.start_date} → {ev.end_date}); engine dùng ngày trong luật.")
        await message.answer(f"✅ Đã nhập luật mới cho giải {_event_title(ev)}: v{rs.version}.{note}\n"
                             "Dùng /recheck để chấm lại toàn bộ hoạt động.")


# =================================================================== callback xác nhận
async def _cb_reply(cb: CallbackQuery, text: str) -> None:
    msg = cb.message
    if msg is not None and hasattr(msg, "edit_text"):
        try:
            await msg.edit_text(text)
            return
        except Exception:  # noqa: BLE001
            pass
        if hasattr(msg, "answer"):
            await msg.answer(text)
            return
    await cb.bot.send_message(chat_id=cb.from_user.id, text=text)


async def _apply_pending(entry: dict[str, Any], actor: int | None) -> str:
    kind = entry["kind"]
    client = deps.get_client()
    async with session_scope() as s:
        if kind == "rules_set":
            ev = await events.get_event(s, entry["event_id"])
            if ev is None:
                return f"Giải #{entry['event_id']} không còn tồn tại."
            rs, old, new = await events.set_rule_value(s, ev, entry["path"], entry["value"], by=actor)
            return (f"✅ Đã áp dụng (luật v{rs.version}):\n{entry['path']}: {_fmt_val(old)} → {_fmt_val(new)}\n"
                    "Dùng /recheck để chấm lại toàn bộ hoạt động.")

        if kind == "rules_reset":
            ev = await events.get_event(s, entry["event_id"])
            if ev is None:
                return f"Giải #{entry['event_id']} không còn tồn tại."
            data = load_default_rules().model_dump(mode="json")
            data["event"].update(name=ev.name, start=ev.start_date.isoformat(), end=ev.end_date.isoformat(),
                                 timezone=ev.timezone or data["event"].get("timezone"))
            cfg = RuleSetConfig.model_validate(data)
            rs = await events.update_rules(s, ev, cfg, by=actor, note="reset về mặc định")
            return f"✅ Đã đặt lại luật về mặc định: v{rs.version} (giữ tên/ngày giải).\nDùng /recheck để chấm lại."

        if kind == "broadcast":
            text = entry["text"]
            aths = await linking.list_connected(s)
            targets = [a.telegram_id for a in aths if a.telegram_id]
            sent = 0
            for tid in targets:
                if await notify.dm(tid, text):
                    sent += 1
            try:
                await events.audit(s, actor, "broadcast", {"sent": sent, "total": len(targets), "text": text[:200]})
            except Exception:  # noqa: BLE001
                log.exception("audit broadcast lỗi")
            return f"📣 Đã gửi {sent}/{len(targets)} người."

        if kind == "unlink":
            ath = await linking.get_athlete_by_strava(s, entry["strava_id"])
            if ath is None:
                return f"Không có athlete strava {entry['strava_id']}."
            await linking.unlink(s, client, ath, delete_data=entry["delete_data"])
            extra = " và đã xoá dữ liệu hoạt động" if entry["delete_data"] else ""
            return f"✅ Đã ngắt kết nối {ath.name} (strava {ath.strava_id}){extra}."

    return f"Loại yêu cầu không rõ: {kind}"


@router.callback_query(F.data.startswith("rules:") | F.data.startswith("confirm:"))
async def on_confirm(cb: CallbackQuery) -> None:
    try:
        try:
            _, action, pid = (cb.data or "").split(":", 2)
        except ValueError:
            await cb.answer("Dữ liệu nút không hợp lệ.", show_alert=True)
            return
        _prune_pending()
        entry = _pending.get(pid)
        if entry is None:
            await cb.answer("Yêu cầu đã hết hạn hoặc đã xử lý.", show_alert=True)
            await _cb_reply(cb, "⌛ Yêu cầu đã hết hạn hoặc đã xử lý. Gõ lại lệnh nếu cần.")
            return
        if entry["admin_id"] is not None and cb.from_user.id != entry["admin_id"]:
            await cb.answer("Chỉ người tạo yêu cầu mới xác nhận được.", show_alert=True)
            return
        if action == "cancel":
            _pending.pop(pid, None)
            await _cb_reply(cb, "❌ Đã huỷ, không thay đổi gì.")
            await cb.answer("Đã huỷ")
            return
        if action != "apply":
            await cb.answer("Hành động không rõ.", show_alert=True)
            return
        _pending.pop(pid, None)
        text = await _apply_pending(entry, cb.from_user.id)
        await _cb_reply(cb, text)
        await cb.answer()
    except Exception as e:  # noqa: BLE001
        log.exception("Callback admin lỗi")
        try:
            await _cb_reply(cb, f"⚠️ Lỗi: {str(e)[:300]}")
            await cb.answer()
        except Exception:  # noqa: BLE001
            log.exception("Không gửi được báo lỗi callback")


# =================================================================== /recheck
@router.message(Command("recheck"))
@admin_cmd
async def cmd_recheck(message: Message) -> None:
    async with session_scope() as s:
        ev = await _require_event(s, message)
        if ev is None:
            return
        summary = await activities.reevaluate_event(s, ev)
    changed = list(summary.changed)
    if not changed:
        await message.answer(f"Đã chấm lại {summary.total} hoạt động, không có thay đổi trạng thái.")
        return
    lines = [f"Đã chấm lại {summary.total} hoạt động, {len(changed)} đổi trạng thái:"]
    for aid, old_valid, new_valid in changed[:20]:
        lines.append(f"{aid}: {'hợp lệ' if old_valid else 'loại'}→{'hợp lệ' if new_valid else 'loại'}")
    if len(changed) > 20:
        lines.append(f"… và {len(changed) - 20} hoạt động khác")
    await _reply(message, "\n".join(lines))


# =================================================================== /exclude /include /adjust
async def _override_cmd(message: Message, command: CommandObject | None, action: str) -> None:
    args = _args(command)
    usage = {
        "exclude": "/exclude <activity_id> [lý do]",
        "include": "/include <activity_id> [lý do]",
        "adjust_km": "/adjust <activity_id> <km> [lý do]",
    }[action]
    if not args or not args[0].isdigit():
        await message.answer(f"Cú pháp: {usage}")
        return
    activity_id = int(args[0])
    value: float | None = None
    rest = args[1:]
    if action == "adjust_km":
        if not rest:
            await message.answer(f"Cú pháp: {usage}")
            return
        try:
            value = _parse_float(rest[0])
        except ValueError:
            await message.answer(f"Km không hợp lệ: {rest[0]!r}. Cú pháp: {usage}")
            return
        if value < 0:
            await message.answer("Km phải >= 0.")
            return
        rest = rest[1:]
    note = " ".join(rest).strip() or None
    uid = _uid(message)
    async with session_scope() as s:
        ev = await _require_event(s, message)
        if ev is None:
            return
        ov = await activities.apply_override(s, ev, activity_id, action, value=value, note=note, by=uid)
        label = {"exclude": "loại", "include": "tính lại", "adjust_km": f"đặt {fmt.fmt_km(value or 0)} km quy đổi cho"}[action]
        lines = [f"✅ Đã {label} hoạt động {activity_id}" + (f" (override #{ov.id})" if getattr(ov, "id", None) else "")
                 + (f" — lý do: {note}" if note else "")]
        try:
            a = await activities.get_activity(s, activity_id)
            if a is not None:
                e = await _load_evaluation(s, activity_id, ev.id)
                lines.append(fmt.activity_line(a, e))
        except Exception as e:  # noqa: BLE001
            log.warning("Không đọc được hoạt động %s sau override: %s", activity_id, e)
    await _reply(message, "\n".join(lines))


@router.message(Command("exclude"))
@admin_cmd
async def cmd_exclude(message: Message, command: CommandObject | None = None) -> None:
    await _override_cmd(message, command, "exclude")


@router.message(Command("include"))
@admin_cmd
async def cmd_include(message: Message, command: CommandObject | None = None) -> None:
    await _override_cmd(message, command, "include")


@router.message(Command("adjust"))
@admin_cmd
async def cmd_adjust(message: Message, command: CommandObject | None = None) -> None:
    await _override_cmd(message, command, "adjust_km")


# =================================================================== /activity
@router.message(Command("activity"))
@admin_cmd
async def cmd_activity(message: Message, command: CommandObject | None = None) -> None:
    args = _args(command)
    if not args or not args[0].isdigit():
        await message.answer("Cú pháp: /activity <id>")
        return
    aid = int(args[0])
    async with session_scope() as s:
        a = await activities.get_activity(s, aid)
        if a is None:
            await message.answer(f"Không có hoạt động {aid} trong DB.")
            return
        ev = await events.get_active_event(s)
        ath = await s.get(Athlete, a.athlete_id)
        e = await _load_evaluation(s, aid, ev.id if ev else None)
        ov = await _load_override(s, aid, ev.id if ev else None)

    km = a.distance_m / 1000
    if e is not None and e.pace_s is not None:
        pace = fmt.fmt_pace(e.pace_s, "per_100m" if e.sport_key == "swim" else "per_km")
    elif km > 0 and a.moving_s:
        pace = fmt.fmt_pace(a.moving_s / km)
    else:
        pace = "-"
    ath_txt = f"{ath.name} (strava {ath.strava_id}, tg {ath.telegram_id or '-'})" if ath else f"athlete_id {a.athlete_id}"
    lines = [
        f"🏃 Hoạt động {a.id} — «{a.name or '(không tên)'}»",
        f"Athlete: {ath_txt}",
        f"Loại: {a.sport_type}" + (f" ({a.type})" if a.type and a.type != a.sport_type else "")
        + f" · bắt đầu {fmt.fmt_datetime(a.start_date_local)}/{a.start_date_local.year}"
        + (f" ({a.timezone})" if a.timezone else ""),
        f"Quãng đường: {fmt.fmt_km(km)} km · moving {fmt.fmt_duration(a.moving_s)} · "
        f"elapsed {fmt.fmt_duration(a.elapsed_s)} · pace {pace}",
        f"GPS map: {_yesno(a.has_map)} · manual: {_yesno(a.manual)} · trainer: {_yesno(a.trainer)} · "
        f"flagged: {_yesno(a.flagged)} · private: {_yesno(a.private)} · device: {a.device_name or '-'}",
        f"Link: https://www.strava.com/activities/{a.id}",
    ]
    if a.deleted_at:
        lines.append(f"🗑 Đã xoá trên Strava lúc {_local(a.deleted_at)}")
    if e is None:
        lines.append("Chấm: ⚪ chưa chấm" + ("" if ev else " (không có giải active)"))
    else:
        if e.valid:
            lines.append(f"Chấm (luật v{e.rules_version}): ✅ hợp lệ · {fmt.fmt_km(e.converted_km)} km quy đổi"
                         f" (thực {fmt.fmt_km(e.raw_km)} km, sport {e.sport_key or '-'})"
                         + (" · có override" if e.overridden else ""))
        else:
            reasons = []
            for r in e.reasons or []:
                code, _, detail = r.partition(":")
                t = REASONS.get(code, code)
                reasons.append(f"{t} ({detail})" if detail else t)
            lines.append(f"Chấm (luật v{e.rules_version}): ❌ loại — {'; '.join(reasons) or 'không rõ lý do'}"
                         + (" · có override" if e.overridden else ""))
        lines.append(f"Chấm lúc: {_local(e.evaluated_at)}")
    if ov is not None:
        val = f" {fmt.fmt_km(ov.value)} km" if ov.value is not None else ""
        lines.append(f"Override: {ov.action}{val}" + (f" — {ov.note}" if ov.note else "")
                     + f" · bởi tg {ov.by_telegram_id or '-'} lúc {_local(ov.created_at)}")
    await _reply(message, "\n".join(lines))


# =================================================================== /athletes
@router.message(Command("athletes"))
@admin_cmd
async def cmd_athletes(message: Message) -> None:
    async with session_scope() as s:
        aths = await linking.list_connected(s)
    if not aths:
        await message.answer("Chưa có athlete nào kết nối.")
        return
    lines = [f"Đã kết nối: {len(aths)} athlete"]
    for a in aths:
        lines.append(f"{a.name} (strava {a.strava_id}, tg {a.telegram_id if a.telegram_id else '-'})")
    await _reply(message, "\n".join(lines))


# =================================================================== /backfill
@router.message(Command("backfill"))
@admin_cmd
async def cmd_backfill(message: Message, command: CommandObject | None = None) -> None:
    args = _args(command)
    if not args:
        await message.answer("Cú pháp: /backfill <strava_id|all>")
        return
    client = deps.get_client()
    async with session_scope() as s:
        ev = await _require_event(s, message)
        if ev is None:
            return
        if args[0].lower() == "all":
            aths = await linking.list_connected(s)
            if not aths:
                await message.answer("Chưa có athlete nào kết nối.")
                return
            await message.answer(f"Đang backfill {len(aths)} athlete…")
            total, ok, errors = 0, 0, []
            for a in aths:
                try:
                    total += await activities.backfill_athlete(s, client, a, ev)
                    ok += 1
                except Exception as e:  # noqa: BLE001
                    log.exception("backfill %s lỗi", a.strava_id)
                    errors.append(f"{a.name} (strava {a.strava_id}): {str(e)[:100]}")
            text = f"✅ Backfill xong {ok}/{len(aths)} athlete: {total} hoạt động mới."
            if errors:
                text += "\nLỗi:\n" + "\n".join(errors[:20])
            await _reply(message, text)
            return
        if not args[0].isdigit():
            await message.answer("Cú pháp: /backfill <strava_id|all>")
            return
        ath = await linking.get_athlete_by_strava(s, int(args[0]))
        if ath is None:
            await message.answer(f"Không có athlete strava {args[0]}.")
            return
        n = await activities.backfill_athlete(s, client, ath, ev)
        await message.answer(f"✅ Backfill {ath.name} (strava {ath.strava_id}): {n} hoạt động mới.")


# =================================================================== /export
@router.message(Command("export"))
@admin_cmd
async def cmd_export(message: Message) -> None:
    async with session_scope() as s:
        ev = await _require_event(s, message)
        if ev is None:
            return
        rows = await stats.export_rows(s, ev)
    if not rows:
        await message.answer("Chưa có dữ liệu để xuất.")
        return
    header: list[str] = []
    for r in rows:
        for k in r.keys():
            if k not in header:
                header.append(k)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header, extrasaction="ignore", lineterminator="\r\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if v is None else v) for k, v in r.items()})
    data = buf.getvalue().encode("utf-8-sig")
    today = fmt.vn_today(get_settings().timezone)
    await _send_file(message, data, f"export-{ev.id}-{today.isoformat()}.csv",
                     caption=f"Xuất {len(rows)} dòng — giải {_event_title(ev)}")


# =================================================================== /post
@router.message(Command("post"))
@admin_cmd
async def cmd_post(message: Message, command: CommandObject | None = None) -> None:
    from app.jobs.daily_post import run_daily_post  # lazy
    args = [a.lower() for a in _args(command)]
    if args and args[0] in ("dry", "preview", "xem"):
        text = await run_daily_post(dry_run=True)
        await _reply(message, "📝 Bản nháp (chưa gửi):\n\n" + (text or "(trống)"))
        return
    if args and args[0] in ("group", "nhom", "nhóm"):
        await run_daily_post(send_group=True)
        await message.answer("✅ Đã soạn và gửi bài vào nhóm.")
        return
    await run_daily_post()
    async with session_scope() as s:
        auto = await _setting_bool(s, "auto_post_group")
    await message.answer("✅ Đã soạn và gửi bài theo cấu hình "
                         + ("(vào nhóm)." if auto else "(chỉ gửi admin; bật auto_post_group để đăng thẳng nhóm)."))


# =================================================================== /settings
@router.message(Command("settings"))
@admin_cmd
async def cmd_settings(message: Message, command: CommandObject | None = None) -> None:
    args = _args(command)
    settings = get_settings()
    async with session_scope() as s:
        if not args:
            lines = ["⚙️ Toggle runtime (1=bật, 0=tắt):"]
            for key in TOGGLE_KEYS:
                cur = await _setting_bool(s, key)
                default = bool(getattr(settings, key, False))
                lines.append(f"{key} = {1 if cur else 0}" + ("" if cur == default else f" (mặc định {1 if default else 0})"))
            lines.append("Đổi: /settings set <key> <0|1>")
            await _reply(message, "\n".join(lines))
            return
        if args[0].lower() != "set" or len(args) < 3:
            await message.answer("Cú pháp: /settings | /settings set <key> <0|1>\nKey: " + ", ".join(TOGGLE_KEYS))
            return
        key = args[1].strip().lower()
        if key not in TOGGLE_KEYS:
            await message.answer(f"❌ Key không hợp lệ: '{key}'. Key cho phép: " + ", ".join(TOGGLE_KEYS))
            return
        try:
            val = _parse_bool(args[2])
        except ValueError as e:
            await message.answer(f"❌ {e}")
            return
        await events.set_setting(s, key, "1" if val else "0")
        await message.answer(f"✅ Đã đặt {key} = {1 if val else 0}")


# =================================================================== /broadcast
@router.message(Command("broadcast"))
@admin_cmd
async def cmd_broadcast(message: Message, command: CommandObject | None = None) -> None:
    text = _raw_args(command)
    if not text:
        await message.answer("Cú pháp: /broadcast <nội dung> — bot sẽ nhắn riêng tới mọi runner đã kết nối Telegram.")
        return
    async with session_scope() as s:
        aths = await linking.list_connected(s)
    n = sum(1 for a in aths if a.telegram_id)
    if n == 0:
        await message.answer("Không có runner nào có Telegram để gửi.")
        return
    pid = _add_pending("broadcast", _uid(message), text=text)
    await _reply(message, f"Gửi tin sau tới {n} người?\n———\n{text}", reply_markup=_confirm_kb("confirm", pid, ok="✅ Gửi"))


# =================================================================== /reconcile /backup
@router.message(Command("reconcile"))
@admin_cmd
async def cmd_reconcile(message: Message) -> None:
    from app.jobs.reconcile import reconcile_all  # lazy
    await message.answer("Đang đối soát với Strava…")
    res = await reconcile_all(deps.get_client())
    await _reply(message, "✅ Đối soát xong:\n" + _fmt_dict(res if isinstance(res, dict) else {"kết quả": res}))


@router.message(Command("backup"))
@admin_cmd
async def cmd_backup(message: Message) -> None:
    from app.jobs.backup import run_backup  # lazy
    res = run_backup()
    if inspect.isawaitable(res):
        res = await res
    if res:
        await message.answer(f"✅ Đã sao lưu: {res}")
    else:
        await message.answer("⚠️ Backup không tạo file (DB không phải SQLite file hoặc lỗi — xem log).")


# =================================================================== /webhook
@router.message(Command("webhook"))
@admin_cmd
async def cmd_webhook(message: Message, command: CommandObject | None = None) -> None:
    args = _args(command)
    settings = get_settings()
    client = deps.get_client()
    sub = args[0].lower() if args else "list"
    if sub == "list":
        subs = await client.list_subscriptions()
        if not subs:
            await message.answer(f"Chưa có webhook subscription.\nĐăng ký: /webhook subscribe → {settings.webhook_url}")
            return
        lines = ["Webhook subscription:"]
        for x in subs:
            lines.append(f"#{x.get('id')} → {x.get('callback_url', '?')}" + (f" · {x.get('created_at')}" if x.get("created_at") else ""))
        lines.append(f"Cấu hình hiện tại: {settings.webhook_url}")
        await _reply(message, "\n".join(lines))
        return
    if sub == "subscribe":
        res = await client.create_subscription(settings.webhook_url, settings.strava_webhook_verify_token)
        sid = res.get("id") if isinstance(res, dict) else res
        await message.answer(f"✅ Đã đăng ký webhook #{sid} → {settings.webhook_url}")
        return
    if sub == "delete":
        if len(args) < 2 or not args[1].isdigit():
            await message.answer("Cú pháp: /webhook delete <id>")
            return
        await client.delete_subscription(int(args[1]))
        await message.answer(f"✅ Đã xoá webhook #{args[1]}")
        return
    await message.answer("Cú pháp: /webhook list | /webhook subscribe | /webhook delete <id>")


# =================================================================== /unlink_athlete
@router.message(Command("unlink_athlete"))
@admin_cmd
async def cmd_unlink_athlete(message: Message, command: CommandObject | None = None) -> None:
    args = _args(command)
    if not args or not args[0].isdigit():
        await message.answer("Cú pháp: /unlink_athlete <strava_id> [delete]")
        return
    strava_id = int(args[0])
    delete_data = any(a.lower() == "delete" for a in args[1:])
    async with session_scope() as s:
        ath = await linking.get_athlete_by_strava(s, strava_id)
        if ath is None:
            await message.answer(f"Không có athlete strava {strava_id}.")
            return
        name, tg = ath.name, ath.telegram_id
    pid = _add_pending("unlink", _uid(message), strava_id=strava_id, delete_data=delete_data)
    extra = " VÀ XOÁ toàn bộ hoạt động của họ" if delete_data else " (giữ dữ liệu hoạt động)"
    await message.answer(f"Ngắt kết nối {name} (strava {strava_id}, tg {tg or '-'}){extra}?\n"
                         "Token Strava sẽ bị thu hồi; runner phải /start lại để kết nối.",
                         reply_markup=_confirm_kb("confirm", pid, ok="✅ Ngắt kết nối"))
