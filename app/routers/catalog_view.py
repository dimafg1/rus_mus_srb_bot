from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

from app.keyboards import (
    catalog_inline_initial,
    catalog_city_inline,
    get_common_menu_button,
    catalog_search_results_keyboard,
)
from app.routers.utils import (
    last_bot_messages,
    clear_bot_messages,
    safe_edit_or_send,
    city_by_slug,
    children_of,
)
from app.models import Category, Item
from sqlalchemy import select
from app.database import SessionLocal
from app.texts import get_text

router = Router(name="catalog_view")
router = Router(name="catalog_search")

class CatalogSearchForm(StatesGroup):
    query = State()

# Кнопка поиска в каталоге
@router.callback_query(F.data == "catalog_search")
async def catalog_search_start(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("Введите ключевое слово для поиска в каталоге:")
    await state.set_state(CatalogSearchForm.query)
    await cb.answer()

# Поисковый ввод
@router.message(CatalogSearchForm.query)
async def catalog_search_query(m: Message, state: FSMContext):
    query = m.text.strip().lower()
    async with SessionLocal() as session:
        items = (await session.execute(
            select(Item)
            .where(
                (Item.title.ilike(f"%{query}%")) | (Item.descr.ilike(f"%{query}%")),
                Item.is_approved.is_(True)
            )
            .order_by(Item.created_at.desc())
        )).scalars().all()
    if not items:
        await m.answer(
            "Ничего не найдено по вашему запросу.",
            reply_markup=catalog_search_results_keyboard()
        )
    else:
        parts = []
        for i in items[:10]:  # не больше 10 анкет
            parts.append(f"• <b>{i.title}</b>\n{i.descr or ''}\n<code>{i.contact}</code>")
        await m.answer(
            "Результаты поиска:\n\n" + "\n\n".join(parts),
            parse_mode="HTML",
            reply_markup=catalog_search_results_keyboard()
        )
    await state.clear()


@router.callback_query(F.data == "go_catalog")
async def go_catalog(cb: CallbackQuery, state: FSMContext) -> None:
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    markup = await catalog_inline_initial()
    text = await get_text("catalog_choose_city", "ru") or "🏙 Каталог – выберите город:"
    await safe_edit_or_send(cb, text, markup)
    await cb.answer()


@router.callback_query(F.data.startswith("citysel:"))
async def catalog_city_handler(cb: CallbackQuery):
    city_slug = cb.data.split(":")[1]

    # Получаем только дочерние категории profile
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

    markup = await catalog_city_inline(city_slug, categories)
    await cb.message.edit_text("Выберите категорию:", reply_markup=markup)


@router.callback_query(F.data.startswith("cat:"))
async def cat_handler(cb: CallbackQuery) -> None:
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    _, city_slug, cat_slug = cb.data.split(":", 2)
    city = await city_by_slug(city_slug)
    async with SessionLocal() as session:
        cat = (await session.execute(
            select(Category).where(Category.slug == cat_slug)
        )).scalar_one()
        children = (await session.execute(
            select(Category).where(Category.parent_id == cat.id)
        )).scalars().all()

    names = [cat.name]
    parent_cat_slug = None
    cur = cat
    while cur.parent_id:
        async with SessionLocal() as session:
            p = (await session.execute(
                select(Category).where(Category.id == cur.parent_id)
            )).scalar_one()
        names.append(p.name)
        if not parent_cat_slug:
            parent_cat_slug = p.slug
        cur = p
    path = " → ".join(reversed(names))
    header = f"<b>Каталог → {city.name} → {path}</b>"

    buttons = []
    if children:
        for child in children:
            buttons.append([InlineKeyboardButton(
                text=child.name,
                callback_data=f"cat:{city_slug}:{child.slug}"
            )])
    else:
        # Листовая категория: показать анкеты-профили (Item)
        async with SessionLocal() as session:
            items = (await session.execute(
                select(Item)
                .where(Item.city_id == city.id, Item.category_id == cat.id, Item.is_approved.is_(True))
                .order_by(Item.created_at.desc())
            )).scalars().all()
        if items:
            for i in items:
                # Сделайте кнопку для каждой анкеты, например по id
                buttons.append([InlineKeyboardButton(
                    text=i.title,
                    callback_data=f"profile:{i.id}:{city_slug}:{cat.slug}"
                )])

    # Навигация
    if parent_cat_slug:
        back_callback = f"cat:{city_slug}:{parent_cat_slug}"
    else:
        back_callback = f"citysel:{city_slug}"

    back_btn = await get_common_menu_button('back')
    if back_btn:
        buttons.append([InlineKeyboardButton(text=back_btn.text, callback_data=back_callback)])

    main_menu_btn = await get_common_menu_button('main_menu')
    if main_menu_btn:
        buttons.append([InlineKeyboardButton(text=main_menu_btn.text, callback_data=main_menu_btn.callback_data)])

    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    # Формируем сообщение
    if children or (not children and items):
        msg = await cb.bot.send_message(cb.message.chat.id, header, reply_markup=markup, parse_mode="HTML")
    else:
        # Листовая, но анкет нет
        msg = await cb.bot.send_message(cb.message.chat.id, header + "\n\nПока нет анкет.", reply_markup=markup, parse_mode="HTML")

    last_bot_messages[cb.message.chat.id] = [msg.message_id]
    await cb.answer()


@router.callback_query(F.data == "catalog:back")
async def catalog_back(cb: CallbackQuery) -> None:
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    markup = await catalog_inline_initial()
    text = await get_text("catalog_choose_city", "ru") or "🏙 Каталог – выберите город:"
    msg = await cb.bot.send_message(cb.message.chat.id, text, reply_markup=markup, parse_mode="HTML")
    last_bot_messages[cb.message.chat.id] = [msg.message_id]
    await cb.answer()
