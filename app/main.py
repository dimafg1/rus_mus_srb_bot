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
from app.routers.sell import router as sell_router
from app.routers.vacancy import router as vacancy_router, VacancyForm
from app.routers.utils import (
    clear_bot_messages,
    last_bot_messages,
    sent_photo_messages,
    my_listing_messages,
)
from app.texts import get_text




last_search_query_message: Dict[int, int] = {}     # Сообщение "Введите запрос..."
last_search_menu_message: Dict[int, int] = {}      # Меню с результатами
last_reply_menu_messages: Dict[int, list] = defaultdict(list)   # ID reply-меню по чатам
my_listing_messages: Dict[int, list] = defaultdict(list)


async def show_market_search_results(m, state, results):
    keyboard = [
        [InlineKeyboardButton(text=f"{listing.title} — {listing.price or ''}", callback_data=f"market_search_detail:{listing.id}")]
        for listing in results
    ]
    keyboard.append([InlineKeyboardButton(text="❌ Новый поиск", callback_data="market_search_new")])
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    await m.answer("Найдено объявлений:", reply_markup=markup)

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

class MarketSearch(StatesGroup):
    waiting_for_query = State()
    waiting_for_detail = State()


dp.include_router(sell_router)
dp.include_router(vacancy_router)


@dp.callback_query(F.data == "go_catalog")
async def go_catalog(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    await state.clear()
    await safe_edit_or_send(cb, "🏙 Каталог\nВыберите действие:", catalog_inline_initial())
    await cb.answer()


@dp.callback_query(F.data == "go_market")
async def go_market(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    await state.clear()
    await safe_edit_or_send(cb, "💸 Барахолка – выберите действие:", market_inline())
    await cb.answer()


@dp.callback_query(F.data == "go_isk")
async def go_isk(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    await state.clear()
    await safe_edit_or_send(cb, "🤝 Ищу – выберите действие:", vacancy_main_inline_view("vcity"))
    await cb.answer()


@dp.callback_query(F.data == "go_events")
async def go_events(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    await state.clear()
    await safe_edit_or_send(cb, "📅 Афиша – выберите действие:", events_main_inline())
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
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="☰ Главное меню", callback_data="main_menu")]
        ]
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


@dp.callback_query(F.data == "apply_catalog")
async def apply_catalog_handler(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("Выберите направление вашей заявки:", reply_markup=catalog_application_category_inline())
    await state.set_state(CatalogForm.category_choice)
    await cb.answer()

@dp.callback_query(F.data.startswith("capcat:"))
async def catalog_application_category_handler(cb: CallbackQuery, state: FSMContext):
    category = cb.data.split(":", 1)[1]
    await state.update_data(category_choice=category)
    await cb.message.edit_text(f"Вы выбрали направление: <b>{category.capitalize()}</b>\nВведите название группы/студии/площадки:")
    await state.set_state(CatalogForm.name)
    await cb.answer()

@dp.callback_query(F.data.startswith("citysel:"))
async def city_selected(cb: CallbackQuery):
    slug = cb.data.split(":", 1)[1]
    city = await city_by_slug(slug)
    roots = await children_of(None)
    header = f"<b>Каталог → {city.name}</b>"
    markup = catalog_city_inline(slug, roots)
    await cb.message.edit_text(header, reply_markup=markup)
    await cb.answer()

@dp.callback_query(F.data.startswith("cat:"))
async def cat_handler(cb: CallbackQuery):
    _, city_slug, cat_slug = cb.data.split(":", 2)
    city = await city_by_slug(city_slug)
    async with SessionLocal() as s:
        cat = (await s.execute(select(Category).where(Category.slug == cat_slug))).scalar_one()
    children = await children_of(cat.id)
    names = [cat.name]
    cur = cat
    while cur.parent_id:
        async with SessionLocal() as s2:
            p = (await s2.execute(select(Category).where(Category.id == cur.parent_id))).scalar_one()
        names.append(p.name)
        cur = p
    path = " → ".join(reversed(names))
    header = f"<b>Каталог → {city.name} → {path}</b>"
    if children:
        markup = catalog_cat_inline(city_slug, children)
        await cb.message.edit_text(header, reply_markup=markup)
    else:
        # Здесь показываем содержимое выбранной категории (например, товары, услуги)
        items = await fetch_items(city.id, cat.id)
        text = header + ("\n\nПока нет анкет." if not items else "\n\n" + "\n\n".join(
            f"• <b>{i.title}</b>\n{i.descr or ''}\n<code>{i.contact}</code>" for i in items))
        markup = catalog_cat_inline(city_slug, [])  # Кнопка "Назад"
        await cb.message.edit_text(text, reply_markup=markup)
    await cb.answer()

@dp.callback_query(F.data == "catalog:back")
async def catalog_back(cb: CallbackQuery):
    await cb.message.edit_text("🏙 Каталог\nВыберите действие:", reply_markup=catalog_inline_initial())
    await cb.answer()

@dp.message(CatalogForm.name)
async def get_catalog_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text)
    await m.answer("Введите адрес (необязательно, можно пропустить):")
    await state.set_state(CatalogForm.address)

@dp.message(CatalogForm.address)
async def get_catalog_address(m: Message, state: FSMContext):
    await state.update_data(address=m.text)
    await m.answer("Прикрепите фото (можно до 3-х, или пропустите):")
    await state.set_state(CatalogForm.photo)

@dp.message(CatalogForm.photo)
async def get_catalog_photo(m: Message, state: FSMContext):
    await state.update_data(photo=m.text)
    await m.answer("Введите описание ваших умений или информации о группе/студии:")
    await state.set_state(CatalogForm.description)

@dp.message(CatalogForm.description)
async def get_catalog_description(m: Message, state: FSMContext):
    await state.update_data(description=m.text)
    await m.answer("Введите информацию о реп. базе (ссылка, если есть):")
    await state.set_state(CatalogForm.repo)

@dp.message(CatalogForm.repo)
async def get_catalog_repo(m: Message, state: FSMContext):
    data = await state.get_data()
    data["repo"] = m.text
    summary = (
        f"Направление: {data.get('category_choice')}\n"
        f"Название: {data.get('name')}\n"
        f"Адрес: {data.get('address')}\n"
        f"Фото: {data.get('photo')}\n"
        f"Описание: {data.get('description')}\n"
        f"Реп. база: {data.get('repo')}"
    )
    await m.answer(f"Проверьте введённые данные:\n\n{summary}\n\nПодтвердите отправку.", 
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                         [InlineKeyboardButton(text="Да", callback_data="confirm:yes"),
                          InlineKeyboardButton(text="Нет", callback_data="confirm:no")]
                     ]))
    await state.clear()

@dp.callback_query(F.data.startswith("confirm:"))
async def catalog_confirm_handler(cb: CallbackQuery, state: FSMContext):
    decision = cb.data.split(":", 1)[1]
    if decision == "yes":
        await cb.message.edit_text("Ваша заявка принята. Спасибо!")
    else:
        await cb.message.edit_text("Заявка отменена.")
    await state.clear()
    await cb.answer()

# ───────────── MARKET (Барахолка) Handlers ───────────── #


@dp.callback_query(F.data.startswith("mcity:"))
async def market_city(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    slug = cb.data.split(":", 1)[1]
    if slug == "choose":
        await cb.message.edit_text("💸 Барахолка – выберите действие:", reply_markup=market_inline())
        await cb.answer()
        return
    city = await city_by_slug(slug)
    async with SessionLocal() as s:
        equip = (await s.execute(select(Category).where(Category.slug == "equip"))).scalar_one()
    subs = await children_of(30)
    buttons = [[InlineKeyboardButton(text=sc.name, callback_data=f"mlist:{slug}:{sc.slug}")]
               for sc in subs]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="mcity:choose")])
    buttons.append([InlineKeyboardButton(text="☰ Главное меню", callback_data="main_menu")])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await cb.message.delete()  # Удаляем старый список объявлений
    except Exception:
        pass
    msg = await cb.bot.send_message(
        cb.message.chat.id, 
        f"<b>Барахолка → {city.name}</b>", 
        reply_markup=markup, 
        parse_mode="HTML"
    )
    last_bot_messages[chat_id] = [msg.message_id]

    await cb.answer()


@dp.callback_query(F.data.startswith("mlist:"))
async def market_list(cb: CallbackQuery):
    chat_id = cb.message.chat.id

    # Удаляем старые фото-сообщения (как выше)
    photo_ids = sent_photo_messages.pop(chat_id, [])
    for msg_id in photo_ids:
        try:
            await cb.bot.delete_message(chat_id, msg_id)
        except Exception:
            pass

    # Удаляем "лишние" сообщения (старое меню), если они есть
    try:
        await cb.message.delete()
    except Exception:
        pass

    _, city_slug, cat_slug = cb.data.split(":", 2)
    city = await city_by_slug(city_slug)
    async with SessionLocal() as s:
        cat = (await s.execute(select(Category).where(Category.slug == cat_slug))).scalar_one()
        children = (await s.execute(select(Category).where(Category.parent_id == cat.id))).scalars().all()

    # Если есть подкатегории — показываем их
    if children:
        buttons = [[InlineKeyboardButton(text=child.name, callback_data=f"mlist:{city_slug}:{child.slug}")]
                   for child in children]
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mcity:{city_slug}")])
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)
        msg = await cb.bot.send_message(
            chat_id,
            f"<b>Барахолка → {city.name} → {cat.name}</b>\n\nВыберите раздел:",
            reply_markup=markup,
            parse_mode="HTML"
        )
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await cb.answer()
        return

    # Если подкатегорий нет — показываем объявления
    listings = await fetch_listings(city.id, cat.id)
    keyboard = [
        [InlineKeyboardButton(
            text=f"{listing.title}" + (f" — {listing.price}" if listing.price else ""),
            callback_data=f"listing:{listing.id}:{city_slug}:{cat_slug}"
        )]
        for listing in listings
    ]
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mcity:{city_slug}")])
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    msg = await cb.bot.send_message(
        chat_id,
        f"<b>Барахолка → {city.name} → {cat.name}</b>\n\nВыберите объявление:",
        reply_markup=markup,
        parse_mode="HTML"
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await cb.answer()


@dp.callback_query(F.data == "my_listings")
async def my_listings_handler(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    user_id = cb.from_user.id

    # Удаляем карточки моих объявлений
    for msg_id in my_listing_messages.get(chat_id, []):
        try:
            await cb.bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
    my_listing_messages[chat_id] = []

    await clear_bot_messages(chat_id, cb.bot)
    await state.clear()

    async with SessionLocal() as s:
        listings = (await s.execute(
            select(Listing)
            .where(Listing.owner_id == user_id)
            .order_by(Listing.created_at.desc())
            .limit(10)
        )).scalars().all()

    if not listings:
        await safe_edit_or_send(cb, "У вас пока нет опубликованных объявлений.", await build_main_menu())
        await cb.answer()
        return

    keyboard = [
        [InlineKeyboardButton(
            text=f"{listing.title}" + (f" — {listing.price}" if listing.price else ""),
            callback_data=f"listing:{listing.id}:{listing.city_id}:{listing.category_id}:my"
        )]
        for listing in listings
    ]
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="go_market")])
    keyboard.append([InlineKeyboardButton(text="☰ Главное меню", callback_data="main_menu")])
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    await safe_edit_or_send(cb, "<b>Ваши объявления:</b>\nВыберите для просмотра или управления.", markup)
    await cb.answer()


@dp.callback_query(F.data == "my_listings_back")
async def my_listings_back_handler(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    user_id = cb.from_user.id

    # Удаляем все карточки объявлений, отправленные ранее (фото и текст)
    if my_listing_messages.get(chat_id):
        for msg_id in my_listing_messages[chat_id]:
            try:
                await cb.bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
        my_listing_messages[chat_id] = []

    # Также удаляем само сообщение с кнопками (если оно не из списка выше)
    try:
        await cb.message.delete()
    except Exception:
        pass

    # Удаляем прочие служебные сообщения (например, подсказки)
    await clear_bot_messages(chat_id, cb.bot)

    # Показываем список ваших объявлений
    async with SessionLocal() as s:
        listings = (await s.execute(
            select(Listing)
            .where(Listing.owner_id == user_id)
            .order_by(Listing.created_at.desc())
            .limit(10)
        )).scalars().all()

    if not listings:
        await safe_edit_or_send(cb, "У вас пока нет опубликованных объявлений.", await build_main_menu())
        await cb.answer()
        return


    keyboard = [
        [InlineKeyboardButton(
            text=f"{listing.title}" + (f" — {listing.price}" if listing.price else ""),
            callback_data=f"listing:{listing.id}:{listing.city_id}:{listing.category_id}:my"
        )]
        for listing in listings
    ]
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="go_market")])
    keyboard.append([InlineKeyboardButton(text="☰ Главное меню", callback_data="main_menu")])
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    await safe_edit_or_send(cb, "<b>Ваши объявления:</b>\nВыберите для просмотра или управления.", markup)
    await cb.answer()




@dp.callback_query(F.data.startswith("listing:"))
async def show_listing_details(cb: CallbackQuery):
    chat_id = cb.message.chat.id

    # Удаляем старое меню объявлений (то, что с кнопкой Назад)
    try:
        await cb.message.delete()
    except Exception:
        pass
    parts = cb.data.split(":")
    listing_id = int(parts[1])
    city_slug = parts[2]
    cat_slug = parts[3]
    from_my = len(parts) > 4 and parts[4] == "my"
    chat_id = cb.message.chat.id

    # --- Удаляем все старые карточки моих объявлений (Вариант А) ---
    if from_my:
        for msg_id in my_listing_messages.get(chat_id, []):
            try:
                await cb.bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
        my_listing_messages[chat_id] = []

    # --- Загрузка объявления из БД ---
    async with SessionLocal() as s:
        listing = (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one()
    photo_ids = listing.photo_file_id.split(",") if listing.photo_file_id else []

    caption = f"<b>{listing.title}</b>\n"
    if listing.price:
        caption += f"Цена: {listing.price}\n"
    if listing.descr:
        caption += f"{listing.descr}\n"
    caption += f"Контакт: {listing.contact}"

    buttons = []
    if listing.owner_id == cb.from_user.id:
        buttons.append([InlineKeyboardButton(text="Удалить объявление", callback_data=f"sell_sold:{listing.id}")])
    elif listing.contact and listing.contact.startswith("@"):
        username = listing.contact.lstrip("@")
        buttons.append([InlineKeyboardButton(text="💬 Связаться с продавцом", url=f"https://t.me/{username}")])
    # Кнопка "Назад"
    if from_my:
        buttons.append([InlineKeyboardButton(text="⬅️ Назад к моим объявлениям", callback_data="my_listings_back")])
    else:
        # ⬇️⬇️ возвращаем с помощью слагов!
        buttons.append([InlineKeyboardButton(text="⬅️ Назад к объявлениям", callback_data=f"mlist:{city_slug}:{cat_slug}")])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    sent_ids = []
    if photo_ids and photo_ids[0]:
        if len(photo_ids) == 1:
            msg = await cb.message.answer_photo(photo_ids[0], caption=caption, reply_markup=markup, parse_mode="HTML")
            sent_ids.append(msg.message_id)
        else:
            from aiogram.types import InputMediaPhoto
            media_group = [InputMediaPhoto(media=photo_ids[0], caption=caption, parse_mode="HTML")]
            for pid in photo_ids[1:]:
                media_group.append(InputMediaPhoto(media=pid))
            msgs = await cb.message.answer_media_group(media=media_group)
            msg2 = await cb.message.answer("Контакты/Управление:", reply_markup=markup)
            sent_ids.extend([m.message_id for m in msgs])
            sent_ids.append(msg2.message_id)
    else:
        msg = await cb.message.answer(caption, reply_markup=markup, parse_mode="HTML")
        sent_ids.append(msg.message_id)

    # --- Запоминаем, чтобы потом чистить (только для моих объявлений) ---
    if from_my and sent_ids:
        my_listing_messages[chat_id].extend(sent_ids)

    # Для обычных объявлений (не мои) — остается sent_photo_messages, если используете
    if not from_my and sent_ids:
        sent_photo_messages.setdefault(chat_id, []).extend(sent_ids)
    if from_my and sent_ids:
        my_listing_messages[chat_id].extend(sent_ids)
        print("my_listing_messages[{}]: {}".format(chat_id, my_listing_messages[chat_id]))

    await cb.answer()



@dp.callback_query(F.data.startswith("showphoto:"))
async def show_listing_photo(cb: CallbackQuery):
    _, listing_id, city_slug, cat_slug = cb.data.split(":")
    listing_id = int(listing_id)
    chat_id = cb.message.chat.id
    async with SessionLocal() as s:
        listing = (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one()
    photo_ids = listing.photo_file_id.split(",") if listing.photo_file_id else []

    caption = f"<b>{listing.title}</b>\n"
    if listing.price:
        caption += f"Цена: {listing.price}\n"
    if listing.descr:
        caption += f"{listing.descr}\n"
    caption += f"Контакт: {listing.contact}"

    sent_ids = []

    if photo_ids:
        if len(photo_ids) == 1:
            msg = await cb.message.answer_photo(
                photo_ids[0],
                caption=caption,
                parse_mode="HTML"
            )
            sent_ids.append(msg.message_id)
        else:
            from aiogram.types import InputMediaPhoto
            media_group = [InputMediaPhoto(media=photo_ids[0], caption=caption, parse_mode="HTML")]
            for pid in photo_ids[1:]:
                media_group.append(InputMediaPhoto(media=pid))
            msgs = await cb.message.answer_media_group(media=media_group)
            # answer_media_group возвращает список сообщений
            sent_ids.extend([m.message_id for m in msgs])
    else:
        await cb.answer("Фото не найдено.", show_alert=True)

    # Сохраняем ID отправленных фото-сообщений для этого чата
    if sent_ids:
        sent_photo_messages.setdefault(chat_id, []).extend(sent_ids)

    await cb.answer()




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




@dp.callback_query(F.data.startswith("toggle:"))
async def toggle_listing(cb: CallbackQuery):
    # Data format: toggle:{city_slug}:{cat_slug}:{listing_id}
    parts = cb.data.split(":")
    if len(parts) != 4:
        await cb.answer("Ошибка данных.")
        return
    _, city_slug, cat_slug, listing_id_str = parts
    try:
        listing_id = int(listing_id_str)
    except ValueError:
        await cb.answer("Неверный идентификатор объявления.")
        return
    chat_id = cb.message.chat.id
    current_expanded = expanded_listing_by_chat.get(chat_id)
    if current_expanded and current_expanded != listing_id:
        msg_id = listing_message_ids[chat_id].get(current_expanded)
        if msg_id:
            async with SessionLocal() as s:
                try:
                    listing = (await s.execute(select(Listing).where(Listing.id == current_expanded))).scalar_one()
                except NoResultFound:
                    listing = None
            if listing:
                header = f"• <b>{listing.title}</b>"
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"{listing.title} — Развернуть",
                                            callback_data=f"toggle:{city_slug}:{cat_slug}:{listing.id}")]
                    ]
                )
                await bot.edit_message_text(header, chat_id=str(chat_id), message_id=msg_id, reply_markup=keyboard, parse_mode="HTML")
        expanded_listing_by_chat[chat_id] = None

    logging.debug(f"Toggle handler called in chat {chat_id} for listing {listing_id}")
    msg_id_current = listing_message_ids[chat_id].get(listing_id)
    if not msg_id_current:
        await cb.answer("Сообщение не найдено.")
        return
    async with SessionLocal() as s:
        try:
            listing = (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one()
        except NoResultFound:
            await bot.edit_message_text("Объявление не найдено или было удалено.", chat_id=str(chat_id), message_id=msg_id_current)
            await cb.answer()
            return
    if expanded_listing_by_chat.get(chat_id) == listing_id:
        header = f"• <b>{listing.title}</b>"
        button_text = f"{listing.title} — Развернуть"
        new_reply = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=button_text, callback_data=f"toggle:{city_slug}:{cat_slug}:{listing.id}")]
        ])
        await bot.edit_message_text(header, chat_id=str(chat_id), message_id=msg_id_current, reply_markup=new_reply, parse_mode="HTML")
        expanded_listing_by_chat[chat_id] = None
    else:
        details = (f"\n    Цена: {listing.price}"
                   f"\n    {listing.descr or 'Нет описания'}"
                   f"\n    Контакт: {listing.contact}")
        full_text = f"• <b>{listing.title}</b>{details}"
        button_text = f"{listing.title} — Свернуть"
        new_reply = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=button_text, callback_data=f"toggle:{city_slug}:{cat_slug}:{listing.id}")]
        ])
        await bot.edit_message_text(full_text, chat_id=str(chat_id), message_id=msg_id_current, reply_markup=new_reply, parse_mode="HTML")
        expanded_listing_by_chat[chat_id] = listing_id
    await cb.answer()

@dp.callback_query(F.data.startswith("item_detail:"))
async def item_detail_handler(cb: CallbackQuery):
    item_id = int(cb.data.split(":", 1)[1])
    async with SessionLocal() as s:
        try:
            listing = (await s.execute(select(Listing).where(Listing.id == item_id))).scalar_one()
        except NoResultFound:
            await cb.message.answer("Объявление не найдено или было удалено.")
            await cb.answer()
            return
    text = f"<b>{listing.title}</b> — {listing.price}\n{listing.descr or 'Нет описания'}\n<code>{listing.contact}</code>"
    photo_ids = listing.photo_file_id.split(",") if listing.photo_file_id else []

    seller_button = None
    if listing.contact and listing.contact.startswith("@"):
        seller_button = InlineKeyboardButton(text="Написать продавцу",
                                             url=f"https://t.me/{listing.contact.lstrip('@')}")
    detail_kb = InlineKeyboardMarkup(inline_keyboard=[])
    if seller_button:
        detail_kb = InlineKeyboardMarkup(inline_keyboard=[[seller_button]])
    if photo_ids:
        if len(photo_ids) == 1:
            await cb.message.answer_photo(photo_ids[0], caption=text, reply_markup=detail_kb)
        else:
            from aiogram.types import InputMediaPhoto
            media_group = [InputMediaPhoto(media=photo_ids[0], caption=text)]
            for pid in photo_ids[1:]:
                media_group.append(InputMediaPhoto(media=pid))
            await cb.message.answer_media_group(media=media_group)
            if seller_button:
                await cb.message.answer("Связаться с продавцом:", reply_markup=detail_kb)
    else:
        await cb.message.answer(text, reply_markup=detail_kb)
    await cb.answer()


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



@dp.callback_query(F.data == "market_search")
async def market_search_start(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # Удаляем все предыдущее (универсально)
    await clear_bot_messages(chat_id, cb.bot)

    # Удаляем меню (которое вызвало этот callback)
    try:
        await cb.message.delete()
    except Exception:
        pass

    # Удаляем сообщения поиска, если есть (страховка)
    old_menu_id = last_search_menu_message.pop(chat_id, None)
    if old_menu_id:
        try:
            await cb.bot.delete_message(chat_id, old_menu_id)
        except Exception:
            pass
    old_query_id = last_search_query_message.pop(chat_id, None)
    if old_query_id:
        try:
            await cb.bot.delete_message(chat_id, old_query_id)
        except Exception:
            pass

    # Приглашение к поиску
    def nav_kb(back: bool = True, main: bool = True) -> InlineKeyboardMarkup:
        buttons = []
        if back:
            buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data="market_menu_back"))
        if main:
            buttons.append(InlineKeyboardButton(text="☰ Главное меню", callback_data="main_menu"))
        return InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None

    # Сначала отправляем кнопки
    nav_msg = await cb.bot.send_message(
        chat_id,
        "⬅️ Назад | ☰ Главное меню",
        reply_markup=nav_kb()  # ваша функция генерации кнопок
    )

    # Затем — текст запроса (пользователь вводит прямо под ним)
    query_msg = await cb.bot.send_message(
        chat_id,
        "Введите запрос для поиска по объявлениям (например: микрофон, Yamaha, комбик):"
    )

    # (если нужно отслеживать оба сообщения для удаления)
    last_search_query_message[chat_id] = query_msg.message_id
    last_search_menu_message[chat_id] = nav_msg.message_id  # например так, если надо

    await state.set_state(MarketSearch.waiting_for_query)
    await cb.answer()





from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

@dp.message(MarketSearch.waiting_for_query)
async def handle_market_search(m: Message, state: FSMContext):
    chat_id = m.chat.id

    # Удаляем предыдущее приглашение и меню
    await clear_bot_messages(chat_id, m.bot)

    old_query_id = last_search_query_message.pop(chat_id, None)
    if old_query_id:
        try:
            await m.bot.delete_message(chat_id, old_query_id)
        except Exception:
            pass
    old_menu_id = last_search_menu_message.pop(chat_id, None)
    if old_menu_id:
        try:
            await m.bot.delete_message(chat_id, old_menu_id)
        except Exception:
            pass

    query = m.text.strip()
    async with SessionLocal() as s:
        results = (await s.execute(
            select(Listing)
            .where(Listing.is_sold.is_(False))
            .where(Listing.title.ilike(f"%{query}%") | Listing.descr.ilike(f"%{query}%"))
            .order_by(Listing.created_at.desc())
            .limit(10)
        )).scalars().all()

    if not results:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Новый поиск", callback_data="market_search")],
                [InlineKeyboardButton(text="⬅️ В меню Барахолки", callback_data="market_menu_back")]
            ]
        )
        msg = await m.answer(
            f"😕 Ничего не найдено по запросу: <b>{query}</b>.\n\n"
            "Попробуйте другой поисковый запрос или вернитесь в меню поиска.",
            reply_markup=kb,
            parse_mode="HTML"
        )
        last_search_menu_message[chat_id] = msg.message_id
        await state.clear()
        return

    await state.update_data(search_results=[l.id for l in results], search_query=query)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=(l.title if len(l.title) < 45 else l.title[:42] + "…"),
                callback_data=f"search_detail:{l.id}"
            )] for l in results
        ] + [
            [InlineKeyboardButton(text="❌ Новый поиск", callback_data="market_search")],
            [InlineKeyboardButton(text="⬅️ В меню Барахолки", callback_data="market_menu_back")]
        ]
    )
    msg = await m.answer(
        f"🔎 Найдено объявлений: <b>{len(results)}</b> по запросу: <b>{query}</b>\n\nВыберите объявление:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    last_search_menu_message[chat_id] = msg.message_id

    await state.set_state(MarketSearch.waiting_for_detail)



@dp.callback_query(F.data.startswith("search_listing:"))
async def show_search_listing(cb: CallbackQuery):
    listing_id = int(cb.data.split(":", 1)[1])
    async with SessionLocal() as s:
        listing = (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one()
    photo_ids = listing.photo_file_id.split(",") if listing.photo_file_id else []

    caption = f"<b>{listing.title}</b>\n"
    if listing.price:
        caption += f"Цена: {listing.price}\n"
    if listing.descr:
        caption += f"{listing.descr}\n"
    caption += f"Контакт: {listing.contact}"

    btns = []
    if listing.owner_id == cb.from_user.id:
        btns.append([InlineKeyboardButton(text="Продано", callback_data=f"sell_sold:{listing.id}")])
    elif listing.contact and listing.contact.startswith("@"):
        username = listing.contact.lstrip("@")
        btns.append([InlineKeyboardButton(text="💬 Связаться с продавцом", url=f"https://t.me/{username}")])

    # Кнопка "⬅️ Назад к поиску"
    btns.append([InlineKeyboardButton(text="⬅️ Назад к поиску", callback_data="back_to_market_search")])
    markup = InlineKeyboardMarkup(inline_keyboard=btns)

    if photo_ids and photo_ids[0]:
        await cb.message.answer_photo(photo_ids[0], caption=caption, reply_markup=markup, parse_mode="HTML")
    else:
        await cb.message.answer(caption, reply_markup=markup, parse_mode="HTML")
    await cb.answer()

@dp.callback_query(F.data == "back_to_market_search")
async def back_to_market_search(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите новый поисковый запрос по объявлениям Барахолки:")
    await state.set_state(MarketSearch.waiting_for_query)
    await cb.answer()


@dp.callback_query(F.data == "market_search_back")
async def market_search_back(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    last_result_ids = data.get("last_search_results", [])
    if not last_result_ids:
        await cb.message.answer("Результаты поиска не найдены. Начните новый поиск.")
        await state.clear()
        return
    # Получаем объекты Listing по id
    async with SessionLocal() as s:
        results = (await s.execute(select(Listing).where(Listing.id.in_(last_result_ids)))).scalars().all()
    await show_market_search_results(cb.message, state, results)
    await cb.answer()

@dp.callback_query(F.data == "market_search_new")
async def market_search_new(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("Введите новый поисковый запрос:")
    await state.set_state(MarketSearch.waiting_for_query)
    await cb.answer()


@dp.callback_query(F.data.startswith("search_detail:"), MarketSearch.waiting_for_detail)
async def show_search_detail(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)
    listing_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        listing = (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one()
        city = (await s.execute(select(City).where(City.id == listing.city_id))).scalar_one()
        cat = (await s.execute(select(Category).where(Category.id == listing.category_id))).scalar_one()
        city_slug = city.slug
        cat_slug = cat.slug

    caption = f"<b>{listing.title}</b>\n"
    if listing.price:
        caption += f"Цена: {listing.price}\n"
    if listing.descr:
        caption += f"{listing.descr}\n"
    caption += f"Контакт: {listing.contact}"

    btns = []
    if listing.owner_id == cb.from_user.id:
        btns.append([InlineKeyboardButton(text="Продано", callback_data=f"sell_sold:{listing.id}")])
    elif listing.contact and listing.contact.startswith("@"):
        username = listing.contact.lstrip("@")
        btns.append([InlineKeyboardButton(text="💬 Связаться с продавцом", url=f"https://t.me/{username}")])
    btns.append([InlineKeyboardButton(text="⬅️ Назад к поиску", callback_data="market_search_results")])
    markup = InlineKeyboardMarkup(inline_keyboard=btns)

    photo_ids = listing.photo_file_id.split(",") if listing.photo_file_id else []

    sent_ids = []
    if photo_ids:
        from aiogram.types import InputMediaPhoto
        if len(photo_ids) == 1:
            msg = await cb.message.answer_photo(photo_ids[0], caption=caption, reply_markup=markup, parse_mode="HTML")
            sent_ids.append(msg.message_id)
        else:
            # Медиагруппа: первая с подписью, остальные без
            media_group = [InputMediaPhoto(media=photo_ids[0], caption=caption, parse_mode="HTML")]
            for pid in photo_ids[1:]:
                media_group.append(InputMediaPhoto(media=pid))
            msgs = await cb.message.answer_media_group(media=media_group)
            msg2 = await cb.message.answer("Контакты/Управление:", reply_markup=markup)
            sent_ids.extend([m.message_id for m in msgs])
            sent_ids.append(msg2.message_id)
    else:
        msg = await cb.message.answer(caption, reply_markup=markup, parse_mode="HTML")
        sent_ids.append(msg.message_id)
        # sent_photo_messages.setdefault(chat_id, []).extend(sent_ids)

    # Для корректного удаления при возврате к поиску — сохраняем ID отправленных сообщений
    if sent_ids:
        # from collections import defaultdict
        # if not hasattr(cb.bot, "sent_photo_messages"):
        #     cb.bot.sent_photo_messages = defaultdict(list)
        # cb.bot.sent_photo_messages[cb.message.chat.id].extend(sent_ids)
        # если у вас уже есть глобальный sent_photo_messages — используйте его:
        sent_photo_messages.setdefault(cb.message.chat.id, []).extend(sent_ids)

    await cb.answer()


@dp.callback_query(F.data == "market_search_results", MarketSearch.waiting_for_detail)
async def back_to_search_results(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # Удаляем карточки объявлений (фото и кнопки), отправленные ранее
    await clear_bot_messages(chat_id, cb.bot)

    old_menu_id = last_search_menu_message.pop(chat_id, None)
    if old_menu_id:
        try:
            await cb.bot.delete_message(chat_id, old_menu_id)
        except Exception:
            pass
    old_query_id = last_search_query_message.pop(chat_id, None)
    if old_query_id:
        try:
            await cb.bot.delete_message(chat_id, old_query_id)
        except Exception:
            pass

    data = await state.get_data()
    ids = data.get("search_results", [])
    query = data.get("search_query", "")
    if not ids:
        msg = await cb.message.answer("Результаты поиска не найдены.")
        last_search_menu_message[chat_id] = msg.message_id
        await state.clear()
        return

    async with SessionLocal() as s:
        results = (await s.execute(select(Listing).where(Listing.id.in_(ids)))).scalars().all()

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=(l.title if len(l.title) < 45 else l.title[:42] + "…"),
                callback_data=f"search_detail:{l.id}"
            )] for l in results
        ] + [
            [InlineKeyboardButton(text="❌ Новый поиск", callback_data="market_search")],
            [InlineKeyboardButton(text="⬅️ В меню Барахолки", callback_data="market_menu_back")]
        ]
    )
    msg = await cb.message.answer(
        f"🔎 Найдено объявлений: <b>{len(results)}</b> по запросу: <b>{query}</b>\n\nВыберите объявление:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    last_search_menu_message[chat_id] = msg.message_id

    await state.set_state(MarketSearch.waiting_for_detail)
    await cb.answer()



@dp.callback_query(F.data == "market_menu_back")
async def market_menu_back(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # Удаляем старое меню поиска, если оно есть
    old_menu_id = last_search_menu_message.pop(chat_id, None)
    if old_menu_id:
        try:
            await cb.bot.delete_message(chat_id, old_menu_id)
        except Exception:
            pass

    # Удаляем старое сообщение "Введите запрос...", если оно было
    old_query_id = last_search_query_message.pop(chat_id, None)
    if old_query_id:
        try:
            await cb.bot.delete_message(chat_id, old_query_id)
        except Exception:
            pass

    # Удаляем сообщения с фото и др.
    await clear_bot_messages(chat_id, cb.bot)

    await state.clear()
    msg = await cb.message.answer("💸 Барахолка – выберите действие:", reply_markup=market_inline())
    last_bot_messages[chat_id] = [msg.message_id]
    await cb.answer()




# ───────────────── Entrypoint ───────────────────────────── #
async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())