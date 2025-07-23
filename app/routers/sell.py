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

router = Router(name="sell")

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
    nav_msg = await m.answer("⬅️ Назад | ☰ Главное меню", reply_markup=nav_markup)
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
async def send_photo_prompt(m: Message, photo_count: int, state: FSMContext):
    left = 3 - photo_count
    if photo_count == 0:
        text_main = (
            "Пришлите <b>фото</b> (до 3 штук). Можно все сразу, можно по очереди.\n"
            "Если вы выделите больше трёх, прикреплены будут только первые три."
        )
        text_tip = "Для загрузки фото нажмите на 📎 слева от строки для сообщений\n⬇️"
    elif left == 2:
        text_main = (
            "Фото добавлено (1/3).\n"
            "Вы можете добавить ещё <b>2 фото</b>, пропустить этот шаг или отменить публикацию."
        )
        text_tip = "Чтобы добавить ещё фото, снова нажмите на 📎 слева\n⬇️"
    elif left == 1:
        text_main = (
            "Фото добавлено (2/3).\n"
            "Вы можете добавить ещё <b>1 фото</b>, пропустить этот шаг или отменить публикацию."
        )
        text_tip = "Чтобы добавить ещё фото, снова нажмите на 📎 слева\n⬇️"
    else:
        text_main = "Что-то пошло не так! Максимум фото — 3."
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
    msg = await m.answer(
        "💸 Создать объявление.\nСначала выберите город:",
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
    msg = await cb.bot.send_message(
        chat_id,
        "💸 Создать объявление.\nСначала выберите город:",
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
    msg = await cb.message.answer(
        f"Город: <b>{city.name}</b>\nВыберите категорию:",
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
        msg = await cb.message.answer(
            f"Категория: <b>{cat.name}</b>\nВыберите подраздел:",
            reply_markup=kb,
        )
        last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
        await state.update_data(cat_id=cat.id, cat_name=cat.name)
        await state.set_state(Sell.cat)
    else:
        await clear_bot_messages(cb.message.chat.id, cb.bot)
        await state.update_data(cat_id=cat.id, cat_name=cat.name)
        await state.set_state(Sell.title)
        await send_with_nav(cb.message, "Введите <b>заголовок</b> объявления (1 строка):", parse_mode="HTML")
    await cb.answer()

# ─────────────── шаг 3 – title ─────────────
@router.message(Sell.title, F.text)
async def sell_title(m: Message, state: FSMContext):
    await clear_bot_messages(m.chat.id, m.bot)
    await state.update_data(title=m.text.strip())
    await state.set_state(Sell.price)
    await send_with_nav(m, "Укажите <b>цену</b> (например: 150 € или 12 000 rsd):", parse_mode="HTML")

# ─────────────── шаг 4 – price ─────────────
@router.message(Sell.price, F.text)
async def sell_price(m: Message, state: FSMContext):
    await clear_bot_messages(m.chat.id, m.bot)
    await state.update_data(price=m.text.strip())
    await state.set_state(Sell.descr)
    await send_with_nav(m, "Короткое описание (или «-» чтобы пропустить):", parse_mode="HTML")

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
            wait_msg = await m.answer("⏳ Пожалуйста, подождите — загружаем фотографии…")
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
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить это сообщение", callback_data=f"delmsg:{m.message_id}")]
        ]
    )
    await m.answer(
        "❗️Пожалуйста, отправьте только фотографию (или видео). Если выбрали не тот тип, используйте скрепку и выберите Фото/Видео.",
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
    await cb.answer("Сообщение удалено.")

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
    msg1 = await cb.message.answer("❌ Создание объявления отменено.")
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg1.message_id)
    msg2 = await cb.message.answer("☰ Главное меню", reply_markup=await sell_nav_keyboard())
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
    msg = await cb.message.answer("✅ Объявление опубликовано!")
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
    nav_kb = await sell_nav_keyboard()
    await cb.message.answer("☰ Главное меню", reply_markup=nav_kb)
    await state.clear()
    await cb.answer()

@router.callback_query(Sell.confirm, F.data == "sell_cancel")
async def sell_cancel(cb: CallbackQuery, state: FSMContext):
    for d in (media_group_cache, media_group_tasks, media_group_wait_msg):
        d.clear()
    await state.clear()
    await cb.message.edit_text("❌ Создание объявления отменено.")
    await cb.answer()

# --- “Продано” — удаление ---
@router.callback_query(F.data.startswith("sell_sold:"))
async def mark_sold(cb: CallbackQuery, state: FSMContext):
    listing_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        l = await s.get(Listing, listing_id)
        if not l or l.owner_id != cb.from_user.id:
            await cb.answer("Только владелец может удалить!", show_alert=True)
            return
    kb = delete_keyboard(listing_id)
    msg = await cb.message.answer(
        "Вы уверены, что хотите удалить своё объявление? Оно будет утеряно безвозвратно.",
        reply_markup=kb
    )
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
    await cb.answer()

@router.callback_query(F.data.startswith("sell_delete_yes:"))
async def delete_yes(cb: CallbackQuery, state: FSMContext):
    listing_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        l = await s.get(Listing, listing_id)
        if not l or l.owner_id != cb.from_user.id:
            await cb.answer("Ошибка! Только владелец может удалить.", show_alert=True)
            return
        await s.delete(l)
        await s.commit()
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    msg = await cb.message.answer("Объявление удалено.")
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
    nav_kb = await sell_nav_keyboard()
    await cb.message.answer("☰ Главное меню", reply_markup=nav_kb)
    await cb.answer()

@router.callback_query(F.data.startswith("sell_delete_no:"))
async def delete_no(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Удаление отменено, объявление осталось активным.")
    await cb.answer()

@router.callback_query(F.data == "sell_start")
async def sell_start_button(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    await cmd_sell(cb.message, state)
    await cb.answer()

@router.callback_query(F.data == "sell_back")
async def sell_back_handler(cb: CallbackQuery, state: FSMContext):
    cur_state = await state.get_state()
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)

    if cur_state == Sell.city.state:
        msg = await cb.message.answer(
            "💸 Барахолка:\nВыберите действие.",
            reply_markup=await market_inline()
        )
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await state.clear()
        await cb.answer()
        return

    if cur_state == Sell.cat.state:
        await state.set_state(Sell.city)
        async with SessionLocal() as s:
            cities = (await s.execute(select(City))).scalars().all()
        kb = await cities_inline(cities)
        msg = await cb.message.answer(
            "💸 Создать объявление.\nСначала выберите город:",
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
        await cb.message.answer(
            f"Город: <b>{data.get('city_name')}</b>\nВыберите категорию:",
            reply_markup=kb
        )

    elif cur_state == Sell.price.state:
        await state.set_state(Sell.title)
        await send_with_nav(cb.message, "Введите <b>заголовок</b> объявления (1 строка):", parse_mode="HTML")

    elif cur_state == Sell.descr.state:
        await state.set_state(Sell.price)
        await send_with_nav(cb.message, "Укажите <b>цену</b> (например: 150 € или 12 000 rsd):", parse_mode="HTML")

    elif cur_state == Sell.photo.state:
        await state.set_state(Sell.descr)
        await send_with_nav(cb.message, "Короткое описание (или «-» чтобы пропустить):", parse_mode="HTML")

    elif cur_state == Sell.confirm.state:
        await state.set_state(Sell.photo)
        await send_photo_prompt(cb.message, 0, state)

    else:
        await state.set_state(Sell.city)
        async with SessionLocal() as s:
            cities = (await s.execute(select(City))).scalars().all()
        kb = await cities_inline(cities)
        await cb.message.answer(
            "💸 Создать объявление.\nСначала выберите город:",
            reply_markup=kb
        )
    await cb.answer()

@router.callback_query(F.data == "sell_city_back")
async def sell_city_back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    async with SessionLocal() as s:
        cities = (await s.execute(select(City))).scalars().all()
    kb = await cities_inline(cities)
    await cb.message.edit_text(
        "💸 Создать объявление.\nСначала выберите город:",
        reply_markup=kb
    )
    last_bot_messages.setdefault(cb.message.chat.id, []).append(cb.message.message_id)
    await cb.answer()
