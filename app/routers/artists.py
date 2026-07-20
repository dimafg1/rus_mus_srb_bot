# app/routers/artists.py
"""
🎤 Исполнители — витрина музыкального слоя (журнал решений Р-12, MVP).

Карточки создаются мастером релизов (app/routers/releases.py); здесь —
лента и карточка исполнителя с его релизами. Связь двусторонняя:
карточка релиза → «Об исполнителе» → карточка исполнителя → его релизы.

Правила: раздел за выключателем releases_enabled; каждый экран чистит
предыдущие сообщения; на каждом экране «⬅️ Назад» (ровно один шаг,
поэтому в callback карточки зашит источник перехода) + «☰ Главное меню».
"""
import json

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlmodel import select

from app.database import SessionLocal
from app.models import Artist, BotUser, Listing, ReleaseMeta, ReleaseTrack
from app.analytics import log_event
from app.features import is_enabled
from app.keyboards import get_common_menu_button
from app.routers.releases import (
    ARTIST_TYPES,
    MAX_LINKS,
    RELEASE_TYPES,
    _MusicEnabledMiddleware,
    _ask_artname,
    _clean_release_source,
    _e,
    _fit_html_lines,
    _has_release_media,
    _load_links,
    _menu_btn,
    _parse_link_text,
    _replace_prompt,
    _send_screen,
)
from app.routers.utils import clear_bot_messages, get_text

router = Router(name="artists")
router.callback_query.middleware(_MusicEnabledMiddleware())
router.message.middleware(_MusicEnabledMiddleware())

PAGE = 8


async def _back_btn(callback_data: str) -> InlineKeyboardButton:
    btn = await get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data=callback_data)
    btn.callback_data = callback_data
    return btn

# Поля карточки для редактирования: код → (подпись, подсказка ввода)
EDIT_FIELDS = {
    "name": ("Название", "Новое название исполнителя?"),
    "type": ("Тип", None),  # выбирается кнопками
    "photo": ("Фото", "Пришлите новое фото или логотип."),
    "descr": ("Описание", "Расскажите об исполнителе (пара абзацев):"),
    "genres": ("Жанры", "Жанры через запятую (например: рок, инди):"),
    "city_text": ("Город", "Город базирования (например: Белград):"),
    "links": ("Ссылки", "Ссылки на соцсети и площадки — одним сообщением,\n"
                        "каждая с новой строки или через пробел:"),
    "contact": ("Контакты", "Дополнительные контакты через пробел — участники "
                            "группы, агент (например: @drummer @manager).\n"
                            "Контакт автора карточки остаётся всегда:"),
}
CLEARABLE = {"descr", "genres", "city_text", "links", "contact"}


class ArtistEdit(StatesGroup):
    value = State()


@router.callback_query(F.data == "go_artists")
@router.callback_query(F.data.startswith("art:list:"))
async def artists_feed(cb: CallbackQuery, state: FSMContext):
    if not await is_enabled("releases_enabled", user_id=cb.from_user.id):
        await cb.answer(await get_text("music_section_unavailable", "ru") or "Раздел временно недоступен.", show_alert=True)
        return
    await cb.answer()
    await state.clear()
    offset = 0
    if cb.data.startswith("art:list:"):
        try:
            offset = max(0, int(cb.data.split(":")[2]))
        except ValueError:
            offset = 0

    from app.routers.admin_panel import is_admin
    admin = is_admin(cb.from_user.id)
    async with SessionLocal() as s:
        q = select(Artist)
        if not admin:  # админ видит и скрытых (с пометкой) — иначе их не найти
            q = q.where(Artist.status == "active")
        artists = (await s.execute(
            q.order_by(Artist.created_at.desc())  # новые сверху, как в релизах
        )).scalars().all()
    total = len(artists)
    page = artists[offset:offset + PAGE]

    rows = [[InlineKeyboardButton(
        text=("🔴 " if a.status != "active" else "") + f"🎤 {a.name} · {a.artist_type}",
        callback_data=f"art:view:{a.id}:list")] for a in page]
    pages = max(1, (total + PAGE - 1) // PAGE)
    if pages > 1:
        nav: list[InlineKeyboardButton] = []
        if offset > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"art:list:{max(0, offset - PAGE)}"))
        nav.append(InlineKeyboardButton(text=f"{offset // PAGE + 1}/{pages}", callback_data="rel:noop"))
        if offset + PAGE < total:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"art:list:{offset + PAGE}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔍 Поиск", callback_data="art:search"),
                 InlineKeyboardButton(text="➕ Добавить исполнителя", callback_data="art:add")])
    rows.append([InlineKeyboardButton(text="🎵 Релизы", callback_data="go_releases"), _menu_btn()])

    text = ("🎤 <b>Исполнители сообщества</b>\n\n"
            "Сольные артисты, группы и музыкальные проекты.")
    if total == 0:
        text += ("\n\nПока пусто. Карточка исполнителя создаётся при публикации "
                 "первого релиза — раздел «🎵 Релизы».")
    await _send_screen(cb.bot, cb.message.chat.id, text,
                       InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "art:add")
async def artist_add(cb: CallbackQuery, state: FSMContext):
    """Создание исполнителя без релиза: та же мини-анкета из мастера релизов,
    но финал — карточка исполнителя (artist_flow='standalone')."""
    await cb.answer()
    await state.clear()
    await state.update_data(artist_flow="standalone", new_artist=None, created_artist_id=None)
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    await _ask_artname(cb.bot, cb.message.chat.id, state)


@router.callback_query(F.data.startswith("art:view:"))
async def artist_view(cb: CallbackQuery):
    parts = cb.data.split(":")
    try:
        artist_id = int(parts[2])
    except (IndexError, ValueError):
        await cb.answer(await get_text("err_invalid_link", "ru") or "Некорректная ссылка.", show_alert=True)
        return
    src = parts[3] if len(parts) > 3 else "list"
    await _show_artist_card(cb, artist_id, src)
    try:
        await cb.answer()
    except Exception:
        pass


async def _show_artist_card(cb: CallbackQuery, artist_id: int, src: str = "list"):
    """Рендер карточки исполнителя. Вызывается и после Скрыть/Показать,
    чтобы экран сразу отражал новый статус."""
    from app.routers.admin_panel import is_admin
    async with SessionLocal() as s:
        artist = (await s.execute(
            select(Artist).where(Artist.id == artist_id)
        )).scalar_one_or_none()
        if not artist:
            await cb.answer(await get_text("artist_not_found", "ru") or "Исполнитель не найден.", show_alert=True)
            return
        if artist.status != "active" and not (
            is_admin(cb.from_user.id) or artist.owner_user_id == cb.from_user.id
        ):
            await cb.answer(await get_text("music_card_unavailable", "ru") or "Карточка недоступна.", show_alert=True)
            return
        # админ и владелец карточки видят и скрытые релизы (с пометкой) —
        # иначе скрытое из бота не найти и не вернуть
        can_see_hidden = is_admin(cb.from_user.id) or artist.owner_user_id == cb.from_user.id
        q = select(ReleaseMeta).where(
            ReleaseMeta.artist_id == artist_id,
            ReleaseMeta.status != "deleted",
        )
        if not can_see_hidden:
            q = q.where(ReleaseMeta.status == "published")
        metas = (await s.execute(
            q.order_by(ReleaseMeta.created_at.desc())
        )).scalars().all()
        releases = []
        for m in metas:
            listing = (await s.execute(
                select(Listing).where(
                    Listing.id == m.listing_id,
                    Listing.type == "release",
                    Listing.status == "active",
                )
            )).scalar_one_or_none()
            tracks = (await s.execute(
                select(ReleaseTrack).where(ReleaseTrack.listing_id == m.listing_id)
            )).scalars().all()
            if (
                listing
                and listing.owner_id == artist.owner_user_id
                and (can_see_hidden or _has_release_media(m, tracks))
            ):
                releases.append((listing, m))

    sub = [_e(artist.artist_type)]
    if artist.genres:
        sub.append(_e(artist.genres))
    if artist.city_text:
        sub.append(_e(artist.city_text))
    lines = [f"🎤 <b>{_e(artist.name)}</b>", " · ".join(sub)]
    if artist.status != "active":
        lines.insert(0, "🚫 <i>Карточка скрыта</i>")
    if artist.descr:
        lines.append("")
        lines.append(_e(artist.descr))
    if artist.contact:
        lines.append("")
        lines.append(f"✍️ Связаться: {_e(artist.contact)}")
    if releases:
        lines.append("")
        lines.append(f"Релизов: {len(releases)}")
    else:
        lines.append("")
        lines.append("Релизов пока нет.")
    caption = _fit_html_lines(lines)

    artist_source = "s" if src == "search" else "l"
    rows = [[InlineKeyboardButton(
        text=("🔴 " if m.status != "published" else "")
             + f"🎵 {l.title} ({RELEASE_TYPES.get(m.release_type, '')})",
        callback_data=f"rel:view:{l.id}:a{artist.id}.{artist_source}")] for l, m in releases]
    # ссылки соцсетей/площадок — кнопками парами
    link_row: list[InlineKeyboardButton] = []
    try:
        for l in _load_links(artist.links):
            link_row.append(InlineKeyboardButton(text=f"🔗 {l['label']}", url=l["url"]))
            if len(link_row) == 2:
                rows.append(link_row)
                link_row = []
    except Exception as e:
        print(f"[artists] links JSON ({artist.id}): {e}")
    if link_row:
        rows.append(link_row)
    if artist.owner_user_id == cb.from_user.id or is_admin(cb.from_user.id):
        rows.append([InlineKeyboardButton(text="✏️ Редактировать",
                                          callback_data=f"art:edit:{artist.id}")])
    if is_admin(cb.from_user.id) and artist.status == "active":
        rows.append([InlineKeyboardButton(text="🚫 Скрыть", callback_data=f"art:hide:{artist.id}")])
    if is_admin(cb.from_user.id) and artist.status == "hidden":
        rows.append([InlineKeyboardButton(text="✅ Показать", callback_data=f"art:show:{artist.id}")])
    # «Назад» — ровно на один шаг: к списку, к результатам поиска
    # или к релизу — туда, откуда пришли
    if src == "list":
        back_cb = "go_artists"
    elif src == "search":
        back_cb = "art:sback"
    elif src.startswith("rel"):
        rel_ref = src[3:]
        listing_ref, _, rel_source = rel_ref.partition(".")
        rel_source = _clean_release_source(rel_source)
        back_cb = (f"rel:view:{listing_ref}:{rel_source}"
                   if listing_ref.isdigit() else "go_artists")
    else:
        back_cb = "go_artists"
    rows.append([await _back_btn(back_cb), _menu_btn()])

    await _send_screen(cb.bot, cb.message.chat.id, caption,
                       InlineKeyboardMarkup(inline_keyboard=rows),
                       photo=artist.photo_file_id)
    await log_event("artist_opened", user_id=cb.from_user.id,
                    entity_type="artist", entity_id=artist.id, source=src[:16])


# ─────────────────────── редактирование карточки ───────────────────────

async def _can_edit(user_id: int, artist: Artist | None) -> bool:
    from app.routers.admin_panel import is_admin
    return bool(artist and (artist.owner_user_id == user_id or is_admin(user_id)))


def _fmt(v, limit=40):
    if not v:
        return "—"
    s = str(v)
    return s[:limit] + "…" if len(s) > limit else s


async def _render_edit_overview(bot, chat_id: int, user_id: int, artist_id: int):
    """Обзор редактирования — по образцу услуг: значения + кнопки «Править»."""
    async with SessionLocal() as s:
        artist = (await s.execute(
            select(Artist).where(Artist.id == artist_id)
        )).scalar_one_or_none()
    if not artist or not await _can_edit(user_id, artist):
        return
    try:
        n_links = len(_load_links(artist.links))
    except Exception:
        n_links = 0
    values = {
        "name": artist.name, "type": artist.artist_type,
        "photo": "есть" if artist.photo_file_id else "—",
        "descr": _fmt(artist.descr), "genres": _fmt(artist.genres),
        "city_text": _fmt(artist.city_text),
        "links": f"{n_links} шт." if n_links else "—",
        "contact": _fmt(artist.contact),
    }
    lines = [f"✏️ <b>Карточка: {_e(artist.name)}</b>", ""]
    for code, (label, _) in EDIT_FIELDS.items():
        lines.append(f"{label}: {_e(values[code])}")
    rows = [[InlineKeyboardButton(text=f"✏️ Править: {label}",
                                  callback_data=f"art:ef:{code}:{artist_id}")]
            for code, (label, _) in EDIT_FIELDS.items()]
    rows.append([await _back_btn(f"art:view:{artist_id}:list"), _menu_btn()])
    await _send_screen(bot, chat_id, "\n".join(lines),
                       InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("art:edit:"))
async def artist_edit(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await _render_edit_overview(cb.bot, cb.message.chat.id, cb.from_user.id,
                                int(cb.data.split(":")[2]))


@router.callback_query(F.data.startswith("art:ef:"))
async def artist_edit_field(cb: CallbackQuery, state: FSMContext):
    _, _, field, aid = cb.data.split(":")
    artist_id = int(aid)
    if field not in EDIT_FIELDS:
        await cb.answer()
        return
    async with SessionLocal() as s:
        artist = (await s.execute(
            select(Artist).where(Artist.id == artist_id)
        )).scalar_one_or_none()
    if not await _can_edit(cb.from_user.id, artist):
        await cb.answer(await get_text("music_no_rights_or_unavailable", "ru") or "Нет прав или карточка недоступна.", show_alert=True)
        return
    await cb.answer()
    if field == "type":  # тип — кнопками
        rows = [[InlineKeyboardButton(text=t, callback_data=f"art:etype:{artist_id}:{i}")]
                for i, t in enumerate(ARTIST_TYPES)]
        rows.append([await _back_btn(f"art:edit:{artist_id}"), _menu_btn()])
        await _replace_prompt(state, cb.bot, cb.message.chat.id, "Выберите тип:",
                              InlineKeyboardMarkup(inline_keyboard=rows))
        return
    await state.set_state(ArtistEdit.value)
    await state.update_data(edit_field=field, edit_artist_id=artist_id)
    label, hint = EDIT_FIELDS[field]
    rows = []
    if field in CLEARABLE:
        rows.append([InlineKeyboardButton(text="🗑 Очистить поле",
                                          callback_data=f"art:eclr:{artist_id}:{field}")])
    rows.append([await _back_btn(f"art:edit:{artist_id}"), _menu_btn()])
    await _replace_prompt(state, cb.bot, cb.message.chat.id, hint,
                          InlineKeyboardMarkup(inline_keyboard=rows))


async def _base_contact(artist_id: int) -> str | None:
    """Базовый контакт карточки — @ник создателя. Не удаляется при редактуре."""
    async with SessionLocal() as s:
        artist = (await s.execute(
            select(Artist).where(Artist.id == artist_id)
        )).scalar_one_or_none()
        if not artist:
            return None
        u = (await s.execute(
            select(BotUser).where(BotUser.user_id == artist.owner_user_id)
        )).scalars().first()
    return f"@{u.username}" if u and u.username else None


async def _save_artist_field(user_id: int, artist_id: int, field: str, value):
    async with SessionLocal() as s:
        artist = (await s.execute(
            select(Artist).where(Artist.id == artist_id)
        )).scalar_one_or_none()
        if not artist or not await _can_edit(user_id, artist):
            return False
        target = {"type": "artist_type"}.get(field, field)
        if target not in {
            "name", "artist_type", "photo_file_id", "descr", "genres",
            "city_text", "links", "contact",
        }:
            return False
        setattr(artist, target, value)
        s.add(artist)
        await s.commit()
    return True


@router.callback_query(F.data.startswith("art:etype:"))
async def artist_edit_type(cb: CallbackQuery, state: FSMContext):
    _, _, aid, idx = cb.data.split(":")
    try:
        idx_int = int(idx)
    except ValueError:
        await cb.answer(await get_text("err_invalid_link", "ru") or "Некорректная ссылка.", show_alert=True)
        return
    t = ARTIST_TYPES[idx_int] if 0 <= idx_int < len(ARTIST_TYPES) else "Другое"
    if not await _save_artist_field(cb.from_user.id, int(aid), "type", t):
        await cb.answer(await get_text("music_no_rights_or_unavailable", "ru") or "Нет прав или карточка недоступна.", show_alert=True)
        return
    await cb.answer()
    await _render_edit_overview(cb.bot, cb.message.chat.id, cb.from_user.id, int(aid))


@router.callback_query(F.data.startswith("art:eclr:"))
async def artist_edit_clear(cb: CallbackQuery, state: FSMContext):
    _, _, aid, field = cb.data.split(":")
    saved = False
    if field == "contact":
        # контакты не обнуляются в пустоту — остаётся базовый (@создатель)
        saved = await _save_artist_field(cb.from_user.id, int(aid), "contact",
                                         await _base_contact(int(aid)))
    elif field in CLEARABLE:
        saved = await _save_artist_field(cb.from_user.id, int(aid), field, None)
    if not saved:
        await cb.answer(await get_text("music_no_rights_or_field_locked", "ru") or "Нет прав или поле нельзя очистить.", show_alert=True)
        return
    await cb.answer(await get_text("music_field_cleared", "ru") or "Очищено.")
    await state.clear()
    await _render_edit_overview(cb.bot, cb.message.chat.id, cb.from_user.id, int(aid))


@router.message(ArtistEdit.value, F.text)
async def artist_edit_text(message: Message, state: FSMContext):
    data = await state.get_data()
    field, artist_id = data.get("edit_field"), data.get("edit_artist_id")
    text_val = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass
    if not field or not artist_id:
        return
    if field == "photo":
        return  # для фото ждём картинку, не текст
    if field == "links":
        links = _parse_link_text(text_val, limit=MAX_LINKS)
        if not links:
            await message.answer(await get_text("music_link_needs_scheme", "ru") or "Нужна полноценная ссылка с http:// или https://.")
            return
        value = json.dumps(links, ensure_ascii=False)
    elif field == "contact":
        # база (@создатель) всегда впереди, ввод только ДОБАВЛЯЕТ контакты
        base = await _base_contact(artist_id)
        extras = [t for t in text_val.replace(",", " ").split() if t and t != base][:5]
        value = " ".join(([base] if base else []) + extras)[:128] or None
    elif field == "name":
        value = text_val[:128]
        if not value:
            return
    else:
        limits = {"descr": 600, "genres": 128, "city_text": 64, "contact": 128}
        value = text_val[:limits.get(field, 255)] or None
    if not await _save_artist_field(message.from_user.id, artist_id, field, value):
        await message.answer(await get_text("music_save_failed_no_rights", "ru") or "Не удалось сохранить: нет прав или карточка недоступна.")
        return
    await state.clear()
    await _render_edit_overview(message.bot, message.chat.id, message.from_user.id, artist_id)


@router.message(ArtistEdit.value, F.photo)
async def artist_edit_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    field, artist_id = data.get("edit_field"), data.get("edit_artist_id")
    try:
        await message.delete()
    except Exception:
        pass
    if field != "photo" or not artist_id:
        return
    if not await _save_artist_field(message.from_user.id, artist_id, "photo_file_id",
                                    message.photo[-1].file_id):
        await message.answer(await get_text("music_save_failed_no_rights", "ru") or "Не удалось сохранить: нет прав или карточка недоступна.")
        return
    await state.clear()
    await _render_edit_overview(message.bot, message.chat.id, message.from_user.id, artist_id)


@router.callback_query(F.data.startswith("art:hide:"))
async def artist_hide(cb: CallbackQuery):
    from app.routers.admin_panel import is_admin
    if not is_admin(cb.from_user.id):
        await cb.answer(await get_text("music_admin_only", "ru") or "Только для администратора.", show_alert=True)
        return
    artist_id = int(cb.data.split(":")[2])
    async with SessionLocal() as s:
        artist = (await s.execute(
            select(Artist).where(Artist.id == artist_id)
        )).scalar_one_or_none()
        if not artist:
            await cb.answer(await get_text("music_not_found", "ru") or "Не найден.", show_alert=True)
            return
        artist.status = "hidden"
        s.add(artist)
        await s.commit()
    await cb.answer()
    await _show_artist_card(cb, artist_id, "list")  # сразу свежий статус и кнопки


@router.callback_query(F.data.startswith("art:show:"))
async def artist_show(cb: CallbackQuery):
    from app.routers.admin_panel import is_admin
    if not is_admin(cb.from_user.id):
        await cb.answer(await get_text("music_admin_only", "ru") or "Только для администратора.", show_alert=True)
        return
    artist_id = int(cb.data.split(":")[2])
    async with SessionLocal() as s:
        artist = (await s.execute(
            select(Artist).where(Artist.id == artist_id)
        )).scalar_one_or_none()
        if not artist:
            await cb.answer(await get_text("music_not_found", "ru") or "Не найден.", show_alert=True)
            return
        artist.status = "active"
        s.add(artist)
        await s.commit()
    await cb.answer()
    await _show_artist_card(cb, artist_id, "list")


# ─────────────────────── поиск исполнителей (fuzzy) ───────────────────────
from app.search.fuzzy import search_items          # noqa: E402
from app.analytics.search_log import log_search    # noqa: E402
from app.routers.releases import _nav_row          # noqa: E402

SEARCH_PAGE = 10


class ArtSearch(StatesGroup):
    waiting_query = State()


@router.callback_query(F.data == "art:search")
async def art_search_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(ArtSearch.waiting_query)
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    await _replace_prompt(state, cb.bot, cb.message.chat.id,
                          "🔍 Введите запрос: название исполнителя, жанр или город "
                          "(от 2 символов).",
                          InlineKeyboardMarkup(inline_keyboard=[await _nav_row("go_artists")]))


async def _render_art_search(bot, chat_id: int, state: FSMContext, offset: int = 0):
    data = await state.get_data()
    results = data.get("art_s_results") or []   # [(artist_id, label), ...]
    q = data.get("art_s_query") or ""
    note = data.get("art_s_note") or ""
    total = len(results)
    pages = max(1, (total + SEARCH_PAGE - 1) // SEARCH_PAGE)
    page = results[offset:offset + SEARCH_PAGE]

    await state.update_data(art_s_offset=offset)
    rows = [[InlineKeyboardButton(text=label, callback_data=f"art:view:{aid}:search")]
            for aid, label in page]
    if pages > 1:
        nav = []
        if offset > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"art:spage:{max(0, offset - SEARCH_PAGE)}"))
        nav.append(InlineKeyboardButton(text=f"{offset // SEARCH_PAGE + 1}/{pages}", callback_data="rel:noop"))
        if offset + SEARCH_PAGE < total:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"art:spage:{offset + SEARCH_PAGE}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔄 Новый поиск", callback_data="art:search")])
    rows.append(await _nav_row("go_artists"))
    await _send_screen(bot, chat_id,
                       f"{note}Результаты по запросу: <b>{_e(q)}</b>\nНайдено: {total}",
                       InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(ArtSearch.waiting_query, F.text)
async def art_search_do(message: Message, state: FSMContext):
    q = (message.text or "").strip()[:128]
    try:
        await message.delete()
    except Exception:
        pass
    if len(q) < 2:
        await _replace_prompt(state, message.bot, message.chat.id,
                              "Минимум 2 символа. Введите запрос ещё раз:",
                              InlineKeyboardMarkup(inline_keyboard=[await _nav_row("go_artists")]))
        return

    async with SessionLocal() as s:
        artists = (await s.execute(
            select(Artist).where(Artist.status == "active")
            .order_by(Artist.created_at.desc()).limit(1000)
        )).scalars().all()
    items = [(a.id, f"🎤 {a.name} · {a.artist_type}",
              [a.name or "", a.genres or "", a.city_text or "", a.descr or ""])
             for a in artists]

    outcome = search_items(items, q, lambda it: it[2])
    await log_search(user_id=message.from_user.id, section="artists",
                     query_raw=outcome.query_raw,
                     query_normalized=outcome.query_normalized,
                     query_effective=outcome.query_effective,
                     match_mode=outcome.match_mode,
                     results_count=len(outcome.results))
    note = ""
    if outcome.match_mode == "corrected" and outcome.query_effective != outcome.query_normalized:
        note = (f"🧠 Показаны результаты по запросу: <b>{_e(outcome.query_effective)}</b> "
                f"(учтена возможная опечатка).\n\n")
    await state.update_data(
        art_s_results=[(it[0], it[1]) for it in outcome.results],
        art_s_query=q, art_s_note=note,
    )
    await _render_art_search(message.bot, message.chat.id, state, 0)


@router.callback_query(F.data.startswith("art:spage:"))
async def art_search_page(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    offset = max(0, int(cb.data.split(":")[2]))
    await _render_art_search(cb.bot, cb.message.chat.id, state, offset)


@router.callback_query(F.data == "art:sback")
async def art_search_back(cb: CallbackQuery, state: FSMContext):
    """С карточки исполнителя — назад к результатам поиска."""
    data = await state.get_data()
    if not data.get("art_s_results"):
        await artists_feed(cb, state)  # ответит на callback сам
        return
    await cb.answer()
    await _render_art_search(cb.bot, cb.message.chat.id, state,
                             data.get("art_s_offset") or 0)
