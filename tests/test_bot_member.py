"""Test lệnh thành viên: gọi handler trực tiếp với FakeMessage; services + session_scope được monkeypatch.

Không chạy polling, không cần admin.py.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.filters import CommandObject

from app.bot import format as fmt
from app.bot import member
from app.bot.bot import MEMBER_COMMANDS, split_text
from app.rules.engine import load_default_rules
from app.services import events, linking, stats
from app.services.stats import AthleteSummary, RankRow, TeamTotals

TZ = "Asia/Ho_Chi_Minh"


# ----------------------------------------------------------------------------- fakes
class FakeUser:
    def __init__(self, id: int, username: str | None, full_name: str):
        self.id, self.username, self.full_name = id, username, full_name


class FakeChat:
    def __init__(self, id: int, type: str):
        self.id, self.type = id, type


class FakeMessage:
    def __init__(self, text: str, *, user_id: int = 42, username: str | None = "runner",
                 full_name: str = "Nguyễn Văn A_[x]", chat_id: int | None = None, chat_type: str = "private",
                 bot=None):
        self.from_user = FakeUser(user_id, username, full_name)
        self.chat = FakeChat(chat_id if chat_id is not None else user_id, chat_type)
        self.text = text
        self.bot = bot
        self.answers: list[tuple[str, object]] = []

    async def answer(self, text: str, reply_markup=None):
        self.answers.append((text, reply_markup))

    @property
    def texts(self) -> list[str]:
        return [t for t, _ in self.answers]

    @property
    def last(self) -> str:
        return self.answers[-1][0]


class FakeBot:
    def __init__(self, username: str = "vtsbot", fail_dm: bool = False):
        self._username, self.fail_dm, self.sent = username, fail_dm, []

    async def me(self):
        return SimpleNamespace(username=self._username)

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail_dm:
            raise RuntimeError("Forbidden: bot can't initiate conversation with a user")
        self.sent.append((chat_id, text, kwargs))


class FakeCallbackQuery:
    def __init__(self, data: str, user_id: int = 42):
        self.data = data
        self.from_user = FakeUser(user_id, "runner", "Nguyễn Văn A_[x]")
        self.message = FakeMessage("", user_id=user_id)   # không phải aiogram Message -> dùng query.answer
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False):
        self.answers.append((text, show_alert))


def athlete(**kw):
    base = dict(id=1, strava_id=99, name="Nguyễn Văn A_[x]", active=True, token=object(), telegram_id=42)
    base.update(kw)
    return SimpleNamespace(**base)


def event(**kw):
    base = dict(id=7, name="Giải thử", start_date=date(2026, 11, 1), end_date=date(2026, 12, 31), status="active")
    base.update(kw)
    return SimpleNamespace(**base)


def activity(**kw):
    base = dict(id=555, distance_m=5200.0, start_date_local=datetime(2026, 11, 5, 6, 12), sport_type="Run")
    base.update(kw)
    return SimpleNamespace(**base)


def evaluation(**kw):
    base = dict(valid=True, pace_s=365.0, sport_key="run", converted_km=5.2, reasons=[])
    base.update(kw)
    return SimpleNamespace(**base)


def rows(n: int) -> list[RankRow]:
    return [RankRow(athlete_id=i, name=f"R{i}", km=float(n - i + 1), days=5, activities=6, rank=i)
            for i in range(1, n + 1)]


@pytest.fixture(autouse=True)
def fake_session(monkeypatch):
    @asynccontextmanager
    async def _scope():
        yield None

    monkeypatch.setattr(member, "session_scope", _scope)


# ----------------------------------------------------------------------------- /start /connect
async def test_start_creates_link_with_button(monkeypatch):
    monkeypatch.setattr(linking, "get_athlete_by_telegram", AsyncMock(return_value=None))
    create = AsyncMock(return_value="tok123")
    monkeypatch.setattr(linking, "create_link_token", create)

    msg = FakeMessage("/start")
    await member.cmd_start(msg)

    create.assert_awaited_once_with(None, 42, "runner")
    assert len(msg.answers) == 1
    text, kb = msg.answers[0]
    assert "đồng ý" in text and "km" in text
    btn = kb.inline_keyboard[0][0]
    assert btn.text == "Connect with Strava"
    assert btn.url == "https://run.test/connect/tok123"


async def test_start_already_connected(monkeypatch):
    monkeypatch.setattr(linking, "get_athlete_by_telegram", AsyncMock(return_value=athlete()))
    create = AsyncMock(side_effect=AssertionError("không được tạo token khi đã kết nối"))
    monkeypatch.setattr(linking, "create_link_token", create)

    msg = FakeMessage("/connect")
    await member.cmd_start(msg)

    assert len(msg.answers) == 1
    assert "đã kết nối" in msg.last and "Nguyễn Văn A_[x]" in msg.last and "/me" in msg.last
    assert msg.answers[0][1] is None
    create.assert_not_awaited()


async def test_start_inactive_or_tokenless_is_not_connected(monkeypatch):
    monkeypatch.setattr(linking, "get_athlete_by_telegram", AsyncMock(return_value=athlete(token=None)))
    monkeypatch.setattr(linking, "create_link_token", AsyncMock(return_value="t2"))
    msg = FakeMessage("/start")
    await member.cmd_start(msg)
    assert "/connect/t2" in msg.answers[0][1].inline_keyboard[0][0].url


async def test_start_in_group_sends_dm(monkeypatch):
    monkeypatch.setattr(linking, "get_athlete_by_telegram", AsyncMock(return_value=None))
    monkeypatch.setattr(linking, "create_link_token", AsyncMock(return_value="tok"))
    bot = FakeBot()
    msg = FakeMessage("/connect@vtsbot", chat_id=-100500, chat_type="supergroup", bot=bot)
    await member.cmd_start(msg)

    assert len(bot.sent) == 1
    chat_id, text, kw = bot.sent[0]
    assert chat_id == 42
    assert kw["reply_markup"].inline_keyboard[0][0].url.endswith("/connect/tok")
    assert "tin nhắn riêng" in msg.last
    assert "/connect/tok" not in msg.last          # không lộ link cá nhân trong nhóm


async def test_start_in_group_dm_fails_points_to_bot(monkeypatch):
    monkeypatch.setattr(linking, "get_athlete_by_telegram", AsyncMock(return_value=None))
    monkeypatch.setattr(linking, "create_link_token", AsyncMock(return_value="tok"))
    msg = FakeMessage("/start", chat_id=-100500, chat_type="group", bot=FakeBot(fail_dm=True))
    await member.cmd_start(msg)
    assert "@vtsbot" in msg.last and "/connect" in msg.last


# ----------------------------------------------------------------------------- /me
async def test_me_not_connected(monkeypatch):
    monkeypatch.setattr(linking, "get_athlete_by_telegram", AsyncMock(return_value=None))
    msg = FakeMessage("/me")
    await member.cmd_me(msg)
    assert msg.last == member.NOT_CONNECTED_TEXT
    assert "/connect" in msg.last


async def test_me_no_active_event(monkeypatch):
    monkeypatch.setattr(linking, "get_athlete_by_telegram", AsyncMock(return_value=athlete()))
    monkeypatch.setattr(events, "get_active_event", AsyncMock(return_value=None))
    msg = FakeMessage("/me")
    await member.cmd_me(msg)
    assert msg.last == member.NO_EVENT_TEXT


async def test_me_with_stats(monkeypatch):
    a, ev = athlete(), event()
    monkeypatch.setattr(linking, "get_athlete_by_telegram", AsyncMock(return_value=a))
    monkeypatch.setattr(events, "get_active_event", AsyncMock(return_value=ev))
    monkeypatch.setattr(events, "get_rules", AsyncMock(return_value=load_default_rules()))   # target_km 45
    summary = AthleteSummary(athlete=a, total_km=12.5, days=3, rank=2, total_athletes=10,
                             recent=[(activity(), evaluation()),
                                     (activity(id=556, distance_m=800, sport_type="Run",
                                               start_date_local=datetime(2026, 11, 4, 18, 0)),
                                      evaluation(valid=False, converted_km=0, reasons=["BELOW_MIN_DISTANCE:0.80"]))])
    summ = AsyncMock(return_value=summary)
    monkeypatch.setattr(stats, "athlete_summary", summ)

    msg = FakeMessage("/me")
    await member.cmd_me(msg)

    summ.assert_awaited_once_with(None, ev, a)
    text = msg.last
    assert text.startswith("🏃 Nguyễn Văn A_[x] · Hạng 2/10 · Tổng 12,50 km · 3 ngày")
    assert "Mục tiêu 45 km · còn thiếu 32,50 km" in text
    assert "Gần đây:" in text
    assert "✅ 06:12 05/11 Run 5,20 km 6:05/km" in text
    assert "❌ 18:00 04/11 Run 0,80 km 6:05/km — chưa đủ quãng đường tối thiểu (0.80)" in text


async def test_me_target_reached_and_unranked(monkeypatch):
    a, ev = athlete(), event()
    monkeypatch.setattr(linking, "get_athlete_by_telegram", AsyncMock(return_value=a))
    monkeypatch.setattr(events, "get_active_event", AsyncMock(return_value=ev))
    monkeypatch.setattr(events, "get_rules", AsyncMock(return_value=load_default_rules()))
    monkeypatch.setattr(stats, "athlete_summary", AsyncMock(return_value=AthleteSummary(
        athlete=a, total_km=50.0, days=12, rank=None, total_athletes=10, recent=[])))
    msg = FakeMessage("/me")
    await member.cmd_me(msg)
    assert "Hạng –/10" in msg.last
    assert "Đã đạt mục tiêu 45 km" in msg.last
    assert "chưa có hoạt động" in msg.last


# ----------------------------------------------------------------------------- /ranking
async def test_ranking_format_and_limit(monkeypatch):
    ev = event(end_date=date(2026, 10, 31))     # giải đã qua -> "đến" = ngày kết thúc (ổn định cho test)
    monkeypatch.setattr(events, "get_active_event", AsyncMock(return_value=ev))
    monkeypatch.setattr(stats, "ranking", AsyncMock(return_value=rows(25)))
    monkeypatch.setattr(stats, "team_totals", AsyncMock(return_value=TeamTotals(sum_km=300.0, active_athletes=25,
                                                                                 activities=120)))
    msg = FakeMessage("/ranking 3")
    await member.cmd_ranking(msg, CommandObject(command="ranking", args="3"))

    lines = msg.last.split("\n")
    assert lines[0] == "🏆 Bảng xếp hạng Giải thử (đến 31/10)"
    assert lines[1] == "🥇 R1 – 25,00 km · 5 ngày"
    assert lines[2] == "🥈 R2 – 24,00 km · 5 ngày"
    assert lines[3] == "🥉 R3 – 23,00 km · 5 ngày"
    assert lines[4] == "… và 22 người khác"
    assert lines[5] == "🌏 Cả đội: 300,00 km, 25 người"
    assert len(lines) == 6


async def test_ranking_default_limit_20_and_bad_arg(monkeypatch):
    monkeypatch.setattr(events, "get_active_event", AsyncMock(return_value=event()))
    monkeypatch.setattr(stats, "ranking", AsyncMock(return_value=rows(25)))
    monkeypatch.setattr(stats, "team_totals", AsyncMock(return_value=TeamTotals(300.0, 25, 120)))
    for cmd in (None, CommandObject(command="ranking", args="abc")):
        msg = FakeMessage("/ranking")
        await member.cmd_ranking(msg, cmd)
        lines = msg.last.split("\n")
        assert lines[20] == "20. R20 – 6,00 km · 5 ngày"
        assert lines[21] == "… và 5 người khác"
        assert lines[0].startswith("🏆 Bảng xếp hạng Giải thử (đến ")


# ----------------------------------------------------------------------------- /today
async def test_today_with_date_arg(monkeypatch):
    ev = event()
    monkeypatch.setattr(events, "get_active_event", AsyncMock(return_value=ev))
    daily = AsyncMock(return_value=[RankRow(1, "A", 10.0, 1, 1), RankRow(2, "B", 5.5, 1, 1)])
    monkeypatch.setattr(stats, "daily", daily)

    msg = FakeMessage("/today 05/11")
    await member.cmd_today(msg, CommandObject(command="today", args="05/11"))

    daily.assert_awaited_once_with(None, ev, date(fmt.vn_today(TZ).year, 11, 5))
    lines = msg.last.split("\n")
    assert lines[0] == "⚡ Km hợp lệ ngày 05/11"
    assert lines[1] == "🥇 A – 10,00 km"          # show_days=False
    assert lines[2] == "🥈 B – 5,50 km"
    assert lines[3] == "👥 2 người · tổng 15,50 km"


async def test_today_iso_default_and_invalid(monkeypatch):
    monkeypatch.setattr(events, "get_active_event", AsyncMock(return_value=event()))
    daily = AsyncMock(return_value=[])
    monkeypatch.setattr(stats, "daily", daily)

    msg = FakeMessage("/today 2026-11-04")
    await member.cmd_today(msg, CommandObject(command="today", args="2026-11-04"))
    assert daily.await_args.args[2] == date(2026, 11, 4)
    assert "(chưa có hoạt động hợp lệ)" in msg.last and "👥 0 người · tổng 0,00 km" in msg.last

    msg = FakeMessage("/today")
    await member.cmd_today(msg, None)
    assert daily.await_args.args[2] == fmt.vn_today(TZ)

    msg = FakeMessage("/today 99/99")
    await member.cmd_today(msg, CommandObject(command="today", args="99/99"))
    assert "không hợp lệ" in msg.last


# ----------------------------------------------------------------------------- /log
async def test_log(monkeypatch):
    a, ev = athlete(), event()
    monkeypatch.setattr(linking, "get_athlete_by_telegram", AsyncMock(return_value=a))
    monkeypatch.setattr(events, "get_active_event", AsyncMock(return_value=ev))
    log = AsyncMock(return_value=[(activity(), evaluation()), (activity(id=2), None)])
    monkeypatch.setattr(stats, "activity_log", log)

    msg = FakeMessage("/log 5")
    await member.cmd_log(msg, CommandObject(command="log", args="5"))
    log.assert_awaited_once_with(None, ev, a, 5)
    lines = msg.last.split("\n")
    assert lines[0].startswith("📒") and "Nguyễn Văn A_[x]" in lines[0]
    assert lines[1] == "✅ 06:12 05/11 Run 5,20 km 6:05/km"
    assert lines[2].startswith("⚪") and "chưa chấm" in lines[2]


async def test_log_not_connected(monkeypatch):
    monkeypatch.setattr(linking, "get_athlete_by_telegram", AsyncMock(return_value=None))
    msg = FakeMessage("/log")
    await member.cmd_log(msg, None)
    assert msg.last == member.NOT_CONNECTED_TEXT


# ----------------------------------------------------------------------------- /rules
def test_rules_text_from_defaults():
    text = member.rules_text(load_default_rules())
    assert "Giải chạy VTS Running Club" in text
    assert "4:00" in text and "17:00" in text
    assert "×0,25" in text and "4 km = 1 km quy đổi" in text
    assert "/100m" in text
    assert "Mục tiêu: 45 km" in text
    assert "VirtualRide" in text
    assert "Bản đồ GPS: bắt buộc" in text and "Nhập tay: loại" in text
    assert "*" not in text and "`" not in text       # văn bản thuần, không ký hiệu markdown


async def test_rules_command(monkeypatch):
    monkeypatch.setattr(events, "get_active_event", AsyncMock(return_value=event()))
    monkeypatch.setattr(events, "get_rules", AsyncMock(return_value=load_default_rules()))
    monkeypatch.setattr(events, "get_rules_version", AsyncMock(return_value=3))
    msg = FakeMessage("/rules")
    await member.cmd_rules(msg)
    assert "4:00" in msg.last and "17:00" in msg.last
    assert "v3" in msg.last


async def test_rules_without_event_shows_defaults(monkeypatch):
    monkeypatch.setattr(events, "get_active_event", AsyncMock(return_value=None))
    msg = FakeMessage("/rules")
    await member.cmd_rules(msg)
    assert msg.last.startswith(member.NO_EVENT_TEXT)
    assert "17:00" in msg.last


# ----------------------------------------------------------------------------- /unlink
async def test_unlink_confirmation_keyboard(monkeypatch):
    monkeypatch.setattr(linking, "get_athlete_by_telegram", AsyncMock(return_value=athlete()))
    msg = FakeMessage("/unlink")
    await member.cmd_unlink(msg)
    text, kb = msg.answers[0]
    assert "chắc chắn" in text
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert datas == ["unlink:yes", "unlink:no"]


async def test_unlink_in_group_redirects_to_private(monkeypatch):
    msg = FakeMessage("/unlink", chat_id=-100500, chat_type="supergroup")
    await member.cmd_unlink(msg)
    assert "tin nhắn riêng" in msg.last and msg.answers[0][1] is None


async def test_unlink_callback_yes_and_no(monkeypatch):
    a = athlete()
    monkeypatch.setattr(linking, "get_athlete_by_telegram", AsyncMock(return_value=a))
    unlink = AsyncMock(return_value=None)
    monkeypatch.setattr(linking, "unlink", unlink)
    monkeypatch.setattr(member, "get_client", lambda: "CLIENT")

    q = FakeCallbackQuery("unlink:yes")
    await member.cb_unlink(q)
    unlink.assert_awaited_once_with(None, "CLIENT", a, delete_data=False)
    assert q.answers[-1][0] == member.UNLINK_DONE_TEXT

    q = FakeCallbackQuery("unlink:no")
    await member.cb_unlink(q)
    assert unlink.await_count == 1
    assert "huỷ" in q.answers[-1][0].lower()


# ----------------------------------------------------------------------------- /id /help
async def test_id():
    msg = FakeMessage("/id", chat_id=-100500, chat_type="supergroup")
    await member.cmd_id(msg)
    assert "User ID: 42" in msg.last
    assert "Chat ID: -100500" in msg.last
    assert "supergroup" in msg.last


async def test_help_member_and_admin():
    msg = FakeMessage("/help", user_id=42)
    await member.cmd_help(msg)
    for cmd, _ in MEMBER_COMMANDS:
        assert f"/{cmd} " in msg.last
    assert "Lệnh admin" not in msg.last

    msg = FakeMessage("/help", user_id=1000)     # ADMIN_TELEGRAM_IDS=1000 trong conftest
    await member.cmd_help(msg)
    assert "Lệnh admin" in msg.last and "/admin" in msg.last


def test_member_commands_cover_required():
    names = [c for c, _ in MEMBER_COMMANDS]
    assert names == ["start", "connect", "me", "today", "ranking", "log", "rules", "unlink", "help"]
    assert all(desc for _, desc in MEMBER_COMMANDS)


# ----------------------------------------------------------------------------- lỗi & cắt tin
async def test_unexpected_error_gives_friendly_message(monkeypatch):
    monkeypatch.setattr(linking, "get_athlete_by_telegram", AsyncMock(side_effect=RuntimeError("db down")))
    msg = FakeMessage("/me")
    await member.cmd_me(msg)
    assert msg.last == member.ERROR_TEXT


async def test_long_reply_is_split_by_lines(monkeypatch):
    monkeypatch.setattr(events, "get_active_event", AsyncMock(return_value=event()))
    monkeypatch.setattr(stats, "ranking", AsyncMock(return_value=[
        RankRow(i, "X" * 60, 1.0, 1, 1) for i in range(100)]))
    monkeypatch.setattr(stats, "team_totals", AsyncMock(return_value=TeamTotals(100.0, 100, 100)))
    msg = FakeMessage("/ranking 100")
    await member.cmd_ranking(msg, CommandObject(command="ranking", args="100"))
    assert len(msg.answers) >= 2
    assert all(len(t) <= 4000 for t in msg.texts)
    assert "\n".join(msg.texts).count("\n") == 101     # 1 tiêu đề + 100 dòng + tổng đội, không mất dòng nào


def test_split_text():
    assert split_text("ngắn") == ["ngắn"]
    text = "\n".join(f"dòng {i}" for i in range(1000))
    parts = split_text(text, limit=500)
    assert all(len(p) <= 500 for p in parts)
    assert "\n".join(parts) == text
    assert split_text("a" * 1200, limit=500) == ["a" * 500, "a" * 500, "a" * 200]
