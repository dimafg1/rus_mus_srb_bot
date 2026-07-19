# app/routers/vacancy_edit_overview.py
from __future__ import annotations

# Короткое RU-описание: модуль «Обзор и редактирование вакансии» (как в Услугах/Барахолке).
from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from sqlalchemy import select
import json
from html import escape as html_escape

from app.database import SessionLocal
from app.models import Listing, City, Category
from app.keyboards import get_common_menu_button
from app.routers.utils import clear_bot_messages, last_bot_messages, safe_edit_or_send, register_bot_messages
from app.models import Category

router = Router(name="vacancy_edit_overview")

VACANCY_ROOT_ID=90


# ─────────────────────────────────────────────────────────────────────────────
# Служебные
# ─────────────────────────────────────────────────────────────────────────────

async def _category_chain_by_db(cat: Category | None) -> str:
    print("FUNC: _category_chain_by_db | cat_id:", getattr(cat, "id", None))
    if not cat:
        return "—"

    names = []
    current_id = cat.id
    guard = 0
    async with SessionLocal() as s:
        while current_id and guard < 20:
            guard += 1
            c = await s.get(Category, current_id)
            if not c:
                break
            # корень "Вакансии" не отображаем
            if getattr(c, "id", None) == VACANCY_ROOT_ID:
                break
            name = (getattr(c, "name", None) or "").strip() or "—"
            names.append(name)

            pid = getattr(c, "parent_id", None)
            if not pid or pid == current_id:
                break
            current_id = pid

    names.reverse()
    chain = " › ".join(names) if names else (getattr(cat, "name", None) or "—")
    print("OK: _category_chain_by_db | chain:", chain)
    return chain

def _pp(handler: str, **kw):
    parts = " ".join(f"{k}={v!r}" for k, v in kw.items())
    print(f"[vacancy_edit_overview.py] handler={handler} {parts}")

def _fmt(val):
    if val is None or val == "" or (isinstance(val, (list, dict)) and not val):
        return "<i>—</i>"
    if isinstance(val, list):
        return f"<i>{html_escape(', '.join(map(str, val)))}</i>"
    if isinstance(val, dict):
        return f"<i>{html_escape(json.dumps(val, ensure_ascii=False))}</i>"
    return f"<i>{html_escape(str(val))}</i>"


async def _owned_vacancy_in_session(s, listing_id: int, user_id: int) -> Listing | None:
    """ID из callback/FSM недоверенный: проверяем владельца и раздел одним запросом."""
    return (await s.execute(select(Listing).where(
        Listing.id == listing_id,
        Listing.owner_id == user_id,
        Listing.type == "vacancy",
    ))).scalar_one_or_none()


async def _authorize_vacancy_callback(cb: CallbackQuery, listing_id: int) -> bool:
    async with SessionLocal() as s:
        listing = await _owned_vacancy_in_session(s, listing_id, cb.from_user.id)
    if listing is None:
        await cb.answer("⛔️ Недостаточно прав.", show_alert=True)
        return False
    return True


async def _load_listing_bundle(listing_id: int):
    """RU: Загрузить Listing + City + Category + defs + flex_vals."""
    async with SessionLocal() as s:
        l: Listing = await s.get(Listing, listing_id)
        if not l:
            return None, None, None, [], {}
        city = await s.get(City, l.city_id) if l.city_id else None
        cat: Category = await s.get(Category, l.category_id) if l.category_id else None
        # defs из Category.fields
        defs = []
        if cat and (cat.fields or "").strip():
            try:
                defs = json.loads(cat.fields)
                if not isinstance(defs, list):
                    defs = []
            except Exception:
                defs = []
        # flex
        flex_vals = {}
        try:
            if l.flex:
                fv = json.loads(l.flex)
                if isinstance(fv, dict):
                    flex_vals = fv
        except Exception:
            pass
        return l, city, cat, defs, flex_vals
    
async def _category_chain(session, cat: Category) -> str:
    names = []
    cur = cat
    while cur:
        names.append((cur.name or "").strip() or "—")
        # поднимаемся к родителю
        if getattr(cur, "parent_id", None):
            cur = await session.get(Category, cur.parent_id)
        else:
            cur = None
    names.reverse()
    return " › ".join(n for n in names if n)

# Хелпер: собрать цепочку категории "Родитель › Дочерняя"
def _cat_chain(cat: Category | None) -> str:
    if not cat:
        return "—"
    names = []
    cur = cat
    seen = set()
    # Пытаемся пройтись по relationship `parent` (если он настроен в модели)
    for _ in range(10):
        if not cur or id(cur) in seen:
            break
        seen.add(id(cur))
        nm = (getattr(cur, "name", None) or "").strip() or "—"
        names.append(nm)
        nxt = getattr(cur, "parent", None)
        # Если relationship не подгружен и нет способа дотянуться до БД — выходим
        if nxt is None and getattr(cur, "parent_id", None) and getattr(cur, "parent", None) is None:
            break
        cur = nxt
    names.reverse()
    return " › ".join(n for n in names if n)

def _build_overview_text(
    l: Listing,
    city: City | None,
    cat: Category | None,
    defs: list[dict],
    flex_vals: dict,
    cat_path: str | None = None,     # ← добавили
) -> str:
    """RU: Сформировать текст обзора с отступами между секциями."""
    lines = []
    lines.append("🛠️ <b>Редактирование объявления</b>")
    lines.append("Раздел: Вакансии")
    if city:
        lines.append(f"Город: {html_escape(city.name or '')}")
    lines.append("")  # пустая строка между городом и категорией
    if cat:
        lines.append(f"Категория: {html_escape(cat_path or cat.name or '')}")
    lines.append("")

    lines.append(f"<b>Заголовок:</b> {_fmt(l.title)}")
    lines.append("")
    lines.append(f"<b>Описание:</b> {_fmt(getattr(l, 'descr', None))}")
    lines.append("")
    lines.append(f"<b>Оплата:</b> {_fmt(l.price)}")
    lines.append("")

    # Дополнительные поля
    for fdef in (defs or []):
        ftype = str(fdef.get("type", "text")).strip().lower()
        if ftype.startswith("__"):
            continue
        key = str(fdef.get("key", "")).strip().lower() or "field"
        label = fdef.get("label") or fdef.get("name") or key
        cur = None
        for k, v in (flex_vals or {}).items():
            if str(k).strip().lower() == key:
                cur = v
                break
        lines.append(f"<b>{html_escape(str(label))}:</b> {_fmt(cur)}")
        lines.append("")
    return "\n".join(lines)

def _back_cb_from_ctx(listing_id: int, data: dict) -> str:
    """RU: Callback возврата к карточке по сохранённому источнику открытия.

    Источник (search/catalog/my) кладётся в FSM-данные при входе в обзор;
    без него возврат вёл бы в «Мои» даже из поиска или каталога."""
    src = (data or {}).get("vef_back_src") or "my"
    if src == "search":
        return f"vac_view:{listing_id}:search"
    if src == "catalog":
        city = (data or {}).get("vef_back_city")
        cat = (data or {}).get("vef_back_cat")
        if city and cat:
            return f"vac_view:{listing_id}:{city}:{cat}"
    return f"vac_view:{listing_id}:::my"


async def _build_overview_kb(listing_id: int, defs: list[dict], back_cb: str | None = None) -> InlineKeyboardMarkup:
    """RU: Клавиатура обзора: правка основных и flex-полей, «Назад к объявлению»."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="✏️ Править заголовок", callback_data=f"vef:main:title:{listing_id}")],
        [InlineKeyboardButton(text="✏️ Править описание",  callback_data=f"vef:main:descr:{listing_id}")],
        [InlineKeyboardButton(text="✏️ Править оплату",  callback_data=f"vef:main:price:{listing_id}")],
    ]
    for fdef in (defs or []):
        ftype = str(fdef.get("type", "text")).strip().lower()
        if ftype.startswith("__"):
            continue
        key   = (str(fdef.get("key", "")).strip().lower() or "field")
        label = fdef.get("label") or fdef.get("name") or key
        rows.append([InlineKeyboardButton(text=f"✏️ Править: {label}", callback_data=f"vef:extra:{key}:{listing_id}")])

    rows.append([InlineKeyboardButton(
        text="⬅️ Назад к объявлению",
        callback_data=back_cb or f"vac_view:{listing_id}:::my",
    )])
    main_btn = await get_common_menu_button('main_menu')
    if main_btn:
        rows.append([main_btn])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def _render_overview(chat_id: int, bot, send, listing_id: int, back_cb: str | None = None):
    """RU: Рендер обзора и клавиатуры (общая точка вызова)."""
    l, city, cat, defs, flex_vals = await _load_listing_bundle(listing_id)
    if not l:
        msg = await send("Объявление не найдено.", parse_mode="HTML")
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        _pp("_render_overview", chat_id=chat_id, listing_id=listing_id, err="not_found")
        return
    # Закрытая/проданная вакансия видна только по маршруту «Мои»: карточка
    # search/catalog фильтрует по status=active и ответила бы «недоступна».
    if (l.status or "").strip() != "active" or getattr(l, "is_sold", False):
        back_cb = f"vac_view:{l.id}:::my"

    cat_path = await _category_chain_by_db(cat) if cat else None
    text = _build_overview_text(l, city, cat, defs, flex_vals, cat_path)

    kb = await _build_overview_kb(l.id, defs, back_cb=back_cb)
    msg = await send(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    _pp("_render_overview", chat_id=chat_id, listing_id=listing_id, msg_id=msg.message_id)


# ─────────────────────────────────────────────────────────────────────────────
# Хендлер входа в обзор
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("vacancy_edit_overview:"))
async def vacancy_edit_overview_entry(cb: CallbackQuery, state: FSMContext):
    """RU: Открыть обзор редактирования вакансии.
    Каноны: удаляем сообщение, по которому нажали, чистим хвосты бота,
    показываем новый экран. В конце — обязательный print.

    Формат: vacancy_edit_overview:<id>[:<src>:<city|- >:<cat|0>] — хвост
    с источником открытия карточки кладём в FSM-данные, чтобы «Назад
    к объявлению» вернул туда, откуда пришли (поиск/каталог/мои).
    """
    chat_id = cb.message.chat.id

    # 0) Удаляем сообщение с кнопкой «Редактировать все поля», по которому кликнули
    try:
        await cb.message.delete()
    except Exception:
        pass

    # 1) Подчистка прочих наших служебных сообщений
    await clear_bot_messages(chat_id, cb.bot)

    # 2) Разбираем ID и проверяем права
    parts = cb.data.split(":")
    try:
        listing_id = int(parts[1])
    except Exception:
        await cb.answer("Некорректный ID", show_alert=True)
        print(f"[vacancy_edit_overview.py] handler=vacancy_edit_overview_entry chat_id={chat_id} data={cb.data!r} err=bad_id")
        return

    if not await _authorize_vacancy_callback(cb, listing_id):
        print(f"[vacancy_edit_overview.py] handler=vacancy_edit_overview_entry chat_id={chat_id} listing_id={listing_id} err=forbidden")
        return

    # 2а) Отмена активного шага редактирования. Именно set_state(None), а не
    # clear(): в данных живут контекст поиска (vac_search_*) и маршрут возврата.
    await state.set_state(None)
    if len(parts) >= 5:
        await state.update_data(
            vef_back_src=(parts[2] or "my"),
            vef_back_city=(None if parts[3] in ("", "-") else parts[3]),
            vef_back_cat=(parts[4] if parts[4] and parts[4] != "0" else None),
        )
    back_cb = _back_cb_from_ctx(listing_id, await state.get_data())

    # 3) Рисуем обзор редактирования (новым сообщением)
    await _render_overview(chat_id, cb.bot, cb.message.answer, listing_id, back_cb=back_cb)

    await cb.answer()
    print(f"[vacancy_edit_overview.py] handler=vacancy_edit_overview_entry chat_id={chat_id} listing_id={listing_id}")


# ─────────────────────────────────────────────────────────────────────────────
# Основные поля: title/descr/price
# ─────────────────────────────────────────────────────────────────────────────
class _MainState(StatesGroup):
    """RU: Состояния ввода основных полей."""
    waiting_title = State()
    waiting_descr = State()
    waiting_price = State()

@router.callback_query(F.data.startswith("vef:main:title:"))
async def vef_main_title_start(cb: CallbackQuery, state: FSMContext):
    """RU: Начало редактирования заголовка (показываем текущее и кнопку Отмена)."""
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)

    try:
        listing_id = int(cb.data.split(":")[3])
    except (IndexError, TypeError, ValueError):
        await cb.answer("Некорректный ID", show_alert=True)
        return
    if not await _authorize_vacancy_callback(cb, listing_id):
        await state.set_state(None)
        return
    await state.update_data(vef_listing_id=listing_id)
    await state.set_state(_MainState.waiting_title)

    l, _, _, _, _ = await _load_listing_bundle(listing_id)
    current = html_escape((l.title or "—") if l else "—")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❎ Отменить", callback_data=f"vacancy_edit_overview:{listing_id}")]
    ])
    txt = (
        "🖊 <b>Заголовок</b>\n"
        f"Текущее значение:\n<code>{current}</code>\n\n"
        "Отправьте новый текст (или скопируйте текущий ↑ и отредактируйте):"
    )
    msg = await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

    await cb.answer()
    _pp("vef_main_title_start", chat_id=chat_id, listing_id=listing_id)

@router.message(_MainState.waiting_title)
async def vef_main_title_save(m: Message, state: FSMContext):
    """RU: Сохранить заголовок и вернуть обзор."""
    chat_id = m.chat.id
    await clear_bot_messages(chat_id, m.bot)
    try:
        await m.delete()
    except Exception:
        pass
    data = await state.get_data()
    try:
        listing_id = int(data["vef_listing_id"])
    except (KeyError, TypeError, ValueError):
        await state.set_state(None)
        await m.answer("Сеанс редактирования потерян. Откройте вакансию ещё раз.")
        return
    title = (m.text or "").strip()
    if not title:
        await m.answer("Заголовок не может быть пустым.")
        return
    async with SessionLocal() as s:
        l = await _owned_vacancy_in_session(s, listing_id, m.from_user.id)
        if not l:
            await m.answer("⛔️ Недостаточно прав.")
            await state.set_state(None)
            _pp("vef_main_title_save", chat_id=chat_id, listing_id=listing_id, err="forbidden")
            return
        l.title = title
        s.add(l); await s.commit()
    await state.set_state(None)
    await _render_overview(chat_id, m.bot, m.answer, listing_id,
                           back_cb=_back_cb_from_ctx(listing_id, data))
    _pp("vef_main_title_save", chat_id=chat_id, listing_id=listing_id)


@router.callback_query(F.data.startswith("vef:main:descr:"))
async def vef_main_descr_start(cb: CallbackQuery, state: FSMContext):
    """RU: Начало редактирования описания (показываем текущее и кнопку Отмена)."""
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)

    try:
        listing_id = int(cb.data.split(":")[3])
    except (IndexError, TypeError, ValueError):
        await cb.answer("Некорректный ID", show_alert=True)
        return
    if not await _authorize_vacancy_callback(cb, listing_id):
        await state.set_state(None)
        return
    await state.update_data(vef_listing_id=listing_id)
    await state.set_state(_MainState.waiting_descr)

    l, _, _, _, _ = await _load_listing_bundle(listing_id)
    current = html_escape((getattr(l, "descr", None) or "—") if l else "—")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❎ Отменить", callback_data=f"vacancy_edit_overview:{listing_id}")]
    ])
    txt = (
        "📄 <b>Описание</b>\n"
        f"Текущее значение:\n<code>{current}</code>\n\n"
        "Отправьте новый текст (или скопируйте текущий ↑ и отредактируйте):"
    )
    msg = await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

    await cb.answer()
    _pp("vef_main_descr_start", chat_id=chat_id, listing_id=listing_id)



@router.message(_MainState.waiting_descr)
async def vef_main_descr_save(m: Message, state: FSMContext):
    """RU: Сохранить описание и вернуть обзор."""
    chat_id = m.chat.id
    await clear_bot_messages(chat_id, m.bot)
    try:
        await m.delete()
    except Exception:
        pass
    data = await state.get_data()
    try:
        listing_id = int(data["vef_listing_id"])
    except (KeyError, TypeError, ValueError):
        await state.set_state(None)
        await m.answer("Сеанс редактирования потерян. Откройте вакансию ещё раз.")
        return
    async with SessionLocal() as s:
        l = await _owned_vacancy_in_session(s, listing_id, m.from_user.id)
        if not l:
            await m.answer("⛔️ Недостаточно прав.")
            await state.set_state(None)
            _pp("vef_main_descr_save", chat_id=chat_id, listing_id=listing_id, err="forbidden")
            return
        l.descr = (m.text or "").strip()
        s.add(l); await s.commit()
    await state.set_state(None)
    await _render_overview(chat_id, m.bot, m.answer, listing_id,
                           back_cb=_back_cb_from_ctx(listing_id, data))
    _pp("vef_main_descr_save", chat_id=chat_id, listing_id=listing_id)

@router.callback_query(F.data.startswith("vef:main:price:"))
async def vef_main_price_start(cb: CallbackQuery, state: FSMContext):
    """RU: Начало редактирования оплаты (показываем текущее и кнопку Отмена)."""
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)

    try:
        listing_id = int(cb.data.split(":")[3])
    except (IndexError, TypeError, ValueError):
        await cb.answer("Некорректный ID", show_alert=True)
        return
    if not await _authorize_vacancy_callback(cb, listing_id):
        await state.set_state(None)
        return
    await state.update_data(vef_listing_id=listing_id)
    await state.set_state(_MainState.waiting_price)

    l, _, _, _, _ = await _load_listing_bundle(listing_id)
    current = html_escape((l.price or "—") if l else "—")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❎ Отменить", callback_data=f"vacancy_edit_overview:{listing_id}")]
    ])
    txt = (
        "💰 <b>Оплата</b>\n"
        f"Текущее значение:\n<code>{current}</code>\n\n"
        "Отправьте новый текст (или скопируйте текущее ↑ и отредактируйте):"
    )
    msg = await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

    await cb.answer()
    _pp("vef_main_price_start", chat_id=chat_id, listing_id=listing_id)


@router.message(_MainState.waiting_price)
async def vef_main_price_save(m: Message, state: FSMContext):
    """RU: Сохранить зарплату и вернуть обзор."""
    chat_id = m.chat.id
    await clear_bot_messages(chat_id, m.bot)
    try:
        await m.delete()
    except Exception:
        pass
    data = await state.get_data()
    try:
        listing_id = int(data["vef_listing_id"])
    except (KeyError, TypeError, ValueError):
        await state.set_state(None)
        await m.answer("Сеанс редактирования потерян. Откройте вакансию ещё раз.")
        return
    price = (m.text or "").strip()
    if not price:
        await m.answer("Оплата не может быть пустой.")
        return
    async with SessionLocal() as s:
        l = await _owned_vacancy_in_session(s, listing_id, m.from_user.id)
        if not l:
            await m.answer("⛔️ Недостаточно прав.")
            await state.set_state(None)
            _pp("vef_main_price_save", chat_id=chat_id, listing_id=listing_id, err="forbidden")
            return
        l.price = price
        s.add(l); await s.commit()
    await state.set_state(None)
    await _render_overview(chat_id, m.bot, m.answer, listing_id,
                           back_cb=_back_cb_from_ctx(listing_id, data))
    _pp("vef_main_price_save", chat_id=chat_id, listing_id=listing_id)


# ─────────────────────────────────────────────────────────────────────────────
# Доп. поля (flex) — простой единый ввод текстом (как в Услугах), без редактирования контакта/города
# ─────────────────────────────────────────────────────────────────────────────
class _FlexState(StatesGroup):
    """RU: Состояние ожидания значения для flex-поля."""
    waiting = State()

@router.callback_query(F.data.startswith("vef:extra:"))
async def vef_extra_start(cb: CallbackQuery, state: FSMContext):
    """RU: Начало ввода значения flex-поля по key."""
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    try:
        _, _, key, lid_s = cb.data.split(":")
        listing_id = int(lid_s)
    except (TypeError, ValueError):
        await cb.answer("Некорректные данные.", show_alert=True)
        return
    if not await _authorize_vacancy_callback(cb, listing_id):
        await state.set_state(None)
        return

    l, _, _, defs, flex_vals = await _load_listing_bundle(listing_id)
    fdef = next((d for d in defs if str(d.get("key","")).strip().lower() == key), None)
    if not fdef:
        await cb.answer("Поле не найдено.", show_alert=True)
        _pp("vef_extra_start", chat_id=chat_id, listing_id=listing_id, key=key, err="no_field")
        return

    label = fdef.get("label") or fdef.get("name") or key
    current = None
    for k, v in (flex_vals or {}).items():
        if str(k).strip().lower() == key:
            current = v
            break

    await state.update_data(vef_listing_id=listing_id, vef_key=key)
    await state.set_state(_FlexState.waiting)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Отменить и вернуться", callback_data=f"vacancy_edit_overview:{listing_id}")],
    ])
    txt = (
        f"Поле: <b>{html_escape(str(label))}</b>\n"
        f"Текущее: {html_escape(str(json.dumps(current, ensure_ascii=False) if isinstance(current, (list,dict)) else (current or '—')))}\n\n"
        f"Отправьте новое значение одним сообщением."
    )
    msg = await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    _pp("vef_extra_start", chat_id=chat_id, listing_id=listing_id, key=key, msg_id=msg.message_id)

@router.message(_FlexState.waiting)
async def vef_extra_value(m: Message, state: FSMContext):
    """RU: Сохранить значение flex-поля и вернуться к обзору."""
    chat_id = m.chat.id
    await clear_bot_messages(chat_id, m.bot)
    try:
        await m.delete()
    except Exception:
        pass

    data = await state.get_data()
    try:
        listing_id = int(data["vef_listing_id"])
        key = str(data["vef_key"]).strip().lower()
    except (KeyError, TypeError, ValueError):
        await state.set_state(None)
        await m.answer("Сеанс редактирования потерян. Откройте вакансию ещё раз.")
        return
    newv = (m.text or m.caption or "").strip()

    async with SessionLocal() as s:
        l = await _owned_vacancy_in_session(s, listing_id, m.from_user.id)
        if not l:
            await m.answer("⛔️ Недостаточно прав.")
            await state.set_state(None)
            _pp("vef_extra_value", chat_id=chat_id, listing_id=listing_id, err="forbidden")
            return
        cat = await s.get(Category, l.category_id)
        try:
            defs = json.loads((cat.fields or "[]") if cat else "[]")
        except Exception:
            defs = []
        fdef = next((
            field for field in defs if isinstance(field, dict)
            and str(field.get("key", "")).strip().lower() == key
            and not str(field.get("type", "text")).strip().lower().startswith("__")
        ), None)
        if fdef is None:
            await state.set_state(None)
            await m.answer("Поле больше недоступно для редактирования.")
            return
        # загрузить и обновить flex
        try:
            flex = json.loads(l.flex or "{}")
            if not isinstance(flex, dict):
                flex = {}
        except Exception:
            flex = {}
        flex[key] = newv
        l.flex = json.dumps(flex, ensure_ascii=False)
        s.add(l); await s.commit()

    await state.set_state(None)
    await _render_overview(chat_id, m.bot, m.answer, listing_id,
                           back_cb=_back_cb_from_ctx(listing_id, data))
    _pp("vef_extra_value", chat_id=chat_id, listing_id=listing_id, field=key)
