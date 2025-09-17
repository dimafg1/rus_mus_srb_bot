# app/routers/vacancy_view.py
from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# Импорты
# ─────────────────────────────────────────────────────────────────────────────
from typing import List, Optional

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select  # or_, func не требуются в этой версии
from aiogram.types import CallbackQuery

from app.database import SessionLocal
from app.models import Listing, City, Category
from app.keyboards import get_common_menu_button
from app.texts import get_text
from app.routers.utils import clear_bot_messages, safe_edit_or_send, render_flex_block
from app.routers.utils import (
    clear_bot_messages,
    last_search_menu_message,
    last_search_query_message,
)
from app.routers.vacancy_utils import (
    vacancy_categories_inline,   # категории: vlist:<city_slug>:<cat_id>
    vacancy_listings_inline,     # список вакансий в категории
    my_vacancies_inline,         # список моих вакансий
    _flex_from_db,               # JSON-строка → dict
    vacancy_main_menu,
)
from aiogram.filters import StateFilter


VACANCY_ROOT_ID = 90

async def _category_chain_by_db(cat: Category | None) -> str:
    if not cat:
        return "—"
    names, cur_id, guard = [], cat.id, 0
    async with SessionLocal() as s:
        while cur_id and guard < 20:
            guard += 1
            c = await s.get(Category, cur_id)
            if not c or c.id == VACANCY_ROOT_ID:
                break
            names.append((c.name or "").strip() or "—")
            if not c.parent_id or c.parent_id == cur_id:
                break
            cur_id = c.parent_id
    names.reverse()
    return " › ".join(names) if names else (cat.name or "—")


router = Router(name="vacancy_view")

# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции (с русскими описаниями и debug-print)
# ─────────────────────────────────────────────────────────────────────────────
def _dbg(handler: str, **kw) -> None:
    """
    Печатает короткую отладочную строку о срабатывании хендлера.
    """
    pairs = " ".join(f"{k}={v!r}" for k, v in kw.items())
    print(f"[vacancy_view.py] handler={handler} {pairs}")


async def _city_id_by_slug(slug: str) -> Optional[int]:
    """
    Получить ID города по его slug. Возвращает None, если не найден.
    """
    async with SessionLocal() as s:
        return (
            await s.execute(select(City.id).where(City.slug == slug))
        ).scalar_one_or_none()


async def _category_children(cat_id: int) -> List[Category]:
    """
    Получить список дочерних категорий по parent_id.
    """
    async with SessionLocal() as s:
        res = await s.execute(select(Category).where(Category.parent_id == cat_id))
        return res.scalars().all()


# ─────────────────────────────────────────────────────────────────────────────
# Просмотр через ГОРОДА: выбор города → выбор категории → список вакансий
# ─────────────────────────────────────────────────────────────────────────────

# RU: выбор города → показать корневые категории «Вакансии» для города
@router.callback_query(F.data.startswith("vcity:"))
async def vacancy_view_city(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)

    # vcity:<slug>
    city_slug = cb.data.split(":", 1)[1] if ":" in cb.data else None
    kb = await vacancy_categories_inline(city_slug, parent_id=None)

    await safe_edit_or_send(
        cb,
        f"🤝 Вакансии → {city_slug or '(город не задан)'}\nВыберите категорию:",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await cb.answer()
    _dbg("vacancy_view_city", city_slug=city_slug, chat_id=chat_id)


# RU: vlist:<city_slug>:<cat_id> — подкатегории или список вакансий листовой категории
@router.callback_query(F.data.startswith("vlist:"))
async def vacancy_list(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)

    # vlist:<slug>:<cat_id>
    _, city_slug, cat_id_s = cb.data.split(":", 2)
    cat_id = int(cat_id_s)

    # Если есть подкатегории — углубляемся
    children = await _category_children(cat_id)
    if children:
        kb = await vacancy_categories_inline(city_slug, parent_id=cat_id)
        await safe_edit_or_send(
            cb,
            "Выберите подкатегорию:",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await cb.answer()
        _dbg("vacancy_list.categories", city_slug=city_slug, cat_id=cat_id, children=len(children))
        return

    # Иначе — показываем вакансии в листовой категории
    city_id = await _city_id_by_slug(city_slug)
    listings: List[Listing] = []
    async with SessionLocal() as s:
        q = (
            select(Listing)
            .where(
                Listing.type == "vacancy",
                Listing.city_id == city_id,
                Listing.category_id == cat_id,
                Listing.is_sold == False,  # noqa: E712
            )
            .order_by(Listing.created_at.desc())
        )
        listings = (await s.execute(q)).scalars().all()

    kb = await vacancy_listings_inline(city_slug, cat_id, listings)
    await safe_edit_or_send(cb, "Выберите объявление:", reply_markup=kb, parse_mode="HTML")
    await cb.answer()
    _dbg("vacancy_list.listings", city_slug=city_slug, cat_id=cat_id, found=len(listings))


# RU: совместимость со старыми кнопками вида vac_cat:<slug>:<cat_id> → редирект в vlist
@router.callback_query(StateFilter(None), F.data.startswith("vac_cat:"))
async def _compat_vac_cat_redirect(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)

    _, city_slug, cat_id = cb.data.split(":", 2)
    await vacancy_list(
        # эмулируем колбэк vlist:<slug>:<cat_id>
        type("Proxy", (), {"message": cb.message, "data": f"vlist:{city_slug}:{cat_id}", "answer": cb.answer})
    )
    _dbg("_compat_vac_cat_redirect", city_slug=city_slug, cat_id=cat_id, chat_id=chat_id)


# ─────────────────────────────────────────────────────────────────────────────
# «Мои вакансии»
# ─────────────────────────────────────────────────────────────────────────────

# RU: список вакансий текущего пользователя
@router.callback_query(F.data == "vac:my")
async def vac_my_listings(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)

    listings: List[Listing] = []
    async with SessionLocal() as s:
        q = (
            select(Listing)
            .where(
                Listing.type == "vacancy",
                Listing.owner_id == cb.from_user.id,
            )
            .order_by(Listing.created_at.desc())
        )
        listings = (await s.execute(q)).scalars().all()

    kb = await my_vacancies_inline(listings)
    await safe_edit_or_send(cb, "Ваши вакансии:", reply_markup=kb, parse_mode="HTML")
    await cb.answer()
    _dbg("vac_my_listings", user_id=cb.from_user.id, count=len(listings))


# ─────────────────────────────────────────────────────────────────────────────
# Карточка вакансии
# ─────────────────────────────────────────────────────────────────────────────

# RU: Карточка вакансии – выводим Город и Категорию сразу под заголовком (сверху).
@router.callback_query(F.data.startswith("vac_view:"))
async def vacancy_view_detail(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)

    # format: vac_view:<id>:<city_slug>:<cat_id>[:my]
    parts = cb.data.split(":")
    listing_id = int(parts[1])
    city_slug = parts[2] if len(parts) > 2 else None
    cat_id = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
    is_my_suffix = (len(parts) > 4 and parts[4] == "my")

    async with SessionLocal() as s:
        listing = (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one()
        city = (await s.execute(select(City).where(City.id == listing.city_id))).scalar_one()
        cat = (await s.execute(select(Category).where(Category.id == listing.category_id))).scalar_one()
        # если раньше не добавляли — убедитесь, что импортировали render_flex_block из utils
        flex_block = await render_flex_block(s, listing, lang="ru")

        # --- ново: собираем путь категории "Родитель › Дочерняя", пропуская корень id=90 ---
        VACANCY_ROOT_ID = 90
        names = []
        cur = cat
        guard = 0
        while cur and guard < 20:
            guard += 1
            if getattr(cur, "id", None) == VACANCY_ROOT_ID:
                break
            nm = (getattr(cur, "name", None) or "").strip()
            if nm:
                names.append(nm)
            pid = getattr(cur, "parent_id", None)
            if not pid or pid == getattr(cur, "id", None):
                break
            # поднимаемся к родителю
            cur = await s.get(Category, pid)
        names.reverse()
        cat_path = " › ".join(names) if names else (cat.name or "")
        # --- конец новго блока ---

    is_owner = (listing.owner_id == cb.from_user.id) or is_my_suffix

    from html import escape as _esc
    lines = []

    # Сразу под заголовком — город и категория
    lines.append(f"Город: <b>{_esc(city.name)}</b>")
    lines.append(f"Категория: <b>{_esc(cat_path)}</b>")
    lines.append("")  # визуальный отступ

    # Заголовок
    lines.append(f"<b>{_esc(listing.title or '(без заголовка)')}</b>")

    lines.append("")  # визуальный отступ
    if listing.descr:
        lines.append(f"{_esc(listing.descr)}")

    lines.append("")  # визуальный отступ
    # Далее — зарплата
    if listing.price:
        lines.append(f"Оплата: <b>{_esc(str(listing.price))}</b>")

    # Доп. поля (с человекочитаемыми лейблами из Category.fields)
    if flex_block:
        lines.append("")          # отступ для читаемости
        lines.append(flex_block)

    contact = listing.contact or ""

    # Кнопки
    buttons: List[List[InlineKeyboardButton]] = []
    if is_owner:
        buttons.append([InlineKeyboardButton(text="✏️ Редактировать все поля", callback_data=f"vacancy_edit_overview:{listing.id}")])
        buttons.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"vac_delete_confirm:{listing.id}")])
    else:
        if contact and contact.startswith("@"):
            buttons.append([InlineKeyboardButton(text="💬 Связаться", url=f"https://t.me/{contact.lstrip('@')}")])

    if city_slug and cat_id:
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"vlist:{city_slug}:{cat_id}")])
    else:
        buttons.append([InlineKeyboardButton(text="⬅️ В меню вакансий", callback_data="go_isk")])

    main_btn = await get_common_menu_button('main_menu')
    if main_btn:
        buttons.append([main_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await safe_edit_or_send(cb, "\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await cb.answer()
    print(f"[vacancy_view.py] handler=vacancy_view_detail listing_id={listing_id} is_owner={is_owner} city_slug={city_slug} cat_id={cat_id}")

# RU: Подтверждение удаления объявления (редактируем текущее сообщение, чтобы ничего не плодить).
@router.callback_query(F.data.startswith("vac_delete_confirm:"))
async def vac_delete_confirm(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    lid = int(cb.data.split(":", 1)[1])

    # Кнопки подтверждения
    rows = [
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"vac_delete_yes:{lid}")],
        [InlineKeyboardButton(text="⬅️ Нет, вернуться", callback_data=f"vac_view:{lid}:::my")],
    ]
    main_btn = await get_common_menu_button("main_menu")
    if main_btn:
        rows.append([main_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    # Редактируем текущую карточку — без создания новых сообщений
    await safe_edit_or_send(cb, "Вы действительно хотите удалить объявление?", kb, parse_mode="HTML")
    await cb.answer()
    print(f"[vacancy_view.py] handler=vac_delete_confirm listing_id={lid} chat_id={chat_id}")



# RU: Удаление вакансии (только автор). Чистим хвосты и показываем навигацию.
@router.callback_query(F.data.startswith("vac_delete_yes:"))
async def vac_delete_yes(cb: CallbackQuery):
    chat_id = cb.message.chat.id

    # Убираем сообщение, по которому кликнули (карточка/подтверждение)
    try:
        await cb.message.delete()
    except Exception:
        pass

    # Канон: подчистка служебных сообщений бота
    await clear_bot_messages(chat_id, cb.bot)

    lid = int(cb.data.split(":", 1)[1])
    async with SessionLocal() as s:
        obj = await s.get(Listing, lid)
        if not obj:
            await cb.answer("Объявление уже удалено.", show_alert=True)
            print(f"[vacancy_view.py] handler=vac_delete_yes listing_id={lid} status=not_found")
            return
        if obj.owner_id != cb.from_user.id:
            await cb.answer("⛔️ Недостаточно прав.", show_alert=True)
            print(f"[vacancy_view.py] handler=vac_delete_yes listing_id={lid} status=forbidden user_id={cb.from_user.id}")
            return

        await s.delete(obj)
        await s.commit()

    rows = [
        [InlineKeyboardButton(text="📄 Мои вакансии", callback_data="vac:my")],
        [InlineKeyboardButton(text="⬅️ В меню вакансий", callback_data="go_isk")],
    ]
    main_btn = await get_common_menu_button("main_menu")
    if main_btn:
        rows.append([main_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await cb.message.answer("✅ Объявление удалено.", reply_markup=kb, parse_mode="HTML")
    await cb.answer("Удалено.")
    print(f"[vacancy_view.py] handler=vac_delete_yes listing_id={lid} status=ok user_id={cb.from_user.id}")



# ─────────────────────────────────────────────────────────────────────────────
# Закрыть/Открыть вакансию
# ─────────────────────────────────────────────────────────────────────────────

# # RU: пометить вакансию закрытой
# @router.callback_query(F.data.startswith("vac_sold:"))
# async def vac_mark_sold(cb: CallbackQuery):
#     chat_id = cb.message.chat.id
#     await clear_bot_messages(chat_id, cb.bot)

#     lid = int(cb.data.split(":", 1)[1])
#     async with SessionLocal() as s:
#         obj = await s.get(Listing, lid)
#         if not obj or obj.owner_id != cb.from_user.id:
#             await cb.answer("⛔️ Недостаточно прав.", show_alert=True)
#             _dbg("vac_mark_sold.denied", listing_id=lid, user_id=cb.from_user.id)
#             return
#         obj.is_sold = True
#         s.add(obj); await s.commit()

#     kb = InlineKeyboardMarkup(inline_keyboard=[
#         [InlineKeyboardButton(text="🔎 Посмотреть объявление", callback_data=f"vac_view:{lid}:::my")],
#         [InlineKeyboardButton(text="⬅️ В меню вакансий", callback_data="go_isk")],
#     ])
#     await cb.message.answer("✅ Вакансия закрыта.", reply_markup=kb, parse_mode="HTML")
#     await cb.answer("Закрыто.")
#     _dbg("vac_mark_sold.ok", listing_id=lid, user_id=cb.from_user.id)


# # RU: снова открыть вакансию
# @router.callback_query(F.data.startswith("vac_reopen:"))
# async def vac_mark_reopen(cb: CallbackQuery):
#     chat_id = cb.message.chat.id
#     await clear_bot_messages(chat_id, cb.bot)

#     lid = int(cb.data.split(":", 1)[1])
#     async with SessionLocal() as s:
#         obj = await s.get(Listing, lid)
#         if not obj or obj.owner_id != cb.from_user.id:
#             await cb.answer("⛔️ Недостаточно прав.", show_alert=True)
#             _dbg("vac_mark_reopen.denied", listing_id=lid, user_id=cb.from_user.id)
#             return
#         obj.is_sold = False
#         s.add(obj); await s.commit()

#     kb = InlineKeyboardMarkup(inline_keyboard=[
#         [InlineKeyboardButton(text="🔎 Посмотреть объявление", callback_data=f"vac_view:{lid}:::my")],
#         [InlineKeyboardButton(text="⬅️ В меню вакансий", callback_data="go_isk")],
#     ])
#     await cb.message.answer("✅ Вакансия снова открыта.", reply_markup=kb, parse_mode="HTML")
#     await cb.answer("Открыто.")
#     _dbg("vac_mark_reopen.ok", listing_id=lid, user_id=cb.from_user.id)


# ─────────────────────────────────────────────────────────────────────────────
# Поиск вакансий
# ─────────────────────────────────────────────────────────────────────────────

class VacSearch(StatesGroup):
    """
    Состояние поиска вакансий.
    """
    waiting_query = State()


async def _search_vacancies(session, q: str):
    """
    Поиск вакансий по заголовку и описанию (только открытые), без учёта регистра.
    Делается Python-фильтром через Unicode casefold(), чтобы кириллица искалась корректно.
    """
    q_cf = (q or "").casefold()

    stmt = (
        select(Listing)
        .where(
            Listing.type == "vacancy",
            Listing.is_sold == False,  # noqa: E712
        )
        .order_by(Listing.created_at.desc())
        .limit(1000)
    )
    rows = (await session.execute(stmt)).scalars().all()

    def hit(it: Listing) -> bool:
        t = (it.title or "").casefold()
        d = (it.descr or "").casefold()
        return (q_cf in t) or (q_cf in d)

    return [it for it in rows if hit(it)]


# RU: старт поиска — просим ввести строку + плашка «Назад/Главное»
@router.callback_query(F.data == "vac_search")
async def vac_search_start(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # 0) Удаляем меню, по которому нажали кнопку «Поиск вакансий»
    try:
        await cb.message.delete()
        print(f"[vac_search_start] deleted menu msg_id={cb.message.message_id} chat={chat_id}")
    except Exception as e:
        print(f"[vac_search_start] cannot delete cb.message: {e}")

    # Очистка: сначала пользовательские (если есть), затем бот-сообщения
    try:
        from app.routers.utils import clear_user_messages
        await clear_user_messages(chat_id, cb.bot)
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    # === Плашка «Назад / Главное меню» ===
    nav_msg = None
    try:
        back_btn = await get_common_menu_button('back')
        if back_btn:
            # Кнопка «Назад» возвращает в главное меню раздела «Вакансии»
            back_btn.callback_data = "vacancy_main_menu"
        main_btn = await get_common_menu_button('main_menu')
        nav_buttons = [b for b in (back_btn, main_btn) if b]
        if nav_buttons:
            nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons])
            nav_text = await get_text('return_to_menu', 'ru') or "Возврат"
            nav_msg = await cb.bot.send_message(chat_id, nav_text, reply_markup=nav_markup)
    except Exception as e:
        # не ломаем основной поток поиска, если плашка не построилась
        print(f"[vacancy_view.py] nav panel error: {e}")
    # === КОНЕЦ плашки ===

    await state.clear()
    await state.set_state(VacSearch.waiting_query)

    # Промпт «Введите запрос…»
    msg = await cb.message.answer(
        "🔎 Введите запрос для поиска (например: «курьер», «дизайнер»). Ищем по заголовку и описанию.",
        parse_mode="HTML",
    )

    # Сохраняем id, чтобы «Главное меню» и _drop_nav_and_prompt смогли всё удалить
    try:
        # для ваших общих чисток
        from app.routers.utils import last_search_menu_message, last_search_query_message
        if nav_msg:
            last_search_menu_message[chat_id] = nav_msg.message_id
        last_search_query_message[chat_id] = msg.message_id
    except Exception:
        pass

    # для FSM (то, что чистит _drop_nav_and_prompt)
    try:
        await state.update_data(
            nav_msg_id=(nav_msg.message_id if nav_msg else None),
            prompt_id=msg.message_id,
            search_prompt_msg_id=msg.message_id,  # оставляю, как у вас было
        )
    except Exception as e:
        print(f"[vac_search_start] state.update_data error: {e}")

    await cb.answer()
    _dbg("vac_search_start", chat_id=chat_id, user_id=cb.from_user.id)


# RU: выполнить поиск, показать результаты
@router.message(VacSearch.waiting_query)
async def vac_search_do(m: Message, state: FSMContext):
    chat_id = m.chat.id

    # Удалить сообщение пользователя (канон)
    try:
        await m.delete()
    except Exception:
        pass

    # Удалить подсказку бота и плашку «Возврат», показанные в vac_search_start
    try:
        data = await state.get_data()
        # Сначала — по FSM ключам
        for key in ("nav_msg_id", "prompt_id", "search_prompt_msg_id"):
            mid = data.get(key)
            if mid:
                try:
                    await m.bot.delete_message(chat_id, mid)
                except Exception:
                    pass
        # Обнулить ключи в FSM
        await state.update_data(nav_msg_id=None, prompt_id=None, search_prompt_msg_id=None)
    except Exception:
        pass

    # Дополнительно — подчистить из общих кэшей (если они вели эти id)
    try:
        from app.routers.utils import last_search_menu_message, last_search_query_message
        for mid in (last_search_menu_message.pop(chat_id, None),
                    last_search_query_message.pop(chat_id, None)):
            if mid:
                try:
                    await m.bot.delete_message(chat_id, mid)
                except Exception:
                    pass
    except Exception:
        pass

    # Общая очистка истории
    try:
        from app.routers.utils import clear_user_messages
        await clear_user_messages(chat_id, m.bot)
    except Exception:
        pass
    await clear_bot_messages(chat_id, m.bot)

    # Сам поиск
    q = (m.text or "").strip()
    if len(q) < 2:
        await m.answer("Минимум 2 символа. Введите запрос ещё раз:", parse_mode="HTML")
        _dbg("vac_search_do.short", chat_id=chat_id, q=q)
        return

    async with SessionLocal() as s:
        rows = await _search_vacancies(s, q)

    # Сборка клавиатуры результатов
    buttons: List[List[InlineKeyboardButton]] = []
    for l in rows:
        title = (l.title or "(без заголовка)").strip()
        price = f" — {l.price}" if getattr(l, "price", None) else ""
        buttons.append([InlineKeyboardButton(text=f"{title}{price}", callback_data=f"vac_view:{l.id}")])

    if not rows:
        buttons.append([InlineKeyboardButton(text="Ничего не найдено", callback_data="go_isk")])

    buttons.append([InlineKeyboardButton(text="⬅️ В меню вакансий", callback_data="go_isk")])
    main_btn = await get_common_menu_button('main_menu')
    if main_btn:
        buttons.append([main_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await m.answer(
        f"Результаты по запросу: <b>{q}</b>\nНайдено: {len(rows)}",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await state.clear()
    _dbg("vac_search_do", chat_id=chat_id, user_id=m.from_user.id, q=q, found=len(rows))

# -*- coding: utf-8 -*-
# RU: Возврат в главное меню раздела «Вакансии».
#     ГАРАНТИРОВАННО чистим: текущее сообщение, кэши last_search_*, id из FSM.
@router.callback_query(F.data == "vacancy_main_menu")
async def vacancy_main_menu_cb(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    print("FUNC: vacancy_main_menu_cb | file: app/routers/vacancy_view.py | chat:", chat_id)

    # 0) Пытаемся удалить именно то сообщение, по которому нажали кнопку
    try:
        await cb.message.delete()
    except Exception as e:
        print("WARN: cannot delete cb.message:", e)

    # 1) Чистим служебные сообщения из общих кэшей
    from app.routers.utils import last_search_menu_message, last_search_query_message, clear_bot_messages
    for mid in (last_search_menu_message.pop(chat_id, None),
                last_search_query_message.pop(chat_id, None)):
        if mid:
            try:
                await cb.bot.delete_message(chat_id, mid)
            except Exception as e:
                print("WARN: delete cached msg:", mid, "|", e)

    # 2) Чистим то, что могли хранить в FSM (например, search_prompt_msg_id)
    try:
        data = await state.get_data()
        for key in ("search_prompt_msg_id", "nav_msg_id", "prompt_id"):
            mid = data.get(key)
            if mid:
                try:
                    await cb.bot.delete_message(chat_id, mid)
                except Exception as e:
                    print(f"WARN: delete {key}:", mid, "|", e)
        # обнуляем поля в FSM
        await state.update_data(search_prompt_msg_id=None, nav_msg_id=None, prompt_id=None)
    except Exception as e:
        print("WARN: FSM cleanup:", e)

    # 3) Общая зачистка «хвоста» бота
    await clear_bot_messages(chat_id, cb.bot)

    # 4) Сбрасываем состояние
    try:
        await state.clear()
    except Exception as e:
        print("WARN: state.clear:", e)

    # 5) Рендерим главное меню «Вакансий»
    from app.routers.vacancy_utils import vacancy_main_menu
    from app.texts import get_text
    try:
        kb = await vacancy_main_menu(lang="ru")
    except TypeError:
        kb = await vacancy_main_menu()
    title = await get_text("vacancy_main_title", "ru") or "Раздел «Вакансии»"

    # отправляем НОВОЕ сообщение с меню (мы предыдущие удалили)
    await cb.bot.send_message(chat_id, title, reply_markup=kb, parse_mode="HTML")

    await cb.answer()
    print("OK: vacancy_main_menu_cb done")
