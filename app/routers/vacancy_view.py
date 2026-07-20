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
from sqlalchemy import select, text  # or_, func не требуются в этой версии
from aiogram.types import CallbackQuery

from app.database import SessionLocal
from app.models import Listing, City, Category
from app.keyboards import get_common_menu_button
from app.texts import get_text
from app.routers.utils import clear_bot_messages, safe_edit_or_send, register_bot_messages, render_flex_block, render_category_path, city_by_slug
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

from app.routers.utils_category_title import format_category_title

from app.routers.utils_kb import grid3

from app.search.fuzzy import search_items

from app.analytics.search_log import log_search
from app.analytics.listing_views import log_listing_view
from app.lifecycle import days_left_text, should_show_extend_button, extend_listing, archive_as_closed, is_active, can_owner_reactivate
from app.routers.utils import build_contact_url, escape_html


VACANCY_ROOT_ID = 90

VACANCY_SEARCH_PAGE_SIZE = 10


def _vacancy_public_predicates():
    """Единые условия публичной выдачи вакансий."""
    return (
        Listing.type == "vacancy",
        Listing.status == "active",
        Listing.is_sold.is_(False),
    )


async def _load_public_vacancy_ids(ids: list[int]) -> tuple[list[int], list[Listing]]:
    clean_ids: list[int] = []
    seen: set[int] = set()
    for raw_id in ids or []:
        try:
            listing_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if listing_id not in seen:
            seen.add(listing_id)
            clean_ids.append(listing_id)
    if not clean_ids:
        return [], []

    async with SessionLocal() as s:
        db_rows = (await s.execute(
            select(Listing).where(Listing.id.in_(clean_ids), *_vacancy_public_predicates())
        )).scalars().all()
    by_id = {row.id: row for row in db_rows}
    valid_ids = [listing_id for listing_id in clean_ids if listing_id in by_id]
    return valid_ids, [by_id[listing_id] for listing_id in valid_ids]

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
        res = await s.execute(select(Category).where(Category.parent_id == cat_id).order_by(text("order_num"), Category.name))
        return res.scalars().all()
    
async def _vacancy_categories_kb(city_slug: str | None, parent_id: int | None) -> InlineKeyboardMarkup:
    """Локальная клавиатура категорий для Вакансий с авто «🔽»."""
    pid = parent_id if parent_id is not None else VACANCY_ROOT_ID
    async with SessionLocal() as s:
        cats = (await s.execute(
            select(Category).where(Category.parent_id == pid).order_by(text("order_num"), Category.name)
        )).scalars().all()

    rows = []
    for c in cats:
        title = await format_category_title(c.id, (c.name or "").strip(), SessionLocal)
        rows.append([InlineKeyboardButton(text=title, callback_data=f"vlist:{city_slug}:{c.id}")])

    # ↓↓↓ вернуть навигацию как просили ↓↓↓
    rows.append([InlineKeyboardButton(text="⬅️ В меню вакансий", callback_data="go_isk")])
    main_btn = await get_common_menu_button('main_menu')
    if main_btn:
        rows.append([main_btn])

    return InlineKeyboardMarkup(inline_keyboard=rows)



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
    kb = await _vacancy_categories_kb(city_slug, parent_id=None)

    # Имя города вместо slug
    city_name = city_slug or "(город не задан)"
    if city_slug:
        try:
            city = await city_by_slug(city_slug)
            if city:
                city_name = city.name
        except Exception:
            pass

    await safe_edit_or_send(
        cb,
        f"🤝 Вакансии → <b>{escape_html(city_name)}</b>\nВыберите категорию:",
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

    # vlist:<slug>:<cat_id>[:<offset>]
    parts = cb.data.split(":")
    city_slug = parts[1]
    cat_id = int(parts[2])
    offset = int(parts[3]) if len(parts) > 3 and parts[3].lstrip("-").isdigit() else 0

    # Хлебные крошки: Вакансии → Город → Категория → …
    city_name = city_slug or ""
    try:
        city_obj = await city_by_slug(city_slug)
        if city_obj:
            city_name = city_obj.name
    except Exception:
        pass
    async with SessionLocal() as s:
        cat_path = await render_category_path(s, cat_id, root_id=VACANCY_ROOT_ID)
    crumbs = f"🤝 Вакансии → <b>{escape_html(city_name)}</b> → {cat_path}"

    # Если есть подкатегории — углубляемся
    children = await _category_children(cat_id)
    if children:
        kb = await _vacancy_categories_kb(city_slug, parent_id=cat_id)
        await safe_edit_or_send(
            cb,
            f"{crumbs}\nВыберите подкатегорию:",
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
                Listing.status == "active",
            )
            .order_by(Listing.created_at.desc())
        )
        listings = (await s.execute(q)).scalars().all()

    kb = await vacancy_listings_inline(city_slug, cat_id, listings, offset=offset)
    tail = "Выберите объявление:" if listings else "Пока пусто в этой категории."
    await safe_edit_or_send(cb, f"{crumbs}\n{tail}", reply_markup=kb, parse_mode="HTML")
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

MY_VACANCIES_PAGE_SIZE = 10


# RU: список вакансий текущего пользователя (с пагинацией и маркером архива)
async def _render_my_vacancies(cb: CallbackQuery, offset: int = 0):
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

    main_btn = await get_common_menu_button("main_menu")

    if not listings:
        rows = [[InlineKeyboardButton(text="⬅️ В меню вакансий", callback_data="go_isk")]]
        if main_btn:
            rows.append([main_btn])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await safe_edit_or_send(cb, "У вас пока нет вакансий.", reply_markup=kb, parse_mode="HTML")
        await cb.answer()
        _dbg("vac_my_listings.empty", user_id=cb.from_user.id, count=0)
        return

    total = len(listings)
    pages = max(1, (total + MY_VACANCIES_PAGE_SIZE - 1) // MY_VACANCIES_PAGE_SIZE)
    if offset >= total:
        offset = (pages - 1) * MY_VACANCIES_PAGE_SIZE
    if offset < 0:
        offset = 0
    page = offset // MY_VACANCIES_PAGE_SIZE + 1

    rows = []
    for l in listings[offset:offset + MY_VACANCIES_PAGE_SIZE]:
        title = (l.title or "(без заголовка)").strip()
        price = f" — {l.price}" if getattr(l, "price", None) else ""
        marker = "📦 " if l.status == "archived" else ""
        rows.append([InlineKeyboardButton(
            text=f"{marker}{title}{price}",
            callback_data=f"vac_view:{l.id}:::my"
        )])

    if pages > 1:
        pager = []
        if offset > 0:
            pager.append(InlineKeyboardButton(
                text="«", callback_data=f"vac_my_page:{offset - MY_VACANCIES_PAGE_SIZE}"))
        pager.append(InlineKeyboardButton(text=f"{page}/{pages}", callback_data="stub"))
        if offset + MY_VACANCIES_PAGE_SIZE < total:
            pager.append(InlineKeyboardButton(
                text="»", callback_data=f"vac_my_page:{offset + MY_VACANCIES_PAGE_SIZE}"))
        rows.append(pager)

    rows.append([InlineKeyboardButton(text="⬅️ В меню вакансий", callback_data="go_isk")])
    if main_btn:
        rows.append([main_btn])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    suffix = f" ({total})" if pages > 1 else ""
    await safe_edit_or_send(cb, f"<b>Ваши вакансии{suffix}:</b>", reply_markup=kb, parse_mode="HTML")
    await cb.answer()
    _dbg("vac_my_listings", user_id=cb.from_user.id, count=total, offset=offset)


@router.callback_query(F.data == "vac:my")
async def vac_my_listings(cb: CallbackQuery):
    await _render_my_vacancies(cb, offset=0)


@router.callback_query(F.data.startswith("vac_my_page:"))
async def vac_my_page(cb: CallbackQuery):
    try:
        offset = int(cb.data.split(":")[1])
    except (ValueError, IndexError):
        offset = 0
    await _render_my_vacancies(cb, offset=offset)


# ─────────────────────────────────────────────────────────────────────────────
# Карточка вакансии
# ─────────────────────────────────────────────────────────────────────────────

# RU: Карточка вакансии – выводим Город и Категорию сразу под заголовком (сверху).
@router.callback_query(F.data.startswith("vac_view:"))
async def vacancy_view_detail(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)

    # format:
    #   vac_view:<id>:<city_slug>:<cat_id>[:my]
    #   vac_view:<id>:search
    parts = cb.data.split(":")
    listing_id = int(parts[1])

    from_search = (len(parts) > 2 and parts[2] == "search")
    city_slug = None
    cat_id = None
    is_my_suffix = False

    if not from_search:
        city_slug = parts[2] if len(parts) > 2 else None
        cat_id = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
        is_my_suffix = (len(parts) > 4 and parts[4] == "my")

    async with SessionLocal() as s:
        stmt = select(Listing).where(Listing.id == listing_id, Listing.type == "vacancy")
        if not is_my_suffix:
            stmt = stmt.where(Listing.status == "active", Listing.is_sold.is_(False))
        listing = (await s.execute(stmt)).scalar_one_or_none()
        if not listing or (is_my_suffix and listing.owner_id != cb.from_user.id):
            await cb.answer(await get_text("vacancy_unavailable_archived", "ru") or "Вакансия недоступна или перенесена в архив.", show_alert=True)
            return
        city = (await s.execute(select(City).where(City.id == listing.city_id))).scalar_one_or_none()
        cat = (await s.execute(select(Category).where(Category.id == listing.category_id))).scalar_one_or_none()
        flex_block = await render_flex_block(s, listing, lang="ru")

        current_category_id = cat_id or listing.category_id
        cat_path = await render_category_path(s, current_category_id, root_id=VACANCY_ROOT_ID)

    # ЛОГ ОТКРЫТИЯ КАРТОЧКИ
    if from_search:
        source = "search"
    elif is_my_suffix:
        source = "my"
    else:
        source = "catalog"

    await log_listing_view(
        listing_id=listing.id,
        user_id=cb.from_user.id,
        section="vacancy",
        action="open",
        source=source,
    )


    is_owner = listing.owner_id == cb.from_user.id

    _esc = escape_html
    lines = []

    # Сразу под заголовком — город и категория
    lines.append(f"Город: <b>{_esc(city.name if city else '—')}</b>")
    lines.append(f"Категория: <b>Вакансии → {cat_path}</b>" if cat_path else "Категория: <b>Вакансии</b>")
    lines.append("")

    # Заголовок
    lines.append(f"<b>{_esc(listing.title or '(без заголовка)')}</b>")

    lines.append("")
    if listing.descr:
        lines.append(f"{_esc(listing.descr)}")

    lines.append("")
    if listing.price:
        lines.append(f"Оплата: <b>{_esc(str(listing.price))}</b>")

    if flex_block:
        lines.append("")
        lines.append(flex_block)

    contact = listing.contact or ""

    if is_owner:
        left_line = days_left_text(listing)
        if left_line:
            lines.append("")
            lines.append("Контакты/Управление:")
            lines.append(left_line)

    # Кнопки
    buttons: List[List[InlineKeyboardButton]] = []

    if is_owner:
        if from_search:
            owner_source = "search"
        elif is_my_suffix:
            owner_source = "my"
        else:
            owner_source = "catalog"

        buttons.append([InlineKeyboardButton(
            text="✏️ Редактировать все поля",
            callback_data=f"vacancy_edit_overview:{listing.id}:{owner_source}:{city_slug or '-'}:{cat_id or 0}"
        )])
        if is_active(listing):
            buttons.append([InlineKeyboardButton(
                text="📦 Закрыть (в архив)",
                callback_data=f"vac_close:{listing.id}:{owner_source}:{city_slug or '-'}:{cat_id or 0}"
            )])
        buttons.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"vac_delete_confirm:{listing.id}")])

        if should_show_extend_button(listing):
            buttons.append([InlineKeyboardButton(
                text="🔄 Продлить на 30 дней",
                callback_data=f"vac_extend:{listing.id}:{owner_source}:{city_slug or '-'}:{cat_id or 0}"
            )])
    else:
        if contact and contact.startswith("@"):
            buttons.append([InlineKeyboardButton(
                text="💬 Связаться",
                url=build_contact_url(listing.id, contact, cb.from_user.id, source),
            )])

    # Назад — строго по источнику открытия карточки
    if from_search or (city_slug and cat_id):
        back_btn = await get_common_menu_button('back')
        if back_btn:
            back_btn.callback_data = "vac_search_results" if from_search else f"vlist:{city_slug}:{cat_id}"
            buttons.append([back_btn])
    else:
        buttons.append([InlineKeyboardButton(text="⬅️ В меню вакансий", callback_data="go_isk")])

    main_btn = await get_common_menu_button('main_menu')
    if main_btn:
        buttons.append([main_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await safe_edit_or_send(cb, "\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await cb.answer()

    print(
        f"[vacancy_view.py] handler=vacancy_view_detail | "
        f"listing_id={listing_id} | is_owner={is_owner} | "
        f"from_search={from_search} | city_slug={city_slug} | cat_id={cat_id}"
    )


   




# RU: Продление вакансии на 30 дней — только автор. Редактируем текущую карточку без создания дублей.
@router.callback_query(F.data.startswith("vac_extend:"))
async def vac_extend_listing(cb: CallbackQuery):
    parts = cb.data.split(":", 4)
    if len(parts) < 5:
        await cb.answer(await get_text("vacancy_extend_data_error", "ru") or "Ошибка данных продления.", show_alert=True)
        return

    try:
        listing_id = int(parts[1])
    except ValueError:
        await cb.answer(await get_text("vacancy_invalid_id", "ru") or "Неверный идентификатор вакансии.", show_alert=True)
        return

    source = parts[2]
    city_slug = None if parts[3] == "-" else parts[3]
    try:
        cat_id = int(parts[4]) if parts[4] and parts[4] != "0" else None
    except ValueError:
        cat_id = None

    async with SessionLocal() as s:
        listing = (await s.execute(
            select(Listing).where(Listing.id == listing_id, Listing.type == "vacancy")
        )).scalar_one_or_none()
        if not listing:
            await cb.answer(await get_text("vacancy_not_found", "ru") or "Вакансия не найдена.", show_alert=True)
            return
        if listing.owner_id != cb.from_user.id:
            await cb.answer(await get_text("vacancy_extend_owner_only", "ru") or "Продлить может только автор вакансии.", show_alert=True)
            return

        if not should_show_extend_button(listing):
            # Либо снято с публикации (admin_removed/unpublished), либо до
            # истечения ещё далеко — старый callback не должен накручивать срок.
            await cb.answer(
                await get_text("vacancy_extend_unavailable", "ru") or "Продление сейчас недоступно. Кнопка появится за 5 дней до истечения срока.",
                show_alert=True,
            )
            return

        extend_listing(listing)
        await s.commit()
        await s.refresh(listing)

    from app.analytics import log_event
    await log_event("listing_extended", user_id=cb.from_user.id,
                    section="vacancy", entity_type="listing", entity_id=listing.id)

    # Обновляем нижний блок управления в уже открытой карточке.
    base_text = cb.message.html_text or cb.message.text or ""
    raw_lines = base_text.splitlines()
    cleaned = []
    skip_management_label = False
    for line in raw_lines:
        stripped = line.strip()
        if stripped == "Контакты/Управление:":
            skip_management_label = True
            continue
        if stripped.startswith("⏳ До архивации:"):
            continue
        # Строки экрана «Закрыть (в архив)» — не тащим их в реактивированную карточку
        if stripped.startswith("🔴 Вакансия закрыта") or stripped.startswith("Вернуть её можно"):
            continue
        cleaned.append(line)

    # Убираем лишние пустые строки в конце перед новым блоком.
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()

    left_line = days_left_text(listing)
    if left_line:
        cleaned.extend(["", "Контакты/Управление:", left_line])

    buttons: List[List[InlineKeyboardButton]] = []
    buttons.append([InlineKeyboardButton(
        text="✏️ Редактировать все поля",
        callback_data=f"vacancy_edit_overview:{listing.id}:{source}:{city_slug or '-'}:{cat_id or 0}"
    )])
    if is_active(listing):
        buttons.append([InlineKeyboardButton(
            text="📦 Закрыть (в архив)",
            callback_data=f"vac_close:{listing.id}:{source}:{city_slug or '-'}:{cat_id or 0}"
        )])
    buttons.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"vac_delete_confirm:{listing.id}")])

    if should_show_extend_button(listing):
        buttons.append([InlineKeyboardButton(
            text="🔄 Продлить на 30 дней",
            callback_data=f"vac_extend:{listing.id}:{source}:{city_slug or '-'}:{cat_id or 0}"
        )])

    if source == "search" or (source == "catalog" and city_slug and cat_id):
        back_btn = await get_common_menu_button('back')
        if back_btn:
            back_btn.callback_data = "vac_search_results" if source == "search" else f"vlist:{city_slug}:{cat_id}"
            buttons.append([back_btn])
    else:
        buttons.append([InlineKeyboardButton(text="⬅️ В меню вакансий", callback_data="go_isk")])

    main_btn = await get_common_menu_button('main_menu')
    if main_btn:
        buttons.append([main_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    await safe_edit_or_send(cb, "\n".join(cleaned), reply_markup=kb, parse_mode="HTML")
    await cb.answer(await get_text("vacancy_extended", "ru") or "Вакансия продлена на 30 дней.")
    print(
        f"[vacancy_view.py] handler=vac_extend_listing | "
        f"listing_id={listing.id} source={source} chat_id={cb.message.chat.id} user_id={cb.from_user.id}"
    )


# RU: «Закрыть (в архив)» — скрыть свою вакансию из выдачи, не удаляя.
#     Вернуть можно кнопкой «Вернуть в каталог» (vac_extend реактивирует).
@router.callback_query(F.data.startswith("vac_close:"))
async def vac_close_listing(cb: CallbackQuery):
    parts = cb.data.split(":", 4)
    if len(parts) < 5:
        await cb.answer(await get_text("vacancy_close_data_error", "ru") or "Ошибка данных закрытия.", show_alert=True)
        return

    try:
        listing_id = int(parts[1])
    except ValueError:
        await cb.answer(await get_text("vacancy_invalid_id", "ru") or "Неверный идентификатор вакансии.", show_alert=True)
        return

    source = parts[2]
    city_slug = None if parts[3] == "-" else parts[3]
    try:
        cat_id = int(parts[4]) if parts[4] and parts[4] != "0" else None
    except ValueError:
        cat_id = None

    async with SessionLocal() as s:
        listing = (await s.execute(
            select(Listing).where(Listing.id == listing_id, Listing.type == "vacancy")
        )).scalar_one_or_none()
        if not listing:
            await cb.answer(await get_text("vacancy_not_found", "ru") or "Вакансия не найдена.", show_alert=True)
            return
        if listing.owner_id != cb.from_user.id:
            await cb.answer(await get_text("vacancy_close_owner_only", "ru") or "Закрыть может только автор вакансии.", show_alert=True)
            return
        if not is_active(listing):
            await cb.answer(await get_text("vacancy_already_closed", "ru") or "Вакансия уже закрыта или в архиве.", show_alert=True)
            return

        archive_as_closed(listing, user_id=cb.from_user.id)
        await s.commit()
        await s.refresh(listing)

    from app.analytics import log_event
    try:
        await log_event("listing_closed", user_id=cb.from_user.id,
                        section="vacancy", entity_type="listing", entity_id=listing.id)
    except Exception as e:
        print(f"[vacancy_view.py] vac_close analytics error listing_id={listing.id}: {e}")

    # Сохраняем текст карточки на экране (как делает продление): убираем только
    # старый блок управления и добавляем строки о закрытии. Иначе после
    # «вернуть» карточка восстановилась бы пустой.
    base_text = cb.message.html_text or cb.message.text or ""
    cleaned_lines = []
    for line in base_text.splitlines():
        stripped = line.strip()
        if stripped == "Контакты/Управление:":
            continue
        if stripped.startswith("⏳ До архивации:"):
            continue
        cleaned_lines.append(line)
    while cleaned_lines and not cleaned_lines[-1].strip():
        cleaned_lines.pop()

    cleaned_lines.extend([
        "",
        "Контакты/Управление:",
        "🔴 Вакансия закрыта и скрыта из каталога.",
        "Вернуть её можно кнопкой ниже — текст сохранён.",
    ])
    text = "\n".join(cleaned_lines)

    buttons: List[List[InlineKeyboardButton]] = []
    buttons.append([InlineKeyboardButton(
        text="↩️ Вернуть в каталог (на 30 дней)",
        callback_data=f"vac_extend:{listing.id}:{source}:{city_slug or '-'}:{cat_id or 0}"
    )])
    buttons.append([InlineKeyboardButton(
        text="✏️ Редактировать все поля",
        callback_data=f"vacancy_edit_overview:{listing.id}:{source}:{city_slug or '-'}:{cat_id or 0}"
    )])
    buttons.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"vac_delete_confirm:{listing.id}")])

    if source == "search" or (source == "catalog" and city_slug and cat_id):
        back_btn = await get_common_menu_button('back')
        if back_btn:
            back_btn.callback_data = "vac_search_results" if source == "search" else f"vlist:{city_slug}:{cat_id}"
            buttons.append([back_btn])
    else:
        buttons.append([InlineKeyboardButton(text="⬅️ В меню вакансий", callback_data="go_isk")])

    main_btn = await get_common_menu_button('main_menu')
    if main_btn:
        buttons.append([main_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    await safe_edit_or_send(cb, text, reply_markup=kb, parse_mode="HTML")
    await cb.answer(await get_text("vacancy_closed", "ru") or "Вакансия закрыта.")
    print(
        f"[vacancy_view.py] handler=vac_close_listing | "
        f"listing_id={listing.id} source={source} chat_id={cb.message.chat.id} user_id={cb.from_user.id}"
    )


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
            await cb.answer(await get_text("vacancy_already_deleted", "ru") or "Объявление уже удалено.", show_alert=True)
            print(f"[vacancy_view.py] handler=vac_delete_yes listing_id={lid} status=not_found")
            return
        if obj.owner_id != cb.from_user.id:
            await cb.answer(await get_text("err_no_rights", "ru") or "⛔️ Недостаточно прав.", show_alert=True)
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
    msg = await cb.message.answer(await get_text("vacancy_deleted", "ru") or "✅ Объявление удалено.", reply_markup=kb, parse_mode="HTML")
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer(await get_text("feedback_deleted", "ru") or "Удалено.")
    print(f"[vacancy_view.py] handler=vac_delete_yes listing_id={lid} status=ok user_id={cb.from_user.id}")
class VacSearch(StatesGroup):
    """
    Состояние поиска вакансий.
    """
    waiting_query = State()




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
        await get_text("vacancy_search_title", "ru") or (
            "🔎 <b>Поиск вакансий</b>\n\n"
            "Введите запрос (например: «барабанщик», «вокалист», «звукорежиссёр»). "
            "Ищем по заголовку и описанию."
        ),
        parse_mode="HTML",
    )
    ids_to_register = [msg.message_id]
    if nav_msg:
        ids_to_register.append(nav_msg.message_id)
    await register_bot_messages(chat_id, ids_to_register)

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
        for key in ("nav_msg_id", "prompt_id", "search_prompt_msg_id"):
            mid = data.get(key)
            if mid:
                try:
                    await m.bot.delete_message(chat_id, mid)
                except Exception:
                    pass
        await state.update_data(nav_msg_id=None, prompt_id=None, search_prompt_msg_id=None)
    except Exception:
        pass

    # Дополнительно — подчистить из общих кэшей
    try:
        from app.routers.utils import last_search_menu_message, last_search_query_message
        for mid in (
            last_search_menu_message.pop(chat_id, None),
            last_search_query_message.pop(chat_id, None),
        ):
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
        buttons: List[List[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text="🔄 Новый поиск", callback_data="vac_search")],
            [InlineKeyboardButton(text="⬅️ В меню вакансий", callback_data="go_isk")],
        ]
        main_btn = await get_common_menu_button("main_menu")
        if main_btn:
            buttons.append([main_btn])

        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        msg = await m.answer(
            await get_text("search_min_2_chars", "ru") or "Минимум 2 символа. Введите запрос ещё раз:",
            reply_markup=kb,
            parse_mode="HTML",
        )

        try:
            from app.routers.utils import last_search_menu_message, last_search_query_message
            last_search_menu_message[chat_id] = msg.message_id
            last_search_query_message[chat_id] = msg.message_id
        except Exception:
            pass
        await register_bot_messages(chat_id, [msg.message_id])

        await state.set_state(VacSearch.waiting_query)
        _dbg("vacancy_view.vac_search_do.short", chat_id=chat_id, q=q)
        return

    async with SessionLocal() as s:
        db_rows = (await s.execute(
            select(Listing)
            .where(
                *_vacancy_public_predicates(),
            )
            .order_by(Listing.created_at.desc())
            .limit(1000)
        )).scalars().all()

    search_outcome = search_items(
        db_rows,
        q,
        lambda it: [
            it.title or "",
            it.descr or "",
        ],
    )

    rows = search_outcome.results
    search_query_raw = search_outcome.query_raw
    search_query_normalized = search_outcome.query_normalized
    search_query_effective = search_outcome.query_effective
    search_match_mode = search_outcome.match_mode

    total_count = len(rows)
    pages = max(1, (total_count + VACANCY_SEARCH_PAGE_SIZE - 1) // VACANCY_SEARCH_PAGE_SIZE)
    page = 1
    rows_page = rows[:VACANCY_SEARCH_PAGE_SIZE]

    # ЛОГИРОВАНИЕ ПОИСКА
    await log_search(
        user_id=m.from_user.id,
        section="vacancy",
        query_raw=search_query_raw,
        query_normalized=search_query_normalized,
        query_effective=search_query_effective,
        match_mode=search_match_mode,
        results_count=total_count,
    )

    # Сохраняем контекст поиска для кнопки «Назад» из карточки
    await state.update_data(
        vac_search_query=q,
        vac_search_query_raw=search_query_raw,
        vac_search_query_normalized=search_query_normalized,
        vac_search_query_effective=search_query_effective,
        vac_search_match_mode=search_match_mode,
        vac_search_result_ids=[l.id for l in rows],
        vac_search_offset=0,
    )

    # Сборка клавиатуры результатов
    buttons: List[List[InlineKeyboardButton]] = []

    for l in rows_page:
        title = (l.title or "(без заголовка)").strip()
        price = f" — {l.price}" if getattr(l, "price", None) else ""
        buttons.append([
            InlineKeyboardButton(
                text=f"{title}{price}",
                callback_data=f"vac_view:{l.id}:search",
            )
        ])

    if pages > 1:
        pager = [
            InlineKeyboardButton(text=f"{page}/{pages}", callback_data="stub"),
            InlineKeyboardButton(
                text="»",
                callback_data=f"vac_search_page:{VACANCY_SEARCH_PAGE_SIZE}"
            )
        ]
        buttons.append(pager)

    buttons.append([InlineKeyboardButton(text="🔄 Новый поиск", callback_data="vac_search")])
    buttons.append([InlineKeyboardButton(text="⬅️ В меню вакансий", callback_data="go_isk")])

    main_btn = await get_common_menu_button("main_menu")
    if main_btn:
        buttons.append([main_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    correction_note = ""
    if search_match_mode == "corrected" and search_query_effective != search_query_normalized:
        note_tmpl = await get_text("search_typo_correction_note", "ru") or (
            "🧠 Показаны результаты по запросу: <b>{query}</b> (учтена возможная опечатка).\n\n"
        )
        correction_note = note_tmpl.format(query=escape_html(search_query_effective))

    msg = await m.answer(
        (await get_text("search_results_found", "ru") or "{correction_note}Результаты по запросу: <b>{query}</b>\nНайдено: {count}").format(correction_note=correction_note, query=escape_html(q), count=total_count),
        reply_markup=kb,
        parse_mode="HTML",
    )

    try:
        from app.routers.utils import last_search_menu_message, last_search_query_message
        last_search_menu_message[chat_id] = msg.message_id
        last_search_query_message[chat_id] = msg.message_id
    except Exception:
        pass
    await register_bot_messages(chat_id, [msg.message_id])

    await state.set_state(VacSearch.waiting_query)

    _dbg(
        "vacancy_view.vac_search_do",
        chat_id=chat_id,
        user_id=m.from_user.id,
        q=q,
        found=total_count,
        match_mode=search_match_mode,
        effective=search_query_effective,
    )
    

@router.callback_query(F.data == "vac_search_results")
async def vac_search_results(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)

    try:
        await cb.message.delete()
    except Exception:
        pass

    data = await state.get_data()
    q = (data.get("vac_search_query") or "").strip()
    ids = data.get("vac_search_result_ids") or []
    
    offset = data.get("vac_search_offset") or 0

    if not ids:
        buttons: List[List[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text="🔄 Новый поиск", callback_data="vac_search")],
            [InlineKeyboardButton(text="⬅️ В меню вакансий", callback_data="go_isk")],
        ]
        main_btn = await get_common_menu_button('main_menu')
        if main_btn:
            buttons.append([main_btn])

        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

        msg = await cb.bot.send_message(
            chat_id,
            (await get_text("vacancy_search_unavailable", "ru") or "Результаты поиска недоступны."),
            reply_markup=kb,
            parse_mode="HTML",
        )

        try:
            from app.routers.utils import last_search_menu_message, last_search_query_message
            last_search_menu_message[chat_id] = msg.message_id
            last_search_query_message[chat_id] = msg.message_id
        except Exception:
            pass
        await register_bot_messages(chat_id, [msg.message_id])

        await cb.answer()
        _dbg("vacancy_view.vac_search_results.empty_ctx", chat_id=chat_id)
        return

    ids, valid_rows = await _load_public_vacancy_ids(ids)
    await state.update_data(vac_search_result_ids=ids)

    # После ревалидации offset мог выехать за край (закрыли вакансии) — прижимаем.
    total_count = len(ids)
    pages = max(1, (total_count + VACANCY_SEARCH_PAGE_SIZE - 1) // VACANCY_SEARCH_PAGE_SIZE)
    if offset >= total_count:
        offset = (pages - 1) * VACANCY_SEARCH_PAGE_SIZE
        await state.update_data(vac_search_offset=offset)
    page = offset // VACANCY_SEARCH_PAGE_SIZE + 1
    rows = valid_rows[offset:offset + VACANCY_SEARCH_PAGE_SIZE]

    buttons: List[List[InlineKeyboardButton]] = []

    for l in rows:
        title = (l.title or "(без заголовка)").strip()
        price = f" — {l.price}" if getattr(l, "price", None) else ""
        buttons.append([
            InlineKeyboardButton(
                text=f"{title}{price}",
                callback_data=f"vac_view:{l.id}:search"
            )
        ])

    # Пагинация — как в vac_search_page, иначе возврат со 2-й страницы её терял
    if pages > 1:
        pager = []
        if page > 1:
            pager.append(InlineKeyboardButton(
                text="«", callback_data=f"vac_search_page:{max(0, offset - VACANCY_SEARCH_PAGE_SIZE)}"))
        pager.append(InlineKeyboardButton(text=f"{page}/{pages}", callback_data="stub"))
        if page < pages:
            pager.append(InlineKeyboardButton(
                text="»", callback_data=f"vac_search_page:{offset + VACANCY_SEARCH_PAGE_SIZE}"))
        buttons.append(pager)

    buttons.append([InlineKeyboardButton(text="🔄 Новый поиск", callback_data="vac_search")])
    buttons.append([InlineKeyboardButton(text="⬅️ В меню вакансий", callback_data="go_isk")])

    main_btn = await get_common_menu_button('main_menu')
    if main_btn:
        buttons.append([main_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    msg = await cb.bot.send_message(
        chat_id,
        (await get_text("search_results_found", "ru") or "{correction_note}Результаты по запросу: <b>{query}</b>\nНайдено: {count}").format(correction_note="", query=escape_html(q), count=total_count),
        reply_markup=kb,
        parse_mode="HTML",
    )

    try:
        from app.routers.utils import last_search_menu_message, last_search_query_message
        last_search_menu_message[chat_id] = msg.message_id
        last_search_query_message[chat_id] = msg.message_id
    except Exception:
        pass
    await register_bot_messages(chat_id, [msg.message_id])

    await cb.answer()
    _dbg("vacancy_view.vac_search_results", chat_id=chat_id, q=q, found=len(rows))





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
    msg = await cb.bot.send_message(chat_id, title, reply_markup=kb, parse_mode="HTML")
    await register_bot_messages(chat_id, [msg.message_id])

    await cb.answer()
    print("OK: vacancy_main_menu_cb done")


@router.callback_query(F.data.startswith("vac_search_page:"))
async def vac_search_page(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    await clear_bot_messages(chat_id, cb.bot)

    try:
        await cb.message.delete()
    except Exception:
        pass

    offset = int(cb.data.split(":")[1])

    await state.update_data(vac_search_offset=offset)

    data = await state.get_data()

    q = data.get("vac_search_query") or ""
    search_query_normalized = data.get("vac_search_query_normalized") or ""
    search_query_effective = data.get("vac_search_query_effective") or ""
    search_match_mode = data.get("vac_search_match_mode") or "none"
    ids = data.get("vac_search_result_ids") or []

    ids, valid_rows = await _load_public_vacancy_ids(ids)
    await state.update_data(vac_search_result_ids=ids)
    rows = valid_rows[offset:offset + VACANCY_SEARCH_PAGE_SIZE]

    total_count = len(ids)
    page = (offset // VACANCY_SEARCH_PAGE_SIZE) + 1
    pages = max(1, (total_count + VACANCY_SEARCH_PAGE_SIZE - 1) // VACANCY_SEARCH_PAGE_SIZE)

    buttons: List[List[InlineKeyboardButton]] = []

    for l in rows:
        title = (l.title or "(без заголовка)").strip()
        price = f" — {l.price}" if getattr(l, "price", None) else ""
        buttons.append([
            InlineKeyboardButton(
                text=f"{title}{price}",
                callback_data=f"vac_view:{l.id}:search"
            )
        ])

    if pages > 1:
        pager = []

        if page > 1:
            pager.append(
                InlineKeyboardButton(
                    text="«",
                    callback_data=f"vac_search_page:{offset - VACANCY_SEARCH_PAGE_SIZE}"
                )
            )

        pager.append(
            InlineKeyboardButton(
                text=f"{page}/{pages}",
                callback_data="stub"
            )
        )

        if page < pages:
            pager.append(
                InlineKeyboardButton(
                    text="»",
                    callback_data=f"vac_search_page:{offset + VACANCY_SEARCH_PAGE_SIZE}"
                )
            )

        buttons.append(pager)

    buttons.append([InlineKeyboardButton(text="🔄 Новый поиск", callback_data="vac_search")])
    buttons.append([InlineKeyboardButton(text="⬅️ В меню вакансий", callback_data="go_isk")])

    main_btn = await get_common_menu_button("main_menu")
    if main_btn:
        buttons.append([main_btn])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    correction_note = ""
    if search_match_mode == "corrected" and search_query_effective != search_query_normalized:
        note_tmpl = await get_text("search_typo_correction_note", "ru") or (
            "🧠 Показаны результаты по запросу: <b>{query}</b> (учтена возможная опечатка).\n\n"
        )
        correction_note = note_tmpl.format(query=escape_html(search_query_effective))

    msg = await cb.bot.send_message(
        chat_id,
        (await get_text("search_results_found", "ru") or "{correction_note}Результаты по запросу: <b>{query}</b>\nНайдено: {count}").format(correction_note=correction_note, query=escape_html(q), count=total_count),
        reply_markup=kb,
        parse_mode="HTML",
    )

    from app.routers.utils import last_search_menu_message, last_search_query_message
    last_search_menu_message[chat_id] = msg.message_id
    last_search_query_message[chat_id] = msg.message_id
    await register_bot_messages(chat_id, [msg.message_id])

    await cb.answer()

    print(
        f"[vacancy_view.py] vac_search_page | "
        f"chat_id={chat_id} page={page}/{pages} "
        f"match_mode={search_match_mode} effective={search_query_effective!r}"
    )
