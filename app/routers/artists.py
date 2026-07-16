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
from app.models import Artist, Listing, ReleaseMeta
from app.analytics import log_event
from app.features import is_enabled
from app.routers.releases import (
    ARTIST_TYPES,
    RELEASE_TYPES,
    _ask_artname,
    _link_label,
    _menu_btn,
    _replace_prompt,
    _send_screen,
)
from app.routers.utils import clear_bot_messages

router = Router(name="artists")

PAGE = 8

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
    "contact": ("Контакт", "Контакт для связи (например: @username).\n"
                           "Он будет виден всем на карточке:"),
}
CLEARABLE = {"descr", "genres", "city_text", "links", "contact"}


class ArtistEdit(StatesGroup):
    value = State()


@router.callback_query(F.data == "go_artists")
@router.callback_query(F.data.startswith("art:list:"))
async def artists_feed(cb: CallbackQuery, state: FSMContext):
    if not await is_enabled("releases_enabled", user_id=cb.from_user.id):
        await cb.answer("Раздел временно недоступен.", show_alert=True)
        return
    await cb.answer()
    await state.clear()
    offset = 0
    if cb.data.startswith("art:list:"):
        try:
            offset = max(0, int(cb.data.split(":")[2]))
        except ValueError:
            offset = 0

    async with SessionLocal() as s:
        artists = (await s.execute(
            select(Artist).where(Artist.status == "active").order_by(Artist.name)
        )).scalars().all()
    total = len(artists)
    page = artists[offset:offset + PAGE]

    rows = [[InlineKeyboardButton(
        text=f"🎤 {a.name} · {a.artist_type}",
        callback_data=f"art:view:{a.id}:list")] for a in page]
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"art:list:{max(0, offset - PAGE)}"))
    if offset + PAGE < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"art:list:{offset + PAGE}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="➕ Добавить исполнителя", callback_data="art:add")])
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
    artist_id = int(parts[2])
    src = parts[3] if len(parts) > 3 else "list"

    from app.routers.admin_panel import is_admin
    async with SessionLocal() as s:
        artist = (await s.execute(
            select(Artist).where(Artist.id == artist_id)
        )).scalar_one_or_none()
        if not artist:
            await cb.answer("Исполнитель не найден.", show_alert=True)
            return
        if artist.status != "active" and not (
            is_admin(cb.from_user.id) or artist.owner_user_id == cb.from_user.id
        ):
            await cb.answer("Карточка недоступна.", show_alert=True)
            return
        metas = (await s.execute(
            select(ReleaseMeta).where(ReleaseMeta.artist_id == artist_id,
                                      ReleaseMeta.status == "published")
            .order_by(ReleaseMeta.created_at.desc())
        )).scalars().all()
        releases = []
        for m in metas:
            listing = (await s.execute(
                select(Listing).where(Listing.id == m.listing_id)
            )).scalar_one_or_none()
            if listing:
                releases.append((listing, m))

    sub = [artist.artist_type]
    if artist.genres:
        sub.append(artist.genres)
    if artist.city_text:
        sub.append(artist.city_text)
    lines = [f"🎤 <b>{artist.name}</b>", " · ".join(sub)]
    if artist.status != "active":
        lines.insert(0, "🚫 <i>Карточка скрыта</i>")
    if artist.descr:
        lines.append("")
        lines.append(artist.descr)
    if artist.contact:
        lines.append("")
        lines.append(f"✍️ Связаться: {artist.contact}")
    if releases:
        lines.append("")
        lines.append(f"Релизов: {len(releases)}")
    else:
        lines.append("")
        lines.append("Релизов пока нет.")
    caption = "\n".join(lines)
    caption = caption[:1020] + "…" if len(caption) > 1024 else caption

    rows = [[InlineKeyboardButton(
        text=f"🎵 {l.title} ({RELEASE_TYPES.get(m.release_type, '')})",
        callback_data=f"rel:view:{l.id}")] for l, m in releases]
    # ссылки соцсетей/площадок — кнопками парами
    link_row: list[InlineKeyboardButton] = []
    try:
        for l in (json.loads(artist.links) if artist.links else []):
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
    # «Назад» — ровно на один шаг: к списку или к релизу, откуда пришли
    back_cb = "go_artists" if src == "list" else f"rel:view:{src[3:]}"
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb), _menu_btn()])

    await _send_screen(cb.bot, cb.message.chat.id, caption,
                       InlineKeyboardMarkup(inline_keyboard=rows),
                       photo=artist.photo_file_id)
    await log_event("artist_opened", user_id=cb.from_user.id,
                    entity_type="artist", entity_id=artist.id, source=src[:16])
    await cb.answer()


# ─────────────────────── редактирование карточки ───────────────────────

async def _can_edit(user_id: int, artist: Artist) -> bool:
    from app.routers.admin_panel import is_admin
    return artist.owner_user_id == user_id or is_admin(user_id)


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
        n_links = len(json.loads(artist.links)) if artist.links else 0
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
    lines = [f"✏️ <b>Карточка: {artist.name}</b>", ""]
    for code, (label, _) in EDIT_FIELDS.items():
        lines.append(f"{label}: {values[code]}")
    rows = [[InlineKeyboardButton(text=f"✏️ Править: {label}",
                                  callback_data=f"art:ef:{code}:{artist_id}")]
            for code, (label, _) in EDIT_FIELDS.items()]
    rows.append([InlineKeyboardButton(text="⬅️ Назад",
                                      callback_data=f"art:view:{artist_id}:list"),
                 _menu_btn()])
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
    await cb.answer()
    _, _, field, aid = cb.data.split(":")
    artist_id = int(aid)
    if field not in EDIT_FIELDS:
        return
    if field == "type":  # тип — кнопками
        rows = [[InlineKeyboardButton(text=t, callback_data=f"art:etype:{artist_id}:{i}")]
                for i, t in enumerate(ARTIST_TYPES)]
        rows.append([InlineKeyboardButton(text="⬅️ Назад",
                                          callback_data=f"art:edit:{artist_id}"), _menu_btn()])
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
    rows.append([InlineKeyboardButton(text="⬅️ Назад",
                                      callback_data=f"art:edit:{artist_id}"), _menu_btn()])
    await _replace_prompt(state, cb.bot, cb.message.chat.id, hint,
                          InlineKeyboardMarkup(inline_keyboard=rows))


async def _save_artist_field(user_id: int, artist_id: int, field: str, value):
    async with SessionLocal() as s:
        artist = (await s.execute(
            select(Artist).where(Artist.id == artist_id)
        )).scalar_one_or_none()
        if not artist or not await _can_edit(user_id, artist):
            return False
        setattr(artist, {"type": "artist_type"}.get(field, field), value)
        s.add(artist)
        await s.commit()
    return True


@router.callback_query(F.data.startswith("art:etype:"))
async def artist_edit_type(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    _, _, aid, idx = cb.data.split(":")
    t = ARTIST_TYPES[int(idx)] if 0 <= int(idx) < len(ARTIST_TYPES) else "Другое"
    await _save_artist_field(cb.from_user.id, int(aid), "type", t)
    await _render_edit_overview(cb.bot, cb.message.chat.id, cb.from_user.id, int(aid))


@router.callback_query(F.data.startswith("art:eclr:"))
async def artist_edit_clear(cb: CallbackQuery, state: FSMContext):
    await cb.answer("Очищено.")
    _, _, aid, field = cb.data.split(":")
    if field in CLEARABLE:
        await _save_artist_field(cb.from_user.id, int(aid), field, None)
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
        raw = text_val.replace(",", " ").split()
        links = [{"label": _link_label(u), "url": u} for u in raw if u.startswith("http")]
        value = json.dumps(links, ensure_ascii=False) if links else None
    elif field == "name":
        value = text_val[:128]
        if not value:
            return
    else:
        limits = {"descr": 600, "genres": 128, "city_text": 64, "contact": 128}
        value = text_val[:limits.get(field, 255)] or None
    await _save_artist_field(message.from_user.id, artist_id, field, value)
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
    await _save_artist_field(message.from_user.id, artist_id, "photo_file_id",
                             message.photo[-1].file_id)
    await state.clear()
    await _render_edit_overview(message.bot, message.chat.id, message.from_user.id, artist_id)


@router.callback_query(F.data.startswith("art:hide:"))
async def artist_hide(cb: CallbackQuery):
    from app.routers.admin_panel import is_admin
    if not is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    artist_id = int(cb.data.split(":")[2])
    async with SessionLocal() as s:
        artist = (await s.execute(
            select(Artist).where(Artist.id == artist_id)
        )).scalar_one_or_none()
        if not artist:
            await cb.answer("Не найден.", show_alert=True)
            return
        artist.status = "hidden"
        s.add(artist)
        await s.commit()
    await cb.answer("Карточка скрыта.", show_alert=True)
