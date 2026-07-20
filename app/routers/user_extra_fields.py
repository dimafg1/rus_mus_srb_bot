# app/routers/user_extra_fields.py
# ====== Пользователь: Доп. поля категории — опрос и сбор значений ======

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from sqlalchemy import select
import json
import inspect
from html import escape as html_escape

from app.database import SessionLocal
from app.models import Category, Listing
from app.routers.utils import clear_bot_messages, last_bot_messages, register_bot_messages, get_text
from app.keyboards import get_common_menu_button
from aiogram.filters import BaseFilter
from aiogram import types

# NEW: для материализации YouTube → file_id
import os
import asyncio
import tempfile
from aiogram.types import FSInputFile

router = Router()

# ====== Состояния FSM (только для text/number) ======
class UserExtraFieldStates(StatesGroup):
    waiting_value = State()
    waiting_video = State()
    waiting_choice = State()


# ====== Ключи для хранения в FSM (важно: VAL_KEY импортируется в market_add.py) ======
DEF_KEY    = "extra_defs"        # list[dict] — описание полей из Category.fields
IDX_KEY    = "extra_idx"         # текущий индекс (0..n-1)
VAL_KEY    = "extra_values"      # dict key->value — ответы пользователя
RESUME_KEY = "extra_resume"      # callback_data, куда вернуться по завершении
LISTING_ID_KEY = "listing_id"    # listing_id, чтобы подтягивать/сохранять значения
OWNER_ID_KEY = "extra_owner_id"
LISTING_TYPE_KEY = "extra_listing_type"

# ──────────────────────────────────────────────────────────────────────────────
# ВНУТРЕННИЕ УТИЛИТЫ
# ──────────────────────────────────────────────────────────────────────────────

def _ctx(ev):
    """Единый доступ к chat_id, bot и функции send()."""
    if isinstance(ev, CallbackQuery):
        return ev.message.chat.id, ev.message.bot, ev.message.answer
    else:
        return ev.chat.id, ev.bot, ev.answer

async def _load_category_fields(session, cat_id: int) -> list[dict]:
    """Читаем JSON-массив дефиниций доп. полей из Category.fields."""
    cat = (await session.execute(select(Category).where(Category.id == cat_id))).scalar_one_or_none()
    if cat is None:
        return []
    try:
        raw = (cat.fields or "").strip()
        data = json.loads(raw) if raw else []
        return data if isinstance(data, list) else []
    except Exception:
        return []

async def _get_listing(session, listing_id: int) -> Listing | None:
    return await session.get(Listing, listing_id)

async def _fmt_value_for_display(val):
    """Человекочитаемый вывод значения."""
    if val is None:
        return "—"
    if isinstance(val, bool):
        bool_yes = await get_text("admin_fields_yes", "ru") or "Да"
        bool_no = await get_text("admin_fields_no", "ru") or "Нет"
        return bool_yes if val else bool_no
    if isinstance(val, list):
        return html_escape(", ".join(map(str, val)))
    # Видео (file_id/ссылка) не «озвучиваем» — плеер покажем отдельно
    if isinstance(val, str):
        sval = val.strip()
        low = sval.lower()
        if "youtube.com" in low or "youtu.be" in low:
            return "—"
        if len(sval) > 20 and " " not in sval:
            return "—"
        return html_escape(sval)
    return html_escape(str(val))


def _flex_dict(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


async def _current_value_from_listing(state: FSMContext, key: str):
    """Достаём исходное значение поля из listing.flex (если есть listing_id в стейте)."""
    data = await state.get_data()
    listing_id = data.get(LISTING_ID_KEY)
    owner_id = data.get(OWNER_ID_KEY)
    listing_type = data.get(LISTING_TYPE_KEY)
    if not listing_id or owner_id is None or listing_type not in ("market", "service"):
        return None
    try:
        async with SessionLocal() as s:
            l = (await s.execute(select(Listing).where(
                Listing.id == int(listing_id),
                Listing.owner_id == owner_id,
                Listing.type == listing_type,
            ))).scalar_one_or_none()
            if not l or not l.flex:
                return None
            flex = json.loads(l.flex)
            if not isinstance(flex, dict):
                return None
            return flex.get(key)
    except Exception:
        return None

async def _controls_row():
    """Нижняя панель управления: Пропустить / Завершить / Назад.
    ВНИМАНИЕ: эти callback-и обрабатывает market_edit.py (edit:skip / edit:finish / edit:back)
    """
    back_btn = await get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data="edit:back")
    back_btn.callback_data = "edit:back"
    return [
        [InlineKeyboardButton(text=await get_text("releases_btn_skip", "ru") or "⏭ Пропустить", callback_data="edit:skip")],
        [InlineKeyboardButton(text=await get_text("extra_field_btn_finish", "ru") or "✅ Завершить", callback_data="edit:finish")],
        [back_btn],
    ]
async def start_extra_fields_for_category(ev, state: FSMContext, cat_id: int, resume_data: str | None):
    """
    ev — CallbackQuery или Message
    cat_id — категория, для которой нужно спросить доп. поля
    resume_data — callback_data для возврата «Назад к объявлению» по завершении
    Требует, чтобы в state уже лежал LISTING_ID_KEY (listing_id), чтобы:
      1) показывать ТЕКУЩЕЕ значение по каждому ключу
      2) сохранить новые значения в l.flex по завершении
    """
    chat_id, bot, send = _ctx(ev)
    await clear_bot_messages(chat_id, bot)

    state_data = await state.get_data()
    listing_id = state_data.get(LISTING_ID_KEY)
    existing_values: dict = {}
    listing_type: str | None = None
    owner_id = getattr(getattr(ev, "from_user", None), "id", None)

    async with SessionLocal() as s:
        if listing_id:
            listing = (await s.execute(select(Listing).where(
                Listing.id == int(listing_id),
                Listing.owner_id == owner_id,
                Listing.type.in_(("market", "service")),
            ))).scalar_one_or_none()
            if listing is None:
                await state.clear()
                msg = await send(await get_text("extra_field_err_not_owner", "ru") or "Можно редактировать только своё объявление.")
                last_bot_messages[chat_id] = [msg.message_id]
                await register_bot_messages(chat_id, [msg.message_id])
                return
            existing_values = _flex_dict(listing.flex)
            listing_type = listing.type
            # Категория из callback_data не является доверенным источником.
            cat_id = listing.category_id
        defs = await _load_category_fields(s, cat_id)

    # Если полей нет — просто вернёмся к объявлению
    if not defs:
        await state.clear()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=await get_text("market_edit_btn_back_to_listing", "ru") or "⬅️ Назад к объявлению", callback_data=resume_data or "noop")]
        ])
        msg = await send(await get_text("extra_field_no_fields", "ru") or "Для этой категории нет дополнительных полей.", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        print(f"[user_extra_fields] no_fields chat={chat_id}, cat_id={cat_id}")
        return

    # Инициализация мастера
    await state.update_data({
        DEF_KEY: defs,
        IDX_KEY: 0,
        VAL_KEY: existing_values,
        RESUME_KEY: resume_data,
        OWNER_ID_KEY: owner_id,
        LISTING_TYPE_KEY: listing_type,
        # LISTING_ID_KEY — должен быть записан извне (market_edit) заранее
    })
    print(f"[user_extra_fields] start chat={chat_id}, fields={len(defs)}, resume={resume_data}")

    await _ask_current_field(ev, state)

# ──────────────────────────────────────────────────────────────────────────────
# ОСНОВНОЙ ШАГ СПРОСА ПОЛЯ
# ──────────────────────────────────────────────────────────────────────────────

async def _ask_current_field(ev, state: FSMContext):
    chat_id, bot, send = _ctx(ev)
    await clear_bot_messages(chat_id, bot)

    data = await state.get_data()
    defs = data.get(DEF_KEY, []) or []
    idx  = int(data.get(IDX_KEY, 0))
    resume = data.get(RESUME_KEY)

    # Завершение мастера (сохранение flex + кнопка «Назад к объявлению»)
    if not defs or idx >= len(defs):
        vals = data.get(VAL_KEY, {}) or {}
        listing_id = data.get(LISTING_ID_KEY)
        owner_id = data.get(OWNER_ID_KEY)
        listing_type = data.get(LISTING_TYPE_KEY)

        # сохраняем в Listing.flex
        if listing_id:
            try:
                json_str = json.dumps(vals, ensure_ascii=False) if vals else None
                async with SessionLocal() as s:
                    l = (await s.execute(select(Listing).where(
                        Listing.id == int(listing_id),
                        Listing.owner_id == owner_id,
                        Listing.type == listing_type,
                        Listing.type.in_(("market", "service")),
                    ))).scalar_one_or_none()
                    if l:
                        l.flex = json_str
                        await s.commit()
                        print(f"[user_extra_fields] saved flex for listing_id={listing_id}: {json_str}")
                    else:
                        await state.clear()
                        msg = await send(await get_text("extra_field_session_stale", "ru") or "Сеанс редактирования устарел. Откройте объявление ещё раз.")
                        last_bot_messages[chat_id] = [msg.message_id]
                        await register_bot_messages(chat_id, [msg.message_id])
                        return
            except Exception as e:
                print(f"[user_extra_fields] ERROR saving flex listing_id={listing_id}: {e}")
                msg = await send(await get_text("extra_field_save_failed", "ru") or "Не удалось сохранить дополнительные поля. Попробуйте ещё раз.")
                last_bot_messages[chat_id] = [msg.message_id]
                await register_bot_messages(chat_id, [msg.message_id])
                return

        # итоговый экран
        lines = [await get_text("extra_field_saved_header", "ru") or "<b>Дополнительные поля сохранены.</b>"]
        for f in defs:
            key   = (str(f.get("key","")).strip().lower() or "field")
            label = f.get("label") or f.get("name") or key
            val   = vals.get(key, None)
            lines.append(f"<b>{html_escape(str(label))}:</b> {await _fmt_value_for_display(val)}")

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=await get_text("market_edit_btn_back_to_listing", "ru") or "⬅️ Назад к объявлению", callback_data=resume or "noop")]
        ])
        msg = await send("\n\n".join(lines), reply_markup=kb, parse_mode="HTML")
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await state.clear()
        print(f"[user_extra_fields] finish chat={chat_id}, msgs={[msg.message_id]}")
        return

    # Готовим поле
    f = defs[idx] if isinstance(defs[idx], dict) else {}
    ftype    = str(f.get("type", "text"))
    label    = f.get("label") or f.get("name") or (await get_text("vac_add_flex_default_label", "ru") or "Поле")
    key      = (str(f.get("key","")).strip().lower() or "field")
    required = bool(f.get("required", False))
    options  = f.get("options") if isinstance(f.get("options"), list) else []

    # Заголовок без индикаторов (1/3) — по вашему требованию
    title = f"<b>{html_escape(str(label))}</b>" + (" *" if required else "")

    # Текущее значение — вытянем из listing.flex (если есть)
    cur_val = await _current_value_from_listing(state, key)
    cur_val_display = await _fmt_value_for_display(cur_val)
    cur_line_tmpl = await get_text("extra_field_current_value_tmpl", "ru") or "Текущее значение: <code>{value}</code>"
    cur_line = cur_line_tmpl.format(value=cur_val_display)

    controls = await _controls_row()

    # text / number
    if ftype in ("text", "number"):
        kb = InlineKeyboardMarkup(inline_keyboard=controls)
        suffix = (await get_text("extra_field_ask_value_suffix_number", "ru") or " (число)") if ftype == "number" else ""
        ask_tmpl = await get_text("extra_field_ask_value_tmpl", "ru") or "{title}\n\n{cur_line}\n\nВведите значение{suffix}:"
        msg = await send(
            ask_tmpl.format(title=title, cur_line=cur_line, suffix=suffix),
            reply_markup=kb, parse_mode="HTML"
        )
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await state.set_state(UserExtraFieldStates.waiting_value)
        print(f"[user_extra_fields] ask {ftype} chat={chat_id}, idx={idx}, key={key}, required={required}")
        return

    # checkbox
    if ftype == "checkbox":
        rows = [[
            InlineKeyboardButton(text=await get_text("vac_add_checkbox_yes", "ru") or "✅ Да", callback_data="extra:checkbox:1"),
            InlineKeyboardButton(text=await get_text("admin_panel_btn_no", "ru") or "❌ Нет", callback_data="extra:checkbox:0"),
        ]]
        rows += controls
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        checkbox_tmpl = await get_text("extra_field_checkbox_prompt", "ru") or "{title}\n\n{cur_line}\n\nВыберите вариант:"
        msg = await send(checkbox_tmpl.format(title=title, cur_line=cur_line), reply_markup=kb, parse_mode="HTML")
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await state.set_state(UserExtraFieldStates.waiting_choice)
        print(f"[user_extra_fields] ask checkbox chat={chat_id}, idx={idx}, key={key}")
        return

    # select
    if ftype == "select":
        buttons = [InlineKeyboardButton(text=str(opt), callback_data=f"extra:select:{i}") for i, opt in enumerate(options)]
        row_len = 3 if len(buttons) > 6 else 2
        rows = [buttons[i:i+row_len] for i in range(0, len(buttons), row_len)] if buttons else []
        rows += controls
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        select_tmpl = await get_text("extra_field_select_prompt", "ru") or "{title}\n\n{cur_line}\n\nВыберите один из вариантов:"
        msg = await send(select_tmpl.format(title=title, cur_line=cur_line), reply_markup=kb, parse_mode="HTML")
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await state.set_state(UserExtraFieldStates.waiting_choice)
        print(f"[user_extra_fields] ask select chat={chat_id}, idx={idx}, key={key}, options={len(options)}")
        return

    # video
    if ftype == "video":
        rows = await _controls_row()
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        # Показываем текущее видео (если это загруженный file_id, а не ссылка),
        # чтобы пользователь видел, что уже есть, пока не заменил.
        preview_ids: list[int] = []
        if cur_val:
            sval = str(cur_val).strip()
            if sval and not sval.lower().startswith("http"):
                try:
                    pv = await bot.send_video(
                        chat_id, sval,
                        caption=await get_text("extra_field_video_current_caption", "ru") or "🎬 Текущее видео (останется, если ничего не пришлёте):")
                    preview_ids.append(pv.message_id)
                except Exception as e:
                    print(f"[user_extra_fields] cannot preview current video chat={chat_id}: {e}")
        video_tmpl = (
            await get_text("extra_field_video_instructions_tmpl", "ru")
            or "{title}\n\n{cur_line}\n\nОтправьте <b>видео одним сообщением</b>:\n• как «Видео» (желательно), или\n• как «Файл» с видео-типом, или\n• <b>ссылку на YouTube</b> — мы подготовим и встроим в карточку.\n\n<i>Будет сохранён</i> <code>file_id</code> <i>для встраивания в карточку.</i>"
        )
        msg = await send(
            video_tmpl.format(title=title, cur_line=cur_line),
            reply_markup=kb, parse_mode="HTML"
        )
        ids = preview_ids + [msg.message_id]
        last_bot_messages[chat_id] = ids
        await register_bot_messages(chat_id, ids)
        await state.set_state(UserExtraFieldStates.waiting_video)
        print(f"[user_extra_fields] ask video chat={chat_id}, idx={idx}, key={key}, required={required} preview={len(preview_ids)}")
        return

    # неизвестный тип — просто перескочим дальше
    await _advance_index_only(state)
    print(f"[user_extra_fields] unknown type '{ftype}' → skip idx={idx}")
    await _ask_current_field(ev, state)

# ──────────────────────────────────────────────────────────────────────────────
# ПЕРЕХОДЫ
# ──────────────────────────────────────────────────────────────────────────────

async def _advance_index_only(state: FSMContext):
    data = await state.get_data()
    idx  = int(data.get(IDX_KEY, 0))
    await state.update_data({IDX_KEY: idx + 1})

async def _advance_with_value(ev, state: FSMContext, value):
    """Сохраняем значение текущего поля в VAL_KEY и двигаем индекс."""
    chat_id, _, _ = _ctx(ev)
    data = await state.get_data()
    defs = data.get(DEF_KEY, []) or []
    idx  = int(data.get(IDX_KEY, 0))
    vals = data.get(VAL_KEY, {}) or {}

    if defs and 0 <= idx < len(defs):
        key = (str(defs[idx].get("key","")).strip().lower() or f"field_{idx}")
        if value is not None:
            vals[key] = value
        await state.update_data({VAL_KEY: vals, IDX_KEY: idx + 1})
        print(f"[user_extra_fields] save chat={chat_id}, key={key}, value={value}")
    else:
        print(f"[user_extra_fields] WARN: idx out of bounds idx={idx}, defs={len(defs)}")

    await _ask_current_field(ev, state)

# ──────────────────────────────────────────────────────────────────────────────
# ПУБЛИЧНЫЕ ХЕЛПЕРЫ ДЛЯ market_edit (делегаты нажатий)
# ──────────────────────────────────────────────────────────────────────────────

async def extra_next(ev, state: FSMContext):
    """Эквивалент «Пропустить» для текущего доп. поля."""
    chat_id, _, _ = _ctx(ev)
    await _advance_index_only(state)
    print(f"[user_extra_fields] extra_next chat={chat_id}")
    await _ask_current_field(ev, state)

async def extra_finish(ev, state: FSMContext):
    """Эквивалент «Завершить»: прыгаем в конец и отдаём итоговое сообщение."""
    chat_id, _, _ = _ctx(ev)
    data = await state.get_data()
    defs = data.get(DEF_KEY, []) or []
    await state.update_data({IDX_KEY: len(defs)})
    print(f"[user_extra_fields] extra_finish chat={chat_id}")
    await _ask_current_field(ev, state)

async def extra_back(ev, state: FSMContext):
    """Шаг назад внутри доп. полей. Если мы на первом, market_edit вернёт к последнему
    «основному» шагу, но сам локальный шаг назад мы делаем здесь, если возможно."""
    chat_id, _, _ = _ctx(ev)
    data = await state.get_data()
    idx  = int(data.get(IDX_KEY, 0))
    if idx <= 0:
        # тут бездействуем — market_edit решит, куда вернуть (в описание)
        print(f"[user_extra_fields] extra_back chat={chat_id}, idx={idx} (first step)")
        await _ask_current_field(ev, state)  # просто перерисуем текущий
        return
    await state.update_data({IDX_KEY: idx - 1})
    print(f"[user_extra_fields] extra_back chat={chat_id}, {idx}→{idx-1}")
    await _ask_current_field(ev, state)

# ──────────────────────────────────────────────────────────────────────────────
# ХЕНДЛЕРЫ ВВОДА ЗНАЧЕНИЙ
# ──────────────────────────────────────────────────────────────────────────────

@router.message(UserExtraFieldStates.waiting_value)
async def user_extra_value_message(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await clear_bot_messages(chat_id, message.bot)

    data = await state.get_data()
    defs = data.get(DEF_KEY, []) or []
    idx  = int(data.get(IDX_KEY, 0))

    ftype = "text"
    if defs and 0 <= idx < len(defs):
        ftype = str((defs[idx] or {}).get("type", "text"))

    txt = (message.text or "").strip()

    if ftype == "number":
        raw = txt.replace(",", ".")
        try:
            num = float(raw)
            if num.is_integer():
                num = int(num)
        except Exception:
            kb = InlineKeyboardMarkup(inline_keyboard=await _controls_row())
            msg = await message.answer(await get_text("extra_field_need_number", "ru") or "Нужно число. Попробуйте ещё раз.", reply_markup=kb)
            last_bot_messages[chat_id] = [msg.message_id]
            await register_bot_messages(chat_id, [msg.message_id])
            print(f"[user_extra_fields] bad number chat={chat_id}, text={txt}")
            return
        await _advance_with_value(message, state, num)
        return

    await _advance_with_value(message, state, txt)

# 4.1 Поймать «настоящее» видео
@router.message(UserExtraFieldStates.waiting_video, F.video)
async def user_extra_video_by_video(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await clear_bot_messages(chat_id, message.bot)
    file_id = message.video.file_id
    print(f"[user_extra_fields] got video (video) chat={chat_id}, file_id={file_id}")
    await _advance_with_value(message, state, file_id)

# 4.2 Поймать «файл» с видео mime-type
@router.message(UserExtraFieldStates.waiting_video, F.document)
async def user_extra_video_by_document(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await clear_bot_messages(chat_id, message.bot)
    doc = message.document
    if doc and doc.mime_type and doc.mime_type.startswith("video/"):
        file_id = doc.file_id
        print(f"[user_extra_fields] got video (document) chat={chat_id}, file_id={file_id}, mime={doc.mime_type}")
        await _advance_with_value(message, state, file_id)
        return

    # Если это не видео-файл — попросим снова
    kb = InlineKeyboardMarkup(inline_keyboard=await _controls_row())
    msg = await message.answer(await get_text("extra_field_not_video_file", "ru") or "Это не видео-файл. Отправьте видео (как видео или как файл с типом video/*).", reply_markup=kb)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    print(f"[user_extra_fields] bad document (not video) chat={chat_id}, mime={getattr(doc, 'mime_type', None)}")

# 4.3 Любой другой контент — мягкая ошибка
# 4.4 Поймать текстовую ссылку на видео (например, YouTube) → МАТЕРИАЛИЗАЦИЯ
@router.message(UserExtraFieldStates.waiting_video, F.text)
async def user_extra_video_by_text(message: Message, state: FSMContext):
    """
    YouTube: сохраняем ссылку как значение поля БЕЗ скачивания.
    Дальше мастер полей идёт на следующий шаг.
    """
    chat_id = message.chat.id
    await clear_bot_messages(chat_id, message.bot)

    txt = (message.text or "").strip()
    low = txt.lower()

    if txt.startswith("http") and ("youtube.com" in low or "youtu.be" in low):
        print(f"[user_extra_fields.py] user_extra_video_by_text | save url | chat_id={chat_id} | url={txt}")
        # важное: сохраняем URL и двигаемся дальше
        await _advance_with_value(message, state, txt)
        return

    # не ссылка на видео
    kb = InlineKeyboardMarkup(inline_keyboard=await _controls_row())
    msg = await message.answer(
        await get_text("extra_field_not_video_link", "ru") or "Это не ссылка на видео. Отправьте видео-файл или ссылку на YouTube.",
        reply_markup=kb
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    print(f"[user_extra_fields.py] user_extra_video_by_text | bad text | chat_id={chat_id} | text={txt}")


@router.message(UserExtraFieldStates.waiting_video)
async def user_extra_video_wrong_content(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await clear_bot_messages(chat_id, message.bot)
    kb = InlineKeyboardMarkup(inline_keyboard=await _controls_row())
    msg = await message.answer(await get_text("extra_field_need_video", "ru") or "Нужно отправить видео. Попробуйте ещё раз.", reply_markup=kb)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    print(f"[user_extra_fields] wrong content while waiting_video chat={chat_id}, content_type={message.content_type}")




@router.callback_query(UserExtraFieldStates.waiting_choice, F.data.regexp(r"^extra:checkbox:(0|1)$"))
async def user_extra_checkbox(cb: CallbackQuery, state: FSMContext):
    val = cb.data.endswith(":1")
    print(f"[user_extra_fields] checkbox chat={cb.message.chat.id}, val={val}")
    await _advance_with_value(cb, state, val)

@router.callback_query(UserExtraFieldStates.waiting_choice, F.data.regexp(r"^extra:select:(\d+)$"))
async def user_extra_select(cb: CallbackQuery, state: FSMContext):
    try:
        opt_idx = int(cb.data.split(":")[-1])
    except Exception:
        opt_idx = -1

    data = await state.get_data()
    defs = data.get(DEF_KEY, []) or []
    idx  = int(data.get(IDX_KEY, 0))
    options = (defs[idx] or {}).get("options", []) if defs and 0 <= idx < len(defs) else []

    value = options[opt_idx] if 0 <= opt_idx < len(options) else None
    print(f"[user_extra_fields] select chat={cb.message.chat.id}, opt_idx={opt_idx}, value={value}")
    await _advance_with_value(cb, state, value)
