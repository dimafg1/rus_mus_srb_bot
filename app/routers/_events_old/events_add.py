# -*- coding: utf-8 -*-
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime, date

from app.database import SessionLocal
from app.models import Event
from app.routers.utils import clear_bot_messages, last_bot_messages
from app.keyboards import get_common_menu_button
from app.texts import get_text

router = Router(name="events_add")

class EventForm(StatesGroup):
    title = State()
    date  = State()
    place = State()
    descr = State()

def _cancel_kb(back_cb="events:open") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❎ Отменить и вернуться", callback_data=back_cb)]
    ])

# Старт из меню «Афиши»
@router.callback_query(F.data == "events:add:start")
async def events_add_start(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    try:
        await cb.message.delete()
    except Exception:
        pass

    txt = (
        "📝 <b>Новое событие</b>\n\n"
        "Отправьте <b>название события</b>:"
    )
    msg = await cb.message.answer(txt, reply_markup=_cancel_kb(), parse_mode="HTML")
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await state.set_state(EventForm.title)
    await state.update_data(nav_msg_id=msg.message_id)
    await cb.answer()

@router.message(EventForm.title)
async def events_add_title(m: Message, state: FSMContext):
    chat_id = m.chat.id
    try: await m.delete()
    except Exception: pass

    title = (m.text or "").strip()
    if not title:
        await m.answer("Пусто. Введите название ещё раз:", reply_markup=_cancel_kb(), parse_mode="HTML")
        return

    await state.update_data(title=title)

    msg = await m.answer(
        "📅 Укажите <b>дату</b> в формате <code>ДД.ММ.ГГГГ</code> (например, 21.11.2025):",
        reply_markup=_cancel_kb(),
        parse_mode="HTML",
    )
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await state.set_state(EventForm.date)

@router.message(EventForm.date)
async def events_add_date(m: Message, state: FSMContext):
    chat_id = m.chat.id
    try: await m.delete()
    except Exception: pass

    raw = (m.text or "").strip()
    try:
        d = datetime.strptime(raw, "%d.%m.%Y").date()
    except Exception:
        msg = await m.answer("Неверный формат. Введите дату в формате <code>ДД.ММ.ГГГГ</code>.",
                             reply_markup=_cancel_kb(), parse_mode="HTML")
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        return

    await state.update_data(event_date=d)

    msg = await m.answer("📍 Укажите <b>место проведения</b> (адрес/площадка):",
                         reply_markup=_cancel_kb(), parse_mode="HTML")
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await state.set_state(EventForm.place)

@router.message(EventForm.place)
async def events_add_place(m: Message, state: FSMContext):
    chat_id = m.chat.id
    try: await m.delete()
    except Exception: pass

    place = (m.text or "").strip()
    await state.update_data(place=place)

    msg = await m.answer("🗒 Отправьте <b>описание</b> (опционально):",
                         reply_markup=_cancel_kb(), parse_mode="HTML")
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await state.set_state(EventForm.descr)

@router.message(EventForm.descr)
async def events_add_descr(m: Message, state: FSMContext):
    chat_id = m.chat.id
    try: await m.delete()
    except Exception: pass

    descr = (m.text or "").strip()
    data = await state.get_data()
    title = data.get("title")
    event_date = data.get("event_date")
    place = data.get("place")
    owner_id = m.from_user.id

    async with SessionLocal() as s:
        s.add(Event(title=title, descr=descr or None, place=place or None,
                    event_date=event_date, owner_id=owner_id))
        await s.commit()

    # Финальный экран
    kb_rows = [
        [InlineKeyboardButton(text="📅 К календарю", callback_data="events:open")],
    ]
    main_btn = await get_common_menu_button('main_menu')
    if main_btn:
        kb_rows.append([main_btn])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    txt = (
        "✅ <b>Событие сохранено</b>\n\n"
        f"Название: {title}\n"
        f"Дата: {event_date.strftime('%d.%m.%Y')}\n"
        f"Место: {place or '—'}\n"
        f"Описание: {descr or '—'}"
    )
    await m.answer(txt, reply_markup=kb, parse_mode="HTML")
    await state.clear()
