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
# --- Доп. поля категории (универсальный опрос) ---
# Импортируем функцию запуска мастера доп. полей и константу VAL_KEY
from app.routers.user_extra_fields import start_extra_fields_for_category, VAL_KEY

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
from app.routers.utils import safe_edit_or_send
import json

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

# --- Чтение JSON-полей категории (как в админке) ---
async def load_category_fields(session, cat_id: int) -> list[dict]:
    cat = (await session.execute(select(Category).where(Category.id == cat_id))).scalar_one()
    try:
        raw = (cat.fields or "").strip()
        data = json.loads(raw) if raw else []
        return data if isinstance(data, list) else []
    except Exception:
        return []

# --- Запуск сценария гибких полей после фото ---
async def start_flex_flow(m_or_cbmsg, state: FSMContext):
    chat_id = m_or_cbmsg.chat.id
    data = await state.get_data()
    after_pub = bool(data.get("extras_after_publish"))  # режим "после публикации"

    async with SessionLocal() as s:
        cat_id = int(data["cat_id"])
        raw_fields = await load_category_fields(s, cat_id)

    supported = {"text", "number", "select", "checkbox"}
    fields: list[dict] = []
    for f in raw_fields or []:
        if isinstance(f, dict) and str(f.get("type","text")).lower() in supported:
            fld = {
                "type": str(f.get("type","text")).lower(),
                "label": (str(f.get("label") or "") or "Поле").strip(),
                "key": (str(f.get("key") or "field")).strip().lower() or "field",
                "required": bool(f.get("required", False)),
            }
            if fld["type"] == "select":
                opts = f.get("options") if isinstance(f.get("options"), list) else []
                fld["options"] = [str(o).strip() for o in opts if str(o).strip()]
            fields.append(fld)

    if not fields:
        # если полей нет: в режиме "после публикации" — просто спасибо + навигация
        if after_pub:
            await clear_bot_messages(chat_id, m_or_cbmsg.bot)
            nav_kb = await sell_nav_keyboard()
            msg = await m_or_cbmsg.answer("Готово. Спасибо!", reply_markup=nav_kb)
            last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
            await state.clear()
            return
        # иначе ведём в обычный превью/подтверждение
        await preview_and_confirm(m_or_cbmsg, state)
        await state.set_state(Sell.confirm)
        return

    await state.update_data(flex_fields=fields, flex_idx=0, flex_values={})
    await ask_current_flex_field(m_or_cbmsg, state)


# --- Показ текущего гибкого поля ---
async def ask_current_flex_field(m_or_cbmsg, state: FSMContext):
    chat_id = m_or_cbmsg.chat.id
    bot = m_or_cbmsg.bot
    await clear_bot_messages(chat_id, bot)

    data = await state.get_data()
    fields = data.get("flex_fields", []) or []
    idx = int(data.get("flex_idx", 0))
    after_pub = bool(data.get("extras_after_publish"))

    # финал опроса
    if idx >= len(fields):
        if after_pub:
            nav_kb = await sell_nav_keyboard()
            msg = await m_or_cbmsg.answer("Готово. Спасибо!", reply_markup=nav_kb)
            last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
            await state.clear()
            print(f"FUNC: ask_current_flex_field | done(after_pub) | chat_id: {chat_id}")
            return
        # обычный сценарий (до публикации)
        await preview_and_confirm(m_or_cbmsg, state)
        await state.set_state(Sell.confirm)
        print(f"FUNC: ask_current_flex_field | done(preview) | chat_id: {chat_id}")
        return

    f = fields[idx]
    label = f.get("label") or "Поле"
    ftype = f.get("type")
    required = bool(f.get("required"))

    rows = []

    if ftype == "select":
        for i, opt in enumerate(f.get("options", [])):
            rows.append([InlineKeyboardButton(text=str(opt), callback_data=f"sell_flex_select:{i}")])
        if not required:
            rows.append([InlineKeyboardButton(text="Пропустить", callback_data="sell_flex_skip")])
        prompt = f"({idx+1}/{len(fields)}) <b>{label}</b>\n\nВыберите один из вариантов:"
    elif ftype == "checkbox":
        rows.append([
            InlineKeyboardButton(text="✅ Да",  callback_data="sell_flex_checkbox:1"),
            InlineKeyboardButton(text="❌ Нет", callback_data="sell_flex_checkbox:0"),
        ])
        if not required:
            rows.append([InlineKeyboardButton(text="Пропустить", callback_data="sell_flex_skip")])
        prompt = f"({idx+1}/{len(fields)}) <b>{label}</b>\n\nВыберите вариант:"
    else:
        if not required:
            rows.append([InlineKeyboardButton(text="Пропустить", callback_data="sell_flex_skip")])
        prompt = f"({idx+1}/{len(fields)}) <b>{label}</b>\n\nВведите значение" + (" (число)." if ftype == "number" else ".")

    # низ: Назад / Главное меню
    back_btn = await get_common_menu_button('sell_back', 'ru')
    main_btn = await get_common_menu_button('main_menu', 'ru')
    nav = []
    if back_btn: nav.append(back_btn)
    if main_btn: nav.append(main_btn)
    if nav: rows.append(nav)

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    msg = await m_or_cbmsg.answer(prompt, reply_markup=kb, parse_mode="HTML")
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)

    await state.set_state(Sell.flex)
    print(f"FUNC: ask_current_flex_field | ask | chat_id: {chat_id} | idx: {idx} | type: {ftype} | msg_id: {msg.message_id}")



# ─────────────────── FSM ───────────────────
class Sell(StatesGroup):
    city    = State()
    cat     = State()
    title   = State()
    price   = State()
    descr   = State()
    photo   = State()
    flex    = State()
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
        # Лист категории: сначала спрашиваем доп. поля для этой категории,
        # а после завершения опроса продолжим на "sell:extras_done".
        # await state.update_data(cat_id=cat.id, cat_name=cat.name)
        # await start_extra_fields_for_category(cb, state, cat.id, resume_data="sell:extras_done")
        # await cb.answer()
        # return
        await state.update_data(cat_id=cat.id, cat_name=cat.name)
        await state.set_state(Sell.title)
        template = await get_text('sell_ask_title', 'ru') or "Enter <b>listing title</b> (one line):"
        await send_with_nav(cb.message, template, parse_mode="HTML")
        await cb.answer()

    
# ====== Продолжить после опроса доп. полей ======
@router.callback_query(F.data == "sell:extras_done")
async def sell_extras_done(cb: CallbackQuery, state: FSMContext):
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    await state.set_state(Sell.title)
    template = await get_text('sell_ask_title', 'ru') or "Enter <b>listing title</b> (one line):"
    # используем уже готовый помощник
    await send_with_nav(cb.message, template, parse_mode="HTML")
    await cb.answer()
    print(f"FUNC: sell_extras_done | chat_id: {cb.message.chat.id} | user_id: {cb.from_user.id}")



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

# ================== ФИНАЛИЗАЦИЯ АЛЬБОМА ==================
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

    # ВАЖНО: здесь ТОЛЬКО m, НИ КАКОГО cb
    if len(photos) >= 3:
        await delete_photo_prompts(m, state)
        await preview_and_confirm(m, state)
        await state.set_state(Sell.confirm)
    else:
        await send_photo_prompt(m, len(photos), state)
        await state.set_state(Sell.photo)

    print(f"FUNC: finalize_album | chat_id: {m.chat.id} | user_id: {m.from_user.id} | photos={len(photos)}")


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
    # await start_flex_flow(cb.message, state)
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

# === Гибкие поля: select ===
@router.callback_query(Sell.flex, F.data.startswith("sell_flex_select:"))
async def flex_select_pick(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    fields = data.get("flex_fields", []) or []
    idx = int(data.get("flex_idx", 0))
    if idx >= len(fields) or fields[idx].get("type") != "select":
        await cb.answer("Неверное действие", show_alert=True); return

    try:
        opt_idx = int(cb.data.split(":")[1])
    except Exception:
        await cb.answer("Неверные данные", show_alert=True); return

    options = fields[idx].get("options", [])
    if opt_idx < 0 or opt_idx >= len(options):
        await cb.answer("Неверная опция", show_alert=True); return

    key = fields[idx]["key"]
    val = str(options[opt_idx])

    flex_values = data.get("flex_values", {}) or {}
    flex_values[key] = val
    await state.update_data(flex_values=flex_values, flex_idx=idx + 1)

    await cb.answer()
    await ask_current_flex_field(cb.message, state)


# === Гибкие поля: checkbox ===
@router.callback_query(Sell.flex, F.data.startswith("sell_flex_checkbox:"))
async def flex_checkbox_pick(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    fields = data.get("flex_fields", []) or []
    idx = int(data.get("flex_idx", 0))
    if idx >= len(fields) or fields[idx].get("type") != "checkbox":
        await cb.answer("Неверное действие", show_alert=True); return

    raw = cb.data.split(":")[1]
    val = True if raw == "1" else False
    key = fields[idx]["key"]

    flex_values = data.get("flex_values", {}) or {}
    flex_values[key] = val
    await state.update_data(flex_values=flex_values, flex_idx=idx + 1)

    await cb.answer()
    await ask_current_flex_field(cb.message, state)


# === Гибкие поля: пропуск (для необязательных) ===
@router.callback_query(Sell.flex, F.data == "sell_flex_skip")
async def flex_skip(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    fields = data.get("flex_fields", []) or []
    idx = int(data.get("flex_idx", 0))
    if idx >= len(fields):
        await cb.answer(); return
    # просто двигаем индекс дальше, ничего не записывая
    await state.update_data(flex_idx=idx + 1)
    await cb.answer("Пропущено")
    await ask_current_flex_field(cb.message, state)


# === Гибкие поля: ввод текст/число сообщением ===
@router.message(Sell.flex, F.text)
async def flex_text_number_input(m: Message, state: FSMContext):
    data = await state.get_data()
    fields = data.get("flex_fields", []) or []
    idx = int(data.get("flex_idx", 0))

    if idx >= len(fields):
        # на всякий случай завершим
        await preview_and_confirm(m, state)
        await state.set_state(Sell.confirm)
        return

    f = fields[idx]
    ftype = f.get("type")
    key = f.get("key")

    # только text/number принимаем текстом
    if ftype not in {"text", "number"}:
        await m.answer("Пожалуйста, воспользуйтесь кнопками ниже.")
        return

    value = (m.text or "").strip()
    if ftype == "number":
        # мягкая валидация: пустое запрещаем, остальное — как есть
        if not value:
            await m.answer("Введите число или нажмите «Пропустить».")
            return

    flex_values = data.get("flex_values", {}) or {}
    flex_values[key] = value
    await state.update_data(flex_values=flex_values, flex_idx=idx + 1)

    await ask_current_flex_field(m, state)


# --- Предпросмотр + confirm ---
async def preview_and_confirm(m: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    header = (f"<b>{data['city_name']} → {data['cat_name']}</b>\n"
              f"{data['title']} — {data['price']}\n"
              f"{data.get('descr','') or ''}")

    # Добавляем собранные гибкие поля (только заполненные пользователем или обязательные)
    flex_fields = data.get("flex_fields") or []
    flex_values = data.get("flex_values") or {}

    if flex_fields:
        parts = []
        for f in flex_fields:
            label = f.get("label") or "Поле"
            key = f.get("key")
            ftype = f.get("type")
            required = bool(f.get("required"))
            if key in flex_values:
                v = flex_values[key]
                if ftype == "checkbox":
                    v = "Да" if bool(v) else "Нет"
                parts.append(f"{label}: {v}")
            else:
                # не показываем пустые необязательные поля
                if required:
                    parts.append(f"{label}: —")
        if parts:
            header += "\n\n" + "\n".join(parts)

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
    msg = await m.answer(header, reply_markup=kb, parse_mode="HTML")
    last_bot_messages.setdefault(m.chat.id, []).append(msg.message_id)
    if sent_ids:
        sent_photo_messages.setdefault(m.chat.id, []).extend(sent_ids)


# ---------- Публикация объявления (без изменений основного сценария) ----------
@router.callback_query(Sell.confirm, F.data == "sell_ok")
async def sell_ok(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # Подчистим временные кеши альбома (безопасно)
    for d in (media_group_cache, media_group_tasks, media_group_wait_msg):
        try:
            d.clear()
        except Exception:
            pass

    data = await state.get_data()
    # Жёсткая проверка обязательных полей
    for k in ("city_id", "cat_id", "title", "price"):
        if not data.get(k):
            await cb.answer(f"Не хватает поля: {k}", show_alert=True)
            print(f"FUNC: sell_ok | MISSING {k} | data_keys={list(data.keys())}")
            return

    try:
        async with SessionLocal() as s:
            # Собираем данные для нового объявления
            l = Listing(
                city_id=int(data["city_id"]),
                category_id=int(data["cat_id"]),
                owner_id=cb.from_user.id,
                title=data["title"],
                price=data["price"],
                descr=data.get("descr"),
                contact=(f"@{cb.from_user.username}" if cb.from_user.username else "контакт не указан"),
                created_at=datetime.utcnow(),
                photo_file_id=",".join(data.get("photos", [])) if data.get("photos") else None,
            )

            # Если доп. поля были пройдены до подтверждения — сохраним сразу
            flex_data = data.get("flex_values")
            if flex_data:
                try:
                    l.flex = json.dumps(flex_data, ensure_ascii=False)
                except Exception:
                    l.flex = None

            s.add(l)
            await s.commit()
            await s.refresh(l)

            # Сохраняем id для последующего сохранения flex после публикации
            await state.update_data(listing_id=l.id)

        # Убираем служебные сообщения
        await clear_bot_messages(chat_id, cb.bot)

        # Экран «опубликовано» + предложение про доп. поля
        text_pub   = (await get_text('sell_published', 'ru')) or "✅ Объявление опубликовано! -db"
        text_extra = (await get_text('sell_extras_offer', 'ru')) or "При желании укажите дополнительные сведения для этой категории:"
        if isinstance(text_extra, str) and text_extra.strip().startswith("[Text not found"):
            text_extra = "При желании укажите дополнительные сведения для этой категории:"

        # Верхние действия
        rows = [
            [InlineKeyboardButton(
                text="🧩 Заполнить дополнительные поля",
                callback_data=f"sell_extras_start:{data['cat_id']}"
            )],
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="sell_extras_skip")],
        ]

        # НИЗ: «Барахолка» + «Главное меню» (без «Назад»)
        market_btn = await get_common_menu_button('go_market', 'ru')  # код из вашего главного меню
        main_btn   = await get_common_menu_button('main_menu', 'ru')

        nav = []
        if market_btn:
            nav.append(InlineKeyboardButton(text=market_btn.text, callback_data=market_btn.callback_data))
        else:
            nav.append(InlineKeyboardButton(text="💸 Барахолка", callback_data="go_market"))
        if main_btn:
            nav.append(InlineKeyboardButton(text=main_btn.text, callback_data=main_btn.callback_data))
        if nav:
            rows.append(nav)

        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        msg = await cb.message.answer(f"{text_pub}\n\n{text_extra}", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]

        # Стейт не чистим — нужен для доп. полей
        await cb.answer()
        print(f"FUNC: sell_ok | SAVED listing_id={l.id} | chat_id={chat_id} | user_id={cb.from_user.id} | msg_id={msg.message_id}")

    except Exception as e:
        await cb.answer(f"Ошибка сохранения: {type(e).__name__}", show_alert=True)
        await cb.message.answer(f"❌ Не удалось сохранить объявление.\n<code>{e}</code>", parse_mode="HTML")
        print(f"FUNC: sell_ok | ERROR {e}")

# ====== Доп. поля: старт мастера (после публикации) ======
@router.callback_query(F.data.startswith("sell_extras_start:"))
async def sell_extras_start_after_pub(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    try:
        cat_id = int(cb.data.split(":", 1)[1])
    except Exception:
        await cb.answer("Нет данных категории.", show_alert=True)
        print("FUNC: sell_extras_start_after_pub | error: no cat_id")
        return
    # ЗАПУСК БЕЗ resume_data -> не будет «Продолжить»
    await start_extra_fields_for_category(cb, state, cat_id, resume_data=None)
    await cb.answer()
    print(f"FUNC: sell_extras_start_after_pub | cat_id={cat_id} | user_id={cb.from_user.id}")

# ====== Доп. поля: завершение мастера (после публикации) ======
@router.callback_query(F.data == "sell:extras_done_after_pub")
async def sell_extras_done_after_pub(cb: CallbackQuery, state: FSMContext):
    """
    Завершение мастера дополнительных полей после публикации. На этом этапе
    пользователь ввёл необходимые значения, и их нужно сохранить в
    соответствующее объявление в колонке flex. Мы извлекаем из FSM
    идентификатор объявления, а также словарь значений дополнительных
    полей (они сохраняются в FSM под ключом VAL_KEY в модуле
    user_extra_fields). После обновления записи в БД отправляем
    пользователю сообщение о завершении и очищаем стейт.
    """
    # Готовим нижнюю навигацию: назад/главное меню
    back_btn = await get_common_menu_button('sell_back', 'ru')
    main_btn = await get_common_menu_button('main_menu', 'ru')
    rows = []
    row = []
    if back_btn:
        row.append(InlineKeyboardButton(text=back_btn.text, callback_data=back_btn.callback_data))
    if main_btn:
        row.append(InlineKeyboardButton(text=main_btn.text, callback_data=main_btn.callback_data))
    if row:
        rows.append(row)
    kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    # Читаем текущие данные FSM: сюда входит listing_id, а также
    # ответы пользователя на дополнительные поля под ключом VAL_KEY
    data = await state.get_data()
    listing_id = data.get("listing_id")
    extra_values = data.get(VAL_KEY) or {}

    # Обновляем запись объявления, если id найден
    if listing_id:
        try:
            async with SessionLocal() as s:
                l = await s.get(Listing, listing_id)
                if l:
                    # сериализуем словарь в JSON; если словарь пустой, оставляем None
                    l.flex = json.dumps(extra_values, ensure_ascii=False) if extra_values else None
                    # добавляем запись обратно в сессию и коммитим
                    s.add(l)
                    await s.commit()
                else:
                    # Такой записи нет; просто продолжаем
                    pass
        except Exception as e:
            # В случае ошибки записываем в лог. Для пользователя
            # выводим уведомление о том, что сохранение не удалось.
            await cb.message.answer(
                f"❌ Не удалось сохранить доп. поля: <code>{type(e).__name__}</code>",
                parse_mode="HTML"
            )

    # Сообщаем пользователю о завершении и очищаем FSM
    await cb.message.answer("Готово. Спасибо!", reply_markup=kb)
    await state.clear()
    await cb.answer()
    print(f"FUNC: sell_extras_done_after_pub | user_id={cb.from_user.id} | listing_id={listing_id} | extras={bool(extra_values)}")

# ====== Доп. поля: «Пропустить» (после публикации) ======
@router.callback_query(F.data == "sell_extras_skip")
async def sell_extras_skip(cb: CallbackQuery, state: FSMContext):
    back_btn = await get_common_menu_button('sell_back', 'ru')
    main_btn = await get_common_menu_button('main_menu', 'ru')
    rows = []
    row = []
    if back_btn:
        row.append(InlineKeyboardButton(text=back_btn.text, callback_data=back_btn.callback_data))
    if main_btn:
        row.append(InlineKeyboardButton(text=main_btn.text, callback_data=main_btn.callback_data))
    if row:
        rows.append(row)
    kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    await cb.message.answer("Ок, без дополнительных сведений.", reply_markup=kb)
    await state.clear()
    await cb.answer()
    print(f"FUNC: sell_extras_skip | user_id={cb.from_user.id}")

# ====== Доп. поля: старт мастера ПОСЛЕ публикации (из единой кнопки) ======
# @router.callback_query(F.data == "sell_extras_start")
# async def sell_extras_start(cb: CallbackQuery, state: FSMContext):
#     # помечаем режим "после публикации", чтобы финал не вёл на повторное подтверждение
#     await state.update_data(extras_after_publish=True)
#     # грузим поля и переходим к первому вопросу
#     await start_flex_flow(cb.message, state)
#     await cb.answer()
#     print(f"FUNC: sell_extras_start | chat_id={cb.message.chat.id} | user_id={cb.from_user.id}")



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

    elif cur_state == Sell.flex.state:
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
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    await state.clear()

    async with SessionLocal() as s:
        cities = (await s.execute(select(City))).scalars().all()

    kb = await cities_inline(cities)
    header = await get_text('sell_choose_city', 'ru') or "Create a listing.\nFirst, choose a city:"
    await safe_edit_or_send(cb, header, reply_markup=kb)
    await cb.answer()
    print(f"FUNC: sell_city_back | chat_id: {chat_id} | user_id: {cb.from_user.id}")
