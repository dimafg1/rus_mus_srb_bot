"""
app/routers/catalog_add.py
-------------------------

This router contains the finite‑state machine for submitting a new
specialist/portfolio entry.  A user enters several fields (category,
name, address, photo, description, repository link) and then is
presented with a summary for confirmation.  The original logic lived
in ``main.py``; extracting it into its own module makes the
monolithic ``main.py`` easier to read and parallels the structure of
the flea‑market (Барахолка) add handlers found in
``market_add.py``.

Currently the collected data is not persisted to the database.  The
interaction is purely conversational: once the user confirms, the
bot thanks them.  A future enhancement could insert a row into
``Item`` or another appropriate model.

Usage:
    from app.routers.catalog_add import router as catalog_add_router
    dp.include_router(catalog_add_router)
"""

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

from app.keyboards import catalog_application_category_inline
from app.routers.utils import safe_edit_or_send
from app.texts import get_text
from app.database import SessionLocal
from app.models import Category
from sqlalchemy import select
from app.keyboards import get_common_menu_button
from app.routers.utils import clear_bot_messages, last_bot_messages  # и любые другие, если используются
from app.models import City



router = Router(name="catalog_add")


class CatalogAddForm(StatesGroup):
    """Finite‑state machine describing the fields for a portfolio application."""
    category_choice = State()
    name = State()
    address = State()
    photo = State()
    description = State()
    repo = State()
    confirm = State()


from app.keyboards import build_city_buttons
from app.keyboards import catalog_cities_inline

@router.callback_query(F.data == "apply_catalog")
async def apply_catalog_handler(cb: CallbackQuery, state: FSMContext) -> None:
    markup = await catalog_cities_inline()
    await cb.message.edit_text("Выберите город для анкеты:", reply_markup=markup)
    await state.set_state(CatalogAddForm.category_choice)
    await cb.answer()

from app.keyboards import main_inline_menu

from app.keyboards import catalog_inline_initial

@router.callback_query(F.data == "catalog_back", CatalogAddForm.category_choice)
async def catalog_back_handler(cb: CallbackQuery, state: FSMContext):
    markup = await catalog_inline_initial()
    await cb.message.edit_text("Каталог — выберите действие:", reply_markup=markup)
    await state.clear()
    await cb.answer()


from app.keyboards import catalog_profile_category_inline

# @router.callback_query(F.data.startswith("apply_city:"), CatalogAddForm.category_choice)
# async def apply_catalog_city_handler(cb: CallbackQuery, state: FSMContext):
#     city_slug = cb.data.split(":")[1]
#     markup = await catalog_profile_category_inline(city_slug)
#     await cb.message.edit_text("Выберите категорию анкеты:", reply_markup=markup)
#     await state.update_data(city=city_slug)
#     await state.set_state(CatalogAddForm.category_choice)
#     await cb.answer()

@router.callback_query(F.data == "catalog_city_back", CatalogAddForm.category_choice)
async def catalog_city_back(cb: CallbackQuery, state: FSMContext):
    city_buttons = await build_city_buttons("apply_city")
    markup = InlineKeyboardMarkup(inline_keyboard=[city_buttons])
    await cb.message.edit_text("Выберите город для анкеты:", reply_markup=markup)
    await cb.answer()




@router.callback_query(F.data.startswith("apply_city:"), CatalogAddForm.category_choice)
async def catalog_city(cb: CallbackQuery, state: FSMContext):
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    city_slug = cb.data.split(":")[1]
    async with SessionLocal() as s:
        city = (await s.execute(select(City).where(City.slug == city_slug))).scalar_one()
        profile_root = (await s.execute(select(Category).where(Category.slug == "profile"))).scalar_one()
        subcats = (await s.execute(
            select(Category).where(Category.parent_id == profile_root.id)
        )).scalars().all()
    await state.update_data(city_id=city.id, city_name=city.name, city_slug=city_slug)
    kb = await catalog_profile_category_inline(subcats, city_slug)
    template = f"Город: <b>{city.name}</b>\nВыберите категорию:"
    msg = await cb.message.answer(template, reply_markup=kb, parse_mode="HTML")
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
    await state.set_state(CatalogAddForm.category_choice)
    await cb.answer()

@router.callback_query(F.data.startswith("profile_cat:"), CatalogAddForm.category_choice)
async def catalog_profile_cat(cb: CallbackQuery, state: FSMContext):
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    _, city_slug, cat_id = cb.data.split(":")
    cat_id = int(cat_id)
    async with SessionLocal() as s:
        cat = (await s.execute(select(Category).where(Category.id == cat_id))).scalar_one()
        subcats = (await s.execute(select(Category).where(Category.parent_id == cat_id))).scalars().all()
    if subcats:
        kb = await catalog_profile_category_inline(subcats, city_slug)
        template = f"Категория: <b>{cat.name}</b>\nВыберите подкатегорию:"
        msg = await cb.message.answer(template, reply_markup=kb, parse_mode="HTML")
        last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
        await state.update_data(cat_id=cat.id, cat_name=cat.name)
        await state.set_state(CatalogAddForm.category_choice)
    else:
        await clear_bot_messages(cb.message.chat.id, cb.bot)
        await state.update_data(cat_id=cat.id, cat_name=cat.name)
        await state.set_state(CatalogAddForm.name)
        await cb.message.answer("Введите название группы/студии/площадки:")
    await cb.answer()


@router.callback_query(F.data.startswith("capcat:"), CatalogAddForm.category_choice)
async def catalog_application_category_handler(cb: CallbackQuery, state: FSMContext) -> None:
    category = cb.data.split(":", 1)[1]
    await state.update_data(category_choice=category)
    await cb.message.edit_text(
        f"Вы выбрали направление: <b>{category.capitalize()}</b>\nВведите название группы/студии/площадки:",
        parse_mode="HTML"
    )
    await state.set_state(CatalogAddForm.name)
    await cb.answer()


@router.message(CatalogAddForm.name)
async def get_catalog_name(m: Message, state: FSMContext) -> None:
    """Store the name and prompt for an optional address."""
    await state.update_data(name=m.text)
    await m.answer("Введите адрес (необязательно, можно пропустить):")
    await state.set_state(CatalogAddForm.address)


@router.message(CatalogAddForm.address)
async def get_catalog_address(m: Message, state: FSMContext) -> None:
    """Store the address and ask for a photo placeholder.

    Note: The original code accepted a photo here, but didn't persist
    it.  We simply capture the text or placeholder for consistency.
    """
    await state.update_data(address=m.text)
    await m.answer("Прикрепите фото (можно до 3-х, или пропустите):")
    await state.set_state(CatalogAddForm.photo)


@router.message(CatalogAddForm.photo)
async def get_catalog_photo(m: Message, state: FSMContext) -> None:
    """Store the photo placeholder and prompt for a description."""
    await state.update_data(photo=m.text)
    await m.answer("Введите описание ваших умений или информации о группе/студии:")
    await state.set_state(CatalogAddForm.description)


@router.message(CatalogAddForm.description)
async def get_catalog_description(m: Message, state: FSMContext) -> None:
    """Store the description and prompt for a repository link."""
    await state.update_data(description=m.text)
    await m.answer("Введите информацию о реп. базе (ссылка, если есть):")
    await state.set_state(CatalogAddForm.repo)


@router.message(CatalogAddForm.repo)
async def get_catalog_repo(m: Message, state: FSMContext) -> None:
    """Show a summary and ask the user to confirm submission."""
    data = await state.get_data()
    data["repo"] = m.text
    summary = (
        f"Направление: {data.get('category_choice')}\n"
        f"Название: {data.get('name')}\n"
        f"Адрес: {data.get('address')}\n"
        f"Фото: {data.get('photo')}\n"
        f"Описание: {data.get('description')}\n"
        f"Реп. база: {data.get('repo')}"
    )
    await m.answer(
        f"Проверьте введённые данные:\n\n{summary}\n\nПодтвердите отправку.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Да", callback_data="catalog_confirm:yes"),
             InlineKeyboardButton(text="Нет", callback_data="catalog_confirm:no")]
        ]),
        parse_mode="HTML"
    )
    await state.set_state(CatalogAddForm.confirm)


@router.callback_query(F.data.startswith("catalog_confirm:"), CatalogAddForm.confirm)
async def catalog_confirm_handler(cb: CallbackQuery, state: FSMContext) -> None:
    """Handle final confirmation of the portfolio application."""
    decision = cb.data.split(":", 1)[1]
    if decision == "yes":
        # In a future version, persist data to the database here.
        await cb.message.edit_text("Ваша заявка принята. Спасибо!")
    else:
        await cb.message.edit_text("Заявка отменена.")
    await state.clear()
    await cb.answer()