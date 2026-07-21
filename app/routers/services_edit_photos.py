from __future__ import annotations

from collections import defaultdict

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Listing
from app.routers.utils import clear_bot_messages, last_bot_messages, register_bot_messages, get_text
from app.routers.services_edit_overview import _render_overview
from app.keyboards import get_common_menu_button


router = Router(name="services_edit_photos")

# -------------------------------------------------------
# Запоминаем и дочищаем пользовательские медиа-сообщения
# -------------------------------------------------------
_user_media_msgs = defaultdict(list)


async def _remember_and_delete_user_media(msg: Message):
    try:
        _user_media_msgs[msg.chat.id].append(msg.message_id)
    except Exception:
        pass
    try:
        await msg.delete()
    except Exception:
        pass


async def _clear_user_media(chat_id: int, bot):
    ids = _user_media_msgs.pop(chat_id, [])
    for mid in ids:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass


# -------------------------------------------------------
# FSM
# -------------------------------------------------------
class ServicePhotoEditStates(StatesGroup):
    waiting_add_photo = State()
    waiting_replace_one = State()


# -------------------------------------------------------
# Внутренние утилиты
# -------------------------------------------------------
async def _get_listing(listing_id: int, owner_id: int | None = None) -> Listing | None:
    async with SessionLocal() as s:
        conditions = [Listing.id == listing_id, Listing.type == "service"]
        if owner_id is not None:
            conditions.append(Listing.owner_id == owner_id)
        return (await s.execute(select(Listing).where(*conditions))).scalar_one_or_none()


async def _save_listing_photos(listing_id: int, owner_id: int, photo_ids: list[str]) -> bool:
    async with SessionLocal() as s:
        listing = (
            await s.execute(select(Listing).where(
                Listing.id == listing_id,
                Listing.owner_id == owner_id,
                Listing.type == "service",
            ))
        ).scalar_one_or_none()
        if not listing:
            return False

        listing.photo_file_id = ",".join(photo_ids) if photo_ids else None
        await s.commit()
        return True


async def _authorize_photo_edit(cb: CallbackQuery, listing_id: int) -> Listing | None:
    listing = await _get_listing(listing_id, cb.from_user.id)
    if listing is None:
        await cb.answer(await get_text("err_not_owner_service", "ru") or "Можно редактировать только свои услуги.", show_alert=True)
    return listing


async def _authorize_photo_message(message: Message, state: FSMContext, listing_id: int) -> bool:
    if await _get_listing(listing_id, message.from_user.id):
        return True
    await state.clear()
    await message.answer(await get_text("err_not_owner_service", "ru") or "Можно редактировать только свои услуги.")
    return False


async def _require_current_photo_session(cb: CallbackQuery, state: FSMContext, listing_id: int) -> dict | None:
    """Не смешивать callback старой карточки с черновиком другого объявления."""
    data = await state.get_data()
    if data.get("sphoto_listing_id") != listing_id:
        await cb.answer(await get_text("photo_edit_session_stale", "ru") or "Сеанс редактирования устарел. Откройте фото ещё раз.", show_alert=True)
        return None
    return data


def _draft_from_listing(listing: Listing) -> list[str]:
    if not listing or not listing.photo_file_id:
        return []
    return [x.strip() for x in listing.photo_file_id.split(",") if x.strip()]


async def _photo_editor_kb(listing_id: int, draft: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if len(draft) < 3:
        rows.append([
            InlineKeyboardButton(
                text=await get_text("photo_edit_btn_add", "ru") or "➕ Добавить фото",
                callback_data=f"sphoto:add:{listing_id}"
            )
        ])

    swap_tmpl = await get_text("photo_edit_btn_swap_tmpl", "ru") or "🔁 Заменить фото {idx}"
    delete_tmpl = await get_text("photo_edit_btn_delete_tmpl", "ru") or "❌ Удалить фото {idx}"
    for idx, _ in enumerate(draft, start=1):
        rows.append([
            InlineKeyboardButton(
                text=swap_tmpl.format(idx=idx),
                callback_data=f"sphoto:swap:{listing_id}:{idx}"
            ),
            InlineKeyboardButton(
                text=delete_tmpl.format(idx=idx),
                callback_data=f"sphoto:del:{listing_id}:{idx}"
            ),
        ])

    back_btn = await get_common_menu_button('back')
    if back_btn:
        back_btn.callback_data = f"sphoto:back:{listing_id}"
        rows.append([back_btn])

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _cancel_kb(listing_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=await get_text("photo_edit_btn_cancel", "ru") or "⬅️ Отмена", callback_data=f"sphoto:cancel:{listing_id}")]
        ]
    )


async def _confirm_kb(listing_id: int) -> InlineKeyboardMarkup:
    cancel_text = await get_text("photo_edit_btn_cancel", "ru") or "⬅️ Отмена"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=await get_text("photo_edit_btn_confirm", "ru") or "✅ Подтвердить", callback_data=f"sphoto:apply:{listing_id}"),
                InlineKeyboardButton(text=cancel_text, callback_data=f"sphoto:cancel:{listing_id}")
            ]
        ]
    )


async def _clear_pending_action(state: FSMContext):
    await state.update_data(
        sphoto_pending_action=None,
        sphoto_pending_index=None,
        sphoto_pending_photo_ids=None,
    )


async def _render_photo_editor(chat_id: int, bot, send, listing_id: int, state: FSMContext):
    await clear_bot_messages(chat_id, bot)
    await _clear_user_media(chat_id, bot)

    data = await state.get_data()
    draft = data.get("sphoto_draft_ids")

    if draft is None:
        listing = await _get_listing(listing_id)
        if not listing:
            msg = await send(await get_text("err_listing_404", "ru") or "Объявление не найдено.")
            last_bot_messages[chat_id] = [msg.message_id]
            await register_bot_messages(chat_id, [msg.message_id])
            return
        draft = _draft_from_listing(listing)
        await state.update_data(
            sphoto_listing_id=listing_id,
            sphoto_draft_ids=draft,
        )

    header_tmpl = (
        await get_text("photo_edit_header_services_tmpl", "ru")
        or "🖼 <b>Редактирование фото услуги</b>\n\nСейчас фото: <b>{count} / 3</b>\n\nМожно удалить отдельное фото, добавить новое или заменить конкретное фото."
    )
    text = header_tmpl.format(count=len(draft))

    message_ids = []

    if draft:
        try:
            if len(draft) == 1:
                p = await bot.send_photo(chat_id, draft[0])
                message_ids.append(p.message_id)
            else:
                media = [InputMediaPhoto(media=fid) for fid in draft]
                msgs = await bot.send_media_group(chat_id, media=media)
                message_ids.extend([m.message_id for m in msgs])
        except Exception as e:
            print(f"[services_edit_photos.py] _render_photo_editor ✗ photo_send_failed | chat_id={chat_id} listing_id={listing_id} | {type(e).__name__}: {e}")

    msg = await send(
        text,
        reply_markup=await _photo_editor_kb(listing_id, draft),
        parse_mode="HTML"
    )
    message_ids.append(msg.message_id)

    last_bot_messages[chat_id] = message_ids
    await register_bot_messages(chat_id, message_ids)

    print(
        f"[services_edit_photos.py] _render_photo_editor | "
        f"chat_id={chat_id} | listing_id={listing_id} | photos={len(draft)}"
    )


async def _show_confirmation(
    chat_id: int,
    bot,
    send,
    listing_id: int,
    text: str,
    preview_photo_ids: list[str] | None = None,
):
    await clear_bot_messages(chat_id, bot)
    await _clear_user_media(chat_id, bot)

    message_ids: list[int] = []

    if preview_photo_ids:
        try:
            if len(preview_photo_ids) == 1:
                p = await bot.send_photo(chat_id, preview_photo_ids[0])
                message_ids.append(p.message_id)
            else:
                media = [InputMediaPhoto(media=fid) for fid in preview_photo_ids]
                msgs = await bot.send_media_group(chat_id, media=media)
                message_ids.extend([m.message_id for m in msgs])
        except Exception as e:
            print(f"[services_edit_photos.py] _show_confirmation ✗ photo_send_failed | chat_id={chat_id} listing_id={listing_id} | {type(e).__name__}: {e}")

    msg = await send(text, reply_markup=await _confirm_kb(listing_id))
    message_ids.append(msg.message_id)

    last_bot_messages[chat_id] = message_ids
    await register_bot_messages(chat_id, message_ids)

    print(
        f"[services_edit_photos.py] _show_confirmation | "
        f"chat_id={chat_id} | listing_id={listing_id} | preview={len(preview_photo_ids or [])}"
    )


# -------------------------------------------------------
# Открыть редактор фото
# -------------------------------------------------------
@router.callback_query(F.data.startswith("sphoto:open:"))
async def sphoto_open(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    try:
        await cb.message.delete()
    except Exception:
        pass

    try:
        listing_id = int(cb.data.split(":")[2])
    except Exception:
        await cb.answer(await get_text("err_invalid_data", "ru") or "Некорректные данные.", show_alert=True)
        return

    listing = await _get_listing(listing_id, cb.from_user.id)
    if not listing:
        await cb.answer(await get_text("err_listing_404", "ru") or "Объявление не найдено.", show_alert=True)
        return

    await state.update_data(
        sphoto_listing_id=listing_id,
        sphoto_draft_ids=_draft_from_listing(listing),
    )
    await _clear_pending_action(state)
    await state.set_state(None)

    await _render_photo_editor(chat_id, cb.message.bot, cb.message.answer, listing_id, state)
    await cb.answer()

    print(
        f"[services_edit_photos.py] sphoto_open | "
        f"chat_id={chat_id} | listing_id={listing_id}"
    )


# -------------------------------------------------------
# Назад в overview
# -------------------------------------------------------
@router.callback_query(F.data.startswith("sphoto:back:"))
async def sphoto_back(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    try:
        await cb.message.delete()
    except Exception:
        pass

    try:
        listing_id = int(cb.data.split(":")[2])
    except Exception:
        await cb.answer(await get_text("err_invalid_data", "ru") or "Некорректные данные.", show_alert=True)
        return
    if not await _authorize_photo_edit(cb, listing_id):
        return

    await _clear_pending_action(state)
    await state.update_data(
        sphoto_listing_id=None,
        sphoto_draft_ids=None,
    )
    await state.set_state(None)

    await _render_overview(chat_id, cb.message.bot, cb.message.answer, listing_id)
    await cb.answer()

    print(
        f"[services_edit_photos.py] sphoto_back | "
        f"chat_id={chat_id} | listing_id={listing_id}"
    )


# -------------------------------------------------------
# Отмена текущего действия -> обратно в редактор фото
# -------------------------------------------------------
@router.callback_query(F.data.startswith("sphoto:cancel:"))
async def sphoto_cancel(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    try:
        listing_id = int(cb.data.split(":")[2])
    except Exception:
        await cb.answer(await get_text("err_invalid_data", "ru") or "Некорректные данные.", show_alert=True)
        return
    if not await _authorize_photo_edit(cb, listing_id):
        return
    if await _require_current_photo_session(cb, state, listing_id) is None:
        return

    await _clear_pending_action(state)
    await state.set_state(None)

    await _render_photo_editor(chat_id, cb.bot, cb.message.answer, listing_id, state)
    await cb.answer()

    print(
        f"[services_edit_photos.py] sphoto_cancel | "
        f"chat_id={chat_id} | listing_id={listing_id}"
    )


# -------------------------------------------------------
# Удалить одно фото -> сначала подтверждение
# -------------------------------------------------------
@router.callback_query(F.data.regexp(r"^sphoto:del:(\d+):(\d+)$"))
async def sphoto_delete_request(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    parts = (cb.data or "").split(":")
    listing_id = int(parts[2])
    idx_1based = int(parts[3])
    if not await _authorize_photo_edit(cb, listing_id):
        return

    data = await _require_current_photo_session(cb, state, listing_id)
    if data is None:
        return
    draft = list(data.get("sphoto_draft_ids") or [])

    idx = idx_1based - 1
    if idx < 0 or idx >= len(draft):
        await cb.answer(await get_text("err_photo_404", "ru") or "Фото не найдено.", show_alert=True)
        return

    await state.update_data(
        sphoto_pending_action="delete",
        sphoto_pending_index=idx,
        sphoto_pending_photo_ids=None,
    )
    await state.set_state(None)

    delete_confirm_tmpl = await get_text("photo_edit_confirm_delete_tmpl", "ru") or "Удалить фото {idx}?"
    await _show_confirmation(
        chat_id,
        cb.bot,
        cb.message.answer,
        listing_id,
        delete_confirm_tmpl.format(idx=idx_1based)
    )
    await cb.answer()

    print(
        f"[services_edit_photos.py] sphoto_delete_request | "
        f"chat_id={chat_id} | listing_id={listing_id} | idx={idx}"
    )


# -------------------------------------------------------
# Добавить одно фото -> отправка фото -> подтверждение -> БД
# -------------------------------------------------------
@router.callback_query(F.data.startswith("sphoto:add:"))
async def sphoto_add(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)

    try:
        listing_id = int(cb.data.split(":")[2])
    except Exception:
        await cb.answer(await get_text("err_invalid_data", "ru") or "Некорректные данные.", show_alert=True)
        return
    if not await _authorize_photo_edit(cb, listing_id):
        return

    data = await _require_current_photo_session(cb, state, listing_id)
    if data is None:
        return
    draft = list(data.get("sphoto_draft_ids") or [])

    if len(draft) >= 3:
        await cb.answer(await get_text("photo_edit_max_3", "ru") or "Максимум 3 фото.", show_alert=True)
        return

    await _clear_pending_action(state)
    await state.update_data(sphoto_listing_id=listing_id)

    tmpl = await get_text("photo_edit_add_prompt", "ru") or "Отправьте одно новое фото для добавления.\n\nСейчас загружено: {count} / 3"
    msg = await cb.message.answer(
        tmpl.format(count=len(draft)),
        reply_markup=await _cancel_kb(listing_id)
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

    await state.set_state(ServicePhotoEditStates.waiting_add_photo)
    await cb.answer()

    print(
        f"[services_edit_photos.py] sphoto_add | "
        f"chat_id={chat_id} | listing_id={listing_id}"
    )


@router.message(ServicePhotoEditStates.waiting_add_photo, F.photo)
async def sphoto_add_receive(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await clear_bot_messages(chat_id, message.bot)
    await _remember_and_delete_user_media(message)

    data = await state.get_data()
    try:
        listing_id = int(data.get("sphoto_listing_id"))
    except (TypeError, ValueError):
        await state.clear()
        await message.answer(await get_text("photo_edit_session_lost", "ru") or "Сеанс редактирования потерян. Откройте фото ещё раз.")
        return
    if not await _authorize_photo_message(message, state, listing_id):
        return

    await state.update_data(
        sphoto_pending_action="add",
        sphoto_pending_photo_ids=[message.photo[-1].file_id],
    )
    await state.set_state(None)

    await _show_confirmation(
        chat_id,
        message.bot,
        message.answer,
        listing_id,
        await get_text("photo_edit_confirm_add", "ru") or "Добавить это фото к объявлению?",
        preview_photo_ids=[message.photo[-1].file_id]
    )

    print(
        f"[services_edit_photos.py] sphoto_add_receive | "
        f"chat_id={chat_id} | listing_id={listing_id}"
    )


@router.message(ServicePhotoEditStates.waiting_add_photo)
async def sphoto_add_not_photo(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await clear_bot_messages(chat_id, message.bot)
    await _remember_and_delete_user_media(message)

    data = await state.get_data()
    try:
        listing_id = int(data.get("sphoto_listing_id"))
    except (TypeError, ValueError):
        await state.clear()
        await message.answer(await get_text("photo_edit_session_lost", "ru") or "Сеанс редактирования потерян. Откройте фото ещё раз.")
        return
    if not await _authorize_photo_message(message, state, listing_id):
        return

    msg = await message.answer(
        (await get_text("photo_edit_need_one_photo", "ru") or "Пожалуйста, отправьте именно одно фото."),
        reply_markup=await _cancel_kb(listing_id)
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

    print(
        f"[services_edit_photos.py] sphoto_add_not_photo | "
        f"chat_id={chat_id} | listing_id={listing_id}"
    )


# -------------------------------------------------------
# Заменить одно фото -> отправка фото -> подтверждение -> БД
# -------------------------------------------------------
@router.callback_query(F.data.regexp(r"^sphoto:swap:(\d+):(\d+)$"))
async def sphoto_swap(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    await clear_bot_messages(chat_id, cb.bot)

    parts = (cb.data or "").split(":")
    listing_id = int(parts[2])
    idx = int(parts[3]) - 1
    if not await _authorize_photo_edit(cb, listing_id):
        return

    data = await _require_current_photo_session(cb, state, listing_id)
    if data is None:
        return
    draft = list(data.get("sphoto_draft_ids") or [])

    if idx < 0 or idx >= len(draft):
        await cb.answer(await get_text("err_photo_404", "ru") or "Фото не найдено.", show_alert=True)
        return

    await _clear_pending_action(state)
    await state.update_data(
        sphoto_listing_id=listing_id,
        sphoto_pending_index=idx
    )

    tmpl = await get_text("photo_edit_swap_prompt", "ru") or "Отправьте новое фото для замены фото {idx}."
    msg = await cb.message.answer(
        tmpl.format(idx=idx + 1),
        reply_markup=await _cancel_kb(listing_id)
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

    await state.set_state(ServicePhotoEditStates.waiting_replace_one)
    await cb.answer()

    print(
        f"[services_edit_photos.py] sphoto_swap | "
        f"chat_id={chat_id} | listing_id={listing_id} | idx={idx}"
    )


@router.message(ServicePhotoEditStates.waiting_replace_one, F.photo)
async def sphoto_receive_replace_one(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await clear_bot_messages(chat_id, message.bot)
    await _remember_and_delete_user_media(message)

    data = await state.get_data()
    try:
        listing_id = int(data.get("sphoto_listing_id"))
        idx = int(data.get("sphoto_pending_index"))
    except (TypeError, ValueError):
        await state.clear()
        await message.answer(await get_text("photo_edit_session_lost", "ru") or "Сеанс редактирования потерян. Откройте фото ещё раз.")
        return
    if not await _authorize_photo_message(message, state, listing_id):
        return

    await state.update_data(
        sphoto_pending_action="replace_one",
        sphoto_pending_photo_ids=[message.photo[-1].file_id],
    )
    await state.set_state(None)

    swap_confirm_tmpl = await get_text("photo_edit_confirm_swap_tmpl", "ru") or "Заменить фото {idx} новым фото?"
    await _show_confirmation(
        chat_id,
        message.bot,
        message.answer,
        listing_id,
        swap_confirm_tmpl.format(idx=idx + 1),
        preview_photo_ids=[message.photo[-1].file_id]
    )

    print(
        f"[services_edit_photos.py] sphoto_receive_replace_one | "
        f"chat_id={chat_id} | listing_id={listing_id} | idx={idx}"
    )


@router.message(ServicePhotoEditStates.waiting_replace_one)
async def sphoto_replace_one_not_photo(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await clear_bot_messages(chat_id, message.bot)
    await _remember_and_delete_user_media(message)

    data = await state.get_data()
    try:
        listing_id = int(data.get("sphoto_listing_id"))
    except (TypeError, ValueError):
        await state.clear()
        await message.answer(await get_text("photo_edit_session_lost", "ru") or "Сеанс редактирования потерян. Откройте фото ещё раз.")
        return
    if not await _authorize_photo_message(message, state, listing_id):
        return

    msg = await message.answer(
        (await get_text("photo_edit_need_photo", "ru") or "Пожалуйста, отправьте именно фото."),
        reply_markup=await _cancel_kb(listing_id)
    )
    last_bot_messages[chat_id] = [msg.message_id]
    await register_bot_messages(chat_id, [msg.message_id])

    print(
        f"[services_edit_photos.py] sphoto_replace_one_not_photo | "
        f"chat_id={chat_id} | listing_id={listing_id}"
    )


# -------------------------------------------------------
# Применить подтверждённое действие -> сразу запись в БД
# -------------------------------------------------------
@router.callback_query(F.data.startswith("sphoto:apply:"))
async def sphoto_apply(cb: CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id

    try:
        listing_id = int(cb.data.split(":")[2])
    except Exception:
        await cb.answer(await get_text("err_invalid_data", "ru") or "Некорректные данные.", show_alert=True)
        return

    data = await _require_current_photo_session(cb, state, listing_id)
    if data is None:
        return
    action = data.get("sphoto_pending_action")
    idx = data.get("sphoto_pending_index")
    pending_photo_ids = list(data.get("sphoto_pending_photo_ids") or [])

    listing = await _get_listing(listing_id, cb.from_user.id)
    if not listing:
        await cb.answer(await get_text("err_listing_404", "ru") or "Объявление не найдено.", show_alert=True)
        return

    current = _draft_from_listing(listing)
    new_photos = list(current)

    if action == "delete":
        if idx is None or idx < 0 or idx >= len(new_photos):
            await cb.answer(await get_text("err_photo_404", "ru") or "Фото не найдено.", show_alert=True)
            return
        new_photos.pop(idx)

    elif action == "add":
        if not pending_photo_ids:
            await cb.answer(await get_text("photo_edit_nothing_to_add", "ru") or "Нет фото для добавления.", show_alert=True)
            return
        if len(new_photos) >= 3:
            await cb.answer(await get_text("photo_edit_max_3", "ru") or "Максимум 3 фото.", show_alert=True)
            return
        new_photos.append(pending_photo_ids[0])
        new_photos = new_photos[:3]

    elif action == "replace_one":
        if idx is None or idx < 0 or idx >= len(new_photos):
            await cb.answer(await get_text("err_photo_404", "ru") or "Фото не найдено.", show_alert=True)
            return
        if not pending_photo_ids:
            await cb.answer(await get_text("photo_edit_nothing_to_replace", "ru") or "Нет фото для замены.", show_alert=True)
            return
        new_photos[idx] = pending_photo_ids[0]

    else:
        await cb.answer(await get_text("photo_edit_no_pending_action", "ru") or "Нет действия для подтверждения.", show_alert=True)
        return

    ok = await _save_listing_photos(listing_id, cb.from_user.id, new_photos)
    if not ok:
        await cb.answer(await get_text("photo_edit_save_failed", "ru") or "Не удалось сохранить фото.", show_alert=True)
        return

    await state.update_data(sphoto_draft_ids=new_photos)
    await _clear_pending_action(state)
    await state.set_state(None)

    await _render_photo_editor(chat_id, cb.bot, cb.message.answer, listing_id, state)
    await cb.answer(await get_text("photo_edit_applied", "ru") or "Изменения применены")

    print(
        f"[services_edit_photos.py] sphoto_apply | "
        f"chat_id={chat_id} | listing_id={listing_id} | action={action} | photos={len(new_photos)}"
    )
