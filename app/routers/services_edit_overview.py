# app/routers/services_edit_overview.py
# -----------------------------------------------------------------------------
# Редактирование УСЛУГ (type='service') «как в Барахолке»:
# Крошки (город/категория) → список текущих значений (title/price/descr + flex)
# → ровные кнопки «Править …». Ввод одним сообщением → мгновенная запись →
# возврат к обзору. Каноны: RU-комменты, зачистка, подробный print.
# -----------------------------------------------------------------------------

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from sqlalchemy import select
import json
import re
from html import escape as html_escape
from urllib.parse import urlsplit

from app.database import SessionLocal
from app.models import Listing, City, Category
from app.routers.utils import clear_bot_messages, last_bot_messages, register_bot_messages, get_text
from app.keyboards import get_common_menu_button

from collections import defaultdict
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
_user_input_msgs = defaultdict(list)


async def _back_row(callback_data: str) -> list[InlineKeyboardButton]:
    """Строка «Назад» из одной кнопки (общий хелпер, текст берётся из menu)."""
    back_btn = await get_common_menu_button('back') or InlineKeyboardButton(text="⬅️ Назад", callback_data=callback_data)
    back_btn.callback_data = callback_data
    return [back_btn]

async def _remember_and_delete_user_message(msg: Message):
    # RU: Запоминаем и удаляем пользовательское сообщение (текст/фото/видео/документ)
    try:
        _user_input_msgs[msg.chat.id].append(msg.message_id)
    except Exception:
        pass
    try:
        await msg.delete()
    except Exception:
        pass

async def _clear_user_inputs(chat_id: int, bot):
    # RU: На всякий случай чистим хвосты пользовательских сообщений
    ids = _user_input_msgs.pop(chat_id, [])
    for mid in ids:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass

# >>> BEGIN: extras helpers
def _allow_extra_for_category(cat: Category) -> bool:
    """RU: Разрешены ли доп. категории для этой категории (читаем из Category.fields)."""
    try:
        raw = (cat.fields or "").strip()
        if not raw:
            return False
        data = json.loads(raw)
        if isinstance(data, list):
            for f in data:
                if isinstance(f, dict) and f.get("type") == "__meta" and f.get("key") == "allow_extra_categories":
                    return bool(f.get("value"))
            return False
        if isinstance(data, dict):
            return bool(data.get("allow_extra_categories"))
        return False
    except Exception:
        return False

def _extra_used(lst: Listing) -> int:
    """RU: Сколько слотов доп. категорий занято у объявления (0..2)."""
    return int(bool(lst.extra_category_id1)) + int(bool(lst.extra_category_id2))
# <<< END: extras helpers


router = Router(name="services_edit_overview")

# # >>> BEGIN: extras callback stub
# @router.callback_query(F.data.startswith("extra:s_open:"))
# async def _open_extra_categories_menu(c: CallbackQuery):
#     # Пока просто подтверждаем клик. Меню добавим на следующем шаге.
#     await c.answer("Меню доп. категорий покажем на следующем шаге.", show_alert=True)
# # <<< END: extras callback stub


# from app.routers.services_view import _send_yt_button as _sv_send_yt_button, WEBAPP_BASE

from collections import defaultdict

# Хранилище msg_id хвостовых сообщений (кнопка «Смотреть видео» / видео file_id)
# _edit_tail_msgs = defaultdict(list)



VERSION_TAG = "SVC-EDIT-KRUMBS v2.1"  # видимая метка для подтверждения
def _fmt(v):
    """Вернуть красивое текстовое представление значения."""
    if v is None or v == "" or (isinstance(v, (list, dict)) and not v):
        return "<i>—</i>"
    if isinstance(v, list):
        return f"<i>{html_escape(', '.join(map(str, v)))}</i>"
    if isinstance(v, dict):
        return f"<i>{html_escape(json.dumps(v, ensure_ascii=False))}</i>"
    return f"<i>{html_escape(str(v))}</i>"


async def _owned_service_in_session(s, listing_id: int, user_id: int) -> Listing | None:
    """Вернуть только принадлежащую пользователю запись типа service."""
    return (await s.execute(
        select(Listing).where(
            Listing.id == listing_id,
            Listing.owner_id == user_id,
            Listing.type == "service",
        )
    )).scalar_one_or_none()


async def _authorize_service_callback(cb: CallbackQuery, listing_id: int) -> bool:
    async with SessionLocal() as s:
        listing = await _owned_service_in_session(s, listing_id, cb.from_user.id)
    if listing is None:
        await cb.answer(await get_text("err_not_owner_service", "ru") or "Можно редактировать только свои услуги.", show_alert=True)
        return False
    return True


async def _service_extra_field_def(s, listing: Listing, key: str) -> dict | None:
    category = await s.get(Category, listing.category_id)
    try:
        defs = json.loads((category.fields or "[]") if category else "[]")
    except Exception:
        defs = []
    if not isinstance(defs, list):
        return None
    return next((
        field for field in defs if isinstance(field, dict)
        and str(field.get("key", "")).strip().lower() == key
    ), None)


async def _owned_service_extra_field(
    s,
    listing_id: int,
    user_id: int,
    key: str,
    expected_type: str,
) -> Listing | None:
    listing = await _owned_service_in_session(s, listing_id, user_id)
    if listing is None:
        return None
    fdef = await _service_extra_field_def(s, listing, key)
    if str((fdef or {}).get("type", "")).strip().lower() != expected_type:
        return None
    return listing


def _valid_http_url(value: str) -> bool:
    raw = value.strip()
    if not raw or any(ord(ch) < 33 for ch in raw) or any(ch in raw for ch in {'"', "'", "<", ">", "\\"}):
        return False
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return False
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
    )

# Короткое RU-пояснение: нормализовать ввод согласно типу flex-поля.
def _normalize_value_for_type(raw, ftype: str):
    """Мягко нормализовать ввод под тип поля."""
    t = (ftype or "text").strip().lower()
    s = "" if raw is None else str(raw).strip()

    if t in ("text", "textarea", "select"):
        return s

    if t == "number":
        try:
            x = s.replace(",", ".")
            return float(x) if "." in x else int(x)
        except Exception:
            return s

    if t == "checkbox":
        return s.lower() in ("1", "true", "да", "yes", "y", "+", "✅", "☑️")

    if t == "multiselect":
        return [p.strip() for p in s.replace("\n", ",").split(",") if p.strip()]

    if t == "video":
        # В услугах видео — ОДНА строка: либо URL, либо file_id.
        return s

    return s

# Короткое RU-пояснение: загрузить Listing/City/Category и схему flex (Category.fields) и значения (listing.flex).
async def _load_listing_bundle(listing_id: int):
    """Загрузить Listing, City, Category, defs flex (Category.fields) и текущие flex-значения (listing.flex)."""
    async with SessionLocal() as s:
        listing = (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one()
        city    = (await s.execute(select(City).where(City.id == listing.city_id))).scalar_one_or_none()
        cat     = (await s.execute(select(Category).where(Category.id == listing.category_id))).scalar_one_or_none()

        # Схема полей для Услуг хранится в Category.fields (а не extra_fields)
        defs = []
        if cat and getattr(cat, "fields", None):
            try:
                defs = json.loads(cat.fields)
                if not isinstance(defs, list):
                    defs = []
            except Exception:
                defs = []

        # Текущие значения flex хранятся в listing.flex (а не extra)
        flex_vals = {}
        if getattr(listing, "flex", None):
            try:
                flex_vals = json.loads(listing.flex)
                if not isinstance(flex_vals, dict):
                    flex_vals = {}
            except Exception:
                flex_vals = {}

    return listing, city, cat, defs, flex_vals

# Короткое RU-пояснение: общий диагностический print.
def _pp(file, fn, chat_id=None, user_id=None, listing_id=None, field=None, msg_id=None, extra=None):
    """Распечатать диагностическую строку с максимумом атрибутов."""
    tail = []
    if chat_id is not None:   tail.append(f"chat_id={chat_id}")
    if user_id is not None:   tail.append(f"user_id={user_id}")
    if listing_id is not None:tail.append(f"listing_id={listing_id}")
    if field is not None:     tail.append(f"field={field}")
    if msg_id is not None:    tail.append(f"msg_id={msg_id}")
    if extra is not None:     tail.append(f"extra={extra}")
    print(f"[{file}] {fn} ✓ | " + " ".join(tail))

# -----------------------------------------------------------------------------
# Рендер обзора «как в Барахолке»
# -----------------------------------------------------------------------------

# Короткое RU-пояснение: сформировать текст обзора (крошки + текущие значения).
async def _build_overview_text(listing: Listing, city: City | None, cat: Category | None, defs: list[dict], flex_vals: dict):
    """Вернуть текст «шапки» + список текущих значений всех полей (title/descr/price + flex), с доп. отступами."""
    overview_title = await get_text("services_edit_overview_title", "ru") or "🛠️ <b>Редактирование объявления</b>"
    city_line_tmpl = await get_text("services_edit_overview_city_line_tmpl", "ru") or "Город: {name}"
    category_line_tmpl = await get_text("services_edit_overview_category_line_tmpl", "ru") or "Категория: {name}"
    title_label = await get_text("services_edit_field_title_label", "ru") or "Заголовок:"
    descr_label = await get_text("services_edit_field_descr_label", "ru") or "Описание:"
    price_label = await get_text("services_edit_field_price_label", "ru") or "Стоимость услуг:"
    video_added = await get_text("services_edit_video_added_indicator", "ru") or "добавлено"

    lines = []
    lines.append(overview_title)
    if city: lines.append(city_line_tmpl.format(name=html_escape(city.name or '')))
    if cat:  lines.append(category_line_tmpl.format(name=html_escape(cat.name or '')))
    lines.append("")

    # Порядок: Заголовок → Описание → Стоимость услуг
    lines.append(f"<b>{title_label}</b> { _fmt(listing.title) }")
    lines.append("")
    lines.append(f"<b>{descr_label}</b> { _fmt(getattr(listing, 'descr', None)) }")
    lines.append("")
    lines.append(f"<b>{price_label}</b> { _fmt(listing.price) }")
    lines.append("")

    # Flex — с отступами; для видео печатаем URL, если это ссылка
    for fdef in defs:
        key   = (str(fdef.get("key","")).strip().lower() or "field")
        ftype = (str(fdef.get("type","")).strip().lower() or "text")
        if ftype.startswith("__"):  continue
        label = fdef.get("label") or fdef.get("name") or key

        cur = None
        for k, v in (flex_vals or {}).items():
            if str(k).strip().lower() == key:
                cur = v
                break

        if ftype == "video":
            if isinstance(cur, str) and cur.strip():
                low = cur.lower()
                if "http" in low or "://" in cur:
                    # Печатаем сам URL
                    lines.append(f"<b>{html_escape(str(label))}:</b> <i>{html_escape(cur)}</i>")
                else:
                    # file_id не раскрываем
                    lines.append(f"<b>{html_escape(str(label))}:</b> <i>{video_added}</i>")
            else:
                lines.append(f"<b>{html_escape(str(label))}:</b> <i>—</i>")
        else:
            lines.append(f"<b>{html_escape(str(label))}:</b> { _fmt(cur) }")

        lines.append("")

    return "\n".join(lines)

# Короткое RU-пояснение: отправить под меню кнопку «Смотреть видео» ТОЛЬКО через web-хелпер из просмотра.
# async def _send_watch_video_button(bot, chat_id: int, video_url: str, listing_id: int):
#     class _DummyMessage:
#         def __init__(self, _bot, _chat_id):
#             self.bot = _bot
#             self.chat = type("C", (), {"id": _chat_id})
#         async def answer(self, text, reply_markup=None, parse_mode=None):
#             return await self.bot.send_message(self.chat.id, text, reply_markup=reply_markup, parse_mode=parse_mode)
#     class _DummyCb:
#         def __init__(self, _bot, _chat_id):
#             self.message = _DummyMessage(_bot, _chat_id)
#             self.bot = _bot
#         async def answer(self, *a, **kw): return
#     dummy = _DummyCb(bot, chat_id)
#     msg = await _sv_send_yt_button(dummy, video_url, listing_id)
#     return getattr(msg, "message_id", None)


# ─────────────────────────────────────────────────────────
# Рендер единого обзора всех полей (как в Барахолке)
# ─────────────────────────────────────────────────────────
# RU: Показ карточки редактирования. YouTube/URL показываем ПРЯМО в карточке
#     (web-preview включён). Отдельное «нижнее» сообщение отправляем ТОЛЬКО,
#     если видео хранится как file_id (нативное видео Телеграма).
async def _render_overview(chat_id: int, bot, answer_method, listing_id: int):
    """Очистить чат, показать обзор; при URL — превью остаётся в карточке,
    при file_id — видео отправляется отдельным сообщением под меню.
    Фото услуги показываются НАД карточкой редактирования.
    Всё добавляем в last_bot_messages для последующей зачистки."""
    # 1) зачистка интерфейса
    await clear_bot_messages(chat_id, bot)
    await _clear_user_inputs(chat_id, bot)

    # 2) загрузка сущностей и текущих значений
    listing, city, cat, defs, flex_vals = await _load_listing_bundle(listing_id)

    # 2.1) фото услуги
    photo_ids = []
    if listing.photo_file_id:
        try:
            photo_ids = [x.strip() for x in listing.photo_file_id.split(",") if x.strip()]
        except Exception:
            photo_ids = []

    # 3) найти значение видео по первому полю type="video"
    video_value: str | None = None
    for f in defs:
        if str(f.get("type", "")).strip().lower() == "video":
            key = (str(f.get("key", "")).strip().lower() or "video")
            val = None
            # значение из flex — без учёта регистра ключа
            for k, v in (flex_vals or {}).items():
                if str(k).strip().lower() == key:
                    val = v
                    break
            if isinstance(val, str) and val.strip():
                video_value = val.strip()
            break

    # 4) текст карточки (крошки + поля)
    text = await _build_overview_text(listing, city, cat, defs, flex_vals)

    # 5) кнопки «Править …»
    btn_field_tmpl = await get_text("services_edit_btn_field_tmpl", "ru") or "✏️ Править: {label}"
    rows = [
        [InlineKeyboardButton(text=(await get_text("services_edit_btn_photo", "ru") or "🖼 Править фото"), callback_data=f"sphoto:open:{listing_id}")],
        [InlineKeyboardButton(text=(await get_text("services_edit_btn_title", "ru") or "✏️ Править заголовок"), callback_data=f"sef:main:title:{listing_id}")],
        [InlineKeyboardButton(text=(await get_text("services_edit_btn_descr", "ru") or "✏️ Править описание"), callback_data=f"sef:main:descr:{listing_id}")],
        [InlineKeyboardButton(text=(await get_text("services_edit_btn_price", "ru") or "✏️ Править стоимость"), callback_data=f"sef:main:price:{listing_id}")],
    ]
    for f in defs:
        key   = (str(f.get("key","")).strip().lower() or "field")
        ftype = (str(f.get("type","")).strip().lower() or "text")
        if ftype.startswith("__"):
            continue
        label = f.get("label") or f.get("name") or key
        if ftype == "video":
            rows.append([InlineKeyboardButton(
                text=btn_field_tmpl.format(label=label),
                callback_data=f"sefx:video:start:{listing_id}:{key}"
            )])
        else:
            rows.append([InlineKeyboardButton(
                text=btn_field_tmpl.format(label=label),
                callback_data=f"sef:extra:{key}:{listing_id}"
            )])

    # RU: Показать «Доп. категории», только если включены у этой категории
    allow_extra = False
    for _f in (defs or []):
        if (
            isinstance(_f, dict)
            and str((_f.get("type") or "")).strip().lower().startswith("__")
            and _f.get("key") == "allow_extra_categories"
            and bool(_f.get("value"))
        ):
            allow_extra = True
            break

    if allow_extra:
        used = int(bool(getattr(listing, "extra_category_id1", None))) + int(bool(getattr(listing, "extra_category_id2", None)))
        extra_btn_tmpl = await get_text("services_edit_btn_extra_categories_tmpl", "ru") or "➕ Доп. категории ({used}/2)"
        rows.append([InlineKeyboardButton(
            text=extra_btn_tmpl.format(used=used),
            callback_data=f"extra:s_open:{listing_id}"
        )])

    rows.append([InlineKeyboardButton(
        text=(await get_text("services_edit_btn_back_to_listing", "ru") or "⬅️ Назад к объявлению"),
        callback_data=f"sv:item:{listing_id}:{listing.city_id}:{listing.category_id}:m"
    )])

    # 6) сначала показываем фото, потом карточку редактирования
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    message_ids = []

    if photo_ids:
        try:
            if len(photo_ids) == 1:
                pmsg = await bot.send_photo(chat_id, photo_ids[0])
                message_ids.append(pmsg.message_id)
            else:
                media = [InputMediaPhoto(media=pid) for pid in photo_ids]
                pmsgs = await bot.send_media_group(chat_id, media=media)
                message_ids.extend([m.message_id for m in pmsgs])
        except Exception as e:
            print(f"[services_edit_overview.py] _render_overview ✗ photo_send_failed | chat_id={chat_id} listing_id={listing_id} | {type(e).__name__}: {e}")

    msg = await answer_method(
        text,
        reply_markup=kb,
        parse_mode="HTML",
        disable_web_page_preview=False,  # ← сохраняем превью YouTube в самой карточке
    )
    message_ids.append(msg.message_id)

    # 7) дополнительное сообщение ПОД карточкой — ТОЛЬКО для file_id
    if video_value:
        sval = video_value.strip()
        low  = sval.lower()
        is_youtube_url = sval.startswith("http") and ("youtube.com" in low or "youtu.be" in low)
        if not is_youtube_url:
            try:
                vmsg = await bot.send_video(chat_id, sval)  # file_id → нативное видео
                message_ids.append(vmsg.message_id)
            except Exception:
                # резерв: если по какой-то причине не отправилось — просто текстом
                try:
                    vmsg2 = await bot.send_message(chat_id, sval)
                    message_ids.append(vmsg2.message_id)
                except Exception as e:
                    print(f"[services_edit_overview.py] _render_overview ✗ video_fallback_failed | chat_id={chat_id} listing_id={listing_id} | {type(e).__name__}: {e}")

    # 8) регистрируем для последующей зачистки
    last_bot_messages[chat_id] = message_ids
    await register_bot_messages(chat_id, message_ids)

    print(
        f"[services_edit_overview.py] _render_overview | "
        f"chat_id={chat_id} | listing_id={listing_id} | "
        f"photos={len(photo_ids)} | flex_fields={len(defs)} | msg_id={msg.message_id}"
    )


# -----------------------------------------------------------------------------
# Точки входа (включая алиасы)
# -----------------------------------------------------------------------------

# Короткое RU-пояснение: стандартный вход — «service_edit_overview:{id}».
@router.callback_query(F.data.startswith("service_edit_overview:"))
async def service_edit_overview(cb: CallbackQuery):
    """Показать обзор редактирования услуги (как в Барахолке)."""
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.message.bot)
    try:
        listing_id = int(cb.data.split(":")[1])
    except Exception:
        await cb.answer(await get_text("err_invalid_id", "ru") or "Некорректный идентификатор.", show_alert=True)
        print(f"[services_edit_overview.py] service_edit_overview ✗ bad_id | data={cb.data}")
        return
    if not await _authorize_service_callback(cb, listing_id):
        return
    await _render_overview(chat_id, cb.message.bot, cb.message.answer, listing_id)
    await cb.answer()
    _pp("services_edit_overview.py", "service_edit_overview", chat_id=chat_id, user_id=cb.from_user.id, listing_id=listing_id, msg_id=cb.message.message_id)

# -----------------------------------------------------------------------------
# Основные поля: ввод → запись → возврат к обзору
# -----------------------------------------------------------------------------

class _MainFieldState(StatesGroup):
    """Состояние ожидания значения для основного поля."""
    waiting = State()

# Короткое RU-пояснение: запросить новое значение, показывая текущее.
async def _ask_main_value(message: Message, listing_id: int, field: str, prompt: str, current_text: str | None):
    """Попросить новое значение (показываем текущее) + кнопка Отмена."""
    chat_id = message.chat.id
    await clear_bot_messages(chat_id, message.bot)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=(await get_text("services_edit_btn_cancel_return", "ru") or "⬅️ Отменить и вернуться"), callback_data=f"sef:cancel:{listing_id}")],
    ])
    cur = (current_text or "").strip()
    main_value_prompt_tmpl = await get_text("services_edit_main_value_prompt_tmpl", "ru") or (
        "{prompt}\nТекущее: <code>{current}</code>\n\n"
        "Отправьте новое значение (или нажмите на текущее выше, чтобы скопировать)."
    )
    txt = main_value_prompt_tmpl.format(prompt=prompt, current=html_escape(cur) if cur else '—')
    msg = await message.answer(txt, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    _pp("services_edit_overview.py", "_ask_main_value", chat_id=chat_id, listing_id=listing_id, field=field, msg_id=msg.message_id)

# Короткое RU-пояснение: начать ввод заголовка.
@router.callback_query(F.data.startswith("sef:main:title:"))
async def sef_main_title(cb: CallbackQuery, state: FSMContext):
    """Начать редактирование заголовка (title)."""
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.message.bot)
    try:
        listing_id = int(cb.data.split(":")[3])
    except Exception:
        await cb.answer(await get_text("err_invalid_id", "ru") or "Некорректный идентификатор.", show_alert=True)
        return
    if not await _authorize_service_callback(cb, listing_id):
        return

    # получаем текущее значение
    async with SessionLocal() as s:
        l = await _owned_service_in_session(s, listing_id, cb.from_user.id)

    await state.set_state(_MainFieldState.waiting)
    await state.update_data(sef_listing_id=listing_id, sef_field="title")
    await _ask_main_value(cb.message, listing_id, "title", (await get_text("services_edit_title_prompt_label", "ru") or "✏️ Изменение заголовка"), l.title or "")
    await cb.answer()
    _pp("services_edit_overview.py", "sef_main_title", chat_id=chat_id, user_id=cb.from_user.id, listing_id=listing_id, field="title", msg_id=cb.message.message_id)


# Короткое RU-пояснение: начать ввод описания.
@router.callback_query(F.data.startswith("sef:main:descr:"))
async def sef_main_descr(cb: CallbackQuery, state: FSMContext):
    """Начать редактирование описания (descr)."""
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.message.bot)
    try:
        listing_id = int(cb.data.split(":")[3])
    except Exception:
        await cb.answer(await get_text("err_invalid_id", "ru") or "Некорректный идентификатор.", show_alert=True)
        return
    if not await _authorize_service_callback(cb, listing_id):
        return

    async with SessionLocal() as s:
        l = await _owned_service_in_session(s, listing_id, cb.from_user.id)

    await state.set_state(_MainFieldState.waiting)
    await state.update_data(sef_listing_id=listing_id, sef_field="descr")
    await _ask_main_value(cb.message, listing_id, "descr", (await get_text("services_edit_descr_prompt_label", "ru") or "💬 Изменение описания"), getattr(l, "descr", "") or "")
    await cb.answer()
    _pp("services_edit_overview.py", "sef_main_descr", chat_id=chat_id, user_id=cb.from_user.id, listing_id=listing_id, field="descr", msg_id=cb.message.message_id)

# Короткое RU-пояснение: начать ввод цены.
@router.callback_query(F.data.startswith("sef:main:price:"))
async def sef_main_price(cb: CallbackQuery, state: FSMContext):
    """Начать редактирование цены (price)."""
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.message.bot)
    try:
        listing_id = int(cb.data.split(":")[3])
    except Exception:
        await cb.answer(await get_text("err_invalid_id", "ru") or "Некорректный идентификатор.", show_alert=True)
        return
    if not await _authorize_service_callback(cb, listing_id):
        return

    async with SessionLocal() as s:
        l = await _owned_service_in_session(s, listing_id, cb.from_user.id)

    await state.set_state(_MainFieldState.waiting)
    await state.update_data(sef_listing_id=listing_id, sef_field="price")
    await _ask_main_value(cb.message, listing_id, "price", (await get_text("services_edit_price_prompt_label", "ru") or "💰 Изменение стоимости"), l.price or "")
    await cb.answer()
    _pp("services_edit_overview.py", "sef_main_price", chat_id=chat_id, user_id=cb.from_user.id, listing_id=listing_id, field="price", msg_id=cb.message.message_id)


# Короткое RU-пояснение: сохранить основное поле и вернуться к обзору (удаляем сообщение пользователя).
@router.message(_MainFieldState.waiting)
async def sef_main_value_entered(message: Message, state: FSMContext):
    """Сохранить title/price/descr → показать обзор с обновлёнными значениями."""
    chat_id = message.chat.id
    await clear_bot_messages(chat_id, message.bot)

    # ✳️ Удаляем пользовательское сообщение (текст/фото/видео/ссылка) — вместе с web-preview YouTube
    await _remember_and_delete_user_message(message)

    data = await state.get_data()
    listing_id = int(data["sef_listing_id"])
    field      = data["sef_field"]

    async with SessionLocal() as s:
        l = await _owned_service_in_session(s, listing_id, message.from_user.id)
        if l is None:
            await state.clear()
            await message.answer(await get_text("err_not_owner_service", "ru") or "Можно редактировать только свои услуги.")
            return
        val = message.text or message.caption or ""
        if field == "title":
            val = val.strip()
            if not val:
                await message.answer(await get_text("market_edit_title_empty", "ru") or "Заголовок не может быть пустым.")
                return
            l.title = val
        elif field == "price":
            val = val.strip()
            if not val:
                await message.answer(await get_text("services_edit_price_empty", "ru") or "Стоимость не может быть пустой.")
                return
            l.price = val
        elif field == "descr":     l.descr = val
        await s.commit()

    await state.clear()
    await _render_overview(chat_id, message.bot, message.answer, listing_id)
    print(f"[services_edit_overview.py] sef_main_value_entered ✓ | chat_id={chat_id} | user_id={message.from_user.id} | listing_id={listing_id} | field={field} | msg_id={message.message_id}")

# -----------------------------------------------------------------------------
# Flex: ввод → запись → возврат к обзору (хранение в listing.flex)
# -----------------------------------------------------------------------------

class _FlexState(StatesGroup):
    """Состояние ожидания значения для flex-поля."""
    waiting = State()

# Короткое RU-пояснение: старт ввода flex-поля по ключу.
@router.callback_query(F.data.startswith("sef:extra:"))
async def sefx_start(cb: CallbackQuery, state: FSMContext):
    """Начать ввод flex-поля (text/textarea/number/checkbox/select/multiselect/video как строка)."""
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.message.bot)
    try:
        _, _, key, listing_id_str = cb.data.split(":")
        listing_id = int(listing_id_str)
    except Exception:
        await cb.answer(await get_text("err_invalid_id", "ru") or "Некорректный идентификатор.", show_alert=True)
        return
    if not await _authorize_service_callback(cb, listing_id):
        return

    # берём схему и текущие значения flex
    listing, _, _, defs, flex_vals = await _load_listing_bundle(listing_id)
    fdef = next((d for d in defs if (str(d.get("key","")).strip().lower() == key)), None)
    if not fdef:
        await cb.answer(await get_text("services_edit_field_not_found", "ru") or "Поле не найдено.", show_alert=True)
        print(f"[services_edit_overview.py] sefx_start ✗ no_field | key={key} listing_id={listing_id}")
        return

    label = fdef.get("label") or fdef.get("name") or key
    # найдём текущее значение (без учёта регистра ключа)
    current = None
    for k, v in (flex_vals or {}).items():
        if str(k).strip().lower() == key:
            current = v
            break

    # для видео: file_id не раскрываем
    video_hidden_file = await get_text("services_edit_video_hidden_file", "ru") or "загружен файл (file_id скрыт)"
    ftype = (str(fdef.get("type") or "text").strip().lower())
    if ftype == "video":
        if isinstance(current, str) and current.strip():
            low = current.lower()
            current_text = current if ("http" in low or "://" in current) else video_hidden_file
        else:
            current_text = ""
    else:
        current_text = (json.dumps(current, ensure_ascii=False) if isinstance(current, (list, dict)) else (current or ""))

    await state.set_state(_FlexState.waiting)
    await state.update_data(sef_listing_id=listing_id, sef_key=key, sef_fdef=fdef)

    # показываем текущее значение в запросе
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=(await get_text("services_edit_btn_cancel_return", "ru") or "⬅️ Отменить и вернуться"), callback_data=f"sef:cancel:{listing_id}")],
    ])
    flex_value_prompt_tmpl = await get_text("services_edit_flex_value_prompt_tmpl", "ru") or (
        "Поле: <b>{label}</b>\nТекущее: <code>{current}</code>\n\n"
        "Отправьте новое значение (или нажмите на текущее выше, чтобы скопировать)."
    )
    txt = flex_value_prompt_tmpl.format(label=html_escape(str(label)), current=html_escape(str(current_text)) if current_text else '—')
    msg = await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

    await cb.answer()
    _pp("services_edit_overview.py", "sefx_start", chat_id=chat_id, user_id=cb.from_user.id, listing_id=listing_id, field=key, msg_id=msg.message_id)

# Короткое RU-пояснение: сохранить flex-значение и вернуться к обзору (удаляем сообщение пользователя).
@router.message(_FlexState.waiting)
async def sefx_value_entered(message: Message, state: FSMContext):
    """Сохранить flex-поле → показать обзор с обновлёнными значениями."""
    chat_id = message.chat.id
    await clear_bot_messages(chat_id, message.bot)

    # ✳️ Удаляем пользовательское сообщение
    await _remember_and_delete_user_message(message)

    data = await state.get_data()
    listing_id = int(data["sef_listing_id"])
    key        = data["sef_key"]
    fdef       = data.get("sef_fdef") or {}

    v_raw  = message.text or message.caption or ""
    v_norm = _normalize_value_for_type(v_raw, (fdef.get("type") or "text"))

    async with SessionLocal() as s:
        l = await _owned_service_in_session(s, listing_id, message.from_user.id)
        if l is None:
            await state.clear()
            await message.answer(await get_text("err_not_owner_service", "ru") or "Можно редактировать только свои услуги.")
            return
        expected_type = str(fdef.get("type") or "text").strip().lower()
        current_def = await _service_extra_field_def(s, l, key)
        current_type = str((current_def or {}).get("type", "")).strip().lower()
        if current_type != expected_type or current_type.startswith("__"):
            await state.clear()
            await message.answer(await get_text("vacancy_edit_field_unavailable", "ru") or "Поле больше недоступно для редактирования.")
            return
        try:
            flex = json.loads(l.flex or "{}")
            if not isinstance(flex, dict):
                flex = {}
        except Exception:
            flex = {}
        flex[key] = v_norm
        l.flex = json.dumps(flex, ensure_ascii=False)
        await s.commit()

    await state.clear()
    await _render_overview(chat_id, message.bot, message.answer, listing_id)
    print(f"[services_edit_overview.py] sefx_value_entered ✓ | chat_id={chat_id} | user_id={message.from_user.id} | listing_id={listing_id} | field={key} | msg_id={message.message_id}")

# -----------------------------------------------------------------------------
# VIDEO (как flex): отдельная ветка (строка: URL или file_id)
# -----------------------------------------------------------------------------

class _VideoState(StatesGroup):
    """Состояние ожидания видео или ссылки."""
    waiting = State()

# Короткое RU-пояснение: начать ввод VIDEO по ключу.
@router.callback_query(F.data.startswith("sefx:video:start:"))
async def sefx_video_start(cb: CallbackQuery, state: FSMContext):
    """Начать ввод VIDEO (одно значение: ссылка или file_id)."""
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.message.bot)
    _, _, _, listing_id_str, key = cb.data.split(":")
    listing_id = int(listing_id_str)
    if not await _authorize_service_callback(cb, listing_id):
        return

    # текущее значение видео
    _, _, _, defs, flex_vals = await _load_listing_bundle(listing_id)
    fdef = next((
        field for field in defs if isinstance(field, dict)
        and str(field.get("key", "")).strip().lower() == key
    ), None)
    if str((fdef or {}).get("type", "")).strip().lower() != "video":
        await cb.answer(await get_text("services_edit_video_field_not_found", "ru") or "Поле видео не найдено.", show_alert=True)
        return
    current = None
    for k, v in (flex_vals or {}).items():
        if str(k).strip().lower() == key:
            current = v
            break
    video_hidden_file = await get_text("services_edit_video_hidden_file", "ru") or "загружен файл (file_id скрыт)"
    if isinstance(current, str) and current.strip():
        low = current.lower()
        current_text = current if ("http" in low or "://" in current) else video_hidden_file
    else:
        current_text = ""

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=(await get_text("services_edit_btn_cancel_return", "ru") or "⬅️ Отменить и вернуться"), callback_data=f"sef:cancel:{listing_id}")],
    ])
    video_prompt_tmpl = await get_text("services_edit_video_prompt_tmpl", "ru") or (
        "Отправьте видеофайл <b>или</b> ссылку (YouTube/веб). Разрешено только одно видео.\n"
        "Текущее: <i>{current}</i>"
    )
    txt = video_prompt_tmpl.format(current=html_escape(str(current_text)) if current_text else '—')
    msg = await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

    await state.set_state(_VideoState.waiting)
    await state.update_data(sef_listing_id=listing_id, sef_key=key)
    await cb.answer()
    _pp("services_edit_overview.py", "sefx_video_start", chat_id=chat_id, user_id=cb.from_user.id, listing_id=listing_id, field=key, msg_id=msg.message_id)

# Короткое RU-пояснение: сохранить ссылку для VIDEO (удаляем сообщение пользователя, чтобы не висела иконка превью).
@router.message(_VideoState.waiting, F.text)
async def sefx_video_link(message: Message, state: FSMContext):
    """Сохранить URL для VIDEO (строкой) → показать обзор."""
    chat_id = message.chat.id
    await clear_bot_messages(chat_id, message.bot)

    # ✳️ Удаляем пользовательское сообщение (и web-preview)
    await _remember_and_delete_user_message(message)

    data = await state.get_data()
    l_id = int(data["sef_listing_id"])
    key  = data["sef_key"]
    url  = (message.text or "").strip()

    if not _valid_http_url(url):
        msg = await message.answer(await get_text("services_edit_invalid_url", "ru") or "Отправьте корректную ссылку, начинающуюся с http:// или https://.")
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        return

    async with SessionLocal() as s:
        l = await _owned_service_extra_field(s, l_id, message.from_user.id, key, "video")
        if l is None:
            await state.clear()
            await message.answer(await get_text("err_not_owner_service", "ru") or "Можно редактировать только свои услуги.")
            return
        try:
            flex = json.loads(l.flex or "{}")
            if not isinstance(flex, dict):
                flex = {}
        except Exception:
            flex = {}
        flex[key] = url  # строкой, как ожидается
        l.flex = json.dumps(flex, ensure_ascii=False)
        await s.commit()

    await state.clear()
    await _render_overview(chat_id, message.bot, message.answer, l_id)
    print(f"[services_edit_overview.py] sefx_video_link ✓ | chat_id={chat_id} | user_id={message.from_user.id} | listing_id={l_id} | field={key} | msg_id={message.message_id}")
    _pp("services_edit_overview.py", "sefx_video_link", chat_id=chat_id, user_id=message.from_user.id, listing_id=l_id, field=key, msg_id=message.message_id)

# Короткое RU-пояснение: сохранить file_id для VIDEO (удаляем сообщение пользователя с видеомедиа).
@router.message(_VideoState.waiting, F.video)
async def sefx_video_file(message: Message, state: FSMContext):
    """Сохранить file_id для VIDEO (строкой) → показать обзор."""
    chat_id = message.chat.id
    await clear_bot_messages(chat_id, message.bot)

    # ✳️ Удаляем пользовательское сообщение (видеофайл)
    await _remember_and_delete_user_message(message)

    data = await state.get_data()
    l_id = int(data["sef_listing_id"])
    key  = data["sef_key"]
    file_id = message.video.file_id

    async with SessionLocal() as s:
        l = await _owned_service_extra_field(s, l_id, message.from_user.id, key, "video")
        if l is None:
            await state.clear()
            await message.answer(await get_text("err_not_owner_service", "ru") or "Можно редактировать только свои услуги.")
            return
        try:
            flex = json.loads(l.flex or "{}")
            if not isinstance(flex, dict):
                flex = {}
        except Exception:
            flex = {}
        flex[key] = file_id
        l.flex = json.dumps(flex, ensure_ascii=False)
        await s.commit()

    await state.clear()
    await _render_overview(chat_id, message.bot, message.answer, l_id)
    print(f"[services_edit_overview.py] sefx_video_file ✓ | chat_id={chat_id} | user_id={message.from_user.id} | listing_id={l_id} | field={key} | msg_id={message.message_id}")

# Короткое RU-пояснение: если прислали не то (например, фото), удаляем это сообщение и просим повторить.
@router.message(_VideoState.waiting)
async def sefx_video_wrong(message: Message, state: FSMContext):
    """Подсказать прислать видеофайл или ссылку (сообщение пользователя удаляем)."""
    chat_id = message.chat.id
    await clear_bot_messages(chat_id, message.bot)

    # ✳️ Удаляем «неподходящее» пользовательское сообщение (фото/документ и т.п.)
    await _remember_and_delete_user_message(message)

    data = await state.get_data()
    l_id = int(data["sef_listing_id"])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=(await get_text("services_edit_btn_cancel_return", "ru") or "⬅️ Отменить и вернуться"), callback_data=f"sef:cancel:{l_id}")],
    ])
    msg = await message.answer(await get_text("services_edit_send_video_or_link", "ru") or "Нужно отправить видеофайл или ссылку. Попробуйте ещё раз.", reply_markup=kb)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    print(f"[services_edit_overview.py] sefx_video_wrong ✗ wrong_content | listing_id={l_id} | msg_id={message.message_id}")

# -----------------------------------------------------------------------------
# Отмена / Назад: всегда шаг назад к ОБЗОРУ
# -----------------------------------------------------------------------------

# Короткое RU-пояснение: отменить ввод и вернуться к обзору текущей услуги.
@router.callback_query(F.data.startswith("sef:cancel:"))
async def sef_cancel(cb: CallbackQuery, state: FSMContext):
    """Отмена ввода → показать обзор (крошки + значения + кнопки)."""
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.message.bot)
    listing_id = int(cb.data.split(":")[2])
    if not await _authorize_service_callback(cb, listing_id):
        await state.clear()
        return
    await state.clear()
    await _render_overview(chat_id, cb.message.bot, cb.message.answer, listing_id)
    await cb.answer()
    _pp("services_edit_overview.py", "sef_cancel", chat_id=chat_id, user_id=cb.from_user.id, listing_id=listing_id, msg_id=cb.message.message_id)


# ─────────────────────────────────────────────────────────
# RU: Открыть мини-меню «Доп. категории» для услуги.
#     Полная зачистка, разбор listing_id, защита try/except, логирование.
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("extra:s_open:"))
async def extra_open_services(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    print(f"[services_edit_overview.py] handler=extra_open_services ENTER | chat_id={chat_id} data={cb.data!r}")

    # Зачистка интерфейса (каноны)
    try:
        await cb.message.delete()
    except Exception:
        pass
    try:
        await clear_bot_messages(chat_id, cb.message.bot)
    except Exception:
        pass

    # Разбор listing_id из callback_data
    try:
        _, _, raw_id = (cb.data or "").split(":")
        listing_id = int(raw_id)
    except Exception:
        await cb.answer(await get_text("err_invalid_data", "ru") or "Некорректные данные.", show_alert=True)
        print(f"[services_edit_overview.py] handler=extra_open_services ERROR parse | chat_id={chat_id} data={cb.data!r}")
        return

    try:
        # Работа с БД
        async with SessionLocal() as s:
            listing = (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one_or_none()
            if not listing:
                await cb.answer(await get_text("err_listing_404", "ru") or "Объявление не найдено.", show_alert=True)
                print(f"[services_edit_overview.py] handler=extra_open_services ERROR no_listing | chat_id={chat_id} listing_id={listing_id}")
                return
            if listing.owner_id != cb.from_user.id or listing.type != "service":
                await cb.answer(await get_text("err_not_owner_service", "ru") or "Можно редактировать только свои услуги.", show_alert=True)
                return

            category = (await s.execute(select(Category).where(Category.id == listing.category_id))).scalar_one()

            # RU: Проверка включения доп. категорий по полю fields категории
            if not _allow_extra_from_fields_raw(category.fields):
                await cb.answer(await get_text("services_edit_extra_disabled", "ru") or "Для этой категории доп. категории выключены.", show_alert=True)
                print(f"[services_edit_overview.py] handler=extra_open_services DENY allow_false | chat_id={chat_id} listing_id={listing_id} cat_id={category.id}")
                return

            used = (1 if listing.extra_category_id1 else 0) + (1 if listing.extra_category_id2 else 0)
            cat1 = (await s.execute(select(Category).where(Category.id == listing.extra_category_id1))).scalar_one_or_none() if listing.extra_category_id1 else None
            cat2 = (await s.execute(select(Category).where(Category.id == listing.extra_category_id2))).scalar_one_or_none() if listing.extra_category_id2 else None

        # Клавиатура мини-меню
        del_extra_tmpl = await get_text("services_edit_btn_delete_extra_category_tmpl", "ru") or "🗑 Удалить: {name}"
        extra_menu_tmpl = await get_text("services_edit_extra_menu_tmpl", "ru") or "Доп. категории для «{title}»\nЗанято слотов: {used}/2"
        kb_rows: list[list[InlineKeyboardButton]] = []
        if used < 2:
            kb_rows.append([InlineKeyboardButton(text=(await get_text("services_edit_btn_add_extra_category", "ru") or "➕ Добавить категорию"), callback_data=f"sextra:add:{listing_id}")])
        if cat1:
            kb_rows.append([InlineKeyboardButton(text=del_extra_tmpl.format(name=cat1.name), callback_data=f"sextra:del:{listing_id}:1")])
        if cat2:
            kb_rows.append([InlineKeyboardButton(text=del_extra_tmpl.format(name=cat2.name), callback_data=f"sextra:del:{listing_id}:2")])
        kb_rows.append(await _back_row(f"sextra:back:{listing_id}"))

        # Показ меню
        msg = await cb.message.answer(
            extra_menu_tmpl.format(title=html_escape(listing.title or ''), used=used),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
            parse_mode="HTML",
        )
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer()

        print(f"[services_edit_overview.py] handler=extra_open_services OK | chat_id={chat_id} listing_id={listing_id} used={used} msg_id={msg.message_id}")

    except Exception as e:
        # Диагностика падения при открытии мини-меню
        try:
            await cb.answer(await get_text("services_edit_extra_open_error", "ru") or "Ошибка при открытии доп. категорий (Услуги).", show_alert=True)
        except Exception:
            pass
        print(f"[services_edit_overview.py] handler=extra_open_services EXC | chat_id={chat_id} listing_id={listing_id} err={e!r}")
        return

# RU: Показать кнопку «Доп. категории», если включено в категории
def _allow_extra_from_fields_raw(raw: str) -> bool:
    try:
        import json
        data = json.loads((raw or "").strip() or "[]")
        if isinstance(data, list):
            for f in data:
                if isinstance(f, dict) and str(f.get("type") or "").startswith("__") and f.get("key") == "allow_extra_categories":
                    return bool(f.get("value"))
        elif isinstance(data, dict):
            return bool(data.get("allow_extra_categories"))
    except Exception:
        pass
    return False


# ─────────────────────────────────────────────────────────
# RU: «⬅️ Назад» из мини-меню «Доп. категории» (Услуги).
#     Удаляем текущее меню/сообщения и возвращаемся в обзор услуги.
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.regexp(r"^sextra:back:(\d+)$"))
async def sextra_back_services(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # зачистка
    try:
        await cb.message.delete()
    except Exception:
        pass
    try:
        await clear_bot_messages(chat_id, cb.message.bot)
    except Exception:
        pass

    # парсим id объявления
    m = re.match(r"^sextra:back:(\d+)$", cb.data or "")
    listing_id = int(m.group(1)) if m else 0
    if not listing_id or not await _authorize_service_callback(cb, listing_id):
        return

    # возврат в обзор редактирования
    await _render_overview(chat_id, cb.message.bot, cb.message.answer, listing_id)

    await cb.answer()
    print(f"[services_edit_overview.py] handler=sextra_back_services OK chat_id={chat_id} listing_id={listing_id}")
@router.callback_query(F.data.regexp(r"^sextra:del:(\d+):(1|2)$"))
async def sextra_del_services(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    # Зачистка текущего сообщения и истории бота/пользователя
    try:
        await cb.message.delete()
    except Exception:
        pass
    try:
        await clear_bot_messages(chat_id, cb.message.bot)
    except Exception:
        pass
    try:
        await _clear_user_inputs(chat_id, cb.message.bot)
    except Exception:
        pass

    # Разбор параметров из callback_data
    m = re.match(r"^sextra:del:(\d+):(1|2)$", cb.data or "")
    if not m:
        await cb.answer(await get_text("err_invalid_data", "ru") or "Некорректные данные.", show_alert=True)
        print(f"[services_edit_overview.py] handler=sextra_del_services ERROR parse chat_id={chat_id} data={cb.data!r}")
        return
    listing_id = int(m.group(1))
    slot       = int(m.group(2))

    # Сброс выбранного слота и перерисовка мини-меню
    async with SessionLocal() as s:
        listing = (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one_or_none()
        if not listing:
            await cb.answer(await get_text("err_listing_404", "ru") or "Объявление не найдено.", show_alert=True)
            print(f"[services_edit_overview.py] handler=sextra_del_services ERROR no_listing chat_id={chat_id} listing_id={listing_id}")
            return
        if listing.owner_id != cb.from_user.id or listing.type != "service":
            await cb.answer(await get_text("err_not_owner_service", "ru") or "Можно редактировать только свои услуги.", show_alert=True)
            return

        if slot == 1:
            listing.extra_category_id1 = None
        else:
            listing.extra_category_id2 = None
        await s.commit()

        category = (await s.execute(select(Category).where(Category.id == listing.category_id))).scalar_one()

        cat1 = None
        if listing.extra_category_id1:
            cat1 = (await s.execute(select(Category).where(Category.id == listing.extra_category_id1))).scalar_one_or_none()
        cat2 = None
        if listing.extra_category_id2:
            cat2 = (await s.execute(select(Category).where(Category.id == listing.extra_category_id2))).scalar_one_or_none()

        used = int(bool(listing.extra_category_id1)) + int(bool(listing.extra_category_id2))

        del_extra_tmpl = await get_text("services_edit_btn_delete_extra_category_tmpl", "ru") or "🗑 Удалить: {name}"
        kb_rows: list[list[InlineKeyboardButton]] = []
        if used < 2 and _allow_extra_for_category(category):
            kb_rows.append([InlineKeyboardButton(text=(await get_text("services_edit_btn_add_extra_category", "ru") or "➕ Добавить категорию"), callback_data=f"sextra:add:{listing_id}")])
        if cat1:
            kb_rows.append([InlineKeyboardButton(text=del_extra_tmpl.format(name=cat1.name), callback_data=f"sextra:del:{listing_id}:1")])
        if cat2:
            kb_rows.append([InlineKeyboardButton(text=del_extra_tmpl.format(name=cat2.name), callback_data=f"sextra:del:{listing_id}:2")])
        kb_rows.append(await _back_row(f"sextra:back:{listing_id}"))

    extra_menu_tmpl = await get_text("services_edit_extra_menu_tmpl", "ru") or "Доп. категории для «{title}»\nЗанято слотов: {used}/2"
    msg = await cb.message.answer(
        extra_menu_tmpl.format(title=html_escape(listing.title or ''), used=used),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        parse_mode="HTML",
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer(await get_text("services_edit_extra_removed_toast", "ru") or "Доп. категория удалена")
    print(f"[services_edit_overview.py] handler=sextra_del_services OK chat_id={chat_id} listing_id={listing_id} slot={slot} msg_id={msg.message_id}")


# ─────────────────────────────────────────────────────────
# RU: Проверка принадлежности категории ветке «Услуги» (корень id=80).
# ─────────────────────────────────────────────────────────
async def _is_services_branch(s, cat_id: int) -> bool:
    try:
        cur = (await s.execute(select(Category).where(Category.id == cat_id))).scalar_one_or_none()
        if not cur:
            return False
        while cur.parent_id is not None:
            cur = (await s.execute(select(Category).where(Category.id == cur.parent_id))).scalar_one_or_none()
            if not cur:
                return False
        return cur.id == 80
    except Exception:
        return False


# ─────────────────────────────────────────────────────────
# RU: Открыть выбор доп. категории (верхний уровень — дети корня 80).
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("sextra:add:"))
async def sextra_add_services(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    # Зачистка
    try:
        await cb.message.delete()
    except Exception:
        pass
    try:
        await clear_bot_messages(chat_id, cb.message.bot)
    except Exception:
        pass
    try:
        await _clear_user_inputs(chat_id, cb.message.bot)
    except Exception:
        pass

    try:
        _, _, raw_id = (cb.data or "").split(":")
        listing_id = int(raw_id)
    except Exception:
        await cb.answer(await get_text("err_invalid_data", "ru") or "Некорректные данные.", show_alert=True)
        print(f"[services_edit_overview.py] handler=sextra_add_services ERROR parse chat_id={chat_id} data={cb.data!r}")
        return

    if not await _authorize_service_callback(cb, listing_id):
        return

    async with SessionLocal() as s:
        listing = await _owned_service_in_session(s, listing_id, cb.from_user.id)
        category = await s.get(Category, listing.category_id)
        if category is None or not _allow_extra_for_category(category):
            await cb.answer(await get_text("services_edit_extra_disabled", "ru") or "Для этой категории доп. категории выключены.", show_alert=True)
            return
        if _extra_used(listing) >= 2:
            await cb.answer(await get_text("services_edit_extra_slots_full", "ru") or "Все два слота уже заняты.", show_alert=True)
            return
        cats = (await s.execute(select(Category).where(Category.parent_id == 80))).scalars().all()
        rows = [[InlineKeyboardButton(text=c.name, callback_data=f"sextra:pick:{listing_id}:{c.id}")] for c in cats]
        rows.append(await _back_row(f"sextra:back:{listing_id}"))

    msg = await cb.message.answer(
        (await get_text("services_edit_choose_extra_category", "ru") or "Выберите категорию (Услуги):"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"[services_edit_overview.py] handler=sextra_add_services OK chat_id={chat_id} listing_id={listing_id} msg_id={msg.message_id}")


# ─────────────────────────────────────────────────────────
# RU: Клик по категории при выборе доп. категории:
#     • есть дети — углубляемся
#     • лист — записываем в первый свободный слот с проверками (ветка 80, не основная, не дубль)
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("sextra:pick:"))
async def sextra_pick_services(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    subcat_tmpl = await get_text("services_edit_choose_extra_subcategory_tmpl", "ru") or "Категория: <b>{name}</b>\nВыберите подкатегорию:"
    del_extra_tmpl = await get_text("services_edit_btn_delete_extra_category_tmpl", "ru") or "🗑 Удалить: {name}"
    extra_menu_tmpl = await get_text("services_edit_extra_menu_tmpl", "ru") or "Доп. категории для «{title}»\nЗанято слотов: {used}/2"
    # Зачистка
    try:
        await cb.message.delete()
    except Exception:
        pass
    try:
        await clear_bot_messages(chat_id, cb.message.bot)
    except Exception:
        pass
    try:
        await _clear_user_inputs(chat_id, cb.message.bot)
    except Exception:
        pass

    try:
        _, _, raw_lid, raw_cid = (cb.data or "").split(":")
        listing_id = int(raw_lid)
        cat_id     = int(raw_cid)
    except Exception:
        await cb.answer(await get_text("err_invalid_data", "ru") or "Некорректные данные.", show_alert=True)
        print(f"[services_edit_overview.py] handler=sextra_pick_services ERROR parse chat_id={chat_id} data={cb.data!r}")
        return

    async with SessionLocal() as s:
        cat = (await s.execute(select(Category).where(Category.id == cat_id))).scalar_one_or_none()
        if not cat:
            await cb.answer(await get_text("services_edit_category_not_found", "ru") or "Категория не найдена.", show_alert=True)
            print(f"[services_edit_overview.py] handler=sextra_pick_services ERROR no_cat chat_id={chat_id} cat_id={cat_id}")
            return

        children = (await s.execute(select(Category).where(Category.parent_id == cat_id))).scalars().all()
        if children:
            rows = [[InlineKeyboardButton(text=c.name, callback_data=f"sextra:pick:{listing_id}:{c.id}")] for c in children]
            if cat.parent_id and cat.parent_id != 80:
                rows.append(await _back_row(f"sextra:up:{listing_id}:{cat.parent_id}"))
            else:
                rows.append(await _back_row(f"sextra:add:{listing_id}"))

            msg = await cb.message.answer(
                subcat_tmpl.format(name=html_escape(cat.name or '')),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                parse_mode="HTML",
            )
            last_bot_messages[chat_id] = [msg.message_id]
            await register_bot_messages(chat_id, [msg.message_id])
            await cb.answer()
            print(f"[services_edit_overview.py] handler=sextra_pick_services NAV chat_id={chat_id} listing_id={listing_id} cat_id={cat_id} msg_id={msg.message_id}")
            return

        listing = (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one_or_none()
        if not listing:
            await cb.answer(await get_text("err_listing_404", "ru") or "Объявление не найдено.", show_alert=True)
            print(f"[services_edit_overview.py] handler=sextra_pick_services ERROR no_listing chat_id={chat_id} listing_id={listing_id}")
            return

        if listing.owner_id != cb.from_user.id or listing.type != "service":
            await cb.answer(await get_text("err_not_owner_service", "ru") or "Можно редактировать только свои услуги.", show_alert=True)
            return

        category = await s.get(Category, listing.category_id)
        if category is None or not _allow_extra_for_category(category):
            await cb.answer(await get_text("services_edit_extra_disabled", "ru") or "Для этой категории доп. категории выключены.", show_alert=True)
            return

        if not await _is_services_branch(s, cat_id):
            await cb.answer(await get_text("services_edit_extra_wrong_branch", "ru") or "Можно выбирать только категории Услуг.", show_alert=True)
            print(f"[services_edit_overview.py] handler=sextra_pick_services REJECT branch chat_id={chat_id} cat_id={cat_id}")
            back_cb = f"sextra:up:{listing_id}:{cat.parent_id}" if (cat.parent_id and cat.parent_id != 80) else f"sextra:add:{listing_id}"
            back_msg = await cb.message.answer((await get_text("services_edit_choose_extra_category", "ru") or "Выберите категорию (Услуги):"), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                await _back_row(back_cb)
            ]))
            last_bot_messages[chat_id] = [back_msg.message_id]
            await register_bot_messages(chat_id, [back_msg.message_id])
            await cb.answer()
            return

        if cat_id == listing.category_id:
            await cb.answer(await get_text("services_edit_extra_same_as_main", "ru") or "Нельзя выбирать основную категорию объявления.", show_alert=True)
            print(f"[services_edit_overview.py] handler=sextra_pick_services REJECT base chat_id={chat_id} listing_id={listing_id} cat_id={cat_id}")
            rows = [await _back_row(f"sextra:add:{listing_id}")]
            back_msg = await cb.message.answer(await get_text("services_edit_choose_another_extra_category", "ru") or "Выберите другую категорию (Услуги):", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
            last_bot_messages[chat_id] = [back_msg.message_id]
            await register_bot_messages(chat_id, [back_msg.message_id])
            await cb.answer()
            return

        if listing.extra_category_id1 == cat_id or listing.extra_category_id2 == cat_id:
            await cb.answer(await get_text("services_edit_extra_duplicate", "ru") or "Такая доп. категория уже выбрана.", show_alert=True)
            print(f"[services_edit_overview.py] handler=sextra_pick_services REJECT dup chat_id={chat_id} listing_id={listing_id} cat_id={cat_id}")
            rows = [await _back_row(f"sextra:add:{listing_id}")]
            back_msg = await cb.message.answer(await get_text("services_edit_choose_another_extra_category", "ru") or "Выберите другую категорию (Услуги):", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
            last_bot_messages[chat_id] = [back_msg.message_id]
            await register_bot_messages(chat_id, [back_msg.message_id])
            await cb.answer()
            return

        slots_used = int(bool(listing.extra_category_id1)) + int(bool(listing.extra_category_id2))
        if slots_used >= 2:
            category = (await s.execute(select(Category).where(Category.id == listing.category_id))).scalar_one()
            cat1 = (await s.execute(select(Category).where(Category.id == listing.extra_category_id1))).scalar_one_or_none() if listing.extra_category_id1 else None
            cat2 = (await s.execute(select(Category).where(Category.id == listing.extra_category_id2))).scalar_one_or_none() if listing.extra_category_id2 else None
            kb_rows: list[list[InlineKeyboardButton]] = []
            if cat1:
                kb_rows.append([InlineKeyboardButton(text=del_extra_tmpl.format(name=cat1.name), callback_data=f"sextra:del:{listing_id}:1")])
            if cat2:
                kb_rows.append([InlineKeyboardButton(text=del_extra_tmpl.format(name=cat2.name), callback_data=f"sextra:del:{listing_id}:2")])
            kb_rows.append(await _back_row(f"sextra:back:{listing_id}"))
            msg = await cb.message.answer(
                extra_menu_tmpl.format(title=html_escape(listing.title or ''), used=2),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
                parse_mode="HTML",
            )
            last_bot_messages[chat_id] = [msg.message_id]
            await register_bot_messages(chat_id, [msg.message_id])
            await cb.answer()
            print(f"[services_edit_overview.py] handler=sextra_pick_services FULL chat_id={chat_id} listing_id={listing_id} msg_id={msg.message_id}")
            return

        # Записываем в 1-й свободный слот
        if listing.extra_category_id1 is None:
            listing.extra_category_id1 = cat_id
        else:
            listing.extra_category_id2 = cat_id
        await s.commit()

        category = (await s.execute(select(Category).where(Category.id == listing.category_id))).scalar_one()
        cat1 = (await s.execute(select(Category).where(Category.id == listing.extra_category_id1))).scalar_one_or_none() if listing.extra_category_id1 else None
        cat2 = (await s.execute(select(Category).where(Category.id == listing.extra_category_id2))).scalar_one_or_none() if listing.extra_category_id2 else None
        used = int(bool(listing.extra_category_id1)) + int(bool(listing.extra_category_id2))

        kb_rows: list[list[InlineKeyboardButton]] = []
        if used < 2 and _allow_extra_for_category(category):
            kb_rows.append([InlineKeyboardButton(text=(await get_text("services_edit_btn_add_extra_category", "ru") or "➕ Добавить категорию"), callback_data=f"sextra:add:{listing_id}")])
        if cat1:
            kb_rows.append([InlineKeyboardButton(text=del_extra_tmpl.format(name=cat1.name), callback_data=f"sextra:del:{listing_id}:1")])
        if cat2:
            kb_rows.append([InlineKeyboardButton(text=del_extra_tmpl.format(name=cat2.name), callback_data=f"sextra:del:{listing_id}:2")])
        kb_rows.append(await _back_row(f"sextra:back:{listing_id}"))

    msg = await cb.message.answer(
        extra_menu_tmpl.format(title=html_escape(listing.title or ''), used=used),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        parse_mode="HTML",
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer(await get_text("services_edit_extra_added_toast", "ru") or "Доп. категория добавлена")
    print(f"[services_edit_overview.py] handler=sextra_pick_services OK chat_id={chat_id} listing_id={listing_id} cat_id={cat_id} used={used} msg_id={msg.message_id}")


# ─────────────────────────────────────────────────────────
# RU: Подняться на уровень выше при выборе доп. категории.
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("sextra:up:"))
async def sextra_up_services(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    subcat_tmpl = await get_text("services_edit_choose_extra_subcategory_tmpl", "ru") or "Категория: <b>{name}</b>\nВыберите подкатегорию:"
    # Зачистка
    try:
        await cb.message.delete()
    except Exception:
        pass
    try:
        await clear_bot_messages(chat_id, cb.message.bot)
    except Exception:
        pass
    try:
        await _clear_user_inputs(chat_id, cb.message.bot)
    except Exception:
        pass

    try:
        _, _, raw_lid, raw_pid = (cb.data or "").split(":")
        listing_id = int(raw_lid)
        parent_id  = int(raw_pid)
    except Exception:
        await cb.answer(await get_text("err_invalid_data", "ru") or "Некорректные данные.", show_alert=True)
        print(f"[services_edit_overview.py] handler=sextra_up_services ERROR parse chat_id={chat_id} data={cb.data!r}")
        return

    if not await _authorize_service_callback(cb, listing_id):
        return

    async with SessionLocal() as s:
        parent = (await s.execute(select(Category).where(Category.id == parent_id))).scalar_one_or_none()
        if not parent or parent.id == 80:
            cats = (await s.execute(select(Category).where(Category.parent_id == 80))).scalars().all()
            rows = [[InlineKeyboardButton(text=c.name, callback_data=f"sextra:pick:{listing_id}:{c.id}")] for c in cats]
            rows.append(await _back_row(f"sextra:back:{listing_id}"))
            msg = await cb.message.answer(
                (await get_text("services_edit_choose_extra_category", "ru") or "Выберите категорию (Услуги):"),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                parse_mode="HTML",
            )
            last_bot_messages[chat_id] = [msg.message_id]
            await register_bot_messages(chat_id, [msg.message_id])
            await cb.answer()
            print(f"[services_edit_overview.py] handler=sextra_up_services ROOT chat_id={chat_id} listing_id={listing_id} msg_id={msg.message_id}")
            return

        children = (await s.execute(select(Category).where(Category.parent_id == parent.id))).scalars().all()
        rows = [[InlineKeyboardButton(text=c.name, callback_data=f"sextra:pick:{listing_id}:{c.id}")] for c in children]
        if parent.parent_id and parent.parent_id != 80:
            rows.append(await _back_row(f"sextra:up:{listing_id}:{parent.parent_id}"))
        else:
            rows.append(await _back_row(f"sextra:add:{listing_id}"))

    msg = await cb.message.answer(
        subcat_tmpl.format(name=html_escape(parent.name or '')),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"[services_edit_overview.py] handler=sextra_up_services OK chat_id={chat_id} listing_id={listing_id} parent_id={parent_id} msg_id={msg.message_id}")
