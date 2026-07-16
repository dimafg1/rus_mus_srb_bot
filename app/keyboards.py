 # app/keyboards.py

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from typing import List
from app.models import Category, City
from sqlalchemy import select
from app.database import SessionLocal  # если у вас по-другому — исправьте путь!
from app.models import Menu, City  # или как у вас называется эта модель
from app.routers.utils import get_text
from app.routers.utils import get_catalog_categories
from app.routers.admin_panel import is_admin
async def get_common_menu_button(code: str, lang="ru"):
    async with SessionLocal() as session:
        result = await session.execute(
            select(Menu).where(Menu.code == code, Menu.visible == 1, Menu.lang == lang)
        )
        btn = result.scalars().first()
    if btn:
        return InlineKeyboardButton(
            text=f"{btn.icon} {btn.text}" if btn.icon else btn.text,
            callback_data=btn.callback_data
        )
    return None

# async def build_city_buttons(callback_prefix: str, lang: str = "ru"):
#     async with SessionLocal() as session:
#         result = await session.execute(select(City))
#         cities = result.scalars().all()
#     return [
#         InlineKeyboardButton(
#             text=city.name,
#             callback_data=f"{callback_prefix}:{city.slug}"
#         ) for city in cities
#     ]

async def get_back_button(code="back", lang="ru"):
    """
    Получить кнопку Назад с нужным callback_data (по умолчанию — обычная 'back', для sell — 'sell_back').
    """
    btn = await get_common_menu_button(code, lang)
    return btn


# ---------- Барахолка ----------
async def build_city_buttons(callback_prefix: str, lang: str = "ru"):
    async with SessionLocal() as session:
        result = await session.execute(select(City))
        cities = result.scalars().all()
    return [
        InlineKeyboardButton(
            text=city.name,
            callback_data=f"{callback_prefix}:{city.slug}"
        ) for city in cities
    ]

async def market_inline(lang="ru"):
    # Получаем все пункты для меню "Барахолка" из базы
    async with SessionLocal() as session:
        result = await session.execute(
            select(Menu)
            .where(Menu.parent_code == "market", Menu.visible == 1, Menu.lang == lang)
            .order_by(Menu.order_num)
        )
        rows = result.scalars().all()
    
    # Сборка клавиатуры
    keyboard = []

    # Первый пункт (обычно "🔎 Поиск по объявлениям")
    first_row = []
    for row in rows:
        if row.callback_data == "market_search":
            first_row.append(
                InlineKeyboardButton(
                    text=f"{row.icon} {row.text}" if row.icon else row.text,
                    callback_data=row.callback_data
                )
            )
    if first_row:
        keyboard.append(first_row)

    # Второй блок — города (по 2 в ряд)
    city_buttons = await build_city_buttons("mcity")
    if city_buttons:
        for i in range(0, len(city_buttons), 2):
            keyboard.append(city_buttons[i:i + 2])

    # Остальные пункты меню (например, "Мои объявления", "Разместить объявление")
    for row in rows:
        if row.callback_data not in ("market_search",):  # уже добавили поиск, пропускаем
            keyboard.append([
                InlineKeyboardButton(
                    text=f"{row.icon} {row.text}" if row.icon else row.text,
                    callback_data=row.callback_data
                )
            ])

    # Добавляем "Главное меню" (общий код)
    from app.keyboards import get_common_menu_button
    main_menu_btn = await get_common_menu_button('main_menu', lang)
    if main_menu_btn:
        keyboard.append([main_menu_btn])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)



async def market_city_inline(city_name, city_slug, subcats):
    buttons = [
        [InlineKeyboardButton(text=name, callback_data=cb_data)]
        for name, cb_data in subcats
    ]

    back_btn = await get_common_menu_button('back')
    if back_btn:
        buttons.append([back_btn])

    main_menu_btn = await get_common_menu_button('main_menu')
    if main_menu_btn:
        buttons.append([main_menu_btn])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def market_list_inline(city_name, city_slug, cat_name, listings):
    buttons = [
        [InlineKeyboardButton(text=title, callback_data=cb_data)]
        for title, cb_data in listings
    ]

    back_btn = await get_common_menu_button('back')
    if back_btn:
        buttons.append([back_btn])

    main_menu_btn = await get_common_menu_button('main_menu')
    if main_menu_btn:
        buttons.append([main_menu_btn])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def listing_detail_keyboard(is_owner: bool, listing_id: int, city_slug: str, cat_slug: str, contact: str):
    buttons = []
    if is_owner:
        buttons.append([InlineKeyboardButton(text="Продано", callback_data=f"sell_sold:{listing_id}")])
    elif contact and contact.startswith("@"):
        buttons.append([InlineKeyboardButton(text="💬 Связаться с продавцом", url=f"https://t.me/{contact.lstrip('@')}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад к объявлениям", callback_data=f"mlist:{city_slug}:{cat_slug}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ---------- S E L L (разместить объявление) ----------
def photo_keyboard(photo_count: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пропустить этот шаг", callback_data="sell_skip_photo")],
        [InlineKeyboardButton(text="Отмена", callback_data="sell_cancel")]
    ])

def confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data="sell_ok")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="sell_cancel")],
    ])

def sold_keyboard(listing_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Продано", callback_data=f"sell_sold:{listing_id}")]
        ]
    )

def delete_keyboard(listing_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да, удалить", callback_data=f"sell_delete_yes:{listing_id}")],
            [InlineKeyboardButton(text="Нет, отменить", callback_data=f"sell_delete_no:{listing_id}")]
        ]
    )

async def cities_inline(cities) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=c.name, callback_data=f"sell_city:{c.slug}")] for c in cities]

    back_btn = await get_back_button('sell_back')
    if back_btn:
        rows.append([back_btn])

    main_menu_btn = await get_common_menu_button('main_menu')
    if main_menu_btn:
        rows.append([main_menu_btn])

    return InlineKeyboardMarkup(inline_keyboard=rows)

async def equip_inline(categories: List[Category], city_slug: str, lang="ru") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=c.name, callback_data=f"sell_cat:{city_slug}:{c.id}")]
        for c in categories
    ]

    back_btn = await get_back_button('sell_back', lang)
    if back_btn:
        rows.append([back_btn])

    main_menu_btn = await get_common_menu_button('main_menu', lang)
    if main_menu_btn:
        rows.append([main_menu_btn])

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ------------------------------------------------------------ Каталог ----------------------------------------------------------------------

async def catalog_inline_initial(lang="ru"):
    city_buttons = await build_city_buttons("citysel", lang)
    keyboard = [
        [InlineKeyboardButton(text="🔎 Поиск по каталогу", callback_data="catalog_search")],
        city_buttons,
        [InlineKeyboardButton(text="📝 ПОДАТЬ ЗАЯВКУ", callback_data="apply_catalog")]
    ]

    main_menu_btn = await get_common_menu_button('main_menu', lang)
    if main_menu_btn:
        keyboard.append([main_menu_btn])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def catalog_city_inline(city_slug: str, categories: List[Category], lang="ru"):
    alternative = "belgrade" if city_slug != "belgrade" else "novisad"
    buttons = [[InlineKeyboardButton(text=c.name, callback_data=f"cat:{city_slug}:{c.slug}")] for c in categories]

    back_btn = await get_common_menu_button('catalog_back', lang)
    if back_btn:
        buttons.append([back_btn])

    main_menu_btn = await get_common_menu_button('main_menu', lang)
    if main_menu_btn:
        buttons.append([main_menu_btn])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def catalog_profile_category_inline(city_slug: str, lang="ru"):
    async with SessionLocal() as session:
        parent = (await session.execute(
            select(Category).where(Category.slug == "profile")
        )).scalar_one_or_none()
        if not parent:
            categories = []
        else:
            categories = (await session.execute(
                select(Category).where(Category.parent_id == parent.id)
            )).scalars().all()
    buttons = [
        [InlineKeyboardButton(text=cat.name, callback_data=f"capcat:{city_slug}:{cat.slug}")]
        for cat in categories
    ]

    # Назад и Главное меню
    back_btn = await get_common_menu_button('catalog_city_back')
    if back_btn:
        buttons.append([InlineKeyboardButton(text=back_btn.text, callback_data='catalog_city_back')])
    main_menu_btn = await get_common_menu_button('main_menu')
    if main_menu_btn:
        buttons.append([InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def catalog_application_category_inline(parent_id=None):
    categories = await get_catalog_categories(parent_id=parent_id)
    inline_buttons = [
        [InlineKeyboardButton(text=cat.name, callback_data=f"cat:{cat.slug}")]
        for cat in categories
    ]
    inline_buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="catalog:back")])
    return InlineKeyboardMarkup(inline_keyboard=inline_buttons)

async def catalog_cities_inline(lang: str = "ru"):
    async with SessionLocal() as session:
        result = await session.execute(select(City))
        cities = result.scalars().all()
    rows = [
        [InlineKeyboardButton(text=city.name, callback_data=f"apply_city:{city.slug}")]
        for city in cities
    ]
    # Кнопка "Назад"
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="catalog_back")])
    # Кнопка "Главное меню"
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def catalog_profile_category_inline(categories: List[Category], city_slug: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=c.name, callback_data=f"profile_cat:{city_slug}:{c.id}")]
        for c in categories
    ]
    # Назад и Главное меню
    back_btn = await get_common_menu_button('catalog_city_back')
    if back_btn:
        rows.append([back_btn])
    main_menu_btn = await get_common_menu_button('main_menu')
    if main_menu_btn:
        rows.append([main_menu_btn])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def catalog_search_button():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔎 Поиск по каталогу", callback_data="catalog_search")]
        ]
    )

def catalog_search_results_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Назад к поиску", callback_data="catalog_search")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ]
    )

async def catalog_category_inline(categories, city_slug, parent_cat_id=None):
    buttons = []
    for cat in categories:
        buttons.append([
            InlineKeyboardButton(
                text=cat.name,
                callback_data=f"catalog_cat:{city_slug}:{cat.id}"
            )
        ])
    # Кнопка Назад, если передан parent_cat_id (или любая ваша логика)
    if parent_cat_id:
        buttons.append([
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data=f"catalog_back:{city_slug}:{parent_cat_id}"
            )
        ])
    # Главное меню
    buttons.append([
        InlineKeyboardButton(
            text="🏠 Главное меню", callback_data="main_menu"
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)



# ---------- Вакансии ----------
async def vacancy_main_inline_view(prefix: str, lang="ru"):
    city_buttons = await build_city_buttons(prefix, lang)
    keyboard = [
        city_buttons
    ]

    back_btn = await get_common_menu_button('back', lang)
    if back_btn:
        keyboard.append([back_btn])

    main_menu_btn = await get_common_menu_button('main_menu', lang)
    if main_menu_btn:
        keyboard.append([main_menu_btn])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def vacancy_category_inline(lang="ru"):
    keyboard = [
        [InlineKeyboardButton(text="Вокал", callback_data="vcat:vocal")],
        [InlineKeyboardButton(text="Музыканты", callback_data="vcat:musicians")],
        [InlineKeyboardButton(text="Звукорежиссер", callback_data="vcat:sound")]
    ]

    back_btn = await get_common_menu_button('back', lang)
    if back_btn:
        keyboard.append([back_btn])

    main_menu_btn = await get_common_menu_button('main_menu', lang)
    if main_menu_btn:
        keyboard.append([main_menu_btn])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def musicians_sub_inline(lang="ru"):
    keyboard = [
        [InlineKeyboardButton(text="клавиши", callback_data="vsub:keys")],
        [InlineKeyboardButton(text="гитара", callback_data="vsub:guitar")],
        [InlineKeyboardButton(text="бас", callback_data="vsub:bass")]
    ]

    back_btn = await get_common_menu_button('back', lang)
    if back_btn:
        keyboard.append([back_btn])

    main_menu_btn = await get_common_menu_button('main_menu', lang)
    if main_menu_btn:
        keyboard.append([main_menu_btn])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# ---------- Афиша ----------
async def events_main_inline(lang="ru"):
    city_buttons = await build_city_buttons("ecity", lang)

    # --- гарантированно разбиваем города по 2 в строке ---
    city_rows = []
    temp_row = []

    for btn in city_buttons:
        temp_row.append(btn)
        if len(temp_row) == 2:
            city_rows.append(temp_row)
            temp_row = []

    if temp_row:  # если нечётное количество
        city_rows.append(temp_row)

    # --- МЕНЮ: Поиск -> Города -> Мои объявления -> остальное ---
    keyboard = [
        [InlineKeyboardButton(text="🔎 Поиск", callback_data="af:search")],
        [InlineKeyboardButton(text="🗓 Календарь", callback_data="af:cal:all")],
    ] + city_rows + [
        [InlineKeyboardButton(text="👤 Мои объявления", callback_data="af:my")],
        [InlineKeyboardButton(text="Ближайшие мероприятия", callback_data="events:near")],
        [InlineKeyboardButton(text="➕ РАЗМЕСТИТЬ ИНФОРМАЦИЮ", callback_data="event_new")],
    ]

    main_menu_btn = await get_common_menu_button('main_menu', lang)
    if main_menu_btn:
        keyboard.append([main_menu_btn])

    print(f"[keyboards.py][events_main_inline] CALLED | cities={len(city_buttons)} | rows={len(keyboard)}")
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def build_main_menu(lang='ru') -> InlineKeyboardMarkup:
    async with SessionLocal() as session:
        result = await session.execute(
            select(Menu)
            .where(Menu.parent_code == "main_menu", Menu.visible == 1, Menu.lang == lang)
            .order_by(Menu.order_num)
        )
        rows = result.scalars().all()
    keyboard = [
        [InlineKeyboardButton(text=row.text, callback_data=row.callback_data)]
        for row in rows
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

