# app/campaigns.py
"""
Партнёрские кампании: выбор и ротация (Strategy v2, слой 2 §5).

Весь показ — под выключателем partner_rotation_enabled (app/features.py):
выключен флаг или ошибка → партнёрских элементов просто нет, бот работает
как раньше. Ротация — взвешенный случайный выбор среди активных кампаний
места показа с учётом дат. Показ логируется событием partner_shown,
открытие карточки — partner_opened (app/routers/partner_view.py).
"""
import logging
import random
from datetime import datetime
from typing import Optional

from aiogram.types import InlineKeyboardButton
from sqlmodel import select

from app.database import SessionLocal
from app.models import Campaign, utcnow_naive
from app.features import is_enabled
from app.analytics import log_event

log = logging.getLogger("app.campaigns")


async def pick_campaign(
    placement: str = "main_menu", *, user_id: Optional[int] = None
) -> Optional[Campaign]:
    """Выбрать кампанию для места показа. None — показывать нечего."""
    try:
        if not await is_enabled("partner_rotation_enabled", user_id=user_id):
            return None
        async with SessionLocal() as s:
            rows = (await s.execute(
                select(Campaign).where(
                    Campaign.placement == placement,
                    Campaign.active == True,  # noqa: E712
                )
            )).scalars().all()
        now = utcnow_naive()
        rows = [
            c for c in rows
            if (c.starts_at is None or c.starts_at <= now)
            and (c.ends_at is None or c.ends_at >= now)
        ]
        if not rows:
            return None
        weights = [max(c.weight, 1) for c in rows]
        return random.choices(rows, weights=weights, k=1)[0]
    except Exception as e:
        log.warning("pick_campaign(%s) failed: %s", placement, e)
        return None


async def partner_menu_button(
    user_id: Optional[int] = None, placement: str = "main_menu"
) -> Optional[InlineKeyboardButton]:
    """Кнопка-строка партнёра для меню (или None). Логирует показ."""
    c = await pick_campaign(placement, user_id=user_id)
    if c is None:
        return None
    await log_event(
        "partner_shown", user_id=user_id,
        entity_type="campaign", entity_id=c.id,
        source=placement, meta={"campaign": c.key},
    )
    return InlineKeyboardButton(text=c.line_text, callback_data=f"partner:{c.key}")
