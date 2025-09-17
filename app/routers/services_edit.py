from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Listing, City, Category
from app.states import EditListing
from app.keyboards import get_common_menu_button
from app.routers.utils import clear_bot_messages, last_bot_messages
from app.routers.services_edit_overview import _render_overview as _render_services_overview


# доп-поля
from app.routers.user_extra_fields import (
    start_extra_fields_for_category,
    extra_next,
    extra_finish,
    extra_back,
)

router = Router()

# -------------------------- ВНУТРЕННИЕ УТИЛИТЫ --------------------------

async def _get_listing(s, listing_id: int) -> Listing:
    return (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one()

async def _listing_title(l: Listing) -> str:
    return l.title or "—"

async def _listing_city_cat(s, l: Listing):
    city = (await s.execute(select(City).where(City.id == l.city_id))).scalar_one()
    cat  = (await s.execute(select(Category).where(Category.id == l.category_id))).scalar_one()
    return city, cat

# -------------------------- ОБЗОР --------------------------

@router.callback_query(F.data.startswith("service_edit_overview:"))
async def service_edit_overview(cb: CallbackQuery, state: FSMContext):
    """Алиас: показать новый обзор редактирования Услуги (как в Барахолке)."""
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    try:
        listing_id = int(cb.data.split(":")[1])
    except Exception:
        await cb.answer("Некорректный ID", show_alert=True)
        return

    await _render_services_overview(chat_id, cb.message.bot, cb.message.answer, listing_id)
    await cb.answer()
    print(f"[services_edit.py] service_edit_overview -> services_edit_overview._render_overview | chat_id={chat_id} user_id={cb.from_user.id} listing_id={listing_id} msg_id={cb.message.message_id}")

# -------------------------- ПОЛЯ --------------------------

@router.callback_query(F.data.startswith("edit:title:"))
async def edit_title_start(cb: CallbackQuery, state: FSMContext):
    listing_id = int(cb.data.split(":")[2])
    await state.set_state(EditListing.waiting_title)          # <-- было EditListing.title
    await state.update_data(listing_id=listing_id)
    await cb.message.answer("Введите новый заголовок (1–70 символов):")
    await cb.answer()


@router.message(EditListing.waiting_title)                    # <-- было EditListing.title
async def edit_title_save(msg: Message, state: FSMContext):
    data = await state.get_data()
    listing_id = int(data["listing_id"])
    title = (msg.text or "").strip()
    if not (1 <= len(title) <= 70):
        await msg.answer("Заголовок должен быть 1–70 символов. Попробуйте снова.")
        return
    async with SessionLocal() as s:
        l = await _get_listing(s, listing_id)
        l.title = title
        s.add(l); await s.commit()
    await msg.answer("Заголовок обновлён.")
    await state.clear()


@router.callback_query(F.data.startswith("edit:descr:"))
async def edit_descr_start(cb: CallbackQuery, state: FSMContext):
    listing_id = int(cb.data.split(":")[2])
    await state.set_state(EditListing.waiting_descr)          # <-- было EditListing.descr
    await state.update_data(listing_id=listing_id)
    await cb.message.answer("Введите новое описание (или оставьте пустым).")
    await cb.answer()


@router.message(EditListing.waiting_descr)                    # <-- было EditListing.descr
async def edit_descr_save(msg: Message, state: FSMContext):
    data = await state.get_data()
    listing_id = int(data["listing_id"])
    descr = (msg.text or "").strip() or None
    async with SessionLocal() as s:
        l = await _get_listing(s, listing_id)
        l.descr = descr
        s.add(l); await s.commit()
    await msg.answer("Описание обновлено.")
    await state.clear()


@router.callback_query(F.data.startswith("edit:price:"))
async def edit_price_start(cb: CallbackQuery, state: FSMContext):
    listing_id = int(cb.data.split(":")[2])
    await state.set_state(EditListing.waiting_price)          # <-- было EditListing.price
    await state.update_data(listing_id=listing_id)
    await cb.message.answer("Укажите новую стоимость (или «Договорная»).")
    await cb.answer()


@router.message(EditListing.waiting_price)                    # <-- было EditListing.price
async def edit_price_save(msg: Message, state: FSMContext):
    data = await state.get_data()
    listing_id = int(data["listing_id"])
    price = (msg.text or "").strip()
    async with SessionLocal() as s:
        l = await _get_listing(s, listing_id)
        l.price = price or "Договорная"
        s.add(l); await s.commit()
    await msg.answer("Стоимость обновлена.")
    await state.clear()


# -------------------------- ДОП. ПОЛЯ --------------------------

@router.callback_query(F.data.startswith("edit:extras:"))
async def edit_extras_start(cb: CallbackQuery, state: FSMContext):
    try:
        _, _, listing_id_s, cat_id_s = cb.data.split(":")
        listing_id = int(listing_id_s); cat_id = int(cat_id_s)
    except Exception:
        await cb.answer("Некорректные параметры", show_alert=True); return

    await state.update_data(listing_id=listing_id)
    # Возврат — в обзор редактирования
    await start_extra_fields_for_category(cb, state, cat_id, resume_data=f"service_edit_overview:{listing_id}")

# -------------------------- ФИНИШ --------------------------

@router.callback_query(F.data.startswith("edit:finish"))
async def edit_finish(cb: CallbackQuery):
    await cb.answer("Редактирование завершено.")
    # Можно вернуть в «Мои услуги» или в объявление — оставляю нейтрально:
    main_btn = await get_common_menu_button('main_menu', 'ru')
    kb = InlineKeyboardMarkup(inline_keyboard=[[main_btn]] if main_btn else [])
    await cb.message.answer("Готово.", reply_markup=kb)
