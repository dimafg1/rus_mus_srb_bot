"""
app/database.py
---------------
Инициализация асинхронной базы данных с помощью SQLModel.

• По умолчанию используется SQLite-файл `dev.db`.
• Строку подключения можно переопределить в `.env`:
    DATABASE_URL=sqlite+aiosqlite:///./dev.db
    # или, позже:
    # DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/musicbot
"""

from typing import AsyncGenerator

from pydantic_settings import BaseSettings      # ✅
from pydantic import ConfigDict                 # ✅
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import sqlite3

# --------------------------------------------------------------------------- #
# Настройки читаются из .env, лишние переменные (например BOT_TOKEN) игнорируются
# --------------------------------------------------------------------------- #
class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./dev.db"

    # pydantic v2: разрешаем «лишние» ключи в .env
    model_config = ConfigDict(extra="ignore", env_file=".env")


settings = Settings()

# --------------------------------------------------------------------------- #
# Создаём движок и фабрику сессий
# --------------------------------------------------------------------------- #
engine = create_async_engine(settings.database_url, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


# --------------------------------------------------------------------------- #
# Асинхронный dependency-генератор для FastAPI / сервисов
# --------------------------------------------------------------------------- #
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


# --------------------------------------------------------------------------- #
# Создание таблиц (вызываем один раз при старте приложения / в сид-скрипте)
# --------------------------------------------------------------------------- #
async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

