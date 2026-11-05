"""Công cụ dòng lệnh quản trị: python -m app.cli <lệnh> [tham số]

  init-db                                   tạo bảng
  gen-key                                   in khoá Fernet cho TOKEN_ENCRYPTION_KEY
  webhook list | subscribe | delete <id>    quản lý webhook subscription Strava
  event new NAME START END [--draft]        tạo giải (YYYY-MM-DD); mặc định active
  event list | activate ID | close ID
  rules show | export [FILE] | import FILE | set PATH VALUE     (giải đang active)
  recheck                                   chấm lại toàn bộ hoạt động của giải active
  reconcile                                 đối soát với Strava ngay
  post [--dry-run] [--date YYYY-MM-DD]      soạn/gửi bài sáng
  backup                                    sao lưu DB SQLite
  athletes                                  danh sách runner đã kết nối
  backfill <strava_id|all>                  lấy lại hoạt động trong cửa sổ giải
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from contextlib import suppress
from datetime import date
from pathlib import Path
from typing import Awaitable, Callable

from app.config import get_settings
from app.db.base import configure_engine, dispose, init_db, session_scope
from app.deps import set_client
from app.strava.client import StravaClient


# ----------------------------------------------------------------- khung chạy
def _run(fn: Callable[[StravaClient], Awaitable[int | None]]) -> int:
    """Khởi tạo DB + client, chạy coroutine, đóng gọn. Trả exit code."""
    async def main() -> int:
        configure_engine()
        await init_db()
        client = StravaClient()
        set_client(client)
        try:
            return int(await fn(client) or 0)
        finally:
            with suppress(Exception):
                await client.aclose()
            set_client(None)
            await dispose()
    return asyncio.run(main())


def _die(msg: str) -> int:
    print(f"✖ {msg}", file=sys.stderr)
    return 1


def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise SystemExit(f"✖ Ngày không hợp lệ: {s!r} (định dạng YYYY-MM-DD)")


async def _need_active_event(session):
    from app.services import events
    ev = await events.get_active_event(session)
    if ev is None:
        raise SystemExit("✖ Chưa có giải nào đang active. Tạo bằng: python -m app.cli event new NAME START END")
    return ev


def _event_line(e) -> str:
    return f"#{e.id:<3} {e.status:<7} {e.start_date} → {e.end_date}  {e.name}"


# ----------------------------------------------------------------- lệnh
def cmd_init_db(_args) -> int:
    async def go(_c):
        print(f"✔ Đã tạo bảng trong {get_settings().database_url}")
    return _run(go)


def cmd_gen_key(_args) -> int:
    from app.security import generate_key
    print(generate_key())
    return 0


def cmd_webhook(args) -> int:
    async def go(client: StravaClient):
        s = get_settings()
        if args.action == "list":
            subs = await client.list_subscriptions()
            if not subs:
                print("(chưa có subscription nào)")
            for sub in subs:
                print(f"#{sub.get('id')}  {sub.get('callback_url')}  (tạo {sub.get('created_at', '?')})")
        elif args.action == "subscribe":
            sub = await client.create_subscription(s.webhook_url, s.strava_webhook_verify_token)
            print(f"✔ Đã đăng ký webhook #{sub.get('id')} -> {s.webhook_url}")
        elif args.action == "delete":
            if args.id is None:
                return _die("Thiếu id: webhook delete <id>")
            await client.delete_subscription(int(args.id))
            print(f"✔ Đã xoá subscription #{args.id}")
    return _run(go)


def cmd_event(args) -> int:
    from app.services import events

    async def go(_c):
        async with session_scope() as s:
            if args.action == "new":
                start, end = _parse_date(args.start), _parse_date(args.end)
                if end < start:
                    return _die("Ngày kết thúc phải >= ngày bắt đầu")
                ev = await events.create_event(s, args.name, start, end, by=None, activate=not args.draft)
                print(f"✔ Đã tạo giải {_event_line(ev)}")
            elif args.action == "list":
                evs = await events.list_events(s)
                if not evs:
                    print("(chưa có giải nào)")
                for e in evs:
                    print(_event_line(e))
            elif args.action in ("activate", "close"):
                ev = await events.get_event(s, int(args.id))
                if ev is None:
                    return _die(f"Không có giải #{args.id}")
                status = "active" if args.action == "activate" else "closed"
                await events.set_event_status(s, ev, status, by=None)
                print(f"✔ Giải #{ev.id} '{ev.name}' -> {status}")
    return _run(go)


def cmd_rules(args) -> int:
    from app.rules.schema import RuleSetConfig
    from app.services import activities, events

    async def reeval(s, ev):
        summary = await activities.reevaluate_event(s, ev)
        print(f"↻ Đã chấm lại {summary.total} hoạt động, {len(summary.changed)} hoạt động đổi trạng thái.")

    async def go(_c):
        async with session_scope() as s:
            ev = await _need_active_event(s)
            cfg = await events.get_rules(s, ev)
            ver = await events.get_rules_version(s, ev)
            if args.action == "show":
                print(f"# Giải #{ev.id} '{ev.name}' — luật version {ver}")
                print(cfg.to_yaml())
            elif args.action == "export":
                text = cfg.to_yaml()
                if args.file:
                    Path(args.file).write_text(text, encoding="utf-8")
                    print(f"✔ Đã ghi luật (version {ver}) ra {args.file}")
                else:
                    print(text)
            elif args.action == "import":
                try:
                    new_cfg = RuleSetConfig.from_yaml(Path(args.file).read_text(encoding="utf-8"))
                except Exception as e:  # noqa: BLE001
                    return _die(f"File luật không hợp lệ: {e}")
                rs = await events.update_rules(s, ev, new_cfg, by=None, note=f"import {Path(args.file).name}")
                print(f"✔ Đã lưu luật version {rs.version} cho giải '{ev.name}'")
                await reeval(s, ev)
            elif args.action == "set":
                try:
                    rs, old, new = await events.set_rule_value(s, ev, args.path, args.value, by=None)
                except (KeyError, ValueError) as e:
                    return _die(f"Không đặt được {args.path}: {e}")
                print(f"✔ {args.path}: {old!r} -> {new!r} (luật version {rs.version})")
                await reeval(s, ev)
    return _run(go)


def cmd_recheck(_args) -> int:
    from app.services import activities

    async def go(_c):
        async with session_scope() as s:
            ev = await _need_active_event(s)
            summary = await activities.reevaluate_event(s, ev)
            print(f"✔ Giải '{ev.name}': chấm lại {summary.total} hoạt động, {len(summary.changed)} đổi trạng thái.")
            for aid, was, now in summary.changed[:30]:
                print(f"   #{aid}: {'hợp lệ' if was else 'loại'} -> {'hợp lệ' if now else 'loại'}")
            if len(summary.changed) > 30:
                print(f"   … và {len(summary.changed) - 30} hoạt động khác")
    return _run(go)


def cmd_reconcile(_args) -> int:
    from app.jobs.reconcile import reconcile_all

    async def go(client: StravaClient):
        r = await reconcile_all(client)
        print(f"✔ Đối soát {r['athletes']} VĐV: +{r['new']} hoạt động mới, {r['deleted']} đã xoá, "
              f"{r['tokens_refreshed']} token refresh.")
        for e in r["errors"]:
            print(f"   ⚠ {e}")
        return 1 if r["errors"] else 0
    return _run(go)


def cmd_post(args) -> int:
    from app.jobs.daily_post import run_daily_post

    async def go(_c):
        day = _parse_date(args.date) if args.date else None
        text = await run_daily_post(day, dry_run=args.dry_run)
        if not text:
            print("(không có bài: ngoài thời gian giải hoặc chưa có giải)")
            return 0
        print(text)
        if not args.dry_run:
            print("\n✔ Đã lưu và gửi bài qua Telegram.", file=sys.stderr)
    return _run(go)


def cmd_backup(_args) -> int:
    from app.jobs.backup import run_backup

    async def go(_c):
        path = await asyncio.to_thread(run_backup)
        print(f"✔ Đã sao lưu: {path}" if path else "(DB không phải SQLite file, không backup)")
    return _run(go)


def cmd_athletes(_args) -> int:
    from app.services import linking

    async def go(_c):
        async with session_scope() as s:
            rows = await linking.list_connected(s)
        if not rows:
            print("(chưa có runner nào kết nối)")
            return 0
        print(f"{'strava_id':>10}  {'telegram':>11}  {'club':<5} tên")
        for a in rows:
            club = {True: "✔", False: "✖", None: "?"}[a.club_member]
            print(f"{a.strava_id:>10}  {str(a.telegram_id or '-'):>11}  {club:<5} {a.name}")
        print(f"— {len(rows)} runner")
    return _run(go)


def cmd_backfill(args) -> int:
    from app.services import activities, events, linking

    async def go(client: StravaClient):
        total = 0
        async with session_scope() as s:
            ev = await events.get_active_event(s)
            if args.target == "all":
                targets = await linking.list_connected(s)
            else:
                a = await linking.get_athlete_by_strava(s, int(args.target))
                if a is None:
                    return _die(f"Không có runner strava_id={args.target}")
                targets = [a]
        for a in targets:
            try:
                async with session_scope() as s:
                    athlete = await s.merge(a)
                    n = await activities.backfill_athlete(s, client, athlete, ev, notify=False)
                total += n
                print(f"  {a.name}: +{n} hoạt động")
            except Exception as e:  # noqa: BLE001
                print(f"  ⚠ {a.name}: {type(e).__name__}: {e}")
        print(f"✔ Backfill {len(targets)} runner, +{total} hoạt động mới"
              + (f" (giải '{ev.name}')" if ev else " (không có giải active: chấm theo event chứa ngày)"))
    return _run(go)


# ----------------------------------------------------------------- argparse
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.cli", description="Quản trị bot VTS Running Club",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="tạo bảng").set_defaults(fn=cmd_init_db)
    sub.add_parser("gen-key", help="in khoá Fernet").set_defaults(fn=cmd_gen_key)

    w = sub.add_parser("webhook", help="webhook Strava")
    w.add_argument("action", choices=["list", "subscribe", "delete"])
    w.add_argument("id", nargs="?")
    w.set_defaults(fn=cmd_webhook)

    e = sub.add_parser("event", help="quản lý giải")
    es = e.add_subparsers(dest="action", required=True)
    en = es.add_parser("new")
    en.add_argument("name")
    en.add_argument("start")
    en.add_argument("end")
    en.add_argument("--draft", action="store_true", help="tạo ở trạng thái draft, chưa active")
    es.add_parser("list")
    es.add_parser("activate").add_argument("id")
    es.add_parser("close").add_argument("id")
    e.set_defaults(fn=cmd_event)

    r = sub.add_parser("rules", help="luật của giải active")
    rs = r.add_subparsers(dest="action", required=True)
    rs.add_parser("show")
    rs.add_parser("export").add_argument("file", nargs="?")
    rs.add_parser("import").add_argument("file")
    rset = rs.add_parser("set")
    rset.add_argument("path", help="vd sports.run.pace_max")
    rset.add_argument("value")
    r.set_defaults(fn=cmd_rules)

    sub.add_parser("recheck", help="chấm lại toàn bộ").set_defaults(fn=cmd_recheck)
    sub.add_parser("reconcile", help="đối soát Strava ngay").set_defaults(fn=cmd_reconcile)

    po = sub.add_parser("post", help="soạn/gửi bài sáng")
    po.add_argument("--dry-run", action="store_true", help="chỉ in, không lưu/không gửi")
    po.add_argument("--date", help="ngày đăng YYYY-MM-DD (mặc định hôm nay)")
    po.set_defaults(fn=cmd_post)

    sub.add_parser("backup", help="sao lưu DB").set_defaults(fn=cmd_backup)
    sub.add_parser("athletes", help="runner đã kết nối").set_defaults(fn=cmd_athletes)

    bf = sub.add_parser("backfill", help="lấy lại hoạt động")
    bf.add_argument("target", help="strava_id hoặc 'all'")
    bf.set_defaults(fn=cmd_backfill)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            return 1
        raise
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
