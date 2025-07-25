# app/routers/market_view.py

from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.fsm.context import FSMContext

from app.texts import get_text
from app.routers.utils import last_bot_messages
from app.keyboards import market_inline
from app.routers.utils import clear_bot_messages, safe_edit_or_send
from aiogram import Router

router = Router()


@router.callback_query(F.data == "go_market")
async def go_market(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # Удаляем предыдущие сообщения интерфейса
    await clear_bot_messages(chat_id, cb.bot)

    # Загружаем текст и клавиатуру для выбора города
    text = await get_text("market_city_select")
    markup = await market_inline()

    # Отправляем новое сообщение
    await safe_edit_or_send(cb, text, reply_markup=markup)
    last_bot_messages.setdefault(chat_id, []).append(cb.message.message_id)
