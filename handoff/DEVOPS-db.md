# DevOps хендофф: база данных WB Spy

Дата снимка: 2026-06-11 13:05 · отдаёт: dev · принимает: DevOps

## TL;DR

- СУБД: **SQLite 3** (файл), режим **WAL**. Не Postgres, не сервер — просто файл.
- В деплое (docker-compose) живёт по пути **`/data/wb.sqlite3`** на volume **`wbspy-data`**.
- Готовый консистентный снимок для заливки: **`handoff/wb-export.sqlite3`** (212 КБ).
- Залить = положить этот файл как `/data/wb.sqlite3` в volume до старта контейнеров.
- Схема **создаётся и мигрируется сама** при старте приложения — отдельный migration-тул не нужен.

## Что в снимке

| таблица | строк | назначение |
|---|---|---|
| `products` | 131 | карточки товаров (nm_id, имя, бренд, supplier_id, subject_id) |
| `price_snapshots` | 3261 | история цен — по строке на каждое наблюдение, основной объём |
| `alerts` | 0 | сработавшие алёрты (дропы / целевые цены) |
| `tracked_suppliers` | 2 | каких продавцов мониторим (250090328 R2D2 + 1 пустой) |
| `watchlist` | 0 | избранное пользователя |
| `target_prices` | 0 | целевые цены |
| `subscribers` | 0 | подписчики Telegram-бота |
| `mutes` | 0 | заглушённые товары по юзерам |
| `app_settings` | 0 | runtime-конфиг (в т.ч. tg_bot_token если введён через UI) |

Файл: 212 КБ. Растёт ~линейно от `price_snapshots`: ≈130 строк/тик, тик раз в 30 мин
→ ~6 тыс строк/сутки → порядка 1–2 МБ/месяц на одного продавца. Для горизонта в годы
SQLite хватает с запасом.

## Артефакты в этой папке

| файл | что |
|---|---|
| `wb-export.sqlite3` | **консистентный снимок** (WAL свёрнут через `VACUUM INTO`). Это и заливать. |
| `wb-dump.sql` | plain-text SQL дамп (`.dump`) — для глаз / переноса в другую СУБД |
| `DEVOPS-db.md` | этот документ |

sha256 снимка:
```
d8e4f37dc14fdb07185b6af84b3505608dc3fa6cb3850ccc592b1dfc83475fb7  wb-export.sqlite3
```

## ⚠️ Почему НЕ копировать `wb.sqlite3` напрямую

В WAL-режиме рабочий файл `wb.sqlite3` в любой момент содержит НЕ все данные —
часть лежит в `wb.sqlite3-wal` (на момент снятия там было ~955 КБ незакоммиченного).
Голый `cp wb.sqlite3` потеряет свежие снапшоты. Поэтому отдаём `VACUUM INTO`-снимок,
который уже всё свёл в один файл. При снятии своих копий в проде — тоже только так:

```bash
sqlite3 /data/wb.sqlite3 "VACUUM INTO '/backup/wb-$(date +%F).sqlite3'"
# или: sqlite3 /data/wb.sqlite3 ".backup '/backup/wb.sqlite3'"
```

(Приложение и само делает это раз в сутки → `/data/backups/wb-YYYYMMDD.sqlite3`,
ротация 14 копий — см. `db.maybe_daily_backup()`.)

## Как залить в деплой

```bash
# 1. создать volume (если ещё нет)
docker volume create wbspy-data

# 2. положить снимок внутрь volume как /data/wb.sqlite3
docker run --rm -v wbspy-data:/data -v "$PWD/handoff":/in alpine \
  sh -c "cp /in/wb-export.sqlite3 /data/wb.sqlite3 && ls -la /data"

# 3. поднять
docker compose -p wbspy up -d --build
```

При первом старте приложение прогонит `SCHEMA` (idempotent, `CREATE TABLE IF NOT
EXISTS` + мягкие `ALTER`) — существующие данные не трогаются.

## Схема (DDL)

```sql
CREATE TABLE products (
    nm_id INTEGER PRIMARY KEY, name TEXT, brand TEXT,
    supplier_id INTEGER, supplier_name TEXT, subject_id INTEGER,
    first_seen_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL);
CREATE INDEX idx_products_supplier ON products(supplier_id);
CREATE INDEX idx_products_subject  ON products(subject_id);

CREATE TABLE price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, nm_id INTEGER NOT NULL,
    price REAL, sale_price REAL, in_stock INTEGER NOT NULL, ts INTEGER NOT NULL);
CREATE INDEX idx_snap_nm_ts ON price_snapshots(nm_id, ts);

CREATE TABLE alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, nm_id INTEGER NOT NULL, ts INTEGER NOT NULL,
    median_price REAL NOT NULL, current_price REAL NOT NULL, drop_pct REAL NOT NULL,
    kind TEXT DEFAULT 'median');
CREATE INDEX idx_alerts_nm_ts ON alerts(nm_id, ts);

CREATE TABLE tracked_suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, supplier_id INTEGER NOT NULL UNIQUE,
    alias TEXT, active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL);

CREATE TABLE watchlist (nm_id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL);
CREATE TABLE target_prices (
    nm_id INTEGER PRIMARY KEY, target_price REAL NOT NULL,
    created_at INTEGER NOT NULL, last_notified_ts INTEGER);
CREATE TABLE subscribers (
    tg_user_id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE mutes (
    tg_user_id INTEGER NOT NULL, nm_id INTEGER NOT NULL, until_ts INTEGER,
    PRIMARY KEY (tg_user_id, nm_id));
CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER NOT NULL);
```

Все `ts` / `*_at` — Unix epoch (целые секунды, UTC).
Цены — рубли (REAL). `sale_price` — цена со скидкой (то что видит покупатель),
`price` — базовая. Детектор работает по `sale_price`.

## Конфиг подключения

| env | дефолт | в деплое |
|---|---|---|
| `DB_PATH` | `./wb.sqlite3` | `/data/wb.sqlite3` (задан в Dockerfile и compose) |

Приложение открывает БД через `aiosqlite.connect(DB_PATH)` и сразу ставит
`PRAGMA journal_mode=WAL`. Никаких логина/пароля/порта — это файл.

## Архитектура web+worker (флаги — ЗАШИТЫ)

Два сервиса из одного образа пишут в один SQLite на volume `wbspy-data`:
- `web`   — `uvicorn wbp.web:app`, **`WEB_API_ONLY=1`** → только API+PWA
- `worker`— `python -m wbp.cli loop` → collector loop + Telegram-бот

1. **Двойной коллектор/бот — РЕШЕНО** (коммит `9a51a43`). При `WEB_API_ONLY=1`
   web НЕ поднимает collector/bot (проверено: 0 тиков, бот `managed_by=worker`).
   `docker-compose.yml` уже содержит `WEB_API_ONLY: "1"` на сервисе web.
   Бот живёт только в worker → конфликта Telegram 409 нет.
   - ⚠️ Токен через UI кладётся в БД (`app_settings`). worker читает его на старте
     (`db.effective_bot_token()`: БД → env). После ввода токена в вебе —
     **рестартни `wbspy-worker`**, чтобы бот подключился. (API `/api/bot/token`
     возвращает это в поле `note`.)

2. **SQLite + общий volume.** WAL между процессами на ЛОКАЛЬНОМ volume — ок.
   Сетевой (NFS/SMB) ломает WAL. На Asha локальный SSD → ок (подтверждено DevOps).

## Health-check (для CI/мониторинга)

Эндпоинт **`GET /healthz`** (коммит см. ниже) — лёгкий, без рендера HTML:
```json
{"status":"ok","mode":"full|api-only","products":131,"last_tick_age_sec":1593}
```
- 200 + `status:ok` — БД доступна.
- 503 + `status:degraded` — БД недоступна.
- `last_tick_age_sec` — возраст последнего снимка цен; если на worker'е он растёт
  бесконтрольно (> ~2× POLL_INTERVAL) — парсер встал (VPN отвалился / WAF).

Бить health лучше в `/healthz`, а не в `/` (тот рендерит весь HTML).
mode у `web` будет `api-only`; реальный парсинг проверяется по worker'у —
у него `/healthz` нет (он не web), смотри `last_tick_age_sec` через web (общая БД).

## Перенос в Postgres (если когда-то понадобится)

Схема плоская, без хитрых типов. `wb-dump.sql` читаемый. Маппинг:
`INTEGER PRIMARY KEY AUTOINCREMENT` → `SERIAL/BIGSERIAL`, `REAL` → `double precision`,
epoch `INTEGER` оставить как `bigint` (или `to_timestamp()`). Объём смешной —
переносится `pgloader` или руками за полчаса. Пока в этом нет нужды: SQLite тянет.
