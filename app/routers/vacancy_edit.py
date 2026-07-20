# app/routers/vacancy_edit.py
from __future__ import annotations

# Короткое RU-описание: входная точка редактирования вакансии — вызывает обзор.
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from app.routers.vacancy_edit_overview import (
    router as _ov_router,
    _authorize_vacancy_callback,
    _back_cb_from_ctx,
    _render_overview,
)
from app.routers.utils import clear_bot_messages, get_text

router = Router(name="vacancy_edit")
# Включаем роутер overview (чтобы все его хендлеры были зарегистрированы)
router.include_router(_ov_router)

@router.callback_query(F.data.startswith("edit_vacancy_overview:"))
async def vacancy_edit_overview_entry(cb: CallbackQuery, state: FSMContext):
    """RU: Вход в обзор через префикс edit_vacancy_overview:<id> (аналог сервисов).
    Легаси-кнопки в старых сообщениях; новые экраны шлют vacancy_edit_overview:."""
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    try:
        listing_id = int(cb.data.split(":")[1])
    except Exception:
        await cb.answer(await get_text("services_edit_invalid_id", "ru") or "Некорректный ID", show_alert=True)
        print("[vacancy_edit.py] handler=vacancy_edit_overview_entry bad_id data=", cb.data)
        return
    if not await _authorize_vacancy_callback(cb, listing_id):
        return
    # Отмена активного шага без потери данных (контекст поиска и пр.)
    await state.set_state(None)
    back_cb = _back_cb_from_ctx(listing_id, await state.get_data())
    await _render_overview(chat_id, cb.bot, cb.message.answer, listing_id, back_cb=back_cb)
    await cb.answer()
    print("[vacancy_edit.py] handler=vacancy_edit_overview_entry listing_id=", listing_id)
