 # app/keyboards.py

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from typing import List
from app.models import Category, City
from sqlalchemy import select
from app.database import SessionLocal  # если у вас по-другому — исправьте путь!
from app.models import Menu  # или как у вас называется эта модель


# ---------- Главное меню ----------
def main_inline_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📂 Каталог", callback_data="go_catalog"),
                InlineKeyboardButton(text="🤝 Ищу", callback_data="go_isk")
            ],
            [
                InlineKeyboardButton(text="📅 Афиша", callback_data="go_events"),
                InlineKeyboardButton(text="💸 Барахолка", callback_data="go_market")
            ],
            [
                InlineKeyboardButton(text="❓ Помощь", callback_data="go_help")
            ]
        ]
    )

# ---------- Барахолка ----------
def market_inline():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔎 Поиск по объявлениям", callback_data="market_search")],
            [InlineKeyboardButton(text="Белград", callback_data="mcity:belgrade"),
             InlineKeyboardButton(text="Нови Сад", callback_data="mcity:novisad")],
            [InlineKeyboardButton(text="📋 Мои объявления", callback_data="my_listings")],    # <-- ВСТАВЛЯЕМ СЮДА
            [InlineKeyboardButton(text="➕ РАЗМЕСТИТЬ ОБЪЯВЛЕНИЕ", callback_data="sell_start")],
            [InlineKeyboardButton(text="☰ Главное меню", callback_data="main_menu")]
        ]
    )


def market_city_inline(city_name, city_slug, subcats):
    # subcats — список (имя, cb_data)
    buttons = [
        [InlineKeyboardButton(text=name, callback_data=cb_data)]
        for name, cb_data in subcats
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="mcity:choose")])
    buttons.append([InlineKeyboardButton(text="☰ Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def market_list_inline(city_name, city_slug, cat_name, listings):
    # listings — список (title, cb_data)
    buttons = [
        [InlineKeyboardButton(text=title, callback_data=cb_data)]
        for title, cb_data in listings
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mcity:{city_slug}")])
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
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="sell_back")])
    rows.append([InlineKeyboardButton(text="☰ Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def equip_inline(categories: List[Category], city_slug: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=c.name, callback_data=f"sell_cat:{city_slug}:{c.id}")]
        for c in categories
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="sell_back")])
    rows.append([InlineKeyboardButton(text="☰ Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- Каталог ----------
def catalog_inline_initial():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Белград", callback_data="citysel:belgrade"),
         InlineKeyboardButton(text="Нови Сад", callback_data="citysel:novisad")],
        [InlineKeyboardButton(text="📝 ПОДАТЬ ЗАЯВКУ", callback_data="apply_catalog")],
        [InlineKeyboardButton(text="☰ Главное меню", callback_data="main_menu")]
    ])

def catalog_city_inline(city_slug: str, categories: List[Category]):
    alternative = "belgrade" if city_slug != "belgrade" else "novisad"
    buttons = [[InlineKeyboardButton(text=c.name, callback_data=f"cat:{city_slug}:{c.slug}")]
               for c in categories]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="catalog:back")])
    buttons.append([InlineKeyboardButton(text=("Белград" if alternative == "belgrade" else "Нови Сад"),
                                          callback_data=f"citysel:{alternative}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def catalog_application_category_inline():
    options = [
        ("Музыканцы", "capcat:musicians"),
        ("Вокал", "capcat:vocal"),
        ("Коллектив/Группа", "capcat:group"),
        ("Звук/Продакшн", "capcat:production"),
        ("Преподавание", "capcat:teaching"),
        ("Студии и площадки", "capcat:studio"),
        ("Оборудование", "capcat:equipment"),
        ("Организация и менеджмент", "capcat:management"),
        ("Подкасты", "capcat:podcasts"),
        ("Другое", "capcat:other")
    ]
    inline_buttons = [[InlineKeyboardButton(text=txt, callback_data=cb)] for txt, cb in options]
    inline_buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="catalog:back")])
    return InlineKeyboardMarkup(inline_keyboard=inline_buttons)

# ---------- Вакансии ----------
def vacancy_main_inline_view(prefix: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Белград", callback_data=f"{prefix}:belgrade"),
         InlineKeyboardButton(text="Нови Сад", callback_data=f"{prefix}:novisad")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="vacancy:back")],
        [InlineKeyboardButton(text="☰ Главное меню", callback_data="main_menu")]
    ])

def vacancy_category_inline():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Вокал", callback_data="vcat:vocal")],
        [InlineKeyboardButton(text="Музыканты", callback_data="vcat:musicians")],
        [InlineKeyboardButton(text="Звукорежиссер", callback_data="vcat:sound")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="vacancy:back")],
        [InlineKeyboardButton(text="☰ Главное меню", callback_data="main_menu")]
    ])

def musicians_sub_inline():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="клавиши", callback_data="vsub:keys")],
        [InlineKeyboardButton(text="гитара", callback_data="vsub:guitar")],
        [InlineKeyboardButton(text="бас", callback_data="vsub:bass")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="vcat:back")],
        [InlineKeyboardButton(text="☰ Главное меню", callback_data="main_menu")]
    ])

# ---------- Афиша ----------
def events_main_inline():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Белград", callback_data="ecity:belgrad"),
         InlineKeyboardButton(text="Нови Сад", callback_data="ecity:novisad")],
        [InlineKeyboardButton(text="Ближайшие мероприятия", callback_data="events:near"),
         InlineKeyboardButton(text="➕ РАЗМЕСТИТЬ ИНФОРМАЦИЮ", callback_data="event_new")],
         [InlineKeyboardButton(text="☰ Главное меню", callback_data="main_menu")]
    ])

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

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
