import asyncio
from datetime import datetime
from typing import List

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Vacancy, City

router = Router()

# ── Шаги FSM ────────────────────────────────────────────────────────────────
class VacancyForm(StatesGroup):
    city = State()    # выбор города
    role = State()    # роль
    descr = State()   # описание вакансии
    confirm = State() # подтверждение

# ── После выбора города (inline) ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("vcity:"))
async def vacancy_city(cb: CallbackQuery, state: FSMContext):
    slug = cb.data.split(":", 1)[1]
    async with SessionLocal() as s:
        city = (await s.execute(select(City).where(City.slug == slug))).scalar_one()
    await state.update_data(city_id=city.id, city_name=city.name)

    await cb.message.edit_text("Укажите роль (например, «Вокалист», «Гитарист» и т.д.):", reply_markup=None)
    await state.set_state(VacancyForm.role)
    await cb.answer()

# ── Обработка роли ───────────────────────────────────────────────────────────
@router.message(VacancyForm.role)
async def vacancy_role(m: Message, state: FSMContext):
    await state.update_data(role=m.text)
    await m.answer("Кратко опишите требования/условия (описание вакансии):")
    await state.set_state(VacancyForm.descr)

# ── Описание ────────────────────────────────────────────────────────────────
@router.message(VacancyForm.descr)
async def vacancy_descr(m: Message, state: FSMContext):
    await state.update_data(descr=m.text)
    data = await state.get_data()

    # Telegram-контакт берём автоматически
    tg_contact = f"@{m.from_user.username}" if m.from_user.username else f"id:{m.from_user.id}"

    preview = (
        f"🤝 Ищу/Предлагаю — проверка данных:\n\n"
        f"🏙 Город: <b>{data['city_name']}</b>\n"
        f"🎯 Роль: <b>{data['role']}</b>\n"
        f"📝 Описание: {data['descr']}\n"
        f"📬 Контакт: <code>{tg_contact}</code>\n\n"
        "Подтвердите публикацию (да/нет):"
    )
    await m.answer(preview)
    await state.set_state(VacancyForm.confirm)

# ── Подтверждение ───────────────────────────────────────────────────────────
@router.message(VacancyForm.confirm)
async def vacancy_confirm(m: Message, state: FSMContext):
    text = m.text.lower()
    if text not in ("да", "yes", "✔️"):
        await m.answer("❌ Отменено.")
        return await state.clear()

    data = await state.get_data()
    tg_contact = f"@{m.from_user.username}" if m.from_user.username else f"id:{m.from_user.id}"

    async with SessionLocal() as s:
        vac = Vacancy(
            city_id=data["city_id"],
            role=data["role"],
            descr=data["descr"],
            contact=tg_contact,
            owner_id=m.from_user.id,
            created_at=datetime.utcnow(),
            is_closed=False,
        )
        s.add(vac)
        await s.commit()

    await m.answer("✅ Вакансия опубликована!")
    await state.clear()
