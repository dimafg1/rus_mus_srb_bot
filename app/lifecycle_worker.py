# app/lifecycle_worker.py
"""Фоновый процесс жизненного цикла объявлений.

Раз в час:
1. Проставляет expires_at активным объявлениям без срока (страховка).
2. Отправляет владельцам напоминание за REMIND_BEFORE_DAYS дней до архивации
   с кнопкой «Продлить» (один раз, отметка reminded_at).
3. Архивирует просроченные объявления (market/service/vacancy).
4. Архивирует события, прошедшие более суток назад (events + events_meta).

Физического удаления нет сознательно: объёмы малы, аналитика дороже.
Запуск: asyncio.create_task(lifecycle_worker(bot)) в main().
"""

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, text as sql_text

from app import lifecycle as lc
from app.database import SessionLocal
from app.models import Category, City, Listing
from app.routers.utils import get_text

CHECK_INTERVAL_SECONDS = 3600  # раз в час
EVENT_GRACE_SECONDS = 86400    # событие архивируется через сутки после начала

# Префиксы callback кнопки «Продлить» — как в существующих обработчиках
_EXTEND_PREFIX = {
    lc.LISTING_TYPE_MARKET: "market_extend",
    lc.LISTING_TYPE_SERVICE: "service_extend",
    lc.LISTING_TYPE_VACANCY: "vac_extend",
}

log = logging.getLogger("app.lifecycle")


async def lifecycle_worker(bot: Bot) -> None:
    """Бесконечный цикл: тик раз в CHECK_INTERVAL_SECONDS. Ошибки не роняют бот."""
    log.info("lifecycle worker started (interval %ss)", CHECK_INTERVAL_SECONDS)
    while True:
        try:
            await _tick(bot)
        except Exception:
            log.exception("lifecycle tick failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def _tick(bot: Bot) -> None:
    now = lc.utcnow()
    stats = {"expires_set": 0, "reminders": 0, "archived_expired": 0, "archived_events": 0}

    async with SessionLocal() as s:
        listings = (await s.execute(
            select(Listing).where(Listing.status == lc.STATUS_ACTIVE)
        )).scalars().all()

        city_slug = {c.id: c.slug for c in (await s.execute(select(City))).scalars().all()}
        cat_slug = {c.id: c.slug for c in (await s.execute(select(Category))).scalars().all()}

        # 1) страховка: активным без срока — проставить
        for l in listings:
            if lc.ensure_expires_at(l):
                stats["expires_set"] += 1

        # 2) напоминания (до архивации, один раз на объявление).
        # Уже просроченным не напоминаем — их сразу архивирует шаг 3,
        # напоминание «через 0 дней» бессмысленно.
        for l in listings:
            if lc.is_expired(l, now=now):
                continue
            if lc.needs_expiry_reminder(l, now=now):
                sent = await _send_reminder(bot, l, city_slug, cat_slug)
                # Отмечаем в любом случае — чтобы не долбить владельца каждый час,
                # если он заблокировал бота или отправка падает.
                l.reminded_at = now
                if sent:
                    stats["reminders"] += 1

        # 3) архив просроченных
        for l in listings:
            if lc.is_expired(l, now=now):
                lc.archive_as_expired(l, now=now)
                stats["archived_expired"] += 1

        # 4) события, прошедшие более суток назад
        cutoff = int(datetime.now(timezone.utc).timestamp()) - EVENT_GRACE_SECONDS
        res = await s.execute(
            sql_text("SELECT listing_id FROM events_meta WHERE start_at_utc < :cutoff"),
            {"cutoff": cutoff},
        )
        past_ids = {r[0] for r in res.fetchall()}
        for l in listings:
            if lc.is_event(l) and lc.is_active(l) and l.id in past_ids:
                lc.archive_as_event_passed(l, now=now)
                stats["archived_events"] += 1

        await s.commit()

    if any(stats.values()):
        log.info("lifecycle tick: %s", stats)


async def _send_reminder(bot: Bot, listing: Listing, city_slug: dict, cat_slug: dict) -> bool:
    prefix = _EXTEND_PREFIX.get((listing.type or "").strip())
    if not prefix:
        return False

    left = lc.days_left(listing) or 0
    tpl = await get_text(
        "expiry_reminder", "ru",
        default="⏳ Ваше объявление «{title}» будет архивировано через {days} дн.\n"
                "Нажмите кнопку ниже, чтобы продлить его на 30 дней.",
    )
    msg_text = tpl.replace("{title}", listing.title or "").replace("{days}", str(left))

    cslug = city_slug.get(listing.city_id, "-")
    kslug = cat_slug.get(listing.category_id, "-")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🔄 Продлить на 30 дней",
            callback_data=f"{prefix}:{listing.id}:{cslug}:{kslug}:my",
        )
    ]])

    try:
        await bot.send_message(listing.owner_id, msg_text, reply_markup=kb)
        return True
    except Exception as e:
        log.warning("reminder send failed | listing=%s owner=%s | %s", listing.id, listing.owner_id, e)
        return False
