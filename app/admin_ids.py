# app/admin_ids.py
"""Единый список Telegram ID администраторов бота.

Раньше был захардкожен отдельно в admin_panel.py и feedback.py — смена
администратора требовала править оба места, легко разойтись. Источник
правды теперь один: env/.env ADMIN_IDS (через тот же config_value, что и
BOT_TOKEN), с дефолтом для совместимости при первом запуске.
"""

from __future__ import annotations

from pathlib import Path

from app.db_path import config_value

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_ADMIN_IDS = "519335258"


def _parse_admin_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids


ADMIN_IDS: list[int] = _parse_admin_ids(
    config_value(_ROOT, "ADMIN_IDS", _DEFAULT_ADMIN_IDS) or _DEFAULT_ADMIN_IDS
)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS
