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
import asyncio
import html
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
from app.keyboards import get_common_menu_button
from app.models import Artist, Category, City, Listing, ReleaseMeta, ReleaseTrack, utcnow_naive
from app.analytics import log_event
from app.analytics.listing_views import log_listing_view
from app.features import is_enabled
from app.routers.utils import (
    clear_bot_messages,
    register_bot_messages,
    last_bot_messages,
    get_text,
)

router = Router(name="releases")

RELEASES_CATEGORY_SLUG = "releases"
PAGE = 8
MAX_LINKS = 10
MAX_TRACKS = 50
MAX_URL_LENGTH = 2048

_release_publish_locks: dict[int, asyncio.Lock] = {}

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
    "music.yandex.ru": "Яндекс Музыка",
    "music.apple.com": "Apple Music",
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


class _MusicEnabledMiddleware:
    """Закрывает все stale callback/FSM-входы музыкального раздела флагом."""

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None or await is_enabled("releases_enabled", user_id=user.id):
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer(await get_text("music_section_unavailable", "ru") or "Раздел временно недоступен.", show_alert=True)
        elif isinstance(event, Message):
            await event.answer(await get_text("releases_section_unavailable_msg", "ru") or "Музыкальный раздел временно недоступен.")
        return None


router.callback_query.middleware(_MusicEnabledMiddleware())
router.message.middleware(_MusicEnabledMiddleware())


def _e(value) -> str:
    """Пользовательский/БД-текст для Telegram HTML."""
    return html.escape(str(value or ""), quote=False)


def _fit_html_lines(lines: list[str], limit: int = 1024) -> str:
    """Обрезает только по границам строк, не разрывая HTML-теги/entities."""
    result: list[str] = []
    for line in lines:
        candidate = "\n".join([*result, line])
        if len(candidate) <= limit:
            result.append(line)
            continue
        if len("\n".join([*result, "…"])) <= limit:
            result.append("…")
        break
    return "\n".join(result)


def _normalize_http_url(value: str) -> str | None:
    """Принимает только полноценные http(s)-URL без credentials/пробелов."""
    raw = (value or "").strip()
    if (
        not raw
        or len(raw) > MAX_URL_LENGTH
        or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in raw)
        or any(ch in raw for ch in "\"'<>`\\")
    ):
        return None
    try:
        parsed = urllib.parse.urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        _ = parsed.port  # невалидный порт должен быть отвергнут
    except (TypeError, ValueError):
        return None
    return raw


def _parse_link_text(text: str, *, limit: int = MAX_LINKS) -> list[dict]:
    links: list[dict] = []
    seen: set[str] = set()
    for token in (text or "").replace(",", " ").split():
        url = _normalize_http_url(token)
        if not url or url in seen:
            continue
        links.append({"label": _link_label(url), "url": url})
        seen.add(url)
        if len(links) >= limit:
            break
    return links


def _load_links(raw_json: str | None) -> list[dict]:
    """Безопасно читает старые данные и отбрасывает опасные/битые URL."""
    try:
        raw_links = json.loads(raw_json) if raw_json else []
    except (TypeError, ValueError):
        return []
    if not isinstance(raw_links, list):
        return []
    links: list[dict] = []
    for item in raw_links[:MAX_LINKS]:
        if not isinstance(item, dict):
            continue
        url = _normalize_http_url(str(item.get("url") or ""))
        if not url:
            continue
        label = str(item.get("label") or _link_label(url))[:64]
        links.append({"label": label, "url": url})
    return links


# ─────────────────────────── helpers ───────────────────────────

async def _menu_btn() -> InlineKeyboardButton:
    return InlineKeyboardButton(text=(await get_text("btn_main_menu", "ru") or "☰ Главное меню"), callback_data="main_menu")


async def _nav_row(back_cb: str) -> list[InlineKeyboardButton]:
    """Железобетонное правило: на каждом экране «Назад» (один шаг) + меню."""
    back_btn = await get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)
    back_btn.callback_data = back_cb
    return [back_btn, await _menu_btn()]


async def _send_screen(bot, chat_id: int, text: str, kb=None, photo=None):
    """Экран раздела: чистим предыдущие сообщения, шлём новое, регистрируем."""
    await clear_bot_messages(chat_id, bot)
    # Поле исторически CSV для market/service. Релиз использует одну обложку;
    # даже повреждённая старая запись не должна передавать Telegram строку id1,id2.
    if isinstance(photo, str):
        photo = next((part.strip() for part in photo.split(",") if part.strip()), None)
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
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    for dom, label in LINK_LABELS.items():
        if host == dom or host.endswith("." + dom):
            return label
    return "Слушать"


def _yt_embeddable(url: str) -> bool:
    """Может ли страница-плеер (video_yt.html) реально открыть эту ссылку.

    Плеер вытаскивает id видео из watch?v=, youtu.be/<id>, /shorts/, /embed/,
    /live/. Плейлисты, каналы и music.youtube.com не встраиваются — для них
    кнопка плеера дала бы «Не удалось открыть видео»."""
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    path = u.path or ""
    if host == "youtu.be":
        return bool(path.strip("/"))
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if urllib.parse.parse_qs(u.query).get("v"):
            return True
        return any(path.startswith(p) for p in ("/shorts/", "/embed/", "/live/"))
    return False


def _youtube_url(links: list[dict]) -> str | None:
    """Первая YouTube-ссылка, пригодная для встроенного плеера."""
    for l in links:
        url = _normalize_http_url(str(l.get("url") or ""))
        if url and _yt_embeddable(url):
            return url
    return None


async def _release_yt_button(video_url: str, listing_id: int):
    """TWA-кнопка «Смотреть видео» для клавиатуры карточки (страница-плеер
    внутри Telegram, без окна подтверждения). None — если кнопку строить не из чего."""
    try:
        from app.routers.services_view import WEBAPP_BASE
        if not video_url or not WEBAPP_BASE:
            return None
        low = video_url.lower()
        if ("youtube.com" not in low) and ("youtu.be" not in low):
            return None
        twa_url = (f"{WEBAPP_BASE}/media/video_yt.html"
                   f"?u={urllib.parse.quote(video_url, safe='')}&listing_id={listing_id}")
        return InlineKeyboardButton(text=(await get_text("btn_watch_video", "ru") or "\u25b6\ufe0f Смотреть видео"), web_app=WebAppInfo(url=twa_url))
    except Exception as e:
        print(f"[releases] _release_yt_button: {e}")
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


async def _release_city_id() -> int | None:
    """Техническая FK-привязка: Белград из seed, затем любой существующий город."""
    async with SessionLocal() as s:
        city = (await s.execute(
            select(City).where(City.slug == "belgrade")
        )).scalars().first()
        if city is None:
            city = (await s.execute(select(City).order_by(City.id))).scalars().first()
    return city.id if city else None


async def _owned_active_artist(artist_id: int, owner_id: int):
    async with SessionLocal() as s:
        return (await s.execute(
            select(Artist).where(
                Artist.id == artist_id,
                Artist.owner_user_id == owner_id,
                Artist.status == "active",
            )
        )).scalar_one_or_none()


async def _load_release(listing_id: int):
    """→ (listing, meta, artist, tracks) либо (None, None, None, [])."""
    async with SessionLocal() as s:
        listing = (await s.execute(
            select(Listing).where(Listing.id == listing_id, Listing.type == "release")
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


def _release_is_public(listing, meta, artist, tracks) -> bool:
    base = bool(
        listing
        and listing.type == "release"
        and listing.status == "active"
        and meta
        and meta.status == "published"
        and artist
        and artist.id == meta.artist_id
        and artist.owner_user_id == listing.owner_id
        and artist.status == "active"
    )
    return base and _has_release_media(meta, tracks)


def _can_view_release(user_id: int, listing, meta, artist, tracks) -> bool:
    if _release_is_public(listing, meta, artist, tracks):
        return True
    if (
        not listing
        or listing.type != "release"
        or listing.status != "active"
        or not meta
        or meta.status == "deleted"
    ):
        return False
    from app.routers.admin_panel import is_admin
    return listing.owner_id == user_id or is_admin(user_id)


def _has_release_media(meta, tracks) -> bool:
    return bool(tracks or (meta and meta.video_file_id) or _load_links(meta.links if meta else None))


async def _release_caption(listing, meta, artist, tracks, *, hidden: bool = False) -> str:
    track_default_title_tmpl = await get_text("releases_track_default_title_tmpl", "ru") or "Трек {n}"
    a_name = _e(artist.name if artist else (await get_text("releases_unknown_artist", "ru") or "Неизвестный исполнитель"))
    title = _e(listing.title)
    t_label = _e(RELEASE_TYPES.get(meta.release_type, meta.release_type) if meta else "")
    lines = [f"🎵 <b>{a_name} — «{title}»</b>"]
    if hidden:
        lines[0:0] = [(await get_text("releases_hidden_marker", "ru") or "🚫 <i>Релиз скрыт</i>"), ""]
    sub = [t_label]
    if meta and meta.release_date:
        sub.append(_e(meta.release_date))
    if meta and meta.genre:
        sub.append(_e(meta.genre))
    lines.append(" · ".join(x for x in sub if x))
    if meta and meta.recorded_at:
        lines.append((await get_text("releases_recorded_at_line_tmpl", "ru") or "🎙 Записано: {value}").format(value=_e(meta.recorded_at)))
    if listing.descr:
        lines.append("")
        lines.append(_e(listing.descr))
    playable = _release_is_public(listing, meta, artist, tracks)
    if tracks and playable:
        lines.append("")
        lines.append(await get_text("releases_tracklist_label", "ru") or "<b>Трек-лист:</b>")
        for t in tracks:
            lines.append(f"{t.position}. {_e(t.title or track_default_title_tmpl.format(n=t.position))}")
    # YouTube в тексте не показываем: как в Услугах, под карточкой идёт
    # отдельная TWA-кнопка «▶️ Смотреть видео» в клавиатуре карточки (_release_yt_button)
    return _fit_html_lines(lines)


async def _release_kb(listing, meta, tracks, *, viewer_id: int, is_admin_user: bool,
                artist=None, back_cb: str = "go_releases",
                back_label: str = "⬅️ К релизам", source: str = "",
                yt_btn: InlineKeyboardButton | None = None,
                yt_url: str | None = None) -> InlineKeyboardMarkup:
    link_btn_tmpl = await get_text("releases_btn_link_tmpl", "ru") or "🎧 {label}"
    rows: list[list[InlineKeyboardButton]] = []
    playable = _release_is_public(listing, meta, artist, tracks)
    if tracks and playable:
        rows.append([InlineKeyboardButton(
            text=(await get_text("releases_btn_listen_telegram", "ru") or "▶️ Слушать в Telegram"),
            callback_data=f"rel:listen:{listing.id}:{source}")])
    if meta and meta.video_file_id and playable:
        rows.append([InlineKeyboardButton(
            text=(await get_text("releases_btn_watch_clip_telegram", "ru") or "▶️ Смотреть клип в Telegram"),
            callback_data=f"rel:video:{listing.id}:{source}")])
    # Видео-плеер (YouTube TWA) — сразу под медиа, ближе к карточке
    if yt_btn is not None and playable:
        rows.append([yt_btn])
    if artist is not None:
        rows.append([InlineKeyboardButton(
            text=(await get_text("releases_btn_about_artist", "ru") or "🎤 Об исполнителе"),
            callback_data=f"art:view:{artist.id}:rel{listing.id}.{source}")])
    links = _load_links(meta.links if meta and playable else None)
    row: list[InlineKeyboardButton] = []
    for l in links:
        # Из общего ряда убираем только ту YouTube-ссылку, что стала кнопкой
        # плеера. Плейлисты/каналы/music.youtube остаются обычными кнопками.
        if yt_url and _normalize_http_url(str(l.get("url") or "")) == yt_url:
            continue
        row.append(InlineKeyboardButton(text=link_btn_tmpl.format(label=l["label"]), url=l["url"]))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    ctl: list[InlineKeyboardButton] = []
    if viewer_id == listing.owner_id or is_admin_user:
        ctl.append(InlineKeyboardButton(text=(await get_text("releases_btn_edit", "ru") or "✏️ Редактировать"), callback_data=f"rel:edit:{listing.id}"))
    if viewer_id == listing.owner_id:
        ctl.append(InlineKeyboardButton(text=(await get_text("releases_btn_delete", "ru") or "🗑 Удалить"), callback_data=f"rel:del:{listing.id}"))
    if is_admin_user and meta and meta.status == "published":
        ctl.append(InlineKeyboardButton(text=(await get_text("releases_btn_admin_hide", "ru") or "🚫 Скрыть"), callback_data=f"rel:admhide:{listing.id}"))
    if is_admin_user and meta and meta.status == "hidden":
        ctl.append(InlineKeyboardButton(text=(await get_text("releases_btn_admin_show", "ru") or "✅ Показать"), callback_data=f"rel:admshow:{listing.id}"))
    if ctl:
        rows.append(ctl)
    # «Пожаловаться» — почти в самом низу: ниже только «Назад» и «Меню»
    if playable:
        rows.append([InlineKeyboardButton(
            text=(await get_text("releases_btn_report", "ru") or "⚠️ Пожаловаться"), callback_data=f"rel:report:{listing.id}")])
    rows.append([InlineKeyboardButton(text=back_label, callback_data=back_cb), await _menu_btn()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _release_back(src: str, artist=None) -> tuple[str, str]:
    """«Назад» с карточки — ровно на шаг, туда, откуда пришли."""
    if src == "s":
        return "rel:sback", (await get_text("releases_btn_back_to_search_results", "ru") or "⬅️ К результатам")
    if src == "my":
        return "rel:my", (await get_text("releases_btn_back_to_my_releases", "ru") or "⬅️ К моим релизам")
    if src.startswith("a"):
        artist_ref, _, artist_source = src[1:].partition(".")
        if artist_ref.isdigit():
            back_source = "search" if artist_source == "s" else "list"
            return f"art:view:{artist_ref}:{back_source}", (await get_text("releases_btn_back_to_artist", "ru") or "⬅️ К исполнителю")
    return "go_releases", (await get_text("releases_btn_back_to_releases", "ru") or "⬅️ К релизам")


def _clean_release_source(src: str) -> str:
    if src in {"", "s", "my"}:
        return src
    if src.startswith("a"):
        artist_ref, _, artist_source = src[1:].partition(".")
        if artist_ref.isdigit() and artist_source in {"", "s", "l"}:
            suffix = f".{artist_source}" if artist_source else ""
            return f"a{artist_ref[:16]}{suffix}"
    return ""


# ─────────────────────────── лента ───────────────────────────

@router.callback_query(F.data == "go_releases")
@router.callback_query(F.data.startswith("rel:list:"))
async def releases_feed(cb: CallbackQuery, state: FSMContext):
    if not await is_enabled("releases_enabled", user_id=cb.from_user.id):
        await cb.answer(await get_text("music_section_unavailable", "ru") or "Раздел временно недоступен.", show_alert=True)
        return
    await state.clear()
    offset = 0
    if cb.data.startswith("rel:list:"):
        try:
            offset = max(0, int(cb.data.split(":")[2]))
        except ValueError:
            offset = 0

    from app.routers.admin_panel import is_admin
    admin = is_admin(cb.from_user.id)
    async with SessionLocal() as s:
        q = select(ReleaseMeta).where(ReleaseMeta.status != "deleted")
        if not admin:  # админ видит все релизы, скрытые — с красным кружком
            q = q.where(ReleaseMeta.status == "published")
        metas = (await s.execute(
            q.order_by(ReleaseMeta.created_at.desc())
        )).scalars().all()
        visible: list[tuple[ReleaseMeta, Listing, Artist]] = []
        for m in metas:
            listing = (await s.execute(
                select(Listing).where(Listing.id == m.listing_id, Listing.type == "release")
            )).scalar_one_or_none()
            artist = (await s.execute(
                select(Artist).where(Artist.id == m.artist_id)
            )).scalar_one_or_none()
            release_tracks = (await s.execute(
                select(ReleaseTrack).where(ReleaseTrack.listing_id == m.listing_id)
            )).scalars().all()
            if not listing or listing.status != "active" or not artist:
                continue
            if admin:
                if m.status != "deleted":
                    visible.append((m, listing, artist))
            elif _release_is_public(listing, m, artist, release_tracks):
                visible.append((m, listing, artist))
        total = len(visible)
        page = visible[offset:offset + PAGE]
        rows: list[list[InlineKeyboardButton]] = []
        for m, listing, artist in page:
            a_name = artist.name if artist else "?"
            t_label = RELEASE_TYPES.get(m.release_type, "")
            release_tracks = (await s.execute(
                select(ReleaseTrack).where(ReleaseTrack.listing_id == m.listing_id)
            )).scalars().all()
            mark = "🔴 " if not _release_is_public(listing, m, artist, release_tracks) else ""
            rows.append([InlineKeyboardButton(
                text=f"{mark}🎵 {a_name} — {listing.title} ({t_label})",
                callback_data=f"rel:view:{listing.id}")])

    pages = max(1, (total + PAGE - 1) // PAGE)
    if pages > 1:
        nav: list[InlineKeyboardButton] = []
        if offset > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"rel:list:{max(0, offset - PAGE)}"))
        nav.append(InlineKeyboardButton(text=f"{offset // PAGE + 1}/{pages}", callback_data="rel:noop"))
        if offset + PAGE < total:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"rel:list:{offset + PAGE}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton(text=(await get_text("releases_btn_search", "ru") or "🔍 Поиск"), callback_data="rel:search"),
                 InlineKeyboardButton(text=(await get_text("releases_btn_add_release", "ru") or "➕ Добавить релиз"), callback_data="rel:add")])
    rows.append([InlineKeyboardButton(text=(await get_text("releases_btn_my_releases", "ru") or "💿 Мои релизы"), callback_data="rel:my"), await _menu_btn()])

    text = await get_text("releases_feed_title", "ru") or "🎵 <b>Релизы сообщества</b>\n\nНовая музыка наших исполнителей: синглы, альбомы, клипы."
    if total == 0:
        text += await get_text("releases_feed_empty_suffix", "ru") or "\n\nРелизов пока нет — станьте первым!"
    await _send_screen(cb.bot, cb.message.chat.id, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


# ─────────────────────────── карточка ───────────────────────────

@router.callback_query(F.data.startswith("rel:view:"))
async def release_view(cb: CallbackQuery):
    parts = cb.data.split(":")
    try:
        listing_id = int(parts[2])
    except (IndexError, ValueError):
        await cb.answer(await get_text("err_invalid_link", "ru") or "Некорректная ссылка.", show_alert=True)
        return
    src = _clean_release_source(parts[3] if len(parts) > 3 else "")
    await _show_release_card(cb, listing_id, src)
    try:
        await cb.answer()
    except Exception:
        pass


async def _show_release_card(cb: CallbackQuery, listing_id: int, src: str = ""):
    """Рендер карточки релиза. Вызывается и после Скрыть/Показать,
    чтобы экран сразу отражал новый статус."""
    listing, meta, artist, tracks = await _load_release(listing_id)
    if not _can_view_release(cb.from_user.id, listing, meta, artist, tracks):
        await cb.answer(await get_text("releases_unavailable", "ru") or "Релиз недоступен.", show_alert=True)
        return

    from app.routers.admin_panel import is_admin
    caption = await _release_caption(
        listing, meta, artist, tracks,
        hidden=not _release_is_public(listing, meta, artist, tracks),
    )
    back_cb, back_label = await _release_back(src, artist)
    links = _load_links(
        meta.links if _release_is_public(listing, meta, artist, tracks) else None
    )
    yt = _youtube_url(links)
    yt_btn = await _release_yt_button(yt, listing.id) if yt else None
    kb = await _release_kb(listing, meta, tracks, artist=artist,
                     viewer_id=cb.from_user.id, is_admin_user=is_admin(cb.from_user.id),
                     back_cb=back_cb, back_label=back_label, source=src,
                     yt_btn=yt_btn, yt_url=yt)
    await _send_screen(cb.bot, cb.message.chat.id, caption, kb, photo=listing.photo_file_id)
    source = "search" if src == "s" else "my" if src == "my" else "artist" if src.startswith("a") else "catalog"
    await log_listing_view(listing_id=listing.id, user_id=cb.from_user.id,
                           section="releases", action="open", source=source)


@router.callback_query(F.data.startswith("rel:listen:"))
async def release_listen(cb: CallbackQuery):
    parts = cb.data.split(":")
    try:
        listing_id = int(parts[2])
    except (IndexError, ValueError):
        await cb.answer(await get_text("err_invalid_link", "ru") or "Некорректная ссылка.", show_alert=True)
        return
    src = _clean_release_source(parts[3] if len(parts) > 3 else "")
    listing, meta, artist, tracks = await _load_release(listing_id)
    if not _release_is_public(listing, meta, artist, tracks):
        await cb.answer(await get_text("releases_unavailable", "ru") or "Релиз недоступен.", show_alert=True)
        return
    if not tracks:
        await cb.answer(await get_text("releases_tracks_not_found", "ru") or "Треки не найдены.", show_alert=True)
        return
    track_default_title_tmpl = await get_text("releases_track_default_title_tmpl", "ru") or "Трек {n}"
    rows = [[InlineKeyboardButton(
        text=f"{t.position}. {t.title or track_default_title_tmpl.format(n=t.position)}",
        callback_data=f"rel:track:{t.id}:{src}")] for t in tracks]
    rows.append([
        InlineKeyboardButton(text=(await get_text("releases_btn_back_to_release", "ru") or "⬅️ К релизу"), callback_data=f"rel:view:{listing_id}:{src}"),
        await _menu_btn(),
    ])
    # трек-лист отдельным сообщением ПОД карточкой (карточку не сносим)
    msg = await cb.bot.send_message(
        cb.message.chat.id, await get_text("releases_choose_track_prompt", "ru") or "Выберите трек:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
    await register_bot_messages(cb.message.chat.id, [msg.message_id])
    await cb.answer()


@router.callback_query(F.data.startswith("rel:video:"))
async def release_video_play(cb: CallbackQuery):
    parts = cb.data.split(":")
    try:
        listing_id = int(parts[2])
    except (IndexError, ValueError):
        await cb.answer(await get_text("err_invalid_link", "ru") or "Некорректная ссылка.", show_alert=True)
        return
    listing, meta, artist, tracks = await _load_release(listing_id)
    if not _release_is_public(listing, meta, artist, tracks) or not meta.video_file_id:
        await cb.answer(await get_text("releases_clip_unavailable", "ru") or "Клип недоступен.", show_alert=True)
        return
    try:
        msg = await cb.bot.send_video(cb.message.chat.id, meta.video_file_id)
        last_bot_messages.setdefault(cb.message.chat.id, []).append(msg.message_id)
        await register_bot_messages(cb.message.chat.id, [msg.message_id])
        await log_listing_view(listing_id=listing_id, user_id=cb.from_user.id,
                               section="releases", action="open", source="video")
        await cb.answer()
    except Exception as e:
        print(f"[releases] send_video failed: {e}")
        await cb.answer(await get_text("releases_clip_send_failed", "ru") or "Не удалось отправить клип.", show_alert=True)


@router.callback_query(F.data.startswith("rel:track:"))
async def release_track_play(cb: CallbackQuery):
    parts = cb.data.split(":")
    try:
        track_id = int(parts[2])
    except (IndexError, ValueError):
        await cb.answer(await get_text("err_invalid_link", "ru") or "Некорректная ссылка.", show_alert=True)
        return
    async with SessionLocal() as s:
        t = (await s.execute(
            select(ReleaseTrack).where(ReleaseTrack.id == track_id)
        )).scalar_one_or_none()
    if not t:
        await cb.answer(await get_text("releases_track_not_found", "ru") or "Трек не найден.", show_alert=True)
        return
    listing, meta, artist, tracks = await _load_release(t.listing_id)
    if not _release_is_public(listing, meta, artist, tracks) or all(x.id != t.id for x in tracks):
        await cb.answer(await get_text("releases_unavailable", "ru") or "Релиз недоступен.", show_alert=True)
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
        await cb.answer(await get_text("releases_track_send_failed", "ru") or "Не удалось отправить трек.", show_alert=True)


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
    try:
        listing_id = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        await cb.answer(await get_text("err_invalid_link", "ru") or "Некорректная ссылка.", show_alert=True)
        return
    listing, meta, artist, tracks = await _load_release(listing_id)
    if not _release_is_public(listing, meta, artist, tracks):
        await cb.answer(await get_text("releases_unavailable", "ru") or "Релиз недоступен.", show_alert=True)
        return
    rows = [[InlineKeyboardButton(text=label, callback_data=f"rel:repdo:{listing_id}:{code}")]
            for code, label in REPORT_REASONS.items()]
    rows.append(await _nav_row("rel:repcancel"))
    msg = await cb.bot.send_message(
        cb.message.chat.id, await get_text("releases_report_prompt", "ru") or "Что не так с этим релизом?",
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
    await cb.answer(await get_text("releases_report_cancelled", "ru") or "Отменено.")


@router.callback_query(F.data.startswith("rel:repdo:"))
async def release_report_send(cb: CallbackQuery, state: FSMContext):
    """Шаг 2: жалоба с причиной уходит админам. «Другое» — просим описать."""
    parts = cb.data.split(":", 3)
    if len(parts) != 4:
        await cb.answer(await get_text("err_invalid_link", "ru") or "Некорректная ссылка.", show_alert=True)
        return
    _, _, lid_raw, reason = parts
    try:
        listing_id = int(lid_raw)
    except ValueError:
        await cb.answer(await get_text("err_invalid_link", "ru") or "Некорректная ссылка.", show_alert=True)
        return

    if reason not in REPORT_REASONS:
        await cb.answer(await get_text("releases_report_unknown_reason", "ru") or "Неизвестная причина.", show_alert=True)
        return
    listing, meta, artist, tracks = await _load_release(listing_id)
    if not _release_is_public(listing, meta, artist, tracks):
        await cb.answer(await get_text("releases_unavailable", "ru") or "Релиз недоступен.", show_alert=True)
        return

    if reason == "other":
        await cb.answer()
        await state.set_state(ReleaseReport.other_text)
        await state.update_data(report_listing_id=listing_id)
        try:
            await cb.message.edit_text(
                await get_text("releases_report_describe_prompt", "ru") or "Опишите своими словами, что не так:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    await _nav_row(f"rel:repback:{listing_id}")]))
        except Exception:
            pass
        return

    await state.clear()
    try:
        await cb.message.delete()
    except Exception:
        pass
    await _notify_report(
        cb.bot, cb.from_user, listing_id, listing, artist, REPORT_REASONS[reason]
    )
    await cb.answer(await get_text("releases_report_sent", "ru") or "Жалоба отправлена. Спасибо!", show_alert=True)


@router.callback_query(F.data.startswith("rel:repback:"))
async def release_report_back(cb: CallbackQuery, state: FSMContext):
    """«Назад» из «Другое» — возвращаем список причин на том же сообщении."""
    try:
        listing_id = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        await cb.answer(await get_text("err_invalid_link", "ru") or "Некорректная ссылка.", show_alert=True)
        return
    await cb.answer()
    await state.clear()
    rows = [[InlineKeyboardButton(text=label, callback_data=f"rel:repdo:{listing_id}:{code}")]
            for code, label in REPORT_REASONS.items()]
    rows.append(await _nav_row("rel:repcancel"))
    try:
        await cb.message.edit_text(await get_text("releases_report_prompt", "ru") or "Что не так с этим релизом?",
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        pass


@router.message(ReleaseReport.other_text, F.text)
async def release_report_other(message: Message, state: FSMContext):
    data = await state.get_data()
    listing_id = data.get("report_listing_id")
    text = message.text.strip()[:400]
    try:
        await message.delete()
    except Exception:
        pass
    if not listing_id:
        await state.clear()
        return
    if not text:
        await message.answer(await get_text("releases_report_empty_text", "ru") or "Опишите причину текстом.")
        return
    listing, meta, artist, tracks = await _load_release(listing_id)
    if not _release_is_public(listing, meta, artist, tracks):
        await state.clear()
        await message.answer(await get_text("releases_already_unavailable", "ru") or "Релиз уже недоступен.")
        return
    await state.clear()
    other_reason_tmpl = await get_text("releases_report_reason_other_tmpl", "ru") or "Другое: {text}"
    await _notify_report(message.bot, message.from_user, listing_id, listing, artist,
                         other_reason_tmpl.format(text=text))
    msg = await message.bot.send_message(
        message.chat.id, await get_text("releases_report_sent", "ru") or "Жалоба отправлена. Спасибо!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=(await get_text("releases_btn_back_to_release", "ru") or "⬅️ К релизу"), callback_data=f"rel:view:{listing_id}"),
             await _menu_btn()]]))
    last_bot_messages.setdefault(message.chat.id, []).append(msg.message_id)
    await register_bot_messages(message.chat.id, [msg.message_id])


async def _notify_report(bot, from_user, listing_id, listing, artist, reason_label):
    notify_tmpl = await get_text("releases_report_notify_tmpl", "ru") or (
        "⚠️ Жалоба на релиз #{id} ({artist} — {title})\n"
        "Причина: {reason}\n"
        "От: {user_id} (@{username})"
    )
    btn_open = await get_text("releases_btn_open_admin", "ru") or "👀 Открыть"
    btn_hide = await get_text("releases_btn_admin_hide", "ru") or "🚫 Скрыть"
    for admin_id in await _admin_ids():
        try:
            msg = await bot.send_message(
                admin_id,
                notify_tmpl.format(
                    id=listing_id,
                    artist=_e(artist.name if artist else '?'),
                    title=_e(listing.title if listing else '?'),
                    reason=_e(reason_label),
                    user_id=from_user.id,
                    username=_e(from_user.username or '—'),
                ),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text=btn_open, callback_data=f"rel:view:{listing_id}"),
                    InlineKeyboardButton(text=btn_hide, callback_data=f"rel:admhide:{listing_id}"),
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
        await cb.answer(await get_text("music_admin_only", "ru") or "Только для администратора.", show_alert=True)
        return
    listing_id = int(cb.data.split(":")[2])
    async with SessionLocal() as s:
        listing = (await s.execute(
            select(Listing).where(Listing.id == listing_id, Listing.type == "release")
        )).scalar_one_or_none()
        meta = (await s.execute(
            select(ReleaseMeta).where(ReleaseMeta.listing_id == listing_id)
        )).scalar_one_or_none()
        if not listing or not meta or meta.status == "deleted":
            await cb.answer(await get_text("music_not_found", "ru") or "Не найден.", show_alert=True)
            return
        meta.status = "hidden"
        s.add(meta)
        await s.commit()
    await cb.answer()
    await _show_release_card(cb, listing_id)  # сразу свежий статус и кнопки


@router.callback_query(F.data.startswith("rel:admshow:"))
async def release_admin_show(cb: CallbackQuery):
    from app.routers.admin_panel import is_admin
    if not is_admin(cb.from_user.id):
        await cb.answer(await get_text("music_admin_only", "ru") or "Только для администратора.", show_alert=True)
        return
    listing_id = int(cb.data.split(":")[2])
    async with SessionLocal() as s:
        listing = (await s.execute(
            select(Listing).where(Listing.id == listing_id, Listing.type == "release")
        )).scalar_one_or_none()
        meta = (await s.execute(
            select(ReleaseMeta).where(ReleaseMeta.listing_id == listing_id)
        )).scalar_one_or_none()
        artist = (await s.execute(
            select(Artist).where(Artist.id == meta.artist_id)
        )).scalar_one_or_none() if meta else None
        tracks = (await s.execute(
            select(ReleaseTrack).where(ReleaseTrack.listing_id == listing_id)
        )).scalars().all()
        if (
            not listing
            or listing.status != "active"
            or not meta
            or meta.status == "deleted"
        ):
            await cb.answer(await get_text("music_not_found", "ru") or "Не найден.", show_alert=True)
            return
        if (
            not artist
            or artist.status != "active"
            or artist.owner_user_id != listing.owner_id
        ):
            await cb.answer(await get_text("releases_show_artist_card_first", "ru") or "Сначала покажите карточку исполнителя.", show_alert=True)
            return
        if not _has_release_media(meta, tracks):
            await cb.answer(await get_text("releases_add_media_first", "ru") or "Сначала добавьте трек, клип или ссылку.", show_alert=True)
            return
        meta.status = "published"
        s.add(meta)
        await s.commit()
    await cb.answer()
    await _show_release_card(cb, listing_id)


# ─────────────────────────── мои релизы ───────────────────────────

@router.callback_query(F.data == "rel:my")
async def my_releases(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    async with SessionLocal() as s:
        listings = (await s.execute(
            select(Listing).where(
                Listing.owner_id == cb.from_user.id,
                Listing.type == "release",
                Listing.status == "active",
            )
            .order_by(Listing.created_at.desc())
        )).scalars().all()
        rows = []
        for l in listings:
            meta = (await s.execute(
                select(ReleaseMeta).where(ReleaseMeta.listing_id == l.id)
            )).scalar_one_or_none()
            if not meta or meta.status == "deleted":
                continue
            artist = (await s.execute(
                select(Artist).where(Artist.id == meta.artist_id)
            )).scalar_one_or_none()
            tracks = (await s.execute(
                select(ReleaseTrack).where(ReleaseTrack.listing_id == l.id)
            )).scalars().all()
            mark = "" if _release_is_public(l, meta, artist, tracks) else "🔴 "
            rows.append([InlineKeyboardButton(
                text=f"{mark}🎵 {l.title}", callback_data=f"rel:view:{l.id}:my")])
    rows.append([InlineKeyboardButton(text=(await get_text("releases_btn_add_release", "ru") or "➕ Добавить релиз"), callback_data="rel:add")])
    rows.append([InlineKeyboardButton(text=(await get_text("releases_btn_back_to_releases", "ru") or "⬅️ К релизам"), callback_data="go_releases"), await _menu_btn()])
    my_releases_empty = "\n\nУ вас пока нет релизов."
    text = (await get_text("releases_my_releases_title", "ru") or "💿 <b>Мои релизы</b>") + ("" if len(rows) > 2 else my_releases_empty)
    await _send_screen(cb.bot, cb.message.chat.id, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data.startswith("rel:del:"))
async def release_delete_ask(cb: CallbackQuery):
    listing_id = int(cb.data.split(":")[2])
    listing, meta, artist, _ = await _load_release(listing_id)
    if not listing or not meta or not await _can_edit_release(
        cb.from_user.id, listing, meta, artist
    ):
        await cb.answer(await get_text("releases_delete_owner_only", "ru") or "Удалить может только автор.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=(await get_text("releases_btn_delete_confirm_yes", "ru") or "✅ Да, удалить"), callback_data=f"rel:delok:{listing_id}"),
        InlineKeyboardButton(text=(await get_text("releases_btn_delete_confirm_no", "ru") or "✖ Отмена"), callback_data=f"rel:view:{listing_id}:my"),
    ]])
    await _send_screen(cb.bot, cb.message.chat.id, await get_text("releases_delete_confirm", "ru") or "Удалить релиз безвозвратно?", kb)
    await cb.answer()


@router.callback_query(F.data.startswith("rel:delok:"))
async def release_delete(cb: CallbackQuery):
    listing_id = int(cb.data.split(":")[2])
    async with SessionLocal() as s:
        listing = (await s.execute(
            select(Listing).where(Listing.id == listing_id, Listing.type == "release")
        )).scalar_one_or_none()
        from app.routers.admin_panel import is_admin
        if (
            not listing
            or listing.status != "active"
            or (listing.owner_id != cb.from_user.id and not is_admin(cb.from_user.id))
        ):
            await cb.answer(await get_text("releases_delete_owner_only", "ru") or "Удалить может только автор.", show_alert=True)
            return
        meta = (await s.execute(
            select(ReleaseMeta).where(ReleaseMeta.listing_id == listing_id)
        )).scalar_one_or_none()
        if not meta or meta.status == "deleted":
            await cb.answer(await get_text("releases_not_found", "ru") or "Релиз не найден.", show_alert=True)
            return
        meta.status = "deleted"
        s.add(meta)
        await s.commit()
    await cb.answer(await get_text("releases_deleted_toast", "ru") or "Релиз удалён.")
    await _send_screen(cb.bot, cb.message.chat.id, await get_text("releases_deleted_screen", "ru") or "Релиз удалён.",
                       InlineKeyboardMarkup(inline_keyboard=[
                           [InlineKeyboardButton(text=(await get_text("releases_btn_my_releases", "ru") or "💿 Мои релизы"), callback_data="rel:my")],
                           [InlineKeyboardButton(text=(await get_text("releases_btn_back_to_releases", "ru") or "⬅️ К релизам"), callback_data="go_releases"), await _menu_btn()],
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
    rows.append([InlineKeyboardButton(text=(await get_text("releases_btn_create_new_artist", "ru") or "➕ Создать нового исполнителя"), callback_data="rel:artnew")])
    rows.append(await _nav_row("go_releases"))
    await _send_screen(cb.bot, cb.message.chat.id,
                       await get_text("releases_ask_whose_release", "ru") or "Чей это релиз?\n\nВыберите вашего исполнителя или создайте нового.",
                       InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("rel:art:"))
async def add_pick_artist(cb: CallbackQuery, state: FSMContext):
    try:
        artist_id = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        await cb.answer(await get_text("err_invalid_link", "ru") or "Некорректная ссылка.", show_alert=True)
        return
    async with SessionLocal() as s:
        artist = (await s.execute(
            select(Artist).where(
                Artist.id == artist_id,
                Artist.owner_user_id == cb.from_user.id,
                Artist.status == "active",
            )
        )).scalar_one_or_none()
    if not artist:
        await cb.answer(await get_text("releases_artist_unavailable_other_owner", "ru") or "Исполнитель недоступен или принадлежит другому пользователю.",
                        show_alert=True)
        return
    await cb.answer()
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
                          await get_text("releases_ask_artist_name", "ru") or "Название исполнителя или группы?",
                          InlineKeyboardMarkup(inline_keyboard=[await _nav_row(back_cb)]))


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
                select(Artist).where(
                    Artist.id == data["created_artist_id"],
                    Artist.owner_user_id == message.from_user.id,
                )
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
    rows.append(await _nav_row("rel:back:artname"))
    arttype_tmpl = await get_text("releases_ask_artist_type_tmpl", "ru") or "«{name}» — это:"
    await _replace_prompt(state, bot, chat_id,
                          arttype_tmpl.format(name=_e(name)), InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("rel:atype:"))
async def artist_type_pick(cb: CallbackQuery, state: FSMContext):
    try:
        idx = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        await cb.answer(await get_text("err_invalid_link", "ru") or "Некорректная ссылка.", show_alert=True)
        return
    await cb.answer()
    data = await state.get_data()
    new_artist = data.get("new_artist") or {}
    new_artist["type"] = ARTIST_TYPES[idx] if 0 <= idx < len(ARTIST_TYPES) else "Другое"
    await state.update_data(new_artist=new_artist)
    if data.get("created_artist_id"):  # правка уже созданного (пришли «Назад»)
        async with SessionLocal() as s:
            a = (await s.execute(
                select(Artist).where(
                    Artist.id == data["created_artist_id"],
                    Artist.owner_user_id == cb.from_user.id,
                )
            )).scalar_one_or_none()
            if a:
                a.artist_type = new_artist["type"]
                s.add(a)
                await s.commit()
    await _ask_artphoto(cb.bot, cb.message.chat.id, state)


async def _ask_artphoto(bot, chat_id: int, state: FSMContext):
    await state.set_state(ReleaseAdd.artist_photo)
    await _replace_prompt(state, bot, chat_id,
                          await get_text("releases_ask_artist_photo", "ru") or "Фото или логотип исполнителя?\n\nПришлите картинку — или пропустите.",
                          InlineKeyboardMarkup(inline_keyboard=[
                              [InlineKeyboardButton(text=(await get_text("releases_btn_skip", "ru") or "⏭ Пропустить"), callback_data="rel:askip")],
                              await _nav_row("rel:back:arttype"),
                          ]))


_artist_create_locks: dict[int, asyncio.Lock] = {}


async def _create_artist_and_continue(event, state: FSMContext):
    """Создаёт исполнителя в БД СРАЗУ (не при публикации релиза!) — чтобы
    он не терялся при сбое мастера и был виден в списке при новом заходе.

    Замок + повторная проверка created_artist_id: двойное нажатие
    «Пропустить» или повторное фото не создают второго исполнителя."""
    uid = event.from_user.id if event.from_user else 0
    lock = _artist_create_locks.setdefault(uid, asyncio.Lock())
    async with lock:
        data = await state.get_data()
        if data.get("created_artist_id"):
            # исполнитель уже создан параллельным нажатием — просто продолжаем
            await _ask_rel_type(event, state)
            return
        na = data.get("new_artist") or {}
        if not na.get("name"):
            return
        # Контакт создателя — базовый, проставляется сразу и не удаляется:
        # у карточки всегда должен быть рабочий контакт для связи
        base_contact = (f"@{event.from_user.username}"
                        if event.from_user and event.from_user.username else None)
        async with SessionLocal() as s:
            artist = Artist(
                name=na["name"], artist_type=na.get("type", "Другое"),
                photo_file_id=na.get("photo"),
                owner_user_id=uid,
                contact=base_contact,
            )
            s.add(artist)
            await s.commit()
            await s.refresh(artist)
        await state.update_data(artist_id=artist.id, created_artist_id=artist.id,
                                no_username_hint=(base_contact is None))
    if (await state.get_data()).get("artist_flow") == "standalone":
        await _finish_standalone_artist(event, state, artist.id)
        return
    await _ask_rel_type(event, state)


async def _finish_standalone_artist(event, state: FSMContext, artist_id: int):
    """Финал создания исполнителя из раздела «Исполнители» (без релиза)."""
    data = await state.get_data()
    no_username = data.get("no_username_hint")
    await state.clear()
    bot = event.bot
    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=(await get_text("releases_btn_open_card", "ru") or "🎤 Открыть карточку"),
                              callback_data=f"art:view:{artist_id}:list")],
        [InlineKeyboardButton(text=(await get_text("releases_btn_add_release_short", "ru") or "🎵 Добавить релиз"), callback_data="rel:add")],
        [InlineKeyboardButton(text=(await get_text("releases_btn_back_to_artists", "ru") or "⬅️ К исполнителям"), callback_data="go_artists"), await _menu_btn()],
    ])
    text = await get_text("releases_artist_created", "ru") or "🎉 Исполнитель создан!"
    if no_username:
        # как в других разделах: без ника контакт вписывается вручную
        text += await get_text("releases_no_username_hint", "ru") or (
            "\n\n⚠️ У вас не задан ник в Telegram, поэтому на карточке "
            "пока нет контакта. Откройте карточку → ✏️ Редактировать → "
            "Контакты и добавьте способ связи (телефон или @ник участника)."
        )
    await _send_screen(bot, chat_id, text, kb)
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
                select(Artist).where(
                    Artist.id == data["created_artist_id"],
                    Artist.owner_user_id == message.from_user.id,
                )
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
    rows.append(await _nav_row(back_cb))
    await _replace_prompt(state, bot, chat_id, await get_text("releases_ask_release_type", "ru") or "Что выпускаем?",
                          InlineKeyboardMarkup(inline_keyboard=rows))
    if isinstance(event, CallbackQuery):
        try:
            await event.answer()
        except Exception:
            pass


async def _ask_title(bot, chat_id: int, state: FSMContext):
    await state.set_state(ReleaseAdd.rel_title)
    await _replace_prompt(state, bot, chat_id,
                          await get_text("releases_ask_title", "ru") or "Название релиза?\n\n(без имени исполнителя — только название)",
                          InlineKeyboardMarkup(inline_keyboard=[await _nav_row("rel:back:rtype")]))


async def _ask_cover(bot, chat_id: int, state: FSMContext):
    await state.set_state(ReleaseAdd.cover)
    await _replace_prompt(state, bot, chat_id,
                          await get_text("releases_ask_cover", "ru") or "Обложка релиза?\n\nПришлите картинку — обложка обязательна.",
                          InlineKeyboardMarkup(inline_keyboard=[await _nav_row("rel:back:title")]))


async def _ask_media(bot, chat_id: int, state: FSMContext):
    await state.set_state(ReleaseAdd.media)
    data = await state.get_data()
    n_tracks = len(data.get("tracks") or [])
    n_links = len(data.get("links") or [])
    status = ""
    if n_tracks or data.get("video") or n_links:
        parts = []
        if n_tracks:
            parts.append((await get_text("releases_media_status_tracks_tmpl", "ru") or "треков: {n}").format(n=n_tracks))
        if data.get("video"):
            parts.append(await get_text("releases_media_status_clip", "ru") or "клип: есть")
        if n_links:
            parts.append((await get_text("releases_media_status_links_tmpl", "ru") or "ссылок: {n}").format(n=n_links))
        status = (await get_text("releases_media_status_prefix", "ru") or "\n\nУже принято — {parts}.").format(parts=", ".join(parts))
    await _replace_prompt(
        state, bot, chat_id,
        (await get_text("releases_ask_media", "ru") or (
            "Теперь сам релиз — присылайте сюда всё, что есть:\n\n"
            "🎧 аудио-треки по одному (в порядке альбома)\n"
            "🎬 видеоклип\n"
            "🔗 ссылки на площадки — YouTube, Spotify, Яндекс и др.\n\n"
            "Можно вперемешку. Когда закончите — нажмите «Готово»."
        )) + status,
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=(await get_text("releases_btn_done", "ru") or "✅ Готово"), callback_data="rel:mdone")],
            await _nav_row("rel:back:cover"),
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
    if len(tracks) >= MAX_TRACKS:
        await message.answer((await get_text("releases_max_tracks_tmpl", "ru") or "Можно прикрепить не больше {max} треков.").format(max=MAX_TRACKS))
        return
    a = message.audio
    if any(t.get("file_unique_id") == a.file_unique_id for t in tracks):
        await message.answer(await get_text("releases_track_already_added", "ru") or "Этот трек уже добавлен.")
        return
    track_default_title_tmpl = await get_text("releases_track_default_title_tmpl", "ru") or "Трек {n}"
    tracks.append({
        "file_id": a.file_id,
        "file_unique_id": a.file_unique_id,
        "title": (a.title or a.file_name or track_default_title_tmpl.format(n=len(tracks) + 1))[:255],
        "duration": a.duration,
        "file_name": a.file_name,
        "mime_type": a.mime_type,
    })
    await state.update_data(tracks=tracks)
    try:
        await message.delete()
    except Exception:
        pass
    tracks_accepted_tmpl = await get_text("releases_tracks_accepted_tmpl", "ru") or "Принято треков: {n} ✔️\n\nПрисылайте следующий или жмите «Готово»."
    await _replace_prompt(
        state, message.bot, message.chat.id,
        tracks_accepted_tmpl.format(n=len(tracks)),
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=(await get_text("releases_btn_done", "ru") or "✅ Готово"), callback_data="rel:mdone")],
            await _nav_row("rel:back:cover"),
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
        await get_text("releases_clip_accepted", "ru") or "Клип принят ✔️\n\nЖмите «Готово».",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=(await get_text("releases_btn_done", "ru") or "✅ Готово"), callback_data="rel:mdone")],
            await _nav_row("rel:back:cover"),
        ]))


@router.message(ReleaseAdd.media, F.document)
async def media_document(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass
    await _replace_prompt(
        state, message.bot, message.chat.id,
        await get_text("releases_wrong_document_type", "ru") or (
            "Файл пришёл как документ — Telegram не сможет играть его как музыку.\n"
            "Пришлите как <b>аудио</b> (через скрепку → «Музыка») или как видео."
        ),
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=(await get_text("releases_btn_done", "ru") or "✅ Готово"), callback_data="rel:mdone")],
            await _nav_row("rel:back:cover"),
        ]))


@router.message(ReleaseAdd.cover, ~F.photo)
async def cover_wrong_type(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass
    await _replace_prompt(state, message.bot, message.chat.id,
                          await get_text("releases_wrong_cover_type", "ru") or "Нужна именно картинка-обложка. Пришлите фото 🙂",
                          InlineKeyboardMarkup(inline_keyboard=[await _nav_row("rel:back:title")]))


@router.message(ReleaseAdd.media, F.text)
async def media_links(message: Message, state: FSMContext):
    """Ссылки принимаются прямо на шаге медиа — вперемешку с файлами."""
    new_links = _parse_link_text(message.text)
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=(await get_text("releases_btn_done", "ru") or "✅ Готово"), callback_data="rel:mdone")],
        await _nav_row("rel:back:cover"),
    ])
    if not new_links:
        await _replace_prompt(state, message.bot, message.chat.id,
                              await get_text("releases_not_a_link", "ru") or (
                                  "Это не похоже на ссылку (нужен адрес с http…). "
                                  "Присылайте треки, клип или ссылки — либо жмите «Готово»."
                              ), kb)
        return
    links = []
    seen: set[str] = set()
    for link in [*(data.get("links") or []), *new_links]:
        url = _normalize_http_url(str(link.get("url") or "")) if isinstance(link, dict) else None
        if not url or url in seen:
            continue
        links.append({"label": _link_label(url), "url": url})
        seen.add(url)
        if len(links) >= MAX_LINKS:
            break
    await state.update_data(links=links)
    got = ", ".join(l["label"] for l in new_links)
    link_accepted_tmpl = await get_text("releases_link_accepted_tmpl", "ru") or (
        "Ссылка принята: {label} ✔️ (всего: {count})\n\n"
        "Присылайте ещё треки/клип/ссылки или жмите «Готово»."
    )
    await _replace_prompt(
        state, message.bot, message.chat.id,
        link_accepted_tmpl.format(label=got, count=len(links)), kb)


@router.callback_query(F.data == "rel:mdone")
async def media_done(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    valid_links = _load_links(json.dumps(data.get("links") or []))
    if not (data.get("tracks") or data.get("video") or valid_links):
        await cb.answer(await get_text("releases_need_media_to_listen", "ru") or "Нужен хотя бы один трек, клип или ссылка — иначе слушать нечего 🙂",
                        show_alert=True)
        return
    await _ask_descr(cb, state)


async def _ask_descr(event, state: FSMContext):
    await state.set_state(ReleaseAdd.descr)
    bot = event.bot
    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
    await _replace_prompt(state, bot, chat_id,
                          await get_text("releases_ask_descr", "ru") or "Пара слов о релизе? (по желанию)",
                          InlineKeyboardMarkup(inline_keyboard=[
                              [InlineKeyboardButton(text=(await get_text("releases_btn_skip", "ru") or "⏭ Пропустить"), callback_data="rel:dskip")],
                              await _nav_row("rel:back:media"),
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
    artist_missing = await get_text("releases_confirm_artist_missing", "ru") or "не выбран — вернитесь в начало"
    artist_line = _e(a.name if a else artist_missing)
    tracks_plus_clip = (await get_text("releases_confirm_tracks_plus_clip", "ru") or " + клип") if data.get("video") else ""
    parts = [
        await get_text("releases_confirm_title", "ru") or "<b>Проверьте:</b>",
        (await get_text("releases_confirm_artist_line_tmpl", "ru") or "Исполнитель: {value}").format(value=artist_line),
        (await get_text("releases_confirm_type_line_tmpl", "ru") or "Тип: {value}").format(value=RELEASE_TYPES.get(data.get('rel_type'), '?')),
        (await get_text("releases_confirm_title_line_tmpl", "ru") or "Название: {value}").format(value=_e(data.get('title'))),
        (await get_text("releases_confirm_tracks_line_tmpl", "ru") or "Треков: {n}").format(n=len(data.get('tracks') or [])) + tracks_plus_clip,
        (await get_text("releases_confirm_links_line_tmpl", "ru") or "Ссылок: {n}").format(n=len(_load_links(json.dumps(data.get('links') or [])))),
    ]
    if data.get("descr"):
        parts.append((await get_text("releases_confirm_descr_line_tmpl", "ru") or "Описание: {value}").format(value=_e(data['descr'][:100])))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=(await get_text("releases_btn_confirm_publish", "ru") or "✅ Публикую — я автор или представитель"),
                              callback_data="rel:pub")],
        await _nav_row("rel:back:descr"),
    ])
    await _replace_prompt(state, bot, chat_id, "\n".join(parts), kb)
    if isinstance(event, CallbackQuery):
        try:
            await event.answer()
        except Exception:
            pass


async def _persist_release(
    data: dict,
    *,
    owner_id: int,
    username: str | None,
    city_id: int,
    category_id: int,
    links: list[dict],
    tracks: list[dict],
) -> int:
    """Атомарно создаёт listing/meta/tracks и повторно проверяет исполнителя."""
    async with SessionLocal() as s:
        artist = (await s.execute(
            select(Artist).where(
                Artist.id == data["artist_id"],
                Artist.owner_user_id == owner_id,
                Artist.status == "active",
            )
        )).scalar_one_or_none()
        if not artist:
            raise ValueError("artist unavailable")
        listing = Listing(
            city_id=city_id,
            category_id=category_id,
            owner_id=owner_id,
            title=data["title"],
            descr=data.get("descr"),
            contact=(f"@{username}" if username else "—"),
            photo_file_id=data["cover"],
            created_at=utcnow_naive(),
            type="release",
        )
        s.add(listing)
        await s.flush()
        video = data.get("video") or {}
        meta = ReleaseMeta(
            listing_id=listing.id,
            artist_id=artist.id,
            release_type=data.get("rel_type", "single"),
            release_date=utcnow_naive().strftime("%d.%m.%Y"),
            links=json.dumps(links, ensure_ascii=False),
            video_file_id=video.get("file_id"),
            video_file_unique_id=video.get("file_unique_id"),
        )
        s.add(meta)
        for position, track in enumerate(tracks, start=1):
            s.add(ReleaseTrack(
                listing_id=listing.id,
                position=position,
                title=track.get("title"),
                file_id=track["file_id"],
                file_unique_id=track.get("file_unique_id"),
                duration=track.get("duration"),
                file_name=track.get("file_name"),
                mime_type=track.get("mime_type"),
            ))
        await s.commit()
        return listing.id


@router.callback_query(F.data == "rel:pub")
async def publish(cb: CallbackQuery, state: FSMContext):
    """Сериализует финальную публикацию для защиты от двойного callback."""
    lock = _release_publish_locks.setdefault(cb.from_user.id, asyncio.Lock())
    if lock.locked():
        await cb.answer(await get_text("services_add_publishing_wait", "ru") or "Публикуем, пожалуйста, подождите.")
        return
    async with lock:
        await _publish_locked(cb, state)


async def _publish_locked(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("rel_publishing"):
        # Защита от двойного нажатия и от дубля после рестарта посреди
        # публикации (FSM теперь переживает рестарт). Не молчим: говорим,
        # как проверить результат и как выйти из этого состояния.
        await cb.answer(
            await get_text("releases_publishing_wait_2", "ru") or "Публикация уже выполнялась. Проверьте раздел «Мои релизы»: если релиза там нет — начните добавление заново.",
            show_alert=True,
        )
        return
    if not data.get("title") or not data.get("cover") or not data.get("artist_id"):
        await cb.answer(await get_text("releases_missing_data_restart", "ru") or "Не хватает данных — начните заново.", show_alert=True)
        return
    valid_links = _load_links(json.dumps(data.get("links") or []))
    tracks_data = (data.get("tracks") or [])[:MAX_TRACKS]
    if not (tracks_data or data.get("video") or valid_links):
        await cb.answer(await get_text("releases_need_one_media", "ru") or "Нужен хотя бы один трек, клип или корректная ссылка.",
                        show_alert=True)
        return
    city_id = await _release_city_id()
    if city_id is None:
        await cb.answer(await get_text("releases_no_cities_configured", "ru") or "Публикация пока невозможна: в базе не настроены города.",
                        show_alert=True)
        return
    artist = await _owned_active_artist(data["artist_id"], cb.from_user.id)
    if not artist:
        await cb.answer(await get_text("releases_artist_unavailable_restart", "ru") or "Исполнитель недоступен. Начните публикацию заново.",
                        show_alert=True)
        return
    await state.update_data(rel_publishing=True)
    await cb.answer(await get_text("releases_publishing_ellipsis", "ru") or "Публикуем…")
    try:
        cat_id = await _ensure_release_category()
        listing_id = await _persist_release(
            data,
            owner_id=cb.from_user.id,
            username=cb.from_user.username,
            city_id=city_id,
            category_id=cat_id,
            links=valid_links,
            tracks=tracks_data,
        )
    except ValueError:
        await state.update_data(rel_publishing=False)
        await _replace_prompt(
            state, cb.bot, cb.message.chat.id,
            await get_text("releases_artist_gone_restart", "ru") or "Исполнитель больше недоступен. Начните публикацию заново.",
            InlineKeyboardMarkup(inline_keyboard=[await _nav_row("go_releases")]),
        )
        return
    except Exception as e:
        print(f"[releases] publish failed: {e}")
        await state.update_data(rel_publishing=False)
        await _replace_prompt(
            state, cb.bot, cb.message.chat.id,
            await get_text("releases_publish_failed", "ru") or "Не удалось опубликовать релиз. Данные сохранены в мастере — попробуйте ещё раз.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=(await get_text("releases_btn_retry", "ru") or "🔄 Повторить"), callback_data="rel:pub")],
                await _nav_row("rel:back:descr"),
            ]),
        )
        return

    await state.clear()
    await log_event("listing_created", user_id=cb.from_user.id,
                    section="releases", entity_type="listing", entity_id=listing_id)

    # уведомление админам с кнопкой «Скрыть» (модерация задним числом)
    new_release_notify_tmpl = await get_text("releases_new_release_notify_tmpl", "ru") or "🆕 Новый релиз #{id}: {title}"
    btn_open_admin = await get_text("releases_btn_open_admin", "ru") or "👀 Открыть"
    btn_admin_hide = await get_text("releases_btn_admin_hide", "ru") or "🚫 Скрыть"
    for admin_id in await _admin_ids():
        if admin_id == cb.from_user.id:
            continue
        try:
            msg = await cb.bot.send_message(
                admin_id, new_release_notify_tmpl.format(id=listing_id, title=_e(data['title'])),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text=btn_open_admin, callback_data=f"rel:view:{listing_id}"),
                    InlineKeyboardButton(text=btn_admin_hide, callback_data=f"rel:admhide:{listing_id}"),
                ]]))
            last_bot_messages.setdefault(admin_id, []).append(msg.message_id)
            await register_bot_messages(admin_id, [msg.message_id])
        except Exception as e:
            print(f"[releases] admin notify {admin_id}: {e}")

    # показываем готовую карточку
    listing, meta, artist, tracks = await _load_release(listing_id)
    from app.routers.admin_panel import is_admin
    hint = ""
    if data.get("no_username_hint"):
        hint = await get_text("releases_published_hint_tmpl", "ru") or (
            "\n\n⚠️ У вас нет ника в Telegram — добавьте контакт на карточку "
            "исполнителя: Об исполнителе → ✏️ Редактировать → Контакты."
        )
    caption = _fit_html_lines([
        (await get_text("releases_published_title", "ru") or "🎉 Опубликовано!") + hint,
        "",
        *(await _release_caption(listing, meta, artist, tracks)).splitlines(),
    ])
    links_pub = _load_links(meta.links if meta else None)
    yt = _youtube_url(links_pub)
    yt_btn = await _release_yt_button(yt, listing.id) if yt else None
    kb = await _release_kb(listing, meta, tracks, artist=artist,
                     viewer_id=cb.from_user.id, is_admin_user=is_admin(cb.from_user.id),
                     yt_btn=yt_btn, yt_url=yt)
    await _send_screen(cb.bot, cb.message.chat.id, caption, kb, photo=listing.photo_file_id)


# ─────────────────────── поиск (fuzzy, как в вакансиях) ───────────────────────
from app.search.fuzzy import search_items          # noqa: E402
from app.analytics.search_log import log_search    # noqa: E402

SEARCH_PAGE = 10


class RelSearch(StatesGroup):
    waiting_query = State()


@router.callback_query(F.data == "rel:noop")
async def rel_noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data == "rel:search")
async def rel_search_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(RelSearch.waiting_query)
    await clear_bot_messages(cb.message.chat.id, cb.bot)
    await _replace_prompt(state, cb.bot, cb.message.chat.id,
                          await get_text("releases_search_ask", "ru") or "🔍 Введите запрос: название релиза или исполнителя (от 2 символов).",
                          InlineKeyboardMarkup(inline_keyboard=[await _nav_row("go_releases")]))


async def _render_rel_search(bot, chat_id: int, state: FSMContext, offset: int = 0):
    data = await state.get_data()
    results = data.get("rel_s_results") or []   # [(listing_id, label), ...]
    q = data.get("rel_s_query") or ""
    note = data.get("rel_s_note") or ""
    total = len(results)
    pages = max(1, (total + SEARCH_PAGE - 1) // SEARCH_PAGE)
    page = results[offset:offset + SEARCH_PAGE]

    await state.update_data(rel_s_offset=offset)
    rows = [[InlineKeyboardButton(text=label, callback_data=f"rel:view:{lid}:s")]
            for lid, label in page]
    if pages > 1:
        nav = []
        if offset > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"rel:spage:{max(0, offset - SEARCH_PAGE)}"))
        nav.append(InlineKeyboardButton(text=f"{offset // SEARCH_PAGE + 1}/{pages}", callback_data="rel:noop"))
        if offset + SEARCH_PAGE < total:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"rel:spage:{offset + SEARCH_PAGE}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton(text=(await get_text("btn_new_search", "ru") or "🔄 Новый поиск"), callback_data="rel:search")])
    rows.append(await _nav_row("go_releases"))
    search_results_tmpl = await get_text("releases_search_results_tmpl", "ru") or "{note}Результаты по запросу: <b>{query}</b>\nНайдено: {total}"
    await _send_screen(bot, chat_id,
                       search_results_tmpl.format(note=note, query=_e(q), total=total),
                       InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(RelSearch.waiting_query, F.text)
async def rel_search_do(message: Message, state: FSMContext):
    q = (message.text or "").strip()[:128]
    try:
        await message.delete()
    except Exception:
        pass
    if len(q) < 2:
        await _replace_prompt(state, message.bot, message.chat.id,
                              await get_text("releases_search_min_2_chars", "ru") or "Минимум 2 символа. Введите запрос ещё раз:",
                              InlineKeyboardMarkup(inline_keyboard=[await _nav_row("go_releases")]))
        return

    # собираем опубликованные релизы с именами исполнителей
    items: list[tuple[int, str, list[str]]] = []
    async with SessionLocal() as s:
        metas = (await s.execute(
            select(ReleaseMeta).where(ReleaseMeta.status == "published")
            .order_by(ReleaseMeta.created_at.desc()).limit(1000)
        )).scalars().all()
        for m in metas:
            listing = (await s.execute(
                select(Listing).where(
                    Listing.id == m.listing_id,
                    Listing.type == "release",
                    Listing.status == "active",
                )
            )).scalar_one_or_none()
            artist = (await s.execute(
                select(Artist).where(Artist.id == m.artist_id, Artist.status == "active")
            )).scalar_one_or_none()
            if not listing or not artist or listing.owner_id != artist.owner_user_id:
                continue
            tracks = (await s.execute(
                select(ReleaseTrack).where(ReleaseTrack.listing_id == listing.id)
            )).scalars().all()
            if not _release_is_public(listing, m, artist, tracks):
                continue
            a_name = artist.name if artist else ""
            items.append((
                listing.id,
                f"🎵 {a_name} — {listing.title}",
                [listing.title or "", a_name, m.genre or "", listing.descr or ""],
            ))

    outcome = search_items(items, q, lambda it: it[2])
    await log_search(user_id=message.from_user.id, section="releases",
                     query_raw=outcome.query_raw,
                     query_normalized=outcome.query_normalized,
                     query_effective=outcome.query_effective,
                     match_mode=outcome.match_mode,
                     results_count=len(outcome.results))
    note = ""
    if outcome.match_mode == "corrected" and outcome.query_effective != outcome.query_normalized:
        correction_note_tmpl = await get_text("search_typo_correction_note", "ru") or "🧠 Показаны результаты по запросу: <b>{query}</b> (учтена возможная опечатка).\n\n"
        note = correction_note_tmpl.format(query=_e(outcome.query_effective))
    await state.update_data(
        rel_s_results=[(it[0], it[1]) for it in outcome.results],
        rel_s_query=q, rel_s_note=note,
    )
    await _render_rel_search(message.bot, message.chat.id, state, 0)


@router.callback_query(F.data.startswith("rel:spage:"))
async def rel_search_page(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    offset = max(0, int(cb.data.split(":")[2]))
    await _render_rel_search(cb.bot, cb.message.chat.id, state, offset)


@router.callback_query(F.data == "rel:sback")
async def rel_search_back(cb: CallbackQuery, state: FSMContext):
    """С карточки — назад к результатам поиска (та же страница)."""
    data = await state.get_data()
    if not data.get("rel_s_results"):
        # результатов в состоянии нет (например, после рестарта) — в ленту
        await releases_feed(cb, state)  # ответит на callback сам
        return
    await cb.answer()
    await _render_rel_search(cb.bot, cb.message.chat.id, state,
                             data.get("rel_s_offset") or 0)


# ─────────────────── редактирование релиза (по образцу услуг) ───────────────────

REL_EDIT_FIELDS_FALLBACK = {
    "title": ("Название", "Новое название релиза?"),
    "rtype": ("Тип", None),  # кнопками
    "cover": ("Обложка", "Пришлите новую обложку (картинку)."),
    "descr": ("Описание", "Пара слов о релизе:"),
    "genre": ("Жанр", "Укажите жанр (до 64 символов):"),
    "recorded_at": ("Где записано", "Студия или место записи (до 128 символов):"),
    "links": ("Ссылки", "Ссылки на площадки одним сообщением —\n"
                        "каждая с новой строки или через пробел:"),
    "video": ("Клип", "Пришлите новый видеоклип файлом."),
}
REL_CLEARABLE = {"descr", "genre", "recorded_at", "links", "video"}


async def _rel_edit_fields() -> dict:
    """Подписи и подсказки полей редактирования релиза (текст из BotText)."""
    return {
        "title": (
            await get_text("releases_edit_title_label", "ru") or "Название",
            await get_text("releases_edit_title_hint", "ru") or "Новое название релиза?",
        ),
        "rtype": (await get_text("releases_edit_type_label", "ru") or "Тип", None),
        "cover": (
            await get_text("releases_edit_cover_label", "ru") or "Обложка",
            await get_text("releases_edit_cover_hint", "ru") or "Пришлите новую обложку (картинку).",
        ),
        "descr": (
            await get_text("releases_edit_descr_label", "ru") or "Описание",
            await get_text("releases_edit_descr_hint", "ru") or "Пара слов о релизе:",
        ),
        "genre": (
            await get_text("releases_edit_genre_label", "ru") or "Жанр",
            await get_text("releases_edit_genre_hint", "ru") or "Укажите жанр (до 64 символов):",
        ),
        "recorded_at": (
            await get_text("releases_edit_recorded_at_label", "ru") or "Где записано",
            await get_text("releases_edit_recorded_at_hint", "ru") or "Студия или место записи (до 128 символов):",
        ),
        "links": (
            await get_text("releases_edit_links_label", "ru") or "Ссылки",
            await get_text("releases_edit_links_hint", "ru") or "Ссылки на площадки одним сообщением —\nкаждая с новой строки или через пробел:",
        ),
        "video": (
            await get_text("releases_edit_video_label", "ru") or "Клип",
            await get_text("releases_edit_video_hint", "ru") or "Пришлите новый видеоклип файлом.",
        ),
    }


class RelEdit(StatesGroup):
    value = State()


async def _can_edit_release(user_id: int, listing, meta, artist) -> bool:
    from app.routers.admin_panel import is_admin
    return bool(
        listing
        and listing.type == "release"
        and listing.status == "active"
        and meta
        and meta.status != "deleted"
        and artist
        and artist.id == meta.artist_id
        and artist.owner_user_id == listing.owner_id
        and (listing.owner_id == user_id or is_admin(user_id))
    )


async def _render_rel_edit(bot, chat_id: int, user_id: int, listing_id: int):
    listing, meta, artist, tracks = await _load_release(listing_id)
    if not listing or not meta or not await _can_edit_release(user_id, listing, meta, artist):
        return
    def short(v, n=40):
        return (str(v)[:n] + "…") if v and len(str(v)) > n else (v or "—")
    try:
        n_links = len(_load_links(meta.links if meta else None))
    except Exception:
        n_links = 0
    present = await get_text("releases_present_marker", "ru") or "есть"
    dash = await get_text("releases_dash_marker", "ru") or "—"
    screen_title_tmpl = await get_text("releases_edit_screen_title_tmpl", "ru") or "✏️ <b>Релиз: {title}</b>"
    artist_line_tmpl = await get_text("releases_edit_artist_line_tmpl", "ru") or "Исполнитель: {name} (меняется только пересозданием)"
    lines = [
        screen_title_tmpl.format(title=_e(listing.title)),
        artist_line_tmpl.format(name=_e(artist.name if artist else dash)),
        "",
        (await get_text("releases_edit_field_title_tmpl", "ru") or "Название: {value}").format(value=_e(listing.title)),
        (await get_text("releases_edit_field_type_tmpl", "ru") or "Тип: {value}").format(value=_e(RELEASE_TYPES.get(meta.release_type, dash))),
        (await get_text("releases_edit_field_cover_tmpl", "ru") or "Обложка: {value}").format(value=present if listing.photo_file_id else dash),
        (await get_text("releases_edit_field_descr_tmpl", "ru") or "Описание: {value}").format(value=_e(short(listing.descr))),
        (await get_text("releases_edit_field_genre_tmpl", "ru") or "Жанр: {value}").format(value=_e(short(meta.genre))),
        (await get_text("releases_edit_field_recorded_tmpl", "ru") or "Где записано: {value}").format(value=_e(short(meta.recorded_at))),
        (await get_text("releases_edit_field_links_tmpl", "ru") or "Ссылки: {value}").format(value=n_links or dash),
        (await get_text("releases_edit_field_video_tmpl", "ru") or "Клип: {value}").format(value=present if meta and meta.video_file_id else dash),
        (await get_text("releases_edit_field_tracks_tmpl", "ru") or "Треки: {value}").format(value=len(tracks) or dash),
    ]
    btn_edit_field_tmpl = await get_text("releases_btn_edit_field_tmpl", "ru") or "✏️ Править: {label}"
    rel_edit_fields = await _rel_edit_fields()
    rows = [[InlineKeyboardButton(text=btn_edit_field_tmpl.format(label=label),
                                  callback_data=f"rel:ref:{code}:{listing_id}")]
            for code, (label, _) in rel_edit_fields.items()]
    btn_tracks_tmpl = await get_text("releases_btn_tracks_count_tmpl", "ru") or "🎼 Треки ({n})"
    rows.append([InlineKeyboardButton(text=btn_tracks_tmpl.format(n=len(tracks)),
                                      callback_data=f"rel:rtracks:{listing_id}")])
    rows.append(await _nav_row(f"rel:view:{listing_id}"))
    await _send_screen(bot, chat_id, "\n".join(lines),
                       InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("rel:edit:"))
async def rel_edit(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await _render_rel_edit(cb.bot, cb.message.chat.id, cb.from_user.id,
                           int(cb.data.split(":")[2]))


@router.callback_query(F.data.startswith("rel:ref:"))
async def rel_edit_field(cb: CallbackQuery, state: FSMContext):
    _, _, field, lid = cb.data.split(":")
    listing_id = int(lid)
    rel_edit_fields = await _rel_edit_fields()
    if field not in rel_edit_fields:
        await cb.answer(await get_text("releases_unknown_field", "ru") or "Неизвестное поле.", show_alert=True)
        return
    listing, meta, artist, tracks = await _load_release(listing_id)
    if not listing or not meta or not await _can_edit_release(
        cb.from_user.id, listing, meta, artist
    ):
        await cb.answer(await get_text("releases_no_rights_or_unavailable", "ru") or "Нет прав или релиз недоступен.", show_alert=True)
        return
    await cb.answer()
    if field == "rtype":
        rows = [[InlineKeyboardButton(text=label, callback_data=f"rel:retype:{listing_id}:{code}")]
                for code, label in RELEASE_TYPES.items()]
        rows.append(await _nav_row(f"rel:edit:{listing_id}"))
        await _replace_prompt(state, cb.bot, cb.message.chat.id, await get_text("releases_choose_type", "ru") or "Выберите тип:",
                              InlineKeyboardMarkup(inline_keyboard=rows))
        return
    await state.set_state(RelEdit.value)
    await state.update_data(redit_field=field, redit_listing_id=listing_id)
    label, hint = rel_edit_fields[field]
    rows = []
    if field in REL_CLEARABLE:
        rows.append([InlineKeyboardButton(text=(await get_text("releases_btn_clear_field", "ru") or "🗑 Очистить поле"),
                                          callback_data=f"rel:reclr:{listing_id}:{field}")])
    rows.append(await _nav_row(f"rel:edit:{listing_id}"))
    await _replace_prompt(state, cb.bot, cb.message.chat.id, hint,
                          InlineKeyboardMarkup(inline_keyboard=rows))


async def _save_release_field(user_id: int, listing_id: int, field: str, value) -> bool:
    async with SessionLocal() as s:
        listing = (await s.execute(
            select(Listing).where(Listing.id == listing_id, Listing.type == "release")
        )).scalar_one_or_none()
        meta = (await s.execute(
            select(ReleaseMeta).where(ReleaseMeta.listing_id == listing_id)
        )).scalar_one_or_none()
        artist = (await s.execute(
            select(Artist).where(Artist.id == meta.artist_id)
        )).scalar_one_or_none() if meta else None
        if (
            not listing
            or not meta
            or not artist
            or artist.owner_user_id != listing.owner_id
            or not await _can_edit_release(user_id, listing, meta, artist)
        ):
            return False
        if field not in {"title", "descr", "cover", "rtype", "genre", "recorded_at",
                         "links", "video"}:
            return False
        if field in {"links", "video"} and not value:
            tracks = (await s.execute(
                select(ReleaseTrack).where(ReleaseTrack.listing_id == listing_id)
            )).scalars().all()
            other_video = meta.video_file_id if field == "links" else None
            other_links = _load_links(meta.links) if field == "video" else []
            if not (tracks or other_video or other_links):
                return False
        if field == "title":
            listing.title = value
            s.add(listing)
        elif field == "descr":
            listing.descr = value
            s.add(listing)
        elif field == "cover":
            listing.photo_file_id = value
            s.add(listing)
        elif field == "rtype" and meta:
            meta.release_type = value
            s.add(meta)
        elif field == "genre" and meta:
            meta.genre = value
            s.add(meta)
        elif field == "recorded_at" and meta:
            meta.recorded_at = value
            s.add(meta)
        elif field == "links" and meta:
            meta.links = value
            s.add(meta)
        elif field == "video" and meta:
            meta.video_file_id = (value or {}).get("file_id") if value else None
            meta.video_file_unique_id = (value or {}).get("file_unique_id") if value else None
            s.add(meta)
        await s.commit()
    return True


@router.callback_query(F.data.startswith("rel:retype:"))
async def rel_edit_type(cb: CallbackQuery, state: FSMContext):
    _, _, lid, code = cb.data.split(":")
    saved = code in RELEASE_TYPES and await _save_release_field(
        cb.from_user.id, int(lid), "rtype", code
    )
    if not saved:
        await cb.answer(await get_text("releases_no_rights_or_unavailable", "ru") or "Нет прав или релиз недоступен.", show_alert=True)
        return
    await cb.answer()
    await _render_rel_edit(cb.bot, cb.message.chat.id, cb.from_user.id, int(lid))


@router.callback_query(F.data.startswith("rel:reclr:"))
async def rel_edit_clear(cb: CallbackQuery, state: FSMContext):
    _, _, lid, field = cb.data.split(":")
    saved = False
    if field in REL_CLEARABLE:
        saved = await _save_release_field(cb.from_user.id, int(lid), field, None)
    if not saved:
        text = (
            await get_text("releases_clear_field_needs_media", "ru") or "Нельзя удалить последний источник: добавьте трек, клип или ссылку."
            if field in {"links", "video"}
            else await get_text("releases_no_rights_or_unavailable", "ru") or "Нет прав или релиз недоступен."
        )
        await cb.answer(text, show_alert=True)
        return
    await cb.answer(await get_text("music_field_cleared", "ru") or "Очищено.")
    await state.clear()
    await _render_rel_edit(cb.bot, cb.message.chat.id, cb.from_user.id, int(lid))


@router.message(RelEdit.value, F.text)
async def rel_edit_text(message: Message, state: FSMContext):
    data = await state.get_data()
    field, listing_id = data.get("redit_field"), data.get("redit_listing_id")
    text_val = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    if not field or not listing_id:
        # это может быть добавление трека — обрабатывается ниже по field
        return
    if field in ("cover", "video", "track"):
        return  # ждём файл, не текст
    if field == "title":
        value = text_val[:200]
        if not value:
            return
    elif field == "links":
        links = _parse_link_text(text_val)
        if not links:
            await message.answer(await get_text("music_link_needs_scheme", "ru") or "Нужна полноценная ссылка с http:// или https://.")
            return
        value = json.dumps(links, ensure_ascii=False)
    elif field == "genre":
        value = text_val[:64] or None
    elif field == "recorded_at":
        value = text_val[:128] or None
    else:
        value = text_val[:600] or None
    if not await _save_release_field(message.from_user.id, listing_id, field, value):
        await message.answer(await get_text("releases_field_no_rights_dot", "ru") or "Не удалось сохранить: нет прав или релиз недоступен.")
        return
    await state.clear()
    await _render_rel_edit(message.bot, message.chat.id, message.from_user.id, listing_id)


@router.message(RelEdit.value, F.photo)
async def rel_edit_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    field, listing_id = data.get("redit_field"), data.get("redit_listing_id")
    try:
        await message.delete()
    except Exception:
        pass
    if field != "cover" or not listing_id:
        return
    if not await _save_release_field(message.from_user.id, listing_id, "cover",
                                     message.photo[-1].file_id):
        await message.answer(await get_text("releases_field_no_rights_dot", "ru") or "Не удалось сохранить: нет прав или релиз недоступен.")
        return
    await state.clear()
    await _render_rel_edit(message.bot, message.chat.id, message.from_user.id, listing_id)


@router.message(RelEdit.value, F.video)
async def rel_edit_video(message: Message, state: FSMContext):
    data = await state.get_data()
    field, listing_id = data.get("redit_field"), data.get("redit_listing_id")
    try:
        await message.delete()
    except Exception:
        pass
    if field != "video" or not listing_id:
        return
    if not await _save_release_field(
        message.from_user.id, listing_id, "video",
        {"file_id": message.video.file_id,
         "file_unique_id": message.video.file_unique_id},
    ):
        await message.answer(await get_text("releases_field_no_rights_dot", "ru") or "Не удалось сохранить: нет прав или релиз недоступен.")
        return
    await state.clear()
    await _render_rel_edit(message.bot, message.chat.id, message.from_user.id, listing_id)


# ─── треки: список с удалением, добавление новых ───

async def _render_rel_tracks(bot, chat_id: int, user_id: int, listing_id: int):
    listing, meta, artist, tracks = await _load_release(listing_id)
    if not listing or not meta or not await _can_edit_release(user_id, listing, meta, artist):
        return
    track_default_title_tmpl = await get_text("releases_track_default_title_tmpl", "ru") or "Трек {n}"
    rows = []
    for t in tracks:
        rows.append([
            InlineKeyboardButton(text=f"{t.position}. {t.title or track_default_title_tmpl.format(n=t.position)}",
                                 callback_data="rel:noop"),
            InlineKeyboardButton(text="🗑", callback_data=f"rel:tdel:{t.id}"),
        ])
    rows.append([InlineKeyboardButton(text=(await get_text("releases_btn_add_track", "ru") or "➕ Добавить трек"),
                                      callback_data=f"rel:tadd:{listing_id}")])
    rows.append(await _nav_row(f"rel:edit:{listing_id}"))
    tracks_screen_title_tmpl = await get_text("releases_tracks_screen_title_tmpl", "ru") or (
        "🎼 <b>Треки релиза «{title}»</b>\n\n🗑 удаляет трек; порядок пересчитывается автоматически."
    )
    await _send_screen(bot, chat_id,
                       tracks_screen_title_tmpl.format(title=_e(listing.title)),
                       InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("rel:rtracks:"))
async def rel_tracks(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await _render_rel_tracks(cb.bot, cb.message.chat.id, cb.from_user.id,
                             int(cb.data.split(":")[2]))


@router.callback_query(F.data.startswith("rel:tdel:"))
async def rel_track_delete(cb: CallbackQuery, state: FSMContext):
    track_id = int(cb.data.split(":")[2])
    async with SessionLocal() as s:
        t = (await s.execute(
            select(ReleaseTrack).where(ReleaseTrack.id == track_id)
        )).scalar_one_or_none()
        if not t:
            await cb.answer(await get_text("releases_track_not_found", "ru") or "Трек не найден.", show_alert=True)
            return
        listing = (await s.execute(
            select(Listing).where(Listing.id == t.listing_id, Listing.type == "release")
        )).scalar_one_or_none()
        meta = (await s.execute(
            select(ReleaseMeta).where(ReleaseMeta.listing_id == t.listing_id)
        )).scalar_one_or_none()
        artist = (await s.execute(
            select(Artist).where(Artist.id == meta.artist_id)
        )).scalar_one_or_none() if meta else None
        if not await _can_edit_release(cb.from_user.id, listing, meta, artist):
            await cb.answer(await get_text("releases_no_rights_short", "ru") or "Нет прав.", show_alert=True)
            return
        listing_id = t.listing_id
        all_tracks = (await s.execute(
            select(ReleaseTrack).where(ReleaseTrack.listing_id == listing_id)
            .order_by(ReleaseTrack.position)
        )).scalars().all()
        if len(all_tracks) == 1 and not (
            meta.video_file_id or _load_links(meta.links)
        ):
            await cb.answer(
                await get_text("releases_cannot_delete_last_source_track", "ru") or "Нельзя удалить последний источник: сначала добавьте клип или ссылку.",
                show_alert=True,
            )
            return
        await s.delete(t)
        await s.flush()
        # пересчёт позиций
        rest = (await s.execute(
            select(ReleaseTrack).where(ReleaseTrack.listing_id == listing_id)
            .order_by(ReleaseTrack.position)
        )).scalars().all()
        for i, tr in enumerate(rest, start=1):
            tr.position = i
            s.add(tr)
        await s.commit()
    await cb.answer(await get_text("releases_track_deleted", "ru") or "Трек удалён.")
    await _render_rel_tracks(cb.bot, cb.message.chat.id, cb.from_user.id, listing_id)


@router.callback_query(F.data.startswith("rel:tadd:"))
async def rel_track_add(cb: CallbackQuery, state: FSMContext):
    listing_id = int(cb.data.split(":")[2])
    listing, meta, artist, tracks = await _load_release(listing_id)
    if not listing or not meta or not await _can_edit_release(
        cb.from_user.id, listing, meta, artist
    ):
        await cb.answer(await get_text("releases_no_rights_or_unavailable", "ru") or "Нет прав или релиз недоступен.", show_alert=True)
        return
    if len(tracks) >= MAX_TRACKS:
        await cb.answer((await get_text("releases_max_tracks_tmpl", "ru") or "Можно прикрепить не больше {max} треков.").format(max=MAX_TRACKS), show_alert=True)
        return
    await cb.answer()
    await state.set_state(RelEdit.value)
    await state.update_data(redit_field="track", redit_listing_id=listing_id)
    await _replace_prompt(state, cb.bot, cb.message.chat.id,
                          await get_text("releases_ask_track_audio", "ru") or "Пришлите аудио-трек (как музыку).",
                          InlineKeyboardMarkup(inline_keyboard=[
                              await _nav_row(f"rel:rtracks:{listing_id}")]))


@router.message(RelEdit.value, F.audio)
async def rel_track_add_audio(message: Message, state: FSMContext):
    data = await state.get_data()
    field, listing_id = data.get("redit_field"), data.get("redit_listing_id")
    try:
        await message.delete()
    except Exception:
        pass
    if field != "track" or not listing_id:
        return
    async with SessionLocal() as s:
        listing = (await s.execute(
            select(Listing).where(Listing.id == listing_id, Listing.type == "release")
        )).scalar_one_or_none()
        meta = (await s.execute(
            select(ReleaseMeta).where(ReleaseMeta.listing_id == listing_id)
        )).scalar_one_or_none()
        artist = (await s.execute(
            select(Artist).where(Artist.id == meta.artist_id)
        )).scalar_one_or_none() if meta else None
        if not await _can_edit_release(message.from_user.id, listing, meta, artist):
            await message.answer(await get_text("releases_no_rights_or_unavailable_msg", "ru") or "Нет прав или релиз недоступен.")
            return
        tracks = (await s.execute(
            select(ReleaseTrack).where(ReleaseTrack.listing_id == listing_id)
        )).scalars().all()
        a = message.audio
        if len(tracks) >= MAX_TRACKS:
            await message.answer((await get_text("releases_max_tracks_tmpl", "ru") or "Можно прикрепить не больше {max} треков.").format(max=MAX_TRACKS))
            return
        if any(t.file_unique_id == a.file_unique_id for t in tracks):
            await message.answer(await get_text("releases_track_already_added", "ru") or "Этот трек уже добавлен.")
            return
        track_default_title_tmpl = await get_text("releases_track_default_title_tmpl", "ru") or "Трек {n}"
        s.add(ReleaseTrack(
            listing_id=listing_id, position=len(tracks) + 1,
            title=(a.title or a.file_name or track_default_title_tmpl.format(n=len(tracks) + 1))[:255],
            file_id=a.file_id, file_unique_id=a.file_unique_id,
            duration=a.duration, file_name=a.file_name, mime_type=a.mime_type,
        ))
        await s.commit()
    await state.clear()
    await _render_rel_tracks(message.bot, message.chat.id, message.from_user.id, listing_id)
