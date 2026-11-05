"""Điểm vào: FastAPI (OAuth callback, webhook, health) + bot Telegram polling + scheduler trong một process.

Chạy: python -m app.main   hoặc   uvicorn app.main:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app import deps
from app.config import get_settings
from app.db.base import configure_engine, dispose, init_db
from app.jobs.scheduler import shutdown_scheduler, start_scheduler
from app.strava.client import StravaClient

log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        logging.basicConfig(level=getattr(logging, str(settings.log_level).upper(), logging.INFO),
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        configure_engine()
        await init_db()
        client = StravaClient()
        deps.set_client(client)

        bot = dp = polling_task = None
        if settings.telegram_bot_token:
            from app.bot.bot import create_bot_and_dispatcher, run_polling
            bot, dp = create_bot_and_dispatcher()
            polling_task = asyncio.create_task(run_polling(bot, dp), name="telegram-polling")
            log.info("Bot Telegram: polling đã khởi động.")
        else:
            log.warning("TELEGRAM_BOT_TOKEN trống: không khởi động bot Telegram.")

        start_scheduler()
        log.info("Sẵn sàng tại %s (callback %s, webhook %s)", settings.public_base_url,
                 settings.redirect_uri, settings.webhook_url)
        try:
            yield
        finally:
            if polling_task is not None:
                polling_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await polling_task
            shutdown_scheduler()
            if bot is not None:
                with suppress(Exception):
                    await bot.session.close()
            with suppress(Exception):
                await client.aclose()
            deps.set_client(None)
            with suppress(Exception):
                await dispose()
            log.info("Đã dừng.")

    app = FastAPI(title="VTS Running Club — Strava bot", version="1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    from app.api import router
    app.include_router(router)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    _s = get_settings()
    uvicorn.run("app.main:app", host=_s.host, port=_s.port, log_level=str(_s.log_level).lower())
