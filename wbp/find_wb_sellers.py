"""Discover supplier IDs продавца Wildberries (всех его юр.лиц).

Идём по 4 целевым категориям через catalog/<shard>/v4/catalog,
собираем уникальные supplier_id + supplier_name, фильтруем те, где имя
матчится с WB-like regex.

Запуск: python -m wbp.find_wb_sellers
"""
from __future__ import annotations

import asyncio
import random
import re
from collections import defaultdict

from loguru import logger

from .config import CATEGORIES
from .wb_api import WbClient

# группы продавцов с короткими тегами для удобства
TARGETS: dict[str, re.Pattern] = {
    "WB": re.compile(
        r"(wildberries|вайлдберри|вайлберри|вайл\s*берри|"
        r"rvb|рвб|wb\s+sellers|wb\s+market)",
        re.IGNORECASE,
    ),
    "DHAUS": re.compile(r"(д\s*хаус|dhaus|d[-_\s]house|digital[\s-]?house)", re.IGNORECASE),
    "ECOMM": re.compile(r"электронная\s+коммерция", re.IGNORECASE),
    "MTS": re.compile(
        r"(мтс|^mts\b|\bmts\b|мтс[-\s]?маркет|мтс[-\s]?цифров|мобильные\s+телесистем)",
        re.IGNORECASE,
    ),
    "MEGAFON": re.compile(r"(мегафон|megafon)", re.IGNORECASE),
}

PAGES_PER_CATEGORY = 5     # 100 товаров на страницу × 5 = 500 топ-товаров на категорию
PAUSE_BETWEEN_PAGES = (1.5, 3.0)
PAUSE_BETWEEN_CATEGORIES = (8.0, 15.0)


async def main() -> None:
    seller_info: dict[int, dict] = {}
    seller_count: dict[int, int] = defaultdict(int)
    seller_categories: dict[int, set[str]] = defaultdict(set)

    async with WbClient() as wb:
        for ci, cat in enumerate(CATEGORIES):
            logger.info("[{}/{}] сканирую {} (shard={}, cat={})",
                        ci + 1, len(CATEGORIES), cat.name, cat.shard, cat.cat)
            for page in range(1, PAGES_PER_CATEGORY + 1):
                products = await wb.category_page(cat.shard, cat.cat, cat.xsubject, page)
                # первая страница пустая — это часто транзиентный soft-block, а не
                # реально пустая категория. Ретраим несколько раз с паузой.
                if not products and page == 1:
                    for attempt in range(3):
                        wait = 6 + attempt * 6
                        logger.info("  стр 1 пуста — ретрай #{} через {}с", attempt + 1, wait)
                        await asyncio.sleep(wait)
                        products = await wb.category_page(cat.shard, cat.cat, cat.xsubject, page)
                        if products:
                            break
                if not products:
                    logger.info("  страница {} пуста — стоп", page)
                    break
                for p in products:
                    sid = p.get("supplierId")
                    if not sid:
                        continue
                    sname = p.get("supplier") or ""
                    seller_count[sid] += 1
                    seller_categories[sid].add(cat.name)
                    if sid not in seller_info:
                        seller_info[sid] = {"name": sname}
                logger.info("  стр {} → {} товаров (всего продавцов: {})",
                            page, len(products), len(seller_info))
                await asyncio.sleep(random.uniform(*PAUSE_BETWEEN_PAGES))
            # пауза между категориями — иначе словим 429
            if ci < len(CATEGORIES) - 1:
                pause = random.uniform(*PAUSE_BETWEEN_CATEGORIES)
                logger.info("пауза {:.1f}с перед следующей категорией", pause)
                await asyncio.sleep(pause)

    print(f"\n{'=' * 80}")
    print(f"найдено уникальных продавцов: {len(seller_info)}")
    print(f"{'=' * 80}\n")

    matches: dict[str, list] = defaultdict(list)
    for sid, info in seller_info.items():
        name = info["name"]
        for tag, pat in TARGETS.items():
            if pat.search(name):
                matches[tag].append((sid, name, seller_count[sid], sorted(seller_categories[sid])))

    all_ids = set()
    for tag in TARGETS:
        rows = matches.get(tag, [])
        if not rows:
            print(f"❌ {tag}: не найдено")
            continue
        rows.sort(key=lambda x: -x[2])
        print(f"\n⭐ {tag}: {len(rows)} продавцов")
        print(f"{'supplierId':>11}  {'тов.':>5}  {'категории':<30}  название")
        print("-" * 95)
        for sid, name, cnt, cats in rows:
            print(f"{sid:>11}  {cnt:>5}  {','.join(cats):<30}  {name!r}")
            all_ids.add(sid)

    if all_ids:
        print(f"\n\n✅ Найдено по фильтрам (всего {len(all_ids)}):")
        print(f"WB_SELF_SUPPLIER_IDS = {sorted(all_ids)}")
    else:
        print("\n❌ По именам-фильтрам никого.")

    # ВСЕГДА печатаем топ продавцов — вдруг нужный есть, но regex не поймал.
    print(f"\n{'─' * 80}")
    print("ТОП-25 продавцов в выборке (для ручной проверки — вдруг кого пропустили):")
    print(f"{'supplierId':>11}  {'тов.':>5}  название")
    print("─" * 60)
    for sid, cnt in sorted(seller_count.items(), key=lambda x: -x[1])[:25]:
        print(f"  {sid:>11}  {cnt:>5}  {seller_info[sid]['name']!r}")
    print(f"\n⚠️  Покрыто категорий: только те где была выдача. Если ноутбуки/"
          f"смартфоны дали 0 — список НЕ полный, добавляй продавцов вручную "
          f"через админку по их /seller/<id>.")


if __name__ == "__main__":
    asyncio.run(main())
