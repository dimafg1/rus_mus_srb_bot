# utils.py
from collections import defaultdict
from app.models import City, Category, Listing, BotMessage
from app.database import SessionLocal
from sqlalchemy import select, delete, text
from typing import Dict, Any, Optional, List
from app.models import Category, Menu  # или Menu, если категории хранятся там
from app.database import SessionLocal
import json
from sqlalchemy.ext.asyncio import AsyncSession
from app.texts import get_text
from PIL import Image, ImageDraw, ImageFont
import tempfile
import os
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
import inspect
import time
import re
import urllib.parse
from pathlib import Path

from app.db_path import dotenv_value, resolve_sqlite_path



last_search_query_message = {}
last_search_menu_message = {}
last_reply_menu_messages = defaultdict(list)
last_bot_messages = defaultdict(list)
my_listing_messages = defaultdict(list)
sent_photo_messages = defaultdict(list)
listing_message_ids = {}
expanded_listing_by_chat = {}

def log(msg: str):
    """
    Печать в лог в формате:
    [файл.py] <handler_or_function> | HH:MM:SS | msg
    Автоматически извлекает имя файла и имя функции из стека вызовов.
    """
    try:
        # уровень 1 — наш непосредственный вызов; 2 — его вызов, если нужно
        frm = inspect.stack()[1]
        filename = os.path.basename(frm.filename)
        func = frm.function
        ts = time.strftime("%H:%M:%S")
        print(f"[{filename}] {func} | {ts} | {msg}")
    except Exception:
        # на всякий случай fallback
        print(f"[LOG] {msg}")


async def register_bot_message(chat_id: int, message_id: int) -> None:
    """Сохраняет сообщение бота в БД для очистки после рестарта."""
    if not chat_id or not message_id:
        return
    try:
        async with SessionLocal() as session:
            exists = (await session.execute(
                select(BotMessage).where(
                    BotMessage.chat_id == int(chat_id),
                    BotMessage.message_id == int(message_id),
                )
            )).scalar_one_or_none()
            if exists:
                return
            session.add(BotMessage(chat_id=int(chat_id), message_id=int(message_id)))
            await session.commit()
    except Exception as e:
        print(f"[utils.py] register_bot_message failed | chat_id={chat_id} msg_id={message_id} | {e}")


async def register_bot_messages(chat_id: int, message_ids: list[int]) -> None:
    """Сохраняет несколько сообщений бота в БД для очистки после рестарта."""
    if not chat_id or not message_ids:
        return
    unique_ids = []
    seen = set()
    for mid in message_ids:
        try:
            mid_int = int(mid)
        except Exception:
            continue
        if mid_int and mid_int not in seen:
            seen.add(mid_int)
            unique_ids.append(mid_int)
    if not unique_ids:
        return
    try:
        async with SessionLocal() as session:
            existing = (await session.execute(
                select(BotMessage.message_id).where(
                    BotMessage.chat_id == int(chat_id),
                    BotMessage.message_id.in_(unique_ids),
                )
            )).scalars().all()
            existing_set = set(existing)
            for mid in unique_ids:
                if mid not in existing_set:
                    session.add(BotMessage(chat_id=int(chat_id), message_id=mid))
            await session.commit()
    except Exception as e:
        print(f"[utils.py] register_bot_messages failed | chat_id={chat_id} ids={unique_ids} | {e}")


async def clear_bot_messages_db(chat_id: int, bot) -> None:
    """Удаляет сохранённые в БД сообщения бота и очищает записи."""
    if not chat_id:
        return
    try:
        async with SessionLocal() as session:
            rows = (await session.execute(
                select(BotMessage).where(BotMessage.chat_id == int(chat_id))
            )).scalars().all()

            for row in rows:
                try:
                    await bot.delete_message(int(chat_id), int(row.message_id))
                except Exception:
                    pass

            if rows:
                await session.execute(
                    delete(BotMessage).where(BotMessage.chat_id == int(chat_id))
                )
                await session.commit()
    except Exception as e:
        print(f"[utils.py] clear_bot_messages_db failed | chat_id={chat_id} | {e}")

async def clear_bot_messages(chat_id, bot):
    # Сначала чистим БД-слой: он переживает рестарт бота.
    await clear_bot_messages_db(chat_id, bot)

    # Удаляем сообщения с фото (в том числе объявления, медиагруппы и пр.)
    for msg_id in sent_photo_messages.get(chat_id, []):
        try:
            await bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
    sent_photo_messages[chat_id] = []

    # Удаляем все вспомогательные сообщения-меню, кнопки и др.
    for msg_id in last_bot_messages.get(chat_id, []):
        try:
            await bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
    last_bot_messages[chat_id] = []

    # Удаляем все карточки из “Мои объявления”
    for msg_id in my_listing_messages.get(chat_id, []):
        try:
            await bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
    my_listing_messages[chat_id] = []

# --- Функция для удаления подсказок о фото, если были ---
async def delete_photo_prompts(message: Message, state: FSMContext):
    data = await state.get_data()
    ids = data.get("photo_prompt_msgs") or []
    if isinstance(ids, int):
        ids = [ids]
    # удалим дубликаты, если вдруг были
    for msg_id in set(ids):
        try:
            await message.bot.delete_message(message.chat.id, msg_id)
        except Exception:
            pass
    await state.update_data(photo_prompt_msgs=[])
    print(f"[services_add.py] delete_photo_prompts ✓ | chat_id={message.chat.id} | ids={ids}")


from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select
from app.database import SessionLocal
from app.models import Menu  # Имя вашей модели таблицы меню

async def build_menu_keyboard(parent_code="main_menu", lang="ru") -> InlineKeyboardMarkup:
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Menu)
            .where(Menu.parent_code == parent_code)
            .where(Menu.visible == 1)
            .where(Menu.lang == lang)
            .order_by(Menu.order_num)
        )).scalars().all()

    # Генерируем кнопки
    keyboard = []
    for row in rows:
        text = f"{row.icon + ' ' if row.icon else ''}{row.text}"
        keyboard.append([InlineKeyboardButton(text=text, callback_data=row.callback_data)])
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    return markup

import aiosqlite

DB_PATH = resolve_sqlite_path(Path(__file__).resolve().parents[2])

async def get_text(code: str, lang: str = "ru", default=None):
    col = f"text_{lang}" if lang in ("ru", "en") else "text_ru"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT {col}, text_ru FROM BotText WHERE code = ?", (code,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0] or row[1] or ""
            return default or f"[{code}]"


# --- Безопасное редактирование или отправка нового сообщения ---
from aiogram.exceptions import TelegramBadRequest

async def safe_edit_or_send(cb, text: str, reply_markup=None, parse_mode="HTML"):
    chat_id = cb.message.chat.id
    try:
        msg = await cb.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest:
        try:
            await cb.message.delete()
        except Exception:
            pass
        msg = await cb.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)

    # Добавляем сообщение в кеш для последующего удаления
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_message(chat_id, msg.message_id)

async def city_by_slug(slug: str) -> City:
    async with SessionLocal() as s:
        return (await s.execute(select(City).where(City.slug == slug))).scalar_one()

async def children_of(parent_id: Optional[int]) -> List[Category]:
    async with SessionLocal() as s:
        q = select(Category).where(Category.parent_id == parent_id).order_by(text("order_num"), Category.name)
        return (await s.execute(q)).scalars().all()

PAGE = 10

async def fetch_listings(city_id: int, cat_id: int, offset: int = 0) -> List[Listing]:
    async with SessionLocal() as s:
        q = (select(Listing)
             .where(Listing.city_id == city_id,
                    Listing.category_id == cat_id,
                    Listing.is_sold.is_(False))
             .order_by(Listing.created_at.desc())
             .offset(offset)
             .limit(PAGE))
        return (await s.execute(q)).scalars().all()
    
async def get_catalog_categories(parent_id=None):
    async with SessionLocal() as session:
        query = select(Category)
        if parent_id is not None:
            query = query.where(Category.parent_id == parent_id)
        categories = (await session.execute(query)).scalars().all()
    # Можно вернуть [{'name': c.name, 'callback_data': ...}, ...] для удобства
    return categories

# ───────────────────────────── FLEX (доп. поля) ─────────────────────────────

def escape_html(s: Any) -> str:
    """Экранировать пользовательское значение для Telegram HTML parse mode."""
    t = "" if s is None else str(s)
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Обратная совместимость для внутренних вызовов этого модуля.
_flex_html = escape_html

def _flex_fmt_value(val: Any) -> Optional[str]:
    if val in (None, "", [], {}):
        return None
    if isinstance(val, bool):
        return "Да" if val else "Нет"
    if isinstance(val, list):
        s = ", ".join(str(x).strip() for x in val if str(x).strip())
        return s or None
    # Для строк будем пытаться определить тип содержимого
    if isinstance(val, str):
        s = val.strip()
        low = s.lower()
        # Если это URL или длинная строка без пробелов (file_id) — не показываем значение
        # URL: содержит http/://
        if "http" in low or "://" in s:
            return ""  # значение будет показано отдельным сообщением
        # file_id: длинная строка без пробелов
        if len(s) > 20 and " " not in s:
            return ""  # прячем идентификатор
        return s or None
    return str(val).strip() or None

def _flex_parse(flex_data: Any) -> Dict[str, Any]:
    if flex_data is None:
        return {}
    if isinstance(flex_data, dict):
        return flex_data
    if isinstance(flex_data, str):
        try:
            obj = json.loads(flex_data)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}

async def _flex_labels_for_category(session: AsyncSession, category_id: int, lang: str = "ru") -> Dict[str, str]:
    """
    Подгрузка подписей для ключей flex из Category.fields.
    Обходит всю цепочку parent_id снизу вверх; дочерние поля перекрывают родительские.
    """
    # Собираем цепочку от текущей до корня
    chain: list = []
    cur_id = category_id
    guard = 0
    while cur_id and guard < 10:
        guard += 1
        cat = await session.get(Category, cur_id)
        if not cat:
            break
        chain.append(cat)
        cur_id = cat.parent_id

    # Идём сверху вниз, дочерние переопределяют родительские
    labels: Dict[str, str] = {}
    for cat in reversed(chain):
        if not cat.fields:
            continue
        try:
            defs = json.loads(cat.fields)
            if isinstance(defs, list):
                for f in defs:
                    key = str(f.get("key", "")).strip().lower()
                    if not key:
                        continue
                    label = (f.get("label") or key).strip()
                    labels[key] = label or key
        except Exception:
            pass
    return labels


async def render_category_path(
    session: AsyncSession,
    category_id: int | None,
    *,
    root_id: int | None = None,
    separator: str = " → ",
) -> str:
    """
    Человекочитаемый путь категории без технических корней.
    Например: "Инструменты → Гитары → Электрогитары".
    """
    if not category_id:
        return ""

    names: List[str] = []
    cur_id = category_id
    guard = 0

    while cur_id and guard < 20:
        guard += 1
        cat = await session.get(Category, cur_id)
        if not cat:
            break

        if root_id is not None and cat.id == root_id:
            break

        name = (getattr(cat, "name", None) or "").strip()
        if name:
            names.append(_flex_html(name))

        parent_id = getattr(cat, "parent_id", None)
        if not parent_id or parent_id == cur_id:
            break
        cur_id = parent_id

    names.reverse()
    return separator.join(names)

async def render_flex_block(session: AsyncSession, listing: Listing, lang: str = "ru") -> str:
    """
    Без заголовка. Формат строк:
    <b>Label:</b> value

    Между логическими строками — пустая строка для читаемости.
    Поля с типом 'video' намеренно НЕ выводим (видео показывается отдельно плеером).
    Служебные поля доп. категорий не показываем как flex; вместо этого выводим
    человекочитаемый блок с названиями дополнительных категорий.
    """
    # Разбираем значения гибких полей
    flex = _flex_parse(listing.flex)

    # Карта типов по ключам — вся цепочка parent_id, дочерние перекрывают родительские
    type_by_key: dict[str, str] = {}
    try:
        chain_cats: list = []
        cur_id = listing.category_id
        guard = 0
        while cur_id and guard < 10:
            guard += 1
            cat = (await session.execute(
                select(Category).where(Category.id == cur_id)
            )).scalar_one_or_none()
            if not cat:
                break
            chain_cats.append(cat)
            cur_id = cat.parent_id
        for cat in reversed(chain_cats):
            if not cat.fields:
                continue
            try:
                defs = json.loads(cat.fields)
                if isinstance(defs, list):
                    for f in defs:
                        k = str(f.get("key", "")).strip().lower()
                        t = str(f.get("type", "")).strip().lower()
                        if k:
                            type_by_key[k] = t
            except Exception:
                pass
    except Exception:
        type_by_key = type_by_key or {}

    # Лейблы для ключей
    labels = await _flex_labels_for_category(session, listing.category_id, lang=lang)

    # Служебные flex-ключи, которые не должны попадать в карточку пользователя.
    # Дополнительные категории ниже выводятся отдельным русским блоком по Listing.extra_category_id*.
    hidden_service_keys = {
        "allow_extra_categories",
        "extra_category_id1",
        "extra_category_id2",
        "extra_category_1",
        "extra_category_2",
    }

    # Собираем строки, пропуская поля типа 'video' и служебные поля
    lines: List[str] = []
    skipped_video = 0
    skipped_service = 0
    for raw_key, raw_val in flex.items():
        key = str(raw_key).strip().lower()
        if not key:
            continue

        # не отображаем служебные поля
        if key in hidden_service_keys:
            skipped_service += 1
            continue

        # не отображаем видео-поля (по типу из определения или по ключу "video")
        if type_by_key.get(key) == "video" or key == "video":
            skipped_video += 1
            continue

        val = _flex_fmt_value(raw_val)
        if val is None:
            continue

        label = escape_html(labels.get(key, key))
        lines.append(f"<b>{label}:</b> {escape_html(val)}")

    # Дополнительные категории здесь намеренно НЕ выводим.
    # Текущая категория/подкатегория показывается в заголовке карточки раздела.

    if not lines:
        print(
            f"[utils.py] render_flex_block | listing_id={listing.id} | "
            f"lines=0 | skipped_video={skipped_video} | skipped_service={skipped_service}"
        )
        return ""

    result = "\n\n".join(lines)
    print(
        f"[utils.py] render_flex_block | listing_id={listing.id} | "
        f"lines={len(lines)} | skipped_video={skipped_video} | skipped_service={skipped_service}"
    )
    return result


async def render_flex_compact(session: AsyncSession, listing: Listing, indent: str = "    ", lang: str = "ru") -> str:
    """
    Компактная версия для «разворота»: без жирного заголовка блока,
    но с отступами и пустыми строками.
    """
    block = await render_flex_block(session, listing, lang=lang)
    if not block:
        return ""
    return "\n".join(indent + line if line else "" for line in block.splitlines())


async def render_main_fields(listing) -> str:
    """
    Форматируем основные поля объявления.
    Между блоками оставляем пустую строку.
    """
    lines = []

    if listing.title:
        lines.append(f"<b>{escape_html(listing.title.strip())}</b>")

    if listing.price:
        lines.append(f"<b>Цена:</b> {escape_html(listing.price)}")

    if listing.descr:
        lines.append(f"<b>Описание:</b> {escape_html(listing.descr.strip())}")

    return "\n\n".join(lines)


async def render_contact(listing, lang="ru") -> str:
    """
    Контакт всегда в конце, отдельным блоком.
    """
    # Контакты архивных/закрытых объявлений не должны оставаться доступными
    # через старые callback-кнопки или раздел «Мои объявления».
    if (
        not listing.contact
        or getattr(listing, "status", "active") != "active"
        or bool(getattr(listing, "is_sold", False))
    ):
        return ""
    contact_label = await get_text("listing_contact", lang) or "Контакт"
    return f"<b>{escape_html(contact_label)}:</b> {escape_html(listing.contact.strip())}"


def make_listing_banner(title: str, price: str | None) -> str:
    """
    Генерирует простую баннер-картинку с Заголовком и Ценой.
    Возвращает путь к временному PNG-файлу (удалите сами после отправки).
    """
    W, H = 1280, 360
    bg = (20, 32, 45)     # тёмный фон
    fg = (255, 255, 255)  # белый текст
    sub = (180, 220, 255) # светлее для цены

    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)

    # Шрифты: пробуем системный, иначе default
    try:
        font_title = ImageFont.truetype("Arial.ttf", 64)
        font_price = ImageFont.truetype("Arial.ttf", 44)
    except Exception:
        font_title = ImageFont.load_default()
        font_price = ImageFont.load_default()

    title = (title or "").strip()
    price = (price or "").strip()

    # Текст: отступы
    pad_x, pad_y = 240, 36
    # Заголовок
    draw.text((pad_x, pad_y), title, font=font_title, fill=fg)
    # Цена, если есть
    if price:
        draw.text((pad_x, pad_y + 64 + 24), f"Цена: {price}", font=font_price, fill=sub)

    # Сохраняем во временный файл
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    img.save(tmp.name, "PNG")
    tmp.close()
    return tmp.name



def build_contact_url(listing_id: int, contact: str, user_id: int, source: str = "direct") -> str:
    username = (contact or "").lstrip("@").strip()
    if not username or not re.fullmatch(r"[A-Za-z0-9_]{1,32}", username):
        return ""

    fallback = f"https://t.me/{username}"
    root = Path(__file__).resolve().parents[2]
    webapp_base = (
        os.getenv("WEBAPP_BASE")
        or dotenv_value(root / ".env.web", "WEBAPP_BASE")
        or dotenv_value(root / ".env", "WEBAPP_BASE")
        or ""
    ).strip().rstrip("/")
    if not webapp_base:
        return fallback

    try:
        from app.web.security import sign_contact_click_token

        token = sign_contact_click_token(
            listing_id=int(listing_id),
            user_id=int(user_id),
            source=(source or "direct").strip() or "direct",
        )
        return f"{webapp_base}/go/contact?t={urllib.parse.quote(token, safe='')}"
    except Exception as exc:
        print(f"[utils.py] build_contact_url fallback | listing_id={listing_id} | {exc}")
        return fallback
