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
import os, urllib.parse, json

from aiogram.filters import StateFilter

from app.database import SessionLocal
from app.models import City, Category, Listing
try:
    from app.models import Menu  # пункты меню из БД
except Exception:
    Menu = None

from app.keyboards import get_common_menu_button, build_main_menu
from app.routers.utils import (
    clear_bot_messages, safe_edit_or_send, register_bot_messages,
    last_bot_messages, sent_photo_messages,
    render_flex_block, render_contact, render_category_path,
    last_search_query_message, last_search_menu_message, my_listing_messages,
    build_contact_url,
)

from app.search.fuzzy import search_items

from app.texts import get_text
from app.states import ServiceSearch

from collections import defaultdict

from app.routers.utils_category_title import format_category_title

from app.routers.utils_kb import grid3
from app.analytics.search_log import log_search
from app.analytics.listing_views import log_listing_view
from app.lifecycle import days_left_text, should_show_extend_button, extend_listing


router = Router(name="services_view")

# service_search_ctx_by_chat = defaultdict(dict)   # {chat_id: {"query": str, "results": [Listing]}}
service_search_messages = defaultdict(list)      # {chat_id: [message_ids]}
services_search_ctx_by_chat = {}
services_last_search_menu_message = {}
services_last_search_query_message = {}

SERVICES_SEARCH_PAGE_SIZE = 10


SERVICES_ROOT_CATEGORY_ID = 80
WEBAPP_BASE = os.getenv("WEBAPP_BASE", "https://unixound.com/rus_mus_srb_bot").rstrip("/")


# ───────────────────────── ВСПОМОГАТЕЛЬНЫЕ ─────────────────────────

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
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)])

    main_menu_btn = await get_common_menu_button("main_menu", "ru")
    if main_menu_btn:
        rows.append([main_menu_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    print(f"[services_view.py] _services_categories_kb | city_id={city_id} parent_id={parent_id} rows={len(rows)}")
    return kb


async def _services_listings_kb(items, city_id: int, cat_id: int) -> InlineKeyboardMarkup:
    """Клавиатура списка услуг в листовой категории."""
    rows = [[InlineKeyboardButton(
        text=(i.title or f"#{i.id}")[:64],
        callback_data=f"sv:item:{i.id}:{city_id}:{cat_id}"
    )] for i in items]

    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"sv:cat:{city_id}:{cat_id}:back")])

    main_menu_btn = await get_common_menu_button("main_menu", "ru")
    if main_menu_btn:
        rows.append([main_menu_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    print(f"[services_view.py] _services_listings_kb | city_id={city_id} cat_id={cat_id} items={len(items)} rows={len(rows)}")
    return kb



# ───────────────────────── ВСПОМОГАТЕЛЬНОЕ: YouTube-кнопка ─────────────────────────
def _is_youtube_url(url: str) -> bool:
    if not url: 
        return False
    u = url.strip().lower()
    return ("youtube.com" in u) or ("youtu.be" in u) or ("m.youtube.com" in u)


# ───────────────────────── ВСПОМОГАТЕЛЬНОЕ: YouTube TWA-кнопка ─────────────────────────
async def _send_yt_button(cb: CallbackQuery, video_url: str, listing_id: int) -> int | None:
    """
    Отправляет TWA-кнопку '▶️ Смотреть видео' ОТДЕЛЬНЫМ сообщением (без заголовка),
    чтобы кнопка оказалась НИЖЕ основного медиа/текста (как в Барахолке).
    Возвращает message_id созданного сообщения или None при ошибке/неподходящем URL.
    """
    try:
        if not video_url:
            return None
        low = video_url.lower()
        if ("youtube.com" not in low) and ("youtu.be" not in low):
            return None
        twa_url = f"{WEBAPP_BASE}/media/video_yt.html?u={urllib.parse.quote(video_url, safe='')}&listing_id={listing_id}"
        yt_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Смотреть видео", web_app=WebAppInfo(url=twa_url))]
        ])
        # NBSP — выглядит «пусто», но Telegram его принимает
        try:
            m = await cb.message.answer("\u00A0", reply_markup=yt_kb)
        except Exception as e:
            # запасной вариант, если вдруг NBSP не пройдёт на конкретном клиенте
            m = await cb.message.answer("•", reply_markup=yt_kb)
        print(f"[services_view.py] _send_yt_button ✓ | listing_id={listing_id} | url={video_url}")

        print(f"[services_view.py] _send_yt_button ✓ | listing_id={listing_id} url={video_url}")
        return m.message_id
    except Exception as e:
        print(f"[services_view.py] _send_yt_button ✗ | listing_id={listing_id} err={e}")
        return None


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
        city = (await s.execute(select(City).where(City.id == city_id))).scalar_one()
        cats = (await s.execute(
            select(Category).where(Category.parent_id == SERVICES_ROOT_CATEGORY_ID).order_by(sql_text("order_num"), Category.name)
        )).scalars().all()

    kb = await _services_categories_kb(cats, city_id=city.id, parent_id=SERVICES_ROOT_CATEGORY_ID)
    text = f"🛎 Услуги → <b>{city.name}</b>\nВыберите категорию:"
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

    await clear_bot_messages(chat_id, cb.bot)
    try:
        await cb.message.delete()
    except Exception:
        pass

    async with SessionLocal() as s:
        city = (await s.execute(select(City).where(City.id == city_id))).scalar_one()
        cat  = (await s.execute(select(Category).where(Category.id == cat_id))).scalar_one()
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
                text = f"🛎 Услуги → <b>{city.name}</b>\nВыберите категорию:"
                msg = await cb.bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
                last_bot_messages[chat_id] = [msg.message_id]
                await register_bot_messages(chat_id, [msg.message_id])
                await cb.answer()
                print(f"[services_view.py] sv_cat ← back to ROOT | chat_id={chat_id} city_id={city_id}")
                return

            # назад к подкатегориям родителя (сиблинги)
            parent = (await s.execute(select(Category).where(Category.id == parent_id))).scalar_one()
            siblings = (await s.execute(
                select(Category).where(Category.parent_id == parent_id).order_by(sql_text("order_num"), Category.name)
            )).scalars().all()

            kb = await _services_categories_kb(siblings, city_id=city_id, parent_id=parent_id)
            text = (
                f"🛎 Услуги → <b>{city.name}</b>\n"
                f"Категория: <b>{parent.name}</b>\nВыберите подкатегорию:"
            )
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
        text = f"🛎 Услуги → <b>{city.name}</b>\nКатегория: <b>{cat.name}</b>\nВыберите подкатегорию:"
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
        rows = [[InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=f"sv:cat:{city_id}:{cat.id}:back"
        )]]

        main_menu_btn = await get_common_menu_button("main_menu", "ru")
        if main_menu_btn:
            rows.append([main_menu_btn])

        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        msg = await cb.bot.send_message(chat_id, "Пока пусто в этой категории.", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(f"[services_view.py] sv_cat → empty | chat_id={chat_id} city_id={city_id} cat_id={cat_id}")
        return

    kb = await _services_listings_kb(items, city_id=city_id, cat_id=cat_id)
    text = f"🛎 Услуги → <b>{city.name}</b>\nКатегория: <b>{cat.name}</b>\nВыберите услугу:"
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
        listing = (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one_or_none()
        city = (await s.execute(select(City).where(City.id == listing.city_id))).scalar_one_or_none() if listing else None
    if not listing:
        msg = await cb.bot.send_message(chat_id, "Объявление не найдено или удалено.")
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
    category_line = f"Категория: <b>Услуги → {category_path}</b>" if category_path else "Категория: <b>Услуги</b>"

    city_line = f"Город: <b>{city.name}</b>" if city else ""
    price_label = (await get_text("service_price", "ru")) or (await get_text("listing_price", "ru")) or "Стоимость услуг"
    title_line = f"<b>{(listing.title or '').strip()}</b>" if listing.title else ""
    descr_line = (listing.descr or "").strip()
    price_line = f"{price_label}: {listing.price}" if listing.price else ""
    main_block = "\n\n".join([p for p in [city_line, category_line, title_line, descr_line, price_line] if p])

    async with SessionLocal() as s:
        flex_block = await render_flex_block(s, listing, lang="ru")
    contact_block = await render_contact(listing, lang="ru")

    caption_parts = [main_block]
    if flex_block:
        caption_parts.append(flex_block)

    # Если есть YouTube/URL — добавляем ссылку перед контактами
    if video_url:
        caption_parts.append(f"Видео: {video_url}")

    if contact_block:
        caption_parts.append(contact_block)

    caption = "\n\n".join([p for p in caption_parts if p]) or " "

    is_owner = listing.owner_id == cb.from_user.id
    management_text = "Контакты/Управление:"
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
            text=(edit_btn.text if edit_btn else "✏️ Редактировать все поля"),
            callback_data=f"service_edit_overview:{listing.id}"
        )])
        del_btn = await get_common_menu_button("btn_delete_service", "ru")
        buttons.append([InlineKeyboardButton(
            text=(del_btn.text if del_btn else "❌ Удалить объявление"),
            callback_data=f"sell_sold:{listing.id}"
        )])

        if should_show_extend_button(listing):
            buttons.append([InlineKeyboardButton(
                text="🔄 Продлить на 30 дней",
                callback_data=f"service_extend:{listing.id}:{urllib.parse.quote(back_cb, safe='')}"
            )])
    elif listing.contact and listing.contact.startswith("@"):
        c_btn = await get_common_menu_button("btn_contact_provider", "ru")
        buttons.append([InlineKeyboardButton(
            text=(c_btn.text if c_btn else "💬 Связаться"),
            url=build_contact_url(listing.id, listing.contact, cb.from_user.id, "services"),
        )])

    # Кнопки навигации — в конец
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)])

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
        await cb.answer("Ошибка данных продления.", show_alert=True)
        return

    try:
        listing_id = int(parts[1])
    except ValueError:
        await cb.answer("Неверный идентификатор услуги.", show_alert=True)
        return

    back_cb = urllib.parse.unquote(parts[2])

    async with SessionLocal() as s:
        listing = (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one_or_none()
        if not listing:
            await cb.answer("Услуга не найдена.", show_alert=True)
            return
        if listing.owner_id != cb.from_user.id:
            await cb.answer("Продлить может только автор услуги.", show_alert=True)
            return

        extend_listing(listing)
        await s.commit()
        await s.refresh(listing)

    management_text = "Контакты/Управление:"
    left_line = days_left_text(listing)
    if left_line:
        management_text += f"\n{left_line}"

    buttons = []

    edit_btn = await get_common_menu_button("btn_edit_service", "ru")
    buttons.append([InlineKeyboardButton(
        text=(edit_btn.text if edit_btn else "✏️ Редактировать все поля"),
        callback_data=f"service_edit_overview:{listing.id}"
    )])

    del_btn = await get_common_menu_button("btn_delete_service", "ru")
    buttons.append([InlineKeyboardButton(
        text=(del_btn.text if del_btn else "❌ Удалить объявление"),
        callback_data=f"sell_sold:{listing.id}"
    )])

    if should_show_extend_button(listing):
        buttons.append([InlineKeyboardButton(
            text="🔄 Продлить на 30 дней",
            callback_data=f"service_extend:{listing.id}:{urllib.parse.quote(back_cb, safe='')}"
        )])

    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)])

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

    await cb.answer("Услуга продлена на 30 дней.")
    print(
        f"[services_view.py] service_extend_listing ✓ | "
        f"listing_id={listing.id} chat_id={cb.message.chat.id} user_id={cb.from_user.id}"
    )


# ───────────────────────────── «МОИ УСЛУГИ» ─────────────────────────────────

@router.callback_query(F.data == "my_services")
async def my_services(cb: CallbackQuery):
    """Список услуг текущего пользователя (type='service')."""
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.message.bot)
    try: await cb.message.delete()
    except Exception: pass

    # Загрузка услуг текущего пользователя
    async with SessionLocal() as s:
        items = (await s.execute(
            select(Listing)
            .where(
                Listing.owner_id == cb.from_user.id,
                Listing.type == "service",
            )
            .order_by(Listing.created_at.desc())
        )).scalars().all()


    if not items:
        rows = [[InlineKeyboardButton(text="⬅️ Назад", callback_data="go_services")]]
        main_btn = await get_common_menu_button("main_menu", "ru")
        if main_btn:
            rows.append([main_btn])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        msg = await cb.bot.send_message(chat_id, "У вас пока нет услуг.", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        print(f"[services_view.py] my_services ✓ empty | chat_id={chat_id} user_id={cb.from_user.id}")
        return

    rows = []
    for it in items[:100]:
        rows.append([InlineKeyboardButton(
            text=(it.title or f'#{it.id}')[:64],
            callback_data=f"sv:item:{it.id}:{it.city_id}:{it.category_id}:m"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="go_services")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    msg = await cb.bot.send_message(chat_id, "Мои услуги:", reply_markup=kb)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"[services_view.py] my_services ✓ | chat_id={chat_id} user_id={cb.from_user.id} count={len(items)}")


# ───────────────────────────── «ПОИСК УСЛУГ» ────────────────────────────────

# Заметка: ранее здесь была заглушка поиска услуг, выводившая сообщение «в разработке».
# Для полноценной реализации поиска, хендлер ниже (services_search_start) обрабатывает тот же
# callback_data `services_search`, поэтому заглушка удалена, чтобы не перехватывать запрос.


# ───────────── Назад в главное меню бота ────────────────────────────────────

@router.callback_query(F.data == "go_services_menu_back")
async def go_services_menu_back(cb: CallbackQuery):
    """Возврат в главное меню бота."""
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    try: await cb.message.delete()
    except Exception: pass

    markup = await build_main_menu()
    text = await get_text("bot_welcome", "ru") or "Бот русского музыкального сообщества Сербии."
    msg = await cb.bot.send_message(chat_id, text, reply_markup=markup)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"[services_view.py] go_services_menu_back ✓ | chat_id={chat_id} user_id={cb.from_user.id} msg_id={msg.message_id}")

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
            [InlineKeyboardButton(text="🔄 Новый поиск", callback_data="services_search_new")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="go_services")],
        ]
        main_btn = await get_common_menu_button("main_menu", "ru")
        if main_btn:
            rows.append([main_btn])

        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        msg = await cb.message.answer("Результаты поиска не найдены. Начните новый поиск.", reply_markup=kb)
        services_last_search_menu_message[chat_id] = msg.message_id
        services_last_search_query_message[chat_id] = msg.message_id
        await register_bot_messages(chat_id, [msg.message_id])
        await state.clear()
        await cb.answer()
        return

    # достаём объекты
    async with SessionLocal() as s:
        results = (await s.execute(select(Listing).where(Listing.id.in_(ids)))).scalars().all()

    # строим клавиатуру — обязательно помечаем ':s', чтобы карточка знала, что мы из поиска
    rows = []
    for r in results:
        title = (r.title or f"#{r.id}")[:64]
        rows.append([InlineKeyboardButton(
            text=title,
            callback_data=f"sv:item:{r.id}:{r.city_id}:{r.category_id}:s"
        )])

    rows.append([InlineKeyboardButton(text="🔄 Новый поиск", callback_data="services_search_new")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="go_services")])

    main_btn = await get_common_menu_button("main_menu", "ru")
    if main_btn:
        rows.append([main_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    msg = await cb.message.answer(
        f"🔎 Найдено: <b>{len(results)}</b> по запросу: <b>{query}</b>\n\nВыберите услугу:",
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

    nav_msg   = await cb.bot.send_message(chat_id, nav_text, reply_markup=nav_markup)
    query_msg = await cb.bot.send_message(chat_id, query_text)

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
        msg = await m.answer("Пустой запрос. Введите текст запроса или нажмите «🔄 Новый поиск».")
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
            .join(Category, Category.id == Listing.category_id)
            .where(or_(Listing.is_sold == 0, Listing.is_sold == False, Listing.is_sold.is_(False)))
            .where(
                or_(
                    func.lower(func.trim(Listing.type)) == "service",
                    Category.id == SERVICES_ROOT_CATEGORY_ID,
                    Category.parent_id == SERVICES_ROOT_CATEGORY_ID,
                )
            )
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

    new_search_btn = InlineKeyboardButton(text="🔄 Новый поиск", callback_data="services_search_new")
    back_btn = InlineKeyboardButton(text="⬅️ Назад", callback_data="go_services")
    main_btn = await get_common_menu_button("main_menu", "ru")

    if not results:
        rows_kb = [[new_search_btn], [back_btn]]
        if main_btn:
            rows_kb.append([main_btn])

        kb = InlineKeyboardMarkup(inline_keyboard=rows_kb)

        msg = await m.answer(
            f"😕 Ничего не найдено по запросу: <b>{query}</b>",
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
        search_query_raw=search_query_raw,
        search_query_normalized=search_query_normalized,
        search_query_effective=search_query_effective,
        search_match_mode=search_match_mode,
    )

    services_search_ctx_by_chat[chat_id] = {
        "ids": ids,
        "query": query,
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
        correction_note = (
            f"🧠 Показаны результаты по запросу: "
            f"<b>{search_query_effective}</b> "
            f"(учтена возможная опечатка).\n\n"
        )

    kb = InlineKeyboardMarkup(inline_keyboard=rows_kb)

    msg = await m.answer(
        f"{correction_note}🔎 Найдено: <b>{total_count}</b> по запросу: <b>{query}</b>\n\nВыберите услугу:",
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


@router.callback_query(F.data == "services_menu_back")
async def services_menu_back(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # Сбрасываем хвосты поиска
    services_search_ctx_by_chat.pop(chat_id, None)

    # Удаляем служебные сообщения поиска
    for mid in (services_last_search_menu_message.pop(chat_id, None),
                services_last_search_query_message.pop(chat_id, None)):
        if mid:
            try:
                await cb.bot.delete_message(chat_id, mid)
            except Exception:
                pass

    # Очистка FSM и возврат в меню Услуг
    await clear_bot_messages(chat_id, cb.bot)
    try:
        await state.clear()
    except Exception:
        pass

    kb = await _services_main_menu_kb()
    title = await get_text("services_menu_title", "ru") or "Раздел «Услуги». Выберите действие:"
    msg = await cb.message.answer(title, reply_markup=kb)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

    await cb.answer()
    print(f"[services_view.py] services_menu_back ✓ | chat_id={chat_id} user_id={cb.from_user.id}")



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

    if not ids:
        msg = await cb.message.answer("Контекст поиска потерян. Начните новый поиск.")
        services_last_search_menu_message[chat_id] = msg.message_id
        services_last_search_query_message[chat_id] = msg.message_id
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        return

    page_ids = ids[offset:offset + SERVICES_SEARCH_PAGE_SIZE]

    async with SessionLocal() as s:
        db_results = (await s.execute(
            select(Listing).where(Listing.id.in_(page_ids))
        )).scalars().all()

    by_id = {r.id: r for r in db_results}
    results = [by_id[i] for i in page_ids if i in by_id]

    total_count = len(ids)
    page = (offset // SERVICES_SEARCH_PAGE_SIZE) + 1
    pages = max(1, (total_count + SERVICES_SEARCH_PAGE_SIZE - 1) // SERVICES_SEARCH_PAGE_SIZE)

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

    rows.append([InlineKeyboardButton(text="🔄 Новый поиск", callback_data="services_search_new")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="go_services")])

    main_btn = await get_common_menu_button("main_menu", "ru")
    if main_btn:
        rows.append([main_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    msg = await cb.message.answer(
        f"🔎 Найдено: <b>{total_count}</b> по запросу: <b>{query}</b>\n\nВыберите услугу:",
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


