# app/web/proxy_media.py
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import StreamingResponse
from aiogram import Bot
from sqlalchemy import select
import aiohttp
import re

from app.database import SessionLocal
from app.models import Listing
from app.web.security import verify_token  # проверка подписи токена t

router = APIRouter(prefix="/rus_mus_srb_bot/media")

# HTML5 <video> требует поддержку Range, иначе перемотка/превью глючат
_RANGE_RE = re.compile(r"bytes=(\d+)-(\d+)?")

def get_bot() -> Bot:
    """
    Возвращает ЕДИНЫЙ экземпляр бота.
    Если у вас в app.main определён глобальный 'bot', просто импортируем его.
    При необходимости можно сделать fallback на ENV-переменную.
    """
    from app.main import bot  # используем ваш экземпляр
    return bot

async def tg_file_url(bot: Bot, file_id: str) -> str:
    f = await bot.get_file(file_id)
    return f"https://api.telegram.org/file/bot{bot.token}/{f.file_path}"

@router.get("/telegram/{kind}/{file_id}")
async def proxy_telegram_media(kind: str, file_id: str, request: Request, t: str | None = None):
    """
    Прокси для фото/видео из Telegram:
    - ТОЛЬКО для известных нам media (валидируем по БД);
    - Без записи на диск, поток прямо из Telegram;
    - Поддержка Range (206/Content-Range/Accept-Ranges/Content-Length);
    - Доступ только при корректном токене t (editor|media), выданном ботом.
    """
    # 0) базовая проверка
    if kind not in ("photo", "video"):
        raise HTTPException(status_code=404, detail="Unknown kind")

    # 1) проверка токена (примем и editor, и media)
    if not t:
        raise HTTPException(status_code=401, detail="Missing token")
    try:
        auth = verify_token(t)  # без указания purpose — примем оба
        if auth.purpose not in ("editor", "media"):
            raise ValueError("Wrong purpose")
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    # 2) валидация, что file_id действительно наш (по текущей схеме: Listing.photo_file_id)
    #    Для видео поддержку обсудим отдельно; здесь проверяем только наличие file_id в Listing.photo_file_id.
    async with SessionLocal() as s:
        res = await s.execute(select(Listing).where(Listing.photo_file_id == file_id))
        listing = res.scalar_one_or_none()
        if not listing:
            raise HTTPException(status_code=404, detail="Unknown media")

        # Совпадение listing/owner с данными из токена
        if getattr(listing, "id", None) != auth.listing_id or getattr(listing, "owner_id", None) != auth.owner_id:
            raise HTTPException(status_code=403, detail="Forbidden")

    # 3) пробрасываем клиентский Range (если есть)
    headers = {}
    client_range = request.headers.get("Range")
    if client_range:
        m = _RANGE_RE.match(client_range.strip())
        if not m:
            raise HTTPException(status_code=416, detail="Bad Range")
        headers["Range"] = client_range

    # 4) получаем реальный URL у Telegram и стримим без сохранения
    bot = get_bot()
    url = await tg_file_url(bot, file_id)

    async with aiohttp.ClientSession() as c:
        async with c.get(url, headers=headers) as r:
            if r.status not in (200, 206):
                raise HTTPException(status_code=502, detail=f"Telegram responded {r.status}")

            resp_headers = {
                "Content-Type": r.headers.get("Content-Type", "application/octet-stream"),
                "Accept-Ranges": "bytes",
            }
            if "Content-Length" in r.headers:
                resp_headers["Content-Length"] = r.headers["Content-Length"]
            if "Content-Range" in r.headers:
                resp_headers["Content-Range"] = r.headers["Content-Range"]

            status = 206 if ("Content-Range" in r.headers or client_range) else 200

            async def body():
                async for chunk in r.content.iter_chunked(64 * 1024):
                    yield chunk

            return StreamingResponse(body(), status_code=status, headers=resp_headers)
