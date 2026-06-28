# -*- coding: utf-8 -*-
"""
app/routers/events_view.py
RU: «Афиша» — меню и календарь событий на базе общих Listing/Category.
Каноны: русские заголовки, явные print, подчистка сообщений перед отдачей нового.
Кнопки:
- ecity:<slug>         — выбор города (сохраняем в FSM)
- events:near          — открыть календарь текущего месяца (с учётом выбранного города)
- events:month:YYYY-MM — листать месяцы (сохраняем фильтр города)
- events:day:YYYY-MM-DD— просмотр конкретного дня
- event_new            — заглушка «добавить событие»
"""

from __future__ import annotations

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

from datetime import date, datetime
import calendar
from typing import Dict, List, Set, Optional

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Listing, Category, City
from app.routers.utils import clear_bot_messages
from app.keyboards import get_common_menu_button

router = Router(name="events_view")

# Корень дерева «Афиша» в Category (не отображаем его в цепочке, используем как якорь)
EVENTS_ROOT_ID = 100


# ────────────────────────── Вспомогательные функции ────────────────────────── #

def _ym_add_month(d: date, delta: int) -> date:
    """RU: Сместить дату на delta месяцев, вернув первое число нового месяца."""
    y, m = d.year, d.month + delta
    while m < 1:
        y -= 1
        m += 12
    while m > 12:
        y += 1
        m -= 12
    return date(y, m, 1)


def _render_month_grid(y: int, m: int, marks: Set[int]) -> str:
    """RU: Отрисовать текстовую сетку месяца; дни с событиями выделяем жирным."""
    cal = calendar.Calendar(firstweekday=0)  # Пн-Вс
    lines = [f"<b>{calendar.month_name[m]} {y}</b>"]
    lines.append("Пн  Вт  Ср  Чт  Пт  Сб  Вс")

    week: List[str] = []
    for d in cal.itermonthdates(y, m):
        if d.month != m:
            week.append("  ")
        else:
            s = f"{d.day:2d}"
            if d.day in marks:
                s = f"<b>{s}</b>"
            week.append(s)
        if len(week) == 7:
            lines.append("  ".join(week))
            week = []
    if week:
        lines.append("  ".join(week))
    return "\n".join(lines)


async def _all_descendant_category_ids(session, root_id: int) -> Set[int]:
    """RU: Получить множество id всех категорий в поддереве root_id (прямые и вложенные)."""
    ids: Set[int] = set()
    queue: List[int] = [root_id]
    guard = 0
    while queue and guard < 1000:
        guard += 1
        pid = queue.pop()
        rows = (await session.execute(
            select(Category).where(Category.parent_id == pid)
        )).scalars().all()
        for c in rows:
            if c.id not in ids:
                ids.add(c.id)
                queue.append(c.id)
    return ids


def _parse_event_date_from_flex(listing: Listing) -> Optional[date]:
    """
    RU: Дата события читается из listing.flex по ключу 'date' или 'event_date'.
    Поддерживаем форматы:
      - 'ДД.ММ.ГГГГ'
      - 'YYYY-MM-DD'
    """
    flex = getattr(listing, "flex", None) or {}
    raw: Optional[str] = None
    for key in ("date", "event_date"):
        v = flex.get(key)
        if isinstance(v, str) and v.strip():
            raw = v.strip()
            break
    if not raw:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except Exception:
            pass
    return None


async def _month_events(session, y: int, m: int, city_id: Optional[int] = None) -> Dict[int, List[Listing]]:
    """
    RU: Вернуть словарь «день -> список Listing» для месяца y-m.
    Фильтруем листинги из поддерева EVENTS_ROOT_ID; дополнительно можно фильтровать по городу.
    """
    cat_ids = await _all_descendant_category_ids(session, EVENTS_ROOT_ID)
    if not cat_ids:
        return {}

    q = select(Listing).where(Listing.category_id.in_(cat_ids))
    if city_id:
        q = q.where(Listing.city_id == city_id)
    q = q.order_by(Listing.created_at.desc())

    listings = (await session.execute(q)).scalars().all()

    by_day: Dict[int, List[Listing]] = {}
    for l in listings:
        d = _parse_event_date_from_flex(l)
        if not d:
            continue
        if d.year == y and d.month == m:
            by_day.setdefault(d.day, []).append(l)

    # Сортировка по названию внутри дня
    for day in by_day:
        by_day[day].sort(key=lambda x: (x.title or "").lower())
    return by_day


# ─────────────────────────────── ХЕНДЛЕРЫ ─────────────────────────────── #

# RU: выбор города «Афиши» — кнопка ecity:<slug>; сохраняем выбор в FSM и остаёмся в меню
@router.callback_query(F.data.startswith("ecity:"))
async def events_choose_city(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    user_id = cb.from_user.id
    print(f"[events_view.py] ENTER events_choose_city | chat_id={chat_id} user_id={user_id} data={cb.data}")

    slug = cb.data.split(":", 1)[1] if ":" in cb.data else None
    if not slug:
        await cb.answer("Некорректный город", show_alert=True)
        print(f"[events_view.py] EXIT  events_choose_city | chat_id={chat_id} user_id={user_id} ERROR=bad-slug")
        return

    async with SessionLocal() as s:
        city = (await s.execute(select(City).where(City.slug == slug))).scalar_one_or_none()

    if not city:
        await cb.answer("Город не найден", show_alert=True)
        print(f"[events_view.py] EXIT  events_choose_city | chat_id={chat_id} user_id={user_id} ERROR=city-not-found slug={slug}")
        return

    try:
        await state.update_data(events_city_id=city.id)
        print(f"[events_view.py] events_choose_city: set events_city_id={city.id}")
    except Exception as e:
        print(f"[events_view.py] events_choose_city: cannot update state -> {e}")

    await cb.answer(f"Город выбран: {city.name}", show_alert=False)
    print(f"[events_view.py] EXIT  events_choose_city | chat_id={chat_id} user_id={user_id} city_id={city.id} slug={slug}")


# RU: «Ближайшие мероприятия» — открыть календарь текущего месяца
@router.callback_query(F.data == "events:near")
async def events_near(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    user_id = cb.from_user.id
    print(f"[events_view.py] ENTER events_near | chat_id={chat_id} user_id={user_id} data={cb.data}")

    # подчистка
    await clear_bot_messages(chat_id, cb.bot)
    try:
        await cb.message.delete()
        print(f"[events_view.py] events_near: deleted cb.message_id={cb.message.message_id}")
    except Exception as e:
        print(f"[events_view.py] events_near: cannot delete cb.message -> {e}")

    data = await state.get_data()
    city_id = data.get("events_city_id")

    today = date.today()
    await _render_month(cb, today.year, today.month, city_id=city_id)

    print(f"[events_view.py] EXIT  events_near | chat_id={chat_id} user_id={user_id} city_id={city_id}")


# RU: Листание месяцев — открыть указанный месяц (учитывая выбранный город)
@router.callback_query(F.data.startswith("events:month:"))
async def events_month(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    user_id = cb.from_user.id
    print(f"[events_view.py] ENTER events_month | chat_id={chat_id} user_id={user_id} data={cb.data}")

    # подчистка
    await clear_bot_messages(chat_id, cb.bot)
    try:
        await cb.message.delete()
        print(f"[events_view.py] events_month: deleted cb.message_id={cb.message.message_id}")
    except Exception as e:
        print(f"[events_view.py] events_month: cannot delete cb.message -> {e}")

    try:
        _, _, ym = cb.data.split(":")
        y_str, m_str = ym.split("-")
        y, m = int(y_str), int(m_str)
    except Exception:
        await cb.answer("Некорректная дата", show_alert=True)
        print(f"[events_view.py] EXIT  events_month | chat_id={chat_id} user_id={user_id} ERROR=parse-date")
        return

    data = await state.get_data()
    city_id = data.get("events_city_id")

    await _render_month(cb, y, m, city_id=city_id)

    print(f"[events_view.py] EXIT  events_month | chat_id={chat_id} user_id={user_id} y={y} m={m} city_id={city_id}")


# RU: Просмотр конкретного дня — список событий за дату
@router.callback_query(F.data.startswith("events:day:"))
async def events_day(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    user_id = cb.from_user.id
    print(f"[events_view.py] ENTER events_day | chat_id={chat_id} user_id={user_id} data={cb.data}")

    # подчистка
    await clear_bot_messages(chat_id, cb.bot)
    try:
        await cb.message.delete()
        print(f"[events_view.py] events_day: deleted cb.message_id={cb.message.message_id}")
    except Exception as e:
        print(f"[events_view.py] events_day: cannot delete cb.message -> {e}")

    # парсим дату
    try:
        _, _, dstr = cb.data.split(":")
        y, m, d = [int(x) for x in dstr.split("-")]
        day_dt = date(y, m, d)
    except Exception:
        await cb.answer("Некорректная дата", show_alert=True)
        print(f"[events_view.py] EXIT  events_day | chat_id={chat_id} user_id={user_id} ERROR=parse-date")
        return

    data = await state.get_data()
    city_id = data.get("events_city_id")

    # выборка/рендер (минимум: только заголовки)
    async with SessionLocal() as s:
        cat_ids = await _all_descendant_category_ids(s, EVENTS_ROOT_ID)
        if not cat_ids:
            listings = []
        else:
            q = select(Listing).where(Listing.category_id.in_(cat_ids))
            if city_id:
                q = q.where(Listing.city_id == city_id)
            q = q.order_by(Listing.created_at.desc())
            listings = (await s.execute(q)).scalars().all()

        day_list: List[Listing] = []
        for l in listings:
            ld = _parse_event_date_from_flex(l)
            if ld == day_dt:
                day_list.append(l)

        city_name = None
        if city_id:
            c = await s.get(City, city_id)
            city_name = getattr(c, "name", None)

    lines: List[str] = []
    if city_name:
        lines.append(f"Город: <b>{city_name}</b>")
        lines.append("")

    lines.append(f"📅 <b>События на {day_dt.strftime('%d.%m.%Y')}</b>")
    lines.append("")
    if day_list:
        for l in sorted(day_list, key=lambda x: (x.title or "").lower()):
            title = (l.title or "(без названия)").strip()
            lines.append(f"• {title}")
    else:
        lines.append("Пока ничего не запланировано.")

    text = "\n".join(lines)

    kb_rows: List[List[InlineKeyboardButton]] = []
    kb_rows.append([InlineKeyboardButton(text="⬅️ К месяцу", callback_data=f"events:month:{y}-{m:02d}")])
    kb_rows.append([InlineKeyboardButton(text="➕ Добавить информацию", callback_data="event_new")])
    main_btn = await get_common_menu_button('main_menu')
    if main_btn:
        kb_rows.append([main_btn])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()

    print(f"[events_view.py] EXIT  events_day | chat_id={chat_id} user_id={user_id} day={day_dt.isoformat()} city_id={city_id}")


# RU: Заглушка «добавить событие» — позже подключим создание Listing + flex
@router.callback_query(F.data == "event_new")
async def events_add_stub(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    user_id = cb.from_user.id
    print(f"[events_view.py] ENTER events_add_stub | chat_id={chat_id} user_id={user_id}")

    # Подчистка хвостов
    await clear_bot_messages(chat_id, cb.bot)
    try:
        await cb.message.delete()
        print(f"[events_view.py] events_add_stub: deleted cb.message_id={cb.message.message_id}")
    except Exception as e:
        print(f"[events_view.py] events_add_stub: cannot delete cb.message -> {e}")

    text = (
        "📝 <b>Добавление события</b>\n\n"
        "В разработке: будем создавать через общий Listing + flex\n"
        "(корень Афиши: Category.id = 100)."
    )

    kb_rows: List[List[InlineKeyboardButton]] = []
    kb_rows.append([InlineKeyboardButton(text="📅 К календарю", callback_data="events:near")])
    main_btn = await get_common_menu_button('main_menu')
    if main_btn:
        kb_rows.append([main_btn])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()

    print(f"[events_view.py] EXIT  events_add_stub | chat_id={chat_id} user_id={user_id}")


# ────────────────────────── Рендер месяца (общая функция) ────────────────────────── #

# RU: Внутренняя функция рендера календаря месяца с кратким списком событий по дням + кнопки дат
async def _render_month(cb: CallbackQuery, y: int, m: int, city_id: Optional[int] = None):
    chat_id = cb.message.chat.id
    user_id = cb.from_user.id
    print(f"[events_view.py] ENTER _render_month | chat_id={chat_id} user_id={user_id} y={y} m={m} city_id={city_id}")

    # Получаем события за месяц
    async with SessionLocal() as s:
        by_day = await _month_events(s, y, m, city_id=city_id)
        city_name = None
        if city_id:
            c = await s.get(City, city_id)
            city_name = getattr(c, "name", None)

    marks = set(by_day.keys())
    grid_text = _render_month_grid(y, m, marks)

    # Текст
    lines: List[str] = []
    if city_name:
        lines.append(f"Город: <b>{city_name}</b>")
        lines.append("")  # отступ

    lines.append(grid_text)
    lines.append("")
    if marks:
        for d in sorted(marks):
            lines.append(f"• <b>{d:02d}.{m:02d}</b>")
            for e in by_day[d]:
                title = (e.title or "(без названия)").strip()
                lines.append(f"  — {title}")
    else:
        lines.append("Событий в этом месяце пока нет.")

    text = "\n".join(lines)

    # Кнопки дат (первые 8, по 4 в строке)
    days_with_events = sorted(marks)[:8]
    day_buttons: List[InlineKeyboardButton] = [
        InlineKeyboardButton(
            text=f"{d:02d}.{m:02d}",
            callback_data=f"events:day:{y}-{m:02d}-{d:02d}"
        )
        for d in days_with_events
    ]

    # Навигация + кнопки
    kb_rows: List[List[InlineKeyboardButton]] = []

    # Листание месяцев
    prev_ym = _ym_add_month(date(y, m, 1), -1)
    next_ym = _ym_add_month(date(y, m, 1), +1)
    kb_rows.append([
        InlineKeyboardButton(text="◀️", callback_data=f"events:month:{prev_ym.year}-{prev_ym.month:02d}"),
        InlineKeyboardButton(text="Сегодня", callback_data="events:near"),
        InlineKeyboardButton(text="▶️", callback_data=f"events:month:{next_ym.year}-{next_ym.month:02d}"),
    ])

    # Ряд(ы) с кнопками дат — по 4 в строке
    if day_buttons:
        for i in range(0, len(day_buttons), 4):
            kb_rows.append(day_buttons[i:i+4])

    kb_rows.append([InlineKeyboardButton(text="➕ Добавить информацию", callback_data="event_new")])

    main_btn = await get_common_menu_button('main_menu')
    if main_btn:
        kb_rows.append([main_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    # Отдаём новое сообщение (предыдущее уже удалили в вызывающем хендлере)
    await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()

    print(f"[events_view.py] EXIT  _render_month | chat_id={chat_id} user_id={user_id} y={y} m={m} city_id={city_id}")
