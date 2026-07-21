# app/routers/services_view.py
# -----------------------------------------------------------------------------
# Раздел «Услуги»: главное меню (из БД), выбор города/категории, список и карточки.
# Каноны: краткий RU-коммент перед каждым хендлером/функцией, очистка чата, print в конце.
# -----------------------------------------------------------------------------

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto, InputMediaVideo, WebAppInfo
)
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, or_, func, text as sql_text
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import joinedload
import urllib.parse, json
from pathlib import Path

from aiogram.filters import StateFilter

from app.database import SessionLocal
from app.models import City, Category, Listing
try:
    from app.models import Menu  # пункты меню из БД
except Exception:
    Menu = None

from app.keyboards import get_common_menu_button, build_main_menu


async def _back_row(callback_data: str) -> list[InlineKeyboardButton]:
    """Строка «Назад» из одной кнопки (общий хелпер, текст берётся из menu)."""
    back_btn = await get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data=callback_data)
    back_btn.callback_data = callback_data
    return [back_btn]
from app.routers.utils import (
    clear_bot_messages, safe_edit_or_send, register_bot_messages,
    last_bot_messages, sent_photo_messages,
    render_flex_block, render_contact, render_category_path,
    last_search_query_message, last_search_menu_message, my_listing_messages,
    build_contact_url, escape_html,
)

from app.search.fuzzy import search_items

from app.texts import get_text
from app.states import ServiceSearch

from collections import defaultdict

from app.routers.utils_category_title import format_category_title

from app.routers.utils_kb import grid3
from app.analytics.search_log import log_search
from app.analytics.listing_views import log_listing_view
from app.lifecycle import days_left_text, should_show_extend_button, extend_listing, archive_as_closed, is_active, can_owner_reactivate
from app.db_path import config_value


router = Router(name="services_view")

# service_search_ctx_by_chat = defaultdict(dict)   # {chat_id: {"query": str, "results": [Listing]}}
service_search_messages = defaultdict(list)      # {chat_id: [message_ids]}
services_search_ctx_by_chat = {}
services_last_search_menu_message = {}
services_last_search_query_message = {}

SERVICES_SEARCH_PAGE_SIZE = 10


SERVICES_ROOT_CATEGORY_ID = 80
_ROOT = Path(__file__).resolve().parents[2]
WEBAPP_BASE = (
    config_value(
        _ROOT,
        "WEBAPP_BASE",
        "https://unixound.com/rus_mus_srb_bot",
        env_files=(".env.web", ".env"),
    )
    or ""
).rstrip("/")


async def _send_yt_button(cb: CallbackQuery, video_url: str, listing_id: int) -> int | None:
    """
    Отправляет TWA-кнопку '▶️ Смотреть видео' ОТДЕЛЬНЫМ сообщением (без заголовка),
    чтобы кнопка оказалась НИЖЕ основного медиа/текста (как в Барахолке).
    Возвращает message_id созданного сообщения (для последующей зачистки).
    """
    try:
        if not video_url or not WEBAPP_BASE:
            return None
        low = video_url.lower()
        if ("youtube.com" not in low) and ("youtu.be" not in low):
            return None
        twa_url = f"{WEBAPP_BASE}/media/video_yt.html?u={urllib.parse.quote(video_url, safe='')}&listing_id={listing_id}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=(await get_text("services_view_btn_watch_video", "ru") or "▶️ Смотреть видео"), web_app=WebAppInfo(url=twa_url))]
        ])
        try:
            m = await cb.message.answer(" ", reply_markup=kb)
        except Exception:
            m = await cb.message.answer("•", reply_markup=kb)
        print(f"[services_view.py] _send_yt_button ✓ | listing_id={listing_id} url={video_url}")
        return m.message_id
    except Exception as e:
        print(f"[services_view.py] _send_yt_button ✗ | listing_id={listing_id} err={e}")
        return None


# ───────────────────────── ВСПОМОГАТЕЛЬНЫЕ ─────────────────────────

def _service_public_predicates():
    """Единые условия публичной выдачи услуг."""
    return (
        Listing.type == "service",
        Listing.status == "active",
        Listing.is_sold.is_(False),
    )


async def _load_public_service_ids(ids: list[int]) -> tuple[list[int], list[Listing]]:
    clean_ids: list[int] = []
    seen: set[int] = set()
    for raw_id in ids or []:
        try:
            listing_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if listing_id not in seen:
            seen.add(listing_id)
            clean_ids.append(listing_id)
    if not clean_ids:
        return [], []

    async with SessionLocal() as s:
        db_rows = (await s.execute(
            select(Listing).where(Listing.id.in_(clean_ids), *_service_public_predicates())
        )).scalars().all()
    by_id = {row.id: row for row in db_rows}
    valid_ids = [listing_id for listing_id in clean_ids if listing_id in by_id]
    return valid_ids, [by_id[listing_id] for listing_id in valid_ids]

async def _fetch_menu_items(parent_code: str, lang: str = "ru"):
    """Получить пункты меню из таблицы menu по parent_code/lang/visible с сортировкой."""
    if Menu is None:
        print("[services_view.py] _fetch_menu_items WARN | Menu model not found")
        return []
    async with SessionLocal() as s:
        res = await s.execute(
            select(Menu)
            .where(Menu.parent_code == parent_code, Menu.lang == lang, Menu.visible == 1)
            .order_by(Menu.order_num, Menu.id)
        )
        rows = res.scalars().all()
    print(f"[services_view.py] _fetch_menu_items ✓ | parent={parent_code} count={len(rows)}")
    return rows


async def _services_main_menu_kb() -> InlineKeyboardMarkup:
    """Главное меню 'Услуг': Поиск → Города → Мои услуги → Разместить → Назад/Главное (из БД)."""
    rows = []

    # 1) Пункты из БД (parent_code='services')
    menu_rows = await _fetch_menu_items("services", "ru")

    def _find(code: str):
        for it in menu_rows:
            if getattr(it, "code", None) == code:
                return it
        return None

    # Поиск
    it = _find("services_search")
    if it:
        label = f"{it.icon} {it.text}" if getattr(it, "icon", None) else it.text
        rows.append([InlineKeyboardButton(text=label, callback_data=it.callback_data)])

    # Города (двухколоночные)
    async with SessionLocal() as s:
        cities = (await s.execute(select(City).order_by(City.id))).scalars().all()
    buf = []
    for c in cities:
        btn = InlineKeyboardButton(text=c.name, callback_data=f"sv:city:{c.id}")
        buf.append(btn)
        if len(buf) == 2:
            rows.append(buf); buf = []
    if buf:
        rows.append(buf)

    # Мои услуги
    it = _find("my_services")
    if it:
        label = f"{it.icon} {it.text}" if getattr(it, "icon", None) else it.text
        rows.append([InlineKeyboardButton(text=label, callback_data=it.callback_data)])

    # Разместить услугу
    it = _find("service_start")
    if it:
        label = f"{it.icon} {it.text}" if getattr(it, "icon", None) else it.text
        rows.append([InlineKeyboardButton(text=label, callback_data=it.callback_data)])

    # Назад/Главное
    # back_btn = await get_common_menu_button("back", "ru")
    # if back_btn:
    #     rows.append([InlineKeyboardButton(text=back_btn.text, callback_data="go_services_menu_back")])
    main_btn = await get_common_menu_button("main_menu", "ru")
    if main_btn:
        rows.append([main_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    print(f"[services_view.py] _services_main_menu_kb ✓ | rows={len(rows)}")
    return kb


async def _services_categories_kb(cats, city_id: int, parent_id: int) -> InlineKeyboardMarkup:
    """Клавиатура категорий/подкатегорий услуг."""
    rows = []
    for c in cats:
        title = await format_category_title(c.id, (c.name or "").strip(), SessionLocal)
        rows.append([InlineKeyboardButton(text=title, callback_data=f"sv:cat:{city_id}:{c.id}")])

    # Назад: с корневого списка — к городам; с вложенного — на уровень выше
    if parent_id == SERVICES_ROOT_CATEGORY_ID:
        back_cb = "sv:cities"
    else:
        back_cb = f"sv:cat:{city_id}:{parent_id}:back"
    rows.append(await _back_row(back_cb))

    main_menu_btn = await get_common_menu_button("main_menu", "ru")
    if main_menu_btn:
        rows.append([main_menu_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    print(f"[services_view.py] _services_categories_kb | city_id={city_id} parent_id={parent_id} rows={len(rows)}")
    return kb


SERVICES_LIST_PAGE_SIZE = 10


async def _services_listings_kb(items, city_id: int, cat_id: int, offset: int = 0) -> InlineKeyboardMarkup:
    """Клавиатура списка услуг в листовой категории (с пагинацией).
    Страницы: sv:cat:<city_id>:<cat_id>:<offset>"""
    total = len(items)
    pages = max(1, (total + SERVICES_LIST_PAGE_SIZE - 1) // SERVICES_LIST_PAGE_SIZE)
    if offset >= total:
        offset = (pages - 1) * SERVICES_LIST_PAGE_SIZE
    if offset < 0:
        offset = 0
    page = offset // SERVICES_LIST_PAGE_SIZE + 1

    rows = [[InlineKeyboardButton(
        text=(i.title or f"#{i.id}")[:64],
        callback_data=f"sv:item:{i.id}:{city_id}:{cat_id}"
    )] for i in items[offset:offset + SERVICES_LIST_PAGE_SIZE]]

    if pages > 1:
        pager: list[InlineKeyboardButton] = []
        if offset > 0:
            pager.append(InlineKeyboardButton(
                text="«", callback_data=f"sv:cat:{city_id}:{cat_id}:{offset - SERVICES_LIST_PAGE_SIZE}"))
        pager.append(InlineKeyboardButton(text=f"{page}/{pages}", callback_data="stub"))
        if offset + SERVICES_LIST_PAGE_SIZE < total:
            pager.append(InlineKeyboardButton(
                text="»", callback_data=f"sv:cat:{city_id}:{cat_id}:{offset + SERVICES_LIST_PAGE_SIZE}"))
        rows.append(pager)

    rows.append(await _back_row(f"sv:cat:{city_id}:{cat_id}:back"))

    main_menu_btn = await get_common_menu_button("main_menu", "ru")
    if main_menu_btn:
        rows.append([main_menu_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    print(f"[services_view.py] _services_listings_kb | city_id={city_id} cat_id={cat_id} items={total} offset={offset} rows={len(rows)}")
    return kb



# ───────────────────────── ВСПОМОГАТЕЛЬНОЕ: YouTube-кнопка ─────────────────────────
def _is_youtube_url(url: str) -> bool:
    if not url: 
        return False
    u = url.strip().lower()
    return ("youtube.com" in u) or ("youtu.be" in u) or ("m.youtube.com" in u)


# ───────────────────────── ВСПОМОГАТЕЛЬНОЕ: YouTube TWA-кнопка ─────────────────────────
# ───────────────────────────────── ВХОД В «УСЛУГИ» ──────────────────────────

@router.callback_query(F.data == "go_services")
async def go_services(cb: CallbackQuery, state: FSMContext | None = None):
    chat_id = cb.message.chat.id

    # удалить служебные сообщения поиска «Услуг» (если остались)
    for mid in (services_last_search_menu_message.pop(chat_id, None),
                services_last_search_query_message.pop(chat_id, None)):
        if mid:
            try:
                await cb.bot.delete_message(chat_id, mid)
            except Exception:
                pass

    # удалить все собранные «мелкие» служебные сообщения поиска
    for mid in service_search_messages.pop(chat_id, []):
        try:
            await cb.bot.delete_message(chat_id, mid)
        except Exception:
            pass

    # сбросить контекст поиска «Услуг»
    services_search_ctx_by_chat.pop(chat_id, None)

    # общая очистка + удаление исходного сообщения
    await clear_bot_messages(chat_id, cb.bot)
    try:
        await cb.message.delete()
    except Exception:
        pass

    kb = await _services_main_menu_kb()
    title = await get_text("services_menu_title", "ru") or "Раздел «Услуги». Выберите действие:"
    msg = await cb.bot.send_message(chat_id, title, reply_markup=kb)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"[services_view.py] go_services ✓ | chat_id={chat_id} user_id={cb.from_user.id} msg_id={msg.message_id}")


@router.callback_query(F.data == "sv:cities")
async def sv_cities(cb: CallbackQuery):
    """Возврат из категорий к главному меню 'Услуг' (города и кнопки)."""
    return await go_services(cb, state=None)


# ───────────────────────── ГОРОД → КАТЕГОРИИ ────────────────────────────────

@router.callback_query(F.data.startswith("sv:city:"))
async def sv_city(cb: CallbackQuery):
    """После выбора города: верхние категории (дети id=80)."""
    chat_id = cb.message.chat.id
    try:
        city_id = int(cb.data.split(":")[2])
    except Exception:
        city_id = 1

    await clear_bot_messages(chat_id, cb.bot)
    try: await cb.message.delete()
    except Exception: pass

    async with SessionLocal() as s:
        city = (await s.execute(select(City).where(City.id == city_id))).scalar_one_or_none()
        if city is None:
            await cb.answer(await get_text("services_add_city_not_found", "ru") or "Город не найден.", show_alert=True)
            return
        cats = (await s.execute(
            select(Category).where(Category.parent_id == SERVICES_ROOT_CATEGORY_ID).order_by(sql_text("order_num"), Category.name)
        )).scalars().all()

    kb = await _services_categories_kb(cats, city_id=city.id, parent_id=SERVICES_ROOT_CATEGORY_ID)
    city_categories_tmpl = await get_text("services_view_city_categories_tmpl", "ru") or "🛎 Услуги → <b>{city}</b>\nВыберите категорию:"
    text = city_categories_tmpl.format(city=escape_html(city.name))
    msg = await cb.bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"[services_view.py] sv_city ✓ | chat_id={chat_id} user_id={cb.from_user.id} city_id={city_id}")


# ───────────── КАТЕГОРИЯ/ПОДКАТЕГОРИЯ → СПИСОК УСЛУГ ────────────────────────

@router.callback_query(F.data.startswith("sv:cat:"))
async def sv_cat(cb: CallbackQuery):
    """
    Показ подкатегорий или списка услуг.
    Если пришёл флаг ':back' — поднимаемся на уровень выше:
      • если родитель = ROOT (80) → показываем верхние категории города
      • иначе → показываем «сиблинги» (детей родителя)
    """
    chat_id = cb.message.chat.id
    parts = cb.data.split(":")
    city_id = int(parts[2])
    cat_id  = int(parts[3])
    going_back = len(parts) >= 5 and parts[4] == "back"
    # 4-й сегмent — offset страницы списка услуг (когда это не «back»)
    offset = int(parts[4]) if (len(parts) >= 5 and parts[4] != "back" and parts[4].lstrip("-").isdigit()) else 0

    await clear_bot_messages(chat_id, cb.bot)
    try:
        await cb.message.delete()
    except Exception:
        pass

    async with SessionLocal() as s:
        city = (await s.execute(select(City).where(City.id == city_id))).scalar_one_or_none()
        cat  = (await s.execute(select(Category).where(Category.id == cat_id))).scalar_one_or_none()
        if city is None or cat is None:
            await cb.answer(await get_text("services_add_city_or_cat_gone", "ru") or "Город или категория больше недоступны.", show_alert=True)
            return
        children = (await s.execute(
            select(Category).where(Category.parent_id == cat_id).order_by(sql_text("order_num"), Category.name)
        )).scalars().all()

        # ── обработка «Назад» ────────────────────────────────────────────────
        if going_back:
            parent_id = cat.parent_id

            # назад к верхним категориям (дети ROOT=80)
            if not parent_id or parent_id == SERVICES_ROOT_CATEGORY_ID:
                top_cats = (await s.execute(
                    select(Category)
                    .where(Category.parent_id == SERVICES_ROOT_CATEGORY_ID)
                    .order_by(sql_text("order_num"), Category.name)
                )).scalars().all()

                kb = await _services_categories_kb(top_cats, city_id=city_id, parent_id=SERVICES_ROOT_CATEGORY_ID)
                city_categories_tmpl = await get_text("services_view_city_categories_tmpl", "ru") or "🛎 Услуги → <b>{city}</b>\nВыберите категорию:"
                text = city_categories_tmpl.format(city=escape_html(city.name))
                msg = await cb.bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
                last_bot_messages[chat_id] = [msg.message_id]
                await register_bot_messages(chat_id, [msg.message_id])
                await cb.answer()
                print(f"[services_view.py] sv_cat ← back to ROOT | chat_id={chat_id} city_id={city_id}")
                return

            # назад к подкатегориям родителя (сиблинги)
            parent = (await s.execute(select(Category).where(Category.id == parent_id))).scalar_one_or_none()
            if parent is None:
                await cb.answer(await get_text("services_add_city_or_cat_gone", "ru") or "Город или категория больше недоступны.", show_alert=True)
                return
            siblings = (await s.execute(
                select(Category).where(Category.parent_id == parent_id).order_by(sql_text("order_num"), Category.name)
            )).scalars().all()

            kb = await _services_categories_kb(siblings, city_id=city_id, parent_id=parent_id)
            subcat_header_tmpl = await get_text("services_view_subcat_header_tmpl", "ru") or "🛎 Услуги → <b>{city}</b>\nКатегория: <b>{category}</b>\nВыберите подкатегорию:"
            text = subcat_header_tmpl.format(city=escape_html(city.name), category=escape_html(parent.name))
            msg = await cb.bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
            last_bot_messages[chat_id] = [msg.message_id]
            await register_bot_messages(chat_id, [msg.message_id])
            await cb.answer()
            print(f"[services_view.py] sv_cat ← back to parent | chat_id={chat_id} city_id={city_id} parent_id={parent_id}")
            return
        # ────────────────────────────────────────────────────────────────────

    # если есть дети и это не «назад» — показываем подкатегории
    if children:
        kb = await _services_categories_kb(children, city_id=city_id, parent_id=cat_id)
        subcat_header_tmpl = await get_text("services_view_subcat_header_tmpl", "ru") or "🛎 Услуги → <b>{city}</b>\nКатегория: <b>{category}</b>\nВыберите подкатегорию:"
        text = subcat_header_tmpl.format(city=escape_html(city.name), category=escape_html(cat.name))
        msg = await cb.bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(f"[services_view.py] sv_cat → children | chat_id={chat_id} city_id={city_id} cat_id={cat_id} children={len(children)}")
        return

    # листовая категория — показываем услуги
    async with SessionLocal() as s:
        items = (await s.execute(
            select(Listing)
            .where(
                Listing.city_id == city_id,
                Listing.type == "service",
                or_(
                    Listing.category_id == cat_id,
                    Listing.extra_category_id1 == cat_id,
                    Listing.extra_category_id2 == cat_id,
                ),
                or_(Listing.is_sold == 0, Listing.is_sold == False, Listing.is_sold.is_(False)),  # noqa: E712
                Listing.status == "active",
            )
            .order_by(Listing.created_at.desc())
        )).scalars().all()

    if not items:
        # :back поднимает на уровень выше ОТ УКАЗАННОЙ категории —
        # передаём саму категорию (передача родителя прыгала через уровень)
        rows = [await _back_row(f"sv:cat:{city_id}:{cat.id}:back")]

        main_menu_btn = await get_common_menu_button("main_menu", "ru")
        if main_menu_btn:
            rows.append([main_menu_btn])

        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        async with SessionLocal() as s:
            cat_path = await render_category_path(s, cat_id, root_id=SERVICES_ROOT_CATEGORY_ID)
        category_empty_tmpl = await get_text("services_view_category_empty_tmpl", "ru") or "🛎 Услуги → <b>{city}</b> → {cat_path}\nПока пусто в этой категории."
        text = category_empty_tmpl.format(city=escape_html(city.name), cat_path=cat_path)
        msg = await cb.bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(f"[services_view.py] sv_cat → empty | chat_id={chat_id} city_id={city_id} cat_id={cat_id}")
        return

    kb = await _services_listings_kb(items, city_id=city_id, cat_id=cat_id, offset=offset)
    listing_choose_tmpl = await get_text("services_view_listing_choose_tmpl", "ru") or "🛎 Услуги → <b>{city}</b>\nКатегория: <b>{category}</b>\nВыберите услугу:"
    text = listing_choose_tmpl.format(city=escape_html(city.name), category=escape_html(cat.name))
    msg = await cb.bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"[services_view.py] sv_cat → list | chat_id={chat_id} city_id={city_id} cat_id={cat_id} items={len(items)}")





# ─────────────────────────────── КАРТОЧКА УСЛУГИ ─────────────────────────────
@router.callback_query(F.data.startswith("sv:item:"))
async def sv_item(cb: CallbackQuery):
    """
    Карточка услуги.
    Порядок отправки:
      1) Видео (file_id) → Фото → или только текст
      2) Кнопка YouTube (если есть) — отдельным сообщением сразу ПОСЛЕ карточки
      3) «Контакты/Управление» — после кнопки
    Кнопка «Назад»:
      • если карточка открыта из ПОИСКА (метка ':s'), возвращает к результатам поиска
      • иначе возвращает к списку в категории (sv:cat)
    """
    chat_id = cb.message.chat.id

    await clear_bot_messages(chat_id, cb.bot)
    try:
        await cb.message.delete()
    except Exception:
        pass

    # Разбор параметров
    parts = cb.data.split(":")
    listing_id = int(parts[2])
    city_id    = int(parts[3])
    cat_id     = int(parts[4])
    marker = parts[5] if len(parts) >= 6 else ""
    from_search = (marker == "s")
    from_my     = (marker == "m")

    # Читаем объявление
    async with SessionLocal() as s:
        stmt = select(Listing).where(Listing.id == listing_id, Listing.type == "service")
        if not from_my:
            stmt = stmt.where(Listing.status == "active", Listing.is_sold.is_(False))
        listing = (await s.execute(stmt)).scalar_one_or_none()
        city = (await s.execute(select(City).where(City.id == listing.city_id))).scalar_one_or_none() if listing else None
    if not listing or (from_my and listing.owner_id != cb.from_user.id):
        msg = await cb.bot.send_message(chat_id, await get_text("services_view_not_found_or_removed", "ru") or "Объявление не найдено или удалено.")
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(f"[services_view.py] sv_item ✗ not_found | listing_id={listing_id}")
        return
    
    # ЛОГ ОТКРЫТИЯ КАРТОЧКИ
    if from_search:
        source = "search"
    elif from_my:
        source = "my"
    else:
        source = "catalog"

    await log_listing_view(
        listing_id=listing.id,
        user_id=cb.from_user.id,
        section="services",
        action="open",
        source=source,
    )


    # Фото
    photo_ids = []
    if listing.photo_file_id:
        try:
            photo_ids = [x for x in listing.photo_file_id.split(",") if x]
        except Exception:
            photo_ids = []

    # Видео из flex
    video_id = None
    video_url = None
    try:
        flex_vals = json.loads(listing.flex) if listing.flex else {}
        if not isinstance(flex_vals, dict):
            flex_vals = {}
    except Exception:
        flex_vals = {}

    video_key = None
    defs = []
    try:
        async with SessionLocal() as s:
            cat = (await s.execute(select(Category).where(Category.id == listing.category_id))).scalar_one_or_none()
            if cat and cat.fields:
                try:
                    defs = json.loads(cat.fields)
                except Exception:
                    defs = []
    except Exception:
        defs = []

    for fdef in defs:
        if str(fdef.get("type", "")).strip().lower() == "video":
            video_key = (str(fdef.get("key", "")).strip().lower() or "video")
            val = None
            for k, v in flex_vals.items():
                if str(k).strip().lower() == video_key:
                    val = v
                    break
            if isinstance(val, str):
                sval = val.strip()
                low = sval.lower()
                if "http" in low or "://" in sval:
                    video_url = sval
                else:
                    video_id = sval
            break

    if not video_id and not video_url and flex_vals:
        for cand in ["video", "video_url", "youtube", "yt", "link", "url"]:
            for k, v in flex_vals.items():
                if str(k).strip().lower() == cand:
                    if isinstance(v, str):
                        sval = v.strip()
                        low = sval.lower()
                        if "http" in low or "://" in sval:
                            video_url = sval
                        else:
                            video_id = sval
                    break
            if video_id or video_url:
                break

    print(f"[services_view.py] sv_item | parsed_video | listing_id={listing_id} | key={video_key} | video_id={bool(video_id)} | video_url={video_url}")

    # Основные блоки
    async with SessionLocal() as s:
        category_path = await render_category_path(s, cat_id, root_id=SERVICES_ROOT_CATEGORY_ID)
    if category_path:
        category_path_tmpl = await get_text("services_view_card_category_path_tmpl", "ru") or "Категория: <b>Услуги → {path}</b>"
        category_line = category_path_tmpl.format(path=category_path)
    else:
        category_line = await get_text("services_view_card_category_root", "ru") or "Категория: <b>Услуги</b>"

    city_tmpl = await get_text("vacancy_card_city", "ru") or "Город: <b>{name}</b>"
    city_line = city_tmpl.format(name=escape_html(city.name)) if city else ""
    price_label = (await get_text("service_price", "ru")) or (await get_text("listing_price", "ru")) or "Стоимость услуг"
    title_line = f"<b>{escape_html((listing.title or '').strip())}</b>" if listing.title else ""
    descr_line = escape_html((listing.descr or "").strip())
    price_line = f"{escape_html(price_label)}: {escape_html(listing.price)}" if listing.price else ""
    main_block = "\n\n".join([p for p in [city_line, category_line, title_line, descr_line, price_line] if p])

    async with SessionLocal() as s:
        flex_block = await render_flex_block(s, listing, lang="ru")
    contact_block = await render_contact(listing, lang="ru")

    caption_parts = [main_block]
    if flex_block:
        caption_parts.append(flex_block)

    # Если есть YouTube/URL — добавляем ссылку перед контактами
    if video_url:
        video_line_tmpl = await get_text("services_view_video_line_tmpl", "ru") or "Видео: {url}"
        caption_parts.append(video_line_tmpl.format(url=escape_html(video_url)))

    if contact_block:
        caption_parts.append(contact_block)

    caption = "\n\n".join([p for p in caption_parts if p]) or " "

    is_owner = listing.owner_id == cb.from_user.id
    management_text = await get_text("vacancy_contacts_mgmt_label", "ru") or "Контакты/Управление:"
    if is_owner:
        left_line = days_left_text(listing)
        if left_line:
            management_text += f"\n{left_line}"

    # Кнопки управления
    if from_search:
        back_cb = "services_search_back"
    elif from_my:
        back_cb = "my_services"
    else:
        back_cb = f"sv:cat:{city_id}:{cat_id}"

    buttons = []

    if is_owner:
        edit_btn = await get_common_menu_button("btn_edit_service", "ru")
        buttons.append([InlineKeyboardButton(
            text=(edit_btn.text if edit_btn else (await get_text("vac_edit_all", "ru") or "✏️ Редактировать все поля")),
            callback_data=f"service_edit_overview:{listing.id}"
        )])
        if is_active(listing):
            buttons.append([InlineKeyboardButton(
                text=(await get_text("vacancy_btn_archive", "ru") or "📦 Закрыть (в архив)"),
                callback_data=f"service_close:{listing.id}:{urllib.parse.quote(back_cb, safe='')}"
            )])
        del_btn = await get_common_menu_button("btn_delete_service", "ru")
        buttons.append([InlineKeyboardButton(
            text=(del_btn.text if del_btn else (await get_text("services_view_btn_delete_listing", "ru") or "❌ Удалить объявление")),
            callback_data=f"sell_sold:{listing.id}"
        )])

        if should_show_extend_button(listing):
            buttons.append([InlineKeyboardButton(
                text=(await get_text("vacancy_btn_extend", "ru") or "🔄 Продлить на 30 дней"),
                callback_data=f"service_extend:{listing.id}:{urllib.parse.quote(back_cb, safe='')}"
            )])
    elif listing.contact and listing.contact.startswith("@"):
        c_btn = await get_common_menu_button("btn_contact_provider", "ru")
        buttons.append([InlineKeyboardButton(
            text=(c_btn.text if c_btn else (await get_text("vacancy_btn_contact", "ru") or "💬 Связаться")),
            url=build_contact_url(listing.id, listing.contact, cb.from_user.id, source),
        )])

    # Кнопки навигации — в конец
    buttons.append(await _back_row(back_cb))

    main_btn = await get_common_menu_button("main_menu", "ru")
    if main_btn:
        buttons.append([main_btn])

    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    sent_ids = []

    # 1) Видео-файл
    if video_id:
        try:
            media = [InputMediaVideo(media=video_id, caption=caption, parse_mode="HTML")]
            for pid in photo_ids:
                media.append(InputMediaPhoto(media=pid))
            msgs = await cb.message.answer_media_group(media=media)
            sent_ids.extend([m.message_id for m in msgs])
        except Exception:
            t = await cb.message.answer(caption, parse_mode="HTML")
            sent_ids.append(t.message_id)

        if video_url and WEBAPP_BASE:
            mid = await _send_yt_button(cb, video_url, listing.id)
            if mid:
                sent_ids.append(mid)

        ctl = await cb.message.answer(management_text, reply_markup=markup)
        sent_ids.append(ctl.message_id)

        sent_photo_messages[chat_id] = sent_ids
        await register_bot_messages(chat_id, sent_ids)
        await cb.answer()
        print(f"[services_view.py] sv_item ✓ video-first | listing_id={listing_id} sent={len(sent_ids)}")
        return

    # 2) Фото
    if photo_ids:
        if len(photo_ids) == 1:
            try:
                m1 = await cb.message.answer_photo(photo_ids[0], caption=caption, parse_mode="HTML")
                sent_ids.append(m1.message_id)
            except Exception:
                m1 = await cb.message.answer(caption, parse_mode="HTML")
                sent_ids.append(m1.message_id)
        else:
            try:
                media = [InputMediaPhoto(media=photo_ids[0], caption=caption, parse_mode="HTML")] + \
                        [InputMediaPhoto(media=pid) for pid in photo_ids[1:]]
                msgs = await cb.message.answer_media_group(media=media)
                sent_ids.extend([m.message_id for m in msgs])
            except Exception:
                for i, pid in enumerate(photo_ids):
                    try:
                        if i == 0:
                            mm = await cb.message.answer_photo(pid, caption=caption, parse_mode="HTML")
                        else:
                            mm = await cb.message.answer_photo(pid)
                        sent_ids.append(mm.message_id)
                    except Exception:
                        pass

        if video_url and WEBAPP_BASE:
            mid = await _send_yt_button(cb, video_url, listing.id)
            if mid:
                sent_ids.append(mid)

        ctl = await cb.message.answer(management_text, reply_markup=markup)
        sent_ids.append(ctl.message_id)

        sent_photo_messages[chat_id] = sent_ids
        await register_bot_messages(chat_id, sent_ids)
        await cb.answer()
        print(f"[services_view.py] sv_item ✓ photos(+yt?) | listing_id={listing_id} sent={len(sent_ids)}")
        return

    # 3) Только текст
    tmsg = await cb.message.answer(caption, parse_mode="HTML")
    last_bot_messages[chat_id] = [tmsg.message_id]

    if video_url and WEBAPP_BASE:
        mid = await _send_yt_button(cb, video_url, listing.id)
        if mid:
            last_bot_messages[chat_id].append(mid)

    ctl = await cb.message.answer(management_text, reply_markup=markup)
    last_bot_messages[chat_id].append(ctl.message_id)
    await register_bot_messages(chat_id, last_bot_messages[chat_id])

    await cb.answer()
    print(f"[services_view.py] sv_item ✓ text-only(+yt?) | listing_id={listing_id}")





# RU: Продление услуги на 30 дней — только автор. Редактируем только блок управления.
@router.callback_query(F.data.startswith("service_extend:"))
async def service_extend_listing(cb: CallbackQuery):
    parts = cb.data.split(":", 2)
    if len(parts) < 3:
        await cb.answer(await get_text("vacancy_extend_data_error", "ru") or "Ошибка данных продления.", show_alert=True)
        return

    try:
        listing_id = int(parts[1])
    except ValueError:
        await cb.answer(await get_text("services_view_invalid_service_id", "ru") or "Неверный идентификатор услуги.", show_alert=True)
        return

    back_cb = urllib.parse.unquote(parts[2])

    async with SessionLocal() as s:
        listing = (await s.execute(
            select(Listing).where(Listing.id == listing_id, Listing.type == "service")
        )).scalar_one_or_none()
        if not listing:
            await cb.answer(await get_text("services_view_not_found", "ru") or "Услуга не найдена.", show_alert=True)
            return
        if listing.owner_id != cb.from_user.id:
            await cb.answer(await get_text("services_view_extend_owner_only", "ru") or "Продлить может только автор услуги.", show_alert=True)
            return

        if not should_show_extend_button(listing):
            # Либо снято с публикации (admin_removed/unpublished), либо до
            # истечения ещё далеко — старый callback не должен накручивать срок.
            await cb.answer(
                await get_text("vacancy_extend_unavailable", "ru") or "Продление сейчас недоступно. Кнопка появится за 5 дней до истечения срока.",
                show_alert=True,
            )
            return

        extend_listing(listing)
        await s.commit()
        await s.refresh(listing)

    from app.analytics import log_event
    await log_event("listing_extended", user_id=cb.from_user.id,
                    section="services", entity_type="listing", entity_id=listing.id)

    management_text = await get_text("vacancy_contacts_mgmt_label", "ru") or "Контакты/Управление:"
    left_line = days_left_text(listing)
    if left_line:
        management_text += f"\n{left_line}"

    buttons = []

    edit_btn = await get_common_menu_button("btn_edit_service", "ru")
    buttons.append([InlineKeyboardButton(
        text=(edit_btn.text if edit_btn else (await get_text("vac_edit_all", "ru") or "✏️ Редактировать все поля")),
        callback_data=f"service_edit_overview:{listing.id}"
    )])

    if is_active(listing):
        buttons.append([InlineKeyboardButton(
            text=(await get_text("vacancy_btn_archive", "ru") or "📦 Закрыть (в архив)"),
            callback_data=f"service_close:{listing.id}:{urllib.parse.quote(back_cb, safe='')}"
        )])

    del_btn = await get_common_menu_button("btn_delete_service", "ru")
    buttons.append([InlineKeyboardButton(
        text=(del_btn.text if del_btn else (await get_text("services_view_btn_delete_listing", "ru") or "❌ Удалить объявление")),
        callback_data=f"sell_sold:{listing.id}"
    )])

    if should_show_extend_button(listing):
        buttons.append([InlineKeyboardButton(
            text=(await get_text("vacancy_btn_extend", "ru") or "🔄 Продлить на 30 дней"),
            callback_data=f"service_extend:{listing.id}:{urllib.parse.quote(back_cb, safe='')}"
        )])

    buttons.append(await _back_row(back_cb))

    main_btn = await get_common_menu_button("main_menu", "ru")
    if main_btn:
        buttons.append([main_btn])

    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    try:
        await cb.message.edit_text(management_text, reply_markup=markup)
    except Exception:
        try:
            await cb.message.delete()
        except Exception:
            pass
        msg = await cb.message.answer(management_text, reply_markup=markup)
        my_listing_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
        await register_bot_messages(cb.message.chat.id, [msg.message_id])

    await cb.answer(await get_text("services_view_extended", "ru") or "Услуга продлена на 30 дней.")
    print(
        f"[services_view.py] service_extend_listing ✓ | "
        f"listing_id={listing.id} chat_id={cb.message.chat.id} user_id={cb.from_user.id}"
    )


# RU: «Закрыть (в архив)» — скрыть свою услугу из выдачи, не удаляя.
#     Вернуть можно кнопкой «Вернуть в каталог» (service_extend реактивирует).
@router.callback_query(F.data.startswith("service_close:"))
async def service_close_listing(cb: CallbackQuery):
    parts = cb.data.split(":", 2)
    if len(parts) < 3:
        await cb.answer(await get_text("vacancy_close_data_error", "ru") or "Ошибка данных закрытия.", show_alert=True)
        return

    try:
        listing_id = int(parts[1])
    except ValueError:
        await cb.answer(await get_text("services_view_invalid_service_id", "ru") or "Неверный идентификатор услуги.", show_alert=True)
        return

    back_cb = urllib.parse.unquote(parts[2])

    async with SessionLocal() as s:
        listing = (await s.execute(
            select(Listing).where(Listing.id == listing_id, Listing.type == "service")
        )).scalar_one_or_none()
        if not listing:
            await cb.answer(await get_text("services_view_not_found", "ru") or "Услуга не найдена.", show_alert=True)
            return
        if listing.owner_id != cb.from_user.id:
            await cb.answer(await get_text("services_view_close_owner_only", "ru") or "Закрыть может только автор услуги.", show_alert=True)
            return
        if not is_active(listing):
            await cb.answer(await get_text("services_view_already_closed", "ru") or "Услуга уже закрыта или в архиве.", show_alert=True)
            return

        archive_as_closed(listing, user_id=cb.from_user.id)
        await s.commit()
        await s.refresh(listing)

    from app.analytics import log_event
    try:
        await log_event("listing_closed", user_id=cb.from_user.id,
                        section="services", entity_type="listing", entity_id=listing.id)
    except Exception as e:
        print(f"[services_view.py] service_close analytics error listing_id={listing.id}: {e}")

    management_text = await get_text("services_view_closed_management_tmpl", "ru") or (
        "Контакты/Управление:\n"
        "🔴 Услуга закрыта и скрыта из каталога.\n"
        "Вернуть её можно кнопкой ниже — текст и фото сохранены."
    )

    buttons = []

    buttons.append([InlineKeyboardButton(
        text=(await get_text("vacancy_btn_restore", "ru") or "↩️ Вернуть в каталог (на 30 дней)"),
        callback_data=f"service_extend:{listing.id}:{urllib.parse.quote(back_cb, safe='')}"
    )])

    edit_btn = await get_common_menu_button("btn_edit_service", "ru")
    buttons.append([InlineKeyboardButton(
        text=(edit_btn.text if edit_btn else (await get_text("vac_edit_all", "ru") or "✏️ Редактировать все поля")),
        callback_data=f"service_edit_overview:{listing.id}"
    )])

    del_btn = await get_common_menu_button("btn_delete_service", "ru")
    buttons.append([InlineKeyboardButton(
        text=(del_btn.text if del_btn else (await get_text("services_view_btn_delete_listing", "ru") or "❌ Удалить объявление")),
        callback_data=f"sell_sold:{listing.id}"
    )])

    buttons.append(await _back_row(back_cb))

    main_btn = await get_common_menu_button("main_menu", "ru")
    if main_btn:
        buttons.append([main_btn])

    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    try:
        await cb.message.edit_text(management_text, reply_markup=markup)
    except Exception:
        try:
            await cb.message.delete()
        except Exception:
            pass
        msg = await cb.message.answer(management_text, reply_markup=markup)
        my_listing_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
        await register_bot_messages(cb.message.chat.id, [msg.message_id])

    await cb.answer(await get_text("services_view_closed", "ru") or "Услуга закрыта.")
    print(
        f"[services_view.py] service_close_listing ✓ | "
        f"listing_id={listing.id} chat_id={cb.message.chat.id} user_id={cb.from_user.id}"
    )


# ───────────────────────────── «МОИ УСЛУГИ» ─────────────────────────────────

MY_SERVICES_PAGE_SIZE = 10


async def _render_my_services(cb: CallbackQuery, offset: int = 0):
    """Список услуг текущего пользователя (type='service') с пагинацией.
    Архивные помечены 📦; всегда есть «Назад» и «Главное меню»."""
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.message.bot)
    try: await cb.message.delete()
    except Exception: pass

    async with SessionLocal() as s:
        items = (await s.execute(
            select(Listing)
            .where(
                Listing.owner_id == cb.from_user.id,
                Listing.type == "service",
            )
            .order_by(Listing.created_at.desc())
        )).scalars().all()

    main_btn = await get_common_menu_button("main_menu", "ru")

    if not items:
        rows = [await _back_row("go_services")]
        if main_btn:
            rows.append([main_btn])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        msg = await cb.bot.send_message(chat_id, await get_text("services_view_my_empty", "ru") or "У вас пока нет услуг.", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(f"[services_view.py] my_services ✓ empty | chat_id={chat_id} user_id={cb.from_user.id}")
        return

    total = len(items)
    pages = max(1, (total + MY_SERVICES_PAGE_SIZE - 1) // MY_SERVICES_PAGE_SIZE)
    if offset >= total:
        offset = (pages - 1) * MY_SERVICES_PAGE_SIZE
    if offset < 0:
        offset = 0
    page = offset // MY_SERVICES_PAGE_SIZE + 1

    rows = []
    for it in items[offset:offset + MY_SERVICES_PAGE_SIZE]:
        marker = "📦 " if it.status == "archived" else ""
        rows.append([InlineKeyboardButton(
            text=(marker + (it.title or f'#{it.id}'))[:64],
            callback_data=f"sv:item:{it.id}:{it.city_id}:{it.category_id}:m"
        )])

    if pages > 1:
        pager = []
        if offset > 0:
            pager.append(InlineKeyboardButton(
                text="«", callback_data=f"my_services_page:{offset - MY_SERVICES_PAGE_SIZE}"))
        pager.append(InlineKeyboardButton(text=f"{page}/{pages}", callback_data="stub"))
        if offset + MY_SERVICES_PAGE_SIZE < total:
            pager.append(InlineKeyboardButton(
                text="»", callback_data=f"my_services_page:{offset + MY_SERVICES_PAGE_SIZE}"))
        rows.append(pager)

    rows.append(await _back_row("go_services"))
    if main_btn:
        rows.append([main_btn])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    suffix = f" ({total})" if pages > 1 else ""
    my_title_tmpl = await get_text("services_view_my_title_tmpl", "ru") or "<b>Мои услуги{suffix}:</b>"
    msg = await cb.bot.send_message(chat_id, my_title_tmpl.format(suffix=suffix), reply_markup=kb, parse_mode="HTML")
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"[services_view.py] my_services ✓ | chat_id={chat_id} user_id={cb.from_user.id} count={total} offset={offset}")


@router.callback_query(F.data == "my_services")
async def my_services(cb: CallbackQuery):
    await _render_my_services(cb, offset=0)


@router.callback_query(F.data.startswith("my_services_page:"))
async def my_services_page(cb: CallbackQuery):
    try:
        offset = int(cb.data.split(":")[1])
    except (ValueError, IndexError):
        offset = 0
    await _render_my_services(cb, offset=offset)


# ───────────────────────────── «ПОИСК УСЛУГ» ────────────────────────────────

# Заметка: ранее здесь была заглушка поиска услуг, выводившая сообщение «в разработке».
# Для полноценной реализации поиска, хендлер ниже (services_search_start) обрабатывает тот же
# callback_data `services_search`, поэтому заглушка удалена, чтобы не перехватывать запрос.


# ───────────────────────────── «ПОИСК УСЛУГ» ────────────────────────────────

@router.callback_query(F.data == "services_search_back")
async def services_search_back(cb: CallbackQuery, state: FSMContext):
    """
    Возврат к последним результатам поиска услуг.
    Берём ids из state или из services_search_ctx_by_chat.
    """
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    try:
        await cb.message.delete()
    except Exception:
        pass

    data = await state.get_data()
    ids   = data.get("search_results") or []
    query = data.get("search_query") or ""

    if not ids:
        ctx = services_search_ctx_by_chat.get(chat_id) or {}
        ids   = ctx.get("ids") or []
        query = ctx.get("query") or query

    if not ids:
        rows = [
            [InlineKeyboardButton(text=(await get_text("vacancy_btn_new_search", "ru") or "🔄 Новый поиск"), callback_data="services_search_new")],
            await _back_row("go_services"),
        ]
        main_btn = await get_common_menu_button("main_menu", "ru")
        if main_btn:
            rows.append([main_btn])

        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        msg = await cb.message.answer(await get_text("services_view_search_no_results_return", "ru") or "Результаты поиска не найдены. Начните новый поиск.", reply_markup=kb)
        services_last_search_menu_message[chat_id] = msg.message_id
        services_last_search_query_message[chat_id] = msg.message_id
        await register_bot_messages(chat_id, [msg.message_id])
        await state.clear()
        await cb.answer()
        return

    # Перепроверяем сохранённые ids: услуга могла быть архивирована после поиска.
    ids, results = await _load_public_service_ids(ids)
    # Восстановленный из RAM контекст кладём в FSM целиком (query тоже),
    # иначе следующий возврат прочитает из FSM пустой запрос.
    await state.update_data(search_results=ids, search_query=query)
    # Обновляем ids/query, не затирая offset и прочий контекст поиска.
    prev_ctx = services_search_ctx_by_chat.get(chat_id) or {}
    services_search_ctx_by_chat[chat_id] = {**prev_ctx, "ids": ids, "query": query}

    if not ids:
        # Например, закрыли единственную услугу из результатов — не бросаем
        # пользователя на экране без навигации.
        rows = [
            [InlineKeyboardButton(text=(await get_text("vacancy_btn_new_search", "ru") or "🔄 Новый поиск"), callback_data="services_search_new")],
            await _back_row("go_services"),
        ]
        main_btn = await get_common_menu_button("main_menu", "ru")
        if main_btn:
            rows.append([main_btn])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        msg = await cb.message.answer(
            await get_text("services_view_search_all_gone", "ru") or "Все услуги из результатов уже недоступны. Начните новый поиск.",
            reply_markup=kb,
        )
        await register_bot_messages(chat_id, [msg.message_id])
        await state.clear()
        await cb.answer()
        return

    # Возвращаемся на ту же страницу, с которой открывали карточку;
    # после ревалидации offset мог выехать за край — прижимаем.
    # FSM — первичен (переживает рестарт), RAM-кэш — запасной вариант.
    offset = int(data.get("search_offset")
                 or (services_search_ctx_by_chat.get(chat_id) or {}).get("offset")
                 or 0)
    total_count = len(ids)
    pages = max(1, (total_count + SERVICES_SEARCH_PAGE_SIZE - 1) // SERVICES_SEARCH_PAGE_SIZE)
    if offset >= total_count:
        offset = (pages - 1) * SERVICES_SEARCH_PAGE_SIZE
    services_search_ctx_by_chat[chat_id]["offset"] = offset
    await state.update_data(search_offset=offset)
    page = offset // SERVICES_SEARCH_PAGE_SIZE + 1
    results_page = results[offset:offset + SERVICES_SEARCH_PAGE_SIZE]

    # строим клавиатуру — обязательно помечаем ':s', чтобы карточка знала, что мы из поиска
    rows = []
    for r in results_page:
        title = (r.title or f"#{r.id}")[:64]
        rows.append([InlineKeyboardButton(
            text=title,
            callback_data=f"sv:item:{r.id}:{r.city_id}:{r.category_id}:s"
        )])

    if pages > 1:
        pager_row = []
        if page > 1:
            pager_row.append(InlineKeyboardButton(
                text="«", callback_data=f"services_search_page:{max(0, offset - SERVICES_SEARCH_PAGE_SIZE)}"))
        pager_row.append(InlineKeyboardButton(text=f"{page}/{pages}", callback_data="stub"))
        if page < pages:
            pager_row.append(InlineKeyboardButton(
                text="»", callback_data=f"services_search_page:{offset + SERVICES_SEARCH_PAGE_SIZE}"))
        rows.append(pager_row)

    rows.append([InlineKeyboardButton(text=(await get_text("vacancy_btn_new_search", "ru") or "🔄 Новый поиск"), callback_data="services_search_new")])
    rows.append(await _back_row("go_services"))

    main_btn = await get_common_menu_button("main_menu", "ru")
    if main_btn:
        rows.append([main_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    search_found_tmpl = await get_text("services_view_search_found_tmpl", "ru") or "{correction_note}🔎 Найдено: <b>{count}</b> по запросу: <b>{query}</b>\n\nВыберите услугу:"
    msg = await cb.message.answer(
        search_found_tmpl.format(correction_note="", count=total_count, query=escape_html(query)),
        reply_markup=kb,
        parse_mode="HTML"
    )
    services_last_search_menu_message[chat_id] = msg.message_id
    services_last_search_query_message[chat_id] = msg.message_id
    await register_bot_messages(chat_id, [msg.message_id])
    await state.set_state(ServiceSearch.waiting_for_detail)
    await cb.answer()
    print(f"[services_view.py] services_search_back ✓ | chat_id={chat_id} results={len(results)} query={query!r}")





@router.callback_query(F.data == "services_search")
async def services_search_start(cb: CallbackQuery, state: FSMContext):
    """Старт поиска услуг — как в Барахолке: плашка «назад/главное» + запрос."""
    chat_id = cb.message.chat.id

    await clear_bot_messages(chat_id, cb.bot)
    try:
        await cb.message.delete()
    except Exception:
        pass

    # чистим только СВОИ служебные сообщения поиска
    for mid in (services_last_search_menu_message.pop(chat_id, None),
                services_last_search_query_message.pop(chat_id, None)):
        if mid:
            try:
                await cb.bot.delete_message(chat_id, mid)
            except Exception:
                pass

    # плашка «Назад/Главное меню»
    back_btn = await get_common_menu_button('back')
    if back_btn:
        back_btn.callback_data = "go_services"
    main_btn = await get_common_menu_button('main_menu')
    nav_buttons = [b for b in (back_btn, main_btn) if b]
    nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons]) if nav_buttons else None

    nav_text    = await get_text('return_to_menu', 'ru') or "Возврат"
    query_text  = await get_text('services_search_query', 'ru') or "Введите запрос для поиска услуг:"

    search_header_tmpl = await get_text("services_view_search_header_tmpl", "ru") or "🔎 <b>Поиск услуг</b>\n\n{query_text}"
    nav_msg   = await cb.bot.send_message(chat_id, nav_text, reply_markup=nav_markup)
    query_msg = await cb.bot.send_message(
        chat_id, search_header_tmpl.format(query_text=query_text), parse_mode="HTML")

    services_last_search_menu_message[chat_id]  = nav_msg.message_id
    services_last_search_query_message[chat_id] = query_msg.message_id
    await register_bot_messages(chat_id, [nav_msg.message_id, query_msg.message_id])

    # 👇 ДОБАВЬТЕ ЭТО:
    # 1) в глобальные кеши — их чистит main_menu_cb
    from app.routers.utils import last_search_menu_message, last_search_query_message
    last_search_menu_message[chat_id]  = nav_msg.message_id
    last_search_query_message[chat_id] = query_msg.message_id

    # 2) в FSM — если у вас main_menu_cb вызывает _drop_nav_and_prompt(...)
    try:
        await state.update_data(nav_msg_id=nav_msg.message_id, prompt_id=query_msg.message_id)
    except Exception:
        pass


    await state.set_state(ServiceSearch.waiting_for_query)
    await cb.answer()
    print(f"[services_view.py] services_search_start ✓ | chat_id={chat_id} user_id={cb.from_user.id}")


@router.message(StateFilter(ServiceSearch.waiting_for_query, ServiceSearch.waiting_for_detail))
async def handle_services_search_query(m: Message, state: FSMContext):
    chat_id = m.chat.id
    query = (m.text or "").strip()

    # удаляем сообщение пользователя
    try:
        await m.delete()
    except Exception:
        try:
            await m.bot.delete_message(chat_id, m.message_id)
        except Exception:
            pass

    if not query:
        msg = await m.answer(await get_text("services_view_search_empty_query", "ru") or "Пустой запрос. Введите текст запроса или нажмите «🔄 Новый поиск».")
        service_search_messages[chat_id].append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])
        await state.set_state(ServiceSearch.waiting_for_query)
        return

    await clear_bot_messages(chat_id, m.bot)

    for mid in (
        services_last_search_menu_message.pop(chat_id, None),
        services_last_search_query_message.pop(chat_id, None),
    ):
        if mid:
            try:
                await m.bot.delete_message(chat_id, mid)
            except Exception:
                pass

    for mid in service_search_messages.pop(chat_id, []):
        try:
            await m.bot.delete_message(chat_id, mid)
        except Exception:
            pass

    async with SessionLocal() as s:
        stmt = (
            select(Listing)
            .where(*_service_public_predicates())
            .order_by(Listing.created_at.desc())
            .limit(500)
        )
        rows = (await s.execute(stmt)).scalars().all()

    search_outcome = search_items(
        rows,
        query,
        lambda it: [
            it.title or "",
            it.descr or "",
        ],
    )

    results = search_outcome.results
    search_query_raw = search_outcome.query_raw
    search_query_normalized = search_outcome.query_normalized
    search_query_effective = search_outcome.query_effective
    search_match_mode = search_outcome.match_mode

    total_count = len(results)
    pages = max(1, (total_count + SERVICES_SEARCH_PAGE_SIZE - 1) // SERVICES_SEARCH_PAGE_SIZE)
    page = 1
    results_page = results[:SERVICES_SEARCH_PAGE_SIZE]

    # 🔥 ЛОГИРОВАНИЕ
    await log_search(
        user_id=m.from_user.id,
        section="services",
        query_raw=search_query_raw,
        query_normalized=search_query_normalized,
        query_effective=search_query_effective,
        match_mode=search_match_mode,
        results_count=total_count,
    )

    new_search_btn = InlineKeyboardButton(text=(await get_text("vacancy_btn_new_search", "ru") or "🔄 Новый поиск"), callback_data="services_search_new")
    back_btn = await get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data="go_services")
    back_btn.callback_data = "go_services"
    main_btn = await get_common_menu_button("main_menu", "ru")

    if not results:
        rows_kb = [[new_search_btn], [back_btn]]
        if main_btn:
            rows_kb.append([main_btn])

        kb = InlineKeyboardMarkup(inline_keyboard=rows_kb)

        nothing_found_tmpl = await get_text("services_view_search_nothing_found_tmpl", "ru") or "😕 Ничего не найдено по запросу: <b>{query}</b>"
        msg = await m.answer(
            nothing_found_tmpl.format(query=escape_html(query)),
            reply_markup=kb,
            parse_mode="HTML"
        )

        services_last_search_menu_message[chat_id] = msg.message_id
        services_last_search_query_message[chat_id] = msg.message_id
        await register_bot_messages(chat_id, [msg.message_id])

        await state.set_state(ServiceSearch.waiting_for_query)

        print(
            f"[services_view.py] handle_services_search_query | "
            f"chat_id={chat_id} results=0 query={query!r} "
            f"match_mode={search_match_mode} effective={search_query_effective!r}"
        )
        return

    ids = [row.id for row in results]

    await state.update_data(
        search_results=ids,
        search_query=query,
        search_offset=0,
        search_query_raw=search_query_raw,
        search_query_normalized=search_query_normalized,
        search_query_effective=search_query_effective,
        search_match_mode=search_match_mode,
    )

    services_search_ctx_by_chat[chat_id] = {
        "ids": ids,
        "query": query,
        "offset": 0,
        "query_raw": search_query_raw,
        "query_normalized": search_query_normalized,
        "query_effective": search_query_effective,
        "match_mode": search_match_mode,
    }

    rows_kb = []

    for row in results_page:
        title = (row.title or f"#{row.id}")[:64]
        rows_kb.append([InlineKeyboardButton(
            text=title,
            callback_data=f"sv:item:{row.id}:{row.city_id}:{row.category_id}:s"
        )])

    if pages > 1:
        pager_row = [
            InlineKeyboardButton(text=f"{page}/{pages}", callback_data="stub"),
            InlineKeyboardButton(
                text="»",
                callback_data=f"services_search_page:{SERVICES_SEARCH_PAGE_SIZE}"
            )
        ]
        rows_kb.append(pager_row)

    rows_kb.append([new_search_btn])
    rows_kb.append([back_btn])
    if main_btn:
        rows_kb.append([main_btn])

    correction_note = ""
    if search_match_mode == "corrected" and search_query_effective != search_query_normalized:
        correction_note_tmpl = await get_text("search_typo_correction_note", "ru") or "🧠 Показаны результаты по запросу: <b>{query}</b> (учтена возможная опечатка).\n\n"
        correction_note = correction_note_tmpl.format(query=escape_html(search_query_effective))

    kb = InlineKeyboardMarkup(inline_keyboard=rows_kb)

    search_found_tmpl = await get_text("services_view_search_found_tmpl", "ru") or "{correction_note}🔎 Найдено: <b>{count}</b> по запросу: <b>{query}</b>\n\nВыберите услугу:"
    msg = await m.answer(
        search_found_tmpl.format(correction_note=correction_note, count=total_count, query=escape_html(query)),
        reply_markup=kb,
        parse_mode="HTML"
    )

    services_last_search_menu_message[chat_id] = msg.message_id
    services_last_search_query_message[chat_id] = msg.message_id
    await register_bot_messages(chat_id, [msg.message_id])

    await state.set_state(ServiceSearch.waiting_for_detail)

    print(
        f"[services_view.py] handle_services_search_query | "
        f"chat_id={chat_id} results={total_count} query={query!r} "
        f"match_mode={search_match_mode} effective={search_query_effective!r}"
    )

    
    

@router.callback_query(F.data == "services_search_new")
async def services_search_new(cb: CallbackQuery, state: FSMContext):
    """Сброс текущих результатов и запрос нового текста (без дублей)."""
    chat_id = cb.message.chat.id

    for mid in (services_last_search_menu_message.pop(chat_id, None),
                services_last_search_query_message.pop(chat_id, None)):
        if mid:
            try: await cb.bot.delete_message(chat_id, mid)
            except Exception: pass
    for mid in service_search_messages.pop(chat_id, []):
        try: await cb.bot.delete_message(chat_id, mid)
        except Exception: pass

    await state.clear()

    back_btn = await get_common_menu_button('back')
    if back_btn:
        back_btn.callback_data = "go_services"
    main_btn = await get_common_menu_button('main_menu')
    nav_buttons = [b for b in (back_btn, main_btn) if b]
    nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons]) if nav_buttons else None

    nav_text   = await get_text('return_to_menu', 'ru') or "Возврат"
    query_text = await get_text('services_search_query', 'ru') or "Введите запрос для поиска услуг:"

    nav_msg   = await cb.bot.send_message(chat_id, nav_text,  reply_markup=nav_markup)
    query_msg = await cb.bot.send_message(chat_id, query_text)

    services_last_search_menu_message[chat_id]  = nav_msg.message_id
    services_last_search_query_message[chat_id] = query_msg.message_id
    await register_bot_messages(chat_id, [nav_msg.message_id, query_msg.message_id])

    await state.set_state(ServiceSearch.waiting_for_query)
    await cb.answer()
    print(f"[services_view.py] services_search_new ✓ | chat_id={chat_id}")


@router.callback_query(F.data.startswith("services_search_page:"))
async def services_search_page(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # Канон: сначала зачистка старого экрана
    await clear_bot_messages(chat_id, cb.bot)
    try:
        await cb.message.delete()
    except Exception:
        pass

    try:
        offset = int(cb.data.split(":")[1])
    except Exception:
        offset = 0

    ctx = services_search_ctx_by_chat.get(chat_id) or {}
    ids = ctx.get("ids") or []
    query = ctx.get("query") or ""

    # RAM-кэш пуст (например, после рестарта) — поднимаем из FSM (SQLite).
    if not ids:
        data = await state.get_data()
        ids = data.get("search_results") or []
        query = data.get("search_query") or ""

    if not ids:
        msg = await cb.message.answer(await get_text("services_view_search_context_lost", "ru") or "Контекст поиска потерян. Начните новый поиск.")
        services_last_search_menu_message[chat_id] = msg.message_id
        services_last_search_query_message[chat_id] = msg.message_id
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        return

    ids, valid_results = await _load_public_service_ids(ids)

    # Старая кнопка пагинации могла нести offset за пределами актуального
    # списка (результаты «сжались») — прижимаем к последней странице.
    total_count = len(ids)
    pages = max(1, (total_count + SERVICES_SEARCH_PAGE_SIZE - 1) // SERVICES_SEARCH_PAGE_SIZE)
    if offset >= total_count:
        offset = (pages - 1) * SERVICES_SEARCH_PAGE_SIZE
    page = (offset // SERVICES_SEARCH_PAGE_SIZE) + 1

    await state.update_data(search_results=ids, search_offset=offset)
    services_search_ctx_by_chat[chat_id] = {**ctx, "ids": ids, "query": query, "offset": offset}
    results = valid_results[offset:offset + SERVICES_SEARCH_PAGE_SIZE]

    rows = []
    for row in results:
        rows.append([InlineKeyboardButton(
            text=(row.title or f"#{row.id}")[:64],
            callback_data=f"sv:item:{row.id}:{row.city_id}:{row.category_id}:s"
        )])

    if pages > 1:
        pager_row = []

        if page > 1:
            prev_offset = max(0, offset - SERVICES_SEARCH_PAGE_SIZE)
            pager_row.append(
                InlineKeyboardButton(
                    text="«",
                    callback_data=f"services_search_page:{prev_offset}"
                )
            )

        pager_row.append(
            InlineKeyboardButton(
                text=f"{page}/{pages}",
                callback_data="stub"
            )
        )

        if page < pages:
            next_offset = offset + SERVICES_SEARCH_PAGE_SIZE
            pager_row.append(
                InlineKeyboardButton(
                    text="»",
                    callback_data=f"services_search_page:{next_offset}"
                )
            )

        rows.append(pager_row)

    rows.append([InlineKeyboardButton(text=(await get_text("vacancy_btn_new_search", "ru") or "🔄 Новый поиск"), callback_data="services_search_new")])
    rows.append(await _back_row("go_services"))

    main_btn = await get_common_menu_button("main_menu", "ru")
    if main_btn:
        rows.append([main_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    search_found_tmpl = await get_text("services_view_search_found_tmpl", "ru") or "{correction_note}🔎 Найдено: <b>{count}</b> по запросу: <b>{query}</b>\n\nВыберите услугу:"
    msg = await cb.message.answer(
        search_found_tmpl.format(correction_note="", count=total_count, query=escape_html(query)),
        reply_markup=kb,
        parse_mode="HTML"
    )

    services_last_search_menu_message[chat_id] = msg.message_id
    services_last_search_query_message[chat_id] = msg.message_id
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

    await state.set_state(ServiceSearch.waiting_for_detail)
    await cb.answer()

    print(
        f"[services_view.py] services_search_page | "
        f"chat_id={chat_id} | page={page}/{pages} | offset={offset} | msg_id={msg.message_id}"
    )
