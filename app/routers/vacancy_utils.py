"""
Helper functions and constants for the vacancy section.

This module contains a few helpers used across the vacancy routers.  It
encapsulates keyboard generation for the vacancy main menu, city
selection and category navigation.  Unlike the existing vacancy
implementation in ``keyboards.py`` (which used a fixed list of
categories), this module builds keyboards dynamically from the
database using the shared ``City`` and ``Category`` models.  The
vacancy section reuses the generic ``Listing`` table for storing
job postings; therefore a dedicated vacancy table is not needed.

The root of the vacancy category tree is defined by
``VACANCY_ROOT_CATEGORY_ID``.  Categories with this value as their
``parent_id`` will appear as top‑level categories for vacancies.

All functions in this module return fully constructed
``InlineKeyboardMarkup`` instances ready to be passed to the bot.

"""

from __future__ import annotations

from typing import List, Optional

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from app.database import SessionLocal
from app.models import City, Category
from sqlalchemy import select
from app.keyboards import get_common_menu_button, build_city_buttons
from aiogram.utils.keyboard import InlineKeyboardMarkup, InlineKeyboardButton, InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton

import json

VACANCY_ROOT_CATEGORY_ID = 90

# RU: Клавиатура категорий ДЛЯ ПУБЛИКАЦИИ (не для просмотра).
# Кнопки ведут на vac_add_cat:<city_slug>:<cat_id>, чтобы остаться в мастере публикации.
async def vacancy_categories_inline_add(city_slug: str, parent_id: int | None) -> InlineKeyboardMarkup:
    """
    Только кнопки категорий для режима публикации.
    Никаких лишних пунктов. Навигацию («Назад», «Главное меню») добавляет вызывающий хендлер.
    """
    if parent_id is None:
        parent_id = VACANCY_ROOT_CATEGORY_ID

    async with SessionLocal() as s:
        q = select(Category).where(Category.parent_id == parent_id)
        if hasattr(Category, "order"):
            q = q.order_by(Category.order.asc())
        else:
            q = q.order_by(Category.name.asc())
        cats = (await s.execute(q)).scalars().all()

    kb = InlineKeyboardBuilder()
    for c in cats:
        kb.button(text=c.name, callback_data=f"vac_add_cat:{city_slug}:{c.id}")
    kb.adjust(1)
    return kb.as_markup()



def _flex_to_db(value):
    """
    Сериализует payload гибких полей в строку (JSON) для записи в БД.
    dict/list -> JSON-строка, пустое -> None, остальное -> json.dumps(...)
    """
    if not value:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))

def _flex_from_db(raw):
    """
    Десериализует строку JSON из БД обратно в dict.
    """
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}

# (опционально) явно экспортируем символы модуля
__all__ = (
    "_flex_to_db",
    "_flex_from_db",
    # ... ваши прочие экспортируемые функции, если нужно
)

# Root category id for vacancies.  This ID should exist in the
# ``category`` table and will serve as the parent for all vacancy
# categories.  See ``admin_panel.py`` where this ID is whitelisted
# alongside the roots for the market (30) and services (80).
VACANCY_ROOT_CATEGORY_ID = 90


# Главное меню раздела «Вакансии» (единый стиль; печать трека в конце)
async def vacancy_main_menu(lang: str = "ru") -> InlineKeyboardMarkup:
    city_buttons = await build_city_buttons("vcity", lang)

    keyboard: List[List[InlineKeyboardButton]] = []
    keyboard.append([InlineKeyboardButton(text="Поиск вакансий 🔎", callback_data="vac_search")])
    if city_buttons:
        for i in range(0, len(city_buttons), 2):
            keyboard.append(city_buttons[i:i+2])
    keyboard.append([InlineKeyboardButton(text="📄 Мои вакансии", callback_data="vac:my")])
    keyboard.append([InlineKeyboardButton(text="➕ РАЗМЕСТИТЬ ВАКАНСИЮ", callback_data="vac:new")])

    main_btn = await get_common_menu_button('main_menu', lang)
    if main_btn:
        keyboard.append([main_btn])

    print(
        f"FUNC: vacancy_main_menu | file: app/routers/vacancy_utils.py | "
        f"lang: {lang} | rows: {len(keyboard)} | cities: {len(city_buttons) if city_buttons else 0}"
    )
    return InlineKeyboardMarkup(inline_keyboard=keyboard)



# Короткое RU-описание: строит клавиатуру категорий; по умолчанию ведёт в просмотр (vlist / vcity),
# но допускает переопределение префиксов для мастера публикации (vac_cat / vac_city).
async def vacancy_categories_inline(
    city_slug: str,
    parent_id: int | None,
    cb_prefix: str = "vlist",          # куда переходить по категории: vlist (просмотр) / vac_cat (публикация)
    back_city_prefix: str = "vcity",   # куда ведёт кнопка "Выбрать другой город": vcity / vac_city
) -> InlineKeyboardMarkup:
    """
    Клавиатура категорий вакансий из БД.
    По умолчанию кнопки ведут на vlist:<city_slug>:<cat_id>, чтобы сработал хендлер просмотра.
    Для мастера публикации передайте cb_prefix="vac_cat" и back_city_prefix="vac_city".
    """
    if parent_id is None:
        parent_id = VACANCY_ROOT_CATEGORY_ID

    async with SessionLocal() as s:
        cats = (
            await s.execute(
                select(Category)
                .where(Category.parent_id == parent_id)
                .order_by(Category.order.asc() if hasattr(Category, "order") else Category.name.asc())
            )
        ).scalars().all()

    kb = InlineKeyboardBuilder()

    # Кнопки категорий → <cb_prefix>:<city_slug>:<cat_id>
    for c in cats:
        kb.button(text=c.name, callback_data=f"{cb_prefix}:{city_slug}:{c.id}")

    # Навигация
    # kb.button(text="⬅️ Выбрать другой город", callback_data=f"{back_city_prefix}:{city_slug}")
    kb.button(text="≡ Меню вакансий", callback_data="go_isk")

    main_btn = await get_common_menu_button("main_menu")
    if main_btn:
        kb.row(main_btn)

    kb.adjust(1)
    return kb.as_markup()

VAC_LIST_PAGE_SIZE = 10


async def vacancy_listings_inline(city_slug: str, cat_id: int, listings, offset: int = 0) -> InlineKeyboardMarkup:
    """
    Список вакансий в выбранной категории и городе (с пагинацией).
    Кнопки ведут на: vac_view:<id>:<city_slug>:<cat_id>
    Страницы: vlist:<city_slug>:<cat_id>:<offset>
    """
    # Узнаём родителя текущей категории для корректной кнопки "Назад"
    parent_id = None
    async with SessionLocal() as s:
        cat = (await s.execute(
            select(Category).where(Category.id == cat_id)
        )).scalar_one_or_none()
        if cat:
            parent_id = cat.parent_id

    rows: list[list[InlineKeyboardButton]] = []

    total = len(listings)
    pages = max(1, (total + VAC_LIST_PAGE_SIZE - 1) // VAC_LIST_PAGE_SIZE)
    if offset >= total:
        offset = (pages - 1) * VAC_LIST_PAGE_SIZE
    if offset < 0:
        offset = 0
    page = offset // VAC_LIST_PAGE_SIZE + 1

    if total:
        for l in listings[offset:offset + VAC_LIST_PAGE_SIZE]:
            title = (l.title or "(без заголовка)").strip()
            price = f" — {l.price}" if getattr(l, "price", None) else ""
            rows.append([InlineKeyboardButton(
                text=f"{title}{price}",
                callback_data=f"vac_view:{l.id}:{city_slug}:{cat_id}"
            )])
    else:
        rows.append([InlineKeyboardButton(text="Пока нет вакансий", callback_data="go_isk")])

    if pages > 1:
        pager: list[InlineKeyboardButton] = []
        if offset > 0:
            pager.append(InlineKeyboardButton(
                text="«", callback_data=f"vlist:{city_slug}:{cat_id}:{offset - VAC_LIST_PAGE_SIZE}"))
        pager.append(InlineKeyboardButton(text=f"{page}/{pages}", callback_data="stub"))
        if offset + VAC_LIST_PAGE_SIZE < total:
            pager.append(InlineKeyboardButton(
                text="»", callback_data=f"vlist:{city_slug}:{cat_id}:{offset + VAC_LIST_PAGE_SIZE}"))
        rows.append(pager)

    # Назад на один уровень вверх
    back_btn = await get_common_menu_button('back')
    if parent_id:
        back_btn.callback_data = f"vlist:{city_slug}:{parent_id}"
    else:
        back_btn.callback_data = f"vcity:{city_slug}"
    rows.append([back_btn])

    # Главное меню
    main_btn = await get_common_menu_button('main_menu')
    if main_btn:
        rows.append([main_btn])

    return InlineKeyboardMarkup(inline_keyboard=rows)




async def my_vacancies_inline(listings) -> InlineKeyboardMarkup:
    """
    Список моих вакансий.
    Ведём в карточку с правами владельца: vac_view:<id>:::my
    (пустые city_slug/cat_id, чтобы кнопка «Назад» в карточке вела в меню «Вакансии»)
    """
    kb = InlineKeyboardBuilder()

    for l in listings:
        title = (l.title or "(без заголовка)").strip()
        price = f" — {l.price}" if getattr(l, "price", None) else ""
        # Формат: vac_view:<id>:<city_slug>:<cat_id>[:my]
        # Для «моих» даём 5-й сегмент :my, а city_slug/cat_id оставляем пустыми
        kb.button(
            text=f"{title}{price}",
            callback_data=f"vac_view:{l.id}:::my"
        )

    if not listings:
        kb.button(text="У вас нет вакансий", callback_data="go_isk")

    # Навигация
    kb.button(text="⬅️ В меню вакансий", callback_data="go_isk")
    main_btn = await get_common_menu_button('main_menu')
    if main_btn:
        kb.row(main_btn)

    kb.adjust(1)
    return kb.as_markup()