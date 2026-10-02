#!/usr/bin/env python3
"""VTS Running Club — bot soạn bài đăng Strava hằng ngày.

Luồng hoạt động:
  23:20 + 23:50  `snapshot`  userbot gửi /ranking + /today cho @tdht_bot, lưu bảng tổng
                     và thống kê km trong ngày (đã quy đổi theo loại vận động).
                     Hai lượt dự phòng nhau vì GitHub Actions hay chạy trễ; lượt đúng giờ
                     muộn nhất được giữ. Chạy sau 0h thì /today đã sang ngày mới nên bot
                     KHÔNG dùng nó: snapshot được gán cho ngày hôm trước, gắn cờ "late"
                     và bài đăng sẽ ước tính km ngày bằng chênh lệch bảng tổng.
  06:47  `post`      dùng snapshot đêm qua để lấy top 3 bảng tổng và top 3 km hôm qua,
                     soạn bài, gửi bản nháp qua Telegram cho admin (kèm cảnh báo nếu
                     dữ liệu bị suy giảm).

Lệnh:
  python main.py login            Đăng nhập userbot Telegram (1 lần, cần OTP) -> in ra TG_SESSION_STRING
  python main.py chat-id          Lấy chat_id của bạn với bot nhận bài
  python main.py snapshot         Chụp bảng xếp hạng hiện tại
  python main.py post [--dry-run] Soạn bài hôm nay (dry-run: chỉ in ra, không gửi)
  python main.py parse FILE       Thử bóc tách một đoạn text /ranking đã lưu
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

import composer

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
# Snapshot chạy trước giờ này (giờ VN) được coi là chạy trễ của đêm hôm trước.
LATE_UNTIL_HOUR = int(os.getenv("SNAPSHOT_LATE_UNTIL_HOUR") or 6)
DATA = BASE / "data"
SNAP_DIR, POST_DIR, RAW_DIR = DATA / "snapshots", DATA / "posts", DATA / "raw"

ROW_RE = re.compile(r"^\s*\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*(\d+(?:[.,]\d+)?)\s*\|\s*(\d+)\s*\|\s*$")
TODAY_ROW_RE = re.compile(
    r"^\s*\|\s*(.+?)\s*\|\s*([^|]+?)\s*\|\s*(\d+(?:[.,]\d+)?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$"
)


# ----------------------------------------------------------------- cấu hình
def env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(name) or default  # biến rỗng (vd. GitHub var chưa đặt) coi như chưa cấu hình
    if required and not val:
        sys.exit(f"Thiếu biến {name} trong file .env")
    return val or ""


def load_cfg() -> dict:
    d = lambda s: date.fromisoformat(s)  # noqa: E731
    csv = lambda s: [x.strip().lower() for x in s.split(",") if x.strip()]  # noqa: E731
    return {
        "start": d(env("EVENT_START", "2026-09-15")),
        "end": d(env("EVENT_END", "2026-10-14")),
        "anniv": d(env("ANNIVERSARY", "2026-10-15")),
        "founded_year": int(env("FOUNDED_YEAR", "2018")),
        "target_km": float(env("TARGET_KM", "45")),
        "hashtags": env("HASHTAGS", "#VTSRunningClub #8NamVTS"),
        "show_daily_km": env("SHOW_DAILY_KM", "1") == "1",
        "daily_ride_factor": float(env("DAILY_RIDE_FACTOR", "0.25")),
        "daily_swim_factor": float(env("DAILY_SWIM_FACTOR", "4")),
        "daily_ride_types": csv(env("DAILY_RIDE_TYPES", "ride,bike,cycle,ebike,virtualride")),
        "daily_swim_types": csv(env("DAILY_SWIM_TYPES", "swim")),
        "llm_provider": env("LLM_PROVIDER", "none").lower(),
        "anthropic_key": env("ANTHROPIC_API_KEY"),
        "anthropic_model": env("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
        "gemini_key": env("GEMINI_API_KEY"),
        "gemini_model": env("GEMINI_MODEL", "gemini-2.5-flash"),
    }


def today_vn() -> date:
    return datetime.now(TZ).date()


# ----------------------------------------------------------------- bóc tách /ranking
def parse_ranking(text: str) -> list[dict]:
    """Đọc bảng ASCII của /ranking: | ST | Nick | Tổng | Days |"""
    rows: dict[str, dict] = {}
    for line in text.splitlines():
        m = ROW_RE.match(line)
        if not m:
            continue
        nick = " ".join(m.group(2).split())
        rows.setdefault(nick, {
            "rank": int(m.group(1)),
            "nick": nick,
            "total": float(m.group(3).replace(",", ".")),
            "days": int(m.group(4)),
        })
    return sorted(rows.values(), key=lambda r: r["rank"])


def parse_today(text: str) -> list[dict]:
    """Đọc bảng ASCII của /today: | Nick | Time | KC | Pace | Typ |."""
    rows: list[dict] = []
    for line in text.splitlines():
        m = TODAY_ROW_RE.match(line)
        if not m:
            continue
        nick = " ".join(m.group(1).split())
        if nick.lower() in {"nick", "st"}:
            continue
        rows.append({
            "nick": nick,
            "distance": float(m.group(3).replace(",", ".")),
            "type": m.group(5).strip().lower(),
        })
    return rows


# ----------------------------------------------------------------- Telegram userbot
def _client(session_string: str | None = None):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    s = env("TG_SESSION_STRING") if session_string is None else session_string
    return TelegramClient(StringSession(s), int(env("TG_API_ID", required=True)), env("TG_API_HASH", required=True))


async def _login() -> None:
    client = _client(session_string="")
    await client.start(phone=lambda: input("Số điện thoại Telegram (dạng +84...): "))
    me = await client.get_me()
    session = client.session.save()
    await client.disconnect()
    print(f"\nĐăng nhập thành công: {me.first_name} (@{me.username}).")
    print("\n=== TG_SESSION_STRING (dán vào GitHub Secret, KHÔNG chia sẻ / KHÔNG commit) ===")
    print(session)
    print("=== hết ===")


def _no_reply_msg(cmd: str, texts: list[str]) -> str:
    got = " | ".join(" ".join(t.split())[:120] for t in texts[-3:]) or "(không nhận được tin nào)"
    return f"@tdht_bot không trả bảng {cmd} trong 90s. Tin đã nhận: {got}"


async def _fetch_ranking() -> tuple[list[dict], str]:
    client = _client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Session Telegram không hợp lệ/hết hạn. Chạy lại: python main.py login rồi cập nhật secret TG_SESSION_STRING")
        texts: list[str] = []
        rows: list[dict] = []
        async with client.conversation(env("TDHT_BOT", "tdht_bot"), timeout=240, exclusive=False) as conv:
            await conv.send_message("/ranking")
            while not rows:  # bỏ qua tin "Xoay màn hình để xem", chờ tới khi có bảng
                try:
                    msg = await conv.get_response(timeout=90)
                except asyncio.TimeoutError:
                    raise RuntimeError(_no_reply_msg("/ranking", texts)) from None
                texts.append(msg.raw_text or "")
                rows = parse_ranking("\n".join(texts))
            while True:  # gom thêm nếu bảng bị tách thành nhiều tin nhắn
                try:
                    msg = await conv.get_response(timeout=6)
                except asyncio.TimeoutError:
                    break
                texts.append(msg.raw_text or "")
        raw = "\n".join(texts)
        return parse_ranking(raw), raw
    finally:
        await client.disconnect()


def fetch_ranking(retries: int = 3) -> tuple[list[dict], str]:
    last: Exception | None = None
    for i in range(retries):
        try:
            rows, raw = asyncio.run(_fetch_ranking())
            if rows:
                return rows, raw
            last = RuntimeError("Không đọc được bảng xếp hạng từ phản hồi của bot")
        except Exception as e:  # noqa: BLE001
            last = e
        if i < retries - 1:
            print(f"Lần {i + 1} thất bại ({last}), thử lại sau 60s...")
            time.sleep(60)
    raise RuntimeError(f"Không lấy được /ranking: {last}")


def _activity_factor(activity_type: str, cfg: dict) -> float:
    t = activity_type.lower()
    if any(k in t for k in cfg["daily_swim_types"]):
        return cfg["daily_swim_factor"]
    if any(k in t for k in cfg["daily_ride_types"]):
        return cfg["daily_ride_factor"]
    return 1.0


def compute_daily_from_today(rows: list[dict], cfg: dict) -> dict:
    totals: dict[str, float] = {}
    for r in rows:
        km = r["distance"] * _activity_factor(r["type"], cfg)
        if km <= 0.005:
            continue
        totals[r["nick"]] = totals.get(r["nick"], 0.0) + km
    deltas = sorted(totals.items(), key=lambda x: -x[1])
    return {"top": deltas[:3], "active": len(deltas), "sum": sum(totals.values())}


async def _fetch_today() -> tuple[list[dict], str]:
    client = _client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Session Telegram không hợp lệ/hết hạn. Chạy lại: python main.py login rồi cập nhật secret TG_SESSION_STRING")
        texts: list[str] = []
        rows: list[dict] = []
        async with client.conversation(env("TDHT_BOT", "tdht_bot"), timeout=240, exclusive=False) as conv:
            await conv.send_message("/today")
            while not rows:
                try:
                    msg = await conv.get_response(timeout=90)
                except asyncio.TimeoutError:
                    raise RuntimeError(_no_reply_msg("/today", texts)) from None
                texts.append(msg.raw_text or "")
                rows = parse_today("\n".join(texts))
                if "chưa có" in (msg.raw_text or "").lower():
                    break
            while True:
                try:
                    msg = await conv.get_response(timeout=6)
                except asyncio.TimeoutError:
                    break
                texts.append(msg.raw_text or "")
        raw = "\n".join(texts)
        return parse_today(raw), raw
    finally:
        await client.disconnect()


def fetch_today(retries: int = 3) -> tuple[list[dict], str]:
    last: Exception | None = None
    for i in range(retries):
        try:
            return asyncio.run(_fetch_today())
        except Exception as e:  # noqa: BLE001
            last = e
        if i < retries - 1:
            print(f"Lần {i + 1} thất bại khi lấy /today ({last}), thử lại sau 30s...")
            time.sleep(30)
    raise RuntimeError(f"Không lấy được /today: {last}")


# ----------------------------------------------------------------- lưu trữ
def save_snapshot(day: date, rows: list[dict], raw: str, daily: dict | None = None, raw_today: str = "",
                  late: bool = False, warnings: list[str] | None = None) -> Path:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / f"{day.isoformat()}.txt").write_text(raw, encoding="utf-8")
    if raw_today:
        (RAW_DIR / f"{day.isoformat()}-today.txt").write_text(raw_today, encoding="utf-8")
    path = SNAP_DIR / f"{day.isoformat()}.json"
    path.write_text(json.dumps({"date": day.isoformat(), "taken_at": datetime.now(TZ).isoformat(),
                                "late": late, "warnings": warnings or [],
                                "rows": rows, "daily": daily}, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_snapshot(day: date) -> dict | None:
    p = SNAP_DIR / f"{day.isoformat()}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def latest_snapshot_before(day: date) -> tuple[date, dict] | None:
    if not SNAP_DIR.exists():
        return None
    days = sorted(date.fromisoformat(p.stem) for p in SNAP_DIR.glob("*.json"))
    earlier = [d for d in days if d < day]
    return (earlier[-1], load_snapshot(earlier[-1])) if earlier else None


def recent_posts(n: int = 5) -> list[str]:
    if not POST_DIR.exists():
        return []
    files = sorted(POST_DIR.glob("*.txt"))[-n:]
    return [f.read_text(encoding="utf-8") for f in files]


def compute_daily(cur: list[dict], prev: list[dict]) -> dict:
    before = {r["nick"]: r["total"] for r in prev}
    deltas = [(r["nick"], r["total"] - before.get(r["nick"], 0.0)) for r in cur]
    deltas = [(n, km) for n, km in deltas if km > 0.005]
    deltas.sort(key=lambda x: -x[1])
    return {"top": deltas[:3], "active": len(deltas), "sum": sum(km for _, km in deltas)}


# ----------------------------------------------------------------- gửi Telegram
def notify(text: str) -> None:
    token, chat = env("NOTIFY_BOT_TOKEN"), env("NOTIFY_CHAT_ID")
    if not (token and chat):
        print("(Chưa cấu hình NOTIFY_BOT_TOKEN/NOTIFY_CHAT_ID — chỉ in ra màn hình)\n" + text)
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Telegram từ chối tin nhắn (HTTP {r.status_code}): {r.text[:300]}")


# ----------------------------------------------------------------- lệnh
def cmd_snapshot(cfg: dict) -> None:
    now = datetime.now(TZ)
    late = now.hour < LATE_UNTIL_HOUR
    # Chạy trễ qua 0h: số liệu thuộc về ngày hôm trước, không phải "hôm nay".
    day = now.date() - timedelta(days=1) if late else now.date()
    if day > cfg["anniv"] + timedelta(days=1):
        print("Giải đã kết thúc, bỏ qua snapshot.")
        return

    existing = load_snapshot(day)
    if late and existing is not None and not existing.get("late"):
        print(f"Đã có snapshot đúng giờ của {day.isoformat()} (lúc {existing.get('taken_at')}), "
              "lượt trễ này không ghi đè.")
        return

    warnings: list[str] = []
    rows, raw = fetch_ranking()
    today_rows: list[dict] = []
    raw_today = ""
    daily: dict | None = None
    if late:
        # /today lúc này đã là ngày mới (gần như trống): dùng nó sẽ làm mất km của ngày cũ.
        msg = (f"Snapshot ngày {day.isoformat()} chạy TRỄ lúc {now:%H:%M %d/%m} (giờ VN), đã qua 0h nên không dùng "
               "/today. Km trong ngày sẽ được ước tính từ chênh lệch bảng tổng, chưa chắc khớp quy đổi.")
        warnings.append(msg)
        print("⚠️ " + msg)
    else:
        try:
            today_rows, raw_today = fetch_today()
            if datetime.now(TZ).date() != day:
                raise RuntimeError("/today trả về sau 0h nên đã sang ngày mới")
            daily = compute_daily_from_today(today_rows, cfg)
        except Exception as e:  # noqa: BLE001
            raw_today = ""
            msg = f"Không lấy được /today ngày {day.isoformat()} ({e}); vẫn lưu /ranking."
            if existing is not None and existing.get("daily") is not None:
                daily = existing["daily"]
                msg += f" Giữ km ngày từ lượt chạy trước ({existing.get('taken_at')})."
            else:
                msg += " Km ngày sẽ được ước tính từ chênh lệch bảng tổng."
            warnings.append(msg)
            print("⚠️ " + msg)

    path = save_snapshot(day, rows, raw, daily=daily, raw_today=raw_today, late=late, warnings=warnings)
    info = f"/today hợp lệ: {daily['active']} người, tổng quy đổi {daily['sum']:.2f} km." if daily else "Không có /today."
    print(f"Đã lưu {len(rows)} runner vào {path.name}. Top 3 tổng: {[r['nick'] for r in rows[:3]]}. {info}")
    if warnings:
        try:
            notify("⚠️ Dữ liệu snapshot bị suy giảm:\n" + "\n".join(warnings))
        except Exception as e:  # noqa: BLE001
            print(f"(Không gửi được cảnh báo Telegram: {e})")


def cmd_post(cfg: dict, dry_run: bool) -> None:
    day = today_vn()
    if day <= cfg["start"] or day > cfg["anniv"]:
        print(f"Ngoài thời gian đăng bài ({cfg['start'] + timedelta(days=1)} → {cfg['anniv']}), bỏ qua.")
        return

    warnings: list[str] = []
    report_day = day - timedelta(days=1)
    snap = load_snapshot(report_day)
    if snap is None:
        prev = latest_snapshot_before(day)
        if prev is None:
            raise RuntimeError("Không có snapshot nào trước ngày đăng bài.")
        warnings.append(f"Không có snapshot ngày {report_day.isoformat()}, đang dùng snapshot {prev[0].isoformat()} "
                        "nên bảng xếp hạng và km trong ngày đều CŨ. Kiểm tra tab Actions.")
        report_day, snap = prev
    warnings += snap.get("warnings") or []

    rows = snap["rows"]
    daily = snap.get("daily")
    prev_date = report_day
    if daily is None:  # snapshot trễ / /today lỗi / snapshot cũ: ước tính bằng chênh lệch bảng tổng
        prev = latest_snapshot_before(report_day)
        if prev:
            daily = compute_daily(rows, prev[1]["rows"])
            prev_date = report_day if prev[0] == report_day - timedelta(days=1) else prev[0]
            warnings.append(f"Km trong ngày là ƯỚC TÍNH từ chênh lệch bảng tổng {prev[0].isoformat()} → "
                            f"{report_day.isoformat()} (đạp xe/bơi có thể chưa quy đổi đúng).")
        else:
            prev_date = None
            warnings.append("Chưa có snapshot hôm trước nên không có mục top km trong ngày.")
    ctx = composer.build_context(day, rows, daily, prev_date, cfg)
    draft = composer.compose(ctx)
    text, source = composer.rewrite_with_llm(ctx, draft, recent_posts(), cfg)

    POST_DIR.mkdir(parents=True, exist_ok=True)
    (POST_DIR / f"{day.isoformat()}.txt").write_text(text, encoding="utf-8")

    if dry_run:
        print(f"[nguồn: {source}]\n{text}")
        for w in warnings:
            print(f"⚠️ {w}")
        return
    note = f"📋 Bài đăng Strava {composer.dmy(day)} (nguồn: {source}). Chạm giữ tin bên dưới → Sao chép → dán vào Club."
    if warnings:
        note += "\n\n⚠️ Dữ liệu bị suy giảm, kiểm tra trước khi đăng:\n" + "\n".join(f"• {w}" for w in warnings)
    notify(note)
    notify(text)
    print("Đã gửi bài qua Telegram.")


def cmd_chat_id() -> None:
    token = env("NOTIFY_BOT_TOKEN", required=True)
    res = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30).json()
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name") or u["message"]["chat"].get("title")
             for u in res.get("result", []) if "message" in u}
    if not chats:
        print("Chưa thấy tin nhắn nào. Hãy mở bot của bạn trên Telegram, bấm Start / gửi 1 tin rồi chạy lại.")
    for cid, name in chats.items():
        print(f"chat_id = {cid}  ({name})")


def main() -> None:
    ap = argparse.ArgumentParser(description="VTS Running Club daily Strava post")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    sub.add_parser("chat-id")
    sub.add_parser("snapshot")
    p = sub.add_parser("post")
    p.add_argument("--dry-run", action="store_true")
    q = sub.add_parser("parse")
    q.add_argument("file")
    args = ap.parse_args()
    cfg = load_cfg()

    try:
        if args.cmd == "login":
            asyncio.run(_login())
        elif args.cmd == "chat-id":
            cmd_chat_id()
        elif args.cmd == "snapshot":
            cmd_snapshot(cfg)
        elif args.cmd == "post":
            cmd_post(cfg, args.dry_run)
        elif args.cmd == "parse":
            for r in parse_ranking(Path(args.file).read_text(encoding="utf-8")):
                print(r)
    except Exception as e:  # noqa: BLE001
        msg = f"⚠️ Bot bài đăng Strava lỗi ở lệnh '{args.cmd}': {e}"
        print(msg, file=sys.stderr)
        if args.cmd in ("snapshot", "post"):
            try:
                notify(msg)
            except Exception:  # noqa: BLE001
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
