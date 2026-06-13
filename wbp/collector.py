"""Периодический сбор данных по WB.

Два режима:
- если WB_SELF_SUPPLIER_IDS пуст → обходим каждую категорию ЦЕЛИКОМ
  (catalog/<shard>/v4/catalog). Это и discovery, и тест-режим: лог покажет
  ТОП-уникальных продавцов на странице, чтобы было видно кого писать в config.
- если supplier-IDs заданы → обходим только эти supplier × category
  (sellers/v4/catalog?supplier=...).

После записи снапшотов прогоняем каждый товар через detector.detect_drop.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable

from loguru import logger

from . import db
from .config import CATEGORIES, Category, settings
from .detector import DropEvent, detect_drop, detect_target
from .wb_api import WbClient, WbProduct


AlertCallback = Callable[[DropEvent, WbProduct], Awaitable[None]]

WB_NAME_RE = re.compile(r"(wildberries|вайлдберри|вайлберри|вб\b|rvb|рвб)", re.I)


async def _flush(batch: list[WbProduct]) -> None:
    await db.upsert_products(batch)
    await db.insert_snapshots(batch)


async def _run_detector(
    by_nm: dict[int, WbProduct], on_alert: AlertCallback
) -> int:
    drops = 0
    for nm_id, prod in by_nm.items():
        if prod.sale_price is None:
            continue
        # сначала проверяем целевую цену (если задана и сейчас сработало) —
        # это пользовательский кейс, он важнее общего детектора падений.
        for detector_fn in (detect_target, detect_drop):
            event = await detector_fn(nm_id, prod.sale_price)
            if event is None:
                continue
            drops += 1
            await db.record_alert(
                nm_id, event.median_price, event.current_price,
                event.drop_pct, kind=event.kind,
            )
            if event.kind == "target":
                await db.touch_target_notified(nm_id)
            try:
                await on_alert(event, prod)
            except Exception as e:
                logger.exception("alert callback failed for nm={}: {}", nm_id, e)
            break  # одно событие на товар в тик
    return drops


def _log_supplier_top(label: str, suppliers: Counter[tuple[int, str]]) -> None:
    if not suppliers:
        return
    top = suppliers.most_common(10)
    logger.info("[{}] уникальных продавцов: {}", label, len(suppliers))
    for (sid, sname), cnt in top:
        marker = "  ⭐ WB-LIKE" if WB_NAME_RE.search(sname or "") else ""
        logger.info("  supplier={:>10}  cnt={:>3}  name={!r}{}", sid, cnt, sname, marker)
    wb_like = [(sid, sname) for (sid, sname), _ in suppliers.items()
               if WB_NAME_RE.search(sname or "")]
    if wb_like:
        ids = sorted({sid for sid, _ in wb_like})
        logger.warning("⭐ найдены WB-подобные supplierId в {}: {}", label, ids)


async def _collect_category(
    wb: WbClient, cat: Category, on_alert: AlertCallback
) -> tuple[int, int]:
    """Полный обход категории — режим discovery + сбор данных."""
    seen = 0
    batch: list[WbProduct] = []
    by_nm: dict[int, WbProduct] = {}
    suppliers: Counter[tuple[int, str]] = Counter()

    async for prod in wb.iter_category_products(cat.shard, cat.cat, cat.xsubject):
        batch.append(prod)
        by_nm[prod.nm_id] = prod
        if prod.supplier_id is not None:
            suppliers[(prod.supplier_id, prod.supplier_name or "")] += 1
        seen += 1
        if len(batch) >= 200:
            await _flush(batch)
            batch.clear()
    if batch:
        await _flush(batch)

    _log_supplier_top(f"category={cat.name}", suppliers)
    drops = await _run_detector(by_nm, on_alert)
    logger.info("category={} seen={} drops={}", cat.name, seen, drops)
    return seen, drops


async def _collect_supplier(
    wb: WbClient, supplier_id: int, on_alert: AlertCallback
) -> tuple[int, int]:
    """Один запрос на продавца — отдаёт ВЕСЬ каталог.
    Фильтрация по subjectId — на нашей стороне (xsubject в WB API на
    sellers/v4 не работает, см. NOTES.md)."""
    target_subjects = {c.xsubject for c in CATEGORIES if c.xsubject is not None}
    # для категорий без xsubject (типа смарт-часы) фильтруем по subject_id
    # из БД WB пока нельзя — пропускаем фильтр и берём всё. Если нужно строже —
    # завести явный allowlist subject_id в config.

    seen = 0
    matched = 0
    batch: list[WbProduct] = []
    by_nm: dict[int, WbProduct] = {}

    async for prod in wb.iter_seller_products(supplier_id, xsubject=None):
        seen += 1
        if target_subjects and prod.subject_id not in target_subjects:
            # для PoC оставим всё — пользователь хочет видеть весь каталог продавца
            pass
        matched += 1
        batch.append(prod)
        by_nm[prod.nm_id] = prod
        if len(batch) >= 200:
            await _flush(batch)
            batch.clear()
    if batch:
        await _flush(batch)

    drops = await _run_detector(by_nm, on_alert)
    logger.info(
        "supplier={} caught={} matched={} drops={}",
        supplier_id, seen, matched, drops,
    )
    return matched, drops


async def collector_tick(on_alert: AlertCallback) -> None:
    if not CATEGORIES:
        logger.warning("CATEGORIES пуст — нечего собирать.")
        return

    started = time.time()
    total_seen = 0
    total_drops = 0

    # читаем актуальный список продавцов из БД на каждом тике — UI может
    # добавить/убрать продавца, и со следующего тика парсер это подхватит.
    supplier_ids = await db.active_supplier_ids()

    # auto-sleep: после AUTO_SLEEP_IDLE_TICKS пустых тиков подряд продавец гасится.
    # 48 тиков × 30 мин ≈ сутки молчания → отключаем чтобы не молотить впустую.
    AUTO_SLEEP_IDLE_TICKS = 48

    async with WbClient() as wb:
        if supplier_ids:
            logger.info("режим: supplier ({} suppliers — один запрос на каждого)",
                        len(supplier_ids))
            for supplier_id in supplier_ids:
                try:
                    s, d = await _collect_supplier(wb, supplier_id, on_alert)
                    total_seen += s
                    total_drops += d
                except Exception as e:
                    logger.exception("supplier={} failed: {}", supplier_id, e)
                    continue
                # обновляем idle-счётчик в БД: 0 при успехе, +1 при пустом ответе.
                try:
                    conn = await db.get_db()
                    if s == 0:
                        await conn.execute(
                            "UPDATE tracked_suppliers SET idle_ticks = COALESCE(idle_ticks,0) + 1, "
                            "last_check_ts = ? WHERE supplier_id = ?",
                            (int(time.time()), supplier_id),
                        )
                        # авто-сон если перевалили порог
                        await conn.execute(
                            "UPDATE tracked_suppliers SET active = 0 "
                            "WHERE supplier_id = ? AND COALESCE(idle_ticks,0) >= ?",
                            (supplier_id, AUTO_SLEEP_IDLE_TICKS),
                        )
                    else:
                        await conn.execute(
                            "UPDATE tracked_suppliers SET idle_ticks = 0, last_check_ts = ? "
                            "WHERE supplier_id = ?",
                            (int(time.time()), supplier_id),
                        )
                    await conn.commit()
                except Exception as e:
                    logger.warning("idle-counter update failed: {}", e)
        else:
            logger.info("режим: discovery — обход {} категорий целиком "
                        "(supplier IDs не заданы)", len(CATEGORIES))
            for cat in CATEGORIES:
                try:
                    s, d = await _collect_category(wb, cat, on_alert)
                    total_seen += s
                    total_drops += d
                except Exception as e:
                    logger.exception("category={} failed: {}", cat.name, e)

    logger.info("tick done: seen={} drops={} in {:.1f}s",
                total_seen, total_drops, time.time() - started)

    # авто-бэкап БД раз в сутки (идемпотентно — частый вызов безопасен)
    try:
        await db.maybe_daily_backup()
    except Exception as e:
        logger.warning("daily backup error: {}", e)


async def run_collector_loop(on_alert: AlertCallback) -> None:
    while True:
        try:
            await collector_tick(on_alert)
        except Exception as e:
            logger.exception("collector_tick crashed: {}", e)
        # интервал сна берём из настроек (БД → env), на лету подхватываем UI-смены
        try:
            s = await db.get_detector_settings(force=True)
            sleep_secs = int(s["poll_interval_seconds"])
        except Exception:
            sleep_secs = settings.poll_interval_seconds
        await asyncio.sleep(sleep_secs)
