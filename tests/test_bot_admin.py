"""Test lệnh admin: gọi handler trực tiếp với FakeMessage, services được monkeypatch qua module."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from aiogram.filters import CommandObject

from app.bot import admin
from app.bot.guards import IsAdmin
from app.rules.engine import load_default_rules
from app.services import activities, events, linking, stats
from app.services.activities import ReevalSummary

ADMIN_ID = 1000   # theo conftest ADMIN_TELEGRAM_IDS


# ------------------------------------------------------------------ fakes
@dataclass
class FakeUser:
    id: int
    username: str | None = "admin"


@dataclass
class FakeChat:
    id: int = ADMIN_ID
    type: str = "private"


class FakeMessage:
    def __init__(self, text: str = "", user_id: int = ADMIN_ID, bot=None, document=None):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.chat = FakeChat(user_id)
        self.bot = bot
        self.document = document
        self.answers: list[tuple[str, object]] = []
        self.documents: list[tuple[object, str | None]] = []
        self.edits: list[str] = []

    async def answer(self, text: str, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))

    async def answer_document(self, document, caption=None, **kw):
        self.documents.append((document, caption))

    async def edit_text(self, text: str, **kw):
        self.edits.append(text)

    @property
    def last(self) -> str:
        return self.answers[-1][0]

    @property
    def last_markup(self):
        return self.answers[-1][1]


class FakeCallback:
    def __init__(self, data: str, user_id: int = ADMIN_ID, message: FakeMessage | None = None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = message or FakeMessage(user_id=user_id)
        self.bot = None
        self.answered: list[tuple[str | None, bool | None]] = []

    async def answer(self, text=None, show_alert=None, **kw):
        self.answered.append((text, show_alert))


def cmd(args: str | None = None, name: str = "x") -> CommandObject:
    return CommandObject(command=name, args=args)


def fake_event(**kw) -> SimpleNamespace:
    base = dict(id=7, name="Giải thử", start_date=date(2027, 1, 1), end_date=date(2027, 1, 31),
                timezone="Asia/Ho_Chi_Minh", status="active", current_rules_id=1)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def no_db(monkeypatch):
    """session_scope giả: không có DB, session = None."""
    @asynccontextmanager
    async def _scope():
        yield None
    monkeypatch.setattr(admin, "session_scope", _scope)
    admin._pending.clear()
    admin._awaiting_import.clear()
    yield
    admin._pending.clear()
    admin._awaiting_import.clear()


@pytest.fixture
def active_event(monkeypatch):
    ev = fake_event()

    async def _active(s):
        return ev

    async def _get(s, event_id):
        return ev if event_id == ev.id else None

    monkeypatch.setattr(events, "get_active_event", _active)
    monkeypatch.setattr(events, "get_event", _get)
    return ev


# ------------------------------------------------------------------ IsAdmin
async def test_is_admin_filter():
    assert await IsAdmin()(FakeMessage(user_id=ADMIN_ID)) is True
    assert await IsAdmin()(FakeMessage(user_id=999)) is False
    msg = FakeMessage()
    msg.from_user = None
    assert await IsAdmin()(msg) is False


def test_router_has_admin_filter():
    assert admin.router.name == "admin"
    # cả message và callback_query đều lọc IsAdmin
    assert any(isinstance(f.callback, IsAdmin) for f in admin.router.message._handler.filters or [])
    assert any(isinstance(f.callback, IsAdmin) for f in admin.router.callback_query._handler.filters or [])


# ------------------------------------------------------------------ /event
async def test_event_new_parses_quoted_name_and_dates(no_db, monkeypatch):
    calls = []

    async def _create(s, name, start, end, *, by, base=None, activate=True):
        calls.append(dict(name=name, start=start, end=end, by=by, activate=activate))
        return fake_event(id=9, name=name, start_date=start, end_date=end, status="active" if activate else "draft")

    async def _ver(s, ev):
        return 1

    monkeypatch.setattr(events, "create_event", _create)
    monkeypatch.setattr(events, "get_rules_version", _ver)

    msg = FakeMessage()
    await admin.cmd_event(msg, cmd('new "Giải mùa xuân 2027" 2027-01-01 2027-01-31'))
    assert calls == [dict(name="Giải mùa xuân 2027", start=date(2027, 1, 1), end=date(2027, 1, 31),
                          by=ADMIN_ID, activate=True)]
    assert "Giải mùa xuân 2027" in msg.last and "/rules" in msg.last

    msg2 = FakeMessage()
    await admin.cmd_event(msg2, cmd('new "Giải nháp" 2027-02-01 2027-02-28 draft'))
    assert calls[-1]["activate"] is False and calls[-1]["name"] == "Giải nháp"
    assert "draft" in msg2.last


async def test_event_new_rejects_bad_dates(no_db, monkeypatch):
    called = []

    async def _create(*a, **k):
        called.append(1)

    monkeypatch.setattr(events, "create_event", _create)
    msg = FakeMessage()
    await admin.cmd_event(msg, cmd('new "X" 2027-01-31 2027-01-01'))
    assert not called and "kết thúc" in msg.last
    msg = FakeMessage()
    await admin.cmd_event(msg, cmd('new "X" 2027-13-01 2027-01-01'))
    assert not called and msg.last.startswith("⚠️ Lỗi:")


async def test_event_list_marks_active(no_db, monkeypatch):
    async def _list(s):
        return [fake_event(id=1, name="Cũ", status="closed"), fake_event(id=2, name="Mới", status="active")]

    monkeypatch.setattr(events, "list_events", _list)
    msg = FakeMessage()
    await admin.cmd_event(msg, cmd(None))
    assert "★ #2 «Mới»" in msg.last and "• #1 «Cũ»" in msg.last


# ------------------------------------------------------------------ /rules
@pytest.fixture
def default_rules(monkeypatch, active_event):
    cfg = load_default_rules()

    async def _rules(s, ev):
        return cfg

    async def _ver(s, ev):
        return 3

    monkeypatch.setattr(events, "get_rules", _rules)
    monkeypatch.setattr(events, "get_rules_version", _ver)
    return cfg


async def test_rules_show_sends_yaml_text(no_db, default_rules):
    msg = FakeMessage()
    await admin.cmd_rules(msg, cmd(None))
    assert "v3" in msg.answers[0][0]
    assert "pace_max: '17:00'" in msg.answers[0][0] or "pace_max: 17:00" in msg.answers[0][0]


async def test_rules_set_preview_then_apply(no_db, default_rules, active_event, monkeypatch):
    msg = FakeMessage()
    await admin.cmd_rules(msg, cmd("set sports.run.pace_max 18:00"))
    assert "sports.run.pace_max: 17:00 → 18:00" in msg.last
    kb = msg.last_markup
    assert kb is not None
    buttons = kb.inline_keyboard[0]
    apply_btn = next(b for b in buttons if b.callback_data.startswith("rules:apply:"))
    cancel_btn = next(b for b in buttons if b.callback_data.startswith("rules:cancel:"))
    pid = apply_btn.callback_data.split(":")[-1]
    assert admin._pending[pid]["path"] == "sports.run.pace_max"
    assert admin._pending[pid]["value"] == "18:00"
    assert admin._pending[pid]["admin_id"] == ADMIN_ID
    assert cancel_btn.callback_data.endswith(pid)

    calls = []

    async def _set(s, ev, path, value, *, by):
        calls.append((ev.id, path, value, by))
        return SimpleNamespace(version=4), "17:00", "18:00"

    monkeypatch.setattr(events, "set_rule_value", _set)

    # người khác bấm -> bị từ chối
    other = FakeCallback(apply_btn.callback_data, user_id=2000)
    await admin.on_confirm(other)
    assert calls == [] and pid in admin._pending

    cb = FakeCallback(apply_btn.callback_data)
    await admin.on_confirm(cb)
    assert calls == [(active_event.id, "sports.run.pace_max", "18:00", ADMIN_ID)]
    assert pid not in admin._pending
    assert "/recheck" in cb.message.edits[-1] and "v4" in cb.message.edits[-1]

    # bấm lại -> hết hạn
    cb2 = FakeCallback(apply_btn.callback_data)
    await admin.on_confirm(cb2)
    assert len(calls) == 1 and cb2.answered[-1][1] is True


async def test_rules_set_cancel(no_db, default_rules):
    msg = FakeMessage()
    await admin.cmd_rules(msg, cmd("set general.daily_cap_km 30"))
    assert "general.daily_cap_km: null → 30" in msg.last
    pid = msg.last_markup.inline_keyboard[0][0].callback_data.split(":")[-1]
    cb = FakeCallback(f"rules:cancel:{pid}")
    await admin.on_confirm(cb)
    assert pid not in admin._pending and "huỷ" in cb.message.edits[-1].lower()


async def test_rules_set_no_change(no_db, default_rules):
    msg = FakeMessage()
    await admin.cmd_rules(msg, cmd("set sports.run.pace_max 17:00"))
    assert msg.last_markup is None and "không có gì thay đổi" in msg.last
    assert not admin._pending


async def test_rules_set_bad_path_reports_error(no_db, default_rules):
    msg = FakeMessage()
    await admin.cmd_rules(msg, cmd("set general.khong_ton_tai 5"))
    assert msg.last.startswith("❌") and "khong_ton_tai" in msg.last
    assert "general.<field>" in msg.last and "sports.<key>.<field>" in msg.last and "excluded_types" in msg.last
    assert not admin._pending

    msg = FakeMessage()
    await admin.cmd_rules(msg, cmd("set sports.bike.factor 0.5"))
    assert msg.last.startswith("❌") and "bike" in msg.last and "run, ride, swim" in msg.last


async def test_rules_set_invalid_value_reports_validation_error(no_db, default_rules):
    msg = FakeMessage()
    await admin.cmd_rules(msg, cmd("set event.end 2020-01-01"))
    assert msg.last.startswith("❌") and "event.end" in msg.last
    msg = FakeMessage()
    await admin.cmd_rules(msg, cmd("set sports.run.pace_min abc"))
    assert msg.last.startswith("❌")


async def test_rules_export_sends_file(no_db, default_rules):
    msg = FakeMessage()
    await admin.cmd_rules(msg, cmd("export"))
    doc, caption = msg.documents[-1]
    assert doc.filename == "rules-v3.yaml" and b"sports:" in doc.data and "v3" in caption


async def test_rules_import_flow(no_db, default_rules, active_event, monkeypatch):
    msg = FakeMessage()
    await admin.cmd_rules(msg, cmd("import"))
    assert ADMIN_ID in admin._awaiting_import and ".yaml" in msg.last

    updated = []

    async def _update(s, ev, cfg, *, by, note=None):
        updated.append((ev.id, cfg.sports[0].pace_max, by, note))
        return SimpleNamespace(version=4)

    monkeypatch.setattr(events, "update_rules", _update)

    yaml_text = load_default_rules().with_value("sports.run.pace_max", "19:00").to_yaml()

    class FakeBot:
        async def download(self, doc):
            import io
            return io.BytesIO(yaml_text.encode("utf-8"))

    doc = SimpleNamespace(file_id="f1", file_name="rules.yaml", file_size=len(yaml_text))
    dmsg = FakeMessage(bot=FakeBot(), document=doc)
    await admin.on_document(dmsg)
    assert updated == [(active_event.id, "19:00", ADMIN_ID, "import rules.yaml")]
    assert "v4" in dmsg.last and "/recheck" in dmsg.last
    assert ADMIN_ID not in admin._awaiting_import

    # không chờ -> bỏ qua im lặng
    dmsg2 = FakeMessage(bot=FakeBot(), document=doc)
    await admin.on_document(dmsg2)
    assert dmsg2.answers == []


async def test_rules_import_bad_yaml(no_db, default_rules, monkeypatch):
    await admin.cmd_rules(FakeMessage(), cmd("import"))

    class FakeBot:
        async def download(self, doc):
            import io
            return io.BytesIO(b"event:\n  name: x\n  start: 2027-02-01\n  end: 2027-01-01\nsports: []\n")

    called = []

    async def _update(*a, **k):
        called.append(1)

    monkeypatch.setattr(events, "update_rules", _update)
    dmsg = FakeMessage(bot=FakeBot(), document=SimpleNamespace(file_id="f", file_name="bad.yaml", file_size=10))
    await admin.on_document(dmsg)
    assert not called and dmsg.last.startswith("❌")


async def test_rules_reset_keeps_event_identity(no_db, default_rules, active_event, monkeypatch):
    msg = FakeMessage()
    await admin.cmd_rules(msg, cmd("reset"))
    pid = msg.last_markup.inline_keyboard[0][0].callback_data.split(":")[-1]
    assert admin._pending[pid]["kind"] == "rules_reset"

    saved = []

    async def _update(s, ev, cfg, *, by, note=None):
        saved.append(cfg)
        return SimpleNamespace(version=4)

    monkeypatch.setattr(events, "update_rules", _update)
    cb = FakeCallback(f"rules:apply:{pid}")
    await admin.on_confirm(cb)
    assert len(saved) == 1
    cfg = saved[0]
    assert cfg.event.name == active_event.name
    assert cfg.event.start == active_event.start_date and cfg.event.end == active_event.end_date
    assert cfg.sports[0].pace_max == "17:00"
    assert "v4" in cb.message.edits[-1]


# ------------------------------------------------------------------ /recheck
async def test_recheck_format(no_db, active_event, monkeypatch):
    async def _reeval(s, ev):
        return ReevalSummary(total=12, changed=[(101, True, False), (102, False, True)])

    monkeypatch.setattr(activities, "reevaluate_event", _reeval)
    msg = FakeMessage()
    await admin.cmd_recheck(msg)
    assert msg.last == "Đã chấm lại 12 hoạt động, 2 đổi trạng thái:\n101: hợp lệ→loại\n102: loại→hợp lệ"


async def test_recheck_no_change_and_truncation(no_db, active_event, monkeypatch):
    async def _none(s, ev):
        return ReevalSummary(total=5, changed=[])

    monkeypatch.setattr(activities, "reevaluate_event", _none)
    msg = FakeMessage()
    await admin.cmd_recheck(msg)
    assert "5 hoạt động, không có thay đổi" in msg.last

    async def _many(s, ev):
        return ReevalSummary(total=50, changed=[(i, True, False) for i in range(25)])

    monkeypatch.setattr(activities, "reevaluate_event", _many)
    msg = FakeMessage()
    await admin.cmd_recheck(msg)
    assert "… và 5 hoạt động khác" in msg.last and msg.last.count("→") == 20


async def test_recheck_without_active_event(no_db, monkeypatch):
    async def _none(s):
        return None

    monkeypatch.setattr(events, "get_active_event", _none)
    msg = FakeMessage()
    await admin.cmd_recheck(msg)
    assert "Chưa có giải" in msg.last


# ------------------------------------------------------------------ /exclude /include /adjust
@pytest.fixture
def override_calls(monkeypatch):
    calls = []

    async def _apply(s, ev, activity_id, action, *, value, note, by):
        calls.append(dict(event_id=ev.id, activity_id=activity_id, action=action, value=value, note=note, by=by))
        return SimpleNamespace(id=55)

    async def _get(s, activity_id):
        return None   # không hiển thị dòng hoạt động

    monkeypatch.setattr(activities, "apply_override", _apply)
    monkeypatch.setattr(activities, "get_activity", _get)
    return calls


async def test_exclude_calls_apply_override(no_db, active_event, override_calls):
    msg = FakeMessage()
    await admin.cmd_exclude(msg, cmd("123456 chạy bằng xe máy"))
    assert override_calls == [dict(event_id=7, activity_id=123456, action="exclude", value=None,
                                   note="chạy bằng xe máy", by=ADMIN_ID)]
    assert "123456" in msg.last and "chạy bằng xe máy" in msg.last and msg.last.startswith("✅")


async def test_include_and_adjust(no_db, active_event, override_calls):
    await admin.cmd_include(FakeMessage(), cmd("42"))
    assert override_calls[-1] == dict(event_id=7, activity_id=42, action="include", value=None, note=None, by=ADMIN_ID)
    await admin.cmd_adjust(FakeMessage(), cmd("42 5,5 đo lại bằng GPS"))
    assert override_calls[-1] == dict(event_id=7, activity_id=42, action="adjust_km", value=5.5,
                                      note="đo lại bằng GPS", by=ADMIN_ID)


async def test_override_usage_errors(no_db, active_event, override_calls):
    msg = FakeMessage()
    await admin.cmd_exclude(msg, cmd(None))
    assert "Cú pháp" in msg.last
    msg = FakeMessage()
    await admin.cmd_adjust(msg, cmd("42 abc"))
    assert "Km không hợp lệ" in msg.last
    assert override_calls == []


async def test_exclude_shows_activity_line(no_db, active_event, monkeypatch):
    async def _apply(s, ev, activity_id, action, *, value, note, by):
        return SimpleNamespace(id=1)

    act = SimpleNamespace(id=5, distance_m=5200.0, sport_type="Run", start_date_local=datetime(2027, 1, 5, 6, 12))
    ev = SimpleNamespace(valid=False, reasons=["ADMIN_EXCLUDED"], pace_s=365.0, sport_key="run", converted_km=0.0)

    async def _get(s, activity_id):
        return act

    async def _load(s, activity_id, event_id):
        return ev

    monkeypatch.setattr(activities, "apply_override", _apply)
    monkeypatch.setattr(activities, "get_activity", _get)
    monkeypatch.setattr(admin, "_load_evaluation", _load)
    msg = FakeMessage()
    await admin.cmd_exclude(msg, cmd("5"))
    assert "❌ 06:12 05/01 Run 5,20 km 6:05/km — ban tổ chức loại" in msg.last


# ------------------------------------------------------------------ /export
async def test_export_csv_has_header_and_bom(no_db, active_event, monkeypatch):
    async def _rows(s, ev):
        return [
            {"activity_id": 1, "athlete": "Nguyễn Văn A", "km": 5.2, "valid": True, "reasons": ""},
            {"activity_id": 2, "athlete": "Trần B", "km": 3.0, "valid": False, "reasons": "NO_GPS_MAP", "extra": None},
        ]

    monkeypatch.setattr(stats, "export_rows", _rows)
    msg = FakeMessage()
    await admin.cmd_export(msg)
    doc, caption = msg.documents[-1]
    assert doc.filename.startswith("export-7-") and doc.filename.endswith(".csv")
    assert doc.data.startswith("\ufeff".encode("utf-8"))
    text = doc.data.decode("utf-8-sig")
    lines = text.splitlines()
    assert lines[0] == "activity_id,athlete,km,valid,reasons,extra"
    assert lines[1] == "1,Nguyễn Văn A,5.2,True,,"
    assert lines[2] == "2,Trần B,3.0,False,NO_GPS_MAP,"
    assert "2 dòng" in caption


async def test_export_empty(no_db, active_event, monkeypatch):
    async def _rows(s, ev):
        return []

    monkeypatch.setattr(stats, "export_rows", _rows)
    msg = FakeMessage()
    await admin.cmd_export(msg)
    assert msg.documents == [] and "Chưa có dữ liệu" in msg.last


# ------------------------------------------------------------------ /settings
async def test_settings_set_invalid_key(no_db, monkeypatch):
    called = []

    async def _set(s, key, value):
        called.append((key, value))

    monkeypatch.setattr(events, "set_setting", _set)
    msg = FakeMessage()
    await admin.cmd_settings(msg, cmd("set khong_co 1"))
    assert called == [] and msg.last.startswith("❌") and "notify_rejected_dm" in msg.last


async def test_settings_set_valid_key_and_show(no_db, monkeypatch):
    store: dict[str, str] = {}

    async def _set(s, key, value):
        store[key] = value

    async def _get(s, key, default=None):
        return store.get(key, default)

    monkeypatch.setattr(events, "set_setting", _set)
    monkeypatch.setattr(events, "get_setting", _get)
    msg = FakeMessage()
    await admin.cmd_settings(msg, cmd("set auto_post_group on"))
    assert store == {"auto_post_group": "1"} and "auto_post_group = 1" in msg.last

    msg = FakeMessage()
    await admin.cmd_settings(msg, cmd("set notify_rejected_dm maybe"))
    assert msg.last.startswith("❌") and "notify_rejected_dm" not in store

    msg = FakeMessage()
    await admin.cmd_settings(msg, cmd(None))
    assert "auto_post_group = 1 (mặc định 0)" in msg.last and "notify_rejected_dm = 1" in msg.last


# ------------------------------------------------------------------ /athletes /broadcast /unlink
def _athletes():
    return [
        SimpleNamespace(name="An", strava_id=11, telegram_id=501),
        SimpleNamespace(name="Bình", strava_id=22, telegram_id=None),
        SimpleNamespace(name="Chi", strava_id=33, telegram_id=503),
    ]


async def test_athletes_list(no_db, monkeypatch):
    async def _list(s):
        return _athletes()

    monkeypatch.setattr(linking, "list_connected", _list)
    msg = FakeMessage()
    await admin.cmd_athletes(msg)
    assert msg.last.startswith("Đã kết nối: 3 athlete")
    assert "An (strava 11, tg 501)" in msg.last and "Bình (strava 22, tg -)" in msg.last


async def test_broadcast_confirm_then_send(no_db, monkeypatch, sent_messages):
    async def _list(s):
        return _athletes()

    async def _audit(*a, **k):
        return None

    monkeypatch.setattr(linking, "list_connected", _list)
    monkeypatch.setattr(events, "audit", _audit)
    msg = FakeMessage()
    await admin.cmd_broadcast(msg, cmd("Mai 6h chạy chung nhé"))
    assert "2 người" in msg.last and "Mai 6h chạy chung nhé" in msg.last
    data = msg.last_markup.inline_keyboard[0][0].callback_data
    assert data.startswith("confirm:apply:")
    assert sent_messages == []
    cb = FakeCallback(data)
    await admin.on_confirm(cb)
    assert sorted(sent_messages) == [(501, "Mai 6h chạy chung nhé"), (503, "Mai 6h chạy chung nhé")]
    assert "2/2" in cb.message.edits[-1]


async def test_unlink_athlete_confirm(no_db, monkeypatch):
    ath = SimpleNamespace(name="An", strava_id=11, telegram_id=501)

    async def _by_strava(s, sid):
        return ath if sid == 11 else None

    calls = []

    async def _unlink(s, client, athlete, *, delete_data=False):
        calls.append((athlete.strava_id, delete_data))

    monkeypatch.setattr(linking, "get_athlete_by_strava", _by_strava)
    monkeypatch.setattr(linking, "unlink", _unlink)
    monkeypatch.setattr(admin.deps, "get_client", lambda: object())

    msg = FakeMessage()
    await admin.cmd_unlink_athlete(msg, cmd("99"))
    assert "Không có athlete" in msg.last

    msg = FakeMessage()
    await admin.cmd_unlink_athlete(msg, cmd("11 delete"))
    assert "XOÁ" in msg.last
    data = msg.last_markup.inline_keyboard[0][0].callback_data
    cb = FakeCallback(data)
    await admin.on_confirm(cb)
    assert calls == [(11, True)] and "Đã ngắt kết nối An" in cb.message.edits[-1]


# ------------------------------------------------------------------ /backfill /webhook /post /reconcile /backup
async def test_backfill_all_sums_and_reports_errors(no_db, active_event, monkeypatch):
    async def _list(s):
        return _athletes()

    async def _backfill(s, client, ath, ev=None, *, notify=False):
        if ath.strava_id == 22:
            raise RuntimeError("token hỏng")
        return 3

    monkeypatch.setattr(linking, "list_connected", _list)
    monkeypatch.setattr(activities, "backfill_athlete", _backfill)
    monkeypatch.setattr(admin.deps, "get_client", lambda: object())
    msg = FakeMessage()
    await admin.cmd_backfill(msg, cmd("all"))
    assert "2/3 athlete: 6 hoạt động mới" in msg.last and "token hỏng" in msg.last


async def test_webhook_subscribe_uses_settings(no_db, monkeypatch):
    class FakeClient:
        last_rate_limit = None

        def __init__(self):
            self.created = []

        async def list_subscriptions(self):
            return [{"id": 5, "callback_url": "https://run.test/webhook"}]

        async def create_subscription(self, url, token):
            self.created.append((url, token))
            return {"id": 6}

        async def delete_subscription(self, sid):
            self.created.append(("del", sid))

    client = FakeClient()
    monkeypatch.setattr(admin.deps, "get_client", lambda: client)
    msg = FakeMessage()
    await admin.cmd_webhook(msg, cmd("list"))
    assert "#5 → https://run.test/webhook" in msg.last
    await admin.cmd_webhook(msg, cmd("subscribe"))
    assert client.created == [("https://run.test/webhook", "vts-verify")] and "#6" in msg.last
    await admin.cmd_webhook(msg, cmd("delete 5"))
    assert client.created[-1] == ("del", 5)


async def test_post_dry_and_lazy_import(no_db, monkeypatch):
    import sys
    import types

    calls = []

    async def run_daily_post(day=None, *, dry_run=False, send_group=None):
        calls.append((dry_run, send_group))
        return "BÀI NHÁP"

    mod = types.ModuleType("app.jobs.daily_post")
    mod.run_daily_post = run_daily_post
    monkeypatch.setitem(sys.modules, "app.jobs.daily_post", mod)

    async def _get(s, key, default=None):
        return None

    monkeypatch.setattr(events, "get_setting", _get)

    msg = FakeMessage()
    await admin.cmd_post(msg, cmd("dry"))
    assert calls == [(True, None)] and "BÀI NHÁP" in msg.last
    await admin.cmd_post(msg, cmd(None))
    assert calls[-1] == (False, None) and "Đã soạn và gửi" in msg.last


async def test_unexpected_error_is_reported(no_db, monkeypatch):
    async def _boom(s):
        raise RuntimeError("DB cháy")

    monkeypatch.setattr(events, "list_events", _boom)
    msg = FakeMessage()
    await admin.cmd_event(msg, cmd(None))
    assert msg.last == "⚠️ Lỗi: DB cháy"


async def test_status_degrades_gracefully(no_db, monkeypatch):
    """session None -> các phần cần DB báo 'không kiểm tra được' nhưng lệnh vẫn trả lời."""
    class FakeClient:
        last_rate_limit = {"limit_15m": 200, "usage_15m": 7, "limit_day": 2000, "usage_day": 70}

        async def list_subscriptions(self):
            raise RuntimeError("mạng lỗi")

    monkeypatch.setattr(admin.deps, "get_client", lambda: FakeClient())
    msg = FakeMessage()
    await admin.cmd_status(msg)
    text = msg.last
    assert "không kiểm tra được" in text
    assert "15 phút 7/200" in text and "public_base_url: https://run.test" in text


def test_split_text_respects_limit():
    text = "\n".join(f"dòng {i}" for i in range(2000))
    chunks = admin._split_text(text, 500)
    assert all(len(c) <= 500 for c in chunks) and "\n".join(chunks) == text
