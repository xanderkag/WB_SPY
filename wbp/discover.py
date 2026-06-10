"""Discovery: один раз дёрнуть, чтобы заполнить config.py реальными ID.

Использование:
    python -m wbp.discover subjects     # печатает subject_id для 4 категорий
    python -m wbp.discover suppliers    # ищет supplierId продавца WB

Логика supplier-поиска:
1. для каждой категории берём первые N страниц популярных товаров через search,
2. собираем уникальные пары (supplierId, supplierName),
3. печатаем те, где имя похоже на «Wildberries» / «Вайлдберриз» / «РВБ» / «RVB».
"""
from __future__ import annotations

import asyncio
import re
import sys
from collections import defaultdict

from loguru import logger

from .wb_api import WbClient


WB_NAME_RE = re.compile(
    r"(wildberries|вайлдберри|вайлберри|вайл\s*берри|вб\b|rvb|рвб)",
    re.IGNORECASE,
)

CATEGORY_QUERIES = ["ноутбук", "смартфон", "планшет", "умные часы"]


async def find_subjects() -> None:
    async with WbClient() as wb:
        menu = await wb.main_menu()
    if not menu:
        print("main-menu.json не отдался", file=sys.stderr)
        return

    targets = ["ноутбук", "смартфон", "планшет", "умные час", "смарт-час"]
    hits: list[tuple[str, int]] = []

    def walk(nodes: list[dict], path: str = "") -> None:
        for n in nodes:
            name = n.get("name", "")
            full = f"{path} > {name}" if path else name
            lname = name.lower()
            if any(t in lname for t in targets):
                # в main-menu это shard_key + id (бывает разной формы)
                sid = n.get("id") or n.get("subject_id")
                if sid:
                    hits.append((full, int(sid)))
            childs = n.get("childs") or n.get("nodes") or []
            if childs:
                walk(childs, full)

    walk(menu if isinstance(menu, list) else menu.get("children", []))
    if not hits:
        print("Категории не найдены в main-menu. Структура изменилась.")
        return
    print("# Скопируй в config.py → SUBJECTS:")
    print("SUBJECTS = {")
    for path, sid in hits:
        print(f'    {path!r}: {sid},')
    print("}")


async def find_suppliers(pages: int = 3) -> None:
    suppliers: dict[int, dict] = {}
    counts: dict[int, int] = defaultdict(int)
    async with WbClient() as wb:
        for q in CATEGORY_QUERIES:
            for page in range(1, pages + 1):
                products = await wb.search(q, page=page)
                logger.info("query={} page={} -> {} products", q, page, len(products))
                for p in products:
                    sid = p.get("supplierId")
                    sname = p.get("supplier") or ""
                    if not sid:
                        continue
                    counts[sid] += 1
                    if sid not in suppliers:
                        suppliers[sid] = {"name": sname, "rating": p.get("supplierRating")}

    wb_like: list[tuple[int, dict]] = [
        (sid, info) for sid, info in suppliers.items()
        if WB_NAME_RE.search(info["name"] or "")
    ]
    print(f"\nВсего уникальных продавцов: {len(suppliers)}")
    print(f"Похожих на WB: {len(wb_like)}\n")

    if wb_like:
        print("# Скопируй в config.py → WB_SELF_SUPPLIER_IDS:")
        print("WB_SELF_SUPPLIER_IDS = [")
        for sid, info in sorted(wb_like, key=lambda x: -counts[x[0]]):
            print(f'    {sid},  # {info["name"]!r} (карточек в выборке: {counts[sid]})')
        print("]")
    else:
        print("Не нашёл селлера, похожего на WB, в выборке. "
              "Попробуй увеличить pages или показать топ-10 крупнейших:")
        top = sorted(suppliers.items(), key=lambda x: -counts[x[0]])[:10]
        for sid, info in top:
            print(f"  {sid}: {info['name']!r} ({counts[sid]} карточек)")


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "subjects":
        await find_subjects()
    elif cmd == "suppliers":
        await find_suppliers()
    else:
        print(__doc__)


if __name__ == "__main__":
    asyncio.run(main())
