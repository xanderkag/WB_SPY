"""SQLite-хранилище через aiosqlite.

Схема намеренно минималистичная: products + price_snapshots + subscribers +
mutes + alerts. История цен пишется WIDE: каждое наблюдение — отдельная строка,
median считаем на лету. Этого достаточно до десятков тысяч SKU и сотен тысяч
точек; если упрёмся — переедем в Postgres.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Iterable

import aiosqlite
from loguru import logger

from .config import settings
from .wb_api import WbProduct


SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    nm_id          INTEGER PRIMARY KEY,
    name           TEXT,
    brand          TEXT,
    supplier_id    INTEGER,
    supplier_name  TEXT,
    subject_id     INTEGER,
    first_seen_at  INTEGER NOT NULL,
    last_seen_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_products_supplier ON products(supplier_id);
CREATE INDEX IF NOT EXISTS idx_products_subject  ON products(subject_id);

CREATE TABLE IF NOT EXISTS price_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nm_id       INTEGER NOT NULL,
    price       REAL,
    sale_price  REAL,
    in_stock    INTEGER NOT NULL,
    ts          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snap_nm_ts ON price_snapshots(nm_id, ts);

CREATE TABLE IF NOT EXISTS subscribers (
    tg_user_id  INTEGER PRIMARY KEY,
    created_at  INTEGER NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS mutes (
    tg_user_id  INTEGER NOT NULL,
    nm_id       INTEGER NOT NULL,
    until_ts    INTEGER,
    PRIMARY KEY (tg_user_id, nm_id)
);

CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    nm_id           INTEGER NOT NULL,
    ts              INTEGER NOT NULL,
    median_price    REAL NOT NULL,
    current_price   REAL NOT NULL,
    drop_pct        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_nm_ts ON alerts(nm_id, ts);

-- продавцы которых мониторим (раньше зашивались в config.WB_SELF_SUPPLIER_IDS).
CREATE TABLE IF NOT EXISTS tracked_suppliers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id  INTEGER NOT NULL UNIQUE,
    alias        TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   INTEGER NOT NULL
);

-- избранные товары пользователя (на устройство, не на TG-юзера).
CREATE TABLE IF NOT EXISTS watchlist (
    nm_id        INTEGER PRIMARY KEY,
    created_at   INTEGER NOT NULL
);

-- целевые цены: пользователь задал «уведомить когда упадёт ниже X».
CREATE TABLE IF NOT EXISTS target_prices (
    nm_id              INTEGER PRIMARY KEY,
    target_price       REAL    NOT NULL,
    created_at         INTEGER NOT NULL,
    last_notified_ts   INTEGER
);

-- runtime-настройки приложения (key/value).
-- сейчас тут лежит tg_bot_token чтобы юзер ставил его прямо из UI.
CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  INTEGER NOT NULL
);

-- точечные товары: мониторим конкретный nm_id, может вообще не принадлежать
-- к нашим tracked_suppliers (или мы заранее не знаем продавца).
-- supplier_id заполняется при первом успешном fetch.
CREATE TABLE IF NOT EXISTS tracked_items (
    nm_id        INTEGER PRIMARY KEY,
    alias        TEXT,
    supplier_id  INTEGER,
    name         TEXT,
    brand        TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   INTEGER NOT NULL
);

-- включаем WAL чтобы web и collector не конфликтовали по записи.
PRAGMA journal_mode=WAL;
"""


_DB: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    global _DB
    if _DB is None:
        path = Path(settings.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _DB = await aiosqlite.connect(path)
        _DB.row_factory = aiosqlite.Row
        await _DB.executescript(SCHEMA)
        await _DB.commit()
        await _migrate_initial_suppliers(_DB)
        logger.info("DB ready at {}", path)
    return _DB


async def _migrate_initial_suppliers(db: aiosqlite.Connection) -> None:
    """Если tracked_suppliers пуст — заливаем туда WB_SELF_SUPPLIER_IDS из config."""
    from .config import WB_SELF_SUPPLIER_IDS
    # мягкие ALTER'ы — добавляются один раз, повторный вызов даёт OperationalError.
    for alter in (
        "ALTER TABLE alerts ADD COLUMN kind TEXT DEFAULT 'median'",
        # счётчик подряд пустых тиков, для авто-сна продавца
        "ALTER TABLE tracked_suppliers ADD COLUMN idle_ticks INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE tracked_suppliers ADD COLUMN last_check_ts INTEGER",
    ):
        try:
            await db.execute(alter)
            await db.commit()
            logger.info("миграция: {}", alter[:60])
        except Exception:
            pass  # колонка уже есть
    cur = await db.execute("SELECT COUNT(*) AS c FROM tracked_suppliers")
    row = await cur.fetchone()
    if row["c"] > 0 or not WB_SELF_SUPPLIER_IDS:
        return
    now = int(time.time())
    await db.executemany(
        "INSERT OR IGNORE INTO tracked_suppliers(supplier_id, active, created_at) "
        "VALUES (?, 1, ?)",
        [(sid, now) for sid in WB_SELF_SUPPLIER_IDS],
    )
    await db.commit()
    logger.info("миграция: {} продавцов перенесено из config в БД", len(WB_SELF_SUPPLIER_IDS))


async def close_db() -> None:
    global _DB
    if _DB is not None:
        await _DB.close()
        _DB = None


# ---------- products ----------

async def upsert_products(products: Iterable[WbProduct]) -> int:
    db = await get_db()
    now = int(time.time())
    rows = [
        (p.nm_id, p.name, p.brand, p.supplier_id, p.supplier_name,
         p.subject_id, now, now)
        for p in products
    ]
    if not rows:
        return 0
    await db.executemany(
        """
        INSERT INTO products(nm_id, name, brand, supplier_id, supplier_name,
                             subject_id, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(nm_id) DO UPDATE SET
            name = excluded.name,
            brand = excluded.brand,
            supplier_id = excluded.supplier_id,
            supplier_name = excluded.supplier_name,
            subject_id = excluded.subject_id,
            last_seen_at = excluded.last_seen_at
        """,
        rows,
    )
    await db.commit()
    return len(rows)


# ---------- snapshots ----------

async def insert_snapshots(products: Iterable[WbProduct]) -> int:
    db = await get_db()
    now = int(time.time())
    rows = [
        (p.nm_id, p.price, p.sale_price, 1 if p.in_stock else 0, now)
        for p in products
        if p.sale_price is not None
    ]
    if not rows:
        return 0
    await db.executemany(
        """
        INSERT INTO price_snapshots(nm_id, price, sale_price, in_stock, ts)
        VALUES (?, ?, ?, ?, ?)
        """,
        rows,
    )
    await db.commit()
    return len(rows)


async def fetch_window_prices(nm_id: int, since_ts: int) -> list[float]:
    db = await get_db()
    async with db.execute(
        "SELECT sale_price FROM price_snapshots WHERE nm_id = ? AND ts >= ? AND sale_price IS NOT NULL",
        (nm_id, since_ts),
    ) as cur:
        rows = await cur.fetchall()
    return [r["sale_price"] for r in rows]


async def latest_snapshot(nm_id: int) -> aiosqlite.Row | None:
    db = await get_db()
    async with db.execute(
        "SELECT * FROM price_snapshots WHERE nm_id = ? ORDER BY ts DESC LIMIT 1",
        (nm_id,),
    ) as cur:
        return await cur.fetchone()


async def all_tracked_nm_ids() -> list[int]:
    db = await get_db()
    async with db.execute("SELECT nm_id FROM products") as cur:
        return [r["nm_id"] for r in await cur.fetchall()]


# ---------- alerts ----------

async def last_alert_ts(nm_id: int) -> int | None:
    db = await get_db()
    async with db.execute(
        "SELECT ts FROM alerts WHERE nm_id = ? ORDER BY ts DESC LIMIT 1",
        (nm_id,),
    ) as cur:
        row = await cur.fetchone()
    return row["ts"] if row else None


async def record_alert(nm_id: int, median: float, current: float, drop_pct: float,
                       kind: str = "median") -> None:
    db = await get_db()
    await db.execute(
        "INSERT INTO alerts(nm_id, ts, median_price, current_price, drop_pct, kind) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (nm_id, int(time.time()), median, current, drop_pct, kind),
    )
    await db.commit()


# ---------- targets ----------

async def get_target(nm_id: int) -> float | None:
    db = await get_db()
    async with db.execute(
        "SELECT target_price FROM target_prices WHERE nm_id = ?", (nm_id,)
    ) as cur:
        row = await cur.fetchone()
    return row["target_price"] if row else None


async def last_target_notified(nm_id: int) -> int | None:
    db = await get_db()
    async with db.execute(
        "SELECT last_notified_ts FROM target_prices WHERE nm_id = ?", (nm_id,)
    ) as cur:
        row = await cur.fetchone()
    return row["last_notified_ts"] if row else None


async def touch_target_notified(nm_id: int) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE target_prices SET last_notified_ts = ? WHERE nm_id = ?",
        (int(time.time()), nm_id),
    )
    await db.commit()


# ---------- subscribers / mutes ----------

async def add_subscriber(tg_user_id: int) -> None:
    db = await get_db()
    await db.execute(
        """
        INSERT INTO subscribers(tg_user_id, created_at, active)
        VALUES (?, ?, 1)
        ON CONFLICT(tg_user_id) DO UPDATE SET active = 1
        """,
        (tg_user_id, int(time.time())),
    )
    await db.commit()


async def remove_subscriber(tg_user_id: int) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE subscribers SET active = 0 WHERE tg_user_id = ?",
        (tg_user_id,),
    )
    await db.commit()


async def active_subscribers() -> list[int]:
    db = await get_db()
    async with db.execute("SELECT tg_user_id FROM subscribers WHERE active = 1") as cur:
        return [r["tg_user_id"] for r in await cur.fetchall()]


async def mute(tg_user_id: int, nm_id: int, until_ts: int | None = None) -> None:
    db = await get_db()
    await db.execute(
        """
        INSERT INTO mutes(tg_user_id, nm_id, until_ts) VALUES (?, ?, ?)
        ON CONFLICT(tg_user_id, nm_id) DO UPDATE SET until_ts = excluded.until_ts
        """,
        (tg_user_id, nm_id, until_ts),
    )
    await db.commit()


async def is_muted(tg_user_id: int, nm_id: int) -> bool:
    db = await get_db()
    async with db.execute(
        "SELECT until_ts FROM mutes WHERE tg_user_id = ? AND nm_id = ?",
        (tg_user_id, nm_id),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return False
    until = row["until_ts"]
    if until is None:
        return True
    return int(time.time()) < until


async def maybe_daily_backup() -> str | None:
    """Раз в сутки делает консистентный снимок БД в backups/wb-YYYYMMDD.sqlite3.
    Использует VACUUM INTO — безопасно при работающем WAL, не ломает живую БД.
    Хранит последние 14 бэкапов, старые удаляет. Идемпотентно: если бэкап за
    сегодня уже есть — ничего не делает."""
    src = Path(settings.db_path)
    if not src.exists():
        return None
    backups = src.parent / "backups"
    backups.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    dst = backups / f"wb-{today}.sqlite3"
    if dst.exists():
        return None  # уже есть за сегодня
    db = await get_db()
    try:
        # VACUUM INTO требует, чтобы путь не существовал
        await db.execute(f"VACUUM INTO '{dst.as_posix()}'")
        logger.info("бэкап БД создан: {}", dst)
    except Exception as e:
        logger.warning("бэкап не удался: {}", e)
        return None
    # ротация: оставляем 14 свежих
    snaps = sorted(backups.glob("wb-*.sqlite3"))
    for old in snaps[:-14]:
        try:
            old.unlink()
        except Exception:
            pass
    return str(dst)


async def get_app_setting(key: str) -> str | None:
    db = await get_db()
    async with db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    return row["value"] if row else None


async def effective_bot_token() -> str | None:
    """Токен из БД (задан через UI) с приоритетом, fallback на .env.
    Используется И web, И worker, чтобы UI-токен доходил до обоих."""
    from_db = await get_app_setting("tg_bot_token")
    return from_db or (settings.tg_bot_token or None)


# ─── runtime-настройки детектора (БД → env) ───────────────────────────
# Все ключи которые юзер может менять через UI, и их типы.
DETECTOR_KEYS = {
    "drop_threshold_pct":    ("drop_threshold_pct",    float),
    "drop_window_hours":     ("drop_window_hours",     int),
    "drop_dedup_hours":      ("drop_dedup_hours",      int),
    "drop_min_points":       ("drop_min_points",       int),
    "poll_interval_seconds": ("poll_interval_seconds", int),
}

_settings_cache: dict[str, float | int] = {}
_settings_cache_ts: float = 0


async def get_detector_settings(force: bool = False) -> dict[str, float | int]:
    """Кэшируется на 30 секунд — на тике это норм, на UI достаточно свежо.
    Каждое значение: БД (app_settings) → .env (config.settings) → дефолт."""
    global _settings_cache, _settings_cache_ts
    now = time.time()
    if not force and (now - _settings_cache_ts < 30) and _settings_cache:
        return _settings_cache
    db = await get_db()
    out: dict[str, float | int] = {}
    for key, (env_attr, typ) in DETECTOR_KEYS.items():
        env_default = getattr(settings, env_attr)
        async with db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        val = env_default
        if row and row["value"] not in (None, ""):
            try:
                val = typ(row["value"])
            except (ValueError, TypeError):
                val = env_default
        out[key] = val
    _settings_cache = out
    _settings_cache_ts = now
    return out


def invalidate_settings_cache() -> None:
    global _settings_cache_ts
    _settings_cache_ts = 0


async def active_supplier_ids() -> list[int]:
    """Источник правды для collector — что мониторим прямо сейчас."""
    db = await get_db()
    async with db.execute(
        "SELECT supplier_id FROM tracked_suppliers WHERE active = 1 ORDER BY id"
    ) as cur:
        return [r["supplier_id"] for r in await cur.fetchall()]


async def watchlist_nm_ids() -> set[int]:
    db = await get_db()
    async with db.execute("SELECT nm_id FROM watchlist") as cur:
        return {r["nm_id"] for r in await cur.fetchall()}


@asynccontextmanager
async def lifespan() -> AsyncIterator[None]:
    await get_db()
    try:
        yield
    finally:
        await close_db()
