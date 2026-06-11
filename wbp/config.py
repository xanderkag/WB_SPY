from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    tg_bot_token: str = ""
    tg_admin_ids: str = ""

    wb_dest: int = -1257786
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    db_path: Path = Path("./wb.sqlite3")

    poll_interval_seconds: int = 600

    use_playwright: bool = False
    use_curl_cffi: bool = False

    # WEB_API_ONLY=1 — web обслуживает только API+PWA, НЕ поднимает collector и
    # Telegram-бота (их держит отдельный worker: `python -m wbp.cli loop`).
    # Защита от двойного парсера и конфликта Telegram getUpdates (409).
    web_api_only: bool = False

    # Прокси: пусто = без прокси (или системный HTTPS_PROXY).
    # Форматы: http://user:pass@host:port, http://host:port, socks5://host:port.
    proxy_url: str = ""

    drop_threshold_pct: float = 10.0
    drop_window_hours: int = 24
    drop_dedup_hours: int = 6
    drop_min_points: int = 6

    @property
    def admin_ids(self) -> List[int]:
        if not self.tg_admin_ids.strip():
            return []
        return [int(x.strip()) for x in self.tg_admin_ids.split(",") if x.strip()]


settings = Settings()


# ----- Targets -----
# supplierId-ы продавца WB. Заполняется после успешного discovery (см. NOTES.md).
# Известные кандидаты по информации на 2026: «ООО Вайлдберриз», «РВБ»/RVB.
# Пока пусто — collector работает в режиме «обход категорий целиком», собирает
# уникальных supplier'ов в логе и пишет всё в БД.
WB_SELF_SUPPLIER_IDS: list[int] = [
    250090328,  # R2D2 — https://www.wildberries.ru/seller/250090328
    560794641,  # https://www.wildberries.ru/seller/560794641
]


@dataclass(frozen=True, slots=True)
class Category:
    name: str
    shard: str       # WB catalog shard, напр. "electronic43"
    cat: int         # id ветки меню (параметр &cat=)
    xsubject: int | None  # subject_id (параметр &xsubject=). None — фильтр по cat.


# Извлечено из main-menu-ru-ru-v3.json (2026-06-09):
CATEGORIES: list[Category] = [
    Category("ноутбуки",   "electronic43", cat=9492, xsubject=2290),
    Category("смартфоны",  "electronic84", cat=9463, xsubject=515),
    Category("планшеты",   "electronic84", cat=9477, xsubject=517),
    Category("смарт-часы", "electronic58", cat=9845, xsubject=None),
]
