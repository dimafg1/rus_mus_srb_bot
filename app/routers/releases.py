# app/routers/releases.py
"""
🎵 Релизы — музыкальный слой бота (журнал решений Р-11/Р-12).

Релиз = listing(type='release') + release_meta (свой жизненный цикл:
published/hidden/deleted, без 30-дневных сроков) + release_track (треки).
Исполнитель (artist) — отдельная сущность, выбирается/создаётся в мастере.

Правила раздела:
- публикация сразу, админам уведомление с кнопкой «Скрыть»;
- хотя бы одна ссылка на площадку ИЛИ прикреплённый файл;
- YouTube-ссылка кладётся в текст карточки — играет во встроенном
  просмотре Telegram; остальные площадки — URL-кнопками;
- альбом: треки по одному (release_track), отправка по выбору из трек-листа;
- «Пожаловаться» на каждой карточке;
- весь раздел за выключателем releases_enabled;
- железное правило чата: каждый экран убирает предыдущие сообщения
  (clear_bot_messages) и регистрирует новые (register_bot_messages).
"""
import json

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlmodel import select

from app.database import SessionLocal
from app.models import Artist, Category, Listing, ReleaseMeta, ReleaseTrack, utcnow_naive
from app.analytics import log_event
from app.analytics.listing_views import log_listing_view
from app.features import is_enabled
from app.routers.utils import (
    clear_bot_messages,
    register_bot_messages,
    last_bot_messages,
)

router = Router(name="releases")

RELEASES_CITY_ID = 999        # техническая привязка (город в релизах не показывается)
RELEASES_CATEGORY_SLUG = "releases"
PAGE = 8

RELEASE_TYPES = {
    "single": "Сингл",
    "ep": "EP",
    "album": "Альбом",
    "clip": "Клип",
    "live": "Live",
}
ARTIST_TYPES = ["Сольный исполнитель", "Группа", "Дуэт", "Проект", "DJ", "Другое"]

LINK_LABELS = {
    "youtube.com": "YouTube", "youtu.be": "YouTube",
    "spotify.com": "Spotify",
    "music.yandex": "Яндекс Музыка",
    "music.apple": "Apple Music",
    "bandcamp.com": "Bandcamp",
    "soundcloud.com": "SoundCloud",
    "vk.com": "VK Музыка", "vk.ru": "VK Музыка",
}


class ReleaseAdd(StatesGroup):
    artist_name = State()
    artist_photo = State()
    rel_title = State()
    cover = State()
    media = State()
    links = State()
    descr = State()


# ─────────────────────────── helpers ───────────────────────────

def _menu_btn() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="☰ Главное меню", callback_data="main_menu")


async def _send_screen(bot, chat_id: int, text: str, kb=None, photo=None):
    """Экран раздела: чистим предыдущие сообщения, шлём новое, регистрируем."""
    await clear_bot_messages(chat_id, bot)
    if photo:
        msg = await bot.send_photo(chat_id, photo, caption=text, reply_markup=kb, parse_mode="HTML")
    else:
        msg = await bot.send_message(
            chat_id, text, reply_markup=kb, parse_mode="HTML",
            disable_web_page_preview=False,
        )
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    return msg


async def _replace_prompt(state: FSMContext, bot, chat_id: int, text: str, kb=None):
    """Шаг мастера: убираем предыдущую подсказку, шлём новую, регистрируем."""
    data = await state.get_data()
    old = data.get("rel_prompt_id")
    if old:
        try:
            await bot.delete_message(chat_id, old)
        except Exception:
            pass
    msg = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    await state.update_data(rel_prompt_id=msg.message_id)
    return msg


def _link_label(url: str) -> str:
    u = url.lower()
    for dom, label in LINK_LABELS.items():
        if dom in u:
            return label
    return "Слушать"


def _youtube_url(links: list[dict]) -> str | None:
    for l in links:
        if l.get("label") == "YouTube":
            return l["url"]
    return None


async def _admin_ids() -> list[int]:
    try:
        from app.routers.admin_panel import ADMIN_IDS
        return list(ADMIN_IDS)
    except Exception:
        return []


async def _ensure_release_category() -> int:
    async with SessionLocal() as s:
        cat = (await s.execute(
            select(Category).where(Category.slug == RELEASES_CATEGORY_SLUG)
        )).scalars().first()
        if cat:
            return cat.id
        cat = Category(slug=RELEASES_CATEGORY_SLUG, name="Релизы", parent_id=None)
        s.add(cat)
        await s.commit()
        await s.refresh(cat)
        return cat.id


async def _load_release(listing_id: int):
    """→ (listing, meta, artist, tracks) либо (None, None, None, [])."""
    async with SessionLocal() as s:
        listing = (await s.execute(
            select(Listing).where(Listing.id == listing_id)
        )).scalar_one_or_none()
        if not listing:
            return None, None, None, []
        meta = (await s.execute(
            select(ReleaseMeta).where(ReleaseMeta.listing_id == listing_id)
        )).scalar_one_or_none()
        artist = None
        if meta:
            artist = (await s.execute(
                select(Artist).where(Artist.id == meta.artist_id)
            )).scalar_one_or_none()
        tracks = (await s.execute(
            select(ReleaseTrack).where(ReleaseTrack.listing_id == listing_id)
            .order_by(ReleaseTrack.position)
        )).scalars().all()
    return listing, meta, artist, tracks


def _release_caption(listing, meta, artist, tracks) -> str:
    a_name = artist.name if artist else "Неизвестный исполнитель"
    t_label = RELEASE_TYPES.get(meta.release_type, meta.release_type) if meta else ""
    lines = [f"🎵 <b>{a_name} — «{listing.title}»</b>"]
    sub = [t_label]
    if meta and meta.release_date:
        sub.append(meta.release_date)
    if meta and meta.genre:
        sub.append(meta.genre)
    lines.append(" · ".join(x for x in sub if x))
    if meta and meta.recorded_at:
        lines.append(f"🎙 Записано: {meta.recorded_at}")
    if listing.descr:
        lines.append("")
        lines.append(listing.descr)
    if tracks:
        lines.append("")
        lines.append("<b>Трек-лист:</b>")
        for t in tracks:
            lines.append(f"{t.position}. {t.title or 'Трек ' + str(t.position)}")
    links = json.loads(meta.links) if meta and meta.links else []
    yt = _youtube_url(links)
    if yt:
        lines.append("")
        lines.append(f'▶️ <a href="{yt}">Смотреть на YouTube</a>')
    caption = "\n".join(lines)
    return caption[:1020] + "…" if len(caption) > 1024 else caption


def _release_kb(listing, meta, tracks, *, viewer_id: int, is_admin_user: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if tracks:
        rows.append([InlineKeyboardButton(
            text="▶️ Слушать в Telegram", callback_data=f"rel:listen:{listing.id}")])
    links = json.loads(meta.links) if meta and meta.links else []
    row: list[InlineKeyboardButton] = []
    for l in links:
        row.append(InlineKeyboardButton(text=f"🎧 {l['label']}", url=l["url"]))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    ctl: list[InlineKeyboardButton] = []
    if viewer_id == listing.owner_id:
        ctl.append(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"rel:del:{listing.id}"))
    if is_admin_user and meta and meta.status == "published":
        ctl.append(InlineKeyboardButton(text="🚫 Скрыть", callback_data=f"rel:admhide:{listing.id}"))
    if ctl:
        rows.append(ctl)
    rows.append([InlineKeyboardButton(text="⚠️ Пожаловаться", callback_data=f"rel:report:{listing.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ К релизам", callback_data="go_releases"), _menu_btn()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─────────────────────────── лента ───────────────────────────

@router.callback_query(F.data == "go_releases")
@router.callback_query(F.data.startswith("rel:list:"))
async def releases_feed(cb: CallbackQuery, state: FSMContext):
    if not await is_enabled("releases_enabled", user_id=cb.from_user.id):
        await cb.answer("Раздел временно недоступен.", show_alert=True)
        return
    await state.clear()
    offset = 0
    if cb.data.startswith("rel:list:"):
        try:
            offset = max(0, int(cb.data.split(":")[2]))
        except ValueError:
            offset = 0

    async with SessionLocal() as s:
        metas = (await s.execute(
            select(ReleaseMeta).where(ReleaseMeta.status == "published")
            .order_by(ReleaseMeta.created_at.desc())
        )).scalars().all()
        total = len(metas)
        page = metas[offset:offset + PAGE]
        rows: list[list[InlineKeyboardButton]] = []
        for m in page:
            listing = (await s.execute(
                select(Listing).where(Listing.id == m.listing_id)
            )).scalar_one_or_none()
            artist = (await s.execute(
                select(Artist).where(Artist.id == m.artist_id)
            )).scalar_one_or_none()
            if not listing:
                continue
            a_name = artist.name if artist else "?"
            t_label = RELEASE_TYPES.get(m.release_type, "")
            rows.append([InlineKeyboardButton(
                text=f"🎵 {a_name} — {listing.title} ({t_label})",
                callback_data=f"rel:view:{listing.id}")])

    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"rel:list:{max(0, offset - PAGE)}"))
    if offset + PAGE < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"rel:list:{offset + PAGE}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="➕ Добавить релиз", callback_data="rel:add")])
    rows.append([InlineKeyboardButton(text="💿 Мои релизы", callback_data="rel:my"), _menu_btn()])

    text = "🎵 <b>Релизы сообщества</b>\n\nНовая музыка наших исполнителей: синглы, альбомы, клипы."
    if total == 0:
        text += "\n\nРелизов пока нет — станьте первым!"
    await _send_screen(cb.bot, cb.message.chat.id, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


# ─────────────────────────── карточка ───────────────────────────

@router.callback_query(F.data.startswith("rel:view:"))
async def release_view(cb: CallbackQuery):
    listing_id = int(cb.data.split(":")[2])
    listing, meta, artist, tracks = await _load_release(listing_id)
    if not listing or not meta or meta.status != "published":
        # владельцу и админу показываем и скрытые
        from app.routers.admin_panel import is_admin
        if not (listing and meta and (cb.from_user.id == listing.owner_id or is_admin(cb.from_user.id))):
            await cb.answer("Релиз недоступен.", show_alert=True)
            return

    from app.routers.admin_panel import is_admin
    caption = _release_caption(listing, meta, artist, tracks)
    if meta.status != "published":
        caption = "🚫 <i>Релиз скрыт</i>\n\n" + caption
    kb = _release_kb(listing, meta, tracks,
                     viewer_id=cb.from_user.id, is_admin_user=is_admin(cb.from_user.id))
    await _send_screen(cb.bot, cb.message.chat.id, caption, kb, photo=listing.photo_file_id)
    await log_listing_view(listing_id=listing.id, user_id=cb.from_user.id,
                           section="releases", action="open", source="catalog")
    await cb.answer()


@router.callback_query(F.data.startswith("rel:listen:"))
async def release_listen(cb: CallbackQuery):
    listing_id = int(cb.data.split(":")[2])
    _, _, _, tracks = await _load_release(listing_id)
    if not tracks:
        await cb.answer("Треки не найдены.", show_alert=True)
        return
    rows = [[InlineKeyboardButton(
        text=f"{t.position}. {t.title or 'Трек ' + str(t.position)}",
        callback_data=f"rel:track:{t.id}")] for t in tracks]
    rows.append([InlineKeyboardButton(text="⬅️ К релизу", callback_data=f"rel:view:{listing_id}")])
    # трек-лист отдельным сообщением ПОД карточкой (карточку не сносим)
    msg = await cb.bot.send_message(
        cb.message.chat.id, "Выберите трек:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
    await cb.answer()


@router.callback_query(F.data.startswith("rel:track:"))
async def release_track_play(cb: CallbackQuery):
    track_id = int(cb.data.split(":")[2])
    async with SessionLocal() as s:
        t = (await s.execute(
            select(ReleaseTrack).where(ReleaseTrack.id == track_id)
        )).scalar_one_or_none()
    if not t:
        await cb.answer("Трек не найден.", show_alert=True)
        return
    try:
        msg = await cb.bot.send_audio(cb.message.chat.id, t.file_id)
        last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
        await register_bot_messages(cb.message.chat.id, [msg.message_id])
        await log_listing_view(listing_id=t.listing_id, user_id=cb.from_user.id,
                               section="releases", action="open", source="track")
        await cb.answer()
    except Exception as e:
        print(f"[releases] send_audio failed: {e}")
        await cb.answer("Не удалось отправить трек.", show_alert=True)


# ─────────────────────────── жалоба и модерация ───────────────────────────

@router.callback_query(F.data.startswith("rel:report:"))
async def release_report(cb: CallbackQuery):
    listing_id = int(cb.data.split(":")[2])
    listing, meta, artist, _ = await _load_release(listing_id)
    for admin_id in await _admin_ids():
        try:
            await cb.bot.send_message(
                admin_id,
                f"⚠️ Жалоба на релиз #{listing_id} "
                f"({artist.name if artist else '?'} — {listing.title if listing else '?'})\n"
                f"От: {cb.from_user.id} (@{cb.from_user.username or '—'})",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="👀 Открыть", callback_data=f"rel:view:{listing_id}"),
                    InlineKeyboardButton(text="🚫 Скрыть", callback_data=f"rel:admhide:{listing_id}"),
                ]]),
            )
        except Exception as e:
            print(f"[releases] report notify {admin_id}: {e}")
    await cb.answer("Жалоба отправлена. Спасибо!", show_alert=True)


@router.callback_query(F.data.startswith("rel:admhide:"))
async def release_admin_hide(cb: CallbackQuery):
    from app.routers.admin_panel import is_admin
    if not is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    listing_id = int(cb.data.split(":")[2])
    async with SessionLocal() as s:
        meta = (await s.execute(
            select(ReleaseMeta).where(ReleaseMeta.listing_id == listing_id)
        )).scalar_one_or_none()
        if not meta:
            await cb.answer("Не найден.", show_alert=True)
            return
        meta.status = "hidden"
        s.add(meta)
        await s.commit()
    await cb.answer("Релиз скрыт.", show_alert=True)


# ─────────────────────────── мои релизы ───────────────────────────

@router.callback_query(F.data == "rel:my")
async def my_releases(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    async with SessionLocal() as s:
        listings = (await s.execute(
            select(Listing).where(Listing.owner_id == cb.from_user.id, Listing.type == "release")
            .order_by(Listing.created_at.desc())
        )).scalars().all()
        rows = []
        for l in listings:
            meta = (await s.execute(
                select(ReleaseMeta).where(ReleaseMeta.listing_id == l.id)
            )).scalar_one_or_none()
            if not meta or meta.status == "deleted":
                continue
            mark = "" if meta.status == "published" else " (скрыт)"
            rows.append([InlineKeyboardButton(
                text=f"🎵 {l.title}{mark}", callback_data=f"rel:view:{l.id}")])
    rows.append([InlineKeyboardButton(text="➕ Добавить релиз", callback_data="rel:add")])
    rows.append([InlineKeyboardButton(text="⬅️ К релизам", callback_data="go_releases"), _menu_btn()])
    text = "💿 <b>Мои релизы</b>" + ("" if len(rows) > 2 else "\n\nУ вас пока нет релизов.")
    await _send_screen(cb.bot, cb.message.chat.id, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data.startswith("rel:del:"))
async def release_delete_ask(cb: CallbackQuery):
    listing_id = int(cb.data.split(":")[2])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"rel:delok:{listing_id}"),
        InlineKeyboardButton(text="✖ Отмена", callback_data=f"rel:view:{listing_id}"),
    ]])
    await _send_screen(cb.bot, cb.message.chat.id, "Удалить релиз безвозвратно?", kb)
    await cb.answer()


@router.callback_query(F.data.startswith("rel:delok:"))
async def release_delete(cb: CallbackQuery):
    listing_id = int(cb.data.split(":")[2])
    async with SessionLocal() as s:
        listing = (await s.execute(
            select(Listing).where(Listing.id == listing_id)
        )).scalar_one_or_none()
        from app.routers.admin_panel import is_admin
        if not listing or (listing.owner_id != cb.from_user.id and not is_admin(cb.from_user.id)):
            await cb.answer("Удалить может только автор.", show_alert=True)
            return
        meta = (await s.execute(
            select(ReleaseMeta).where(ReleaseMeta.listing_id == listing_id)
        )).scalar_one_or_none()
        if meta:
            meta.status = "deleted"
            s.add(meta)
            await s.commit()
    await cb.answer("Релиз удалён.")
    await _send_screen(cb.bot, cb.message.chat.id, "Релиз удалён.",
                       InlineKeyboardMarkup(inline_keyboard=[
                           [InlineKeyboardButton(text="💿 Мои релизы", callback_data="rel:my")],
                           [InlineKeyboardButton(text="⬅️ К релизам", callback_data="go_releases"), _menu_btn()],
                       ]))


# ─────────────────────────── мастер добавления ───────────────────────────

@router.callback_query(F.data == "rel:add")
async def add_start(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    async with SessionLocal() as s:
        artists = (await s.execute(
            select(Artist).where(Artist.owner_user_id == cb.from_user.id,
                                 Artist.status == "active")
        )).scalars().all()
    rows = [[InlineKeyboardButton(text=f"🎤 {a.name}", callback_data=f"rel:art:{a.id}")]
            for a in artists]
    rows.append([InlineKeyboardButton(text="➕ Создать нового исполнителя", callback_data="rel:artnew")])
    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="go_releases")])
    await _send_screen(cb.bot, cb.message.chat.id,
                       "Чей это релиз?\n\nВыберите вашего исполнителя или создайте нового.",
                       InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data.startswith("rel:art:"))
async def add_pick_artist(cb: CallbackQuery, state: FSMContext):
    artist_id = int(cb.data.split(":")[2])
    await state.update_data(artist_id=artist_id, new_artist=None)
    await _ask_rel_type(cb, state)


@router.callback_query(F.data == "rel:artnew")
async def add_new_artist(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ReleaseAdd.artist_name)
    await _replace_prompt(state, cb.bot, cb.message.chat.id,
                          "Название исполнителя или группы?")
    await cb.answer()


@router.message(ReleaseAdd.artist_name, F.text)
async def artist_name_input(message: Message, state: FSMContext):
    name = message.text.strip()[:128]
    try:
        await message.delete()
    except Exception:
        pass
    if not name:
        return
    await state.update_data(new_artist={"name": name})
    rows = [[InlineKeyboardButton(text=t, callback_data=f"rel:atype:{i}")]
            for i, t in enumerate(ARTIST_TYPES)]
    await state.set_state(None)
    await _replace_prompt(state, message.bot, message.chat.id,
                          f"«{name}» — это:", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("rel:atype:"))
async def artist_type_pick(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":")[2])
    data = await state.get_data()
    new_artist = data.get("new_artist") or {}
    new_artist["type"] = ARTIST_TYPES[idx] if 0 <= idx < len(ARTIST_TYPES) else "Другое"
    await state.update_data(new_artist=new_artist)
    await state.set_state(ReleaseAdd.artist_photo)
    await _replace_prompt(state, cb.bot, cb.message.chat.id,
                          "Фото или логотип исполнителя?\n\nПришлите картинку — или пропустите.",
                          InlineKeyboardMarkup(inline_keyboard=[[
                              InlineKeyboardButton(text="⏭ Пропустить", callback_data="rel:askip")]]))
    await cb.answer()


@router.message(ReleaseAdd.artist_photo, F.photo)
async def artist_photo_input(message: Message, state: FSMContext):
    data = await state.get_data()
    new_artist = data.get("new_artist") or {}
    new_artist["photo"] = message.photo[-1].file_id
    await state.update_data(new_artist=new_artist)
    try:
        await message.delete()
    except Exception:
        pass
    await _ask_rel_type(message, state)


@router.callback_query(F.data == "rel:askip")
async def artist_photo_skip(cb: CallbackQuery, state: FSMContext):
    await _ask_rel_type(cb, state)


async def _ask_rel_type(event, state: FSMContext):
    await state.set_state(None)
    bot = event.bot
    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
    rows = [[InlineKeyboardButton(text=label, callback_data=f"rel:rtype:{code}")]
            for code, label in RELEASE_TYPES.items()]
    await _replace_prompt(state, bot, chat_id, "Что выпускаем?",
                          InlineKeyboardMarkup(inline_keyboard=rows))
    if isinstance(event, CallbackQuery):
        try:
            await event.answer()
        except Exception:
            pass


@router.callback_query(F.data.startswith("rel:rtype:"))
async def rel_type_pick(cb: CallbackQuery, state: FSMContext):
    code = cb.data.split(":")[2]
    if code not in RELEASE_TYPES:
        await cb.answer()
        return
    await state.update_data(rel_type=code)
    await state.set_state(ReleaseAdd.rel_title)
    await _replace_prompt(state, cb.bot, cb.message.chat.id,
                          "Название релиза?\n\n(без имени исполнителя — только название)")
    await cb.answer()


@router.message(ReleaseAdd.rel_title, F.text)
async def rel_title_input(message: Message, state: FSMContext):
    title = message.text.strip()[:200]
    try:
        await message.delete()
    except Exception:
        pass
    if not title:
        return
    await state.update_data(title=title)
    await state.set_state(ReleaseAdd.cover)
    await _replace_prompt(state, message.bot, message.chat.id,
                          "Обложка релиза?\n\nПришлите картинку — обложка обязательна.")


@router.message(ReleaseAdd.cover, F.photo)
async def cover_input(message: Message, state: FSMContext):
    await state.update_data(cover=message.photo[-1].file_id)
    try:
        await message.delete()
    except Exception:
        pass
    await state.set_state(ReleaseAdd.media)
    await state.update_data(tracks=[], video=None)
    await _replace_prompt(
        state, message.bot, message.chat.id,
        "Теперь сам релиз — можно прямо в Telegram:\n\n"
        "🎧 пришлите аудио-треки по одному (в порядке альбома)\n"
        "🎬 или один видеоклип\n\n"
        "Большой клип удобнее дать ссылкой на YouTube на следующем шаге.\n"
        "Когда закончите — нажмите «Готово».",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Готово / Пропустить", callback_data="rel:mdone")]]))


@router.message(ReleaseAdd.media, F.audio)
async def media_audio(message: Message, state: FSMContext):
    data = await state.get_data()
    tracks = data.get("tracks") or []
    a = message.audio
    tracks.append({
        "file_id": a.file_id,
        "file_unique_id": a.file_unique_id,
        "title": (a.title or a.file_name or f"Трек {len(tracks) + 1}")[:255],
        "duration": a.duration,
        "file_name": a.file_name,
        "mime_type": a.mime_type,
    })
    await state.update_data(tracks=tracks)
    try:
        await message.delete()
    except Exception:
        pass
    await _replace_prompt(
        state, message.bot, message.chat.id,
        f"Принято треков: {len(tracks)} ✔️\n\nПрисылайте следующий или жмите «Готово».",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Готово", callback_data="rel:mdone")]]))


@router.message(ReleaseAdd.media, F.video)
async def media_video(message: Message, state: FSMContext):
    v = message.video
    await state.update_data(video={"file_id": v.file_id, "file_unique_id": v.file_unique_id})
    try:
        await message.delete()
    except Exception:
        pass
    await _replace_prompt(
        state, message.bot, message.chat.id,
        "Клип принят ✔️\n\nЖмите «Готово».",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Готово", callback_data="rel:mdone")]]))


@router.callback_query(F.data == "rel:mdone")
async def media_done(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ReleaseAdd.links)
    await _replace_prompt(
        state, cb.bot, cb.message.chat.id,
        "Ссылки на площадки?\n\nПришлите одним сообщением — каждая ссылка с новой "
        "строки или через пробел (YouTube, Spotify, Яндекс, Bandcamp…).",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⏭ Без ссылок", callback_data="rel:lskip")]]))
    await cb.answer()


@router.message(ReleaseAdd.links, F.text)
async def links_input(message: Message, state: FSMContext):
    raw = message.text.replace(",", " ").split()
    links = [{"label": _link_label(u), "url": u} for u in raw if u.startswith("http")]
    try:
        await message.delete()
    except Exception:
        pass
    if not links:
        await _replace_prompt(state, message.bot, message.chat.id,
                              "Не увидел ссылок (нужны адреса, начинающиеся с http…). "
                              "Попробуйте ещё раз или пропустите.",
                              InlineKeyboardMarkup(inline_keyboard=[[
                                  InlineKeyboardButton(text="⏭ Без ссылок", callback_data="rel:lskip")]]))
        return
    await state.update_data(links=links)
    await _ask_descr(message, state)


@router.callback_query(F.data == "rel:lskip")
async def links_skip(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not (data.get("tracks") or data.get("video")):
        await cb.answer("Нужна хотя бы одна ссылка ИЛИ файл — иначе слушать нечего 🙂",
                        show_alert=True)
        return
    await state.update_data(links=[])
    await _ask_descr(cb, state)


async def _ask_descr(event, state: FSMContext):
    await state.set_state(ReleaseAdd.descr)
    bot = event.bot
    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
    await _replace_prompt(state, bot, chat_id,
                          "Пара слов о релизе? (по желанию)",
                          InlineKeyboardMarkup(inline_keyboard=[[
                              InlineKeyboardButton(text="⏭ Пропустить", callback_data="rel:dskip")]]))
    if isinstance(event, CallbackQuery):
        try:
            await event.answer()
        except Exception:
            pass


@router.message(ReleaseAdd.descr, F.text)
async def descr_input(message: Message, state: FSMContext):
    await state.update_data(descr=message.text.strip()[:600])
    try:
        await message.delete()
    except Exception:
        pass
    await _confirm(message, state)


@router.callback_query(F.data == "rel:dskip")
async def descr_skip(cb: CallbackQuery, state: FSMContext):
    await state.update_data(descr=None)
    await _confirm(cb, state)


async def _confirm(event, state: FSMContext):
    await state.set_state(None)
    data = await state.get_data()
    bot = event.bot
    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
    new_artist = data.get("new_artist")
    if new_artist:
        artist_line = f"{new_artist['name']} ({new_artist.get('type', '—')}) — новый"
    else:
        async with SessionLocal() as s:
            a = (await s.execute(
                select(Artist).where(Artist.id == data.get("artist_id"))
            )).scalar_one_or_none()
        artist_line = a.name if a else "?"
    parts = [
        "<b>Проверьте:</b>",
        f"Исполнитель: {artist_line}",
        f"Тип: {RELEASE_TYPES.get(data.get('rel_type'), '?')}",
        f"Название: {data.get('title')}",
        f"Треков: {len(data.get('tracks') or [])}" + (" + клип" if data.get("video") else ""),
        f"Ссылок: {len(data.get('links') or [])}",
    ]
    if data.get("descr"):
        parts.append(f"Описание: {data['descr'][:100]}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Публикую — я автор или представитель",
                              callback_data="rel:pub")],
        [InlineKeyboardButton(text="✖ Отмена", callback_data="go_releases")],
    ])
    await _replace_prompt(state, bot, chat_id, "\n".join(parts), kb)
    if isinstance(event, CallbackQuery):
        try:
            await event.answer()
        except Exception:
            pass


@router.callback_query(F.data == "rel:pub")
async def publish(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("title") or not data.get("cover"):
        await cb.answer("Не хватает данных — начните заново.", show_alert=True)
        return
    cat_id = await _ensure_release_category()
    async with SessionLocal() as s:
        artist_id = data.get("artist_id")
        if data.get("new_artist"):
            na = data["new_artist"]
            artist = Artist(name=na["name"], artist_type=na.get("type", "Другое"),
                            photo_file_id=na.get("photo"), owner_user_id=cb.from_user.id)
            s.add(artist)
            await s.flush()
            artist_id = artist.id
        listing = Listing(
            city_id=RELEASES_CITY_ID, category_id=cat_id, owner_id=cb.from_user.id,
            title=data["title"],
            descr=data.get("descr"),
            contact=(f"@{cb.from_user.username}" if cb.from_user.username else "—"),
            photo_file_id=data["cover"], created_at=utcnow_naive(), type="release",
        )
        s.add(listing)
        await s.flush()
        video = data.get("video") or {}
        meta = ReleaseMeta(
            listing_id=listing.id, artist_id=artist_id,
            release_type=data.get("rel_type", "single"),
            release_date=utcnow_naive().strftime("%d.%m.%Y"),
            links=json.dumps(data.get("links") or [], ensure_ascii=False),
            video_file_id=video.get("file_id"),
            video_file_unique_id=video.get("file_unique_id"),
        )
        s.add(meta)
        for i, t in enumerate(data.get("tracks") or [], start=1):
            s.add(ReleaseTrack(
                listing_id=listing.id, position=i, title=t.get("title"),
                file_id=t["file_id"], file_unique_id=t.get("file_unique_id"),
                duration=t.get("duration"), file_name=t.get("file_name"),
                mime_type=t.get("mime_type"),
            ))
        await s.commit()
        listing_id = listing.id

    await state.clear()
    await log_event("listing_created", user_id=cb.from_user.id,
                    section="releases", entity_type="listing", entity_id=listing_id)

    # уведомление админам с кнопкой «Скрыть» (модерация задним числом)
    for admin_id in await _admin_ids():
        if admin_id == cb.from_user.id:
            continue
        try:
            await cb.bot.send_message(
                admin_id, f"🆕 Новый релиз #{listing_id}: {data['title']}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="👀 Открыть", callback_data=f"rel:view:{listing_id}"),
                    InlineKeyboardButton(text="🚫 Скрыть", callback_data=f"rel:admhide:{listing_id}"),
                ]]))
        except Exception as e:
            print(f"[releases] admin notify {admin_id}: {e}")

    # показываем готовую карточку
    listing, meta, artist, tracks = await _load_release(listing_id)
    from app.routers.admin_panel import is_admin
    caption = "🎉 Опубликовано!\n\n" + _release_caption(listing, meta, artist, tracks)
    kb = _release_kb(listing, meta, tracks,
                     viewer_id=cb.from_user.id, is_admin_user=is_admin(cb.from_user.id))
    await _send_screen(cb.bot, cb.message.chat.id, caption[:1024], kb, photo=listing.photo_file_id)
    await cb.answer("Релиз опубликован!")
