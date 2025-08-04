from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

from app.keyboards import (
    catalog_inline_initial,
    catalog_city_inline,
    get_common_menu_button,
    catalog_search_results_keyboard,
)
from app.routers.utils import (
    last_bot_messages,
    clear_bot_messages,
    safe_edit_or_send,
    city_by_slug,
    children_of,
)
from app.models import Category, Item
from sqlalchemy import select
from app.database import SessionLocal
from app.texts import get_text
import inspect

router = Router(name="catalog_view")
router = Router(name="catalog_search")

class CatalogSearchForm(StatesGroup):
    query = State()

# Кнопка поиска в каталоге
@router.callback_query(F.data == "catalog_search")
async def catalog_search_start(cb: CallbackQuery, state: FSMContext):
    """
    Запустить режим поиска по каталогу. Вместо редактирования предыдущего сообщения,
    как было раньше, мы очищаем интерфейс, показываем панель возврата
    (кнопки «Назад» и «Главное меню») и затем отправляем приглашение ввести
    поисковый запрос. Заголовок панели возврата берётся из таблицы BotText
    с кодом ``return_to_menu`` (по умолчанию — «Возврат»).
    """
    chat_id = cb.message.chat.id

    # Очистить старые сообщения (в т.ч. предыдущие навигационные панели)
    await clear_bot_messages(chat_id, cb.bot)

    # Формируем навигационную панель
    nav_text = await get_text('return_to_menu', 'ru') or "Возврат"
    back_btn = await get_common_menu_button('catalog_back')
    main_menu_btn = await get_common_menu_button('main_menu')
    nav_buttons = []
    # Кнопка «Назад» ведёт к выбору города каталога
    if back_btn:
        # используем текст из базы, но перезаписываем callback_data на существующий хендлер
        nav_buttons.append(InlineKeyboardButton(text=back_btn.text, callback_data=back_btn.callback_data))
    if main_menu_btn:
        nav_buttons.append(InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data))
    nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons]) if nav_buttons else None
    nav_msg = await cb.bot.send_message(chat_id, nav_text, reply_markup=nav_markup)

    # Текст приглашения к поиску. Если в базе нет отдельного текста, используем дефолт.
    prompt = "Введите ключевое слово для поиска в каталоге:"
    query_msg = await cb.bot.send_message(chat_id, prompt)

    # Сохраняем id сообщений для последующего удаления
    last_bot_messages[chat_id] = [nav_msg.message_id, query_msg.message_id]

    # Переключаем состояние на ожидание запроса
    await state.set_state(CatalogSearchForm.query)
    await cb.answer()
    print(f">>> {inspect.currentframe().f_code.co_name}")

# Поисковый ввод
@router.message(CatalogSearchForm.query)
async def catalog_search_query(m: Message, state: FSMContext):
    """
    Обработка введённого пользователем поискового запроса. Перед выводом результатов
    очищаем интерфейс и снова рисуем панель возврата. Кнопка «Назад» в панели
    переводит пользователя обратно к вводу поискового запроса (callback_data
    ``catalog_search``), а кнопка «Главное меню» возвращает в главное меню.
    """
    chat_id = m.chat.id
    query = m.text.strip().lower() if m.text else ""

    # Очищаем предыдущие сообщения, включая панель возврата и приглашение к поиску
    await clear_bot_messages(chat_id, m.bot)

    # Формируем новую навигационную панель для результатов
    nav_text = await get_text('return_to_menu', 'ru') or "Возврат"
    # Получаем базовую кнопку «Назад» (текст) и назначаем callback_data на перезапуск поиска
    base_back_btn = await get_common_menu_button('back')
    nav_buttons = []
    if base_back_btn:
        nav_buttons.append(InlineKeyboardButton(text=base_back_btn.text, callback_data='catalog_search'))
    main_menu_btn = await get_common_menu_button('main_menu')
    if main_menu_btn:
        nav_buttons.append(InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data))
    nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons]) if nav_buttons else None
    nav_msg = await m.bot.send_message(chat_id, nav_text, reply_markup=nav_markup)

    # Выполняем поиск
    async with SessionLocal() as session:
        items = (await session.execute(
            select(Item)
            .where(
                (Item.title.ilike(f"%{query}%")) | (Item.descr.ilike(f"%{query}%")),
                Item.is_approved.is_(True)
            )
            .order_by(Item.created_at.desc())
        )).scalars().all()

    # Формируем текст результата
    if not items:
        result_text = "Ничего не найдено по вашему запросу."
    else:
        parts = []
        for i in items[:10]:  # выводим не больше 10 анкет
            parts.append(f"• <b>{i.title}</b>\n{i.descr or ''}\n<code>{i.contact}</code>")
        result_text = "Результаты поиска:\n\n" + "\n\n".join(parts)

    # Отправляем сообщение с результатами. Сохраняем клавиатуру для повторного поиска и выхода в меню.
    result_msg = await m.bot.send_message(
        chat_id,
        result_text,
        parse_mode="HTML",
        reply_markup=catalog_search_results_keyboard()
    )

    # Запоминаем отправленные сообщения для последующего удаления
    last_bot_messages[chat_id] = [nav_msg.message_id, result_msg.message_id]

    # Сбрасываем состояние
    await state.clear()
    print(f">>> {inspect.currentframe().f_code.co_name}")


@router.callback_query(F.data == "go_catalog")
async def go_catalog(cb: CallbackQuery, state: FSMContext) -> None:
    """
    Переход в раздел каталога из главного меню. Очищает предыдущие
    сообщения, отображает панель возврата и затем показывает список
    действий в каталоге (поиск, выбор города, подать заявку).
    """
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    # Панель возврата: на верхнем уровне кнопка «Назад» ведёт в главное меню
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
    # Сообщение с вариантами каталога
    markup = await catalog_inline_initial()
    text = await get_text("catalog_choose_city", "ru") or "🏙 Каталог – выберите город:"
    msg = await cb.bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
    last_bot_messages[chat_id] = [msg.message_id]
    await cb.answer()
    print(f">>> {inspect.currentframe().f_code.co_name}")


# ====== Каталог: вывод категорий и анкет с разделяющей кнопкой ======
@router.callback_query(F.data.startswith("citysel:") | F.data.startswith("cat:"))
async def catalog_city_handler(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    data = cb.data.split(":")
    # Определяем город и категорию (если есть)
    if data[0] == "citysel":
        city_slug = data[1]
        cat_slug = None
    else:
        _, city_slug, cat_slug = data

    city = await city_by_slug(city_slug)

    async with SessionLocal() as session:
        if not cat_slug:
            parent = (await session.execute(
                select(Category).where(Category.slug == "profile")
            )).scalar_one_or_none()
            if not parent:
                categories = []
            else:
                categories = (await session.execute(
                    select(Category).where(Category.parent_id == parent.id)
                )).scalars().all()
            path = f"{city.name}"
            parent_cat = None
            items = []
        else:
            parent = (await session.execute(
                select(Category).where(Category.slug == cat_slug)
            )).scalar_one()
            categories = (await session.execute(
                select(Category).where(Category.parent_id == parent.id)
            )).scalars().all()
            # Строим путь для хедера (цепочку категорий)
            names = [parent.name]
            cur = parent
            while cur.parent_id:
                p = (await session.execute(
                    select(Category).where(Category.id == cur.parent_id)
                )).scalar_one()
                names.append(p.name)
                cur = p
            path = f"{city.name} → " + " → ".join(reversed(names))
            parent_cat = parent
            # --- ВСЕГДА получаем анкеты для текущей категории ---
            items = (await session.execute(
                select(Item)
                .where(Item.city_id == city.id, Item.category_id == parent.id, Item.is_approved.is_(True))
                .order_by(Item.created_at.desc())
            )).scalars().all()

    await clear_bot_messages(chat_id, cb.bot)  # Удаляем все старое

    buttons = []
    # 1. Сначала подкатегории (если есть)
    if categories:
        for child in categories:
            buttons.append([InlineKeyboardButton(
                text=child.name,
                callback_data=f"cat:{city_slug}:{child.slug}"
            )])
        # 2. Разделяющая кнопка — если есть анкеты!
        if items:
            buttons.append([InlineKeyboardButton(text="— Анкеты в категории —", callback_data="stub")])
    # 3. Затем анкеты (если есть)
    if items:
        for i in items:
            buttons.append([InlineKeyboardButton(
                text=i.title,
                callback_data=f"profile:{i.id}:{city_slug}:{cat_slug}"
            )])

    # Навигация (Назад / Главное меню)
    if cat_slug:
        async with SessionLocal() as session:
            cat_obj = (await session.execute(
                select(Category).where(Category.slug == cat_slug)
            )).scalar_one_or_none()
            if cat_obj and cat_obj.parent_id:
                parent_of_parent = (await session.execute(
                    select(Category).where(Category.id == cat_obj.parent_id)
                )).scalar_one()
                if parent_of_parent.slug == "profile":
                    back_callback = f"citysel:{city_slug}"
                else:
                    back_callback = f"cat:{city_slug}:{parent_of_parent.slug}"
            else:
                back_callback = f"citysel:{city_slug}"
    else:
        back_callback = "go_catalog"

    back_btn = await get_common_menu_button('back')
    main_menu_btn = await get_common_menu_button('main_menu')
    nav_row = []
    if back_btn:
        nav_row.append(InlineKeyboardButton(text=back_btn.text, callback_data=back_callback))
    if main_menu_btn:
        nav_row.append(InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data))
    if nav_row:
        buttons.append(nav_row)

    header = f"<b>Каталог → {path}</b>"
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    if categories or (not categories and items):
        msg = await cb.bot.send_message(chat_id, header, reply_markup=markup, parse_mode="HTML")
    else:
        msg = await cb.bot.send_message(chat_id, header + "\n\nПока нет анкет.", reply_markup=markup, parse_mode="HTML")

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


# @router.callback_query(F.data.startswith("cat:"))
# async def cat_handler(cb: CallbackQuery) -> None:
#     """
#     Обработка выбора категории или подкатегории. Перед выводом нового
#     содержимого очищаем старые сообщения, затем показываем панель
#     возврата, после чего отправляем сообщение с заголовком и кнопками.
#     Кнопка «Назад» в панели ведёт к родительской категории или выбору
#     города, в зависимости от уровня вложенности.
#     """
#     chat_id = cb.message.chat.id
#     await clear_bot_messages(chat_id, cb.bot)
#     _, city_slug, cat_slug = cb.data.split(":", 2)
#     city = await city_by_slug(city_slug)
#     async with SessionLocal() as session:
#         cat = (await session.execute(
#             select(Category).where(Category.slug == cat_slug)
#         )).scalar_one()
#         children = (await session.execute(
#             select(Category).where(Category.parent_id == cat.id)
#         )).scalars().all()

#     # Собираем цепочку категорий для заголовка
#     names = [cat.name]
#     parent_cat_slug = None
#     cur = cat
#     while cur.parent_id:
#         async with SessionLocal() as session:
#             p = (await session.execute(
#                 select(Category).where(Category.id == cur.parent_id)
#             )).scalar_one()
#         names.append(p.name)
#         if not parent_cat_slug:
#             parent_cat_slug = p.slug
#         cur = p
#     path = " → ".join(reversed(names))
#     header = f"<b>Каталог → {city.name} → {path}</b>"

#     # Формируем кнопки содержимого
#     buttons = []
#     if children:
#         for child in children:
#             buttons.append([InlineKeyboardButton(
#                 text=child.name,
#                 callback_data=f"cat:{city_slug}:{child.slug}"
#             )])
#     else:
#         # Листовая категория: показать анкеты-профили (Item)
#         async with SessionLocal() as session:
#             items = (await session.execute(
#                 select(Item)
#                 .where(Item.city_id == city.id, Item.category_id == cat.id, Item.is_approved.is_(True))
#                 .order_by(Item.created_at.desc())
#             )).scalars().all()
#         if items:
#             for i in items:
#                 buttons.append([InlineKeyboardButton(
#                     text=i.title,
#                     callback_data=f"profile:{i.id}:{city_slug}:{cat.slug}"
#                 )])

#     # Определяем куда ведёт кнопка «Назад»
#     if parent_cat_slug:
#         back_callback = f"cat:{city_slug}:{parent_cat_slug}"
#     else:
#         back_callback = f"citysel:{city_slug}"

#     # Формируем панель возврата
#     nav_text = await get_text('return_to_menu', 'ru') or "Возврат"
#     base_back_btn = await get_common_menu_button('back')
#     nav_buttons = []
#     if base_back_btn:
#         nav_buttons.append(InlineKeyboardButton(text=base_back_btn.text, callback_data=back_callback))
#     main_menu_btn = await get_common_menu_button('main_menu')
#     if main_menu_btn:
#         nav_buttons.append(InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data))
#     nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons]) if nav_buttons else None
#     nav_msg = await cb.bot.send_message(chat_id, nav_text, reply_markup=nav_markup)

#     # Добавляем навигацию внизу списка содержимого
#     back_btn = await get_common_menu_button('back')
#     if back_btn:
#         buttons.append([InlineKeyboardButton(text=back_btn.text, callback_data=back_callback)])
#     main_menu_bottom = await get_common_menu_button('main_menu')
#     if main_menu_bottom:
#         buttons.append([InlineKeyboardButton(text=main_menu_bottom.text, callback_data=main_menu_bottom.callback_data)])
#     markup = InlineKeyboardMarkup(inline_keyboard=buttons)

#     # Отправляем основное сообщение
#     if children or (not children and items):
#         msg = await cb.bot.send_message(chat_id, header, reply_markup=markup, parse_mode="HTML")
#     else:
#         msg = await cb.bot.send_message(chat_id, header + "\n\nПока нет анкет.", reply_markup=markup, parse_mode="HTML")

#     # Сохраняем id сообщений для последующего удаления
#     last_bot_messages[chat_id] = [nav_msg.message_id, msg.message_id]
#     await cb.answer()
#     print(f">>> {inspect.currentframe().f_code.co_name}")


@router.callback_query(F.data == "catalog:back")
async def catalog_back(cb: CallbackQuery) -> None:
    """
    Возврат к корню каталога. Очищает все сообщения, отображает панель
    возврата (в данном случае обе кнопки ведут в главное меню) и выводит
    начальное сообщение каталога.
    """
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    # Панель возврата: обе кнопки отправляют пользователя в главное меню
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
    text = await get_text("catalog_choose_city", "ru") or "🏙 Каталог – выберите город:"
    msg = await cb.bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
    last_bot_messages[chat_id] = [msg.message_id]
    await cb.answer()
    print(f">>> {inspect.currentframe().f_code.co_name}")

