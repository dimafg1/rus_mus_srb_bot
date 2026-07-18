# app/routers/market_edit_overview.py

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from sqlalchemy import select
import json
import inspect
import math
from html import escape as html_escape
from urllib.parse import urlsplit
from aiogram.utils.keyboard import InlineKeyboardBuilder


from app.database import SessionLocal
from app.models import Listing, City, Category
from app.routers.utils import clear_bot_messages, last_bot_messages, register_bot_messages
from app.keyboards import get_common_menu_button


# RU: Хранилище id пользовательских сообщений и утилиты зачистки
from collections import defaultdict
from aiogram.types import Message
import re


_user_input_msgs = defaultdict(list)

async def _remember_and_delete_user_message(msg: Message):
    """RU: Запомнить и удалить пользовательское сообщение (текст/фото/видео/док)."""
    try:
        _user_input_msgs[msg.chat.id].append(msg.message_id)
    except Exception:
        pass
    try:
        await msg.delete()
    except Exception:
        pass

async def _clear_user_inputs(chat_id: int, bot):
    """RU: На всякий случай дочистить все запомненные пользовательские сообщения."""
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



router = Router()

# RU: куда возвращаться из экрана редактирования по каждому чату.
# Ключ: chat_id, значение: callback_data для кнопки «Назад к объявлению».
edit_return_cb_by_chat: dict[int, str] = {}


# # >>> BEGIN: extras callback stub
# @router.callback_query(F.data.startswith("extra:open:"))
# async def _open_extra_categories_menu(c: CallbackQuery):
#     # Пока просто подтверждаем клик. Меню добавим на следующем шаге.
#     await c.answer("Меню доп. категорий покажем на следующем шаге.", show_alert=True)
# # <<< END: extras callback stub



# ─────────────────────────────────────────────────────────
# FSM для редактирования ОДНОГО поля (универсально)
# ─────────────────────────────────────────────────────────
class OneFieldStates(StatesGroup):
    waiting_value = State()  # для text/number; checkbox/select идут коллбэками

# Дополнительные состояния для ожидания видео при редактировании одного поля
class ExtraVideoStates(StatesGroup):
    waiting_video = State()


# ─────────────────────────────────────────────────────────
# Внутренние утилиты
# ─────────────────────────────────────────────────────────
async def _get_listing(s, listing_id: int) -> Listing:
    return (await s.execute(select(Listing).where(
        Listing.id == listing_id,
        Listing.type == "market",
    ))).scalar_one()


async def _owned_market_in_session(s, listing_id: int, user_id: int) -> Listing | None:
    """Callback and FSM ids are untrusted; every edit must pass this query."""
    return (await s.execute(select(Listing).where(
        Listing.id == listing_id,
        Listing.owner_id == user_id,
        Listing.type == "market",
    ))).scalar_one_or_none()


async def _authorize_market_callback(cb: CallbackQuery, listing_id: int) -> bool:
    async with SessionLocal() as s:
        listing = await _owned_market_in_session(s, listing_id, cb.from_user.id)
    if listing is None:
        await cb.answer("Можно редактировать только свои объявления.", show_alert=True)
        return False
    return True

async def _get_city_cat(s, listing: Listing):
    city = (await s.execute(select(City).where(City.id == listing.city_id))).scalar_one()
    cat  = (await s.execute(select(Category).where(Category.id == listing.category_id))).scalar_one()
    return city, cat

async def _load_category_fields(s, cat_id: int) -> list[dict]:
    cat = (await s.execute(select(Category).where(Category.id == cat_id))).scalar_one()
    try:
        raw = (cat.fields or "").strip()
        data = json.loads(raw) if raw else []
        return data if isinstance(data, list) else []
    except Exception:
        return []


async def _extra_field_def(s, listing: Listing, key: str) -> dict | None:
    defs = await _load_category_fields(s, listing.category_id)
    return next((
        field for field in defs
        if (str(field.get("key", "")).strip().lower() or "field") == key
    ), None)


async def _owned_market_extra_field(
    s,
    listing_id: int,
    user_id: int,
    key: str,
    expected_type: str,
) -> Listing | None:
    listing = await _owned_market_in_session(s, listing_id, user_id)
    if listing is None:
        return None
    fdef = await _extra_field_def(s, listing, key)
    if str((fdef or {}).get("type", "")).strip().lower() != expected_type:
        return None
    return listing


def _is_youtube_url(value: str) -> bool:
    raw = value.strip()
    if not raw or any(ord(ch) < 33 for ch in raw) or any(ch in raw for ch in {'"', "'", "<", ">", "\\"}):
        return False
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    host = parsed.hostname.lower().rstrip(".")
    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")

def _fmt(val):
    if val is None:
        return "—"
    if isinstance(val, bool):
        return "Да" if val else "Нет"
    if isinstance(val, list):
        return html_escape(", ".join(map(str, val)))
    # строки для видео: если это file_id, скрываем идентификатор; если ссылка — обозначаем
    if isinstance(val, str):
        sval = val.strip()
        low = sval.lower()
        # Для видео вообще не «болтаем» текст в карточке — показываем превью ниже
        if "youtube.com" in low or "youtu.be" in low:
            return "—"
        if len(sval) > 20 and " " not in sval:
            # file_id: тоже «—», реальное превью отправим отдельно
            return "—"
        return html_escape(sval)

    return html_escape(str(val))

def _controls_cancel(listing_id: int):
    # Только «Отменить» (возврат к списку полей)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❎ Отменить", callback_data=f"ef:cancel:{listing_id}")],
    ])

def _ctx(ev):
    if isinstance(ev, CallbackQuery):
        return ev.message.chat.id, ev.message.bot, ev.message.answer
    else:
        return ev.chat.id, ev.bot, ev.answer
async def _render_overview(chat_id: int, bot, send, listing_id: int):
    await clear_bot_messages(chat_id, bot)
    await _clear_user_inputs(chat_id, bot)

    async with SessionLocal() as s:
        listing = await _get_listing(s, listing_id)
        city, cat = await _get_city_cat(s, listing)
        defs = await _load_category_fields(s, listing.category_id)

    # Фото объявления
    photo_ids = []
    if listing.photo_file_id:
        try:
            photo_ids = [x.strip() for x in listing.photo_file_id.split(",") if x.strip()]
        except Exception:
            photo_ids = []

    # flex значения + поиск видео-поля
    try:
        flex_vals = json.loads(listing.flex) if listing.flex else {}
        if not isinstance(flex_vals, dict):
            flex_vals = {}
    except Exception:
        flex_vals = {}

    video_value: str | None = None
    video_key: str | None = None
    for f in defs:
        if str(f.get("type", "")).strip().lower() == "video":
            video_key = (str(f.get("key", "")).strip().lower() or "field")
            val = flex_vals.get(video_key)
            if isinstance(val, str) and val.strip():
                video_value = val.strip()
            break

    # единый ровный список — БЕЗ заголовков «Основные/Дополнительные»
    lines = [
        "🛠 <b>Редактирование объявления</b>",
        f"Город: <b>{html_escape(city.name or '')}</b>",
        f"Категория: <b>{html_escape(cat.name or '')}</b>",
        "",
        f"<b>Заголовок:</b> <i>{_fmt(listing.title)}</i>",
        "",
        f"<b>Цена:</b> <i>{_fmt(listing.price)}</i>",
        "",
        f"<b>Описание:</b> <i>{_fmt(listing.descr)}</i>",
    ]

    # кнопки «Править …» под каждым пунктом
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🖼 Править фото",      callback_data=f"mphoto:open:{listing_id}")],
        [InlineKeyboardButton(text="✏️ Править заголовок", callback_data=f"ef:main:title:{listing_id}")],
        [InlineKeyboardButton(text="✏️ Править цену",      callback_data=f"ef:main:price:{listing_id}")],
        [InlineKeyboardButton(text="✏️ Править описание",  callback_data=f"ef:main:descr:{listing_id}")],
    ]

    # добавить все доп-поля той же лентой
    for f in defs:
        ftype = str((f.get("type") or "")).strip().lower()
        if ftype.startswith("__"):
            continue

        key   = (str(f.get("key", "")).strip().lower() or "field")
        label = f.get("label") or f.get("name") or key
        val   = flex_vals.get(key)

        lines.append("")
        if ftype == "video":
            if isinstance(val, str) and val.strip():
                sval = val.strip()
                low = sval.lower()
                if _is_youtube_url(sval):
                    lines.append(f"<b>{html_escape(str(label))}:</b> {html_escape(sval)}")
                else:
                    lines.append(f"<b>{html_escape(str(label))}:</b> <i>добавлено</i>")
            else:
                lines.append(f"<b>{html_escape(str(label))}:</b> <i>—</i>")
        else:
            lines.append(f"<b>{html_escape(str(label))}:</b> <i>{_fmt(val)}</i>")

        rows.append([
            InlineKeyboardButton(
                text=f"✏️ Править: {label}",
                callback_data=f"ef:extra:{key}:{listing_id}"
            )
        ])

    # Доп. категории
    if _allow_extra_for_category(cat):
        used = _extra_used(listing)
        rows.append([
            InlineKeyboardButton(
                text=f"➕ Доп. категории ({used}/2)",
                callback_data=f"extra:open:{listing_id}"
            )
        ])

    # навигация в самый низ
    # RU: возвращаемся туда, откуда пользователь реально пришёл:
    # из «Моих объявлений», из каталога/категории или из поиска.
    return_cb = edit_return_cb_by_chat.get(chat_id) or f"listing:{listing_id}:{city.slug}:{cat.slug}:my"
    back_text = "⬅️ Назад к результатам поиска" if return_cb == "market_search_results" else "⬅️ Назад к объявлению"
    rows.append([
        InlineKeyboardButton(
            text=back_text,
            callback_data=return_cb
        )
    ])

    main_menu_btn = await get_common_menu_button("main_menu", "ru")
    if main_menu_btn:
        rows.append([main_menu_btn])

    text = "\n".join(lines)
    message_ids = []

    # 1) Сначала показываем фото объявления
    if photo_ids:
        try:
            if len(photo_ids) == 1:
                pmsg = await bot.send_photo(chat_id, photo_ids[0])
                message_ids.append(pmsg.message_id)
            else:
                media = [InputMediaPhoto(media=pid) for pid in photo_ids]
                pmsgs = await bot.send_media_group(chat_id, media=media)
                message_ids.extend([m.message_id for m in pmsgs])
        except Exception:
            pass

    # 2) Потом карточку обзора с кнопками
    msg = await send(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
        disable_web_page_preview=False,
    )
    message_ids.append(msg.message_id)

    # 3) Отдельное сообщение ПОСЛЕ карточки — только для file_id видео
    if video_value:
        sval = video_value
        low  = sval.lower()
        is_youtube_url = _is_youtube_url(sval)
        if not is_youtube_url:
            try:
                vmsg = await bot.send_video(chat_id, sval)
                message_ids.append(vmsg.message_id)
            except Exception:
                try:
                    vmsg2 = await bot.send_message(chat_id, sval)
                    message_ids.append(vmsg2.message_id)
                except Exception:
                    pass

    last_bot_messages[chat_id] = message_ids
    await register_bot_messages(chat_id, message_ids)

    print(
        f"[market_edit_overview.py] {_render_overview.__name__} | "
        f"chat_id={chat_id} | listing_id={listing_id} | "
        f"photos={len(photo_ids)} | flex_fields={len(defs)} | msg_id={msg.message_id}"
    )
# ─────────────────────────────────────────────────────────
# Экран-обзор: точка входа
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("edit_listing_overview:"))
async def edit_listing_overview(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # RU: поддерживаем несколько форматов входа:
    #   edit_listing_overview:<id>
    #   edit_listing_overview:<id>:<city_slug>:<cat_slug>:catalog
    #   edit_listing_overview:<id>:<city_slug>:<cat_slug>:my
    #   edit_listing_overview:<id>:search
    parts = (cb.data or "").split(":")
    try:
        listing_id = int(parts[1])
    except (IndexError, TypeError, ValueError):
        await cb.answer("Некорректные данные.", show_alert=True)
        return

    async with SessionLocal() as s:
        listing = await _owned_market_in_session(s, listing_id, cb.from_user.id)
        if listing is None:
            await cb.answer("Можно редактировать только свои объявления.", show_alert=True)
            return
        city, cat = await _get_city_cat(s, listing)

    # RU: сохраняем реальный маршрут возврата.
    # Если открыли из каталога — вернёмся в карточку без :my,
    # чтобы дальше кнопка «Назад» в карточке вела обратно в список категории.
    # Если открыли из «Моих объявлений» — оставляем :my.
    if len(parts) >= 3 and parts[2] == "search":
        return_cb = "market_search_results"
    elif len(parts) >= 5:
        city_slug = parts[2] or city.slug
        cat_slug = parts[3] or cat.slug
        source = parts[4]
        if source == "my":
            return_cb = f"listing:{listing_id}:{city_slug}:{cat_slug}:my"
        else:
            return_cb = f"listing:{listing_id}:{city_slug}:{cat_slug}"
    else:
        # Старый формат — безопасно оставляем прежнее поведение.
        return_cb = f"listing:{listing_id}:{city.slug}:{cat.slug}:my"

    edit_return_cb_by_chat[chat_id] = return_cb

    await _render_overview(chat_id, cb.message.bot, cb.message.answer, listing_id)
    await state.update_data(ef_listing_id=listing_id, ef_return_cb=return_cb)

    await cb.answer()
    print(
        f"FUNC: {inspect.currentframe().f_code.co_name} | "
        f"chat_id={chat_id} | user_id={cb.from_user.id} | "
        f"listing_id={listing_id} | return_cb={return_cb}"
    )

# ─────────────────────────────────────────────────────────
# Нажатие «Править …» (основные поля)
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.regexp(r"^ef:main:(title|price|descr):(\d+)$"))
async def ef_edit_main(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.message.bot)

    _, _, field, listing_id_str = cb.data.split(":")
    listing_id = int(listing_id_str)
    if not await _authorize_market_callback(cb, listing_id):
        return

    async with SessionLocal() as s:
        listing = await _owned_market_in_session(s, listing_id, cb.from_user.id)
        city, cat = await _get_city_cat(s, listing)

    if field == "title":
        title = "🪧 <b>Заголовок</b>"
        cur   = _fmt(listing.title)
        ask   = "Отправьте новый текст (или нажмите на значение выше, чтобы скопировать):"
    elif field == "price":
        title = "💰 <b>Цена</b>"
        cur   = _fmt(listing.price)
        ask   = "Отправьте новую цену (или нажмите на значение выше, чтобы скопировать):"
    else:
        title = "📝 <b>Описание</b>"
        cur   = _fmt(listing.descr)
        ask   = "Отправьте новый текст (или нажмите на значение выше, чтобы скопировать):"

    kb = _controls_cancel(listing_id)
    msg = await cb.message.answer(
        f"{title}\n\nТекущее значение:\n<code>{cur}</code>\n\n{ask}",
        reply_markup=kb, parse_mode="HTML"
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

    await state.update_data(ef_mode="main", ef_field=field, ef_listing_id=listing_id)
    await state.set_state(OneFieldStates.waiting_value)
    await cb.answer()

    print(f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id={chat_id} | field={field} | listing_id={listing_id} | msg_id={msg.message_id}")


@router.message(OneFieldStates.waiting_value)
async def ef_apply_main_or_extra_textnum(m: Message, state: FSMContext):
    """Применение значения для text/number (как у основного, так и у extra с типом text/number)."""
    chat_id = m.chat.id
    await clear_bot_messages(chat_id, m.bot)
    await _remember_and_delete_user_message(m)

    data = await state.get_data()
    mode   = data.get("ef_mode")          # "main" или "extra"
    field  = data.get("ef_field")         # 'title'/'price'/'descr' ИЛИ extra-key
    try:
        l_id = int(data.get("ef_listing_id"))
    except (TypeError, ValueError):
        await state.clear()
        await m.answer("Сеанс редактирования потерян. Откройте карточку ещё раз.")
        return
    e_type = data.get("ef_type", "text")  # только для extra (text/number)

    new_text = (m.text or "").strip()
    if mode == "main" and field == "title" and not new_text:
        msg = await m.answer("Заголовок не может быть пустым.", reply_markup=_controls_cancel(l_id))
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        return

    async with SessionLocal() as s:
        listing = await _owned_market_in_session(s, l_id, m.from_user.id)
        if listing is None:
            await state.clear()
            await m.answer("Можно редактировать только свои объявления.")
            return

        if mode == "main":
            if field == "title":
                listing.title = new_text
            elif field == "price":
                listing.price = new_text
            else:
                listing.descr = new_text
        elif mode == "extra" and field:
            # extra text/number
            fdef = await _extra_field_def(s, listing, field)
            actual_type = str((fdef or {}).get("type", "")).strip().lower()
            if actual_type not in {"text", "number"} or actual_type != e_type:
                await state.clear()
                await m.answer("Поле больше недоступно для редактирования.")
                return
            try:
                flex = json.loads(listing.flex) if listing.flex else {}
                if not isinstance(flex, dict):
                    flex = {}
            except Exception:
                flex = {}

            if e_type == "number":
                raw = new_text.replace(",", ".")
                try:
                    num = float(raw)
                    if not math.isfinite(num):
                        raise ValueError("non-finite number")
                    if num.is_integer():
                        num = int(num)
                    flex[field] = num
                except Exception:
                    msg = await m.answer("Нужно число. Попробуйте снова.", reply_markup=_controls_cancel(l_id))
                    last_bot_messages[chat_id] = [msg.message_id]
                    await register_bot_messages(chat_id, [msg.message_id])
                    print(f"FUNC: {inspect.currentframe().f_code.co_name} | bad number | text={new_text}")
                    return
            else:
                flex[field] = new_text

            listing.flex = json.dumps(flex, ensure_ascii=False) if flex else None
        else:
            await state.clear()
            await m.answer("Сеанс редактирования повреждён. Откройте карточку ещё раз.")
            return

        await s.commit()

    # безопасный возврат в обзор без _FakeCb
    await _render_overview(chat_id, m.bot, m.answer, l_id)
    await state.clear()

    print(f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id={chat_id} | mode={mode} | field={field} | listing_id={l_id} | saved")


# ─────────────────────────────────────────────────────────
# Нажатие «Править …» (доп. поля)
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.regexp(r"^ef:extra:([^:]+):(\d+)$"))
async def ef_edit_extra(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.message.bot)

    _, _, key, listing_id_str = cb.data.split(":")
    listing_id = int(listing_id_str)
    if not await _authorize_market_callback(cb, listing_id):
        return

    async with SessionLocal() as s:
        listing = await _owned_market_in_session(s, listing_id, cb.from_user.id)
        defs = await _load_category_fields(s, listing.category_id)

    # найдём определение поля
    fdef = next((f for f in defs if (str(f.get("key","")).strip().lower() or "field") == key), None)
    if not fdef:
        await cb.answer("Поле не найдено.", show_alert=True)
        return

    ftype  = str(fdef.get("type", "text"))
    label  = fdef.get("label") or fdef.get("name") or key
    opts   = fdef.get("options") if isinstance(fdef.get("options"), list) else []

    # текущее значение
    cur_val = None
    try:
        flex = json.loads(listing.flex) if listing.flex else {}
        if isinstance(flex, dict):
            cur_val = flex.get(key)
    except Exception:
        pass

    title = f"<b>{html_escape(str(label))}</b>"
    cur_line = f"Текущее значение:\n<code>{_fmt(cur_val)}</code>"

    # интерфейсы по типам
    if ftype in ("text", "number"):
        kb = _controls_cancel(listing_id)
        msg = await cb.message.answer(f"{title}\n\n{cur_line}\n\nВведите значение" + (" (число)" if ftype=='number' else "") + ":", reply_markup=kb, parse_mode="HTML")
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        await state.update_data(ef_mode="extra", ef_field=key, ef_listing_id=listing_id, ef_type=ftype)
        await state.set_state(OneFieldStates.waiting_value)

    elif ftype == "checkbox":
        rows = [[
            InlineKeyboardButton(text="✅ Да",  callback_data=f"efx:checkbox:1:{key}:{listing_id}"),
            InlineKeyboardButton(text="❌ Нет", callback_data=f"efx:checkbox:0:{key}:{listing_id}"),
        ], [
            InlineKeyboardButton(text="❎ Отменить", callback_data=f"ef:cancel:{listing_id}")
        ]]
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        msg = await cb.message.answer(f"{title}\n\n{cur_line}\n\nВыберите вариант:", reply_markup=kb, parse_mode="HTML")
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])

    elif ftype == "video":
        # Запрос на редактирование видео-поля. Покажем текущее видео (если есть) и попросим
        # пользователя отправить новое видео или ссылку на YouTube в одном сообщении.
        # Кнопка «Отменить» позволит вернуться к обзору.
        kb = _controls_cancel(listing_id)
        header = f"{title}\n\n{cur_line}\n\n"
        header += (
            "Отправьте одно сообщение с видео (как Видео или как файл)\n"
            "или пришлите ссылку на YouTube."
        )
        # Отправляем текущее значение: если это file_id (длинная строка без пробелов), то видео;
        # если это ссылка, просто текстом (Telegram сделает превью)
        sent_preview = False
        if isinstance(cur_val, str):
            sval = cur_val.strip()
            low = sval.lower()
            try:
                if (len(sval) > 20 and " " not in sval) and ("http" not in low and "://" not in sval):
                    vmsg = await cb.message.answer_video(sval)
                    sent_preview = True
                    last_bot_messages.setdefault(chat_id, []).append(vmsg.message_id)
                    await register_bot_messages(chat_id, [vmsg.message_id])
                elif "youtube.com" in low or "youtu.be" in low or ("http" in low or "://" in sval):
                    tmsg = await cb.message.answer(sval)
                    sent_preview = True
                    last_bot_messages.setdefault(chat_id, []).append(tmsg.message_id)
                    await register_bot_messages(chat_id, [tmsg.message_id])
            except Exception:
                pass
        # Теперь отправляем инструкцию
        msg = await cb.message.answer(header, reply_markup=kb, parse_mode="HTML")
        last_bot_messages[chat_id] = last_bot_messages.get(chat_id, []) + [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])
        # Устанавливаем режим ожидания видео
        await state.update_data(ef_mode="extra_video", ef_field=key, ef_listing_id=listing_id)
        await state.set_state(ExtraVideoStates.waiting_video)

    elif ftype == "select":
        buttons = [InlineKeyboardButton(text=str(opt), callback_data=f"efx:select:{i}:{key}:{listing_id}") for i, opt in enumerate(opts)]
        row_len = 3 if len(buttons) > 6 else 2
        rows = [buttons[i:i+row_len] for i in range(0, len(buttons), row_len)] if buttons else []
        rows.append([InlineKeyboardButton(text="❎ Отменить", callback_data=f"ef:cancel:{listing_id}")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        msg = await cb.message.answer(f"{title}\n\n{cur_line}\n\nВыберите вариант:", reply_markup=kb, parse_mode="HTML")
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])

    else:
        await cb.answer("Неизвестный тип поля.", show_alert=True)

    await cb.answer()

    print(f"FUNC: {inspect.currentframe().f_code.co_name} | chat_id={chat_id} | listing_id={listing_id} | key={key} | ftype={ftype}")


# checkbox выбор
@router.callback_query(F.data.regexp(r"^efx:checkbox:(0|1):([^:]+):(\d+)$"))
async def efx_checkbox(cb: CallbackQuery, state: FSMContext):
    _, _, bit, key, l_id = cb.data.split(":")
    l_id = int(l_id)
    val = bit == "1"
    if not await _authorize_market_callback(cb, l_id):
        return

    async with SessionLocal() as s:
        listing = await _owned_market_in_session(s, l_id, cb.from_user.id)
        if listing is None:
            await cb.answer("Объявление больше недоступно.", show_alert=True)
            return
        fdef = await _extra_field_def(s, listing, key)
        if str((fdef or {}).get("type", "")).strip().lower() != "checkbox":
            await cb.answer("Поле не найдено.", show_alert=True)
            return
        try:
            flex = json.loads(listing.flex) if listing.flex else {}
            if not isinstance(flex, dict):
                flex = {}
        except Exception:
            flex = {}
        flex[key] = val
        listing.flex = json.dumps(flex, ensure_ascii=False) if flex else None
        await s.commit()

    await _render_overview(cb.message.chat.id, cb.message.bot, cb.message.answer, l_id)
    await state.clear()
    await cb.answer()

    print(f"FUNC: {inspect.currentframe().f_code.co_name} | checkbox saved | key={key} | val={val} | listing_id={l_id}")


# select выбор
@router.callback_query(F.data.regexp(r"^efx:select:(\d+):([^:]+):(\d+)$"))
async def efx_select(cb: CallbackQuery, state: FSMContext):
    _, _, idx_str, key, l_id = cb.data.split(":")
    l_id = int(l_id)
    opt_idx = int(idx_str)
    if not await _authorize_market_callback(cb, l_id):
        return

    async with SessionLocal() as s:
        listing = await _owned_market_in_session(s, l_id, cb.from_user.id)
        if listing is None:
            await cb.answer("Объявление больше недоступно.", show_alert=True)
            return
        fdef = await _extra_field_def(s, listing, key)
        if str((fdef or {}).get("type", "")).strip().lower() != "select":
            await cb.answer("Поле не найдено.", show_alert=True)
            return
        options = (fdef.get("options") if fdef else []) or []
        if not 0 <= opt_idx < len(options):
            await cb.answer("Вариант больше недоступен.", show_alert=True)
            return
        value = options[opt_idx]

        try:
            flex = json.loads(listing.flex) if listing.flex else {}
            if not isinstance(flex, dict):
                flex = {}
        except Exception:
            flex = {}
        flex[key] = value
        listing.flex = json.dumps(flex, ensure_ascii=False) if flex else None
        await s.commit()

    await _render_overview(cb.message.chat.id, cb.message.bot, cb.message.answer, l_id)
    await state.clear()
    await cb.answer()

    print(f"FUNC: {inspect.currentframe().f_code.co_name} | select saved | key={key} | value={value} | listing_id={l_id}")


# ─────────────────────────────────────────────────────────
# Отмена и возврат к обзору
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.regexp(r"^ef:cancel:(\d+)$"))
async def ef_cancel(cb: CallbackQuery, state: FSMContext):
    l_id = int(cb.data.split(":")[2])
    if not await _authorize_market_callback(cb, l_id):
        await state.clear()
        return
    await _render_overview(cb.message.chat.id, cb.message.bot, cb.message.answer, l_id)
    await state.clear()
    await cb.answer()
    print(f"FUNC: {inspect.currentframe().f_code.co_name} | cancel | listing_id={l_id}")


# ─────────────────────────────────────────────────────────
# Обработчики ввода видео при редактировании одного поля
# ─────────────────────────────────────────────────────────

@router.message(ExtraVideoStates.waiting_video, F.video)
async def efx_video_by_video(message: Message, state: FSMContext):
    """
    Сохраняет загруженное видео (как file_id) в flex и возвращает к обзору.
    """
    chat_id = message.chat.id
    await clear_bot_messages(chat_id, message.bot)
    await _remember_and_delete_user_message(message)

    data = await state.get_data()
    key = data.get("ef_field")
    l_id = data.get("ef_listing_id")
    if not key or not l_id:
        # потерян контекст, просто выходим
        await state.clear()
        return
    file_id = message.video.file_id
    async with SessionLocal() as s:
        listing = await _owned_market_extra_field(
            s, int(l_id), message.from_user.id, key, "video"
        )
        if listing is None:
            await state.clear()
            await message.answer("Можно редактировать только свои объявления.")
            return
        # загрузить текущее flex
        try:
            flex = json.loads(listing.flex) if listing.flex else {}
            if not isinstance(flex, dict):
                flex = {}
        except Exception:
            flex = {}
        flex[key] = file_id
        listing.flex = json.dumps(flex, ensure_ascii=False) if flex else None
        await s.commit()
    # показываем обновлённый обзор
    await _render_overview(chat_id, message.bot, message.answer, int(l_id))
    await state.clear()

@router.message(ExtraVideoStates.waiting_video, F.document)
async def efx_video_by_document(message: Message, state: FSMContext):
    """
    Обрабатывает сообщение-документ. Если документ является видео (mime_type
    начинается с video/), сохраняем file_id. Иначе просим повторить.
    """
    chat_id = message.chat.id
    await clear_bot_messages(chat_id, message.bot)
    await _remember_and_delete_user_message(message)

    data = await state.get_data()
    key = data.get("ef_field")
    l_id = data.get("ef_listing_id")
    if not key or not l_id:
        await state.clear()
        return
    async with SessionLocal() as s:
        listing = await _owned_market_extra_field(
            s, int(l_id), message.from_user.id, key, "video"
        )
        if listing is None:
            await state.clear()
            await message.answer("Можно редактировать только свои объявления.")
            return
    doc = message.document
    if doc and doc.mime_type and doc.mime_type.startswith("video/"):
        file_id = doc.file_id
        async with SessionLocal() as s:
            listing = await _owned_market_extra_field(
                s, int(l_id), message.from_user.id, key, "video"
            )
            if listing is None:
                await state.clear()
                await message.answer("Объявление больше недоступно.")
                return
            try:
                flex = json.loads(listing.flex) if listing.flex else {}
                if not isinstance(flex, dict):
                    flex = {}
            except Exception:
                flex = {}
            flex[key] = file_id
            listing.flex = json.dumps(flex, ensure_ascii=False) if flex else None
            await s.commit()
        await _render_overview(chat_id, message.bot, message.answer, int(l_id))
        await state.clear()
        return
    # не видео
    kb = _controls_cancel(int(l_id))
    msg = await message.answer("Это не видео-файл. Отправьте видео (как видео или файл).", reply_markup=kb)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

@router.message(ExtraVideoStates.waiting_video, F.text)
async def efx_video_by_text(message: Message, state: FSMContext):
    """
    Обрабатывает текстовое сообщение. Если это ссылка на YouTube, сохраняем её.
    Иначе просим отправить корректное видео.
    """
    chat_id = message.chat.id
    await clear_bot_messages(chat_id, message.bot)
    await _remember_and_delete_user_message(message)

    data = await state.get_data()
    key = data.get("ef_field")
    l_id = data.get("ef_listing_id")
    if not key or not l_id:
        await state.clear()
        return
    async with SessionLocal() as s:
        listing = await _owned_market_extra_field(
            s, int(l_id), message.from_user.id, key, "video"
        )
    if listing is None:
        await state.clear()
        await message.answer("Можно редактировать только свои объявления.")
        return
    txt = (message.text or "").strip()
    if _is_youtube_url(txt):
        # сохраняем ссылку
        async with SessionLocal() as s:
            listing = await _owned_market_extra_field(
                s, int(l_id), message.from_user.id, key, "video"
            )
            if listing is None:
                await state.clear()
                await message.answer("Можно редактировать только свои объявления.")
                return
            try:
                flex = json.loads(listing.flex) if listing.flex else {}
                if not isinstance(flex, dict):
                    flex = {}
            except Exception:
                flex = {}
            flex[key] = txt
            listing.flex = json.dumps(flex, ensure_ascii=False) if flex else None
            await s.commit()
        await _render_overview(chat_id, message.bot, message.answer, int(l_id))
        await state.clear()
        return
    # не ссылка
    kb = _controls_cancel(int(l_id))
    msg = await message.answer(
        "Это не ссылка на видео. Отправьте видео-файл или ссылку на YouTube.", reply_markup=kb
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

@router.message(ExtraVideoStates.waiting_video)
async def efx_video_wrong_content(message: Message, state: FSMContext):
    """
    Обрабатывает любой другой контент в режиме ожидания видео. Просим отправить
    корректный видео-файл или ссылку.
    """
    chat_id = message.chat.id
    await clear_bot_messages(chat_id, message.bot)
    await _remember_and_delete_user_message(message)

    data = await state.get_data()
    l_id = data.get("ef_listing_id")
    key = data.get("ef_field")
    if l_id and key:
        async with SessionLocal() as s:
            listing = await _owned_market_extra_field(
                s, int(l_id), message.from_user.id, key, "video"
            )
        if listing is None:
            await state.clear()
            await message.answer("Можно редактировать только свои объявления.")
            return
    # покажем стандартную клавиатуру «Отменить»
    kb = _controls_cancel(int(l_id)) if l_id else None
    msg = await message.answer("Нужно отправить видео. Попробуйте ещё раз.", reply_markup=kb)
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

# ─────────────────────────────────────────────────────────
# RU: Открыть мини-меню управления «Доп. категории» для объявления.
#     Зачищаем меню, проверяем разрешение, показываем слоты и кнопки.
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("extra:open:"))
async def extra_open_market(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # РАННИЙ ФИЛЬТР ВЕТКИ: если объявление не из Барахолки (корень != 30) — выходим,
    # чтобы не перехватывать «Услуги». Зачистку выполнит профильный хендлер.
    try:
        _, _, _raw_id = (cb.data or "").split(":")
        _lid = int(_raw_id)
    except Exception:
        return
    async with SessionLocal() as _s:
        _lst = (await _s.execute(select(Listing).where(Listing.id == _lid))).scalar_one_or_none()
        if not _lst or _lst.type != "market":
            return
        _cat = (await _s.execute(select(Category).where(Category.id == _lst.category_id))).scalar_one_or_none()
        if not _cat:
            return
        _root = _cat
        while _root.parent_id is not None:
            _root = (await _s.execute(select(Category).where(Category.id == _root.parent_id))).scalar_one_or_none()
            if not _root:
                return
        if _root.id != 30:
            return

    # 1) Зачистка предыдущих меню/сообщений
    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    # 2) listing_id из callback_data
    try:
        _, _, raw_id = cb.data.split(":")
        listing_id = int(raw_id)
    except Exception:
        await cb.answer("Некорректные данные.", show_alert=True)
        print(f"[market_edit_overview.py] handler=extra_open_market ERROR parse chat_id={chat_id} data={cb.data!r}")
        return
    if not await _authorize_market_callback(cb, listing_id):
        return

    # 3) Загрузить объявление и категорию, проверить разрешение
    async with SessionLocal() as s:
        listing = await _owned_market_in_session(s, listing_id, cb.from_user.id)
        if not listing:
            await cb.answer("Объявление не найдено.", show_alert=True)
            print(f"[market_edit_overview.py] handler=extra_open_market ERROR no_listing listing_id={listing_id} chat_id={chat_id}")
            return
        category = (await s.execute(select(Category).where(Category.id == listing.category_id))).scalar_one()
        if not _allow_extra_for_category(category):
            await cb.answer("Для этой категории доп. категории выключены.", show_alert=True)
            print(f"[market_edit_overview.py] handler=extra_open_market DENY listing_id={listing_id} cat_id={category.id} chat_id={chat_id}")
            return

        used = _extra_used(listing)
        cat1 = (await s.execute(select(Category).where(Category.id == listing.extra_category_id1))).scalar_one_or_none() if listing.extra_category_id1 else None
        cat2 = (await s.execute(select(Category).where(Category.id == listing.extra_category_id2))).scalar_one_or_none() if listing.extra_category_id2 else None

    # 4) Собрать клавиатуру мини-меню
    kb_rows = []
    if used < 2:
        kb_rows.append([InlineKeyboardButton(text="➕ Добавить категорию", callback_data=f"mextra:add:{listing_id}")])
    if cat1:
        kb_rows.append([InlineKeyboardButton(text=f"🗑 Удалить: {cat1.name}", callback_data=f"mextra:del:{listing_id}:1")])
    if cat2:
        kb_rows.append([InlineKeyboardButton(text=f"🗑 Удалить: {cat2.name}", callback_data=f"mextra:del:{listing_id}:2")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mextra:back:{listing_id}")])


    msg = await cb.message.answer(
        f"Доп. категории для «{html_escape(listing.title or '')}»\nЗанято слотов: {used}/2",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML"
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

    await cb.answer()
    print(f"[market_edit_overview.py] handler=extra_open_market OK listing_id={listing_id} used={used} chat_id={chat_id} msg_id={msg.message_id}")

# ─────────────────────────────────────────────────────────
# RU: «⬅️ Назад» из мини-меню «Доп. категории» (Барахолка).
#     Удаляем текущее меню/сообщения и возвращаемся в обзор объявления.
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.regexp(r"^mextra:back:(\d+)$"))
async def mextra_back_market(cb: CallbackQuery, state: FSMContext):
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
    m = re.match(r"^mextra:back:(\d+)$", cb.data or "")
    listing_id = int(m.group(1)) if m else 0
    if not listing_id or not await _authorize_market_callback(cb, listing_id):
        return

    # возврат в обзор редактирования
    await _render_overview(chat_id, cb.message.bot, cb.message.answer, listing_id)

    await cb.answer()
    print(f"[market_edit_overview.py] handler=mextra_back_market OK chat_id={chat_id} listing_id={listing_id}")

# ─────────────────────────────────────────────────────────
# RU: Удалить доп. категорию (слот 1 или 2) в Барахолке.
#     После удаления остаёмся в мини-меню «Доп. категории».
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.regexp(r"^mextra:del:(\d+):(1|2)$"))
async def mextra_del_market(cb: CallbackQuery, state: FSMContext):
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
    m = re.match(r"^mextra:del:(\d+):(1|2)$", cb.data or "")
    if not m:
        await cb.answer("Некорректные данные.", show_alert=True)
        print(f"[market_edit_overview.py] handler=mextra_del_market ERROR parse chat_id={chat_id} data={cb.data!r}")
        return
    listing_id = int(m.group(1))
    slot       = int(m.group(2))
    if not await _authorize_market_callback(cb, listing_id):
        return

    # Сброс выбранного слота и перерисовка мини-меню
    async with SessionLocal() as s:
        listing = await _owned_market_in_session(s, listing_id, cb.from_user.id)
        if not listing:
            await cb.answer("Объявление не найдено.", show_alert=True)
            print(f"[market_edit_overview.py] handler=mextra_del_market ERROR no_listing chat_id={chat_id} listing_id={listing_id}")
            return

        # Обнулить слот
        if slot == 1:
            listing.extra_category_id1 = None
        else:
            listing.extra_category_id2 = None
        await s.commit()

        # Данные для перерисовки меню
        category = (await s.execute(select(Category).where(Category.id == listing.category_id))).scalar_one()

        cat1 = None
        if listing.extra_category_id1:
            cat1 = (await s.execute(select(Category).where(Category.id == listing.extra_category_id1))).scalar_one_or_none()
        cat2 = None
        if listing.extra_category_id2:
            cat2 = (await s.execute(select(Category).where(Category.id == listing.extra_category_id2))).scalar_one_or_none()

        used = int(bool(listing.extra_category_id1)) + int(bool(listing.extra_category_id2))

        # Соберём клавиатуру мини-меню
        kb_rows: list[list[InlineKeyboardButton]] = []
        if used < 2 and _allow_extra_for_category(category):
            kb_rows.append([InlineKeyboardButton(text="➕ Добавить категорию", callback_data=f"mextra:add:{listing_id}")])
        if cat1:
            kb_rows.append([InlineKeyboardButton(text=f"🗑 Удалить: {cat1.name}", callback_data=f"mextra:del:{listing_id}:1")])
        if cat2:
            kb_rows.append([InlineKeyboardButton(text=f"🗑 Удалить: {cat2.name}", callback_data=f"mextra:del:{listing_id}:2")])
        kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mextra:back:{listing_id}")])

    # Показ обновлённого мини-меню (остаёмся на месте)
    msg = await cb.message.answer(
        f"Доп. категории для «{html_escape(listing.title or '')}»\nЗанято слотов: {used}/2",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        parse_mode="HTML",
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer("Доп. категория удалена")
    print(f"[market_edit_overview.py] handler=mextra_del_market OK chat_id={chat_id} listing_id={listing_id} slot={slot} msg_id={msg.message_id}")


# ─────────────────────────────────────────────────────────
# RU: Вспомогательная функция: проверить ветку Барахолки (корень id=30).
# ─────────────────────────────────────────────────────────
async def _is_market_branch(s, cat_id: int) -> bool:
    try:
        cur = (await s.execute(select(Category).where(Category.id == cat_id))).scalar_one_or_none()
        if not cur:
            return False
        while cur.parent_id is not None:
            cur = (await s.execute(select(Category).where(Category.id == cur.parent_id))).scalar_one_or_none()
            if not cur:
                return False
        return cur.id == 30
    except Exception:
        return False


# ─────────────────────────────────────────────────────────
# RU: Открыть выбор доп. категории (верхний уровень — дети корня 30).
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("mextra:add:"))
async def mextra_add_market(cb: CallbackQuery, state: FSMContext):
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
        await cb.answer("Некорректные данные.", show_alert=True)
        print(f"[market_edit_overview.py] handler=mextra_add_market ERROR parse chat_id={chat_id} data={cb.data!r}")
        return
    if not await _authorize_market_callback(cb, listing_id):
        return

    async with SessionLocal() as s:
        listing = await _owned_market_in_session(s, listing_id, cb.from_user.id)
        category = (await s.execute(
            select(Category).where(Category.id == listing.category_id)
        )).scalar_one_or_none()
        if category is None or not _allow_extra_for_category(category):
            await cb.answer("Для этой категории доп. категории выключены.", show_alert=True)
            return
        if _extra_used(listing) >= 2:
            await cb.answer("Все два слота уже заняты.", show_alert=True)
            return
        cats = (await s.execute(select(Category).where(Category.parent_id == 30))).scalars().all()
        rows = [[InlineKeyboardButton(text=c.name, callback_data=f"mextra:pick:{listing_id}:{c.id}")] for c in cats]
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mextra:back:{listing_id}")])

    msg = await cb.message.answer(
        "Выберите категорию (Барахолка):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"[market_edit_overview.py] handler=mextra_add_market OK chat_id={chat_id} listing_id={listing_id} msg_id={msg.message_id}")


# ─────────────────────────────────────────────────────────
# RU: Клик по категории при выборе доп. категории:
#     • есть дети — углубляемся
#     • лист — записываем в первый свободный слот, с проверками
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("mextra:pick:"))
async def mextra_pick_market(cb: CallbackQuery, state: FSMContext):
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
        _, _, raw_lid, raw_cid = (cb.data or "").split(":")
        listing_id = int(raw_lid)
        cat_id     = int(raw_cid)
    except Exception:
        await cb.answer("Некорректные данные.", show_alert=True)
        print(f"[market_edit_overview.py] handler=mextra_pick_market ERROR parse chat_id={chat_id} data={cb.data!r}")
        return
    if not await _authorize_market_callback(cb, listing_id):
        return

    async with SessionLocal() as s:
        cat = (await s.execute(select(Category).where(Category.id == cat_id))).scalar_one_or_none()
        if not cat:
            await cb.answer("Категория не найдена.", show_alert=True)
            print(f"[market_edit_overview.py] handler=mextra_pick_market ERROR no_cat chat_id={chat_id} cat_id={cat_id}")
            return

        children = (await s.execute(select(Category).where(Category.parent_id == cat_id))).scalars().all()
        if children:
            rows = [[InlineKeyboardButton(text=c.name, callback_data=f"mextra:pick:{listing_id}:{c.id}")] for c in children]
            if cat.parent_id and cat.parent_id != 30:
                rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mextra:up:{listing_id}:{cat.parent_id}")])
            else:
                rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mextra:add:{listing_id}")])

            msg = await cb.message.answer(
                f"Категория: <b>{html_escape(cat.name or '')}</b>\nВыберите подкатегорию:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                parse_mode="HTML",
            )
            last_bot_messages[chat_id] = [msg.message_id]
            await register_bot_messages(chat_id, [msg.message_id])
            await cb.answer()
            print(f"[market_edit_overview.py] handler=mextra_pick_market NAV chat_id={chat_id} listing_id={listing_id} cat_id={cat_id} msg_id={msg.message_id}")
            return

        listing = await _owned_market_in_session(s, listing_id, cb.from_user.id)
        if not listing:
            await cb.answer("Объявление не найдено.", show_alert=True)
            print(f"[market_edit_overview.py] handler=mextra_pick_market ERROR no_listing chat_id={chat_id} listing_id={listing_id}")
            return

        category = (await s.execute(
            select(Category).where(Category.id == listing.category_id)
        )).scalar_one_or_none()
        if category is None or not _allow_extra_for_category(category):
            await cb.answer("Для этой категории доп. категории выключены.", show_alert=True)
            return

        if not await _is_market_branch(s, cat_id):
            await cb.answer("Можно выбирать только категории Барахолки.", show_alert=True)
            print(f"[market_edit_overview.py] handler=mextra_pick_market REJECT branch chat_id={chat_id} cat_id={cat_id}")
            back_cb = f"mextra:up:{listing_id}:{cat.parent_id}" if (cat.parent_id and cat.parent_id != 30) else f"mextra:add:{listing_id}"
            back_msg = await cb.message.answer("Выберите категорию (Барахолка):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)]
            ]))
            last_bot_messages[chat_id] = [back_msg.message_id]
            await register_bot_messages(chat_id, [back_msg.message_id])
            await cb.answer()
            return

        if cat_id == listing.category_id:
            await cb.answer("Нельзя выбирать основную категорию объявления.", show_alert=True)
            print(f"[market_edit_overview.py] handler=mextra_pick_market REJECT base chat_id={chat_id} listing_id={listing_id} cat_id={cat_id}")
            back_msg = await cb.message.answer("Выберите другую категорию (Барахолка):",
                                               reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                                   [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mextra:add:{listing_id}")]
                                               ]),
                                               parse_mode="HTML")
            last_bot_messages[chat_id] = [back_msg.message_id]
            await register_bot_messages(chat_id, [back_msg.message_id])
            await cb.answer()
            return

        if listing.extra_category_id1 == cat_id or listing.extra_category_id2 == cat_id:
            await cb.answer("Такая доп. категория уже выбрана.", show_alert=True)
            print(f"[market_edit_overview.py] handler=mextra_pick_market REJECT dup chat_id={chat_id} listing_id={listing_id} cat_id={cat_id}")
            back_msg = await cb.message.answer("Выберите другую категорию (Барахолка):",
                                               reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                                   [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mextra:add:{listing_id}")]
                                               ]),
                                               parse_mode="HTML")
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
                kb_rows.append([InlineKeyboardButton(text=f"🗑 Удалить: {cat1.name}", callback_data=f"mextra:del:{listing_id}:1")])
            if cat2:
                kb_rows.append([InlineKeyboardButton(text=f"🗑 Удалить: {cat2.name}", callback_data=f"mextra:del:{listing_id}:2")])
            kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mextra:back:{listing_id}")])
            msg = await cb.message.answer(
                f"Доп. категории для «{html_escape(listing.title or '')}»\nЗанято слотов: 2/2",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
                parse_mode="HTML",
            )
            last_bot_messages[chat_id] = [msg.message_id]
            await register_bot_messages(chat_id, [msg.message_id])
            await cb.answer()
            print(f"[market_edit_overview.py] handler=mextra_pick_market FULL chat_id={chat_id} listing_id={listing_id} msg_id={msg.message_id}")
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
            kb_rows.append([InlineKeyboardButton(text="➕ Добавить категорию", callback_data=f"mextra:add:{listing_id}")])
        if cat1:
            kb_rows.append([InlineKeyboardButton(text=f"🗑 Удалить: {cat1.name}", callback_data=f"mextra:del:{listing_id}:1")])
        if cat2:
            kb_rows.append([InlineKeyboardButton(text=f"🗑 Удалить: {cat2.name}", callback_data=f"mextra:del:{listing_id}:2")])
        kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mextra:back:{listing_id}")])

    msg = await cb.message.answer(
        f"Доп. категории для «{html_escape(listing.title or '')}»\nЗанято слотов: {used}/2",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        parse_mode="HTML",
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer("Доп. категория добавлена")
    print(f"[market_edit_overview.py] handler=mextra_pick_market OK chat_id={chat_id} listing_id={listing_id} cat_id={cat_id} used={used} msg_id={msg.message_id}")


# ─────────────────────────────────────────────────────────
# RU: Подняться на уровень выше при выборе доп. категории.
# ─────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("mextra:up:"))
async def mextra_up_market(cb: CallbackQuery, state: FSMContext):
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
        _, _, raw_lid, raw_pid = (cb.data or "").split(":")
        listing_id = int(raw_lid)
        parent_id  = int(raw_pid)
    except Exception:
        await cb.answer("Некорректные данные.", show_alert=True)
        print(f"[market_edit_overview.py] handler=mextra_up_market ERROR parse chat_id={chat_id} data={cb.data!r}")
        return
    if not await _authorize_market_callback(cb, listing_id):
        return

    async with SessionLocal() as s:
        parent = (await s.execute(select(Category).where(Category.id == parent_id))).scalar_one_or_none()
        if not parent or parent.id == 30:
            cats = (await s.execute(select(Category).where(Category.parent_id == 30))).scalars().all()
            rows = [[InlineKeyboardButton(text=c.name, callback_data=f"mextra:pick:{listing_id}:{c.id}")] for c in cats]
            rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mextra:back:{listing_id}")])
            msg = await cb.message.answer(
                "Выберите категорию (Барахолка):",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                parse_mode="HTML",
            )
            last_bot_messages[chat_id] = [msg.message_id]
            await register_bot_messages(chat_id, [msg.message_id])
            await cb.answer()
            print(f"[market_edit_overview.py] handler=mextra_up_market ROOT chat_id={chat_id} listing_id={listing_id} msg_id={msg.message_id}")
            return

        children = (await s.execute(select(Category).where(Category.parent_id == parent.id))).scalars().all()
        rows = [[InlineKeyboardButton(text=c.name, callback_data=f"mextra:pick:{listing_id}:{c.id}")] for c in children]
        if parent.parent_id and parent.parent_id != 30:
            rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mextra:up:{listing_id}:{parent.parent_id}")])
        else:
            rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mextra:add:{listing_id}")])

    msg = await cb.message.answer(
        f"Категория: <b>{html_escape(parent.name or '')}</b>\nВыберите подкатегорию:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"[market_edit_overview.py] handler=mextra_up_market OK chat_id={chat_id} listing_id={listing_id} parent_id={parent_id} msg_id={msg.message_id}")
