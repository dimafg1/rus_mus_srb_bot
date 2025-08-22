# app/routers/market_view.py

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
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
    render_flex_compact
)

router = Router()


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

    buttons = [[InlineKeyboardButton(text=sc.name, callback_data=f"mlist:{slug}:{sc.slug}")]
               for sc in subs]

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

    # Удаляем старые фото-сообщения
    photo_ids = sent_photo_messages.pop(chat_id, [])
    for msg_id in photo_ids:
        try:
            await cb.bot.delete_message(chat_id, msg_id)
        except Exception:
            pass

    # Удаляем старое меню (если есть)
    try:
        await cb.message.delete()
    except Exception:
        pass

    _, city_slug, cat_slug = cb.data.split(":", 2)
    city = await city_by_slug(city_slug)
    async with SessionLocal() as s:
        cat = (await s.execute(select(Category).where(Category.slug == cat_slug))).scalar_one()
        children = (await s.execute(select(Category).where(Category.parent_id == cat.id))).scalars().all()

    keyboard = []

    # 1) Подкатегории
    if children:
        for child in children:
            keyboard.append([
                InlineKeyboardButton(
                    text=child.name,
                    callback_data=f"mlist:{city_slug}:{child.slug}"
                )
            ])
        listings = await fetch_listings(city.id, cat.id)
        if listings:
            keyboard.append([InlineKeyboardButton(text="— Объявления —", callback_data="stub")])
    else:
        listings = await fetch_listings(city.id, cat.id)

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
            parent_cat = (await s.execute(select(Category).where(Category.id == cat.parent_id))).scalar_one()
        keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mlist:{city_slug}:{parent_cat.slug}")])
    else:
        keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mcity:{city_slug}")])

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
    if not ids:
        msg = await cb.message.answer("Search results not found.")
        last_search_menu_message[chat_id] = msg.message_id
        await state.clear()
        return

    async with SessionLocal() as s:
        results = (await s.execute(select(Listing).where(Listing.id.in_(ids)))).scalars().all()

    new_search_btn = await get_common_menu_button('market_new_search')
    to_market_btn = await get_common_menu_button('market_menu_back')

    found_count = await get_text('market_found_count', 'ru') or "Found"
    found_query = await get_text('market_found_query', 'ru') or "for"
    found_select = await get_text('market_found_select', 'ru') or "Select a listing"

    buttons = [[InlineKeyboardButton(
        text=(l.title if len(l.title) < 45 else l.title[:42] + "…"),
        callback_data=f"search_detail:{l.id}"
    )] for l in results]

    if new_search_btn:
        buttons.append([new_search_btn])
    if to_market_btn:
        buttons.append([to_market_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    msg = await cb.message.answer(
        f"🔎 {found_count}: <b>{len(results)}</b> {found_query}: <b>{query}</b>\n\n{found_select}:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    last_search_menu_message[chat_id] = msg.message_id
    last_search_query_message[chat_id] = msg.message_id

    await state.set_state(MarketSearch.waiting_for_detail)
    await cb.answer()

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | cb.data: {cb.data} | chat_id: {chat_id} | user_id: {cb.from_user.id} | "
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )


@router.callback_query(F.data == "market_menu_back")
async def market_menu_back(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    for mid in (last_search_menu_message.pop(chat_id, None), last_search_query_message.pop(chat_id, None)):
        if mid:
            try:
                await cb.bot.delete_message(chat_id, mid)
            except Exception:
                pass

    await clear_bot_messages(chat_id, cb.bot)
    await state.clear()

    msg = await cb.message.answer(
        "💸 Барахолка – выберите действие:",
        reply_markup=await market_inline()
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await cb.answer()

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | cb.data: {cb.data} | chat_id: {chat_id} | user_id: {cb.from_user.id} | "
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )


@router.callback_query(F.data == "my_listings")
async def my_listings_handler(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    user_id = cb.from_user.id

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
            .where(Listing.owner_id == user_id)
            .order_by(Listing.created_at.desc())
            .limit(10)
        )).scalars().all()

    if not listings:
        main_menu = await build_main_menu()
        await safe_edit_or_send(cb, "У вас пока нет опубликованных объявлений.", main_menu)
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
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )


@router.callback_query(F.data == "my_listings_back")
async def my_listings_back_handler(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    user_id = cb.from_user.id

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
            .where(Listing.owner_id == user_id)
            .order_by(Listing.created_at.desc())
            .limit(10)
        )).scalars().all()

    if not listings:
        main_menu = await build_main_menu()
        await safe_edit_or_send(cb, "У вас пока нет опубликованных объявлений.", main_menu)
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
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )


@router.callback_query(F.data.startswith("listing:"))
async def show_listing_details(cb: CallbackQuery):
    chat_id = cb.message.chat.id

    try:
        await cb.message.delete()
    except Exception:
        pass

    parts = cb.data.split(":")
    listing_id = int(parts[1])
    city_slug = parts[2]
    cat_slug = parts[3]
    from_my = len(parts) > 4 and parts[4] == "my"

    async with SessionLocal() as s:
        listing = (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one()

    photo_ids = listing.photo_file_id.split(",") if listing.photo_file_id else []

    # Формируем caption
    caption_parts = []

    # Основные поля
    main_block = await render_main_fields(listing)
    if main_block:
        caption_parts.append(main_block)

    # Доп. сведения (flex)
    async with SessionLocal() as s:
        flex_block = await render_flex_block(s, listing, lang="ru")
    if flex_block:
        caption_parts.append(flex_block)

    # Контакт (всегда в конце)
    contact_block = await render_contact(listing, lang="ru")
    if contact_block:
        caption_parts.append(contact_block)

    caption = "\n\n".join(caption_parts)

    # --- кнопки ---
    buttons = []

    if listing.owner_id == cb.from_user.id:
        # ✏️ Редактировать
        edit_btn = await get_common_menu_button('btn_edit_listing', lang='ru')
        edit_btn = InlineKeyboardButton(
            text=edit_btn.text if edit_btn else "✏️ Редактировать все поля",
            callback_data=f"edit_listing_overview:{listing.id}"
        )
        buttons.append([edit_btn])

        # ❌ Удалить (как у вас было)
        btn = await get_common_menu_button('btn_delete_listing', lang='ru')
        btn = InlineKeyboardButton(text=btn.text, callback_data=f"sell_sold:{listing.id}") if btn \
            else InlineKeyboardButton(text="❌ Delete listing", callback_data=f"sell_sold:{listing.id}")
        buttons.append([btn])
    elif listing.contact and listing.contact.startswith("@"):
        username = listing.contact.lstrip("@")
        btn = await get_common_menu_button('btn_contact_seller', lang='ru')
        btn = InlineKeyboardButton(text=btn.text, url=f"https://t.me/{username}") if btn \
            else InlineKeyboardButton(text="💬 Contact seller", url=f"https://t.me/{username}")
        buttons.append([btn])

    if from_my:
        btn = await get_common_menu_button('btn_back_my_listings', lang='ru')
        btn = InlineKeyboardButton(text=btn.text, callback_data="my_listings_back") if btn \
            else InlineKeyboardButton(text="⬅️ Back to my listings", callback_data="my_listings_back")
        buttons.append([btn])
    else:
        btn = await get_common_menu_button('btn_back_listings', lang='ru')
        btn = InlineKeyboardButton(text=btn.text, callback_data=f"mlist:{city_slug}:{cat_slug}") if btn \
            else InlineKeyboardButton(text="⬅️ Back to listings", callback_data=f"mlist:{city_slug}:{cat_slug}")
        buttons.append([btn])

    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    # --- отправка ---
    sent_ids = []
    if photo_ids and photo_ids[0]:
        if len(photo_ids) == 1:
            msg = await cb.message.answer_photo(photo_ids[0], caption=caption, reply_markup=markup, parse_mode="HTML")
            sent_ids.append(msg.message_id)
        else:
            media_group = [InputMediaPhoto(media=photo_ids[0], caption=caption, parse_mode="HTML")]
            for pid in photo_ids[1:]:
                media_group.append(InputMediaPhoto(media=pid))
            msgs = await cb.message.answer_media_group(media=media_group)
            msg2 = await cb.message.answer("Контакты/Управление:", reply_markup=markup)
            sent_ids.extend([m.message_id for m in msgs])
            sent_ids.append(msg2.message_id)
    else:
        msg = await cb.message.answer(caption, reply_markup=markup, parse_mode="HTML")
        sent_ids.append(msg.message_id)

    if not from_my and sent_ids:
        sent_photo_messages.setdefault(chat_id, []).extend(sent_ids)
    if from_my and sent_ids:
        my_listing_messages.setdefault(chat_id, []).extend(sent_ids)

    await cb.answer()

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | user_id: {cb.from_user.id} | "
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
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
        seller_button = InlineKeyboardButton(text="Написать продавцу",
                                             url=f"https://t.me/{listing.contact.lstrip('@')}")

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


@router.message(MarketSearch.waiting_for_query)
async def handle_market_search(m: Message, state: FSMContext):
    chat_id = m.chat.id

    await clear_bot_messages(chat_id, m.bot)

    for mid in (last_search_query_message.pop(chat_id, None), last_search_menu_message.pop(chat_id, None)):
        if mid:
            try:
                await m.bot.delete_message(chat_id, mid)
            except Exception:
                pass

    query = (m.text or "").strip()

    async with SessionLocal() as s:
        results = (await s.execute(
            select(Listing)
            .where(Listing.is_sold.is_(False))
            .where(Listing.title.ilike(f"%{query}%") | Listing.descr.ilike(f"%{query}%"))
            .order_by(Listing.created_at.desc())
            .limit(10)
        )).scalars().all()

    new_search_btn = await get_common_menu_button('market_new_search')
    to_market_btn = await get_common_menu_button('market_menu_back')

    found_count = await get_text('market_found_count', 'ru') or "Found"
    found_query = await get_text('market_found_query', 'ru') or "for"
    found_select = await get_text('market_found_select', 'ru') or "Select a listing"

    if not results:
        buttons = []
        if new_search_btn:
            buttons.append([new_search_btn])
        if to_market_btn:
            buttons.append([to_market_btn])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

        msg = await m.answer(
            f"😕 Ничего не найдено по запросу: <b>{query}</b>.\n\n"
            "Попробуйте другой поисковый запрос или вернитесь в меню поиска.",
            reply_markup=kb,
            parse_mode="HTML"
        )
        last_search_menu_message[chat_id] = msg.message_id
        last_search_query_message[chat_id] = msg.message_id
        await state.clear()
        return

    await state.update_data(search_results=[l.id for l in results], search_query=query)

    buttons = [[InlineKeyboardButton(
        text=(l.title if len(l.title) < 45 else l.title[:42] + "…"),
        callback_data=f"search_detail:{l.id}"
    )] for l in results]

    if new_search_btn:
        buttons.append([new_search_btn])
    if to_market_btn:
        buttons.append([to_market_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    msg = await m.answer(
        f"🔎 {found_count}: <b>{len(results)}</b> {found_query}: <b>{query}</b>\n\n{found_select}:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    last_search_menu_message[chat_id] = msg.message_id
    last_search_query_message[chat_id] = msg.message_id

    await state.set_state(MarketSearch.waiting_for_detail)

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {m.chat.id} | user_id: {m.from_user.id} | "
        f"last_search_query_message: {last_search_query_message.get(m.chat.id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(m.chat.id)} | "
        f"last_bot_messages: {last_bot_messages.get(m.chat.id)}"
    )


@router.callback_query(F.data.startswith("search_listing:"))
async def show_search_listing(cb: CallbackQuery):
    listing_id = int(cb.data.split(":", 1)[1])
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
    async with SessionLocal() as s:
        flex_block = await render_flex_block(s, listing, lang="ru")
    if flex_block:
        caption += "\n" + flex_block


    btns = []
    if listing.owner_id == cb.from_user.id:
        btns.append([InlineKeyboardButton(text="Продано", callback_data=f"sell_sold:{listing.id}")])
    elif listing.contact and listing.contact.startswith("@"):
        username = listing.contact.lstrip("@")
        btns.append([InlineKeyboardButton(text="💬 Связаться с продавцом", url=f"https://t.me/{username}")])

    btns.append([InlineKeyboardButton(text="⬅️ Назад к поиску", callback_data="back_to_market_search")])
    markup = InlineKeyboardMarkup(inline_keyboard=btns)

    if photo_ids and photo_ids[0]:
        await cb.message.answer_photo(photo_ids[0], caption=caption, reply_markup=markup, parse_mode="HTML")
    else:
        await cb.message.answer(caption, reply_markup=markup, parse_mode="HTML")

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
    await clear_bot_messages(chat_id, cb.bot)

    listing_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        listing = (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one()
        city = (await s.execute(select(City).where(City.id == listing.city_id))).scalar_one()
        cat = (await s.execute(select(Category).where(Category.id == listing.category_id))).scalar_one()
        city_slug = city.slug
        cat_slug = cat.slug

    price_label = (await get_text('listing_price', 'ru')) or "Price"
    contact_label = (await get_text('listing_contact', 'ru')) or "Contact"
    caption = f"<b>{listing.title}</b>\n"
    if listing.price:
        caption += f"{price_label}: {listing.price}\n"
    if listing.descr:
        caption += f"{listing.descr}\n"
    caption += f"{contact_label}: {listing.contact}"
    async with SessionLocal() as s:
        flex_block = await render_flex_block(s, listing, lang="ru")
    if flex_block:
        caption += "\n" + flex_block


    btns = []
    if listing.owner_id == cb.from_user.id:
        btns.append([InlineKeyboardButton(text="Продано", callback_data=f"sell_sold:{listing.id}")])
    elif listing.contact and listing.contact.startswith("@"):
        username = listing.contact.lstrip("@")
        btns.append([InlineKeyboardButton(text="💬 Связаться с продавцом", url=f"https://t.me/{username}")])
    btns.append([InlineKeyboardButton(text="⬅️ Назад к поиску", callback_data="market_search_results")])

    markup = InlineKeyboardMarkup(inline_keyboard=btns)
    photo_ids = listing.photo_file_id.split(",") if listing.photo_file_id else []

    sent_ids = []
    if photo_ids:
        if len(photo_ids) == 1:
            msg = await cb.message.answer_photo(photo_ids[0], caption=caption, reply_markup=markup, parse_mode="HTML")
            sent_ids.append(msg.message_id)
        else:
            media_group = [InputMediaPhoto(media=photo_ids[0], caption=caption, parse_mode="HTML")]
            for pid in photo_ids[1:]:
                media_group.append(InputMediaPhoto(media=pid))
            msgs = await cb.message.answer_media_group(media=media_group)
            msg2 = await cb.message.answer("Контакты/Управление:", reply_markup=markup)
            sent_ids.extend([m.message_id for m in msgs])
            sent_ids.append(msg2.message_id)
    else:
        msg = await cb.message.answer(caption, reply_markup=markup, parse_mode="HTML")
        sent_ids.append(msg.message_id)

    if sent_ids:
        sent_photo_messages.setdefault(chat_id, []).extend(sent_ids)

    await cb.answer()

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | user_id: {cb.from_user.id} | "
        f"last_search_query_message: {last_search_query_message.get(chat_id)} | "
        f"last_search_menu_message: {last_search_menu_message.get(chat_id)} | "
        f"last_bot_messages: {last_bot_messages.get(chat_id)}"
    )
