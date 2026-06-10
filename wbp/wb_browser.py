"""Playwright-обёртка над WB API — для обхода anti-bot (PoW, fingerprint, WAF).

Используем `BrowserContext.request` — это полноценный HTTP-клиент Playwright,
который ходит как настоящий браузер (TLS-фингерпринт Chromium, HTTP/2, заголовки
как у реальной сессии). Он не страдает от CORS и не требует открытой страницы.

Опциональный прогрев: один раз заходим на главную wildberries.ru, чтобы
получить cookies сессии. Если не получилось — продолжаем без них, на голом
фингерпринте.

Singleton: один браузер на процесс. Закрытие — через `await close()`.
"""
from __future__ import annotations

import asyncio
import json
import random
from typing import Any

from loguru import logger

try:
    from playwright.async_api import (
        APIResponse,
        Browser,
        BrowserContext,
        Page,
        async_playwright,
    )
except ImportError as e:  # pragma: no cover
    raise RuntimeError(
        "Playwright не установлен. Запусти: pip install playwright && playwright install chromium"
    ) from e


_WARMUP_URL = "https://www.wildberries.ru/"
_WARMUP_TIMEOUT_MS = 60_000


class WbBrowser:
    _instance: "WbBrowser | None" = None

    def __init__(self) -> None:
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._warmed = False
        self._warmup_lock = asyncio.Lock()

    @classmethod
    async def get(cls) -> "WbBrowser":
        if cls._instance is None:
            cls._instance = cls()
            await cls._instance._start()
        return cls._instance

    async def _start(self) -> None:
        from .config import settings as _settings
        from .wb_api import _mask_proxy
        proxy_kw = {}
        if _settings.proxy_url:
            proxy_kw["proxy"] = {"server": _settings.proxy_url}
            logger.info("Chromium через прокси: {}", _mask_proxy(_settings.proxy_url))
        logger.info("запускаю Chromium через Playwright")
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            **proxy_kw,
        )
        self._context = await self._browser.new_context(
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Origin": "https://www.wildberries.ru",
                "Referer": "https://www.wildberries.ru/",
            },
        )
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined })"
        )

    async def _warmup(self) -> None:
        if self._warmed:
            return
        async with self._warmup_lock:
            if self._warmed:
                return
            assert self._context is not None
            try:
                logger.info("прогрев: открываю {} ({}мс таймаут)", _WARMUP_URL, _WARMUP_TIMEOUT_MS)
                self._page = await self._context.new_page()
                await self._page.goto(
                    _WARMUP_URL, wait_until="domcontentloaded", timeout=_WARMUP_TIMEOUT_MS,
                )
                await asyncio.sleep(random.uniform(2.0, 3.5))
                cookies = await self._context.cookies()
                logger.info("прогрев готов, cookies: {}", len(cookies))
            except Exception as e:
                logger.warning("прогрев упал: {!s:.120} — продолжаем без cookies", e)
            finally:
                self._warmed = True

    async def fetch_json(self, url: str, params: dict | None = None) -> Any:
        """Качаем JSON через context.request — TLS-fingerprint браузера, без CORS."""
        await self._warmup()
        assert self._context is not None

        try:
            resp: APIResponse = await self._context.request.get(
                url,
                params=params,
                headers={"Accept": "*/*"},
                timeout=15_000,
            )
        except Exception as e:
            logger.error("request.get({}) crashed: {}", url, e)
            return None

        body = await resp.body()
        if resp.status != 200:
            preview = body[:200].decode("utf-8", errors="replace")
            logger.warning("GET {} -> {} (body={!r})", url, resp.status, preview)
            return None
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            preview = body[:200].decode("utf-8", errors="replace")
            logger.warning("JSON parse fail {}: {} (preview={!r})", url, e, preview)
            return None

    async def close(self) -> None:
        if self._page:
            try: await self._page.close()
            except Exception: pass
        if self._context:
            try: await self._context.close()
            except Exception: pass
        if self._browser:
            try: await self._browser.close()
            except Exception: pass
        if self._pw:
            try: await self._pw.stop()
            except Exception: pass
        WbBrowser._instance = None
        logger.info("Chromium остановлен")
