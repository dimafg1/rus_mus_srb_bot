# app/routers/market_view.py

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, text
from sqlalchemy.exc import NoResultFound
import logging
import inspect

from app.database import SessionLocal
from app.models import Category, Listing, City
import json
from app.keyboards import (
    market_inline,
    build_main_menu,
    get_common_menu_button,
)
from app.texts import get_text
from app.states import MarketSearch
from app.routers.market_utils import show_market_search_results
from app.routers.utils import (
    clear_bot_messages,
    safe_edit_or_send,
    register_bot_messages,
    last_bot_messages,
    sent_photo_messages,
    last_search_query_message,
    last_search_menu_message,
    my_listing_messages,
    city_by_slug,
    children_of,
    fetch_listings,
    expanded_listing_by_chat,
    listing_message_ids,
    render_flex_block,
    render_main_fields,
    render_contact,
    render_flex_compact,
    render_category_path,
    build_contact_url,
)

from app.search.fuzzy import search_items

from app.analytics.search_log import log_search
from app.analytics.listing_views import log_listing_view

from app.lifecycle import (
    days_left_text,
    should_show_extend_button,
    extend_listing,
)

import os, urllib.parse
from aiogram.types import WebAppInfo

from app.routers.utils_category_title import format_category_title

from app.routers.utils_kb import grid3

from aiogram.filters import StateFilter


router = Router()

WEBAPP_BASE = os.getenv("WEBAPP_BASE", "https://unixound.com/rus_mus_srb_bot").rstrip("/")
USE_CONTACT_REDIRECT = os.getenv("USE_CONTACT_REDIRECT", "1").strip().lower() in {"1", "true", "yes", "on"}

# ─────────────────────────────────────────────────────────
# ФЛАГ «пришли из поиска» по чатам (для корректного «Назад»)
# ─────────────────────────────────────────────────────────
came_from_search_by_chat: dict[int, bool] = {}

# ─────────────────────────────────────────────────────────
# Кэш последнего поиска по чату (устойчив к очистке FSM)
# ─────────────────────────────────────────────────────────
last_search_ctx_by_chat: dict[int, dict] = {}


MARKET_SEARCH_PAGE_SIZE = 10



@router.callback_query(F.data == "go_market")
async def go_market(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)

    text = await get_text("market_choose_action", "ru") or "💸 Flea market – choose action:"
    markup = await market_inline()

    await safe_edit_or_send(cb, text, reply_markup=markup)
    last_bot_messages.setdefault(chat_id, []).append(cb.message.message_id)

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {cb.message.chat.id} | "
        f"user_id: {cb.from_user.id} | "
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )


@router.callback_query(F.data.startswith("mcity:"))
async def market_city(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    slug = cb.data.split(":", 1)[1]

    if slug == "choose":
        markup = await market_inline()
        await safe_edit_or_send(cb, await get_text("market_choose_action", "ru"), reply_markup=markup)
        await cb.answer()
        return

    city = await city_by_slug(slug)
    subs = await children_of(30)

    buttons = []
    for sc in subs:
        title = await format_category_title(sc.id, (sc.name or "").strip(), SessionLocal)
        buttons.append([InlineKeyboardButton(text=title, callback_data=f"mlist:{slug}:{sc.slug}")])


    back_btn = await get_common_menu_button('back')
    if back_btn:
        back_btn.callback_data = "mcity:choose"
        buttons.append([back_btn])

    main_menu_btn = await get_common_menu_button('main_menu')
    if main_menu_btn:
        buttons.append([main_menu_btn])

    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    try:
        await cb.message.delete()
    except Exception:
        pass

    msg = await cb.bot.send_message(
        chat_id,
        f"<b>Барахолка → {city.name}</b>",
        reply_markup=markup,
        parse_mode="HTML"
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {cb.message.chat.id} | "
        f"user_id: {cb.from_user.id} | "
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )


# ====== Вывод объявлений и подкатегорий в Барахолке ======
@router.callback_query(F.data.startswith("mlist:"))
async def market_list(cb: CallbackQuery):
    chat_id = cb.message.chat.id

    # Чистим все сообщения: сначала из DB (переживает рестарт), потом из памяти
    await clear_bot_messages(chat_id, cb.bot)

    # Удаляем текущее сообщение (кнопку «Назад»), если оно не было удалено выше
    try:
        await cb.message.delete()
    except Exception:
        pass

    _, city_slug, cat_slug = cb.data.split(":", 2)
    city = await city_by_slug(city_slug)
    async with SessionLocal() as s:
        cat = (await s.execute(select(Category).where(Category.slug == cat_slug))).scalar_one()
        children = (await s.execute(select(Category).where(Category.parent_id == cat.id).order_by(text("order_num"), Category.name))).scalars().all()

    keyboard = []

    # 1) Подкатегории
    if children:
        for child in children:
            title = await format_category_title(child.id, (child.name or "").strip(), SessionLocal)
            keyboard.append([
                InlineKeyboardButton(
                    text=title,
                    callback_data=f"mlist:{city_slug}:{child.slug}"
                )
            ])

        async with SessionLocal() as s:
            listings = (await s.execute(
                select(Listing)
                .where(
                    Listing.city_id == city.id,
                    (Listing.category_id == cat.id) |
                    (Listing.extra_category_id1 == cat.id) |
                    (Listing.extra_category_id2 == cat.id),
                    Listing.is_sold.is_(False),
                    Listing.status == "active",
                )
                .order_by(Listing.created_at.desc())
            )).scalars().all()
        if listings:
            keyboard.append([InlineKeyboardButton(text="— Объявления —", callback_data="stub")])
    else:
        # RU: Подтянуть объявления по базовой И доп. категориям (слоты 1/2)
        async with SessionLocal() as s:
            listings = (await s.execute(
                select(Listing)
                .where(
                    Listing.city_id == city.id,
                    (Listing.category_id == cat.id) |
                    (Listing.extra_category_id1 == cat.id) |
                    (Listing.extra_category_id2 == cat.id),
                    Listing.is_sold.is_(False),
                    Listing.status == "active",
                )
                .order_by(Listing.created_at.desc())
            )).scalars().all()

    # 2) Объявления
    if listings:
        for listing in listings:
            keyboard.append([
                InlineKeyboardButton(
                    text=f"{listing.title}" + (f" — {listing.price}" if listing.price else ""),
                    callback_data=f"listing:{listing.id}:{city_slug}:{cat_slug}"
                )
            ])

    # 3) Кнопка Назад
    if cat.parent_id:
        async with SessionLocal() as s:
            parent_cat = (
                await s.execute(select(Category).where(Category.id == cat.parent_id))
            ).scalar_one()

        keyboard.append([
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"mlist:{city_slug}:{parent_cat.slug}"
            )
        ])
    else:
        keyboard.append([
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"mcity:{city_slug}"
            )
        ])

    # 4) Главное меню
    main_menu_btn = await get_common_menu_button('main_menu')
    if main_menu_btn:
        keyboard.append([main_menu_btn])

    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    msg = await cb.bot.send_message(
        chat_id,
        f"<b>Барахолка → {city.name} → {cat.name}</b>\n\n" +
        ("Выберите подкатегорию или объявление:" if children and listings else
         "Выберите подкатегорию:" if children else "Выберите объявление:"),
        reply_markup=markup,
        parse_mode="HTML"
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()

    print(
        f"[market_view.py] {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {chat_id} | "
        f"user_id: {cb.from_user.id} | "
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )




@router.callback_query(F.data == "market_search")
async def market_search_start(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    await clear_bot_messages(chat_id, cb.bot)

    try:
        await cb.message.delete()
    except Exception:
        pass

    # Удаляем старые служебные сообщения
    for mid in (last_search_menu_message.pop(chat_id, None), last_search_query_message.pop(chat_id, None)):
        if mid:
            try:
                await cb.bot.delete_message(chat_id, mid)
            except Exception:
                pass

    # Кнопки навигации
    back_btn = await get_common_menu_button('back')
    if back_btn:
        back_btn.callback_data = "market_menu_back"
    main_menu_btn = await get_common_menu_button('main_menu')
    buttons = [b for b in (back_btn, main_menu_btn) if b]
    nav_markup = InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None

    query_text = await get_text('market_search_query', 'ru') or \
        "Enter your search query for listings (e.g., microphone, Yamaha, amp):"
    nav_text = await get_text('return_to_menu', 'ru') or "Return"

    nav_msg = await cb.bot.send_message(chat_id, nav_text, reply_markup=nav_markup)
    query_msg = await cb.bot.send_message(chat_id, query_text)

    last_search_query_message[chat_id] = query_msg.message_id
    last_search_menu_message[chat_id] = nav_msg.message_id
    await register_bot_messages(chat_id, [nav_msg.message_id, query_msg.message_id])

    await state.set_state(MarketSearch.waiting_for_query)
    await cb.answer()

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {chat_id} | "
        f"user_id: {cb.from_user.id} | "
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )


@router.callback_query(F.data == "back_to_market_search")
async def back_to_market_search(cb: CallbackQuery, state: FSMContext):
    msg = await cb.message.answer("Введите новый поисковый запрос по объявлениям Барахолки:")
    last_search_query_message[cb.message.chat.id] = msg.message_id
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
    await state.set_state(MarketSearch.waiting_for_query)
    await cb.answer()

    chat_id = cb.message.chat.id
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {cb.data} | chat_id: {chat_id} | user_id: {cb.from_user.id} | "
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )


@router.callback_query(F.data == "market_search_back")
async def market_search_back(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    last_result_ids = data.get("last_search_results", [])
    if not last_result_ids:
        await cb.message.answer("Результаты поиска не найдены. Начните новый поиск.")
        await state.clear()
        return

    async with SessionLocal() as s:
        results = (await s.execute(select(Listing).where(Listing.id.in_(last_result_ids)))).scalars().all()

    await show_market_search_results(cb.message, state, results)
    await cb.answer()

    chat_id = cb.message.chat.id
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {cb.data} | chat_id: {chat_id} | user_id: {cb.from_user.id} | "
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )


@router.callback_query(F.data == "market_search_new")
async def market_search_new(cb: CallbackQuery, state: FSMContext):
    msg = await safe_edit_or_send(cb, "Введите новый поисковый запрос:")
    last_search_query_message[cb.message.chat.id] = msg.message_id
    await state.set_state(MarketSearch.waiting_for_query)
    await cb.answer()

    chat_id = cb.message.chat.id
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {cb.data} | chat_id: {chat_id} | user_id: {cb.from_user.id} | "
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )


@router.callback_query(F.data == "market_search_results", MarketSearch.waiting_for_detail)
async def back_to_search_results(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    await clear_bot_messages(chat_id, cb.bot)

    for mid in (last_search_menu_message.pop(chat_id, None), last_search_query_message.pop(chat_id, None)):
        if mid:
            try:
                await cb.bot.delete_message(chat_id, mid)
            except Exception:
                pass

    data = await state.get_data()
    ids = data.get("search_results", [])
    query = data.get("search_query", "")
    # Синхронизируем кэш (вдруг он пуст)
    last_search_ctx_by_chat[chat_id] = {"ids": ids, "query": query}

    if not ids:
        main_menu_btn = await get_common_menu_button('main_menu')
        buttons = []
        if main_menu_btn:
            buttons.append([main_menu_btn])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

        msg = await cb.message.answer(
            "Search results not found.",
            reply_markup=kb
        )
        last_search_menu_message[chat_id] = msg.message_id
        await register_bot_messages(chat_id, [msg.message_id])
        await state.clear()
        return

    page_ids = ids[:MARKET_SEARCH_PAGE_SIZE]

    async with SessionLocal() as s:
        db_results = (await s.execute(
            select(Listing).where(Listing.id.in_(page_ids))
        )).scalars().all()

    # сохраняем порядок как в ids
    by_id = {l.id: l for l in db_results}
    results = [by_id[i] for i in page_ids if i in by_id]

    total_count = len(ids)
    page = 1
    pages = max(1, (total_count + MARKET_SEARCH_PAGE_SIZE - 1) // MARKET_SEARCH_PAGE_SIZE)

    new_search_btn = await get_common_menu_button('market_new_search')
    to_market_btn = await get_common_menu_button('market_menu_back')
    main_menu_btn = await get_common_menu_button('main_menu')

    found_count = await get_text('market_found_count', 'ru') or "Found"
    found_query = await get_text('market_found_query', 'ru') or "for"
    found_select = await get_text('market_found_select', 'ru') or "Select a listing"

    buttons = [[InlineKeyboardButton(
        text=(l.title if len(l.title) < 45 else l.title[:42] + "…"),
        callback_data=f"search_detail:{l.id}"
    )] for l in results]

    if pages > 1:
        pager_row = [
            InlineKeyboardButton(text=f"{page}/{pages}", callback_data="stub"),
            InlineKeyboardButton(text="»", callback_data=f"market_search_page:{MARKET_SEARCH_PAGE_SIZE}")
        ]
        buttons.append(pager_row)

    if new_search_btn:
        buttons.append([new_search_btn])
    if to_market_btn:
        buttons.append([to_market_btn])
    if main_menu_btn:
        buttons.append([main_menu_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    msg = await cb.message.answer(
        f"🔎 {found_count}: <b>{total_count}</b> {found_query}: <b>{query}</b>\n\n{found_select}:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    last_search_menu_message[chat_id] = msg.message_id
    last_search_query_message[chat_id] = msg.message_id
    await register_bot_messages(chat_id, [msg.message_id])

    await state.set_state(MarketSearch.waiting_for_detail)
    await cb.answer()

    print(
        f"[market_view.py] {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {cb.data} | chat_id: {chat_id} | user_id: {cb.from_user.id} | "
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )




# ─────────────────────────────────────────────────────────
# Назад к результатам поиска (резервный хендлер, без фильтра состояния)
# Срабатывает, если после редактирования состояние сброшено.
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data == "market_search_results")
async def back_to_search_results_any(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # Канон: чистим хвосты и удаляем исходное сообщение
    await clear_bot_messages(chat_id, cb.bot)
    try:
        await cb.message.delete()
    except Exception:
        pass

    # Пытаемся восстановить результаты
    data = await state.get_data()
    ids = data.get("search_results") or []
    query = data.get("search_query") or ""

    # Если FSM пуст — берём из нашего кэша последнего поиска по чату
    # last_search_ctx_by_chat должен быть объявлен глобально рядом с router = Router()
    if not ids:
        ctx = last_search_ctx_by_chat.get(chat_id)
        if ctx:
            ids = ctx.get("ids", []) or []
            query = ctx.get("query", "") or ""

    # Если контекст утерян — понятный fallback
    if not ids:
        new_search_btn = await get_common_menu_button('market_new_search', lang='ru')
        to_market_btn = await get_common_menu_button('market_menu_back', lang='ru')
        buttons = []
        if new_search_btn:
            buttons.append([new_search_btn])
        if to_market_btn:
            buttons.append([to_market_btn])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

        msg = await cb.message.answer(
            "Результаты предыдущего поиска недоступны. "
            "Начните новый поиск или вернитесь в меню Барахолки.",
            reply_markup=kb
        )
        last_search_menu_message[chat_id] = msg.message_id
        last_search_query_message[chat_id] = msg.message_id
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(
            f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | user_id: {cb.from_user.id} | "
            f"NO_STATE_RESULTS | last_search_query_message: {last_search_query_message.get(chat_id)} | "
            f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
            f"last_bot_messages: {last_bot_messages.get(chat_id)}"
        )
        return

    page_ids = ids[:MARKET_SEARCH_PAGE_SIZE]

    async with SessionLocal() as s:
        db_results = (await s.execute(
            select(Listing).where(Listing.id.in_(page_ids))
        )).scalars().all()

    by_id = {l.id: l for l in db_results}
    results = [by_id[i] for i in page_ids if i in by_id]

    # синхронизируем кэш
    last_search_ctx_by_chat[chat_id] = {"ids": ids, "query": query}

    # Если первая страница уже пустая — объявления удалены
    if not results:
        new_search_btn = await get_common_menu_button('market_new_search', lang='ru')
        to_market_btn = await get_common_menu_button('market_menu_back', lang='ru')
        buttons = []
        if new_search_btn:
            buttons.append([new_search_btn])
        if to_market_btn:
            buttons.append([to_market_btn])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        msg = await cb.message.answer(
            "Похоже, объявления из предыдущего поиска были удалены. "
            "Начните новый поиск или вернитесь в меню Барахолки.",
            reply_markup=kb
        )
        last_search_menu_message[chat_id] = msg.message_id
        last_search_query_message[chat_id] = msg.message_id
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(
            f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | user_id: {cb.from_user.id} | "
            f"STALE_IDS | last_search_query_message: {last_search_query_message.get(chat_id)} | "
            f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
            f"last_bot_messages: {last_bot_messages.get(chat_id)}"
        )
        return

    total_count = len(ids)
    page = 1
    pages = max(1, (total_count + MARKET_SEARCH_PAGE_SIZE - 1) // MARKET_SEARCH_PAGE_SIZE)

    found_count = await get_text('market_found_count', 'ru') or "Found"
    found_query = await get_text('market_found_query', 'ru') or "for"
    found_select = await get_text('market_found_select', 'ru') or "Select a listing"

    buttons = [[InlineKeyboardButton(
        text=(l.title if len(l.title) < 45 else l.title[:42] + "…"),
        callback_data=f"search_detail:{l.id}"
    )] for l in results]

    if pages > 1:
        pager_row = [
            InlineKeyboardButton(text=f"{page}/{pages}", callback_data="stub"),
            InlineKeyboardButton(text="»", callback_data=f"market_search_page:{MARKET_SEARCH_PAGE_SIZE}")
        ]
        buttons.append(pager_row)

    new_search_btn = await get_common_menu_button('market_new_search', lang='ru')
    to_market_btn = await get_common_menu_button('market_menu_back', lang='ru')
    if new_search_btn:
        buttons.append([new_search_btn])
    if to_market_btn:
        buttons.append([to_market_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    msg = await cb.message.answer(
        f"🔎 {found_count}: <b>{total_count}</b> {found_query}: <b>{query}</b>\n\n{found_select}:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    last_search_menu_message[chat_id] = msg.message_id
    last_search_query_message[chat_id] = msg.message_id
    await register_bot_messages(chat_id, [msg.message_id])

    await state.set_state(MarketSearch.waiting_for_detail)

    # Помечаем чат как «из поиска», чтобы другие экраны (карточка/обзор) знали куда вести «Назад»
    if 'came_from_search_by_chat' in globals():
        came_from_search_by_chat[chat_id] = True

    await cb.answer()
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | user_id: {cb.from_user.id} | "
        f"RESTORED_RESULTS={len(results)} | last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )


@router.callback_query(F.data == "market_menu_back")
async def market_menu_back(cb: CallbackQuery, state: FSMContext):
    """# Барахолка: «Назад» → главное меню Барахолки"""
    chat_id = cb.message.chat.id
    user_id = cb.from_user.id

    # Логи — по канонам (вход)
    print(
        f"[market_view.py] {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {cb.data} | chat_id: {chat_id} | user_id: {user_id} | "
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )

    # Сбрасываем любые «поисковые» хвосты (chat_id уже объявлен)
    came_from_search_by_chat.pop(chat_id, None)
    last_search_ctx_by_chat.pop(chat_id, None)

    # Удаляем служебные сообщения поиска (если были)
    for mid in (last_search_menu_message.pop(chat_id, None), last_search_query_message.pop(chat_id, None)):
        if mid:
            try:
                await cb.bot.delete_message(chat_id, mid)
            except Exception:
                pass

    # Канон: очистка предыдущих служебных сообщений
    await clear_bot_messages(chat_id, cb.bot)

    # Чистим FSM — на всякий случай
    try:
        await state.clear()
    except Exception as e:
        print(f"[market_view.py] {inspect.currentframe().f_code.co_name} | state.clear() skipped: {e}")

    # Возврат в меню Барахолки
    msg = await cb.message.answer("💸 Барахолка – выберите действие:", reply_markup=await market_inline())
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

    await cb.answer()

    # Логи — по канонам (выход)
    print(
        f"[market_view.py] EXIT {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {cb.data} | chat_id: {chat_id} | user_id: {user_id} | "
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )


@router.callback_query(F.data == "my_listings")
async def my_listings_handler(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    user_id = cb.from_user.id

    # Сбрасываем старый флаг поиска, чтобы "Мои объявления" не считались поиском
    came_from_search_by_chat.pop(chat_id, None)

    # Удаляем карточки моих объявлений
    for msg_id in my_listing_messages.get(chat_id, []):
        try:
            await cb.bot.delete_message(chat_id, msg_id)
        except Exception:
            pass

    await clear_bot_messages(chat_id, cb.bot)
    await state.clear()

    async with SessionLocal() as s:
        listings = (await s.execute(
            select(Listing)
            .where(Listing.owner_id == user_id, Listing.type == "market")
            .order_by(Listing.created_at.desc())
            .limit(10)
        )).scalars().all()

    if not listings:
        keyboard = []

        back_btn = await get_common_menu_button('back')
        if back_btn:
            back_btn.callback_data = "go_market"
            keyboard.append([back_btn])

        main_menu_btn = await get_common_menu_button('main_menu')
        if main_menu_btn:
            keyboard.append([main_menu_btn])

        markup = InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None

        await safe_edit_or_send(cb, "У вас пока нет опубликованных объявлений.", markup)
        await cb.answer()
        return

    header = await get_text('market_my_listings', 'ru') or "Your listings"

    keyboard = [[InlineKeyboardButton(
        text=f"{listing.title}" + (f" — {listing.price}" if listing.price else ""),
        callback_data=f"listing:{listing.id}:{listing.city_id}:{listing.category_id}:my"
    )] for listing in listings]

    back_btn = await get_common_menu_button('back')
    if back_btn:
        back_btn.callback_data = "go_market"
        keyboard.append([back_btn])

    main_menu_btn = await get_common_menu_button('main_menu')
    if main_menu_btn:
        keyboard.append([main_menu_btn])

    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    await safe_edit_or_send(cb, f"<b>{header}:</b>", markup)
    await cb.answer()

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | user_id: {cb.from_user.id} | "
        f"came_from_search={came_from_search_by_chat.get(chat_id)} | "
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )




@router.callback_query(F.data == "my_listings_back")
async def my_listings_back_handler(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    user_id = cb.from_user.id

    # Сбрасываем старый флаг поиска, чтобы "Мои объявления" не считались поиском
    came_from_search_by_chat.pop(chat_id, None)

    if my_listing_messages.get(chat_id):
        for msg_id in my_listing_messages[chat_id]:
            try:
                await cb.bot.delete_message(chat_id, msg_id)
            except Exception:
                pass

    try:
        await cb.message.delete()
    except Exception:
        pass

    await clear_bot_messages(chat_id, cb.bot)

    async with SessionLocal() as s:
        listings = (await s.execute(
            select(Listing)
            .where(Listing.owner_id == user_id, Listing.type == "market")
            .order_by(Listing.created_at.desc())
            .limit(10)
        )).scalars().all()

    if not listings:
        keyboard = []

        back_btn = await get_common_menu_button('back')
        if back_btn:
            back_btn.callback_data = "go_market"
            keyboard.append([back_btn])

        main_menu_btn = await get_common_menu_button('main_menu')
        if main_menu_btn:
            keyboard.append([main_menu_btn])

        markup = InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None

        await safe_edit_or_send(cb, "У вас пока нет опубликованных объявлений.", markup)
        await cb.answer()
        return

    keyboard = [[InlineKeyboardButton(
        text=f"{listing.title}" + (f" — {listing.price}" if listing.price else ""),
        callback_data=f"listing:{listing.id}:{listing.city_id}:{listing.category_id}:my"
    )] for listing in listings]

    back_btn = await get_common_menu_button('back')
    if back_btn:
        back_btn.callback_data = "go_market"
        keyboard.append([back_btn])

    main_menu_btn = await get_common_menu_button('main_menu')
    if main_menu_btn:
        keyboard.append([main_menu_btn])

    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    await safe_edit_or_send(cb, "<b>Ваши объявления:</b>\nВыберите для просмотра или управления.", markup)
    await cb.answer()

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | user_id: {cb.from_user.id} | "
        f"came_from_search={came_from_search_by_chat.get(chat_id)} | "
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )



# ─────────────────────────────────────────────────────────
# Просмотр объявления (единая точка) — корректный «Назад»
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("listing:"))
async def show_listing_details(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    user_id = cb.from_user.id

    # Канон: чистим служебные сообщения и удаляем исходник, чтобы не плодить меню
    await clear_bot_messages(chat_id, cb.bot)
    try:
        await cb.message.delete()
    except Exception:
        pass

    # Разбор callback
    parts = cb.data.split(":")
    listing_id = int(parts[1])
    city_slug = parts[2]
    cat_slug = parts[3]
    from_my = len(parts) > 4 and parts[4] == "my"

    # Достаём объявление
    async with SessionLocal() as s:
        listing = (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one()

    # Определяем категорию, из которой открыта карточка, для заголовка.
    current_category_id = listing.category_id
    if not from_my and cat_slug:
        try:
            async with SessionLocal() as s:
                current_cat = (await s.execute(
                    select(Category).where(Category.slug == cat_slug)
                )).scalar_one_or_none()
            if current_cat:
                current_category_id = current_cat.id
        except Exception:
            current_category_id = listing.category_id

    async with SessionLocal() as s:
        category_path = await render_category_path(s, current_category_id, root_id=30)
    category_line = f"Категория: <b>Барахолка → {category_path}</b>" if category_path else "Категория: <b>Барахолка</b>"

    # Для callback listing:... источник берём только из самого маршрута:
    # либо "my", либо "catalog". Поиск сюда не должен подмешиваться.
    came_from_search = False

    if from_my:
        source = "my"
    else:
        source = "catalog"

    # ЛОГ ПРОСМОТРА КАРТОЧКИ
    await log_listing_view(
        listing_id=listing.id,
        user_id=user_id,
        section="market",
        action="open",
        source=source,
    )

    # Фото
    photo_ids = listing.photo_file_id.split(",") if listing.photo_file_id else []

    # Видео из flex: найдём поле с типом video в Category.fields и достанем значение
    video_id: str | None = None
    video_url: str | None = None
    if listing.flex:
        try:
            flex_vals = json.loads(listing.flex)
            if not isinstance(flex_vals, dict):
                flex_vals = {}
        except Exception:
            flex_vals = {}
        try:
            async with SessionLocal() as s:
                cat = (await s.execute(select(Category).where(Category.id == listing.category_id))).scalar_one()
            defs = []
            try:
                defs = json.loads(cat.fields) if cat and cat.fields else []
                if not isinstance(defs, list):
                    defs = []
            except Exception:
                defs = []
            for f in defs:
                if str(f.get("type", "")).lower() == "video":
                    key = (str(f.get("key", "")).strip().lower() or "field")
                    val = flex_vals.get(key)
                    if isinstance(val, str):
                        sval = val.strip()
                        low = sval.lower()
                        if "http" in low or "://" in sval:
                            video_url = sval
                        else:
                            video_id = sval
                    break
            # Fallback: если категория без определения полей — ищем "video" напрямую
            if not video_url and not video_id:
                raw = flex_vals.get("video", "")
                if isinstance(raw, str) and raw.strip():
                    sval = raw.strip()
                    if "http" in sval.lower() or "://" in sval:
                        video_url = sval
                    else:
                        video_id = sval
        except Exception:
            pass

    # Формирование caption
    caption_parts = []

    title_line = f"<b>{listing.title}</b>"
    descr_line = (listing.descr or "").strip()
    price_label = (await get_text('listing_price', 'ru')) or "Price"
    price_line = f"{price_label}: {listing.price}" if listing.price else ""

    block_lines = [title_line, category_line]
    if descr_line:
        block_lines.append(descr_line)
    if listing.price:
        block_lines.append(price_line)

    caption_parts.append("\n\n".join(block_lines))

    # Доп. сведения (flex)
    async with SessionLocal() as s:
        flex_block = await render_flex_block(s, listing, lang="ru")
    if flex_block:
        caption_parts.append(flex_block)

    if video_url:
        caption_parts.append(f"Видео: {video_url}")

    # Контакты
    contact_block = await render_contact(listing, lang="ru")
    if contact_block:
        caption_parts.append(contact_block)

    caption = "\n\n".join(caption_parts)

    is_owner = listing.owner_id == cb.from_user.id
    management_text = "Контакты/Управление:"
    if is_owner:
        left_line = days_left_text(listing)
        if left_line:
            management_text += f"\n{left_line}"

    # Кнопки
    buttons: list[list[InlineKeyboardButton]] = []

    if is_owner:
        edit_btn = await get_common_menu_button('btn_edit_listing', lang='ru')
        edit_btn = InlineKeyboardButton(
            text=edit_btn.text if edit_btn else "✏️ Редактировать все поля",
            callback_data=f"edit_listing_overview:{listing.id}:{city_slug}:{cat_slug}:{'my' if from_my else 'catalog'}"
        )
        buttons.append([edit_btn])

        del_btn = await get_common_menu_button('btn_delete_listing', lang='ru')
        del_btn = InlineKeyboardButton(text=del_btn.text, callback_data=f"sell_sold:{listing.id}") if del_btn \
            else InlineKeyboardButton(text="❌ Delete listing", callback_data=f"sell_sold:{listing.id}")
        buttons.append([del_btn])

        if should_show_extend_button(listing):
            extend_source = "my" if from_my else "catalog"
            buttons.append([
                InlineKeyboardButton(
                    text="🔄 Продлить на 30 дней",
                    callback_data=f"market_extend:{listing.id}:{city_slug}:{cat_slug}:{extend_source}"
                )
            ])

    elif listing.contact and listing.contact.startswith("@"):
        source = "search" if came_from_search else "my" if from_my else "catalog"
        c_btn = await get_common_menu_button('btn_contact_seller', lang='ru')
        buttons.append([InlineKeyboardButton(
            text=c_btn.text if c_btn else "💬 Contact seller",
            url=build_contact_url(listing.id, listing.contact, cb.from_user.id, source),
        )])

    # Куда вести «Назад»
    if from_my:
        back_btn = await get_common_menu_button('btn_back_my_listings', lang='ru')
        back_btn = InlineKeyboardButton(text=back_btn.text, callback_data="my_listings_back") if back_btn \
            else InlineKeyboardButton(text="⬅️ Back to my listings", callback_data="my_listings_back")
        buttons.append([back_btn])
    elif came_from_search:
        back_btn = await get_common_menu_button('btn_back_search', lang='ru')
        back_btn = InlineKeyboardButton(text=back_btn.text, callback_data="market_search_results") if back_btn \
            else InlineKeyboardButton(text="⬅️ Назад к поиску", callback_data="market_search_results")
        buttons.append([back_btn])
    else:
        back_btn = await get_common_menu_button('btn_back_listings', lang='ru')
        back_btn = InlineKeyboardButton(text=back_btn.text, callback_data=f"mlist:{city_slug}:{cat_slug}") if back_btn \
            else InlineKeyboardButton(text="⬅️ Back to listings", callback_data=f"mlist:{city_slug}:{cat_slug}")
        buttons.append([back_btn])

    main_btn = await get_common_menu_button('main_menu', lang='ru')
    if main_btn:
        buttons.append([main_btn])

    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    content_markup = None if is_owner else markup
    management_sent = False

    # Отдельная кнопка «Смотреть видео» для YouTube
    video_only_markup = None
    if video_url and (("youtube.com" in video_url.lower()) or ("youtu.be" in video_url.lower())) and WEBAPP_BASE:
        try:
            twa_url = f"{WEBAPP_BASE}/media/video_yt.html?u={urllib.parse.quote(video_url, safe='')}&listing_id={listing.id}"
            video_only_markup = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="|        ▶️   Смотреть  видео         |", web_app=WebAppInfo(url=twa_url))]]
            )
        except Exception as e:
            print(f"[market_view.py] show_listing_details | WATCH_BTN_ERROR {e}")

    sent_ids: list[int] = []

    if video_id:
        if photo_ids and photo_ids[0]:
            try:
                media = [InputMediaVideo(media=video_id, caption=caption, parse_mode="HTML")]
                for pid in photo_ids:
                    media.append(InputMediaPhoto(media=pid))
                msgs = await cb.message.answer_media_group(media=media)
                sent_ids.extend([m.message_id for m in msgs])

                msg2 = await cb.message.answer(management_text, reply_markup=markup)
                sent_ids.append(msg2.message_id)
                management_sent = True
            except Exception:
                try:
                    if photo_ids:
                        if len(photo_ids) == 1:
                            pmsg = await cb.message.answer_photo(photo_ids[0], caption=caption, parse_mode="HTML")
                            sent_ids.append(pmsg.message_id)
                        else:
                            media = [InputMediaPhoto(media=photo_ids[0], caption=caption, parse_mode="HTML")]
                            for pid in photo_ids[1:]:
                                media.append(InputMediaPhoto(media=pid))
                            pmsgs = await cb.message.answer_media_group(media=media)
                            sent_ids.extend([m.message_id for m in pmsgs])
                    vmsg = await cb.message.answer_video(video_id, reply_markup=content_markup)
                    sent_ids.append(vmsg.message_id)
                except Exception:
                    tmsg = await cb.message.answer(caption, parse_mode="HTML")
                    sent_ids.append(tmsg.message_id)
                    try:
                        vmsg = await cb.message.answer_video(video_id, reply_markup=content_markup)
                        sent_ids.append(vmsg.message_id)
                    except Exception:
                        pass
        else:
            try:
                vmsg = await cb.message.answer_video(video_id, caption=caption, reply_markup=content_markup, parse_mode="HTML")
                sent_ids.append(vmsg.message_id)
            except Exception:
                tmsg = await cb.message.answer(caption, parse_mode="HTML")
                sent_ids.append(tmsg.message_id)
                try:
                    vmsg = await cb.message.answer_video(video_id, reply_markup=content_markup)
                    sent_ids.append(vmsg.message_id)
                except Exception:
                    pass

    elif video_url:
        if photo_ids and photo_ids[0]:
            try:
                if len(photo_ids) == 1:
                    p = await cb.message.answer_photo(photo_ids[0], caption=caption, parse_mode="HTML")
                    sent_ids.append(p.message_id)
                else:
                    media = [InputMediaPhoto(media=photo_ids[0], caption=caption, parse_mode="HTML")]
                    for pid in photo_ids[1:]:
                        media.append(InputMediaPhoto(media=pid))
                    pmsgs = await cb.message.answer_media_group(media=media)
                    sent_ids.extend([m.message_id for m in pmsgs])
            except Exception:
                t = await cb.message.answer(caption, parse_mode="HTML")
                sent_ids.append(t.message_id)
        else:
            t = await cb.message.answer(caption, parse_mode="HTML")
            sent_ids.append(t.message_id)

        if video_only_markup:
            try:
                vid_btn_msg = await cb.message.answer("\u2063", reply_markup=video_only_markup)
                sent_ids.append(vid_btn_msg.message_id)
            except Exception as e:
                print(f"[market_view.py] show_listing_details | video_only button send failed: {e}")

        try:
            kmsg = await cb.message.answer(management_text, reply_markup=markup)
            sent_ids.append(kmsg.message_id)
            management_sent = True
        except Exception:
            pass

    else:
        if photo_ids and photo_ids[0]:
            if len(photo_ids) == 1:
                try:
                    msg = await cb.message.answer_photo(photo_ids[0], caption=caption, reply_markup=content_markup, parse_mode="HTML")
                    sent_ids.append(msg.message_id)
                except Exception:
                    msg = await cb.message.answer(caption, reply_markup=content_markup, parse_mode="HTML")
                    sent_ids.append(msg.message_id)
            else:
                try:
                    media_group = [InputMediaPhoto(media=photo_ids[0], caption=caption, parse_mode="HTML")]
                    for pid in photo_ids[1:]:
                        media_group.append(InputMediaPhoto(media=pid))
                    msgs = await cb.message.answer_media_group(media=media_group)
                    sent_ids.extend([m.message_id for m in msgs])
                    msg2 = await cb.message.answer(management_text, reply_markup=markup)
                    sent_ids.append(msg2.message_id)
                    management_sent = True
                except Exception:
                    for idx, pid in enumerate(photo_ids):
                        try:
                            if idx == 0:
                                m = await cb.message.answer_photo(pid, caption=caption, parse_mode="HTML")
                            else:
                                m = await cb.message.answer_photo(pid)
                            sent_ids.append(m.message_id)
                        except Exception:
                            pass
                    try:
                        mmsg = await cb.message.answer(management_text, reply_markup=markup)
                        sent_ids.append(mmsg.message_id)
                        management_sent = True
                    except Exception:
                        pass
        else:
            msg = await cb.message.answer(caption, reply_markup=content_markup, parse_mode="HTML")
            sent_ids.append(msg.message_id)

    if is_owner and not management_sent:
        try:
            mmsg = await cb.message.answer(management_text, reply_markup=markup)
            sent_ids.append(mmsg.message_id)
            management_sent = True
        except Exception as e:
            print(f"[market_view.py] show_listing_details | owner management send failed: {e}")

    if not came_from_search and not from_my and sent_ids:
        sent_photo_messages.setdefault(chat_id, []).extend(sent_ids)
    if from_my and sent_ids:
        my_listing_messages.setdefault(chat_id, []).extend(sent_ids)
    if sent_ids:
        await register_bot_messages(chat_id, sent_ids)

    await cb.answer()

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | user_id: {cb.from_user.id} | "
        f"from_my={from_my} | came_from_search={came_from_search} | "
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )


@router.callback_query(F.data.startswith("market_extend:"))
async def market_extend_listing(cb: CallbackQuery):
    parts = cb.data.split(":", 4)
    if len(parts) < 5:
        await cb.answer("Ошибка данных продления.", show_alert=True)
        return

    _, listing_id_raw, city_slug, cat_slug, source = parts

    try:
        listing_id = int(listing_id_raw)
    except ValueError:
        await cb.answer("Неверный идентификатор объявления.", show_alert=True)
        return

    async with SessionLocal() as s:
        listing = (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one_or_none()
        if not listing:
            await cb.answer("Объявление не найдено.", show_alert=True)
            return

        if listing.owner_id != cb.from_user.id:
            await cb.answer("Продлить может только автор объявления.", show_alert=True)
            return

        extend_listing(listing)
        await s.commit()
        await s.refresh(listing)

    management_text = "Контакты/Управление:"
    left_line = days_left_text(listing)
    if left_line:
        management_text += f"\n{left_line}"

    buttons: list[list[InlineKeyboardButton]] = []

    edit_btn = await get_common_menu_button('btn_edit_listing', lang='ru')
    buttons.append([
        InlineKeyboardButton(
            text=edit_btn.text if edit_btn else "✏️ Редактировать все поля",
            callback_data=f"edit_listing_overview:{listing.id}:{city_slug}:{cat_slug}:{source}"
        )
    ])

    del_btn = await get_common_menu_button('btn_delete_listing', lang='ru')
    buttons.append([
        InlineKeyboardButton(
            text=del_btn.text if del_btn else "❌ Delete listing",
            callback_data=f"sell_sold:{listing.id}"
        )
    ])

    if should_show_extend_button(listing):
        buttons.append([
            InlineKeyboardButton(
                text="🔄 Продлить на 30 дней",
                callback_data=f"market_extend:{listing.id}:{city_slug}:{cat_slug}:{source}"
            )
        ])

    if source == "my":
        back_btn = await get_common_menu_button('btn_back_my_listings', lang='ru')
        buttons.append([
            InlineKeyboardButton(
                text=back_btn.text if back_btn else "⬅️ Back to my listings",
                callback_data="my_listings_back"
            )
        ])
    else:
        back_btn = await get_common_menu_button('btn_back_listings', lang='ru')
        buttons.append([
            InlineKeyboardButton(
                text=back_btn.text if back_btn else "⬅️ Back to listings",
                callback_data=f"mlist:{city_slug}:{cat_slug}"
            )
        ])

    main_btn = await get_common_menu_button('main_menu', lang='ru')
    if main_btn:
        buttons.append([main_btn])

    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    try:
        await cb.message.edit_text(management_text, reply_markup=markup)
    except Exception:
        try:
            await cb.message.delete()
        except Exception:
            pass
        msg = await cb.message.answer(management_text, reply_markup=markup)
        my_listing_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
        await register_bot_messages(cb.message.chat.id, [msg.message_id])

    await cb.answer("Объявление продлено на 30 дней.")

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"listing_id={listing.id} | chat_id={cb.message.chat.id} | "
        f"user_id={cb.from_user.id} | source={source}"
    )


@router.callback_query(F.data.startswith("showphoto:"))
async def show_listing_photo(cb: CallbackQuery):
    _, listing_id, *_ = cb.data.split(":")
    listing_id = int(listing_id)
    chat_id = cb.message.chat.id

    async with SessionLocal() as s:
        listing = (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one()

    photo_ids = listing.photo_file_id.split(",") if listing.photo_file_id else []

    price_label = (await get_text('listing_price', 'ru')) or "Price"
    contact_label = (await get_text('listing_contact', 'ru')) or "Contact"
    caption = f"<b>{listing.title}</b>\n"
    if listing.price:
        caption += f"{price_label}: {listing.price}\n"
    if listing.descr:
        caption += f"{listing.descr}\n"
    caption += f"{contact_label}: {listing.contact}"

    sent_ids = []

    if photo_ids:
        if len(photo_ids) == 1:
            msg = await cb.message.answer_photo(photo_ids[0], caption=caption, parse_mode="HTML")
            sent_ids.append(msg.message_id)
        else:
            media_group = [InputMediaPhoto(media=photo_ids[0], caption=caption, parse_mode="HTML")]
            for pid in photo_ids[1:]:
                media_group.append(InputMediaPhoto(media=pid))
            msgs = await cb.message.answer_media_group(media=media_group)
            sent_ids.extend([m.message_id for m in msgs])
    else:
        await cb.answer("Фото не найдено.", show_alert=True)

    if sent_ids:
        sent_photo_messages.setdefault(chat_id, []).extend(sent_ids)

    await cb.answer()

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | user_id: {cb.from_user.id} | "
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )


@router.callback_query(F.data.startswith("toggle:"))
async def toggle_listing(cb: CallbackQuery):
    # Data format: toggle:{city_slug}:{cat_slug}:{listing_id}
    parts = cb.data.split(":")
    if len(parts) != 4:
        await cb.answer("Ошибка данных.")
        return

    _, city_slug, cat_slug, listing_id_str = parts
    try:
        listing_id = int(listing_id_str)
    except ValueError:
        await cb.answer("Неверный идентификатор объявления.")
        return

    chat_id = cb.message.chat.id
    current_expanded = expanded_listing_by_chat.get(chat_id)

    if current_expanded and current_expanded != listing_id:
        msg_id = listing_message_ids[chat_id].get(current_expanded)
        if msg_id:
            async with SessionLocal() as s:
                try:
                    listing = (await s.execute(select(Listing).where(Listing.id == current_expanded))).scalar_one()
                except NoResultFound:
                    listing = None
            if listing:
                header = f"• <b>{listing.title}</b>"
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text=f"{listing.title} — Развернуть",
                        callback_data=f"toggle:{city_slug}:{cat_slug}:{listing.id}"
                    )
                ]])
                await cb.bot.edit_message_text(
                    header, chat_id=str(chat_id), message_id=msg_id,
                    reply_markup=keyboard, parse_mode="HTML"
                )
        expanded_listing_by_chat[chat_id] = None

    logging.debug(f"Toggle handler called in chat {chat_id} for listing {listing_id}")
    msg_id_current = listing_message_ids[chat_id].get(listing_id)
    if not msg_id_current:
        await cb.answer("Сообщение не найдено.")
        return

    async with SessionLocal() as s:
        try:
            listing = (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one()
        except NoResultFound:
            await cb.bot.edit_message_text(
                "Объявление не найдено или было удалено.",
                chat_id=str(chat_id),
                message_id=msg_id_current
            )
            await cb.answer()
            return

    if expanded_listing_by_chat.get(chat_id) == listing_id:
        header = f"• <b>{listing.title}</b>"
        button_text = f"{listing.title} — Развернуть"
        new_reply = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=button_text, callback_data=f"toggle:{city_slug}:{cat_slug}:{listing.id}")
        ]])
        await cb.bot.edit_message_text(
            header, chat_id=str(chat_id), message_id=msg_id_current,
            reply_markup=new_reply, parse_mode="HTML"
        )
        expanded_listing_by_chat[chat_id] = None
    else:
        price_label = (await get_text('listing_price', 'ru')) or "Price"
        contact_label = (await get_text('listing_contact', 'ru')) or "Contact"
        no_descr = (await get_text('listing_no_descr', 'ru')) or "No description"

        details = (
            f"\n    {price_label}: {listing.price}"
            f"\n    {listing.descr or no_descr}"
            f"\n    {contact_label}: {listing.contact}"
        )
        # Доп. сведения – компактно
        async with SessionLocal() as s:
            flex_compact = await render_flex_compact(s, listing, indent="    ", lang="ru")
        if flex_compact:
            details += "\n" + flex_compact

        full_text = f"• <b>{listing.title}</b>{details}"
        button_text = f"{listing.title} — Свернуть"
        new_reply = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=button_text, callback_data=f"toggle:{city_slug}:{cat_slug}:{listing.id}")
        ]])
        await cb.bot.edit_message_text(
            full_text, chat_id=str(chat_id), message_id=msg_id_current,
            reply_markup=new_reply, parse_mode="HTML"
        )
        expanded_listing_by_chat[chat_id] = listing_id

    await cb.answer()

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | user_id: {cb.from_user.id} | "
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )


@router.callback_query(F.data.startswith("item_detail:"))
async def item_detail_handler(cb: CallbackQuery):
    item_id = int(cb.data.split(":", 1)[1])
    async with SessionLocal() as s:
        try:
            listing = (await s.execute(select(Listing).where(Listing.id == item_id))).scalar_one()
        except NoResultFound:
            await cb.message.answer("Объявление не найдено или было удалено.")
            await cb.answer()
            return

    text = (
        f"<b>{listing.title}</b>"
        f"{(' — ' + listing.price) if listing.price else ''}\n"
        f"{listing.descr or 'Нет описания'}\n"
        f"<code>{listing.contact}</code>"
    )
    photo_ids = listing.photo_file_id.split(",") if listing.photo_file_id else []

    seller_button = None
    if listing.contact and listing.contact.startswith("@"):
        seller_button = InlineKeyboardButton(
            text="Написать продавцу",
            url=build_contact_url(listing.id, listing.contact, cb.from_user.id, "search"),
        )
    detail_kb = InlineKeyboardMarkup(inline_keyboard=[[seller_button]]) if seller_button \
        else InlineKeyboardMarkup(inline_keyboard=[])

    if photo_ids:
        if len(photo_ids) == 1:
            await cb.message.answer_photo(photo_ids[0], caption=text, reply_markup=detail_kb)
        else:
            media_group = [InputMediaPhoto(media=photo_ids[0], caption=text)]
            for pid in photo_ids[1:]:
                media_group.append(InputMediaPhoto(media=pid))
            await cb.message.answer_media_group(media=media_group)
            if seller_button:
                await cb.message.answer("Связаться с продавцом:", reply_markup=detail_kb)
    else:
        await cb.message.answer(text, reply_markup=detail_kb)

    await cb.answer()

    chat_id = cb.message.chat.id
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | user_id: {cb.from_user.id} | "
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )

@router.message(StateFilter(MarketSearch.waiting_for_query, MarketSearch.waiting_for_detail))
async def handle_market_search(m: Message, state: FSMContext):
    chat_id = m.chat.id

    # удаляем сообщение пользователя с поисковым запросом
    try:
        await m.delete()
    except Exception:
        try:
            await m.bot.delete_message(chat_id, m.message_id)
        except Exception:
            pass

    await clear_bot_messages(chat_id, m.bot)

    for mid in (
        last_search_query_message.pop(chat_id, None),
        last_search_menu_message.pop(chat_id, None),
    ):
        if mid:
            try:
                await m.bot.delete_message(chat_id, mid)
            except Exception:
                pass

    query = (m.text or "").strip()

    async with SessionLocal() as s:
        rows = (await s.execute(
            select(Listing)
            .where(Listing.is_sold.is_(False), Listing.status == "active")
            .order_by(Listing.created_at.desc())
        )).scalars().all()

    search_outcome = search_items(
        rows,
        query,
        lambda it: [
            it.title or "",
            it.descr or "",
        ],
    )

    all_results = search_outcome.results
    search_query_raw = search_outcome.query_raw
    search_query_normalized = search_outcome.query_normalized
    search_query_effective = search_outcome.query_effective
    search_match_mode = search_outcome.match_mode

    total_count = len(all_results)
    results = all_results[:MARKET_SEARCH_PAGE_SIZE]
    page = 1
    pages = max(1, (total_count + MARKET_SEARCH_PAGE_SIZE - 1) // MARKET_SEARCH_PAGE_SIZE)

    # ЛОГИРОВАНИЕ ПОИСКА
    await log_search(
        user_id=m.from_user.id,
        section="market",
        query_raw=search_query_raw,
        query_normalized=search_query_normalized,
        query_effective=search_query_effective,
        match_mode=search_match_mode,
        results_count=total_count,
    )

    new_search_btn = await get_common_menu_button("market_new_search")
    to_market_btn = await get_common_menu_button("market_menu_back")
    main_menu_btn = await get_common_menu_button("main_menu")

    found_count = await get_text("market_found_count", "ru") or "Found"
    found_query = await get_text("market_found_query", "ru") or "for"
    found_select = await get_text("market_found_select", "ru") or "Select a listing"

    if not results:
        buttons = []
        if new_search_btn:
            buttons.append([new_search_btn])
        if to_market_btn:
            buttons.append([to_market_btn])
        if main_menu_btn:
            buttons.append([main_menu_btn])

        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

        msg = await m.answer(
            f"😕 Ничего не найдено по запросу: <b>{query}</b>.\n\n"
            "Попробуйте другой поисковый запрос или вернитесь в меню поиска.",
            reply_markup=kb,
            parse_mode="HTML"
        )
        last_search_menu_message[chat_id] = msg.message_id
        last_search_query_message[chat_id] = msg.message_id
        await register_bot_messages(chat_id, [msg.message_id])

        # при пустом результате остаёмся в режиме нового ввода
        await state.set_state(MarketSearch.waiting_for_query)

        print(
            f"[market_view.py] handle_market_search | "
            f"chat_id={chat_id} user_id={m.from_user.id} "
            f"results=0 query={query!r} match_mode={search_match_mode} "
            f"effective={search_query_effective!r}"
        )
        return

    await state.update_data(
        search_results=[l.id for l in all_results],
        search_query=query,
        search_query_raw=search_query_raw,
        search_query_normalized=search_query_normalized,
        search_query_effective=search_query_effective,
        search_match_mode=search_match_mode,
    )

    # Кэшируем ВСЕ результаты и запрос
    last_search_ctx_by_chat[chat_id] = {
        "ids": [l.id for l in all_results],
        "query": query,
        "query_raw": search_query_raw,
        "query_normalized": search_query_normalized,
        "query_effective": search_query_effective,
        "match_mode": search_match_mode,
    }

    buttons = [[InlineKeyboardButton(
        text=(l.title if len(l.title) < 45 else l.title[:42] + "…"),
        callback_data=f"search_detail:{l.id}"
    )] for l in results]

    if pages > 1:
        pager_row = [
            InlineKeyboardButton(text=f"{page}/{pages}", callback_data="stub")
        ]
        pager_row.append(
            InlineKeyboardButton(
                text="»",
                callback_data=f"market_search_page:{MARKET_SEARCH_PAGE_SIZE}"
            )
        )
        buttons.append(pager_row)

    if new_search_btn:
        buttons.append([new_search_btn])
    if to_market_btn:
        buttons.append([to_market_btn])
    if main_menu_btn:
        buttons.append([main_menu_btn])

    correction_note = ""
    if search_match_mode == "corrected" and search_query_effective != search_query_normalized:
        correction_note = (
            f"🧠 Показаны результаты по запросу: "
            f"<b>{search_query_effective}</b> "
            f"(учтена возможная опечатка).\n\n"
        )

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    msg = await m.answer(
        f"{correction_note}🔎 {found_count}: <b>{total_count}</b> {found_query}: <b>{query}</b>\n\n{found_select}:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    last_search_menu_message[chat_id] = msg.message_id
    last_search_query_message[chat_id] = msg.message_id
    await register_bot_messages(chat_id, [msg.message_id])

    # при найденных результатах нужен режим detail, чтобы карточки открывались
    await state.set_state(MarketSearch.waiting_for_detail)

    print(
        f"[market_view.py] handle_market_search | "
        f"chat_id={m.chat.id} | user_id={m.from_user.id} | "
        f"results={len(all_results)} query={query!r} "
        f"match_mode={search_match_mode} effective={search_query_effective!r} | "
        f"last_search_query_message: {last_search_query_message.get(m.chat.id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(m.chat.id)} | "
        f"last_bot_messages: {last_bot_messages.get(m.chat.id)}"
    )


@router.callback_query(F.data.startswith("market_search_page:"))
async def market_search_page(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    await clear_bot_messages(chat_id, cb.bot)
    try:
        await cb.message.delete()
    except Exception:
        pass

    try:
        offset = int(cb.data.split(":")[1])
    except Exception:
        offset = 0

    data = await state.get_data()
    ids = data.get("search_results") or []
    query = data.get("search_query") or ""

    if not ids:
        ctx = last_search_ctx_by_chat.get(chat_id) or {}
        ids = ctx.get("ids") or []
        query = ctx.get("query") or ""

    if not ids:
        msg = await cb.message.answer("Результаты поиска потеряны. Начните новый поиск.")
        last_search_menu_message[chat_id] = msg.message_id
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        return

    page_ids = ids[offset:offset + MARKET_SEARCH_PAGE_SIZE]

    async with SessionLocal() as s:
        db_results = (await s.execute(
            select(Listing).where(Listing.id.in_(page_ids))
        )).scalars().all()

    by_id = {l.id: l for l in db_results}
    results = [by_id[i] for i in page_ids if i in by_id]

    total_count = len(ids)
    page = (offset // MARKET_SEARCH_PAGE_SIZE) + 1
    pages = max(1, (total_count + MARKET_SEARCH_PAGE_SIZE - 1) // MARKET_SEARCH_PAGE_SIZE)

    found_count = await get_text('market_found_count', 'ru') or "Found"
    found_query = await get_text('market_found_query', 'ru') or "for"
    found_select = await get_text('market_found_select', 'ru') or "Select a listing"

    buttons = [[InlineKeyboardButton(
        text=(l.title if len(l.title) < 45 else l.title[:42] + "…"),
        callback_data=f"search_detail:{l.id}"
    )] for l in results]

    if pages > 1:
        pager_row = []
        if page > 1:
            prev_offset = max(0, offset - MARKET_SEARCH_PAGE_SIZE)
            pager_row.append(
                InlineKeyboardButton(text="«", callback_data=f"market_search_page:{prev_offset}")
            )

        pager_row.append(
            InlineKeyboardButton(text=f"{page}/{pages}", callback_data="stub")
        )

        if page < pages:
            next_offset = offset + MARKET_SEARCH_PAGE_SIZE
            pager_row.append(
                InlineKeyboardButton(text="»", callback_data=f"market_search_page:{next_offset}")
            )

        buttons.append(pager_row)

    new_search_btn = await get_common_menu_button('market_new_search')
    to_market_btn = await get_common_menu_button('market_menu_back')
    main_menu_btn = await get_common_menu_button('main_menu')

    if new_search_btn:
        buttons.append([new_search_btn])
    if to_market_btn:
        buttons.append([to_market_btn])
    if main_menu_btn:
        buttons.append([main_menu_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    msg = await cb.message.answer(
        f"🔎 {found_count}: <b>{total_count}</b> {found_query}: <b>{query}</b>\n\n{found_select}:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    last_search_menu_message[chat_id] = msg.message_id
    last_search_query_message[chat_id] = msg.message_id
    await register_bot_messages(chat_id, [msg.message_id])

    await state.set_state(MarketSearch.waiting_for_detail)
    await cb.answer()

    print(
        f"[market_view.py] market_search_page | "
        f"chat_id={chat_id} | page={page}/{pages} | offset={offset}"
    )



@router.callback_query(F.data.startswith("search_listing:"))
async def show_search_listing(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    # Канон: чистим меню/хвосты и удаляем исходник
    await clear_bot_messages(chat_id, cb.bot)
    try:
        await cb.message.delete()
    except Exception:
        pass
    # Отмечаем, что теперь «режим поиска» активен для этого чата
    came_from_search_by_chat[chat_id] = True

    listing_id = int(cb.data.split(":", 1)[1])
    async with SessionLocal() as s:
        listing = (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one()
        category_path = await render_category_path(s, listing.category_id, root_id=30)

    photo_ids = listing.photo_file_id.split(",") if listing.photo_file_id else []

    price_label = (await get_text('listing_price', 'ru')) or "Price"
    contact_label = (await get_text('listing_contact', 'ru')) or "Contact"
    caption = f"<b>{listing.title}</b>"
    if category_path:
        caption += f"\n\nКатегория: <b>Барахолка → {category_path}</b>"
    else:
        caption += "\n\nКатегория: <b>Барахолка</b>"
    if listing.descr:
        caption += f"\n\n{listing.descr}"
    if listing.price:
        caption += f"\n\n{price_label}: {listing.price}"
    caption += f"\n\n{contact_label}: {listing.contact}"


    async with SessionLocal() as s:
        flex_block = await render_flex_block(s, listing, lang="ru")
    if flex_block:
        caption += "\n" + flex_block


    # btns = []
    buttons = []

    if listing.owner_id == cb.from_user.id:
        # ✏️ Редактировать все поля
        edit_btn = await get_common_menu_button('btn_edit_listing', lang='ru')
        edit_btn = InlineKeyboardButton(
            text=edit_btn.text if edit_btn else "✏️ Редактировать все поля",
            callback_data=f"edit_listing_overview:{listing.id}:search"
        )
        buttons.append([edit_btn])

        # ❌ Удалить (ваш сценарий «Продано»)
        del_btn = await get_common_menu_button('btn_delete_listing', lang='ru')
        del_btn = InlineKeyboardButton(text=del_btn.text, callback_data=f"sell_sold:{listing.id}") if del_btn \
            else InlineKeyboardButton(text="❌ Delete listing", callback_data=f"sell_sold:{listing.id}")
        buttons.append([del_btn])
    elif listing.contact and listing.contact.startswith("@"):
        c_btn = await get_common_menu_button('btn_contact_seller', lang='ru')
        buttons.append([InlineKeyboardButton(
            text=c_btn.text if c_btn else "💬 Contact seller",
            url=build_contact_url(listing.id, listing.contact, cb.from_user.id, "search"),
        )])

    # ⬅️ Назад к поиску
    # стало — возвращаемся к прошлым результатам
    back_btn = await get_common_menu_button('btn_back_search', lang='ru')
    back_btn = InlineKeyboardButton(text=back_btn.text, callback_data="market_search_results") if back_btn \
        else InlineKeyboardButton(text="⬅️ Назад к поиску", callback_data="market_search_results")
    buttons.append([back_btn])

    main_btn = await get_common_menu_button('main_menu', lang='ru')
    if main_btn:
        buttons.append([main_btn])    

    print(f"FUNC: {inspect.currentframe().f_code.co_name} | back=market_search_results | listing_id={listing.id} | chat_id={cb.message.chat.id} | user_id={cb.from_user.id}")


    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    if photo_ids and photo_ids[0]:
        sent = await cb.message.answer_photo(photo_ids[0], caption=caption, reply_markup=markup, parse_mode="HTML")
    else:
        sent = await cb.message.answer(caption, reply_markup=markup, parse_mode="HTML")
    await register_bot_messages(chat_id, [sent.message_id])

    await cb.answer()

    chat_id = cb.message.chat.id
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | user_id: {cb.from_user.id} | "
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )


@router.callback_query(F.data.startswith("search_detail:"), MarketSearch.waiting_for_detail)
async def show_search_detail(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    user_id = cb.from_user.id

    # Канон: чистим хвосты и удаляем исходник
    await clear_bot_messages(chat_id, cb.bot)
    try:
        await cb.message.delete()
    except Exception:
        pass

    # Активируем флаг «из поиска»
    came_from_search_by_chat[chat_id] = True

    try:
        listing_id = int(cb.data.split(":")[1])
    except Exception:
        await cb.answer("Не удалось открыть объявление.", show_alert=True)
        return

    async with SessionLocal() as s:
        listing = (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one()
        city = (await s.execute(select(City).where(City.id == listing.city_id))).scalar_one()
        cat = (await s.execute(select(Category).where(Category.id == listing.category_id))).scalar_one()
        city_slug = city.slug
        cat_slug = cat.slug
        category_path = await render_category_path(s, listing.category_id, root_id=30)

    # ЛОГИРОВАНИЕ ПРОСМОТРА КАРТОЧКИ
    await log_listing_view(
        listing_id=listing.id,
        user_id=user_id,
        section="market",
        action="open",
        source="search",
    )

    price_label = (await get_text('listing_price', 'ru')) or "Price"
    contact_label = (await get_text('listing_contact', 'ru')) or "Contact"

    caption = f"<b>{listing.title}</b>"
    if category_path:
        caption += f"\n\nКатегория: <b>Барахолка → {category_path}</b>"
    else:
        caption += "\n\nКатегория: <b>Барахолка</b>"
    if listing.descr:
        caption += f"\n\n{listing.descr}"
    if listing.price:
        caption += f"\n\n{price_label}: {listing.price}"
    caption += f"\n\n{contact_label}: {listing.contact}"

    async with SessionLocal() as s:
        flex_block = await render_flex_block(s, listing, lang="ru")
    if flex_block:
        caption += "\n" + flex_block

    buttons = []

    if listing.owner_id == user_id:
        edit_btn = await get_common_menu_button('btn_edit_listing', lang='ru')
        edit_btn = InlineKeyboardButton(
            text=edit_btn.text if edit_btn else "✏️ Редактировать все поля",
            callback_data=f"edit_listing_overview:{listing.id}:search"
        )
        buttons.append([edit_btn])

        del_btn = await get_common_menu_button('btn_delete_listing', lang='ru')
        del_btn = InlineKeyboardButton(
            text=del_btn.text,
            callback_data=f"sell_sold:{listing.id}"
        ) if del_btn else InlineKeyboardButton(
            text="❌ Delete listing",
            callback_data=f"sell_sold:{listing.id}"
        )
        buttons.append([del_btn])

    elif listing.contact and listing.contact.startswith("@"):
        username = listing.contact.lstrip("@").strip()
        contact_btn = await get_common_menu_button('btn_contact_seller', lang='ru')

        contact_btn = InlineKeyboardButton(
            text=contact_btn.text if contact_btn else "💬 Contact seller",
            url=f"https://t.me/{username}",
        )
        buttons.append([contact_btn])

    back_btn = await get_common_menu_button('btn_back_search', lang='ru')
    back_btn = InlineKeyboardButton(
        text=back_btn.text,
        callback_data="market_search_results"
    ) if back_btn else InlineKeyboardButton(
        text="⬅️ Назад к поиску",
        callback_data="market_search_results"
    )
    buttons.append([back_btn])

    main_btn = await get_common_menu_button('main_menu', lang='ru')
    if main_btn:
        buttons.append([main_btn])

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"back=market_search_results | listing_id={listing.id} | "
        f"chat_id={chat_id} | user_id={user_id}"
    )

    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    photo_ids = listing.photo_file_id.split(",") if listing.photo_file_id else []

    sent_ids = []
    if photo_ids:
        if len(photo_ids) == 1:
            msg = await cb.message.answer_photo(
                photo_ids[0],
                caption=caption,
                reply_markup=markup,
                parse_mode="HTML"
            )
            sent_ids.append(msg.message_id)
        else:
            try:
                media = [InputMediaPhoto(media=photo_ids[0], caption=caption, parse_mode="HTML")]
                for pid in photo_ids[1:]:
                    media.append(InputMediaPhoto(media=pid))
                msgs = await cb.message.answer_media_group(media=media)
                sent_ids.extend([m.message_id for m in msgs])

                ctl = await cb.message.answer("Контакты/Управление:", reply_markup=markup)
                sent_ids.append(ctl.message_id)
            except Exception:
                first = True
                for pid in photo_ids:
                    if first:
                        m = await cb.message.answer_photo(pid, caption=caption, parse_mode="HTML")
                        first = False
                    else:
                        m = await cb.message.answer_photo(pid)
                    sent_ids.append(m.message_id)

                ctl = await cb.message.answer("Контакты/Управление:", reply_markup=markup)
                sent_ids.append(ctl.message_id)
    else:
        msg = await cb.message.answer(caption, reply_markup=markup, parse_mode="HTML")
        sent_ids.append(msg.message_id)

    sent_photo_messages[chat_id] = sent_ids
    if sent_ids:
        await register_bot_messages(chat_id, sent_ids)

    await cb.answer()

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"done | listing_id={listing.id} | chat_id={chat_id} | user_id={user_id} | "
        f"sent_ids={sent_ids}"
    )


