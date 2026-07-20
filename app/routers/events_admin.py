"""app/routers/events_admin.py

Модерация Афиши v1:
- список pending
- approve -> published
- reject  -> rejected
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from sqlalchemy import text as sql
from app.database import SessionLocal
from app.events_meta import ensure_events_meta
from app.routers.utils import clear_bot_messages, last_bot_messages, log, escape_html, get_text
from app.routers.admin_panel import is_admin


router = Router(name="events_admin")

PAGE = 10
_TZ = ZoneInfo("Europe/Belgrade")


async def _fetch_pending(offset: int) -> list[dict]:
    now_utc = int(datetime.now(timezone.utc).timestamp())
    q = sql("""
        SELECT
            l.id AS id,
            l.title AS title,
            em.start_at_utc AS start_at_utc
        FROM listing l
        JOIN events_meta em ON em.listing_id = l.id
        WHERE l.type='events'
          AND l.status='active'
          AND l.is_sold=0
          AND em.status='pending'
          AND em.start_at_utc >= :now_utc
        ORDER BY em.start_at_utc ASC
        LIMIT :limit OFFSET :offset
    """)
    try:
        await ensure_events_meta()
        async with SessionLocal() as s:
            res = await s.execute(q, {"limit": PAGE, "offset": offset, "now_utc": now_utc})
            return [dict(r._mapping) for r in res.fetchall()]
    except Exception as e:
        print(f"[AFISHA][ADMIN] fetch_pending error: {e}")
        return []


async def _fetch_one(event_id: int) -> dict | None:
    now_utc = int(datetime.now(timezone.utc).timestamp())
    q = sql("""
        SELECT
            l.id AS id,
            l.title AS title,
            l.descr AS descr,
            l.contact AS contact,
            l.photo_file_id AS photo_file_id,
            CASE
                WHEN lower(COALESCE(c.slug, '')) = 'other'
                     AND NULLIF(trim(em.city_text), '') IS NOT NULL
                THEN em.city_text
                ELSE COALESCE(c.name, em.city_text)
            END AS city_name,
            em.venue_text AS venue_text,
            em.price_text AS price_text,
            em.start_at_utc AS start_at_utc,
            em.status AS status
        FROM listing l
        JOIN events_meta em ON em.listing_id = l.id
        LEFT JOIN city c ON c.id=l.city_id
        WHERE l.id=:id
          AND l.type='events'
          AND l.status='active'
          AND l.is_sold=0
          AND em.status='pending'
          AND em.start_at_utc >= :now_utc
        LIMIT 1
    """)
    try:
        await ensure_events_meta()
        async with SessionLocal() as s:
            res = await s.execute(q, {"id": event_id, "now_utc": now_utc})
            row = res.first()
            return dict(row._mapping) if row else None
    except Exception as e:
        print(f"[AFISHA][ADMIN] fetch_one error: {e}")
        return None


async def _kb_list(offset: int, has_more: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin:events:{max(0, offset-PAGE)}"))
    if has_more:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin:events:{offset+PAGE}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text=await get_text("events_admin_btn_back_to_panel", "ru") or "◀️ В админ-панель", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _kb_event(event_id: int, back_offset: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=await get_text("vac_add_btn_publish", "ru") or "✅ Опубликовать", callback_data=f"admin:event:pub:{event_id}:{back_offset}")],
        [InlineKeyboardButton(text=await get_text("events_admin_btn_reject", "ru") or "❌ Отклонить", callback_data=f"admin:event:rej:{event_id}:{back_offset}")],
        [InlineKeyboardButton(text=await get_text("events_admin_btn_back_to_list", "ru") or "◀️ К списку", callback_data=f"admin:events:{back_offset}")],
    ])


@router.callback_query(F.data.startswith("admin:events"))
async def admin_events_list(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    if not is_admin(cb.from_user.id):
        await cb.answer(await get_text("err_no_access_short", "ru") or "Нет доступа", show_alert=True)
        return

    # offset
    offset = 0
    parts = cb.data.split(":")
    if len(parts) == 3 and parts[2].isdigit():
        offset = int(parts[2])

    log(f"[events_admin.py] admin_events_list | chat_id={chat_id} offset={offset}")

    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    items = await _fetch_pending(offset)
    if not items:
        msg = await cb.message.answer(await get_text("events_admin_no_pending", "ru") or "Нет событий на модерации.", reply_markup=await _kb_list(offset=0, has_more=False))
        last_bot_messages[chat_id] = [msg.message_id]
        await cb.answer()
        return

    lines = [await get_text("events_admin_pending_header", "ru") or "🧾 <b>Афиша: модерация (pending)</b>"]
    title_fallback = await get_text("events_admin_title_fallback", "ru") or "Событие"
    kb_rows: list[list[InlineKeyboardButton]] = []
    for it in items:
        title = (it.get("title") or title_fallback).strip()
        if len(title) > 48:
            title = title[:45] + "…"
        kb_rows.append([InlineKeyboardButton(text=title, callback_data=f"admin:event:view:{int(it['id'])}:{offset}")])

    has_more = len(items) == PAGE
    kb_rows.extend((await _kb_list(offset=offset, has_more=has_more)).inline_keyboard)
    msg = await cb.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    last_bot_messages[chat_id] = [msg.message_id]
    await cb.answer()


@router.callback_query(F.data.startswith("admin:event:view:"))
async def admin_event_view(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    if not is_admin(cb.from_user.id):
        await cb.answer(await get_text("err_no_access_short", "ru") or "Нет доступа", show_alert=True)
        return

    parts = cb.data.split(":")
    try:
        event_id = int(parts[3])
    except Exception:
        event_id = 0
    try:
        back_offset = int(parts[4])
    except Exception:
        back_offset = 0

    log(f"[events_admin.py] admin_event_view | chat_id={chat_id} id={event_id}")

    try:
        await cb.message.delete()
    except Exception:
        pass
    await clear_bot_messages(chat_id, cb.bot)

    row = await _fetch_one(event_id)
    if not row:
        back_to_list_btn = await get_text("events_admin_btn_back_to_list", "ru") or "◀️ К списку"
        msg = await cb.message.answer(await get_text("events_admin_not_found", "ru") or "Не найдено.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=back_to_list_btn, callback_data=f"admin:events:{back_offset}")]]))
        last_bot_messages[chat_id] = [msg.message_id]
        await cb.answer()
        return

    dt = datetime.fromtimestamp(int(row.get("start_at_utc") or 0), tz=timezone.utc).astimezone(_TZ)
    when = dt.strftime("%d.%m.%Y %H:%M") if row.get("start_at_utc") else "—"
    title = escape_html(row.get("title") or (await get_text("events_admin_title_fallback", "ru") or "Событие"))
    city = escape_html(row.get("city_name") or (await get_text("af_no_city_fallback", "ru") or "Город"))
    venue = escape_html(row.get("venue_text") or "")
    price = escape_html(row.get("price_text") or "—")
    descr = escape_html((row.get("descr") or "").strip())
    txt = (
        f"🧾 <b>{title}</b>\n"
        f"📅 {when}\n"
        f"📍 {city}{(', ' + venue) if venue else ''}\n"
        f"💲 {price}\n\n"
        f"{descr}"
    ).strip()

    kb = await _kb_event(event_id, back_offset)
    if row.get("photo_file_id"):
        sent = await cb.message.answer_photo(photo=row["photo_file_id"], caption=txt, parse_mode="HTML", reply_markup=kb)
    else:
        sent = await cb.message.answer(txt, parse_mode="HTML", reply_markup=kb)
    last_bot_messages[chat_id] = [sent.message_id]
    await cb.answer()


async def _set_status(event_id: int, status: str) -> bool:
    if status not in {"published", "rejected"}:
        return False
    now_utc = int(datetime.now(timezone.utc).timestamp())
    try:
        await ensure_events_meta()
        async with SessionLocal() as s:
            result = await s.execute(sql("""
                UPDATE events_meta
                SET status=:st, updated_at=strftime('%s','now')
                WHERE listing_id=:id
                  AND status='pending'
                  AND start_at_utc >= :now_utc
                  AND EXISTS (
                      SELECT 1 FROM listing l
                      WHERE l.id=:id AND l.type='events'
                        AND l.status='active' AND l.is_sold=0
                  )
            """), {"st": status, "id": event_id, "now_utc": now_utc})
            await s.commit()
            return bool(result.rowcount)
    except Exception as e:
        print(f"[AFISHA][ADMIN] set_status error: {e}")
        return False


@router.callback_query(F.data.startswith("admin:event:pub:"))
async def admin_event_publish(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer(await get_text("err_no_access_short", "ru") or "Нет доступа", show_alert=True)
        return
    try:
        parts = (cb.data or "").split(":")
        event_id = int(parts[3])
        back_offset = max(0, int(parts[4]))
    except (IndexError, TypeError, ValueError):
        await cb.answer(await get_text("events_admin_stale_button", "ru") or "Некорректная или устаревшая кнопка.", show_alert=True)
        return
    if not await _set_status(event_id, "published"):
        await cb.answer(await get_text("events_admin_already_processed", "ru") or "Событие уже обработано или недоступно.", show_alert=True)
        return
    cb.data = f"admin:events:{back_offset}"
    await admin_events_list(cb)


@router.callback_query(F.data.startswith("admin:event:rej:"))
async def admin_event_reject(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer(await get_text("err_no_access_short", "ru") or "Нет доступа", show_alert=True)
        return
    try:
        parts = (cb.data or "").split(":")
        event_id = int(parts[3])
        back_offset = max(0, int(parts[4]))
    except (IndexError, TypeError, ValueError):
        await cb.answer(await get_text("events_admin_stale_button", "ru") or "Некорректная или устаревшая кнопка.", show_alert=True)
        return
    if not await _set_status(event_id, "rejected"):
        await cb.answer(await get_text("events_admin_already_processed", "ru") or "Событие уже обработано или недоступно.", show_alert=True)
        return
    cb.data = f"admin:events:{back_offset}"
    await admin_events_list(cb)
