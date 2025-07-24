# utils.py
from collections import defaultdict


sent_photo_messages = defaultdict(list)
last_bot_messages = defaultdict(list)
my_listing_messages = defaultdict(list)

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
