from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from app.database import SessionLocal
from app.web.proxy_media import router as proxy_media_router
from app.web.contact_redirect import router as contact_redirect_router

app = FastAPI(title="Unixound Bot Web")
app.include_router(proxy_media_router)
app.include_router(contact_redirect_router)

_VIDEO_PLAYER_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Видео</title>
<style>html,body,#player{margin:0;width:100%;height:100%;background:#000;color:#fff}
body{display:flex;align-items:center;justify-content:center;font:16px sans-serif}
iframe{border:0;width:100%;height:100%}</style></head>
<body><div id="player">Не удалось открыть видео</div><script>
(() => {
  const raw = new URLSearchParams(location.search).get('u') || '';
  let id = '';
  try {
    const u = new URL(raw);
    const host = u.hostname.toLowerCase().replace(/^www\\./, '');
    if (host === 'youtu.be') id = u.pathname.split('/').filter(Boolean)[0] || '';
    if (host === 'youtube.com' || host === 'm.youtube.com') {
      id = u.searchParams.get('v') || '';
      if (!id && u.pathname.startsWith('/shorts/')) id = u.pathname.split('/')[2] || '';
      if (!id && u.pathname.startsWith('/embed/')) id = u.pathname.split('/')[2] || '';
    }
  } catch (_) {}
  if (!/^[A-Za-z0-9_-]{6,20}$/.test(id)) return;
  const frame = document.createElement('iframe');
  frame.src = 'https://www.youtube-nocookie.com/embed/' + encodeURIComponent(id) + '?autoplay=1';
  frame.allow = 'autoplay; encrypted-media; picture-in-picture';
  frame.allowFullscreen = true;
  const player = document.getElementById('player');
  player.textContent = '';
  player.appendChild(frame);
})();
</script></body></html>"""


@app.get("/rus_mus_srb_bot/media/video_yt.html", response_class=HTMLResponse)
async def youtube_player():
    return HTMLResponse(
        _VIDEO_PLAYER_HTML,
        headers={
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                "frame-src https://www.youtube-nocookie.com"
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


@app.get("/healthz")
async def healthz():
    async with SessionLocal() as session:
        await session.execute(text("SELECT 1"))
    return {"ok": True}
