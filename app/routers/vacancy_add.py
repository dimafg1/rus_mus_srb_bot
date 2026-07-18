"""
Handlers and helper functions for creating vacancy listings.

This module implements a workflow similar to the flea market ("Барахолка")
for posting new job vacancies.  Users select a city, choose a
category from the vacancy tree (rooted at category id 90), enter a
title, description and salary, optionally fill in any additional
fields defined for the selected category, preview the result and
confirm publication.  Unlike the market section, vacancies do not
support photos; this is the only substantial deviation from the
existing market posting logic.

The new listings are persisted into the shared ``listing`` table with
``type`` set to ``"vacancy"``.  Contacts are automatically filled
using the user's Telegram @username when available.

Callback prefixes:

* ``vac:new`` – entry point for posting a new vacancy
* ``vac_city:<city_slug>`` – select a city
* ``vac_cat:<city_slug>:<cat_id>`` – select a category/subcategory
* ``vac_confirm`` – confirm publication
* ``vac_cancel`` – cancel publication

"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from app.models import utcnow_naive
from app.lifecycle import ensure_expires_at
from html import escape as _esc
from typing import List, Optional

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, text as sql_text

from app.database import SessionLocal
from app.models import City, Category, Listing
from app.keyboards import get_common_menu_button, build_city_buttons
from app.routers.vacancy_utils import (
    VACANCY_ROOT_CATEGORY_ID,
    vacancy_categories_inline,
    vacancy_categories_inline_add,
)
from app.routers.utils import (
    clear_bot_messages,
    last_bot_messages,
    safe_edit_or_send,
    register_bot_messages,
)

from datetime import datetime
from sqlalchemy import select
from app.database import SessionLocal
from app.models import Listing, City, Category
from app.routers.vacancy_utils import _flex_to_db

from app.routers.utils_category_title import format_category_title


# Note: we don't use start_extra_fields_for_category here since vacancy
# extra fields are handled internally via _start_flex_flow.
from app.routers.utils import get_text

from app.routers.vacancy_utils import (
    vacancy_categories_inline_add,
)

from app.routers.utils_kb import grid3


try:
    from app.routers.utils import clear_user_messages  # optional
except Exception:
    clear_user_messages = None

# RU: Удаляет навигационное сообщение «Возврат» и последнюю подсказку,
#     а также сбрасывает их id в FSM (канон чистоты).
async def _drop_nav_and_prompt(state: FSMContext, chat_id: int, bot, current_msg=None) -> None:
    # Текущее сообщение (часто это «Возврат») — удалить по возможности
    if current_msg:
        try:
            await current_msg.delete()
        except Exception:
            pass

    data = await state.get_data()
    for key in ("nav_msg_id", "prompt_id"):
        mid = data.get(key)
        if mid:
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:
                pass

    # Сбросить, чтобы их не пытались удалить повторно на следующих шагах
    await state.update_data(nav_msg_id=None, prompt_id=None)


# RU: Жёсткая очистка всех прошлых сообщений и меню (канон).
async def _purge_all(chat_id: int, bot) -> None:
    if clear_user_messages:
        try:
            await clear_user_messages(chat_id, bot)  # удаляем пользовательские
        except Exception:
            pass
    await clear_bot_messages(chat_id, bot)          # удаляем бот-сообщения/меню

# RU: Отправить подсказку и сохранить её message_id в FSM чтобы потом удалить.
async def _ask_and_store(state, msg_func, text: str):
    msg = await msg_func(text, parse_mode="HTML")
    await state.update_data(prompt_id=msg.message_id)


async def _city_id_by_slug(slug: str) -> int | None:
    """RU: получить ID города по slug (локальный хелпер, чтобы не дёргать другие модули)."""
    async with SessionLocal() as s:
        return (await s.execute(select(City.id).where(City.slug == slug))).scalar_one_or_none()


async def _is_vacancy_category(session, category: Category | None) -> bool:
    """Проверить принадлежность категории дереву вакансий и оборвать циклы."""
    current = category
    seen: set[int] = set()
    while current is not None and current.id not in seen:
        if current.id == VACANCY_ROOT_CATEGORY_ID:
            return True
        seen.add(current.id)
        if current.parent_id is None:
            return False
        current = await session.get(Category, current.parent_id)
    return False

async def _vacancy_categories_kb_add(city_slug: str, parent_id: int | None) -> InlineKeyboardMarkup:
    """Клавиатура категорий для публикации вакансий (без лишних пунктов) с авто «🔽»."""
    pid = parent_id if parent_id is not None else VACANCY_ROOT_CATEGORY_ID
    async with SessionLocal() as s:
        cats = (await s.execute(
            select(Category).where(Category.parent_id == pid).order_by(sql_text("order_num"), Category.name)
        )).scalars().all()

    rows = []
    for c in cats:
        title = await format_category_title(c.id, (c.name or "").strip(), SessionLocal)
        rows.append([InlineKeyboardButton(text=title, callback_data=f"vac_add_cat:{city_slug}:{c.id}")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def _flex_to_db(value):
    """Превращает dict/list/любое значение в JSON-строку для БД."""
    if not value:
        return None  # в БД пойдёт NULL
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, str):
        return value  # допустим, уже JSON
    # числа/булевы/даты и пр. — тоже в JSON
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))

def _flex_from_db(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}  # на всякий случай


router = Router(name="vacancy_add")
_vacancy_publish_locks: dict[int, asyncio.Lock] = {}


# ─────────────────────────────────────────────────────────────────────────────
# FSM: stages for creating a vacancy
class VacForm(StatesGroup):
    city = State()
    cat = State()
    title = State()
    descr = State()
    price = State()
    flex = State()
    confirm = State()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers for navigation and extra fields

async def _vacancy_nav_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Return a keyboard with back and main menu buttons for vacancy flow."""
    back_btn = await get_common_menu_button('back', lang)
    main_btn = await get_common_menu_button('main_menu', lang)
    rows: List[List[InlineKeyboardButton]] = []
    if back_btn:
        rows.append([InlineKeyboardButton(text=back_btn.text, callback_data="go_isk")])
    if main_btn:
        rows.append([main_btn])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _vacancy_send_with_nav(m: Message, text: str, parse_mode: Optional[str] = None) -> Message:
    """Send a message preceded by the navigation buttons.

    The first message contains the navigation keyboard, and the second
    contains the actual prompt.  Both message ids are stored in
    ``last_bot_messages`` for subsequent cleanup.
    """
    chat_id = m.chat.id
    nav_text = await get_text('return_to_menu', 'ru') or "Возврат"
    nav_kb = await _vacancy_nav_keyboard()
    msg_nav = await m.answer(nav_text, reply_markup=nav_kb)
    last_bot_messages.setdefault(chat_id, []).append(msg_nav.message_id)
    await register_bot_messages(chat_id, [msg_nav.message_id])
    msg = await m.answer(text, parse_mode=parse_mode)
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    return msg


async def _load_category_fields(session, cat_id: int) -> List[dict]:
    """Load the JSON-defined extra fields for a category."""
    cat = (await session.execute(select(Category).where(Category.id == cat_id))).scalar_one()
    try:
        raw = (cat.fields or "").strip()
        data = json.loads(raw) if raw else []
        return data if isinstance(data, list) else []
    except Exception:
        return []


async def _start_flex_flow(m_or_cbmsg, state: FSMContext):
    """Launch the extra fields wizard if the category defines any."""
    data = await state.get_data()
    cat_id = int(data.get("cat_id"))
    async with SessionLocal() as s:
        raw_fields = await _load_category_fields(s, cat_id)
    supported = {"text", "number", "select", "checkbox"}
    fields: List[dict] = []
    for f in raw_fields or []:
        if isinstance(f, dict) and str(f.get("type", "text")).lower() in supported:
            fld = {
                "type": str(f.get("type", "text")).lower(),
                "label": (str(f.get("label") or "") or "Поле").strip(),
                "key": (str(f.get("key") or "field")).strip().lower() or "field",
                "required": bool(f.get("required", False)),
            }
            if fld["type"] == "select":
                opts = f.get("options") if isinstance(f.get("options"), list) else []
                fld["options"] = [str(o).strip() for o in opts if str(o).strip()]
            fields.append(fld)

    if not fields:
        # no extra fields defined – go straight to preview
        await vacancy_preview_and_confirm(m_or_cbmsg, state)
        await state.set_state(VacForm.confirm)
        return
    # Initialise flex fields data
    await state.update_data(flex_fields=fields, flex_idx=0, flex_values={})
    await _ask_current_flex_field(m_or_cbmsg, state)


async def _ask_current_flex_field(m_or_cbmsg, state: FSMContext):
    """Ask the user the next extra field question."""
    chat_id = m_or_cbmsg.chat.id
    bot = m_or_cbmsg.bot
    await clear_bot_messages(chat_id, bot)
    data = await state.get_data()
    fields = data.get("flex_fields", []) or []
    idx = int(data.get("flex_idx", 0))
    # If we've asked all fields, proceed to preview
    if idx >= len(fields):
        await vacancy_preview_and_confirm(m_or_cbmsg, state)
        await state.set_state(VacForm.confirm)
        return
    f_def = fields[idx]
    label = f_def.get("label") or "Поле"
    ftype = f_def.get("type")
    required = bool(f_def.get("required"))
    rows: List[List[InlineKeyboardButton]] = []
    prompt = ""
    if ftype == "select":
        for i, opt in enumerate(f_def.get("options", [])):
            rows.append([
                InlineKeyboardButton(
                    text=str(opt),
                    callback_data=f"vac_flex_select:{i}"
                )
            ])
        if not required:
            rows.append([
                InlineKeyboardButton(text="Пропустить", callback_data="vac_flex_skip")
            ])
        prompt = f"({idx+1}/{len(fields)}) <b>{_esc(label)}</b>\n\nВыберите один из вариантов:"
    elif ftype == "checkbox":
        rows.append([
            InlineKeyboardButton(text="✅ Да", callback_data="vac_flex_checkbox:1"),
            InlineKeyboardButton(text="❌ Нет", callback_data="vac_flex_checkbox:0"),
        ])
        if not required:
            rows.append([
                InlineKeyboardButton(text="Пропустить", callback_data="vac_flex_skip")
            ])
        prompt = f"({idx+1}/{len(fields)}) <b>{_esc(label)}</b>\n\nВыберите вариант:"
    else:
        # text or number
        if not required:
            rows.append([
                InlineKeyboardButton(text="Пропустить", callback_data="vac_flex_skip")
            ])
        prompt = f"({idx+1}/{len(fields)}) <b>{_esc(label)}</b>\n\nВведите значение" + (" (число)." if ftype == "number" else ".")
    # Append nav buttons
    back_btn = await get_common_menu_button('back')
    main_btn = await get_common_menu_button('main_menu')
    nav: List[InlineKeyboardButton] = []
    if back_btn:
        nav.append(InlineKeyboardButton(text=back_btn.text, callback_data="go_isk"))
    if main_btn:
        nav.append(main_btn)
    if nav:
        rows.append(nav)
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    msg = await m_or_cbmsg.answer(prompt, reply_markup=kb, parse_mode="HTML")
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    await state.set_state(VacForm.flex)


async def _update_flex_value(state: FSMContext, value: Optional[str]):
    """Store the user's answer for the current flex field and advance the index."""
    data = await state.get_data()
    idx = int(data.get("flex_idx", 0))
    fields = data.get("flex_fields", []) or []
    if 0 <= idx < len(fields):
        key = fields[idx].get("key")
        fv = data.get("flex_values", {}) or {}
        if value is not None:
            fv[key] = value
        await state.update_data(flex_values=fv, flex_idx=idx + 1)


async def vacancy_preview_and_confirm(m_or_cbmsg, state: FSMContext):
    """Compile a preview of the vacancy and ask for confirmation."""
    data = await state.get_data()
    city_name = data.get("city_name")
    cat_name = data.get("cat_name")
    title = data.get("title")
    descr = data.get("descr") or "—"
    price = data.get("price") or "—"
    flex_vals = data.get("flex_values", {}) or {}
    # Build flex block text
    flex_lines: List[str] = []
    for k, v in flex_vals.items():
        flex_lines.append(f"• {_esc(k.capitalize())}: <i>{_esc(str(v))}</i>")
    flex_block = "\n".join(flex_lines)
    preview_lines: List[str] = []
    preview_lines.append(f"<b>Город:</b> {_esc(city_name)}")
    preview_lines.append(f"<b>Категория:</b> {_esc(cat_name)}")
    preview_lines.append(f"<b>Заголовок:</b> {_esc(title)}")
    preview_lines.append(f"<b>Описание:</b> { _esc(descr) }")
    preview_lines.append(f"<b>Зарплата:</b> {_esc(price)}")
    if flex_block:
        preview_lines.append(flex_block)
    preview_text = "\n".join(preview_lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data="vac_publish")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="vac_cancel")],
    ])
    msg = await m_or_cbmsg.answer(preview_text, reply_markup=kb, parse_mode="HTML")
    last_bot_messages.setdefault(m_or_cbmsg.chat.id, []).append(msg.message_id)
    await register_bot_messages(m_or_cbmsg.chat.id, [msg.message_id])


# ─────────────────────────────────────────────────────────────────────────────
# Handlers for the vacancy posting flow

@router.callback_query(F.data == "vac:new")
async def vacancy_start(cb: CallbackQuery, state: FSMContext):
    """Entry point for posting a new vacancy."""
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    try:
        await cb.message.delete()
    except Exception:
        pass
    await state.clear()
    # Ensure the root vacancy category exists.  If not present in the
    # database it will be created automatically.  This allows the
    # administrator to drop the vacancy table and still rely on the
    # shared category tree.
    async with SessionLocal() as s:
        root = (await s.execute(select(Category).where(Category.id == VACANCY_ROOT_CATEGORY_ID))).scalars().first()
        if root is None:
            s.add(Category(id=VACANCY_ROOT_CATEGORY_ID, name="Вакансии", slug="vacancies", parent_id=None))
            await s.commit()
    # Show list of cities
    city_buttons = await build_city_buttons("vac_city")
    # Build keyboard: show cities in a single row (fits many cities)
    kb = InlineKeyboardMarkup(inline_keyboard=[city_buttons] if city_buttons else [])
    back_btn = await get_common_menu_button('back')
    if back_btn:
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=back_btn.text, callback_data="go_isk")
        ])
    main_btn = await get_common_menu_button('main_menu')
    if main_btn:
        kb.inline_keyboard.append([main_btn])
    header = await get_text('sell_choose_city', 'ru') or "Создать вакансию.\nСначала выберите город:"
    msg = await cb.message.answer(header, reply_markup=kb)
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    await state.set_state(VacForm.city)
    await cb.answer()

# RU: Публикация → вернуться к списку городов.
@router.callback_query(F.data == "vac_add_citylist")
async def vacancy_citylist(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await _purge_all(chat_id, cb.bot)

    # ряд кнопок городов с префиксом 'vac_city'
    city_buttons = await build_city_buttons("vac_city", lang="ru")

    # собираем клавиатуру: города в первую строку/строки, ниже — «Главное меню»
    keyboard: list[list[InlineKeyboardButton]] = []
    if city_buttons:
        keyboard.append(city_buttons)

    main_btn = await get_common_menu_button('main_menu', 'ru')
    if main_btn:
        keyboard.append([main_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=keyboard)

    await safe_edit_or_send(cb, "Выберите город для публикации вакансии:", reply_markup=kb, parse_mode="HTML")
    await cb.answer()
    print(f"[vacancy_add.py] handler=vacancy_citylist chat_id={chat_id}")


# RU: Публикация → выбран город: показываем категории + «Назад» (к списку городов) + «Главное меню».
@router.callback_query(F.data.startswith("vac_city:"))
async def vacancy_choose_city(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await _purge_all(chat_id, cb.bot)

    city_slug = cb.data.split(":", 1)[1]
    async with SessionLocal() as s:
        city = (await s.execute(select(City).where(City.slug == city_slug))).scalar_one_or_none()
    if city is None:
        await cb.answer("Город не найден.", show_alert=True)
        return
    await state.update_data(city_id=city.id, city_slug=city.slug, city_name=city.name)

    # базовая клавиатура ДЛЯ ПУБЛИКАЦИИ (только категории, без навигации)
    kb_base = await _vacancy_categories_kb_add(city_slug, parent_id=None)

    # навигация: «Назад» -> список городов; «Главное меню»
    rows = list(kb_base.inline_keyboard or [])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="vac_add_citylist")])

    main_btn = await get_common_menu_button('main_menu', 'ru')
    if main_btn:
        rows.append([main_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    await safe_edit_or_send(
        cb,
        f"Город: <b>{_esc(city.name or city.slug)}</b>\nВыберите категорию:",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await cb.answer()
    print(f"[vacancy_add.py] handler=vacancy_choose_city chat_id={chat_id} city_slug={city.slug} city_id={city.id}")




# RU: Публикация → выбор категории.
#     Если у категории есть дети — показываем подкатегории с кнопкой «Назад».
#     Если категория листовая — рисуем ОТДЕЛЬНО «Возврат» и ОТДЕЛЬНО подсказку «введите заголовок».
@router.callback_query(F.data.startswith("vac_add_cat:"))
async def vacancy_choose_category(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # 0) Удаляем сообщение, по которому нажали (чтобы не плодить)
    try:
        await cb.message.delete()
    except Exception:
        pass

    # 1) Удаляем прошлые служебные («Возврат», «подсказка»), если были
    data = await state.get_data()
    for key in ("nav_msg_id", "prompt_id"):
        mid = data.get(key)
        if mid:
            try:
                await cb.bot.delete_message(chat_id, mid)
            except Exception:
                pass

    # 2) Базовая подчистка истории (если используется у вас)
    try:
        from app.routers.utils import clear_user_messages
        await clear_user_messages(chat_id, cb.bot)
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    # 3) Парсим колбэк: vac_add_cat:<city_slug>:<cat_id>
    try:
        _, city_slug, cat_id_s = cb.data.split(":", 2)
        cat_id = int(cat_id_s)
    except (TypeError, ValueError):
        await cb.answer("Некорректная категория.", show_alert=True)
        return

    # 4) Грузим детей и родителя
    async with SessionLocal() as s:
        city = (await s.execute(select(City).where(City.slug == city_slug))).scalar_one_or_none()
        category = await s.get(Category, cat_id)
        if city is None or category is None or not await _is_vacancy_category(s, category):
            await cb.answer("Город или категория больше недоступны.", show_alert=True)
            return
        children = (await s.execute(
            select(Category).where(Category.parent_id == cat_id)
            .order_by(sql_text("order_num"), Category.name)  # как при просмотре
        )).scalars().all()
        parent_id = category.parent_id

    # 5А) Есть подкатегории → показываем их
    if children:
        # аккуратная клавиатура без «лишних» пунктов (вариант для публикации)
        kb = await _vacancy_categories_kb_add(city_slug, parent_id=cat_id)

        rows = list(kb.inline_keyboard or [])

        # «Назад» только если это НЕ корень (чтобы не зациклиться)
        if parent_id is not None:
            rows.append([InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"vac_add_cat:{city_slug}:{parent_id}"
            )])

        # «Главное меню»
        main_btn = await get_common_menu_button('main_menu')
        if main_btn:
            rows.append([main_btn])

        kb_full = InlineKeyboardMarkup(inline_keyboard=rows)

        # ВНИМАНИЕ: отправляем НОВОЕ сообщение (старое мы удалили выше)
        msg = await cb.message.answer("Выберите подкатегорию:", reply_markup=kb_full, parse_mode="HTML")
        print(f"[vacancy_add.py] handler=vacancy_choose_category step=children chat_id={chat_id} cat_id={cat_id} parent_id={parent_id} msg_id={msg.message_id}")
        await cb.answer()
        return

    # 5Б) Листовая категория → фиксируем и спрашиваем заголовок
    await state.update_data(
        category_id=cat_id,
        cat_name=category.name,
        city_id=city.id,
        city_name=city.name,
        city_slug=city.slug,
    )

    # back_to_id нужен для «Назад» на следующих шагах; если корень — «Назад» не рисуем
    back_to_id = parent_id  # None для корня
    await state.update_data(back_to_id=back_to_id)

    # 5Б.1) Рисуем «Возврат» (только если есть куда вернуться)
    if back_to_id is not None:
        nav_rows = [[InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=f"vac_add_cat:{city_slug}:{back_to_id}"
        )]]
        main_btn = await get_common_menu_button('main_menu')
        if main_btn:
            nav_rows.append([main_btn])
        kb_nav = InlineKeyboardMarkup(inline_keyboard=nav_rows)
        nav_msg = await cb.message.answer("◀️ Возврат", reply_markup=kb_nav, parse_mode="HTML")
        await state.update_data(nav_msg_id=nav_msg.message_id)
    else:
        # даже если «Назад» нет, «Главное меню» всё равно можно показать отдельной кнопкой ниже,
        # но чтобы не плодить ещё одно сообщение — оставим только подсказку ввода.
        pass

    # 5Б.2) Подсказка на ввод заголовка
    prompt_msg = await cb.message.answer("✏️ Введите <b>заголовок</b> вакансии:", parse_mode="HTML")
    await state.update_data(prompt_id=prompt_msg.message_id)

    # 5Б.3) Ставим состояние
    await state.set_state(VacForm.title)

    print(f"[vacancy_add.py] handler=vacancy_choose_category step=leaf chat_id={chat_id} cat_id={cat_id} parent_id={parent_id} nav_msg_id={data.get('nav_msg_id')}→{prompt_msg.message_id}")
    await cb.answer()


# RU: Универсальный «Назад» для публикации.
# Поддерживает форматы:
# - vac_add_back:title  -> вернуться на шаг ввода заголовка
# - vac_add_back:descr  -> вернуться на шаг ввода описания
# - vac_add_back:<city_slug>:<parent_id> -> вернуться в дерево категорий (режим публикации)
@router.callback_query(F.data.startswith("vac_add_back:"))
async def vacancy_add_back(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    from html import escape as _esc

    # 0) Удаляем сообщение, по которому нажали
    try:
        await cb.message.delete()
    except Exception:
        pass

    # 1) Удаляем прошлые служебные сообщения (nav/prompt), если они были сохранены в FSM
    data = await state.get_data()
    for key in ("nav_msg_id", "prompt_id"):
        mid = data.get(key)
        if mid:
            try:
                await cb.bot.delete_message(chat_id, mid)
            except Exception:
                pass

    parts = cb.data.split(":")

    # ───────── вариант: два токена (title / descr) ─────────
    if len(parts) == 2:
        _, target = parts

        if target == "title":
            # Назад к вводу заголовка. "Назад" на этом экране ведёт в дерево категорий.
            city_slug = data.get("city_slug")
            back_to_id = data.get("back_to_id")

            # если back_to_id ещё не вычисляли — вычислим по parent_id категории
            if back_to_id is None:
                try:
                    cat_id = data.get("category_id")
                    if cat_id is not None:
                        async with SessionLocal() as s:
                            parent_id = (await s.execute(
                                select(Category.parent_id).where(Category.id == int(cat_id))
                            )).scalar_one_or_none()
                        back_to_id = parent_id if parent_id is not None else VACANCY_ROOT_CATEGORY_ID
                        await state.update_data(back_to_id=back_to_id)
                except Exception:
                    pass

            buttons: list[list[InlineKeyboardButton]] = []
            if city_slug and back_to_id is not None:
                buttons.append([
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data=f"vac_add_cat:{city_slug}:{back_to_id}"
                    )
                ])
            main_btn = await get_common_menu_button('main_menu', 'ru')
            if main_btn:
                buttons.append([main_btn])
            kb = InlineKeyboardMarkup(inline_keyboard=buttons)

            nav = await cb.message.answer("◀️ Возврат", reply_markup=kb, parse_mode="HTML")
            await state.update_data(nav_msg_id=nav.message_id)

            prompt = await cb.message.answer("✏️ Введите <b>заголовок</b> вакансии:", parse_mode="HTML")
            await state.update_data(prompt_id=prompt.message_id)

            await state.set_state(VacForm.title)
            await cb.answer()
            print(f"[vacancy_add.py] vacancy_add_back -> title | chat_id={chat_id}")
            return

        if target == "descr":
            # Назад к вводу описания. "Назад" на этом экране ведёт на шаг «Заголовок».
            buttons = [[InlineKeyboardButton(text="⬅️ Назад", callback_data="vac_add_back:title")]]
            main_btn = await get_common_menu_button('main_menu', 'ru')
            if main_btn:
                buttons.append([main_btn])
            kb = InlineKeyboardMarkup(inline_keyboard=buttons)

            nav = await cb.message.answer("◀️ Возврат", reply_markup=kb, parse_mode="HTML")
            await state.update_data(nav_msg_id=nav.message_id)

            st = await state.get_data()
            title = _esc(st.get("title") or "—")
            try:
                tmpl = await get_text('sell_ask_descr', 'ru')
            except Exception:
                tmpl = None
            tmpl = tmpl or "Краткое описание (или «-» чтобы пропустить):"

            prompt = await cb.message.answer(
                f"<b>Вы уже ввели</b>\n• Заголовок: <i>{title}</i>\n\n{tmpl}",
                parse_mode="HTML",
            )
            await state.update_data(prompt_id=prompt.message_id)

            await state.set_state(VacForm.descr)
            await cb.answer()
            print(f"[vacancy_add.py] vacancy_add_back -> descr | chat_id={chat_id}")
            return

        # неизвестная цель — тихо игнорируем
        await cb.answer()
        return

    # ───────── вариант: три токена (возврат в дерево категорий) ─────────
    if len(parts) == 3:
        _, city_slug, parent_id_s = parts
        try:
            parent_id = int(parent_id_s)
        except Exception:
            parent_id = VACANCY_ROOT_CATEGORY_ID

        kb = await _vacancy_categories_kb_add(city_slug, parent_id=parent_id)

        # добавим «Главное меню», если его нет
        rows = list(kb.inline_keyboard or [])
        main_btn = await get_common_menu_button('main_menu', 'ru')
        if main_btn and not any(
            getattr(btn, "callback_data", None) == getattr(main_btn, "callback_data", "main_menu")
            for row in rows for btn in row
        ):
            rows.append([main_btn])

        await cb.message.answer("Выберите категорию:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
        await cb.answer()
        print(f"[vacancy_add.py] vacancy_add_back -> categories | chat_id={chat_id} city={city_slug} parent_id={parent_id}")
        return

    # что-то экзотическое — просто ответим
    await cb.answer()


# RU: Публикация → пользователь ввёл заголовок: удаляем ввод, старые «Возврат» и подсказку;
#     создаём новое «Возврат» и подсказку для описания; сохраняем их id в FSM.
@router.message(VacForm.title, F.text)
async def vacancy_input_title(m: Message, state: FSMContext):
    chat_id = m.chat.id
    from html import escape as _esc

    # 0) удалить сообщение пользователя
    try:
        await m.delete()
    except Exception:
        pass

    title = (m.text or "").strip()
    if not title:
        msg = await m.answer("Заголовок не может быть пустым. Введите заголовок вакансии:")
        await state.update_data(prompt_id=msg.message_id)
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])
        return

    # 1) удалить прошлые «Возврат» и подсказку
    data = await state.get_data()
    for key in ("nav_msg_id", "prompt_id"):
        mid = data.get(key)
        if mid:
            try:
                await m.bot.delete_message(chat_id, mid)
            except Exception:
                pass

    # 2) базовая очистка истории (если у вас так принято)
    try:
        from app.routers.utils import clear_user_messages
        await clear_user_messages(chat_id, m.bot)
    except Exception:
        pass
    await clear_bot_messages(chat_id, m.bot)

    # 3) сохранить заголовок и перейти к описанию
    await state.update_data(title=title)
    await state.set_state(VacForm.descr)

    # 4) новое «Возврат»
    city_slug = data.get("city_slug")
    back_to_id = data.get("back_to_id")
    if back_to_id is None:
        # подстраховка: если забыли сохранить в предыдущем шаге — вычислим
        try:
            cat_id = data.get("category_id")
            if cat_id is not None:
                async with SessionLocal() as s:
                    parent_id = (await s.execute(
                        select(Category.parent_id).where(Category.id == int(cat_id))
                    )).scalar_one_or_none()
                back_to_id = parent_id if parent_id is not None else VACANCY_ROOT_CATEGORY_ID
                await state.update_data(back_to_id=back_to_id)
        except Exception:
            pass

    buttons: list[list[InlineKeyboardButton]] = []
    if city_slug is not None and back_to_id is not None:
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="vac_add_back:title")])
    main_btn = await get_common_menu_button('main_menu', 'ru')
    if main_btn:
        buttons.append([main_btn])

    if buttons:
        kb_nav = InlineKeyboardMarkup(inline_keyboard=buttons)
        nav_msg = await m.answer("◀️ Возврат", reply_markup=kb_nav, parse_mode="HTML")
        await state.update_data(nav_msg_id=nav_msg.message_id)

    # 5) подсказка «введите описание»
    try:
        tmpl = await get_text('sell_ask_descr', 'ru')
    except Exception:
        tmpl = None
    tmpl = tmpl or "Краткое описание (или «-» чтобы пропустить):"
    helper = f"<b>Вы уже ввели</b>\n• Заголовок: <i>{_esc(title) or '—'}</i>"

    prompt_msg = await m.answer(f"{helper}\n\n{tmpl}", parse_mode="HTML")
    await state.update_data(prompt_id=prompt_msg.message_id)

    print(f"[vacancy_add.py] handler=vacancy_input_title chat_id={chat_id} title_len={len(title)} nav_new={data.get('nav_msg_id')} prompt_new={prompt_msg.message_id}")



# RU: Публикация → пользователь ввёл описание: удаляем ввод + старые «Возврат» и подсказку;
#     создаём новое «Возврат» и подсказку для ввода цены (с кнопками «Бесплатно» / «По договоренности»);
#     сохраняем их id в FSM.
@router.message(VacForm.descr, F.text)
async def vacancy_input_descr(m: Message, state: FSMContext):
    chat_id = m.chat.id
    from html import escape as _esc

    # 0) удалить сообщение пользователя
    try:
        await m.delete()
    except Exception:
        pass

    # 1) удалить прошлые «Возврат» и подсказку
    data = await state.get_data()
    for key in ("nav_msg_id", "prompt_id"):
        mid = data.get(key)
        if mid:
            try:
                await m.bot.delete_message(chat_id, mid)
            except Exception:
                pass

    # 2) базовая очистка истории (если используется)
    try:
        from app.routers.utils import clear_user_messages
        await clear_user_messages(chat_id, m.bot)
    except Exception:
        pass
    await clear_bot_messages(chat_id, m.bot)

    # 3) сохранить описание и перейти к цене («-» — пропуск, как обещает подсказка)
    descr = (m.text or "").strip()
    await state.update_data(descr=None if descr == "-" else descr)
    await state.set_state(VacForm.price)

    # 4) новое «Возврат»
    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="vac_add_back:descr")]
    ]
    main_btn = await get_common_menu_button('main_menu', 'ru')
    if main_btn:
        buttons.append([main_btn])
    kb_nav = InlineKeyboardMarkup(inline_keyboard=buttons)
    nav_msg = await m.answer("◀️ Возврат", reply_markup=kb_nav, parse_mode="HTML")
    await state.update_data(nav_msg_id=nav_msg.message_id)

    # 5) подсказка «укажите цену» + две быстрые кнопки
    #    текст: «Укажите стоимость оплаты или нажмите на нужную кнопку»
    try:
        tmpl = await get_text('vac_ask_price', 'ru')
    except Exception:
        tmpl = None
    tmpl = tmpl or "Укажите стоимость оплаты или нажмите на нужную кнопку:"

    st = await state.get_data()
    title = _esc(st.get("title") or "—")
    helper = (
        f"<b>Вы уже ввели</b>\n"
        f"• Заголовок: <i>{title}</i>\n"
        f"• Описание: <i>{_esc(descr) or '—'}</i>"
    )

    kb_quick = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Бесплатно", callback_data="vac_price_choice:free"),
                InlineKeyboardButton(text="По договоренности", callback_data="vac_price_choice:deal"),
            ]
        ]
    )


    prompt_msg = await m.answer(f"{helper}\n\n{tmpl}", reply_markup=kb_quick, parse_mode="HTML")
    await state.update_data(prompt_id=prompt_msg.message_id)

    print(
        f"[vacancy_add.py] handler=vacancy_input_descr chat_id={chat_id} "
        f"descr_len={len(descr)} nav_new={nav_msg.message_id} prompt_new={prompt_msg.message_id}"
    )

# RU: Публикация → быстрый выбор цены кнопками «Бесплатно / По договоренности».
#     Удаляем текущую клавиатуру и «Возврат», записываем цену и запускаем тот же
#     сценарий, что и при ручном вводе цены (VacForm.price, F.text).
@router.callback_query(VacForm.price, F.data.regexp(r"^vac_price_choice:(free|deal)$"))
async def vacancy_price_choice(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    if await state.get_state() != VacForm.price.state:
        await cb.answer("Этот шаг публикации уже завершён.", show_alert=True)
        return

    # 1) удалить сообщение с клавиатурой (то, по которому кликнули)
    try:
        await cb.message.delete()
    except Exception:
        pass

    # 2) удалить «Возврат», если висит
    data = await state.get_data()
    nav_id = data.get("nav_msg_id")
    if nav_id:
        try:
            await cb.bot.delete_message(chat_id, nav_id)
        except Exception:
            pass

    # 3) определить текст цены из выбора
    choice = cb.data.split(":", 1)[1]
    price_text = "бесплатно" if choice == "free" else "по договоренности"

    # 4) сохранить цену в FSM и выставить состояние, как при вводе цены
    await state.update_data(price=price_text)
    await state.set_state(VacForm.price)

    # 5) Если есть ваш обработчик текстовой цены — переиспользуем его напрямую,
    #    чтобы дальше всё шло по тому же пути публикации.
    fn = globals().get("vacancy_input_price")
    if callable(fn):
        # лёгкий прокси-объект Message с нужными атрибутами
        class _ProxyMsg:
            def __init__(self, cb, text):
                self.chat = cb.message.chat
                self.bot = cb.bot
                self.from_user = cb.from_user
                self.text = text
            async def delete(self):  # при вызове text-хендлера мы не хотим падать
                pass
            async def answer(self, *a, **kw):
                return await cb.message.answer(*a, **kw)

        await fn(_ProxyMsg(cb, price_text), state)
    else:
        # 6) Фолбэк: просто сообщим, что цена установлена (если текстового хендлера нет).
        await cb.message.answer(f"Оплата установлена: <b>{price_text}</b>.", parse_mode="HTML")
        # тут можете вызвать вашу финализацию, если она есть:
        # end_fn = globals().get("vacancy_publish_finalize") or globals().get("vacancy_publish")
        # if callable(end_fn): await end_fn(cb, state)

    await cb.answer()
    print(f"[vacancy_add.py] handler=vacancy_price_choice chat_id={chat_id} choice={choice} price='{price_text}'")



# RU: Назад с шага «Описание» → вернуться к вводу заголовка
@router.callback_query(F.data == "vac_add_back:title")
async def vac_back_to_title(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # удалить кликнутое сообщение + прошлые nav/prompt
    try:
        await cb.message.delete()
    except Exception:
        pass
    data = await state.get_data()
    for key in ("nav_msg_id", "prompt_id"):
        mid = data.get(key)
        if mid:
            try:
                await cb.bot.delete_message(chat_id, mid)
            except Exception:
                pass

    # подготовить навигацию: Назад = в дерево категорий
    city_slug = data.get("city_slug")
    back_to_id = data.get("back_to_id")
    if back_to_id is None:
        try:
            cat_id = data.get("category_id")
            if cat_id is not None:
                async with SessionLocal() as s:
                    parent_id = (await s.execute(
                        select(Category.parent_id).where(Category.id == int(cat_id))
                    )).scalar_one_or_none()
                back_to_id = parent_id if parent_id is not None else VACANCY_ROOT_CATEGORY_ID
                await state.update_data(back_to_id=back_to_id)
        except Exception:
            pass

    buttons: list[list[InlineKeyboardButton]] = []
    if city_slug and back_to_id is not None:
        buttons.append([InlineKeyboardButton(
            text="⬅️ Назад", callback_data=f"vac_add_cat:{city_slug}:{back_to_id}"
        )])
    main_btn = await get_common_menu_button('main_menu', 'ru')
    if main_btn:
        buttons.append([main_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    # показать заново шаг заголовка
    nav = await cb.message.answer("◀️ Возврат", reply_markup=kb, parse_mode="HTML")
    await state.update_data(nav_msg_id=nav.message_id)

    prompt = await cb.message.answer("✏️ Введите <b>заголовок</b> вакансии:", parse_mode="HTML")
    await state.update_data(prompt_id=prompt.message_id)

    await state.set_state(VacForm.title)
    await cb.answer()
    print(f"[vacancy_add.py] vac_back_to_title ✓ | chat_id={chat_id}")


# RU: Назад с шага «Цена» → вернуться к вводу описания
@router.callback_query(F.data == "vac_add_back:descr")
async def vac_back_to_descr(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    from html import escape as _esc

    # удалить кликнутое сообщение + прошлые nav/prompt
    try:
        await cb.message.delete()
    except Exception:
        pass
    data = await state.get_data()
    for key in ("nav_msg_id", "prompt_id"):
        mid = data.get(key)
        if mid:
            try:
                await cb.bot.delete_message(chat_id, mid)
            except Exception:
                pass

    # навигация: Назад = к шагу «Заголовок»
    buttons = [[InlineKeyboardButton(text="⬅️ Назад", callback_data="vac_add_back:title")]]
    main_btn = await get_common_menu_button('main_menu', 'ru')
    if main_btn:
        buttons.append([main_btn])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    # показать заново шаг описания
    nav = await cb.message.answer("◀️ Возврат", reply_markup=kb, parse_mode="HTML")
    await state.update_data(nav_msg_id=nav.message_id)

    st = await state.get_data()
    title = _esc(st.get("title") or "—")
    try:
        tmpl = await get_text('sell_ask_descr', 'ru')
    except Exception:
        tmpl = None
    tmpl = tmpl or "Краткое описание (или «-» чтобы пропустить):"
    prompt = await cb.message.answer(
        f"<b>Вы уже ввели</b>\n• Заголовок: <i>{title}</i>\n\n{tmpl}",
        parse_mode="HTML",
    )
    await state.update_data(prompt_id=prompt.message_id)

    await state.set_state(VacForm.descr)
    await cb.answer()
    print(f"[vacancy_add.py] vac_back_to_descr ✓ | chat_id={chat_id}")


# ─────────────────────────────────────────────────────────────────────────────
# RU: Финальный шаг публикации вакансии.
# После ввода зарплаты мы сразу сохраняем объявление в БД (без фото и без flex),
# а затем показываем меню «Редактировать все поля / К объявлению / Меню вакансий».
# Обязательная очистка старых сообщений и финальный print-сообщение.
# ─────────────────────────────────────────────────────────────────────────────
@router.message(VacForm.price)
async def vacancy_input_price(m: Message, state: FSMContext):
    """Финализировать публикацию ровно один раз даже при повторной отправке."""
    lock = _vacancy_publish_locks.setdefault(m.from_user.id, asyncio.Lock())
    if lock.locked():
        await m.answer("Публикуем, пожалуйста, подождите.")
        return
    async with lock:
        if await state.get_state() != VacForm.price.state:
            return
        await _vacancy_input_price_locked(m, state)


async def _vacancy_input_price_locked(m: Message, state: FSMContext):
    chat_id = m.chat.id

    price_text = (m.text or "").strip()
    if not price_text:
        msg = await m.answer("Введите стоимость оплаты или воспользуйтесь кнопкой быстрого выбора.")
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])
        return

    # 0) удалить сообщение пользователя (канон)
    try:
        await m.delete()
    except Exception:
        pass

    # 1) очистка всех предыдущих сообщений/меню
    try:
        # если есть утилита удаления пользовательских сообщений — используем
        from app.routers.utils import clear_user_messages
        await clear_user_messages(chat_id, m.bot)
    except Exception:
        pass
    await clear_bot_messages(chat_id, m.bot)

    # 2) забрать данные мастера и сохранить цену
    data = await state.get_data()
    await state.update_data(price=price_text)
    data = await state.get_data()

    title = (data.get("title") or "").strip()
    descr = (data.get("descr") or "").strip()
    try:
        city_id = int(data["city_id"])
        cat_id = int(data["category_id"])
    except (KeyError, TypeError, ValueError):
        await m.answer("Не хватает данных города или категории. Начните публикацию заново.")
        await state.clear()
        return
    if not title or not price_text:
        await m.answer("Заголовок и стоимость не могут быть пустыми. Начните публикацию заново.")
        await state.clear()
        return

    # 3) сформировать контакт по умолчанию (как в других разделах)
    username = (m.from_user.username or "").strip()
    contact = f"@{username}" if username else "контакт не указан"

    # 4) создать и сохранить объявление
    from datetime import datetime
    try:
        async with SessionLocal() as s:
            city = await s.get(City, city_id)
            cat  = await s.get(Category, cat_id)
            if city is None or cat is None or not await _is_vacancy_category(s, cat):
                await m.answer("Город или категория больше не существует. Начните публикацию заново.")
                await state.clear()
                return

            l = Listing(
                city_id=city.id,
                category_id=cat.id,
                owner_id=m.from_user.id,
                title=title,
                price=price_text,
                descr=descr,
                contact=contact,
                photo_file_id=None,     # фото в вакансиях не используем
                is_sold=False,
                created_at=utcnow_naive(),
                type="vacancy",
                flex=None,              # доп.поля редактируются ПОСЛЕ публикации
                extra_category_id1=None,
                extra_category_id2=None,
            )
            ensure_expires_at(l)  # срок жизни 30 дней
            s.add(l)
            await s.flush()
            listing_id = l.id
            await s.commit()
    except Exception as e:
        await m.answer("Не удалось сохранить вакансию. Попробуйте ещё раз.")
        print(f"[vacancy_add.py] vacancy_input_price DB error: {e}")
        return

    # После успешного commit очищаем FSM до любых необязательных действий.
    # Поэтому сбой аналитики или Telegram не создаст дубль при повторе.
    await state.clear()

    from app.analytics import log_event
    try:
        await log_event("listing_created", user_id=m.from_user.id,
                        section="vacancy", entity_type="listing", entity_id=listing_id)
    except Exception as e:
        print(f"[vacancy_add.py] vacancy_input_price analytics error listing_id={listing_id}: {e}")

    # 5) собрать пост-публикационное меню (аналогично Услугам/Барахолке)
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()

    # тексты берём из БД, при недоступности — дефолты без «-db»
    t_pub  = (await get_text("sell_published", "ru")) or "✅ Объявление опубликовано."
    t_off  = (await get_text("sell_extras_offer", "ru")) or "Можно отредактировать любые поля, включая дополнительные."
    t_edit = (await get_text("vac_edit_all", "ru")) or "✏️ Редактировать все поля"
    t_open = (await get_text("vac_go_listing", "ru")) or "📄 К объявлению"
    t_menu = (await get_text("vac_to_menu", "ru")) or "≡ Меню вакансий"

    # 1) Редактирование всех полей
    kb.row(InlineKeyboardButton(text=t_edit, callback_data=f"vacancy_edit_overview:{listing_id}"))
    # 2) К объявлению (собственный роутер вакансий)
    kb.row(InlineKeyboardButton(text=t_open, callback_data=f"vac_view:{listing_id}:::my"))
    # 3) Меню вакансий
    kb.row(InlineKeyboardButton(text=t_menu, callback_data="go_isk"))
    # 4) Главное меню (если есть)
    main_btn = await get_common_menu_button('main_menu')
    if main_btn:
        kb.row(main_btn)

    # 6) сообщить пользователю и очистить состояние
    msg = await m.answer(f"{t_pub}\n\n{t_off}", reply_markup=kb.as_markup(), parse_mode="HTML")
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

    # 7) обязательный debug-print по канонам
    print(f"[vacancy_add.py] handler=vacancy_input_price published_id={listing_id} city_id={city.id} cat_id={cat.id}")


@router.callback_query(F.data.startswith("vac_flex_select:"), VacForm.flex)
async def vacancy_flex_select(cb: CallbackQuery, state: FSMContext):
    """Handle selection for a select-type extra field."""
    _, idx_str = cb.data.split(":", 1)
    idx = int(idx_str)
    data = await state.get_data()
    fields = data.get("flex_fields", []) or []
    field = fields[int(data.get("flex_idx", 0))]
    options = field.get("options", [])
    value = options[idx] if 0 <= idx < len(options) else None
    await _update_flex_value(state, value)
    await _ask_current_flex_field(cb, state)
    await cb.answer()


@router.callback_query(F.data.startswith("vac_flex_checkbox:"), VacForm.flex)
async def vacancy_flex_checkbox(cb: CallbackQuery, state: FSMContext):
    """Handle yes/no for checkbox-type extra field."""
    _, val = cb.data.split(":", 1)
    value = True if val == "1" else False if val == "0" else None
    await _update_flex_value(state, value)
    await _ask_current_flex_field(cb, state)
    await cb.answer()


@router.callback_query(F.data == "vac_flex_skip", VacForm.flex)
async def vacancy_flex_skip(cb: CallbackQuery, state: FSMContext):
    """Skip an optional extra field."""
    await _update_flex_value(state, None)
    await _ask_current_flex_field(cb, state)
    await cb.answer()


@router.callback_query(VacForm.confirm, F.data == "vac_publish")
async def vacancy_publish(cb: CallbackQuery, state: FSMContext):
    lock = _vacancy_publish_locks.setdefault(cb.from_user.id, asyncio.Lock())
    if lock.locked():
        await cb.answer("Публикуем, пожалуйста, подождите.")
        return
    async with lock:
        if await state.get_state() != VacForm.confirm.state:
            await cb.answer("Объявление уже опубликовано.")
            return
        await _vacancy_publish_locked(cb, state)


async def _vacancy_publish_locked(cb: CallbackQuery, state: FSMContext):
    """RU: финальный шаг — коммит в БД и показ меню редактирования/просмотра."""
    chat_id = cb.message.chat.id

    # Канон: чистим предыдущие сообщения/меню
    try:
        from app.routers.utils import clear_user_messages
        await clear_user_messages(chat_id, cb.bot)
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    data = await state.get_data()

    # 1) Считать обязательные данные мастера (заполнялись на предыдущих шагах)
    city_slug = data.get("pub_city_slug")
    cat_id = int(data.get("pub_cat_id") or 0)
    title = (data.get("title") or "").strip()
    price = (data.get("price") or "").strip()
    descr = (data.get("descr") or "").strip()
    contact = (data.get("contact") or "").strip()
    extra_cat1 = data.get("extra_category_id1")
    extra_cat2 = data.get("extra_category_id2")
    flex_dict = data.get("flex") or {}

    if not city_slug or not cat_id or not title:
        await cb.answer("Не хватает данных для публикации. Вернитесь и заполните поля.", show_alert=True)
        print("[vacancy_add.py] handler=vacancy_publish missing city/title/cat", data)
        return

    city_id = await _city_id_by_slug(city_slug)
    if not city_id:
        await cb.answer("Город не найден. Выберите город заново.", show_alert=True)
        print("[vacancy_add.py] handler=vacancy_publish bad city_slug=", city_slug)
        return

    # 2) Подготовить payload (flex -> сериализация)
    flex_payload = _flex_to_db(flex_dict)  # важно: не dict в SQLite

    # 3) Сохранить объявление
    async with SessionLocal() as s:
        obj = Listing(
            type="vacancy",
            city_id=city_id,
            category_id=cat_id,
            owner_id=cb.from_user.id,
            title=title,
            price=price,
            descr=descr,
            contact=contact,
            photo_file_id=None,      # вакансия — без фото
            is_sold=False,
            created_at=utcnow_naive(),  # если в модели нет default
            flex=flex_payload,
            extra_category_id1=extra_cat1,
            extra_category_id2=extra_cat2,
        )
        ensure_expires_at(obj)  # срок жизни 30 дней
        s.add(obj)
        await s.flush()            # получим obj.id без доп. запроса
        listing_id = obj.id
        await s.commit()

    # Commit завершён: очищаем мастер до аналитики/Telegram, чтобы повторный
    # callback не создал вторую запись при сбое необязательного шага.
    await state.clear()

    from app.analytics import log_event
    try:
        await log_event("listing_created", user_id=cb.from_user.id,
                        section="vacancy", entity_type="listing", entity_id=listing_id)
    except Exception as e:
        print(f"[vacancy_add.py] vacancy_publish analytics error listing_id={listing_id}: {e}")

    # 4) Пост-публикационное меню (аналог других разделов)
    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="✏️ Редактировать объявление", callback_data=f"vacancy_edit_overview:{listing_id}")],
        [InlineKeyboardButton(text="🔎 Посмотреть", callback_data=f"vac_view:{listing_id}:::my")],
        [InlineKeyboardButton(text="≡ Меню вакансий", callback_data="go_isk")],
    ]
    main_btn = await get_common_menu_button("main_menu")
    if main_btn:
        buttons.append([main_btn])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    msg = await cb.message.answer("✅ Объявление опубликовано.", reply_markup=kb, parse_mode="HTML")
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()

    print(f"[vacancy_add.py] handler=vacancy_publish listing_id={listing_id} user_id={cb.from_user.id}")


@router.callback_query(F.data == "vac_cancel")
async def vacancy_cancel(cb: CallbackQuery, state: FSMContext):
    """Cancel the vacancy posting and reset the state."""
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    await state.clear()
    msg = await cb.message.answer("Публикация вакансии отменена.", reply_markup=await _vacancy_nav_keyboard())
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
    await cb.answer()
