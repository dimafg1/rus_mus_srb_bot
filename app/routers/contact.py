import re
from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton
from sqlalchemy import select

from app.database import SessionLocal
from app.models import ContactView, Listing

router = Router(name="contact")


def _contact_url(contact: str) -> str | None:
    """Превращает любой Telegram-контакт в https://t.me/ ссылку."""
    c = (contact or "").strip()
    if not c:
        return None
    cl = c.lower()
    if cl.startswith("https://t.me/"):
        return c
    if cl.startswith("http://t.me/"):
        return "https://" + c[7:]
    if cl.startswith("t.me/"):
        return "https://" + c
    if c.startswith("@"):
        return f"https://t.me/{c[1:]}"
    digits = re.sub(r"[^\d+]", "", c)
    if digits.startswith("+") and len(digits) >= 8:
        return f"https://t.me/{digits}"
    return None


def make_contact_btn(listing_id: int, contact: str, section: str) -> InlineKeyboardButton | None:
    """Возвращает callback-кнопку «Написать» или None если контакт не распознан."""
    if not _contact_url(contact):
        return None
    c = (contact or "").strip()
    if c.startswith("@"):
        label = f"💬 Написать {c}"
    else:
        digits = re.sub(r"[^\d+]", "", c)
        label = f"📞 {digits}" if digits.startswith("+") else "💬 Написать"
    return InlineKeyboardButton(
        text=label,
        callback_data=f"cnt:{listing_id}:{section}",
    )


@router.callback_query(F.data.startswith("cnt:"))
async def handle_contact_click(cb: CallbackQuery):
    parts = cb.data.split(":")
    try:
        listing_id = int(parts[1])
        section = parts[2] if len(parts) > 2 else "unknown"
    except (IndexError, ValueError):
        await cb.answer("Ошибка данных", show_alert=True)
        return

    async with SessionLocal() as s:
        listing = (await s.execute(
            select(Listing).where(Listing.id == listing_id)
        )).scalar_one_or_none()

        if not listing:
            await cb.answer("Объявление не найдено", show_alert=True)
            return

        s.add(ContactView(listing_id=listing_id, section=section, user_id=cb.from_user.id))
        await s.commit()
        url = _contact_url(listing.contact or "")

    if url:
        try:
            await cb.answer(url=url)
        except Exception:
            # fallback: показываем контакт текстом
            await cb.answer(listing.contact or "Контакт не указан", show_alert=True)
    else:
        await cb.answer(listing.contact or "Контакт не указан", show_alert=True)
