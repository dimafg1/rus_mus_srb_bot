# coding: utf-8
"""
codex_review.py
Железобетонно «заткнутый» роутер для работы с Codex/Code-review.
- Режим активируется только командой /codex_review.
- Работает только в состоянии CodexState.waiting_code.
- Ограничен по правам: только is_admin(user_id) может запускать и использовать.
- Есть команда отмены /codex_cancel.
- Нет глобальных перехватчиков текста/файлов.
"""

import asyncio
import logging
from typing import Optional

from aiogram import F
from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

# Подключаем ваш клиент для Codex (если есть). Если его нет — обработаем аккуратно.
try:
    from app.routers.codex_client import CodexClient
except Exception:
    CodexClient = None  # будем ругаться аккуратно

# Хелпер проверки прав — используем ваш is_admin из админ-панели, если есть
try:
    from app.routers.admin_panel import is_admin
except Exception:
    # fallback: запретим всё, если нет is_admin
    def is_admin(user_id: int) -> bool:
        return False

# Локальные утилиты приложения (для безопасных ответов/логов)
try:
    from app.routers.utils import last_bot_messages, clear_bot_messages, register_bot_messages
except Exception:
    # заглушки, если модуль отличается — неблокирующие
    last_bot_messages = {}
    async def clear_bot_messages(chat_id, bot):
        return
    async def register_bot_messages(chat_id, ids):
        return

router = Router(name="codex_review")
logger = logging.getLogger("codex_review")

# Состояние: только одно — ждём код/файл
class CodexState(StatesGroup):
    waiting_code = State()

# --- Вспомогательные функции ---------------------------------
async def _is_requester_admin(state: FSMContext, user_id: int) -> bool:
    """
    Проверяем, что пользователь — запустивший режим и админ.
    Возвращает True только если в FSM хранится codex_user_id == user_id и is_admin(user_id) == True.
    """
    try:
        data = await state.get_data()
        owner = data.get("codex_user_id")
        if owner is None:
            return False
        if int(owner) != int(user_id):
            return False
        return bool(is_admin(user_id))
    except Exception:
        return False

async def _reply_and_log(m: Message, text: str):
    sent = None
    try:
        sent = await m.reply(text)
    except Exception:
        try:
            sent = await m.answer(text)
        except Exception:
            logger.exception("Не удалось отправить сообщение пользователю.")
    if sent:
        last_bot_messages.setdefault(m.chat.id, []).append(sent.message_id)
        try:
            await register_bot_messages(m.chat.id, [sent.message_id])
        except Exception:
            pass

# --- Команда запуска режима ---------------------------------
@router.message(Command(commands=["codex_review"]))
async def cmd_codex_review_start(m: Message, state: FSMContext):
    """
    Точка входа в Codex-режим.
    Доступна только админу (is_admin). После этого бот ждёт код/файл и НЕ реагирует на остальные сообщения.
    """
    user_id = m.from_user.id
    chat_id = m.chat.id

    if not is_admin(user_id):
        await _reply_and_log(m, "Доступ запрещён: команда доступна только администраторам.")
        return

    # Сохраняем в FSM кто владелец сессии (чтобы никто другой не мог использовать)
    await state.set_state(CodexState.waiting_code)
    await state.update_data(codex_user_id=user_id)

    await _reply_and_log(
        m,
        (
            "✅ Режим Codex включён.\n\n"
            "Отправьте .py файл или вставьте код одним сообщением.\n"
            "Чтобы отменить — отправьте /codex_cancel.\n"
            "Я обработаю только сообщения от вас, пока вы в этом режиме."
        ),
    )
    logger.info("Codex mode started by user %s in chat %s", user_id, chat_id)


# --- Команда отмены режима ---------------------------------
@router.message(Command(commands=["codex_cancel"]))
async def cmd_codex_review_cancel(m: Message, state: FSMContext):
    """
    Явная отмена режима. Может выполнить только владелец сессии (и админ).
    """
    user_id = m.from_user.id
    ok = await _is_requester_admin(state, user_id)
    if not ok:
        await _reply_and_log(m, "Нечего отменять (или вы не владелец сессии).")
        return

    await state.clear()
    await _reply_and_log(m, "Режим Codex отменён. Больше не реагирую на код.")
    logger.info("Codex mode cancelled by user %s", user_id)


# --- Обработка .py документа (только в состоянии и только от владельца) ----
@router.message(CodexState.waiting_code, F.document)
async def codex_handle_document(m: Message, state: FSMContext):
    """
    Обработка .py файла. Активна только если:
    - FSM==CodexState.waiting_code
    - Отправитель совпадает с codex_user_id
    - is_admin(user) == True
    """
    user_id = m.from_user.id
    chat_id = m.chat.id

    # Безопасная проверка прав
    if not await _is_requester_admin(state, user_id):
        # просто игнорируем — не реагируем публично
        logger.warning("codex_handle_document: rejected from user %s", user_id)
        return

    # Проверяем имя файла, минимальная проверка
    filename = getattr(m.document, "file_name", "") or ""
    if not filename.lower().endswith(".py"):
        await _reply_and_log(m, "Пожалуйста, пришлите файл с расширением .py")
        return

    # Скачиваем файл и передаём в обработчик (если есть)
    try:
        file = await m.document.get_file()
        file_bytes = await file.download(destination=bytes)  # returns bytes
    except Exception as e:
        logger.exception("Ошибка при загрузке файла")
        await _reply_and_log(m, "Не удалось загрузить файл: " + str(e))
        return

    code_text = None
    try:
        code_text = file_bytes.decode("utf-8", errors="replace")
    except Exception:
        code_text = None

    if not code_text:
        await _reply_and_log(m, "Не могу прочитать содержимое файла.")
        return

    # Вызов Codex-клиента (если есть)
    if CodexClient is None:
        await _reply_and_log(m, "Codex-клиент не доступен на сервере (CodexClient отсутствует).")
        return

    # Выполняем асинхронную обработку, ограничиваем таймаут
    client = CodexClient()
    await _reply_and_log(m, "Анализирую код в Codex...")
    try:
        # review_code — ожидаемый метод в вашем CodexClient; делаем защищённый вызов
        result = await asyncio.wait_for(client.review_code(code_text, filename=filename), timeout=60)
        # Предполагаем, что review_code возвращает текст
        await _reply_and_log(m, f"Результат:\n\n{result}")
    except asyncio.TimeoutError:
        await _reply_and_log(m, "Время ожидания анализа истекло.")
    except Exception as e:
        logger.exception("Ошибка в CodexClient.review_code")
        await _reply_and_log(m, "Ошибка обращения к Codex: " + str(e))


# --- Обработка текста (одним сообщением с кодом) -------------------------
@router.message(CodexState.waiting_code, F.text)
async def codex_handle_text(m: Message, state: FSMContext):
    """
    Обработка сообщения с кодом (plain-text). Активна только в режиме и только от владельца-админа.
    Чтобы не поймать случайную фразу, мы не реагируем на сообщения, начинающиеся с '/' (команды).
    """
    user_id = m.from_user.id

    # Безопасная проверка: владелец и админ
    if not await _is_requester_admin(state, user_id):
        logger.warning("codex_handle_text: rejected from user %s", user_id)
        return

    # Игнор команд
    if (m.text or "").strip().startswith("/"):
        # не обрабатываем команды как код
        return

    # Получаем текст (ограничиваем длину для безопасности)
    code_text = (m.text or "").strip()
    if not code_text:
        await _reply_and_log(m, "Пустое сообщение — пришлите код или .py файл.")
        return
    if len(code_text) > 20000:
        await _reply_and_log(m, "Слишком большой фрагмент кода — пришлите файл .py.")
        return

    if CodexClient is None:
        await _reply_and_log(m, "Codex-клиент не доступен на сервере.")
        return

    client = CodexClient()
    await _reply_and_log(m, "Анализирую код в Codex...")
    try:
        result = await asyncio.wait_for(client.review_code(code_text, filename="snippet.py"), timeout=60)
        await _reply_and_log(m, f"Результат:\n\n{result}")
    except asyncio.TimeoutError:
        await _reply_and_log(m, "Время ожидания анализа истекло.")
    except Exception as e:
        logger.exception("Ошибка в CodexClient.review_code")
        await _reply_and_log(m, "Ошибка обращения к Codex: " + str(e))


# --- Защита от «подвешенных» сессий: авто-таймаут (опционально) ------------
# Если пользователь долго не отвечает, можно автоматически выйти из режима.
# Этот таймаут НЕ обязателен; если хотите — включите вызов из scheduler.
async def codex_auto_timeout_cleanup(state: FSMContext, timeout_seconds: int = 3600):
    """
    Очистить FSM сессии Codex, если были. Можно запускать по расписанию.
    """
    try:
        data = await state.get_data()
        if data.get("codex_user_id"):
            await state.clear()
            logger.info("codex_auto_timeout_cleanup: cleared codex session")
    except Exception:
        logger.exception("codex_auto_timeout_cleanup failed")


# --- Экспорт роутера ------------------------------------------------------
# router уже создан вверху — импортируйте его как: from app.routers.codex_review import router
__all__ = ["router"]
