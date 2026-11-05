"""Kênh gửi tin Telegram tách khỏi aiogram để services/jobs không import bot.

Bot đăng ký sender bằng `set_sender(fn)`; nơi khác gọi `dm/group/admins`.
Khi chưa có sender (test, CLI) tin được in ra log và bỏ qua.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from app.config import get_settings

log = logging.getLogger(__name__)

Sender = Callable[[int, str], Awaitable[None]]  # (chat_id, text)
_sender: Sender | None = None


def set_sender(fn: Sender | None) -> None:
    global _sender
    _sender = fn


async def send(chat_id: int, text: str) -> bool:
    if _sender is None:
        log.info("[notify] (chưa có sender) -> %s: %s", chat_id, text)
        return False
    try:
        await _sender(chat_id, text)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("Không gửi được tin tới %s: %s", chat_id, e)
        return False


async def dm(telegram_id: int | None, text: str) -> bool:
    return await send(telegram_id, text) if telegram_id else False


async def group(text: str) -> bool:
    gid = get_settings().group_chat_id
    return await send(gid, text) if gid else False


async def admins(text: str) -> None:
    for aid in get_settings().admin_telegram_ids:
        await send(aid, text)
