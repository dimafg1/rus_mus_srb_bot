from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.fsm.context import FSMContext
from app.routers.utils import clear_bot_messages, last_bot_messages, get_text, log
from app.database import SessionLocal
from sqlalchemy import text as sql
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from app.keyboards import get_common_menu_button
from aiogram.fsm.state import StatesGroup, State
from aiogram.filters import StateFilter
from app.search.fuzzy import search_items, normalize_search_text

from app.analytics.search_log import log_search
from app.analytics.listing_views import log_listing_view
from app.routers.utils import register_bot_messages

import calendar as pycal
from datetime import timedelta

router = Router()

AFISHA_PAGE_SIZE = 10
AFISHA_MY_PAGE_SIZE = 10
AFISHA_SEARCH_PAGE_SIZE = 10
_TZ = ZoneInfo("Europe/Belgrade")

# ─────────────────────────────────────────────────────────
# ПОИСК по Афише (FSM + кэши сообщений/контекста)
# ─────────────────────────────────────────────────────────
class AfishaSearchStates(StatesGroup):
    wait_query = State()

# сообщения поиска (чтобы удалять хвосты точечно)
af_search_nav_msg_id: dict[int, int] = {}
af_search_prompt_msg_id: dict[int, int] = {}
af_search_results_msg_ids: dict[int, list[int]] = {}

# контекст поиска по чату (устойчив к очистке FSM, если вдруг где-то делают state.clear())
af_search_ctx_by_chat: dict[int, dict] = {}




def _fmt_dt(utc_ts: int) -> tuple[str, str]:
    """
    Форматирование даты события для отображения в карточке.
    """

    dt = datetime.fromtimestamp(utc_ts)

    months = [
        "января", "февраля", "марта", "апреля",
        "мая", "июня", "июля", "августа",
        "сентября", "октября", "ноября", "декабря"
    ]

    day = dt.day
    month = months[dt.month - 1]
    year = dt.year

    date_str = f"{day} {month} {year}"
    time_str = dt.strftime("%H:%M")

    print(f"[events_view.py][_fmt_dt][done] utc_ts={utc_ts} -> date={date_str} time={time_str}")

    return date_str, time_str


def _month_bounds_local(year: int, month: int) -> tuple[datetime, datetime]:
    start_local = datetime(year, month, 1, 0, 0, 0, tzinfo=_TZ)
    if month == 12:
        end_local = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=_TZ)
    else:
        end_local = datetime(year, month + 1, 1, 0, 0, 0, tzinfo=_TZ)
    return start_local, end_local


def _day_bounds_local(year: int, month: int, day: int) -> tuple[datetime, datetime]:
    start_local = datetime(year, month, day, 0, 0, 0, tzinfo=_TZ)
    end_local = start_local + timedelta(days=1)
    return start_local, end_local


async def _fetch_month_marks_all(year: int, month: int) -> set[int]:
    """Вернёт множество дней месяца (1..31), где есть опубликованные будущие события (вся Сербия)."""
    start_local, end_local = _month_bounds_local(year, month)
    start_utc = int(start_local.astimezone(timezone.utc).timestamp())
    end_utc = int(end_local.astimezone(timezone.utc).timestamp())

    now_utc = int(datetime.now(_TZ).astimezone(timezone.utc).timestamp())
    start_utc = max(start_utc, now_utc)

    q = sql("""
        SELECT em.start_at_utc
        FROM listing l
        JOIN events_meta em ON em.listing_id = l.id
        WHERE
            l.type = 'events'
            AND l.is_sold = 0
            AND em.status = 'published'
            AND em.start_at_utc >= :start_utc
            AND em.start_at_utc <  :end_utc
        ORDER BY em.start_at_utc ASC
    """)
    async with SessionLocal() as s:
        res = await s.execute(q, {"start_utc": start_utc, "end_utc": end_utc})
        rows = res.fetchall()

    days: set[int] = set()
    for (ts,) in rows:
        try:
            dt_local = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(_TZ)
            if dt_local.year == year and dt_local.month == month:
                days.add(int(dt_local.day))
        except Exception:
            pass

    print(f"[events_view.py][_fetch_month_marks_all][done] y={year} m={month} marks={sorted(days)[:10]} count={len(days)}")
    return days


def _kb_calendar_month_all(year: int, month: int, marks: set[int]) -> InlineKeyboardMarkup:
    weeks = pycal.monthcalendar(year, month)
    wd = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    month_name = [
        "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
        "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"
    ][month - 1]

    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)

    rows: list[list[InlineKeyboardButton]] = []

    # месяц и год — отдельной кнопкой
    rows.append([
        InlineKeyboardButton(
            text=f"{month_name} {year}",
            callback_data="af:cal:noop"
        )
    ])

    # стрелки — отдельной строкой
    rows.append([
        InlineKeyboardButton(text="«", callback_data=f"af:cal:all:{prev_y}:{prev_m}"),
        InlineKeyboardButton(text="»", callback_data=f"af:cal:all:{next_y}:{next_m}")
    ])

    # дни недели
    rows.append([InlineKeyboardButton(text=x, callback_data="af:cal:noop") for x in wd])

    # дни месяца
    for w in weeks:
        row: list[InlineKeyboardButton] = []
        for d in w:
            if d == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="af:cal:noop"))
            else:
                label = f"•{d}" if d in marks else str(d)
                row.append(InlineKeyboardButton(
                    text=label,
                    callback_data=f"af:cal:day:all:{year}:{month}:{d}:0"
                ))
        rows.append(row)

    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="go_events")])
    rows.append([InlineKeyboardButton(text="≡ Главное меню", callback_data="main_menu")])

    return InlineKeyboardMarkup(inline_keyboard=rows)



async def _afisha_root_kb() -> InlineKeyboardMarkup:
    print("[events_view.py] _afisha_root_kb CALLED")
    rows: list[list[InlineKeyboardButton]] = []
    try:
        async with SessionLocal() as s:
            res = await s.execute(sql("SELECT id, name FROM city ORDER BY id ASC LIMIT 20"))
            cities = res.fetchall()
    except Exception:
        cities = []

    row: list[InlineKeyboardButton] = []
    for cid, name in cities:
        row.append(InlineKeyboardButton(text=str(name), callback_data=f"af:ecity:{int(cid)}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton(text="🔎 Поиск", callback_data="af:search")])
    rows.append([InlineKeyboardButton(text="👤 Мои объявления", callback_data="af:my")])

    rows.append([InlineKeyboardButton(text="Ближайшие мероприятия", callback_data="events:near")])
    rows.append([InlineKeyboardButton(text="➕ Разместить информацию", callback_data="event_new")])

    try:
        mm = await get_common_menu_button("main_menu")
        if mm:
            rows.append([InlineKeyboardButton(text=mm.text, callback_data=mm.callback_data)])
    except Exception:
        pass

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    print(f"[events_view.py][_afisha_root_kb][done] cities={len(cities)} rows={len(rows)}")
    return kb


async def _fetch_events(offset: int, limit: int, city_id: int | None = None):
    now_utc = int(datetime.now(_TZ).astimezone(timezone.utc).timestamp())
    params = {"now_utc": now_utc, "limit": int(limit), "offset": int(offset)}
    where_city = ""
    if city_id is not None:
        where_city = "AND l.city_id = :city_id"
        params["city_id"] = int(city_id)

    q = sql(f"""
        SELECT
            l.id AS listing_id,
            l.title,
            l.photo_file_id,
            l.descr,
            COALESCE(c.name, em.city_text) AS city_name,
            em.venue_text,
            em.price_text,
            em.start_at_utc
        FROM listing l
        JOIN events_meta em ON em.listing_id = l.id
        LEFT JOIN city c ON c.id = l.city_id
        WHERE
            l.type = 'events'
            AND l.is_sold = 0
            AND em.status = 'published'
            AND em.start_at_utc >= :now_utc
            {where_city}
        ORDER BY em.start_at_utc ASC
        LIMIT :limit OFFSET :offset
    """)
    async with SessionLocal() as s:
        res = await s.execute(q, params)
        out = [dict(r._mapping) for r in res.fetchall()]
        print(f"[events_view.py][_fetch_events][done] offset={offset} limit={limit} city_id={city_id} now_utc={now_utc} count={len(out)}")
        return out


async def _count_events(city_id: int | None = None) -> int:
    now_utc = int(datetime.now(_TZ).astimezone(timezone.utc).timestamp())
    params = {"now_utc": now_utc}
    where_city = ""
    if city_id is not None:
        where_city = "AND l.city_id = :city_id"
        params["city_id"] = int(city_id)

    q = sql(f"""
        SELECT COUNT(*)
        FROM listing l
        JOIN events_meta em ON em.listing_id = l.id
        WHERE
            l.type = 'events'
            AND l.is_sold = 0
            AND em.status = 'published'
            AND em.start_at_utc >= :now_utc
            {where_city}
    """)
    async with SessionLocal() as s:
        res = await s.execute(q, params)
        return int(res.scalar_one() or 0)



async def _fetch_my_events(owner_id: int, offset: int, limit: int):
    # показываем только будущие события пользователя (даже если "draft" ещё не делаете — это ок)
    now_utc = int(datetime.now(_TZ).astimezone(timezone.utc).timestamp())
    params = {"owner_id": int(owner_id), "now_utc": now_utc, "limit": int(limit), "offset": int(offset)}

    q = sql("""
        SELECT
            l.id AS listing_id,
            l.title,
            l.photo_file_id,
            l.descr,
            COALESCE(c.name, em.city_text) AS city_name,
            em.venue_text,
            em.price_text,
            em.start_at_utc,
            em.status
        FROM listing l
        JOIN events_meta em ON em.listing_id = l.id
        LEFT JOIN city c ON c.id = l.city_id
        WHERE
            l.type = 'events'
            AND l.owner_id = :owner_id
            AND l.is_sold = 0
            AND em.start_at_utc >= :now_utc
        ORDER BY em.start_at_utc ASC
        LIMIT :limit OFFSET :offset
    """)
    async with SessionLocal() as s:
        res = await s.execute(q, params)
        out = [dict(r._mapping) for r in res.fetchall()]
        print(f"[events_view.py][_fetch_my_events][done] owner_id={owner_id} offset={offset} limit={limit} now_utc={now_utc} count={len(out)}")
        return out

def _contact_url(contact: str) -> str | None:
    c = (contact or "").strip()
    if not c:
        return None

    # если это tg://... или уже ссылка — оставляем как есть
    if c.startswith("tg://"):
        return c
    if c.startswith("https://") or c.startswith("http://"):
        return c

    # чистим возможный префикс @
    if c.startswith("@"):
        c = c[1:].strip()

    if not c:
        return None

    # если это чисто число — трактуем как user_id
    if c.isdigit():
        return f"tg://user?id={c}"

    # username (без пробелов/слешей) — пробуем открыть чат через tg://resolve
    # это в части клиентов открывает сразу диалог, а не инфо
    if " " not in c and "/" not in c:
        return f"tg://resolve?domain={c}"

    return None


@router.callback_query(F.data == "af:my")
async def af_my(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    owner_id = cb.from_user.id

    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    items = await _fetch_my_events(owner_id=owner_id, offset=0, limit=AFISHA_MY_PAGE_SIZE)

    if not items:
        kb = _kb_back_and_main("go_events")
        msg = await cb.message.answer("У вас пока нет опубликованных будущих событий.", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(f"[events_view.py][af_my][done] chat_id={chat_id} owner_id={owner_id} empty=1 msg_id={msg.message_id}")
        return

    rows: list[list[InlineKeyboardButton]] = []
    for it in items:
        d, t = _fmt_dt(it["start_at_utc"])
        rows.append([InlineKeyboardButton(
            text=f"{d} {t} — {it['title']}",
            callback_data=f"af:my:open:{it['listing_id']}:0"
        )])

    has_more = len(items) == AFISHA_MY_PAGE_SIZE
    more_cb = f"af:my:more:{AFISHA_MY_PAGE_SIZE}" if has_more else None
    nav = _kb_list_nav(back_cb="go_events", more_cb=more_cb)

    kb = InlineKeyboardMarkup(inline_keyboard=rows + nav.inline_keyboard)
    msg = await cb.message.answer("👤 <b>Мои объявления</b>", parse_mode="HTML", reply_markup=kb)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"[events_view.py][af_my][done] chat_id={chat_id} owner_id={owner_id} count={len(items)} has_more={int(has_more)} msg_id={msg.message_id} more_cb={more_cb}")

@router.callback_query(F.data.startswith("af:my:more:"))
async def af_my_more(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    owner_id = cb.from_user.id

    try:
        await cb.message.delete()
    except Exception:
        pass

    try:
        offset = int(cb.data.rsplit(":", 1)[-1])
    except Exception:
        offset = 0

    items = await _fetch_my_events(owner_id=owner_id, offset=offset, limit=AFISHA_MY_PAGE_SIZE)

    if not items:
        kb = _kb_back_and_main("af:my")
        msg = await cb.message.answer("Это всё по вашим событиям.", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(f"[events_view.py][af_my_more][done] chat_id={chat_id} owner_id={owner_id} offset={offset} empty=1 msg_id={msg.message_id}")
        return

    rows: list[list[InlineKeyboardButton]] = []
    for it in items:
        d, t = _fmt_dt(it["start_at_utc"])
        rows.append([InlineKeyboardButton(
            text=f"{d} {t} — {it['title']}",
            callback_data=f"af:my:open:{it['listing_id']}:{offset}"
        )])

    has_more = len(items) == AFISHA_MY_PAGE_SIZE
    more_cb = f"af:my:more:{offset + AFISHA_MY_PAGE_SIZE}" if has_more else None
    nav = _kb_list_nav(back_cb="af:my", more_cb=more_cb)

    kb = InlineKeyboardMarkup(inline_keyboard=rows + nav.inline_keyboard)
    msg = await cb.message.answer("👤 <b>Мои объявления</b>", parse_mode="HTML", reply_markup=kb)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"[events_view.py][af_my_more][done] chat_id={chat_id} owner_id={owner_id} offset={offset} count={len(items)} has_more={int(has_more)} msg_id={msg.message_id} more_cb={more_cb}")

def _kb_my_card(listing_id: int, back_cb: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"af:my:edit:{listing_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"af:my:del:{listing_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=back_cb)],
        [InlineKeyboardButton(text="≡ Главное меню", callback_data="main_menu")],
    ])
    print(f"[events_view.py][_kb_my_card][done] listing_id={listing_id} back_cb={back_cb}")
    return kb


@router.callback_query(F.data.startswith("af:my:open:"))
async def af_my_open(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    owner_id = cb.from_user.id

    parts = cb.data.split(":")
    try:
        listing_id = int(parts[3])
        offset = parts[4] if len(parts) > 4 else "0"
    except Exception:
        await cb.answer("Не удалось открыть событие.")
        print(f"[events_view.py][af_my_open][done] chat_id={chat_id} cb_data={cb.data} parse_failed=1")
        return

    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    it = await _fetch_event_by_id(listing_id)
    if not it:
        kb = _kb_back_and_main("af:my")
        msg = await cb.message.answer("Событие не найдено.", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(f"[events_view.py][af_my_open][done] chat_id={chat_id} listing_id={listing_id} found=0 msg_id={msg.message_id}")
        return

    # защита: открывать “мои” можно только свои
    try:
        async with SessionLocal() as s:
            chk = await s.execute(sql("SELECT owner_id FROM listing WHERE id=:id LIMIT 1"), {"id": listing_id})
            row = chk.first()
            if not row or int(row[0]) != int(owner_id):
                await cb.answer("Это не ваше объявление.")
                print(f"[events_view.py][af_my_open][deny] chat_id={chat_id} listing_id={listing_id} owner_id={owner_id}")
                return
    except Exception as e:
        print(f"[events_view.py][af_my_open][owner_check_error] {e}")

    # ЛОГ ОТКРЫТИЯ СВОЕЙ КАРТОЧКИ
    await log_listing_view(
        listing_id=listing_id,
        user_id=cb.from_user.id,
        section="events",
        action="open",
        source="my",
    )


    d, t = _fmt_dt(it["start_at_utc"])

    city_name = (it.get("city_name") or "").strip()
    city_text = (it.get("city_text") or "").strip()
    if city_text and (not city_name or city_name.startswith("Друг")):
        city = city_text
    else:
        city = city_name

    price = (it.get("price_text") or "").strip()
    descr = (it.get("descr") or "").strip()
    venue = (it.get("venue_text") or "").strip()
    contact = (it.get("contact") or "").strip()
    contact_url = _contact_url(contact) if contact else None

    lines = [
        f"Дата:   {d}",
        f"Время:  {t}",
    ]
    if city:
        lines.append(f"Город:  {city}")
    if venue:
        lines.append(f"Место:  {venue}")
    if price:
        lines.append(f"Цена:   {price}")
    if contact:
        lines.append(f"Контакт: {contact}")
    if descr:
        lines.append("")
        lines.append(descr)

    caption = f"<b>{it['title']}</b>\n\n" + "\n".join(lines)

    back_cb = "af:my" if offset == "0" else f"af:my:more:{offset}"
    kb = _kb_my_card(listing_id=listing_id, back_cb=back_cb)

    # кнопка "Связаться" — одной строкой, сверху
    if contact_url:
        try:
            kb.inline_keyboard.insert(0, [InlineKeyboardButton(text="📞 Связаться с контактом", url=contact_url)])
        except Exception:
            pass

    photo = it.get("photo_file_id")
    if photo:
        msg = await cb.message.answer_photo(photo=photo, caption=caption, parse_mode="HTML", reply_markup=kb)
    else:
        msg = await cb.message.answer(caption, parse_mode="HTML", reply_markup=kb)

    last_bot_messages[chat_id] = [msg.message_id]

    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"[events_view.py][af_my_open][done] chat_id={chat_id} listing_id={listing_id} back_cb={back_cb} msg_id={msg.message_id}")



async def _fetch_event_by_id(listing_id: int):
    q = sql("""
        SELECT
            l.id AS listing_id,
            l.title,
            l.photo_file_id,
            l.descr,
            l.contact,
            c.name AS city_name,
            em.city_text AS city_text,
            em.venue_text,
            em.price_text,
            em.start_at_utc
        FROM listing l
        JOIN events_meta em ON em.listing_id = l.id
        LEFT JOIN city c ON c.id = l.city_id
        WHERE l.id = :lid AND l.type = 'events'
        LIMIT 1
    """)
    async with SessionLocal() as s:
        res = await s.execute(q, {"lid": int(listing_id)})
        row = res.first()
        out = dict(row._mapping) if row else None
        print(f"[events_view.py][_fetch_event_by_id][done] listing_id={listing_id} found={bool(out)}")
        return out


async def show_event_card(message: Message, listing_id: int) -> bool:
    """
    Открыть карточку события по listing_id из deep-link /start evt_<id>.
    Возвращает True, если карточка показана, иначе False.
    """
    chat_id = message.chat.id

    try:
        await clear_bot_messages(chat_id, message.bot)
    except Exception:
        pass

    it = await _fetch_event_by_id(listing_id)
    if not it:
        msg = await message.answer("Событие не найдено.", reply_markup=_kb_back_and_main("go_events"))
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        print(f"[events_view.py][show_event_card][done] chat_id={chat_id} listing_id={listing_id} found=0 msg_id={msg.message_id}")
        return False

    d, t = _fmt_dt(it["start_at_utc"])

    city_name = (it.get("city_name") or "").strip()
    city_text = (it.get("city_text") or "").strip()
    if city_text and (not city_name or "друг" in city_name.lower()):
        city = city_text
    else:
        city = city_name

    price = (it.get("price_text") or "").strip()
    descr = (it.get("descr") or "").strip()
    venue = (it.get("venue_text") or "").strip()
    contact = (it.get("contact") or "").strip()
    contact_url = _contact_url(contact) if contact else None

    lines = [f"🗓 {d} • {t}"]
    if city:
        lines.append(f"📍 {city}")
    if venue:
        lines.append(f"🏠 {venue}")
    if price:
        lines.append(f"💲 {price}")
    if descr:
        lines.append("")
        lines.append(descr)
    if contact:
        lines.append("")
        lines.append(f"📞 Контакт: {contact}")

    caption = f" <b>{it['title']}</b>\n\n" + "\n".join(lines)

    kb = _kb_back_and_main("go_events")

    if contact_url:
        try:
            kb.inline_keyboard.insert(0, [InlineKeyboardButton(text="📞 Связаться с контактом", url=contact_url)])
        except Exception:
            pass

    photo = it.get("photo_file_id")
    if photo:
        msg = await message.answer_photo(photo=photo, caption=caption, parse_mode="HTML", reply_markup=kb)
    else:
        msg = await message.answer(caption, parse_mode="HTML", reply_markup=kb)

    last_bot_messages[chat_id] = [msg.message_id]

    await register_bot_messages(chat_id, [msg.message_id])
    print(f"[events_view.py][show_event_card][done] chat_id={chat_id} listing_id={listing_id} found=1 msg_id={msg.message_id}")
    return True




def _kb_back_and_main(back_cb: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=back_cb)],
        [InlineKeyboardButton(text="≡ Главное меню", callback_data="main_menu")],
    ])
    print(f"[events_view.py][_kb_back_and_main][done] back_cb={back_cb}")
    return kb


def _kb_list_nav(back_cb: str, more_cb: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if more_cb:
        rows.append([InlineKeyboardButton(text="Показать ещё ▶️", callback_data=more_cb)])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back_cb)])
    rows.append([InlineKeyboardButton(text="≡ Главное меню", callback_data="main_menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    print(f"[events_view.py][_kb_list_nav][done] back_cb={back_cb} more_cb={more_cb} rows={len(rows)}")
    return kb

# ─────────────────────────────────────────────────────────
# Служебное: подчистить ТОЛЬКО хвосты поиска Афиши
# ─────────────────────────────────────────────────────────
async def _af_search_cleanup(chat_id: int, bot):
    print(f"[events_view.py][_af_search_cleanup][call] chat_id={chat_id}")

    # 1) удаляем nav/prompt
    for dct_name, dct in (("nav", af_search_nav_msg_id), ("prompt", af_search_prompt_msg_id)):
        mid = dct.pop(chat_id, None)
        if mid:
            try:
                await bot.delete_message(chat_id, mid)
            except Exception as e:
                print(f"[events_view.py][_af_search_cleanup][warn] delete {dct_name} mid={mid} err={e}")

    # 2) удаляем результаты (список)
    mids = af_search_results_msg_ids.pop(chat_id, [])
    for mid in mids:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception as e:
            print(f"[events_view.py][_af_search_cleanup][warn] delete results mid={mid} err={e}")

    print(f"[events_view.py][_af_search_cleanup][done] chat_id={chat_id}")



# ─────────────────────────────────────────────────────────
# Служебное: fuzzy-поиск событий Афиши (только будущие, published)
# ─────────────────────────────────────────────────────────
def _af_search_text_getter(item: dict) -> list[str]:
    return [
        str(item.get("title") or ""),
        str(item.get("descr") or ""),
        str(item.get("venue_text") or ""),
        str(item.get("city_name") or ""),
        str(item.get("city_text") or ""),
    ]

def _af_field_match_score(value: str, q: str) -> int:
    """
    RU: Оценка совпадения одного поля с запросом.
    Чем выше число, тем важнее совпадение.
    """
    if not value or not q:
        return 0

    v = normalize_search_text(value)
    if not v:
        return 0

    # точное совпадение всего поля
    if v == q:
        return 100

    # совпадение по словам
    tokens = v.split()
    if q in tokens:
        return 80

    # начало поля
    if v.startswith(q):
        return 60

    # начало одного из слов
    if any(tok.startswith(q) for tok in tokens):
        return 45

    # просто вхождение
    if q in v:
        return 25

    return 0


def _af_weighted_rank(item: dict, effective_query: str) -> tuple[int, int]:
    """
    RU: Взвешенный ранг результата для Афиши.
    1) Сначала релевантность по полям
    2) Потом дата события: чем раньше ближайшее событие, тем лучше
    """
    title = str(item.get("title") or "")
    descr = str(item.get("descr") or "")
    venue = str(item.get("venue_text") or "")
    city_name = str(item.get("city_name") or "")
    city_text = str(item.get("city_text") or "")

    title_score = _af_field_match_score(title, effective_query)
    venue_score = _af_field_match_score(venue, effective_query)
    city_name_score = _af_field_match_score(city_name, effective_query)
    city_text_score = _af_field_match_score(city_text, effective_query)
    descr_score = _af_field_match_score(descr, effective_query)

    # Веса полей:
    # title      — самый важный
    # venue      — очень важный
    # city       — средний
    # descr      — самый слабый
    weighted = (
        title_score * 1000
        + venue_score * 300
        + city_name_score * 120
        + city_text_score * 100
        + descr_score * 40
    )

    # Чем меньше start_at_utc, тем событие ближе и тем лучше
    start_at = int(item.get("start_at_utc") or 0)

    # Сортируем по:
    # 1) weighted DESC
    # 2) start_at ASC
    return (weighted, -start_at)



async def _fetch_search_event_candidates() -> list[dict]:
    now_utc = int(datetime.now(_TZ).astimezone(timezone.utc).timestamp())
    q = sql("""
        SELECT
            l.id AS listing_id,
            l.title,
            l.descr,
            COALESCE(c.name, em.city_text) AS city_name,
            em.city_text,
            em.venue_text,
            em.start_at_utc
        FROM listing l
        JOIN events_meta em ON em.listing_id = l.id
        LEFT JOIN city c ON c.id = l.city_id
        WHERE
            l.type = 'events'
            AND l.is_sold = 0
            AND em.status = 'published'
            AND em.start_at_utc >= :now_utc
        ORDER BY em.start_at_utc ASC
    """)
    async with SessionLocal() as s:
        res = await s.execute(q, {"now_utc": now_utc})
        out = [dict(r._mapping) for r in res.fetchall()]

    print(
        f"[events_view.py][_fetch_search_event_candidates][done] "
        f"now_utc={now_utc} count={len(out)}"
    )
    return out


async def _run_afisha_search(query: str) -> dict:
    q = (query or "").strip()
    candidates = await _fetch_search_event_candidates()
    outcome = search_items(candidates, q, _af_search_text_getter)

    ranked_results = list(outcome.results)

    if ranked_results and outcome.query_effective:
        effective_query = normalize_search_text(outcome.query_effective)

        ranked_results.sort(
            key=lambda item: _af_weighted_rank(item, effective_query),
            reverse=True,
        )

        # при равном weighted более ранняя дата должна быть выше,
        # поэтому делаем вторичную стабильную сортировку по дате ASC
        ranked_results.sort(
            key=lambda item: (
                -_af_weighted_rank(item, effective_query)[0],
                int(item.get("start_at_utc") or 0),
            )
        )

    result = {
        "query_raw": outcome.query_raw,
        "query_normalized": outcome.query_normalized,
        "query_effective": outcome.query_effective,
        "match_mode": outcome.match_mode,
        "results": ranked_results,
        "total": len(ranked_results),
    }

    print(
        f"[events_view.py][_run_afisha_search][done] "
        f"query_raw={result['query_raw']!r} "
        f"normalized={result['query_normalized']!r} "
        f"effective={result['query_effective']!r} "
        f"match_mode={result['match_mode']} total={result['total']}"
    )
    return result

async def _search_events(query: str, offset: int, limit: int):
    payload = await _run_afisha_search(query)
    items = payload["results"][int(offset): int(offset) + int(limit)]
    print(
        f"[events_view.py][_search_events][done] "
        f"q={(query or '').strip()!r} offset={offset} limit={limit} count={len(items)}"
    )
    return items


async def _count_search_events(query: str) -> int:
    payload = await _run_afisha_search(query)
    total = int(payload["total"])
    print(f"[events_view.py][_count_search_events][done] q={(query or '').strip()!r} total={total}")
    return total

@router.callback_query(F.data == "afisha")
async def afisha_entry(cb: CallbackQuery, state: FSMContext):
    print("[events_view.py] afisha_entry CALLED")
    chat_id = cb.message.chat.id
    try:
        await cb.message.delete()
    except Exception:
        pass

    await clear_bot_messages(chat_id, cb.bot)

    title = await get_text("events_choose_city", "ru") or "🗓 <b>Афиша —------------------------------</b>"
    kb = await _afisha_root_kb()

    msg = await cb.message.answer(title, parse_mode="HTML", reply_markup=kb)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    log("[events_view.py] afisha_entry | shown")
    await cb.answer()
    print(f"[events_view.py][afisha_entry][done] chat_id={chat_id} cb_data={cb.data} msg_id={msg.message_id}")


@router.callback_query(F.data == "af:root")
async def af_root(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    title = await get_text("events_choose_city", "ru") or "🗓 <b>Афиша —------------------------------</b>"
    kb = await _afisha_root_kb()
    msg = await cb.message.answer(title, parse_mode="HTML", reply_markup=kb)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    log("[events_view.py] af_root | shown")
    await cb.answer()
    print(f"[events_view.py][af_root][done] chat_id={chat_id} cb_data={cb.data} msg_id={msg.message_id}")


@router.callback_query(F.data == "events:near")
async def events_near(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    items = await _fetch_events(offset=0, limit=AFISHA_PAGE_SIZE)

    if not items:
        kb = _kb_back_and_main("go_events")
        msg = await cb.message.answer("Пока нет запланированных событий.", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(f"[events_view.py][events_near][done] chat_id={chat_id} cb_data={cb.data} empty=1 msg_id={msg.message_id}")
        return

    rows: list[list[InlineKeyboardButton]] = []
    for it in items:
        d, t = _fmt_dt(it["start_at_utc"])
        rows.append([InlineKeyboardButton(
            text=f"{d} {t} — {it['title']}",
            callback_data=f"af:open:{it['listing_id']}:near:0"
        )])

    total = await _count_events()
    pages = max(1, (total + AFISHA_PAGE_SIZE - 1) // AFISHA_PAGE_SIZE)
    page = 1

    pager = []
    pager.append(InlineKeyboardButton(text=f"{page}/{pages}", callback_data="af:cal:noop"))
    if page < pages:
        pager.append(InlineKeyboardButton(text="»", callback_data=f"events:near:more:{AFISHA_PAGE_SIZE}"))

    if pages > 1:
        rows.append(pager)

    nav = _kb_list_nav(back_cb="go_events", more_cb=None)
    kb = InlineKeyboardMarkup(inline_keyboard=rows + nav.inline_keyboard)

    msg = await cb.message.answer("🗓 <b>Ближайшие мероприятия</b>", parse_mode="HTML", reply_markup=kb)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    log(f"[events_view.py] events_near | count={len(items)}")
    await cb.answer()
    print(f"[events_view.py][events_near][done] chat_id={chat_id} cb_data={cb.data} count={len(items)} page=1/{pages} msg_id={msg.message_id}")




@router.callback_query(F.data.startswith("events:near:more:"))
async def events_near_more(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    try:
        await cb.message.delete()
    except Exception:
        pass

    try:
        offset = int(cb.data.rsplit(":", 1)[-1])
    except Exception:
        offset = 0

    items = await _fetch_events(offset=offset, limit=AFISHA_PAGE_SIZE)

    if not items:
        kb = _kb_back_and_main("events:near")
        msg = await cb.message.answer("Это всё на сейчас.", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(f"[events_view.py][events_near_more][done] chat_id={chat_id} cb_data={cb.data} offset={offset} empty=1 msg_id={msg.message_id}")
        return

    rows: list[list[InlineKeyboardButton]] = []
    for it in items:
        d, t = _fmt_dt(it["start_at_utc"])
        rows.append([InlineKeyboardButton(
            text=f"{d} {t} — {it['title']}",
            callback_data=f"af:open:{it['listing_id']}:near:{offset}"
        )])

    total = await _count_events()
    pages = max(1, (total + AFISHA_PAGE_SIZE - 1) // AFISHA_PAGE_SIZE)
    page = (offset // AFISHA_PAGE_SIZE) + 1

    pager = []
    if page > 1:
        prev_offset = max(0, offset - AFISHA_PAGE_SIZE)
        pager.append(InlineKeyboardButton(text="«", callback_data=f"events:near:more:{prev_offset}"))

    pager.append(InlineKeyboardButton(text=f"{page}/{pages}", callback_data="af:cal:noop"))

    if page < pages:
        next_offset = offset + AFISHA_PAGE_SIZE
        pager.append(InlineKeyboardButton(text="»", callback_data=f"events:near:more:{next_offset}"))

    if pages > 1:
        rows.append(pager)

    nav = _kb_list_nav(back_cb="events:near", more_cb=None)
    kb = InlineKeyboardMarkup(inline_keyboard=rows + nav.inline_keyboard)

    msg = await cb.message.answer("🗓 <b>Ближайшие мероприятия</b>", parse_mode="HTML", reply_markup=kb)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    log(f"[events_view.py] events_near_more | offset={offset} | count={len(items)}")
    await cb.answer()
    print(f"[events_view.py][events_near_more][done] chat_id={chat_id} cb_data={cb.data} offset={offset} count={len(items)} page={page}/{pages} msg_id={msg.message_id}")



    

@router.callback_query(F.data.startswith("af:open:"))
async def af_open_event(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    parts = cb.data.split(":")

    try:
        listing_id = int(parts[2])
    except Exception:
        await cb.answer("Не удалось открыть событие.")
        print(f"[events_view.py][af_open_event][done] chat_id={chat_id} cb_data={cb.data} parse_listing_id_failed=1")
        return

    context = parts[3] if len(parts) > 3 else "near"
    offset = parts[4] if len(parts) > 4 else "0"

    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    it = await _fetch_event_by_id(listing_id)
    if not it:
        kb = _kb_back_and_main("events:near")
        msg = await cb.message.answer("Событие не найдено.", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(f"[events_view.py][af_open_event][done] chat_id={chat_id} cb_data={cb.data} listing_id={listing_id} found=0 msg_id={msg.message_id}")
        return

    if context == "near":
        source = "near"
    elif context == "cal":
        source = "calendar"
    elif context == "calcity":
        source = "calendar_city"
    elif context == "city":
        source = "city_list"
    else:
        source = "other"

    # ЛОГ ОТКРЫТИЯ КАРТОЧКИ
    await log_listing_view(
        listing_id=listing_id,
        user_id=cb.from_user.id,
        section="events",
        action="open",
        source=source,
    )


    d, t = _fmt_dt(it["start_at_utc"])

    city_name = (it.get("city_name") or "").strip()
    city_text = (it.get("city_text") or "").strip()
    if city_text and (not city_name or "друг" in city_name.lower()):
        city = city_text
    else:
        city = city_name

    price = (it.get("price_text") or "").strip()
    descr = (it.get("descr") or "").strip()
    venue = (it.get("venue_text") or "").strip()
    contact = (it.get("contact") or "").strip()
    contact_url = _contact_url(contact) if contact else None

    lines = [
        f"Дата:   {d}",
        f"Время:  {t}",
    ]
    if city:
        lines.append(f"Город:  {city}")
    if venue:
        lines.append(f"Место:  {venue}")
    if price:
        lines.append(f"Цена:   {price}")
    if contact:
        lines.append(f"Контакт: {contact}")
    if descr:
        lines.append("")
        lines.append(descr)

    caption = f"<b>{it['title']}</b>\n\n" + "\n".join(lines)

    # ---- BACK LOGIC ----
    if context == "near":
        back_cb = "events:near" if offset == "0" else f"events:near:more:{offset}"

    elif context == "cal":
        # af:open:<id>:cal:<year>:<month>:<day>:<offset>
        try:
            y = int(parts[4])
            m = int(parts[5])
            day = int(parts[6])
            off = int(parts[7]) if len(parts) > 7 else 0
        except Exception:
            dt = datetime.now(_TZ)
            y, m, day, off = dt.year, dt.month, dt.day, 0
        back_cb = f"af:cal:day:all:{y}:{m}:{day}:{off}"

    elif context == "calcity":
        # af:open:<id>:calcity:<slug>:<year>:<month>:<day>:<offset>
        try:
            slug = parts[4]
            y = int(parts[5])
            m = int(parts[6])
            day = int(parts[7])
            off = int(parts[8]) if len(parts) > 8 else 0
        except Exception:
            slug = ""
            dt = datetime.now(_TZ)
            y, m, day, off = dt.year, dt.month, dt.day, 0
        back_cb = f"af:cal:day:city:{slug}:{y}:{m}:{day}:{off}"

    else:
        back_cb = "go_events"

    kb = _kb_back_and_main(back_cb)

    # кнопка "Связаться" — одной строкой, сверху
    if contact_url:
        try:
            kb.inline_keyboard.insert(0, [InlineKeyboardButton(text="📞 Связаться с контактом", url=contact_url)])
        except Exception:
            pass

    photo = it.get("photo_file_id")
    if photo:
        msg = await cb.message.answer_photo(photo=photo, caption=caption, parse_mode="HTML", reply_markup=kb)
    else:
        msg = await cb.message.answer(caption, parse_mode="HTML", reply_markup=kb)

    last_bot_messages[chat_id] = [msg.message_id]

    await register_bot_messages(chat_id, [msg.message_id])
    log(f"[events_view.py] af_open_event | id={listing_id}")
    await cb.answer()
    print(f"[events_view.py][af_open_event][done] chat_id={chat_id} cb_data={cb.data} listing_id={listing_id} context={context} back_cb={back_cb} msg_id={msg.message_id} has_photo={int(bool(photo))}")



@router.callback_query(F.data.startswith("af:ecity:"))
async def af_city_list(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    try:
        city_id = int(cb.data.split(":")[-1])
    except Exception:
        await cb.answer("Город не распознан.")
        print(f"[events_view.py][af_city_list][done] chat_id={chat_id} cb_data={cb.data} parse_city_id_failed=1")
        return

    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    items = await _fetch_events(offset=0, limit=AFISHA_PAGE_SIZE, city_id=city_id)

    if not items:
        kb = _kb_back_and_main("af:root")
        msg = await cb.message.answer("В этом городе пока нет ближайших событий.", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(f"[events_view.py][af_city_list][done] chat_id={chat_id} cb_data={cb.data} city_id={city_id} empty=1 msg_id={msg.message_id}")
        return

    rows: list[list[InlineKeyboardButton]] = []
    for it in items:
        d, t = _fmt_dt(it["start_at_utc"])
        rows.append([InlineKeyboardButton(
            text=f"{d} {t} — {it['title']}",
            callback_data=f"af:open:{it['listing_id']}:city:{city_id}"
        )])

    has_more = len(items) == AFISHA_PAGE_SIZE
    more_cb = f"af:city:more:{city_id}:{AFISHA_PAGE_SIZE}" if has_more else None
    nav = _kb_list_nav(back_cb="go_events", more_cb=more_cb)
    kb = InlineKeyboardMarkup(inline_keyboard=rows + nav.inline_keyboard)

    msg = await cb.message.answer("🗓 <b>События по городу</b>", parse_mode="HTML", reply_markup=kb)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    log(f"[events_view.py] af_city_list | city_id={city_id} | count={len(items)}")
    await cb.answer()
    print(f"[events_view.py][af_city_list][done] chat_id={chat_id} cb_data={cb.data} city_id={city_id} count={len(items)} has_more={int(has_more)} msg_id={msg.message_id} more_cb={more_cb}")


@router.callback_query(F.data.startswith("af:city:more:"))
async def af_city_more(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    parts = cb.data.split(":")
    try:
        city_id = int(parts[3])
        offset = int(parts[4])
    except Exception:
        city_id, offset = 0, 0

    try:
        await cb.message.delete()
    except Exception:
        pass

    items = await _fetch_events(offset=offset, limit=AFISHA_PAGE_SIZE, city_id=city_id)
    if not items:
        kb = _kb_back_and_main(f"af:ecity:{city_id}")
        msg = await cb.message.answer("Это всё на сейчас.", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(f"[events_view.py][af_city_more][done] chat_id={chat_id} cb_data={cb.data} city_id={city_id} offset={offset} empty=1 msg_id={msg.message_id}")
        return

    rows: list[list[InlineKeyboardButton]] = []
    for it in items:
        d, t = _fmt_dt(it["start_at_utc"])
        rows.append([InlineKeyboardButton(
            text=f"{d} {t} — {it['title']}",
            callback_data=f"af:open:{it['listing_id']}:city:{city_id}"
        )])

    has_more = len(items) == AFISHA_PAGE_SIZE
    more_cb = f"af:city:more:{city_id}:{offset + AFISHA_PAGE_SIZE}" if has_more else None
    nav = _kb_list_nav(back_cb=f"af:ecity:{city_id}", more_cb=more_cb)
    kb = InlineKeyboardMarkup(inline_keyboard=rows + nav.inline_keyboard)

    msg = await cb.message.answer("🗓 <b>События по городу</b>", parse_mode="HTML", reply_markup=kb)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    log(f"[events_view.py] af_city_more | city_id={city_id} | offset={offset} | count={len(items)}")
    await cb.answer()
    print(f"[events_view.py][af_city_more][done] chat_id={chat_id} cb_data={cb.data} city_id={city_id} offset={offset} count={len(items)} has_more={int(has_more)} msg_id={msg.message_id} more_cb={more_cb}")


@router.callback_query(F.data.startswith("ecity:"))
async def ecity_city_list(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    slug = cb.data.split(":", 1)[-1].strip()

    print(f"[events_view.py][ecity_city_list][call] chat_id={chat_id} cb_data={cb.data} slug={slug}")

    # 0) удалить сообщение, по которому нажали
    try:
        await cb.message.delete()
    except Exception:
        pass

    # 1) подчистить служебные сообщения
    await clear_bot_messages(chat_id, cb.bot)

    # 2) slug -> id (БЕЗ хардкода)
    city_id = None
    try:
        async with SessionLocal() as s:
            city_id = None
            city_name = ""

            res = await s.execute(
                sql("SELECT id, name FROM city WHERE slug = :slug LIMIT 1"),
                {"slug": slug},
            )
            row = res.first()
            if row:
                city_id = int(row[0])
                city_name = (row[1] or "").strip()
    except Exception as e:
        print(f"[events_view.py][ecity_city_list][db_error] chat_id={chat_id} slug={slug} err={e!r}")

    if not city_id:
        await cb.answer("Город не найден.")
        print(f"[events_view.py][ecity_city_list][done] chat_id={chat_id} slug={slug} city_id=None")
        return

    # 3) дальше — как af_city_list, но back -> go_events
    items = await _fetch_events(offset=0, limit=AFISHA_PAGE_SIZE, city_id=city_id)

    if not items:
        kb = _kb_back_and_main("go_events")
        msg = await cb.message.answer("В этом городе пока нет ближайших событий.", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(f"[events_view.py][ecity_city_list][done] chat_id={chat_id} slug={slug} city_id={city_id} empty=1 msg_id={msg.message_id}")
        return

    rows: list[list[InlineKeyboardButton]] = []

    btn_city = city_name or slug
    rows.append([InlineKeyboardButton(
        text=f"🗓 Календарь — {btn_city}",
        callback_data=f"af:cal:city:{slug}"
    )])

    for it in items:
        d, t = _fmt_dt(it["start_at_utc"])
        rows.append([InlineKeyboardButton(
            text=f"{d} {t} — {it['title']}",
            callback_data=f"af:open:{it['listing_id']}:city:{city_id}"
        )])


    has_more = len(items) == AFISHA_PAGE_SIZE
    more_cb = f"af:city:more:{city_id}:{AFISHA_PAGE_SIZE}" if has_more else None

    nav = _kb_list_nav(back_cb="go_events", more_cb=more_cb)
    kb = InlineKeyboardMarkup(inline_keyboard=rows + nav.inline_keyboard)

    msg = await cb.message.answer("🗓 <b>События по городу</b>", parse_mode="HTML", reply_markup=kb)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()

    print(f"[events_view.py][ecity_city_list][done] chat_id={chat_id} slug={slug} city_id={city_id} count={len(items)} has_more={int(has_more)} msg_id={msg.message_id} more_cb={more_cb}")


# ─────────────────────────────────────────────────────────
# Афиша: 🔎 Поиск — вход
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data == "af:search")
async def af_search_entry(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    print(f"[events_view.py][af_search_entry][call] chat_id={chat_id} cb_data={cb.data}")

    # удаляем сообщение, по которому нажали
    try:
        await cb.message.delete()
    except Exception:
        pass

    # чистим общие хвосты бота + хвосты поиска
    await clear_bot_messages(chat_id, cb.bot)
    await _af_search_cleanup(chat_id, cb.bot)

    # ставим состояние ожидания запроса
    await state.set_state(AfishaSearchStates.wait_query)

    # навигация
    nav_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="go_events")],
        [InlineKeyboardButton(text="≡ Главное меню", callback_data="main_menu")],
    ])
    nav_msg = await cb.bot.send_message(chat_id, "Навигация:", reply_markup=nav_kb)
    af_search_nav_msg_id[chat_id] = nav_msg.message_id

    # подсказка
    prompt_msg = await cb.bot.send_message(
        chat_id,
        "🔎 Введите поисковый запрос по Афише (название/описание/город/площадка):"
    )
    af_search_prompt_msg_id[chat_id] = prompt_msg.message_id

    # чтобы cleanup_router не снёс эти сообщения — кладём их в общий кэш
    last_bot_messages[chat_id] = [nav_msg.message_id, prompt_msg.message_id]
    await register_bot_messages(chat_id, [nav_msg.message_id, prompt_msg.message_id])
    await cb.answer()
    print(f"[events_view.py][af_search_entry][done] chat_id={chat_id} nav_id={nav_msg.message_id} prompt_id={prompt_msg.message_id}")


# ─────────────────────────────────────────────────────────
# Афиша: 🔎 Поиск — пользователь ввёл запрос
# ─────────────────────────────────────────────────────────
@router.message(StateFilter(AfishaSearchStates.wait_query))
async def af_search_query(m: Message, state: FSMContext):
    chat_id = m.chat.id
    q = (m.text or "").strip()
    print(f"[events_view.py][af_search_query][call] chat_id={chat_id} q={q!r}")

    # пользовательское сообщение — по канону удаляем
    try:
        await m.bot.delete_message(chat_id, m.message_id)
    except Exception:
        pass

    # удаляем nav/prompt поиска, чтобы не было дубля кнопок
    nav_mid = af_search_nav_msg_id.pop(chat_id, None)
    if nav_mid:
        try:
            await m.bot.delete_message(chat_id, nav_mid)
        except Exception:
            pass

    prompt_mid = af_search_prompt_msg_id.pop(chat_id, None)
    if prompt_mid:
        try:
            await m.bot.delete_message(chat_id, prompt_mid)
        except Exception:
            pass

    # подчистим прошлые результаты поиска
    mids = af_search_results_msg_ids.get(chat_id, [])
    for mid in mids:
        try:
            await m.bot.delete_message(chat_id, mid)
        except Exception:
            pass
    af_search_results_msg_ids[chat_id] = []

    # подчистим общий кэш от уже удалённых nav/prompt/results
    old_ids = last_bot_messages.get(chat_id, [])
    ids_to_remove = set()
    if nav_mid:
        ids_to_remove.add(nav_mid)
    if prompt_mid:
        ids_to_remove.add(prompt_mid)
    ids_to_remove.update(mids)

    if old_ids:
        last_bot_messages[chat_id] = [mid for mid in old_ids if mid not in ids_to_remove]

    if not q:
        rows = [
            [InlineKeyboardButton(text="🔄 Новый поиск", callback_data="af:search")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="go_events")],
            [InlineKeyboardButton(text="≡ Главное меню", callback_data="main_menu")],
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=rows)

        msg = await m.bot.send_message(
            chat_id,
            "Пустой запрос. Введите текст для поиска:",
            reply_markup=kb
        )
        af_search_results_msg_ids[chat_id] = [msg.message_id]
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])
        # остаёмся в режиме ожидания нового текста
        await state.set_state(AfishaSearchStates.wait_query)

        print(f"[events_view.py][af_search_query][done] chat_id={chat_id} empty_query=1 msg_id={msg.message_id}")
        return

    # сохраняем контекст (и в FSM, и в кэше по чату)
    await state.update_data(af_search_query=q)
    af_search_ctx_by_chat[chat_id] = {"q": q}

    # ищем первую страницу
    items = await _search_events(q, offset=0, limit=AFISHA_SEARCH_PAGE_SIZE)
    total = await _count_search_events(q)

    # ЛОГИРОВАНИЕ ПОИСКА
    await log_search(
        user_id=m.from_user.id,
        section="events",
        query_raw=q,
        query_normalized=q,
        query_effective=q,
        match_mode="partial" if total > 0 else "none",
        results_count=total,
    )

    if not items:
        rows = [
            [InlineKeyboardButton(text="🔄 Новый поиск", callback_data="af:search")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="af:search")],
            [InlineKeyboardButton(text="≡ Главное меню", callback_data="main_menu")],
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=rows)

        msg = await m.bot.send_message(
            chat_id,
            f"Ничего не найдено по запросу: <b>{q}</b>",
            parse_mode="HTML",
            reply_markup=kb
        )
        af_search_results_msg_ids[chat_id] = [msg.message_id]
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])
        # остаёмся в режиме ожидания нового текста
        await state.set_state(AfishaSearchStates.wait_query)

        print(f"[events_view.py][af_search_query][done] chat_id={chat_id} found=0 msg_id={msg.message_id}")
        return

    rows: list[list[InlineKeyboardButton]] = []
    for it in items:
        d, t = _fmt_dt(it["start_at_utc"])
        rows.append([InlineKeyboardButton(
            text=f"{d} {t} — {it['title']}",
            callback_data=f"af:search:open:{it['listing_id']}:0"
        )])

    pages = max(1, (total + AFISHA_SEARCH_PAGE_SIZE - 1) // AFISHA_SEARCH_PAGE_SIZE)
    page = 1

    pager = []
    pager.append(
        InlineKeyboardButton(text=f"{page}/{pages}", callback_data="af:cal:noop")
    )

    if page < pages:
        pager.append(
            InlineKeyboardButton(text="»", callback_data=f"af:search:more:{AFISHA_SEARCH_PAGE_SIZE}")
        )

    if pages > 1:
        rows.append(pager)

    rows.append([InlineKeyboardButton(text="🔄 Новый поиск", callback_data="af:search")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="af:search")])
    rows.append([InlineKeyboardButton(text="≡ Главное меню", callback_data="main_menu")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    msg = await m.bot.send_message(
        chat_id,
        f"🔎 Результаты поиска: <b>{q}</b>",
        parse_mode="HTML",
        reply_markup=kb
    )

    af_search_results_msg_ids[chat_id] = [msg.message_id]
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    # остаёмся в режиме ожидания нового текста
    await state.set_state(AfishaSearchStates.wait_query)

    print(
        f"[events_view.py][af_search_query][done] "
        f"chat_id={chat_id} found={len(items)} total={total} page=1/{pages} msg_id={msg.message_id}"
    )


    

@router.callback_query(F.data == "af:search:results:first")
async def af_search_results_first(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    print(f"[events_view.py][af_search_results_first][call] chat_id={chat_id}")

    try:
        await cb.message.delete()
    except Exception:
        pass

    data = await state.get_data()
    q = (data.get("af_search_query") or "").strip()
    if not q:
        q = ((af_search_ctx_by_chat.get(chat_id) or {}).get("q") or "").strip()

    if not q:
        msg = await cb.message.answer(
            "Контекст поиска потерян. Нажмите «🔎 Поиск» заново.",
            reply_markup=_kb_back_and_main("af:root")
        )
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(f"[events_view.py][af_search_results_first][done] chat_id={chat_id} no_ctx=1 msg_id={msg.message_id}")
        return

    mids = af_search_results_msg_ids.get(chat_id, [])
    for mid in mids:
        try:
            await cb.bot.delete_message(chat_id, mid)
        except Exception:
            pass
    af_search_results_msg_ids[chat_id] = []

    payload = await _run_afisha_search(q)
    items = payload["results"][:AFISHA_SEARCH_PAGE_SIZE]
    total = int(payload["total"])
    pages = max(1, (total + AFISHA_SEARCH_PAGE_SIZE - 1) // AFISHA_SEARCH_PAGE_SIZE)

    await state.update_data(
        af_search_query=payload["query_raw"],
        af_search_query_normalized=payload["query_normalized"],
        af_search_query_effective=payload["query_effective"],
        af_search_match_mode=payload["match_mode"],
    )
    af_search_ctx_by_chat[chat_id] = {
        "q": payload["query_raw"],
        "query_normalized": payload["query_normalized"],
        "query_effective": payload["query_effective"],
        "match_mode": payload["match_mode"],
    }

    if not items:
        msg = await cb.message.answer(
            f"Ничего не найдено по запросу: <b>{payload['query_raw']}</b>",
            parse_mode="HTML",
            reply_markup=_kb_back_and_main("af:search")
        )
        af_search_results_msg_ids[chat_id] = [msg.message_id]
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(f"[events_view.py][af_search_results_first][done] chat_id={chat_id} found=0 msg_id={msg.message_id}")
        return

    rows: list[list[InlineKeyboardButton]] = []
    for it in items:
        d, t = _fmt_dt(it["start_at_utc"])
        rows.append([InlineKeyboardButton(
            text=f"{d} {t} — {it['title']}",
            callback_data=f"af:search:open:{it['listing_id']}:0"
        )])

    correction_note = ""
    if payload["match_mode"] == "corrected" and payload["query_effective"] != payload["query_normalized"]:
        correction_note = (
            f"🧠 Показаны результаты по запросу: "
            f"<b>{payload['query_effective']}</b> "
            f"(учтена возможная опечатка).\n\n"
        )

    pager = [InlineKeyboardButton(text=f"1/{pages}", callback_data="af:cal:noop")]
    if pages > 1:
        pager.append(InlineKeyboardButton(text="»", callback_data=f"af:search:more:{AFISHA_SEARCH_PAGE_SIZE}"))
        rows.append(pager)

    rows.append([InlineKeyboardButton(text="🔄 Новый поиск", callback_data="af:search")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="af:search")])
    rows.append([InlineKeyboardButton(text="≡ Главное меню", callback_data="main_menu")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    msg = await cb.message.answer(
        f"{correction_note}🔎 Результаты поиска: <b>{payload['query_raw']}</b>",
        parse_mode="HTML",
        reply_markup=kb
    )

    af_search_results_msg_ids[chat_id] = [msg.message_id]
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(
        f"[events_view.py][af_search_results_first][done] chat_id={chat_id} found={len(items)} "
        f"total={total} match_mode={payload['match_mode']} effective={payload['query_effective']!r} msg_id={msg.message_id}"
    )


    

# ─────────────────────────────────────────────────────────
# Афиша: 🔎 Поиск — показать ещё
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("af:search:more:"))
async def af_search_more(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    print(f"[events_view.py][af_search_more][call] chat_id={chat_id} cb_data={cb.data}")

    try:
        await cb.message.delete()
    except Exception:
        pass

    try:
        offset = int(cb.data.rsplit(":", 1)[-1])
    except Exception:
        offset = 0

    data = await state.get_data()
    q = (data.get("af_search_query") or "")
    if not q:
        q = (af_search_ctx_by_chat.get(chat_id) or {}).get("q", "")

    if not q:
        msg = await cb.message.answer("Контекст поиска потерян. Нажмите «🔎 Поиск» заново.", reply_markup=_kb_back_and_main("af:root"))
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(f"[events_view.py][af_search_more][done] chat_id={chat_id} NO_CTX msg_id={msg.message_id}")
        return

    mids = af_search_results_msg_ids.get(chat_id, [])
    for mid in mids:
        try:
            await cb.bot.delete_message(chat_id, mid)
        except Exception:
            pass
    af_search_results_msg_ids[chat_id] = []

    payload = await _run_afisha_search(q)
    total = int(payload["total"])
    items = payload["results"][offset: offset + AFISHA_SEARCH_PAGE_SIZE]

    await state.update_data(
        af_search_query=payload["query_raw"],
        af_search_query_normalized=payload["query_normalized"],
        af_search_query_effective=payload["query_effective"],
        af_search_match_mode=payload["match_mode"],
    )
    af_search_ctx_by_chat[chat_id] = {
        "q": payload["query_raw"],
        "query_normalized": payload["query_normalized"],
        "query_effective": payload["query_effective"],
        "match_mode": payload["match_mode"],
    }

    if not items:
        msg = await cb.message.answer("Это всё по вашему запросу.", reply_markup=_kb_back_and_main("af:search"))
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(f"[events_view.py][af_search_more][done] chat_id={chat_id} offset={offset} empty=1 msg_id={msg.message_id}")
        return

    rows: list[list[InlineKeyboardButton]] = []
    for it in items:
        d, t = _fmt_dt(it["start_at_utc"])
        rows.append([InlineKeyboardButton(
            text=f"{d} {t} — {it['title']}",
            callback_data=f"af:search:open:{it['listing_id']}:{offset}"
        )])

    pages = max(1, (total + AFISHA_SEARCH_PAGE_SIZE - 1) // AFISHA_SEARCH_PAGE_SIZE)
    page = (offset // AFISHA_SEARCH_PAGE_SIZE) + 1

    correction_note = ""
    if payload["match_mode"] == "corrected" and payload["query_effective"] != payload["query_normalized"]:
        correction_note = (
            f"🧠 Показаны результаты по запросу: "
            f"<b>{payload['query_effective']}</b> "
            f"(учтена возможная опечатка).\n\n"
        )

    pager = []
    if page > 1:
        prev_offset = max(0, offset - AFISHA_SEARCH_PAGE_SIZE)
        pager.append(InlineKeyboardButton(text="«", callback_data=f"af:search:more:{prev_offset}"))
    pager.append(InlineKeyboardButton(text=f"{page}/{pages}", callback_data="af:cal:noop"))
    if page < pages:
        next_offset = offset + AFISHA_SEARCH_PAGE_SIZE
        pager.append(InlineKeyboardButton(text="»", callback_data=f"af:search:more:{next_offset}"))
    rows.append(pager)

    rows.append([InlineKeyboardButton(text="🔄 Новый поиск", callback_data="af:search")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="af:search")])
    rows.append([InlineKeyboardButton(text="≡ Главное меню", callback_data="main_menu")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    msg = await cb.message.answer(
        f"{correction_note}🔎 Результаты поиска: <b>{payload['query_raw']}</b>",
        parse_mode="HTML",
        reply_markup=kb,
    )

    af_search_results_msg_ids[chat_id] = [msg.message_id]
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(
        f"[events_view.py][af_search_more][done] chat_id={chat_id} offset={offset} count={len(items)} "
        f"page={page}/{pages} match_mode={payload['match_mode']} effective={payload['query_effective']!r} msg_id={msg.message_id}"
    )

# ─────────────────────────────────────────────────────────
# Афиша: 🔎 Поиск — открыть карточку события
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("af:search:open:"))
async def af_search_open(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    print(f"[events_view.py][af_search_open][call] chat_id={chat_id} cb_data={cb.data}")

    parts = cb.data.split(":")
    try:
        listing_id = int(parts[3])
        offset = parts[4] if len(parts) > 4 else "0"
    except Exception:
        await cb.answer("Не удалось открыть карточку.")
        print(f"[events_view.py][af_search_open][done] chat_id={chat_id} parse_failed=1")
        return

    try:
        await cb.message.delete()
    except Exception:
        pass

    await clear_bot_messages(chat_id, cb.bot)

    it = await _fetch_event_by_id(listing_id)
    if not it:
        msg = await cb.message.answer("Событие не найдено.", reply_markup=_kb_back_and_main("af:search"))
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(f"[events_view.py][af_search_open][done] chat_id={chat_id} listing_id={listing_id} found=0 msg_id={msg.message_id}")
        return

    # ЛОГ ОТКРЫТИЯ КАРТОЧКИ ИЗ ПОИСКА
    await log_listing_view(
        listing_id=listing_id,
        user_id=cb.from_user.id,
        section="events",
        action="open",
        source="search",
    )


    d, t = _fmt_dt(it["start_at_utc"])
    city = it.get("city_name") or ""
    price = it.get("price_text") or ""
    descr = it.get("descr") or ""
    venue = it.get("venue_text") or ""

    lines = [
        f"Дата:   {d}",
        f"Время:  {t}",
    ]
    if city:
        lines.append(f"Город:  {city}")
    if venue:
        lines.append(f"Место:  {venue}")
    if price:
        lines.append(f"Цена:   {price}")
    if descr:
        lines.append("")
        lines.append(descr)

    caption = f"<b>{it['title']}</b>\n\n" + "\n".join(lines)

    back_cb = f"af:search:more:{offset}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад к результатам", callback_data=back_cb)],
        [InlineKeyboardButton(text="≡ Главное меню", callback_data="main_menu")],
    ])

    photo = it.get("photo_file_id")
    if photo:
        msg = await cb.message.answer_photo(photo=photo, caption=caption, parse_mode="HTML", reply_markup=kb)
    else:
        msg = await cb.message.answer(caption, parse_mode="HTML", reply_markup=kb)

    last_bot_messages[chat_id] = [msg.message_id]

    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"[events_view.py][af_search_open][done] chat_id={chat_id} listing_id={listing_id} back_cb={back_cb} msg_id={msg.message_id}")


@router.callback_query(F.data.startswith("af:cal:all"))
async def af_cal_all(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    await cb.answer()

    parts = cb.data.split(":")
    # варианты: "af:cal:all" или "af:cal:all:YYYY:MM"
    if len(parts) >= 5:
        try:
            year = int(parts[3])
            month = int(parts[4])
        except Exception:
            dt = datetime.now(_TZ)
            year, month = dt.year, dt.month
    else:
        dt = datetime.now(_TZ)
        year, month = dt.year, dt.month

    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    marks = await _fetch_month_marks_all(year, month)
    kb = _kb_calendar_month_all(year, month, marks)

    msg = await cb.message.answer(
        "🗓 Выберите дату, чтобы посмотреть события.",
        parse_mode="HTML",
        reply_markup=kb,
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    print(f"[events_view.py][af_cal_all][done] chat_id={chat_id} y={year} m={month} msg_id={msg.message_id}")

@router.callback_query(F.data.startswith("af:cal:day:all:"))
async def af_cal_day_all(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    await cb.answer()

    parts = cb.data.split(":")
    # af:cal:day:all:Y:M:D:offset
    try:
        year = int(parts[4]); month = int(parts[5]); day = int(parts[6])
        offset = int(parts[7]) if len(parts) > 7 else 0
    except Exception:
        print(f"[events_view.py][af_cal_day_all][bad_cb] chat_id={chat_id} cb_data={cb.data!r}")
        return

    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    start_local, end_local = _day_bounds_local(year, month, day)
    start_utc = int(start_local.astimezone(timezone.utc).timestamp())
    end_utc = int(end_local.astimezone(timezone.utc).timestamp())

    q = sql("""
        SELECT
            l.id AS listing_id,
            l.title,
            em.start_at_utc
        FROM listing l
        JOIN events_meta em ON em.listing_id = l.id
        WHERE
            l.type='events'
            AND l.is_sold=0
            AND em.status='published'
            AND em.start_at_utc >= :start_utc
            AND em.start_at_utc <  :end_utc
        ORDER BY em.start_at_utc ASC
        LIMIT :limit OFFSET :offset
    """)

    async with SessionLocal() as s:
        res = await s.execute(q, {
            "start_utc": start_utc,
            "end_utc": end_utc,
            "limit": AFISHA_PAGE_SIZE,
            "offset": offset,
        })
        items = [dict(r._mapping) for r in res.fetchall()]

    if not items:
        kb = _kb_back_and_main(f"af:cal:all:{year}:{month}")
        msg = await cb.message.answer("На выбранную дату событий нет.", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        print(f"[events_view.py][af_cal_day_all][done] chat_id={chat_id} y={year} m={month} d={day} empty=1 msg_id={msg.message_id}")
        return

    rows: list[list[InlineKeyboardButton]] = []
    for it in items:
        d_str, t_str = _fmt_dt(it["start_at_utc"])
        rows.append([InlineKeyboardButton(
            text=f"{t_str} — {it['title']}",
            callback_data=f"af:open:{it['listing_id']}:cal:{year}:{month}:{day}:{offset}"
        )])

    # пагинация “ещё”
    has_more = len(items) == AFISHA_PAGE_SIZE
    more_cb = f"af:cal:day:all:{year}:{month}:{day}:{offset + AFISHA_PAGE_SIZE}" if has_more else None
    nav = _kb_list_nav(back_cb=f"af:cal:all:{year}:{month}", more_cb=more_cb)

    kb = InlineKeyboardMarkup(inline_keyboard=rows + nav.inline_keyboard)

    msg = await cb.message.answer(
        f"🗓 <b>События на {day:02d}.{month:02d}.{str(year)[-2:]}</b>",
        parse_mode="HTML",
        reply_markup=kb,
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    print(f"[events_view.py][af_cal_day_all][done] chat_id={chat_id} y={year} m={month} d={day} count={len(items)} has_more={int(has_more)} msg_id={msg.message_id}")



@router.callback_query(F.data.startswith("af:cal:city:"))
async def af_cal_city(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    await cb.answer()

    parts = cb.data.split(":")
    # варианты:
    # af:cal:city:<slug>
    # af:cal:city:<slug>:YYYY:MM
    slug = parts[3] if len(parts) > 3 else ""
    if len(parts) >= 6:
        try:
            year = int(parts[4]); month = int(parts[5])
        except Exception:
            dt = datetime.now(_TZ); year, month = dt.year, dt.month
    else:
        dt = datetime.now(_TZ); year, month = dt.year, dt.month

    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    marks = await _fetch_month_marks_city(slug, year, month)
    kb = _kb_calendar_month_city(slug, year, month, marks)

    msg = await cb.message.answer(
        "🗓 Выберите дату, чтобы посмотреть события.",
        parse_mode="HTML",
        reply_markup=kb,
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    print(f"[events_view.py][af_cal_city][done] chat_id={chat_id} slug={slug} y={year} m={month} msg_id={msg.message_id}")

@router.callback_query(F.data.startswith("af:cal:day:city:"))
async def af_cal_day_city(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    await cb.answer()

    parts = cb.data.split(":")
    # af:cal:day:city:<slug>:Y:M:D:offset
    try:
        slug = parts[4]
        year = int(parts[5]); month = int(parts[6]); day = int(parts[7])
        offset = int(parts[8]) if len(parts) > 8 else 0
    except Exception:
        print(f"[events_view.py][af_cal_day_city][bad_cb] chat_id={chat_id} cb_data={cb.data!r}")
        return

    city_id = await _city_id_by_slug(slug)
    if not city_id:
        kb = _kb_back_and_main(f"ecity:{slug}")
        msg = await cb.message.answer("Город не найден.", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        return

    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    start_local, end_local = _day_bounds_local(year, month, day)
    start_utc = int(start_local.astimezone(timezone.utc).timestamp())
    end_utc = int(end_local.astimezone(timezone.utc).timestamp())

    q = sql("""
        SELECT
            l.id AS listing_id,
            l.title,
            em.start_at_utc
        FROM listing l
        JOIN events_meta em ON em.listing_id = l.id
        WHERE
            l.type='events'
            AND l.is_sold=0
            AND em.status='published'
            AND l.city_id = :city_id
            AND em.start_at_utc >= :start_utc
            AND em.start_at_utc <  :end_utc
        ORDER BY em.start_at_utc ASC
        LIMIT :limit OFFSET :offset
    """)

    async with SessionLocal() as s:
        res = await s.execute(q, {
            "city_id": city_id,
            "start_utc": start_utc,
            "end_utc": end_utc,
            "limit": AFISHA_PAGE_SIZE,
            "offset": offset,
        })
        items = [dict(r._mapping) for r in res.fetchall()]

    if not items:
        kb = _kb_back_and_main(f"af:cal:city:{slug}:{year}:{month}")
        msg = await cb.message.answer("На выбранную дату событий нет.", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        return

    rows: list[list[InlineKeyboardButton]] = []
    for it in items:
        _, t_str = _fmt_dt(it["start_at_utc"])
        rows.append([InlineKeyboardButton(
            text=f"{t_str} — {it['title']}",
            callback_data=f"af:open:{it['listing_id']}:calcity:{slug}:{year}:{month}:{day}:{offset}"
        )])

    has_more = len(items) == AFISHA_PAGE_SIZE
    more_cb = f"af:cal:day:city:{slug}:{year}:{month}:{day}:{offset + AFISHA_PAGE_SIZE}" if has_more else None
    nav = _kb_list_nav(back_cb=f"af:cal:city:{slug}:{year}:{month}", more_cb=more_cb)

    kb = InlineKeyboardMarkup(inline_keyboard=rows + nav.inline_keyboard)

    msg = await cb.message.answer(
        f"🗓 <b>События на {day:02d}.{month:02d}.{str(year)[-2:]} (город)</b>",
        parse_mode="HTML",
        reply_markup=kb,
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
@router.callback_query(F.data == "af:cal:noop")
async def af_cal_noop(cb: CallbackQuery):
    await cb.answer(cache_time=60)


async def _city_id_by_slug(slug: str) -> int | None:
    slug = (slug or "").strip()
    if not slug:
        return None
    try:
        async with SessionLocal() as s:
            res = await s.execute(
                sql("SELECT id FROM city WHERE slug = :slug LIMIT 1"),
                {"slug": slug},
            )
            row = res.first()
            return int(row[0]) if row else None
    except Exception as e:
        print(f"[events_view.py][_city_id_by_slug][err] slug={slug!r} {type(e).__name__}: {e}")
        return None    
    
async def _fetch_month_marks_city(slug: str, year: int, month: int) -> set[int]:
    city_id = await _city_id_by_slug(slug)
    if not city_id:
        return set()

    start_local, end_local = _month_bounds_local(year, month)
    start_utc = int(start_local.astimezone(timezone.utc).timestamp())
    end_utc = int(end_local.astimezone(timezone.utc).timestamp())

    now_utc = int(datetime.now(_TZ).astimezone(timezone.utc).timestamp())
    start_utc = max(start_utc, now_utc)

    q = sql("""
        SELECT em.start_at_utc
        FROM listing l
        JOIN events_meta em ON em.listing_id = l.id
        WHERE
            l.type='events'
            AND l.is_sold=0
            AND em.status='published'
            AND l.city_id = :city_id
            AND em.start_at_utc >= :start_utc
            AND em.start_at_utc <  :end_utc
        ORDER BY em.start_at_utc ASC
    """)
    async with SessionLocal() as s:
        res = await s.execute(q, {"city_id": city_id, "start_utc": start_utc, "end_utc": end_utc})
        rows = res.fetchall()

    days: set[int] = set()
    for (ts,) in rows:
        try:
            dt_local = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(_TZ)
            if dt_local.year == year and dt_local.month == month:
                days.add(int(dt_local.day))
        except Exception:
            pass

    print(f"[events_view.py][_fetch_month_marks_city][done] slug={slug} y={year} m={month} count={len(days)}")
    return days

def _kb_calendar_month_city(slug: str, year: int, month: int, marks: set[int]) -> InlineKeyboardMarkup:
    weeks = pycal.monthcalendar(year, month)
    wd = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    month_name = [
        "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
        "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"
    ][month - 1]

    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)

    rows: list[list[InlineKeyboardButton]] = []

    # месяц и год — отдельной кнопкой
    rows.append([
        InlineKeyboardButton(
            text=f"{month_name} {year}",
            callback_data="af:cal:noop"
        )
    ])

    # стрелки — отдельной строкой
    rows.append([
        InlineKeyboardButton(text="«", callback_data=f"af:cal:city:{slug}:{prev_y}:{prev_m}"),
        InlineKeyboardButton(text="»", callback_data=f"af:cal:city:{slug}:{next_y}:{next_m}")
    ])

    # дни недели
    rows.append([InlineKeyboardButton(text=x, callback_data="af:cal:noop") for x in wd])

    # дни месяца
    for w in weeks:
        row: list[InlineKeyboardButton] = []
        for d in w:
            if d == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="af:cal:noop"))
            else:
                label = f"•{d}" if d in marks else str(d)
                row.append(InlineKeyboardButton(
                    text=label,
                    callback_data=f"af:cal:day:city:{slug}:{year}:{month}:{d}:0"
                ))
        rows.append(row)

    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"ecity:{slug}")])
    rows.append([InlineKeyboardButton(text="≡ Главное меню", callback_data="main_menu")])

    return InlineKeyboardMarkup(inline_keyboard=rows)
