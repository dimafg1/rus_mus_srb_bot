from __future__ import annotations
import asyncio
from collections import defaultdict

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from sqlalchemy import select
from datetime import datetime

from app.database import SessionLocal
from app.models import City, Category, Listing
from aiogram.types.input_file import FSInputFile

from app.routers.utils import clear_bot_messages, last_bot_messages, sent_photo_messages, my_listing_messages, delete_photo_prompts, get_text

# === Импорт универсальных клавиатур из keyboards.py ===
from app.keyboards import (
    get_common_menu_button,
    market_inline,
    photo_keyboard,
    confirm_keyboard,
    sold_keyboard,
    delete_keyboard,
    cities_inline,
    equip_inline,
)

from app.routers.utils import (
    last_search_query_message,
    last_search_menu_message,
    last_reply_menu_messages,
    last_bot_messages,
    my_listing_messages,
    sent_photo_messages,
)

import sys; print("PYTHONPATH:", sys.path)
from app.routers.utils import my_listing_messages
from app.routers.utils import safe_edit_or_send
print("my_listing_messages:", type(my_listing_messages))


router = Router(name="market_addl")

# ========== КЭШИ для альбомов ==========
media_group_cache = {}
media_group_tasks = {}
media_group_wait_msg = {}

# --- Универсальная клавиатура для навигации ---
async def sell_nav_keyboard(lang="ru"):
    # Берём специальные кнопки "Назад" и "Главное меню"
    back_btn = await get_common_menu_button('sell_back', lang)
    main_menu_btn = await get_common_menu_button('main_menu', lang)
    buttons = []
    if back_btn:
        buttons.append(back_btn)
    if main_menu_btn:
        buttons.append(main_menu_btn)
    return InlineKeyboardMarkup(inline_keyboard=[[btn] for btn in buttons if btn])


# --- Универсальная функция для вопроса с меню ---
async def send_with_nav(m, text, parse_mode=None):
    nav_markup = await sell_nav_keyboard()
    nav_text = await get_text('return_to_menu', 'ru') or "Return"
    nav_msg = await m.answer(nav_text, reply_markup=nav_markup)
    last_bot_messages.setdefault(m.chat.id, []).append(nav_msg.message_id)
    msg = await m.answer(text, parse_mode=parse_mode)
    last_bot_messages.setdefault(m.chat.id, []).append(msg.message_id)
    return nav_msg, msg



# ─────────────────── FSM ───────────────────
class Sell(StatesGroup):
    city    = State()
    cat     = State()
    title   = State()
    price   = State()
    descr   = State()
    photo   = State()
    confirm = State()

# ─────────────────── helpers ───────────────
async def send_photo_prompt(m: Message, photo_count: int, state: FSMContext, lang="ru"):
    left = 3 - photo_count
    if photo_count == 0:
        text_main = (
            await get_text('sell_photo_0_main', lang)
            or "Send a <b>photo</b> (up to 3). You can send all at once or one by one.\nIf you select more than three, only the first three will be attached."
        )
        text_tip = (
            await get_text('sell_photo_0_tip', lang)
            or "To upload a photo, click the 📎 to the left of the message box\n⬇️"
        )
    elif left == 2:
        text_main = (
            await get_text('sell_photo_1_main', lang)
            or "Photo added (1/3).\nYou can add <b>2 more</b> photos, skip this step, or cancel posting."
        )
        text_tip = (
            await get_text('sell_photo_1_tip', lang)
            or "To add more, click the 📎 again on the left\n⬇️"
        )
    elif left == 1:
        text_main = (
            await get_text('sell_photo_2_main', lang)
            or "Photo added (2/3).\nYou can add <b>1 more</b> photo, skip this step, or cancel posting."
        )
        text_tip = (
            await get_text('sell_photo_2_tip', lang)
            or "To add more, click the 📎 again on the left\n⬇️"
        )
    else:
        text_main = (
            await get_text('sell_photo_max_main', lang)
            or "Something went wrong! Maximum is 3 photos."
        )
        text_tip = ""

    msg = await m.answer(text_main, reply_markup=photo_keyboard(photo_count))
    msg2 = None
    last_bot_messages.setdefault(m.chat.id, []).append(msg.message_id)
    if text_tip:
        msg2 = await m.answer(text_tip)
        last_bot_messages.setdefault(m.chat.id, []).append(msg2.message_id)
        await state.update_data(photo_prompt_msgs=[msg.message_id, msg2.message_id])
    else:
        await state.update_data(photo_prompt_msgs=[msg.message_id])


# ─────────────────── /sell start ───────────
@router.message(Command(commands=["sell"]))
async def cmd_sell(m: Message, state: FSMContext):
    await clear_bot_messages(m.chat.id, m.bot)
    async with SessionLocal() as s:
        cities = (await s.execute(select(City))).scalars().all()
    kb = await cities_inline(cities)
    header = await get_text('sell_choose_city', 'ru') or "Create a listing.\nFirst, choose a city:"
    msg = await m.answer(
        header,
        reply_markup=kb
    )
    last_bot_messages.setdefault(m.chat.id, []).append(msg.message_id)
    await state.set_state(Sell.city)

@router.callback_query(F.data == "sell_start")
async def sell_start_button(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)
    async with SessionLocal() as s:
        cities = (await s.execute(select(City))).scalars().all()
    header = await get_text('sell_choose_city', 'ru') or "Create a listing.\nFirst, choose a city:"
    msg = await cb.bot.send_message(
        chat_id,
        header,
        reply_markup=await cities_inline(cities)
    )
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await state.set_state(Sell.city)
    await cb.answer()

# ─────────────── шаг 1 – город ─────────────
@router.callback_query(F.data.startswith("sell_city:"), Sell.city)
async def sell_city(cb: CallbackQuery, state: FSMContext):
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    city_slug = cb.data.split(":")[1]
    async with SessionLocal() as s:
        city = (await s.execute(select(City).where(City.slug == city_slug))).scalar_one()
        equip_root = (await s.execute(select(Category).where(Category.slug == "equip"))).scalar_one()
        subcats = (await s.execute(
            select(Category).where(Category.parent_id == equip_root.id)
        )).scalars().all()
    await state.update_data(city_id=city.id, city_name=city.name, city_slug=city_slug)
    kb = await equip_inline(subcats, city_slug)

    # --- ВЫНОСИМ ТЕКСТ В БД ---
    template = await get_text('sell_choose_category', 'ru') or "City: <b>{city_name}</b>\nChoose a category:"
    text = template.format(city_name=city.name)

    msg = await cb.message.answer(
        text,
        reply_markup=kb
    )
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
    await state.set_state(Sell.cat)
    await cb.answer()


# ─────────────── шаг 2 – категория ─────────
@router.callback_query(F.data.startswith("sell_cat:"), Sell.cat)
async def sell_cat(cb: CallbackQuery, state: FSMContext):
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    _, city_slug, cat_id = cb.data.split(":")
    cat_id = int(cat_id)
    async with SessionLocal() as s:
        cat = (await s.execute(select(Category).where(Category.id == cat_id))).scalar_one()
        subcats = (await s.execute(select(Category).where(Category.parent_id == cat_id))).scalars().all()
    if subcats:
        kb = await equip_inline(subcats, city_slug)
        # --- Получаем текст из базы ---
        template = await get_text('sell_choose_subcategory', 'ru') or "Category: <b>{cat_name}</b>\nChoose a subcategory:"
        text = template.format(cat_name=cat.name)
        msg = await cb.message.answer(
            text,
            reply_markup=kb,
        )
        last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
        await state.update_data(cat_id=cat.id, cat_name=cat.name)
        await state.set_state(Sell.cat)
    else:
        await clear_bot_messages(cb.message.chat.id, cb.bot)
        await state.update_data(cat_id=cat.id, cat_name=cat.name)
        await state.set_state(Sell.title)
        template = await get_text('sell_ask_title', 'ru') or "Enter <b>listing title</b> (one line):"
        await send_with_nav(cb.message, template, parse_mode="HTML")
    await cb.answer()

# ─────────────── шаг 3 – title ─────────────
@router.message(Sell.title, F.text)
async def sell_title(m: Message, state: FSMContext):
    await clear_bot_messages(m.chat.id, m.bot)
    await state.update_data(title=m.text.strip())
    await state.set_state(Sell.price)
    # --- Получаем текст из базы ---
    template = await get_text('sell_ask_price', 'ru') or "Enter <b>price</b> (e.g.: 150 € or 12,000 rsd):"
    await send_with_nav(m, template, parse_mode="HTML")


# ─────────────── шаг 4 – price ─────────────
@router.message(Sell.price, F.text)
async def sell_price(m: Message, state: FSMContext):
    await clear_bot_messages(m.chat.id, m.bot)
    await state.update_data(price=m.text.strip())
    await state.set_state(Sell.descr)
    # --- Получаем текст из базы ---
    template = await get_text('sell_ask_descr', 'ru') or "Short description (or '-' to skip):"
    await send_with_nav(m, template, parse_mode="HTML")


# ─────────────── шаг 5 – descr ─────────────
@router.message(Sell.descr)
async def sell_descr(m: Message, state: FSMContext):
    text = m.text.strip()
    await state.update_data(descr=None if text == "-" else text)
    await state.set_state(Sell.photo)
    await send_photo_prompt(m, 0, state)

# ================== **ШАГ 6 — ФОТО** ==================
@router.message(Sell.photo, F.photo)
async def sell_photo(m: Message, state: FSMContext):
    if m.media_group_id:
        group_id = m.media_group_id
        if group_id not in media_group_cache:
            media_group_cache[group_id] = []
        media_group_cache[group_id].append(m.photo[-1].file_id)
        if group_id not in media_group_tasks:
            media_group_tasks[group_id] = None
            template = await get_text('sell_wait_photos', 'ru') or "⏳ Please wait — uploading photos…"
            wait_msg = await m.answer(template)
            media_group_wait_msg[group_id] = wait_msg.message_id
            media_group_tasks[group_id] = asyncio.create_task(finalize_album(m, state, group_id))
        return
    data = await state.get_data()
    photos = data.get("photos", []) or []
    if len(photos) < 3:
        photos.append(m.photo[-1].file_id)
        photos = photos[:3]
        await state.update_data(photos=photos)
    if len(photos) >= 3:
        await delete_photo_prompts(m, state)
        await preview_and_confirm(m, state)
        await state.set_state(Sell.confirm)
    else:
        await delete_photo_prompts(m, state)
        await send_photo_prompt(m, len(photos), state)

async def finalize_album(m: Message, state: FSMContext, group_id):
    await asyncio.sleep(1.5)
    album_photos = media_group_cache.pop(group_id, [])
    media_group_tasks.pop(group_id, None)
    data = await state.get_data()
    photos = data.get("photos", []) or []
    for fid in album_photos:
        if len(photos) < 3:
            photos.append(fid)
    photos = photos[:3]
    await state.update_data(photos=photos)
    wait_msg_id = media_group_wait_msg.pop(group_id, None)
    if wait_msg_id:
        try:
            await m.bot.delete_message(m.chat.id, wait_msg_id)
        except Exception:
            pass
    if len(photos) >= 3:
        await preview_and_confirm(m, state)
        await state.set_state(Sell.confirm)
    elif len(photos) == 2:
        await send_photo_prompt(m, len(photos), state)
        await state.set_state(Sell.photo)
    elif len(photos) == 1:
        await send_photo_prompt(m, len(photos), state)
        await state.set_state(Sell.photo)

@router.message(Sell.photo)
async def handle_not_photo(m: Message, state: FSMContext):
    if m.photo or m.video:
        return

    btn = await get_common_menu_button('delete_message', lang="ru")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[btn]] if btn else []
    )

    await m.answer(
        await get_text('sell_not_photo', lang="ru") or
        "❗️Please send only a photo (or video). If you selected the wrong type, use the paperclip and choose Photo/Video.",
        reply_markup=kb
    )



@router.callback_query(F.data.startswith("delmsg:"))
async def delete_msg_cb(cb: CallbackQuery):
    msg_id = int(cb.data.split(":")[1])
    try:
        await cb.bot.delete_message(cb.message.chat.id, msg_id)
    except Exception:
        pass
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer(
        await get_text('delete_message_done', lang="ru") or
        "Message deleted."
    )


@router.callback_query(Sell.photo, F.data == "sell_skip_photo")
async def sell_skip_photo(cb: CallbackQuery, state: FSMContext):
    await delete_photo_prompts(cb.message, state)
    await preview_and_confirm(cb.message, state)
    await state.set_state(Sell.confirm)
    await cb.answer()

@router.callback_query(Sell.photo, F.data == "sell_cancel")
async def sell_cancel_photo(cb: CallbackQuery, state: FSMContext):
    await delete_photo_prompts(cb.message, state)
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    
    # Сообщение об отмене (русский из БД, иначе английский дефолт)
    cancel_text = await get_text('sell_cancelled', lang="ru") or "❌ Listing creation cancelled."

    msg1 = await cb.message.answer(cancel_text)
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg1.message_id)
    
    # Формируем клавиатуру, где уже есть кнопка "Главное меню"
    nav_kb = await sell_nav_keyboard()
    msg2 = await cb.message.answer(
        (await get_text('main_menu', 'ru')) or "Main menu",
        reply_markup=nav_kb
    )

    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg2.message_id)

    await state.clear()
    await cb.answer()


# --- Предпросмотр + confirm ---
async def preview_and_confirm(m: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    header = (f"<b>{data['city_name']} → {data['cat_name']}</b>\n"
              f"{data['title']} — {data['price']}\n"
              f"{data.get('descr','')}")
    kb = confirm_keyboard()
    sent_ids = []
    if photos:
        if len(photos) == 1:
            msg_photo = await m.answer_photo(photos[0])
            sent_ids.append(msg_photo.message_id)
        else:
            media = [InputMediaPhoto(media=fid) for fid in photos]
            msgs = await m.answer_media_group(media)
            sent_ids.extend([msg.message_id for msg in msgs])
    msg = await m.answer(header, reply_markup=kb)
    last_bot_messages.setdefault(m.chat.id, []).append(msg.message_id)
    if sent_ids:
        sent_photo_messages.setdefault(m.chat.id, []).extend(sent_ids)

@router.callback_query(Sell.confirm, F.data == "sell_ok")
async def sell_ok(cb: CallbackQuery, state: FSMContext):
    for d in (media_group_cache, media_group_tasks, media_group_wait_msg):
        d.clear()
    data = await state.get_data()
    async with SessionLocal() as s:
        l = Listing(
            city_id=data["city_id"],
            category_id=data["cat_id"],
            owner_id=cb.from_user.id,
            title=data["title"],
            price=data["price"],
            descr=data.get("descr"),
            contact=cb.from_user.username and f"@{cb.from_user.username}" or "контакт не указан",
            created_at=datetime.utcnow(),
            photo_file_id=",".join(data.get("photos", [])) if data.get("photos") else None,
        )
        s.add(l)
        await s.commit()
        await s.refresh(l)
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    # После публикации:
    msg1 = await cb.message.answer(
        (await get_text('sell_published', 'ru')) or "✅ Listing published!"
    )
    nav_kb = await sell_nav_keyboard()
    msg2 = await cb.message.answer(
        (await get_text('return_to_menu', 'ru')) or "Return",
        reply_markup=nav_kb
    )
    last_bot_messages.setdefault(cb.message.chat.id, []).extend([msg1.message_id, msg2.message_id])
    await state.clear()
    await cb.answer()



@router.callback_query(Sell.confirm, F.data == "sell_cancel")
async def sell_cancel(cb: CallbackQuery, state: FSMContext):
    for d in (media_group_cache, media_group_tasks, media_group_wait_msg):
        d.clear()
    await state.clear()
    # Текст берём из БД, если нет — английский дефолт
    cancel_text = (await get_text('sell_cancelled', 'ru')) or "❌ Listing creation cancelled."
    await safe_edit_or_send(cb, cancel_text)
    await cb.answer()


# --- “Продано” — удаление ---
@router.callback_query(F.data.startswith("sell_sold:"))
async def mark_sold(cb: CallbackQuery, state: FSMContext):
    listing_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        l = await s.get(Listing, listing_id)
        if not l or l.owner_id != cb.from_user.id:
            # Берём текст из БД, дефолт — английский
            err_text = (await get_text('sell_delete_owner_only', 'ru')) or "Only the owner can delete!"
            await cb.answer(err_text, show_alert=True)
            return
    kb = delete_keyboard(listing_id)
    confirm_text = (await get_text('sell_delete_confirm', 'ru')) or "Are you sure you want to delete your listing? It will be permanently lost."
    msg = await cb.message.answer(
        confirm_text,
        reply_markup=kb
    )
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
    await cb.answer()


@router.callback_query(F.data.startswith("sell_delete_yes:"))
async def delete_yes(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    listing_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        l = await s.get(Listing, listing_id)
        if not l or l.owner_id != cb.from_user.id:
            err_text = (await get_text('sell_delete_only_owner', 'ru')) or "Error! Only the owner can delete."
            await cb.answer(err_text, show_alert=True)
            return
        await s.delete(l)
        await s.commit()
    # 1. Удаляем карточки объявлений, если они есть
    if my_listing_messages.get(chat_id):
        for msg_id in my_listing_messages[chat_id]:
            try:
                await cb.bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
        my_listing_messages[chat_id] = []

    # 2. Очищаем все служебные сообщения
    await clear_bot_messages(chat_id, cb.bot)
    last_bot_messages[chat_id] = []

    # 3. Отправляем новые сообщения и добавляем их в кэш
    deleted_text = (await get_text('sell_deleted', 'ru')) or "Listing deleted."
    msg = await cb.message.answer(deleted_text)
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)

    nav_kb = await sell_nav_keyboard()
    menu_text = (await get_text('return_to_menu', 'ru')) or "Return"
    msg2 = await cb.message.answer(menu_text, reply_markup=nav_kb)
    last_bot_messages.setdefault(chat_id, []).append(msg2.message_id)

    await cb.answer()





@router.callback_query(F.data.startswith("sell_delete_no:"))
async def delete_no(cb: CallbackQuery, state: FSMContext):
    cancel_text = (await get_text('sell_delete_cancel', 'ru')) or "Deletion canceled, listing is still active."
    await cb.message.answer(cancel_text)
    await cb.answer()


@router.callback_query(F.data == "sell_back")
async def sell_back_handler(cb: CallbackQuery, state: FSMContext):
    cur_state = await state.get_state()
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)

    if cur_state == Sell.city.state:
        await clear_bot_messages(chat_id, cb.bot)
        market_text = (await get_text('market_choose_action', 'ru')) or "💸 Marketplace:\nChoose an action."
        msg = await cb.message.answer(
            market_text,
            reply_markup=await market_inline()
        )
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await state.clear()
        await cb.answer()
        return


    if cur_state == Sell.cat.state:
        await clear_bot_messages(chat_id, cb.bot)
        await state.set_state(Sell.city)
        async with SessionLocal() as s:
            cities = (await s.execute(select(City))).scalars().all()
        kb = await cities_inline(cities)
        header = await get_text('sell_choose_city', 'ru') or "Create a listing.\nFirst, choose a city:"
        msg = await cb.message.answer(
            header,
            reply_markup=kb
        )
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)


    elif cur_state == Sell.title.state:
        await state.set_state(Sell.cat)
        data = await state.get_data()
        city_slug = data.get("city_slug")
        async with SessionLocal() as s:
            cat = (await s.execute(select(Category).where(Category.slug == "equip"))).scalar_one()
            subcats = (await s.execute(select(Category).where(Category.parent_id == cat.id))).scalars().all()
        kb = await equip_inline(subcats, city_slug)
        # --- Получаем текст из базы ---
        template = await get_text('sell_choose_category_back', 'ru') or "City: <b>{city_name}</b>\nChoose a category:"
        text = template.format(city_name=data.get('city_name'))
        try:
            await safe_edit_or_send(cb, 
                text,
                reply_markup=kb,
                parse_mode="HTML"
            )
        except Exception as e:
            try:
                await cb.message.delete()
            except Exception:
                pass
            msg = await cb.message.answer(
                text,
                reply_markup=kb,
                parse_mode="HTML"
            )
            last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)





    elif cur_state == Sell.price.state:
        await clear_bot_messages(chat_id, cb.bot)
        await state.set_state(Sell.title)
        template = await get_text('sell_ask_title', 'ru') or "Enter <b>listing title</b> (one line):"
        await send_with_nav(cb.message, template, parse_mode="HTML")

    elif cur_state == Sell.descr.state:
        await clear_bot_messages(chat_id, cb.bot)
        await state.set_state(Sell.price)
        template = await get_text('sell_ask_price', 'ru') or "Enter <b>price</b> (e.g.: 150 € or 12,000 rsd):"
        await send_with_nav(cb.message, template, parse_mode="HTML")

    elif cur_state == Sell.photo.state:
        await clear_bot_messages(chat_id, cb.bot)
        await state.set_state(Sell.descr)
        template = await get_text('sell_ask_descr', 'ru') or "Short description (or '-' to skip):"
        await send_with_nav(cb.message, template, parse_mode="HTML")

    elif cur_state == Sell.confirm.state:
        await clear_bot_messages(chat_id, cb.bot)
        await state.set_state(Sell.photo)
        await send_photo_prompt(cb.message, 0, state)

    else:
        await clear_bot_messages(chat_id, cb.bot)
        await state.set_state(Sell.city)
        async with SessionLocal() as s:
            cities = (await s.execute(select(City))).scalars().all()
        kb = await cities_inline(cities)
        template = await get_text('sell_create_start', 'ru') or "💸 Create a listing.\nFirst, choose a city:"
        await cb.message.answer(
            template,
            reply_markup=kb
        )
    await cb.answer()


@router.callback_query(F.data == "sell_city_back")
async def sell_city_back(cb: CallbackQuery, state: FSMContext):
    await clear_bot_messages(chat_id, cb.bot)
    await state.clear()
    async with SessionLocal() as s:
        cities = (await s.execute(select(City))).scalars().all()
    kb = await cities_inline(cities)
    header = await get_text('sell_choose_city', 'ru') or "Create a listing.\nFirst, choose a city:"
    await safe_edit_or_send(cb, 
        header,
        reply_markup=kb
    )
    last_bot_messages.setdefault(cb.message.chat.id, []).append(cb.message.message_id)
    await cb.answer()