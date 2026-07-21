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
from sqlalchemy import select, text as sql_text
from datetime import datetime
from app.models import utcnow_naive
from app.lifecycle import ensure_expires_at

from app.database import SessionLocal
from app.models import City, Category, Listing
from aiogram.types.input_file import FSInputFile

from app.routers.utils import clear_bot_messages, last_bot_messages, sent_photo_messages, my_listing_messages, delete_photo_prompts, get_text, register_bot_messages
from app.moderation import is_muted
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

from app.routers.utils import safe_edit_or_send
import json

from html import escape as _esc

from app.routers.utils_category_title import format_category_title

from app.routers.utils_kb import grid3





router = Router(name="market_addl")

# ========== КЭШИ для альбомов ==========
# Telegram не гарантирует уникальность media_group_id между разными чатами,
# поэтому все временные данные изолированы ключом (chat_id, media_group_id).
AlbumKey = tuple[int, str]
media_group_cache: dict[AlbumKey, list[str]] = {}
media_group_tasks: dict[AlbumKey, asyncio.Task | None] = {}
media_group_wait_msg: dict[AlbumKey, int] = {}


async def _clear_market_album_cache(chat_id: int, bot=None) -> None:
    """Отменить незавершённые альбомы только текущего чата."""
    current = asyncio.current_task()
    keys = {key for key in media_group_cache if key[0] == chat_id}
    keys.update(key for key in media_group_tasks if key[0] == chat_id)
    keys.update(key for key in media_group_wait_msg if key[0] == chat_id)
    wait_ids: list[int] = []
    for key in keys:
        task = media_group_tasks.pop(key, None)
        if task and task is not current and not task.done():
            task.cancel()
        media_group_cache.pop(key, None)
        wait_id = media_group_wait_msg.pop(key, None)
        if isinstance(wait_id, int) and wait_id > 0:
            wait_ids.append(wait_id)
    if bot is not None:
        for wait_id in wait_ids:
            try:
                await bot.delete_message(chat_id, wait_id)
            except Exception:
                pass

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


def _first_lines(text: str, n: int = 3) -> str:
    """Вернуть первые n строк (если строк больше — добавить «…»)."""
    if not text:
        return ""
    lines = str(text).splitlines()
    if len(lines) <= n:
        return "\n".join(lines)
    return "\n".join(lines[:n]) + "\n…"

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
async def send_with_nav(m, text, parse_mode=None, reply_markup=None):
    nav_markup = await sell_nav_keyboard()
    nav_text = await get_text('return_to_menu', 'ru') or "Return"
    nav_msg = await m.answer(nav_text, reply_markup=nav_markup)
    last_bot_messages.setdefault(m.chat.id, []).append(nav_msg.message_id)
    await register_bot_messages(m.chat.id, [nav_msg.message_id])
    msg = await m.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
    last_bot_messages.setdefault(m.chat.id, []).append(msg.message_id)
    await register_bot_messages(m.chat.id, [msg.message_id])
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
                "label": (str(f.get("label") or "") or (await get_text("vac_add_flex_default_label", "ru") or "Поле")).strip(),
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
            msg = await m_or_cbmsg.answer(await get_text("market_add_flex_done_thanks", "ru") or "Готово. Спасибо!", reply_markup=nav_kb)
            last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
            await register_bot_messages(chat_id, [msg.message_id])
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
            msg = await m_or_cbmsg.answer(await get_text("market_add_flex_done_thanks", "ru") or "Готово. Спасибо!", reply_markup=nav_kb)
            last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
            await register_bot_messages(chat_id, [msg.message_id])
            await state.clear()
            print(f"FUNC: ask_current_flex_field | done(after_pub) | chat_id: {chat_id}")
            return
        # обычный сценарий (до публикации)
        await preview_and_confirm(m_or_cbmsg, state)
        await state.set_state(Sell.confirm)
        print(f"FUNC: ask_current_flex_field | done(preview) | chat_id: {chat_id}")
        return

    f = fields[idx]
    label = f.get("label") or (await get_text("vac_add_flex_default_label", "ru") or "Поле")
    ftype = f.get("type")
    required = bool(f.get("required"))

    skip_text = await get_text("btn_skip", "ru") or "Пропустить"

    rows = []

    if ftype == "select":
        for i, opt in enumerate(f.get("options", [])):
            rows.append([InlineKeyboardButton(text=str(opt), callback_data=f"sell_flex_select:{i}")])
        if not required:
            rows.append([InlineKeyboardButton(text=skip_text, callback_data="sell_flex_skip")])
        prompt_tmpl = await get_text("market_add_flex_select_prompt_tmpl", "ru") or "({idx}/{total}) <b>{label}</b>\n\nВыберите один из вариантов:"
        prompt = prompt_tmpl.format(idx=idx + 1, total=len(fields), label=label)
    elif ftype == "checkbox":
        rows.append([
            InlineKeyboardButton(text=await get_text("vac_add_checkbox_yes", "ru") or "✅ Да", callback_data="sell_flex_checkbox:1"),
            InlineKeyboardButton(text=await get_text("admin_panel_btn_no", "ru") or "❌ Нет", callback_data="sell_flex_checkbox:0"),
        ])
        if not required:
            rows.append([InlineKeyboardButton(text=skip_text, callback_data="sell_flex_skip")])
        prompt_tmpl = await get_text("market_add_flex_checkbox_prompt_tmpl", "ru") or "({idx}/{total}) <b>{label}</b>\n\nВыберите вариант:"
        prompt = prompt_tmpl.format(idx=idx + 1, total=len(fields), label=label)
    else:
        if not required:
            rows.append([InlineKeyboardButton(text=skip_text, callback_data="sell_flex_skip")])
        prompt_tmpl = await get_text("market_add_flex_text_prompt_tmpl", "ru") or "({idx}/{total}) <b>{label}</b>\n\nВведите значение{suffix}"
        if ftype == "number":
            suffix = await get_text("market_add_flex_number_suffix", "ru") or " (число)."
        else:
            suffix = await get_text("market_add_flex_default_suffix", "ru") or "."
        prompt = prompt_tmpl.format(idx=idx + 1, total=len(fields), label=label, suffix=suffix)

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
    await register_bot_messages(chat_id, [msg.message_id])

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

    # — «что уже ввели» (вверху) —
    import html as _html
    data   = await state.get_data()
    _title = (data.get("title") or "").strip()
    _descr = (data.get("descr") or "").strip()
    _price = (data.get("price") or "").strip()

    if _descr:
        _lines = _descr.splitlines()
        _descr_short = "\n".join(_lines[:3]) + ("\n…" if len(_lines) > 3 else "")
    else:
        _descr_short = ""

    helper_tmpl = (
        await get_text("market_add_helper_title_descr_price_tmpl", lang)
        or "<b>Вы уже ввели</b>\n• Заголовок: <i>{title}</i>\n• Описание: <i>{descr}</i>\n• Стоимость: <i>{price}</i>"
    )
    helper = helper_tmpl.format(
        title=_html.escape(_title) if _title else '—',
        descr=_html.escape(_descr_short) if _descr_short else '—',
        price=_html.escape(_price) if _price else '—',
    )

    # — порядок: helper → пустая строка → инструкция —
    text_main = f"{helper}\n\n{text_main}"

    # RU: Плашка «Возврат» (Назад / Главное меню) — СВЕРХУ, над всем шагом,
    #     как на остальных шагах мастера (железное правило навигации).
    nav_markup = await sell_nav_keyboard(lang)
    nav_text = await get_text('return_to_menu', 'ru') or "Возврат"
    nav_msg = await m.answer(nav_text, reply_markup=nav_markup)
    last_bot_messages.setdefault(m.chat.id, []).append(nav_msg.message_id)
    await register_bot_messages(m.chat.id, [nav_msg.message_id])

    # RU: показать уже загруженные фото, чтобы пользователь видел, что уже сохранено.
    preview_ids: list[int] = []
    photos_so_far = (data.get("photos", []) or [])[:photo_count]
    if photos_so_far:
        if len(photos_so_far) == 1:
            p_msg = await m.answer_photo(photos_so_far[0])
            preview_ids = [p_msg.message_id]
        else:
            media = [InputMediaPhoto(media=fid) for fid in photos_so_far]
            p_msgs = await m.bot.send_media_group(m.chat.id, media)
            preview_ids = [pm.message_id for pm in p_msgs]
        last_bot_messages.setdefault(m.chat.id, []).extend(preview_ids)
        await register_bot_messages(m.chat.id, preview_ids)

    msg = await m.answer(
        text_main,
        reply_markup=photo_keyboard(photo_count),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    last_bot_messages.setdefault(m.chat.id, []).append(msg.message_id)
    await register_bot_messages(m.chat.id, [msg.message_id])

    msg2 = None
    if text_tip:
        msg2 = await m.answer(text_tip)
        last_bot_messages.setdefault(m.chat.id, []).append(msg2.message_id)
        await register_bot_messages(m.chat.id, [msg2.message_id])
        await state.update_data(photo_prompt_msgs=[nav_msg.message_id] + preview_ids + [msg.message_id, msg2.message_id])
    else:
        await state.update_data(photo_prompt_msgs=[nav_msg.message_id] + preview_ids + [msg.message_id])

    print(
        f"[market_add.py] send_photo_prompt ✓ | chat_id={m.chat.id} | user_id={m.from_user.id} | "
        f"photo_count={photo_count} | msg_id={msg.message_id} | msg2_id={getattr(msg2, 'message_id', None)}"
    )


# ─────────────────── /sell start ───────────
@router.message(Command(commands=["sell"]))
async def cmd_sell(m: Message, state: FSMContext):
    if await is_muted(m.from_user.id):
        await m.answer(await get_text("err_user_muted", "ru") or "⛔️ Ваш аккаунт временно ограничен в публикации нового контента. Вы можете написать администратору через «Обратную связь».")
        return
    await clear_bot_messages(m.chat.id, m.bot)
    await _clear_market_album_cache(m.chat.id, m.bot)
    await state.clear()
    async with SessionLocal() as s:
        cities = (await s.execute(select(City))).scalars().all()
    kb = await cities_inline(cities)
    header = await get_text('sell_choose_city', 'ru') or "Create a listing.\nFirst, choose a city:"
    msg = await m.answer(
        header,
        reply_markup=kb
    )
    last_bot_messages.setdefault(m.chat.id, []).append(msg.message_id)
    await register_bot_messages(m.chat.id, [msg.message_id])
    await state.set_state(Sell.city)

@router.callback_query(F.data == "sell_start")
async def sell_start_button(cb: CallbackQuery, state: FSMContext):
    if await is_muted(cb.from_user.id):
        await cb.answer(await get_text("err_user_muted", "ru") or "⛔️ Ваш аккаунт временно ограничен в публикации нового контента. Вы можете написать администратору через «Обратную связь».", show_alert=True)
        return
    chat_id = cb.message.chat.id
    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)
    await _clear_market_album_cache(chat_id, cb.bot)
    await state.clear()
    async with SessionLocal() as s:
        cities = (await s.execute(select(City))).scalars().all()
    header = await get_text('sell_choose_city', 'ru') or "Create a listing.\nFirst, choose a city:"
    msg = await cb.bot.send_message(
        chat_id,
        header,
        reply_markup=await cities_inline(cities)
    )
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    await state.set_state(Sell.city)
    await cb.answer()

# ─────────────── шаг 1 – город ─────────────
@router.callback_query(F.data.startswith("sell_city:"), Sell.city)
async def sell_city(cb: CallbackQuery, state: FSMContext):
    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    city_slug = cb.data.split(":")[1]
    async with SessionLocal() as s:
        city = (await s.execute(select(City).where(City.slug == city_slug))).scalar_one()
        equip_root = (await s.execute(select(Category).where(Category.slug == "equip"))).scalar_one()
        subcats = (await s.execute(
            select(Category).where(Category.parent_id == equip_root.id)
            .order_by(sql_text("order_num"), Category.name)  # как при просмотре
        )).scalars().all()
    await state.update_data(city_id=city.id, city_name=city.name, city_slug=city_slug)
    fmt = []
    for sc in subcats:
        title = await format_category_title(sc.id, (sc.name or "").strip(), SessionLocal)
        fmt.append(type("Proxy", (), {"id": sc.id, "name": title, "slug": sc.slug}))
    kb = await equip_inline(fmt, city_slug)

    # --- ВЫНОСИМ ТЕКСТ В БД ---
    template = await get_text('sell_choose_category', 'ru') or "City: <b>{city_name}</b>\nChoose a category:"
    text = template.format(city_name=city.name)

    msg = await cb.message.answer(
        text,
        reply_markup=kb
    )
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
    await state.set_state(Sell.cat)
    await cb.answer()


# ─────────────── шаг 2 – категория ─────────
@router.callback_query(F.data.startswith("sell_cat:"), Sell.cat)
async def sell_cat(cb: CallbackQuery, state: FSMContext):
    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    _, city_slug, cat_id = cb.data.split(":")
    cat_id = int(cat_id)
    async with SessionLocal() as s:
        cat = (await s.execute(select(Category).where(Category.id == cat_id))).scalar_one()
        subcats = (await s.execute(select(Category).where(Category.parent_id == cat_id).order_by(sql_text("order_num"), Category.name))).scalars().all()
    if subcats:
        fmt = []
        for sc in subcats:
            title = await format_category_title(sc.id, (sc.name or "").strip(), SessionLocal)
            fmt.append(type("Proxy", (), {"id": sc.id, "name": title, "slug": sc.slug}))
        kb = await equip_inline(fmt, city_slug)
        # --- Получаем текст из базы ---
        template = await get_text('sell_choose_subcategory', 'ru') or "Category: <b>{cat_name}</b>\nChoose a subcategory:"
        text = template.format(cat_name=cat.name)
        msg = await cb.message.answer(
            text,
            reply_markup=kb,
        )
        last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
        await register_bot_messages(cb.message.chat.id, [msg.message_id])
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


# RU: Клавиатура «Пропустить» под шагом описания (кнопка вместо ввода «-»).
async def descr_skip_keyboard() -> InlineKeyboardMarkup:
    skip_text = await get_text("btn_skip", "ru") or "Пропустить"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=skip_text, callback_data="sell_descr_skip")]
    ])


# RU: Рендер шага «Описание» — читает уже сохранённый заголовок из FSM.
#     Общий для перехода вперёд (после Title) и кнопки «Назад» (из Price).
async def _render_descr_step(target, state: FSMContext):
    data = await state.get_data()
    title = data.get("title") or ""

    template = await get_text('sell_ask_descr', 'ru') or "Short description (or tap Skip):"

    from html import escape as _esc
    helper_tmpl = await get_text("vac_add_already_ended_for_helper", "ru") or "<b>Вы уже ввели</b>\n• Заголовок: <i>{title}</i>"
    helper = helper_tmpl.format(title=_esc(title) or '—')
    await send_with_nav(target, f"{helper}\n\n{template}", parse_mode="HTML", reply_markup=await descr_skip_keyboard())


# RU: Рендер шага «Стоимость» — читает уже сохранённые заголовок и описание из FSM.
#     Общий для перехода вперёд (после Descr) и кнопки «Назад» (из Photo).
async def _render_price_step(target, state: FSMContext):
    data = await state.get_data()
    title = data.get("title") or ""
    descr = data.get("descr") or ""
    # первые 3 строки описания + «…» если длиннее
    lines = descr.splitlines() if descr else []
    descr_short = "\n".join(lines[:3]) + ("\n…" if len(lines) > 3 else "")

    template = (await get_text('sell_ask_price', 'ru')) or "Enter <b>price</b> (e.g.: 150 € or 12,000 rsd):"

    from html import escape as _esc
    helper_tmpl = (
        await get_text("vac_add_already_entered_title_descr_tmpl", "ru")
        or "<b>Вы уже ввели</b>\n• Заголовок: <i>{title}</i>\n• Описание: <i>{descr}</i>"
    )
    helper = helper_tmpl.format(title=_esc(title) or '—', descr=_esc(descr_short) or '—')
    await send_with_nav(target, f"{helper}\n\n{template}", parse_mode="HTML")


# RU: Общий шаг перехода Описание → Стоимость: и по тексту, и по кнопке «Пропустить».
async def _advance_from_descr(m_for_answer, chat_id: int, bot, state: FSMContext, descr_text: str | None):
    await clear_bot_messages(chat_id, bot)
    await state.update_data(descr=descr_text)
    await state.set_state(Sell.price)
    await _render_price_step(m_for_answer, state)


# ─────────────── шаг 3 – title ─────────────
# RU: Шаг «Заголовок» — сохраняем, удаляем сообщение пользователя,
#     выводим СНАЧАЛА «что уже ввели», ПОТОМ инструкцию для описания.
@router.message(Sell.title, F.text)
async def sell_title(m: Message, state: FSMContext):
    chat_id = m.chat.id
    await clear_bot_messages(chat_id, m.bot)
    try:
        await m.delete()
    except Exception:
        pass

    title = (m.text or "").strip()
    await state.update_data(title=title)
    await state.set_state(Sell.descr)
    await _render_descr_step(m, state)

    print(f"[market_add.py] sell_title ✓ | chat_id={chat_id} | user_id={m.from_user.id} | field=title")


# ─────────────── шаг 4 – descr ─────────────
# RU: Шаг «Описание» — сохраняем, удаляем сообщение пользователя,
#     ПОКАЗЫВАЕМ СНАЧАЛА «что уже ввели» (заголовок + ≤3 строки описания),
#     ПОТОМ инструкцию для стоимости.
@router.message(Sell.descr)
async def sell_descr(m: Message, state: FSMContext):
    chat_id = m.chat.id
    try:
        await m.delete()
    except Exception:
        pass

    text = (m.text or "").strip()
    descr_text = None if text == "-" else (text or None)
    await _advance_from_descr(m, chat_id, m.bot, state, descr_text)

    print(f"[market_add.py] sell_descr ✓ | chat_id={chat_id} | user_id={m.from_user.id} | field=descr")


# RU: Клик по кнопке «Пропустить» на шаге описания — то же самое, что ввод «-».
@router.callback_query(Sell.descr, F.data == "sell_descr_skip")
async def sell_descr_skip(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await _advance_from_descr(cb.message, chat_id, cb.bot, state, None)
    await cb.answer()

    print(f"[market_add.py] sell_descr_skip ✓ | chat_id={chat_id} | user_id={cb.from_user.id} | field=descr")

# ─────────────── шаг 5 – price ─────────────
# RU: Шаг «Стоимость» — сохраняем, удаляем сообщение, переходим к фото.
#     (Если захотите — можно аналогично показать «Вы уже ввели» и на шаге фото.)
@router.message(Sell.price, F.text)
async def sell_price(m: Message, state: FSMContext):
    chat_id = m.chat.id
    await clear_bot_messages(chat_id, m.bot)
    try:
        await m.delete()
    except Exception:
        pass

    price = (m.text or "").strip()
    await state.update_data(price=price)
    await state.set_state(Sell.photo)

    # Переход на промпт для фото — с учётом уже загруженных (возврат «Назад»)
    _cnt = len(((await state.get_data()).get("photos") or []))
    msg = await send_photo_prompt(m, _cnt, state)

    print(f"[market_add.py] sell_price ✓ | chat_id={chat_id} | user_id={m.from_user.id} | field=price | msg_id={getattr(msg, 'message_id', '-')}")


# ================== **ШАГ 6 — ФОТО** ==================
@router.message(Sell.photo, F.photo)
async def sell_photo(m: Message, state: FSMContext):
    if m.media_group_id:
        key: AlbumKey = (m.chat.id, str(m.media_group_id))
        if key not in media_group_cache:
            media_group_cache[key] = []
        media_group_cache[key].append(m.photo[-1].file_id)
        await _remember_and_delete_user_media(m)

        if key not in media_group_tasks:
            # Плейсхолдер ставим до первого await, иначе две части альбома
            # могут одновременно создать две задачи финализации.
            media_group_tasks[key] = None
            template = await get_text('sell_wait_photos', 'ru') or "⏳ Please wait — uploading photos…"
            wait_msg = await m.answer(template)
            media_group_wait_msg[key] = wait_msg.message_id
            media_group_tasks[key] = asyncio.create_task(finalize_album(m, state, key))
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
        await preview_and_confirm(m, state)
        await state.set_state(Sell.confirm)
    else:
        await delete_photo_prompts(m, state)
        await send_photo_prompt(m, len(photos), state)

# ================== ФИНАЛИЗАЦИЯ АЛЬБОМА ==================
async def finalize_album(m: Message, state: FSMContext, key: AlbumKey):
    try:
        await asyncio.sleep(1.5)
    except asyncio.CancelledError:
        raise

    # Общая навигация могла очистить FSM, пока Telegram собирал альбом.
    if await state.get_state() != Sell.photo.state:
        await _clear_market_album_cache(m.chat.id, m.bot)
        return

    album_photos = media_group_cache.pop(key, [])
    media_group_tasks.pop(key, None)

    data = await state.get_data()
    photos = data.get("photos", []) or []
    for fid in album_photos:
        if len(photos) < 3:
            photos.append(fid)
    photos = photos[:3]
    await state.update_data(photos=photos)

    wait_msg_id = media_group_wait_msg.pop(key, None)
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
    await _clear_user_media(cb.message.chat.id, cb.bot)

    # await start_flex_flow(cb.message, state)
    await preview_and_confirm(cb.message, state)
    await state.set_state(Sell.confirm)
    await cb.answer()

@router.callback_query(Sell.photo, F.data == "sell_cancel")
async def sell_cancel_photo(cb: CallbackQuery, state: FSMContext):
    await delete_photo_prompts(cb.message, state)
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    await _clear_user_media(cb.message.chat.id, cb.bot)

    
    # Сообщение об отмене (русский из БД, иначе английский дефолт)
    cancel_text = await get_text('sell_cancelled', lang="ru") or "❌ Listing creation cancelled."

    msg1 = await cb.message.answer(cancel_text)
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg1.message_id)
    await register_bot_messages(cb.message.chat.id, [msg1.message_id])
    
    # Формируем клавиатуру, где уже есть кнопка "Главное меню"
    nav_kb = await sell_nav_keyboard()
    msg2 = await cb.message.answer(
        (await get_text('main_menu', 'ru')) or "Main menu",
        reply_markup=nav_kb
    )

    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg2.message_id)
    await register_bot_messages(cb.message.chat.id, [msg2.message_id])

    await state.clear()
    await cb.answer()

# === Гибкие поля: select ===
@router.callback_query(Sell.flex, F.data.startswith("sell_flex_select:"))
async def flex_select_pick(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    fields = data.get("flex_fields", []) or []
    idx = int(data.get("flex_idx", 0))
    if idx >= len(fields) or fields[idx].get("type") != "select":
        await cb.answer(await get_text("market_add_flex_invalid_action", "ru") or "Неверное действие", show_alert=True); return

    try:
        opt_idx = int(cb.data.split(":")[1])
    except Exception:
        await cb.answer(await get_text("err_bad_data", "ru") or "Неверные данные", show_alert=True); return

    options = fields[idx].get("options", [])
    if opt_idx < 0 or opt_idx >= len(options):
        await cb.answer(await get_text("market_add_flex_invalid_option", "ru") or "Неверная опция", show_alert=True); return

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
        await cb.answer(await get_text("market_add_flex_invalid_action", "ru") or "Неверное действие", show_alert=True); return

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
    await cb.answer(await get_text("vac_add_flex_skipped_toast", "ru") or "Пропущено")
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
        await m.answer(await get_text("market_add_flex_use_buttons", "ru") or "Пожалуйста, воспользуйтесь кнопками ниже.")
        return

    value = (m.text or "").strip()
    if ftype == "number":
        # мягкая валидация: пустое запрещаем, остальное — как есть
        if not value:
            await m.answer(await get_text("market_add_flex_need_number", "ru") or "Введите число или нажмите «Пропустить».")
            return

    flex_values = data.get("flex_values", {}) or {}
    flex_values[key] = value
    await state.update_data(flex_values=flex_values, flex_idx=idx + 1)

    await ask_current_flex_field(m, state)


# --- Предпросмотр + confirm ---
async def preview_and_confirm(m: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    # Заголовок шапки раздела (город → категория) оставляем.
    # Пользовательский ввод экранируем: «Mackie <3» не должен ломать HTML.
    header = f"<b>{_esc(data['city_name'])} → {_esc(data['cat_name'])}</b>\n"

    # Порядок: Название → Описание → Цена
    title_line = _esc(f"{data['title']}".strip())
    descr_line = _esc((data.get('descr') or "").strip())
    price_label = (await get_text('listing_price', 'ru')) or "Price"
    price_line = _esc(f"{price_label}: {data.get('price', '')}".strip())

    parts = [title_line]
    if descr_line:
        parts.append(descr_line)
    if data.get('price'):
        parts.append(price_line)

    header += "\n".join(parts)

    # Добавляем собранные гибкие поля
    flex_fields = data.get("flex_fields") or []
    flex_values = data.get("flex_values") or {}
    if flex_fields:
        default_label = await get_text("vac_add_flex_default_label", "ru") or "Поле"
        bool_yes = await get_text("admin_fields_yes", "ru") or "Да"
        bool_no = await get_text("admin_fields_no", "ru") or "Нет"
        lines = []
        for f in flex_fields:
            label = f.get("label") or default_label
            key = f.get("key")
            ftype = f.get("type")
            required = bool(f.get("required"))
            if key in flex_values:
                v = flex_values[key]
                if ftype == "checkbox":
                    v = bool_yes if bool(v) else bool_no
                lines.append(_esc(f"{label}: {v}"))
            else:
                if required:
                    lines.append(_esc(f"{label}: —"))
        if lines:
            header += "\n\n" + "\n".join(lines)

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
    await register_bot_messages(m.chat.id, [msg.message_id])
    if sent_ids:
        sent_photo_messages.setdefault(m.chat.id, []).extend(sent_ids)


# ---------- Публикация объявления (без изменений основного сценария) ----------
from collections import defaultdict as _dd
_market_publish_locks: dict[int, asyncio.Lock] = _dd(asyncio.Lock)


@router.callback_query(Sell.confirm, F.data == "sell_ok")
async def sell_ok(cb: CallbackQuery, state: FSMContext):
    """Не допускаем параллельную публикацию двойным нажатием кнопки."""
    lock = _market_publish_locks[cb.from_user.id]
    if lock.locked():
        await cb.answer(await get_text("services_add_publishing_wait", "ru") or "Публикуем, пожалуйста, подождите.")
        return
    async with lock:
        # Второй update мог попасть в диспетчер до смены состояния первым update.
        if await state.get_state() != Sell.confirm.state:
            await cb.answer(await get_text("services_add_already_published", "ru") or "Объявление уже опубликовано.")
            return
        await _sell_ok_locked(cb, state)


async def _sell_ok_locked(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # Подчистим временные кеши альбома только текущего пользователя.
    await _clear_market_album_cache(chat_id, cb.bot)

    data = await state.get_data()
    # Жёсткая проверка обязательных полей
    for k in ("city_id", "cat_id", "title", "price"):
        if not data.get(k):
            tmpl = await get_text("services_add_missing_field_tmpl", "ru") or "Не хватает поля: {field}"
            await cb.answer(tmpl.format(field=k), show_alert=True)
            print(f"FUNC: sell_ok | MISSING {k} | data_keys={list(data.keys())}")
            return

    # ── Этап 1: сохранение в БД. Только его ошибка означает «не сохранилось».
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
                created_at=utcnow_naive(),
                type="market",
                photo_file_id=",".join(data.get("photos", [])) if data.get("photos") else None,
            )

            # Если доп. поля были пройдены до подтверждения — сохраним сразу
            flex_data = data.get("flex_values")
            if flex_data:
                try:
                    l.flex = json.dumps(flex_data, ensure_ascii=False)
                except Exception:
                    l.flex = None

            ensure_expires_at(l)  # срок жизни 30 дней
            s.add(l)
            await s.commit()
            # refresh не нужен: expire_on_commit=False, l.id уже присвоен.
            # После commit в этом try ничего нет — «не удалось сохранить»
            # означает ровно то, что написано.
    except Exception as e:
        err_tmpl = await get_text("services_add_save_error_tmpl", "ru") or "Ошибка сохранения: {error}"
        await cb.answer(err_tmpl.format(error=type(e).__name__), show_alert=True)
        detail_tmpl = await get_text("market_add_save_error_detail", "ru") or "❌ Не удалось сохранить объявление.\n<code>{error}</code>"
        await cb.message.answer(detail_tmpl.format(error=e), parse_mode="HTML")
        print(f"FUNC: sell_ok | DB ERROR {e}")
        return

    # Сохраняем id для последующего сохранения flex после публикации.
    # Состояние снимаем сразу после commit: повторный клик по старой
    # кнопке «Опубликовать» не создаст дубль (данные в FSM остаются —
    # они нужны мастеру доп. полей). Сбой FSM здесь — отдельная беда:
    # объявление уже в БД, пользователю нельзя говорить «не сохранилось».
    try:
        await state.update_data(listing_id=l.id)
        await state.set_state(None)
    except Exception as e:
        print(f"FUNC: sell_ok | FSM ERROR after save listing_id={l.id}: {e}")

    # ── Этап 2: аналитика и экран «опубликовано». Объявление УЖЕ сохранено —
    # любая ошибка здесь не должна выглядеть как «не удалось сохранить».
    try:
        from app.analytics import log_event
        try:
            await log_event("listing_created", user_id=cb.from_user.id,
                            section="market", entity_type="listing", entity_id=l.id)
        except Exception as e:
            print(f"FUNC: sell_ok | analytics error listing_id={l.id}: {e}")

        # Убираем служебные сообщения
        await clear_bot_messages(chat_id, cb.bot)
        await _clear_user_media(chat_id, cb.bot)


        # Экран «опубликовано» + предложение про доп. поля
        text_pub   = (await get_text('sell_published', 'ru')) or "✅ Объявление опубликовано! -db"
        text_extra = (await get_text('sell_extras_offer', 'ru')) or "При желании укажите дополнительные сведения для этой категории:"
        if isinstance(text_extra, str) and text_extra.strip().startswith("[Text not found"):
            text_extra = "При желании укажите дополнительные сведения для этого объявления:"

        # Верхние действия
        # Получаем слаги для кнопки «К объявлению» — свежей сессией
        async with SessionLocal() as s2:
            city = (await s2.execute(select(City).where(City.id == l.city_id))).scalar_one()
            cat  = (await s2.execute(select(Category).where(Category.id == l.category_id))).scalar_one()
        rows = [
            [InlineKeyboardButton(
                text=await get_text("vac_edit_all", "ru") or "✏️ Редактировать все поля",
                callback_data=f"edit_listing_overview:{l.id}"
            )],
            [InlineKeyboardButton(
                text=await get_text("vac_go_listing", "ru") or "📄 К объявлению",
                callback_data=f"listing:{l.id}:{city.slug}:{cat.slug}:my"
            )],
        ]
        print(
            f"FUNC: sell_ok | buttons => edit_listing_overview:{l.id} ; "
            f"listing:{l.id}:{city.slug}:{cat.slug}:my | chat_id={chat_id} | user_id={cb.from_user.id}"
        )

        # НИЗ: «Барахолка» + «Главное меню» (без «Назад»)
        market_btn = await get_common_menu_button('go_market', 'ru')  # код из вашего главного меню
        main_btn   = await get_common_menu_button('main_menu', 'ru')

        nav = []
        if market_btn:
            nav.append(InlineKeyboardButton(text=market_btn.text, callback_data=market_btn.callback_data))
        else:
            nav.append(InlineKeyboardButton(text=await get_text("market_add_btn_market_fallback", "ru") or "💸 Барахолка", callback_data="go_market"))
        if main_btn:
            nav.append(InlineKeyboardButton(text=main_btn.text, callback_data=main_btn.callback_data))
        if nav:
            rows.append(nav)

        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        msg = await cb.message.answer(f"{text_pub}\n\n{text_extra}", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])

        # Стейт не чистим — нужен для доп. полей
        await cb.answer()
        print(f"FUNC: sell_ok | SAVED listing_id={l.id} | chat_id={chat_id} | user_id={cb.from_user.id} | msg_id={msg.message_id}")

    except Exception as e:
        # Экран не собрался, но объявление сохранено — говорим правду
        print(f"FUNC: sell_ok | POST-SAVE ERROR listing_id={l.id}: {e}")
        try:
            await cb.answer()
            fallback_tmpl = (
                await get_text("market_add_published_fallback_screen", "ru")
                or "✅ Объявление №{id} опубликовано. Экран не загрузился — найти его можно в «Моих объявлениях»."
            )
            await cb.message.answer(fallback_tmpl.format(id=l.id))
        except Exception:
            pass


@router.callback_query(Sell.confirm, F.data == "sell_cancel")
async def sell_cancel(cb: CallbackQuery, state: FSMContext):
    await _clear_market_album_cache(cb.message.chat.id, cb.bot)
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
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
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
        # Тип запоминаем до удаления: этим обработчиком пользуются и Услуги,
        # и навигация после удаления должна вести в родной раздел.
        listing_type = (l.type or "market").strip()
        await s.delete(l)
        await s.commit()

    # Удаление завершает любой текущий сценарий (поиск и т.п.): иначе
    # следующий текст пользователя попал бы в обработчик поискового запроса.
    # Данные не трогаем — контекст поиска ещё нужен для возвратов.
    await state.set_state(None)
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
    await register_bot_messages(chat_id, [msg.message_id])

    # «Назад» ведёт к списку «Мои …» родного раздела. Прежний sell_back
    # здесь запускал мастер создания объявления Барахолки — даже после
    # удаления услуги.
    if listing_type == "service":
        back_btn = InlineKeyboardButton(text=await get_text("market_add_btn_my_services", "ru") or "⬅️ Мои услуги", callback_data="my_services")
    else:
        back_btn = InlineKeyboardButton(text=await get_text("market_add_btn_my_listings", "ru") or "⬅️ Мои объявления", callback_data="my_listings")
    rows = [[back_btn]]
    main_btn = await get_common_menu_button('main_menu', 'ru')
    if main_btn:
        rows.append([main_btn])
    nav_kb = InlineKeyboardMarkup(inline_keyboard=rows)
    menu_text = (await get_text('return_to_menu', 'ru')) or "Return"
    msg2 = await cb.message.answer(menu_text, reply_markup=nav_kb)
    last_bot_messages.setdefault(chat_id, []).append(msg2.message_id)
    await register_bot_messages(chat_id, [msg2.message_id])

    await cb.answer()





@router.callback_query(F.data.startswith("sell_delete_no:"))
async def sell_delete_no(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    listing_id = int(cb.data.split(":")[1])

    # Удаляем только окно подтверждения (без «спасибо» и прочего)
    try:
        await cb.message.delete()
    except Exception:
        pass

    # Ничего не отправляем, просто закрываем спиннер
    await cb.answer()

    print(
        f"FUNC: sell_delete_no | CANCELLED | listing_id={listing_id} | "
        f"chat_id={chat_id} | user_id={cb.from_user.id}"
    )


@router.callback_query(F.data == "sell_back")
async def sell_back_handler(cb: CallbackQuery, state: FSMContext):
    cur_state = await state.get_state()
    chat_id = cb.message.chat.id
    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    if cur_state == Sell.city.state:
        market_text = (await get_text('market_choose_action', 'ru')) or "💸 Marketplace:\nChoose an action."
        msg = await cb.message.answer(market_text, reply_markup=await market_inline())
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])
        await state.clear()
        await cb.answer()
        return

    if cur_state == Sell.cat.state:
        await state.set_state(Sell.city)
        async with SessionLocal() as s:
            cities = (await s.execute(select(City))).scalars().all()
        kb = await cities_inline(cities)
        header = await get_text('sell_choose_city', 'ru') or "Create a listing.\nFirst, choose a city:"
        msg = await cb.message.answer(header, reply_markup=kb)
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])

    elif cur_state == Sell.title.state:
        # назад к выбору категории
        await state.set_state(Sell.cat)
        data = await state.get_data()
        city_slug = data.get("city_slug")
        async with SessionLocal() as s:
            root = (await s.execute(select(Category).where(Category.slug == "equip"))).scalar_one()
            subs = (await s.execute(select(Category).where(Category.parent_id == root.id).order_by(sql_text("order_num"), Category.name))).scalars().all()
        fmt = []
        for sc in subs:
            title = await format_category_title(sc.id, (sc.name or "").strip(), SessionLocal)
            fmt.append(type("Proxy", (), {"id": sc.id, "name": title, "slug": sc.slug}))
        kb = await equip_inline(fmt, city_slug)
        template = await get_text('sell_choose_category_back', 'ru') or "City: <b>{city_name}</b>\nChoose a category:"
        text = template.format(city_name=data.get('city_name'))
        try:
            await safe_edit_or_send(cb, text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            try: await cb.message.delete()
            except Exception: pass
            msg = await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
            last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
            await register_bot_messages(chat_id, [msg.message_id])

    elif cur_state == Sell.descr.state:
        # было Title → Descr → Price → Photo, значит назад из Descr → Title
        await state.set_state(Sell.title)
        template = await get_text('sell_ask_title', 'ru') or "Enter <b>listing title</b> (one line):"
        await send_with_nav(cb.message, template, parse_mode="HTML")

    elif cur_state == Sell.price.state:
        # назад из Price → Descr
        await state.set_state(Sell.descr)
        await _render_descr_step(cb.message, state)

    elif cur_state == Sell.photo.state:
        # назад из Photo → Price
        await state.set_state(Sell.price)
        await _render_price_step(cb.message, state)

    elif cur_state == Sell.confirm.state:
        # назад из предпросмотра: показываем реальное число уже загруженных фото
        await state.set_state(Sell.photo)
        _photos = (await state.get_data()).get("photos", []) or []
        await send_photo_prompt(cb.message, len(_photos), state)

    elif cur_state == Sell.flex.state:
        await state.set_state(Sell.photo)
        _photos = (await state.get_data()).get("photos", []) or []
        await send_photo_prompt(cb.message, len(_photos), state)

    else:
        await state.set_state(Sell.city)
        async with SessionLocal() as s:
            cities = (await s.execute(select(City))).scalars().all()
        kb = await cities_inline(cities)
        template = await get_text('sell_create_start', 'ru') or "💸 Create a listing.\nFirst, choose a city:"
        msg = await cb.message.answer(template, reply_markup=kb)
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])

    await cb.answer()


@router.callback_query(F.data == "sell_city_back")
async def sell_city_back(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)
    await state.clear()

    async with SessionLocal() as s:
        cities = (await s.execute(select(City))).scalars().all()

    kb = await cities_inline(cities)
    header = await get_text('sell_choose_city', 'ru') or "Create a listing.\nFirst, choose a city:"
    await safe_edit_or_send(cb, header, reply_markup=kb)
    await cb.answer()
    print(f"FUNC: sell_city_back | chat_id: {chat_id} | user_id: {cb.from_user.id}")
