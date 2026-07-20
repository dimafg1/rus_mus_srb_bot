from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.routers.utils import get_text


async def show_market_search_results(m, state, results):
    keyboard = [
        [InlineKeyboardButton(text=f"{listing.title} — {listing.price or ''}", callback_data=f"market_search_detail:{listing.id}")]
        for listing in results
    ]
    keyboard.append([InlineKeyboardButton(text=await get_text("market_utils_btn_new_search", "ru") or "❌ Новый поиск", callback_data="market_search_new")])
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    await m.answer(await get_text("market_utils_search_found", "ru") or "Найдено объявлений:", reply_markup=markup)