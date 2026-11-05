"""Bộ lọc quyền admin cho aiogram."""
from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import Message

from app.config import get_settings


class IsAdmin(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user is not None and get_settings().is_admin(message.from_user.id)
