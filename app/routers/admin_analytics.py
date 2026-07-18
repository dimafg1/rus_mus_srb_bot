# -*- coding: utf-8 -*-
"""
app/routers/admin_analytics.py

Админская аналитика.
Только чтение существующих таблиц:
- search_log
- listing_views
- listing

Новых таблиц, миграций, contact-логирования и фоновых задач здесь нет.
"""

from __future__ import annotations

from datetime import date, timedelta
from html import escape

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import text as sql

from app.database import SessionLocal
from app.routers.admin_panel import is_admin
from app.routers.utils import clear_bot_messages, last_bot_messages


router = Router()


PAGE_LIMIT = 10
OWNERS_PAGE_LIMIT = 5
OWNER_LISTINGS_PAGE_LIMIT = 7


async def _send_admin_analytics_message(cb: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Единый вывод сообщений аналитики с сохранением в общий кэш очистки."""
    chat_id = cb.message.chat.id

    try:
        await cb.message.delete()
    except Exception as e:
        print(f"[admin_analytics] delete clicked message failed: {e}")

    await clear_bot_messages(chat_id, cb.bot)

    msg = await cb.bot.send_message(
        chat_id,
        text,
        parse_mode="HTML",
        reply_markup=markup,
        disable_web_page_preview=True,
    )
    last_bot_messages[chat_id] = [msg.message_id]

    try:
        await cb.answer()
    except Exception:
        pass


def _analytics_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Рост", callback_data="admin:analytics:growth")],
            [InlineKeyboardButton(text="📊 Обзор по разделам", callback_data="admin:analytics:sections")],
            [InlineKeyboardButton(text="🔎 Популярные запросы", callback_data="admin:analytics:top_searches")],
            [InlineKeyboardButton(text="❌ Пустые поиски", callback_data="admin:analytics:no_results")],
            [InlineKeyboardButton(text="🧠 Типы поиска", callback_data="admin:analytics:search_quality")],
            [InlineKeyboardButton(text="🔥 Топ открываемых карточек", callback_data="admin:analytics:top_cards")],
            [InlineKeyboardButton(text="📂 Источники открытий", callback_data="admin:analytics:sources")],
            [InlineKeyboardButton(text="📈 Search → open", callback_data="admin:analytics:search_conversion")],
            [InlineKeyboardButton(text="👤 Авторы объявлений", callback_data="admin:analytics:owners")],
            [InlineKeyboardButton(text="🏙 По городам", callback_data="admin:analytics:cities")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin")],
        ]
    )


def _analytics_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К аналитике", callback_data="admin:analytics")],
            [InlineKeyboardButton(text="🏠 Админ-панель", callback_data="admin")],
        ]
    )


async def _send_admin_analytics_messages(
    cb: CallbackQuery,
    messages: list[tuple[str, InlineKeyboardMarkup | None]],
) -> None:
    """Вывод нескольких сообщений аналитики с сохранением всех id для общей очистки.

    Используется там, где важно показать карточки отдельно: текст автора → сразу его кнопка.
    """
    chat_id = cb.message.chat.id

    try:
        await cb.message.delete()
    except Exception as e:
        print(f"[admin_analytics] delete clicked message failed: {e}")

    await clear_bot_messages(chat_id, cb.bot)

    sent_ids: list[int] = []
    for text, markup in messages:
        msg = await cb.bot.send_message(
            chat_id,
            text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )
        sent_ids.append(msg.message_id)

    last_bot_messages[chat_id] = sent_ids

    try:
        await cb.answer()
    except Exception:
        pass


def _short_button_text(value: object, *, limit: int = 42) -> str:
    text = str(value or "").strip()
    if not text:
        text = "—"
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _count_label(n: int, one: str, few: str, many: str) -> str:
    n_abs = abs(int(n))
    if 11 <= n_abs % 100 <= 14:
        word = many
    elif n_abs % 10 == 1:
        word = one
    elif 2 <= n_abs % 10 <= 4:
        word = few
    else:
        word = many
    return f"{n} {word}"


def _owner_button_label(owner_id: object, contacts_raw: object, listings_count: object) -> str:
    contact = _pick_owner_contact(contacts_raw) or "без контакта"
    contact = _short_button_text(contact, limit=30)
    count = int(listings_count or 0)
    return f"{contact} • {_count_label(count, 'объявление', 'объявления', 'объявлений')}"


def _owners_menu_kb(rows_data, offset: int, total: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    for r in rows_data:
        rows.append([
            InlineKeyboardButton(
                text=_owner_button_label(r["owner_id"], r["contacts_raw"], r["listings_count"]),
                callback_data=f"admin:analytics:owner:{r['owner_id']}:{offset}:0",
            )
        ])

    nav: list[InlineKeyboardButton] = []
    prev_offset = max(0, offset - OWNERS_PAGE_LIMIT)
    next_offset = offset + OWNERS_PAGE_LIMIT
    total_pages = max(1, (total + OWNERS_PAGE_LIMIT - 1) // OWNERS_PAGE_LIMIT)
    current_page = min(total_pages, offset // OWNERS_PAGE_LIMIT + 1)

    if offset > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"admin:analytics:owners:{prev_offset}"))
    nav.append(InlineKeyboardButton(text=f"{current_page}/{total_pages}", callback_data=f"admin:analytics:owners:{offset}"))
    if next_offset < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"admin:analytics:owners:{next_offset}"))
    rows.append(nav)

    rows.append([InlineKeyboardButton(text="◀️ К аналитике", callback_data="admin:analytics")])
    rows.append([InlineKeyboardButton(text="🏠 Админ-панель", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _owner_detail_kb(
    owner_id: object,
    owner_offset: int,
    listing_offset: int,
    total: int,
    listing_rows=None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if listing_rows:
        for r in listing_rows:
            listing_id = r["listing_id"]
            title = _short_button_text(r["title"] or "Без названия", limit=34)
            opens = int(r["opens"] or 0)
            rows.append([
                InlineKeyboardButton(
                    text=f"{title} • {opens}",
                    callback_data=f"admin:analytics:listing:{listing_id}:{owner_id}:{owner_offset}:{listing_offset}",
                )
            ])

    if total > OWNER_LISTINGS_PAGE_LIMIT:
        nav: list[InlineKeyboardButton] = []
        prev_offset = max(0, listing_offset - OWNER_LISTINGS_PAGE_LIMIT)
        next_offset = listing_offset + OWNER_LISTINGS_PAGE_LIMIT
        total_pages = max(1, (total + OWNER_LISTINGS_PAGE_LIMIT - 1) // OWNER_LISTINGS_PAGE_LIMIT)
        current_page = min(total_pages, listing_offset // OWNER_LISTINGS_PAGE_LIMIT + 1)

        if listing_offset > 0:
            nav.append(
                InlineKeyboardButton(
                    text="◀️",
                    callback_data=f"admin:analytics:owner:{owner_id}:{owner_offset}:{prev_offset}",
                )
            )
        nav.append(
            InlineKeyboardButton(
                text=f"{current_page}/{total_pages}",
                callback_data=f"admin:analytics:owner:{owner_id}:{owner_offset}:{listing_offset}",
            )
        )
        if next_offset < total:
            nav.append(
                InlineKeyboardButton(
                    text="▶️",
                    callback_data=f"admin:analytics:owner:{owner_id}:{owner_offset}:{next_offset}",
                )
            )
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="◀️ К авторам", callback_data=f"admin:analytics:owners:{owner_offset}")])
    rows.append([InlineKeyboardButton(text="📊 К аналитике", callback_data="admin:analytics")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _listing_detail_kb(owner_id: object, owner_offset: int, listing_offset: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="◀️ К объявлениям автора",
                    callback_data=f"admin:analytics:owner:{owner_id}:{owner_offset}:{listing_offset}",
                )
            ],
            [InlineKeyboardButton(text="◀️ К авторам", callback_data=f"admin:analytics:owners:{owner_offset}")],
            [InlineKeyboardButton(text="📊 К аналитике", callback_data="admin:analytics")],
        ]
    )


def _safe_text(value: object, *, fallback: str = "—") -> str:
    text = str(value or "").strip()
    return escape(text if text else fallback)


def _display_city_name(value: object) -> str:
    """Город для человеческого вывода: без технической точки из seed-данных."""
    text = str(value or "").strip()
    if text.endswith("."):
        text = text[:-1].strip()
    return text or "—"


def _safe_query_label(value: str | None) -> str:
    return _safe_text(value)


def _pct(part: int | float, total: int | float) -> str:
    if not total:
        return "0%"
    return f"{(float(part) / float(total) * 100):.1f}%"


def _week_trend(this_week: int, prev_week: int) -> str:
    arrow = "↑" if this_week > prev_week else ("↓" if this_week < prev_week else "→")
    return f"+{this_week} {arrow}  пред. нед. +{prev_week}"


def _avg(part: int | float, total: int | float) -> str:
    if not total:
        return "0.0"
    return f"{(float(part) / float(total)):.1f}"


def _format_dt_short(value: object) -> str:
    """Форматирует дату из SQLite в короткий вид dd.mm.yy HH:MM."""
    raw = str(value or "").strip()
    if not raw:
        return "—"

    # SQLite обычно отдаёт 'YYYY-MM-DD HH:MM:SS.ffffff' или ISO-строку.
    clean = raw.replace("T", " ").split("+")[0].split(".")[0].strip()
    date_part, _, time_part = clean.partition(" ")
    ymd = date_part.split("-")
    if len(ymd) == 3:
        yyyy, mm, dd = ymd
        hhmm = ":".join((time_part or "00:00").split(":")[:2])
        if len(yyyy) == 4 and mm and dd:
            return f"{dd}.{mm}.{yyyy[-2:]} {hhmm}"

    return escape(raw)


def _section_label(value: str | None) -> str:
    labels = {
        "market": "Барахолка",
        "services": "Услуги",
        "service": "Услуги",
        "vacancy": "Вакансии",
        "vacancies": "Вакансии",
        "events": "Афиша",
        "afisha": "Афиша",
        "release": "Релизы",
        "releases": "Релизы",
        "artists": "Исполнители",
        None: "не указан",
        "": "не указан",
    }
    return labels.get(value, value or "не указан")


def _source_label(value: str | None) -> str:
    labels = {
        "search": "поиск",
        "catalog": "каталог",
        "my": "мои объявления",
        "calendar": "календарь",
        "calendar_city": "календарь/город",
        "city_list": "список города",
        "near": "ближайшие",
        "recent": "свежие",
        "direct": "прямой переход",
        None: "не указан",
        "": "не указан",
    }
    return labels.get(value, value or "не указан")


def _normalize_contact(value: object) -> str:
    return str(value or "").strip()


def _contact_to_html_link(value: object) -> str:
    """Возвращает безопасное HTML-представление контакта автора.

    В текущей БД нет отдельной таблицы Telegram-профилей пользователей,
    поэтому для отчёта по авторам используем contact из их объявлений.
    """
    contact = _normalize_contact(value)
    if not contact:
        return "—"

    escaped = escape(contact)

    if contact.startswith("@") and len(contact) > 1:
        username = contact[1:].strip()
        if username:
            return f'<a href="https://t.me/{escape(username)}">{escaped}</a>'

    lower = contact.lower()
    if lower.startswith("https://t.me/") or lower.startswith("http://t.me/"):
        return f'<a href="{escaped}">{escaped}</a>'

    if lower.startswith("tg://"):
        return f'<a href="{escaped}">{escaped}</a>'

    return escaped


def _pick_owner_contact(contacts_raw: object) -> str:
    """Берём первый пригодный контакт из GROUP_CONCAT(DISTINCT contact)."""
    contacts = [c.strip() for c in str(contacts_raw or "").split(",") if c and c.strip()]
    if not contacts:
        return ""

    # Сначала предпочитаем Telegram username/link.
    for c in contacts:
        low = c.lower()
        if c.startswith("@") or "t.me/" in low or low.startswith("tg://"):
            return c

    return contacts[0]


def _contacts_count_label(contacts_raw: object) -> str:
    contacts = sorted({c.strip() for c in str(contacts_raw or "").split(",") if c and c.strip()})
    if len(contacts) <= 1:
        return ""
    return f"; контактов в объявлениях: {len(contacts)}"


@router.callback_query(F.data == "admin:analytics")
async def admin_analytics_menu(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    async with SessionLocal() as s:
        # Поиск и просмотры
        search_count = (await s.execute(sql("SELECT COUNT(*) FROM search_log"))).scalar_one() or 0
        no_result_count = (await s.execute(sql("SELECT COUNT(*) FROM search_log WHERE results_count = 0"))).scalar_one() or 0
        open_count = (await s.execute(sql("SELECT COUNT(*) FROM listing_views WHERE action = 'open'"))).scalar_one() or 0
        try:
            contact_count = (await s.execute(sql("SELECT COUNT(*) FROM listing_views WHERE action = 'contact'"))).scalar_one() or 0
        except Exception:
            contact_count = 0

        # Объявления
        total_listings = (await s.execute(sql("SELECT COUNT(*) FROM listing"))).scalar_one() or 0
        new_listings_week = (await s.execute(sql(
            "SELECT COUNT(*) FROM listing WHERE created_at >= datetime('now', '-7 days')"
        ))).scalar_one() or 0
        new_listings_prev = (await s.execute(sql(
            "SELECT COUNT(*) FROM listing WHERE created_at >= datetime('now', '-14 days')"
            " AND created_at < datetime('now', '-7 days')"
        ))).scalar_one() or 0

        # Пользователи (BotUser)
        try:
            total_users = (await s.execute(sql("SELECT COUNT(*) FROM BotUser"))).scalar_one() or 0
            new_users_week = (await s.execute(sql(
                "SELECT COUNT(*) FROM BotUser WHERE first_seen >= datetime('now', '-7 days')"
            ))).scalar_one() or 0
            new_users_prev = (await s.execute(sql(
                "SELECT COUNT(*) FROM BotUser WHERE first_seen >= datetime('now', '-14 days')"
                " AND first_seen < datetime('now', '-7 days')"
            ))).scalar_one() or 0
            dau = (await s.execute(sql(
                "SELECT COUNT(*) FROM BotUser WHERE last_seen >= datetime('now', 'start of day')"
            ))).scalar_one() or 0
            wau = (await s.execute(sql(
                "SELECT COUNT(*) FROM BotUser WHERE last_seen >= datetime('now', '-7 days')"
            ))).scalar_one() or 0
            mau = (await s.execute(sql(
                "SELECT COUNT(*) FROM BotUser WHERE last_seen >= datetime('now', '-30 days')"
            ))).scalar_one() or 0
            authors_converted = (await s.execute(sql(
                "SELECT COUNT(DISTINCT l.owner_id) FROM listing l"
                " JOIN BotUser bu ON bu.user_id = l.owner_id"
            ))).scalar_one() or 0
        except Exception as e:
            print(f"[admin_analytics] BotUser query error: {e}")
            total_users = new_users_week = new_users_prev = dau = wau = mau = authors_converted = 0

    search_warn = "  ⚠️" if search_count and no_result_count / search_count > 0.25 else ""
    conversion = f"{authors_converted} ({_pct(authors_converted, total_users)})" if total_users else "—"

    text = (
        "📊 <b>Аналитика</b>\n\n"
        f"👥 <b>Пользователей:</b> {total_users}\n"
        f"  За неделю: {_week_trend(new_users_week, new_users_prev)}\n"
        f"  Сегодня: {dau}  /  нед.: {wau}  /  мес.: {mau}\n"
        f"  Разместили объявление: {conversion}\n\n"
        f"📋 <b>Объявлений:</b> {total_listings}\n"
        f"  За неделю: {_week_trend(new_listings_week, new_listings_prev)}\n\n"
        f"🔎 <b>Поиск:</b> {search_count} запросов\n"
        f"  Пустых: {no_result_count} ({_pct(no_result_count, search_count)}){search_warn}\n"
        f"  Открытий карточек: {open_count}\n"
        f"  Нажатий «Написать»: {contact_count} ({_pct(contact_count, open_count)} от открытий)\n\n"
        "Выберите раздел:"
    )

    await _send_admin_analytics_message(cb, text, _analytics_main_kb())


@router.callback_query(F.data == "admin:analytics:growth")
async def admin_analytics_growth(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    today = date.today()
    days_range = [today - timedelta(days=i) for i in range(6, -1, -1)]

    async with SessionLocal() as s:
        try:
            user_rows = (await s.execute(sql(
                "SELECT DATE(first_seen) AS day, COUNT(*) AS cnt FROM BotUser"
                " WHERE first_seen >= datetime('now', '-6 days') GROUP BY day"
            ))).mappings().all()
            new_users_week = (await s.execute(sql(
                "SELECT COUNT(*) FROM BotUser WHERE first_seen >= datetime('now', '-7 days')"
            ))).scalar_one() or 0
            new_users_prev = (await s.execute(sql(
                "SELECT COUNT(*) FROM BotUser WHERE first_seen >= datetime('now', '-14 days')"
                " AND first_seen < datetime('now', '-7 days')"
            ))).scalar_one() or 0
        except Exception as e:
            print(f"[admin_analytics:growth] BotUser error: {e}")
            user_rows, new_users_week, new_users_prev = [], 0, 0

        listing_rows = (await s.execute(sql(
            "SELECT DATE(created_at) AS day, COUNT(*) AS cnt FROM listing"
            " WHERE created_at >= datetime('now', '-6 days') GROUP BY day"
        ))).mappings().all()
        new_listings_week = (await s.execute(sql(
            "SELECT COUNT(*) FROM listing WHERE created_at >= datetime('now', '-7 days')"
        ))).scalar_one() or 0
        new_listings_prev = (await s.execute(sql(
            "SELECT COUNT(*) FROM listing WHERE created_at >= datetime('now', '-14 days')"
            " AND created_at < datetime('now', '-7 days')"
        ))).scalar_one() or 0

    user_map = {r["day"]: int(r["cnt"] or 0) for r in user_rows}
    listing_map = {r["day"]: int(r["cnt"] or 0) for r in listing_rows}

    lines = ["📅 <b>Рост — динамика за 7 дней</b>", ""]

    lines.append("👥 <b>Новые пользователи</b>")
    for d in days_range:
        lines.append(f"  {d.strftime('%d.%m')}  {user_map.get(d.strftime('%Y-%m-%d'), 0)}")
    lines.append(f"  Итого: +{new_users_week}  /  пред. нед. +{new_users_prev}")

    lines.append("")
    lines.append("📋 <b>Новые объявления</b>")
    for d in days_range:
        lines.append(f"  {d.strftime('%d.%m')}  {listing_map.get(d.strftime('%Y-%m-%d'), 0)}")
    lines.append(f"  Итого: +{new_listings_week}  /  пред. нед. +{new_listings_prev}")

    await _send_admin_analytics_message(cb, "\n".join(lines), _analytics_back_kb())


@router.callback_query(F.data == "admin:analytics:sections")
async def admin_analytics_sections(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    async with SessionLocal() as s:
        search_rows = (await s.execute(sql("""
            SELECT
                section,
                COUNT(*) AS searches,
                SUM(CASE WHEN results_count = 0 THEN 1 ELSE 0 END) AS no_results,
                COUNT(DISTINCT user_id) AS search_users
            FROM search_log
            GROUP BY section
        """))).mappings().all()
        open_rows = (await s.execute(sql("""
            SELECT
                section,
                COUNT(*) AS opens,
                COUNT(DISTINCT user_id) AS open_users,
                SUM(CASE WHEN source = 'search' THEN 1 ELSE 0 END) AS search_opens,
                SUM(CASE WHEN source = 'catalog' THEN 1 ELSE 0 END) AS catalog_opens
            FROM listing_views
            WHERE action = 'open'
            GROUP BY section
        """))).mappings().all()

    data: dict[str, dict[str, int]] = {}
    for r in search_rows:
        section = r["section"] or ""
        data.setdefault(section, {})
        data[section].update(
            searches=int(r["searches"] or 0),
            no_results=int(r["no_results"] or 0),
            search_users=int(r["search_users"] or 0),
        )
    for r in open_rows:
        section = r["section"] or ""
        data.setdefault(section, {})
        data[section].update(
            opens=int(r["opens"] or 0),
            open_users=int(r["open_users"] or 0),
            search_opens=int(r["search_opens"] or 0),
            catalog_opens=int(r["catalog_opens"] or 0),
        )

    def sort_key(item: tuple[str, dict[str, int]]) -> int:
        _, values = item
        return int(values.get("opens", 0)) + int(values.get("searches", 0))

    lines = ["📊 <b>Обзор по разделам</b>", ""]
    if not data:
        lines.append("Пока нет данных в search_log и listing_views.")
    else:
        for section, values in sorted(data.items(), key=sort_key, reverse=True):
            searches = values.get("searches", 0)
            no_results = values.get("no_results", 0)
            opens = values.get("opens", 0)
            search_opens = values.get("search_opens", 0)
            catalog_opens = values.get("catalog_opens", 0)
            open_users = values.get("open_users", 0)
            lines.append(
                f"<b>{_safe_text(_section_label(section))}</b> <code>{_safe_text(section)}</code>\n"
                f"   🔎 поисков: <b>{searches}</b>, пустых: <b>{no_results}</b> ({_pct(no_results, searches)})\n"
                f"   👁 открытий: <b>{opens}</b>, пользователей: <b>{open_users}</b>\n"
                f"   🔍 из поиска: <b>{search_opens}</b>, 📂 из каталога: <b>{catalog_opens}</b>"
            )

    await _send_admin_analytics_message(cb, "\n".join(lines), _analytics_back_kb())


@router.callback_query(F.data == "admin:analytics:top_searches")
async def admin_analytics_top_searches(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    q = sql("""
        SELECT
            COALESCE(NULLIF(query_effective, ''), NULLIF(query_normalized, ''), query_raw) AS q,
            section,
            COUNT(*) AS cnt,
            SUM(CASE WHEN results_count = 0 THEN 1 ELSE 0 END) AS zero_cnt
        FROM search_log
        GROUP BY q, section
        ORDER BY cnt DESC, q ASC
        LIMIT :limit
    """)

    async with SessionLocal() as s:
        rows = (await s.execute(q, {"limit": PAGE_LIMIT})).mappings().all()

    lines = ["🔎 <b>Популярные запросы</b>", ""]
    if not rows:
        lines.append("Пока нет записей в search_log.")
    else:
        for i, r in enumerate(rows, start=1):
            lines.append(
                f"{i}. <b>{_safe_query_label(r['q'])}</b> "
                f"— {r['cnt']} раз(а), раздел: <code>{_safe_text(r['section'])}</code>, "
                f"пустых: {r['zero_cnt']}"
            )

    await _send_admin_analytics_message(cb, "\n".join(lines), _analytics_back_kb())


@router.callback_query(F.data == "admin:analytics:no_results")
async def admin_analytics_no_results(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    q = sql("""
        SELECT
            COALESCE(NULLIF(query_effective, ''), NULLIF(query_normalized, ''), query_raw) AS q,
            section,
            COUNT(*) AS cnt
        FROM search_log
        WHERE results_count = 0
        GROUP BY q, section
        ORDER BY cnt DESC, q ASC
        LIMIT :limit
    """)

    async with SessionLocal() as s:
        rows = (await s.execute(q, {"limit": PAGE_LIMIT})).mappings().all()

    lines = ["❌ <b>Пустые поиски</b>", ""]
    lines.append("Только запросы, где <code>results_count = 0</code>.")
    lines.append("")
    if not rows:
        lines.append("Пока нет пустых поисков.")
    else:
        for i, r in enumerate(rows, start=1):
            lines.append(
                f"{i}. <b>{_safe_query_label(r['q'])}</b> "
                f"— {r['cnt']} раз(а), раздел: <code>{_safe_text(r['section'])}</code>"
            )

    await _send_admin_analytics_message(cb, "\n".join(lines), _analytics_back_kb())


@router.callback_query(F.data == "admin:analytics:search_quality")
async def admin_analytics_search_quality(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    async with SessionLocal() as s:
        total = (await s.execute(sql("SELECT COUNT(*) FROM search_log"))).scalar_one()
        no_results = (await s.execute(sql("SELECT COUNT(*) FROM search_log WHERE results_count = 0"))).scalar_one()
        mode_rows = (await s.execute(sql("""
            SELECT match_mode, COUNT(*) AS cnt
            FROM search_log
            GROUP BY match_mode
            ORDER BY cnt DESC, match_mode ASC
        """))).mappings().all()

    lines = [
        "🧠 <b>Типы поиска</b>",
        "",
        "Это не список пустых запросов, а распределение по <code>match_mode</code>.",
        "Пустые поиски здесь показаны только общей строкой для ориентира.",
        "",
        f"Всего поисков: <b>{total}</b>",
        f"Пустых поисков: <b>{no_results}</b> ({_pct(no_results, total)})",
        "",
        "<b>Match mode:</b>",
    ]

    if not mode_rows:
        lines.append("Пока нет данных по match_mode.")
    else:
        for r in mode_rows:
            mode = r["match_mode"] or "unknown"
            cnt = int(r["cnt"] or 0)
            lines.append(f"• <code>{_safe_text(mode)}</code>: <b>{cnt}</b> ({_pct(cnt, total)})")

    await _send_admin_analytics_message(cb, "\n".join(lines), _analytics_back_kb())


@router.callback_query(F.data == "admin:analytics:top_cards")
async def admin_analytics_top_cards(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    q = sql("""
        SELECT
            lv.listing_id,
            lv.section,
            COUNT(*) AS opens,
            COUNT(DISTINCT lv.user_id) AS users,
            SUM(CASE WHEN lv.source = 'search' THEN 1 ELSE 0 END) AS search_opens,
            SUM(CASE WHEN lv.source = 'catalog' THEN 1 ELSE 0 END) AS catalog_opens,
            COALESCE(l.title, 'Без названия') AS title,
            l.owner_id AS owner_id
        FROM listing_views lv
        LEFT JOIN listing l ON l.id = lv.listing_id
        WHERE lv.action = 'open'
        GROUP BY lv.listing_id, lv.section, l.title, l.owner_id
        ORDER BY opens DESC, users DESC, lv.listing_id DESC
        LIMIT :limit
    """)

    async with SessionLocal() as s:
        rows = (await s.execute(q, {"limit": PAGE_LIMIT})).mappings().all()

    lines = ["🔥 <b>Топ открываемых карточек</b>", ""]
    if not rows:
        lines.append("Пока нет открытий карточек в listing_views.")
    else:
        for i, r in enumerate(rows, start=1):
            title = str(r["title"] or "Без названия").strip()
            if len(title) > 60:
                title = title[:57] + "..."
            lines.append(
                f"{i}. <b>{_safe_text(title)}</b>\n"
                f"   ID: <code>{r['listing_id']}</code>, owner: <code>{_safe_text(r['owner_id'])}</code>, "
                f"раздел: <code>{_safe_text(r['section'])}</code>\n"
                f"   открытий: <b>{r['opens']}</b>, пользователей: {r['users']}\n"
                f"   🔍 из поиска: <b>{r['search_opens'] or 0}</b>, 📂 из каталога: <b>{r['catalog_opens'] or 0}</b>"
            )

    await _send_admin_analytics_message(cb, "\n".join(lines), _analytics_back_kb())


@router.callback_query(F.data == "admin:analytics:sources")
async def admin_analytics_sources(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    q = sql("""
        SELECT
            section,
            source,
            COUNT(*) AS opens,
            COUNT(DISTINCT user_id) AS users
        FROM listing_views
        WHERE action = 'open'
        GROUP BY section, source
        ORDER BY opens DESC, section ASC
    """)

    async with SessionLocal() as s:
        rows = (await s.execute(q)).mappings().all()

    lines = ["📂 <b>Источники открытий карточек</b>", ""]
    if not rows:
        lines.append("Пока нет открытий карточек в listing_views.")
    else:
        for r in rows:
            lines.append(
                f"• <code>{_safe_text(r['section'])}</code> / {_safe_text(_source_label(r['source']))}: "
                f"<b>{r['opens']}</b> открытий, пользователей: {r['users']}"
            )

    await _send_admin_analytics_message(cb, "\n".join(lines), _analytics_back_kb())


@router.callback_query(F.data == "admin:analytics:search_conversion")
async def admin_analytics_search_conversion(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    async with SessionLocal() as s:
        search_rows = (await s.execute(sql("""
            SELECT section, COUNT(*) AS searches
            FROM search_log
            GROUP BY section
        """))).mappings().all()
        open_rows = (await s.execute(sql("""
            SELECT section, COUNT(*) AS search_opens
            FROM listing_views
            WHERE action = 'open' AND source = 'search'
            GROUP BY section
        """))).mappings().all()

    searches_by_section = {r["section"] or "": int(r["searches"] or 0) for r in search_rows}
    opens_by_section = {r["section"] or "": int(r["search_opens"] or 0) for r in open_rows}
    sections = sorted(set(searches_by_section) | set(opens_by_section))

    total_searches = sum(searches_by_section.values())
    total_search_opens = sum(opens_by_section.values())

    lines = [
        "📈 <b>Search → open</b>",
        "",
        "Это условная метрика: сколько открытий карточек из поиска приходится на количество поисковых запросов.",
        "Она не является точной пользовательской воронкой, но хорошо показывает полезность поиска.",
        "",
        f"Всего поисков: <b>{total_searches}</b>",
        f"Открытий из поиска: <b>{total_search_opens}</b>",
        f"Условная конверсия: <b>{_pct(total_search_opens, total_searches)}</b>",
        "",
    ]

    if not sections:
        lines.append("Пока нет данных.")
    else:
        for section in sections:
            searches = searches_by_section.get(section, 0)
            opens = opens_by_section.get(section, 0)
            lines.append(
                f"• <b>{_safe_text(_section_label(section))}</b> <code>{_safe_text(section)}</code>: "
                f"поисков {searches}, open из поиска {opens}, {_pct(opens, searches)}"
            )

    await _send_admin_analytics_message(cb, "\n".join(lines), _analytics_back_kb())


@router.callback_query(F.data == "admin:analytics:cities")
async def admin_analytics_cities(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    async with SessionLocal() as s:
        rows = (await s.execute(sql("""
            SELECT c.id, c.name,
                   COUNT(DISTINCT l.id) AS total,
                   COUNT(DISTINCT CASE
                       WHEN l.status='active' AND COALESCE(l.is_sold,0)=0 THEN l.id
                   END) AS active,
                   SUM(CASE WHEN lv.action='open' THEN 1 ELSE 0 END) AS views,
                   COUNT(DISTINCT CASE WHEN lv.action='open' THEN lv.user_id END) AS viewers,
                   SUM(CASE WHEN lv.action='contact' THEN 1 ELSE 0 END) AS contacts,
                   COUNT(DISTINCT CASE WHEN l.type='market' THEN l.id END) AS market,
                   COUNT(DISTINCT CASE WHEN l.type='service' THEN l.id END) AS service,
                   COUNT(DISTINCT CASE WHEN l.type='vacancy' THEN l.id END) AS vacancy,
                   COUNT(DISTINCT CASE WHEN l.type='events' THEN l.id END) AS events,
                   COUNT(DISTINCT CASE WHEN l.type='release' THEN l.id END) AS release
            FROM city c
            LEFT JOIN listing l ON l.city_id=c.id
            LEFT JOIN listing_views lv ON lv.listing_id=l.id
            GROUP BY c.id, c.name
            ORDER BY views DESC, total DESC
        """))).mappings().all()

    if not rows:
        await _send_admin_analytics_message(cb, "🏙 <b>Города</b>\n\nНет данных.", _analytics_back_kb())
        return

    lines = ["🏙 <b>Аналитика по городам</b>", ""]
    section_labels = {"market": "Барахолка", "service": "Услуги",
                      "vacancy": "Вакансии", "events": "Афиша",
                      "release": "Релизы"}
    for r in rows:
        name = _safe_text((r["name"] or "").rstrip(".").strip())
        total   = int(r["total"] or 0)
        active  = int(r["active"] or 0)
        views   = int(r["views"] or 0)
        viewers = int(r["viewers"] or 0)
        contacts = int(r["contacts"] or 0)
        conv = f"{contacts/views*100:.1f}%" if views else "—"
        # build per-section breakdown
        by_sec = []
        for key, label in section_labels.items():
            cnt = int(r[key] or 0)
            if cnt:
                by_sec.append(f"{label}: {cnt}")
        sec_str = " · ".join(by_sec) if by_sec else "—"
        lines.append(
            f"📍 <b>{name}</b>\n"
            f"   📋 <b>{total}</b> объявл. (акт.: <b>{active}</b>)\n"
            f"   👁 <b>{views}</b> просм. · 👥 <b>{viewers}</b> польз.\n"
            f"   📞 <b>{contacts}</b> контакт. · конверсия: <b>{conv}</b>\n"
            f"   {_safe_text(sec_str)}"
        )

    await _send_admin_analytics_message(cb, "\n".join(lines), _analytics_back_kb())


async def _fetch_owners_summary(offset: int, limit: int):
    q = sql("""
        SELECT
            l.owner_id AS owner_id,
            GROUP_CONCAT(DISTINCT l.contact) AS contacts_raw,
            COUNT(DISTINCT l.id) AS listings_count,
            COUNT(DISTINCT CASE
                WHEN l.status='active' AND COALESCE(l.is_sold, 0)=0 THEN l.id
            END) AS active_listings,
            COUNT(DISTINCT CASE
                WHEN l.status!='active' OR COALESCE(l.is_sold, 0)=1 THEN l.id
            END) AS sold_listings,
            COUNT(lv.id) AS opens,
            COUNT(DISTINCT lv.user_id) AS unique_viewers,
            SUM(CASE WHEN lv.source = 'search' THEN 1 ELSE 0 END) AS search_opens,
            SUM(CASE WHEN lv.source = 'catalog' THEN 1 ELSE 0 END) AS catalog_opens,
            MAX(l.created_at) AS last_listing_at
        FROM listing l
        LEFT JOIN listing_views lv
            ON lv.listing_id = l.id
           AND lv.action = 'open'
        GROUP BY l.owner_id
        ORDER BY opens DESC, listings_count DESC, l.owner_id DESC
        LIMIT :limit OFFSET :offset
    """)
    async with SessionLocal() as s:
        rows = (await s.execute(q, {"limit": limit, "offset": offset})).mappings().all()
        total_owners = (await s.execute(sql("SELECT COUNT(DISTINCT owner_id) FROM listing"))).scalar_one()
        total_listings = (await s.execute(sql("SELECT COUNT(*) FROM listing"))).scalar_one()
        owners_with_opens = (await s.execute(sql("""
            SELECT COUNT(*)
            FROM (
                SELECT l.owner_id
                FROM listing l
                JOIN listing_views lv ON lv.listing_id = l.id AND lv.action = 'open'
                GROUP BY l.owner_id
            ) x
        """))).scalar_one()
    return rows, int(total_owners or 0), int(total_listings or 0), int(owners_with_opens or 0)


async def _show_owners_page(cb: CallbackQuery, offset: int = 0) -> None:
    rows, total_owners, total_listings, owners_with_opens = await _fetch_owners_summary(offset, OWNERS_PAGE_LIMIT)

    page_from = offset + 1 if total_owners else 0
    page_to = min(offset + OWNERS_PAGE_LIMIT, total_owners)

    text = (
        "👤 <b>Авторы объявлений</b>\n\n"
        f"Всего авторов: <b>{total_owners}</b>\n"
        f"Всего карточек: <b>{total_listings}</b>\n"
        f"Авторов с открытиями карточек: <b>{owners_with_opens}</b>\n"
        f"Показаны авторы: <b>{page_from}–{page_to}</b>"
    )

    if not rows:
        text += "\n\nПока нет авторов объявлений."

    await _send_admin_analytics_message(cb, text, _owners_menu_kb(rows, offset, total_owners))


@router.callback_query(F.data == "admin:analytics:owners")
async def admin_analytics_owners(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await _show_owners_page(cb, 0)


@router.callback_query(F.data.startswith("admin:analytics:owners:"))
async def admin_analytics_owners_page(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    try:
        offset = max(0, int((cb.data or "").split(":")[-1]))
    except Exception:
        offset = 0

    await _show_owners_page(cb, offset)


@router.callback_query(F.data.startswith("admin:analytics:owner:"))
async def admin_analytics_owner_detail(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    parts = (cb.data or "").split(":")
    try:
        owner_id = int(parts[3])
        owner_offset = max(0, int(parts[4]))
        listing_offset = max(0, int(parts[5]))
    except Exception:
        await cb.answer("Некорректные данные автора", show_alert=True)
        return

    owner_q = sql("""
        SELECT
            l.owner_id AS owner_id,
            GROUP_CONCAT(DISTINCT l.contact) AS contacts_raw,
            COUNT(DISTINCT l.id) AS listings_count,
            COUNT(DISTINCT CASE
                WHEN l.status='active' AND COALESCE(l.is_sold, 0)=0 THEN l.id
            END) AS active_listings,
            COUNT(DISTINCT CASE
                WHEN l.status!='active' OR COALESCE(l.is_sold, 0)=1 THEN l.id
            END) AS sold_listings,
            COUNT(lv.id) AS opens,
            COUNT(DISTINCT lv.user_id) AS unique_viewers,
            SUM(CASE WHEN lv.source = 'search' THEN 1 ELSE 0 END) AS search_opens,
            SUM(CASE WHEN lv.source = 'catalog' THEN 1 ELSE 0 END) AS catalog_opens,
            MAX(l.created_at) AS last_listing_at
        FROM listing l
        LEFT JOIN listing_views lv
            ON lv.listing_id = l.id
           AND lv.action = 'open'
        WHERE l.owner_id = :owner_id
        GROUP BY l.owner_id
    """)
    listings_q = sql("""
        SELECT
            l.id AS listing_id,
            l.title AS title,
            l.type AS listing_type,
            l.is_sold AS is_sold,
            COUNT(lv.id) AS opens,
            COUNT(DISTINCT lv.user_id) AS unique_viewers,
            SUM(CASE WHEN lv.source = 'search' THEN 1 ELSE 0 END) AS search_opens,
            SUM(CASE WHEN lv.source = 'catalog' THEN 1 ELSE 0 END) AS catalog_opens
        FROM listing l
        LEFT JOIN listing_views lv
            ON lv.listing_id = l.id
           AND lv.action = 'open'
        WHERE l.owner_id = :owner_id
        GROUP BY l.id, l.title, l.type, l.is_sold
        ORDER BY opens DESC, l.id DESC
        LIMIT :limit OFFSET :offset
    """)
    count_q = sql("SELECT COUNT(*) FROM listing WHERE owner_id = :owner_id")

    async with SessionLocal() as s:
        owner = (await s.execute(owner_q, {"owner_id": owner_id})).mappings().first()
        rows = (await s.execute(
            listings_q,
            {"owner_id": owner_id, "limit": OWNER_LISTINGS_PAGE_LIMIT, "offset": listing_offset},
        )).mappings().all()
        total = int((await s.execute(count_q, {"owner_id": owner_id})).scalar_one() or 0)

    if not owner:
        await _send_admin_analytics_message(
            cb,
            "👤 <b>Автор не найден</b>",
            InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="◀️ К авторам", callback_data=f"admin:analytics:owners:{owner_offset}")]]
            ),
        )
        return

    contacts_raw = owner["contacts_raw"]
    owner_contact = _pick_owner_contact(contacts_raw)
    contacts_note = _contacts_count_label(contacts_raw)
    listings_count = int(owner["listings_count"] or 0)
    active_listings = int(owner["active_listings"] or 0)
    sold_listings = int(owner["sold_listings"] or 0)
    opens = int(owner["opens"] or 0)
    unique_viewers = int(owner["unique_viewers"] or 0)
    search_opens = int(owner["search_opens"] or 0)
    catalog_opens = int(owner["catalog_opens"] or 0)

    page_from = listing_offset + 1 if total else 0
    page_to = min(listing_offset + OWNER_LISTINGS_PAGE_LIMIT, total)

    text = (
        "👤 <b>Автор объявления</b>\n\n"
        f"Контакт: {_contact_to_html_link(owner_contact)}\n"
        f"{_safe_text(contacts_note, fallback='')}\n\n"
        f"Карточек: <b>{listings_count}</b>\n"
        f"Активных: <b>{active_listings}</b>\n"
        f"Закрытых/проданных: <b>{sold_listings}</b>\n\n"
        f"Открытий всего: <b>{opens}</b>\n"
        f"Уникальных пользователей: <b>{unique_viewers}</b>\n"
        f"Средн. открытий на карточку: <b>{_avg(opens, listings_count)}</b>\n"
        f"Из поиска: <b>{search_opens}</b>\n"
        f"Из каталога: <b>{catalog_opens}</b>\n\n"
        f"<b>Объявления {page_from}–{page_to}</b>"
    )

    if not rows:
        text += "\n\nУ автора нет объявлений."

    await _send_admin_analytics_message(
        cb,
        text,
        _owner_detail_kb(owner_id, owner_offset, listing_offset, total, rows),
    )


@router.callback_query(F.data.startswith("admin:analytics:listing:"))
async def admin_analytics_listing_detail(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    parts = (cb.data or "").split(":")
    try:
        listing_id = int(parts[3])
        owner_id = int(parts[4])
        owner_offset = max(0, int(parts[5]))
        listing_offset = max(0, int(parts[6]))
    except Exception:
        await cb.answer("Некорректные данные объявления", show_alert=True)
        return

    q = sql("""
        SELECT
            l.id AS listing_id,
            l.owner_id AS owner_id,
            l.title AS title,
            l.descr AS descr,
            l.price AS price,
            l.contact AS contact,
            l.type AS listing_type,
            c.name AS city_name,
            (
                SELECT GROUP_CONCAT(name, ' / ')
                FROM (
                    WITH RECURSIVE cat_tree(id, name, parent_id, depth) AS (
                        SELECT c0.id, c0.name, c0.parent_id, 0
                        FROM category c0
                        WHERE c0.id = l.category_id
                        UNION ALL
                        SELECT cp.id, cp.name, cp.parent_id, cat_tree.depth + 1
                        FROM category cp
                        JOIN cat_tree ON cat_tree.parent_id = cp.id
                    )
                    SELECT name
                    FROM cat_tree
                    ORDER BY depth DESC
                )
            ) AS category_path,
            cat.name AS category_name,
            l.is_sold AS is_sold,
            l.created_at AS created_at,
            COUNT(lv.id) AS opens,
            COUNT(DISTINCT lv.user_id) AS unique_viewers,
            SUM(CASE WHEN lv.source = 'search' THEN 1 ELSE 0 END) AS search_opens,
            SUM(CASE WHEN lv.source = 'catalog' THEN 1 ELSE 0 END) AS catalog_opens
        FROM listing l
        LEFT JOIN listing_views lv
            ON lv.listing_id = l.id
           AND lv.action = 'open'
        LEFT JOIN city c ON c.id = l.city_id
        LEFT JOIN category cat ON cat.id = l.category_id
        WHERE l.id = :listing_id
        GROUP BY l.id, l.owner_id, l.title, l.descr, l.price, l.contact, l.type,
                 c.name, cat.name, l.is_sold, l.created_at
    """)

    async with SessionLocal() as s:
        item = (await s.execute(q, {"listing_id": listing_id})).mappings().first()

    if not item:
        await _send_admin_analytics_message(
            cb,
            "📂 <b>Объявление не найдено</b>",
            _listing_detail_kb(owner_id, owner_offset, listing_offset),
        )
        return

    title = str(item["title"] or "Без названия").strip()
    descr = str(item["descr"] or "").strip()
    if len(descr) > 700:
        descr = descr[:697] + "..."
    status = "закрыто/продано" if int(item["is_sold"] or 0) else "активно"

    category_display = item["category_path"] or item["category_name"]

    lines = [
        "📂 <b>Объявление автора</b>",
        "",
        f"<b>{_safe_text(title)}</b>",
    ]

    if descr:
        lines.extend(["", "<b>Описание:</b>", _safe_text(descr)])

    lines.extend([
        "",
        f"Город: <b>{_safe_text(_display_city_name(item['city_name']))}</b>",
        f"Категория: <b>{_safe_text(category_display)}</b>",
        f"Цена: <b>{_safe_text(item['price'])}</b>",
        f"Контакт: {_contact_to_html_link(item['contact'])}",
        f"Опубликовано: <b>{_format_dt_short(item['created_at'])}</b>",
        f"Статус: <b>{status}</b>",
        "",
        f"Открытий: <b>{int(item['opens'] or 0)}</b>",
        f"Уникальных пользователей: <b>{int(item['unique_viewers'] or 0)}</b>",
        f"🔍 из поиска: <b>{int(item['search_opens'] or 0)}</b>, 📂 из каталога: <b>{int(item['catalog_opens'] or 0)}</b>",
    ])

    await _send_admin_analytics_message(
        cb,
        "\n".join(lines),
        _listing_detail_kb(owner_id, owner_offset, listing_offset),
    )
