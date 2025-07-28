"""
app/routers/catalog_view.py
--------------------------

This router encapsulates all callback handlers related to navigating the
specialist/portfolio catalogue. Previously these handlers lived in
``main.py`` directly, which cluttered that module and mixed catalogue
logic with other unrelated features.  To improve maintainability
and mirror the organisation used for the flea‑market (Барахолка)
functionality, the catalogue navigation handlers have been extracted
into their own router.

Handlers defined here implement a two‑level menu for browsing
categories within a selected city and viewing items (portfolios) in
leaf categories.  A separate router (``catalog_add.py``) handles
creation of new portfolio applications.

Usage:
    from app.routers.catalog_view import router as catalog_view_router
    dp.include_router(catalog_view_router)

The router listens for the following callback_data patterns:

* ``go_catalog`` – entry point into the catalogue from the main menu
* ``citysel:<slug>`` – a city has been chosen; shows root categories
* ``cat:<city_slug>:<cat_slug>`` – navigate into a specific category;
  either lists child categories or displays items in a leaf
* ``catalog:back`` – return to the initial catalogue menu

Note: This module uses helper functions from ``app.routers.utils`` for
safe message editing and cleanup, as well as keyboards from
``app.keyboards``.
"""

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

from app.keyboards import (
    catalog_inline_initial,
    catalog_city_inline,
    get_common_menu_button,
)
from app.routers.utils import (
    last_bot_messages,
    clear_bot_messages,
    safe_edit_or_send,
    city_by_slug,
    children_of,
)
from app.models import Category, Item
from sqlalchemy import select
from app.database import SessionLocal
from app.texts import get_text

router = Router(name="catalog_view")


@router.callback_query(F.data == "go_catalog")
async def go_catalog(cb: CallbackQuery, state: FSMContext) -> None:
    """Entry point into the specialists/portfolio catalogue.

    Clears previous bot messages and displays the initial catalogue menu
    consisting of city choices and the option to submit a new portfolio
    application.  The actual form handling lives in ``catalog_add.py``.
    """
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    markup = await catalog_inline_initial()
    text = await get_text("catalog_choose_city", "ru") or "🏙 Каталог – выберите город:"
    await safe_edit_or_send(cb, text, markup)
    await cb.answer()


@router.callback_query(F.data.startswith("citysel:"))
async def city_selected(cb: CallbackQuery) -> None:
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    slug = cb.data.split(":", 1)[1]
    city = await city_by_slug(slug)
    roots = await children_of(None)
    header = f"<b>Каталог → {city.name}</b>"
    markup = await catalog_city_inline(slug, roots)
    msg = await cb.bot.send_message(cb.message.chat.id, header, reply_markup=markup, parse_mode="HTML")
    last_bot_messages[cb.message.chat.id] = [msg.message_id]   # <-- Это нужно!
    await cb.answer()




@router.callback_query(F.data.startswith("cat:"))
async def cat_handler(cb: CallbackQuery) -> None:
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    _, city_slug, cat_slug = cb.data.split(":", 2)
    city = await city_by_slug(city_slug)
    async with SessionLocal() as session:
        cat = (await session.execute(
            select(Category).where(Category.slug == cat_slug)
        )).scalar_one()
    children = await children_of(cat.id)
    # build breadcrumb и находим parent_cat_slug
    names = [cat.name]
    parent_cat_slug = None
    cur = cat
    while cur.parent_id:
        async with SessionLocal() as session:
            p = (await session.execute(
                select(Category).where(Category.id == cur.parent_id)
            )).scalar_one()
        names.append(p.name)
        if not parent_cat_slug:
            parent_cat_slug = p.slug
        cur = p
    path = " → ".join(reversed(names))
    header = f"<b>Каталог → {city.name} → {path}</b>"

    # универсальный блок кнопок для children и items
    buttons = []
    # кнопки самих подкатегорий, если они есть
    if children:
        for child in children:
            buttons.append([InlineKeyboardButton(text=child.name, callback_data=f"cat:{city_slug}:{child.slug}")])

    if parent_cat_slug:
        # Назад к родителю
        back_callback = f"cat:{city_slug}:{parent_cat_slug}"
    else:
        # Назад к списку городов
        back_callback = f"citysel:{city_slug}"

    back_btn = await get_common_menu_button('back')
    if back_btn:
        buttons.append([InlineKeyboardButton(text=back_btn.text, callback_data=back_callback)])

    main_menu_btn = await get_common_menu_button('main_menu')
    if main_menu_btn:
        buttons.append([InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data)])

    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    if children:
        msg = await cb.bot.send_message(cb.message.chat.id, header, reply_markup=markup, parse_mode="HTML")
        last_bot_messages[cb.message.chat.id] = [msg.message_id]
    else:
        # Leaf category: show items if any
        async with SessionLocal() as session:
            items = (await session.execute(
                select(Item)
                .where(Item.city_id == city.id, Item.category_id == cat.id, Item.is_approved.is_(True))
                .order_by(Item.created_at.desc())
            )).scalars().all()
        text = header
        if not items:
            text += "\n\nПока нет анкет."
        else:
            parts = []
            for i in items:
                title = i.title
                descr = i.descr or ""
                contact = i.contact
                parts.append(f"• <b>{title}</b>\n{descr}\n<code>{contact}</code>")
            text += "\n\n" + "\n\n".join(parts)
        msg = await cb.bot.send_message(cb.message.chat.id, text, reply_markup=markup, parse_mode="HTML")
        last_bot_messages[cb.message.chat.id] = [msg.message_id]
    await cb.answer()


@router.callback_query(F.data == "catalog:back")
async def catalog_back(cb: CallbackQuery) -> None:
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    markup = await catalog_inline_initial()
    text = await get_text("catalog_choose_city", "ru") or "🏙 Каталог – выберите город:"
    msg = await cb.bot.send_message(cb.message.chat.id, text, reply_markup=markup, parse_mode="HTML")
    last_bot_messages[cb.message.chat.id] = [msg.message_id]
    await cb.answer()
