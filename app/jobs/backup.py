"""Backup SQLite bằng sqlite3 backup API (an toàn khi DB đang mở ở chế độ WAL).

run_backup() -> đường dẫn file backup, hoặc None nếu DB không phải SQLite file.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import get_settings

log = logging.getLogger(__name__)

BACKUP_PREFIX = "vts-"


def sqlite_file_path(database_url: str) -> str | None:
    """'sqlite+aiosqlite:///./data/vts.db' -> './data/vts.db'; in-memory / không phải sqlite -> None."""
    url = (database_url or "").strip()
    if not url.lower().startswith("sqlite"):
        return None
    if ":memory:" in url or "///" not in url:
        return None
    path = url.split("///", 1)[1].split("?", 1)[0]
    if not path or path.startswith(":"):
        return None
    return path


def run_backup() -> str | None:
    settings = get_settings()
    src = sqlite_file_path(settings.database_url)
    if src is None:
        log.info("[backup] DATABASE_URL không phải SQLite file, bỏ qua backup.")
        return None
    if not os.path.exists(src):
        log.warning("[backup] Không tìm thấy file DB %s, bỏ qua.", src)
        return None

    bdir = Path(settings.backup_dir)
    bdir.mkdir(parents=True, exist_ok=True)
    try:
        now = datetime.now(ZoneInfo(settings.timezone))
    except Exception:  # noqa: BLE001
        now = datetime.now()
    dst = bdir / f"{BACKUP_PREFIX}{now:%Y%m%d-%H%M}.db"
    tmp = dst.with_suffix(".db.part")

    src_conn = sqlite3.connect(src)
    try:
        if tmp.exists():
            tmp.unlink()
        dst_conn = sqlite3.connect(tmp)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    os.replace(tmp, dst)
    log.info("[backup] Đã sao lưu DB -> %s", dst)

    keep = max(1, int(settings.backup_keep or 1))
    files = sorted(bdir.glob(f"{BACKUP_PREFIX}*.db"), key=lambda p: p.name, reverse=True)
    for old in files[keep:]:
        try:
            old.unlink()
            log.info("[backup] Đã xoá backup cũ %s", old.name)
        except OSError as e:
            log.warning("[backup] Không xoá được %s: %s", old, e)
    return str(dst)


async def run_backup_async() -> str | None:
    return await asyncio.to_thread(run_backup)
