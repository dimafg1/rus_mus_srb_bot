from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


async def show_market_search_results(m, state, results):
    keyboard = [
        [InlineKeyboardButton(text=f"{listing.title} — {listing.price or ''}", callback_data=f"market_search_detail:{listing.id}")]
        for listing in results
    ]
    keyboard.append([InlineKeyboardButton(text="❌ Новый поиск", callback_data="market_search_new")])
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    await m.answer("Найдено объявлений:", reply_markup=markup)