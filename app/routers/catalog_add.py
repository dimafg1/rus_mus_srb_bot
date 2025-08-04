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
import inspect



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
    """
    Начало публикации анкеты в каталоге. Перед показом списка городов
    очищаем предыдущие сообщения и выводим навигационную панель с
    кнопками «Назад» и «Главное меню», как это реализовано в
    разделе Барахолка. Затем отправляем сообщение с выбором города.
    """
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    # Панель возврата: «Назад» отправляет к корню каталога, т.е. callback «catalog:back»
    # nav_text = await get_text('return_to_menu', 'ru') or "Возврат"
    # back_btn = await get_common_menu_button('catalog_back')
    # main_menu_btn = await get_common_menu_button('main_menu')
    # nav_buttons = []
    # if back_btn:
    #     nav_buttons.append(InlineKeyboardButton(text=back_btn.text, callback_data=back_btn.callback_data))
    # if main_menu_btn:
    #     nav_buttons.append(InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data))
    # nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons]) if nav_buttons else None
    # nav_msg = await cb.bot.send_message(chat_id, nav_text, reply_markup=nav_markup)
    # Сообщение выбора города
    markup = await catalog_cities_inline()
    prompt = "Выберите город для анкеты:"
    msg = await cb.bot.send_message(chat_id, prompt, reply_markup=markup)
    last_bot_messages[chat_id] = [msg.message_id]
    await state.set_state(CatalogAddForm.category_choice)
    await cb.answer()
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {getattr(cb.message.chat, 'id', None)} | "
        f"user_id: {getattr(cb.from_user, 'id', None)} | "
        f"msg_ids: {last_bot_messages.get(cb.message.chat.id) if 'last_bot_messages' in globals() else 'n/a'} | "
        f"keyboard_rows: {len(markup.inline_keyboard) if 'markup' in locals() else 'n/a'}"
    )

from app.keyboards import main_inline_menu

from app.keyboards import catalog_inline_initial

@router.callback_query(F.data == "catalog_back", CatalogAddForm.category_choice)
async def catalog_back_handler(cb: CallbackQuery, state: FSMContext):
    """
    Обработчик кнопки «catalog_back» в процессе добавления анкеты. Возвращает
    пользователя к начальному экрану каталога. Также добавляем панель
    возврата наверху.
    """
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    # Панель возврата (обе кнопки ведут в главное меню)
    # nav_text = await get_text('return_to_menu', 'ru') or "Возврат"
    # base_back_btn = await get_common_menu_button('back')
    # main_menu_btn = await get_common_menu_button('main_menu')
    # nav_buttons = []
    # if base_back_btn:
    #     nav_buttons.append(InlineKeyboardButton(text=base_back_btn.text, callback_data=main_menu_btn.callback_data if main_menu_btn else 'main_menu'))
    # if main_menu_btn:
    #     nav_buttons.append(InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data))
    # nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons]) if nav_buttons else None
    # nav_msg = await cb.bot.send_message(chat_id, nav_text, reply_markup=nav_markup)
    # Сообщение каталога
    markup = await catalog_inline_initial()
    msg = await cb.bot.send_message(chat_id, "Каталог — выберите действие:", reply_markup=markup)
    last_bot_messages[chat_id] = [msg.message_id]
    await state.clear()
    await cb.answer()
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {getattr(cb.message.chat, 'id', None)} | "
        f"user_id: {getattr(cb.from_user, 'id', None)} | "
        f"msg_ids: {last_bot_messages.get(cb.message.chat.id) if 'last_bot_messages' in globals() else 'n/a'} | "
        f"keyboard_rows: {len(markup.inline_keyboard) if 'markup' in locals() else 'n/a'}"
    )


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
    """
    Обработчик кнопки возврата на шаг выбора города. Показывает только список городов и внизу две кнопки.
    """
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)

    city_buttons = await build_city_buttons("apply_city")
    markup = InlineKeyboardMarkup(inline_keyboard=[city_buttons])

    # Добавляем "Назад" (в корень каталога) и "Главное меню" в одну строку в самый низ
    back_btn = await get_common_menu_button('back')
    main_menu_btn = await get_common_menu_button('main_menu')
    nav_row = []
    if back_btn:
        nav_row.append(InlineKeyboardButton(text=back_btn.text, callback_data='catalog:back'))
    if main_menu_btn:
        nav_row.append(InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data))
    if nav_row:
        markup.inline_keyboard.append(nav_row)

    msg = await cb.bot.send_message(chat_id, "Выберите город для анкеты:", reply_markup=markup)
    last_bot_messages[chat_id] = [msg.message_id]
    await cb.answer()
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {getattr(cb.message.chat, 'id', None)} | "
        f"user_id: {getattr(cb.from_user, 'id', None)} | "
        f"msg_ids: {last_bot_messages.get(cb.message.chat.id) if 'last_bot_messages' in globals() else 'n/a'} | "
        f"keyboard_rows: {len(markup.inline_keyboard) if 'markup' in locals() else 'n/a'}"
    )




@router.callback_query(F.data.startswith("apply_city:"), CatalogAddForm.category_choice)
async def catalog_city(cb: CallbackQuery, state: FSMContext):
    """
    Пользователь выбрал город для анкеты. Очищаем предыдущее, выводим список категорий профиля для выбранного города,
    а внизу — кнопки "Назад" и "Главное меню".
    """
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    city_slug = cb.data.split(":")[1]
    async with SessionLocal() as s:
        city = (await s.execute(select(City).where(City.slug == city_slug))).scalar_one()
        profile_root = (await s.execute(select(Category).where(Category.slug == "profile"))).scalar_one()
        subcats = (await s.execute(
            select(Category).where(Category.parent_id == profile_root.id)
        )).scalars().all()
    await state.update_data(city_id=city.id, city_name=city.name, city_slug=city_slug)

    # Строим клавиатуру с категориями, без добавления "Назад" и "Главное меню" внутри!
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=cat.name, callback_data=f"profile_cat:{city_slug}:{cat.id}")]
            for cat in subcats
        ]
    )
    # Добавляем “Назад” и “Главное меню” в конец клавиатуры
    back_btn = await get_common_menu_button('back')
    main_menu_btn = await get_common_menu_button('main_menu')
    nav_row = []
    if back_btn:
        nav_row.append(InlineKeyboardButton(text=back_btn.text, callback_data='catalog_city_back'))
    if main_menu_btn:
        nav_row.append(InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data))
    if nav_row:
        kb.inline_keyboard.append(nav_row)

    template = f"<b>Каталог ➔ {city.name}</b>"
    msg = await cb.bot.send_message(chat_id, template, reply_markup=kb, parse_mode="HTML")
    last_bot_messages[chat_id] = [msg.message_id]
    await state.set_state(CatalogAddForm.category_choice)
    await cb.answer()
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {getattr(cb.message.chat, 'id', None)} | "
        f"user_id: {getattr(cb.from_user, 'id', None)} | "
        f"msg_ids: {last_bot_messages.get(cb.message.chat.id) if 'last_bot_messages' in globals() else 'n/a'} | "
        f"keyboard_rows: {len(kb.inline_keyboard) if 'kb' in locals() else 'n/a'}"
    )


@router.callback_query(F.data.startswith("profile_cat:"), CatalogAddForm.category_choice)
async def catalog_profile_cat(cb: CallbackQuery, state: FSMContext):
    """
    Пользователь выбрал категорию профиля. Если есть подкатегории, выводим
    их список. Иначе переходим к вводу названия анкеты. В обоих случаях
    отображается навигационная панель с «Назад» и «Главное меню».
    """
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    _, city_slug, cat_id = cb.data.split(":")
    cat_id = int(cat_id)
    async with SessionLocal() as s:
        cat = (await s.execute(select(Category).where(Category.id == cat_id))).scalar_one()
        subcats = (await s.execute(select(Category).where(Category.parent_id == cat_id))).scalars().all()
    # Навигационная панель: назад к выбору категории (catalog_city_back)
    nav_text = await get_text('return_to_menu', 'ru') or "Возврат"
    base_back_btn = await get_common_menu_button('back')
    main_menu_btn = await get_common_menu_button('main_menu')
    nav_buttons = []
    if base_back_btn:
        nav_buttons.append(InlineKeyboardButton(text=base_back_btn.text, callback_data='catalog_city_back'))
    if main_menu_btn:
        nav_buttons.append(InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data))
    nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons]) if nav_buttons else None
    nav_msg = await cb.bot.send_message(chat_id, nav_text, reply_markup=nav_markup)
    if subcats:
        kb = await catalog_profile_category_inline(subcats, city_slug)
        template = f"Категория: <b>{cat.name}</b>\nВыберите подкатегорию:"
        msg = await cb.bot.send_message(chat_id, template, reply_markup=kb, parse_mode="HTML")
        last_bot_messages[chat_id] = [nav_msg.message_id, msg.message_id]
        await state.update_data(cat_id=cat.id, cat_name=cat.name)
        await state.set_state(CatalogAddForm.category_choice)
    else:
        # Нет подкатегорий — переходим к вводу названия
        await state.update_data(cat_id=cat.id, cat_name=cat.name)
        await state.set_state(CatalogAddForm.name)
        prompt_msg = await cb.bot.send_message(chat_id, "Введите название группы/студии/площадки:")
        last_bot_messages[chat_id] = [nav_msg.message_id, prompt_msg.message_id]
    await cb.answer()

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {getattr(cb.message.chat, 'id', None)} | "
        f"user_id: {getattr(cb.from_user, 'id', None)} | "
        f"msg_ids: {last_bot_messages.get(cb.message.chat.id) if 'last_bot_messages' in globals() else 'n/a'} | "
        f"keyboard_rows: {len(markup.inline_keyboard) if 'markup' in locals() else 'n/a'}"
    )



@router.callback_query(F.data.startswith("capcat:"), CatalogAddForm.category_choice)
async def catalog_application_category_handler(cb: CallbackQuery, state: FSMContext) -> None:
    """
    Устаревший обработчик выбора категории заявки. Раньше просто редактировал
    существующее сообщение. Теперь очищаем интерфейс, показываем панель
    возврата и отправляем новый запрос на ввод названия.
    """
    chat_id = cb.message.chat.id
    category = cb.data.split(":", 1)[1]
    await state.update_data(category_choice=category)
    await clear_bot_messages(chat_id, cb.bot)
    # Панель возврата: назад к корню каталога
    nav_text = await get_text('return_to_menu', 'ru') or "Возврат"
    back_btn = await get_common_menu_button('catalog_back')
    main_menu_btn = await get_common_menu_button('main_menu')
    nav_buttons = []
    if back_btn:
        nav_buttons.append(InlineKeyboardButton(text=back_btn.text, callback_data=back_btn.callback_data))
    if main_menu_btn:
        nav_buttons.append(InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data))
    nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons]) if nav_buttons else None
    nav_msg = await cb.bot.send_message(chat_id, nav_text, reply_markup=nav_markup)
    # Сообщение запроса названия
    prompt_text = f"Вы выбрали направление: <b>{category.capitalize()}</b>\nВведите название группы/студии/площадки:"
    msg = await cb.bot.send_message(chat_id, prompt_text, parse_mode="HTML")
    last_bot_messages[chat_id] = [nav_msg.message_id, msg.message_id]
    await state.set_state(CatalogAddForm.name)
    await cb.answer()

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {getattr(cb.message.chat, 'id', None)} | "
        f"user_id: {getattr(cb.from_user, 'id', None)} | "
        f"msg_ids: {last_bot_messages.get(cb.message.chat.id) if 'last_bot_messages' in globals() else 'n/a'} | "
        f"keyboard_rows: {len(markup.inline_keyboard) if 'markup' in locals() else 'n/a'}"
    )



@router.message(CatalogAddForm.name)
async def get_catalog_name(m: Message, state: FSMContext) -> None:
    """
    Запрашиваем у пользователя адрес после ввода названия. Перед отправкой
    нового сообщения очищаем предыдущее и показываем панель возврата.
    """
    chat_id = m.chat.id
    await clear_bot_messages(chat_id, m.bot)
    await state.update_data(name=m.text)
    # Панель возврата: назад к выбору категории
    nav_text = await get_text('return_to_menu', 'ru') or "Возврат"
    base_back_btn = await get_common_menu_button('back')
    main_menu_btn = await get_common_menu_button('main_menu')
    nav_buttons = []
    if base_back_btn:
        nav_buttons.append(InlineKeyboardButton(text=base_back_btn.text, callback_data='catalog_city_back'))
    if main_menu_btn:
        nav_buttons.append(InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data))
    nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons]) if nav_buttons else None
    nav_msg = await m.answer(nav_text, reply_markup=nav_markup)
    # Запрос адреса
    msg = await m.answer("Введите адрес (необязательно, можно пропустить):")
    last_bot_messages[chat_id] = [nav_msg.message_id, msg.message_id]
    await state.set_state(CatalogAddForm.address)

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {getattr(cb.message.chat, 'id', None)} | "
        f"user_id: {getattr(cb.from_user, 'id', None)} | "
        f"msg_ids: {last_bot_messages.get(cb.message.chat.id) if 'last_bot_messages' in globals() else 'n/a'} | "
        f"keyboard_rows: {len(markup.inline_keyboard) if 'markup' in locals() else 'n/a'}"
    )



@router.message(CatalogAddForm.address)
async def get_catalog_address(m: Message, state: FSMContext) -> None:
    """
    Сохраняем адрес и просим прикрепить фото. Предварительно очищаем
    предыдущие сообщения и отображаем панель возврата.
    """
    chat_id = m.chat.id
    await clear_bot_messages(chat_id, m.bot)
    await state.update_data(address=m.text)
    nav_text = await get_text('return_to_menu', 'ru') or "Возврат"
    base_back_btn = await get_common_menu_button('back')
    main_menu_btn = await get_common_menu_button('main_menu')
    nav_buttons = []
    if base_back_btn:
        nav_buttons.append(InlineKeyboardButton(text=base_back_btn.text, callback_data='catalog_city_back'))
    if main_menu_btn:
        nav_buttons.append(InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data))
    nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons]) if nav_buttons else None
    nav_msg = await m.answer(nav_text, reply_markup=nav_markup)
    msg = await m.answer("Прикрепите фото (можно до 3-х, или пропустите):")
    last_bot_messages[chat_id] = [nav_msg.message_id, msg.message_id]
    await state.set_state(CatalogAddForm.photo)

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {getattr(cb.message.chat, 'id', None)} | "
        f"user_id: {getattr(cb.from_user, 'id', None)} | "
        f"msg_ids: {last_bot_messages.get(cb.message.chat.id) if 'last_bot_messages' in globals() else 'n/a'} | "
        f"keyboard_rows: {len(markup.inline_keyboard) if 'markup' in locals() else 'n/a'}"
    )



@router.message(CatalogAddForm.photo)
async def get_catalog_photo(m: Message, state: FSMContext) -> None:
    """
    Сохраняем информацию о фото и просим описание. Добавляем панель
    возврата.
    """
    chat_id = m.chat.id
    await clear_bot_messages(chat_id, m.bot)
    await state.update_data(photo=m.text)
    nav_text = await get_text('return_to_menu', 'ru') or "Возврат"
    base_back_btn = await get_common_menu_button('back')
    main_menu_btn = await get_common_menu_button('main_menu')
    nav_buttons = []
    if base_back_btn:
        nav_buttons.append(InlineKeyboardButton(text=base_back_btn.text, callback_data='catalog_city_back'))
    if main_menu_btn:
        nav_buttons.append(InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data))
    nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons]) if nav_buttons else None
    nav_msg = await m.answer(nav_text, reply_markup=nav_markup)
    msg = await m.answer("Введите описание ваших умений или информации о группе/студии:")
    last_bot_messages[chat_id] = [nav_msg.message_id, msg.message_id]
    await state.set_state(CatalogAddForm.description)

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {getattr(cb.message.chat, 'id', None)} | "
        f"user_id: {getattr(cb.from_user, 'id', None)} | "
        f"msg_ids: {last_bot_messages.get(cb.message.chat.id) if 'last_bot_messages' in globals() else 'n/a'} | "
        f"keyboard_rows: {len(markup.inline_keyboard) if 'markup' in locals() else 'n/a'}"
    )



@router.message(CatalogAddForm.description)
async def get_catalog_description(m: Message, state: FSMContext) -> None:
    """
    Сохраняем описание и просим информацию о репетиционной базе. Добавляем
    панель возврата.
    """
    chat_id = m.chat.id
    await clear_bot_messages(chat_id, m.bot)
    await state.update_data(description=m.text)
    nav_text = await get_text('return_to_menu', 'ru') or "Возврат"
    base_back_btn = await get_common_menu_button('back')
    main_menu_btn = await get_common_menu_button('main_menu')
    nav_buttons = []
    if base_back_btn:
        nav_buttons.append(InlineKeyboardButton(text=base_back_btn.text, callback_data='catalog_city_back'))
    if main_menu_btn:
        nav_buttons.append(InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data))
    nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons]) if nav_buttons else None
    nav_msg = await m.answer(nav_text, reply_markup=nav_markup)
    msg = await m.answer("Введите информацию о реп. базе (ссылка, если есть):")
    last_bot_messages[chat_id] = [nav_msg.message_id, msg.message_id]
    await state.set_state(CatalogAddForm.repo)

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {getattr(cb.message.chat, 'id', None)} | "
        f"user_id: {getattr(cb.from_user, 'id', None)} | "
        f"msg_ids: {last_bot_messages.get(cb.message.chat.id) if 'last_bot_messages' in globals() else 'n/a'} | "
        f"keyboard_rows: {len(markup.inline_keyboard) if 'markup' in locals() else 'n/a'}"
    )



@router.message(CatalogAddForm.repo)
async def get_catalog_repo(m: Message, state: FSMContext) -> None:
    """
    Сохраняем информацию о репетиционной базе, показываем сводку и
    предлагаем подтвердить. Также рисуем панель возврата наверху.
    """
    chat_id = m.chat.id
    await clear_bot_messages(chat_id, m.bot)
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
    # Навигационная панель: назад к выбору города
    nav_text = await get_text('return_to_menu', 'ru') or "Возврат"
    base_back_btn = await get_common_menu_button('back')
    main_menu_btn = await get_common_menu_button('main_menu')
    nav_buttons = []
    if base_back_btn:
        nav_buttons.append(InlineKeyboardButton(text=base_back_btn.text, callback_data='catalog_city_back'))
    if main_menu_btn:
        nav_buttons.append(InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data))
    nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons]) if nav_buttons else None
    nav_msg = await m.answer(nav_text, reply_markup=nav_markup)
    # Сообщение с подтверждением
    confirm_msg = await m.answer(
        f"Проверьте введённые данные:\n\n{summary}\n\nПодтвердите отправку.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Да", callback_data="catalog_confirm:yes"),
             InlineKeyboardButton(text="Нет", callback_data="catalog_confirm:no")]
        ]),
        parse_mode="HTML"
    )
    last_bot_messages[chat_id] = [nav_msg.message_id, confirm_msg.message_id]
    await state.set_state(CatalogAddForm.confirm)

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {getattr(cb.message.chat, 'id', None)} | "
        f"user_id: {getattr(cb.from_user, 'id', None)} | "
        f"msg_ids: {last_bot_messages.get(cb.message.chat.id) if 'last_bot_messages' in globals() else 'n/a'} | "
        f"keyboard_rows: {len(markup.inline_keyboard) if 'markup' in locals() else 'n/a'}"
    )



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

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {getattr(cb.message.chat, 'id', None)} | "
        f"user_id: {getattr(cb.from_user, 'id', None)} | "
        f"msg_ids: {last_bot_messages.get(cb.message.chat.id) if 'last_bot_messages' in globals() else 'n/a'} | "
        f"keyboard_rows: {len(markup.inline_keyboard) if 'markup' in locals() else 'n/a'}"
    )
