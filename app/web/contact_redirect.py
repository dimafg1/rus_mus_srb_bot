from fastapi import APIRouter, HTTPException, Request
from starlette.responses import RedirectResponse
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Listing
from app.analytics.listing_views import log_listing_view
from app.web.security import verify_contact_click_token

router = APIRouter(prefix="/rus_mus_srb_bot/go")


def _is_telegram_preview(user_agent: str) -> bool:
    ua = (user_agent or "").lower()
    return (
        "telegrambot" in ua
        or "telegram-bot" in ua
        or "telegrambot (like twitterbot)" in ua
    )


@router.get("/contact")
async def go_contact(request: Request, t: str):
    """
    1-click redirect на контакт продавца.
    Сценарий:
    - бот выдаёт ссылку с подписанным токеном
    - веб-роут валидирует токен
    - логирует action='contact'
    - делает 302 на https://t.me/<username>
    """
    try:
        auth = verify_contact_click_token(t)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    async with SessionLocal() as s:
        listing = (await s.execute(
            select(Listing).where(Listing.id == auth.listing_id)
        )).scalar_one_or_none()

    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")

    contact = (listing.contact or "").strip()
    if not contact.startswith("@"):
        raise HTTPException(status_code=404, detail="Telegram contact not found")

    username = contact.lstrip("@")
    user_agent = request.headers.get("user-agent", "")

    # ВАЖНО: превью Telegram не считаем реальным кликом
    if not _is_telegram_preview(user_agent):
        await log_listing_view(
            listing_id=listing.id,
            user_id=auth.user_id,
            section="market",
            action="contact",
            source=auth.source,
        )

    return RedirectResponse(
        url=f"https://t.me/{username}",
        status_code=302,
    )