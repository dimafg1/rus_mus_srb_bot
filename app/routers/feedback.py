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

# ====== Обратная связь: меню (написать / мои обращения) ======
@router.callback_query(F.data == "feedback")
async def feedback_menu(cb: CallbackQuery, state: FSMContext):
    """Экран раздела «Обратная связь»: выбор — написать или посмотреть свои обращения."""
    chat_id = cb.message.chat.id
    await state.clear()
    await clear_bot_messages(chat_id, cb.bot)

    main_menu_btn = await get_common_menu_button('main_menu')
    rows = [
        [InlineKeyboardButton(text="✍️ Написать нам", callback_data="fb:write")],
        [InlineKeyboardButton(text="📨 Мои обращения", callback_data="fb:mine")],
    ]
    if main_menu_btn:
        rows.append([InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data)])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    msg = await cb.bot.send_message(
        chat_id, "✉️ <b>Обратная связь</b>\n\nВыберите действие:", parse_mode="HTML", reply_markup=kb)
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"FUNC: feedback_menu | chat_id={chat_id} | user_id={cb.from_user.id}")


# ====== Обратная связь: написать нам ======
@router.callback_query(F.data == "fb:write")
async def fb_write(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    await state.set_state(FeedbackStates.waiting_for_feedback_message)

    # Панель навигации: Назад (в меню обратной связи) + Главное меню
    back_btn = await get_common_menu_button('back')
    nav_buttons = []
    if back_btn:
        back_btn.callback_data = "feedback"
        nav_buttons.append(back_btn)
    main_menu_btn = await get_common_menu_button('main_menu')
    if main_menu_btn:
        nav_buttons.append(InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data))
    nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons])
    nav_text = await get_text('return_to_menu', 'ru') or "Возврат"
    nav_msg = await cb.bot.send_message(chat_id, nav_text, reply_markup=nav_markup, parse_mode="HTML")

    msg = await cb.message.answer(
        "✉️ <b>Обратная связь</b>\n\nПожалуйста, опишите Ваш вопрос или предложение:",
        parse_mode="HTML"
    )

    last_bot_messages.setdefault(chat_id, []).extend([nav_msg.message_id, msg.message_id])
    await register_bot_messages(chat_id, [nav_msg.message_id, msg.message_id])
    print(f"FUNC: fb_write | chat_id={chat_id} | user_id={cb.from_user.id}")

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

    # Сохраняем в БД и получаем id обращения (для кнопок «Нужен ответ» / «Ответить»)
    async with SessionLocal() as session:
        res = await session.execute(
            text("INSERT INTO feedback (user_id, username, message) VALUES (:user_id, :username, :message)"),
            {"user_id": user_id, "username": username, "message": text_msg}
        )
        feedback_id = res.lastrowid
        if not feedback_id:
            fid_row = (await session.execute(text("SELECT last_insert_rowid()"))).first()
            feedback_id = int(fid_row[0]) if fid_row else 0
        await session.commit()

    # Чистим прежние сообщения бота в чате отправителя ДО отправки уведомлений:
    # иначе в self-test (админ = отправитель, тот же чат) чистка удалит
    # только что созданное уведомление.
    await clear_bot_messages(chat_id, message.bot)

    # Уведомляем админов. Один заблокировавший бота админ не должен ронять
    # ответ пользователю — рассылка идёт независимо по каждому ID.
    who = f"@{username}" if username else f"id{user_id}"
    status_line = "🕓 Статус: пользователь решает, нужен ли ответ."
    admin_text = _format_admin_notif(feedback_id, who, user_id, text_msg, status_line)
    admin_kb = _admin_notif_kb(feedback_id, with_reply=True)
    msgs = []
    for admin_id in ADMIN_IDS:
        try:
            m = await message.bot.send_message(admin_id, admin_text, parse_mode="HTML", reply_markup=admin_kb)
            if m:
                msgs.append((admin_id, m.message_id))
                # Регистрируем уведомление — уйдёт при следующей навигации админа
                last_bot_messages.setdefault(admin_id, []).append(m.message_id)
                await register_bot_messages(admin_id, [m.message_id])
        except Exception as e:
            print(f"[WARN] feedback_receive | admin notify failed | admin_id={admin_id}: {e}")
    _fb_admin_notifs[feedback_id] = {"who": who, "user_id": user_id, "body": text_msg, "msgs": msgs}
    # «Вооружаем» админов на быстрый ответ этому обращению (просто набрать текст)
    _arm_admin_reply(feedback_id, user_id, text_msg)

    # Пользователю — тёплое подтверждение + выбор «Нужен ответ / Ответ не нужен»
    main_menu_btn = await get_common_menu_button('main_menu')
    rows = [[
        InlineKeyboardButton(text="🔔 Нужен ответ", callback_data=f"fb:need:{feedback_id}"),
        InlineKeyboardButton(text="Ответ не нужен", callback_data=f"fb:noneed:{feedback_id}"),
    ]]
    if main_menu_btn:
        rows.append([InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data)])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    reply = (
        "🙏 <b>Спасибо, что нашли время написать</b> — для нас это действительно важно.\n"
        "Ваше сообщение уже у администратора.\n\n"
        "Если Вам нужен ответ, нажмите, пожалуйста, кнопку «Нужен ответ» — "
        "администратор обязательно ответит Вам в ближайшее время.\n\n"
        "Ещё раз спасибо за участие!"
    )
    msg = await message.bot.send_message(chat_id, reply, parse_mode="HTML", reply_markup=kb)

    # Сохраняем id сообщений для последующего удаления
    last_bot_messages.setdefault(chat_id, []).extend([msg.message_id])
    await register_bot_messages(chat_id, [msg.message_id])

    await state.clear()
    print(
        f"FUNC: feedback_receive | user_id: {user_id} | feedback_id: {feedback_id} | chat_id: {chat_id}"
    )


# ====== Пользователь: «Нужен ответ» / «Ответ не нужен» ======

class AdminReplyStates(StatesGroup):
    waiting_reply = State()


def _who(username: str | None, user_id: int) -> str:
    return f"@{username}" if username else f"id{user_id}"


# Реестр уведомлений админам по обращению: feedback_id → {who, user_id, body, msgs:[(admin_id, msg_id)]}
# В памяти (теряется при рестарте → fallback). Одно уведомление на обращение — оно
# редактируется на месте (новое → запросили ответ → отвечено), а не плодится.
_fb_admin_notifs: dict[int, dict] = {}


def _admin_notif_kb(feedback_id: int, *, with_reply: bool = True) -> InlineKeyboardMarkup:
    row = []
    if with_reply:
        row.append(InlineKeyboardButton(text="✍️ Ответить", callback_data=f"fb:reply:{feedback_id}"))
    row.append(InlineKeyboardButton(text="✖️ Убрать", callback_data=f"fb:dismiss:{feedback_id}"))
    return InlineKeyboardMarkup(inline_keyboard=[
        row,
        [InlineKeyboardButton(text="📋 Список обращений", callback_data="admin_feedback")],
    ])


def _format_admin_notif(feedback_id: int, who: str, user_id: int, body: str, status_line: str) -> str:
    return (
        f"✉️ <b>Обращение №{feedback_id}</b>\n"
        f"От: {who} (<code>{user_id}</code>)\n"
        f"{status_line}\n\n"
        f"{html_escape(body or '')}"
    )


async def _update_admin_notif(bot, feedback_id: int, status_line: str, *,
                              with_reply: bool, create_if_missing: bool,
                              who: str = "—", user_id: int = 0) -> None:
    """Обновить (отредактировать) уже отправленные админам уведомления по обращению.
    Если сохранённых нет и create_if_missing — отправить компактное новое.
    Так уведомление одно на обращение и не размножается."""
    entry = _fb_admin_notifs.get(feedback_id)
    kb = _admin_notif_kb(feedback_id, with_reply=with_reply)
    if entry and entry.get("msgs"):
        text_html = _format_admin_notif(
            feedback_id, entry.get("who", who), entry.get("user_id", user_id),
            entry.get("body", ""), status_line)
        ok = False
        for admin_id, msg_id in entry["msgs"]:
            try:
                await bot.edit_message_text(
                    text_html, chat_id=admin_id, message_id=msg_id,
                    parse_mode="HTML", reply_markup=kb)
                ok = True
            except Exception as e:
                print(f"[WARN] _update_admin_notif | edit failed | admin={admin_id} msg={msg_id}: {e}")
        if ok:
            return
    if not create_if_missing:
        return
    compact = (
        f"✉️ <b>Обращение №{feedback_id}</b>\n"
        f"От: {who} (<code>{user_id}</code>)\n{status_line}"
    )
    msgs = []
    for admin_id in ADMIN_IDS:
        try:
            m = await bot.send_message(admin_id, compact, parse_mode="HTML", reply_markup=kb)
            if m:
                msgs.append((admin_id, m.message_id))
                # Регистрируем — уйдёт при следующей навигации админа
                last_bot_messages.setdefault(admin_id, []).append(m.message_id)
                await register_bot_messages(admin_id, [m.message_id])
        except Exception as e:
            print(f"[WARN] _update_admin_notif | send failed | admin={admin_id}: {e}")
    _fb_admin_notifs[feedback_id] = {"who": who, "user_id": user_id, "body": "", "msgs": msgs}


@router.callback_query(F.data.startswith("fb:dismiss"))
async def fb_dismiss(cb: CallbackQuery):
    """Админ убирает уведомление из чата."""
    try:
        await cb.message.delete()
    except Exception:
        pass
    try:
        _fb_admin_notifs.pop(int(cb.data.split(":")[2]), None)
    except (ValueError, IndexError):
        pass
    await cb.answer("Убрано.")


# «Активное обращение» на админа: последнее, на которое можно ответить,
# просто набрав текст (без нажатия «Ответить»). admin_id → {fb_id, user_id, original}.
# Не блокирующее состояние: кнопки меню (callback-и) работают как обычно.
_admin_reply_target: dict[int, dict] = {}


def _arm_admin_reply(feedback_id: int, user_id: int, original: str) -> None:
    for admin_id in ADMIN_IDS:
        _admin_reply_target[admin_id] = {
            "fb_id": feedback_id, "user_id": user_id, "original": original or ""}


async def _deliver_reply(bot, admin_chat_id: int, feedback_id, target_user_id: int,
                         original: str, reply_text: str) -> bool:
    """Доставить ответ пользователю + подтверждение админу. Возвращает delivered.
    Используется и кнопкой «Ответить», и «быстрым» ответом (просто набрал текст)."""
    user_main_btn = await get_common_menu_button('main_menu')
    user_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=user_main_btn.text, callback_data=user_main_btn.callback_data)
    ]]) if user_main_btn else None
    # Сначала вопрос пользователя, потом ответ — так логичнее читать
    user_msg = (
        "✉️ <b>Ответ администратора</b>\n\n"
        f"<i>На Ваше сообщение:</i>\n«{html_escape(original)}»\n\n"
        f"➡️ {html_escape(reply_text)}"
    )
    delivered = False
    try:
        sent = await bot.send_message(target_user_id, user_msg, parse_mode="HTML", reply_markup=user_kb)
        delivered = True
        last_bot_messages.setdefault(target_user_id, []).append(sent.message_id)
        await register_bot_messages(target_user_id, [sent.message_id])
    except Exception as e:
        print(f"[WARN] _deliver_reply | delivery failed | target={target_user_id}: {e}")

    if delivered:
        async with SessionLocal() as session:
            await session.execute(
                text("UPDATE feedback SET answered_at=CURRENT_TIMESTAMP, answer_text=:ans WHERE id=:id"),
                {"id": feedback_id, "ans": reply_text},
            )
            await session.commit()
        await _update_admin_notif(
            bot, feedback_id, "✅ <b>Отвечено.</b>", with_reply=False, create_if_missing=False)
        _fb_admin_notifs.pop(feedback_id, None)
        note = f"✅ Ответ отправлен пользователю (обращение №{feedback_id})."
    else:
        note = "⚠️ Не удалось доставить ответ — возможно, пользователь заблокировал бота."

    admin_rows = [[InlineKeyboardButton(text="📋 Список обращений", callback_data="admin_feedback")]]
    if user_main_btn:
        admin_rows.append([InlineKeyboardButton(
            text=user_main_btn.text, callback_data=user_main_btn.callback_data)])
    cmsg = await bot.send_message(admin_chat_id, note, reply_markup=InlineKeyboardMarkup(inline_keyboard=admin_rows))
    last_bot_messages.setdefault(admin_chat_id, []).append(cmsg.message_id)
    await register_bot_messages(admin_chat_id, [cmsg.message_id])
    _admin_reply_target.pop(admin_chat_id, None)  # обращение обработано
    return delivered


def _admin_can_quick_reply(message: Message) -> bool:
    """Фильтр: админ «вооружён» активным обращением и печатает обычный текст."""
    return (
        message.from_user is not None
        and message.from_user.id in ADMIN_IDS
        and message.from_user.id in _admin_reply_target
        and bool(message.text)
        and not message.text.startswith("/")
    )


async def _show_user_note(cb: CallbackQuery, note_html: str):
    """Заменить экран одним сообщением с кнопкой «Главное меню»."""
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    main_menu_btn = await get_common_menu_button('main_menu')
    rows = []
    if main_menu_btn:
        rows.append([InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data)])
    kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    msg = await cb.bot.send_message(chat_id, note_html, parse_mode="HTML", reply_markup=kb)
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])


@router.callback_query(F.data.startswith("fb:need:"))
async def fb_need(cb: CallbackQuery):
    try:
        feedback_id = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        await cb.answer()
        return
    user_id = cb.from_user.id
    username = cb.from_user.username

    # Помечаем обращение (только своё) как ждущее ответа
    async with SessionLocal() as session:
        await session.execute(
            text("UPDATE feedback SET needs_reply=1 WHERE id=:id AND user_id=:uid"),
            {"id": feedback_id, "uid": user_id},
        )
        await session.commit()

    await _show_user_note(
        cb,
        "🔔 <b>Спасибо!</b> Мы передали администратору, что Вам нужен ответ.\n"
        "Он придёт сюда же, в этот чат, в ближайшее время.",
    )

    # Обновляем то же уведомление админам (не плодим новое)
    await _update_admin_notif(
        cb.bot, feedback_id, "🔔 <b>Пользователь запросил ответ.</b>",
        with_reply=True, create_if_missing=True,
        who=_who(username, user_id), user_id=user_id)
    # «Вооружаем» админов на быстрый ответ именно этому обращению
    _entry = _fb_admin_notifs.get(feedback_id)
    _arm_admin_reply(feedback_id, user_id, _entry.get("body", "") if _entry else "")
    await cb.answer("Спасибо! Ответим Вам здесь.")
    print(f"FUNC: fb_need | feedback_id={feedback_id} | user_id={user_id}")


@router.callback_query(F.data.startswith("fb:noneed:"))
async def fb_noneed(cb: CallbackQuery):
    try:
        feedback_id = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        feedback_id = 0
    await _show_user_note(
        cb,
        "🙏 <b>Спасибо за участие!</b>\nБудем рады видеть Вас снова.",
    )
    # Обновляем то же уведомление админам (без нового сообщения)
    if feedback_id:
        await _update_admin_notif(
            cb.bot, feedback_id, "ℹ️ <b>Ответ не требуется.</b>",
            with_reply=False, create_if_missing=False,
            who=_who(cb.from_user.username, cb.from_user.id), user_id=cb.from_user.id)
        _fb_admin_notifs.pop(feedback_id, None)
    await cb.answer("Спасибо!")
    print(f"FUNC: fb_noneed | user_id={cb.from_user.id} | feedback_id={feedback_id}")


# ====== Администратор: ответ пользователю через бота ======

@router.callback_query(F.data.startswith("fb:reply:"))
async def fb_reply_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Недоступно.", show_alert=True)
        return
    try:
        feedback_id = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        await cb.answer()
        return

    async with SessionLocal() as session:
        row = (await session.execute(
            text("SELECT user_id, username, message FROM feedback WHERE id=:id"),
            {"id": feedback_id},
        )).first()
    if not row:
        await cb.answer("Обращение не найдено.", show_alert=True)
        return
    target_user_id, target_username, original = int(row[0]), row[1], row[2]

    await state.set_state(AdminReplyStates.waiting_reply)
    await state.update_data(
        fb_id=feedback_id,
        fb_user_id=target_user_id,
        fb_username=target_username,
        fb_original=original,
    )

    prompt = (
        f"✍️ <b>Ответ на обращение №{feedback_id}</b>\n"
        f"Кому: {_who(target_username, target_user_id)} (<code>{target_user_id}</code>)\n\n"
        f"<i>Их сообщение:</i>\n{html_escape(original or '')}\n\n"
        f"Напишите ответ одним сообщением — я отправлю его пользователю."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🕓 Ответить позже", callback_data="fb:reply_later")
    ]])
    pmsg = await cb.bot.send_message(cb.message.chat.id, prompt, parse_mode="HTML", reply_markup=kb)
    await state.update_data(fb_prompt_id=pmsg.message_id)
    # Регистрируем приглашение — уйдёт при навигации, даже если админ не отправил ответ
    last_bot_messages.setdefault(cb.message.chat.id, []).append(pmsg.message_id)
    await register_bot_messages(cb.message.chat.id, [pmsg.message_id])
    await cb.answer()
    print(f"FUNC: fb_reply_start | feedback_id={feedback_id} | admin={cb.from_user.id}")


@router.callback_query(F.data == "fb:reply_later")
async def fb_reply_later(cb: CallbackQuery, state: FSMContext):
    """Отложить ответ: выйти из ввода, напомнить, где ответить позже."""
    data = await state.get_data()
    prompt_id = data.get("fb_prompt_id")
    fb_id = data.get("fb_id")
    await state.clear()
    _admin_reply_target.pop(cb.from_user.id, None)  # отложили — снимаем быстрый ответ
    chat_id = cb.message.chat.id
    if prompt_id:
        try:
            await cb.bot.delete_message(chat_id, prompt_id)
        except Exception:
            pass

    main_menu_btn = await get_common_menu_button('main_menu')
    rows = [[InlineKeyboardButton(text="📋 Список обращений", callback_data="admin_feedback")]]
    if main_menu_btn:
        rows.append([InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data)])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    msg = await cb.bot.send_message(
        chat_id,
        "🕓 Хорошо, ответите позже. Открыть обращения можно кнопкой ниже.",
        reply_markup=kb,
    )
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer("Ответите позже.")
    print(f"FUNC: fb_reply_later | feedback_id={fb_id} | admin={cb.from_user.id}")


@router.message(StateFilter(AdminReplyStates.waiting_reply))
async def fb_reply_send(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    reply_text = (message.text or "").strip()
    if not reply_text:
        await message.answer("Пожалуйста, отправьте ответ текстом одним сообщением.")
        return

    data = await state.get_data()
    feedback_id = data.get("fb_id")
    target_user_id = data.get("fb_user_id")
    original = data.get("fb_original") or ""
    prompt_id = data.get("fb_prompt_id")
    admin_chat_id = message.chat.id

    # Чистим ввод: набранный ответ и приглашение к вводу
    try:
        await message.delete()
    except Exception:
        pass
    if prompt_id:
        try:
            await message.bot.delete_message(admin_chat_id, prompt_id)
        except Exception:
            pass

    delivered = await _deliver_reply(
        message.bot, admin_chat_id, feedback_id, target_user_id, original, reply_text)
    await state.clear()
    print(f"FUNC: fb_reply_send | feedback_id={feedback_id} | delivered={delivered} | admin={message.from_user.id}")


@router.message(StateFilter(None), _admin_can_quick_reply)
async def fb_quick_reply(message: Message, state: FSMContext):
    """Быстрый ответ: админ просто набрал текст, не нажимая «Ответить».
    Уходит в последнее «активное» обращение. Меню-кнопки при этом работают
    как обычно (они callback-и и этот фильтр их не трогает)."""
    reply_text = (message.text or "").strip()
    target = _admin_reply_target.get(message.from_user.id)
    if not reply_text or not target:
        return
    admin_chat_id = message.chat.id
    try:
        await message.delete()
    except Exception:
        pass
    delivered = await _deliver_reply(
        message.bot, admin_chat_id, target["fb_id"], target["user_id"],
        target.get("original") or "", reply_text)
    print(f"FUNC: fb_quick_reply | feedback_id={target.get('fb_id')} | delivered={delivered} | admin={message.from_user.id}")


# ====== Пользователь: «Мои обращения» — перечитать вопрос и ответ ======

FB_MINE_PAGE_SIZE = 5


async def _render_fb_mine(cb: CallbackQuery, offset: int = 0):
    chat_id = cb.message.chat.id
    user_id = cb.from_user.id
    await clear_bot_messages(chat_id, cb.bot)

    async with SessionLocal() as session:
        total = (await session.execute(
            text("SELECT COUNT(*) FROM feedback WHERE user_id=:uid"), {"uid": user_id})).scalar_one()
        rows = (await session.execute(
            text("SELECT id, message, answered_at, needs_reply FROM feedback "
                 "WHERE user_id=:uid ORDER BY created_at DESC, id DESC LIMIT :lim OFFSET :off"),
            {"uid": user_id, "lim": FB_MINE_PAGE_SIZE, "off": offset})).fetchall()

    main_menu_btn = await get_common_menu_button('main_menu')

    if not total:
        back_btn = await get_common_menu_button('back')
        kb_rows = []
        if back_btn:
            back_btn.callback_data = "feedback"
            kb_rows.append([back_btn])
        if main_menu_btn:
            kb_rows.append([InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data)])
        msg = await cb.bot.send_message(
            chat_id, "У Вас пока нет обращений.", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()
        return

    pages = max(1, (total + FB_MINE_PAGE_SIZE - 1) // FB_MINE_PAGE_SIZE)
    if offset >= total:
        offset = (pages - 1) * FB_MINE_PAGE_SIZE
    page = offset // FB_MINE_PAGE_SIZE + 1

    kb_rows = []
    for r in rows:
        fid, msg_text, answered, needs = r[0], (r[1] or ""), r[2], r[3]
        mark = "✅" if answered else ("⏳" if needs else "•")
        title = (msg_text.strip().replace("\n", " "))[:40] or f"#{fid}"
        kb_rows.append([InlineKeyboardButton(text=f"{mark} {title}", callback_data=f"fb:mineview:{fid}")])

    if pages > 1:
        pager = []
        if offset > 0:
            pager.append(InlineKeyboardButton(text="«", callback_data=f"fb:mine:{offset - FB_MINE_PAGE_SIZE}"))
        pager.append(InlineKeyboardButton(text=f"{page}/{pages}", callback_data="stub"))
        if offset + FB_MINE_PAGE_SIZE < total:
            pager.append(InlineKeyboardButton(text="»", callback_data=f"fb:mine:{offset + FB_MINE_PAGE_SIZE}"))
        kb_rows.append(pager)

    back_btn = await get_common_menu_button('back')
    if back_btn:
        back_btn.callback_data = "feedback"
        kb_rows.append([back_btn])
    if main_menu_btn:
        kb_rows.append([InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data)])

    msg = await cb.bot.send_message(
        chat_id,
        "📨 <b>Мои обращения</b>\n<i>✅ отвечено · ⏳ ждёт ответа</i>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()


@router.callback_query(F.data == "fb:mine")
async def fb_mine(cb: CallbackQuery):
    await _render_fb_mine(cb, offset=0)


@router.callback_query(F.data.startswith("fb:mine:"))
async def fb_mine_page(cb: CallbackQuery):
    try:
        offset = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        offset = 0
    await _render_fb_mine(cb, offset=offset)


@router.callback_query(F.data.startswith("fb:mineview:"))
async def fb_mine_view(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    user_id = cb.from_user.id
    try:
        fid = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        await cb.answer()
        return

    await clear_bot_messages(chat_id, cb.bot)
    async with SessionLocal() as session:
        row = (await session.execute(
            text("SELECT message, answer_text, answered_at, needs_reply "
                 "FROM feedback WHERE id=:id AND user_id=:uid"),
            {"id": fid, "uid": user_id})).first()

    main_menu_btn = await get_common_menu_button('main_menu')
    kb_rows = []
    if row:
        kb_rows.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"fb:minedel:{fid}")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ К обращениям", callback_data="fb:mine")])
    if main_menu_btn:
        kb_rows.append([InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data)])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    if not row:
        body = "Обращение не найдено."
    else:
        question, answer, answered, needs = row[0], row[1], row[2], row[3]
        lines = [
            f"📨 <b>Обращение №{fid}</b>\n",
            f"<b>Ваш вопрос:</b>\n«{html_escape(question or '')}»",
        ]
        if answer:
            lines.append(f"\n<b>Ответ администратора:</b>\n{html_escape(answer)}")
        elif needs:
            lines.append("\n<i>Администратор ещё не ответил. Ответ придёт сюда же.</i>")
        else:
            lines.append("\n<i>Ответ по этому обращению не запрашивался.</i>")
        body = "\n".join(lines)

    msg = await cb.bot.send_message(chat_id, body, parse_mode="HTML", reply_markup=kb)
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"FUNC: fb_mine_view | fid={fid} | user_id={user_id}")


@router.callback_query(F.data.startswith("fb:minedel:"))
async def fb_mine_delete_confirm(cb: CallbackQuery):
    """Просим подтверждение — удаление своего обращения необратимо."""
    try:
        fid = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        await cb.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"fb:mineview:{fid}")],
        [InlineKeyboardButton(text="✅ Удалить навсегда", callback_data=f"fb:minedel_yes:{fid}")],
    ])
    try:
        await cb.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass
    await cb.answer("Удалить это обращение?")


@router.callback_query(F.data.startswith("fb:minedel_yes:"))
async def fb_mine_delete_yes(cb: CallbackQuery):
    """Удаляет обращение пользователя (только своё) и возвращает к списку."""
    user_id = cb.from_user.id
    try:
        fid = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        await cb.answer()
        return

    async with SessionLocal() as session:
        await session.execute(
            text("DELETE FROM feedback WHERE id=:id AND user_id=:uid"),
            {"id": fid, "uid": user_id},
        )
        await session.commit()

    # Обращение исчезло — снимаем возможные ссылки на него из памяти админа
    _fb_admin_notifs.pop(fid, None)
    for admin_id, tgt in list(_admin_reply_target.items()):
        if tgt.get("fb_id") == fid:
            _admin_reply_target.pop(admin_id, None)

    await cb.answer("Удалено.")
    await _render_fb_mine(cb, offset=0)
    print(f"FUNC: fb_mine_delete_yes | fid={fid} | user_id={user_id}")
