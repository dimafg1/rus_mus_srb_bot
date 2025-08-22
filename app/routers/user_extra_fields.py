# app/routers/user_extra_fields.py
# ====== Пользователь: Доп. поля категории — опрос и сбор значений ======

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from sqlalchemy import select
import json
import inspect

from app.database import SessionLocal
from app.models import Category, Listing
from app.routers.utils import clear_bot_messages, last_bot_messages

router = Router()

# ====== Состояния FSM (только для text/number) ======
class UserExtraFieldStates(StatesGroup):
    waiting_value = State()

# ====== Ключи для хранения в FSM (важно: VAL_KEY импортируется в market_add.py) ======
DEF_KEY    = "extra_defs"        # list[dict] — описание полей из Category.fields
IDX_KEY    = "extra_idx"         # текущий индекс (0..n-1)
VAL_KEY    = "extra_values"      # dict key->value — ответы пользователя
RESUME_KEY = "extra_resume"      # callback_data, куда вернуться по завершении
LISTING_ID_KEY = "listing_id"    # listing_id, чтобы подтягивать/сохранять значения

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
    cat = (await session.execute(select(Category).where(Category.id == cat_id))).scalar_one()
    try:
        raw = (cat.fields or "").strip()
        data = json.loads(raw) if raw else []
        return data if isinstance(data, list) else []
    except Exception:
        return []

async def _get_listing(session, listing_id: int) -> Listing | None:
    return await session.get(Listing, listing_id)

def _fmt_value_for_display(val):
    """Человекочитаемый вывод значения."""
    if val is None:
        return "—"
    if isinstance(val, bool):
        return "Да" if val else "Нет"
    if isinstance(val, list):
        return ", ".join(map(str, val))
    return str(val)

async def _current_value_from_listing(state: FSMContext, key: str):
    """Достаём исходное значение поля из listing.flex (если есть listing_id в стейте)."""
    data = await state.get_data()
    listing_id = data.get(LISTING_ID_KEY)
    if not listing_id:
        return None
    try:
        async with SessionLocal() as s:
            l = await _get_listing(s, int(listing_id))
            if not l or not l.flex:
                return None
            flex = json.loads(l.flex)
            if not isinstance(flex, dict):
                return None
            return flex.get(key)
    except Exception:
        return None

def _controls_row():
    """Нижняя панель управления: Пропустить / Завершить / Назад.
    ВНИМАНИЕ: эти callback-и обрабатывает market_edit.py (edit:skip / edit:finish / edit:back)
    """
    return [
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data="edit:skip")],
        [InlineKeyboardButton(text="✅ Завершить", callback_data="edit:finish")],
        [InlineKeyboardButton(text="⬅️ Назад",      callback_data="edit:back")],
    ]

# ──────────────────────────────────────────────────────────────────────────────
# ПУБЛИЧНЫЙ ВХОД (запускает мастер доп. полей)
# ──────────────────────────────────────────────────────────────────────────────

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

    async with SessionLocal() as s:
        defs = await _load_category_fields(s, cat_id)

    # Если полей нет — просто вернёмся к объявлению
    if not defs:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад к объявлению", callback_data=resume_data or "noop")]
        ])
        msg = await send("Для этой категории нет дополнительных полей.", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]
        print(f"[user_extra_fields] no_fields chat={chat_id}, cat_id={cat_id}")
        return

    # Инициализация мастера
    await state.update_data({
        DEF_KEY: defs,
        IDX_KEY: 0,
        VAL_KEY: {},
        RESUME_KEY: resume_data
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

        # сохраняем в Listing.flex
        if listing_id:
            try:
                json_str = json.dumps(vals, ensure_ascii=False) if vals else None
                async with SessionLocal() as s:
                    l = await _get_listing(s, int(listing_id))
                    if l:
                        l.flex = json_str
                        await s.commit()
                        print(f"[user_extra_fields] saved flex for listing_id={listing_id}: {json_str}")
            except Exception as e:
                print(f"[user_extra_fields] ERROR saving flex listing_id={listing_id}: {e}")

        # итоговый экран
        lines = ["<b>Дополнительные поля сохранены.</b>"]
        for f in defs:
            key   = (str(f.get("key","")).strip().lower() or "field")
            label = f.get("label") or f.get("name") or key
            val   = vals.get(key, None)
            lines.append(f"<b>{label}:</b> {_fmt_value_for_display(val)}")

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад к объявлению", callback_data=resume or "noop")]
        ])
        msg = await send("\n\n".join(lines), reply_markup=kb, parse_mode="HTML")
        last_bot_messages[chat_id] = [msg.message_id]
        print(f"[user_extra_fields] finish chat={chat_id}, msgs={[msg.message_id]}")
        return

    # Готовим поле
    f = defs[idx] if isinstance(defs[idx], dict) else {}
    ftype    = str(f.get("type", "text"))
    label    = f.get("label") or f.get("name") or "Поле"
    key      = (str(f.get("key","")).strip().lower() or "field")
    required = bool(f.get("required", False))
    options  = f.get("options") if isinstance(f.get("options"), list) else []

    # Заголовок без индикаторов (1/3) — по вашему требованию
    title = f"<b>{label}</b>" + (" *" if required else "")

    # Текущее значение — вытянем из listing.flex (если есть)
    cur_val = await _current_value_from_listing(state, key)
    cur_line = f"Текущее значение: <b>{_fmt_value_for_display(cur_val)}</b>"

    controls = _controls_row()

    # text / number
    if ftype in ("text", "number"):
        kb = InlineKeyboardMarkup(inline_keyboard=controls)
        msg = await send(
            f"{title}\n\n{cur_line}\n\nВведите значение" + (" (число)" if ftype == "number" else "") + ":",
            reply_markup=kb, parse_mode="HTML"
        )
        last_bot_messages[chat_id] = [msg.message_id]
        await state.set_state(UserExtraFieldStates.waiting_value)
        print(f"[user_extra_fields] ask {ftype} chat={chat_id}, idx={idx}, key={key}, required={required}")
        return

    # checkbox
    if ftype == "checkbox":
        rows = [[
            InlineKeyboardButton(text="✅ Да",  callback_data="extra:checkbox:1"),
            InlineKeyboardButton(text="❌ Нет", callback_data="extra:checkbox:0"),
        ]]
        rows += controls
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        msg = await send(f"{title}\n\n{cur_line}\n\nВыберите вариант:", reply_markup=kb, parse_mode="HTML")
        last_bot_messages[chat_id] = [msg.message_id]
        print(f"[user_extra_fields] ask checkbox chat={chat_id}, idx={idx}, key={key}")
        return

    # select
    if ftype == "select":
        buttons = [InlineKeyboardButton(text=str(opt), callback_data=f"extra:select:{i}") for i, opt in enumerate(options)]
        row_len = 3 if len(buttons) > 6 else 2
        rows = [buttons[i:i+row_len] for i in range(0, len(buttons), row_len)] if buttons else []
        rows += controls
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        msg = await send(f"{title}\n\n{cur_line}\n\nВыберите один из вариантов:", reply_markup=kb, parse_mode="HTML")
        last_bot_messages[chat_id] = [msg.message_id]
        print(f"[user_extra_fields] ask select chat={chat_id}, idx={idx}, key={key}, options={len(options)}")
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
            kb = InlineKeyboardMarkup(inline_keyboard=_controls_row())
            msg = await message.answer("Нужно число. Попробуйте ещё раз.", reply_markup=kb)
            last_bot_messages[chat_id] = [msg.message_id]
            print(f"[user_extra_fields] bad number chat={chat_id}, text={txt}")
            return
        await _advance_with_value(message, state, num)
        return

    await _advance_with_value(message, state, txt)

@router.callback_query(F.data.regexp(r"^extra:checkbox:(0|1)$"))
async def user_extra_checkbox(cb: CallbackQuery, state: FSMContext):
    val = cb.data.endswith(":1")
    print(f"[user_extra_fields] checkbox chat={cb.message.chat.id}, val={val}")
    await _advance_with_value(cb, state, val)

@router.callback_query(F.data.regexp(r"^extra:select:(\d+)$"))
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
