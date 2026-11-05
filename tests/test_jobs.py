"""Kiểm tra app.jobs: daily_post, backup, scheduler, reconcile (services/strava được monkeypatch)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import reset_settings
from app.db.models import Activity, Athlete, Post, utcnow
from app.rules.engine import load_default_rules
from app.services import activities, events, linking, stats
from app.services.stats import RankRow
from app.strava import tokens as strava_tokens
from app.strava.client import StravaAuthError, StravaRateLimited

ADMIN_ID, GROUP_ID = 1000, -100500
START, END = date(2026, 9, 15), date(2026, 10, 14)


@dataclass
class FakeEvent:
    id: int = 1
    name: str = "Giải chạy thử nghiệm"
    start_date: date = START
    end_date: date = END
    status: str = "active"
    timezone: str = "Asia/Ho_Chi_Minh"


def rank(aid: int, name: str, km: float, days: int = 1) -> RankRow:
    return RankRow(athlete_id=aid, name=name, km=km, days=days, activities=days)


@pytest.fixture
def fake_services(monkeypatch):
    """events/stats giả cho daily_post. Trả về dict có thể chỉnh trong test."""
    state = {
        "active": FakeEvent(),
        "all": [FakeEvent()],
        "auto_post_group": None,       # None -> dùng default truyền vào
        "ranking": [rank(1, "Nguyễn Văn An", 120.5, 20), rank(2, "Trần Thị Bình", 100.0, 18),
                    rank(3, "Lê Cường", 90.25, 15), rank(4, "Phạm Dũng", 10.0, 2)],
        "daily": [rank(3, "Lê Cường", 12.3), rank(2, "Trần Thị Bình", 8.0), rank(5, "Hoàng Em", 5.5),
                  rank(1, "Nguyễn Văn An", 3.0), rank(4, "Phạm Dũng", 0.0)],
        "calls": [],
    }

    async def get_active_event(s):
        return state["active"]

    async def list_events(s):
        return list(state["all"])

    async def get_rules(s, event):
        return load_default_rules()

    async def setting_bool(s, key, default):
        state["calls"].append(("setting_bool", key, default))
        return default if state["auto_post_group"] is None else state["auto_post_group"]

    async def ranking(s, event, *, until=None, limit=None):
        state["calls"].append(("ranking", until))
        return list(state["ranking"])

    async def daily(s, event, day):
        state["calls"].append(("daily", day))
        return list(state["daily"])

    monkeypatch.setattr(events, "get_active_event", get_active_event)
    monkeypatch.setattr(events, "list_events", list_events)
    monkeypatch.setattr(events, "get_rules", get_rules)
    monkeypatch.setattr(events, "setting_bool", setting_bool)
    monkeypatch.setattr(stats, "ranking", ranking)
    monkeypatch.setattr(stats, "daily", daily)
    return state


async def count_posts(db) -> list[Post]:
    async with db() as s:
        return list((await s.execute(select(Post))).scalars().all())


# ============================================================================ daily_post
async def test_dry_run_returns_text_without_sending_or_saving(db, sent_messages, fake_services):
    from app.jobs.daily_post import run_daily_post
    day = date(2026, 10, 1)
    text = await run_daily_post(day, dry_run=True)
    assert "🥇 Nguyễn Văn An" in text and "Hoàng Em" in text and text.endswith("#VTSRunningClub")
    assert "4 anh chị em" in text                              # Phạm Dũng 0 km không tính là active
    assert sent_messages == []
    assert await count_posts(db) == []
    assert ("ranking", date(2026, 9, 30)) in fake_services["calls"]
    assert ("daily", date(2026, 9, 30)) in fake_services["calls"]


async def test_real_run_sends_two_admin_messages_not_group(db, sent_messages, fake_services, _settings):
    from app.jobs.daily_post import run_daily_post
    assert _settings.auto_post_group is False
    day = date(2026, 10, 1)
    text = await run_daily_post(day)
    assert text
    assert [cid for cid, _ in sent_messages] == [ADMIN_ID, ADMIN_ID]
    assert sent_messages[0][1] == "📋 Bài đăng 01/10 (nguồn: template)"
    assert sent_messages[1][1] == text
    assert ("setting_bool", "auto_post_group", False) in fake_services["calls"]
    posts = await count_posts(db)
    assert len(posts) == 1
    assert posts[0].post_date == day and posts[0].text == text
    assert posts[0].source == "template" and posts[0].sent_to == "admins" and posts[0].event_id == 1


async def test_send_group_true_sends_group(db, sent_messages, fake_services):
    from app.jobs.daily_post import run_daily_post
    text = await run_daily_post(date(2026, 10, 1), send_group=True)
    assert (GROUP_ID, text) in sent_messages
    assert [cid for cid, _ in sent_messages] == [ADMIN_ID, ADMIN_ID, GROUP_ID]
    assert (await count_posts(db))[0].sent_to == "admins,group"
    assert not any(c[0] == "setting_bool" for c in fake_services["calls"])


async def test_runtime_setting_enables_group(db, sent_messages, fake_services):
    from app.jobs.daily_post import run_daily_post
    fake_services["auto_post_group"] = True
    text = await run_daily_post(date(2026, 10, 1))
    assert (GROUP_ID, text) in sent_messages


async def test_send_group_false_overrides_setting(db, sent_messages, fake_services):
    from app.jobs.daily_post import run_daily_post
    fake_services["auto_post_group"] = True
    await run_daily_post(date(2026, 10, 1), send_group=False)
    assert all(cid == ADMIN_ID for cid, _ in sent_messages)


@pytest.mark.parametrize("day", [START - timedelta(days=3), START])
async def test_day_on_or_before_start_returns_empty(db, sent_messages, fake_services, day):
    from app.jobs.daily_post import run_daily_post
    assert await run_daily_post(day) == ""
    assert sent_messages == [] and await count_posts(db) == []


async def test_day_after_end_window_returns_empty_but_finale_day_posts(db, sent_messages, fake_services):
    from app.jobs.daily_post import run_daily_post
    assert await run_daily_post(END + timedelta(days=2), dry_run=True) == ""
    finale = await run_daily_post(END + timedelta(days=1), dry_run=True)
    assert finale.startswith("🏁 KHÉP LẠI")


async def test_no_active_event_falls_back_to_recent_event(db, sent_messages, fake_services):
    from app.jobs.daily_post import run_daily_post
    fake_services["active"] = None
    old = FakeEvent(id=7, name="Giải cũ", start_date=date(2026, 1, 1), end_date=date(2026, 1, 31), status="closed")
    recent = FakeEvent(id=8, name="Giải vừa xong", start_date=START, end_date=END, status="closed")
    fake_services["all"] = [recent, old]
    text = await run_daily_post(END + timedelta(days=1), dry_run=True)
    assert text.startswith("🏁 KHÉP LẠI GIẢI CHẠY VTS RUNNING CLUB")   # tên lấy từ rules.event.name
    fake_services["all"] = [old]
    assert await run_daily_post(END + timedelta(days=1), dry_run=True) == ""


async def test_history_passed_from_recent_posts(db, sent_messages, fake_services, monkeypatch):
    from app import composer
    from app.jobs import daily_post
    async with db() as s:
        for i in range(7):
            s.add(Post(event_id=1, post_date=date(2026, 9, 20) + timedelta(days=i), text=f"bài {i}", source="template"))
        await s.commit()
    seen = {}

    def fake_rewrite(ctx, draft, history, cfg):
        seen["history"] = history
        seen["ctx"] = ctx
        return draft, "template"

    monkeypatch.setattr(composer, "rewrite_with_llm", fake_rewrite)
    await daily_post.run_daily_post(date(2026, 10, 1), dry_run=True)
    assert seen["history"] == [f"bài {i}" for i in range(2, 7)]
    assert seen["ctx"]["rules_summary"].startswith("Chạy/đi bộ: pace 4:00–17:00/km, tối thiểu 1 km;")
    assert seen["ctx"]["streak_days"] == 25 and seen["ctx"]["female_target_km"] == 45


def test_rules_summary_text():
    from app.jobs.daily_post import rules_summary
    s = rules_summary(load_default_rules())
    assert s == ("Chạy/đi bộ: pace 4:00–17:00/km, tối thiểu 1 km; "
                 "Đạp xe ×0,25: pace 1:30–10:00/km, tối thiểu 4 km; "
                 "Bơi ×4: pace 1:15–4:00/100m, tối thiểu 0,1 km.")


# ============================================================================ backup
def _make_sqlite(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (x INTEGER)")
    con.execute("INSERT INTO t VALUES (42)")
    con.commit()
    con.close()


def test_run_backup_creates_file_and_prunes(tmp_path, monkeypatch):
    from app.jobs.backup import run_backup
    src = tmp_path / "vts.db"
    _make_sqlite(src)
    bdir = tmp_path / "bk"
    bdir.mkdir()
    for name in ("vts-20260101-0000.db", "vts-20260102-0000.db", "vts-20260103-0000.db"):
        (bdir / name).write_bytes(b"old")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{src}")
    monkeypatch.setenv("BACKUP_DIR", str(bdir))
    monkeypatch.setenv("BACKUP_KEEP", "2")
    reset_settings()

    out = run_backup()
    assert out is not None
    dst = bdir / out.split("/")[-1]
    assert dst.exists() and dst.name.startswith("vts-") and dst.name.endswith(".db")
    con = sqlite3.connect(dst)
    assert con.execute("SELECT x FROM t").fetchone() == (42,)
    con.close()
    remaining = sorted(p.name for p in bdir.glob("*.db"))
    assert remaining == sorted(["vts-20260103-0000.db", dst.name])
    assert not list(bdir.glob("*.part"))


def test_run_backup_non_sqlite_returns_none(monkeypatch):
    from app.jobs.backup import run_backup, sqlite_file_path
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    reset_settings()
    assert run_backup() is None
    assert sqlite_file_path("sqlite+aiosqlite://") is None
    assert sqlite_file_path("sqlite+aiosqlite:///:memory:") is None
    assert sqlite_file_path("sqlite+aiosqlite:///./data/vts.db") == "./data/vts.db"
    assert sqlite_file_path("sqlite+aiosqlite:////app/data/vts.db?mode=rw") == "/app/data/vts.db"


async def test_run_backup_async(tmp_path, monkeypatch):
    from app.jobs.backup import run_backup_async
    src = tmp_path / "vts.db"
    _make_sqlite(src)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{src}")
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "bk"))
    reset_settings()
    out = await run_backup_async()
    assert out and out.endswith(".db")


# ============================================================================ scheduler
async def test_start_scheduler_registers_five_jobs_and_shuts_down():
    from app.jobs import scheduler as sch
    s = sch.start_scheduler()
    try:
        assert s.running and sch.scheduler is s
        ids = {j.id for j in s.get_jobs()}
        assert ids == {"reconcile", "daily_post", "backup", "purge_raw", "refresh_tokens"}
        assert len(s.get_jobs()) == 5
        daily = s.get_job("daily_post")
        assert "hour='6'" in str(daily.trigger) and "minute='30'" in str(daily.trigger)
        assert s.get_job("reconcile").max_instances == 1 and s.get_job("reconcile").coalesce is True
        assert sch.start_scheduler() is s                       # gọi lại không tạo scheduler mới
        status = sch.jobs_status()
        assert {x["id"] for x in status} == ids
    finally:
        sch.shutdown_scheduler()
    assert sch.scheduler is None
    sch.shutdown_scheduler()                                    # idempotent


async def test_job_wrapper_notifies_admin_on_error_but_not_rate_limit(sent_messages):
    from app.jobs.scheduler import _wrap

    async def boom():
        raise ValueError("hỏng")

    async def limited():
        raise StravaRateLimited("429", retry_after=30)

    await _wrap("test", boom)()
    assert len(sent_messages) == 1 and "Job test lỗi" in sent_messages[0][1] and "hỏng" in sent_messages[0][1]
    await _wrap("test", limited)()
    assert len(sent_messages) == 1


# ============================================================================ reconcile
class FakeClient:
    def __init__(self, data: dict[str, list[dict]], raise_for: dict | None = None):
        self.data, self.raise_for, self.calls = data, raise_for or {}, []

    async def list_all_activities(self, access_token, *, after=None, before=None, max_pages=20):
        self.calls.append((access_token, after))
        if access_token in self.raise_for:
            raise self.raise_for[access_token]
        return list(self.data.get(access_token, []))


@pytest.fixture
async def reconcile_env(db, monkeypatch):
    """2 athlete thật trong DB + 4 Activity của athlete 1; services/token được giả."""
    now = utcnow()
    async with db() as s:
        a1 = Athlete(strava_id=101, display_name="An")
        a2 = Athlete(strava_id=102, display_name="Bình")
        s.add_all([a1, a2])
        await s.flush()

        def act(aid, athlete_id, start, deleted=None):
            return Activity(id=aid, athlete_id=athlete_id, sport_type="Run", start_date=start, start_date_local=start,
                            distance_m=5000, moving_s=1800, elapsed_s=1900, deleted_at=deleted)

        s.add_all([
            act(11, a1.id, now - timedelta(hours=5)),                          # còn trên Strava
            act(12, a1.id, now - timedelta(hours=30)),                         # không còn -> xoá
            act(13, a1.id, now - timedelta(days=10)),                          # ngoài cửa sổ -> bỏ qua
            act(14, a1.id, now - timedelta(hours=2), deleted=now),             # đã đánh dấu xoá -> bỏ qua
        ])
        await s.commit()
        ids = (a1.id, a2.id)

    rec = {"ingested": [], "deleted": [], "refresh_within": None, "auth_fail": set()}

    async def list_connected(s):
        return list((await s.execute(select(Athlete).order_by(Athlete.id))).scalars().all())

    async def get_valid_access_token(s, client, athlete):
        if athlete.strava_id in rec["auth_fail"]:
            raise StravaAuthError("401 revoked")
        return f"tok-{athlete.strava_id}"

    async def ingest_from_json(s, athlete, data, *, notify=True):
        assert notify is False
        rec["ingested"].append((athlete.strava_id, data["id"]))
        return SimpleNamespace(created=data["id"] not in (11, 12, 13, 14), changed=False)

    async def mark_deleted(s, aid):
        rec["deleted"].append(aid)
        return None

    async def refresh_expiring(s, client, within_s=1800):
        rec["refresh_within"] = within_s
        return 3

    monkeypatch.setattr(linking, "list_connected", list_connected)
    monkeypatch.setattr(strava_tokens, "get_valid_access_token", get_valid_access_token)
    monkeypatch.setattr(strava_tokens, "refresh_expiring", refresh_expiring)
    monkeypatch.setattr(activities, "ingest_from_json", ingest_from_json)
    monkeypatch.setattr(activities, "mark_deleted", mark_deleted)
    rec["ids"] = ids
    return rec


async def test_reconcile_counts_new_and_deleted(db, sent_messages, reconcile_env, _settings):
    from app.jobs.reconcile import reconcile_all
    client = FakeClient({"tok-101": [{"id": 11}, {"id": 15}], "tok-102": [{"id": 21}]})
    r = await reconcile_all(client)
    assert r == {"athletes": 2, "new": 2, "deleted": 1, "errors": [], "tokens_refreshed": 3}
    assert reconcile_env["deleted"] == [12]
    assert sorted(reconcile_env["ingested"]) == [(101, 11), (101, 15), (102, 21)]
    assert reconcile_env["refresh_within"] == 7200
    assert sent_messages == []
    expected_after = (datetime.utcnow() - timedelta(days=_settings.reconcile_lookback_days)).timestamp()
    assert all(abs(after - expected_after) < 120 for _, after in client.calls)


async def test_reconcile_auth_error_deactivates_and_continues(db, sent_messages, reconcile_env):
    from app.jobs.reconcile import reconcile_all
    reconcile_env["auth_fail"].add(101)
    client = FakeClient({"tok-102": [{"id": 21}]})
    r = await reconcile_all(client)
    assert r["athletes"] == 2 and r["new"] == 1 and r["deleted"] == 0 and r["tokens_refreshed"] == 3
    assert len(r["errors"]) == 1 and "An" in r["errors"][0]
    async with db() as s:
        a1 = (await s.execute(select(Athlete).where(Athlete.strava_id == 101))).scalar_one()
        a2 = (await s.execute(select(Athlete).where(Athlete.strava_id == 102))).scalar_one()
    assert a1.active is False and a2.active is True
    assert reconcile_env["deleted"] == []                      # không xoá nhầm khi chưa lấy được danh sách
    assert [cid for cid, _ in sent_messages] == [ADMIN_ID, ADMIN_ID]   # báo ngay + tóm tắt cuối
    assert "Đối soát Strava có 1 lỗi" in sent_messages[-1][1]


async def test_reconcile_rate_limit_stops_loop(db, sent_messages, reconcile_env):
    from app.jobs.reconcile import reconcile_all
    client = FakeClient({}, raise_for={"tok-101": StravaRateLimited("429", retry_after=600)})
    r = await reconcile_all(client)
    assert r["athletes"] == 2 and r["new"] == 0 and r["deleted"] == 0
    assert len(r["errors"]) == 1 and "600" in r["errors"][0]
    assert len(client.calls) == 1                              # dừng ngay, không gọi athlete 2
    assert reconcile_env["ingested"] == []
    assert r["tokens_refreshed"] == 3


async def test_reconcile_other_error_continues(db, sent_messages, reconcile_env):
    from app.jobs.reconcile import reconcile_all
    client = FakeClient({"tok-102": [{"id": 21}]}, raise_for={"tok-101": RuntimeError("mạng lỗi")})
    r = await reconcile_all(client)
    assert r["new"] == 1 and len(r["errors"]) == 1 and "mạng lỗi" in r["errors"][0]
    assert reconcile_env["deleted"] == []


# ============================================================================ cli / main
def test_cli_parser_and_gen_key(capsys):
    from app.cli import build_parser, main
    args = build_parser().parse_args(["post", "--dry-run", "--date", "2026-10-01"])
    assert args.cmd == "post" and args.dry_run and args.date == "2026-10-01"
    args = build_parser().parse_args(["event", "new", "Giải A", "2026-09-15", "2026-10-14", "--draft"])
    assert args.action == "new" and args.draft and args.name == "Giải A"
    args = build_parser().parse_args(["rules", "set", "sports.run.pace_max", "18:00"])
    assert args.path == "sports.run.pace_max" and args.value == "18:00"
    assert build_parser().parse_args(["backfill", "all"]).target == "all"
    assert main(["gen-key"]) == 0
    key = capsys.readouterr().out.strip()
    assert len(key) == 44


def test_create_app_includes_api_router():
    pytest.importorskip("app.api")
    from app.main import create_app
    app = create_app()
    paths = set(app.openapi()["paths"])            # FastAPI mới gói router con trong _IncludedRouter
    assert "/webhook" in paths and "/health" in paths and "/auth/callback" in paths
