# WB Spy

Мониторинг цен и наличия товаров продавцов на **Wildberries**. Каждые 30 минут парсер ходит в WB и пишет снапшоты цен. Детектор ловит резкие падения (≥10% от медианы за 24ч) и отправляет уведомления в Telegram-бот. Веб-интерфейс — mobile-first PWA, можно поставить на iPhone «как нативное приложение».

## Возможности

- 🛒 **Сбор каталога продавца** через `catalog.wb.ru/sellers/v4/catalog`.
- 📉 **Детектор дропов**: медиана за 24ч × (1 − 10%), dedup 6ч, минимум 6 точек истории.
- 🎯 **Целевая цена**: задал «уведомить когда упадёт ниже X» — придёт алёрт независимо от медианы.
- ★ **Watchlist** — звёздочка на карточке, отдельный таб «Мои».
- 🔥 **Бейджи лучшей цены**: «минимум за N дней», «минимум за 30д», «цель достигнута».
- 🤖 **Telegram-бот** (aiogram) — `/start`, `/list`, `/mute`, `/status`. Рассылка алёртов всем активным подписчикам.
- 📱 **PWA** — manifest + service worker + apple-touch-icon. На iPhone «Поделиться → На главный экран» → fullscreen-app.
- ⚙️ **CRUD продавцов из UI** — добавил supplierId в табе «Статус», парсер подхватит со следующего тика.

## Архитектура

Весь стек — **один Python-процесс** (`uvicorn wbp.web:app`), который держит коллектор, детектор, бота и веб-API. Состояние — в SQLite. Доступ к WB — через системный VPN на macOS.

```mermaid
graph TB
    User[👤 Юзер<br/>iPhone / Mac<br/>PWA в Safari]

    subgraph Internet["🌐 Интернет (внешние сервисы)"]
        WB["🛒 Wildberries<br/>catalog.wb.ru<br/>sellers/v4/catalog"]
        TG["📨 Telegram API<br/>api.telegram.org"]
        GH["📂 GitHub<br/>xanderkag/WB_SPY"]
        VPNGate["🇯🇵🇷🇺 VPN Gate<br/>(публичные OpenVPN)"]
    end

    subgraph Mac["💻 macOS — твой ноут (на котором всё крутится)"]
        Tunnel["🔐 Tunnelblick<br/>OpenVPN-туннель<br/>(подменяет IP)"]

        subgraph Process["⚙️ uvicorn wbp.web:app — один процесс Python"]
            direction TB
            Collector["⏱ Collector loop<br/>tick раз в 30 мин"]
            CFFI["🌐 curl_cffi<br/>TLS-impersonation<br/>Chrome 124"]
            Detector["📉 Detector<br/>медиана 24ч × -10%<br/>+ target-цены<br/>+ дедуп 6ч"]
            Bot["🤖 Telegram Bot<br/>aiogram polling<br/>/start /list /mute"]
            WebAPI["🟦 FastAPI<br/>/api/products<br/>/api/alerts<br/>/api/watchlist<br/>/api/targets<br/>/api/bot/token<br/>/api/suppliers"]
            SPA["📱 SPA (PWA)<br/>Tailwind + vanilla JS<br/>4 таба, service worker"]
        end

        DB[("💾 SQLite + WAL<br/>wb.sqlite3")]
        Tables["products • price_snapshots<br/>alerts • watchlist<br/>target_prices<br/>tracked_suppliers<br/>app_settings<br/>subscribers • mutes"]
    end

    Collector -->|"запрос со<br/>списком supplier-IDs"| CFFI
    CFFI -->|HTTPS| Tunnel
    Tunnel -.->|"подключение через"| VPNGate
    Tunnel -->|"туннель с RU/JP IP"| WB
    WB -->|"JSON: products,<br/>цены, supplier"| CFFI
    CFFI -->|снимки| Collector
    Collector -->|UPSERT| DB

    Collector --> Detector
    Detector -->|читает историю 24ч| DB
    Detector -.->|"DropEvent<br/>(median / target)"| Bot
    Bot -->|"sendMessage + HTML"| TG
    TG -->|"getUpdates (long-poll)"| Bot
    Bot -->|"subscribers, mutes, chat_id"| DB

    User -->|"http://localhost:8000"| WebAPI
    WebAPI -->|CRUD| DB
    WebAPI -->|"index.html + /static"| SPA
    SPA -.->|"fetch /api/*"| WebAPI
    SPA -->|"тап /start в Telegram"| TG

    User -->|"вставил токен<br/>→ POST /api/bot/token"| WebAPI
    WebAPI -->|"перезапуск polling-task"| Bot
    WebAPI -->|"сохранил в app_settings"| DB

    Process -.->|"git push"| GH
    GH -.->|"git clone на новой машине"| Process

    DB --- Tables
```

### Что происходит за один тик коллектора

```mermaid
sequenceDiagram
    autonumber
    participant Tmr as ⏱ Таймер 30 мин
    participant Col as Collector
    participant DB as SQLite
    participant CFFI as curl_cffi
    participant VPN as Tunnelblick
    participant WB as Wildberries
    participant Det as Detector
    participant Bot as Bot
    participant TG as Telegram API

    Tmr->>Col: tick()
    Col->>DB: SELECT supplier_id<br/>FROM tracked_suppliers WHERE active=1
    DB-->>Col: [250090328, 560794641]

    loop по каждому supplier
        Col->>CFFI: GET sellers/v4/catalog?supplier=...
        CFFI->>VPN: HTTPS через туннель
        VPN->>WB: с RU/JP IP, TLS Chrome 124
        alt WAF доволен
            WB-->>VPN: 200 OK + JSON
            VPN-->>CFFI: products[]
            CFFI-->>Col: список товаров
        else 429 Too Many Requests
            WB-->>CFFI: 429
            Note over CFFI: backoff 4с / 16с / 64с / 256с
            CFFI->>WB: повтор
        end
        Col->>DB: UPSERT products<br/>INSERT price_snapshots
    end

    Col->>Det: проверь каждый товар
    loop по каждому nm_id
        Det->>DB: SELECT sale_price<br/>FROM price_snapshots<br/>WHERE ts >= now - 24ч
        DB-->>Det: история цен
        Det->>DB: SELECT target_price<br/>WHERE nm_id=?
        alt дроп ≥ 10% от медианы<br/>или цена ≤ target
            Det->>DB: INSERT alerts (kind)
            Det->>Bot: DropEvent
            Bot->>DB: SELECT tg_user_id<br/>FROM subscribers WHERE active=1
            loop по подписчикам
                Bot->>TG: sendMessage(chat_id, текст)
                TG-->>Bot: 200 OK
            end
        end
    end
```

## Как работает (обход анти-бота)

WB защищает свой каталог от парсинга:

- `search.wb.ru` — требует Proof-of-Work challenge (`x-pow`).
- `card.wb.ru` и `www.wildberries.ru` — TCP-drop по IP после серии запросов.
- `catalog.wb.ru/sellers/v4/catalog` — **открыт без PoW**, но WAF режет известные дата-центровые/прокси-IP.

Решения в проекте:
1. **`curl_cffi` с TLS-фингерпринтом Chrome 124** — обходит JA3/JA4-фильтрацию, WAF принимает за реальный браузер.
2. **Retry с экспоненциальным backoff** (4с → 16с → 64с → 256с) на 429/5xx.
3. **VPN на стороне macOS** (Tunnelblick + VPN Gate) — единственный реалистичный способ сменить IP, публичные прокси не работают (проверено эмпирически: 0 из 916).

## Стек

- Python 3.11
- httpx + curl_cffi (HTTP-клиенты с TLS-impersonation)
- aiosqlite (SQLite + WAL)
- FastAPI + uvicorn (веб-API)
- aiogram 3.x (Telegram-бот)
- Tailwind CSS via CDN (UI), vanilla JS (no build step)
- loguru (логирование)

## Структура

```
wbp/
├── config.py          — настройки + категории (shard/cat/xsubject)
├── wb_api.py          — высокоуровневый клиент к WB endpoints
├── wb_cffi.py         — curl_cffi backend (Chrome TLS impersonation)
├── wb_browser.py      — Playwright fallback (для случаев когда нужен реальный браузер)
├── db.py              — SQLite-схема + миграции + асинхронные хелперы
├── detector.py        — детектор дропов (median 24ч) и целевых цен
├── collector.py       — главный цикл, опрос продавцов
├── bot.py             — Telegram-бот
├── cli.py             — точка входа (`once` / `loop`), запускает коллектор + бот
├── web.py             — FastAPI: API + раздача SPA
├── discover.py        — поиск subject_id и supplierId через menu
├── find_wb_sellers.py — discovery WB-продавцов по regex (Wildberries/RVB/Мегафон/etc)
└── static/
    ├── index.html     — единая SPA, mobile-first, dark theme
    ├── manifest.webmanifest
    ├── sw.js          — Service Worker для PWA-кэша
    └── icon.svg       — иконка приложения
```

## Установка

```bash
git clone https://github.com/xanderkag/WB_SPY.git
cd WB_SPY

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# заполни TG_BOT_TOKEN в .env (получить у @BotFather)
```

## Запуск

### Парсер + Telegram-бот

```bash
python -m wbp.cli loop
# или в фоне:
nohup python -m wbp.cli loop > wb.log 2>&1 &
```

Парсер тикает каждые `POLL_INTERVAL_SECONDS` (по умолчанию 1800с = 30 мин). При первом старте создаст БД, выполнит миграции и сразу сделает первый тик.

### Веб-интерфейс

```bash
uvicorn wbp.web:app --host 0.0.0.0 --port 8000
```

Доступен на `http://localhost:8000` и (с другого устройства в той же Wi-Fi) на `http://<IP-мака>:8000`.

На iPhone в Safari → **Поделиться → Добавить на экран «Домой»** → ставится как PWA с иконкой.

## Конфигурация (.env)

```
TG_BOT_TOKEN=...           # токен от @BotFather
USE_CURL_CFFI=1            # 1 — TLS Chrome 124 impersonation (рекомендуется)
USE_PLAYWRIGHT=0           # 1 — fallback на Playwright/Chromium (тяжелее)
POLL_INTERVAL_SECONDS=1800 # как часто опрашиваем (30 мин по умолчанию)
DROP_THRESHOLD_PCT=10      # порог падения для алёрта
DROP_WINDOW_HOURS=24       # окно медианы
DROP_DEDUP_HOURS=6         # не алёртить чаще раза в N часов
DROP_MIN_POINTS=6          # минимум точек в окне для срабатывания
PROXY_URL=                 # опционально: http://user:pass@host:port или socks5://...
```

## Откуда брать supplierId

В Telegram-боте можно использовать команды, или через UI:
1. На wildberries.ru открой страницу продавца — URL вида `/seller/250090328`.
2. Скопируй число — это `supplierId`.
3. В табе «Статус» нажми **+ добавить**, вставь.

Парсер подхватит со следующего тика.

## Discovery (если supplierId неизвестен)

```bash
python -m wbp.find_wb_sellers
```

Пройдётся по 4 категориям, найдёт продавцов с именами, похожими на «Wildberries» / «РВБ» / «RVB» / «Мегафон» / «МТС» / «ДХаус» / «Электронная коммерция».

## VPN

В `vpn-configs/` лежат 8 готовых `.ovpn` от VPN Gate (3 RU + 5 JP). Подключаются через **Tunnelblick** (бесплатный GUI для macOS): двойной клик на файл → меню Tunnelblick → Connect.

⚠️ Публичные VPN-серверы логируются, для прода брать платный residential.

## Состояние

- Парсер боевой, гоняет супплайеров каждые 30 мин.
- Web UI готов: каталог, watchlist, целевые цены, статус, управление продавцами.
- PWA устанавливается на iPhone.
- Telegram-бот ловит подписку через `/start`, шлёт алёрты.

## License

MIT
