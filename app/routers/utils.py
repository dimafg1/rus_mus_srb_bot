# utils.py
from collections import defaultdict
from app.models import City, Category, Listing
from app.database import SessionLocal
from sqlalchemy import select
from typing import Optional, List
from app.models import Category, Menu  # или Menu, если категории хранятся там
from app.database import SessionLocal


last_search_query_message = {}
last_search_menu_message = {}
last_reply_menu_messages = defaultdict(list)
last_bot_messages = defaultdict(list)
my_listing_messages = defaultdict(list)
sent_photo_messages = defaultdict(list)
listing_message_ids = {}
expanded_listing_by_chat = {}

async def clear_bot_messages(chat_id, bot):
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
async def delete_photo_prompts(message, state):
    data = await state.get_data()
    prompt_msgs = data.get("photo_prompt_msgs", [])
    for msg_id in prompt_msgs:
        try:
            await message.bot.delete_message(message.chat.id, msg_id)
        except Exception:
            pass
    await state.update_data(photo_prompt_msgs=[])

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

DB_PATH = "dev.db"  # Укажите ваш путь или используйте переменную из настроек!

async def get_text(code: str, lang: str = "ru", default=None):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT text FROM BotText WHERE code = ? AND lang = ?", (code, lang)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else (default or f"[Text not found for: {code}]")


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

async def city_by_slug(slug: str) -> City:
    async with SessionLocal() as s:
        return (await s.execute(select(City).where(City.slug == slug))).scalar_one()

async def children_of(parent_id: Optional[int]) -> List[Category]:
    async with SessionLocal() as s:
        q = select(Category).where(Category.parent_id == parent_id)
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
