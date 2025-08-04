from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from app.routers.utils import clear_bot_messages, last_bot_messages
from sqlalchemy import select
from app.database import SessionLocal
from app.models import Category
from app.states import AdminCategoryStates
from aiogram.types import ReplyKeyboardRemove
import inspect

router = Router()

ADMIN_IDS = [519335258]  # замените на свой Telegram ID

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def get_admin_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗂 Редактировать категории", callback_data="admin:edit_categories")
            ],
            [
                InlineKeyboardButton(text="◀️ Главное меню", callback_data="main_menu")
            ]
        ]
    )

# ====== Вход в админ-панель ======
@router.message(Command("admin"))
async def admin_panel_entry(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("У вас нет доступа к админ-панели.")
        return

    await clear_bot_messages(message.chat.id, message.bot)

    menu = get_admin_menu()
    msg = await message.answer(
        "🔒 <b>Админ-панель</b>\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=menu
    )
    last_bot_messages[message.chat.id] = [msg.message_id]
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"m.text: {getattr(message, 'text', None)} | "
        f"chat_id: {getattr(message.chat, 'id', None)} | "
        f"user_id: {getattr(message.from_user, 'id', None)} | "
        f"msg_id: {getattr(msg, 'message_id', None)} | "
        f"keyboard_rows: {len(menu.inline_keyboard) if menu else 'n/a'}"
    )

# ====== Админ: редактирование категорий ======
@router.callback_query(F.data == "admin:edit_categories")
async def admin_edit_categories_cb(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    await clear_bot_messages(cb.message.chat.id, cb.bot)
    
    async with SessionLocal() as session:
        cats = (await session.execute(
            select(Category).where(Category.parent_id == None)
        )).scalars().all()

    keyboard = []
    for cat in cats:
        keyboard.append([
            InlineKeyboardButton(text=cat.name, callback_data=f"admin:edit_category:{cat.id}"),
        ])
    keyboard.append([
        InlineKeyboardButton(text="◀️ Назад", callback_data="admin")
    ])
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    msg = await cb.message.answer(
        "🗂 <b>Список категорий</b>\nНажмите на категорию для редактирования.",
        reply_markup=markup,
        parse_mode="HTML"
    )
    last_bot_messages[cb.message.chat.id] = [msg.message_id]
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {getattr(cb.message.chat, 'id', None)} | "
        f"user_id: {getattr(cb.from_user, 'id', None)} | "
        f"msg_id: {getattr(msg, 'message_id', None)}"
    )

# ====== Админ: Главное меню (возврат по кнопке "Назад") ======
@router.callback_query(F.data == "admin")
async def admin_main_menu_cb(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    await clear_bot_messages(cb.message.chat.id, cb.bot)

    menu = get_admin_menu()
    msg = await cb.message.answer(
        "🔒 <b>Админ-панель</b>\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=menu
    )
    last_bot_messages[cb.message.chat.id] = [msg.message_id]
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {getattr(cb.message.chat, 'id', None)} | "
        f"user_id: {getattr(cb.from_user, 'id', None)} | "
        f"msg_id: {getattr(msg, 'message_id', None)}"
    )

# ====== Админ: многоуровневое меню подкатегорий ======
@router.callback_query(F.data.startswith("admin:edit_category:"))
async def admin_edit_subcategories_cb(cb: CallbackQuery, state: FSMContext = None):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    await clear_bot_messages(cb.message.chat.id, cb.bot)
    parent_id = int(cb.data.split(":")[-1])
    ROOT_CATEGORY_IDS = {30, 80}

    async with SessionLocal() as session:
        parent_category = (await session.execute(
            select(Category).where(Category.id == parent_id)
        )).scalar_one()
        subcategories = (await session.execute(
            select(Category).where(Category.parent_id == parent_id)
        )).scalars().all()

    keyboard = []
    
    for subcat in subcategories:
        keyboard.append([
            InlineKeyboardButton(
                text=f"📁 {subcat.name}",
                callback_data=f"admin:edit_category:{subcat.id}"
            )
        ])
    keyboard.append([
        InlineKeyboardButton(
            text="✚ Добавить подкатегорию",
            callback_data=f"admin:add_category:{parent_id}"
        )
    ])
    
    if parent_category.id not in ROOT_CATEGORY_IDS:
        keyboard.append([
            InlineKeyboardButton(
                text="———————————————",
                callback_data="noop"
            )
        ])
        keyboard.append([
            InlineKeyboardButton(
                text=f"✏️ Редактировать {parent_category.name}",
                callback_data=f"admin:rename_category:{parent_category.id}"
            )
        ])
        if not subcategories:  # <--- Показывать "Удалить" ТОЛЬКО если нет подкатегорий!
            keyboard.append([
                InlineKeyboardButton(
                    text=f"🗑️ Удалить {parent_category.name}",
                    callback_data=f"admin:delete_category:{parent_category.id}"
                )
            ])
    keyboard.append([
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=(
                f"admin:edit_category:{parent_category.parent_id}"
                if parent_category.parent_id is not None else
                "admin:edit_categories"
            )
        )
    ])
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    msg = await cb.message.answer(
        f"📂Категория  -  <b>{parent_category.name}</b>\n\nВыберите подкатегорию или действие.",
        reply_markup=markup,
        parse_mode="HTML"
    )
    last_bot_messages[cb.message.chat.id] = [msg.message_id]
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {getattr(cb.message.chat, 'id', None)} | "
        f"user_id: {getattr(cb.from_user, 'id', None)} | "
        f"msg_id: {getattr(msg, 'message_id', None)}"
    )

# ====== Админ: запрос названия новой подкатегории ======
@router.callback_query(F.data.startswith("admin:add_category:"))
async def admin_add_category_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    await clear_bot_messages(cb.message.chat.id, cb.bot)
    parent_id = int(cb.data.split(":")[-1])
    await state.update_data(parent_id=parent_id)

    menu = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:edit_category:{parent_id}"),
            InlineKeyboardButton(text="☰ Главное меню", callback_data="admin"),
        ]
    ])
    menu_msg = await cb.message.answer(
        "Возврат",
        reply_markup=menu
    )
    msg = await cb.message.answer(
        "✏️ Введите <b>название</b> новой категории:",
        parse_mode="HTML"
    )
    last_bot_messages[cb.message.chat.id] = [menu_msg.message_id, msg.message_id]
    await state.set_state(AdminCategoryStates.waiting_for_new_category_name)
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {getattr(cb.message.chat, 'id', None)} | "
        f"user_id: {getattr(cb.from_user, 'id', None)} | "
        f"msg_id: {getattr(msg, 'message_id', None)}"
    )

# ====== Админ: Ввод имени новой категории ======
@router.message(AdminCategoryStates.waiting_for_new_category_name)
async def admin_add_category_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа к этой функции.")
        return

    if message.text == "⬅️ Назад":
        data = await state.get_data()
        parent_id = data.get("parent_id")
        await state.clear()
        from aiogram.types import CallbackQuery
        fake_cb = CallbackQuery(
            id="fake",
            from_user=message.from_user,
            chat_instance="",
            message=message,
            data=f"admin:edit_category:{parent_id}"
        )
        await admin_edit_subcategories_cb(fake_cb, state)
        return

    await clear_bot_messages(message.chat.id, message.bot)
    data = await state.get_data()
    parent_id = data.get("parent_id")
    category_name = message.text.strip()
    await state.update_data(category_name=category_name)

    menu = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:edit_category:{parent_id}"),
            InlineKeyboardButton(text="☰ Главное меню", callback_data="admin"),
        ]
    ])
    menu_msg = await message.answer("Возврат", reply_markup=menu)
    msg = await message.answer("✏️ Введите <b>slug</b> для категории ...")
    last_bot_messages[message.chat.id] = [menu_msg.message_id, msg.message_id]
    await state.set_state(AdminCategoryStates.waiting_for_new_category_slug)
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"step: waiting_for_new_category_name | "
        f"chat_id: {getattr(message.chat, 'id', None)} | "
        f"user_id: {getattr(message.from_user, 'id', None)} | "
        f"parent_id: {parent_id} | "
        f"category_name: {category_name} | "
        f"msg_id: {getattr(msg, 'message_id', None)}"
    )

# ====== Админ: Ввод slug для новой категории ======
@router.message(AdminCategoryStates.waiting_for_new_category_slug)
async def admin_add_category_slug(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа к этой функции.")
        return

    if message.text == "⬅️ Назад":
        data = await state.get_data()
        parent_id = data.get("parent_id")
        await state.clear()
        from aiogram.types import CallbackQuery
        fake_cb = CallbackQuery(
            id="fake",
            from_user=message.from_user,
            chat_instance="",
            message=message,
            data=f"admin:edit_category:{parent_id}"
        )
        await admin_edit_subcategories_cb(fake_cb, state)
        return

    await clear_bot_messages(message.chat.id, message.bot)
    data = await state.get_data()
    parent_id = data.get("parent_id")
    category_name = data.get("category_name")
    slug = message.text.strip().lower()

    import re
    if not re.fullmatch(r'[a-z0-9_\-]+', slug):
        from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
        markup = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="⬅️ Назад")]],
            resize_keyboard=True
        )
        msg = await message.answer(
            "❗️Slug должен содержать только латинские буквы, цифры, дефис или _ (нижнее подчёркивание). Введите снова:",
            parse_mode="HTML",
            reply_markup=markup
        )
        last_bot_messages[message.chat.id] = [msg.message_id]
        print(
            f"FUNC: {inspect.currentframe().f_code.co_name} | "
            f"step: slug_validate_fail | "
            f"chat_id: {getattr(message.chat, 'id', None)} | "
            f"user_id: {getattr(message.from_user, 'id', None)} | "
            f"parent_id: {parent_id} | "
            f"category_name: {category_name} | "
            f"slug: {slug} | "
            f"msg_id: {getattr(msg, 'message_id', None)}"
        )
        return

    async with SessionLocal() as session:
        exists = (await session.execute(
            select(Category).where(
                Category.parent_id == parent_id,
                Category.slug == slug
            )
        )).first()
        if exists:
            msg = await message.answer(
                f"❗️Категория с таким slug уже существует на этом уровне. Введите другой slug:",
                parse_mode="HTML",
                reply_markup=ReplyKeyboardRemove()
            )
            last_bot_messages[message.chat.id] = [msg.message_id]
            print(
                f"FUNC: {inspect.currentframe().f_code.co_name} | "
                f"step: slug_duplicate | "
                f"chat_id: {getattr(message.chat, 'id', None)} | "
                f"user_id: {getattr(message.from_user, 'id', None)} | "
                f"parent_id: {parent_id} | "
                f"category_name: {category_name} | "
                f"slug: {slug} | "
                f"msg_id: {getattr(msg, 'message_id', None)}"
            )
            return

        new_category = Category(slug=slug, name=category_name, parent_id=parent_id)
        session.add(new_category)
        await session.commit()

    await state.clear()
    await admin_send_success(message, f"✅ Категория <b>{category_name}</b> ({slug}) добавлена.", parent_id)
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"step: category_added | "
        f"chat_id: {getattr(message.chat, 'id', None)} | "
        f"user_id: {getattr(message.from_user, 'id', None)} | "
        f"parent_id: {parent_id} | "
        f"category_name: {category_name} | "
        f"slug: {slug}"
    )
    return

# ====== Админ: Переименование категории (запрос) ======
@router.callback_query(F.data.startswith("admin:rename_category:"))
async def admin_rename_category_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    await clear_bot_messages(cb.message.chat.id, cb.bot)
    cat_id = int(cb.data.split(":")[-1])
    async with SessionLocal() as session:
        category = (await session.execute(
            select(Category).where(Category.id == cat_id)
        )).scalar_one()

    await state.update_data(rename_cat_id=cat_id, old_name=category.name, old_slug=category.slug)

    menu = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:edit_category:{category.parent_id}"),
            InlineKeyboardButton(text="☰ Главное меню", callback_data="admin"),
        ]
    ])
    menu_msg = await cb.message.answer("Возврат", reply_markup=menu)
    msg = await cb.message.answer(
        f"✏️ Переименование категории:\n<b>{category.name}</b>\n\nВведите новое название:",
        parse_mode="HTML"
    )
    last_bot_messages[cb.message.chat.id] = [menu_msg.message_id, msg.message_id]
    await state.set_state(AdminCategoryStates.renaming_category_name)
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {getattr(cb.message.chat, 'id', None)} | "
        f"user_id: {getattr(cb.from_user, 'id', None)} | "
        f"cat_id: {cat_id} | "
        f"old_name: {getattr(category, 'name', None)} | "
        f"old_slug: {getattr(category, 'slug', None)} | "
        f"menu_msg_id: {getattr(menu_msg, 'message_id', None)} | "
        f"msg_id: {getattr(msg, 'message_id', None)}"
    )

# ====== Админ: Ввод нового названия (переименование) ======
@router.message(AdminCategoryStates.renaming_category_name)
async def admin_rename_category_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("У вас нет доступа к этой функции.")
        return

    if message.text == "⬅️ Назад":
        data = await state.get_data()
        cat_id = data.get("rename_cat_id")
        async with SessionLocal() as session:
            category = (await session.execute(
                select(Category).where(Category.id == cat_id)
            )).scalar_one()
        await state.clear()
        from aiogram.types import CallbackQuery
        fake_cb = CallbackQuery(
            id="fake",
            from_user=message.from_user,
            chat_instance="",
            message=message,
            data=f"admin:edit_category:{category.parent_id}"
        )
        await admin_edit_subcategories_cb(fake_cb, state)
        return

    await clear_bot_messages(message.chat.id, message.bot)
    data = await state.get_data()
    cat_id = data.get("rename_cat_id")
    old_slug = data.get("old_slug")
    new_name = message.text.strip()
    await state.update_data(new_name=new_name)

    menu = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:edit_category:{cat_id}"),
            InlineKeyboardButton(text="☰ Главное меню", callback_data="admin"),
        ]
    ])
    menu_msg = await message.answer("Возврат", reply_markup=menu)

    slug_menu = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"⬇️ Оставить прежний slug: {old_slug}",
                callback_data=f"admin:keep_slug:{cat_id}"
            )
        ]
    ])
    msg = await message.answer(
        f"✏️ Введите <b>slug</b> для категории (или нажмите кнопку ниже для старого slug):\n"
        f"Текущий slug: <code>{old_slug}</code>",
        parse_mode="HTML",
        reply_markup=slug_menu
    )
    last_bot_messages[message.chat.id] = [menu_msg.message_id, msg.message_id]
    await state.set_state(AdminCategoryStates.renaming_category_slug)
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"step: renaming_category_name | "
        f"chat_id: {getattr(message.chat, 'id', None)} | "
        f"user_id: {getattr(message.from_user, 'id', None)} | "
        f"cat_id: {cat_id} | "
        f"old_slug: {old_slug} | "
        f"new_name: {new_name} | "
        f"msg_id: {getattr(msg, 'message_id', None)}"
    )

# ====== Админ: Ввод нового slug (или оставить старый через кнопку) ======
@router.message(AdminCategoryStates.renaming_category_slug)
async def admin_rename_category_slug(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа к этой функции.")
        return

    if message.text == "⬅️ Назад":
        data = await state.get_data()
        cat_id = data.get("rename_cat_id")
        async with SessionLocal() as session:
            category = (await session.execute(
                select(Category).where(Category.id == cat_id)
            )).scalar_one()
        await state.clear()
        from aiogram.types import CallbackQuery
        fake_cb = CallbackQuery(
            id="fake",
            from_user=message.from_user,
            chat_instance="",
            message=message,
            data=f"admin:edit_category:{category.parent_id}"
        )
        await admin_edit_subcategories_cb(fake_cb, state)
        return

    await clear_bot_messages(message.chat.id, message.bot)
    data = await state.get_data()
    cat_id = data.get("rename_cat_id")
    new_name = data.get("new_name")
    old_slug = data.get("old_slug")

    if hasattr(message, "data") and message.data == f"admin:keep_slug:{cat_id}":
        slug = old_slug
    else:
        slug = message.text.strip().lower()

    import re
    if not slug or not re.fullmatch(r'[a-z0-9_\-]+', slug):
        menu = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:edit_category:{cat_id}"),
                InlineKeyboardButton(text="☰ Главное меню", callback_data="admin")
            ],
            [
                InlineKeyboardButton(text=f"⬇️ Оставить прежний slug: {old_slug}",
                                     callback_data=f"admin:keep_slug:{cat_id}")
            ]
        ])
        msg = await message.answer(
            "❗️Slug должен содержать только латинские буквы, цифры, дефис или _ (нижнее подчёркивание).\n"
            "Введите новый slug или нажмите кнопку ниже, чтобы оставить старый (⬇️):",
            reply_markup=menu,
            parse_mode="HTML"
        )
        last_bot_messages[chat_id] = [menu_msg.message_id, msg.message_id]
        print(
            f"FUNC: {inspect.currentframe().f_code.co_name} | "
            f"step: renaming_category_slug_fail | "
            f"chat_id: {getattr(message.chat, 'id', None)} | "
            f"user_id: {getattr(message.from_user, 'id', None)} | "
            f"cat_id: {cat_id} | "
            f"old_slug: {old_slug} | "
            f"new_name: {new_name} | "
            f"input_slug: {slug} | "
            f"msg_id: {getattr(msg, 'message_id', None)}"
        )
        return

    async with SessionLocal() as session:
        category = (await session.execute(
            select(Category).where(Category.id == cat_id)
        )).scalar_one()
        parent_id = category.parent_id
        exists = (await session.execute(
            select(Category).where(
                Category.parent_id == parent_id,
                Category.slug == slug,
                Category.id != cat_id
            )
        )).first()
        if exists:
            menu = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:edit_category:{cat_id}"),
                    InlineKeyboardButton(text="☰ Главное меню", callback_data="admin")
                ],
                [
                    InlineKeyboardButton(text=f"⬇️ Оставить прежний slug: {old_slug}",
                                         callback_data=f"admin:keep_slug:{cat_id}")
                ]
            ])
            msg = await message.answer(
                f"❗️Категория с таким slug уже существует на этом уровне. Введите другой slug или нажмите кнопку ниже, чтобы оставить прежний (⬇️):",
                reply_markup=menu,
                parse_mode="HTML"
            )
            last_bot_messages[chat_id] = [menu_msg.message_id, msg.message_id]
            print(
                f"FUNC: {inspect.currentframe().f_code.co_name} | "
                f"step: renaming_category_slug_duplicate | "
                f"chat_id: {getattr(message.chat, 'id', None)} | "
                f"user_id: {getattr(message.from_user, 'id', None)} | "
                f"cat_id: {cat_id} | "
                f"old_slug: {old_slug} | "
                f"new_name: {new_name} | "
                f"input_slug: {slug} | "
                f"msg_id: {getattr(msg, 'message_id', None)}"
            )
            return

        category.name = new_name
        category.slug = slug
        session.add(category)
        await session.commit()

    await state.clear()
    await admin_send_success(message, f"✅ Категория <b>{new_name}</b> ({slug}) успешно переименована.", parent_id)
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"step: renaming_category_slug_done | "
        f"chat_id: {getattr(message.chat, 'id', None)} | "
        f"user_id: {getattr(message.from_user, 'id', None)} | "
        f"cat_id: {cat_id} | "
        f"old_slug: {old_slug} | "
        f"new_name: {new_name} | "
        f"final_slug: {slug}"
    )
    return

# ====== Админ: обработка инлайн-кнопки "оставить slug" ======
@router.callback_query(F.data.startswith("admin:keep_slug:"))
async def admin_keep_slug_cb(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    await clear_bot_messages(cb.message.chat.id, cb.bot)

    data = await state.get_data()
    cat_id = int(cb.data.split(":")[-1])
    new_name = data.get("new_name")
    old_slug = data.get("old_slug")

    async with SessionLocal() as session:
        category = (await session.execute(
            select(Category).where(Category.id == cat_id)
        )).scalar_one()
        category.name = new_name
        category.slug = old_slug
        parent_id = category.parent_id
        session.add(category)
        await session.commit()

    await state.clear()
    await admin_send_success(cb, f"✅ Категория <b>{new_name}</b> ({old_slug}) успешно переименована.", parent_id)
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"step: keep_slug_done | "
        f"chat_id: {getattr(cb.message.chat, 'id', None)} | "
        f"user_id: {getattr(cb.from_user, 'id', None)} | "
        f"cat_id: {cat_id} | "
        f"old_slug: {old_slug} | "
        f"new_name: {new_name} | "
        f"parent_id: {parent_id}"
    )
    return

# ====== Админ: Подтверждение удаления категории ======
@router.callback_query(F.data.startswith("admin:delete_category:"))
async def admin_delete_category_confirm(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    await clear_bot_messages(cb.message.chat.id, cb.bot)
    cat_id = int(cb.data.split(":")[-1])
    async with SessionLocal() as session:
        category = (await session.execute(
            select(Category).where(Category.id == cat_id)
        )).scalar_one()

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Нет", callback_data=f"admin:edit_category:{category.id}")],
            [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"admin:delete_category_yes:{category.id}")]
        ]
    )
    msg = await cb.message.answer(
        f"⚠️ <b>Удалить категорию?</b>\n\n<b>{category.name}</b>\n\n"
        "Категория будет безвозвратно удалениа!\n\n"
        "<i>Вы уверены?</i>",
        reply_markup=markup,
        parse_mode="HTML"
    )
    last_bot_messages[cb.message.chat.id] = [msg.message_id]
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {cb.data} | chat_id: {cb.message.chat.id} | "
        f"user_id: {cb.from_user.id} | msg_id: {msg.message_id}"
    )


# ====== Админ: Удаление категории (после подтверждения) ======
@router.callback_query(F.data.startswith("admin:delete_category_yes:"))
async def admin_delete_category(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    await clear_bot_messages(cb.message.chat.id, cb.bot)
    cat_id = int(cb.data.split(":")[-1])

    async with SessionLocal() as session:
        cat = (await session.execute(
            select(Category).where(Category.id == cat_id)
        )).scalar_one()
        parent_id = cat.parent_id
        cat_name = cat.name

        # --- Проверяем, есть ли подкатегории ---
        subcats = (await session.execute(
            select(Category).where(Category.parent_id == cat_id)
        )).scalars().all()
        if subcats:
            # Если есть подкатегории — выводим ошибку и НЕ удаляем!
            await admin_send_success(
                cb,
                f"❗️Нельзя удалить <b>{cat_name}</b> — сначала удалите все подкатегории!",
                parent_id
            )
            print(
                f"FUNC: {inspect.currentframe().f_code.co_name} | "
                f"cb.data: {cb.data} | chat_id: {cb.message.chat.id} | "
                f"user_id: {cb.from_user.id} | cat_id: {cat_id} | parent_id: {parent_id} | BLOCKED: HAS_SUBCATS"
            )
            return

        # --- Если подкатегорий нет — удаляем как обычно ---
        await session.delete(cat)
        await session.commit()

    await admin_send_success(cb, f"🗑️ Категория <b>{cat_name}</b> удалена.", parent_id)
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {cb.data} | chat_id: {cb.message.chat.id} | "
        f"user_id: {cb.from_user.id} | cat_id: {cat_id} | parent_id: {parent_id} | SUCCESS"
    )

# ====== Админ: Сообщение об успешном действии ======
async def admin_send_success(cb_or_msg, text: str, parent_id: int):
    if isinstance(cb_or_msg, CallbackQuery):
        chat_id = cb_or_msg.message.chat.id
        bot = cb_or_msg.message.bot
        send_func = cb_or_msg.message.answer
    elif isinstance(cb_or_msg, Message):
        chat_id = cb_or_msg.chat.id
        bot = cb_or_msg.bot
        send_func = cb_or_msg.answer
    else:
        raise Exception("admin_send_success: ожидается CallbackQuery или Message")

    await clear_bot_messages(chat_id, bot)

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="ОК", callback_data=f"admin:success_ok:{parent_id}")]
        ]
    )
    msg = await send_func(
        text, reply_markup=markup, parse_mode="HTML"
    )

    last_bot_messages[chat_id] = [msg.message_id]

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"text: {text} | parent_id: {parent_id} | "
        f"chat_id: {chat_id} | "
        f"user_id: {getattr(cb_or_msg.from_user, 'id', None)} | "
        f"msg_id: {getattr(msg, 'message_id', None)}"
    )

# ====== Админ: Кнопка ОК (возврат к списку) ======
@router.callback_query(F.data.startswith("admin:success_ok:"))
async def admin_success_ok_cb(cb: CallbackQuery, state: FSMContext):
    parent_id = int(cb.data.split(":")[-1])
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    from aiogram.types import CallbackQuery
    fake_cb = CallbackQuery(
        id="fake",
        from_user=cb.from_user,
        chat_instance=cb.chat_instance,
        message=cb.message,
        data=f"admin:edit_category:{parent_id}"
    )
    await admin_edit_subcategories_cb(fake_cb, state)
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {cb.data} | parent_id: {parent_id} | "
        f"chat_id: {getattr(cb.message.chat, 'id', None)} | user_id: {cb.from_user.id}"
    )
