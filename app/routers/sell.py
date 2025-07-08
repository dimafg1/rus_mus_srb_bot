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

from app.routers.utils import clear_bot_messages, last_bot_messages, sent_photo_messages



# ========== КЭШИ для альбомов ==========
media_group_cache = {}  # {group_id: [file_id, ...]}
media_group_tasks = {}                 # {group_id: asyncio.Task}
media_group_wait_msg = {}              # {group_id: wait_msg_id}


router = Router(name="sell")

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
    if text_tip:
        msg2 = await m.answer(text_tip)
        await state.update_data(photo_prompt_msgs=[msg.message_id, msg2.message_id])
    else:
        await state.update_data(photo_prompt_msgs=[msg.message_id])

async def delete_photo_prompts(m: Message, state: FSMContext):
    data = await state.get_data()
    for msg_id in data.get("photo_prompt_msgs", []):
        try:
            await m.bot.delete_message(m.chat.id, msg_id)
        except Exception:
            pass
    await state.update_data(photo_prompt_msgs=[])


async def cities_inline() -> InlineKeyboardMarkup:
    async with SessionLocal() as s:
        cities = (await s.execute(select(City))).scalars().all()
    rows = [[InlineKeyboardButton(text=c.name,
                                  callback_data=f"sell_city:{c.slug}")]
            for c in cities]
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def equip_inline(parent_id: int, city_slug: str) -> InlineKeyboardMarkup:
    async with SessionLocal() as s:
        cats = (await s.execute(
            select(Category).where(Category.parent_id == parent_id)
        )).scalars().all()
    rows = [
        [InlineKeyboardButton(text=c.name, callback_data=f"sell_cat:{city_slug}:{c.id}")]
        for c in cats
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def photo_keyboard(photo_count: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пропустить этот шаг", callback_data="sell_skip_photo")],
        [InlineKeyboardButton(text="Отмена", callback_data="sell_cancel")]
    ])

def confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data="sell_ok")],
        [InlineKeyboardButton(text="❌ Отменить",    callback_data="sell_cancel")],
    ])

def sold_keyboard(listing_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Продано", callback_data=f"sell_sold:{listing_id}")]
        ]
    )

def delete_keyboard(listing_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да, удалить", callback_data=f"sell_delete_yes:{listing_id}")],
            [InlineKeyboardButton(text="Нет, отменить", callback_data=f"sell_delete_no:{listing_id}")]
        ]
    )

# ─────────────────── /sell start ───────────
@router.message(Command(commands=["sell"]))
async def cmd_sell(m: Message, state: FSMContext):
    await clear_bot_messages(m.chat.id, m.bot)  # ОЧИСТКА ПЕРЕД ЗАПУСКОМ
    msg = await m.answer(
        "💸 Создать объявление.\nСначала выберите город:",
        reply_markup=await cities_inline()
    )
    last_bot_messages.setdefault(m.chat.id, []).append(msg.message_id)
    await state.set_state(Sell.city)






@router.callback_query(F.data == "sell_start")
async def sell_start_button(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)  # ДОБАВИТЬ
    await cmd_sell(cb.message, state)
    await cb.answer()


# ─────────────── шаг 1 – город ─────────────
@router.callback_query(F.data.startswith("sell_city:"), Sell.city)
async def sell_city(cb: CallbackQuery, state: FSMContext):
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    city_slug = cb.data.split(":")[1]
    async with SessionLocal() as s:
        city = (await s.execute(select(City).where(City.slug == city_slug))).scalar_one()
        equip_root = (await s.execute(select(Category).where(Category.slug == "equip"))).scalar_one()
    await state.update_data(city_id=city.id, city_name=city.name)
    msg = await cb.message.answer(
        f"Город: <b>{city.name}</b>\nВыберите категорию:",
        reply_markup=await equip_inline(equip_root.id, city_slug)
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
        has_children = (await s.execute(
            select(Category).where(Category.parent_id == cat_id)
        )).scalars().first()
    if has_children:
        msg = await cb.message.answer(
            f"Категория: <b>{cat.name}</b>\nВыберите подраздел:",
            reply_markup=await equip_inline(cat_id, city_slug),
        )
        last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)

        await state.update_data(cat_id=cat.id, cat_name=cat.name)
        await state.set_state(Sell.cat)

    else:
        await state.update_data(cat_id=cat.id, cat_name=cat.name)
        await state.set_state(Sell.title)
        msg = await cb.message.answer("Введите <b>заголовок</b> объявления (1 строка):")
        last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)


    await cb.answer()


# ─────────────── шаг 3 – title ─────────────
@router.message(Sell.title, F.text)
async def sell_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(Sell.price)
    msg = await m.answer("Укажите <b>цену</b> (например: 150 € или 12 000 rsd):")
    last_bot_messages.setdefault(m.chat.id, []).append(msg.message_id)

# ─────────────── шаг 4 – price ─────────────
@router.message(Sell.price, F.text)
async def sell_price(m: Message, state: FSMContext):
    await state.update_data(price=m.text.strip())
    await state.set_state(Sell.descr)
    msg = await m.answer("Короткое описание (или «-» чтобы пропустить):")
    last_bot_messages.setdefault(m.chat.id, []).append(msg.message_id)

# ─────────────── шаг 5 – descr ─────────────
@router.message(Sell.descr)
async def sell_descr(m: Message, state: FSMContext):
    text = m.text.strip()
    await state.update_data(descr=None if text == "-" else text)
    await state.set_state(Sell.photo)
    # Показываем первое приглашение (удалять нечего)
    await send_photo_prompt(m, 0, state)




# ================== **ШАГ 6 — ФОТО** ==================


# 2. Основной хендлер для одиночных фото и альбомов
@router.message(Sell.photo, F.photo)
async def sell_photo(m: Message, state: FSMContext):
    if m.media_group_id:
        group_id = m.media_group_id

        if group_id not in media_group_cache:
            media_group_cache[group_id] = []
        media_group_cache[group_id].append(m.photo[-1].file_id)

        # Защита от дублей: сразу регистрируем заглушку
        if group_id not in media_group_tasks:
            media_group_tasks[group_id] = None  # Ставим замок!
            wait_msg = await m.answer("⏳ Пожалуйста, подождите — загружаем фотографии…")
            media_group_wait_msg[group_id] = wait_msg.message_id
            media_group_tasks[group_id] = asyncio.create_task(finalize_album(m, state, group_id))
        return

    # --- Одиночное фото ---
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

    # Удалить ⏳ сообщение
    wait_msg_id = media_group_wait_msg.pop(group_id, None)
    if wait_msg_id:
        try:
            await m.bot.delete_message(m.chat.id, wait_msg_id)
        except Exception:
            pass

    if len(photos) >= 3:
        # После альбома — сразу предпросмотр!
        await preview_and_confirm(m, state)
        await state.set_state(Sell.confirm)
    elif len(photos) == 2:
        # Приглашение добавить ещё 1 фото!
        await send_photo_prompt(m, len(photos), state)
        await state.set_state(Sell.photo)
    elif len(photos) == 1:
        # Приглашение добавить ещё 2 фото!
        await send_photo_prompt(m, len(photos), state)
        await state.set_state(Sell.photo)



# 1. Сначала "НЕ ФОТО" (обязателен порядок!)
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


# ──────────── Кнопки для фото ──────────────

@router.callback_query(Sell.photo, F.data == "sell_skip_photo")
async def sell_skip_photo(cb: CallbackQuery, state: FSMContext):
    await delete_photo_prompts(cb.message, state)
    await preview_and_confirm(cb.message, state)
    await state.set_state(Sell.confirm)
    await cb.answer()


@router.callback_query(Sell.photo, F.data == "sell_cancel")
async def sell_cancel_photo(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Создание объявления отменено.")
    await cb.answer()

# ─────────────── предпросмотр + confirm ────
async def preview_and_confirm(m: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    header = (f"<b>{data['city_name']} → {data['cat_name']}</b>\n"
              f"{data['title']} — {data['price']}\n"
              f"{data.get('descr','')}")
    kb = confirm_keyboard()
    if photos:
        if len(photos) == 1:
            await m.answer_photo(photos[0])
        else:
            media = [InputMediaPhoto(media=fid) for fid in photos]
            await m.answer_media_group(media)
    msg = await m.answer(header, reply_markup=kb)
    last_bot_messages.setdefault(m.chat.id, []).append(msg.message_id)

# ─────────────── шаг 7 – confirm ───────────
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
    await cb.message.edit_text("✅ Объявление опубликовано!")
    last_bot_messages.setdefault(cb.message.chat.id, []).append(cb.message.message_id)

    await state.clear()
    await cb.answer()

@router.callback_query(Sell.confirm, F.data == "sell_cancel")
async def sell_cancel(cb: CallbackQuery, state: FSMContext):
    for d in (media_group_cache, media_group_tasks, media_group_wait_msg):
        d.clear()
    await state.clear()
    await cb.message.edit_text("❌ Создание объявления отменено.")
    await cb.answer()

# ─────────────── “Продано” — удаление ──────
@router.callback_query(F.data.startswith("sell_sold:"))
async def mark_sold(cb: CallbackQuery, state: FSMContext):
    listing_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        l = await s.get(Listing, listing_id)
        if not l or l.owner_id != cb.from_user.id:
            await cb.answer("Только владелец может удалить!", show_alert=True)
            return
    await cb.message.answer(
        "Вы уверены, что хотите удалить своё объявление? Оно будет утеряно безвозвратно.",
        reply_markup=delete_keyboard(listing_id)
    )
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
    await cb.message.edit_text("Объявление удалено.")
    await cb.answer()

@router.callback_query(F.data.startswith("sell_delete_no:"))
async def delete_no(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Удаление отменено, объявление осталось активным.")
    await cb.answer()

@router.callback_query(F.data == "sell_start")
async def sell_start_button(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)   # Удалить "мусор" с экрана
    await cmd_sell(cb.message, state)           # Перейти к созданию объявления
    await cb.answer()
