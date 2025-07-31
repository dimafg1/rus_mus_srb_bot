# app/routers/catalog_add.py

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
from app.models import City, Category
from app.models import Profile  # предполагается что класс называется Profile
from app.database import SessionLocal
from app.keyboards import (
    catalog_cities_inline,
    catalog_category_inline,   # = аналог equip_inline для каталога, выдаёт подкатегории по parent_id
    get_common_menu_button,
)
from app.texts import get_text
from app.routers.utils import (
    clear_bot_messages,
    last_bot_messages,
)

router = Router(name="catalog_add")

class ProfileAddForm(StatesGroup):
    city = State()
    category = State()
    title = State()
    name = State()
    contact = State()
    description = State()
    photo = State()
    confirm = State()

# ———————— Шаг 1: выбор города ————————
@router.callback_query(F.data == "apply_catalog")
async def start_catalog_add(cb: CallbackQuery, state: FSMContext):
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    markup = await catalog_cities_inline()
    msg = await cb.message.answer("Выберите город:", reply_markup=markup)
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
    await state.set_state(ProfileAddForm.city)
    await cb.answer()

# ———————— Шаг 2: выбор категории с вложенностью ————————
@router.callback_query(F.data.startswith("apply_city:"), ProfileAddForm.city)
async def catalog_city(cb: CallbackQuery, state: FSMContext):
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    city_slug = cb.data.split(":")[1]
    async with SessionLocal() as s:
        city = (await s.execute(select(City).where(City.slug == city_slug))).scalar_one()
        root = (await s.execute(select(Category).where(Category.slug == "profile"))).scalar_one()
        categories = (await s.execute(select(Category).where(Category.parent_id == root.id))).scalars().all()
    await state.update_data(city_id=city.id, city_name=city.name, city_slug=city_slug)
    markup = await catalog_category_inline(categories, city_slug, parent_cat_id=None)
    msg = await cb.message.answer(f"Город: <b>{city.name}</b>\nВыберите категорию:", reply_markup=markup, parse_mode="HTML")
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
    await state.set_state(ProfileAddForm.category)
    await cb.answer()

@router.callback_query(F.data.startswith("cat:"), ProfileAddForm.category)
async def catalog_category(cb: CallbackQuery, state: FSMContext):
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    _, city_slug, cat_slug, *rest = cb.data.split(":")
    async with SessionLocal() as s:
        cat = (await s.execute(select(Category).where(Category.slug == cat_slug))).scalar_one()
        subcats = (await s.execute(select(Category).where(Category.parent_id == cat.id))).scalars().all()
    await state.update_data(cat_id=cat.id, cat_name=cat.name, cat_slug=cat.slug)
    if subcats:
        path = rest or []
        path.append(cat.slug)
        markup = await catalog_category_inline(subcats, city_slug, path=path)
        msg = await cb.message.answer(
            f"<b>Город: {await state.get_data()['city_name']}</b>\nВыбрана категория: <b>{cat.name}</b>\nВыберите подкатегорию:",
            reply_markup=markup, parse_mode="HTML"
        )
        last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
        await state.set_state(ProfileAddForm.category)
    else:
        msg = await cb.message.answer(
            f"Категория выбрана: <b>{cat.name}</b>\nВведите <b>название анкеты</b>:",
            reply_markup=await nav_keyboard()
        )
        last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
        await state.set_state(ProfileAddForm.title)
    await cb.answer()

# ———————— Шаг 3: Ввод названия анкеты (title) ————————
@router.message(ProfileAddForm.title)
async def catalog_title(m: Message, state: FSMContext):
    await clear_bot_messages(m.chat.id, m.bot)
    await state.update_data(title=m.text)
    msg = await m.answer("Введите ФИО или псевдоним (необязательно, можно пропустить):", reply_markup=await nav_keyboard())
    last_bot_messages.setdefault(m.chat.id, []).append(msg.message_id)
    await state.set_state(ProfileAddForm.name)

# ———————— Шаг 4: Имя (name) ————————
@router.message(ProfileAddForm.name)
async def catalog_name(m: Message, state: FSMContext):
    await clear_bot_messages(m.chat.id, m.bot)
    await state.update_data(name=m.text)
    user = m.from_user
    contact = user.username and f"@{user.username}" or str(user.id)
    await state.update_data(contact=contact)
    msg = await m.answer("Введите дополнительную контактную информацию (или пропустите):", reply_markup=await nav_keyboard())
    last_bot_messages.setdefault(m.chat.id, []).append(msg.message_id)
    await state.set_state(ProfileAddForm.contact)

# ———————— Шаг 5: Контакт ————————
@router.message(ProfileAddForm.contact)
async def catalog_contact(m: Message, state: FSMContext):
    await clear_bot_messages(m.chat.id, m.bot)
    contact = m.text.strip() or (await state.get_data())["contact"]
    await state.update_data(contact=contact)
    msg = await m.answer("Опишите себя/коллектив/услугу (или пропустите):", reply_markup=await nav_keyboard())
    last_bot_messages.setdefault(m.chat.id, []).append(msg.message_id)
    await state.set_state(ProfileAddForm.description)

# ———————— Шаг 6: Описание ————————
@router.message(ProfileAddForm.description)
async def catalog_descr(m: Message, state: FSMContext):
    await clear_bot_messages(m.chat.id, m.bot)
    await state.update_data(descr=m.text)
    msg = await m.answer("Прикрепите фото (до 3-х, можно пропустить):", reply_markup=await nav_keyboard())
    last_bot_messages.setdefault(m.chat.id, []).append(msg.message_id)
    await state.set_state(ProfileAddForm.photo)

# ———————— Шаг 7: Фото (один или до 3) ————————
@router.message(ProfileAddForm.photo, F.photo)
async def catalog_photo(m: Message, state: FSMContext):
    await clear_bot_messages(m.chat.id, m.bot)
    data = await state.get_data()
    photos = data.get("photos", []) or []
    if len(photos) < 3:
        photos.append(m.photo[-1].file_id)
        await state.update_data(photos=photos)
    if len(photos) < 3:
        msg = await m.answer(f"Фото добавлено ({len(photos)}/3). Ещё фото или 'далее'.", reply_markup=await nav_keyboard())
        last_bot_messages.setdefault(m.chat.id, []).append(msg.message_id)
        await state.set_state(ProfileAddForm.photo)
    else:
        await preview_and_confirm(m, state)

@router.message(ProfileAddForm.photo)
async def catalog_photo_text(m: Message, state: FSMContext):
    await clear_bot_messages(m.chat.id, m.bot)
    await preview_and_confirm(m, state)

# ———————— Preview + подтверждение ————————
async def preview_and_confirm(m: Message, state: FSMContext):
    await clear_bot_messages(m.chat.id, m.bot)
    data = await state.get_data()
    summary = (
        f"<b>Город:</b> {data.get('city_name')}\n"
        f"<b>Категория:</b> {data.get('cat_name')}\n"
        f"<b>Название:</b> {data.get('title')}\n"
        f"<b>Имя:</b> {data.get('name')}\n"
        f"<b>Контакт:</b> {data.get('contact')}\n"
        f"<b>Описание:</b> {data.get('descr')}\n"
        f"<b>Фото:</b> {len(data.get('photos', []))} шт."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="profile_confirm:yes")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="profile_confirm:no")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="profile_confirm:back")],
    ])
    msg = await m.answer(f"Проверьте данные:\n\n{summary}", reply_markup=kb, parse_mode="HTML")
    last_bot_messages.setdefault(m.chat.id, []).append(msg.message_id)
    await state.set_state(ProfileAddForm.confirm)

@router.callback_query(F.data.startswith("profile_confirm:"), ProfileAddForm.confirm)
async def confirm_profile(cb: CallbackQuery, state: FSMContext):
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    d = cb.data.split(":")[1]
    if d == "yes":
        data = await state.get_data()
        async with SessionLocal() as s:
            profile = Profile(
                city_id=data["city_id"],
                category_id=data["cat_id"],
                owner_id=cb.from_user.id,
                title=data["title"],
                name=data["name"],
                contact=data["contact"],
                descr=data["descr"],
                photo_file_ids=",".join(data.get("photos", [])) if data.get("photos") else None,
            )
            s.add(profile)
            await s.commit()
        msg = await cb.message.edit_text("Ваша анкета опубликована. Спасибо!")
        last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
        await state.clear()
    elif d == "back":
        msg = await cb.message.edit_text("Прикрепите фото (до 3-х, можно пропустить):", reply_markup=await nav_keyboard())
        last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
        await state.set_state(ProfileAddForm.photo)
    else:
        msg = await cb.message.edit_text("Публикация отменена.")
        last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
        await state.clear()
    await cb.answer()

# ———————— Клавиатура "Назад"/"Главное меню" ————————
async def nav_keyboard():
    back_btn = await get_common_menu_button('back')
    main_btn = await get_common_menu_button('main_menu')
    buttons = []
    if back_btn:
        buttons.append([InlineKeyboardButton(text=back_btn.text, callback_data=back_btn.callback_data)])
    if main_btn:
        buttons.append([InlineKeyboardButton(text=main_btn.text, callback_data=main_btn.callback_data)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
