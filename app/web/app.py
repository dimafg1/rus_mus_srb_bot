from fastapi import FastAPI
from app.web.proxy_media import router as proxy_media_router
from app.web.contact_redirect import router as contact_redirect_router

app = FastAPI(title="Unixound Bot Web")
app.include_router(proxy_media_router)
app.include_router(contact_redirect_router)

from fastapi import Request
from datetime import datetime
import aiosqlite


@app.get("/log_contact_click")
async def log_contact_click(request: Request):
    try:
        listing_id = request.query_params.get("listing_id")
        source = request.query_params.get("source")

        async with aiosqlite.connect("dev.db") as db:
            await db.execute("""
                INSERT INTO listing_views (listing_id, action, source, created_at)
                VALUES (?, 'contact_click', ?, ?)
            """, (
                int(listing_id) if listing_id else None,
                source or "unknown",
                datetime.utcnow().isoformat()
            ))
            await db.commit()

    except Exception as e:
        print("[web] log_contact_click ERROR:", e)

    return {"ok": True}

