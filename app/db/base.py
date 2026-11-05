"""Kết nối DB (SQLAlchemy 2 async + aiosqlite).

- `init_db()` tạo bảng (create_all) và bật WAL cho SQLite.
- `session_scope()` dùng trong services/jobs/bot: `async with session_scope() as s:`.
- Test gọi `configure_engine("sqlite+aiosqlite://")` để dùng DB trong bộ nhớ.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def configure_engine(url: str | None = None) -> AsyncEngine:
    global _engine, _session_factory
    url = url or get_settings().database_url
    if url.startswith("sqlite") and ":memory:" not in url and url.count("/") >= 3:
        path = url.split("///", 1)[1]
        if path and not path.startswith(":"):
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    kwargs = {}
    if url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool
        if ":memory:" in url or url.endswith("sqlite+aiosqlite://"):
            kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    _engine = create_async_engine(url, future=True, **kwargs)

    @event.listens_for(_engine.sync_engine, "connect")
    def _on_connect(dbapi_conn, _):  # pragma: no cover - trivial
        try:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
        except Exception:  # noqa: BLE001
            pass

    _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_engine() -> AsyncEngine:
    return _engine or configure_engine()


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        configure_engine()
    assert _session_factory is not None
    return _session_factory


async def init_db() -> None:
    from app.db import models  # noqa: F401  (đăng ký bảng)
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("SELECT 1"))


async def dispose() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Mở session, commit khi xong, rollback khi lỗi."""
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
