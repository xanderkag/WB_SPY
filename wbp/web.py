"""Web API + static SPA для просмотра состояния парсера на телефоне/десктопе.

Запуск:
    uvicorn wbp.web:app --host 0.0.0.0 --port 8000

Тогда открыть:
- на маке:   http://localhost:8000
- на iPhone: http://<IP-мака-в-Wi-Fi>:8000  (см. System Settings → Network)

Web читает ту же wb.sqlite3 что и парсер. Парсер можно при этом не останавливать.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import settings


app = FastAPI(title="WB Parser", docs_url="/api/docs", redoc_url=None)

ROOT = Path(__file__).parent
STATIC = ROOT / "static"


_SCHEMA_APPLIED = False


def _db() -> sqlite3.Connection:
    global _SCHEMA_APPLIED
    db = sqlite3.connect(settings.db_path)
    db.row_factory = sqlite3.Row
    if not _SCHEMA_APPLIED:
        # синхронно прогоняем схему один раз — на случай если коллектор стартовал
        # на старой версии БД, а мы добавили новые таблицы.
        from . import db as _dbmod
        try:
            db.executescript(_dbmod.SCHEMA)
            db.commit()
        except Exception as e:
            print("schema apply warn:", e)
        # одноразовая миграция: если tracked_suppliers пустой — заливаем из config.
        try:
            from .config import WB_SELF_SUPPLIER_IDS
            c = db.execute("SELECT COUNT(*) FROM tracked_suppliers").fetchone()[0]
            if c == 0 and WB_SELF_SUPPLIER_IDS:
                now = int(time.time())
                db.executemany(
                    "INSERT OR IGNORE INTO tracked_suppliers(supplier_id, active, created_at) VALUES (?, 1, ?)",
                    [(sid, now) for sid in WB_SELF_SUPPLIER_IDS],
                )
                db.commit()
        except Exception as e:
            print("supplier migration warn:", e)
        _SCHEMA_APPLIED = True
    return db


def _img_urls(nm_id: int) -> list[str]:
    """Возвращает список кандидатов URL картинки.
    Стратегия: сначала пробуем basket, рассчитанный приблизительной формулой
    под vol → basket, потом соседние. На фронте фоллбэк по списку с таймаутом."""
    short = nm_id // 1000
    vol = short // 100
    part = short
    # эмпирическая таблица WB vol → basket-NN (на 2026). Для свежих nm 700M+ vol ~7-9k.
    ranges = [
        (0, 143, 1), (144, 287, 2), (288, 431, 3), (432, 719, 4),
        (720, 1007, 5), (1008, 1061, 6), (1062, 1115, 7), (1116, 1169, 8),
        (1170, 1313, 9), (1314, 1601, 10), (1602, 1655, 11), (1656, 1919, 12),
        (1920, 2045, 13), (2046, 2189, 14), (2190, 2405, 15), (2406, 2621, 16),
        (2622, 2837, 17), (2838, 3053, 18), (3054, 3269, 19), (3270, 3485, 20),
        (3486, 3845, 21), (3846, 4321, 22), (4322, 4581, 23), (4582, 5125, 24),
        (5126, 5429, 25), (5430, 5703, 26), (5704, 6313, 27), (6314, 6886, 28),
        (6887, 7563, 29), (7564, 8169, 30), (8170, 8438, 31), (8439, 999999, 32),
    ]
    best = 30
    for lo, hi, b in ranges:
        if lo <= vol <= hi:
            best = b
            break
    # отдаём в порядке: рассчитанный, ±1, ±2, потом остальные ближайшие
    order = [best, best + 1, best - 1, best + 2, best - 2, best + 3, best - 3, best + 4]
    seen = set()
    urls = []
    for b in order:
        if 1 <= b <= 32 and b not in seen:
            seen.add(b)
            urls.append(f"https://basket-{b:02d}.wbbasket.ru/vol{vol}/part{part}/{nm_id}/images/c246x328/1.webp")
    return urls


def _img(nm_id: int) -> str:
    """Совместимость: возвращаем первую ссылку из списка (для use в alerts/history)."""
    return _img_urls(nm_id)[0]


# ---------- API ----------

@app.get("/api/stats")
def api_stats():
    with _db() as db:
        c = db.execute
        prods = c("SELECT COUNT(*) FROM products").fetchone()[0]
        snaps = c("SELECT COUNT(*) FROM price_snapshots").fetchone()[0]
        alerts = c("SELECT COUNT(*) FROM alerts").fetchone()[0]
        suppliers = c("SELECT COUNT(DISTINCT supplier_id) FROM products").fetchone()[0]
        last_ts = c("SELECT MAX(ts) FROM price_snapshots").fetchone()[0]
        first_ts = c("SELECT MIN(ts) FROM price_snapshots").fetchone()[0]
        subs = c("SELECT COUNT(*) FROM subscribers WHERE active=1").fetchone()[0]
        per_supplier = [
            dict(r) for r in c(
                "SELECT supplier_id, supplier_name, COUNT(*) AS n "
                "FROM products GROUP BY supplier_id, supplier_name ORDER BY n DESC"
            ).fetchall()
        ]
    return {
        "products": prods,
        "snapshots": snaps,
        "alerts": alerts,
        "suppliers": suppliers,
        "subscribers": subs,
        "last_tick_ts": last_ts,
        "first_tick_ts": first_ts,
        "now": int(time.time()),
        "by_supplier": per_supplier,
    }


@app.get("/api/products")
def api_products(
    supplier: int | None = None,
    brand: str | None = None,
    q: str | None = None,
    limit: int = 200,
    sort: str = "price_desc",
):
    sort_sql = {
        "price_desc":  "ORDER BY last_price DESC",
        "price_asc":   "ORDER BY last_price ASC",
        "name":        "ORDER BY p.name",
        "newest":      "ORDER BY p.first_seen_at DESC",
        "biggest_drop":"ORDER BY drop_pct_24h DESC NULLS LAST",
    }.get(sort, "ORDER BY last_price DESC")

    where = []
    params: list[Any] = []
    if supplier:
        where.append("p.supplier_id = ?"); params.append(supplier)
    if brand:
        where.append("p.brand = ?"); params.append(brand)
    if q:
        where.append("p.name LIKE ?"); params.append(f"%{q}%")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # окно 24ч для drop_pct
    window_start = int(time.time()) - 24 * 3600

    sql = f"""
        SELECT p.nm_id, p.name, p.brand, p.supplier_id, p.supplier_name, p.subject_id,
               (SELECT sale_price FROM price_snapshots
                 WHERE nm_id = p.nm_id ORDER BY ts DESC LIMIT 1) AS last_price,
               (SELECT ts FROM price_snapshots
                 WHERE nm_id = p.nm_id ORDER BY ts DESC LIMIT 1) AS last_ts,
               (SELECT COUNT(*) FROM price_snapshots
                 WHERE nm_id = p.nm_id AND ts >= {window_start}) AS points_24h,
               (SELECT MAX(sale_price) FROM price_snapshots
                 WHERE nm_id = p.nm_id AND ts >= {window_start}) AS max_24h,
               (SELECT MIN(sale_price) FROM price_snapshots
                 WHERE nm_id = p.nm_id AND ts >= {window_start}) AS min_24h
        FROM products p
        {where_sql}
        {sort_sql.replace('drop_pct_24h', '((max_24h - last_price) * 100.0 / NULLIF(max_24h, 0))')}
        LIMIT ?
    """
    params.append(limit)
    with _db() as db:
        rows = [dict(r) for r in db.execute(sql, params).fetchall()]
        # одним запросом подтягиваем sparkline-данные (цены 24ч) для всех nm
        if rows:
            nm_ids = [r["nm_id"] for r in rows]
            placeholders = ",".join("?" * len(nm_ids))
            snap_rows = db.execute(
                f"SELECT nm_id, sale_price FROM price_snapshots "
                f"WHERE nm_id IN ({placeholders}) AND ts >= ? AND sale_price IS NOT NULL "
                f"ORDER BY nm_id, ts ASC",
                [*nm_ids, window_start],
            ).fetchall()
            by_nm: dict[int, list[float]] = {}
            for sr in snap_rows:
                by_nm.setdefault(sr["nm_id"], []).append(sr["sale_price"])
            for r in rows:
                r["spark"] = by_nm.get(r["nm_id"], [])
            # «лучшая цена за всё время» и «за 30 дней» — батчем
            d30 = int(time.time()) - 30 * 86400
            stats_rows = db.execute(
                f"""SELECT nm_id,
                          MIN(sale_price) AS min_all,
                          MIN(CASE WHEN ts >= ? THEN sale_price END) AS min_30d,
                          (SELECT MIN(ts) FROM price_snapshots ps2
                            WHERE ps2.nm_id = price_snapshots.nm_id) AS first_ts
                    FROM price_snapshots
                    WHERE nm_id IN ({placeholders}) AND sale_price IS NOT NULL
                    GROUP BY nm_id""",
                [d30, *nm_ids],
            ).fetchall()
            stats = {r["nm_id"]: dict(r) for r in stats_rows}
            # таргеты
            t_rows = db.execute(
                f"SELECT nm_id, target_price FROM target_prices WHERE nm_id IN ({placeholders})",
                nm_ids,
            ).fetchall()
            targets = {r["nm_id"]: r["target_price"] for r in t_rows}
            now = int(time.time())
            for r in rows:
                s = stats.get(r["nm_id"], {})
                r["min_all"] = s.get("min_all")
                r["min_30d"] = s.get("min_30d")
                r["days_tracked"] = max(
                    1,
                    (now - (s.get("first_ts") or now)) // 86400
                )
                r["target_price"] = targets.get(r["nm_id"])
    # подтягиваем watchlist одним SET для star-индикации
    with _db() as db:
        watched = {row["nm_id"] for row in db.execute(
            "SELECT nm_id FROM watchlist"
        ).fetchall()}
    for r in rows:
        if r["max_24h"] and r["last_price"] is not None and r["max_24h"] > 0:
            r["drop_pct_24h"] = round((r["max_24h"] - r["last_price"]) * 100.0 / r["max_24h"], 1)
        else:
            r["drop_pct_24h"] = None
        r["img"] = _img(r["nm_id"])
        r["img_urls"] = _img_urls(r["nm_id"])
        r["wb_url"] = f"https://www.wildberries.ru/catalog/{r['nm_id']}/detail.aspx"
        r["watched"] = r["nm_id"] in watched
    return {"items": rows}


@app.get("/api/products/{nm_id}/history")
def api_history(nm_id: int):
    with _db() as db:
        prod = db.execute(
            "SELECT * FROM products WHERE nm_id = ?", (nm_id,)
        ).fetchone()
        if not prod:
            raise HTTPException(404)
        rows = db.execute(
            "SELECT ts, sale_price, in_stock FROM price_snapshots "
            "WHERE nm_id = ? ORDER BY ts ASC", (nm_id,),
        ).fetchall()
    return {
        "product": dict(prod),
        "img": _img(nm_id),
        "img_urls": _img_urls(nm_id),
        "wb_url": f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx",
        "snapshots": [{"ts": r["ts"], "price": r["sale_price"], "in_stock": r["in_stock"]} for r in rows],
    }


@app.get("/api/alerts")
def api_alerts(limit: int = 50):
    with _db() as db:
        rows = db.execute(
            """
            SELECT a.id, a.nm_id, a.ts, a.median_price, a.current_price, a.drop_pct,
                   COALESCE(a.kind, 'median') AS kind,
                   p.name, p.brand, p.supplier_name
            FROM alerts a LEFT JOIN products p USING(nm_id)
            ORDER BY a.ts DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["img"] = _img(d["nm_id"])
        d["img_urls"] = _img_urls(d["nm_id"])
        d["wb_url"] = f"https://www.wildberries.ru/catalog/{d['nm_id']}/detail.aspx"
        items.append(d)
    return {"items": items}


# ---------- suppliers ----------

class SupplierIn(BaseModel):
    supplier_id: int = Field(..., gt=0)
    alias: str | None = None


@app.get("/api/suppliers")
def api_suppliers():
    with _db() as db:
        rows = db.execute(
            """SELECT t.id, t.supplier_id, t.alias, t.active, t.created_at,
                      (SELECT supplier_name FROM products WHERE supplier_id = t.supplier_id LIMIT 1) AS detected_name,
                      (SELECT COUNT(*) FROM products WHERE supplier_id = t.supplier_id) AS products_n
               FROM tracked_suppliers t
               ORDER BY t.id"""
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/suppliers")
def api_supplier_add(s: SupplierIn):
    with _db() as db:
        try:
            db.execute(
                "INSERT INTO tracked_suppliers(supplier_id, alias, active, created_at) "
                "VALUES (?, ?, 1, ?)",
                (s.supplier_id, s.alias, int(time.time())),
            )
            db.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(409, "уже есть")
    return {"ok": True}


@app.delete("/api/suppliers/{tid}")
def api_supplier_delete(tid: int):
    with _db() as db:
        db.execute("DELETE FROM tracked_suppliers WHERE id = ?", (tid,))
        db.commit()
    return {"ok": True}


@app.post("/api/suppliers/{tid}/toggle")
def api_supplier_toggle(tid: int):
    with _db() as db:
        db.execute(
            "UPDATE tracked_suppliers SET active = 1 - active WHERE id = ?", (tid,)
        )
        db.commit()
    return {"ok": True}


# ---------- targets ----------

class TargetIn(BaseModel):
    nm_id: int = Field(..., gt=0)
    target_price: float = Field(..., gt=0)


@app.post("/api/targets")
def api_target_set(t: TargetIn):
    with _db() as db:
        db.execute(
            """INSERT INTO target_prices(nm_id, target_price, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(nm_id) DO UPDATE SET
                 target_price = excluded.target_price,
                 last_notified_ts = NULL""",
            (t.nm_id, t.target_price, int(time.time())),
        )
        db.commit()
    return {"ok": True}


@app.delete("/api/targets/{nm_id}")
def api_target_del(nm_id: int):
    with _db() as db:
        db.execute("DELETE FROM target_prices WHERE nm_id = ?", (nm_id,))
        db.commit()
    return {"ok": True}


# ---------- watchlist ----------

@app.get("/api/watchlist")
def api_watchlist():
    """Список избранного с обогащением — последние цены и базовая инфа."""
    window_start = int(time.time()) - 24 * 3600
    with _db() as db:
        rows = db.execute(
            """
            SELECT w.nm_id, w.created_at,
                   p.name, p.brand, p.supplier_id, p.supplier_name, p.subject_id,
                   (SELECT sale_price FROM price_snapshots
                     WHERE nm_id = p.nm_id ORDER BY ts DESC LIMIT 1) AS last_price,
                   (SELECT ts FROM price_snapshots
                     WHERE nm_id = p.nm_id ORDER BY ts DESC LIMIT 1) AS last_ts,
                   (SELECT MAX(sale_price) FROM price_snapshots
                     WHERE nm_id = p.nm_id AND ts >= ?) AS max_24h
            FROM watchlist w LEFT JOIN products p USING(nm_id)
            ORDER BY w.created_at DESC
            """, (window_start,),
        ).fetchall()
    items = []
    nm_ids = [r["nm_id"] for r in rows]
    stats: dict[int, dict] = {}
    targets: dict[int, float] = {}
    if nm_ids:
        with _db() as db:
            ph = ",".join("?" * len(nm_ids))
            d30 = int(time.time()) - 30 * 86400
            for sr in db.execute(
                f"""SELECT nm_id,
                          MIN(sale_price) AS min_all,
                          MIN(CASE WHEN ts >= ? THEN sale_price END) AS min_30d,
                          (SELECT MIN(ts) FROM price_snapshots ps2 WHERE ps2.nm_id = price_snapshots.nm_id) AS first_ts
                    FROM price_snapshots WHERE nm_id IN ({ph}) AND sale_price IS NOT NULL
                    GROUP BY nm_id""",
                [d30, *nm_ids],
            ).fetchall():
                stats[sr["nm_id"]] = dict(sr)
            for tr in db.execute(
                f"SELECT nm_id, target_price FROM target_prices WHERE nm_id IN ({ph})", nm_ids
            ).fetchall():
                targets[tr["nm_id"]] = tr["target_price"]
    now = int(time.time())
    for r in rows:
        d = dict(r)
        if d.get("max_24h") and d.get("last_price") and d["max_24h"] > 0:
            d["drop_pct_24h"] = round((d["max_24h"] - d["last_price"]) * 100.0 / d["max_24h"], 1)
        else:
            d["drop_pct_24h"] = None
        s = stats.get(d["nm_id"], {})
        d["min_all"] = s.get("min_all")
        d["min_30d"] = s.get("min_30d")
        d["days_tracked"] = max(1, (now - (s.get("first_ts") or now)) // 86400)
        d["target_price"] = targets.get(d["nm_id"])
        d["img"] = _img(d["nm_id"])
        d["img_urls"] = _img_urls(d["nm_id"])
        d["wb_url"] = f"https://www.wildberries.ru/catalog/{d['nm_id']}/detail.aspx"
        d["watched"] = True
        items.append(d)
    return {"items": items}


class WatchIn(BaseModel):
    nm_id: int = Field(..., gt=0)


@app.post("/api/watchlist")
def api_watch_add(w: WatchIn):
    with _db() as db:
        db.execute(
            "INSERT OR IGNORE INTO watchlist(nm_id, created_at) VALUES (?, ?)",
            (w.nm_id, int(time.time())),
        )
        db.commit()
    return {"ok": True}


@app.delete("/api/watchlist/{nm_id}")
def api_watch_del(nm_id: int):
    with _db() as db:
        db.execute("DELETE FROM watchlist WHERE nm_id = ?", (nm_id,))
        db.commit()
    return {"ok": True}


# ---------- static ----------

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


# Service Worker должен быть доступен из корня, иначе у него ограниченный scope.
@app.get("/sw.js")
def sw():
    return FileResponse(STATIC / "sw.js", media_type="application/javascript")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
