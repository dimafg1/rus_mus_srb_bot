import asyncio
from collections import defaultdict
from typing import List, Dict, Optional
from datetime import datetime
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
from app.models import City, Category, Item, Listing
from app.keyboards import (
    main_inline_menu,
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
from app.routers.vacancy import router as vacancy_router, VacancyForm
from app.routers.utils import (
    clear_bot_messages,
    last_bot_messages,
    sent_photo_messages,
    my_listing_messages,
)
from app.texts import get_text
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





last_search_query_message: Dict[int, int] = {}     # Сообщение "Введите запрос..."
last_search_menu_message: Dict[int, int] = {}      # Меню с результатами
last_reply_menu_messages: Dict[int, list] = defaultdict(list)   # ID reply-меню по чатам
my_listing_messages: Dict[int, list] = defaultdict(list)


# async def show_market_search_results(m, state, results):
#     keyboard = [
#         [InlineKeyboardButton(text=f"{listing.title} — {listing.price or ''}", callback_data=f"market_search_detail:{listing.id}")]
#         for listing in results
#     ]
#     keyboard.append([InlineKeyboardButton(text="❌ Новый поиск", callback_data="market_search_new")])
#     markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
#     await m.answer("Найдено объявлений:", reply_markup=markup)

async def safe_edit_or_send(cb: CallbackQuery, text: str, reply_markup=None, parse_mode="HTML"):
    chat_id = cb.message.chat.id
    try:
        msg = await cb.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    except Exception:
        try:
            await cb.message.delete()
        except Exception:
            pass
        msg = await cb.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)



PAGE = 5  # Количество записей на странице

# Global dictionaries to track sent listing messages and the currently expanded listing per chat.
listing_message_ids: Dict[int, Dict[int, int]] = {}
expanded_listing_by_chat: Dict[int, int] = {}
# Новый словарь для хранения id сообщений с фото

# Set up logging to see debug output.
logging.basicConfig(level=logging.DEBUG)


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

# ───────── FSM for forms ─────────
class CatalogForm(VacancyForm):
    category_choice: State = State()
    name: State = State()
    address: State = State()
    photo: State = State()
    description: State = State()
    repo: State = State()

class ExtendedVacancyForm(VacancyForm):
    text: State = State()

class EventForm(VacancyForm):
    date: State = State()
    details: State = State()

# class MarketSearch(StatesGroup):
#     waiting_for_query = State()
#     waiting_for_detail = State()


dp.include_router(market_add_router)
dp.include_router(vacancy_router)

# Include catalogue routers extracted from main.py.  These routers handle
# navigation of the specialists/portfolio catalogue (catalog_view) and
# submission of new portfolio applications (catalog_add).  Without
# including them the corresponding callback handlers would not be
# registered with the dispatcher.
from app.routers.catalog_view import router as catalog_view_router
from app.routers.catalog_add import router as catalog_add_router
dp.include_router(catalog_view_router)
dp.include_router(catalog_add_router)


# NOTE: catalogue navigation has been moved to app/routers/catalog_view.py.
# The handler registration below is intentionally disabled to avoid
# duplicate callbacks.  See catalog_view.go_catalog for the current
# implementation.
# @dp.callback_query(F.data == "go_catalog")
# async def go_catalog(cb: CallbackQuery, state: FSMContext):
#     await clear_bot_messages(cb.message.chat.id, cb.bot)
#     markup = await catalog_inline_initial()
#     await safe_edit_or_send(cb, await get_text("catalog_choose_city", "ru"), markup)
#     await cb.answer()


@dp.callback_query(F.data == "go_isk")
async def go_isk(cb: CallbackQuery, state: FSMContext):
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    markup = await vacancy_main_inline_view("vcity")
    await safe_edit_or_send(cb, await get_text("vacancy_choose_city", "ru"), markup)
    await cb.answer()



@dp.callback_query(F.data == "go_events")
async def go_events(cb: CallbackQuery, state: FSMContext):
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    markup = await events_main_inline()
    await safe_edit_or_send(cb, await get_text("events_choose_city", "ru"), markup)
    await cb.answer()



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
    # Получаем кнопку "Главное меню" из базы
    main_menu_btn = await get_common_menu_button('main_menu')
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[main_menu_btn]] if main_menu_btn else []
    )
    await safe_edit_or_send(cb, help_text, kb)
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



# ↓↓↓ обновленный хендлер ↓↓↓
@dp.callback_query(F.data == "main_menu")
async def main_menu_cb(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # Удалить сообщение с приглашением к поиску
    query_msg_id = last_search_query_message.pop(chat_id, None)
    if query_msg_id:
        try:
            await cb.bot.delete_message(chat_id, query_msg_id)
        except Exception:
            pass

    # Удалить меню поиска (если вдруг есть)
    menu_msg_id = last_search_menu_message.pop(chat_id, None)
    if menu_msg_id:
        try:
            await cb.bot.delete_message(chat_id, menu_msg_id)
        except Exception:
            pass

    # Удалить прочие служебные сообщения
    await clear_bot_messages(chat_id, cb.bot)

    await state.clear()

    welcome = await get_text("welcome", "ru")
    if not welcome:
        welcome = "👋 Привет всем!\n<b>Главное меню</b>\nВыберите раздел:"

    # Вот здесь — новый способ построения меню!
    menu_markup = await build_main_menu(lang="ru")

    await safe_edit_or_send(cb, welcome, menu_markup)
    await cb.answer()





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


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await clear_bot_messages(chat_id, message.bot)
    await state.clear()
    welcome = await get_text("welcome", "ru")
    if not welcome:
        welcome = "👋 Привет всем!\n<b>Главное меню</b>\nВыберите раздел:"
    msg = await message.answer(welcome, reply_markup=await build_main_menu(), parse_mode="HTML")
    last_bot_messages[chat_id].append(msg.message_id)   # ДОБАВЛЯЕМ



@dp.callback_query(F.data.startswith("vcity:"))
async def vacancy_isk_city(cb: CallbackQuery, state: FSMContext):
    _, city_slug = cb.data.split(":", 1)
    city = await city_by_slug(city_slug)
    await cb.message.edit_text(f"🤝 Ищу → {city.name}\nВыберите направление для просмотра анкет:", 
                                 reply_markup=vacancy_category_inline())
    await cb.answer()

@dp.callback_query(F.data == "vacancy:back")
async def vacancy_back(cb: CallbackQuery):
    await cb.message.edit_text("🤝 Ищу – выберите действие:", reply_markup=vacancy_main_inline_view("vcity"))
    await cb.answer()

@dp.callback_query(F.data.startswith("vcat:"))
async def vacancy_category(cb: CallbackQuery, state: FSMContext):
    data = cb.data.split(":", 1)[1]
    if data == "musicians":
        await cb.message.edit_text("Выберите подкатегорию для 'Музыканты':", reply_markup=musicians_sub_inline())
    elif data == "back":
        await cb.message.edit_text("Выберите направление:", reply_markup=vacancy_category_inline())
    else:
        await state.update_data(category=data)
        await cb.message.edit_text(f"Показываем анкеты по направлению <b>{data.capitalize()}</b>...", 
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад", callback_data="vacancy:back")]
                                     ]))
    await cb.answer()

@dp.callback_query(F.data.startswith("vsub:"))
async def vacancy_sub_category(cb: CallbackQuery, state: FSMContext):
    sub = cb.data.split(":", 1)[1]
    await state.update_data(category=sub)
    await cb.message.edit_text(f"Показываем анкеты по подкатегории <b>{sub.capitalize()}</b>...", 
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                     [InlineKeyboardButton(text="⬅️ Назад", callback_data="vacancy:back")]
                                 ]))
    await cb.answer()

@dp.message(ExtendedVacancyForm.text)
async def receive_vacancy_text(m: Message, state: FSMContext):
    await m.answer("Ваше объявление отправлено на модерацию (функционал в разработке).", 
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="OK", callback_data="confirm:yes")]
                    ]))
    await state.clear()


@dp.callback_query(F.data.startswith("pcity:"))
async def predl_city(cb: CallbackQuery, state: FSMContext):
    _, city_slug = cb.data.split(":", 1)
    city = await city_by_slug(city_slug)
    await cb.message.edit_text(f"🗣 Предлагаю → {city.name}\nВыберите направление для просмотра анкет:", 
                                 reply_markup=vacancy_category_inline())
    await cb.answer()

@dp.callback_query(F.data == "pcity_start")
async def predl_start_cb(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("🗣 Разместить объявление – выберите город:", 
                                 reply_markup=vacancy_main_inline_view("pcity"))
    await state.clear()
    await state.set_state(ExtendedVacancyForm.city)
    await cb.answer()

# -------- АФИША --------

@dp.callback_query(F.data.startswith("ecity:"))
async def events_city(cb: CallbackQuery, state: FSMContext):
    _, city_slug = cb.data.split(":", 1)
    city = await city_by_slug(city_slug)
    await cb.message.edit_text(f"📅 Афиша → {city.name}\nПока нет опубликованных мероприятий.\nНажмите '➕ Разместить информацию' для добавления.")
    await cb.answer()

@dp.callback_query(F.data == "event_new")
async def event_new_cb(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("➕ Разместить информацию – введите дату и время мероприятия (например, 2025-05-10 19:00):")
    await state.set_state(EventForm.date)
    await cb.answer()

@dp.callback_query(F.data == "events:back")
async def events_back(cb: CallbackQuery):
    await cb.message.edit_text("📅 Афиша – выберите действие:", reply_markup=events_main_inline())
    await cb.answer()

@dp.message(EventForm.date)
async def get_event_date(m: Message, state: FSMContext):
    await state.update_data(date=m.text)
    await m.answer("Введите дополнительные детали мероприятия (место, описание и т.д.):")
    await state.set_state(EventForm.details)

@dp.message(EventForm.details)
async def get_event_details(m: Message, state: FSMContext):
    data = await state.get_data()
    data["details"] = m.text
    summary = f"Дата и время: {data.get('date')}\nДетали: {data.get('details')}"
    await m.answer(f"Проверьте информацию о мероприятии:\n\n{summary}\n\nПодтвердите размещение.", 
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="Да", callback_data="confirm:yes"),
                         InlineKeyboardButton(text="Нет", callback_data="confirm:no")]
                    ]))
    await state.clear()


# ───────────────── Entrypoint ───────────────────────────── #
async def main():
    await init_db()

    # Подключаем роутеры
    dp.include_router(market_view_router)  # 👈 добавили этот

    # Запуск бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
