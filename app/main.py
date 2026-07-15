# --- simple env loader (.env.ai) ---
import os
from pathlib import Path
def _load_env_ai():
    env_path = Path(__file__).resolve().parent.parent / ".env.ai"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
_load_env_ai()
# --- end env loader ---


import asyncio
from collections import defaultdict
from typing import List, Dict, Optional, Any, Callable, Awaitable
from datetime import datetime
from app.models import utcnow_naive
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    Update,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from pydantic_settings import BaseSettings
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

# --- Приложение/Бот ---
from app.database import init_db, SessionLocal
from app.models import City, Category, Item, Listing, BotUser
from app.keyboards import (
    # main_inline_menu,
    market_inline,
    photo_keyboard,
    confirm_keyboard,
    sold_keyboard,
    delete_keyboard,
    cities_inline,
    equip_inline,
    catalog_inline_initial,
    catalog_city_inline,
    catalog_application_category_inline,
    vacancy_main_inline_view,
    vacancy_category_inline,
    musicians_sub_inline,
    events_main_inline,
)
from app.routers.market_add import router as market_add_router
from app.routers.market_edit import router as market_edit_router   # 🔹 добавили
from app.routers.market_edit_photos import router as market_edit_photos_router

from app.routers.services_add import router as services_add_router
from app.routers.services_view import router as services_view_router
from app.routers.services_edit_overview import router as services_edit_overview_router
from app.routers.services_edit import router as services_edit_router
from app.routers.services_edit_photos import router as services_edit_photos_router



from app.routers.utils import (
    clear_bot_messages,
    register_bot_messages,
    last_bot_messages,
    sent_photo_messages,
    my_listing_messages,
)
# get_text берём из routers.utils (aiosqlite-версия с поддержкой default);
# версия из app.texts здесь раньше импортировалась и тут же перекрывалась
from app.routers.utils import get_text
from app.keyboards import get_common_menu_button
from app.routers.market_view import router as market_view_router
from app.routers.utils import safe_edit_or_send

from app.routers.utils import (
    last_search_query_message,
    last_search_menu_message,
    last_reply_menu_messages,
    last_bot_messages,
    my_listing_messages,
    sent_photo_messages,
)
from app.states import MarketSearch
import inspect
from app.routers import feedback
from app.routers.admin_panel import is_admin
from app.routers.admin_fields import router as admin_fields_router
from app.routers.user_extra_fields import router as user_extra_fields_router
from app.routers.market_edit_overview import router as market_edit_overview_router

from app.routers.events_view import router as events_view_router
from app.routers.events_add import router as events_add_router
from app.routers.events_admin import router as events_admin_router



# импорты рядом с остальными роутерами
from app.routers.vacancy_add import router as vacancy_add_router
from app.routers.vacancy_view import router as vacancy_view_router
from app.routers.vacancy_utils import vacancy_main_menu
from app.routers.vacancy_edit import router as vacancy_edit_router
# from app.routers.vacancy_edit_overview import router as vacancy_edit_overview_router

from app.routers.codex_review import router as codex_review_router

from app.routers.utils import log





# last_search_query_message: Dict[int, int] = {}     # Сообщение "Введите запрос..."
# last_search_menu_message: Dict[int, int] = {}      # Меню с результатами
# last_reply_menu_messages: Dict[int, list] = defaultdict(list)   # ID reply-меню по чатам
# my_listing_messages: Dict[int, list] = defaultdict(list)



async def safe_edit_or_send(cb: CallbackQuery, text: str, reply_markup=None, parse_mode="HTML"):
    chat_id = cb.message.chat.id
    try:
        msg = await cb.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])
    except Exception:
        try:
            await cb.message.delete()
        except Exception:
            pass
        msg = await cb.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])



PAGE = 5  # Количество записей на странице

# Global dictionaries to track sent listing messages and the currently expanded listing per chat.
listing_message_ids: Dict[int, Dict[int, int]] = {}
expanded_listing_by_chat: Dict[int, int] = {}
# Новый словарь для хранения id сообщений с фото

# Set up logging to see debug output.
# logging.basicConfig(level=logging.DEBUG)
# ───────── logging (quiet by default) ─────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)

# Дублируем логи в файл с ротацией: logs/bot.log, 5 МБ x 5 файлов
from logging.handlers import RotatingFileHandler
_log_dir = Path(__file__).resolve().parent.parent / "logs"
_log_dir.mkdir(exist_ok=True)
_file_handler = RotatingFileHandler(
    _log_dir / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"))
logging.getLogger().addHandler(_file_handler)

# Приглушаем болтливые библиотеки
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
# ───────────────────────────────────────────────


# ─────────────────────── Middleware: сброс накопившихся нажатий ──────────────
class DropStaleCallbackMiddleware:
    """Отбрасывает callback_query с update_id <= порогового, накопившиеся пока бот не работал."""

    def __init__(self, last_update_id: int) -> None:
        self._threshold = last_update_id
        self._log = logging.getLogger("app.middleware")
        self._log.info("Stale callback threshold: update_id <= %s", self._threshold)

    async def __call__(
        self,
        handler: Callable[[Update, Dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: Dict[str, Any],
    ) -> Any:
        if event.callback_query and event.update_id <= self._threshold:
            self._log.warning(
                "Dropped stale callback_query data=%r update_id=%s",
                event.callback_query.data,
                event.update_id,
            )
            try:
                await event.callback_query.answer()
            except Exception:
                pass
            return
        return await handler(event, data)


class TrackUserMiddleware:
    """Обновляет BotUser.last_seen при каждом входящем обновлении от пользователя."""

    async def __call__(
        self,
        handler: Callable[[Update, Dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: Dict[str, Any],
    ) -> Any:
        user = None
        if event.message and event.message.from_user:
            user = event.message.from_user
        elif event.callback_query and event.callback_query.from_user:
            user = event.callback_query.from_user

        if user and not user.is_bot:
            # Источник первого входа: deep-link параметр «/start <payload>».
            # Попадает в запись только при INSERT (первое появление пользователя);
            # блок on_conflict его не трогает — существующим не перезаписывается.
            first_source = None
            text = event.message.text if event.message else None
            if text and text.startswith("/start "):
                first_source = text.split(maxsplit=1)[1].strip()[:64] or None

            try:
                async with SessionLocal() as s:
                    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
                    now = utcnow_naive()
                    username = user.username or None
                    full_name = (f"{user.first_name or ''} {user.last_name or ''}").strip() or None
                    stmt = sqlite_insert(BotUser).values(
                        user_id=user.id,
                        username=username,
                        full_name=full_name,
                        last_seen=now,
                        first_seen=now,
                        first_source=first_source,
                    ).on_conflict_do_update(
                        index_elements=["user_id"],
                        set_={"username": username, "full_name": full_name, "last_seen": now},
                    )
                    await s.execute(stmt)
                    await s.commit()
            except Exception:
                pass

        return await handler(event, data)


async def get_last_update_id(bot: Bot) -> int:
    """Получить максимальный update_id из очереди ДО старта polling."""
    try:
        updates = await bot.get_updates(limit=100, timeout=0)
        if updates:
            return max(u.update_id for u in updates)
    except Exception as e:
        logging.getLogger("app.main").warning("get_updates failed: %s", e)
    return 0
# ─────────────────────────────────────────────────────────────────────────────


# ───────────────────────── Settings ────────────────────────── #
class Settings(BaseSettings):
    bot_token: str
    model_config = {"env_file": ".env"}

settings = Settings()
bot = Bot(
    token=settings.bot_token,
    default=DefaultBotProperties(parse_mode="HTML"),
)
dp = Dispatcher()


# ── Глобальный обработчик ошибок: полный трейсбек + контекст в лог ──────────
@dp.errors()
async def on_error(event, **kwargs):
    log = logging.getLogger("app.errors")
    update = getattr(event, "update", None)
    exc = getattr(event, "exception", None)
    ctx = {"update_id": getattr(update, "update_id", None)}
    try:
        if update and update.callback_query:
            ctx["user_id"] = update.callback_query.from_user.id
            ctx["callback_data"] = update.callback_query.data
        elif update and update.message:
            ctx["user_id"] = update.message.from_user.id if update.message.from_user else None
            ctx["text"] = (update.message.text or "")[:200]
    except Exception:
        pass
    log.error("Unhandled error | %s", ctx, exc_info=exc)
    return True  # ошибка обработана, aiogram не роняет polling


print("=== rus_mus_srb_bot started (DEV) ===")

# ── CLEANUP ROUTER: удаляет любые лишние сообщения в ЛС ─────────────────────
from aiogram import Router
cleanup_router = Router(name="cleanup_router")

WHITELIST_CMDS = {}
ALLOWED_STATE_PREFIXES = ("Sell", "ServicesAdd", "VacancyAdd")  # ваши мастера

def _is_allowed_state(state_name: str | None) -> bool:
    if not state_name:
        return False
    return state_name.split(":", 1)[0] in ALLOWED_STATE_PREFIXES

@cleanup_router.message()   # ловит всё, что не перехватили другие хендлеры
async def delete_stray_messages(message: Message, state: FSMContext):
    # работаем только в личке
    if getattr(message.chat, "type", None) != "private":
        return

    txt = (message.text or "").strip()

    # whitelisted-команды не трогаем — их обрабатывают свои хендлеры
    if txt.startswith("/") and txt.split()[0] in WHITELIST_CMDS:
        return

    # если пользователь сейчас в мастере публикации — не трогаем
    if _is_allowed_state(await state.get_state()):
        return

    try:
        await message.bot.delete_message(message.chat.id, message.message_id)
    except Exception as e:
        print(f"[cleanup_router] delete failed: {e}")
# ─────────────────────────────────────────────────────────────────────────────


from app.routers.admin_panel import router as admin_panel_router
from app.routers.admin_analytics import router as admin_analytics_router
dp.include_router(admin_panel_router)
dp.include_router(admin_analytics_router)
dp.include_router(admin_fields_router)
dp.include_router(market_edit_overview_router)
dp.include_router(services_edit_router)
dp.include_router(market_edit_photos_router)
dp.include_router(vacancy_add_router)
dp.include_router(vacancy_view_router)
dp.include_router(vacancy_edit_router)
dp.include_router(codex_review_router)
dp.include_router(events_view_router)
dp.include_router(events_add_router)
dp.include_router(events_admin_router)

print("[routers] codex_review_router included")
# dp.include_router(vacancy_edit_overview_router)

# # ───────── FSM for forms ─────────
# class CatalogForm(VacancyForm):
#     category_choice: State = State()
#     name: State = State()
#     address: State = State()
#     photo: State = State()
#     description: State = State()
#     repo: State = State()

# class ExtendedVacancyForm(VacancyForm):
#     text: State = State()

# class EventForm(VacancyForm):
#     date: State = State()
#     details: State = State()



dp.include_router(market_add_router)
dp.include_router(market_edit_router)
# dp.include_router(vacancy_router)
dp.include_router(feedback.router)
dp.include_router(user_extra_fields_router)


# from app.routers.catalog_view import router as catalog_view_router
# from app.routers.catalog_add import router as catalog_add_router
# dp.include_router(catalog_view_router)
# dp.include_router(catalog_add_router)

dp.include_router(services_add_router)
dp.include_router(services_view_router)
dp.include_router(services_edit_overview_router)
dp.include_router(services_edit_photos_router)
print("[routers] services_edit_overview_router included")



@dp.callback_query(F.data == "go_isk")
async def go_isk(cb: CallbackQuery, state: FSMContext):
    await cb.answer()                 # 1) сразу закрываем "часики" Telegram
    await state.clear()               # 2) дальше уже любая логика
    kb = await vacancy_main_menu()
    await cb.message.edit_text(
        "Раздел «Вакансии»",
        reply_markup=kb,
        parse_mode="HTML"
    )
    last_bot_messages.setdefault(cb.message.chat.id, []).append(cb.message.message_id)
    await register_bot_messages(cb.message.chat.id, [cb.message.message_id])



@dp.callback_query(F.data == "go_events")
async def go_events(cb: CallbackQuery, state: FSMContext):
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    markup = await events_main_inline()
    await safe_edit_or_send(cb, await get_text("events_choose_city", "ru"), markup)
    await cb.answer()
    print(f"[main.py] go_events ✓ ")



@dp.callback_query(F.data == "go_help")
async def go_help(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    help_text = await get_text("help", "ru")
    if not help_text:
        help_text = (
            "Справка по использованию бота:\n"
            "• Выберите раздел в меню ниже\n"
            "• Для возврата используйте кнопку 'Назад'\n"
            "• Введите 'отмена' для отмены любого действия"
        )
    rows = [[InlineKeyboardButton(text="❓ Частые вопросы", callback_data="go_faq")]]
    main_menu_btn = await get_common_menu_button('main_menu')
    if main_menu_btn:
        rows.append([main_menu_btn])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await safe_edit_or_send(cb, help_text, kb)
    await cb.answer()


@dp.callback_query(F.data == "go_faq")
async def go_faq(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    faq_text = await get_text("faq", "ru")
    rows = [[InlineKeyboardButton(text="⬅️ Назад к помощи", callback_data="go_help")]]
    main_menu_btn = await get_common_menu_button('main_menu')
    if main_menu_btn:
        rows.append([main_menu_btn])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await safe_edit_or_send(cb, faq_text, kb)
    await cb.answer()




from app.models import Menu  # Не забудьте импортировать модель Menu

# ↓↓↓ новая функция для построения клавиатуры главного меню ↓↓↓
async def build_main_menu(lang="ru") -> InlineKeyboardMarkup:
    async with SessionLocal() as session:
        result = await session.execute(
            select(Menu)
            .where(Menu.parent_code == "main_menu", Menu.visible == 1, Menu.lang == lang)
            .order_by(Menu.order_num)
        )
        rows = result.scalars().all()


    # Группируем по две кнопки в строку
    keyboard = []
    temp_row = []
    for row in rows:
        btn = InlineKeyboardButton(
            text=(f"{row.icon} " if row.icon else "") + row.text,
            callback_data=row.callback_data
        )
        temp_row.append(btn)
        if len(temp_row) == 2:
            keyboard.append(temp_row)
            temp_row = []
    if temp_row:  # если нечетное число, добавляем последнюю кнопку
        keyboard.append(temp_row)
    return InlineKeyboardMarkup(inline_keyboard=keyboard)





# ↓↓↓ обновленный хендлер (замените целиком) ↓↓↓
@dp.callback_query(F.data == "main_menu")
async def main_menu_cb(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # ─── Диагностика до очистки ───────────────────────────────────────────────
    print(
        f"[BEFORE] main_menu_cb | chat_id={chat_id} | "
        f"query_cached={last_search_query_message.get(chat_id)} | "
        f"menu_cached={last_search_menu_message.get(chat_id)} | "
        f"bot_msgs={last_bot_messages.get(chat_id)}"
    )

    # 1) Удаляем сообщение, по которому нажали кнопку
    try:
        await cb.message.delete()
    except Exception as e:
        print(f"[WARN] main_menu_cb delete clicked msg: {e}")

    # 2) Чистим «Возврат» и «Подсказку» из FSM
    try:
        from app.routers.vacancy_add import _drop_nav_and_prompt
        await _drop_nav_and_prompt(state, chat_id, cb.bot)
    except Exception as e:
        print(f"[WARN] main_menu_cb _drop_nav_and_prompt failed: {e}")
        data = await state.get_data()
        for key in ("nav_msg_id", "prompt_id"):
            mid = data.get(key)
            if mid:
                try:
                    await cb.bot.delete_message(chat_id, mid)
                except Exception as e2:
                    print(f"[WARN] main_menu_cb delete {key}={mid}: {e2}")
        await state.update_data(nav_msg_id=None, prompt_id=None)

    # 3) Вычищаем кэши поиска
    qid = last_search_query_message.pop(chat_id, None)
    mid = last_search_menu_message.pop(chat_id, None)
    print(f"[POP] main_menu_cb | query_id={qid} | menu_id={mid}")
    for _mid in (qid, mid):
        if _mid:
            try:
                await cb.bot.delete_message(chat_id, _mid)
            except Exception as e:
                print(f"[WARN] main_menu_cb delete cached search msg_id={_mid}: {e}")

    # 4) Общая подчистка прочих служебных сообщений бота
    await clear_bot_messages(chat_id, cb.bot)

    # 4.1) Удаляем черновик Афиши (если пользователь был в мастере)
    try:
        from app.routers.events_add import _delete_draft
        await _delete_draft(cb.bot, chat_id, state)
    except Exception:
        pass

    # 5) Сброс состояния
    try:
        await state.clear()
    except Exception as e:
        print(f"[WARN] main_menu_cb state.clear(): {e}")

    # 6) Рисуем главное меню
    try:
        welcome = await get_text("welcome", "ru")
    except Exception as e:
        print(f"[WARN] main_menu_cb get_text('welcome'): {e}")
        welcome = None
    welcome = welcome or "👋 Привет всем!\n<b>Главное меню</b>\nВыберите раздел:"

    menu_markup = await build_main_menu(lang="ru")

    # 6.1) Добавим «Админ-панель», если нужно
    try:
        from aiogram.types import InlineKeyboardButton
        from app.routers.admin_panel import is_admin
        if not getattr(menu_markup, "inline_keyboard", None):
            menu_markup.inline_keyboard = []
        if is_admin(cb.from_user.id):
            if not any(
                getattr(btn, "callback_data", None) == "admin"
                for row in menu_markup.inline_keyboard for btn in row
            ):
                menu_markup.inline_keyboard.append(
                    [InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin")]
                )
    except Exception as e:
        print(f"[WARN] main_menu_cb add admin button: {e}")

    # Важно: отправляем новое сообщение и кладём его в кэш
    msg = None
    try:
        # можно через bot.send_message, чтобы не зависеть от удалённого cb.message
        msg = await cb.bot.send_message(
            chat_id, welcome, reply_markup=menu_markup, parse_mode="HTML"
        )
    except Exception as e:
        print(f"[ERROR] main_menu_cb send_message: {e}")

    try:
        await cb.answer()
    except Exception:
        pass

    # Кладём в кэш только если реально есть отправленное сообщение
    if msg:
        lst = last_bot_messages.get(chat_id) or []
        lst.append(msg.message_id)
        last_bot_messages[chat_id] = lst
        await register_bot_messages(chat_id, [msg.message_id])
        print(f"[CACHE] main_menu_cb store msg_id={msg.message_id}")

    # ─── Диагностика после ───────────────────────────────────────────────────
    print(
        f"[AFTER] main_menu_cb | chat_id={chat_id} | "
        f"query_cached={last_search_query_message.get(chat_id)} | "
        f"menu_cached={last_search_menu_message.get(chat_id)} | "
        f"bot_msgs={last_bot_messages.get(chat_id)}"
    )





# ───────────────── Database Helpers ───────────────────────────── #
async def city_by_slug(slug: str) -> City:
    async with SessionLocal() as s:
        return (await s.execute(select(City).where(City.slug == slug))).scalar_one()

async def children_of(parent_id: Optional[int]) -> List[Category]:
    async with SessionLocal() as s:
        q = select(Category).where(Category.parent_id == parent_id)
        return (await s.execute(q)).scalars().all()

async def fetch_items(city_id: int, cat_id: int, offset: int = 0) -> List[Item]:
    async with SessionLocal() as s:
        q = (select(Item)
             .where(Item.city_id == city_id,
                    Item.category_id == cat_id,
                    Item.is_approved.is_(True))
             .order_by(Item.created_at.desc())
             .offset(offset)
             .limit(PAGE))
        return (await s.execute(q)).scalars().all()

async def fetch_listings(city_id: int, cat_id: int, offset: int = 0) -> List[Listing]:
    async with SessionLocal() as s:
        q = (select(Listing)
             .where(Listing.city_id == city_id,
                    Listing.category_id == cat_id,
                    Listing.is_sold.is_(False))
             .order_by(Listing.created_at.desc())
             .offset(offset)
             .limit(PAGE))
        return (await s.execute(q)).scalars().all()

# ───────────────── Handlers ───────────────────────────── #

@dp.message(lambda m: m.text and m.text.lower() in ["отмена", "cancel"])
async def cancel_handler(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Действие отменено.")


# ───────────── MARKET (Барахолка) Handlers ───────────── #

import asyncio
import re
from aiogram.types import Message

async def delete_user_command_with_delay(message: Message, delay: float = 2.0):
    if not message.text:
        return

    text = message.text.strip()
    if not text.startswith("/"):
        return

    try:
        # для /start — задержка, для остальных можно тоже оставить ту же
        await asyncio.sleep(delay)
        await message.delete()
    except Exception as e:
        print(f"[delete_user_command_with_delay] can't delete user cmd: {e}")



@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    chat_id = message.chat.id

    # Аналитика: событие входа (source = deep-link параметр, если был)
    from app.analytics import log_event
    start_source = None
    if message.text and message.text.startswith("/start "):
        start_source = message.text.split(maxsplit=1)[1].strip()[:64] or None
    await log_event("user_started", user_id=message.from_user.id, source=start_source)

    # # 0) Сначала пытаемся удалить исходное сообщение пользователя (/start)
    # try:
    #     await message.bot.delete_message(chat_id=chat_id, message_id=message.message_id)
    # except Exception as e:
    #     # не критично (например, нет прав в группе или сообщение слишком старое)
    #     print(f"[cmd_start] can't delete /start: {e}")

    # 1) чистим предыдущие сообщения бота (как и было)
    await clear_bot_messages(chat_id, message.bot)

    # 2) сбрасываем состояние (как и было)
    await state.clear()

    # 3) текст приветствия
    welcome = await get_text("welcome", "ru")
    if not welcome:
        welcome = "👋 Привет!\n<b>Главное меню</b>\nВыберите раздел:"

    # 4) базовое меню
    markup = await build_main_menu()

    # 5) добавляем «Админ-панель» админу (без дублей) — как и было
    if is_admin(message.from_user.id):
        if not any(
            getattr(btn, "callback_data", None) == "admin"
            for row in (markup.inline_keyboard or []) for btn in row
        ):
            markup.inline_keyboard.append(
                [InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin")]
            )

    # 6) отправляем главное меню
    msg = await message.answer(welcome, reply_markup=markup, parse_mode="HTML")
    last_bot_messages[chat_id].append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])

    # await delete_user_command_with_delay(message, delay=2.0)

    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"chat_id: {getattr(message.chat, 'id', None)} | "
        f"user_id: {getattr(message.from_user, 'id', None)} | "
        f"msg_id: {getattr(message, 'message_id', None)}"
    )



# @dp.callback_query(F.data.startswith("vcity:"))
# async def vacancy_isk_city(cb: CallbackQuery, state: FSMContext):
#     _, city_slug = cb.data.split(":", 1)
#     city = await city_by_slug(city_slug)
#     await safe_edit_or_send(cb, f"🤝 Ищу → {city.name}\nВыберите направление для просмотра анкет:", 
#                                  reply_markup=vacancy_category_inline())
#     await cb.answer()

# @dp.callback_query(F.data == "vacancy:back")
# async def vacancy_back(cb: CallbackQuery):
#     await safe_edit_or_send(cb, "🤝 Ищу – выберите действие:", reply_markup=vacancy_main_inline_view("vcity"))
#     await cb.answer()

# @dp.callback_query(F.data.startswith("vcat:"))
# async def vacancy_category(cb: CallbackQuery, state: FSMContext):
#     data = cb.data.split(":", 1)[1]
#     if data == "musicians":
#         await safe_edit_or_send(cb, "Выберите подкатегорию для 'Музыканты':", reply_markup=musicians_sub_inline())
#     elif data == "back":
#         await safe_edit_or_send(cb, "Выберите направление:", reply_markup=vacancy_category_inline())
#     else:
#         await state.update_data(category=data)
#         await safe_edit_or_send(cb, f"Показываем анкеты по направлению <b>{data.capitalize()}</b>...", 
#                                      reply_markup=InlineKeyboardMarkup(inline_keyboard=[
#                                          [InlineKeyboardButton(text="⬅️ Назад", callback_data="vacancy:back")]
#                                      ]))
#     await cb.answer()

# @dp.callback_query(F.data.startswith("vsub:"))
# async def vacancy_sub_category(cb: CallbackQuery, state: FSMContext):
#     sub = cb.data.split(":", 1)[1]
#     await state.update_data(category=sub)
#     await safe_edit_or_send(cb, f"Показываем анкеты по подкатегории <b>{sub.capitalize()}</b>...", 
#                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=[
#                                      [InlineKeyboardButton(text="⬅️ Назад", callback_data="vacancy:back")]
#                                  ]))
#     await cb.answer()





@dp.message(Command("myid"))
async def get_my_id(message: Message):
    msg = await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")
    last_bot_messages[message.chat.id].append(msg.message_id)
    await register_bot_messages(message.chat.id, [msg.message_id])




# ───────────────── Entrypoint ───────────────────────────── #
async def main():
    await init_db()

    # Получаем порог СТАРЫХ обновлений до регистрации роутеров и старта polling
    last_update_id = await get_last_update_id(bot)

    # Middleware регистрируется ПЕРВЫМ — до всех роутеров
    dp.update.outer_middleware(DropStaleCallbackMiddleware(last_update_id))
    dp.update.outer_middleware(TrackUserMiddleware())

    # Подключаем роутеры
    dp.include_router(market_view_router)  # 👈 как у вас было
    dp.include_router(cleanup_router)

    # Жизненный цикл объявлений: архивация, напоминания (раз в час)
    from app.lifecycle_worker import lifecycle_worker
    lifecycle_task = asyncio.create_task(lifecycle_worker(bot))

    # Тихая и корректная остановка по Ctrl+C / SIGTERM
    try:

        await dp.start_polling(bot, drop_pending_updates=True)
    except asyncio.CancelledError:
        # Опрос отменён — штатная ситуация при остановке
        pass
    finally:
        lifecycle_task.cancel()
        # Закрываем HTTP-сессию бота и прочие ресурсы
        try:
            await bot.session.close()
        except Exception:
            pass
        # Если у вас есть пулы/коннекты к БД/кэшу — закрывайте их здесь.
        # Например:
        # await engine.dispose()
        print("Bot stopped gracefully.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Гасим traceback на Windows при Ctrl+C
        print("Interrupted by user.")

