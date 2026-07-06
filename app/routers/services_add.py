# app/routers/services_add.py
# ─────────────────────────────────────────────────────────────────────────────
# Публикация услуги (логика как в Барахолке):
# Город → Категория → Заголовок → Описание → Стоимость → Фото → Предпросмотр → Публикация
# Каноны: чистим чат, короткое описание РУ перед функциями/хендлерами, print в конце.
# На каждом шаге (кроме списков) выводим плашку «Возврат -db» (собственный callback services:back).
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations
import asyncio, json
from datetime import datetime
from app.models import utcnow_naive

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, func

from app.database import SessionLocal
from app.models import City, Category, Listing
from html import escape
from collections import defaultdict

# Утилиты/кейши (как в Барахолке)
from app.routers.utils import (
    clear_bot_messages,
    safe_edit_or_send,
    last_bot_messages,
    sent_photo_messages,
    delete_photo_prompts,
    get_text,
)

# Клавиатуры (как в Барахолке)
from app.keyboards import (
    get_common_menu_button,
    photo_keyboard,
    confirm_keyboard,
    cities_inline,  # не используем напрямую, но оставлю для единообразия
)

from app.routers.user_extra_fields import start_extra_fields_for_category

from app.routers.utils_category_title import format_category_title

from app.routers.utils_kb import grid3


router = Router(name="services_add")
SERVICES_ROOT_CATEGORY_ID = 80  # корень дерева категорий «Услуги»

# ========== КЭШИ для альбомов (своё пространство имён!) ==========
media_group_cache: dict[int, list[str]] = {}
media_group_tasks: dict[int, asyncio.Task] = {}
media_group_wait_msg: dict[int, int] = {}

# RU: фиксируем и чистим пользовательские медиа-сообщения (фото/видео/URL)
_user_media_msgs = defaultdict(list)

async def _remember_and_delete_user_media(msg: Message):
    """Запомнить и удалить пользовательское медиа/текст (с URL-превью)."""
    try:
        _user_media_msgs[msg.chat.id].append(msg.message_id)
    except Exception:
        pass
    try:
        await msg.delete()
    except Exception:
        pass

async def _clear_user_media(chat_id: int, bot):
    """Дочистить все запомненные пользовательские сообщения (на всякий случай)."""
    ids = _user_media_msgs.pop(chat_id, [])
    for mid in ids:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass




# ─────────────────────────────────────────────────────────────────────────────
# FSM: этапы мастера создания услуги
class ServiceForm(StatesGroup):
    title   = State()
    descr   = State()
    price   = State()
    photo   = State()
    flex    = State()
    confirm = State()


# ─────────────────────────────────────────────────────────────────────────────
# Плашка «Возврат -db» (в собственном неймспейсе services:*)

async def _services_return_kb(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура плашки 'Возврат -db' (⬅️ Назад / ≡ Главное меню)."""
    back_btn = await get_common_menu_button('sell_back', lang)
    main_btn = await get_common_menu_button('main_menu', lang)
    rows = []
    if back_btn:
        rows.append([InlineKeyboardButton(text=back_btn.text, callback_data="services:back")])
    if main_btn:
        rows.append([main_btn])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def _send_with_services_nav(m: Message, text: str, reply_markup=None, parse_mode=None):
    """Плашка 'Возврат -db' + текст шага; оба message_id кладём в кеш для дальнейшей очистки."""
    nav_text = await get_text('return_to_menu', 'ru') or "Возврат -db"
    nav_kb   = await _services_return_kb()
    msg_nav  = await m.answer(nav_text, reply_markup=nav_kb)
    last_bot_messages.setdefault(m.chat.id, []).append(msg_nav.message_id)
    await register_bot_messages(m.chat.id, [msg_nav.message_id])

    msg = await m.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    last_bot_messages.setdefault(m.chat.id, []).append(msg.message_id)
    await register_bot_messages(m.chat.id, [msg.message_id])
    return msg_nav, msg


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные клавиатуры для шага «Стоимость»

def _deal_price_kb() -> InlineKeyboardMarkup:
    """Кнопка «Договорная» на шаге стоимости."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Договорная", callback_data="services:price:deal")]
    ])

def _photo_skip_kb() -> InlineKeyboardMarkup:
    """Кнопка «Пропустить фото» (используем собственную, чтобы не мешать барахолке)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пропустить фото", callback_data="services:photo:skip")],
        [InlineKeyboardButton(text="Отмена", callback_data="services:cancel")]
    ])


# ─────────────────────────────────────────────────────────────────────────────
# СТАРТ ПОТОКА: город → категории

@router.callback_query(F.data == "service_start")
async def service_start(cb: CallbackQuery, state: FSMContext):
    """Старт публикации услуги: выбор города (как в Барахолке)."""
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    try: await cb.message.delete()
    except Exception: pass
    await state.clear()

    async with SessionLocal() as s:
        cities = (await s.execute(select(City).order_by(City.id))).scalars().all()

    # Клавиатура городов (двухколоночная – как у вас в услугах)
    rows, buf = [], []
    for c in cities:
        btn = InlineKeyboardButton(text=c.name, callback_data=f"services:add:city:{c.id}")
        buf.append(btn)
        if len(buf) == 2:
            rows.append(buf); buf = []
    if buf: rows.append(buf)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="go_services")])

    main_btn = await get_common_menu_button("main_menu", "ru")
    if main_btn:
        rows.append([main_btn])    
    
    
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    header = await get_text('sell_choose_city', 'ru') or "Создать объявление.\nСначала выберите город:"
    msg = await cb.message.answer(header, reply_markup=kb)
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
    print(f"[services_add.py] service_start ✓ | chat_id={cb.message.chat.id} user_id={cb.from_user.id}")


@router.callback_query(F.data.startswith("services:add:city:"))
async def services_add_select_city(cb: CallbackQuery, state: FSMContext):
    """После выбора города показываем верхние категории услуг (дети root=80)."""
    try:
        city_id = int(cb.data.split(":")[3])
    except Exception:
        city_id = 1

    await clear_bot_messages(cb.message.chat.id, cb.bot)
    try: await cb.message.delete()
    except Exception: pass

    async with SessionLocal() as s:
        city = (await s.execute(select(City).where(City.id == city_id))).scalar_one()
        cats = (await s.execute(
            select(Category).where(Category.parent_id == SERVICES_ROOT_CATEGORY_ID).order_by(Category.name)
        )).scalars().all()

    await state.update_data(city_id=city.id, city_name=city.name, city_slug=city.slug)

    rows = []
    for cat in cats:
        title = await format_category_title(cat.id, (cat.name or "").strip(), SessionLocal)
        rows.append([InlineKeyboardButton(text=title, callback_data=f"services:add:cat:{cat.id}:{city.id}")])

    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="service_start")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    tmpl = await get_text('sell_choose_category', 'ru') or "Город: <b>{city_name}</b>\nВыберите категорию:"
    text = tmpl.format(city_name=city.name)
    msg = await cb.message.answer(text, reply_markup=kb)
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
    print(f"[services_add.py] services_add_select_city ✓ | chat_id={cb.message.chat.id} user_id={cb.from_user.id} city_id={city_id}")


@router.callback_query(F.data.startswith("services:add:cat:"))
async def services_add_select_category(cb: CallbackQuery, state: FSMContext):
    """Категория: узел → углубиться; лист → перейти к шагу «Заголовок»."""
    try:
        _, _, _, cat_id_str, city_id_str = cb.data.split(":")
        cat_id = int(cat_id_str); city_id = int(city_id_str)
    except Exception:
        cat_id = SERVICES_ROOT_CATEGORY_ID; city_id = 1

    await clear_bot_messages(cb.message.chat.id, cb.bot)
    try: await cb.message.delete()
    except Exception: pass

    async with SessionLocal() as s:
        cat = (await s.execute(select(Category).where(Category.id == cat_id))).scalar_one()
        cnt = (await s.scalar(select(func.count()).select_from(Category).where(Category.parent_id == cat_id))) or 0

    if cnt > 0:
        async with SessionLocal() as s:
            cats = (await s.execute(select(Category).where(Category.parent_id == cat_id).order_by(Category.name))).scalars().all()
        rows = [[InlineKeyboardButton(text=c.name, callback_data=f"services:add:cat:{c.id}:{city_id}")] for c in cats]
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"services:add:city:{city_id}")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        tmpl = await get_text('sell_choose_subcategory', 'ru') or "Категория: <b>{cat_name}</b>\nВыберите подкатегорию:"
        text = tmpl.format(cat_name=cat.name)
        msg = await cb.message.answer(text, reply_markup=kb)
        last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
        await register_bot_messages(cb.message.chat.id, [msg.message_id])
        print(f"[services_add.py] services_add_select_category → deepen ✓ | chat_id={cb.message.chat.id} user_id={cb.from_user.id} city_id={city_id} parent_id={cat_id} children={cnt}")
        return

    await state.update_data(cat_id=cat.id, cat_name=cat.name)
    await state.set_state(ServiceForm.title)
    await _send_with_services_nav(cb.message, "Введите заголовок объявления (1 строка):")
    print(f"[services_add.py] services_add_select_category → leaf ✓ | chat_id={cb.message.chat.id} user_id={cb.from_user.id} city_id={city_id} category_id={cat_id}")


# ─────────────────────────────────────────────────────────────────────────────
# TITLE → DESCR → PRICE

# ───────────────────── ХЕНДЛЕР: Заголовок → спрашиваем описание ─────────────────────
@router.message(ServiceForm.title)
async def service_title_set(m: Message, state: FSMContext):
    """Шаг: Заголовок → спрашиваем описание (показываем введённый заголовок)."""
    chat_id = m.chat.id
    await clear_bot_messages(chat_id, m.bot)
    try:
        await m.delete()
    except Exception:
        pass

    title = (m.text or "").strip()
    await state.update_data(title=title)
    await state.set_state(ServiceForm.descr)

    # Плашка «Назад / Главное меню»
    back_btn = await get_common_menu_button("back", "ru")
    if back_btn:
        back_btn.callback_data = "go_services"   # если у вас другой callback — подставьте его
    main_btn = await get_common_menu_button("main_menu", "ru")
    nav_buttons = [b for b in (back_btn, main_btn) if b]
    nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons]) if nav_buttons else None
    nav_text = await get_text('return_to_menu', 'ru') or "Возврат"
    nav_msg = await m.answer(nav_text, reply_markup=nav_markup)

    # Сообщение шага
    text_msg = await m.answer(f"Заголовок — <b>{escape(title)}</b>\n\nОпишите услугу:", parse_mode="HTML")

    # ✅ Запоминаем оба сообщения, чтобы потом удалить
    last_bot_messages[chat_id] = [nav_msg.message_id, text_msg.message_id]
    await register_bot_messages(chat_id, [nav_msg.message_id, text_msg.message_id])

    print(f"[services_add.py] service_title_set ✓ | chat_id={chat_id} user_id={m.from_user.id} title={title!r}")

# ───────────────────── ХЕНДЛЕР: Описание → спрашиваем стоимость ─────────────────────
@router.message(ServiceForm.descr)
async def service_descr_set(m: Message, state: FSMContext):
    """Шаг: Описание → спрашиваем стоимость (показываем заголовок и описание)."""
    chat_id = m.chat.id
    await clear_bot_messages(chat_id, m.bot)
    try:
        await m.delete()
    except Exception:
        pass

    descr = (m.text or "").strip()
    await state.update_data(descr=descr)
    await state.set_state(ServiceForm.price)

    data  = await state.get_data()
    title = (data.get("title") or "").strip()
    descr = (data.get("descr") or "").strip()

    # Плашка «Назад / Главное меню»
    back_btn = await get_common_menu_button("back", "ru")
    if back_btn:
        back_btn.callback_data = "go_services"   # при необходимости замените
    main_btn = await get_common_menu_button("main_menu", "ru")
    nav_buttons = [b for b in (back_btn, main_btn) if b]
    nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons]) if nav_buttons else None
    nav_text = await get_text('return_to_menu', 'ru') or "Возврат"
    nav_msg = await m.answer(nav_text, reply_markup=nav_markup)

    # Клавиатура цены (кнопка «Договорная»)
    price_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Договорная", callback_data="services:price:deal")]
    ])

    # Сообщение шага
    price_text = (
        f"Заголовок — <b>{escape(title)}</b>\n\n"
        f"{escape(descr)}\n\n"
        "Введите стоимость (прейскурант) услуг\n"
        "или нажмите «Договорная»."
    )
    text_msg = await m.answer(price_text, parse_mode="HTML", reply_markup=price_kb)

    # ✅ Запоминаем оба сообщения
    last_bot_messages[chat_id] = [nav_msg.message_id, text_msg.message_id]
    await register_bot_messages(chat_id, [nav_msg.message_id, text_msg.message_id])

    print(f"[services_add.py] service_descr_set ✓ | chat_id={chat_id} user_id={m.from_user.id} title={title!r} descr_len={len(descr)}")


# ───────────────────── ХЕНДЛЕР: Цена → «Договорная» (к фото с резюме) ─────────────────────
@router.callback_query(ServiceForm.price, F.data == "services:price:deal")
async def service_price_deal(cb: CallbackQuery, state: FSMContext):
    """Кнопка «Договорная» → сохраняем цену и просим фото, показав уже введённые поля."""
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    try: await cb.message.delete()
    except Exception: pass

    await state.update_data(price="Договорная")
    await state.set_state(ServiceForm.photo)

    # ВАЖНО: первый промпт по фото — через _send_photo_prompt
    await state.update_data(photo_prompt_msgs=[])
    await _send_photo_prompt(cb.message, 0, state)

    await cb.answer()
    print(f"[services_add.py] service_price_deal ✓ | chat_id={chat_id} user_id={cb.from_user.id} price='Договорная'")


# ───────────────────── ХЕНДЛЕР: Цена (ввод числа/текста) → к фото с резюме ─────────────────────
@router.message(ServiceForm.price)
async def service_price_set(m: Message, state: FSMContext):
    """Пользователь ввёл цену → сохраняем и просим фото, показав уже введённые поля."""
    chat_id = m.chat.id
    await clear_bot_messages(chat_id, m.bot)
    try:
        await m.delete()
    except Exception:
        pass

    raw = (m.text or "").strip()
    # здесь оставьте вашу валидацию цены/нормализацию, если есть
    await state.update_data(price=raw)
    await state.update_data(photo_prompt_msgs=[])
    await _send_photo_prompt(m, 0, state)

    print(f"[services_add.py] service_price_set ✓ | chat_id={chat_id} user_id={m.from_user.id} price={price!r}")


# ─────────────────────────────────────────────────────────────────────────────
# ШАГ ФОТО (альбомы, подсказки, skip/cancel) — как в Барахолке

async def _send_photo_prompt(m: Message, photo_count: int, state: FSMContext, lang="ru"):
    """Показываем подсказки по фото (0/1/2/макс) и клавиатуру."""
    left = 3 - photo_count
    if photo_count == 0:
        text_main = (await get_text('sell_photo_0_main', lang)) or "Пришлите фото (до 3). Можно альбомом."
        text_tip  = (await get_text('sell_photo_0_tip',  lang)) or "Чтобы загрузить фото — нажмите 📎 слева."
    elif left == 2:
        text_main = (await get_text('sell_photo_1_main', lang)) or "Фото добавлено (1/3). Можно ещё 2."
        text_tip  = (await get_text('sell_photo_1_tip',  lang)) or "Чтобы добавить ещё — снова нажмите 📎."
    elif left == 1:
        text_main = (await get_text('sell_photo_2_main', lang)) or "Фото добавлено (2/3). Можно ещё 1."
        text_tip  = (await get_text('sell_photo_2_tip',  lang)) or "Чтобы добавить ещё — нажмите 📎."
    else:
        text_main = (await get_text('sell_photo_max_main', lang)) or "Максимум 3 фото."
        text_tip  = ""

    # ── Шапка «что уже ввели»
    from html import escape as esc
    data  = await state.get_data()
    title = esc((data.get("title") or "").strip())
    descr = (data.get("descr") or "").strip()
    price = (data.get("price") or "").strip()
    price_label = (await get_text("service_price", lang)) or (await get_text("listing_price", lang)) or "Стоимость услуг"

    # ≤3 строки описания
    if descr:
        lines = descr.splitlines()
        descr_short = "\n".join(lines[:3]) + ("\n…" if len(lines) > 3 else "")
    else:
        descr_short = ""

    header = (
        (f"Заголовок — <b>{title}</b>\n" if title else "") +
        (f"{esc(descr_short)}\n" if descr_short else "") +
        (f"{price_label} — <b>{esc(price)}</b>\n" if price else "")
    )

    # Итоговый текст: шапка → пустая строка → инструкция
    if header:
        text_main = f"{header}\n{text_main}"

    # ── Отправка и сохранение message_id для последующего удаления
    msg = await m.answer(text_main, reply_markup=_photo_skip_kb(), parse_mode="HTML")
    last_bot_messages.setdefault(m.chat.id, []).append(msg.message_id)
    await register_bot_messages(m.chat.id, [msg.message_id])

    msg2 = None
    if text_tip:
        msg2 = await m.answer(text_tip)
        last_bot_messages.setdefault(m.chat.id, []).append(msg2.message_id)
        await register_bot_messages(m.chat.id, [msg2.message_id])
        await state.update_data(photo_prompt_msgs=[msg.message_id, msg2.message_id])
    else:
        await state.update_data(photo_prompt_msgs=[msg.message_id])

    print(f"[services_add.py] _send_photo_prompt ✓ | chat_id={m.chat.id} user_id={m.from_user.id} count={photo_count}")


@router.message(ServiceForm.photo, F.photo)
async def service_photo(m: Message, state: FSMContext):
    """Обработка фото на шаге: одиночные и media_group (альбом)."""
    if m.media_group_id:
        gid = m.media_group_id
        media_group_cache.setdefault(gid, []).append(m.photo[-1].file_id)
        await _remember_and_delete_user_media(m)


        # Показываем «ожидалку» ОДИН раз на альбом.
        # ВАЖНО: сначала ставим плейсхолдер, потом await — чтобы не получить гонку и дубликат.
        if gid not in media_group_wait_msg:
            media_group_wait_msg[gid] = -1  # плейсхолдер, предотвращает двойной показ
            wait_text = (await get_text('sell_wait_photos', 'ru')) \
                        or "⏳ Пожалуйста, подождите — загружаем фотографии…"
            msg = await m.answer(wait_text)
            media_group_wait_msg[gid] = msg.message_id

        # Переинициализируем задачу финализации (аналог Барахолки)
        if gid in media_group_tasks and not media_group_tasks[gid].done():
            media_group_tasks[gid].cancel()
        media_group_tasks[gid] = asyncio.create_task(_finalize_album(m, state, gid))
        return


    data = await state.get_data()
    photos = data.get("photos", []) or []
    if len(photos) < 3:
        photos.append(m.photo[-1].file_id)
        await _remember_and_delete_user_media(m)

        photos = photos[:3]
        await state.update_data(photos=photos)

    if len(photos) >= 3:
        await delete_photo_prompts(m, state)
        await _preview_and_confirm(m, state)
        await state.set_state(ServiceForm.confirm)
    else:
        await delete_photo_prompts(m, state)
        await _send_photo_prompt(m, len(photos), state)

    print(f"[services_add.py] service_photo ✓ | chat_id={m.chat.id} user_id={m.from_user.id} photos={len((await state.get_data()).get('photos', []))}")


# RU: финализация альбома — собираем file_id, убираем «ожидалку»,
#     удаляем предыдущие подсказки, показываем следующий шаг.
async def _finalize_album(m: Message, state: FSMContext, gid: str | int):
    try:
        # даём Telegram доклеить все части альбома
        await asyncio.sleep(0.6)
    except Exception:
        pass

    # забираем накопленные фото для этого альбома
    ids = media_group_cache.pop(gid, [])
    # убрать «ожидалку», если показывали
    wait_mid = media_group_wait_msg.pop(gid, None)
    if wait_mid and isinstance(wait_mid, int) and wait_mid > 0:
        try:
            await m.bot.delete_message(m.chat.id, wait_mid)
        except Exception:
            pass

    # сохранить в FSM (макс. 3)
    data = await state.get_data()
    photos = (data.get("photos") or [])[:]
    for fid in ids:
        if len(photos) >= 3:
            break
        photos.append(fid)
    photos = photos[:3]
    await state.update_data(photos=photos)

    # ❗️ключевой момент: убрать ПРЕДЫДУЩИЕ подсказки «Пришлите фото…»
    await delete_photo_prompts(m, state)

    # следующий шаг
    if len(photos) >= 3:
        await _preview_and_confirm(m, state)
        await state.set_state(ServiceForm.confirm)
    else:
        await _send_photo_prompt(m, len(photos), state)

    print(f"[services_add.py] _finalize_album ✓ | chat_id={m.chat.id} user_id={m.from_user.id} photos={len(photos)} gid={gid}")


@router.message(ServiceForm.photo)
async def service_not_photo(m: Message, state: FSMContext):
    """Защита от неверного типа контента на шаге фото."""
    if m.photo or getattr(m, "video", None):
        return
    btn = await get_common_menu_button('delete_message', 'ru')
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn]] if btn else [])
    await m.answer(
        (await get_text('sell_not_photo', 'ru')) or
        "Пожалуйста, отправляйте только фото (или видео).",
        reply_markup=kb
    )
    print(f"[services_add.py] service_not_photo ✓ | chat_id={m.chat.id} user_id={m.from_user.id}")


@router.callback_query(ServiceForm.photo, F.data == "services:photo:skip")
async def service_skip_photo(cb: CallbackQuery, state: FSMContext):
    """Пропуск шага фото → предпросмотр и подтверждение."""
    await delete_photo_prompts(cb.message, state)
    await _clear_user_media(cb.message.chat.id, cb.bot)

    await _preview_and_confirm(cb.message, state)
    await state.set_state(ServiceForm.confirm)
    await cb.answer()
    print(f"[services_add.py] service_skip_photo ✓ | chat_id={cb.message.chat.id} user_id={cb.from_user.id}")


# RU: Безопасно получить текст из БД с фолбэком.
async def _text(key: str, lang: str, default: str) -> str:
    try:
        from app.routers.utils import get_text  # чтобы не падать, если импорта нет выше
        txt = await get_text(key, lang)
    except Exception:
        txt = None
    if not txt or txt.strip().startswith("[Text not found"):
        return default
    return txt


@router.callback_query(ServiceForm.photo, F.data == "services:cancel")
async def service_cancel_photo(cb: CallbackQuery, state: FSMContext):
    """Отмена публикации услуги на шаге фото: подчистка всего и выход в главное меню."""
    chat_id = cb.message.chat.id

    # 0) Удаляем сообщение, по которому нажали
    try:
        await cb.message.delete()
    except Exception:
        pass

    # 1) Удаляем служебные подсказки/медиа текущего шага
    try:
        await delete_photo_prompts(cb.message, state)
    except Exception:
        pass
    try:
        await _clear_user_media(chat_id, cb.bot)
    except Exception:
        pass

    # 2) Удаляем сохранённые нами ранее подсказки/меню по id из FSM (если есть)
    data = await state.get_data()
    for key in ("nav_msg_id", "prompt_id", "search_prompt_msg_id", "search_result_msg_id"):
        mid = data.get(key)
        if mid:
            try:
                await cb.bot.delete_message(chat_id, mid)
            except Exception:
                pass

    # 3) Общая подчистка истории: сначала пользовательские, затем бот-сообщения
    try:
        from app.routers.utils import clear_user_messages
        await clear_user_messages(chat_id, cb.bot)
    except Exception:
        pass
    from app.routers.utils import clear_bot_messages, last_bot_messages, register_bot_messages
    await clear_bot_messages(chat_id, cb.bot)

    # 4) Сообщение «Отменено»
    cancel_text = await _text("sell_cancelled", "ru", "❌ Публикация отменена.")
    msg = await cb.bot.send_message(chat_id, cancel_text, parse_mode="HTML")
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])

    # 5) Низ: «Главное меню»
    from app.keyboards import get_common_menu_button
    main_btn = await get_common_menu_button("main_menu", "ru")
    if main_btn:
        kb = InlineKeyboardMarkup(inline_keyboard=[[main_btn]])
        title = await _text("main_menu", "ru", "Главное меню")
        m2 = await cb.bot.send_message(chat_id, title, reply_markup=kb, parse_mode="HTML")
        last_bot_messages.setdefault(chat_id, []).append(m2.message_id)
        await register_bot_messages(chat_id, [m2.message_id])

    # 6) Сброс FSM и ответ на колбэк
    await state.clear()
    await cb.answer()

    # 7) Обязательный print: файл/хендлер
    print(f"[services_add.py] handler=service_cancel_photo chat_id={chat_id} user_id={cb.from_user.id}")


# ─────────────────────────────────────────────────────────────────────────────
# Предпросмотр + подтверждение (точно как в Барахолке)

async def _preview_and_confirm(m: Message, state: FSMContext):
    """Собираем предпросмотр: фото-альбом (если есть) + шапка и поля."""
    data = await state.get_data()
    photos = data.get("photos", [])

    header = f"<b>{data['city_name']} → {data['cat_name']}</b>\n"

    # Порядок: Название → Описание → Стоимость услуг
    title_line = f"{data['title']}".strip()
    descr_line = (data.get('descr') or "").strip()
    price_label = (await get_text('service_price', 'ru')) or (await get_text('listing_price','ru')) or "Стоимость услуг"
    price_line = f"{price_label}: {data.get('price', '')}".strip()

    # Собираем список строк (без пустых) и добавляем их к header через перенос строк.
    lines = []
    if title_line:
        lines.append(title_line)
    if descr_line:
        lines.append(descr_line)
    if data.get('price'):
        lines.append(price_line)
    if lines:
        header += "\n".join(lines)

    kb = confirm_keyboard()
    sent_ids = []
    if photos:
        if len(photos) == 1:
            msg_photo = await m.answer_photo(photos[0])
            sent_ids.append(msg_photo.message_id)
        else:
            media = [InputMediaPhoto(media=fid) for fid in photos]
            msg_group = await m.bot.send_media_group(m.chat.id, media)
            sent_ids.extend([x.message_id for x in msg_group])

    msg_header = await m.answer(header, reply_markup=kb, parse_mode="HTML")
    last_bot_messages.setdefault(m.chat.id, []).append(msg_header.message_id)
    await register_bot_messages(m.chat.id, [msg_header.message_id])
    if sent_ids:
        sent_photo_messages.setdefault(m.chat.id, []).extend(sent_ids)

    print(f"[services_add.py] _preview_and_confirm ✓ | chat_id={m.chat.id} user_id={m.from_user.id} photos={len(photos)}")


# ─────────────────────────────────────────────────────────────────────────────
# Публикация в БД (type='service') + экран «Опубликовано!»

@router.callback_query(ServiceForm.confirm, F.data == "sell_ok")
async def service_ok(cb: CallbackQuery, state: FSMContext):
    """Сохраняем Listing (type=service), показываем экран «Опубликовано!» и кнопки."""
    chat_id = cb.message.chat.id

    # Сброс временных кешей альбома
    for d in (media_group_cache, media_group_tasks, media_group_wait_msg):
        try: d.clear()
        except Exception: pass

    data = await state.get_data()
    # обязательные поля
    for k in ("city_id", "cat_id", "title", "price"):
        if not data.get(k):
            await cb.answer(f"Не хватает поля: {k}", show_alert=True)
            print(f"[services_add.py] service_ok | MISSING {k} | data_keys={list(data.keys())}")
            return

    try:
        async with SessionLocal() as s:
            l = Listing(
                city_id   = int(data["city_id"]),
                category_id = int(data["cat_id"]),
                owner_id  = cb.from_user.id,
                title     = data["title"],
                price     = data["price"],
                descr     = data.get("descr"),
                contact   = (f"@{cb.from_user.username}" if cb.from_user.username else "контакт не указан"),
                created_at= utcnow_naive(),
                type      = "service",
                photo_file_id=",".join(data.get("photos", [])) if data.get("photos") else None,
            )

            # Сохраним flex сразу, если он уже есть (на будущее)
            flex_data = data.get("flex_values")
            if flex_data:
                try: l.flex = json.dumps(flex_data, ensure_ascii=False)
                except Exception: l.flex = None

            s.add(l)
            await s.commit()
            await s.refresh(l)

            await state.update_data(listing_id=l.id)

            # Низ: «Услуги» + «Главное меню»
            services_btn = await get_common_menu_button('go_services', 'ru')
            main_btn     = await get_common_menu_button('main_menu',   'ru')

            # Верхние действия
            city = (await s.execute(select(City).where(City.id == l.city_id))).scalar_one()
            cat  = (await s.execute(select(Category).where(Category.id == l.category_id))).scalar_one()

        await clear_bot_messages(chat_id, cb.bot)
        await _clear_user_media(chat_id, cb.bot)


        text_pub   = (await get_text('sell_published', 'ru')) or "✅ Объявление опубликовано!"
        text_extra = (await get_text('sell_extras_offer', 'ru')) or "При желании укажите дополнительные сведения для этой категории:"

        rows = [
            [InlineKeyboardButton(text="✏️ Редактировать все поля", callback_data=f"service_edit_overview:{l.id}")],
            [InlineKeyboardButton(text="📄 К объявлению", callback_data=f"listing:{l.id}:{city.slug}:{cat.slug}:my")],
        ]
        nav = []
        if services_btn:
            nav.append(InlineKeyboardButton(text=services_btn.text, callback_data=services_btn.callback_data))
        if main_btn:
            nav.append(InlineKeyboardButton(text=main_btn.text, callback_data=main_btn.callback_data))
        if nav:
            rows.append(nav)

        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        msg = await cb.message.answer(f"{text_pub}\n\n{text_extra}", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])

        await cb.answer()
        print(f"[services_add.py] service_ok ✓ | SAVED listing_id={l.id} | chat_id={chat_id} user_id={cb.from_user.id} msg_id={msg.message_id}")

    except Exception as e:
        await cb.answer(f"Ошибка сохранения: {type(e).__name__}", show_alert=True)
        await cb.message.answer(f"❌ Не удалось сохранить объявление.\n<code>{e}</code>", parse_mode="HTML")
        print(f"[services_add.py] service_ok | ERROR {e}")

# ─────────────────────────────────────────────────────────────────────────────
# Отмена на этапе подтверждения (как в Барахолке)

@router.callback_query(ServiceForm.confirm, F.data == "sell_cancel")
async def service_cancel_confirm(cb: CallbackQuery, state: FSMContext):
    """
    Обработка кнопки «Отменить» на этапе подтверждения услуги.
    Полностью сбрасывает мастер создания услуги и показывает сообщение об отмене.
    """
    chat_id = cb.message.chat.id
    # Очистим все кеши альбома (аналогично service_cancel_photo)
    for d in (media_group_cache, media_group_tasks, media_group_wait_msg):
        try:
            d.clear()
        except Exception:
            pass
    # Очищаем все сообщения бота в чате
    await clear_bot_messages(chat_id, cb.message.bot)
    try:
        await cb.message.delete()
    except Exception:
        pass

    # Текст отмены берём из BotText, иначе дефолт
    cancel_text = await get_text('sell_cancelled', 'ru') or "❌ Публикация отменена."
    msg1 = await cb.message.answer(cancel_text)
    last_bot_messages.setdefault(chat_id, []).append(msg1.message_id)
    await register_bot_messages(chat_id, [msg1.message_id])

    # Показываем кнопку «Главное меню» (для возврата)
    main_btn = await get_common_menu_button('main_menu', 'ru')
    if main_btn:
        kb = InlineKeyboardMarkup(inline_keyboard=[[main_btn]])
        msg2 = await cb.message.answer(
            (await get_text('main_menu', 'ru')) or "Главное меню",
            reply_markup=kb
        )
        last_bot_messages.setdefault(chat_id, []).append(msg2.message_id)
        await register_bot_messages(chat_id, [msg2.message_id])

    await state.clear()
    await cb.answer()
    print(f"[services_add.py] service_cancel_confirm ✓ | chat_id={chat_id} user_id={cb.from_user.id}")


# ─────────────────────────────────────────────────────────────────────────────
# Назад по шагам (services:back): confirm→photo→price→descr→title→категории

@router.callback_query(F.data == "services:back")
async def services_back(cb: CallbackQuery, state: FSMContext):
    """Кнопка «Назад» со своих шагов публикации (без конфликтов с Барахолкой)."""
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    try: await cb.message.delete()
    except Exception: pass

    cur = await state.get_state()
    data = await state.get_data()
    city_id = int(data.get("city_id") or 1)

    if cur == ServiceForm.descr.state:
        await state.set_state(ServiceForm.title)
        await _send_with_services_nav(cb.message, "Введите заголовок объявления (1 строка):")
        print(f"[services_add.py] services_back → title ✓ | chat_id={cb.message.chat.id} user_id={cb.from_user.id}"); return

    if cur == ServiceForm.price.state:
        await state.set_state(ServiceForm.descr)
        await _send_with_services_nav(cb.message, "Опишите услугу")
        print(f"[services_add.py] services_back → descr ✓ | chat_id={cb.message.chat.id} user_id={cb.from_user.id}"); return

    if cur == ServiceForm.photo.state:
        await state.set_state(ServiceForm.price)
        await _send_with_services_nav(
            cb.message,
            "Введите стоимость (прейскурант) услуг\nили нажмите «Договорная».",
            reply_markup=_deal_price_kb()
        )
        print(f"[services_add.py] services_back → price ✓ | chat_id={cb.message.chat.id} user_id={cb.from_user.id}"); return

    if cur == ServiceForm.confirm.state:
        await state.set_state(ServiceForm.photo)
        # RU: вернулись на шаг фото → промпт рисуем корректно с учетом уже добавленных фото
        data = await state.get_data()
        cnt = len((data.get("photos") or []))
        await state.update_data(photo_prompt_msgs=[])
        await _send_photo_prompt(cb.message, cnt, state)
        print(f"[services_add.py] services_back → photo ✓ | chat_id={cb.message.chat.id} user_id={cb.from_user.id}"); return

    # Возврат к списку категорий
    async with SessionLocal() as s:
        cats = (await s.execute(
            select(Category).where(Category.parent_id == SERVICES_ROOT_CATEGORY_ID).order_by(Category.name)
        )).scalars().all()
    rows = []
    for c in cats:
        title = await format_category_title(c.id, (c.name or "").strip(), SessionLocal)
        rows.append([InlineKeyboardButton(text=title, callback_data=f"services:add:cat:{c.id}:{city_id}")])

    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"services:add:city:{city_id}")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await safe_edit_or_send(cb, "Выберите категорию", reply_markup=kb)
    print(f"[services_add.py] services_back → categories ✓ | chat_id={cb.message.chat.id} user_id={cb.from_user.id} city_id={city_id}")
