"""Детектор резких падений цены.

Правило: если текущая sale_price ≤ median(sale_price за DROP_WINDOW_HOURS) *
(1 - DROP_THRESHOLD_PCT/100), И в окне есть как минимум DROP_MIN_POINTS точек,
И последний алёрт по этому товару был раньше чем DROP_DEDUP_HOURS назад —
возвращаем событие.

Median, а не average — устойчивее к редким выбросам (например, краткий
«прогрев» цены за 5 минут перед скидкой).
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass

from .config import settings
from . import db


@dataclass(slots=True)
class DropEvent:
    nm_id: int
    median_price: float
    current_price: float
    drop_pct: float    # положительное число, например 12.5 = упало на 12.5%
    kind: str = "median"  # 'median' или 'target' (целевая цена сработала)


async def detect_target(nm_id: int, current_price: float) -> DropEvent | None:
    """Целевая цена: если current ≤ цели и мы не уведомляли о ней последние
    DROP_DEDUP_HOURS — fire."""
    s = await db.get_detector_settings()
    target = await db.get_target(nm_id)
    if target is None or current_price > target:
        return None
    last = await db.last_target_notified(nm_id)
    if last is not None:
        dedup_start = int(time.time()) - int(s["drop_dedup_hours"]) * 3600
        if last >= dedup_start:
            return None
    pct_below = (target - current_price) / target * 100 if target > 0 else 0
    return DropEvent(
        nm_id=nm_id,
        median_price=target,
        current_price=current_price,
        drop_pct=pct_below,
        kind="target",
    )


async def detect_drop(nm_id: int, current_price: float) -> DropEvent | None:
    if current_price is None or current_price <= 0:
        return None

    s = await db.get_detector_settings()
    window_start = int(time.time()) - int(s["drop_window_hours"]) * 3600
    prices = await db.fetch_window_prices(nm_id, window_start)
    if len(prices) < int(s["drop_min_points"]):
        return None

    median = statistics.median(prices)
    if median <= 0:
        return None

    threshold = median * (1 - float(s["drop_threshold_pct"]) / 100)
    if current_price > threshold:
        return None

    last_ts = await db.last_alert_ts(nm_id)
    if last_ts is not None:
        dedup_start = int(time.time()) - int(s["drop_dedup_hours"]) * 3600
        if last_ts >= dedup_start:
            return None

    drop_pct = (median - current_price) / median * 100
    return DropEvent(
        nm_id=nm_id,
        median_price=median,
        current_price=current_price,
        drop_pct=drop_pct,
    )
