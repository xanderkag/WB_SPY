"""Тонкий async-клиент к публичным JSON-эндпоинтам Wildberries.

Используем только то, что отдаёт сам сайт под капотом. Никакого Playwright/
headless. Контракты могут меняться — клиент специально терпим к структуре
ответа: если поля нет, возвращаем None, не падаем.
"""
from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterable

import httpx
from loguru import logger

from .config import settings


CATALOG_HOSTS = [
    "https://catalog.wb.ru",
]
SEARCH_HOSTS = [
    "https://search.wb.ru",
]
CARD_HOSTS = [
    "https://card.wb.ru",
]
STATIC_BASKET = "https://static-basket-01.wbbasket.ru"


def _mask_proxy(url: str) -> str:
    """Прячем пароль из логов."""
    import re
    return re.sub(r"://[^:]+:[^@]+@", "://***:***@", url)


def _extract_products(data: Any) -> list[dict]:
    """WB разные эндпоинты отдают products по разным путям:
    sellers/v4: {"products": [...], "total": N}
    catalog/<shard>/v4 и search: {"data": {"products": [...]}}.
    Поддерживаем оба."""
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("products"), list):
        return data["products"]
    inner = data.get("data")
    if isinstance(inner, dict) and isinstance(inner.get("products"), list):
        return inner["products"]
    return []


def _safe_json(content: bytes, url: str) -> Any:
    """WB временами шлёт невалидный JSON (NaN/незакавыченные ключи).
    Сначала пробуем строгий, потом нестрогий, потом «обрезать до последней закрывающей»."""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(content, strict=False)
    except json.JSONDecodeError as e:
        # дампим первый битый ответ для разбора
        from pathlib import Path
        dump = Path("/tmp/wb_bad_response.txt")
        if not dump.exists():
            dump.write_bytes(content[:50000])
            logger.warning("битый JSON от {}, дамп в {}", url, dump)
        logger.warning("JSON parse fail at {}: {} (len={})", url, e, len(content))
        return None


def _default_headers() -> dict[str, str]:
    return {
        "User-Agent": settings.user_agent,
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Origin": "https://www.wildberries.ru",
        "Referer": "https://www.wildberries.ru/",
    }


@dataclass(slots=True)
class WbProduct:
    nm_id: int
    name: str
    brand: str
    supplier_id: int | None
    supplier_name: str | None
    subject_id: int | None
    price: float | None        # «обычная» цена в рублях
    sale_price: float | None   # цена со скидкой в рублях
    in_stock: bool

    @classmethod
    def from_api(cls, p: dict[str, Any]) -> "WbProduct | None":
        nm = p.get("id") or p.get("nm_id") or p.get("nmId")
        if not nm:
            return None
        sizes = p.get("sizes") or []
        # цена: WB отдаёт salePriceU/priceU в копейках, ИЛИ современные поля
        # в sizes[].price.product/sizes[].price.basic (тоже копейки) — берём максимум доступного
        price_u = p.get("priceU")
        sale_u = p.get("salePriceU")
        if not price_u or not sale_u:
            for s in sizes:
                pr = s.get("price") or {}
                price_u = price_u or pr.get("basic")
                sale_u = sale_u or pr.get("product") or pr.get("total")
                if price_u and sale_u:
                    break
        price = (price_u / 100) if price_u else None
        sale_price = (sale_u / 100) if sale_u else None
        in_stock = any((stock.get("qty") or 0) > 0
                       for s in sizes for stock in (s.get("stocks") or []))
        return cls(
            nm_id=int(nm),
            name=str(p.get("name") or "").strip(),
            brand=str(p.get("brand") or "").strip(),
            supplier_id=p.get("supplierId"),
            supplier_name=p.get("supplier"),
            subject_id=p.get("subjectId") or p.get("subject"),
            price=price,
            sale_price=sale_price,
            in_stock=in_stock,
        )


class WbClient:
    def __init__(self, http2: bool = True, timeout: float = 15.0) -> None:
        proxy = settings.proxy_url or None
        self._client = httpx.AsyncClient(
            http2=http2,
            timeout=timeout,
            headers=_default_headers(),
            follow_redirects=True,
            proxy=proxy,
        )
        if proxy:
            logger.info("httpx через прокси: {}", _mask_proxy(proxy))

    async def __aenter__(self) -> "WbClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def _get_json(self, url: str, params: dict | None = None) -> Any:
        if settings.use_curl_cffi:
            from .wb_cffi import WbCffi
            c = await WbCffi.get()
            return await c.fetch_json(url, params)
        if settings.use_playwright:
            from .wb_browser import WbBrowser
            br = await WbBrowser.get()
            return await br.fetch_json(url, params)

        attempt = 0
        while True:
            attempt += 1
            try:
                resp = await self._client.get(url, params=params)
                if resp.status_code == 200 and resp.content:
                    return _safe_json(resp.content, url)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError(
                        f"retryable {resp.status_code}", request=resp.request, response=resp
                    )
                logger.warning("GET {} -> {}", url, resp.status_code)
                return None
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                if attempt >= 4:
                    logger.error("GET {} failed after {} attempts: {}", url, attempt, e)
                    return None
                backoff = (2 ** attempt) + random.uniform(0, 0.5)
                logger.info("retry {} in {:.1f}s ({})", url, backoff, e)
                await asyncio.sleep(backoff)

    # ---------- discovery ----------

    async def main_menu(self) -> Any:
        """Дерево категорий с subject_id."""
        return await self._get_json(f"{STATIC_BASKET}/vol0/data/main-menu-ru-ru-v3.json")

    async def search(self, query: str, page: int = 1) -> list[dict]:
        url = f"{SEARCH_HOSTS[0]}/exactmatch/ru/common/v13/search"
        params = {
            "appType": 1,
            "curr": "rub",
            "dest": settings.wb_dest,
            "lang": "ru",
            "page": page,
            "query": query,
            "resultset": "catalog",
            "sort": "popular",
            "spp": 30,
            "suppressSpellcheck": "false",
        }
        data = await self._get_json(url, params=params)
        if not data:
            return []
        return _extract_products(data)

    # ---------- catalog by category ----------

    async def category_page(
        self, shard: str, cat: int, xsubject: int | None, page: int
    ) -> list[dict]:
        url = f"{CATALOG_HOSTS[0]}/catalog/{shard}/v4/catalog"
        params = {
            "ab_testing": "false",
            "appType": 1,
            "cat": cat,
            "curr": "rub",
            "dest": settings.wb_dest,
            "lang": "ru",
            "page": page,
            "sort": "popular",
            "spp": 30,
        }
        if xsubject is not None:
            params["xsubject"] = xsubject
        data = await self._get_json(url, params=params)
        if not data:
            return []
        return _extract_products(data)

    async def iter_category_products(
        self, shard: str, cat: int, xsubject: int | None, max_pages: int = 50
    ) -> AsyncIterator[WbProduct]:
        for page in range(1, max_pages + 1):
            raw = await self.category_page(shard, cat, xsubject, page)
            if not raw:
                return
            for p in raw:
                prod = WbProduct.from_api(p)
                if prod:
                    yield prod
            await asyncio.sleep(random.uniform(0.2, 0.6))

    # ---------- catalog by seller ----------

    async def seller_catalog_page(
        self, supplier_id: int, xsubject: int | None, page: int
    ) -> list[dict]:
        url = f"{CATALOG_HOSTS[0]}/sellers/v4/catalog"
        params = {
            "ab_testing": "false",
            "appType": 1,
            "curr": "rub",
            "dest": settings.wb_dest,
            "lang": "ru",
            "page": page,
            "sort": "popular",
            "spp": 30,
            "supplier": supplier_id,
            "uclusters": 0,
        }
        if xsubject is not None:
            params["xsubject"] = xsubject
        data = await self._get_json(url, params=params)
        if not data:
            return []
        return _extract_products(data)

    async def iter_seller_products(
        self, supplier_id: int, xsubject: int | None, max_pages: int = 50
    ) -> AsyncIterator[WbProduct]:
        for page in range(1, max_pages + 1):
            raw = await self.seller_catalog_page(supplier_id, xsubject, page)
            if not raw:
                return
            for p in raw:
                prod = WbProduct.from_api(p)
                if prod:
                    yield prod
            # дёргать слишком быстро — не надо
            await asyncio.sleep(random.uniform(0.2, 0.6))

    # ---------- batch detail by nm ----------

    async def cards_detail(self, nm_ids: Iterable[int]) -> list[WbProduct]:
        nm_list = list(nm_ids)
        out: list[WbProduct] = []
        # WB принимает ~100 nm на запрос
        for i in range(0, len(nm_list), 100):
            chunk = nm_list[i:i + 100]
            url = f"{CARD_HOSTS[0]}/cards/v2/detail"
            params = {
                "appType": 1,
                "curr": "rub",
                "dest": settings.wb_dest,
                "spp": 30,
                "nm": ";".join(str(n) for n in chunk),
            }
            data = await self._get_json(url, params=params)
            if not data:
                continue
            for p in _extract_products(data):
                prod = WbProduct.from_api(p)
                if prod:
                    out.append(prod)
            await asyncio.sleep(random.uniform(0.2, 0.5))
        return out
