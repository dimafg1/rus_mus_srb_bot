from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from app.routers.utils import clear_bot_messages, last_bot_messages, register_bot_messages
from sqlalchemy import select, text, func, and_, or_
from app.database import SessionLocal
from app.models import Category, BotUser, Item, Listing, Profile
from app.states import AdminCategoryStates, AdminFieldStates
from aiogram.types import ReplyKeyboardRemove
import inspect
import re
from datetime import datetime
from app.models import utcnow_naive
import json
import pytz
from html import escape as html_escape
from app.admin_ids import ADMIN_IDS, is_admin
SERBIA_TZ = pytz.timezone("Europe/Belgrade")
FEEDBACK_PAGE_SIZE = 10


router = Router()


def _normalized_category_name(value: str | None) -> str:
    name = (value or "").strip()
    if not name or any(ord(ch) < 32 for ch in name):
        raise ValueError("Название категории не может быть пустым")
    if len(name) > 200:
        raise ValueError("Название категории длиннее 200 символов")
    return name


def _normalized_category_slug(value: str | None) -> str:
    slug = (value or "").strip().lower()
    if not slug or not re.fullmatch(r"[a-z0-9_-]+", slug):
        raise ValueError("Недопустимый slug")
    if len(slug) > 100:
        raise ValueError("Slug длиннее 100 символов")
    return slug

def get_admin_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗂 Редактировать категории", callback_data="admin:edit_categories")
            ],
            [
                InlineKeyboardButton(text="🎭 Афиша: модерация", callback_data="admin:events:0")
            ],
            [
                InlineKeyboardButton(text="📬 Обратная связь", callback_data="admin_feedback")
            ],
            [
                InlineKeyboardButton(text="📊 Аналитика", callback_data="admin:analytics")
            ],
            [
                InlineKeyboardButton(text="👥 Пользователи бота", callback_data="admin:users:0")
            ],
            [
                InlineKeyboardButton(text="👤 Панель пользователя", callback_data="main_menu")
            ]
        ]
    )

# --- Утилиты для чтения/записи списка полей категории (Category.fields: JSON) ---
async def load_category_fields(session, cat_id: int) -> list[dict]:
    cat = (await session.execute(select(Category).where(Category.id == cat_id))).scalar_one()
    try:
        return json.loads(cat.fields) if cat.fields else []
    except Exception:
        return []

async def save_category_fields(session, cat_id: int, fields: list[dict]) -> None:
    cat = (await session.execute(select(Category).where(Category.id == cat_id))).scalar_one()
    cat.fields = json.dumps(fields, ensure_ascii=False)
    session.add(cat)
    await session.commit()


# ====== Вход в админ-панель ======
@router.message(Command("admin"))
async def admin_panel_entry(message: Message):
    if not is_admin(message.from_user.id):
        msg = await message.answer("У вас нет доступа к админ-панели.")
        last_bot_messages.setdefault(message.chat.id, []).append(msg.message_id)
        await register_bot_messages(message.chat.id, [msg.message_id])
        return

    await clear_bot_messages(message.chat.id, message.bot)

    menu = get_admin_menu()
    msg = await message.answer(
        "🔒 <b>Админ-панель</b>\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=menu
    )
    last_bot_messages[message.chat.id] = [msg.message_id]
    await register_bot_messages(message.chat.id, [msg.message_id])
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
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {getattr(cb.message.chat, 'id', None)} | "
        f"user_id: {getattr(cb.from_user, 'id', None)} | "
        f"msg_id: {getattr(msg, 'message_id', None)}"
    )

@router.callback_query(F.data == "admin")
async def admin_main_menu_cb(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    chat_id = cb.message.chat.id

    # 1) Удаляем сообщение, по которому нажали кнопку (предыдущее меню)
    try:
        await cb.message.delete()
    except Exception as e:
        print(f"[WARN] admin_main_menu_cb delete clicked msg: {e}")

    # 2) Чистим служебные сообщения из кэша
    await clear_bot_messages(chat_id, cb.bot)

    # 3) Рисуем админ-меню и кладём его в кэш
    menu = get_admin_menu()
    msg = None
    try:
        msg = await cb.bot.send_message(
            chat_id,
            "🔒 <b>Админ-панель</b>\nВыберите действие:",
            parse_mode="HTML",
            reply_markup=menu
        )
    except Exception as e:
        print(f"[ERROR] admin_main_menu_cb send_message: {e}")

    if msg:
        lst = last_bot_messages.get(chat_id) or []
        lst.append(msg.message_id)
        last_bot_messages[chat_id] = lst
        await register_bot_messages(chat_id, lst)

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {chat_id} | "
        f"user_id: {getattr(cb.from_user, 'id', None)} | "
        f"msg_id: {getattr(msg, 'message_id', None) if msg else None} | "
        f"cached_bot_msgs={last_bot_messages.get(chat_id)}"
    )

    try:
        await cb.answer()
    except Exception:
        pass

# ====== Админ: многоуровневое меню подкатегорий (с хлебными крошками) ======
@router.callback_query(F.data.startswith("admin:edit_category:"))
async def admin_edit_subcategories_cb(cb: CallbackQuery, state: FSMContext = None):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    await clear_bot_messages(cb.message.chat.id, cb.bot)

    try:
        parent_id = int(cb.data.split(":")[-1])
    except Exception:
        await cb.answer("Неверные данные", show_alert=True)
        return

    ROOT_CATEGORY_IDS = {30, 80, 90}

    async with SessionLocal() as session:
        # Текущая категория
        parent_category = (await session.execute(
            select(Category).where(Category.id == parent_id)
        )).scalar_one()

        # Собираем цепочку родителей до корня
        chain_names: list[str] = []
        cur = parent_category
        while cur:
            chain_names.append(html_escape(cur.name or ""))
            if cur.parent_id is None:
                break
            cur = (await session.execute(
                select(Category).where(Category.id == cur.parent_id)
            )).scalar_one()

        breadcrumb = " → ".join(reversed(chain_names))

        # Дочерние подкатегории
        subcategories = (await session.execute(
            select(Category).where(Category.parent_id == parent_id)
        )).scalars().all()

    # Клавиатура
    keyboard = []

    # Кнопка «Поля категории» — не для корней
    if parent_category.id not in ROOT_CATEGORY_IDS:
        keyboard.append([
            InlineKeyboardButton(
                text="⚙️ Дополнительные поля категории",
                callback_data=f"admin:fields:{parent_id}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(text="------ Подкатегории ------", callback_data="noop")
    ])

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
        keyboard.append([InlineKeyboardButton(text="———————————————", callback_data="noop")])
        keyboard.append([
            InlineKeyboardButton(
                text=f"✏️ Редактировать {parent_category.name}",
                callback_data=f"admin:rename_category:{parent_category.id}"
            )
        ])
        if not subcategories:  # показывать «Удалить» только если нет подкатегорий
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

    keyboard.append([
        InlineKeyboardButton(
            text="🛠 Админ-панель",
            callback_data=f"admin"
        )
    ])

    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    # Заголовок с полной цепочкой
    text = (
        f"📂 Админпанель - Категории: \n\n<b>{breadcrumb}</b>\n\n"
    )

    msg = await cb.message.answer(text, reply_markup=markup, parse_mode="HTML")
    last_bot_messages[cb.message.chat.id] = [msg.message_id]
    await register_bot_messages(cb.message.chat.id, [msg.message_id])

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"cb.data: {getattr(cb, 'data', None)} | "
        f"chat_id: {getattr(cb.message.chat, 'id', None)} | "
        f"user_id: {getattr(cb.from_user, 'id', None)} | "
        f"breadcrumb: {breadcrumb} | "
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
            InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin"),
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
    await register_bot_messages(cb.message.chat.id, [menu_msg.message_id, msg.message_id])
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
    try:
        category_name = _normalized_category_name(message.text)
    except ValueError as exc:
        msg = await message.answer(f"❗️{html_escape(str(exc))}. Введите название снова:")
        last_bot_messages[message.chat.id] = [msg.message_id]
        await register_bot_messages(message.chat.id, [msg.message_id])
        return
    await state.update_data(category_name=category_name)

    menu = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:edit_category:{parent_id}"),
            InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin"),
        ]
    ])
    menu_msg = await message.answer("Возврат", reply_markup=menu)
    msg = await message.answer("✏️ Введите <b>slug</b> для категории ...")
    last_bot_messages[message.chat.id] = [menu_msg.message_id, msg.message_id]
    await register_bot_messages(message.chat.id, [menu_msg.message_id, msg.message_id])
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
    try:
        slug = _normalized_category_slug(message.text)
    except ValueError:
        slug = ""

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
        await register_bot_messages(message.chat.id, [msg.message_id])
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
                func.lower(func.trim(Category.slug)) == slug
            )
        )).first()
        if exists:
            msg = await message.answer(
                "❗️Категория с таким slug уже существует. Введите другой slug:",
                parse_mode="HTML",
                reply_markup=ReplyKeyboardRemove()
            )
            last_bot_messages[message.chat.id] = [msg.message_id]
            await register_bot_messages(message.chat.id, [msg.message_id])
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
    await admin_send_success(
        message,
        f"✅ Категория <b>{html_escape(category_name)}</b> ({slug}) добавлена.",
        parent_id,
    )
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
            InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin"),
        ]
    ])
    menu_msg = await cb.message.answer("Возврат", reply_markup=menu)
    msg = await cb.message.answer(
        f"✏️ Переименование категории:\n<b>{html_escape(category.name or '')}</b>\n\nВведите новое название:",
        parse_mode="HTML"
    )
    last_bot_messages[cb.message.chat.id] = [menu_msg.message_id, msg.message_id]
    await register_bot_messages(cb.message.chat.id, [menu_msg.message_id, msg.message_id])
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
    try:
        new_name = _normalized_category_name(message.text)
    except ValueError as exc:
        msg = await message.answer(f"❗️{html_escape(str(exc))}. Введите название снова:")
        last_bot_messages[message.chat.id] = [msg.message_id]
        await register_bot_messages(message.chat.id, [msg.message_id])
        return
    await state.update_data(new_name=new_name)

    menu = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:edit_category:{cat_id}"),
            InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin"),
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
        f"Текущий slug: <code>{html_escape(old_slug or '')}</code>",
        parse_mode="HTML",
        reply_markup=slug_menu
    )
    last_bot_messages[message.chat.id] = [menu_msg.message_id, msg.message_id]
    await register_bot_messages(message.chat.id, [menu_msg.message_id, msg.message_id])
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
        try:
            slug = _normalized_category_slug(message.text)
        except ValueError:
            slug = ""

    import re
    if not slug or not re.fullmatch(r'[a-z0-9_\-]+', slug):
        menu = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:edit_category:{cat_id}"),
                InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin")
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
        chat_id = message.chat.id
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
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
                func.lower(func.trim(Category.slug)) == slug,
                Category.id != cat_id
            )
        )).first()
        if exists:
            menu = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:edit_category:{cat_id}"),
                    InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin")
                ],
                [
                    InlineKeyboardButton(text=f"⬇️ Оставить прежний slug: {old_slug}",
                                         callback_data=f"admin:keep_slug:{cat_id}")
                ]
            ])
            msg = await message.answer(
                "❗️Категория с таким slug уже существует. Введите другой slug "
                "или нажмите кнопку ниже, чтобы оставить прежний (⬇️):",
                reply_markup=menu,
                parse_mode="HTML"
            )
            chat_id = message.chat.id
            last_bot_messages[chat_id] = [msg.message_id]
            await register_bot_messages(chat_id, [msg.message_id])
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
    await admin_send_success(
        message,
        f"✅ Категория <b>{html_escape(new_name)}</b> ({slug}) успешно переименована.",
        parent_id,
    )
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
    try:
        new_name = _normalized_category_name(data.get("new_name"))
        old_slug = _normalized_category_slug(data.get("old_slug"))
    except ValueError:
        await state.clear()
        await cb.answer("Данные формы устарели. Начните переименование снова.", show_alert=True)
        return

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
    await admin_send_success(
        cb,
        f"✅ Категория <b>{html_escape(new_name or '')}</b> ({html_escape(old_slug or '')}) успешно переименована.",
        parent_id,
    )
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
        f"⚠️ <b>Удалить категорию?</b>\n\n<b>{html_escape(category.name or '')}</b>\n\n"
        "Категория будет безвозвратно удалениа!\n\n"
        "<i>Вы уверены?</i>",
        reply_markup=markup,
        parse_mode="HTML"
    )
    last_bot_messages[cb.message.chat.id] = [msg.message_id]
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
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
                f"❗️Нельзя удалить <b>{html_escape(cat_name or '')}</b> — сначала удалите все подкатегории!",
                parent_id
            )
            print(
                f"FUNC: {inspect.currentframe().f_code.co_name} | "
                f"cb.data: {cb.data} | chat_id: {cb.message.chat.id} | "
                f"user_id: {cb.from_user.id} | cat_id: {cat_id} | parent_id: {parent_id} | BLOCKED: HAS_SUBCATS"
            )
            return

        # Категория участвует не только как основная, но и как дополнительная.
        # Удалять её при любых ссылках нельзя: иначе админка оставит сиротские id,
        # а бот с PRAGMA foreign_keys=ON получит IntegrityError.
        listings_count = (await session.execute(
            select(func.count(Listing.id)).where(
                or_(
                    Listing.category_id == cat_id,
                    Listing.extra_category_id1 == cat_id,
                    Listing.extra_category_id2 == cat_id,
                )
            )
        )).scalar_one()
        if listings_count:
            await admin_send_success(
                cb,
                f"❗️Нельзя удалить <b>{html_escape(cat_name or '')}</b> — категория используется "
                f"в {listings_count} объявлениях (включая архивные).",
                parent_id,
            )
            return

        items_count = (await session.execute(
            select(func.count(Item.id)).where(Item.category_id == cat_id)
        )).scalar_one()
        profiles_count = (await session.execute(
            select(func.count(Profile.id)).where(Profile.category_id == cat_id)
        )).scalar_one()
        if items_count or profiles_count:
            refs = []
            if items_count:
                refs.append(f"анкеты: {items_count}")
            if profiles_count:
                refs.append(f"профили: {profiles_count}")
            await admin_send_success(
                cb,
                f"❗️Нельзя удалить <b>{html_escape(cat_name or '')}</b> — категория используется "
                + ", ".join(refs) + ".",
                parent_id,
            )
            return

        # --- Если подкатегорий и ссылок нет — удаляем ---
        await session.delete(cat)
        await session.commit()

    await admin_send_success(
        cb,
        f"🗑️ Категория <b>{html_escape(cat_name or '')}</b> удалена.",
        parent_id,
    )
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
    await register_bot_messages(chat_id, [msg.message_id])

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


# Новый обработчик входа в обратную связь
@router.callback_query(F.data == "admin_feedback")
async def admin_feedback_entry(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await send_admin_feedback_list(cb, offset=0)

# Новый обработчик страниц
@router.callback_query(F.data.startswith("admin_feedback_list"))
async def admin_feedback_list(cb: CallbackQuery):
    # Извлекаем смещение из callback_data
    data = cb.data.split(":")
    offset = int(data[1]) if len(data) > 1 and data[1].isdigit() else 0
    await send_admin_feedback_list(cb, offset=offset)

# Бизнес-логика списка с пагинацией (отдельная функция!)
async def send_admin_feedback_list(cb: CallbackQuery, offset: int = 0):
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    limit = FEEDBACK_PAGE_SIZE

    async with SessionLocal() as session:
        # всего писем
        total = (await session.execute(text("SELECT COUNT(*) AS c FROM feedback"))).scalar_one()

        result = await session.execute(
            text(
                "SELECT id, username, created_at, is_read "
                "FROM feedback "
                "ORDER BY created_at DESC, id DESC "
                "LIMIT :limit OFFSET :offset"
            ),
            {"limit": limit, "offset": offset}
        )
        rows = result.fetchall()

    keyboard = []

    # Кнопка "Более поздние" (новее) — только если offset > 0
    if offset > 0:
        keyboard.append([InlineKeyboardButton(
            text="⏫⏫⏫ Более поздние ⏫⏫⏫",
            callback_data=f"admin_feedback_list:{max(0, offset - limit)}"
        )])

    # Сообщения (нумерация глобальная)
    start_no = offset + 1
    for i, r in enumerate(rows):
        idx = offset + i + 1
        status = "•" if not r.is_read else ""
        uname = f"@{r.username}" if r.username else "Без ника"

        dt = r.created_at
        if isinstance(dt, str):
            try:
                dt = datetime.fromisoformat(dt)
            except Exception:
                dt = datetime.strptime(dt[:19], "%Y-%m-%d %H:%M:%S")
        if dt.tzinfo is None:
            dt = pytz.UTC.localize(dt)
        dt = dt.astimezone(SERBIA_TZ)
        dt_str = dt.strftime("%H:%M %d:%m:%y")

        btn_text = f"{idx}. {status} {uname} · {dt_str}"
        keyboard.append([InlineKeyboardButton(
            text=btn_text,
            callback_data=f"admin_feedback_view:{r.id}"
        )])

    # Кнопка "Более ранние" — если есть ещё страницы
    if offset + limit < total:
        keyboard.append([InlineKeyboardButton(
            text="⏬⏬⏬ Более ранние ⏬⏬⏬",
            callback_data=f"admin_feedback_list:{offset + limit}"
        )])

    # Кнопки возврата
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin")])
    keyboard.append([InlineKeyboardButton(text="🛠️ Админпанель", callback_data="admin")])

    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    end_no = min(offset + len(rows), total)
    header = f"📬 <b>Сообщения (новые сверху)</b>\nПоказаны {start_no}–{end_no} из {total}"

    msg = await cb.message.answer(header, reply_markup=markup, parse_mode="HTML")
    last_bot_messages[cb.message.chat.id] = [msg.message_id]
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
    print(
        f"FUNC: send_admin_feedback_list | offset: {offset} | "
        f"chat_id: {cb.message.chat.id} | user_id: {cb.from_user.id} | rows: {len(rows)} | total: {total}"
    )

# ── Админ: просмотр одного письма обратной связи (все кнопки в ОДНОМ сообщении)
#     • Удаляем предыдущее сообщение (в т.ч. экран подтверждения)
#     • Считаем позицию [pos/total], соседей (новые сверху)
#     • Клавиатура под письмом: ⏫⏫⏫ / ⏬⏬⏬ / (🗑 Удалить  ↩️ К списку)
@router.callback_query(F.data.startswith("admin_feedback_view:"))
async def admin_feedback_view(cb: CallbackQuery):
    # 1) Сносим сообщение, по которому пришли (чтобы не висели подтверждения и пр.)
    try:
        await cb.message.delete()
    except Exception:
        pass

    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)

    fb_id = int(cb.data.split(":")[1])

    # 2) Читаем письмо + считаем позицию/соседей (новые сверху: created_at DESC, id DESC)
    async with SessionLocal() as session:
        res = await session.execute(
            text(
                "SELECT id, user_id, username, message, created_at, is_read "
                "FROM feedback WHERE id = :id"
            ),
            {"id": fb_id}
        )
        row = res.fetchone()
        if not row:
            msg = await cb.message.answer("Сообщение не найдено или удалено.")
            last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
            await register_bot_messages(cb.message.chat.id, [msg.message_id])
            return

        # помечаем прочитанным
        await session.execute(text("UPDATE feedback SET is_read = 1 WHERE id = :id"), {"id": fb_id})
        await session.commit()

        total = (await session.execute(text("SELECT COUNT(*) AS c FROM feedback"))).scalar_one()

        count_newer = (await session.execute(
            text(
                "SELECT COUNT(*) AS c FROM feedback "
                "WHERE created_at > :cur_dt "
                "   OR (created_at = :cur_dt AND id > :cur_id)"
            ),
            {"cur_dt": row.created_at, "cur_id": row.id}
        )).scalar_one()
        pos = count_newer + 1

        prev_row = (await session.execute(
            text(
                "SELECT id FROM feedback "
                "WHERE created_at > :cur_dt "
                "   OR (created_at = :cur_dt AND id > :cur_id) "
                "ORDER BY created_at ASC, id ASC "
                "LIMIT 1"
            ),
            {"cur_dt": row.created_at, "cur_id": row.id}
        )).fetchone()
        prev_id = prev_row.id if prev_row else None

        next_row = (await session.execute(
            text(
                "SELECT id FROM feedback "
                "WHERE created_at < :cur_dt "
                "   OR (created_at = :cur_dt AND id < :cur_id) "
                "ORDER BY created_at DESC, id DESC "
                "LIMIT 1"
            ),
            {"cur_dt": row.created_at, "cur_id": row.id}
        )).fetchone()
        next_id = next_row.id if next_row else None

    # 3) Текст письма
    uname = f"@{row.username}" if row.username else "Без ника"
    dt_val = row.created_at
    try:
        dt_val = datetime.fromisoformat(dt_val) if isinstance(dt_val, str) else dt_val
    except Exception:
        dt_val = utcnow_naive()
    dt_str = dt_val.strftime("%H:%M %d:%m:%y")

    header = (
        f"📨 <b>Обратная связь</b>  <i>[{pos}/{total}]</i>\n"
        f"<b>От:</b> {uname}\n"
        f"<b>Дата:</b> {dt_str}\n"
    )
    body = row.message or "—"
    msg_text = f"{header}\n{body}"

    # 4) На какую страницу возвращаться «к списку»
    back_offset = ((pos - 1) // FEEDBACK_PAGE_SIZE) * FEEDBACK_PAGE_SIZE

    # 5) Клавиатура (три строки максимум) — всё в одном сообщении
    rows = []
    if prev_id:
        rows.append([InlineKeyboardButton(text="⏫⏫⏫", callback_data=f"admin_feedback_view:{prev_id}")])
    if next_id:
        rows.append([InlineKeyboardButton(text="⏬⏬⏬", callback_data=f"admin_feedback_view:{next_id}")])
    rows.append([
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"admin_feedback_delete:{row.id}"),
        InlineKeyboardButton(text="↩️ К списку", callback_data=f"admin_feedback_list:{back_offset}")
    ])
    markup = InlineKeyboardMarkup(inline_keyboard=rows)

    msg = await cb.message.answer(msg_text, reply_markup=markup, parse_mode="HTML")
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

    # 6) Лог
    print(
        f"FUNC: admin_feedback_view | id={fb_id} | pos={pos}/{total} | "
        f"prev_id={prev_id} | next_id={next_id} | back_offset={back_offset} | "
        f"sent_id={msg.message_id} | chat_id={chat_id} | user_id={cb.from_user.id}"
    )



# ⛔️ Подтверждение удаления (редактируем только клавиатуру, без мусора)
@router.callback_query(F.data.startswith("admin_feedback_delete:"))
async def admin_feedback_delete_confirm(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    fb_id = int(cb.data.split(":")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_feedback_view:{fb_id}")],
        [InlineKeyboardButton(text="✅ Удалить навсегда", callback_data=f"admin_feedback_delete_yes:{fb_id}")]
    ])
    try:
        await cb.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass

    await cb.answer("Удалить это сообщение?")
    print(f"FUNC: admin_feedback_delete_confirm | id={fb_id} | chat_id={cb.message.chat.id} | user_id={cb.from_user.id}")


# ── Админ: удаление письма навсегда (с переходом к соседу и чисткой подтверждения) ──
@router.callback_query(F.data.startswith("admin_feedback_delete_yes:"))
async def admin_feedback_delete_yes(cb: CallbackQuery):
    # Сначала убираем экран подтверждения, чтобы он не висел
    try:
        await cb.message.delete()
    except Exception:
        pass

    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    fb_id = int(cb.data.split(":")[1])

    async with SessionLocal() as session:
        res = await session.execute(
            text("SELECT id, created_at FROM feedback WHERE id = :id"),
            {"id": fb_id}
        )
        cur = res.fetchone()
        if not cur:
            await send_admin_feedback_list(cb, offset=0)
            print(f"FUNC: admin_feedback_delete_yes | already_deleted id={fb_id}")
            return

        prev_row = (await session.execute(
            text(
                "SELECT id FROM feedback "
                "WHERE created_at > :cur_dt OR (created_at = :cur_dt AND id > :cur_id) "
                "ORDER BY created_at ASC, id ASC LIMIT 1"
            ),
            {"cur_dt": cur.created_at, "cur_id": cur.id}
        )).fetchone()
        prev_id = prev_row.id if prev_row else None

        next_row = (await session.execute(
            text(
                "SELECT id FROM feedback "
                "WHERE created_at < :cur_dt OR (created_at = :cur_dt AND id < :cur_id) "
                "ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            {"cur_dt": cur.created_at, "cur_id": cur.id}
        )).fetchone()
        next_id = next_row.id if next_row else None

        await session.execute(text("DELETE FROM feedback WHERE id = :id"), {"id": fb_id})
        await session.commit()

    target_id = next_id or prev_id
    if target_id:
        # Переходим к соседнему письму
        from aiogram.types import CallbackQuery as CQ
        fake = CQ(
            id="fake",
            from_user=cb.from_user,
            chat_instance=cb.chat_instance,
            message=cb.message,
            data=f"admin_feedback_view:{target_id}",
        )
        await admin_feedback_view(fake)
    else:
        # Писем не осталось — вернёмся к списку
        await send_admin_feedback_list(cb, offset=0)

    print(f"FUNC: admin_feedback_delete_yes | deleted_id={fb_id} | next_id={next_id} | prev_id={prev_id} | chat_id={cb.message.chat.id} | user_id={cb.from_user.id}")


# ====== Список пользователей бота ======

USERS_PAGE_SIZE = 20


@router.callback_query(F.data.startswith("admin:users:"))
async def admin_users_list(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа.", show_alert=True)
        return

    try:
        offset = int(cb.data.split(":")[2])
    except Exception:
        offset = 0

    async with SessionLocal() as s:
        total_row = await s.execute(select(func.count()).select_from(BotUser))
        total = total_row.scalar() or 0

        users_rows = await s.execute(
            select(BotUser).order_by(BotUser.last_seen.desc()).offset(offset).limit(USERS_PAGE_SIZE)
        )
        users = users_rows.scalars().all()

    if not users:
        await cb.answer("Список пользователей пуст.", show_alert=True)
        return

    lines = [f"<b>👥 Пользователи бота</b> (всего: {total})"]
    for u in users:
        nick = f"@{u.username}" if u.username else (u.full_name or "—")
        dt_belgrade = u.last_seen.replace(tzinfo=pytz.utc).astimezone(SERBIA_TZ)
        dt_str = dt_belgrade.strftime("%d.%m.%Y %H:%M")
        lines.append(f"• {nick}  <code>{u.user_id}</code>  {dt_str}")

    text = "\n\n".join(lines)

    nav_buttons = []
    if offset > 0:
        nav_buttons.append(
            InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:users:{max(0, offset - USERS_PAGE_SIZE)}")
        )
    if offset + USERS_PAGE_SIZE < total:
        nav_buttons.append(
            InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"admin:users:{offset + USERS_PAGE_SIZE}")
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[nav_buttons] if nav_buttons else [])
    kb.inline_keyboard.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin")])

    await clear_bot_messages(cb.message.chat.id, cb.bot)
    msg = await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
    await cb.answer()
