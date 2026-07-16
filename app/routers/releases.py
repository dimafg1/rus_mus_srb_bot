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
import urllib.parse

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo,
)
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


class ReleaseReport(StatesGroup):
    other_text = State()


# ─────────────────────────── helpers ───────────────────────────

def _menu_btn() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="☰ Главное меню", callback_data="main_menu")


def _nav_row(back_cb: str) -> list[InlineKeyboardButton]:
    """Железобетонное правило: на каждом экране «Назад» (один шаг) + меню."""
    return [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb), _menu_btn()]


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


async def _send_release_yt_button(bot, chat_id: int, video_url: str, listing_id: int):
    """TWA-кнопка «▶️ Смотреть видео» отдельным сообщением под карточкой —
    образец: app/routers/services_view.py::_send_yt_button. Открывает
    страницу-плеер внутри Telegram, без окна подтверждения."""
    try:
        from app.routers.services_view import WEBAPP_BASE
        if not video_url or not WEBAPP_BASE:
            return
        low = video_url.lower()
        if ("youtube.com" not in low) and ("youtu.be" not in low):
            return
        twa_url = (f"{WEBAPP_BASE}/media/video_yt.html"
                   f"?u={urllib.parse.quote(video_url, safe='')}&listing_id={listing_id}")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Смотреть видео", web_app=WebAppInfo(url=twa_url))]
        ])
        try:
            m = await bot.send_message(chat_id, " ", reply_markup=kb)
        except Exception:
            m = await bot.send_message(chat_id, "•", reply_markup=kb)
        last_bot_messages.setdefault(chat_id, []).append(m.message_id)
        await register_bot_messages(chat_id, [m.message_id])
    except Exception as e:
        print(f"[releases] _send_release_yt_button: {e}")


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
    # YouTube в тексте не показываем: как в Услугах, под карточкой идёт
    # отдельная TWA-кнопка «▶️ Смотреть видео» (см. _send_release_yt_button)
    caption = "\n".join(lines)
    return caption[:1020] + "…" if len(caption) > 1024 else caption


def _release_kb(listing, meta, tracks, *, viewer_id: int, is_admin_user: bool,
                artist=None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if tracks:
        rows.append([InlineKeyboardButton(
            text="▶️ Слушать в Telegram", callback_data=f"rel:listen:{listing.id}")])
    if artist is not None:
        rows.append([InlineKeyboardButton(
            text="🎤 Об исполнителе",
            callback_data=f"art:view:{artist.id}:rel{listing.id}")])
    links = json.loads(meta.links) if meta and meta.links else []
    row: list[InlineKeyboardButton] = []
    for l in links:
        if l.get("label") == "YouTube":
            continue  # YouTube живёт голой ссылкой в тексте — открывается без подтверждений
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
    kb = _release_kb(listing, meta, tracks, artist=artist,
                     viewer_id=cb.from_user.id, is_admin_user=is_admin(cb.from_user.id))
    await _send_screen(cb.bot, cb.message.chat.id, caption, kb, photo=listing.photo_file_id)
    links = json.loads(meta.links) if meta and meta.links else []
    yt = _youtube_url(links)
    if yt:
        await _send_release_yt_button(cb.bot, cb.message.chat.id, yt, listing.id)
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

REPORT_REASONS = {
    "rights": "Чужой материал (нарушение прав)",
    "spam": "Спам или реклама",
    "offtopic": "Не по теме раздела",
    "dup": "Дубликат",
    "other": "Другое",
}


@router.callback_query(F.data.startswith("rel:report:"))
async def release_report_ask(cb: CallbackQuery):
    """Шаг 1 жалобы: выбор причины (отдельным сообщением под карточкой)."""
    listing_id = int(cb.data.split(":")[2])
    rows = [[InlineKeyboardButton(text=label, callback_data=f"rel:repdo:{listing_id}:{code}")]
            for code, label in REPORT_REASONS.items()]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="rel:repcancel"), _menu_btn()])
    msg = await cb.bot.send_message(
        cb.message.chat.id, "Что не так с этим релизом?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
    await cb.answer()


@router.callback_query(F.data == "rel:repcancel")
async def release_report_cancel(cb: CallbackQuery):
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer("Отменено.")


@router.callback_query(F.data.startswith("rel:repdo:"))
async def release_report_send(cb: CallbackQuery, state: FSMContext):
    """Шаг 2: жалоба с причиной уходит админам. «Другое» — просим описать."""
    _, _, lid_raw, reason = cb.data.split(":", 3)
    listing_id = int(lid_raw)

    if reason == "other":
        await cb.answer()
        await state.set_state(ReleaseReport.other_text)
        await state.update_data(report_listing_id=listing_id)
        try:
            await cb.message.edit_text(
                "Опишите своими словами, что не так:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"rel:repback:{listing_id}"),
                     _menu_btn()]]))
        except Exception:
            pass
        return


@router.callback_query(F.data.startswith("rel:repback:"))
async def release_report_back(cb: CallbackQuery, state: FSMContext):
    """«Назад» из «Другое» — возвращаем список причин на том же сообщении."""
    await cb.answer()
    await state.clear()
    listing_id = int(cb.data.split(":")[2])
    rows = [[InlineKeyboardButton(text=label, callback_data=f"rel:repdo:{listing_id}:{code}")]
            for code, label in REPORT_REASONS.items()]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="rel:repcancel"), _menu_btn()])
    try:
        await cb.message.edit_text("Что не так с этим релизом?",
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        pass

    reason_label = REPORT_REASONS.get(reason, "Другое")
    listing, meta, artist, _ = await _load_release(listing_id)
    try:
        await cb.message.delete()  # убираем сообщение с причинами
    except Exception:
        pass
    await _notify_report(cb.bot, cb.from_user, listing_id, listing, artist, reason_label)
    await cb.answer("Жалоба отправлена. Спасибо!", show_alert=True)


@router.message(ReleaseReport.other_text, F.text)
async def release_report_other(message: Message, state: FSMContext):
    data = await state.get_data()
    listing_id = data.get("report_listing_id")
    text = message.text.strip()[:400]
    try:
        await message.delete()
    except Exception:
        pass
    await state.clear()
    if not listing_id:
        return
    listing, meta, artist, _ = await _load_release(listing_id)
    await _notify_report(message.bot, message.from_user, listing_id, listing, artist,
                         f"Другое: {text}")
    msg = await message.bot.send_message(
        message.chat.id, "Жалоба отправлена. Спасибо!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К релизу", callback_data=f"rel:view:{listing_id}"),
             _menu_btn()]]))
    last_bot_messages.setdefault(message.chat.id, []).append(msg.message_id)
    await register_bot_messages(message.chat.id, [msg.message_id])


async def _notify_report(bot, from_user, listing_id, listing, artist, reason_label):
    for admin_id in await _admin_ids():
        try:
            msg = await bot.send_message(
                admin_id,
                f"⚠️ Жалоба на релиз #{listing_id} "
                f"({artist.name if artist else '?'} — {listing.title if listing else '?'})\n"
                f"Причина: {reason_label}\n"
                f"От: {from_user.id} (@{from_user.username or '—'})",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="👀 Открыть", callback_data=f"rel:view:{listing_id}"),
                    InlineKeyboardButton(text="🚫 Скрыть", callback_data=f"rel:admhide:{listing_id}"),
                ]]),
            )
            # уведомление тоже подчиняется железному правилу очистки чата
            last_bot_messages.setdefault(admin_id, []).append(msg.message_id)
            await register_bot_messages(admin_id, [msg.message_id])
        except Exception as e:
            print(f"[releases] report notify {admin_id}: {e}")


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
    await cb.answer()
    await state.clear()
    async with SessionLocal() as s:
        artists = (await s.execute(
            select(Artist).where(Artist.owner_user_id == cb.from_user.id,
                                 Artist.status == "active")
        )).scalars().all()
    rows = [[InlineKeyboardButton(text=f"🎤 {a.name}", callback_data=f"rel:art:{a.id}")]
            for a in artists]
    rows.append([InlineKeyboardButton(text="➕ Создать нового исполнителя", callback_data="rel:artnew")])
    rows.append(_nav_row("go_releases"))
    await _send_screen(cb.bot, cb.message.chat.id,
                       "Чей это релиз?\n\nВыберите вашего исполнителя или создайте нового.",
                       InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("rel:art:"))
async def add_pick_artist(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    artist_id = int(cb.data.split(":")[2])
    await state.update_data(artist_id=artist_id, new_artist=None, created_artist_id=None)
    # убираем экран выбора исполнителя, дальше — цепочка подсказок мастера
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    await _ask_rel_type(cb, state)


@router.callback_query(F.data == "rel:artnew")
async def add_new_artist(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    # новый заход с экрана выбора — черновик исполнителя с чистого листа
    await state.update_data(new_artist=None, created_artist_id=None, artist_flow=None)
    # убираем экран выбора исполнителя — иначе его кнопки остаются висеть
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    await _ask_artname(cb.bot, cb.message.chat.id, state)


async def _ask_artname(bot, chat_id: int, state: FSMContext):
    await state.set_state(ReleaseAdd.artist_name)
    data = await state.get_data()
    # анкета вызывается из двух мест: мастер релиза и раздел «Исполнители»
    back_cb = "go_artists" if data.get("artist_flow") == "standalone" else "rel:add"
    await _replace_prompt(state, bot, chat_id,
                          "Название исполнителя или группы?",
                          InlineKeyboardMarkup(inline_keyboard=[_nav_row(back_cb)]))


@router.message(ReleaseAdd.artist_name, F.text)
async def artist_name_input(message: Message, state: FSMContext):
    name = message.text.strip()[:128]
    try:
        await message.delete()
    except Exception:
        pass
    if not name:
        return
    data = await state.get_data()
    new_artist = {**(data.get("new_artist") or {}), "name": name}
    await state.update_data(new_artist=new_artist)
    # если исполнитель уже создан (вернулись «Назад») — правим его в БД
    if data.get("created_artist_id"):
        async with SessionLocal() as s:
            a = (await s.execute(
                select(Artist).where(Artist.id == data["created_artist_id"])
            )).scalar_one_or_none()
            if a:
                a.name = name
                s.add(a)
                await s.commit()
    await state.set_state(None)
    await _ask_arttype(message.bot, message.chat.id, state)


async def _ask_arttype(bot, chat_id: int, state: FSMContext):
    data = await state.get_data()
    name = (data.get("new_artist") or {}).get("name", "")
    rows = [[InlineKeyboardButton(text=t, callback_data=f"rel:atype:{i}")]
            for i, t in enumerate(ARTIST_TYPES)]
    rows.append(_nav_row("rel:back:artname"))
    await _replace_prompt(state, bot, chat_id,
                          f"«{name}» — это:", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("rel:atype:"))
async def artist_type_pick(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    idx = int(cb.data.split(":")[2])
    data = await state.get_data()
    new_artist = data.get("new_artist") or {}
    new_artist["type"] = ARTIST_TYPES[idx] if 0 <= idx < len(ARTIST_TYPES) else "Другое"
    await state.update_data(new_artist=new_artist)
    if data.get("created_artist_id"):  # правка уже созданного (пришли «Назад»)
        async with SessionLocal() as s:
            a = (await s.execute(
                select(Artist).where(Artist.id == data["created_artist_id"])
            )).scalar_one_or_none()
            if a:
                a.artist_type = new_artist["type"]
                s.add(a)
                await s.commit()
    await _ask_artphoto(cb.bot, cb.message.chat.id, state)


async def _ask_artphoto(bot, chat_id: int, state: FSMContext):
    await state.set_state(ReleaseAdd.artist_photo)
    await _replace_prompt(state, bot, chat_id,
                          "Фото или логотип исполнителя?\n\nПришлите картинку — или пропустите.",
                          InlineKeyboardMarkup(inline_keyboard=[
                              [InlineKeyboardButton(text="⏭ Пропустить", callback_data="rel:askip")],
                              _nav_row("rel:back:arttype"),
                          ]))


async def _create_artist_and_continue(event, state: FSMContext):
    """Создаёт исполнителя в БД СРАЗУ (не при публикации релиза!) — чтобы
    он не терялся при сбое мастера и был виден в списке при новом заходе."""
    data = await state.get_data()
    na = data.get("new_artist") or {}
    if not na.get("name"):
        return
    async with SessionLocal() as s:
        artist = Artist(
            name=na["name"], artist_type=na.get("type", "Другое"),
            photo_file_id=na.get("photo"),
            owner_user_id=(event.from_user.id if event.from_user else 0),
        )
        s.add(artist)
        await s.commit()
        await s.refresh(artist)
    await state.update_data(artist_id=artist.id, created_artist_id=artist.id)
    if (await state.get_data()).get("artist_flow") == "standalone":
        await _finish_standalone_artist(event, state, artist.id)
        return
    await _ask_rel_type(event, state)


async def _finish_standalone_artist(event, state: FSMContext, artist_id: int):
    """Финал создания исполнителя из раздела «Исполнители» (без релиза)."""
    await state.clear()
    bot = event.bot
    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎤 Открыть карточку",
                              callback_data=f"art:view:{artist_id}:list")],
        [InlineKeyboardButton(text="🎵 Добавить релиз", callback_data="rel:add")],
        [InlineKeyboardButton(text="⬅️ К исполнителям", callback_data="go_artists"), _menu_btn()],
    ])
    await _send_screen(bot, chat_id, "🎉 Исполнитель создан!", kb)
    if isinstance(event, CallbackQuery):
        try:
            await event.answer()
        except Exception:
            pass


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
    if data.get("created_artist_id"):  # правка фото уже созданного
        async with SessionLocal() as s:
            a = (await s.execute(
                select(Artist).where(Artist.id == data["created_artist_id"])
            )).scalar_one_or_none()
            if a:
                a.photo_file_id = new_artist["photo"]
                s.add(a)
                await s.commit()
        await _ask_rel_type(message, state)
        return
    await _create_artist_and_continue(message, state)


@router.callback_query(F.data == "rel:askip")
async def artist_photo_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if data.get("created_artist_id"):
        await _ask_rel_type(cb, state)
        return
    await _create_artist_and_continue(cb, state)


async def _ask_rel_type(event, state: FSMContext):
    await state.set_state(None)
    bot = event.bot
    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
    data = await state.get_data()
    # шаг назад: создавали исполнителя → к его фото; выбирали из списка → к списку
    back_cb = "rel:back:artphoto" if data.get("created_artist_id") else "rel:add"
    rows = [[InlineKeyboardButton(text=label, callback_data=f"rel:rtype:{code}")]
            for code, label in RELEASE_TYPES.items()]
    rows.append(_nav_row(back_cb))
    await _replace_prompt(state, bot, chat_id, "Что выпускаем?",
                          InlineKeyboardMarkup(inline_keyboard=rows))
    if isinstance(event, CallbackQuery):
        try:
            await event.answer()
        except Exception:
            pass


async def _ask_title(bot, chat_id: int, state: FSMContext):
    await state.set_state(ReleaseAdd.rel_title)
    await _replace_prompt(state, bot, chat_id,
                          "Название релиза?\n\n(без имени исполнителя — только название)",
                          InlineKeyboardMarkup(inline_keyboard=[_nav_row("rel:back:rtype")]))


async def _ask_cover(bot, chat_id: int, state: FSMContext):
    await state.set_state(ReleaseAdd.cover)
    await _replace_prompt(state, bot, chat_id,
                          "Обложка релиза?\n\nПришлите картинку — обложка обязательна.",
                          InlineKeyboardMarkup(inline_keyboard=[_nav_row("rel:back:title")]))


async def _ask_media(bot, chat_id: int, state: FSMContext):
    await state.set_state(ReleaseAdd.media)
    data = await state.get_data()
    n_tracks = len(data.get("tracks") or [])
    n_links = len(data.get("links") or [])
    status = ""
    if n_tracks or data.get("video") or n_links:
        parts = []
        if n_tracks:
            parts.append(f"треков: {n_tracks}")
        if data.get("video"):
            parts.append("клип: есть")
        if n_links:
            parts.append(f"ссылок: {n_links}")
        status = "\n\nУже принято — " + ", ".join(parts) + "."
    await _replace_prompt(
        state, bot, chat_id,
        "Теперь сам релиз — присылайте сюда всё, что есть:\n\n"
        "🎧 аудио-треки по одному (в порядке альбома)\n"
        "🎬 видеоклип\n"
        "🔗 ссылки на площадки — YouTube, Spotify, Яндекс и др.\n\n"
        "Можно вперемешку. Когда закончите — нажмите «Готово»." + status,
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Готово", callback_data="rel:mdone")],
            _nav_row("rel:back:cover"),
        ]))


@router.callback_query(F.data.startswith("rel:back:"))
async def wiz_back(cb: CallbackQuery, state: FSMContext):
    """«Назад» — ровно один шаг по цепочке мастера."""
    await cb.answer()
    step = cb.data.split(":")[2]
    bot, chat_id = cb.bot, cb.message.chat.id
    if step == "artname":
        await _ask_artname(bot, chat_id, state)
    elif step == "arttype":
        await state.set_state(None)
        await _ask_arttype(bot, chat_id, state)
    elif step == "artphoto":
        await _ask_artphoto(bot, chat_id, state)
    elif step == "rtype":
        await _ask_rel_type(cb, state)
    elif step == "title":
        await _ask_title(bot, chat_id, state)
    elif step == "cover":
        await _ask_cover(bot, chat_id, state)
    elif step == "media":
        await _ask_media(bot, chat_id, state)
    elif step == "descr":
        await _ask_descr(cb, state)


@router.callback_query(F.data.startswith("rel:rtype:"))
async def rel_type_pick(cb: CallbackQuery, state: FSMContext):
    code = cb.data.split(":")[2]
    if code not in RELEASE_TYPES:
        await cb.answer()
        return
    await state.update_data(rel_type=code)
    await _ask_title(cb.bot, cb.message.chat.id, state)
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
    await _ask_cover(message.bot, message.chat.id, state)


@router.message(ReleaseAdd.cover, F.photo)
async def cover_input(message: Message, state: FSMContext):
    await state.update_data(cover=message.photo[-1].file_id)
    try:
        await message.delete()
    except Exception:
        pass
    # медиа-данные НЕ сбрасываем: пользователь мог вернуться «Назад» к обложке
    await _ask_media(message.bot, message.chat.id, state)


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
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Готово", callback_data="rel:mdone")],
            _nav_row("rel:back:cover"),
        ]))


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
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Готово", callback_data="rel:mdone")],
            _nav_row("rel:back:cover"),
        ]))


@router.message(ReleaseAdd.media, F.document)
async def media_document(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass
    await _replace_prompt(
        state, message.bot, message.chat.id,
        "Файл пришёл как документ — Telegram не сможет играть его как музыку.\n"
        "Пришлите как <b>аудио</b> (через скрепку → «Музыка») или как видео.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Готово", callback_data="rel:mdone")],
            _nav_row("rel:back:cover"),
        ]))


@router.message(ReleaseAdd.cover, ~F.photo)
async def cover_wrong_type(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass
    await _replace_prompt(state, message.bot, message.chat.id,
                          "Нужна именно картинка-обложка. Пришлите фото 🙂",
                          InlineKeyboardMarkup(inline_keyboard=[_nav_row("rel:back:title")]))


@router.message(ReleaseAdd.media, F.text)
async def media_links(message: Message, state: FSMContext):
    """Ссылки принимаются прямо на шаге медиа — вперемешку с файлами."""
    raw = message.text.replace(",", " ").split()
    new_links = [{"label": _link_label(u), "url": u} for u in raw if u.startswith("http")]
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Готово", callback_data="rel:mdone")],
        _nav_row("rel:back:cover"),
    ])
    if not new_links:
        await _replace_prompt(state, message.bot, message.chat.id,
                              "Это не похоже на ссылку (нужен адрес с http…). "
                              "Присылайте треки, клип или ссылки — либо жмите «Готово».", kb)
        return
    links = (data.get("links") or []) + new_links
    await state.update_data(links=links)
    got = ", ".join(l["label"] for l in new_links)
    await _replace_prompt(
        state, message.bot, message.chat.id,
        f"Ссылка принята: {got} ✔️ (всего: {len(links)})\n\n"
        "Присылайте ещё треки/клип/ссылки или жмите «Готово».", kb)


@router.callback_query(F.data == "rel:mdone")
async def media_done(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not (data.get("tracks") or data.get("video") or data.get("links")):
        await cb.answer("Нужен хотя бы один трек, клип или ссылка — иначе слушать нечего 🙂",
                        show_alert=True)
        return
    await _ask_descr(cb, state)


async def _ask_descr(event, state: FSMContext):
    await state.set_state(ReleaseAdd.descr)
    bot = event.bot
    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
    await _replace_prompt(state, bot, chat_id,
                          "Пара слов о релизе? (по желанию)",
                          InlineKeyboardMarkup(inline_keyboard=[
                              [InlineKeyboardButton(text="⏭ Пропустить", callback_data="rel:dskip")],
                              _nav_row("rel:back:media"),
                          ]))
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
    async with SessionLocal() as s:
        a = (await s.execute(
            select(Artist).where(Artist.id == data.get("artist_id"))
        )).scalar_one_or_none()
    artist_line = a.name if a else "не выбран — вернитесь в начало"
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
        _nav_row("rel:back:descr"),
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
    if data.get("rel_publishing"):  # защита от двойного нажатия
        await cb.answer()
        return
    if not data.get("title") or not data.get("cover") or not data.get("artist_id"):
        await cb.answer("Не хватает данных — начните заново.", show_alert=True)
        return
    await state.update_data(rel_publishing=True)
    await cb.answer("Публикуем…")
    cat_id = await _ensure_release_category()
    async with SessionLocal() as s:
        artist_id = data["artist_id"]
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
            msg = await cb.bot.send_message(
                admin_id, f"🆕 Новый релиз #{listing_id}: {data['title']}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="👀 Открыть", callback_data=f"rel:view:{listing_id}"),
                    InlineKeyboardButton(text="🚫 Скрыть", callback_data=f"rel:admhide:{listing_id}"),
                ]]))
            last_bot_messages.setdefault(admin_id, []).append(msg.message_id)
            await register_bot_messages(admin_id, [msg.message_id])
        except Exception as e:
            print(f"[releases] admin notify {admin_id}: {e}")

    # показываем готовую карточку
    listing, meta, artist, tracks = await _load_release(listing_id)
    from app.routers.admin_panel import is_admin
    caption = "🎉 Опубликовано!\n\n" + _release_caption(listing, meta, artist, tracks)
    kb = _release_kb(listing, meta, tracks, artist=artist,
                     viewer_id=cb.from_user.id, is_admin_user=is_admin(cb.from_user.id))
    await _send_screen(cb.bot, cb.message.chat.id, caption[:1024], kb, photo=listing.photo_file_id)
    links_pub = json.loads(meta.links) if meta and meta.links else []
    yt = _youtube_url(links_pub)
    if yt:
        await _send_release_yt_button(cb.bot, cb.message.chat.id, yt, listing.id)
