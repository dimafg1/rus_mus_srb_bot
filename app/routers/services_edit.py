from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from sqlalchemy import select
from html import escape

from app.database import SessionLocal
from app.models import Listing, City, Category
from app.keyboards import get_common_menu_button
from app.routers.utils import clear_bot_messages, last_bot_messages, register_bot_messages, get_text
from app.routers.services_edit_overview import _render_overview as _render_services_overview


# доп-поля
from app.routers.user_extra_fields import (
    start_extra_fields_for_category,
    extra_next,
    extra_finish,
    extra_back,
)

router = Router(name="services_edit_legacy")


class ServiceLegacyEdit(StatesGroup):
    """Изолированные состояния старого редактора, не конфликтующие с барахолкой."""
    waiting_title = State()
    waiting_descr = State()
    waiting_price = State()

# -------------------------- ВНУТРЕННИЕ УТИЛИТЫ --------------------------

async def _get_listing(s, listing_id: int) -> Listing:
    return (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one()


async def _get_owned_service(s, listing_id: int, user_id: int) -> Listing | None:
    return (await s.execute(
        select(Listing).where(
            Listing.id == listing_id,
            Listing.owner_id == user_id,
            Listing.type == "service",
        )
    )).scalar_one_or_none()

async def _listing_title(l: Listing) -> str:
    return l.title or "—"

async def _listing_city_cat(s, l: Listing):
    city = (await s.execute(select(City).where(City.id == l.city_id))).scalar_one()
    cat  = (await s.execute(select(Category).where(Category.id == l.category_id))).scalar_one()
    return city, cat

# -------------------------- ОБЗОР --------------------------

@router.callback_query(F.data.startswith("service_legacy_edit_overview:"))
async def service_edit_overview(cb: CallbackQuery, state: FSMContext):
    """Алиас: показать новый обзор редактирования Услуги (как в Барахолке)."""
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    try:
        listing_id = int(cb.data.split(":")[1])
    except Exception:
        await cb.answer(await get_text("services_edit_invalid_id", "ru") or "Некорректный ID", show_alert=True)
        return

    async with SessionLocal() as s:
        if not await _get_owned_service(s, listing_id, cb.from_user.id):
            await cb.answer(await get_text("err_not_owner_service", "ru") or "Можно редактировать только свои услуги.", show_alert=True)
            return
    await _render_services_overview(chat_id, cb.message.bot, cb.message.answer, listing_id)
    await cb.answer()
    print(f"[services_edit.py] service_edit_overview -> services_edit_overview._render_overview | chat_id={chat_id} user_id={cb.from_user.id} listing_id={listing_id} msg_id={cb.message.message_id}")

# -------------------------- ПОЛЯ --------------------------

@router.callback_query(F.data.startswith("service_legacy_edit:title:"))
async def edit_title_start(cb: CallbackQuery, state: FSMContext):
    listing_id = int(cb.data.rsplit(":", 1)[1])
    await state.update_data(listing_id=listing_id)
    async with SessionLocal() as s:
        l = await _get_owned_service(s, listing_id, cb.from_user.id)
        if not l:
            await cb.answer(await get_text("err_not_owner_service", "ru") or "Можно редактировать только свои услуги.", show_alert=True)
            await state.clear()
            return
        current = l.title or "—"
    await state.set_state(ServiceLegacyEdit.waiting_title)
    tmpl = await get_text("services_edit_title_prompt", "ru") or (
        "🪧 <b>Заголовок</b>\n\nТекущее значение:\n<code>{current}</code>\n\n"
        "Отправьте новый текст (или скопируйте текущий ↑ и отредактируйте):"
    )
    msg = await cb.message.answer(tmpl.format(current=escape(current)), parse_mode="HTML")
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
    await cb.answer()


@router.message(ServiceLegacyEdit.waiting_title)
async def edit_title_save(msg: Message, state: FSMContext):
    data = await state.get_data()
    listing_id = int(data["listing_id"])
    title = (msg.text or "").strip()
    if not (1 <= len(title) <= 70):
        err = await msg.answer(await get_text("services_edit_title_len_error", "ru") or "Заголовок должен быть 1–70 символов. Попробуйте снова.")
        last_bot_messages.setdefault(msg.chat.id, []).append(err.message_id)
        await register_bot_messages(msg.chat.id, [err.message_id])
        return
    async with SessionLocal() as s:
        l = await _get_owned_service(s, listing_id, msg.from_user.id)
        if not l:
            await msg.answer(await get_text("err_not_owner_service", "ru") or "Можно редактировать только свои услуги.")
            await state.clear()
            return
        l.title = title
        s.add(l); await s.commit()
    ok = await msg.answer(await get_text("services_edit_title_saved", "ru") or "Заголовок обновлён.")
    last_bot_messages.setdefault(msg.chat.id, []).append(ok.message_id)
    await register_bot_messages(msg.chat.id, [ok.message_id])
    await state.clear()


@router.callback_query(F.data.startswith("service_legacy_edit:descr:"))
async def edit_descr_start(cb: CallbackQuery, state: FSMContext):
    listing_id = int(cb.data.rsplit(":", 1)[1])
    await state.update_data(listing_id=listing_id)
    async with SessionLocal() as s:
        l = await _get_owned_service(s, listing_id, cb.from_user.id)
        if not l:
            await cb.answer(await get_text("err_not_owner_service", "ru") or "Можно редактировать только свои услуги.", show_alert=True)
            await state.clear()
            return
        current = l.descr or "—"
    await state.set_state(ServiceLegacyEdit.waiting_descr)
    tmpl = await get_text("services_edit_descr_prompt", "ru") or (
        "📝 <b>Описание</b>\n\nТекущее значение:\n<code>{current}</code>\n\n"
        "Отправьте новый текст (или скопируйте текущий ↑ и отредактируйте):"
    )
    msg = await cb.message.answer(tmpl.format(current=escape(current)), parse_mode="HTML")
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
    await cb.answer()


@router.message(ServiceLegacyEdit.waiting_descr)
async def edit_descr_save(msg: Message, state: FSMContext):
    data = await state.get_data()
    listing_id = int(data["listing_id"])
    descr = (msg.text or "").strip() or None
    async with SessionLocal() as s:
        l = await _get_owned_service(s, listing_id, msg.from_user.id)
        if not l:
            await msg.answer(await get_text("err_not_owner_service", "ru") or "Можно редактировать только свои услуги.")
            await state.clear()
            return
        l.descr = descr
        s.add(l); await s.commit()
    ok = await msg.answer(await get_text("services_edit_descr_saved", "ru") or "Описание обновлено.")
    last_bot_messages.setdefault(msg.chat.id, []).append(ok.message_id)
    await register_bot_messages(msg.chat.id, [ok.message_id])
    await state.clear()


@router.callback_query(F.data.startswith("service_legacy_edit:price:"))
async def edit_price_start(cb: CallbackQuery, state: FSMContext):
    listing_id = int(cb.data.rsplit(":", 1)[1])
    await state.update_data(listing_id=listing_id)
    async with SessionLocal() as s:
        l = await _get_owned_service(s, listing_id, cb.from_user.id)
        if not l:
            await cb.answer(await get_text("err_not_owner_service", "ru") or "Можно редактировать только свои услуги.", show_alert=True)
            await state.clear()
            return
        current = l.price or "—"
    await state.set_state(ServiceLegacyEdit.waiting_price)
    tmpl = await get_text("services_edit_price_prompt", "ru") or (
        "💰 <b>Стоимость</b>\n\nТекущее значение:\n<code>{current}</code>\n\n"
        "Отправьте новую стоимость (или скопируйте текущую ↑ и отредактируйте):"
    )
    msg = await cb.message.answer(tmpl.format(current=escape(current)), parse_mode="HTML")
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
    await cb.answer()


@router.message(ServiceLegacyEdit.waiting_price)
async def edit_price_save(msg: Message, state: FSMContext):
    data = await state.get_data()
    listing_id = int(data["listing_id"])
    price = (msg.text or "").strip()
    async with SessionLocal() as s:
        l = await _get_owned_service(s, listing_id, msg.from_user.id)
        if not l:
            await msg.answer(await get_text("err_not_owner_service", "ru") or "Можно редактировать только свои услуги.")
            await state.clear()
            return
        l.price = price or (await get_text("services_add_btn_deal_price", "ru") or "Договорная")
        s.add(l); await s.commit()
    ok = await msg.answer(await get_text("services_edit_price_saved", "ru") or "Стоимость обновлена.")
    last_bot_messages.setdefault(msg.chat.id, []).append(ok.message_id)
    await register_bot_messages(msg.chat.id, [ok.message_id])
    await state.clear()


# -------------------------- ДОП. ПОЛЯ --------------------------

@router.callback_query(F.data.startswith("service_legacy_edit:extras:"))
async def edit_extras_start(cb: CallbackQuery, state: FSMContext):
    try:
        _, listing_id_s, cat_id_s = cb.data.rsplit(":", 2)
        listing_id = int(listing_id_s); cat_id = int(cat_id_s)
    except Exception:
        await cb.answer(await get_text("services_edit_invalid_params", "ru") or "Некорректные параметры", show_alert=True); return

    async with SessionLocal() as s:
        if not await _get_owned_service(s, listing_id, cb.from_user.id):
            await cb.answer(await get_text("err_not_owner_service", "ru") or "Можно редактировать только свои услуги.", show_alert=True)
            return
    await state.update_data(listing_id=listing_id)
    # Возврат — в обзор редактирования
    await start_extra_fields_for_category(cb, state, cat_id, resume_data=f"service_edit_overview:{listing_id}")

# -------------------------- ФИНИШ --------------------------

@router.callback_query(F.data.startswith("service_legacy_edit:finish"))
async def edit_finish(cb: CallbackQuery):
    await cb.answer(await get_text("services_edit_finished", "ru") or "Редактирование завершено.")
    # Можно вернуть в «Мои услуги» или в объявление — оставляю нейтрально:
    main_btn = await get_common_menu_button('main_menu', 'ru')
    kb = InlineKeyboardMarkup(inline_keyboard=[[main_btn]] if main_btn else [])
    done = await cb.message.answer(await get_text("services_edit_done", "ru") or "Готово.", reply_markup=kb)
    last_bot_messages.setdefault(cb.message.chat.id, []).append(done.message_id)
    await register_bot_messages(cb.message.chat.id, [done.message_id])
