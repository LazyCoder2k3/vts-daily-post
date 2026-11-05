"""Lệnh cho thành viên (runner): /start /connect /me /today /ranking /log /rules /unlink /id /help.

Mọi handler mở `async with session_scope() as s:` rồi gọi services; trả lời tiếng Việt, văn bản thuần.
Hoạt động cả trong chat riêng và nhóm (aiogram Command tự xử lý hậu tố @botname).
"""
from __future__ import annotations

import functools
import logging
from datetime import date, datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot import format as fmt
from app.bot.bot import MEMBER_COMMANDS, split_text
from app.config import get_settings
from app.db.base import session_scope
from app.deps import get_client
from app.rules.schema import RuleSetConfig, SportRule
from app.services import events, linking, stats

log = logging.getLogger(__name__)

router = Router(name="member")

ERROR_TEXT = "⚠️ Có lỗi, thử lại sau."
NOT_CONNECTED_TEXT = "Bạn chưa kết nối Strava. Gõ /connect để bắt đầu."
NO_EVENT_TEXT = "Hiện chưa có giải nào đang diễn ra."
UNLINK_DONE_TEXT = "Đã ngắt kết nối. Dữ liệu đã tính vẫn giữ; muốn xoá hẳn hãy nhắn admin."

SPORT_LABELS = {
    "run": "Chạy / đi bộ",
    "ride": "Đạp xe",
    "swim": "Bơi",
    "walk": "Đi bộ",
    "hike": "Leo núi",
}


# ----------------------------------------------------------------------------- tiện ích
def safe(handler):
    """Bắt mọi lỗi bất ngờ trong handler: ghi log và trả lời ngắn cho người dùng.

    Dùng functools.wraps để aiogram vẫn đọc được chữ ký gốc (inject `command`, `bot`, …).
    """

    @functools.wraps(handler)
    async def wrapper(event, *args, **kwargs):
        try:
            return await handler(event, *args, **kwargs)
        except Exception:  # noqa: BLE001
            log.exception("Lỗi trong handler %s", handler.__name__)
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer(ERROR_TEXT, show_alert=True)
                else:
                    await event.answer(ERROR_TEXT)
            except Exception:  # noqa: BLE001
                log.debug("Không gửi được thông báo lỗi", exc_info=True)

    return wrapper


async def _reply(message: Message, text: str, reply_markup=None) -> None:
    """Trả lời, tự cắt tin dài; bàn phím (nếu có) gắn vào phần cuối."""
    chunks = split_text(text)
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        await message.answer(chunk, reply_markup=reply_markup if is_last else None)


async def _bot_username(bot) -> str | None:
    if bot is None:
        return None
    try:
        me = await bot.me()
        return me.username
    except Exception:  # noqa: BLE001
        return None


async def _has_token(session, athlete) -> bool:
    """athlete.token là quan hệ lazy; với session async phải nạp trước khi đọc."""
    try:
        from sqlalchemy import inspect as sa_inspect
        state = sa_inspect(athlete)
        if "token" in state.unloaded and session is not None:
            await session.refresh(athlete, attribute_names=["token"])
    except Exception:  # noqa: BLE001 — không phải ORM instance (test) hoặc không refresh được
        pass
    return getattr(athlete, "token", None) is not None


async def _connected_athlete(session, telegram_id: int):
    """Athlete đang kết nối (active + còn token) hoặc None."""
    athlete = await linking.get_athlete_by_telegram(session, telegram_id)
    if athlete is None or not getattr(athlete, "active", False):
        return None
    if not await _has_token(session, athlete):
        return None
    return athlete


def _parse_int(args: str | None, default: int, lo: int, hi: int) -> int:
    if not args:
        return default
    try:
        n = int(args.strip().split()[0])
    except (ValueError, IndexError):
        return default
    return max(lo, min(hi, n))


def parse_day(args: str | None, tz: str) -> date | None:
    """'' -> hôm nay; 'dd/mm', 'dd/mm/yyyy', 'yyyy-mm-dd', 'hôm qua' -> ngày; sai định dạng -> None."""
    today = fmt.vn_today(tz)
    if not args or not args.strip():
        return today
    s = args.strip().split()[0].lower()
    if s in ("hôm_qua", "homqua", "yesterday", "qua"):
        return fmt.yesterday(tz)
    if s in ("hôm_nay", "homnay", "today", "nay"):
        return today
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, pattern).date()
        except ValueError:
            continue
    try:  # dd/mm -> năm hiện tại
        return datetime.strptime(f"{s}/{today.year}", "%d/%m/%Y").date()
    except ValueError:
        return None


def _num(x: float) -> str:
    """1.0 -> '1', 0.25 -> '0,25'."""
    return f"{x:g}".replace(".", ",")


# ----------------------------------------------------------------------------- render luật
def _sport_text(sp: SportRule, general) -> list[str]:
    label = SPORT_LABELS.get(sp.key, sp.key)
    unit = "/100m" if sp.pace_unit == "per_100m" else "/km"
    lines = [f"• {label} ({sp.key}): {', '.join(sp.types)}"]
    detail: list[str] = []
    factor = f"hệ số ×{_num(sp.factor)}"
    if sp.factor and sp.factor != 1.0:
        if sp.factor < 1:
            factor += f" ({_num(1 / sp.factor)} km = 1 km quy đổi)"
        else:
            factor += f" (1 km = {_num(sp.factor)} km quy đổi)"
    detail.append(factor)
    if sp.min_distance_km:
        detail.append(f"tối thiểu {_num(sp.min_distance_km)} km")
    if sp.max_distance_km is not None:
        detail.append(f"tối đa {_num(sp.max_distance_km)} km")
    if sp.pace_min and sp.pace_max:
        detail.append(f"pace {sp.pace_min}–{sp.pace_max}{unit}")
    elif sp.pace_min:
        detail.append(f"pace nhanh nhất {sp.pace_min}{unit}")
    elif sp.pace_max:
        detail.append(f"pace chậm nhất {sp.pace_max}{unit}")
    lines.append("   " + " · ".join(detail))
    overrides: list[str] = []
    if sp.require_gps_map is not None and sp.require_gps_map != general.require_gps_map:
        overrides.append("bản đồ GPS: " + ("bắt buộc" if sp.require_gps_map else "không bắt buộc"))
    if sp.trainer_policy is not None and sp.trainer_policy != general.trainer_policy:
        overrides.append("treadmill/máy tập: " + ("được tính" if sp.trainer_policy == "allow" else "loại"))
    if overrides:
        lines.append("   riêng môn này: " + "; ".join(overrides))
    return lines


def rules_text(cfg: RuleSetConfig) -> str:
    """Luật giải dạng tiếng Việt dễ đọc (dùng cho /rules; admin có thể tái dùng)."""
    ev, g = cfg.event, cfg.general
    lines = [
        f"📜 Luật giải: {ev.name}",
        f"📅 Thời gian: {ev.start.day:02d}/{ev.start.month:02d}/{ev.start.year} – "
        f"{ev.end.day:02d}/{ev.end.month:02d}/{ev.end.year} (giờ {ev.timezone})",
    ]
    if ev.grace_days:
        lines.append(f"⏳ Đồng bộ trễ: chấp nhận tới {ev.grace_days} ngày sau khi giải kết thúc "
                     f"(hoạt động vẫn phải diễn ra trong giải)")
    if ev.target_km:
        lines.append(f"🎯 Mục tiêu: {_num(ev.target_km)} km quy đổi / người")

    lines += ["", "🏅 Môn được tính:"]
    for sp in cfg.sports:
        lines += _sport_text(sp, g)

    basis = "thời gian di chuyển (moving)" if g.pace_basis == "moving" else "tổng thời gian (elapsed)"
    overlap = "loại hoạt động tải sau" if g.overlap_policy == "reject_later" else "bỏ qua (không kiểm tra)"
    lines += [
        "",
        "⚙️ Quy định chung:",
        f"• Bản đồ GPS: {'bắt buộc' if g.require_gps_map else 'không bắt buộc'}",
        f"• Nhập tay: {'loại' if g.reject_manual else 'được tính'}",
        f"• Treadmill / máy tập: {'loại' if g.trainer_policy == 'reject' else 'được tính'}",
        f"• Bị Strava gắn cờ: {'loại' if g.reject_flagged else 'được tính'}",
        f"• Pace tính theo: {basis}",
        f"• Hai hoạt động trùng thời gian: {overlap}",
        f"• Giới hạn km quy đổi / ngày: {_num(g.daily_cap_km) + ' km' if g.daily_cap_km else 'không giới hạn'}",
    ]
    if g.min_moving_ratio:
        lines.append(f"• Tỷ lệ thời gian di chuyển tối thiểu: {_num(g.min_moving_ratio * 100)}%")
    if g.allowed_visibility:
        lines.append(f"• Chỉ tính hoạt động ở chế độ: {', '.join(g.allowed_visibility)}")

    if cfg.excluded_types:
        lines += ["", "🚫 Không tính: " + ", ".join(cfg.excluded_types)]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- /start /connect
def _connect_text(name: str) -> str:
    return (
        f"👋 Chào {name}! Mình là bot thống kê chạy bộ của VTS Running Club.\n"
        "Bấm nút bên dưới để kết nối Strava — bot chỉ đọc hoạt động của bạn, không đăng gì lên Strava.\n\n"
        "Khi kết nối, bạn đồng ý cho bot hiển thị tên và số km quy đổi của bạn trong nhóm.\n"
        "Link có hiệu lực 30 phút. Sau khi kết nối gõ /me để xem thành tích."
    )


@router.message(Command("start", "connect"))
@safe
async def cmd_start(message: Message) -> None:
    user = message.from_user
    settings = get_settings()
    async with session_scope() as s:
        athlete = await _connected_athlete(s, user.id)
        if athlete is not None:
            await _reply(message, f"✅ Bạn đã kết nối với Strava: {athlete.name}. Gõ /me để xem thành tích.")
            return
        token = await linking.create_link_token(s, user.id, user.username)

    url = f"{settings.public_base_url.rstrip('/')}/connect/{token}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Connect with Strava", url=url)]])
    text = _connect_text(user.full_name or user.username or "bạn")

    if message.chat.type == "private":
        await _reply(message, text, reply_markup=kb)
        return

    # Trong nhóm: không dán link cá nhân vào nhóm, cố gửi qua tin nhắn riêng.
    bot = message.bot
    if bot is not None:
        try:
            await bot.send_message(user.id, text, reply_markup=kb, disable_web_page_preview=True)
            await _reply(message, f"📩 {user.full_name}, mình đã gửi link kết nối Strava qua tin nhắn riêng, "
                                  "bạn kiểm tra nhé.")
            return
        except Exception as e:  # noqa: BLE001 — thường do user chưa từng mở chat với bot
            log.info("Không DM được cho %s: %s", user.id, e)
    username = await _bot_username(bot)
    hint = f"Hãy mở @{username} và gõ /connect" if username else "Hãy mở tin nhắn riêng với bot và gõ /connect"
    await _reply(message, f"🔒 Để bảo mật, mời bạn kết nối Strava trong tin nhắn riêng. {hint}.")


# ----------------------------------------------------------------------------- /me
@router.message(Command("me"))
@safe
async def cmd_me(message: Message) -> None:
    user = message.from_user
    async with session_scope() as s:
        athlete = await _connected_athlete(s, user.id)
        if athlete is None:
            await _reply(message, NOT_CONNECTED_TEXT)
            return
        event = await events.get_active_event(s)
        if event is None:
            await _reply(message, NO_EVENT_TEXT)
            return
        cfg = await events.get_rules(s, event)
        summ = await stats.athlete_summary(s, event, athlete)

    rank = str(summ.rank) if summ.rank else "–"
    lines = [f"🏃 {athlete.name} · Hạng {rank}/{summ.total_athletes} · Tổng {fmt.fmt_km(summ.total_km)} km "
             f"· {summ.days} ngày"]
    target = cfg.event.target_km
    if target:
        if summ.total_km >= target:
            lines.append(f"🎯 Đã đạt mục tiêu {_num(target)} km 🎉")
        else:
            lines.append(f"🎯 Mục tiêu {_num(target)} km · còn thiếu {fmt.fmt_km(target - summ.total_km)} km")
    if summ.recent:
        lines.append("Gần đây:")
        lines += [fmt.activity_line(a, ev) for a, ev in summ.recent]
    else:
        lines.append("(chưa có hoạt động nào trong giải)")
    await _reply(message, "\n".join(lines))


# ----------------------------------------------------------------------------- /today
@router.message(Command("today"))
@safe
async def cmd_today(message: Message, command: CommandObject | None = None) -> None:
    tz = get_settings().timezone
    day = parse_day(command.args if command else None, tz)
    if day is None:
        await _reply(message, "Ngày không hợp lệ. Dùng /today, /today 05/11 hoặc /today 2026-11-05.")
        return
    async with session_scope() as s:
        event = await events.get_active_event(s)
        if event is None:
            await _reply(message, NO_EVENT_TEXT)
            return
        rows = await stats.daily(s, event, day)
    text = fmt.ranking_text(rows, f"⚡ Km hợp lệ ngày {fmt.fmt_date(day)}", limit=50, show_days=False)
    total = sum(r.km for r in rows)
    text += f"\n👥 {len(rows)} người · tổng {fmt.fmt_km(total)} km"
    await _reply(message, text)


# ----------------------------------------------------------------------------- /ranking
@router.message(Command("ranking"))
@safe
async def cmd_ranking(message: Message, command: CommandObject | None = None) -> None:
    n = _parse_int(command.args if command else None, default=20, lo=1, hi=100)
    tz = get_settings().timezone
    async with session_scope() as s:
        event = await events.get_active_event(s)
        if event is None:
            await _reply(message, NO_EVENT_TEXT)
            return
        rows = await stats.ranking(s, event)
        totals = await stats.team_totals(s, event)
    until = min(fmt.vn_today(tz), event.end_date)
    text = fmt.ranking_text(rows, f"🏆 Bảng xếp hạng {event.name} (đến {fmt.fmt_date(until)})", limit=n)
    text += f"\n🌏 Cả đội: {fmt.fmt_km(totals.sum_km)} km, {totals.active_athletes} người"
    await _reply(message, text)


# ----------------------------------------------------------------------------- /log
@router.message(Command("log"))
@safe
async def cmd_log(message: Message, command: CommandObject | None = None) -> None:
    n = _parse_int(command.args if command else None, default=10, lo=1, hi=50)
    user = message.from_user
    async with session_scope() as s:
        athlete = await _connected_athlete(s, user.id)
        if athlete is None:
            await _reply(message, NOT_CONNECTED_TEXT)
            return
        event = await events.get_active_event(s)
        if event is None:
            await _reply(message, NO_EVENT_TEXT)
            return
        items = await stats.activity_log(s, event, athlete, n)
    lines = [f"📒 Hoạt động gần đây của {athlete.name}:"]
    if items:
        lines += [fmt.activity_line(a, ev) for a, ev in items]
    else:
        lines.append("(chưa có hoạt động nào trong giải)")
    await _reply(message, "\n".join(lines))


# ----------------------------------------------------------------------------- /rules
@router.message(Command("rules"))
@safe
async def cmd_rules(message: Message) -> None:
    async with session_scope() as s:
        event = await events.get_active_event(s)
        if event is None:
            from app.rules.engine import load_default_rules
            text = NO_EVENT_TEXT + " Luật mặc định sẽ áp dụng cho giải tới:\n\n" + rules_text(load_default_rules())
            await _reply(message, text)
            return
        cfg = await events.get_rules(s, event)
        version = await events.get_rules_version(s, event)
    await _reply(message, rules_text(cfg) + f"\n\n(phiên bản luật v{version})")


# ----------------------------------------------------------------------------- /unlink
@router.message(Command("unlink"))
@safe
async def cmd_unlink(message: Message) -> None:
    user = message.from_user
    if message.chat.type != "private":
        await _reply(message, "🔒 Để tránh nhầm người, hãy gõ /unlink trong tin nhắn riêng với bot.")
        return
    async with session_scope() as s:
        athlete = await _connected_athlete(s, user.id)
    if athlete is None:
        await _reply(message, "Bạn hiện không kết nối Strava. Gõ /connect nếu muốn kết nối.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Ngắt kết nối", callback_data="unlink:yes"),
        InlineKeyboardButton(text="Huỷ", callback_data="unlink:no"),
    ]])
    await _reply(message, f"Bạn chắc chắn muốn ngắt kết nối Strava ({athlete.name})?\n"
                          "Bot sẽ thu hồi quyền truy cập; km đã tính trong giải vẫn được giữ.", reply_markup=kb)


async def _cb_reply(query: CallbackQuery, text: str) -> None:
    msg = query.message
    if isinstance(msg, Message):
        try:
            await msg.edit_text(text, reply_markup=None)
            await query.answer()
            return
        except Exception:  # noqa: BLE001 — tin quá cũ / đã sửa
            log.debug("Không edit được tin xác nhận", exc_info=True)
    await query.answer(text, show_alert=True)


@router.callback_query(F.data.startswith("unlink:"))
@safe
async def cb_unlink(query: CallbackQuery) -> None:
    choice = (query.data or "").split(":", 1)[1]
    if choice != "yes":
        await _cb_reply(query, "Đã huỷ. Bạn vẫn kết nối với Strava.")
        return
    async with session_scope() as s:
        athlete = await linking.get_athlete_by_telegram(s, query.from_user.id)
        if athlete is None or not getattr(athlete, "active", False):
            await _cb_reply(query, "Bạn hiện không kết nối Strava.")
            return
        await linking.unlink(s, get_client(), athlete, delete_data=False)
    await _cb_reply(query, UNLINK_DONE_TEXT)


# ----------------------------------------------------------------------------- /id /help
@router.message(Command("id"))
@safe
async def cmd_id(message: Message) -> None:
    user = message.from_user
    lines = [
        f"🆔 User ID: {user.id}" if user else "🆔 User ID: (không có)",
        f"💬 Chat ID: {message.chat.id}",
        f"📂 Loại chat: {message.chat.type}",
    ]
    await _reply(message, "\n".join(lines))


@router.message(Command("help"))
@safe
async def cmd_help(message: Message) -> None:
    lines = ["📖 Lệnh dành cho runner:"]
    lines += [f"/{cmd} – {desc}" for cmd, desc in MEMBER_COMMANDS]
    lines.append("/id – Xem user id / chat id")
    if message.from_user and get_settings().is_admin(message.from_user.id):
        lines += ["", "🔧 Lệnh admin: /status /event /rules set … — xem /admin"]
    await _reply(message, "\n".join(lines))
