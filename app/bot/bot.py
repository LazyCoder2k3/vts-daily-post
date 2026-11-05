"""Khởi tạo Bot + Dispatcher aiogram và chạy polling.

- Văn bản thuần (parse_mode=None): tên runner có ký tự đặc biệt (_ * [ …) không làm hỏng tin.
- Đăng ký `app.notify.set_sender` để services/jobs gửi Telegram mà không import aiogram.
- `MEMBER_COMMANDS` dùng chung cho set_my_commands và /help.
"""
from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand, BotCommandScopeDefault

from app import notify
from app.config import Settings, get_settings

log = logging.getLogger(__name__)

# (lệnh, mô tả) — tiếng Việt, hiển thị trong menu "/" của Telegram và trong /help
MEMBER_COMMANDS: list[tuple[str, str]] = [
    ("start", "Bắt đầu, kết nối Strava"),
    ("connect", "Kết nối tài khoản Strava"),
    ("me", "Thành tích của tôi trong giải"),
    ("today", "Km hợp lệ hôm nay (hoặc ngày dd/mm)"),
    ("ranking", "Bảng xếp hạng (tuỳ chọn: số người)"),
    ("log", "Hoạt động gần đây của tôi"),
    ("rules", "Luật giải hiện hành"),
    ("unlink", "Ngắt kết nối Strava"),
    ("help", "Danh sách lệnh"),
]

TELEGRAM_MAX_LEN = 4096
CHUNK_LEN = 4000   # chừa chỗ an toàn dưới giới hạn 4096 của Telegram


def split_text(text: str, limit: int = CHUNK_LEN) -> list[str]:
    """Cắt tin dài thành nhiều phần ≤ limit ký tự, ưu tiên cắt theo dòng.

    Một dòng đơn lẻ dài hơn limit sẽ bị cắt cứng theo ký tự.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:                 # dòng quá dài: cắt cứng
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = line if not cur else f"{cur}\n{line}"
        if len(candidate) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


def make_sender(bot: Bot) -> notify.Sender:
    """Sender cho app.notify: gửi văn bản thuần, tắt preview link, tự cắt tin dài."""

    async def _send(chat_id: int, text: str) -> None:
        for chunk in split_text(text):
            await bot.send_message(chat_id, chunk, disable_web_page_preview=True)

    return _send


def create_bot_and_dispatcher(settings: Settings | None = None) -> tuple[Bot, Dispatcher]:
    settings = settings or get_settings()
    bot = Bot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=None))
    dp = Dispatcher()

    # Import trong hàm: test module member không cần admin.py, và tránh import vòng.
    from app.bot import admin, member

    # Admin trước: lệnh dạng "/rules set …" (có IsAdmin) được thử trước; không khớp thì rơi xuống member.
    dp.include_router(admin.router)
    dp.include_router(member.router)

    notify.set_sender(make_sender(bot))
    return bot, dp


async def run_polling(bot: Bot, dp: Dispatcher) -> None:
    """Đặt menu lệnh mặc định rồi chạy long-polling (không bắt tín hiệu — caller quản lý vòng đời)."""
    try:
        await bot.set_my_commands(
            [BotCommand(command=c, description=d) for c, d in MEMBER_COMMANDS],
            scope=BotCommandScopeDefault(),
        )
    except Exception as e:  # noqa: BLE001 — menu lệnh không quan trọng bằng việc bot chạy
        log.warning("Không đặt được menu lệnh: %s", e)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=False)
