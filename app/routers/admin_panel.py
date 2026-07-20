from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from app.routers.utils import clear_bot_messages, last_bot_messages, register_bot_messages, get_text
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
import app.keyboards as _keyboards  # импорт модуля, а не имени — иначе цикл
                                     # (app.keyboards импортирует is_admin отсюда)
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

async def get_admin_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=await get_text("admin_panel_btn_edit_categories", "ru") or "🗂 Редактировать категории", callback_data="admin:edit_categories")
            ],
            [
                InlineKeyboardButton(text=await get_text("admin_panel_btn_events_moderation", "ru") or "🎭 Афиша: модерация", callback_data="admin:events:0")
            ],
            [
                InlineKeyboardButton(text=await get_text("admin_panel_btn_feedback", "ru") or "📬 Обратная связь", callback_data="admin_feedback")
            ],
            [
                InlineKeyboardButton(text=await get_text("admin_panel_btn_analytics", "ru") or "📊 Аналитика", callback_data="admin:analytics")
            ],
            [
                InlineKeyboardButton(text=await get_text("admin_panel_btn_users", "ru") or "👥 Пользователи бота", callback_data="admin:users:0")
            ],
            [
                InlineKeyboardButton(text=await get_text("admin_panel_btn_user_panel", "ru") or "👤 Панель пользователя", callback_data="main_menu")
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
        msg = await message.answer(await get_text("admin_panel_no_access", "ru") or "У вас нет доступа к админ-панели.")
        last_bot_messages.setdefault(message.chat.id, []).append(msg.message_id)
        await register_bot_messages(message.chat.id, [msg.message_id])
        return

    await clear_bot_messages(message.chat.id, message.bot)

    menu = await get_admin_menu()
    msg = await message.answer(
        (await get_text("admin_panel_header", "ru") or "🔒 <b>Админ-панель</b>\nВыберите действие:"),
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
        await cb.answer(await get_text("err_no_access_short", "ru") or "Нет доступа", show_alert=True)
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
        (await get_text("admin_panel_categories_list_header", "ru") or "🗂 <b>Список категорий</b>\nНажмите на категорию для редактирования."),
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
        await cb.answer(await get_text("err_no_access_short", "ru") or "Нет доступа", show_alert=True)
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
    menu = await get_admin_menu()
    msg = None
    try:
        msg = await cb.bot.send_message(
            chat_id,
            (await get_text("admin_panel_header", "ru") or "🔒 <b>Админ-панель</b>\nВыберите действие:"),
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
        await cb.answer(await get_text("err_no_access_short", "ru") or "Нет доступа", show_alert=True)
        return

    await clear_bot_messages(cb.message.chat.id, cb.bot)

    try:
        parent_id = int(cb.data.split(":")[-1])
    except Exception:
        await cb.answer(await get_text("err_bad_data", "ru") or "Неверные данные", show_alert=True)
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
                text=(await get_text("admin_panel_btn_extra_fields", "ru") or "⚙️ Дополнительные поля категории"),
                callback_data=f"admin:fields:{parent_id}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(text=(await get_text("admin_panel_btn_subcats_divider", "ru") or "------ Подкатегории ------"), callback_data="noop")
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
            text=(await get_text("admin_panel_btn_add_subcategory", "ru") or "✚ Добавить подкатегорию"),
            callback_data=f"admin:add_category:{parent_id}"
        )
    ])

    if parent_category.id not in ROOT_CATEGORY_IDS:
        keyboard.append([InlineKeyboardButton(text="———————————————", callback_data="noop")])
        edit_btn_tmpl = await get_text("admin_panel_btn_edit_category_tmpl", "ru") or "✏️ Редактировать {name}"
        keyboard.append([
            InlineKeyboardButton(
                text=edit_btn_tmpl.format(name=parent_category.name),
                callback_data=f"admin:rename_category:{parent_category.id}"
            )
        ])
        if not subcategories:  # показывать «Удалить» только если нет подкатегорий
            delete_btn_tmpl = await get_text("admin_panel_btn_delete_category_tmpl", "ru") or "🗑️ Удалить {name}"
            keyboard.append([
                InlineKeyboardButton(
                    text=delete_btn_tmpl.format(name=parent_category.name),
                    callback_data=f"admin:delete_category:{parent_category.id}"
                )
            ])

    back_cb = (
        f"admin:edit_category:{parent_category.parent_id}"
        if parent_category.parent_id is not None else
        "admin:edit_categories"
    )
    back_btn = await _keyboards.get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)
    back_btn.callback_data = back_cb
    keyboard.append([back_btn])

    keyboard.append([
        InlineKeyboardButton(
            text=(await get_text("admin_panel_btn_admin_panel", "ru") or "🛠 Админ-панель"),
            callback_data=f"admin"
        )
    ])

    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    # Заголовок с полной цепочкой
    breadcrumb_tmpl = await get_text("admin_panel_categories_breadcrumb_tmpl", "ru") or "📂 Админпанель - Категории: \n\n<b>{breadcrumb}</b>\n\n"
    text = breadcrumb_tmpl.format(breadcrumb=breadcrumb)

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
        await cb.answer(await get_text("err_no_access_short", "ru") or "Нет доступа", show_alert=True)
        return

    await clear_bot_messages(cb.message.chat.id, cb.bot)
    parent_id = int(cb.data.split(":")[-1])
    await state.update_data(parent_id=parent_id)

    back_btn = await _keyboards.get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:edit_category:{parent_id}")
    back_btn.callback_data = f"admin:edit_category:{parent_id}"
    menu = InlineKeyboardMarkup(inline_keyboard=[
        [
            back_btn,
            InlineKeyboardButton(text=(await get_text("admin_panel_btn_admin_panel", "ru") or "🛠 Админ-панель"), callback_data="admin"),
        ]
    ])
    menu_msg = await cb.message.answer(
        (await get_text("admin_panel_return", "ru") or "Возврат"),
        reply_markup=menu
    )
    msg = await cb.message.answer(
        (await get_text("admin_panel_ask_category_name", "ru") or "✏️ Введите <b>название</b> новой категории:"),
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
        await message.answer(await get_text("admin_panel_no_access_short", "ru") or "Нет доступа к этой функции.")
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
        name_error_tmpl = await get_text("admin_panel_name_error_tmpl", "ru") or "❗️{error}. Введите название снова:"
        msg = await message.answer(name_error_tmpl.format(error=html_escape(str(exc))))
        last_bot_messages[message.chat.id] = [msg.message_id]
        await register_bot_messages(message.chat.id, [msg.message_id])
        return
    await state.update_data(category_name=category_name)

    back_btn = await _keyboards.get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:edit_category:{parent_id}")
    back_btn.callback_data = f"admin:edit_category:{parent_id}"
    menu = InlineKeyboardMarkup(inline_keyboard=[
        [
            back_btn,
            InlineKeyboardButton(text=(await get_text("admin_panel_btn_admin_panel", "ru") or "🛠 Админ-панель"), callback_data="admin"),
        ]
    ])
    menu_msg = await message.answer((await get_text("admin_panel_return", "ru") or "Возврат"), reply_markup=menu)
    msg = await message.answer((await get_text("admin_panel_ask_category_slug", "ru") or "✏️ Введите <b>slug</b> для категории ..."))
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
        await message.answer(await get_text("admin_panel_no_access_short", "ru") or "Нет доступа к этой функции.")
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
            await get_text("admin_panel_slug_invalid_simple", "ru") or "❗️Slug должен содержать только латинские буквы, цифры, дефис или _ (нижнее подчёркивание). Введите снова:",
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
                await get_text("admin_panel_slug_duplicate_simple", "ru") or "❗️Категория с таким slug уже существует. Введите другой slug:",
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
    added_tmpl = await get_text("admin_panel_category_added_tmpl", "ru") or "✅ Категория <b>{name}</b> ({slug}) добавлена."
    await admin_send_success(
        message,
        added_tmpl.format(name=html_escape(category_name), slug=slug),
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
        await cb.answer(await get_text("err_no_access_short", "ru") or "Нет доступа", show_alert=True)
        return

    await clear_bot_messages(cb.message.chat.id, cb.bot)
    cat_id = int(cb.data.split(":")[-1])
    async with SessionLocal() as session:
        category = (await session.execute(
            select(Category).where(Category.id == cat_id)
        )).scalar_one()

    await state.update_data(rename_cat_id=cat_id, old_name=category.name, old_slug=category.slug)

    back_btn = await _keyboards.get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:edit_category:{category.parent_id}")
    back_btn.callback_data = f"admin:edit_category:{category.parent_id}"
    menu = InlineKeyboardMarkup(inline_keyboard=[
        [
            back_btn,
            InlineKeyboardButton(text=(await get_text("admin_panel_btn_admin_panel", "ru") or "🛠 Админ-панель"), callback_data="admin"),
        ]
    ])
    menu_msg = await cb.message.answer((await get_text("admin_panel_return", "ru") or "Возврат"), reply_markup=menu)
    rename_prompt_tmpl = await get_text("admin_panel_rename_prompt_tmpl", "ru") or "✏️ Переименование категории:\n<b>{name}</b>\n\nВведите новое название:"
    msg = await cb.message.answer(
        rename_prompt_tmpl.format(name=html_escape(category.name or '')),
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
        await message.answer(await get_text("admin_panel_no_access_polite", "ru") or "У вас нет доступа к этой функции.")
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
        name_error_tmpl = await get_text("admin_panel_name_error_tmpl", "ru") or "❗️{error}. Введите название снова:"
        msg = await message.answer(name_error_tmpl.format(error=html_escape(str(exc))))
        last_bot_messages[message.chat.id] = [msg.message_id]
        await register_bot_messages(message.chat.id, [msg.message_id])
        return
    await state.update_data(new_name=new_name)

    back_btn = await _keyboards.get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:edit_category:{cat_id}")
    back_btn.callback_data = f"admin:edit_category:{cat_id}"
    menu = InlineKeyboardMarkup(inline_keyboard=[
        [
            back_btn,
            InlineKeyboardButton(text=(await get_text("admin_panel_btn_admin_panel", "ru") or "🛠 Админ-панель"), callback_data="admin"),
        ]
    ])
    menu_msg = await message.answer((await get_text("admin_panel_return", "ru") or "Возврат"), reply_markup=menu)

    slug_menu = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=(await get_text("admin_panel_btn_keep_slug_tmpl", "ru") or "⬇️ Оставить прежний slug: {slug}").format(slug=old_slug),
                callback_data=f"admin:keep_slug:{cat_id}"
            )
        ]
    ])
    rename_slug_prompt_tmpl = await get_text("admin_panel_rename_slug_prompt_tmpl", "ru") or (
        "✏️ Введите <b>slug</b> для категории (или нажмите кнопку ниже для старого slug):\n"
        "Текущий slug: <code>{old_slug}</code>"
    )
    msg = await message.answer(
        rename_slug_prompt_tmpl.format(old_slug=html_escape(old_slug or '')),
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
        await message.answer(await get_text("admin_panel_no_access_short", "ru") or "Нет доступа к этой функции.")
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
        back_btn = await _keyboards.get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:edit_category:{cat_id}")
        back_btn.callback_data = f"admin:edit_category:{cat_id}"
        menu = InlineKeyboardMarkup(inline_keyboard=[
            [
                back_btn,
                InlineKeyboardButton(text=(await get_text("admin_panel_btn_admin_panel", "ru") or "🛠 Админ-панель"), callback_data="admin")
            ],
            [
                InlineKeyboardButton(text=(await get_text("admin_panel_btn_keep_slug_tmpl", "ru") or "⬇️ Оставить прежний slug: {slug}").format(slug=old_slug),
                                     callback_data=f"admin:keep_slug:{cat_id}")
            ]
        ])
        msg = await message.answer(
            await get_text("admin_panel_slug_invalid_with_keep", "ru") or (
                "❗️Slug должен содержать только латинские буквы, цифры, дефис или _ (нижнее подчёркивание).\n"
                "Введите новый slug или нажмите кнопку ниже, чтобы оставить старый (⬇️):"
            ),
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
            back_btn = await _keyboards.get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:edit_category:{cat_id}")
            back_btn.callback_data = f"admin:edit_category:{cat_id}"
            menu = InlineKeyboardMarkup(inline_keyboard=[
                [
                    back_btn,
                    InlineKeyboardButton(text=(await get_text("admin_panel_btn_admin_panel", "ru") or "🛠 Админ-панель"), callback_data="admin")
                ],
                [
                    InlineKeyboardButton(text=(await get_text("admin_panel_btn_keep_slug_tmpl", "ru") or "⬇️ Оставить прежний slug: {slug}").format(slug=old_slug),
                                         callback_data=f"admin:keep_slug:{cat_id}")
                ]
            ])
            msg = await message.answer(
                await get_text("admin_panel_slug_duplicate_with_keep", "ru") or (
                    "❗️Категория с таким slug уже существует. Введите другой slug "
                    "или нажмите кнопку ниже, чтобы оставить прежний (⬇️):"
                ),
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
    renamed_tmpl = await get_text("admin_panel_category_renamed_tmpl", "ru") or "✅ Категория <b>{name}</b> ({slug}) успешно переименована."
    await admin_send_success(
        message,
        renamed_tmpl.format(name=html_escape(new_name), slug=slug),
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
        await cb.answer(await get_text("err_no_access_short", "ru") or "Нет доступа", show_alert=True)
        return

    await clear_bot_messages(cb.message.chat.id, cb.bot)

    data = await state.get_data()
    cat_id = int(cb.data.split(":")[-1])
    try:
        new_name = _normalized_category_name(data.get("new_name"))
        old_slug = _normalized_category_slug(data.get("old_slug"))
    except ValueError:
        await state.clear()
        await cb.answer(await get_text("admin_panel_rename_stale", "ru") or "Данные формы устарели. Начните переименование снова.", show_alert=True)
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
    renamed_tmpl = await get_text("admin_panel_category_renamed_tmpl", "ru") or "✅ Категория <b>{name}</b> ({slug}) успешно переименована."
    await admin_send_success(
        cb,
        renamed_tmpl.format(name=html_escape(new_name or ''), slug=html_escape(old_slug or '')),
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
        await cb.answer(await get_text("err_no_access_short", "ru") or "Нет доступа", show_alert=True)
        return

    await clear_bot_messages(cb.message.chat.id, cb.bot)
    cat_id = int(cb.data.split(":")[-1])
    async with SessionLocal() as session:
        category = (await session.execute(
            select(Category).where(Category.id == cat_id)
        )).scalar_one()

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=(await get_text("admin_panel_btn_no", "ru") or "❌ Нет"), callback_data=f"admin:edit_category:{category.id}")],
            [InlineKeyboardButton(text=(await get_text("btn_yes_delete", "ru") or "✅ Да, удалить"), callback_data=f"admin:delete_category_yes:{category.id}")]
        ]
    )
    delete_confirm_tmpl = await get_text("admin_panel_delete_confirm_tmpl", "ru") or (
        "⚠️ <b>Удалить категорию?</b>\n\n<b>{name}</b>\n\n"
        "Категория будет безвозвратно удалениа!\n\n"
        "<i>Вы уверены?</i>"
    )
    msg = await cb.message.answer(
        delete_confirm_tmpl.format(name=html_escape(category.name or '')),
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
        await cb.answer(await get_text("err_no_access_short", "ru") or "Нет доступа", show_alert=True)
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
            blocked_subcats_tmpl = await get_text("admin_panel_delete_blocked_subcats_tmpl", "ru") or "❗️Нельзя удалить <b>{name}</b> — сначала удалите все подкатегории!"
            await admin_send_success(
                cb,
                blocked_subcats_tmpl.format(name=html_escape(cat_name or '')),
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
            blocked_listings_tmpl = await get_text("admin_panel_delete_blocked_listings_tmpl", "ru") or "❗️Нельзя удалить <b>{name}</b> — категория используется в {count} объявлениях (включая архивные)."
            await admin_send_success(
                cb,
                blocked_listings_tmpl.format(name=html_escape(cat_name or ''), count=listings_count),
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
                items_tmpl = await get_text("admin_panel_ref_items_tmpl", "ru") or "анкеты: {count}"
                refs.append(items_tmpl.format(count=items_count))
            if profiles_count:
                profiles_tmpl = await get_text("admin_panel_ref_profiles_tmpl", "ru") or "профили: {count}"
                refs.append(profiles_tmpl.format(count=profiles_count))
            blocked_refs_tmpl = await get_text("admin_panel_delete_blocked_refs_tmpl", "ru") or "❗️Нельзя удалить <b>{name}</b> — категория используется {refs}."
            await admin_send_success(
                cb,
                blocked_refs_tmpl.format(name=html_escape(cat_name or ''), refs=", ".join(refs)),
                parent_id,
            )
            return

        # --- Если подкатегорий и ссылок нет — удаляем ---
        await session.delete(cat)
        await session.commit()

    deleted_tmpl = await get_text("admin_panel_category_deleted_tmpl", "ru") or "🗑️ Категория <b>{name}</b> удалена."
    await admin_send_success(
        cb,
        deleted_tmpl.format(name=html_escape(cat_name or '')),
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
            [InlineKeyboardButton(text=(await get_text("admin_panel_btn_ok", "ru") or "ОК"), callback_data=f"admin:success_ok:{parent_id}")]
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
def _fmt_dt_belgrade(dt_raw) -> str:
    dt = dt_raw
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except Exception:
            dt = datetime.strptime(dt[:19], "%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is None:
        dt = pytz.UTC.localize(dt)
    dt = dt.astimezone(SERBIA_TZ)
    return dt.strftime("%H:%M %d:%m:%y")


def _fb_status_marker(answered_at, needs_reply, is_read) -> str:
    # ✅ отвечено · 🔔 запросили ответ · • непрочитано
    if answered_at:
        return "✅"
    if needs_reply:
        return "🔔"
    if not is_read:
        return "•"
    return ""


@router.callback_query(F.data == "admin_feedback")
async def admin_feedback_entry(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer(await get_text("err_no_access_short", "ru") or "Нет доступа", show_alert=True)
        return
    # Убираем сообщение, с которого нажали (уведомление/меню), как в карточке обращения
    try:
        await cb.message.delete()
    except Exception:
        pass
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
        unanswered = (await session.execute(text(
            "SELECT COUNT(*) AS c FROM feedback WHERE needs_reply=1 AND answered_at IS NULL"
        ))).scalar_one()

        result = await session.execute(
            text(
                "SELECT id, username, created_at, is_read, needs_reply, answered_at "
                "FROM feedback "
                "ORDER BY created_at DESC, id DESC "
                "LIMIT :limit OFFSET :offset"
            ),
            {"limit": limit, "offset": offset}
        )
        rows = result.fetchall()

    keyboard = []

    # Подстраховка: неотвеченные — отдельным пунктом сразу под сообщением,
    # чтобы не потерять их в общем списке, даже если бегло листать список.
    if unanswered:
        unanswered_tmpl = await get_text("admin_panel_btn_unanswered_tmpl", "ru") or "🔔 Неотвеченные ({count})"
        unanswered_label = unanswered_tmpl.format(count=unanswered)
    else:
        unanswered_label = await get_text("admin_panel_btn_unanswered_none", "ru") or "✅ Неотвеченных нет"
    keyboard.append([InlineKeyboardButton(text=unanswered_label, callback_data="admin_feedback_unanswered")])

    # Сообщения (нумерация глобальная)
    start_no = offset + 1
    for i, r in enumerate(rows):
        idx = offset + i + 1
        status = _fb_status_marker(r.answered_at, r.needs_reply, r.is_read)
        uname = f"@{r.username}" if r.username else (await get_text("admin_panel_no_username", "ru") or "Без ника")
        dt_str = _fmt_dt_belgrade(r.created_at)

        btn_text = f"{idx}. {status} {uname} · {dt_str}"
        keyboard.append([InlineKeyboardButton(
            text=btn_text,
            callback_data=f"admin_feedback_view:{r.id}"
        )])

    # Пагинация — единый стиль бота: «  page/pages  »
    pages = max(1, (total + limit - 1) // limit)
    if pages > 1:
        page = offset // limit + 1
        pager = []
        if offset > 0:
            pager.append(InlineKeyboardButton(
                text="«", callback_data=f"admin_feedback_list:{max(0, offset - limit)}"))
        pager.append(InlineKeyboardButton(text=f"{page}/{pages}", callback_data="stub"))
        if offset + limit < total:
            pager.append(InlineKeyboardButton(
                text="»", callback_data=f"admin_feedback_list:{offset + limit}"))
        keyboard.append(pager)

    # Кнопки возврата
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin")])
    keyboard.append([InlineKeyboardButton(text=(await get_text("admin_panel_btn_admin_panel_alt", "ru") or "🛠️ Админпанель"), callback_data="admin")])

    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    end_no = min(offset + len(rows), total)
    list_header_tmpl = await get_text("admin_panel_feedback_list_header_tmpl", "ru") or "📬 <b>Сообщения от пользователей (новые сверху)</b>\nПоказаны {start}–{end} из {total}"
    header = list_header_tmpl.format(start=start_no, end=end_no, total=total)

    msg = await cb.message.answer(header, reply_markup=markup, parse_mode="HTML")
    last_bot_messages[cb.message.chat.id] = [msg.message_id]
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
    print(
        f"FUNC: send_admin_feedback_list | offset: {offset} | "
        f"chat_id: {cb.message.chat.id} | user_id: {cb.from_user.id} | rows: {len(rows)} | total: {total}"
    )


# ── Админ: «Неотвеченные» — подстраховка, отдельный фильтрованный список ──
#    Только обращения, где пользователь запросил ответ, а ответа ещё нет.
#    Старейшие — первыми (сначала отвечаем тем, кто ждёт дольше всех).
@router.callback_query(F.data == "admin_feedback_unanswered")
async def admin_feedback_unanswered_entry(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer(await get_text("err_no_access_short", "ru") or "Нет доступа", show_alert=True)
        return
    try:
        await cb.message.delete()
    except Exception:
        pass
    await send_admin_feedback_unanswered_list(cb, offset=0)


@router.callback_query(F.data.startswith("admin_feedback_unanswered_list"))
async def admin_feedback_unanswered_list(cb: CallbackQuery):
    data = cb.data.split(":")
    offset = int(data[1]) if len(data) > 1 and data[1].isdigit() else 0
    await send_admin_feedback_unanswered_list(cb, offset=offset)


async def send_admin_feedback_unanswered_list(cb: CallbackQuery, offset: int = 0):
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    limit = FEEDBACK_PAGE_SIZE

    async with SessionLocal() as session:
        total = (await session.execute(text(
            "SELECT COUNT(*) AS c FROM feedback WHERE needs_reply=1 AND answered_at IS NULL"
        ))).scalar_one()

        result = await session.execute(
            text(
                "SELECT id, username, created_at "
                "FROM feedback WHERE needs_reply=1 AND answered_at IS NULL "
                "ORDER BY created_at ASC, id ASC "
                "LIMIT :limit OFFSET :offset"
            ),
            {"limit": limit, "offset": offset}
        )
        rows = result.fetchall()

    keyboard = []

    if not rows and offset == 0:
        keyboard.append([InlineKeyboardButton(text=(await get_text("admin_panel_btn_back_to_feedback_list", "ru") or "◀️ К списку сообщений"), callback_data="admin_feedback")])
        keyboard.append([InlineKeyboardButton(text=(await get_text("admin_panel_btn_admin_panel_alt", "ru") or "🛠️ Админпанель"), callback_data="admin")])
        header = await get_text("admin_panel_feedback_unanswered_empty", "ru") or "🔔 <b>Неотвеченные сообщения пользователей</b>\n\n✅ Отличная работа — неотвеченных обращений нет."
    else:
        start_no = offset + 1
        for i, r in enumerate(rows):
            idx = offset + i + 1
            uname = f"@{r.username}" if r.username else (await get_text("admin_panel_no_username", "ru") or "Без ника")
            dt_str = _fmt_dt_belgrade(r.created_at)
            keyboard.append([InlineKeyboardButton(
                text=f"{idx}. 🔔 {uname} · {dt_str}",
                callback_data=f"admin_feedback_view:{r.id}"
            )])

        pages = max(1, (total + limit - 1) // limit)
        if pages > 1:
            page = offset // limit + 1
            pager = []
            if offset > 0:
                pager.append(InlineKeyboardButton(
                    text="«", callback_data=f"admin_feedback_unanswered_list:{max(0, offset - limit)}"))
            pager.append(InlineKeyboardButton(text=f"{page}/{pages}", callback_data="stub"))
            if offset + limit < total:
                pager.append(InlineKeyboardButton(
                    text="»", callback_data=f"admin_feedback_unanswered_list:{offset + limit}"))
            keyboard.append(pager)

        keyboard.append([InlineKeyboardButton(text=(await get_text("admin_panel_btn_back_to_feedback_list", "ru") or "◀️ К списку сообщений"), callback_data="admin_feedback")])
        keyboard.append([InlineKeyboardButton(text=(await get_text("admin_panel_btn_admin_panel_alt", "ru") or "🛠️ Админпанель"), callback_data="admin")])

        end_no = min(offset + len(rows), total)
        unanswered_header_tmpl = await get_text("admin_panel_feedback_unanswered_header_tmpl", "ru") or "🔔 <b>Неотвеченные сообщения пользователей (старые сверху)</b>\nПоказаны {start}–{end} из {total}"
        header = unanswered_header_tmpl.format(start=start_no, end=end_no, total=total)

    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    msg = await cb.message.answer(header, reply_markup=markup, parse_mode="HTML")
    last_bot_messages[cb.message.chat.id] = [msg.message_id]
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
    print(
        f"FUNC: send_admin_feedback_unanswered_list | offset: {offset} | "
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
        await cb.answer(await get_text("err_no_access_short", "ru") or "Нет доступа", show_alert=True)
        return

    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)

    fb_id = int(cb.data.split(":")[1])

    # 2) Читаем письмо + считаем позицию/соседей (новые сверху: created_at DESC, id DESC)
    async with SessionLocal() as session:
        res = await session.execute(
            text(
                "SELECT id, user_id, username, message, created_at, is_read, "
                "needs_reply, answered_at "
                "FROM feedback WHERE id = :id"
            ),
            {"id": fb_id}
        )
        row = res.fetchone()
        if not row:
            msg = await cb.message.answer(await get_text("admin_panel_feedback_not_found", "ru") or "Сообщение не найдено или удалено.")
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
    uname = f"@{row.username}" if row.username else (await get_text("admin_panel_no_username", "ru") or "Без ника")
    dt_val = row.created_at
    try:
        dt_val = datetime.fromisoformat(dt_val) if isinstance(dt_val, str) else dt_val
    except Exception:
        dt_val = utcnow_naive()
    dt_str = dt_val.strftime("%H:%M %d:%m:%y")

    if row.answered_at:
        status_line = await get_text("admin_panel_feedback_status_answered", "ru") or "✅ <b>Отвечено</b>\n"
    elif row.needs_reply:
        status_line = await get_text("admin_panel_feedback_status_needs_reply", "ru") or "🔔 <b>Пользователь запросил ответ</b>\n"
    else:
        status_line = ""
    view_header_tmpl = await get_text("admin_panel_feedback_view_header_tmpl", "ru") or (
        "📨 <b>Обратная связь</b>  <i>[{pos}/{total}]</i>\n"
        "{status_line}<b>От:</b> {uname}\n"
        "<b>Дата:</b> {dt}\n"
    )
    header = view_header_tmpl.format(pos=pos, total=total, status_line=status_line, uname=uname, dt=dt_str)
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
    rows.append([InlineKeyboardButton(text=(await get_text("admin_panel_btn_reply", "ru") or "✍️ Ответить"), callback_data=f"fb:reply:{row.id}")])
    rows.append([
        InlineKeyboardButton(text=(await get_text("btn_delete", "ru") or "🗑 Удалить"), callback_data=f"admin_feedback_delete:{row.id}"),
        InlineKeyboardButton(text=(await get_text("admin_panel_btn_back_to_list", "ru") or "↩️ К списку"), callback_data=f"admin_feedback_list:{back_offset}")
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
        await cb.answer(await get_text("err_no_access_short", "ru") or "Нет доступа", show_alert=True)
        return

    fb_id = int(cb.data.split(":")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=(await get_text("btn_cancel", "ru") or "❌ Отмена"), callback_data=f"admin_feedback_view:{fb_id}")],
        [InlineKeyboardButton(text=(await get_text("admin_panel_btn_delete_forever", "ru") or "✅ Удалить навсегда"), callback_data=f"admin_feedback_delete_yes:{fb_id}")]
    ])
    try:
        await cb.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass

    await cb.answer(await get_text("admin_panel_feedback_delete_confirm_toast", "ru") or "Удалить это сообщение?")
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
        await cb.answer(await get_text("err_no_access_short", "ru") or "Нет доступа", show_alert=True)
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
        await cb.answer(await get_text("err_no_access", "ru") or "Нет доступа.", show_alert=True)
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
        await cb.answer(await get_text("admin_panel_users_empty", "ru") or "Список пользователей пуст.", show_alert=True)
        return

    users_header_tmpl = await get_text("admin_panel_users_header_tmpl", "ru") or "<b>👥 Пользователи бота</b> (всего: {total})"
    lines = [users_header_tmpl.format(total=total)]
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
            InlineKeyboardButton(text=(await get_text("admin_panel_btn_forward", "ru") or "Вперёд ▶️"), callback_data=f"admin:users:{offset + USERS_PAGE_SIZE}")
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[nav_buttons] if nav_buttons else [])
    kb.inline_keyboard.append([InlineKeyboardButton(text=(await get_text("admin_panel_btn_admin_panel", "ru") or "🛠 Админ-панель"), callback_data="admin")])

    await clear_bot_messages(cb.message.chat.id, cb.bot)
    msg = await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
    await cb.answer()
