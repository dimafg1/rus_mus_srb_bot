import html

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Listing, City, Category
from app.states import EditListing
from app.keyboards import get_common_menu_button
from app.routers.utils import clear_bot_messages, last_bot_messages, register_bot_messages, get_text

# доп-поля
from app.routers.user_extra_fields import (
    start_extra_fields_for_category,
    extra_next,
    extra_finish,
    extra_back,
)

router = Router()

# -------------------------- ВНУТРЕННИЕ УТИЛИТЫ --------------------------

async def _get_listing(s, listing_id: int, owner_id: int) -> Listing | None:
    """Return only an owned market listing; callback/FSM ids are untrusted."""
    return (await s.execute(select(Listing).where(
        Listing.id == listing_id,
        Listing.owner_id == owner_id,
        Listing.type == "market",
    ))).scalar_one_or_none()

async def _slugs_for_listing(s, listing: Listing):
    city = (await s.execute(select(City).where(City.id == listing.city_id))).scalar_one()
    cat  = (await s.execute(select(Category).where(Category.id == listing.category_id))).scalar_one()
    return city.slug, cat.slug

async def _nav_row(city_slug: str, cat_slug: str, listing_id: int):
    # Кнопки: Пропустить, Завершить, Назад (унифицировано)
    back_btn = await get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data="edit:back")
    back_btn.callback_data = "edit:back"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data="edit:skip")],
        [InlineKeyboardButton(text="✅ Завершить", callback_data=f"edit:finish:{listing_id}")],
        [back_btn]
    ])

def _is_extra_mode(data: dict) -> bool:
    # Мы в «доп-мастере», если в FSM есть описания доп-полей
    return "extra_defs" in data and data.get("extra_defs") is not None

def _extra_index(data: dict) -> int:
    return int(data.get("extra_idx", 0))

# -------------------------- ВОПРОСЫ ОСНОВНЫХ ПОЛЕЙ --------------------------

async def _ask_title(ev, state: FSMContext, listing: Listing, city_slug: str, cat_slug: str):
    chat_id = ev.message.chat.id if isinstance(ev, CallbackQuery) else ev.chat.id
    bot = ev.message.bot if isinstance(ev, CallbackQuery) else ev.bot
    await clear_bot_messages(chat_id, bot)

    kb = await _nav_row(city_slug, cat_slug, listing.id)
    msg = await (ev.message.answer if isinstance(ev, CallbackQuery) else ev.answer)(
        f"🪧 <b>Заголовок</b>\n\nТекущее значение:\n<code>{html.escape(str(listing.title or '—'))}</code>\n\n"
        "Отправьте новый текст (или скопируйте текущий ↑ и отредактируйте):",
        reply_markup=kb, parse_mode="HTML"
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await state.set_state(EditListing.waiting_title)
    print(f"[edit.ask_title] chat={chat_id} user={ev.from_user.id} listing={listing.id}")

async def _ask_price(ev, state: FSMContext, listing: Listing, city_slug: str, cat_slug: str):
    chat_id = ev.message.chat.id if isinstance(ev, CallbackQuery) else ev.chat.id
    bot = ev.message.bot if isinstance(ev, CallbackQuery) else ev.bot
    await clear_bot_messages(chat_id, bot)

    kb = await _nav_row(city_slug, cat_slug, listing.id)
    msg = await (ev.message.answer if isinstance(ev, CallbackQuery) else ev.answer)(
        f"💰 <b>Цена</b>\n\nТекущее значение:\n<code>{html.escape(str(listing.price or '—'))}</code>\n\n"
        "Отправьте новую цену (или скопируйте текущую ↑ и отредактируйте):",
        reply_markup=kb, parse_mode="HTML"
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await state.set_state(EditListing.waiting_price)
    print(f"[edit.ask_price] chat={chat_id} user={ev.from_user.id} listing={listing.id}")

async def _ask_descr(ev, state: FSMContext, listing: Listing, city_slug: str, cat_slug: str):
    chat_id = ev.message.chat.id if isinstance(ev, CallbackQuery) else ev.chat.id
    bot = ev.message.bot if isinstance(ev, CallbackQuery) else ev.bot
    await clear_bot_messages(chat_id, bot)

    kb = await _nav_row(city_slug, cat_slug, listing.id)
    msg = await (ev.message.answer if isinstance(ev, CallbackQuery) else ev.answer)(
        "📝 <b>Описание</b>\n\nТекущее значение:\n"
        f"<code>{html.escape(str(listing.descr or '—'))}</code>\n\n"
        "Отправьте новый текст (или скопируйте текущий ↑ и отредактируйте):",
        reply_markup=kb, parse_mode="HTML"
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await state.set_state(EditListing.waiting_descr)
    print(f"[edit.ask_descr] chat={chat_id} user={ev.from_user.id} listing={listing.id}")

async def _go_flex_wizard(ev, state: FSMContext, listing: Listing, city_slug: str, cat_slug: str):
    """
    Переходим к мастеру доп. полей (user_extra_fields.py).
    Он сам задаёт вопросы и в конце сохранит listing.flex.
    Возврат «Назад с первого шага доп-полей» делаем в edit_back -> _ask_descr.
    """
    await state.update_data(listing_id=listing.id, city_slug=city_slug, cat_slug=cat_slug)
    print(f"[edit.go_flex] user={ev.from_user.id} listing={listing.id}")
    resume = f"listing:{listing.id}:{city_slug}:{cat_slug}:my"
    await start_extra_fields_for_category(ev, state, listing.category_id, resume_data=resume)

# -------------------------- СТАРТ МАСТЕРА РЕДАКТИРОВАНИЯ --------------------------

@router.callback_query(F.data.startswith("edit_listing:"))
async def edit_listing_menu(cb: CallbackQuery, state: FSMContext):
    try:
        await cb.message.delete()
    except Exception:
        pass
    try:
        listing_id = int(cb.data.split(":")[1])
    except (IndexError, TypeError, ValueError):
        await cb.answer(await get_text("err_invalid_data", "ru") or "Некорректные данные.", show_alert=True)
        return
    async with SessionLocal() as s:
        listing = await _get_listing(s, listing_id, cb.from_user.id)
        if listing is None:
            await cb.answer("Можно редактировать только свои объявления.", show_alert=True)
            return
        city_slug, cat_slug = await _slugs_for_listing(s, listing)

    await state.update_data(edit_listing_id=listing_id, city_slug=city_slug, cat_slug=cat_slug)
    print(f"[edit.start] chat={cb.message.chat.id} user={cb.from_user.id} listing={listing_id}")
    await _ask_title(cb, state, listing, city_slug, cat_slug)
    await cb.answer()

# -------------------------- КНОПКИ «ПРОПУСТИТЬ / ЗАВЕРШИТЬ / НАЗАД» --------------------------

@router.callback_query(F.data == "edit:skip")
async def edit_skip(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    if not listing_id:
        await cb.answer("Сеанс редактирования потерян. Откройте карточку ещё раз.", show_alert=True)
        await state.clear()
        print(f"[edit.skip] lost session chat={cb.message.chat.id}")
        return

    async with SessionLocal() as s:
        listing = await _get_listing(s, int(listing_id), cb.from_user.id)
        if listing is None:
            await cb.answer("Можно редактировать только свои объявления.", show_alert=True)
            await state.clear()
            return
        city_slug, cat_slug = await _slugs_for_listing(s, listing)

    # Если сейчас идёт мастер доп-полей — делегируем туда только после проверки владельца.
    cur_state = await state.get_state()
    if _is_extra_mode(data) and cur_state not in (EditListing.waiting_title, EditListing.waiting_price, EditListing.waiting_descr):
        print(f"[edit.skip->extra_next] chat={cb.message.chat.id} user={cb.from_user.id}")
        await extra_next(cb, state)
        await cb.answer()
        return

    print(f"[edit.skip.core] chat={cb.message.chat.id} state={cur_state}")
    if cur_state == EditListing.waiting_title:
        await _ask_price(cb, state, listing, city_slug, cat_slug)
    elif cur_state == EditListing.waiting_price:
        await _ask_descr(cb, state, listing, city_slug, cat_slug)
    elif cur_state == EditListing.waiting_descr:
        await _go_flex_wizard(cb, state, listing, city_slug, cat_slug)
    else:
        await cb.answer()

@router.callback_query(F.data.startswith("edit:finish"))
async def edit_finish(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    if not listing_id:
        await cb.answer("Сеанс редактирования потерян. Откройте карточку ещё раз.", show_alert=True)
        await state.clear()
        print(f"[edit.finish] lost session chat={cb.message.chat.id}")
        return

    async with SessionLocal() as s:
        listing = await _get_listing(s, int(listing_id), cb.from_user.id)
        if listing is None:
            await cb.answer("Можно редактировать только свои объявления.", show_alert=True)
            await state.clear()
            return
        city_slug, cat_slug = await _slugs_for_listing(s, listing)

    # Делегируем в доп-мастер только после проверки владельца и типа.
    cur_state = await state.get_state()
    if _is_extra_mode(data) and cur_state not in (EditListing.waiting_title, EditListing.waiting_price, EditListing.waiting_descr):
        print(f"[edit.finish->extra_finish] chat={cb.message.chat.id} user={cb.from_user.id}")
        await extra_finish(cb, state)
        await cb.answer()
        return

    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Вернуться к объявлению",
                              callback_data=f"listing:{listing.id}:{city_slug}:{cat_slug}:my")]
    ])
    msg = await cb.message.answer("Изменения сохранены ✅", reply_markup=kb)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await state.clear()
    await cb.answer()
    print(f"[edit.finish.core] chat={chat_id} user={cb.from_user.id} listing={listing.id}")

@router.callback_query(F.data == "edit:back")
async def edit_back(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    if not listing_id:
        await cb.answer("Сеанс редактирования потерян. Откройте карточку ещё раз.", show_alert=True)
        await state.clear()
        print(f"[edit.back] lost session chat={cb.message.chat.id}")
        return

    async with SessionLocal() as s:
        listing = await _get_listing(s, int(listing_id), cb.from_user.id)
        if listing is None:
            await cb.answer("Можно редактировать только свои объявления.", show_alert=True)
            await state.clear()
            return
        city_slug, cat_slug = await _slugs_for_listing(s, listing)

    cur_state = await state.get_state()

    # Ветвь доп-полей
    if _is_extra_mode(data) and cur_state not in (EditListing.waiting_title, EditListing.waiting_price, EditListing.waiting_descr):
        if _extra_index(data) > 0:
            print(f"[edit.back->extra_back] chat={cb.message.chat.id} user={cb.from_user.id}")
            await extra_back(cb, state)
            await cb.answer()
            return
        # На первом доп-шаге — вернёмся к «описанию»
        print(f"[edit.back.first_extra->descr] chat={cb.message.chat.id} user={cb.from_user.id}")
        await _ask_descr(cb, state, listing, city_slug, cat_slug)
        await cb.answer()
        return

    # Ветвь основных полей
    if cur_state == EditListing.waiting_descr:
        await _ask_price(cb, state, listing, city_slug, cat_slug)
    elif cur_state == EditListing.waiting_price:
        await _ask_title(cb, state, listing, city_slug, cat_slug)
    else:
        # С заголовка «назад» — в карточку
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ Вернуться к объявлению",
                                 callback_data=f"listing:{listing.id}:{city_slug}:{cat_slug}:my")
        ]])
        chat_id = cb.message.chat.id
        await clear_bot_messages(chat_id, cb.bot)
        msg = await cb.message.answer("Возврат к объявлению.", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"[edit.back.core] chat={cb.message.chat.id} user={cb.from_user.id} state={cur_state}")

# -------------------------- ВВОД ЗНАЧЕНИЙ ОСНОВНЫХ ПОЛЕЙ --------------------------

@router.message(EditListing.waiting_title)
async def edit_title_apply(m: Message, state: FSMContext):
    try:
        await m.delete()  # RU: удаляем ввод пользователя, чтобы не висел справа
    except Exception:
        pass
    new_title = (m.text or "").strip()
    if not new_title:
        await m.answer("Заголовок не может быть пустым.")
        return
    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    if not listing_id:
        await m.answer("Сеанс редактирования потерян. Откройте карточку ещё раз.")
        await state.clear()
        print(f"[edit.title.apply] lost session chat={m.chat.id}")
        return

    async with SessionLocal() as s:
        listing = await _get_listing(s, int(listing_id), m.from_user.id)
        if listing is None:
            await m.answer("Можно редактировать только свои объявления.")
            await state.clear()
            print(f"[edit.title.apply] forbidden user={m.from_user.id} listing={listing_id}")
            return
        listing.title = new_title
        await s.commit()
        city_slug, cat_slug = await _slugs_for_listing(s, listing)

    print(f"[edit.title.saved] chat={m.chat.id} user={m.from_user.id} listing={listing_id} title='{new_title}'")
    await _ask_price(m, state, listing, city_slug, cat_slug)

@router.message(EditListing.waiting_price)
async def edit_price_apply(m: Message, state: FSMContext):
    try:
        await m.delete()  # RU: удаляем ввод пользователя, чтобы не висел справа
    except Exception:
        pass
    new_price = (m.text or "").strip()
    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    if not listing_id:
        await m.answer("Сеанс редактирования потерян. Откройте карточку ещё раз.")
        await state.clear()
        print(f"[edit.price.apply] lost session chat={m.chat.id}")
        return

    async with SessionLocal() as s:
        listing = await _get_listing(s, int(listing_id), m.from_user.id)
        if listing is None:
            await m.answer("Можно редактировать только свои объявления.")
            await state.clear()
            print(f"[edit.price.apply] forbidden user={m.from_user.id} listing={listing_id}")
            return
        listing.price = new_price
        await s.commit()
        city_slug, cat_slug = await _slugs_for_listing(s, listing)

    print(f"[edit.price.saved] chat={m.chat.id} user={m.from_user.id} listing={listing_id} price='{new_price}'")
    await _ask_descr(m, state, listing, city_slug, cat_slug)

@router.message(EditListing.waiting_descr)
async def edit_descr_apply(m: Message, state: FSMContext):
    try:
        await m.delete()  # RU: удаляем ввод пользователя, чтобы не висел справа
    except Exception:
        pass
    new_descr = (m.text or "").strip()
    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    if not listing_id:
        await m.answer("Сеанс редактирования потерян. Откройте карточку ещё раз.")
        await state.clear()
        print(f"[edit.descr.apply] lost session chat={m.chat.id}")
        return

    async with SessionLocal() as s:
        listing = await _get_listing(s, int(listing_id), m.from_user.id)
        if listing is None:
            await m.answer("Можно редактировать только свои объявления.")
            await state.clear()
            print(f"[edit.descr.apply] forbidden user={m.from_user.id} listing={listing_id}")
            return
        listing.descr = new_descr
        await s.commit()
        city_slug, cat_slug = await _slugs_for_listing(s, listing)

    print(f"[edit.descr.saved] chat={m.chat.id} user={m.from_user.id} listing={listing_id} descr_len={len(new_descr)}")
    # После описания — переходим к доп. полям (flex-мастер)
    await _go_flex_wizard(m, state, listing, city_slug, cat_slug)
