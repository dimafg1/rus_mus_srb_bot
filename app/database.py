# app/database.py
from typing import AsyncGenerator
from pydantic_settings import BaseSettings
from pydantic import ConfigDict
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import event, text

# --------------------------------------------------------------------------- #
# Настройки из .env (DATABASE_URL=sqlite+aiosqlite:///./dev.db)
# --------------------------------------------------------------------------- #
class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./dev.db"
    model_config = ConfigDict(extra="ignore", env_file=".env")

settings = Settings()

# --------------------------------------------------------------------------- #
# ЕДИНСТВЕННОЕ создание движка (без повторных переопределений!)
# --------------------------------------------------------------------------- #
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    connect_args={"timeout": 30},   # важно для aiosqlite
    future=True,
    echo=False,
)

# PRAGMA для каждого подключения к SQLite
@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_conn, conn_record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA synchronous=NORMAL;")
    cur.execute("PRAGMA busy_timeout=30000;")  # 30 секунд ожидания, если занято
    cur.execute("PRAGMA foreign_keys=ON;")
    cur.close()

# --------------------------------------------------------------------------- #
# Фабрика сессий
# --------------------------------------------------------------------------- #
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# --------------------------------------------------------------------------- #
# Dependency / init
# --------------------------------------------------------------------------- #
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session

async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        # Миграция: добавляем first_seen если таблица создавалась до этой колонки
        try:
            await conn.execute(text("ALTER TABLE BotUser ADD COLUMN first_seen DATETIME"))
            await conn.execute(text("UPDATE BotUser SET first_seen = last_seen WHERE first_seen IS NULL"))
        except Exception:
            pass  # колонка уже есть — нормально
        # Миграция: источник первого входа (deep-link параметр /start)
        try:
            await conn.execute(text("ALTER TABLE BotUser ADD COLUMN first_source VARCHAR(64)"))
        except Exception:
            pass  # колонка уже есть — нормально
        # Посев выключателей монетизации (все выключены; словарь: app/features.py)
        try:
            for key in (
                "monetization_enabled",
                "paid_plans_enabled",
                "paid_ranking_enabled",
                "partner_rotation_enabled",
                "payments_enabled",
            ):
                await conn.execute(text(
                    "INSERT OR IGNORE INTO feature_flags (key, enabled, audience, updated_at) "
                    "VALUES (:key, 0, 'all', datetime('now'))"
                ), {"key": key})
        except Exception as e:
            print(f"[init_db] посев feature_flags: {e}")
