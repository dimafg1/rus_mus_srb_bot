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


