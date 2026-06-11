"""Telegram-бот: подписки на алёрты + ручные команды.

Команды:
- /start       — подписаться на алёрты
- /stop        — отписаться
- /list        — последние 20 дропов
- /mute <nm>   — заглушить товар по nm_id (бессрочно; снять — /unmute)
- /unmute <nm>
- /status      — диагностика
"""
from __future__ import annotations

import html
from typing import Iterable

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message
from loguru import logger

from . import db
from .config import settings
from .detector import DropEvent
from .wb_api import WbProduct


def wb_card_url(nm_id: int) -> str:
    return f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"


def wb_card_image(nm_id: int) -> str:
    """basket-N зависит от диапазона nm_id. Алгоритм публично известный."""
    short = nm_id // 1000
    vol = short // 100
    part = short
    basket = _basket_for_vol(vol)
    return (
        f"https://basket-{basket:02d}.wbbasket.ru/vol{vol}/part{part}/{nm_id}/images/big/1.webp"
    )


def _basket_for_vol(vol: int) -> int:
    # упрощённая таблица; для надёжности — пробуем 01..20 на стороне клиента TG
    ranges = [
        (0, 143, 1), (144, 287, 2), (288, 431, 3), (432, 719, 4),
        (720, 1007, 5), (1008, 1061, 6), (1062, 1115, 7), (1116, 1169, 8),
        (1170, 1313, 9), (1314, 1601, 10), (1602, 1655, 11), (1656, 1919, 12),
        (1920, 2045, 13), (2046, 2189, 14), (2190, 2405, 15), (2406, 2621, 16),
        (2622, 2837, 17), (2838, 3053, 18), (3054, 3269, 19), (3270, 10000, 20),
    ]
    for lo, hi, b in ranges:
        if lo <= vol <= hi:
            return b
    return 20


def format_alert(event: DropEvent, prod: WbProduct) -> str:
    name = html.escape(prod.name or f"nm={prod.nm_id}")
    brand = html.escape(prod.brand or "")
    median = f"{event.median_price:,.0f}".replace(",", " ")
    current = f"{event.current_price:,.0f}".replace(",", " ")
    return (
        f"<b>📉 Падение цены {event.drop_pct:.1f}%</b>\n"
        f"<b>{name}</b>\n"
        f"{brand}\n\n"
        f"Было (медиана 24ч): <s>{median} ₽</s>\n"
        f"Сейчас: <b>{current} ₽</b>\n\n"
        f"<a href=\"{wb_card_url(prod.nm_id)}\">Открыть на Wildberries</a>"
    )


# ---------- handlers ----------

dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if message.from_user is None:
        return
    await db.add_subscriber(message.from_user.id)
    await message.answer(
        "Подписка оформлена. Буду присылать алёрты о падениях цен ≥ "
        f"{settings.drop_threshold_pct:.0f}% от медианы за "
        f"{settings.drop_window_hours}ч.\n\n"
        "Команды: /list, /mute &lt;nm&gt;, /unmute &lt;nm&gt;, /stop, /status"
    )


@dp.message(Command("stop"))
async def cmd_stop(message: Message) -> None:
    if message.from_user is None:
        return
    await db.remove_subscriber(message.from_user.id)
    await message.answer("Отписан. Возврат — /start.")


@dp.message(Command("mute"))
async def cmd_mute(message: Message, command: CommandObject) -> None:
    if message.from_user is None:
        return
    nm = _parse_nm(command.args)
    if nm is None:
        await message.answer("Использование: <code>/mute 12345678</code>")
        return
    await db.mute(message.from_user.id, nm)
    await message.answer(f"Заглушил <code>{nm}</code>.")


@dp.message(Command("unmute"))
async def cmd_unmute(message: Message, command: CommandObject) -> None:
    if message.from_user is None:
        return
    nm = _parse_nm(command.args)
    if nm is None:
        await message.answer("Использование: <code>/unmute 12345678</code>")
        return
    # просто помечаем until_ts в прошлом
    import time as _t
    await db.mute(message.from_user.id, nm, until_ts=int(_t.time()) - 1)
    await message.answer(f"Снял мьют с <code>{nm}</code>.")


@dp.message(Command("list"))
async def cmd_list(message: Message) -> None:
    conn = await db.get_db()
    async with conn.execute(
        """
        SELECT a.nm_id, a.median_price, a.current_price, a.drop_pct, a.ts,
               p.name, p.brand
        FROM alerts a
        LEFT JOIN products p USING(nm_id)
        ORDER BY a.ts DESC LIMIT 20
        """
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        await message.answer("Пока ни одного дропа.")
        return
    lines = ["<b>Последние дропы:</b>"]
    for r in rows:
        nm = r["nm_id"]
        name = html.escape((r["name"] or f"nm={nm}")[:60])
        lines.append(
            f"• {r['drop_pct']:.1f}% — <a href=\"{wb_card_url(nm)}\">{name}</a> "
            f"({int(r['current_price'])} ₽)"
        )
    await message.answer("\n".join(lines), disable_web_page_preview=True)


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    conn = await db.get_db()
    async with conn.execute("SELECT COUNT(*) AS c FROM products") as cur:
        prods = (await cur.fetchone())["c"]
    async with conn.execute("SELECT COUNT(*) AS c FROM price_snapshots") as cur:
        snaps = (await cur.fetchone())["c"]
    async with conn.execute("SELECT COUNT(*) AS c FROM alerts") as cur:
        alerts = (await cur.fetchone())["c"]
    async with conn.execute("SELECT COUNT(*) AS c FROM subscribers WHERE active = 1") as cur:
        subs = (await cur.fetchone())["c"]
    await message.answer(
        f"Товаров: {prods}\nСнапшотов: {snaps}\nАлёртов всего: {alerts}\nПодписчиков: {subs}"
    )


def _parse_nm(args: str | None) -> int | None:
    if not args:
        return None
    try:
        return int(args.strip().split()[0])
    except (ValueError, IndexError):
        return None


# ---------- broadcast ----------

async def make_alert_callback(bot: Bot):
    async def cb(event: DropEvent, prod: WbProduct) -> None:
        text = format_alert(event, prod)
        subs = await db.active_subscribers()
        for uid in subs:
            if await db.is_muted(uid, prod.nm_id):
                continue
            try:
                await bot.send_message(uid, text, disable_web_page_preview=False)
            except Exception as e:
                logger.warning("send to {} failed: {}", uid, e)
    return cb


def make_bot(token: str | None = None) -> Bot:
    tok = token or settings.tg_bot_token
    if not tok:
        raise RuntimeError("Токен бота не задан (ни в БД через UI, ни в .env)")
    return Bot(
        token=tok,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
