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


@router.callback_query(F.data == "apply_catalog")
async def apply_catalog_handler(cb: CallbackQuery, state: FSMContext) -> None:
    """Start the portfolio application process.

    The handler prompts the user to choose a high‑level direction for
    their application (musician, vocal, production, etc.).  The
    directions are defined in ``catalog_application_category_inline``.
    """
    await cb.message.edit_text(
        "Выберите направление вашей заявки:",
        reply_markup=catalog_application_category_inline()
    )
    await state.set_state(CatalogAddForm.category_choice)
    await cb.answer()


@router.callback_query(F.data.startswith("capcat:"), CatalogAddForm.category_choice)
async def catalog_application_category_handler(cb: CallbackQuery, state: FSMContext) -> None:
    """Record the selected high‑level category and ask for the name."""
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