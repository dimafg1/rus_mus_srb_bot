# app/web/proxy_media.py
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import StreamingResponse
from aiogram import Bot
from sqlalchemy import select
import aiohttp
import re
import json

from app.database import SessionLocal
from app.models import Listing
from app.web.security import verify_token  # проверка подписи токена t
import asyncio
import mimetypes

router = APIRouter(prefix="/rus_mus_srb_bot/media")

# HTML5 <video> требует поддержку Range, иначе перемотка/превью глючат
_RANGE_RE = re.compile(r"bytes=(\d+)-(\d+)?")

def get_bot() -> Bot:
    """Возвращает ваш единый экземпляр бота."""
    from app.main import bot
    return bot

async def tg_file_url(bot: Bot, file_id: str) -> str:
    f = await bot.get_file(file_id)
    return f"https://api.telegram.org/file/bot{bot.token}/{f.file_path}"

def _split_ids(raw: str | None) -> list[str]:
    """Парсим строку с file_id: допускаем запятые, пробелы, переносы."""
    if not raw:
        return []
    parts = []
    for chunk in raw.replace("\n", ",").replace("\r", ",").split(","):
        s = chunk.strip()
        if s:
            parts.append(s)
    # на всякий: если кто-то разделил пробелами
    if len(parts) == 1 and " " in parts[0]:
        parts = [p for p in parts[0].split(" ") if p.strip()]
    return parts

def _collect_allowed_file_ids(listing: Listing) -> set[str]:
    """Собираем все допустимые file_id из объявления (без миграций БД)."""
    allowed: set[str] = set()

    # 1) Основное поле: может быть один id или несколько через запятую
    for pfid in _split_ids(getattr(listing, "photo_file_id", None)):
        allowed.add(pfid)

    # 2) Доп. медиа в flex (JSON-текст) — опционально
    flex_raw = getattr(listing, "flex", None)
    if not flex_raw:
        return allowed

    try:
        data = json.loads(flex_raw)
    except Exception:
        return allowed

    # Вариант A: одиночное поле video_file_id (на будущее)
    vfid = data.get("video_file_id")
    if isinstance(vfid, str) and vfid.strip():
        allowed.add(vfid.strip())

    # Вариант B: список строк videos
    videos = data.get("videos")
    if isinstance(videos, list):
        for v in videos:
            if isinstance(v, str) and v.strip():
                allowed.add(v.strip())

    # Вариант C: media[] со словарями
    media = data.get("media")
    if isinstance(media, list):
        for m in media:
            if isinstance(m, dict):
                fid = m.get("file_id")
                if isinstance(fid, str) and fid.strip():
                    allowed.add(fid.strip())

    # Вариант D: photos[] как список строк
    photos = data.get("photos")
    if isinstance(photos, list):
        for p in photos:
            if isinstance(p, str) and p.strip():
                allowed.add(p.strip())

    return allowed

@router.get("/telegram/{kind}/{file_id}")
async def proxy_telegram_media(kind: str, file_id: str, request: Request, t: str | None = None):
    """
    Прокси для фото/видео из Telegram:
    - Доступ только при корректном токене t (editor|media), выданном ботом.
    - Берём объявление по listing_id из токена, сверяем owner_id.
    - Разрешаем file_id только из самого объявления:
        * photo_file_id (в т.ч. несколько через запятую/пробелы/переносы)
        * + опционально из flex (videos/media/photos/...)
    - Без записи на диск, поток прямо из Telegram.
    - Поддержка Range (206/Content-Range/Accept-Ranges/Content-Length).
    """
    if kind not in ("photo", "video"):
        raise HTTPException(status_code=404, detail="Unknown kind")

    if not t:
        raise HTTPException(status_code=401, detail="Missing token")
    try:
        auth = verify_token(t)  # примем editor или media
        if auth.purpose not in ("editor", "media"):
            raise ValueError("Wrong purpose")
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    async with SessionLocal() as s:
        res = await s.execute(select(Listing).where(Listing.id == auth.listing_id))
        listing = res.scalar_one_or_none()
        if not listing:
            raise HTTPException(status_code=404, detail="Listing not found")
        if getattr(listing, "owner_id", None) != auth.owner_id:
            raise HTTPException(status_code=403, detail="Forbidden")

        allowed = _collect_allowed_file_ids(listing)
        if file_id not in allowed:
            raise HTTPException(status_code=404, detail="Unknown media for this listing")

    headers = {}
    client_range = request.headers.get("Range")
    if client_range:
        m = _RANGE_RE.match(client_range.strip())
        if not m:
            raise HTTPException(status_code=416, detail="Bad Range")
        headers["Range"] = client_range

    bot = get_bot()
    url = await tg_file_url(bot, file_id)

    async with aiohttp.ClientSession() as c:
        async with c.get(url, headers=headers) as r:
            if r.status not in (200, 206):
                raise HTTPException(status_code=502, detail=f"Telegram responded {r.status}")

        # --- определить корректный Content-Type ---
        ct = r.headers.get("Content-Type")
        if not ct or ct.startswith("application/octet-stream"):
            if kind == "photo":
                ct = "image/jpeg"
            elif kind == "video":
                ct = "video/mp4"
            else:
                guess, _ = mimetypes.guess_type(str(url))
                ct = guess or "application/octet-stream"

        # === НАДЁЖНЫЙ ПУТЬ ДЛЯ ФОТО: докачиваем по Range и собираем байты ===
        if kind == "photo":
            data = bytearray()
            CHUNK = 256 * 1024  # 256KB
            MAX_TRIES = 3

            # закрываем текущий r (он нам больше не нужен), будем сами бить на куски
            await r.release()

            start = 0
            while True:
                rng = {"Range": f"bytes={start}-{start+CHUNK-1}"}
                tries = 0
                while True:
                    try:
                        async with c.get(url, headers=rng) as part:
                            if part.status not in (200, 206):
                                raise HTTPException(status_code=502, detail=f"Telegram responded {part.status}")
                            chunk = await part.read()
                            data.extend(chunk)
                            # если сервер вернул меньше чем запросили — это последний кусок
                            if len(chunk) < CHUNK:
                                break
                            start += len(chunk)
                            break
                    except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError):
                        tries += 1
                        if tries >= MAX_TRIES:
                            raise HTTPException(status_code=502, detail="Upstream closed during photo download")
                        await asyncio.sleep(0.2 * tries)  # маленький бэкофф и повтор

                # выходим из внешнего цикла, если это был последний кусок
                if len(data) < start + CHUNK:
                    break

            from fastapi import Response
            return Response(
                content=bytes(data),
                media_type=ct,
                headers={"Content-Disposition": "inline"},
            )

        # === ДЛЯ ВИДЕО ОСТАВЛЯЕМ СТРИМ (Range) ===
        resp_headers = {
            "Content-Type": ct,
            "Accept-Ranges": "bytes",
            "Content-Disposition": "inline",
        }
        if "Content-Range" in r.headers:
            resp_headers["Content-Range"] = r.headers["Content-Range"]

        status = 206 if ("Content-Range" in r.headers or client_range) else 200

        async def body():
            try:
                async for chunk in r.content.iter_chunked(64 * 1024):
                    yield chunk
            except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError):
                return
            except asyncio.CancelledError:
                return

        return StreamingResponse(body(), status_code=status, headers=resp_headers)
