# app/web/app.py
from fastapi import FastAPI
from app.web.proxy_media import router as proxy_media_router

app = FastAPI(title="Unixound Bot Web")
app.include_router(proxy_media_router)
