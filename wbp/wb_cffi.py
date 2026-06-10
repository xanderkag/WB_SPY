"""curl_cffi async-клиент с TLS-impersonation Chrome.

Идея: WB-WAF умеет распознавать httpx / requests по TLS-fingerprint (JA3/JA4)
и резать. curl_cffi через libcurl-impersonate отправляет хэндшейк, идентичный
настоящему Chrome. Часто это снимает PoW-челлендж и rate-limit.

Singleton: одна session на процесс, переиспользует cookies.
"""
from __future__ import annotations

import json
from typing import Any

from loguru import logger

try:
    from curl_cffi.requests import AsyncSession
except ImportError as e:  # pragma: no cover
    raise RuntimeError("Поставь: pip install curl_cffi") from e


_DEFAULT_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Origin": "https://www.wildberries.ru",
    "Referer": "https://www.wildberries.ru/",
}


class WbCffi:
    _instance: "WbCffi | None" = None

    def __init__(self) -> None:
        self._session: AsyncSession | None = None

    @classmethod
    async def get(cls) -> "WbCffi":
        if cls._instance is None:
            cls._instance = cls()
            await cls._instance._start()
        return cls._instance

    async def _start(self) -> None:
        from .config import settings as _s
        from .wb_api import _mask_proxy

        kw: dict = {
            "impersonate": "chrome124",  # TLS-fingerprint Chrome 124
            "timeout": 15,
            "headers": _DEFAULT_HEADERS,
        }
        if _s.proxy_url:
            kw["proxies"] = {"http": _s.proxy_url, "https": _s.proxy_url}
            logger.info("curl_cffi через прокси: {}", _mask_proxy(_s.proxy_url))
        logger.info("стартую curl_cffi AsyncSession (impersonate=chrome124)")
        self._session = AsyncSession(**kw)

    async def fetch_json(self, url: str, params: dict | None = None) -> Any:
        import asyncio, random
        assert self._session is not None
        for attempt in range(1, 5):
            try:
                r = await self._session.get(url, params=params)
            except Exception as e:
                logger.error("curl_cffi GET {} crashed (attempt {}): {}", url, attempt, e)
                if attempt >= 4:
                    return None
                await asyncio.sleep(2 ** attempt + random.uniform(0, 0.5))
                continue
            if r.status_code == 200 and r.content:
                try:
                    return json.loads(r.content)
                except json.JSONDecodeError as e:
                    preview = r.text[:200] if r.text else ""
                    logger.warning("curl_cffi JSON parse fail {}: {} preview={!r}", url, e, preview)
                    return None
            # retryable: 429, 5xx, 0 (TCP)
            if r.status_code in (429, 500, 502, 503, 504):
                backoff = (4 ** attempt) + random.uniform(0, 1.5)  # 4, 16, 64, 256s
                logger.warning("curl_cffi GET {} -> {} retry #{} в {:.0f}с",
                               url, r.status_code, attempt, backoff)
                if attempt >= 4:
                    return None
                await asyncio.sleep(backoff)
                continue
            # non-retryable
            preview = r.text[:200] if r.text else ""
            logger.warning("curl_cffi GET {} -> {} body={!r}", url, r.status_code, preview)
            return None
        return None

    async def close(self) -> None:
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass
        WbCffi._instance = None
