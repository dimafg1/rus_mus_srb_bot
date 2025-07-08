# app/routers/utils.py

from typing import Dict, List

last_bot_messages: Dict[int, List[int]] = {}
sent_photo_messages: Dict[int, list] = {}

async def clear_bot_messages(chat_id: int, bot):
    # Очищаем текстовые/инлайн сообщения
    msg_ids = last_bot_messages.pop(chat_id, [])
    for msg_id in msg_ids:
        try:
            await bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
    # Очищаем фото и медиа
    photo_msg_ids = sent_photo_messages.pop(chat_id, [])
    for msg_id in photo_msg_ids:
        try:
            await bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
