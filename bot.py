from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.db import Database
from app.handlers import get_routers
from app.services.session_manager import SessionManager
from app.services.webcontrol import WebControl


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    db = Database(settings.db_path)
    await db.init()

    webcontrol = WebControl(settings.public_url)
    await webcontrol.start(settings.webcontrol_host, settings.webcontrol_port)

    manager = SessionManager(settings, db, bot, webcontrol)
    await manager.start()
    await manager.restore()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(manager.finalize_expired, "interval", minutes=settings.sweep_minutes)
    scheduler.add_job(manager.flush_pushes, "interval", seconds=45)
    scheduler.add_job(manager.sweep_stale, "interval", minutes=10)
    scheduler.add_job(manager.retry_subscriptions, "interval", minutes=4)
    scheduler.start()

    dp["settings"] = settings
    dp["db"] = db
    dp["manager"] = manager
    for r in get_routers():
        dp.include_router(r)

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await manager.stop()
        await webcontrol.stop()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
