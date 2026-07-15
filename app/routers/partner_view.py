# app/routers/partner_view.py
"""
Карточка партнёрской кампании (Strategy v2, слой 2 §5).

Открывается кнопкой-строкой из главного меню (callback partner:<key>).
Открытие логируется событием partner_opened. Кнопки карточки — прямые
URL (клики по внешним ссылкам станут измеримыми после публичного
деплоя веб-части через /go/-редирект).
"""
import json

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlmodel import select

from app.database import SessionLocal
from app.models import Campaign
from app.analytics import log_event
from app.routers.utils import register_bot_messages

router = Router(name="partner_view")


@router.callback_query(F.data.startswith("partner:"))
async def partner_card(cb: CallbackQuery):
    key = cb.data.split(":", 1)[1]

    async with SessionLocal() as s:
        c = (await s.execute(
            select(Campaign).where(Campaign.key == key, Campaign.active == True)  # noqa: E712
        )).scalar_one_or_none()

    if c is None:
        await cb.answer("Карточка сейчас недоступна.", show_alert=True)
        return

    await log_event(
        "partner_opened", user_id=cb.from_user.id,
        entity_type="campaign", entity_id=c.id, meta={"campaign": c.key},
    )

    # Кнопки карточки: внешние ссылки из кампании + возврат в меню
    rows: list[list[InlineKeyboardButton]] = []
    if c.buttons:
        try:
            for b in json.loads(c.buttons):
                rows.append([InlineKeyboardButton(text=b["text"], url=b["url"])])
        except Exception as e:
            print(f"[partner_view] buttons JSON error ({c.key}): {e}")
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    chat_id = cb.message.chat.id
    try:
        await cb.message.delete()
    except Exception as e:
        print(f"[partner_view] delete menu msg: {e}")

    if c.photo_file_id:
        msg = await cb.bot.send_photo(
            chat_id, c.photo_file_id, caption=c.card_text,
            reply_markup=kb, parse_mode="HTML",
        )
    else:
        msg = await cb.bot.send_message(
            chat_id, c.card_text, reply_markup=kb,
            parse_mode="HTML", disable_web_page_preview=True,
        )
    await register_bot_messages(chat_id, [msg.message_id])

    try:
        await cb.answer()
    except Exception:
        pass
