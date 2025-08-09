from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
import json, inspect, re

from app.database import SessionLocal
from app.models import Category
from app.states import AdminFieldStates
from app.routers.utils import clear_bot_messages, last_bot_messages

# берём проверку админа из admin_panel (важно: admin_panel НЕ должен импортировать этот файл)
from app.routers.admin_panel import is_admin

router = Router()

ROOT_CATEGORY_IDS = {30, 80}

# --- утилиты чтения/записи JSON-полей в Category.fields ---
async def load_category_fields(session, cat_id: int) -> list[dict]:
    cat = (await session.execute(select(Category).where(Category.id == cat_id))).scalar_one()
    try:
        val = (cat.fields or "").strip()
        data = json.loads(val) if val else []
        return data if isinstance(data, list) else []
    except Exception:
        return []

async def save_category_fields(session, cat_id: int, fields: list[dict]) -> None:
    cat = (await session.execute(select(Category).where(Category.id == cat_id))).scalar_one()
    cat.fields = json.dumps(fields, ensure_ascii=False)
    session.add(cat)
    await session.commit()

# УНИКАЛЬНОСТЬ КЛЮЧА В КАТЕГОРИИ
async def field_key_exists(session, cat_id: int, key: str) -> bool:
    key_l = (key or "").strip().lower()
    fields = await load_category_fields(session, cat_id)
    return any(isinstance(f, dict) and str(f.get("key", "")).lower() == key_l for f in fields)


# ====== Админ: Поля категории — меню ======
@router.callback_query(F.data.regexp(r"^admin:fields:(\d+)$"))
async def admin_fields_menu(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return

    chat_id = cb.message.chat.id
    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    cat_id = int(re.match(r"^admin:fields:(\d+)$", cb.data).group(1))
    if cat_id in ROOT_CATEGORY_IDS:
        await cb.answer("Для корневых разделов поля не настраиваются.", show_alert=True)
        # назад в меню категории
        fake = CallbackQuery.model_construct(
            id="fake", from_user=cb.from_user, chat_instance=cb.chat_instance,
            message=cb.message, data=f"admin:edit_category:{cat_id}"
        )
        from app.routers.admin_panel import admin_edit_subcategories_cb
        await admin_edit_subcategories_cb(fake, state)
        print(f"FUNC: {inspect.currentframe().f_code.co_name} | step: root_forbidden | chat_id: {chat_id} | user_id: {cb.from_user.id} | cat_id: {cat_id}")
        return

    async with SessionLocal() as s:
        category = (await s.execute(select(Category).where(Category.id == cat_id))).scalar_one()
        fields = await load_category_fields(s, cat_id)

    rows = []
    if fields:
        for i, fld in enumerate(fields, start=1):
            title = (fld.get("label") or fld.get("name") or f"Поле {i}") if isinstance(fld, dict) else f"Поле {i}"
            rows.append([InlineKeyboardButton(text=f"• {title}", callback_data=f"admin:field_edit:{cat_id}:{i-1}")])
    else:
        rows.append([InlineKeyboardButton(text="— Полей пока нет —", callback_data="noop")])

    rows.append([InlineKeyboardButton(text="✚ Добавить поле", callback_data=f"admin:fields:add:{cat_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:edit_category:{cat_id}")])

    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    msg = await cb.message.answer(
        f"⚙️ <b>Поля категории</b>\n<b>{category.name}</b>\n\nВыберите поле или действие.",
        reply_markup=markup, parse_mode="HTML"
    )
    last_bot_messages[chat_id] = [msg.message_id]

    print(f"FUNC: {inspect.currentframe().f_code.co_name} | step: show_fields_menu | chat_id: {chat_id} | user_id: {cb.from_user.id} | cat_id: {cat_id} | fields_count: {len(fields)} | msg_id: {msg.message_id}")

# ====== Админ: Поля — старт добавления ======
@router.callback_query(F.data.regexp(r"^admin:fields:add:(\d+)$"))
async def admin_fields_add_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return

    await clear_bot_messages(cb.message.chat.id, cb.bot)
    cat_id = int(re.match(r"^admin:fields:add:(\d+)$", cb.data).group(1))
    await state.update_data(field_cat_id=cat_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Текст",   callback_data="admin:field_type:text"),
         InlineKeyboardButton(text="🔢 Число",   callback_data="admin:field_type:number")],
        [InlineKeyboardButton(text="📋 Список",  callback_data="admin:field_type:select"),
         InlineKeyboardButton(text="☑️ Чекбокс", callback_data="admin:field_type:checkbox")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:fields:{cat_id}")]
    ])
    msg = await cb.message.answer("➕ <b>Новое поле</b>\nВыберите <b>тип</b> поля:", reply_markup=kb, parse_mode="HTML")
    last_bot_messages[cb.message.chat.id] = [msg.message_id]
    await state.set_state(AdminFieldStates.choosing_type)

    print(f"FUNC: {inspect.currentframe().f_code.co_name} | cat_id: {cat_id} | chat_id: {cb.message.chat.id} | user_id: {cb.from_user.id} | msg_id: {msg.message_id}")

# ====== Админ: меню выбранного поля ======
@router.callback_query(F.data.startswith("admin:field_edit:"))
async def admin_field_edit_menu(cb: CallbackQuery):
    # Заголовок: меню действий для конкретного поля
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return

    chat_id = cb.message.chat.id
    try:
        _, _, cat_id_s, idx_s = cb.data.split(":")
        cat_id = int(cat_id_s); idx = int(idx_s)
    except Exception:
        await cb.answer("Неверные данные", show_alert=True); return

    await clear_bot_messages(chat_id, cb.bot)

    async with SessionLocal() as s:
        category = (await s.execute(select(Category).where(Category.id == cat_id))).scalar_one()
        fields = await load_category_fields(s, cat_id)

    if not isinstance(fields, list) or idx < 0 or idx >= len(fields):
        # вернёмся к списку полей
        await cb.answer("Поле не найдено", show_alert=True)
        fake = CallbackQuery(id="fake", from_user=cb.from_user, chat_instance=cb.chat_instance, message=cb.message, data=f"admin:fields:{cat_id}")
        await admin_fields_menu(fake, None)
        return

    f = fields[idx] if isinstance(fields[idx], dict) else {}
    ftype = f.get("type", "text")
    label = f.get("label", "(без названия)")
    key = f.get("key", "field")
    req = "✅ Обязательное" if f.get("required") else "❌ Необязательное"
    opts = ", ".join(f.get("options", [])) if ftype == "select" else "—"

    text = (
        f"⚙️ <b>Поле</b> — <b>{label}</b>\n"
        f"<b>Тип:</b> {ftype}\n"
        f"<b>Ключ:</b> <code>{key}</code>\n"
        f"<b>Обязательность:</b> {req}\n"
        f"<b>Варианты:</b> {opts}"
    )

    rows = [
        [InlineKeyboardButton(text="✏️ Заголовок", callback_data=f"admin:field_edit_label:{cat_id}:{idx}"),
         InlineKeyboardButton(text="🔑 Ключ",      callback_data=f"admin:field_edit_key:{cat_id}:{idx}")]
    ]
    if ftype == "select":
        rows.append([InlineKeyboardButton(text="📋 Варианты (select)", callback_data=f"admin:field_edit_options:{cat_id}:{idx}")])
    rows.append([InlineKeyboardButton(text=("❎ Сделать необязательным" if f.get("required") else "✅ Сделать обязательным"),
                                      callback_data=f"admin:field_toggle_required:{cat_id}:{idx}")])
    rows.append([InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"admin:field_delete_confirm:{cat_id}:{idx}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:fields:{cat_id}")])

    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    msg = await cb.message.answer(text, reply_markup=markup, parse_mode="HTML")
    last_bot_messages[chat_id] = [msg.message_id]

    import inspect
    print(f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | user_id: {cb.from_user.id} | cat_id: {cat_id} | idx: {idx} | msg_id: {msg.message_id}")


# ====== Админ: переключить обязательность ======
@router.callback_query(F.data.startswith("admin:field_toggle_required:"))
async def admin_field_toggle_required(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return

    chat_id = cb.message.chat.id
    _, _, cat_id_s, idx_s = cb.data.split(":")
    cat_id = int(cat_id_s); idx = int(idx_s)

    await clear_bot_messages(chat_id, cb.bot)

    async with SessionLocal() as s:
        fields = await load_category_fields(s, cat_id)
        if not isinstance(fields, list) or idx < 0 or idx >= len(fields):
            await cb.answer("Поле не найдено", show_alert=True)
            fake = CallbackQuery(id="fake", from_user=cb.from_user, chat_instance=cb.chat_instance, message=cb.message, data=f"admin:fields:{cat_id}")
            await admin_fields_menu(fake, None)
            return
        f = fields[idx]
        f["required"] = not bool(f.get("required"))
        await save_category_fields(s, cat_id, fields)

    # вернёмся в меню поля
    fake = CallbackQuery(id="fake", from_user=cb.from_user, chat_instance=cb.chat_instance, message=cb.message,
                         data=f"admin:field_edit:{cat_id}:{idx}")
    await admin_field_edit_menu(fake)
    import inspect
    print(f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | user_id: {cb.from_user.id} | cat_id: {cat_id} | idx: {idx}")


# ====== Админ: подтвердить удаление поля ======
@router.callback_query(F.data.startswith("admin:field_delete_confirm:"))
async def admin_field_delete_confirm(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return

    chat_id = cb.message.chat.id
    _, _, cat_id_s, idx_s = cb.data.split(":")
    cat_id = int(cat_id_s); idx = int(idx_s)

    await clear_bot_messages(chat_id, cb.bot)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:field_edit:{cat_id}:{idx}")],
        [InlineKeyboardButton(text="✅ Удалить", callback_data=f"admin:field_delete_yes:{cat_id}:{idx}")]
    ])
    msg = await cb.message.answer("Удалить это поле безвозвратно?", reply_markup=kb)
    last_bot_messages[chat_id] = [msg.message_id]

    import inspect
    print(f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | cat_id: {cat_id} | idx: {idx} | msg_id: {msg.message_id}")


# ====== Админ: удалить поле ======
@router.callback_query(F.data.startswith("admin:field_delete_yes:"))
async def admin_field_delete_yes(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return

    chat_id = cb.message.chat.id
    _, _, cat_id_s, idx_s = cb.data.split(":")
    cat_id = int(cat_id_s); idx = int(idx_s)

    await clear_bot_messages(chat_id, cb.bot)

    async with SessionLocal() as s:
        fields = await load_category_fields(s, cat_id)
        if isinstance(fields, list) and 0 <= idx < len(fields):
            removed = fields.pop(idx)
            await save_category_fields(s, cat_id, fields)
        else:
            removed = None

    # назад к списку полей
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ОК", callback_data=f"admin:fields:{cat_id}")]
    ])
    msg = await cb.message.answer("🗑️ Поле удалено." if removed else "Поле не найдено.", reply_markup=kb)
    last_bot_messages[chat_id] = [msg.message_id]

    import inspect
    print(f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | user_id: {cb.from_user.id} | cat_id: {cat_id} | idx: {idx} | removed: {bool(removed)} | msg_id: {msg.message_id}")


# ====== Админ: редактировать заголовок (шаг 1) ======
@router.callback_query(F.data.startswith("admin:field_edit_label:"))
async def admin_field_edit_label_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return

    chat_id = cb.message.chat.id
    _, _, cat_id_s, idx_s = cb.data.split(":")
    cat_id = int(cat_id_s); idx = int(idx_s)

    await clear_bot_messages(chat_id, cb.bot)
    await state.update_data(edit_cat_id=cat_id, edit_idx=idx)

    # достаём текущий label
    async with SessionLocal() as s:
        fields = await load_category_fields(s, cat_id)
    old_label = ""
    if isinstance(fields, list) and 0 <= idx < len(fields) and isinstance(fields[idx], dict):
        old_label = str(fields[idx].get("label", ""))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:field_edit:{cat_id}:{idx}")]
    ])
    msg = await cb.message.answer(
        f"Текущий заголовок: <b>{old_label or '(пусто)'}</b>\n\n"
        f"Введите новый <b>заголовок</b> поля:",
        reply_markup=kb, parse_mode="HTML"
    )
    last_bot_messages[chat_id] = [msg.message_id]
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | "
        f"cat_id: {cat_id} | idx: {idx} | old_label: {old_label} | msg_id: {msg.message_id}"
    )


@router.message(AdminFieldStates.editing_label)
async def admin_field_edit_label_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа."); return

    await clear_bot_messages(message.chat.id, message.bot)
    data = await state.get_data()
    cat_id = int(data.get("edit_cat_id")); idx = int(data.get("edit_idx"))
    new_label = (message.text or "").strip()

    async with SessionLocal() as s:
        fields = await load_category_fields(s, cat_id)
        if isinstance(fields, list) and 0 <= idx < len(fields) and isinstance(fields[idx], dict):
            fields[idx]["label"] = new_label
            await save_category_fields(s, cat_id, fields)

    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="ОК", callback_data=f"admin:field_edit:{cat_id}:{idx}")]])
    msg = await message.answer("✅ Заголовок обновлён.", reply_markup=kb)
    last_bot_messages[message.chat.id] = [msg.message_id]

    import inspect
    print(f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {message.chat.id} | cat_id: {cat_id} | idx: {idx} | msg_id: {msg.message_id}")


# ====== Админ: редактировать ключ (шаг 1) ======
@router.callback_query(F.data.startswith("admin:field_edit_key:"))
async def admin_field_edit_key_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return

    chat_id = cb.message.chat.id
    _, _, cat_id_s, idx_s = cb.data.split(":")
    cat_id = int(cat_id_s); idx = int(idx_s)

    await clear_bot_messages(chat_id, cb.bot)
    await state.update_data(edit_cat_id=cat_id, edit_idx=idx)

    # достаём текущий key
    async with SessionLocal() as s:
        fields = await load_category_fields(s, cat_id)
    old_key = ""
    if isinstance(fields, list) and 0 <= idx < len(fields) and isinstance(fields[idx], dict):
        old_key = str(fields[idx].get("key", ""))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:field_edit:{cat_id}:{idx}")]
    ])
    msg = await cb.message.answer(
        f"Текущий ключ: <code>{old_key or '(пусто)'}</code>\n\n"
        f"Введите новый <b>ключ</b> (латиница/цифры/_):",
        reply_markup=kb, parse_mode="HTML"
    )
    last_bot_messages[chat_id] = [msg.message_id]
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | "
        f"cat_id: {cat_id} | idx: {idx} | old_key: {old_key} | msg_id: {msg.message_id}"
    )

# ====== Админ: Проверка уникальности ключа (разрешаем «старый» ключ у самого поля) ======
@router.message(AdminFieldStates.editing_key)
async def admin_field_edit_key_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа."); return

    await clear_bot_messages(message.chat.id, message.bot)
    import re
    key = (message.text or "").strip().lower()
    if not re.fullmatch(r'[a-z0-9_]+', key):
        msg = await message.answer("❗️Только латиница/цифры/нижнее подчёркивание. Введите снова.")
        last_bot_messages[message.chat.id] = [msg.message_id]
        print(
            f"FUNC: {inspect.currentframe().f_code.co_name} | step: bad_key | "
            f"chat_id: {message.chat.id} | key: {key} | msg_id: {msg.message_id}"
        )
        return

    data = await state.get_data()
    cat_id = int(data.get("edit_cat_id")); idx = int(data.get("edit_idx"))

    async with SessionLocal() as s:
        fields = await load_category_fields(s, cat_id)
        # проверяем дубликаты среди ДРУГИХ полей этой категории
        duplicate = any(
            i != idx and isinstance(f, dict) and str(f.get("key", "")).lower() == key
            for i, f in enumerate(fields)
        )
        if duplicate:
            msg = await message.answer("❗️Ключ уже используется в этой категории. Введите другой.")
            last_bot_messages[message.chat.id] = [msg.message_id]
            print(
                f"FUNC: {inspect.currentframe().f_code.co_name} | step: key_duplicate | "
                f"chat_id: {message.chat.id} | cat_id: {cat_id} | key: {key} | msg_id: {msg.message_id}"
            )
            return

        if isinstance(fields, list) and 0 <= idx < len(fields) and isinstance(fields[idx], dict):
            fields[idx]["key"] = key
            await save_category_fields(s, cat_id, fields)

    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="ОК", callback_data=f"admin:field_edit:{cat_id}:{idx}")]])
    msg = await message.answer("✅ Ключ обновлён.", reply_markup=kb)
    last_bot_messages[message.chat.id] = [msg.message_id]

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {message.chat.id} | "
        f"cat_id: {cat_id} | idx: {idx} | new_key: {key} | msg_id: {msg.message_id}"
    )

# ====== Админ: редактировать варианты (select) ======
@router.callback_query(F.data.startswith("admin:field_edit_options:"))
async def admin_field_edit_options_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return

    chat_id = cb.message.chat.id
    _, _, cat_id_s, idx_s = cb.data.split(":")
    cat_id = int(cat_id_s); idx = int(idx_s)

    await clear_bot_messages(chat_id, cb.bot)
    await state.update_data(edit_cat_id=cat_id, edit_idx=idx)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:field_edit:{cat_id}:{idx}")]
    ])
    msg = await cb.message.answer("Введите варианты через запятую:\n<code>Опция 1, Опция 2, ...</code>", reply_markup=kb, parse_mode="HTML")
    last_bot_messages[chat_id] = [msg.message_id]
    await state.set_state(AdminFieldStates.editing_options)

    import inspect
    print(f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | cat_id: {cat_id} | idx: {idx} | msg_id: {msg.message_id}")


@router.message(AdminFieldStates.editing_options)
async def admin_field_edit_options_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа."); return

    await clear_bot_messages(message.chat.id, message.bot)
    data = await state.get_data()
    cat_id = int(data.get("edit_cat_id")); idx = int(data.get("edit_idx"))
    opts = [o.strip() for o in (message.text or "").split(",") if o.strip()]

    async with SessionLocal() as s:
        fields = await load_category_fields(s, cat_id)
        if isinstance(fields, list) and 0 <= idx < len(fields) and isinstance(fields[idx], dict):
            fields[idx]["options"] = opts
            await save_category_fields(s, cat_id, fields)

    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="ОК", callback_data=f"admin:field_edit:{cat_id}:{idx}")]])
    msg = await message.answer("✅ Варианты обновлены.", reply_markup=kb)
    last_bot_messages[message.chat.id] = [msg.message_id]

    import inspect
    print(f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {message.chat.id} | cat_id: {cat_id} | idx: {idx} | msg_id: {msg.message_id}")



# ====== Админ: тип поля ======
@router.callback_query(F.data.regexp(r"^admin:field_type:(text|number|select|checkbox)$"), AdminFieldStates.choosing_type)
async def admin_field_pick_type(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return

    await clear_bot_messages(cb.message.chat.id, cb.bot)
    ftype = cb.data.split(":")[-1]
    await state.update_data(field_type=ftype)
    cat_id = (await state.get_data()).get("field_cat_id")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:fields:add:{cat_id}")]
    ])
    msg = await cb.message.answer("Введите <b>заголовок</b> поля (например: <i>Модель</i>)", reply_markup=kb, parse_mode="HTML")
    last_bot_messages[cb.message.chat.id] = [msg.message_id]
    await state.set_state(AdminFieldStates.waiting_label)

    print(f"FUNC: {inspect.currentframe().f_code.co_name} | type: {ftype} | chat_id: {cb.message.chat.id} | user_id: {cb.from_user.id} | msg_id: {msg.message_id}")

# ====== Админ: ввод заголовка ======
@router.message(AdminFieldStates.waiting_label)
async def admin_field_label(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа."); return

    await clear_bot_messages(message.chat.id, message.bot)
    label = message.text.strip()
    await state.update_data(field_label=label)

    key_suggest = re.sub(r'[^a-z0-9_]+', '_', label.lower()).strip("_") or "field"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⬇️ Оставить ключ: {key_suggest}", callback_data=f"admin:field_keepkey:{key_suggest}")]
    ])
    msg = await message.answer("Введите <b>ключ</b> (латиница/цифры/_)\nили нажмите кнопку ниже.", reply_markup=kb, parse_mode="HTML")
    last_bot_messages[message.chat.id] = [msg.message_id]
    await state.set_state(AdminFieldStates.waiting_key)

    print(f"FUNC: {inspect.currentframe().f_code.co_name} | label: {label} | key_suggest: {key_suggest} | chat_id: {message.chat.id} | user_id: {message.from_user.id} | msg_id: {msg.message_id}")

# ====== Админ: оставить сгенерированный ключ ======
@router.callback_query(F.data.startswith("admin:field_keepkey:"), AdminFieldStates.waiting_key)
async def admin_field_keep_key(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return

    await clear_bot_messages(cb.message.chat.id, cb.bot)
    key = cb.data.split(":")[-1]
    data = await state.get_data()
    cat_id = data.get("field_cat_id")

    # проверка дубликата
    async with SessionLocal() as s:
        if await field_key_exists(s, cat_id, key):
            msg = await cb.message.answer("❗️Ключ уже используется в этой категории. Введите другой.")
            last_bot_messages[cb.message.chat.id] = [msg.message_id]
            # остаёмся в состоянии waiting_key
            import inspect
            print(
                f"FUNC: {inspect.currentframe().f_code.co_name} | step: key_duplicate | "
                f"cat_id: {cat_id} | key: {key} | chat_id: {cb.message.chat.id} | user_id: {cb.from_user.id} | "
                f"msg_id: {msg.message_id}"
            )
            return

    await state.update_data(field_key=key)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Обязательно", callback_data="admin:field_required:1"),
         InlineKeyboardButton(text="❌ Необязательно", callback_data="admin:field_required:0")]
    ])
    msg = await cb.message.answer("Поле обязательное?", reply_markup=kb)
    last_bot_messages[cb.message.chat.id] = [msg.message_id]
    await state.set_state(AdminFieldStates.waiting_required)

    import inspect
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | step: ok | "
        f"cat_id: {cat_id} | key: {key} | chat_id: {cb.message.chat.id} | user_id: {cb.from_user.id} | "
        f"msg_id: {msg.message_id}"
    )

# ====== Админ: ввод ключа вручную ======
@router.message(AdminFieldStates.waiting_key)
async def admin_field_key(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа."); return

    await clear_bot_messages(message.chat.id, message.bot)

    import re
    key = (message.text or "").strip().lower()
    if not re.fullmatch(r'[a-z0-9_]+', key):
        msg = await message.answer("❗️Ключ: только латиница/цифры/нижнее подчёркивание. Введите снова.")
        last_bot_messages[message.chat.id] = [msg.message_id]
        import inspect
        print(
            f"FUNC: {inspect.currentframe().f_code.co_name} | step: bad_key | "
            f"key: {key} | chat_id: {message.chat.id} | user_id: {message.from_user.id} | msg_id: {msg.message_id}"
        )
        return

    data = await state.get_data()
    cat_id = data.get("field_cat_id")

    # проверка дубликата
    async with SessionLocal() as s:
        if await field_key_exists(s, cat_id, key):
            msg = await message.answer("❗️Ключ уже используется в этой категории. Введите другой ключ.")
            last_bot_messages[message.chat.id] = [msg.message_id]
            import inspect
            print(
                f"FUNC: {inspect.currentframe().f_code.co_name} | step: key_duplicate | "
                f"cat_id: {cat_id} | key: {key} | chat_id: {message.chat.id} | user_id: {message.from_user.id} | "
                f"msg_id: {msg.message_id}"
            )
            return

    await state.update_data(field_key=key)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Обязательно", callback_data="admin:field_required:1"),
         InlineKeyboardButton(text="❌ Необязательно", callback_data="admin:field_required:0")]
    ])
    msg = await message.answer("Поле обязательное?", reply_markup=kb)
    last_bot_messages[message.chat.id] = [msg.message_id]
    await state.set_state(AdminFieldStates.waiting_required)

    import inspect
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | step: ok | "
        f"cat_id: {cat_id} | key: {key} | chat_id: {message.chat.id} | user_id: {message.from_user.id} | "
        f"msg_id: {msg.message_id}"
    )

# ====== Админ: обязательность поля ======
@router.callback_query(F.data.regexp(r"^admin:field_required:(1|0)$"), AdminFieldStates.waiting_required)
async def admin_field_required(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return

    await clear_bot_messages(cb.message.chat.id, cb.bot)
    required = cb.data.endswith(":1")
    await state.update_data(field_required=required)

    data = await state.get_data()
    if data.get("field_type") == "select":
        msg = await cb.message.answer("Укажите варианты через запятую:\n<code>Sony, Yamaha, AKG</code>", parse_mode="HTML")
        last_bot_messages[cb.message.chat.id] = [msg.message_id]
        await state.set_state(AdminFieldStates.waiting_options)
    else:
        await persist_field_and_back(cb, state)

    print(f"FUNC: {inspect.currentframe().f_code.co_name} | required: {required} | chat_id: {cb.message.chat.id} | user_id: {cb.from_user.id}")

# ====== Админ: варианты для select ======
@router.message(AdminFieldStates.waiting_options)
async def admin_field_options(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа."); return

    await clear_bot_messages(message.chat.id, message.bot)
    options = [o.strip() for o in message.text.split(",") if o.strip()]
    await state.update_data(field_options=options)
    await persist_field_and_back(message, state)

    print(f"FUNC: {inspect.currentframe().f_code.co_name} | options_count: {len(options)} | chat_id: {message.chat.id} | user_id: {message.from_user.id}")

# --- сохранить поле и вернуться к списку ---
async def persist_field_and_back(cb_or_msg, state: FSMContext):
    if isinstance(cb_or_msg, CallbackQuery):
        chat_id = cb_or_msg.message.chat.id; bot = cb_or_msg.message.bot; send = cb_or_msg.message.answer; user_id = cb_or_msg.from_user.id
    else:
        chat_id = cb_or_msg.chat.id; bot = cb_or_msg.bot; send = cb_or_msg.answer; user_id = cb_or_msg.from_user.id

    await clear_bot_messages(chat_id, bot)

    data = await state.get_data()
    cat_id = data["field_cat_id"]
    field = {
        "type": data.get("field_type", "text"),
        "label": data.get("field_label", ""),
        "key": data.get("field_key", ""),
        "required": bool(data.get("field_required", False)),
    }
    if field["type"] == "select":
        field["options"] = data.get("field_options", [])

    async with SessionLocal() as s:
        fields = await load_category_fields(s, cat_id)
        if not isinstance(fields, list): fields = []
        field["key"] = str(field["key"]).strip().lower()
        # защита от дубликатов (на случай гонки)
        if any(isinstance(f, dict) and str(f.get("key", "")).lower() == field["key"] for f in fields):
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад к полям", callback_data=f"admin:fields:{cat_id}")]
            ])
            msg = await send("❗️Ключ уже используется в этой категории.\nВведите другой ключ (латиница/цифры/_):", reply_markup=kb)
            last_bot_messages[chat_id] = [msg.message_id]
            await state.set_state(AdminFieldStates.waiting_key)
            print(
                f"FUNC: {inspect.currentframe().f_code.co_name} | step: key_duplicate | "
                f"cat_id: {cat_id} | chat_id: {chat_id} | msg_id: {msg.message_id}"
            )
            return
        fields.append(field)
        await save_category_fields(s, cat_id, fields)


    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="ОК", callback_data=f"admin:fields:{cat_id}")]])
    msg = await send(
        f"✅ Поле добавлено:\n<b>{field['label']}</b> [{field['type']}] (key: <code>{field['key']}</code>)",
        reply_markup=kb, parse_mode="HTML"
    )
    last_bot_messages[chat_id] = [msg.message_id]

    print(f"FUNC: {inspect.currentframe().f_code.co_name} | saved_field: {field} | cat_id: {cat_id} | chat_id: {chat_id} | user_id: {user_id} | msg_id: {msg.message_id}")
