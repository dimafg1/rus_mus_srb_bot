from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from app.states import FeedbackStates
from app.routers.utils import clear_bot_messages, last_bot_messages, get_text, register_bot_messages
from sqlalchemy import text
from app.database import SessionLocal
from html import escape as html_escape
import inspect
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from app.keyboards import get_common_menu_button
from app.admin_ids import ADMIN_IDS

router = Router()

# ====== Обратная связь: выбор в главном меню ======
@router.callback_query(F.data == "feedback")
async def feedback_start(cb: CallbackQuery, state: FSMContext):
    """
    Обратная связь: старт. Просим пользователя написать сообщение.
    Показываем сверху панель "Возврат" с единственной кнопкой "Главное меню".
    """
    chat_id = cb.message.chat.id

    await clear_bot_messages(chat_id, cb.bot)
    await state.set_state(FeedbackStates.waiting_for_feedback_message)

    # --- Навигационная панель "Возврат" с одной кнопкой "Главное меню" ---
    # Если у вас уже есть текст в БД, используем его; иначе дефолт.
    try:
        from app.routers.utils import get_text  # если уже импортирован выше — удалите эту строку
        nav_text = await get_text('return_to_menu', 'ru') or "Возврат -db"
    except Exception:
        nav_text = "Возврат -db"

    main_menu_btn = await get_common_menu_button('main_menu')
    nav_buttons = []
    if main_menu_btn:
        nav_buttons.append(
            InlineKeyboardButton(
                text=main_menu_btn.text,
                callback_data=main_menu_btn.callback_data
            )
        )
    nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons]) if nav_buttons else None

    # Сначала шапка
    nav_msg = await cb.bot.send_message(chat_id, nav_text, reply_markup=nav_markup, parse_mode="HTML")

    # Затем основной текст запроса
    msg = await cb.message.answer(
        "✉️ <b>Обратная связь</b>\n\nПожалуйста, опишите ваш вопрос или предложение одним сообщением:",
        parse_mode="HTML"
    )

    # Оба сообщения в кеш, чтобы потом удалить
    last_bot_messages.setdefault(chat_id, []).extend([nav_msg.message_id, msg.message_id])
    await register_bot_messages(chat_id, [nav_msg.message_id, msg.message_id])

    print(
        f"FUNC: feedback_start | chat_id: {chat_id} | user_id: {cb.from_user.id} | cb.data: {cb.data}"
    )

@router.message(StateFilter(FeedbackStates.waiting_for_feedback_message))
async def feedback_receive(message: Message, state: FSMContext):
    """
    Обратная связь: получаем текст, сохраняем в БД, уведомляем админа, отвечаем пользователю,
    добавляем кнопку Главное меню прямо под сообщением-ответом (и только его!).
    """
    from sqlalchemy import text  # обязательно!
    from app.keyboards import get_common_menu_button
    from app.routers.utils import clear_bot_messages, last_bot_messages, register_bot_messages
    from app.database import SessionLocal
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    chat_id = message.chat.id
    user_id = message.from_user.id
    username = message.from_user.username
    text_msg = message.text

    # >>> ВАЖНО: удаляем пользовательское сообщение с текстом
    try:
        await message.delete()
        print(f"[DEL] feedback_receive | deleted user msg | chat_id={chat_id} user_id={user_id}")
    except Exception as e:
        print(f"[WARN] feedback_receive | cannot delete user msg: {e}")

    # Сохраняем в БД
    async with SessionLocal() as session:
        await session.execute(
            text("INSERT INTO feedback (user_id, username, message) VALUES (:user_id, :username, :message)"),
            {"user_id": user_id, "username": username, "message": text_msg}
        )
        await session.commit()

    # Уведомляем админов. Один заблокировавший бота админ не должен ронять
    # ответ пользователю — рассылка идёт независимо по каждому ID.
    who = f"@{username}" if username else f"id{user_id}"
    admin_text = (
        f"✉️ <b>Новое обращение</b>\n"
        f"От: {who} (<code>{user_id}</code>)\n\n"
        f"{html_escape(text_msg or '')}"
    )
    for admin_id in ADMIN_IDS:
        try:
            await message.bot.send_message(admin_id, admin_text, parse_mode="HTML")
        except Exception as e:
            print(f"[WARN] feedback_receive | admin notify failed | admin_id={admin_id}: {e}")

    # Очищаем старые сообщения
    await clear_bot_messages(chat_id, message.bot)

    # Только одно сообщение пользователю — с кнопкой "Главное меню"
    main_menu_btn = await get_common_menu_button('main_menu')
    nav_buttons = []
    if main_menu_btn:
        nav_buttons.append(InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data))
    nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons]) if nav_buttons else None

    reply = (
        "✅ <b>Спасибо за ваше обращение!</b>\n\n"
        "Ваше сообщение отправлено администратору. После прочтения мы обязательно дадим вам ответ."
    )
    msg = await message.bot.send_message(chat_id, reply, parse_mode="HTML", reply_markup=nav_markup)

    # Сохраняем id сообщений для последующего удаления
    last_bot_messages.setdefault(chat_id, []).extend([msg.message_id])
    await register_bot_messages(chat_id, [msg.message_id])

    await state.clear()
    print(
        f"FUNC: feedback_receive | user_id: {user_id} | text: {text_msg} | chat_id: {chat_id}"
    )
