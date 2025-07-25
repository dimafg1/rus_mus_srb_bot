# app/routers/market_view.py

from aiogram import F
from aiogram import Router
from aiogram.types import CallbackQuery, InputMediaPhoto
from aiogram.fsm.context import FSMContext

from app.routers.utils import (
    clear_bot_messages, 
    safe_edit_or_send, 
    last_bot_messages, 
    sent_photo_messages, 
    last_search_query_message, 
    last_search_menu_message,
    my_listing_messages
)
from app.keyboards import (
    market_inline, 
    build_main_menu,
    get_common_menu_button
)
from app.texts import get_text
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from app.database import SessionLocal
from app.models import Category, Listing
from app.routers.utils import city_by_slug, children_of, fetch_listings
from app.keyboards import get_common_menu_button
from app.routers.utils import get_text
from app.routers.utils import expanded_listing_by_chat, listing_message_ids
# from app.misc import bot



from app.routers.utils import (
    last_search_query_message,
    last_search_menu_message,
    last_reply_menu_messages,
    last_bot_messages,
    my_listing_messages,
    sent_photo_messages,
)

from app.routers.market_utils import show_market_search_results
import logging
from app.states import MarketSearch



router = Router()


@router.callback_query(F.data == "go_market")
async def go_market(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # Удаляем предыдущие сообщения интерфейса
    await clear_bot_messages(chat_id, cb.bot)

    # Загружаем текст и клавиатуру для выбора города
    text = await get_text("market_choose_action", "ru") or "💸 Flea market – choose action:"
    markup = await market_inline()

    # Отправляем новое сообщение
    await safe_edit_or_send(cb, text, reply_markup=markup)
    last_bot_messages.setdefault(chat_id, []).append(cb.message.message_id)


@router.callback_query(F.data.startswith("mcity:"))
async def market_city(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    slug = cb.data.split(":", 1)[1]
    if slug == "choose":
        markup = await market_inline()
        await cb.message.edit_text(await get_text("market_choose_action", "ru"), reply_markup=markup)
        await cb.answer()
        return
    city = await city_by_slug(slug)
    async with SessionLocal() as s:
        equip = (await s.execute(select(Category).where(Category.slug == "equip"))).scalar_one()
    subs = await children_of(30)
    buttons = [[InlineKeyboardButton(text=sc.name, callback_data=f"mlist:{slug}:{sc.slug}")]
               for sc in subs]
    # --- Динамические кнопки ---
    back_btn = await get_common_menu_button('back')
    if back_btn:
        back_btn.callback_data = "mcity:choose"  # назначаем нужный callback
        buttons.append([back_btn])
    main_menu_btn = await get_common_menu_button('main_menu')
    if main_menu_btn:
        buttons.append([main_menu_btn])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await cb.message.delete()  # Удаляем старый список объявлений
    except Exception:
        pass
    msg = await cb.bot.send_message(
        cb.message.chat.id,
        f"<b>Барахолка → {city.name}</b>",
        reply_markup=markup,
        parse_mode="HTML"
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await cb.answer()


@router.callback_query(F.data.startswith("mlist:"))
async def market_list(cb: CallbackQuery):
    chat_id = cb.message.chat.id

    # Удаляем старые фото-сообщения (как выше)
    photo_ids = sent_photo_messages.pop(chat_id, [])
    for msg_id in photo_ids:
        try:
            await cb.bot.delete_message(chat_id, msg_id)
        except Exception:
            pass

    # Удаляем "лишние" сообщения (старое меню), если они есть
    try:
        await cb.message.delete()
    except Exception:
        pass

    _, city_slug, cat_slug = cb.data.split(":", 2)
    city = await city_by_slug(city_slug)
    async with SessionLocal() as s:
        cat = (await s.execute(select(Category).where(Category.slug == cat_slug))).scalar_one()
        children = (await s.execute(select(Category).where(Category.parent_id == cat.id))).scalars().all()

    # Если есть подкатегории — показываем их
    if children:
        buttons = [[InlineKeyboardButton(text=child.name, callback_data=f"mlist:{city_slug}:{child.slug}")]
                   for child in children]
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mcity:{city_slug}")])
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)
        msg = await cb.bot.send_message(
            chat_id,
            f"<b>Барахолка → {city.name} → {cat.name}</b>\n\nВыберите раздел:",
            reply_markup=markup,
            parse_mode="HTML"
        )
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await cb.answer()
        return

    # Если подкатегорий нет — показываем объявления
    listings = await fetch_listings(city.id, cat.id)
    keyboard = [
        [InlineKeyboardButton(
            text=f"{listing.title}" + (f" — {listing.price}" if listing.price else ""),
            callback_data=f"listing:{listing.id}:{city_slug}:{cat_slug}"
        )]
        for listing in listings
    ]
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mcity:{city_slug}")])
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    msg = await cb.bot.send_message(
        chat_id,
        f"<b>Барахолка → {city.name} → {cat.name}</b>\n\nВыберите объявление:",
        reply_markup=markup,
        parse_mode="HTML"
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await cb.answer()

@router.callback_query(F.data == "market_search")
async def market_search_start(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # Удаляем все предыдущее (универсально)
    await clear_bot_messages(chat_id, cb.bot)

    # Удаляем меню (которое вызвало этот callback)
    try:
        await cb.message.delete()
    except Exception:
        pass

    # Удаляем сообщения поиска, если есть (страховка)
    old_menu_id = last_search_menu_message.pop(chat_id, None)
    if old_menu_id:
        try:
            await cb.bot.delete_message(chat_id, old_menu_id)
        except Exception:
            pass
    old_query_id = last_search_query_message.pop(chat_id, None)
    if old_query_id:
        try:
            await cb.bot.delete_message(chat_id, old_query_id)
        except Exception:
            pass

    # --- Формируем кнопки навигации из базы ---
    back_btn = await get_common_menu_button('back')
    if back_btn:
        back_btn.callback_data = "market_menu_back"
    main_menu_btn = await get_common_menu_button('main_menu')

    buttons = []
    if back_btn:
        buttons.append(back_btn)
    if main_menu_btn:
        buttons.append(main_menu_btn)

    nav_markup = InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None

    # Получаем текст приглашения к поиску из BotText
    query_text = await get_text('market_search_query', 'ru')
    if not query_text:
        query_text = "Enter your search query for listings (e.g., microphone, Yamaha, amp):"

    # Сначала отправляем кнопки
    nav_text = await get_text('return_to_menu', 'ru') or "Return"
    nav_msg = await cb.bot.send_message(
        chat_id,
        nav_text,
        reply_markup=nav_markup
    )


    # Затем — текст запроса (пользователь вводит прямо под ним)
    query_msg = await cb.bot.send_message(
        chat_id,
        query_text
    )

    # (если нужно отслеживать оба сообщения для удаления)
    last_search_query_message[chat_id] = query_msg.message_id
    last_search_menu_message[chat_id] = nav_msg.message_id

    await state.set_state(MarketSearch.waiting_for_query)
    await cb.answer()

@router.callback_query(F.data == "back_to_market_search")
async def back_to_market_search(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите новый поисковый запрос по объявлениям Барахолки:")
    await state.set_state(MarketSearch.waiting_for_query)
    await cb.answer()

@router.callback_query(F.data == "market_search_back")
async def market_search_back(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    last_result_ids = data.get("last_search_results", [])
    if not last_result_ids:
        await cb.message.answer("Результаты поиска не найдены. Начните новый поиск.")
        await state.clear()
        return
    # Получаем объекты Listing по id
    async with SessionLocal() as s:
        results = (await s.execute(select(Listing).where(Listing.id.in_(last_result_ids)))).scalars().all()
    await show_market_search_results(cb.message, state, results)
    await cb.answer()


@router.callback_query(F.data == "market_search_new")
async def market_search_new(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("Введите новый поисковый запрос:")
    await state.set_state(MarketSearch.waiting_for_query)
    await cb.answer()

@router.callback_query(F.data == "market_search_results", MarketSearch.waiting_for_detail)
async def back_to_search_results(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # Удаляем карточки объявлений (фото и кнопки), отправленные ранее
    await clear_bot_messages(chat_id, cb.bot)

    old_menu_id = last_search_menu_message.pop(chat_id, None)
    if old_menu_id:
        try:
            await cb.bot.delete_message(chat_id, old_menu_id)
        except Exception:
            pass
    old_query_id = last_search_query_message.pop(chat_id, None)
    if old_query_id:
        try:
            await cb.bot.delete_message(chat_id, old_query_id)
        except Exception:
            pass

    data = await state.get_data()
    ids = data.get("search_results", [])
    query = data.get("search_query", "")
    if not ids:
        msg = await cb.message.answer("Search results not found.")  # Можно тоже вынести в базу
        last_search_menu_message[chat_id] = msg.message_id
        await state.clear()
        return

    async with SessionLocal() as s:
        results = (await s.execute(select(Listing).where(Listing.id.in_(ids)))).scalars().all()

    # --- Кнопки из базы ---
    new_search_btn = await get_common_menu_button('market_new_search')
    to_market_btn = await get_common_menu_button('market_menu_back')

    # --- Служебные строки из базы ---
    found_count = await get_text('market_found_count', 'ru') or "Found"
    found_query = await get_text('market_found_query', 'ru') or "for"
    found_select = await get_text('market_found_select', 'ru') or "Select a listing"

    # --- Кнопки объявлений ---
    buttons = [
        [InlineKeyboardButton(
            text=(l.title if len(l.title) < 45 else l.title[:42] + "…"),
            callback_data=f"search_detail:{l.id}"
        )] for l in results
    ]
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

    await state.set_state(MarketSearch.waiting_for_detail)
    await cb.answer()

@router.callback_query(F.data == "market_menu_back")
async def market_menu_back(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # Удаляем старое меню поиска, если оно есть
    old_menu_id = last_search_menu_message.pop(chat_id, None)
    if old_menu_id:
        try:
            await cb.bot.delete_message(chat_id, old_menu_id)
        except Exception:
            pass

    # Удаляем старое сообщение "Введите запрос...", если оно было
    old_query_id = last_search_query_message.pop(chat_id, None)
    if old_query_id:
        try:
            await cb.bot.delete_message(chat_id, old_query_id)
        except Exception:
            pass

    # Удаляем сообщения с фото и др.
    await clear_bot_messages(chat_id, cb.bot)

    await state.clear()
    # Вот здесь нужен await!
    msg = await cb.message.answer(
        "💸 Барахолка – выберите действие:",
        reply_markup=await market_inline()
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await cb.answer()

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
    #my_listing_messages[chat_id] = []

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

    # Получаем заголовок из БД
    header = await get_text('market_my_listings', 'ru') or "Your listings"

    keyboard = [
        [InlineKeyboardButton(
            text=f"{listing.title}" + (f" — {listing.price}" if listing.price else ""),
            callback_data=f"listing:{listing.id}:{listing.city_id}:{listing.category_id}:my"
        )]
        for listing in listings
    ]

    back_btn = await get_common_menu_button('back')
    if back_btn:
        back_btn.callback_data = "go_market"
        keyboard.append([back_btn])

    main_menu_btn = await get_common_menu_button('main_menu')
    if main_menu_btn:
        keyboard.append([main_menu_btn])

    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    # Добавляем двоеточие в коде
    await safe_edit_or_send(cb, f"<b>{header}:</b>", markup)
    await cb.answer()

@router.callback_query(F.data == "my_listings_back")
async def my_listings_back_handler(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    user_id = cb.from_user.id

    # Удаляем все карточки объявлений, отправленные ранее (фото и текст)
    if my_listing_messages.get(chat_id):
        for msg_id in my_listing_messages[chat_id]:
            try:
                await cb.bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
        #my_listing_messages[chat_id] = []

    # Также удаляем само сообщение с кнопками (если оно не из списка выше)
    try:
        await cb.message.delete()
    except Exception:
        pass

    # Удаляем прочие служебные сообщения (например, подсказки)
    await clear_bot_messages(chat_id, cb.bot)

    # Показываем список ваших объявлений
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

    keyboard = [
        [InlineKeyboardButton(
            text=f"{listing.title}" + (f" — {listing.price}" if listing.price else ""),
            callback_data=f"listing:{listing.id}:{listing.city_id}:{listing.category_id}:my"
        )]
        for listing in listings
    ]

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

@router.callback_query(F.data.startswith("listing:"))
async def show_listing_details(cb: CallbackQuery):
    chat_id = cb.message.chat.id

    # Удаляем старое меню объявлений (то, что с кнопкой Назад)
    try:
        await cb.message.delete()
    except Exception:
        pass
    parts = cb.data.split(":")
    listing_id = int(parts[1])
    city_slug = parts[2]
    cat_slug = parts[3]
    from_my = len(parts) > 4 and parts[4] == "my"
    chat_id = cb.message.chat.id

    # --- Удаляем все старые карточки моих объявлений (Вариант А) ---
    if from_my:
        for msg_id in my_listing_messages.get(chat_id, []):
            try:
                await cb.bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
        my_listing_messages[chat_id] = []

    # --- Загрузка объявления из БД ---
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


    buttons = []

    # Кнопка "Удалить объявление" — только для владельца объявления
    if listing.owner_id == cb.from_user.id:
        btn = await get_common_menu_button('btn_delete_listing', lang='ru')
        # меняем только callback_data, текст и иконка уже есть!
        if btn:
            btn = InlineKeyboardButton(
                text=btn.text,
                callback_data=f"sell_sold:{listing.id}"
            )
        else:
            btn = InlineKeyboardButton(text="❌ Delete listing", callback_data=f"sell_sold:{listing.id}")
        buttons.append([btn])

    # Кнопка "Связаться с продавцом"
    elif listing.contact and listing.contact.startswith("@"):
        username = listing.contact.lstrip("@")
        btn = await get_common_menu_button('btn_contact_seller', lang='ru')
        if btn:
            btn = InlineKeyboardButton(
                text=btn.text,
                url=f"https://t.me/{username}"
            )
        else:
            btn = InlineKeyboardButton(text="💬 Contact seller", url=f"https://t.me/{username}")
        buttons.append([btn])

    # Кнопка "Назад к моим объявлениям"
    if from_my:
        btn = await get_common_menu_button('btn_back_my_listings', lang='ru')
        if btn:
            btn = InlineKeyboardButton(
                text=btn.text,
                callback_data="my_listings_back"
            )
        else:
            btn = InlineKeyboardButton(text="⬅️ Back to my listings", callback_data="my_listings_back")
        buttons.append([btn])
    else:
        btn = await get_common_menu_button('btn_back_listings', lang='ru')
        if btn:
            btn = InlineKeyboardButton(
                text=btn.text,
                callback_data=f"mlist:{city_slug}:{cat_slug}"
            )
        else:
            btn = InlineKeyboardButton(text="⬅️ Back to listings", callback_data=f"mlist:{city_slug}:{cat_slug}")
        buttons.append([btn])

    markup = InlineKeyboardMarkup(inline_keyboard=buttons)


    
    sent_ids = []
    if photo_ids and photo_ids[0]:
        if len(photo_ids) == 1:
            msg = await cb.message.answer_photo(photo_ids[0], caption=caption, reply_markup=markup, parse_mode="HTML")
            sent_ids.append(msg.message_id)
        else:
            from aiogram.types import InputMediaPhoto
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

       # Для обычных объявлений (не мои) — остается sent_photo_messages, если используете
    if not from_my and sent_ids:
        sent_photo_messages.setdefault(chat_id, []).extend(sent_ids)
    if from_my and sent_ids:
        my_listing_messages[chat_id].extend(sent_ids)
        print("my_listing_messages[{}]: {}".format(chat_id, my_listing_messages[chat_id]))

    await cb.answer()

@router.callback_query(F.data.startswith("showphoto:"))
async def show_listing_photo(cb: CallbackQuery):
    _, listing_id, city_slug, cat_slug = cb.data.split(":")
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
            msg = await cb.message.answer_photo(
                photo_ids[0],
                caption=caption,
                parse_mode="HTML"
            )
            sent_ids.append(msg.message_id)
        else:
            from aiogram.types import InputMediaPhoto
            media_group = [InputMediaPhoto(media=photo_ids[0], caption=caption, parse_mode="HTML")]
            for pid in photo_ids[1:]:
                media_group.append(InputMediaPhoto(media=pid))
            msgs = await cb.message.answer_media_group(media=media_group)
            # answer_media_group возвращает список сообщений
            sent_ids.extend([m.message_id for m in msgs])
    else:
        await cb.answer("Фото не найдено.", show_alert=True)

    # Сохраняем ID отправленных фото-сообщений для этого чата
    if sent_ids:
        sent_photo_messages.setdefault(chat_id, []).extend(sent_ids)

    await cb.answer()

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
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"{listing.title} — Развернуть",
                                            callback_data=f"toggle:{city_slug}:{cat_slug}:{listing.id}")]
                    ]
                )
                await bot.edit_message_text(header, chat_id=str(chat_id), message_id=msg_id, reply_markup=keyboard, parse_mode="HTML")
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
            await bot.edit_message_text("Объявление не найдено или было удалено.", chat_id=str(chat_id), message_id=msg_id_current)
            await cb.answer()
            return
    if expanded_listing_by_chat.get(chat_id) == listing_id:
        header = f"• <b>{listing.title}</b>"
        button_text = f"{listing.title} — Развернуть"
        new_reply = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=button_text, callback_data=f"toggle:{city_slug}:{cat_slug}:{listing.id}")]
        ])
        await bot.edit_message_text(header, chat_id=str(chat_id), message_id=msg_id_current, reply_markup=new_reply, parse_mode="HTML")
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
        full_text = f"• <b>{listing.title}</b>{details}"
        button_text = f"{listing.title} — Свернуть"
        new_reply = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=button_text, callback_data=f"toggle:{city_slug}:{cat_slug}:{listing.id}")]
        ])
        await bot.edit_message_text(
            full_text,
            chat_id=str(chat_id),
            message_id=msg_id_current,
            reply_markup=new_reply,
            parse_mode="HTML"
        )
        expanded_listing_by_chat[chat_id] = listing_id
    await cb.answer()

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
    text = f"<b>{listing.title}</b> — {listing.price}\n{listing.descr or 'Нет описания'}\n<code>{listing.contact}</code>"
    photo_ids = listing.photo_file_id.split(",") if listing.photo_file_id else []

    seller_button = None
    if listing.contact and listing.contact.startswith("@"):
        seller_button = InlineKeyboardButton(text="Написать продавцу",
                                             url=f"https://t.me/{listing.contact.lstrip('@')}")
    detail_kb = InlineKeyboardMarkup(inline_keyboard=[])
    if seller_button:
        detail_kb = InlineKeyboardMarkup(inline_keyboard=[[seller_button]])
    if photo_ids:
        if len(photo_ids) == 1:
            await cb.message.answer_photo(photo_ids[0], caption=text, reply_markup=detail_kb)
        else:
            from aiogram.types import InputMediaPhoto
            media_group = [InputMediaPhoto(media=photo_ids[0], caption=text)]
            for pid in photo_ids[1:]:
                media_group.append(InputMediaPhoto(media=pid))
            await cb.message.answer_media_group(media=media_group)
            if seller_button:
                await cb.message.answer("Связаться с продавцом:", reply_markup=detail_kb)
    else:
        await cb.message.answer(text, reply_markup=detail_kb)
    await cb.answer()

@router.message(MarketSearch.waiting_for_query)
async def handle_market_search(m: Message, state: FSMContext):
    chat_id = m.chat.id

    # Удаляем предыдущее приглашение и меню
    await clear_bot_messages(chat_id, m.bot)

    old_query_id = last_search_query_message.pop(chat_id, None)
    if old_query_id:
        try:
            await m.bot.delete_message(chat_id, old_query_id)
        except Exception:
            pass
    old_menu_id = last_search_menu_message.pop(chat_id, None)
    if old_menu_id:
        try:
            await m.bot.delete_message(chat_id, old_menu_id)
        except Exception:
            pass

    query = m.text.strip()
    async with SessionLocal() as s:
        results = (await s.execute(
            select(Listing)
            .where(Listing.is_sold.is_(False))
            .where(Listing.title.ilike(f"%{query}%") | Listing.descr.ilike(f"%{query}%"))
            .order_by(Listing.created_at.desc())
            .limit(10)
        )).scalars().all()

    # --- Кнопки из базы ---
    new_search_btn = await get_common_menu_button('market_new_search')
    to_market_btn = await get_common_menu_button('market_menu_back')

    # --- Получаем текстовые части из базы ---
    found_count = await get_text('market_found_count', 'ru') or "Found"
    found_query = await get_text('market_found_query', 'ru') or "for"
    found_select = await get_text('market_found_select', 'ru') or "Select a listing"

    # --- Не найдено ---
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
        await state.clear()
        return

    await state.update_data(search_results=[l.id for l in results], search_query=query)

    # --- Кнопки объявлений ---
    buttons = [
        [InlineKeyboardButton(
            text=(l.title if len(l.title) < 45 else l.title[:42] + "…"),
            callback_data=f"search_detail:{l.id}"
        )] for l in results
    ]
    if new_search_btn:
        buttons.append([new_search_btn])
    if to_market_btn:
        buttons.append([to_market_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    # --- Собираем текст сообщения по частям из базы ---
    msg = await m.answer(
        f"🔎 {found_count}: <b>{len(results)}</b> {found_query}: <b>{query}</b>\n\n{found_select}:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    last_search_menu_message[chat_id] = msg.message_id

    await state.set_state(MarketSearch.waiting_for_detail)

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


    btns = []
    if listing.owner_id == cb.from_user.id:
        btns.append([InlineKeyboardButton(text="Продано", callback_data=f"sell_sold:{listing.id}")])
    elif listing.contact and listing.contact.startswith("@"):
        username = listing.contact.lstrip("@")
        btns.append([InlineKeyboardButton(text="💬 Связаться с продавцом", url=f"https://t.me/{username}")])

    # Кнопка "⬅️ Назад к поиску"
    btns.append([InlineKeyboardButton(text="⬅️ Назад к поиску", callback_data="back_to_market_search")])
    markup = InlineKeyboardMarkup(inline_keyboard=btns)

    if photo_ids and photo_ids[0]:
        await cb.message.answer_photo(photo_ids[0], caption=caption, reply_markup=markup, parse_mode="HTML")
    else:
        await cb.message.answer(caption, reply_markup=markup, parse_mode="HTML")
    await cb.answer()

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
        from aiogram.types import InputMediaPhoto
        if len(photo_ids) == 1:
            msg = await cb.message.answer_photo(photo_ids[0], caption=caption, reply_markup=markup, parse_mode="HTML")
            sent_ids.append(msg.message_id)
        else:
            # Медиагруппа: первая с подписью, остальные без
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
        # sent_photo_messages.setdefault(chat_id, []).extend(sent_ids)

    # Для корректного удаления при возврате к поиску — сохраняем ID отправленных сообщений
    if sent_ids:
        # from collections import defaultdict
        # if not hasattr(cb.bot, "sent_photo_messages"):
        #     cb.bot.sent_photo_messages = defaultdict(list)
        # cb.bot.sent_photo_messages[cb.message.chat.id].extend(sent_ids)
        # если у вас уже есть глобальный sent_photo_messages — используйте его:
        sent_photo_messages.setdefault(cb.message.chat.id, []).extend(sent_ids)

    await cb.answer()
