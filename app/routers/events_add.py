from aiogram import Router, F
from aiogram.types import (
    CallbackQuery, Message,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import re

from app.routers.utils import clear_bot_messages, last_bot_messages, get_text, register_bot_messages, escape_html
from app.database import SessionLocal
from app.events_meta import ensure_events_meta
from sqlalchemy import text as sql
from app.keyboards import get_common_menu_button, events_main_inline
from app.routers.utils import log


router = Router()

_TZ = ZoneInfo("Europe/Belgrade")
_MAX_MONTHS_AHEAD = 6

# ===================== ХЕЛПЕРЫ И УТИЛИТЫ =====================

# Коротко: удаляем nav/prompt, что могли остаться в FSM
async def _drop_nav_and_prompt(state: FSMContext, chat_id: int, bot):
    data = await state.get_data()
    for key in ("nav_msg_id", "prompt_id"):
        mid = data.get(key)
        if mid:
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:
                pass
    await state.update_data(nav_msg_id=None, prompt_id=None)

# Жёсткий ресет UI шага: удаляем nav/prompt и все служебные сообщения
async def _reset_step_ui(state: FSMContext, chat_id: int, bot):
    data = await state.get_data()
    for key in ("nav_msg_id", "prompt_id"):
        mid = data.get(key)
        if mid:
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:
                pass
    await state.update_data(nav_msg_id=None, prompt_id=None)
    try:
        await clear_bot_messages(chat_id, bot)
    except Exception:
        pass
    last_bot_messages[chat_id] = []
    print(f"[AFISHA][RESET][done] chat={chat_id}")


# ===================== DRAFT (СВОДКА ВВЕДЁННЫХ ДАННЫХ) =====================
# Идея: пользователю всегда видна «черновая карточка» (одно сообщение), которая
# обновляется по мере заполнения полей. Черновик НЕ добавляем в last_bot_messages,
# чтобы clear_bot_messages() его не удалял на каждом шаге. Удаляем черновик только
# при Publish/Cancel/Back->entry.

def _draft_text_from_data(data: dict, city_name: str | None = None) -> str:
    # Важно: делаем компактно, но с заголовками полей (как вы просили).
    title = (data.get("title") or "—")
    date_local = data.get("date_local") or "—"       # YYYY-MM-DD
    hh = data.get("time_hh"); mm = data.get("time_mm")
    time_str = "—"
    if isinstance(hh, int) and isinstance(mm, int):
        time_str = f"{hh:02d}:{mm:02d}"

    city_text = data.get("city_text")
    city_str = (city_name or "").strip() or (city_text or "—")

    price = data.get("price_text") or "—"
    venue = data.get("venue_text") or "—"
    descr = data.get("descr") or "—"
    photo_id = data.get("photo_id")
    photo_str = "✅" if photo_id else "—"

    # Делаем аккуратные заголовки (HTML).
    parts = [
        "🧾 <b>Черновик объявления</b>",
        f"<b>Название:</b> {escape_html(title)}",
        f"<b>Дата:</b> {escape_html(date_local)}",
        f"<b>Время:</b> {escape_html(time_str)}",
        f"<b>Город:</b> {escape_html(city_str)}",
        f"<b>Цена:</b> {escape_html(price)}",
        f"<b>Площадка:</b> {escape_html(venue)}",
        f"<b>Описание:</b> {escape_html(descr)}",
        f"<b>Фото:</b> {photo_str}",
    ]
    return "\n".join(parts)

async def _update_draft(bot, chat_id: int, state: FSMContext, reply_markup: InlineKeyboardMarkup | None = None):
    """
    Создаёт/обновляет одно сообщение-черновик и хранит его message_id в FSM:
      - draft_msg_id
      - draft_is_photo (0/1)
    """
    data = await state.get_data()
    draft_id = data.get("draft_msg_id")
    draft_is_photo = int(data.get("draft_is_photo") or 0)

    # город (если выбран по id) — подтянем имя, чтобы было красиво в черновике
    city_name = None
    try:
        cid = data.get("city_id")
        if isinstance(cid, int):
            city_name = await _city_name_by_id(cid)
    except Exception:
        city_name = None

    text = _draft_text_from_data(data, city_name=city_name)

    # Если черновик ещё не создан — создаём текстовое сообщение
    if not draft_id:
        msg = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
        await state.update_data(draft_msg_id=msg.message_id, draft_is_photo=0)
        print(f"[AFISHA][DRAFT] created | chat={chat_id} msg_id={msg.message_id}")
        return msg.message_id

    # Если ранее был фото-черновик, а мы хотим текст — проще пересоздать
    if draft_is_photo:
        try:
            await bot.delete_message(chat_id, int(draft_id))
        except Exception:
            pass
        msg = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
        await state.update_data(draft_msg_id=msg.message_id, draft_is_photo=0)
        print(f"[AFISHA][DRAFT] recreated(text) | chat={chat_id} old_id={draft_id} new_id={msg.message_id}")
        return msg.message_id

    # Обычное обновление текста
    try:
        await bot.edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=int(draft_id),
            parse_mode="HTML",
            reply_markup=reply_markup
        )
        print(f"[AFISHA][DRAFT] updated | chat={chat_id} msg_id={draft_id}")

    except Exception as e:
        # ВАЖНО: если Telegram отвечает "message is not modified" — это НЕ ошибка логики,
        # просто текст/кнопки не изменились. В этом случае ничего не делаем, чтобы не плодить дубли.
        err_txt = str(e).lower()
        if "message is not modified" in err_txt:
            print(f"[AFISHA][DRAFT] not modified (skip recreate) | chat={chat_id} msg_id={draft_id} err={e}")
            return int(draft_id)

        # Иначе — реально не смогли отредактировать (например, сообщение удалили) → пересоздаём
        print(f"[AFISHA][DRAFT] edit failed -> recreate | chat={chat_id} msg_id={draft_id} err={e}")
        msg = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
        await state.update_data(draft_msg_id=msg.message_id, draft_is_photo=0)
        return msg.message_id

    return int(draft_id)

async def _delete_draft(bot, chat_id: int, state: FSMContext):
    data = await state.get_data()
    mid = data.get("draft_msg_id")
    if mid:
        try:
            await bot.delete_message(chat_id, int(mid))
        except Exception:
            pass
    await state.update_data(draft_msg_id=None, draft_is_photo=0)
    print(f"[AFISHA][DRAFT] deleted | chat={chat_id} mid={mid}")


# Навигационная панель «Назад + Главное меню» и фиксация nav_msg_id
async def _send_nav(chat_id: int, bot, state: FSMContext, back_cb: str | None):
    try:
        nav_text = await get_text('return_to_menu', 'ru') or "Возврат -db"
    except Exception:
        nav_text = "Возврат -db"

    buttons = []
    if back_cb:
        buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=back_cb))
    try:
        main_btn = await get_common_menu_button('main_menu')
        if main_btn:
            buttons.append(InlineKeyboardButton(text=main_btn.text, callback_data=main_btn.callback_data))
    except Exception:
        pass

    kb = InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None
    nav_msg = await bot.send_message(chat_id, nav_text, reply_markup=kb, parse_mode="HTML")
    await state.update_data(nav_msg_id=nav_msg.message_id)
    last_bot_messages.setdefault(chat_id, []).append(nav_msg.message_id)
    await register_bot_messages(chat_id, [nav_msg.message_id])
    return nav_msg.message_id

# Удаление пользовательского сообщения безопасно
async def _delete_user_msg(bot, chat_id: int, msg_id: int | None):
    if not msg_id:
        return
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass

# ===================== FSM СОСТОЯНИЯ =====================

class AfishaAddStates(StatesGroup):
    wait_title = State()
    wait_date = State()
    wait_time = State()
    wait_city_choice = State()
    wait_city_text = State()
    wait_price_choice = State()
    wait_price_text = State()
    wait_venue = State()
    wait_descr = State()
    wait_photo = State()
    preview = State()


class AfishaEditStates(StatesGroup):
    title = State()
    date = State()
    time = State()
    price = State()
    city = State()
    city_text = State()
    venue = State()
    descr = State()
    photo = State()


# ===================== ВАЛИДАЦИЯ / ФОРМАТ =====================

def _strip(s: str | None) -> str:
    return (s or "").strip()

def _valid_title(s: str) -> bool:
    return 1 <= len(s) <= 100

def _parse_date_ddmmyy(raw: str) -> datetime | None:
    """
    Принимает:
    - 07.10.25 / 07-10-25 / 07/10/25
    - 07.10.2025 / 07-10-2025 / 07/10/2025
    - 071025 (DDMMYY)
    - 07102025 (DDMMYYYY)
    - 2026-02-20 / 2026.02.20 / 2026/02/20 (на всякий случай)
    Возвращает datetime(year, month, day) или None
    """
    s = (raw or "").strip()
    if not s:
        return None

    # 1) Слитные форматы (оставляем только цифры)
    digits = re.sub(r"\D", "", s)
    try:
        if len(digits) == 6:  # DDMMYY
            day = int(digits[0:2])
            month = int(digits[2:4])
            year = 2000 + int(digits[4:6])
            return datetime(year, month, day)

        if len(digits) == 8:
            # Может быть DDMMYYYY или YYYYMMDD — различаем по первой паре
            a = int(digits[0:2])
            b = int(digits[2:4])
            c = int(digits[4:8])
            if 1 <= a <= 31 and 1 <= b <= 12 and 2000 <= c <= 2100:
                # DDMMYYYY
                return datetime(c, b, a)

            year = int(digits[0:4])
            month = int(digits[4:6])
            day = int(digits[6:8])
            if 2000 <= year <= 2100:
                return datetime(year, month, day)
    except ValueError:
        return None

    # 2) Форматы с разделителями
    patterns = (
        "%d.%m.%y", "%d-%m-%y", "%d/%m/%y",
        "%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y",
        "%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d",
    )
    for fmt in patterns:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue

    return None


def _parse_time_hhmm(s: str) -> tuple[int, int] | None:
    """Гибкий парсер времени.
    Поддержка:
      - HH:MM, H:MM
      - HH.MM, HH-MM, HH MM
      - HHMM / HMM (например 930 -> 09:30, 2030 -> 20:30)
      - HH (интерпретируем как HH:00)
    """
    s = s.strip()
    if not s:
        return None

    s_low = s.lower().strip()

    # чисто цифры: 3-4 знака (HMM/HHMM) или 1-2 (HH)
    if re.fullmatch(r"\d{1,4}", s_low):
        n = s_low
        if len(n) <= 2:
            hh = int(n); mm = 0
        else:
            # HMM / HHMM
            hh = int(n[:-2]); mm = int(n[-2:])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return (hh, mm)
        return None

    # разделители: :, ., -, пробел
    s_norm = re.sub(r"[\s]+", ":", s_low)
    s_norm = s_norm.replace(".", ":").replace("-", ":")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s_norm)
    if not m:
        return None
    hh, mm = map(int, m.groups())
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return (hh, mm)
    return None

def _is_past_or_too_far(date_local: datetime) -> str | None:
    now_local = datetime.now(_TZ).date()
    if date_local.date() < now_local:
        return "Дата в прошлом. Укажите будущую дату."
    limit = (now_local.replace(day=1) + timedelta(days=31*_MAX_MONTHS_AHEAD))
    if date_local.date() > limit:
        return f"Слишком далеко. Не позже, чем через {_MAX_MONTHS_AHEAD} месяцев."
    return None

def _to_start_utc(date_local_str: str, hh: int, mm: int) -> int:
    dt_local = datetime.strptime(date_local_str, "%Y-%m-%d").replace(hour=int(hh), minute=int(mm))
    return int(dt_local.replace(tzinfo=_TZ).astimezone(timezone.utc).timestamp())

# ===================== БД / СПРАВОЧНИКИ =====================

async def _city_name_by_id(city_id: int | None) -> str | None:
    if not city_id:
        return None
    try:
        async with SessionLocal() as s:
            res = await s.execute(sql("SELECT name FROM city WHERE id = :id LIMIT 1"), {"id": city_id})
            row = res.first()
            return row[0] if row else None
    except Exception as e:
        print(f"[AFISHA] city_name_by_id error: {e}")
        return None


async def _other_city_id() -> int | None:
    """ID справочной строки ``slug=other`` для произвольного города."""
    try:
        async with SessionLocal() as s:
            row = (await s.execute(sql(
                "SELECT id FROM city WHERE lower(slug)='other' ORDER BY id LIMIT 1"
            ))).first()
            return int(row[0]) if row else None
    except Exception as e:
        print(f"[AFISHA] other_city_id error: {e}")
        return None


async def _event_root_category_id(session) -> int:
    """Resolve the root Afisha category without reusing a seeded child slug."""
    query = sql("""
        SELECT id
        FROM category
        WHERE lower(trim(slug))='event' AND parent_id IS NULL
        ORDER BY id
    """)
    roots = [int(row[0]) for row in (await session.execute(query)).fetchall()]
    if len(roots) > 1:
        raise RuntimeError("Found multiple root categories with slug='event'; resolve the catalog conflict")
    if roots:
        return roots[0]

    await session.execute(sql("""
        INSERT INTO category (slug, name, parent_id, fields)
        SELECT 'event', 'Афиша', NULL, NULL
        WHERE NOT EXISTS (
            SELECT 1 FROM category
            WHERE lower(trim(slug))='event' AND parent_id IS NULL
        )
    """))
    roots = [int(row[0]) for row in (await session.execute(query)).fetchall()]
    if len(roots) != 1:
        raise RuntimeError("Could not create an unambiguous root category for Afisha")
    return roots[0]


async def _mark_event_pending(session, listing_id: int) -> None:
    """Send an edited event back to moderation."""
    await session.execute(sql("""
        UPDATE events_meta
        SET status='pending', updated_at=strftime('%s','now')
        WHERE listing_id=:id
    """), {"id": int(listing_id)})


async def _kb_city_from_db() -> InlineKeyboardMarkup:
    rows = []
    try:
        async with SessionLocal() as s:
            res = await s.execute(sql("SELECT id, name FROM city ORDER BY id ASC LIMIT 20"))
            rows = [tuple(r) for r in res.fetchall()]
    except Exception as e:
        print(f"[AFISHA] load cities error: {e}")
        rows = []
    btn_rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for cid, name in rows:
        row.append(InlineKeyboardButton(text=str(name), callback_data=f"af:add:city:{cid}"))
        if len(row) == 2:
            btn_rows.append(row); row = []
    if row:
        btn_rows.append(row)
    btn_rows.append([InlineKeyboardButton(text="Другой", callback_data="af:add:city:other")])
    return InlineKeyboardMarkup(inline_keyboard=btn_rows)

async def _kb_city_from_db_edit() -> InlineKeyboardMarkup:
    btn_rows = []
    row = []
    try:
        async with SessionLocal() as s:
            res = await s.execute(sql("SELECT id, name FROM city ORDER BY id ASC LIMIT 20"))
            cities = res.all()
    except Exception as e:
        cities = []
        print(f"[events_add.py][kb_city_from_db_edit][db.fail] {type(e).__name__}: {e}")

    for cid, name in cities:
        row.append(InlineKeyboardButton(text=str(name), callback_data=f"af:editc:{cid}"))
        if len(row) == 2:
            btn_rows.append(row)
            row = []
    if row:
        btn_rows.append(row)

    btn_rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="af:editf:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=btn_rows)

def _kb_skip(cbdata: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пропустить", callback_data=cbdata)]
    ])

def _kb_price() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Бесплатно", callback_data="af:add:price:free")],
        [InlineKeyboardButton(text="На донатах", callback_data="af:add:price:donate")],
        [InlineKeyboardButton(text="Ввести цену", callback_data="af:add:price:custom")],
    ])

def _kb_preview() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Опубликовать ✅", callback_data="af:add:publish")],
        [InlineKeyboardButton(text="Исправить ✏️", callback_data="af:add:edit")],
        [InlineKeyboardButton(text="Отмена", callback_data="af:add:cancel")],
    ])

# --- Afisha "edit after publish" (overview) ---
def _af_dbg(func: str, stage: str, msg: str = "") -> None:
    print(f"[events_add.py][{func}][{stage}] {msg}".rstrip())


def _extract_back_cb_from_markup(msg: Message) -> str:
    """
    Нюанс: edit-кнопка сейчас не несёт offset страницы "Мои".
    Поэтому берём callback "◀️ Назад" прямо из markup карточки, откуда нажали "Редактировать".
    """
    try:
        rm = msg.reply_markup
        if not rm or not rm.inline_keyboard:
            return "af:my"
        for row in rm.inline_keyboard:
            for btn in row:
                if getattr(btn, "text", "") == "◀️ Назад" and getattr(btn, "callback_data", None):
                    return btn.callback_data
    except Exception:
        pass
    return "af:my"


def _kb_afisha_edit_overview(listing_id: int, back_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Название", callback_data=f"af:editf:title:{listing_id}")],
        [InlineKeyboardButton(text="✏️ Дата", callback_data=f"af:editf:date:{listing_id}")],
        [InlineKeyboardButton(text="✏️ Время", callback_data=f"af:editf:time:{listing_id}")],
        [InlineKeyboardButton(text="✏️ Город", callback_data=f"af:editf:city:{listing_id}")],
        [InlineKeyboardButton(text="✏️ Место", callback_data=f"af:editf:venue:{listing_id}")],
        [InlineKeyboardButton(text="✏️ Цена", callback_data=f"af:editf:price:{listing_id}")],
        [InlineKeyboardButton(text="✏️ Описание", callback_data=f"af:editf:descr:{listing_id}")],
        [InlineKeyboardButton(text="✏️ Фото", callback_data=f"af:editf:photo:{listing_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=back_cb)],
        [InlineKeyboardButton(text="≡ Главное меню", callback_data="main_menu")],
    ])


async def _render_afisha_edit_overview(chat_id: int, bot, state: FSMContext) -> int:
    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    title = data.get("title") or "—"

    # исходные данные (как раньше)
    date_local = data.get("date_local") or "—"   # ожидается YYYY-MM-DD
    time_hh = data.get("time_hh")
    time_mm = data.get("time_mm")

    # формат для карточки: дата DD-MM-YY
    date_card = "—"
    if date_local and date_local != "—":
        try:
            dt_tmp = datetime.strptime(date_local, "%Y-%m-%d")
            date_card = dt_tmp.strftime("%d-%m-%y")
        except Exception:
            # если вдруг в FSM лежит уже не YYYY-MM-DD — покажем как есть
            date_card = date_local

    # время (как раньше)
    time_card = "—"
    if time_hh is not None and time_mm is not None:
        try:
            time_card = f"{int(time_hh):02d}:{int(time_mm):02d}"
        except Exception:
            pass

    city_text = data.get("city_text") or "—"
    # если где-то сохранился префикс "Другой:" — убираем его в отображении
    if isinstance(city_text, str):
        ct = city_text.strip()
        if ct.lower().startswith("другой:"):
            city_text = ct.split(":", 1)[1].strip() or "—"
    venue_text = data.get("venue_text") or "—"
    price_text = data.get("price_text") or "—"
    descr = (data.get("descr") or "").strip()
    descr_short = (descr[:180] + "…") if len(descr) > 180 else (descr or "—")
    photo_id = data.get("photo_id")

    back_cb = data.get("edit_back_cb") or "af:my"

    text = (
        "✏️ <b>Редактирование объявления</b>\n"
        f"ID: <code>{listing_id}</code>\n\n"
        f"<b>Название:</b> {escape_html(title)}\n"
        f"<b>Дата:</b> {escape_html(date_card)}\n"
        f"<b>Время:</b> {escape_html(time_card)}\n"
        f"<b>Город:</b> {escape_html(city_text)}\n"
        f"<b>Место:</b> {escape_html(venue_text)}\n"
        f"<b>Цена:</b> {escape_html(price_text)}\n"
        f"<b>Описание:</b> {escape_html(descr_short)}\n"
        f"<b>Фото:</b> {'есть' if photo_id else 'нет'}\n\n"
        "Выберите поле для редактирования:"
    )

    msg = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=_kb_afisha_edit_overview(int(listing_id), back_cb),
    )
    await state.update_data(edit_overview_msg_id=msg.message_id)
    return msg.message_id


# ===================== ХЕНДЛЕРЫ ДОБАВЛЕНИЯ =====================

# Публикация Афиши: вход по кнопке «Разместить»
@router.callback_query(F.data == "event_new")
async def afisha_add_entry(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    print(f"[AFISHA][ENTRY] afisha_add_entry | chat_id={chat_id}")
    try:
        await cb.message.delete()
    except Exception:
        pass
    await _reset_step_ui(state, chat_id, cb.bot)
    await state.clear()
    await _update_draft(cb.bot, chat_id, state)

    await _send_nav(chat_id, cb.bot, state, back_cb="af:add:back:entry")
    await state.set_state(AfishaAddStates.wait_title)
    msg = await cb.message.answer(
        "📝 Введите <b>название</b> события (до 100 символов):",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.update_data(prompt_id=msg.message_id)
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"[AFISHA][AFTER] afisha_add_entry | chat_id={chat_id}")

@router.callback_query(F.data.startswith("af:my:edit:"))
async def afisha_edit_entry(cb: CallbackQuery, state: FSMContext):
    func = "afisha_edit_entry"
    chat_id = cb.message.chat.id
    owner_id = cb.from_user.id

    back_cb_list = _extract_back_cb_from_markup(cb.message)  # af:my или af:my:more:<offset>

    try:
        listing_id = int(cb.data.split(":")[-1])
    except Exception:
        await cb.answer("Не удалось открыть редактирование.")
        _af_dbg(func, "parse_failed", f"chat_id={chat_id} data={cb.data!r}")
        return

    offset = "0"
    try:
        if isinstance(back_cb_list, str) and back_cb_list.startswith("af:my:more:"):
            offset = back_cb_list.split(":")[-1] or "0"
    except Exception:
        offset = "0"

    # ✅ вот правильный back_cb: вернуться в карточку этого объявления
    back_cb = f"af:my:open:{listing_id}:{offset}"

    _af_dbg(func, "enter", f"listing_id={listing_id} chat_id={chat_id} back_cb={back_cb!r}")

    # убираем карточку "моего объявления"
    try:
        await cb.message.delete()
    except Exception as e:
        _af_dbg(func, "delete_card_fail", f"{type(e).__name__}: {e}")

    # чистим возможные хвосты "мастера добавления", но FSM дальше используем под overview
    await _reset_step_ui(state, chat_id, cb.bot)
    await state.clear()

    # грузим данные из БД
    try:
        async with SessionLocal() as s:
            res = await s.execute(sql("""
                SELECT
                    l.id, l.owner_id, l.city_id, l.title, l.price, l.descr, l.photo_file_id,
                    em.start_at_utc, em.venue_text, em.city_text, em.price_text
                FROM listing l
                JOIN events_meta em ON em.listing_id = l.id
                WHERE l.id = :id AND l.type='events' AND l.status='active' AND l.is_sold=0
                LIMIT 1
            """), {"id": listing_id})
            row = res.first()
    except Exception as e:
        row = None
        _af_dbg(func, "db_error", f"{type(e).__name__}: {e}")

    if not row:
        await cb.answer("Объявление не найдено.")
        _af_dbg(func, "not_found", f"id={listing_id} chat_id={chat_id}")
        return

    if int(row[1]) != int(owner_id):
        await cb.answer("Это не ваше объявление.")
        _af_dbg(func, "deny", f"id={listing_id} chat_id={chat_id} owner_id={owner_id} db_owner={row[1]}")
        return

    _lid, _owner, city_id, title, price, descr, photo_id, start_at_utc, venue_text, city_text, price_text = row

    # обратно в локальные дату/время
    try:
        dt_local = datetime.fromtimestamp(int(start_at_utc), tz=timezone.utc).astimezone(_TZ)
        date_local = dt_local.strftime("%Y-%m-%d")
        time_hh = int(dt_local.strftime("%H"))
        time_mm = int(dt_local.strftime("%M"))
    except Exception as e:
        _af_dbg(func, "dt_parse_fail", f"{type(e).__name__}: {e}")
        date_local = None
        time_hh = None
        time_mm = None

    await state.update_data(
        edit_listing_id=int(listing_id),
        edit_back_cb=back_cb,
        title=title,
        date_local=date_local,
        time_hh=time_hh,
        time_mm=time_mm,
        city_id=int(city_id) if city_id is not None else None,
        city_text=city_text,
        # нюанс: в БД есть и listing.price, и events_meta.price_text — пока показываем price_text, иначе price
        price_text=price_text or (price or None),
        venue_text=venue_text,
        descr=descr,
        photo_id=photo_id,
    )

    mid = await _render_afisha_edit_overview(chat_id, cb.bot, state)
    await cb.answer()
    _af_dbg(func, "done", f"overview_msg_id={mid}")


def _kb_edit_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="af:editf:cancel")],
    ])

def _kb_edit_photo(has_photo: bool) -> InlineKeyboardMarkup:
    rows = []
    rows.append([InlineKeyboardButton(text="📷 Загрузить фото", callback_data="af:editph:upload")])
    if has_photo:
        rows.append([InlineKeyboardButton(text="🗑 Удалить фото", callback_data="af:editph:del")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="af:editf:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _kb_confirm_photo_delete() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data="af:editph:del:yes"),
            InlineKeyboardButton(text="❌ Нет", callback_data="af:editph:del:no"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="af:editf:cancel")],
    ])


def _kb_edit_price() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🆓 Бесплатно", callback_data="af:editp:free"),
            InlineKeyboardButton(text="💛 На донатах", callback_data="af:editp:donate"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="af:editf:cancel")],
    ])


@router.callback_query(F.data.startswith("af:editf:title:"))
async def afisha_edit_title_start(cb: CallbackQuery, state: FSMContext):
    func = "afisha_edit_title_start"
    chat_id = cb.message.chat.id

    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    cur_title = (data.get("title") or "").strip()

    if not listing_id:
        _af_dbg(func, "no_ctx", f"chat_id={chat_id}")
        await cb.answer("Контекст редактирования потерян.")
        return

    # удаляем экран overview
    try:
        await cb.message.delete()
    except Exception as e:
        _af_dbg(func, "delete_overview_fail", f"{type(e).__name__}: {e}")

    # строка с текущим названием
    cur_line = f"Текущее: <code>{escape_html(cur_title)}</code>\n\n" if cur_title else ""

    prompt = await cb.bot.send_message(
        chat_id=chat_id,
        text=(
            "✏️ <b>Редактирование названия</b>\n\n"
            f"{cur_line}"
            "Введите новое название (до 80 символов):"
        ),
        parse_mode="HTML",
        reply_markup=_kb_edit_cancel(),
    )

    await state.update_data(edit_prompt_msg_id=prompt.message_id)
    await state.set_state(AfishaEditStates.title)

    _af_dbg(func, "prompt", f"listing_id={listing_id} prompt_mid={prompt.message_id}")
    await cb.answer()


@router.callback_query(F.data == "af:editf:cancel")
async def afisha_edit_cancel(cb: CallbackQuery, state: FSMContext):
    func = "afisha_edit_cancel"
    chat_id = cb.message.chat.id

    try:
        await cb.message.delete()
    except Exception as e:
        _af_dbg(func, "delete_prompt_fail", f"{type(e).__name__}: {e}")

    await state.set_state(None)
    mid = await _render_afisha_edit_overview(chat_id, cb.bot, state)
    _af_dbg(func, "done", f"overview_mid={mid}")
    await cb.answer()

# Заголовок (получаем текст)
@router.message(StateFilter(AfishaAddStates.wait_title))
async def af_add_title(message: Message, state: FSMContext):
    chat_id = message.chat.id
    title = _strip(message.text)
    if not _valid_title(title):
        msg = await message.answer("Название некорректно. До 100 символов. Введите заново:")
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])
        print(f"[AFISHA][ERR] wait_title invalid | chat_id={chat_id}")
        return
    await state.update_data(title=title)
    await _update_draft(message.bot, chat_id, state)
    await _delete_user_msg(message.bot, chat_id, message.message_id)

    await _reset_step_ui(state, chat_id, message.bot)
    await _send_nav(chat_id, message.bot, state, back_cb="af:add:back:title")
    await state.set_state(AfishaAddStates.wait_date)

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Сегодня"), KeyboardButton(text="Завтра")]],
        resize_keyboard=True, one_time_keyboard=True
    )
    msg = await message.answer("📅 Введите дату в формате DD-MM-YY (или выберите «Сегодня/Завтра»):", reply_markup=kb)
    await state.update_data(prompt_id=msg.message_id)
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    print(f"[AFISHA][STEP] wait_date | chat_id={chat_id}")

# Дата (получаем текст)
@router.message(StateFilter(AfishaAddStates.wait_date))
async def af_add_date(message: Message, state: FSMContext):
    chat_id = message.chat.id
    raw = _strip(message.text)
    today = datetime.now(_TZ).date()
    if raw.lower() == "сегодня":
        date_local = datetime(today.year, today.month, today.day)
    elif raw.lower() == "завтра":
        t = today + timedelta(days=1)
        date_local = datetime(t.year, t.month, t.day)
    else:
        dt = _parse_date_ddmmyy(raw)
        if not dt:
            await _delete_user_msg(message.bot, chat_id, message.message_id)
            msg = await message.answer("Неверный формат. Пример: 07-10-25. Попробуйте ещё раз:")
            last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
            await register_bot_messages(chat_id, [msg.message_id])
            print(f"[AFISHA][ERR] wait_date format | chat_id={chat_id} | raw={raw}")
            return
        date_local = dt

    err = _is_past_or_too_far(date_local)
    if err:
        await _delete_user_msg(message.bot, chat_id, message.message_id)
        msg = await message.answer(err + "\nВведите дату ещё раз:")
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])
        print(f"[AFISHA][ERR] wait_date window | chat_id={chat_id} | raw={raw}")
        return

    await state.update_data(date_local=date_local.strftime("%Y-%m-%d"))
    await _update_draft(message.bot, chat_id, state)
    await _delete_user_msg(message.bot, chat_id, message.message_id)

    await _reset_step_ui(state, chat_id, message.bot)
    await _send_nav(chat_id, message.bot, state, back_cb="af:add:back:date")
    await state.set_state(AfishaAddStates.wait_time)

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="18:00"), KeyboardButton(text="19:00"), KeyboardButton(text="20:00")]],
        resize_keyboard=True, one_time_keyboard=True
    )
    msg = await message.answer("⏰ Укажите время (HH:MM), например 19:00:", reply_markup=kb)
    await state.update_data(prompt_id=msg.message_id)
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    print(f"[AFISHA][STEP] wait_time | chat_id={chat_id}")

# Время (получаем текст)
@router.message(StateFilter(AfishaAddStates.wait_time))
async def af_add_time(message: Message, state: FSMContext):
    chat_id = message.chat.id
    raw = _strip(message.text)
    hhmm = _parse_time_hhmm(raw)
    if not hhmm:
        await _delete_user_msg(message.bot, chat_id, message.message_id)
        msg = await message.answer("Неверный формат. Пример: 19:00. Введите ещё раз:")
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])
        print(f"[AFISHA][ERR] wait_time format | chat_id={chat_id} | raw={raw}")
        return

    hh, mm = hhmm
    data = await state.get_data()
    try:
        start_at_utc = _to_start_utc(data.get("date_local"), hh, mm)
    except Exception:
        start_at_utc = 0
    if start_at_utc <= int(datetime.now(timezone.utc).timestamp()):
        await _delete_user_msg(message.bot, chat_id, message.message_id)
        msg = await message.answer("Это время уже прошло. Укажите будущее время:")
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])
        return

    await state.update_data(time_hh=hh, time_mm=mm)
    await _update_draft(message.bot, chat_id, state)
    await _delete_user_msg(message.bot, chat_id, message.message_id)

    await _reset_step_ui(state, chat_id, message.bot)
    await _send_nav(chat_id, message.bot, state, back_cb="af:add:back:time")
    await state.set_state(AfishaAddStates.wait_city_choice)

    kb = await _kb_city_from_db()
    msg = await message.answer("👇 Выберите город из списка ниже:", reply_markup=kb)
    await state.update_data(prompt_id=msg.message_id)
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    print(f"[AFISHA][STEP] wait_city_choice | chat_id={chat_id}")

# Город: выбор из БД / «Другой»
@router.callback_query(StateFilter(AfishaAddStates.wait_city_choice), F.data.startswith("af:add:city:"))
async def af_add_city_choice(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    val = cb.data.rsplit(":", 1)[-1]
    try:
        await cb.message.delete()
    except Exception:
        pass

    if val == "other":
        await _reset_step_ui(state, chat_id, cb.bot)
        await _send_nav(chat_id, cb.bot, state, back_cb="af:add:back:time")
        await state.set_state(AfishaAddStates.wait_city_text)
        await _update_draft(cb.bot, chat_id, state)
        msg = await cb.message.answer("Укажите город (текстом):")
        await state.update_data(prompt_id=msg.message_id)
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer(); print(f"[AFISHA][CITY] other | chat_id={chat_id}"); return

    try:
        city_id = int(val)
    except Exception:
        city_id = None

    await state.update_data(city_id=city_id, city_text=None)
    await _update_draft(cb.bot, chat_id, state)

    await _reset_step_ui(state, chat_id, cb.bot)
    await _send_nav(chat_id, cb.bot, state, back_cb="af:add:back:time")
    await state.set_state(AfishaAddStates.wait_price_choice)
    msg = await cb.message.answer("💲 Укажите цену:", reply_markup=_kb_price())
    await state.update_data(prompt_id=msg.message_id)
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])

    await cb.answer()
    print(f"[AFISHA][CITY] chosen={city_id} | chat_id={chat_id}")

# Город: текст для «Другой»
@router.message(StateFilter(AfishaAddStates.wait_city_text))
async def af_add_city_text(message: Message, state: FSMContext):
    chat_id = message.chat.id
    city_text = _strip(message.text)
    if not city_text:
        msg = await message.answer("Введите название города строкой:")
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])
        print(f"[AFISHA][ERR] wait_city_text empty | chat_id={chat_id}")
        return

    other_city_id = await _other_city_id()
    if other_city_id is None:
        msg = await message.answer("Не найден служебный город «Другой». Обратитесь к администратору.")
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])
        return

    await state.update_data(city_id=other_city_id, city_text=city_text)
    await _update_draft(message.bot, chat_id, state)
    await _delete_user_msg(message.bot, chat_id, message.message_id)

    await _reset_step_ui(state, chat_id, message.bot)
    await _send_nav(chat_id, message.bot, state, back_cb="af:add:back:time")
    await state.set_state(AfishaAddStates.wait_price_choice)
    msg = await message.answer("💲 Укажите цену:", reply_markup=_kb_price())
    await state.update_data(prompt_id=msg.message_id)
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    print(f"[AFISHA][STEP] wait_price_choice | chat_id={chat_id}")

# Цена: выбор (бесплатно / донат / ввести)
@router.callback_query(StateFilter(AfishaAddStates.wait_price_choice), F.data.startswith("af:add:price:"))
async def af_add_price_choice(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    val = cb.data.rsplit(":", 1)[-1]
    try:
        await cb.message.delete()
    except Exception:
        pass

    if val == "free":
        await state.update_data(price_text="Бесплатно")
        await _update_draft(cb.bot, chat_id, state)
    elif val == "donate":
        await state.update_data(price_text="На донатах")
        await _update_draft(cb.bot, chat_id, state)
    else:
        await _reset_step_ui(state, chat_id, cb.bot)
        await _send_nav(chat_id, cb.bot, state, back_cb="af:add:back:price")
        await state.set_state(AfishaAddStates.wait_price_text)
        msg = await cb.message.answer("Введите цену (любым текстом):")
        await state.update_data(prompt_id=msg.message_id)
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])
        await cb.answer(); print(f"[AFISHA][PRICE] custom | chat_id={chat_id}"); return

    await _reset_step_ui(state, chat_id, cb.bot)
    await _send_nav(chat_id, cb.bot, state, back_cb="af:add:back:price")
    await state.set_state(AfishaAddStates.wait_venue)
    msg = await cb.message.answer("Площадка (можно пропустить):", reply_markup=_kb_skip("af:add:skip:venue"))
    await state.update_data(prompt_id=msg.message_id)
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"[AFISHA][PRICE] {val} | chat_id={chat_id}")


@router.message(StateFilter(AfishaAddStates.wait_price_choice))
async def af_add_price_direct(message: Message, state: FSMContext):
    """
    Пользователь ввёл цену напрямую, не нажимая кнопку «Ввести цену».
    """
    chat_id = message.chat.id
    price_text = _strip(message.text)

    if not price_text:
        msg = await message.answer("Пусто. Введите цену или используйте кнопки ниже.")
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])
        print(f"[AFISHA][ERR] wait_price_choice direct empty | chat_id={chat_id}")
        return

    await state.update_data(price_text=price_text)
    await _update_draft(message.bot, chat_id, state)
    await _delete_user_msg(message.bot, chat_id, message.message_id)

    await _reset_step_ui(state, chat_id, message.bot)
    await _send_nav(chat_id, message.bot, state, back_cb="af:add:back:price")
    await state.set_state(AfishaAddStates.wait_venue)

    msg = await message.answer(
        "Площадка (можно пропустить):",
        reply_markup=_kb_skip("af:add:skip:venue")
    )
    await state.update_data(prompt_id=msg.message_id)
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])

    print(f"[AFISHA][PRICE] direct_input -> wait_venue | chat_id={chat_id} price={price_text!r}")





# Цена: произвольный ввод
@router.message(StateFilter(AfishaAddStates.wait_price_text))
async def af_add_price_text(message: Message, state: FSMContext):
    chat_id = message.chat.id
    price_text = _strip(message.text)
    if not price_text:
        msg = await message.answer("Пусто. Введите цену или /skip чтобы пропустить:")
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])
        print(f"[AFISHA][ERR] wait_price_text empty | chat_id={chat_id}")
        return

    await state.update_data(price_text=price_text)
    await _update_draft(message.bot, chat_id, state)
    await _delete_user_msg(message.bot, chat_id, message.message_id)

    await _reset_step_ui(state, chat_id, message.bot)
    await _send_nav(chat_id, message.bot, state, back_cb="af:add:back:price_text")
    await state.set_state(AfishaAddStates.wait_venue)
    msg = await message.answer("Площадка (можно пропустить):", reply_markup=_kb_skip("af:add:skip:venue"))
    await state.update_data(prompt_id=msg.message_id)
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    print(f"[AFISHA][STEP] wait_venue | chat_id={chat_id}")

# Площадка (ввод)
@router.message(StateFilter(AfishaAddStates.wait_venue))
async def af_add_venue(message: Message, state: FSMContext):
    chat_id = message.chat.id
    venue = _strip(message.text)
    await state.update_data(venue_text=venue if venue else None)
    await _update_draft(message.bot, chat_id, state)

    await _delete_user_msg(message.bot, chat_id, message.message_id)
    await _reset_step_ui(state, chat_id, message.bot)
    await _send_nav(chat_id, message.bot, state, back_cb="af:add:back:venue")
    await state.set_state(AfishaAddStates.wait_descr)

    msg = await message.answer("Описание (до 500 символов, можно пропустить):", reply_markup=_kb_skip("af:add:skip:descr"))
    await state.update_data(prompt_id=msg.message_id)
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    print(f"[AFISHA][STEP] wait_descr | chat_id={chat_id}")

@router.message(AfishaEditStates.title)
async def afisha_edit_title_apply(message: Message, state: FSMContext):
    func = "afisha_edit_title_apply"
    chat_id = message.chat.id
    owner_id = message.from_user.id
    new_title = (message.text or "").strip()

    _af_dbg(func, "in", f"len={len(new_title)} title={new_title!r}")

    if not (1 <= len(new_title) <= 80):
        await message.answer("Название должно быть от 1 до 80 символов.")
        return

    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    prompt_mid = data.get("edit_prompt_msg_id")

    if not listing_id:
        _af_dbg(func, "no_ctx", f"chat_id={chat_id}")
        await message.answer("Контекст редактирования потерян. Вернитесь в «Мои объявления».")
        return

    # обновляем БД
    try:
        async with SessionLocal() as s:
            await s.execute(sql("""
                UPDATE listing
                SET title = :title
                WHERE id = :id AND owner_id = :owner AND type='events' AND is_sold=0
            """), {"title": new_title, "id": int(listing_id), "owner": int(owner_id)})
            await _mark_event_pending(s, listing_id)
            await s.commit()
        _af_dbg(func, "db.ok", f"id={listing_id}")
    except Exception as e:
        _af_dbg(func, "db.fail", f"{type(e).__name__}: {e}")
        await message.answer("Не удалось сохранить. Попробуйте ещё раз.")
        return

    # чистим пользовательское сообщение и промпт
    try:
        await message.delete()
    except Exception:
        pass
    if prompt_mid:
        try:
            await message.bot.delete_message(chat_id=chat_id, message_id=int(prompt_mid))
        except Exception:
            pass

    # обновляем FSM-данные и выходим из состояния
    await state.update_data(title=new_title, edit_prompt_msg_id=None)
    await state.set_state(None)

    mid = await _render_afisha_edit_overview(chat_id, message.bot, state)
    _af_dbg(func, "done", f"overview_mid={mid}")

def _parse_dt_local(s: str) -> datetime | None:
    s = (s or "").strip()
    # поддержим пару вариантов, но без “колбасы”
    fmts = [
        "%Y-%m-%d %H:%M",   # 2026-02-18 20:30
        "%Y-%m-%d %H.%M",   # 2026-02-18 20.30
        "%d.%m.%Y %H:%M",   # 18.02.2026 20:30
        "%d.%m.%Y %H.%M",   # 18.02.2026 20.30
    ]
    for fmt in fmts:
        try:
            dt_naive = datetime.strptime(s, fmt)
            return dt_naive.replace(tzinfo=_TZ)  # локальная TZ проекта
        except Exception:
            continue
    return None


@router.callback_query(F.data.startswith("af:editf:date:"))
async def afisha_edit_date_start(cb: CallbackQuery, state: FSMContext):
    func = "afisha_edit_date_start"
    chat_id = cb.message.chat.id

    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    date_local = data.get("date_local") or "—"

    if not listing_id:
        _af_dbg(func, "no_ctx", f"chat_id={chat_id}")
        await cb.answer("Контекст редактирования потерян.")
        return

    try:
        await cb.message.delete()
    except Exception as e:
        _af_dbg(func, "delete_overview_fail", f"{type(e).__name__}: {e}")

    prompt = await cb.bot.send_message(
        chat_id=chat_id,
        text=(
            "✏️ <b>Редактирование даты</b>\n\n"
            f"Текущая дата: <code>{date_local}</code>\n\n"
            "Введите дату (как при создании):\n"
            "<code>200226</code> или <code>20.02.26</code> или <code>2026-02-20</code>"
        ),
        parse_mode="HTML",
        reply_markup=_kb_edit_cancel(),
    )

    await state.update_data(edit_prompt_msg_id=prompt.message_id)
    await state.set_state(AfishaEditStates.date)
    _af_dbg(func, "prompt", f"listing_id={listing_id} prompt_mid={prompt.message_id}")
    await cb.answer()


@router.message(AfishaEditStates.date)
async def afisha_edit_date_apply(message: Message, state: FSMContext):
    func = "afisha_edit_date_apply"
    chat_id = message.chat.id
    owner_id = message.from_user.id
    raw = (message.text or "").strip()

    dt = _parse_date_ddmmyy(raw)  # ваш парсер
    _af_dbg(func, "in", f"raw={raw!r} parsed={'ok' if dt else 'fail'}")

    if not dt:
        await message.answer(
            "Не понял дату.\n"
            "Примеры: <code>200226</code> / <code>20.02.26</code> / <code>2026-02-20</code>",
            parse_mode="HTML",
        )
        return

    date_err = _is_past_or_too_far(dt)
    if date_err:
        await message.answer(date_err)
        return

    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    prompt_mid = data.get("edit_prompt_msg_id")

    if not listing_id:
        _af_dbg(func, "no_ctx", "no edit_listing_id")
        await message.answer("Контекст редактирования потерян. Вернитесь в «Мои объявления».")
        return

    # время оставляем текущее из FSM
    hh = data.get("time_hh")
    mm = data.get("time_mm")
    if hh is None or mm is None:
        hh, mm = 20, 0  # запасной вариант, но лучше чтобы было в FSM

    date_local_str = dt.strftime("%Y-%m-%d")

    try:
        start_at_utc = _to_start_utc(date_local_str, int(hh), int(mm))
    except Exception as e:
        _af_dbg(func, "to_utc_fail", f"{type(e).__name__}: {e}")
        await message.answer("Не удалось обработать дату. Попробуйте ещё раз.")
        return
    if start_at_utc <= int(datetime.now(timezone.utc).timestamp()):
        await message.answer("Выбранные дата и время уже прошли. Укажите будущую дату.")
        return

    try:
        async with SessionLocal() as s:
            await s.execute(sql("""
                UPDATE events_meta
                SET start_at_utc = :ts
                WHERE listing_id = :id
                  AND EXISTS (
                      SELECT 1 FROM listing
                      WHERE id = :id
                        AND owner_id = :owner
                        AND type='events'
                        AND is_sold=0
                  )
            """), {"ts": int(start_at_utc), "id": int(listing_id), "owner": int(owner_id)})
            await _mark_event_pending(s, listing_id)
            await s.commit()
        _af_dbg(func, "db.ok", f"id={listing_id} ts={start_at_utc}")
    except Exception as e:
        _af_dbg(func, "db.fail", f"{type(e).__name__}: {e}")
        await message.answer("Не удалось сохранить дату. Попробуйте ещё раз.")
        return

    # чистим сообщения
    try:
        await message.delete()
    except Exception:
        pass
    if prompt_mid:
        try:
            await message.bot.delete_message(chat_id=chat_id, message_id=int(prompt_mid))
        except Exception:
            pass

    await state.update_data(date_local=date_local_str, edit_prompt_msg_id=None)
    await state.set_state(None)

    mid = await _render_afisha_edit_overview(chat_id, message.bot, state)
    _af_dbg(func, "done", f"overview_mid={mid}")

@router.callback_query(F.data.startswith("af:editf:time:"))
async def afisha_edit_time_start(cb: CallbackQuery, state: FSMContext):
    func = "afisha_edit_time_start"
    chat_id = cb.message.chat.id

    # ВАЖНО: сразу ответим, чтобы “крутилка” не висела даже если дальше упадём
    await cb.answer()

    data = await state.get_data()
    listing_id = data.get("edit_listing_id")

    _af_dbg(func, "enter", f"chat_id={chat_id} listing_id={listing_id} data_keys={list(data.keys())}")

    hh = data.get("time_hh")
    mm = data.get("time_mm")
    cur_time = "—"
    if hh is not None and mm is not None:
        try:
            cur_time = f"{int(hh):02d}:{int(mm):02d}"
        except Exception:
            pass

    date_local = data.get("date_local") or "—"

    if not listing_id:
        _af_dbg(func, "no_ctx", f"chat_id={chat_id}")
        await cb.message.answer("Контекст редактирования потерян. Вернитесь в «Мои объявления».")
        return

    # удаляем экран overview
    try:
        await cb.message.delete()
    except Exception as e:
        _af_dbg(func, "delete_overview_fail", f"{type(e).__name__}: {e}")

    prompt = await cb.bot.send_message(
        chat_id=chat_id,
        text=(
            "✏️ <b>Редактирование времени</b>\n\n"
            f"Дата: <code>{date_local}</code>\n"
            f"Текущее время: <code>{cur_time}</code>\n\n"
            "Введите время (как при создании):\n"
            "<code>2000</code> / <code>20-00</code> / <code>20:00</code> / <code>20_00</code>"
        ),
        parse_mode="HTML",
        reply_markup=_kb_edit_cancel(),
    )

    await state.update_data(edit_prompt_msg_id=prompt.message_id)
    await state.set_state(AfishaEditStates.time)
    _af_dbg(func, "prompt", f"prompt_mid={prompt.message_id}")

@router.message(StateFilter(AfishaEditStates.time))
async def afisha_edit_time_apply(message: Message, state: FSMContext):
    func = "afisha_edit_time_apply"
    chat_id = message.chat.id
    owner_id = message.from_user.id

    raw = (message.text or "").strip()
    raw_norm = raw.replace("_", " ")  # поддержим 20_00

    t = _parse_time_hhmm(raw_norm)  # ваш гибкий парсер: 2000 / 20-00 / 20:00 / 20 00 и т.д.
    _af_dbg(func, "in", f"raw={raw!r} norm={raw_norm!r} parsed={'ok' if t else 'fail'}")

    if not t:
        await message.answer(
            "Не понял время.\n"
            "Примеры: <code>2000</code> / <code>20-00</code> / <code>20:00</code> / <code>20_00</code>",
            parse_mode="HTML",
        )
        return

    hh, mm = t

    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    date_local_str = data.get("date_local")
    prompt_mid = data.get("edit_prompt_msg_id")

    _af_dbg(func, "ctx", f"listing_id={listing_id} date_local={date_local_str}")

    if not listing_id or not date_local_str:
        await message.answer("Контекст редактирования потерян. Вернитесь в «Мои объявления».")
        _af_dbg(func, "no_ctx", "missing listing_id or date_local")
        return

    try:
        start_at_utc = _to_start_utc(date_local_str, int(hh), int(mm))
    except Exception as e:
        _af_dbg(func, "to_utc_fail", f"{type(e).__name__}: {e}")
        await message.answer("Не удалось обработать время. Попробуйте ещё раз.")
        return
    if start_at_utc <= int(datetime.now(timezone.utc).timestamp()):
        await message.answer("Выбранные дата и время уже прошли. Укажите будущее время.")
        return

    try:
        async with SessionLocal() as s:
            await s.execute(sql("""
                UPDATE events_meta
                SET start_at_utc = :ts
                WHERE listing_id = :id
                  AND EXISTS (
                      SELECT 1 FROM listing
                      WHERE id = :id
                        AND owner_id = :owner
                        AND type='events'
                        AND is_sold=0
                  )
            """), {"ts": int(start_at_utc), "id": int(listing_id), "owner": int(owner_id)})
            await _mark_event_pending(s, listing_id)
            await s.commit()
        _af_dbg(func, "db.ok", f"id={listing_id} ts={start_at_utc}")
    except Exception as e:
        _af_dbg(func, "db.fail", f"{type(e).__name__}: {e}")
        await message.answer("Не удалось сохранить время. Попробуйте ещё раз.")
        return

    # чистим сообщение пользователя и промпт
    try:
        await message.delete()
    except Exception:
        pass

    if prompt_mid:
        try:
            await message.bot.delete_message(chat_id=chat_id, message_id=int(prompt_mid))
        except Exception:
            pass

    await state.update_data(time_hh=int(hh), time_mm=int(mm), edit_prompt_msg_id=None)
    await state.set_state(None)

    mid = await _render_afisha_edit_overview(chat_id, message.bot, state)
    _af_dbg(func, "done", f"overview_mid={mid}")

@router.callback_query(F.data.startswith("af:editf:price:"))
async def afisha_edit_price_start(cb: CallbackQuery, state: FSMContext):
    func = "afisha_edit_price_start"
    chat_id = cb.message.chat.id
    await cb.answer()

    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    cur_price = (data.get("price_text") or "").strip() or "—"

    if not listing_id:
        _af_dbg(func, "no_ctx", f"chat_id={chat_id}")
        await cb.message.answer("Контекст редактирования потерян. Вернитесь в «Мои объявления».")
        return

    try:
        await cb.message.delete()
    except Exception as e:
        _af_dbg(func, "delete_overview_fail", f"{type(e).__name__}: {e}")

    prompt = await cb.bot.send_message(
        chat_id=chat_id,
        text=(
            "✏️ <b>Редактирование цены</b>\n\n"
            f"Текущая цена: <code>{escape_html(cur_price)}</code>\n\n"
            "Введите новую цену (можно как текст):\n"
            "Например: <code>0</code>, <code>500</code>, <code>500 rsd</code>, <code>бесплатно</code>"
        ),
        parse_mode="HTML",
        reply_markup=_kb_edit_price(),
    )
    await state.update_data(edit_prompt_msg_id=prompt.message_id)
    await state.set_state(AfishaEditStates.price)
    _af_dbg(func, "prompt", f"listing_id={listing_id} prompt_mid={prompt.message_id}")

@router.callback_query(F.data == "af:editp:free")
async def afisha_edit_price_btn_free(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _afisha_edit_price_apply_value(
        bot=cb.bot,
        chat_id=cb.message.chat.id,
        owner_id=cb.from_user.id,
        state=state,
        new_price_text="Бесплатно",
        func="afisha_edit_price_btn_free",
        prompt_message_id=cb.message.message_id,
    )


@router.callback_query(F.data == "af:editp:donate")
async def afisha_edit_price_btn_donate(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _afisha_edit_price_apply_value(
        bot=cb.bot,
        chat_id=cb.message.chat.id,
        owner_id=cb.from_user.id,
        state=state,
        new_price_text="На донатах",
        func="afisha_edit_price_btn_donate",
        prompt_message_id=cb.message.message_id,
    )



async def _afisha_edit_price_apply_value(
    bot,
    chat_id: int,
    owner_id: int,
    state: FSMContext,
    new_price_text: str,
    func: str,
    prompt_message_id: int | None = None,
) -> None:
    _af_dbg(func, "apply", f"new_price_text={new_price_text!r}")

    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    prompt_mid = prompt_message_id or data.get("edit_prompt_msg_id")

    if not listing_id:
        _af_dbg(func, "no_ctx", "missing listing_id")
        await bot.send_message(chat_id, "Контекст редактирования потерян. Вернитесь в «Мои объявления».")
        return

    # listing.price -> число или NULL
    price_num = None
    try:
        digits = "".join(ch for ch in new_price_text if ch.isdigit())
        if digits:
            price_num = int(digits)
    except Exception:
        price_num = None

    try:
        async with SessionLocal() as s:
            await s.execute(sql("""
                UPDATE events_meta
                SET price_text = :pt
                WHERE listing_id = :id
                  AND EXISTS (
                      SELECT 1 FROM listing
                      WHERE id = :id
                        AND owner_id = :owner
                        AND type='events'
                        AND is_sold=0
                  )
            """), {"pt": new_price_text, "id": int(listing_id), "owner": int(owner_id)})

            await s.execute(sql("""
                UPDATE listing
                SET price = :pn
                WHERE id = :id
                  AND owner_id = :owner
                  AND type='events'
                  AND is_sold=0
            """), {"pn": price_num, "id": int(listing_id), "owner": int(owner_id)})

            await _mark_event_pending(s, listing_id)
            await s.commit()

        _af_dbg(func, "db.ok", f"id={listing_id} price_num={price_num}")
    except Exception as e:
        _af_dbg(func, "db.fail", f"{type(e).__name__}: {e}")
        await bot.send_message(chat_id, "Не удалось сохранить цену. Попробуйте ещё раз.")
        return

    # удалим prompt (сообщение с кнопками)
    if prompt_mid:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(prompt_mid))
        except Exception:
            pass

    await state.update_data(price_text=new_price_text, edit_prompt_msg_id=None)
    await state.set_state(None)

    mid = await _render_afisha_edit_overview(chat_id, bot, state)
    _af_dbg(func, "done", f"overview_mid={mid}")

@router.message(StateFilter(AfishaEditStates.price))
async def afisha_edit_price_apply(message: Message, state: FSMContext):
    func = "afisha_edit_price_apply"
    chat_id = message.chat.id
    owner_id = message.from_user.id

    raw = (message.text or "").strip()
    val = raw.lower().strip()

    # быстрые варианты как при публикации
    if val in {"free", "бесплатно", "free.", "бесплатно."}:
        new_price_text = "Бесплатно"
    elif val in {"donate", "донат", "на донатах", "донаты"}:
        new_price_text = "На донатах"
    else:
        new_price_text = raw
    _af_dbg(func, "in", f"raw={raw!r}")

    if not new_price_text:
        await message.answer("Цена не может быть пустой.")
        return

    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    prompt_mid = data.get("edit_prompt_msg_id")

    if not listing_id:
        _af_dbg(func, "no_ctx", "missing listing_id")
        await message.answer("Контекст редактирования потерян. Вернитесь в «Мои объявления».")
        return

    # Попробуем извлечь число для listing.price (если возможно), иначе оставим NULL
    price_num = None
    try:
        # берём первые цифры из строки
        digits = "".join(ch for ch in new_price_text if ch.isdigit())
        if digits:
            price_num = int(digits)
    except Exception:
        price_num = None

    try:
        async with SessionLocal() as s:
            # 1) events_meta.price_text (как “истина” для отображения)
            await s.execute(sql("""
                UPDATE events_meta
                SET price_text = :pt
                WHERE listing_id = :id
                  AND EXISTS (
                      SELECT 1 FROM listing
                      WHERE id = :id
                        AND owner_id = :owner
                        AND type='events'
                        AND is_sold=0
                  )
            """), {"pt": new_price_text, "id": int(listing_id), "owner": int(owner_id)})

            # 2) listing.price (число или NULL)
            await s.execute(sql("""
                UPDATE listing
                SET price = :pn
                WHERE id = :id
                  AND owner_id = :owner
                  AND type='events'
                  AND is_sold=0
            """), {"pn": price_num, "id": int(listing_id), "owner": int(owner_id)})

            await _mark_event_pending(s, listing_id)
            await s.commit()

        _af_dbg(func, "db.ok", f"id={listing_id} price_num={price_num} price_text={new_price_text!r}")
    except Exception as e:
        _af_dbg(func, "db.fail", f"{type(e).__name__}: {e}")
        await message.answer("Не удалось сохранить цену. Попробуйте ещё раз.")
        return

    # чистим сообщения
    try:
        await message.delete()
    except Exception:
        pass
    if prompt_mid:
        try:
            await message.bot.delete_message(chat_id=chat_id, message_id=int(prompt_mid))
        except Exception:
            pass

    await state.update_data(price_text=new_price_text, edit_prompt_msg_id=None)
    await state.set_state(None)

    mid = await _render_afisha_edit_overview(chat_id, message.bot, state)
    _af_dbg(func, "done", f"overview_mid={mid}")

@router.callback_query(F.data.startswith("af:editf:city:"))
async def afisha_edit_city_start(cb: CallbackQuery, state: FSMContext):
    func = "afisha_edit_city_start"
    chat_id = cb.message.chat.id
    await cb.answer()

    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    cur_city = (data.get("city_text") or "").strip() or "—"

    if not listing_id:
        _af_dbg(func, "no_ctx", f"chat_id={chat_id}")
        await cb.message.answer("Контекст редактирования потерян. Вернитесь в «Мои объявления».")
        return

    try:
        await cb.message.delete()
    except Exception as e:
        _af_dbg(func, "delete_overview_fail", f"{type(e).__name__}: {e}")

    kb = await _kb_city_from_db_edit()
    prompt = await cb.bot.send_message(
        chat_id=chat_id,
        text=(
            "✏️ <b>Редактирование города</b>\n\n"
            f"Текущий город: <code>{escape_html(cur_city)}</code>\n\n"
            "Выберите город из списка или нажмите «Другой»:"
        ),
        parse_mode="HTML",
        reply_markup=kb,
    )

    await state.update_data(edit_prompt_msg_id=prompt.message_id)
    await state.set_state(AfishaEditStates.city)
    _af_dbg(func, "prompt", f"listing_id={listing_id} prompt_mid={prompt.message_id}")

@router.callback_query(F.data.startswith("af:editc:"))
async def afisha_edit_city_pick(cb: CallbackQuery, state: FSMContext):
    func = "afisha_edit_city_pick"
    chat_id = cb.message.chat.id
    owner_id = cb.from_user.id
    await cb.answer()

    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    prompt_mid = data.get("edit_prompt_msg_id")

    if not listing_id:
        _af_dbg(func, "no_ctx", "missing listing_id")
        await cb.message.answer("Контекст редактирования потерян. Вернитесь в «Мои объявления».")
        return

    tail = cb.data.split(":")[-1]

    # 1) выбран конкретный city_id
    try:
        city_id = int(tail)
    except Exception:
        _af_dbg(func, "bad_id", f"data={cb.data!r}")
        return

    # 2) найдём имя города (надёжно)
    city_name = ""
    try:
        city_name = (await _city_name_by_id(city_id) or "").strip()
    except Exception as e:
        _af_dbg(func, "city_name_by_id_fail", f"city_id={city_id} {type(e).__name__}: {e}")
        city_name = ""

    # fallback: напрямую из БД, если helper не дал имя
    if not city_name:
        try:
            async with SessionLocal() as s:
                res = await s.execute(
                    sql("SELECT name FROM city WHERE id = :id LIMIT 1"),
                    {"id": int(city_id)},
                )
                row = res.first()
                city_name = (row[0] if row else "") or ""
                city_name = city_name.strip()
        except Exception as e:
            _af_dbg(func, "city_name_db_fail", f"city_id={city_id} {type(e).__name__}: {e}")
            city_name = ""

    if not city_name:
        _af_dbg(func, "city_name_empty", f"city_id={city_id}")
        await cb.message.answer("Не удалось определить название города.")
        return

    # 3) Если выбран "Другой город" из БД — переходим к вводу текста
    # (проверяем по подстроке, чтобы не зависеть от точного написания)
    if "друг" in city_name.lower():
        try:
            await cb.message.edit_text(
                "✏️ <b>Другой город</b>\n\nВведите город текстом (например: <code>Зренянин</code>):",
                parse_mode="HTML",
                reply_markup=_kb_edit_cancel(),
            )
        except Exception as e:
            _af_dbg(func, "edit_text_fail", f"{type(e).__name__}: {e}")
            try:
                await cb.message.delete()
            except Exception:
                pass
            msg = await cb.bot.send_message(
                chat_id=chat_id,
                text="✏️ <b>Другой город</b>\n\nВведите город текстом (например: <code>Зренянин</code>):",
                parse_mode="HTML",
                reply_markup=_kb_edit_cancel(),
            )
            await state.update_data(edit_prompt_msg_id=msg.message_id)

        # сохраняем выбранный city_id (это будет "Другой город")
        await state.update_data(city_id=int(city_id), city_text=city_name)
        await state.set_state(AfishaEditStates.city_text)

        _af_dbg(func, "other_city_mode", f"listing_id={listing_id} other_city_id={city_id}")
        return

    # 4) Обычный город: обновляем БД: listing.city_id + events_meta.city_text
    try:
        async with SessionLocal() as s:
            await s.execute(sql("""
                UPDATE listing
                SET city_id = :cid
                WHERE id = :id
                  AND owner_id = :owner
                  AND type='events'
                  AND is_sold=0
            """), {"cid": int(city_id), "id": int(listing_id), "owner": int(owner_id)})

            await s.execute(sql("""
                UPDATE events_meta
                SET city_text = :ct
                WHERE listing_id = :id
            """), {"ct": city_name, "id": int(listing_id)})

            await _mark_event_pending(s, listing_id)
            await s.commit()

        _af_dbg(func, "db.ok", f"id={listing_id} city_id={city_id} city_name={city_name!r}")
    except Exception as e:
        _af_dbg(func, "db.fail", f"{type(e).__name__}: {e}")
        await cb.message.answer("Не удалось сохранить город. Попробуйте ещё раз.")
        return

    # удаляем prompt (экран выбора города)
    if prompt_mid:
        try:
            await cb.bot.delete_message(chat_id=chat_id, message_id=int(prompt_mid))
        except Exception:
            pass

    await state.update_data(city_id=int(city_id), city_text=city_name, edit_prompt_msg_id=None)
    await state.set_state(None)

    mid = await _render_afisha_edit_overview(chat_id, cb.bot, state)
    _af_dbg(func, "done", f"overview_mid={mid}")



@router.message(StateFilter(AfishaEditStates.city_text))
async def afisha_edit_city_text_apply(message: Message, state: FSMContext):
    func = "afisha_edit_city_text_apply"
    chat_id = message.chat.id
    owner_id = message.from_user.id

    raw = (message.text or "").strip()
    _af_dbg(func, "in", f"raw={raw!r}")

    if not raw:
        await message.answer("Город не может быть пустым.")
        return

    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    prompt_mid = data.get("edit_prompt_msg_id")
    other_city_id = data.get("city_id")  # тут должен быть ID "Другой город" из БД

    if not listing_id or not other_city_id:
        _af_dbg(func, "no_ctx", f"listing_id={listing_id} other_city_id={other_city_id}")
        await message.answer("Контекст редактирования потерян. Вернитесь в «Мои объявления».")
        return

    try:
        async with SessionLocal() as s:
            await s.execute(sql("""
                UPDATE listing
                SET city_id = :cid
                WHERE id = :id
                  AND owner_id = :owner
                  AND type='events'
                  AND is_sold=0
            """), {"cid": int(other_city_id), "id": int(listing_id), "owner": int(owner_id)})

            await s.execute(sql("""
                UPDATE events_meta
                SET city_text = :ct
                WHERE listing_id = :id
            """), {"ct": raw, "id": int(listing_id)})

            await _mark_event_pending(s, listing_id)
            await s.commit()

        _af_dbg(func, "db.ok", f"id={listing_id} city_id={other_city_id} city_text={raw!r}")
    except Exception as e:
        _af_dbg(func, "db.fail", f"{type(e).__name__}: {e}")
        await message.answer("Не удалось сохранить город. Попробуйте ещё раз.")
        return
    

    # чистим
    try:
        await message.delete()
    except Exception:
        pass
    if prompt_mid:
        try:
            await message.bot.delete_message(chat_id=chat_id, message_id=int(prompt_mid))
        except Exception:
            pass

    await state.update_data(city_id=int(other_city_id), city_text=f"Другой: {raw}", edit_prompt_msg_id=None)
    await state.set_state(None)

    mid = await _render_afisha_edit_overview(chat_id, message.bot, state)
    _af_dbg(func, "done", f"overview_mid={mid}")
@router.callback_query(F.data.startswith("af:editf:venue:"))
async def afisha_edit_venue_start(cb: CallbackQuery, state: FSMContext):
    func = "afisha_edit_venue_start"
    chat_id = cb.message.chat.id
    await cb.answer()

    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    cur_venue = (data.get("venue_text") or "").strip() or "—"

    if not listing_id:
        _af_dbg(func, "no_ctx", f"chat_id={chat_id}")
        await cb.message.answer("Контекст редактирования потерян. Вернитесь в «Мои объявления».")
        return

    try:
        await cb.message.delete()
    except Exception as e:
        _af_dbg(func, "delete_overview_fail", f"{type(e).__name__}: {e}")

    prompt = await cb.bot.send_message(
        chat_id=chat_id,
        text=(
            "✏️ <b>Редактирование места</b>\n\n"
            f"Текущее место: <code>{escape_html(cur_venue)}</code>\n\n"
            "Введите новое место/площадку.\n"
            "Пример: <code>Zappa Barka</code> или <code>KC Grad</code>"
        ),
        parse_mode="HTML",
        reply_markup=_kb_edit_cancel(),
    )

    await state.update_data(edit_prompt_msg_id=prompt.message_id)
    await state.set_state(AfishaEditStates.venue)
    _af_dbg(func, "prompt", f"listing_id={listing_id} prompt_mid={prompt.message_id}")

@router.message(StateFilter(AfishaEditStates.venue))
async def afisha_edit_venue_apply(message: Message, state: FSMContext):
    func = "afisha_edit_venue_apply"
    chat_id = message.chat.id
    owner_id = message.from_user.id

    raw = (message.text or "").strip()
    _af_dbg(func, "in", f"raw={raw!r}")

    if not raw:
        await message.answer("Место не может быть пустым.")
        return

    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    prompt_mid = data.get("edit_prompt_msg_id")

    if not listing_id:
        _af_dbg(func, "no_ctx", "missing listing_id")
        await message.answer("Контекст редактирования потерян. Вернитесь в «Мои объявления».")
        return

    try:
        async with SessionLocal() as s:
            await s.execute(sql("""
                UPDATE events_meta
                SET venue_text = :vt
                WHERE listing_id = :id
                  AND EXISTS (
                      SELECT 1 FROM listing
                      WHERE id = :id
                        AND owner_id = :owner
                        AND type='events'
                        AND is_sold=0
                  )
            """), {"vt": raw, "id": int(listing_id), "owner": int(owner_id)})
            await _mark_event_pending(s, listing_id)
            await s.commit()

        _af_dbg(func, "db.ok", f"id={listing_id} venue_text={raw!r}")
    except Exception as e:
        _af_dbg(func, "db.fail", f"{type(e).__name__}: {e}")
        await message.answer("Не удалось сохранить место. Попробуйте ещё раз.")
        return

    try:
        await message.delete()
    except Exception:
        pass
    if prompt_mid:
        try:
            await message.bot.delete_message(chat_id=chat_id, message_id=int(prompt_mid))
        except Exception:
            pass

    await state.update_data(venue_text=raw, edit_prompt_msg_id=None)
    await state.set_state(None)

    mid = await _render_afisha_edit_overview(chat_id, message.bot, state)
    _af_dbg(func, "done", f"overview_mid={mid}")


@router.callback_query(F.data.startswith("af:editf:descr:"))
async def afisha_edit_descr_start(cb: CallbackQuery, state: FSMContext):
    func = "afisha_edit_descr_start"
    chat_id = cb.message.chat.id
    await cb.answer()

    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    cur_descr = (data.get("descr") or "").strip()
    cur_short = (cur_descr[:500] + "…") if len(cur_descr) > 500 else (cur_descr or "—")

    if not listing_id:
        _af_dbg(func, "no_ctx", f"chat_id={chat_id}")
        await cb.message.answer("Контекст редактирования потерян. Вернитесь в «Мои объявления».")
        return

    try:
        await cb.message.delete()
    except Exception as e:
        _af_dbg(func, "delete_overview_fail", f"{type(e).__name__}: {e}")

    prompt = await cb.bot.send_message(
        chat_id=chat_id,
        text=(
            "✏️ <b>Редактирование описания</b>\n\n"
            f"Текущее:\n<code>{escape_html(cur_short)}</code>\n\n"
            "Введите новое описание одним сообщением.\n"
            "Чтобы убрать описание — отправьте <code>-</code>"
        ),
        parse_mode="HTML",
        reply_markup=_kb_edit_cancel(),
    )

    await state.update_data(edit_prompt_msg_id=prompt.message_id)
    await state.set_state(AfishaEditStates.descr)
    _af_dbg(func, "prompt", f"listing_id={listing_id} prompt_mid={prompt.message_id}")


@router.message(StateFilter(AfishaEditStates.descr))
async def afisha_edit_descr_apply(message: Message, state: FSMContext):
    func = "afisha_edit_descr_apply"
    chat_id = message.chat.id
    owner_id = message.from_user.id

    raw = (message.text or "").strip()
    _af_dbg(func, "in", f"len={len(raw)}")

    # спец-команда "стереть описание"
    if raw == "-":
        new_descr = ""
    else:
        new_descr = raw

    # ограничим длину, чтобы Telegram caption не ломать (и вообще здравый предел)
    if len(new_descr) > 2000:
        await message.answer("Слишком длинно. Давайте до 2000 символов.")
        return

    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    prompt_mid = data.get("edit_prompt_msg_id")

    if not listing_id:
        _af_dbg(func, "no_ctx", "missing listing_id")
        await message.answer("Контекст редактирования потерян. Вернитесь в «Мои объявления».")
        return

    # сохраняем в listing.descr (так у вас и читается в карточках)
    try:
        async with SessionLocal() as s:
            await s.execute(sql("""
                UPDATE listing
                SET descr = :d
                WHERE id = :id
                  AND owner_id = :owner
                  AND type='events'
                  AND is_sold=0
            """), {"d": new_descr, "id": int(listing_id), "owner": int(owner_id)})
            await _mark_event_pending(s, listing_id)
            await s.commit()

        _af_dbg(func, "db.ok", f"id={listing_id} descr_len={len(new_descr)}")
    except Exception as e:
        _af_dbg(func, "db.fail", f"{type(e).__name__}: {e}")
        await message.answer("Не удалось сохранить описание. Попробуйте ещё раз.")
        return

    # чистим сообщения
    try:
        await message.delete()
    except Exception:
        pass
    if prompt_mid:
        try:
            await message.bot.delete_message(chat_id=chat_id, message_id=int(prompt_mid))
        except Exception:
            pass

    await state.update_data(descr=new_descr, edit_prompt_msg_id=None)
    await state.set_state(None)

    mid = await _render_afisha_edit_overview(chat_id, message.bot, state)
    _af_dbg(func, "done", f"overview_mid={mid}")


@router.callback_query(F.data.startswith("af:editf:photo:"))
async def afisha_edit_photo_start(cb: CallbackQuery, state: FSMContext):
    func = "afisha_edit_photo_start"
    chat_id = cb.message.chat.id
    await cb.answer()

    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    cur_photo = data.get("photo_id")

    if not listing_id:
        _af_dbg(func, "no_ctx", f"chat_id={chat_id}")
        await cb.message.answer("Контекст редактирования потерян. Вернитесь в «Мои объявления».")
        return

    try:
        await cb.message.delete()
    except Exception:
        pass

    kb = _kb_edit_photo(bool(cur_photo))

    if cur_photo:
        # показываем текущее фото
        msg = await cb.bot.send_photo(
            chat_id=chat_id,
            photo=cur_photo,
            caption="✏️ <b>Редактирование фото</b>\n\nНиже текущее фото.\nВы можете загрузить новое или удалить его.",
            parse_mode="HTML",
            reply_markup=kb,
        )
    else:
        msg = await cb.bot.send_message(
            chat_id=chat_id,
            text="✏️ <b>Редактирование фото</b>\n\nФото сейчас отсутствует.\nВы можете загрузить новое.",
            parse_mode="HTML",
            reply_markup=kb,
        )

    await state.update_data(edit_prompt_msg_id=msg.message_id)
    await state.set_state(AfishaEditStates.photo)

    _af_dbg(func, "prompt", f"listing_id={listing_id} has_photo={int(bool(cur_photo))}")



@router.callback_query(StateFilter(AfishaEditStates.photo), F.data == "af:editph:upload")
async def afisha_edit_photo_upload(cb: CallbackQuery, state: FSMContext):
    func = "afisha_edit_photo_upload"
    await cb.answer()

    try:
        await cb.message.edit_text(
            "📷 <b>Отправьте фото</b> одним сообщением.\n\n"
            "Если передумали — нажмите «Отмена».",
            parse_mode="HTML",
            reply_markup=_kb_edit_cancel(),
        )
    except Exception as e:
        _af_dbg(func, "edit_text_fail", f"{type(e).__name__}: {e}")

    _af_dbg(func, "wait_photo", "ok")

@router.callback_query(StateFilter(AfishaEditStates.photo), F.data == "af:editph:del")
async def afisha_edit_photo_delete_ask(cb: CallbackQuery, state: FSMContext):
    func = "afisha_edit_photo_delete_ask"
    await cb.answer()

    try:
        await cb.message.edit_caption(
            "🗑 <b>Удалить фото?</b>\n\nЭто действие нельзя отменить.",
            parse_mode="HTML",
            reply_markup=_kb_confirm_photo_delete(),
        )
    except Exception:
        # если prompt был текстовым (без caption) — редактируем текст
        try:
            await cb.message.edit_text(
                "🗑 <b>Удалить фото?</b>\n\nЭто действие нельзя отменить.",
                parse_mode="HTML",
                reply_markup=_kb_confirm_photo_delete(),
            )
        except Exception as e:
            _af_dbg(func, "edit_fail", f"{type(e).__name__}: {e}")

    _af_dbg(func, "ask", "ok")

@router.callback_query(StateFilter(AfishaEditStates.photo), F.data == "af:editph:del:no")
async def afisha_edit_photo_delete_no(cb: CallbackQuery, state: FSMContext):
    func = "afisha_edit_photo_delete_no"
    await cb.answer()

    data = await state.get_data()
    cur_photo = data.get("photo_id")
    kb = _kb_edit_photo(bool(cur_photo))

    try:
        if cur_photo:
            await cb.message.edit_caption(
                "✏️ <b>Редактирование фото</b>\n\nНиже текущее фото.\nВы можете загрузить новое или удалить его.",
                parse_mode="HTML",
                reply_markup=kb,
            )
        else:
            await cb.message.edit_text(
                "✏️ <b>Редактирование фото</b>\n\nФото сейчас отсутствует.\nВы можете загрузить новое.",
                parse_mode="HTML",
                reply_markup=kb,
            )
    except Exception as e:
        _af_dbg(func, "back_fail", f"{type(e).__name__}: {e}")

    _af_dbg(func, "back", "ok")

@router.callback_query(StateFilter(AfishaEditStates.photo), F.data == "af:editph:del:yes")
async def afisha_edit_photo_delete_yes(cb: CallbackQuery, state: FSMContext):
    func = "afisha_edit_photo_delete_yes"
    chat_id = cb.message.chat.id
    owner_id = cb.from_user.id
    await cb.answer()

    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    prompt_mid = data.get("edit_prompt_msg_id")

    if not listing_id:
        _af_dbg(func, "no_ctx", "missing listing_id")
        await cb.message.answer("Контекст редактирования потерян. Вернитесь в «Мои объявления».")
        return

    try:
        async with SessionLocal() as s:
            await s.execute(sql("""
                UPDATE listing
                SET photo_file_id = NULL
                WHERE id = :id
                  AND owner_id = :owner
                  AND type='events'
                  AND is_sold=0
            """), {"id": int(listing_id), "owner": int(owner_id)})
            await _mark_event_pending(s, listing_id)
            await s.commit()
        _af_dbg(func, "db.ok", f"id={listing_id} photo=NULL")
    except Exception as e:
        _af_dbg(func, "db.fail", f"{type(e).__name__}: {e}")
        await cb.message.answer("Не удалось удалить фото. Попробуйте ещё раз.")
        return

    # удаляем prompt и возвращаем overview
    if prompt_mid:
        try:
            await cb.bot.delete_message(chat_id=chat_id, message_id=int(prompt_mid))
        except Exception:
            pass

    await state.update_data(photo_id=None, edit_prompt_msg_id=None)
    await state.set_state(None)

    mid = await _render_afisha_edit_overview(chat_id, cb.bot, state)
    _af_dbg(func, "done", f"overview_mid={mid}")


@router.message(StateFilter(AfishaEditStates.photo))
async def afisha_edit_photo_apply(message: Message, state: FSMContext):
    func = "afisha_edit_photo_apply"
    chat_id = message.chat.id
    owner_id = message.from_user.id

    if not message.photo:
        await message.answer("Нужно прислать именно фото (не файл).")
        _af_dbg(func, "not_photo", "no message.photo")
        return

    file_id = message.photo[-1].file_id  # самое большое
    _af_dbg(func, "in", f"file_id={file_id!r}")

    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    prompt_mid = data.get("edit_prompt_msg_id")

    if not listing_id:
        _af_dbg(func, "no_ctx", "missing listing_id")
        await message.answer("Контекст редактирования потерян. Вернитесь в «Мои объявления».")
        return

    try:
        async with SessionLocal() as s:
            await s.execute(sql("""
                UPDATE listing
                SET photo_file_id = :ph
                WHERE id = :id
                  AND owner_id = :owner
                  AND type='events'
                  AND is_sold=0
            """), {"ph": file_id, "id": int(listing_id), "owner": int(owner_id)})
            await _mark_event_pending(s, listing_id)
            await s.commit()
        _af_dbg(func, "db.ok", f"id={listing_id} photo=set")
    except Exception as e:
        _af_dbg(func, "db.fail", f"{type(e).__name__}: {e}")
        await message.answer("Не удалось сохранить фото. Попробуйте ещё раз.")
        return

    # чистим
    try:
        await message.delete()
    except Exception:
        pass
    if prompt_mid:
        try:
            await message.bot.delete_message(chat_id=chat_id, message_id=int(prompt_mid))
        except Exception:
            pass

    await state.update_data(photo_id=file_id, edit_prompt_msg_id=None)
    await state.set_state(None)

    mid = await _render_afisha_edit_overview(chat_id, message.bot, state)
    _af_dbg(func, "done", f"overview_mid={mid}")





# Площадка: пропуск
@router.callback_query(StateFilter(AfishaAddStates.wait_venue), F.data == "af:add:skip:venue")
async def af_add_venue_skip(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    try:
        await cb.message.delete()
    except Exception:
        pass
    await state.update_data(venue_text=None)
    await _update_draft(cb.bot, chat_id, state)

    await _reset_step_ui(state, chat_id, cb.bot)
    await _send_nav(chat_id, cb.bot, state, back_cb="af:add:back:venue")
    await state.set_state(AfishaAddStates.wait_descr)
    msg = await cb.message.answer("Описание (до 500 символов, можно пропустить):", reply_markup=_kb_skip("af:add:skip:descr"))
    await state.update_data(prompt_id=msg.message_id)
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"[AFISHA][STEP] skip venue -> wait_descr | chat_id={chat_id}")

# Описание (ввод)
@router.message(StateFilter(AfishaAddStates.wait_descr))
async def af_add_descr(message: Message, state: FSMContext):
    chat_id = message.chat.id
    descr = _strip(message.text)
    if descr and len(descr) > 500:
        msg = await message.answer("Слишком длинно (макс. 500). Сократите или /skip:")
        last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
        await register_bot_messages(chat_id, [msg.message_id])
        print(f"[AFISHA][ERR] wait_descr too long | chat_id={chat_id}")
        return

    await state.update_data(descr=descr if descr else None)
    await _update_draft(message.bot, chat_id, state)
    await _delete_user_msg(message.bot, chat_id, message.message_id)

    await _reset_step_ui(state, chat_id, message.bot)
    await _send_nav(chat_id, message.bot, state, back_cb="af:add:back:descr")
    await state.set_state(AfishaAddStates.wait_photo)
    msg = await message.answer("Фото (одно, можно пропустить):", reply_markup=_kb_skip("af:add:skip:photo"))
    await state.update_data(prompt_id=msg.message_id)
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    print(f"[AFISHA][STEP] wait_photo | chat_id={chat_id}")

# Описание: пропуск
@router.callback_query(StateFilter(AfishaAddStates.wait_descr), F.data == "af:add:skip:descr")
async def af_add_descr_skip(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    try:
        await cb.message.delete()
    except Exception:
        pass
    await state.update_data(descr=None)
    await _update_draft(cb.bot, chat_id, state)

    await _reset_step_ui(state, chat_id, cb.bot)
    await _send_nav(chat_id, cb.bot, state, back_cb="af:add:back:descr")
    await state.set_state(AfishaAddStates.wait_photo)
    msg = await cb.message.answer("Фото (одно, можно пропустить):", reply_markup=_kb_skip("af:add:skip:photo"))
    await state.update_data(prompt_id=msg.message_id)
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"[AFISHA][STEP] skip descr -> wait_photo | chat_id={chat_id}")

# Фото (прислали фото)
@router.message(StateFilter(AfishaAddStates.wait_photo), F.photo)
async def af_add_photo(message: Message, state: FSMContext):
    chat_id = message.chat.id
    file_id = message.photo[-1].file_id if message.photo else None
    await state.update_data(photo_id=file_id)
    await _update_draft(message.bot, chat_id, state)

    await _delete_user_msg(message.bot, chat_id, message.message_id)
    await _go_preview(message, state)
    print(f"[AFISHA][PHOTO] got photo | chat_id={chat_id}")



# Фото: прислали не фото / текст
@router.message(StateFilter(AfishaAddStates.wait_photo))
async def af_add_photo_not_photo(message: Message, state: FSMContext):
    chat_id = message.chat.id
    t = (_strip(message.text) or "").lower()

    if t in ("/skip", "пропустить"):
        await state.update_data(photo_id=None)
        await _update_draft(message.bot, chat_id, state)
        await _delete_user_msg(message.bot, chat_id, message.message_id)
        await _go_preview(message, state)
        print(f"[AFISHA][PHOTO] skipped | chat_id={chat_id}")
        return

    await _delete_user_msg(message.bot, chat_id, message.message_id)

    msg = await message.answer("Пришлите фото или нажмите «Пропустить».")
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    print(f"[AFISHA][ERR] wait_photo not a photo | chat_id={chat_id}")



# Фото: пропуск кнопкой
@router.callback_query(StateFilter(AfishaAddStates.wait_photo), F.data == "af:add:skip:photo")
async def af_add_photo_skip(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    try:
        await cb.message.delete()
    except Exception:
        pass
    await state.update_data(photo_id=None)
    await _update_draft(cb.bot, chat_id, state)
    await _go_preview(cb.message, state)
    await cb.answer()
    print(f"[AFISHA][PHOTO] skip via button | chat_id={chat_id}")

# Служебный: сформировать превью
async def _go_preview(message: Message, state: FSMContext):
    chat_id = message.chat.id
    data = await state.get_data()

    title = data.get("title") or "Без названия"
    date_local = data.get("date_local")          # YYYY-MM-DD
    hh = data.get("time_hh"); mm = data.get("time_mm")
    city_id = data.get("city_id"); city_text = data.get("city_text")
    price = data.get("price_text")
    venue = data.get("venue_text")
    descr = data.get("descr")
    photo_id = data.get("photo_id")

    # Формируем человекочитаемый блок (как карточку)
    try:
        dt_local = datetime.strptime(date_local, "%Y-%m-%d").replace(hour=int(hh), minute=int(mm))
        when_line = f"{dt_local.strftime('%d-%m-%y')} • {dt_local.strftime('%H:%M')}"
    except Exception:
        when_line = "Дата/время"

    city_name = await _city_name_by_id(city_id)
    city_line = city_name or (city_text or "Город")
    place_line = city_line if not venue else f"{city_line}, {venue}"
    price_line = f"💲 {price}" if price else ""

    parts = [
        f"🧾 <b>{escape_html(title)}</b>",
        f"📅 {escape_html(when_line)}",
        f"📍 {escape_html(place_line)}",
    ]
    if price_line:
        parts.append(escape_html(price_line))
    if descr:
        parts.append("\n" + escape_html(descr))

    card_text = "\n".join(parts)

    # 1) Чистим «служебку» шага (nav/prompt/кеш), но НЕ трогаем черновик
    await _reset_step_ui(state, chat_id, message.bot)

    # 2) Переводим FSM в preview
    await state.set_state(AfishaAddStates.preview)

    # 3) Делаем ровно ОДНО сообщение, которое остаётся до публикации:
    #    - если фото есть — черновик становится фото-сообщением
    #    - если фото нет — черновик обычный текст
    data = await state.get_data()
    draft_id = data.get("draft_msg_id")
    draft_is_photo = int(data.get("draft_is_photo") or 0)

    if photo_id:
        # если был текстовый черновик — удаляем и создаём фото-черновик
        if draft_id:
            try:
                await message.bot.delete_message(chat_id, int(draft_id))
            except Exception:
                pass
        sent = await message.answer_photo(photo=photo_id, caption=card_text, parse_mode="HTML", reply_markup=_kb_preview())
        await state.update_data(draft_msg_id=sent.message_id, draft_is_photo=1)
        print(f"[AFISHA][STEP] preview(photo) | chat_id={chat_id} msg_id={sent.message_id}")
        return

    # без фото — редактируем/создаём текстовый черновик с кнопками превью
    if not draft_id or draft_is_photo:
        if draft_id and draft_is_photo:
            try:
                await message.bot.delete_message(chat_id, int(draft_id))
            except Exception:
                pass
        sent = await message.answer(card_text, parse_mode="HTML", reply_markup=_kb_preview())
        await state.update_data(draft_msg_id=sent.message_id, draft_is_photo=0)
        print(f"[AFISHA][STEP] preview(text-created) | chat_id={chat_id} msg_id={sent.message_id}")
        return

    try:
        await message.bot.edit_message_text(
            text=card_text,
            chat_id=chat_id,
            message_id=int(draft_id),
            parse_mode="HTML",
            reply_markup=_kb_preview()
        )
        print(f"[AFISHA][STEP] preview(text-updated) | chat_id={chat_id} msg_id={draft_id}")
    except Exception as e:
        print(f"[AFISHA][STEP] preview edit failed -> recreate | chat_id={chat_id} msg_id={draft_id} err={e}")
        sent = await message.answer(card_text, parse_mode="HTML", reply_markup=_kb_preview())
        await state.update_data(draft_msg_id=sent.message_id, draft_is_photo=0)


# Превью: «Исправить»
@router.callback_query(StateFilter(AfishaAddStates.preview), F.data == "af:add:edit")
async def af_add_edit(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    # ВАЖНО: превью — это тот же «черновик». Его НЕ удаляем, чтобы пользователь
    # продолжал видеть введённые данные. Просто уходим на редактирование.
    await _reset_step_ui(state, chat_id, cb.bot)
    await _update_draft(cb.bot, chat_id, state, reply_markup=None)

    await state.set_state(AfishaAddStates.wait_title)
    await _send_nav(chat_id, cb.bot, state, back_cb="af:add:back:entry")
    msg = await cb.message.answer("Измените название (или отправьте новое):")
    await state.update_data(prompt_id=msg.message_id)
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])

    await cb.answer()
    print(f"[AFISHA][EDIT] -> title | chat_id={chat_id}")

# Превью: «Отмена»
@router.callback_query(StateFilter(AfishaAddStates.preview), F.data == "af:add:cancel")
async def af_add_cancel(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await _delete_draft(cb.bot, chat_id, state)
    try:
        await cb.message.delete()
    except Exception:
        pass
    await _reset_step_ui(state, chat_id, cb.bot)
    await state.clear()

    try:
        main_btn = await get_common_menu_button('main_menu')
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=main_btn.text, callback_data=main_btn.callback_data)]]) if main_btn else None
    except Exception:
        kb = None

    msg = await cb.message.answer("Отменено.", reply_markup=kb)
    last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
    await register_bot_messages(chat_id, [msg.message_id])
    await cb.answer()
    print(f"[AFISHA][CANCEL] | chat_id={chat_id}")

# Превью: «Опубликовать»
import asyncio as _asyncio
from collections import defaultdict as _dd
_event_publish_locks: dict[int, _asyncio.Lock] = _dd(_asyncio.Lock)


@router.callback_query(StateFilter(AfishaAddStates.preview), F.data == "af:add:publish")
async def af_add_publish(cb: CallbackQuery, state: FSMContext):
    """Не допускаем параллельную публикацию двойным нажатием кнопки."""
    lock = _event_publish_locks[cb.from_user.id]
    if lock.locked():
        await cb.answer("Публикуем, пожалуйста, подождите.")
        return
    async with lock:
        # Второй update мог попасть в диспетчер до очистки FSM первым update.
        if await state.get_state() != AfishaAddStates.preview.state:
            await cb.answer("Событие уже опубликовано.")
            return
        await _af_add_publish_locked(cb, state)


async def _af_add_publish_locked(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await _delete_draft(cb.bot, chat_id, state)
    try:
        await cb.message.delete()
    except Exception:
        pass
    await _reset_step_ui(state, chat_id, cb.bot)

    data = await state.get_data()
    title = data.get("title")
    date_local = data.get("date_local")
    hh = data.get("time_hh"); mm = data.get("time_mm")
    city_id = data.get("city_id"); city_text = data.get("city_text")
    price_text = data.get("price_text")
    venue_text = data.get("venue_text")
    descr = data.get("descr")
    photo_id = data.get("photo_id")

    owner_id = cb.from_user.id
    contact = f"@{(cb.from_user.username or '').strip()}" if cb.from_user.username else str(owner_id)

    if not isinstance(city_id, int) and city_text:
        city_id = await _other_city_id()
    if not isinstance(city_id, int):
        sent = await cb.message.answer("Не удалось определить город. Создайте объявление заново.")
        last_bot_messages.setdefault(chat_id, []).append(sent.message_id)
        await register_bot_messages(chat_id, [sent.message_id])
        await state.clear()
        await cb.answer()
        return

    try:
        start_at_utc = _to_start_utc(date_local, int(hh), int(mm))
    except Exception as e:
        print(f"[AFISHA] start_at_utc error: {e}")
        sent = await cb.message.answer("Не удалось преобразовать дату/время. Попробуйте заново.")
        last_bot_messages.setdefault(chat_id, []).append(sent.message_id)
        await register_bot_messages(chat_id, [sent.message_id])
        await state.clear()
        await cb.answer()
        return

    if start_at_utc <= int(datetime.now(timezone.utc).timestamp()):
        sent = await cb.message.answer("Дата и время события уже прошли. Создайте объявление заново.")
        last_bot_messages.setdefault(chat_id, []).append(sent.message_id)
        await register_bot_messages(chat_id, [sent.message_id])
        await state.clear()
        await cb.answer()
        return

    listing_id = None
    try:
        await ensure_events_meta()
        async with SessionLocal() as s:
            category_id = await _event_root_category_id(s)
            await s.execute(sql("""
                INSERT INTO listing (
                    city_id, category_id, owner_id, title, price, descr, contact,
                    photo_file_id, is_sold, created_at, type, flex, extra_category_id1, extra_category_id2, status
                ) VALUES (
                    :city_id, :category_id, :owner_id, :title, :price, :descr, :contact,
                    :photo_file_id, 0, CURRENT_TIMESTAMP, 'events', '{}', NULL, NULL, 'active'
                )
            """), {
                "city_id": city_id,
                "category_id": category_id,
                "owner_id": owner_id,
                "title": title,
                "price": price_text or "",
                "descr": descr or "",
                "contact": contact,
                "photo_file_id": photo_id or None,
            })
            res = await s.execute(sql("SELECT last_insert_rowid()"))
            listing_id = int(res.scalar_one())

            await s.execute(sql("""
                INSERT INTO events_meta (
                    listing_id, start_at_utc, timezone, venue_text, city_text, price_text, status
                ) VALUES (
                    :listing_id, :start_at_utc, 'Europe/Belgrade', :venue_text, :city_text, :price_text, 'pending'
                )
            """), {
                "listing_id": listing_id,
                "start_at_utc": start_at_utc,
                "venue_text": venue_text or None,
                "city_text": city_text or None,
                "price_text": price_text or None,
            })
            await s.commit()

        # Чистим FSM СРАЗУ после commit: рестарт или сбой интерфейса в этом
        # окне не оставит состояние preview с возможностью повторной публикации.
        await state.clear()
    except Exception as e:
        print(f"[AFISHA] publish DB error: {e}")
        sent = await cb.message.answer("Не удалось сохранить объявление. Попробуйте позже.")
        last_bot_messages.setdefault(chat_id, []).append(sent.message_id)
        await register_bot_messages(chat_id, [sent.message_id])
        await state.clear()
        await cb.answer()
        return

    # Событие УЖЕ сохранено — ошибка аналитики не должна выглядеть как сбой
    try:
        from app.analytics import log_event
        await log_event("listing_created", user_id=owner_id,
                        section="events", entity_type="listing", entity_id=listing_id)
    except Exception as e:
        print(f"[AFISHA] publish analytics error listing_id={listing_id}: {e}")

    txt = "✅ Отправлено на модерацию. После проверки событие появится в Афише."

    rows = []
    try:
        main_btn = await get_common_menu_button('main_menu')
        if main_btn:
            rows.append([InlineKeyboardButton(text=main_btn.text, callback_data=main_btn.callback_data)])
    except Exception:
        pass

    kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    sent = await cb.message.answer(
        txt,
        disable_web_page_preview=True,
        reply_markup=kb
    )


    last_bot_messages.setdefault(chat_id, []).append(sent.message_id)
    await register_bot_messages(chat_id, [sent.message_id])
    await cb.answer()
    print(f"[AFISHA][PUBLISH] ok id={listing_id} | chat_id={chat_id}")

# Назад: чистим всё и возвращаем нужный шаг
@router.callback_query(F.data.startswith("af:add:back:"))
async def af_add_back(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    back_to = cb.data.rsplit(":", 1)[-1]
    print(f"[AFISHA][BACK][CLICK] chat={chat_id} data={cb.data}")

    # 0) удалить сообщение, по которому нажали
    try:
        await cb.message.delete()
    except Exception:
        pass

    # 1) снести nav/prompt
    try:
        data = await state.get_data()
        for key in ("nav_msg_id", "prompt_id"):
            mid = data.get(key)
            if mid:
                try:
                    await cb.bot.delete_message(chat_id, mid)
                except Exception:
                    pass
        await state.update_data(nav_msg_id=None, prompt_id=None)
    except Exception:
        pass

    # 2) подчистить служебные сообщения бота
    try:
        await clear_bot_messages(chat_id, cb.bot)
    except Exception:
        pass
    last_bot_messages[chat_id] = []
    print(f"[AFISHA][BACK][CLEAN] chat={chat_id}")

    # 3) переходы назад по шагам

    # выход из мастера совсем
    if back_to == "entry":
        await _delete_draft(cb.bot, chat_id, state)
        await state.clear()

        title = await get_text("events_choose_city", "ru") or "📅 <b>Афиша</b>"
        kb = await events_main_inline(lang="ru")

        msg = await cb.message.answer(title, parse_mode="HTML", reply_markup=kb)
        last_bot_messages[chat_id] = [msg.message_id]
        await register_bot_messages(chat_id, [msg.message_id])

        await cb.answer()
        print(f"[events_add.py][af_add_back][entry][done] chat={chat_id} data={cb.data} msg_id={msg.message_id}")
        return

    # назад к заголовку
    if back_to == "title":
        await state.set_state(AfishaAddStates.wait_title)
        nav_id = await _send_nav(chat_id, cb.bot, state, back_cb="af:add:back:entry")
        msg = await cb.message.answer("Измените название (или отправьте новое):")
        await state.update_data(prompt_id=msg.message_id)
        last_bot_messages.setdefault(chat_id, []).extend([nav_id, msg.message_id])
        await register_bot_messages(chat_id, [nav_id, msg.message_id])
        await cb.answer()
        print(f"[AFISHA][BACK] -> title")
        return

    # назад к дате
    if back_to == "date":
        await state.set_state(AfishaAddStates.wait_date)
        nav_id = await _send_nav(chat_id, cb.bot, state, back_cb="af:add:back:title")
        msg = await cb.message.answer("📅 Введите дату в формате DD-MM-YY (или «Сегодня/Завтра»):")
        await state.update_data(prompt_id=msg.message_id)
        last_bot_messages.setdefault(chat_id, []).extend([nav_id, msg.message_id])
        await register_bot_messages(chat_id, [nav_id, msg.message_id])
        await cb.answer()
        print(f"[AFISHA][BACK] -> date")
        return

    # назад ко времени
    if back_to == "time":
        await state.set_state(AfishaAddStates.wait_time)
        nav_id = await _send_nav(chat_id, cb.bot, state, back_cb="af:add:back:date")
        msg = await cb.message.answer("⏰ Укажите время (HH:MM), например 19:00:")
        await state.update_data(prompt_id=msg.message_id)
        last_bot_messages.setdefault(chat_id, []).extend([nav_id, msg.message_id])
        await register_bot_messages(chat_id, [nav_id, msg.message_id])
        await cb.answer()
        print(f"[AFISHA][BACK] -> time")
        return

    # назад к выбору города
    if back_to == "price":
        await state.set_state(AfishaAddStates.wait_city_choice)
        nav_id = await _send_nav(chat_id, cb.bot, state, back_cb="af:add:back:time")
        kb = await _kb_city_from_db()
        msg = await cb.message.answer("👇 Выберите город из списка ниже:", reply_markup=kb)
        await state.update_data(prompt_id=msg.message_id)
        last_bot_messages.setdefault(chat_id, []).extend([nav_id, msg.message_id])
        await register_bot_messages(chat_id, [nav_id, msg.message_id])
        await cb.answer()
        print(f"[AFISHA][BACK] -> city_choice")
        return

    # назад к меню цены
    if back_to == "price_text":
        await state.set_state(AfishaAddStates.wait_price_choice)
        nav_id = await _send_nav(chat_id, cb.bot, state, back_cb="af:add:back:price")
        msg = await cb.message.answer("💲 Укажите цену:", reply_markup=_kb_price())
        await state.update_data(prompt_id=msg.message_id)
        last_bot_messages.setdefault(chat_id, []).extend([nav_id, msg.message_id])
        await register_bot_messages(chat_id, [nav_id, msg.message_id])
        await cb.answer()
        print(f"[AFISHA][BACK] -> price_choice")
        return

    # назад к цене из площадки
    if back_to == "venue":
        await state.set_state(AfishaAddStates.wait_price_choice)
        nav_id = await _send_nav(chat_id, cb.bot, state, back_cb="af:add:back:price")
        msg = await cb.message.answer("💲 Укажите цену:", reply_markup=_kb_price())
        await state.update_data(prompt_id=msg.message_id)
        last_bot_messages.setdefault(chat_id, []).extend([nav_id, msg.message_id])
        await register_bot_messages(chat_id, [nav_id, msg.message_id])
        await cb.answer()
        print(f"[AFISHA][BACK] -> price_choice (from venue)")
        return

    # назад к площадке из описания
    if back_to == "descr":
        await state.set_state(AfishaAddStates.wait_venue)
        nav_id = await _send_nav(chat_id, cb.bot, state, back_cb="af:add:back:price")
        msg = await cb.message.answer("Площадка (можно пропустить):", reply_markup=_kb_skip("af:add:skip:venue"))
        await state.update_data(prompt_id=msg.message_id)
        last_bot_messages.setdefault(chat_id, []).extend([nav_id, msg.message_id])
        await register_bot_messages(chat_id, [nav_id, msg.message_id])
        await cb.answer()
        print(f"[AFISHA][BACK] -> venue")
        return

    # fallback — только если что-то вообще неизвестное
    await state.set_state(AfishaAddStates.wait_title)
    nav_id = await _send_nav(chat_id, cb.bot, state, back_cb="af:add:back:entry")
    msg = await cb.message.answer("📝 Введите <b>название</b> события (до 100 символов):", parse_mode="HTML")
    await state.update_data(prompt_id=msg.message_id)
    last_bot_messages.setdefault(chat_id, []).extend([nav_id, msg.message_id])
    await register_bot_messages(chat_id, [nav_id, msg.message_id])
    await cb.answer()
    print(f"[AFISHA][BACK] -> default(entry)")
