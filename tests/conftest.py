"""Fixture chung: DB SQLite trong bộ nhớ, settings test, không gửi Telegram thật."""
from __future__ import annotations

import os

import pytest
import pytest_asyncio

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:TEST")
os.environ.setdefault("STRAVA_CLIENT_ID", "1")
os.environ.setdefault("STRAVA_CLIENT_SECRET", "secret")
os.environ.setdefault("PUBLIC_BASE_URL", "https://run.test")
os.environ.setdefault("ADMIN_TELEGRAM_IDS", "1000")
os.environ.setdefault("GROUP_CHAT_ID", "-100500")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "")

from app.config import get_settings, reset_settings  # noqa: E402
from app.db import base as dbbase  # noqa: E402


@pytest.fixture(autouse=True)
def _settings():
    reset_settings()
    yield get_settings()
    reset_settings()


@pytest_asyncio.fixture
async def db():
    """Engine SQLite in-memory mới cho mỗi test; trả về session factory."""
    await dbbase.dispose()
    dbbase.configure_engine("sqlite+aiosqlite://")
    await dbbase.init_db()
    yield dbbase.get_session_factory()
    await dbbase.dispose()


@pytest.fixture
def sent_messages(monkeypatch):
    """Bắt mọi tin gửi qua app.notify."""
    from app import notify
    box: list[tuple[int, str]] = []

    async def _sender(chat_id: int, text: str) -> None:
        box.append((chat_id, text))

    notify.set_sender(_sender)
    yield box
    notify.set_sender(None)
