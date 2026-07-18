# app/routers/vacancy_edit.py
from __future__ import annotations

# Короткое RU-описание: входная точка редактирования вакансии — вызывает обзор.
from aiogram import F, Router
from aiogram.types import CallbackQuery
from app.routers.vacancy_edit_overview import (
    router as _ov_router,
    _authorize_vacancy_callback,
    _render_overview,
)
from app.routers.utils import clear_bot_messages

router = Router(name="vacancy_edit")
# Включаем роутер overview (чтобы все его хендлеры были зарегистрированы)
router.include_router(_ov_router)

@router.callback_query(F.data.startswith("edit_vacancy_overview:"))
async def vacancy_edit_overview_entry(cb: CallbackQuery):
    """RU: Вход в обзор через префикс edit_vacancy_overview:<id> (аналог сервисов)."""
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    try:
        listing_id = int(cb.data.split(":")[1])
    except Exception:
        await cb.answer("Некорректный ID", show_alert=True)
        print("[vacancy_edit.py] handler=vacancy_edit_overview_entry bad_id data=", cb.data)
        return
    if not await _authorize_vacancy_callback(cb, listing_id):
        return
    await _render_overview(chat_id, cb.bot, cb.message.answer, listing_id)
    await cb.answer()
    print("[vacancy_edit.py] handler=vacancy_edit_overview_entry listing_id=", listing_id)
