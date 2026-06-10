# wb-parser — статус и план продолжения

> Дата паузы: 2026-06-09 (вечер). Остановлены из-за WAF-блокировки IP в WB.

## Что сделано
- Скелет на Python 3.11 + httpx + aiosqlite + loguru. Telegram-бот отложен.
- Структура:
  - `wbp/config.py` — настройки + два пустых списка `WB_SELF_SUPPLIER_IDS`, `SUBJECTS`.
  - `wbp/wb_api.py` — async-клиент (search / catalog / sellers / cards / basket).
  - `wbp/db.py` — SQLite со схемой products / price_snapshots / alerts / subscribers / mutes.
  - `wbp/detector.py` — медиана 24ч, dedup 6ч, мин. 6 точек, порог 10%.
  - `wbp/collector.py` — цикл опроса.
  - `wbp/discover.py` — поиск subject_id и supplierId.
  - `wbp/cli.py` — консольный entrypoint (`once` / `loop`), алёрты в stdout.
  - `wbp/bot.py` — Telegram, на потом.
- venv создан в `.venv` (Python 3.11.15), зависимости установлены.

## Что узнали о WB API
- `static-basket-01.wbbasket.ru/vol0/data/main-menu-ru-ru-v3.json` — отдаётся без защиты.
  - Извлекли `shard` + `subject` для нужных категорий:

    | Категория | shard | xsubject | cat |
    |---|---|---|---|
    | Ноутбуки | electronic43 | 2290 | 9492 |
    | Смартфоны | electronic84 | 515 | 9463 |
    | Планшеты | electronic84 | 517 | 9477 |
    | Смарт-часы и браслеты | electronic58 | — | 9845 |

- Endpoints проверены и **работают БЕЗ Proof-of-Work** в принципе:
  - `https://catalog.wb.ru/catalog/<shard>/v4/catalog?...&cat=<id>&xsubject=<id>`
  - `https://catalog.wb.ru/sellers/v4/catalog?supplier=<id>&...`
- Endpoints, которые **используют PoW (x-pow header) или TCP-режут наш IP**:
  - `search.wb.ru/exactmatch/.../search` → 429 + `x-pow` в `access-control-allow-headers`.
  - `card.wb.ru/cards/v2/detail` → TCP timeout.
  - `www.wildberries.ru/catalog/<nm>/detail.aspx` → TCP timeout.

## Текущая блокировка
Наш IP в WAF WB. После пары запросов через `search.wb.ru` мы получили:
1. Жёсткий 429 на `search.wb.ru` с требованием PoW.
2. TCP-drop на `card.wb.ru` и `www.wildberries.ru`.
3. **Soft block** на `catalog.wb.ru` — 200 + `{"products":[],"total":0}` на любые валидные параметры.

Это означает, что без обхода (VPN / прокси / Playwright) данных не будет.

## Финальный диагноз 2026-06-09 (Playwright не помог)
- Поставили Playwright + Chromium (`.venv` с playwright уже), переписали `wb_api._get_json` на переключатель: `USE_PLAYWRIGHT=1` → ходим через `BrowserContext.request`.
- `wbp/wb_browser.py` — singleton-обёртка, реалистичный UA + cookies через `goto wildberries.ru`, fingerprint-маскировка.
- Результат: `goto wildberries.ru → ERR_TIMED_OUT` (TCP отказ), `catalog.wb.ru/sellers/v4/catalog → 403 Forbidden` от nginx (Angie).
- **Вывод: бан на уровне IP**, фингерпринт/PoW тут ни при чём. Playwright и httpx упираются в одну и ту же стену.
- Единственный путь — смена IP (VPN, residential proxy). Дать через `playwright launch(proxy=...)` если будет proxy URL.

Supplier ID, который запросил пользователь: **250090328** (https://www.wildberries.ru/seller/250090328) — прописан в `WB_SELF_SUPPLIER_IDS`.

## План на завтра
1. Проверить что блок снят: `curl -s 'https://catalog.wb.ru/sellers/v4/catalog?appType=1&curr=rub&dest=-1257786&page=1&sort=popular&spp=30&supplier=100329'`
   - Должно вернуть **непустой** список товаров (>25 байт ответа). Если 25 байт `{"products":[],"total":0}` — IP всё ещё в блоке.
2. Если разблокирован — запустить discovery каталогом (не search-ем, search мы больше не трогаем):
   - Дёрнуть `catalog/<shard>/v4/catalog` для 4 категорий, страница 1–3.
   - Собрать уникальные пары `(supplierId, supplier)`.
   - Найти те, где имя соответствует `Wildberries|Вайлдберри|РВБ|RVB`.
   - Записать в `wbp/config.py` → `WB_SELF_SUPPLIER_IDS` и `SUBJECTS`.
3. Запустить `python -m wbp.cli once` — один тик, проверить что в БД появились снапшоты.
4. Если ОК → `python -m wbp.cli loop` для постоянного мониторинга.

Если завтра тоже забанит — переходим на план B: Playwright (`pip install playwright && playwright install chromium`) и переписываем `_get_json` в `wbp/wb_api.py` на хождение через браузер.

## Команды для быстрого продолжения
```bash
cd /Users/alexanderliapustin/Desktop/SLAI/wb-parser
source .venv/bin/activate

# тест блокировки
curl -s --max-time 8 "https://catalog.wb.ru/catalog/electronic43/v4/catalog?appType=1&cat=9492&curr=rub&dest=-1257786&page=1&sort=popular&spp=30&xsubject=2290"
# если ответ > 1 КБ — IP свободен

# discovery каталогом (нужно дописать в wbp/discover.py команду `catalog`)
# пока вручную:
python -c "
import asyncio
from wbp.wb_api import WbClient

async def main():
    async with WbClient() as wb:
        # пробуем endpoint catalog/<shard>/v4/catalog — wb_api.py пока туда не ходит, нужна реализация
        ...
asyncio.run(main())
"
```

## Что ещё TODO в коде
- `wb_api.py`: добавить метод `category_page(shard, cat, xsubject, page)` — он-то и должен качать каталог по категории, а не sellers по продавцу. Сейчас его нет.
- `discover.py`: команду `python -m wbp.discover catalog` — обход 4 категорий через новый метод, поиск WB-подобных suppliers.
- Уточнить, нужен ли `card.wb.ru` вообще в обычной работе. Если `catalog/<shard>/v4` даёт всё что нужно (цена + supplier + sizes), то можно жить без `card.wb.ru` (и не упираться в PoW). Это снимет одну зависимость.
