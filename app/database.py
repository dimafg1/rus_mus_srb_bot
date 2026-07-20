# app/database.py
from typing import AsyncGenerator
import os
from pathlib import Path

from pydantic_settings import BaseSettings
from pydantic import ConfigDict
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import event, text

from app.db_path import (
    absolutize_sqlite_url,
    dotenv_value,
    resolve_sqlite_path,
    sqlite_url_for_path,
)


ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------- #
# Настройки из .env (DATABASE_URL=sqlite+aiosqlite:///./dev.db)
# --------------------------------------------------------------------------- #
class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./dev.db"
    model_config = ConfigDict(extra="ignore", env_file=ROOT / ".env")

settings = Settings()

# ``sqlite:///./...`` is otherwise resolved from the launcher's cwd. Keep the
# async bot/web engine on the same file as sync admin/backup tools even when a
# developer starts Python via an absolute path from another directory.
_database_path_override = (
    os.getenv("DATABASE_PATH")
    or dotenv_value(ROOT / ".env", "DATABASE_PATH")
)
if _database_path_override:
    settings.database_url = sqlite_url_for_path(resolve_sqlite_path(ROOT))
else:
    settings.database_url = absolutize_sqlite_url(settings.database_url, ROOT)

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

def _is_duplicate_column(exc: Exception) -> bool:
    """SQLite бросает 'duplicate column name: X', когда ALTER TABLE ADD COLUMN
    добавляет уже существующую колонку — это ожидаемый, безопасный случай.
    Любую другую ошибку (database is locked, нет места, синтаксис) глушить
    нельзя: иначе бот стартует с недомигрированной БД."""
    return "duplicate column name" in str(exc).lower()


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        # Миграция: добавляем first_seen если таблица создавалась до этой колонки
        try:
            await conn.execute(text("ALTER TABLE BotUser ADD COLUMN first_seen DATETIME"))
            await conn.execute(text("UPDATE BotUser SET first_seen = last_seen WHERE first_seen IS NULL"))
        except Exception as e:
            if not _is_duplicate_column(e):
                raise  # колонка уже есть — нормально; остальное — громко наружу
        # Миграция: источник первого входа (deep-link параметр /start)
        try:
            await conn.execute(text("ALTER TABLE BotUser ADD COLUMN first_source VARCHAR(64)"))
        except Exception as e:
            if not _is_duplicate_column(e):
                raise
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
            # Релизы: включены сразу (бот до публичного запуска),
            # флаг — аварийный рубильник (Р-11)
            await conn.execute(text(
                "INSERT OR IGNORE INTO feature_flags (key, enabled, audience, updated_at) "
                "VALUES ('releases_enabled', 1, 'all', datetime('now'))"
            ))
        except Exception as e:
            print(f"[init_db] посев feature_flags: {e}")
        # Миграция: доп. поля карточки исполнителя (Р-12)
        for col, ddl in (
            ("descr", "TEXT"), ("genres", "VARCHAR(128)"),
            ("city_text", "VARCHAR(64)"), ("links", "TEXT"),
            ("contact", "VARCHAR(128)"),
        ):
            try:
                await conn.execute(text(f"ALTER TABLE artist ADD COLUMN {col} {ddl}"))
            except Exception as e:
                if not _is_duplicate_column(e):
                    raise  # колонка уже есть — нормально; остальное — громко наружу
        # Миграция: обратная связь — запрос ответа пользователем и отметка ответа (Р-15)
        for col, ddl in (
            ("needs_reply", "INTEGER NOT NULL DEFAULT 0"),
            ("answered_at", "DATETIME"),
            ("answer_text", "TEXT"),
        ):
            try:
                await conn.execute(text(f"ALTER TABLE feedback ADD COLUMN {col} {ddl}"))
            except Exception as e:
                if not _is_duplicate_column(e):
                    raise  # колонка уже есть — нормально; остальное — громко наружу
