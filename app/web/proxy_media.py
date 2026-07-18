# app/web/proxy_media.py
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import Response, StreamingResponse
from sqlalchemy import select
import aiohttp
import os
import re
import json
from pathlib import Path

from app.database import SessionLocal
from app.db_path import dotenv_value
from app.models import Listing, ReleaseMeta
from app.web.security import verify_token  # проверка подписи токена t
import asyncio
import mimetypes

router = APIRouter(prefix="/rus_mus_srb_bot/media")

_PRIVATE_MEDIA_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

# HTML5 <video> требует поддержку Range, иначе перемотка/превью глючат
_RANGE_RE = re.compile(r"bytes=([0-9]*)-([0-9]*)", re.IGNORECASE)


def _validated_range_header(raw: str) -> str:
    """Validate one RFC 7233 byte range before forwarding it upstream."""
    value = raw.strip()
    match = _RANGE_RE.fullmatch(value)
    if not match:
        raise HTTPException(
            status_code=416,
            detail="Bad Range",
            headers=_PRIVATE_MEDIA_HEADERS,
        )
    start, end = match.groups()
    if not start and not end:
        raise HTTPException(
            status_code=416,
            detail="Bad Range",
            headers=_PRIVATE_MEDIA_HEADERS,
        )
    if len(start) > 20 or len(end) > 20:
        raise HTTPException(
            status_code=416,
            detail="Bad Range",
            headers=_PRIVATE_MEDIA_HEADERS,
        )
    if start and end and int(end) < int(start):
        raise HTTPException(
            status_code=416,
            detail="Bad Range",
            headers=_PRIVATE_MEDIA_HEADERS,
        )
    return f"bytes={start}-{end}"

def _get_bot_token() -> str:
    root = Path(__file__).resolve().parents[2]
    token = os.getenv("BOT_TOKEN") or dotenv_value(root / ".env", "BOT_TOKEN")
    if not token:
        raise HTTPException(status_code=503, detail="Bot token is not configured")
    return token.strip()


async def tg_file_url(file_id: str) -> str:
    token = _get_bot_token()
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"https://api.telegram.org/bot{token}/getFile",
                params={"file_id": file_id},
            ) as response:
                payload = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Telegram getFile failed") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Invalid Telegram getFile response")
    result = payload.get("result")
    if (
        not payload.get("ok")
        or not isinstance(result, dict)
        or not isinstance(result.get("file_path"), str)
        or not result["file_path"]
    ):
        raise HTTPException(status_code=404, detail="Telegram file not found")
    return f"https://api.telegram.org/file/bot{token}/{result['file_path']}"

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

def _collect_allowed_file_ids(
    listing: Listing,
    release_video_file_id: str | None = None,
) -> set[str]:
    """Собираем все допустимые file_id из объявления (без миграций БД)."""
    allowed: set[str] = set()

    # 1) Основное поле: может быть один id или несколько через запятую
    for pfid in _split_ids(getattr(listing, "photo_file_id", None)):
        allowed.add(pfid)

    # 2) Доп. медиа в flex (JSON-текст) — опционально
    flex_raw = getattr(listing, "flex", None)
    if release_video_file_id:
        allowed.add(release_video_file_id.strip())
    if not flex_raw:
        return allowed

    try:
        data = json.loads(flex_raw)
    except Exception:
        return allowed
    if not isinstance(data, dict):
        return allowed

    # Вариант A: одиночное поле video_file_id (на будущее)
    vfid = data.get("video_file_id")
    if isinstance(vfid, str) and vfid.strip():
        allowed.add(vfid.strip())

    # Фактическое поле, которое используют старые мастера объявлений.
    legacy_video = data.get("video")
    if isinstance(legacy_video, str) and legacy_video.strip() and not legacy_video.startswith(("http://", "https://")):
        allowed.add(legacy_video.strip())

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

        release_video_file_id = None
        if listing.type == "release":
            release_video_file_id = (await s.execute(
                select(ReleaseMeta.video_file_id).where(
                    ReleaseMeta.listing_id == listing.id
                )
            )).scalar_one_or_none()
        allowed = _collect_allowed_file_ids(listing, release_video_file_id)
        if file_id not in allowed:
            raise HTTPException(status_code=404, detail="Unknown media for this listing")

    headers = {}
    client_range = request.headers.get("Range")
    if client_range:
        headers["Range"] = _validated_range_header(client_range)

    url = await tg_file_url(file_id)

    # Для StreamingResponse объекты session/response обязаны жить до конца
    # передачи тела. Поэтому не выходим из async with до возврата response.
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
    try:
        upstream = await session.get(url, headers=headers)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        await session.close()
        raise HTTPException(status_code=502, detail="Telegram media request failed") from exc

    if upstream.status == 416:
        response_headers = {
            **_PRIVATE_MEDIA_HEADERS,
            "Accept-Ranges": "bytes",
        }
        if "Content-Range" in upstream.headers:
            response_headers["Content-Range"] = upstream.headers["Content-Range"]
        upstream.release()
        await session.close()
        raise HTTPException(
            status_code=416,
            detail="Range not satisfiable",
            headers=response_headers,
        )
    if upstream.status not in (200, 206):
        status = upstream.status
        upstream.release()
        await session.close()
        raise HTTPException(status_code=502, detail=f"Telegram responded {status}")

    ct = upstream.headers.get("Content-Type")
    if not ct or ct.startswith("application/octet-stream"):
        if kind == "photo":
            ct = "image/jpeg"
        elif kind == "video":
            ct = "video/mp4"
        else:
            guess, _ = mimetypes.guess_type(str(url))
            ct = guess or "application/octet-stream"

    response_headers = {
        **_PRIVATE_MEDIA_HEADERS,
        "Accept-Ranges": "bytes",
        "Content-Disposition": "inline",
    }
    for name in ("Content-Range", "Content-Length", "ETag", "Last-Modified"):
        if name in upstream.headers:
            response_headers[name] = upstream.headers[name]

    if kind == "photo":
        try:
            data = await upstream.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise HTTPException(status_code=502, detail="Telegram photo download failed") from exc
        finally:
            upstream.release()
            await session.close()
        return Response(
            content=data,
            status_code=upstream.status,
            media_type=ct,
            headers=response_headers,
        )

    async def body():
        try:
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                yield chunk
        except (aiohttp.ClientError, asyncio.TimeoutError, asyncio.CancelledError):
            return
        finally:
            upstream.release()
            await session.close()

    return StreamingResponse(
        body(),
        status_code=upstream.status,
        media_type=ct,
        headers=response_headers,
    )
