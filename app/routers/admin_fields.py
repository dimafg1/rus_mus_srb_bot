from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
import json, inspect, re

from app.database import SessionLocal
from app.models import Category
from app.states import AdminFieldStates
from app.routers.utils import clear_bot_messages, last_bot_messages, register_bot_messages, get_text

# берём проверку админа из admin_panel (важно: admin_panel НЕ должен импортировать этот файл)
from app.routers.admin_panel import is_admin
from app.keyboards import get_common_menu_button
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

# >>> BEGIN: extras helpers for allow_extra_categories flag
def _get_allow_extra_flag_from_fields_raw(raw: str) -> bool:
    """RU: Читаем флаг разрешения доп. категорий из Category.fields.
    Поддерживаем 2 формата:
    1) список полей с мета-записью {"type":"__meta","key":"allow_extra_categories","value": true}
    2) короткая форма {"allow_extra_categories": true}
    """
    try:
        text = (raw or "").strip()
        if not text:
            return False
        data = json.loads(text)
        if isinstance(data, list):
            for f in data:
                if isinstance(f, dict) and f.get("type") == "__meta" and f.get("key") == "allow_extra_categories":
                    return bool(f.get("value"))
            return False
        if isinstance(data, dict):
            return bool(data.get("allow_extra_categories"))
        return False
    except Exception:
        return False

async def _set_allow_extra_flag(session, cat_id: int, value: bool) -> None:
    """RU: Сохраняем флаг в формате «мета-записи» (список). Короткую форму не используем на запись."""
    cat = (await session.execute(select(Category).where(Category.id == cat_id))).scalar_one()
    try:
        data = json.loads((cat.fields or "").strip() or "[]")
    except Exception:
        data = []
    if not isinstance(data, list):
        data = []

    idx = None
    for i, f in enumerate(data):
        if isinstance(f, dict) and f.get("type") == "__meta" and f.get("key") == "allow_extra_categories":
            idx = i
            break
    if idx is None:
        data.insert(0, {"type": "__meta", "key": "allow_extra_categories", "value": bool(value)})
    else:
        data[idx]["value"] = bool(value)

    cat.fields = json.dumps(data, ensure_ascii=False)
    session.add(cat)
    await session.commit()
# <<< END: extras helpers



# УНИКАЛЬНОСТЬ КЛЮЧА В КАТЕГОРИИ
async def field_key_exists(session, cat_id: int, key: str) -> bool:
    key_l = (key or "").strip().lower()
    fields = await load_category_fields(session, cat_id)
    return any(isinstance(f, dict) and str(f.get("key", "")).lower() == key_l for f in fields)


# ====== Админ: Поля категории — меню (стрелки справа) ======
@router.callback_query(F.data.regexp(r"^admin:fields:(\d+)$"))
async def admin_fields_menu(cb: CallbackQuery, state: FSMContext, cat_id: int | None = None):
    # RU: Меню «Доп. поля категории». Поддерживает прямой вызов с cat_id,
    #     чтобы перерисовывать экран без "фейкового" CallbackQuery.
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return

    chat_id = cb.message.chat.id
    if cat_id is None:
        try:
            cat_id = int(re.match(r"^admin:fields:(\d+)$", (cb.data or "")).group(1))
        except Exception:
            await cb.answer(await get_text("err_bad_data", "ru") or "Неверные данные", show_alert=True); return


    # подчистка
    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    async with SessionLocal() as s:
        category = (await s.execute(select(Category).where(Category.id == cat_id))).scalar_one()
        fields = await load_category_fields(s, cat_id)

    # Статус + явное действие в одной строке
    allow_flag = _get_allow_extra_flag_from_fields_raw(category.fields)
    rows: list[list[InlineKeyboardButton]] = [[
        InlineKeyboardButton(
            text=f"Доп.категории: {'включены' if allow_flag else 'выключены'}",
            callback_data="noop"  # это просто индикатор статуса
        ),
        InlineKeyboardButton(
            text=("❌ Выключить" if allow_flag else "✅ Включить"),
            callback_data=f"admin:fields:toggle_extra:{cat_id}:{0 if allow_flag else 1}"
        ),
    ]]
    # разделитель
    rows.append([InlineKeyboardButton(text="———————————————", callback_data="noop")])



    # rows: list[list[InlineKeyboardButton]] = []

    # RU: строим список ТОЛЬКО по видимым полям (без мета),
    #     но в callback передаем РЕАЛЬНЫЕ индексы исходного массива.
    if fields:
        # реальные индексы видимых полей
        visible_idxs: list[int] = []
        for i, f in enumerate(fields):
            if isinstance(f, dict) and str((f.get("type") or "text")).startswith("__"):
                continue
            visible_idxs.append(i)

        if not visible_idxs:
            rows.append([InlineKeyboardButton(text="— Полей пока нет —", callback_data="noop")])
        else:
            for pos, idx in enumerate(visible_idxs):
                fld = fields[idx]
                if isinstance(fld, dict):
                    ftype = (fld.get("type") or "text")
                    required_star = "★ " if fld.get("required") else ""
                    label = (fld.get("label") or fld.get("name") or f"Поле {pos+1}")
                else:
                    ftype, required_star, label = "text", "", f"Поле {pos+1}"

                title = f"{required_star}{pos+1}. {label} ({ftype})"

                line: list[InlineKeyboardButton] = [
                    InlineKeyboardButton(text=title, callback_data=f"admin:field_edit:{cat_id}:{idx}")
                ]
                # стрелки только если есть сосед среди ВИДИМЫХ
                if pos > 0:
                    line.append(InlineKeyboardButton(text="⬆️", callback_data=f"admin:field_move:{cat_id}:{idx}:up"))
                if pos < len(visible_idxs) - 1:
                    line.append(InlineKeyboardButton(text="⬇️", callback_data=f"admin:field_move:{cat_id}:{idx}:down"))

                rows.append(line)
    else:
        rows.append([InlineKeyboardButton(text="— Полей пока нет —", callback_data="noop")])



    rows.append([InlineKeyboardButton(text="✚ Добавить поле", callback_data=f"admin:fields:add:{cat_id}")])
    back_btn = await get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:edit_category:{cat_id}")
    back_btn.callback_data = f"admin:edit_category:{cat_id}"
    rows.append([back_btn])

    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    msg = await cb.message.answer(
        f"⚙️ <b>Дополнительные поля категории</b>\n<b>{category.name}</b>\n\nВыберите поле или действие.",
        reply_markup=markup, parse_mode="HTML"
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"step: show_fields_menu | chat_id: {chat_id} | user_id: {cb.from_user.id} | "
        f"cat_id: {cat_id} | fields_count: {len(fields)} | msg_id: {msg.message_id}"
    )

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
        [InlineKeyboardButton(text="🎬 Видео",   callback_data="admin:field_type:video")],
    ])
    back_btn = await get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:fields:{cat_id}")
    back_btn.callback_data = f"admin:fields:{cat_id}"
    kb.inline_keyboard.append([back_btn])
    msg = await cb.message.answer("➕ <b>Новое поле</b>\nВыберите <b>тип</b> поля:", reply_markup=kb, parse_mode="HTML")
    last_bot_messages[cb.message.chat.id] = [msg.message_id]
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
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
        await cb.answer(await get_text("err_bad_data", "ru") or "Неверные данные", show_alert=True); return

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
    back_btn = await get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:fields:{cat_id}")
    back_btn.callback_data = f"admin:fields:{cat_id}"
    rows.append([back_btn])

    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    msg = await cb.message.answer(text, reply_markup=markup, parse_mode="HTML")
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

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
    await register_bot_messages(chat_id, [msg.message_id])

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
    await register_bot_messages(chat_id, [msg.message_id])

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
    await state.set_state(AdminFieldStates.editing_label)


    # достаём текущий label
    async with SessionLocal() as s:
        fields = await load_category_fields(s, cat_id)
    old_label = ""
    if isinstance(fields, list) and 0 <= idx < len(fields) and isinstance(fields[idx], dict):
        old_label = str(fields[idx].get("label", ""))

    back_btn = await get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:field_edit:{cat_id}:{idx}")
    back_btn.callback_data = f"admin:field_edit:{cat_id}:{idx}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [back_btn]
    ])
    msg = await cb.message.answer(
        f"Текущий заголовок: <b>{old_label or '(пусто)'}</b>\n\n"
        f"Введите новый <b>заголовок</b> поля:",
        reply_markup=kb, parse_mode="HTML"
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | "
        f"cat_id: {cat_id} | idx: {idx} | old_label: {old_label} | msg_id: {msg.message_id}"
    )


@router.message(AdminFieldStates.editing_label)
async def admin_field_edit_label_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(await get_text("err_no_access", "ru") or "Нет доступа."); return

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
    await register_bot_messages(message.chat.id, [msg.message_id])

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
    await state.set_state(AdminFieldStates.editing_key)


    # достаём текущий key
    async with SessionLocal() as s:
        fields = await load_category_fields(s, cat_id)
    old_key = ""
    if isinstance(fields, list) and 0 <= idx < len(fields) and isinstance(fields[idx], dict):
        old_key = str(fields[idx].get("key", ""))

    back_btn = await get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:field_edit:{cat_id}:{idx}")
    back_btn.callback_data = f"admin:field_edit:{cat_id}:{idx}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [back_btn]
    ])
    msg = await cb.message.answer(
        f"Текущий ключ: <code>{old_key or '(пусто)'}</code>\n\n"
        f"Введите новый <b>ключ</b> (латиница/цифры/_):",
        reply_markup=kb, parse_mode="HTML"
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | "
        f"cat_id: {cat_id} | idx: {idx} | old_key: {old_key} | msg_id: {msg.message_id}"
    )

# ====== Админ: Проверка уникальности ключа (разрешаем «старый» ключ у самого поля) ======
@router.message(AdminFieldStates.editing_key)
async def admin_field_edit_key_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(await get_text("err_no_access", "ru") or "Нет доступа."); return

    await clear_bot_messages(message.chat.id, message.bot)
    import re
    key = (message.text or "").strip().lower()
    if not re.fullmatch(r'[a-z0-9_]+', key):
        msg = await message.answer("❗️Только латиница/цифры/нижнее подчёркивание. Введите снова.")
        last_bot_messages[message.chat.id] = [msg.message_id]
        await register_bot_messages(message.chat.id, [msg.message_id])
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
            await register_bot_messages(message.chat.id, [msg.message_id])
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
    await register_bot_messages(message.chat.id, [msg.message_id])

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

    back_btn = await get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:field_edit:{cat_id}:{idx}")
    back_btn.callback_data = f"admin:field_edit:{cat_id}:{idx}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [back_btn]
    ])
    msg = await cb.message.answer("Введите варианты через запятую:\n<code>Опция 1, Опция 2, ...</code>", reply_markup=kb, parse_mode="HTML")
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await state.set_state(AdminFieldStates.editing_options)

    import inspect
    print(f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {chat_id} | cat_id: {cat_id} | idx: {idx} | msg_id: {msg.message_id}")


@router.message(AdminFieldStates.editing_options)
async def admin_field_edit_options_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(await get_text("err_no_access", "ru") or "Нет доступа."); return

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
    await register_bot_messages(message.chat.id, [msg.message_id])

    import inspect
    print(f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id: {message.chat.id} | cat_id: {cat_id} | idx: {idx} | msg_id: {msg.message_id}")



# ====== Админ: тип поля ======
@router.callback_query(F.data.regexp(r"^admin:field_type:(text|number|select|checkbox|video)$"), AdminFieldStates.choosing_type)
async def admin_field_pick_type(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return

    await clear_bot_messages(cb.message.chat.id, cb.bot)
    ftype = cb.data.split(":")[-1]
    await state.update_data(field_type=ftype)
    cat_id = (await state.get_data()).get("field_cat_id")

    back_btn = await get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:fields:add:{cat_id}")
    back_btn.callback_data = f"admin:fields:add:{cat_id}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [back_btn]
    ])
    msg = await cb.message.answer("Введите <b>заголовок</b> поля (например: <i>Модель</i>)", reply_markup=kb, parse_mode="HTML")
    last_bot_messages[cb.message.chat.id] = [msg.message_id]
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
    await state.set_state(AdminFieldStates.waiting_label)

    print(f"FUNC: {inspect.currentframe().f_code.co_name} | type: {ftype} | chat_id: {cb.message.chat.id} | user_id: {cb.from_user.id} | msg_id: {msg.message_id}")

# ====== Админ: ввод заголовка ======
@router.message(AdminFieldStates.waiting_label)
async def admin_field_label(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(await get_text("err_no_access", "ru") or "Нет доступа."); return

    await clear_bot_messages(message.chat.id, message.bot)
    label = message.text.strip()
    await state.update_data(field_label=label)

    key_suggest = re.sub(r'[^a-z0-9_]+', '_', label.lower()).strip("_") or "field"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⬇️ Оставить ключ: {key_suggest}", callback_data=f"admin:field_keepkey:{key_suggest}")]
    ])
    msg = await message.answer("Введите <b>ключ</b> (латиница/цифры/_)\nили нажмите кнопку ниже.", reply_markup=kb, parse_mode="HTML")
    last_bot_messages[message.chat.id] = [msg.message_id]
    await register_bot_messages(message.chat.id, [msg.message_id])
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
            await register_bot_messages(cb.message.chat.id, [msg.message_id])
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
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
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
        await message.answer(await get_text("err_no_access", "ru") or "Нет доступа."); return

    await clear_bot_messages(message.chat.id, message.bot)

    import re
    key = (message.text or "").strip().lower()
    if not re.fullmatch(r'[a-z0-9_]+', key):
        msg = await message.answer("❗️Ключ: только латиница/цифры/нижнее подчёркивание. Введите снова.")
        last_bot_messages[message.chat.id] = [msg.message_id]
        await register_bot_messages(message.chat.id, [msg.message_id])
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
            await register_bot_messages(message.chat.id, [msg.message_id])
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
    await register_bot_messages(message.chat.id, [msg.message_id])
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
        await register_bot_messages(cb.message.chat.id, [msg.message_id])
        await state.set_state(AdminFieldStates.waiting_options)
    else:
        await persist_field_and_back(cb, state)

    print(f"FUNC: {inspect.currentframe().f_code.co_name} | required: {required} | chat_id: {cb.message.chat.id} | user_id: {cb.from_user.id}")

# ====== Админ: варианты для select ======
@router.message(AdminFieldStates.waiting_options)
async def admin_field_options(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(await get_text("err_no_access", "ru") or "Нет доступа."); return

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
            await register_bot_messages(chat_id, [msg.message_id])
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
    await register_bot_messages(chat_id, [msg.message_id])

    print(f"FUNC: {inspect.currentframe().f_code.co_name} | saved_field: {field} | cat_id: {cat_id} | chat_id: {chat_id} | user_id: {user_id} | msg_id: {msg.message_id}")


# ====== Админ: просмотр одного поля (детали + кнопки) ======
@router.callback_query(F.data.startswith("admin:fields:view:"))
async def admin_field_view(cb: CallbackQuery, state: FSMContext):
    """
    Показывает детали одного поля + даёт действия:
    ⬆️/⬇️ переместить, ✏️ редактировать, 🗑 удалить, ⬅️ назад.
    """
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return

    chat_id = cb.message.chat.id
    try:
        _, _, _, cat_id_s, idx_s = cb.data.split(":")
        cat_id, idx = int(cat_id_s), int(idx_s)
    except Exception:
        await cb.answer(await get_text("err_bad_data", "ru") or "Неверные данные", show_alert=True); return

    # подчистка
    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    async with SessionLocal() as s:
        category = (await s.execute(select(Category).where(Category.id == cat_id))).scalar_one()
        fields = await load_category_fields(s, cat_id)

    if not isinstance(fields, list) or idx < 0 or idx >= len(fields):
        await cb.answer("Поле не найдено", show_alert=True)
        # вернёмся к списку полей
        fake = CallbackQuery(
            id="fake", from_user=cb.from_user, chat_instance=cb.chat_instance,
            message=cb.message, data=f"admin:fields:{cat_id}"
        )
        await admin_fields_menu(fake, state)
        return

    f = fields[idx] if isinstance(fields[idx], dict) else {}
    ftype   = f.get("type", "text")
    flabel  = f.get("label", "(без названия)")
    fkey    = f.get("key", "-")
    freq    = "Да" if f.get("required") else "Нет"
    fopts   = f.get("options") if isinstance(f.get("options"), list) else None

    text = (
        f"⚙️ <b>Поле категории:</b> {category.name}\n\n"
        f"<b>Название:</b> {flabel}\n"
        f"<b>Ключ:</b> <code>{fkey}</code>\n"
        f"<b>Тип:</b> {ftype}\n"
        f"<b>Обязательно:</b> {freq}\n"
        + (f"<b>Варианты:</b> {', '.join(map(str, fopts))}\n" if ftype == "select" and fopts else "")
    )

    rows = []
    # перемещение
    rows.append([
        InlineKeyboardButton(text="⬆️ Выше", callback_data=f"admin:field_move:{cat_id}:{idx}:up"),
        InlineKeyboardButton(text="⬇️ Ниже", callback_data=f"admin:field_move:{cat_id}:{idx}:down"),
    ])
    # редактирование/удаление (предполагаем, что эти обработчики у вас уже есть)
    rows.append([
        InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"admin:field_edit:{cat_id}:{idx}"),
        InlineKeyboardButton(text="🗑 Удалить",       callback_data=f"admin:field_delete_confirm:{cat_id}:{idx}"),
    ])
    back_btn = await get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:fields:{cat_id}")
    back_btn.callback_data = f"admin:fields:{cat_id}"
    rows.append([back_btn])

    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    msg = await cb.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

    import inspect
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"chat_id: {chat_id} | user_id: {cb.from_user.id} | cat_id: {cat_id} | idx: {idx} | "
        f"msg_id: {msg.message_id}"
    )


# ====== Админ: перемещение поля вверх/вниз ======
@router.callback_query(F.data.startswith("admin:field_move:"))
async def admin_field_move(cb: CallbackQuery, state: FSMContext):
    """
    Перемещает поле в списке полей категории.
    """
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return

    chat_id = cb.message.chat.id

    # ожидаем: admin:field_move:<cat_id>:<idx>:<direction>
    parts = cb.data.split(":")
    if len(parts) != 5:
        await cb.answer(await get_text("err_bad_data", "ru") or "Неверные данные", show_alert=True); return
    _, _, cat_id_s, idx_s, direction = parts

    try:
        cat_id = int(cat_id_s)
        idx = int(idx_s)
        direction = direction.lower()
    except Exception:
        await cb.answer(await get_text("err_bad_data", "ru") or "Неверные данные", show_alert=True); return

    # подчистка
    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    async with SessionLocal() as s:
        fields = await load_category_fields(s, cat_id)
        if not isinstance(fields, list):
            fields = []
        n = len(fields)

        if idx < 0 or idx >= n:
            await cb.answer("Поле не найдено", show_alert=True)
            fake = CallbackQuery(
                id="fake", from_user=cb.from_user, chat_instance=cb.chat_instance,
                message=cb.message, data=f"admin:fields:{cat_id}"
            )
            await admin_fields_menu(fake, state)
            return

        new_idx = idx
        if direction == "up":
            if idx == 0:
                await cb.answer("Уже наверху", show_alert=True)
            else:
                fields[idx-1], fields[idx] = fields[idx], fields[idx-1]
                new_idx = idx - 1
                await save_category_fields(s, cat_id, fields)
        elif direction == "down":
            if idx == n - 1:
                await cb.answer("Уже внизу", show_alert=True)
            else:
                fields[idx+1], fields[idx] = fields[idx], fields[idx+1]
                new_idx = idx + 1
                await save_category_fields(s, cat_id, fields)
        else:
            await cb.answer("Неизвестное направление", show_alert=True)

    # после перемещения просто перерисуем список полей
    fake = CallbackQuery(
        id="fake",
        from_user=cb.from_user,
        chat_instance=cb.chat_instance,
        message=cb.message,
        data=f"admin:fields:{cat_id}",
    )
    await admin_fields_menu(fake, state)

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"chat_id: {chat_id} | user_id: {cb.from_user.id} | cat_id: {cat_id} | "
        f"idx: {idx} -> {new_idx} | dir: {direction}"
    )

# ─────────────────────────────────────────────────────────
# RU: Тумблер «Доп. категории: Вкл/Выкл» в админке полей категории.
#     Чистим прошлое меню, сохраняем флаг и перерисовываем этот же экран
#     через ПРЯМОЙ вызов admin_fields_menu(..., cat_id=...).
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.regexp(r"^admin:fields:toggle_extra:(\d+):(0|1)$"))
async def admin_fields_toggle_extra(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # зачистка предыдущих сообщений/меню
    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return

    m = re.match(r"^admin:fields:toggle_extra:(\d+):(0|1)$", (cb.data or ""))
    if not m:
        await cb.answer(await get_text("err_bad_data", "ru") or "Неверные данные", show_alert=True)
        print(f"[admin_fields.py] handler=admin_fields_toggle_extra ERROR parse chat_id={chat_id} data={cb.data!r}")
        return

    cat_id = int(m.group(1))
    new_val = bool(int(m.group(2)))

    async with SessionLocal() as s:
        await _set_allow_extra_flag(s, cat_id, new_val)

    await cb.answer("Сохранено")
    # перерисовываем без "фейкового" CallbackQuery
    await admin_fields_menu(cb, state, cat_id=cat_id)

    print(f"[admin_fields.py] handler=admin_fields_toggle_extra OK cat_id={cat_id} new_val={new_val} chat_id={chat_id}")
