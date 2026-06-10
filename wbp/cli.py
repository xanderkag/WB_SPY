"""Консольный + Telegram режим.

Запуск:
    python -m wbp.cli once    # один тик коллектора и выход (для отладки)
    python -m wbp.cli loop    # бесконечный цикл по POLL_INTERVAL_SECONDS
                              # + Telegram-бот, если задан TG_BOT_TOKEN

Алёрты при срабатывании детектора всегда печатаются в stdout.
Если задан TG_BOT_TOKEN — параллельно идёт рассылка подписчикам бота.
Подписаться: написать боту /start в Telegram.
"""
from __future__ import annotations

import asyncio
import sys

from loguru import logger

from . import db
from .collector import collector_tick, run_collector_loop
from .config import settings
from .detector import DropEvent
from .wb_api import WbProduct


async def console_alert(event: DropEvent, prod: WbProduct) -> None:
    bar = "=" * 60
    name = (prod.name or f"nm={prod.nm_id}").strip()
    print(
        f"\n{bar}\n"
        f"📉 ДРОП {event.drop_pct:.1f}%\n"
        f"   {name}\n"
        f"   brand={prod.brand}  nm={prod.nm_id}\n"
        f"   медиана 24ч: {event.median_price:,.0f} ₽\n"
        f"   сейчас:      {event.current_price:,.0f} ₽\n"
        f"   https://www.wildberries.ru/catalog/{prod.nm_id}/detail.aspx\n"
        f"{bar}",
        flush=True,
    )


def _make_combined_alert(tg_cb):
    """Объединяет console_alert + telegram callback в один."""
    async def cb(event: DropEvent, prod: WbProduct) -> None:
        await console_alert(event, prod)
        try:
            await tg_cb(event, prod)
        except Exception as e:
            logger.warning("TG alert failed: {}", e)
    return cb


async def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    if settings.use_playwright:
        logger.info("USE_PLAYWRIGHT=1 — гоняем JSON через Chromium")
    if settings.use_curl_cffi:
        logger.info("USE_CURL_CFFI=1 — TLS-impersonation Chrome 124")

    bot = None
    dp = None
    tg_task = None
    alert_cb = console_alert

    if mode == "loop" and settings.tg_bot_token:
        from .bot import dp as _dp, make_alert_callback, make_bot
        bot = make_bot()
        dp = _dp
        # подключаем БД и формируем TG callback
        async with db.lifespan():
            tg_cb = await make_alert_callback(bot)
            alert_cb = _make_combined_alert(tg_cb)
            logger.info("Telegram бот включён — пиши /start в TG чтобы подписаться")
            tg_task = asyncio.create_task(dp.start_polling(bot))
            try:
                await run_collector_loop(alert_cb)
            finally:
                tg_task.cancel()
                try:
                    await tg_task
                except asyncio.CancelledError:
                    pass
                try:
                    await bot.session.close()
                except Exception:
                    pass
                await _close_backends()
        return

    # без TG: либо once, либо loop без токена
    async with db.lifespan():
        try:
            if mode == "once":
                logger.info("один тик коллектора → выход")
                await collector_tick(alert_cb)
            elif mode == "loop":
                logger.info("бесконечный цикл (TG-бот не подключён — нет TG_BOT_TOKEN)")
                await run_collector_loop(alert_cb)
            else:
                print(__doc__)
        finally:
            await _close_backends()


async def _close_backends() -> None:
    if settings.use_playwright:
        from .wb_browser import WbBrowser
        if WbBrowser._instance is not None:
            await WbBrowser._instance.close()
    if settings.use_curl_cffi:
        from .wb_cffi import WbCffi
        if WbCffi._instance is not None:
            await WbCffi._instance.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nостановлено", flush=True)
