# app/routers/market_edit_overview.py

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from sqlalchemy import select
import json
import inspect

from app.database import SessionLocal
from app.models import Listing, City, Category
from app.routers.utils import clear_bot_messages, last_bot_messages

router = Router()


# ─────────────────────────────────────────────────────────
# FSM для редактирования ОДНОГО поля (универсально)
# ─────────────────────────────────────────────────────────
class OneFieldStates(StatesGroup):
    waiting_value = State()  # для text/number; checkbox/select идут коллбэками


# ─────────────────────────────────────────────────────────
# Внутренние утилиты
# ─────────────────────────────────────────────────────────
async def _get_listing(s, listing_id: int) -> Listing:
    return (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one()

async def _get_city_cat(s, listing: Listing):
    city = (await s.execute(select(City).where(City.id == listing.city_id))).scalar_one()
    cat  = (await s.execute(select(Category).where(Category.id == listing.category_id))).scalar_one()
    return city, cat

async def _load_category_fields(s, cat_id: int) -> list[dict]:
    cat = (await s.execute(select(Category).where(Category.id == cat_id))).scalar_one()
    try:
        raw = (cat.fields or "").strip()
        data = json.loads(raw) if raw else []
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _fmt(val):
    if val is None:
        return "—"
    if isinstance(val, bool):
        return "Да" if val else "Нет"
    if isinstance(val, list):
        return ", ".join(map(str, val))
    return str(val)

def _controls_cancel(listing_id: int):
    # Только «Отменить» (возврат к списку полей)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❎ Отменить", callback_data=f"ef:cancel:{listing_id}")],
    ])

def _ctx(ev):
    if isinstance(ev, CallbackQuery):
        return ev.message.chat.id, ev.message.bot, ev.message.answer
    else:
        return ev.chat.id, ev.bot, ev.answer


# ─────────────────────────────────────────────────────────
# Рендер единого обзора всех полей
# ─────────────────────────────────────────────────────────
async def _render_overview(chat_id: int, bot, send, listing_id: int):
    await clear_bot_messages(chat_id, bot)

    async with SessionLocal() as s:
        listing = await _get_listing(s, listing_id)
        city, cat = await _get_city_cat(s, listing)
        defs = await _load_category_fields(s, listing.category_id)

    # flex значения
    try:
        flex_vals = json.loads(listing.flex) if listing.flex else {}
        if not isinstance(flex_vals, dict):
            flex_vals = {}
    except Exception:
        flex_vals = {}

    # единый ровный список — БЕЗ заголовков «Основные/Дополнительные»
    lines = [
        "🛠 <b>Редактирование объявления</b>",
        f"Город: <b>{city.name}</b>",
        f"Категория: <b>{cat.name}</b>",
        "",
        f"<b>Заголовок:</b> <i>{_fmt(listing.title)}</i>",
        "",
        f"<b>Цена:</b> <i>{_fmt(listing.price)}</i>",
        "",
        f"<b>Описание:</b> <i>{_fmt(listing.descr)}</i>",
    ]

    # кнопки «Править …» под каждым пунктом
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="✏️ Править заголовок", callback_data=f"ef:main:title:{listing_id}")],
        [InlineKeyboardButton(text="✏️ Править цену",      callback_data=f"ef:main:price:{listing_id}")],
        [InlineKeyboardButton(text="✏️ Править описание",  callback_data=f"ef:main:descr:{listing_id}")],
    ]

    # добавить все доп-поля той же лентой
    for f in defs:
        key   = (str(f.get("key","")).strip().lower() or "field")
        label = f.get("label") or f.get("name") or key
        val   = flex_vals.get(key)
        lines.append("")
        lines.append(f"<b>{label}:</b> <i>{_fmt(val)}</i>")
        rows.append([InlineKeyboardButton(text=f"✏️ Править: {label}", callback_data=f"ef:extra:{key}:{listing_id}")])

    # навигация в самый низ
    rows.append([InlineKeyboardButton(text="⬅️ Назад к объявлению", callback_data=f"listing:{listing_id}:{city.slug}:{cat.slug}:my")])

    text = "\n".join(lines)
    msg = await send(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    last_bot_messages[chat_id] = [msg.message_id]

    print(
        f"FUNC: {_render_overview.__name__} | chat_id={chat_id} | listing_id={listing_id} | "
        f"flex_fields={len(defs)} | msg_id={msg.message_id}"
    )


# ─────────────────────────────────────────────────────────
# Экран-обзор: точка входа
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("edit_listing_overview:"))
async def edit_listing_overview(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    listing_id = int(cb.data.split(":")[1])

    async with SessionLocal() as s:
        listing = await _get_listing(s, listing_id)
        if listing.owner_id != cb.from_user.id:
            await cb.answer("Можно редактировать только свои объявления.", show_alert=True)
            return

    await _render_overview(chat_id, cb.message.bot, cb.message.answer, listing_id)
    await state.update_data(ef_listing_id=listing_id)

    await cb.answer()
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id={chat_id} | user_id={cb.from_user.id} | listing_id={listing_id}"
    )


# ─────────────────────────────────────────────────────────
# Нажатие «Править …» (основные поля)
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.regexp(r"^ef:main:(title|price|descr):(\d+)$"))
async def ef_edit_main(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.message.bot)

    _, _, field, listing_id_str = cb.data.split(":")
    listing_id = int(listing_id_str)

    async with SessionLocal() as s:
        listing = await _get_listing(s, listing_id)
        city, cat = await _get_city_cat(s, listing)

    if field == "title":
        title = "🪧 <b>Заголовок</b>"
        cur   = _fmt(listing.title)
        ask   = "Отправьте новый текст заголовка:"
    elif field == "price":
        title = "💰 <b>Цена</b>"
        cur   = _fmt(listing.price)
        ask   = "Отправьте новую цену:"
    else:
        title = "📝 <b>Описание</b>"
        cur   = _fmt(listing.descr)
        ask   = "Отправьте новый текст описания:"

    kb = _controls_cancel(listing_id)
    msg = await cb.message.answer(
        f"{title}\n\nТекущее значение:\n<b>{cur}</b>\n\n{ask}",
        reply_markup=kb, parse_mode="HTML"
    )
    last_bot_messages[chat_id] = [msg.message_id]

    await state.update_data(ef_mode="main", ef_field=field, ef_listing_id=listing_id)
    await state.set_state(OneFieldStates.waiting_value)
    await cb.answer()

    print(f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id={chat_id} | field={field} | listing_id={listing_id} | msg_id={msg.message_id}")


@router.message(OneFieldStates.waiting_value)
async def ef_apply_main_or_extra_textnum(m: Message, state: FSMContext):
    """Применение значения для text/number (как у основного, так и у extra с типом text/number)."""
    chat_id = m.chat.id
    await clear_bot_messages(chat_id, m.bot)

    data = await state.get_data()
    mode   = data.get("ef_mode")          # "main" или "extra"
    field  = data.get("ef_field")         # 'title'/'price'/'descr' ИЛИ extra-key
    l_id   = int(data.get("ef_listing_id"))
    e_type = data.get("ef_type", "text")  # только для extra (text/number)

    new_text = (m.text or "").strip()

    async with SessionLocal() as s:
        listing = await _get_listing(s, l_id)

        if mode == "main":
            if field == "title":
                listing.title = new_text
            elif field == "price":
                listing.price = new_text
            else:
                listing.descr = new_text
        else:
            # extra text/number
            try:
                flex = json.loads(listing.flex) if listing.flex else {}
                if not isinstance(flex, dict):
                    flex = {}
            except Exception:
                flex = {}

            if e_type == "number":
                raw = new_text.replace(",", ".")
                try:
                    num = float(raw)
                    if num.is_integer():
                        num = int(num)
                    flex[field] = num
                except Exception:
                    msg = await m.answer("Нужно число. Попробуйте снова.", reply_markup=_controls_cancel(l_id))
                    last_bot_messages[chat_id] = [msg.message_id]
                    print(f"FUNC: {inspect.currentframe().f_code.co_name} | bad number | text={new_text}")
                    return
            else:
                flex[field] = new_text

            listing.flex = json.dumps(flex, ensure_ascii=False) if flex else None

        await s.commit()

    # безопасный возврат в обзор без _FakeCb
    await _render_overview(chat_id, m.bot, m.answer, l_id)
    await state.clear()

    print(f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id={chat_id} | mode={mode} | field={field} | listing_id={l_id} | saved")


# ─────────────────────────────────────────────────────────
# Нажатие «Править …» (доп. поля)
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.regexp(r"^ef:extra:([^:]+):(\d+)$"))
async def ef_edit_extra(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.message.bot)

    _, _, key, listing_id_str = cb.data.split(":")
    listing_id = int(listing_id_str)

    async with SessionLocal() as s:
        listing = await _get_listing(s, listing_id)
        defs = await _load_category_fields(s, listing.category_id)

    # найдём определение поля
    fdef = next((f for f in defs if (str(f.get("key","")).strip().lower() or "field") == key), None)
    if not fdef:
        await cb.answer("Поле не найдено.", show_alert=True)
        return

    ftype  = str(fdef.get("type", "text"))
    label  = fdef.get("label") or fdef.get("name") or key
    opts   = fdef.get("options") if isinstance(fdef.get("options"), list) else []

    # текущее значение
    cur_val = None
    try:
        flex = json.loads(listing.flex) if listing.flex else {}
        if isinstance(flex, dict):
            cur_val = flex.get(key)
    except Exception:
        pass

    title = f"<b>{label}</b>"
    cur_line = f"Текущее значение:\n<b>{_fmt(cur_val)}</b>"

    # интерфейсы по типам
    if ftype in ("text", "number"):
        kb = _controls_cancel(listing_id)
        msg = await cb.message.answer(f"{title}\n\n{cur_line}\n\nВведите значение" + (" (число)" if ftype=='number' else "") + ":", reply_markup=kb, parse_mode="HTML")
        last_bot_messages[chat_id] = [msg.message_id]
        await state.update_data(ef_mode="extra", ef_field=key, ef_listing_id=listing_id, ef_type=ftype)
        await state.set_state(OneFieldStates.waiting_value)

    elif ftype == "checkbox":
        rows = [[
            InlineKeyboardButton(text="✅ Да",  callback_data=f"efx:checkbox:1:{key}:{listing_id}"),
            InlineKeyboardButton(text="❌ Нет", callback_data=f"efx:checkbox:0:{key}:{listing_id}"),
        ], [
            InlineKeyboardButton(text="❎ Отменить", callback_data=f"ef:cancel:{listing_id}")
        ]]
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        msg = await cb.message.answer(f"{title}\n\n{cur_line}\n\nВыберите вариант:", reply_markup=kb, parse_mode="HTML")
        last_bot_messages[chat_id] = [msg.message_id]

    elif ftype == "select":
        buttons = [InlineKeyboardButton(text=str(opt), callback_data=f"efx:select:{i}:{key}:{listing_id}") for i, opt in enumerate(opts)]
        row_len = 3 if len(buttons) > 6 else 2
        rows = [buttons[i:i+row_len] for i in range(0, len(buttons), row_len)] if buttons else []
        rows.append([InlineKeyboardButton(text="❎ Отменить", callback_data=f"ef:cancel:{listing_id}")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        msg = await cb.message.answer(f"{title}\n\n{cur_line}\n\nВыберите вариант:", reply_markup=kb, parse_mode="HTML")
        last_bot_messages[chat_id] = [msg.message_id]

    else:
        await cb.answer("Неизвестный тип поля.", show_alert=True)

    await cb.answer()

    print(f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id={chat_id} | listing_id={listing_id} | key={key} | ftype={ftype}")


# checkbox выбор
@router.callback_query(F.data.regexp(r"^efx:checkbox:(0|1):([^:]+):(\d+)$"))
async def efx_checkbox(cb: CallbackQuery, state: FSMContext):
    _, _, bit, key, l_id = cb.data.split(":")
    l_id = int(l_id)
    val = bit == "1"

    async with SessionLocal() as s:
        listing = await _get_listing(s, l_id)
        try:
            flex = json.loads(listing.flex) if listing.flex else {}
            if not isinstance(flex, dict):
                flex = {}
        except Exception:
            flex = {}
        flex[key] = val
        listing.flex = json.dumps(flex, ensure_ascii=False) if flex else None
        await s.commit()

    await _render_overview(cb.message.chat.id, cb.message.bot, cb.message.answer, l_id)
    await state.clear()
    await cb.answer()

    print(f"FUNC: {inspect.currentframe().f_code.co_name} | checkbox saved | key={key} | val={val} | listing_id={l_id}")


# select выбор
@router.callback_query(F.data.regexp(r"^efx:select:(\d+):([^:]+):(\d+)$"))
async def efx_select(cb: CallbackQuery, state: FSMContext):
    _, _, idx_str, key, l_id = cb.data.split(":")
    l_id = int(l_id)
    opt_idx = int(idx_str)

    async with SessionLocal() as s:
        listing = await _get_listing(s, l_id)
        defs = await _load_category_fields(s, listing.category_id)
        fdef = next((f for f in defs if (str(f.get("key","")).strip().lower() or "field") == key), None)
        options = (fdef.get("options") if fdef else []) or []
        value = options[opt_idx] if 0 <= opt_idx < len(options) else None

        try:
            flex = json.loads(listing.flex) if listing.flex else {}
            if not isinstance(flex, dict):
                flex = {}
        except Exception:
            flex = {}
        flex[key] = value
        listing.flex = json.dumps(flex, ensure_ascii=False) if flex else None
        await s.commit()

    await _render_overview(cb.message.chat.id, cb.message.bot, cb.message.answer, l_id)
    await state.clear()
    await cb.answer()

    print(f"FUNC: {inspect.currentframe().f_code.co_name} | select saved | key={key} | value={value} | listing_id={l_id}")


# ─────────────────────────────────────────────────────────
# Отмена и возврат к обзору
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.regexp(r"^ef:cancel:(\d+)$"))
async def ef_cancel(cb: CallbackQuery, state: FSMContext):
    l_id = int(cb.data.split(":")[2])
    await _render_overview(cb.message.chat.id, cb.message.bot, cb.message.answer, l_id)
    await state.clear()
    await cb.answer()
    print(f"FUNC: {inspect.currentframe().f_code.co_name} | cancel | listing_id={l_id}")
