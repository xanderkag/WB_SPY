"""Точка входа: одновременно крутим коллектор и Telegram-бот."""
from __future__ import annotations

import asyncio

from loguru import logger

from . import db
from .bot import dp, make_alert_callback, make_bot
from .collector import run_collector_loop


async def main() -> None:
    bot = make_bot()
    on_alert = await make_alert_callback(bot)

    async with db.lifespan():
        collector_task = asyncio.create_task(run_collector_loop(on_alert))
        try:
            logger.info("starting bot polling + collector loop")
            await dp.start_polling(bot)
        finally:
            collector_task.cancel()
            try:
                await collector_task
            except asyncio.CancelledError:
                pass
            await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
