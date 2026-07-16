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
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlmodel import select

from app.database import SessionLocal
from app.models import Artist, Listing, ReleaseMeta
from app.analytics import log_event
from app.features import is_enabled
from app.routers.releases import (
    RELEASE_TYPES,
    _ask_artname,
    _menu_btn,
    _send_screen,
)
from app.routers.utils import clear_bot_messages

router = Router(name="artists")

PAGE = 8


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

    lines = [f"🎤 <b>{artist.name}</b>", artist.artist_type]
    if artist.status != "active":
        lines.insert(0, "🚫 <i>Карточка скрыта</i>")
    if releases:
        lines.append("")
        lines.append(f"Релизов: {len(releases)}")
    else:
        lines.append("")
        lines.append("Релизов пока нет.")
    caption = "\n".join(lines)

    rows = [[InlineKeyboardButton(
        text=f"🎵 {l.title} ({RELEASE_TYPES.get(m.release_type, '')})",
        callback_data=f"rel:view:{l.id}")] for l, m in releases]
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
