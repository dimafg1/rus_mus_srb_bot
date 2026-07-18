"""Schema bootstrap and legacy migration for the Afisha metadata table."""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.database import engine


_MIGRATION_LOCK = asyncio.Lock()
_TABLE = "events_meta"
_NEW_TABLE = "events_meta__new"


def _create_table_sql(table_name: str) -> str:
    if table_name not in {_TABLE, _NEW_TABLE}:
        raise ValueError("Unexpected events_meta table name")
    return f"""
        CREATE TABLE \"{table_name}\" (
            listing_id       INTEGER NOT NULL PRIMARY KEY
                REFERENCES listing(id) ON DELETE CASCADE ON UPDATE CASCADE,
            start_at_utc     INTEGER NOT NULL,
            start_date_local TEXT,
            start_time_local TEXT,
            timezone         TEXT NOT NULL DEFAULT 'Europe/Belgrade',
            venue_text       TEXT,
            city_text        TEXT,
            lat              REAL,
            lon              REAL,
            price_text       TEXT,
            status           TEXT NOT NULL DEFAULT 'pending',
            created_at       INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            updated_at       INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            CHECK (status IN ('pending','published','rejected'))
        )
    """


_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_events_meta_start "
    "ON events_meta(start_at_utc)"
)
_TRIGGER_SQL = """
    CREATE TRIGGER IF NOT EXISTS trg_events_meta_updated
    AFTER UPDATE ON events_meta
    FOR EACH ROW
    BEGIN
        UPDATE events_meta
        SET updated_at = strftime('%s','now')
        WHERE listing_id = NEW.listing_id;
    END
"""


async def _table_is_current(conn) -> bool:
    table_sql = (
        await conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"),
            {"name": _TABLE},
        )
    ).scalar_one_or_none()
    if not table_sql:
        return False

    columns = {
        row._mapping["name"]: row._mapping
        for row in (await conn.execute(text("PRAGMA table_info(events_meta)"))).fetchall()
    }
    required_columns = {
        "listing_id",
        "start_at_utc",
        "start_date_local",
        "start_time_local",
        "timezone",
        "venue_text",
        "city_text",
        "lat",
        "lon",
        "price_text",
        "status",
        "created_at",
        "updated_at",
    }
    if not required_columns.issubset(columns):
        return False

    if int(columns["listing_id"].get("pk") or 0) != 1:
        return False
    for column in ("listing_id", "start_at_utc", "timezone", "status", "created_at", "updated_at"):
        if int(columns[column].get("notnull") or 0) != 1:
            return False

    default_status = str(columns["status"].get("dflt_value") or "").casefold()
    default_timezone = str(columns["timezone"].get("dflt_value") or "").casefold()
    created_default = str(columns["created_at"].get("dflt_value") or "").casefold()
    updated_default = str(columns["updated_at"].get("dflt_value") or "").casefold()
    normalized_sql = str(table_sql).casefold()
    if "pending" not in default_status:
        return False
    if "europe/belgrade" not in default_timezone:
        return False
    if "strftime" not in created_default or "strftime" not in updated_default:
        return False
    if "check" not in normalized_sql or "'cancelled'" in normalized_sql:
        return False
    if not all(f"'{status}'" in normalized_sql for status in ("pending", "published", "rejected")):
        return False

    foreign_keys = [
        row._mapping
        for row in (await conn.execute(text("PRAGMA foreign_key_list(events_meta)"))).fetchall()
    ]
    return any(
        str(fk.get("table") or "").casefold() == "listing"
        and str(fk.get("from") or "").casefold() == "listing_id"
        and str(fk.get("to") or "").casefold() == "id"
        and str(fk.get("on_update") or "").casefold() == "cascade"
        and str(fk.get("on_delete") or "").casefold() == "cascade"
        for fk in foreign_keys
    )


def _copy_expression(column: str, old_columns: set[str]) -> str:
    if column == "status":
        if "status" not in old_columns:
            return "'rejected'"
        return """
            CASE lower(trim(COALESCE(status, '')))
                WHEN 'pending' THEN 'pending'
                WHEN 'published' THEN 'published'
                WHEN 'rejected' THEN 'rejected'
                WHEN 'cancelled' THEN 'rejected'
                ELSE 'rejected'
            END
        """
    if column == "timezone":
        return (
            "COALESCE(NULLIF(trim(timezone), ''), 'Europe/Belgrade')"
            if column in old_columns
            else "'Europe/Belgrade'"
        )
    if column in {"created_at", "updated_at"}:
        return (
            f"COALESCE({column}, strftime('%s','now'))"
            if column in old_columns
            else "strftime('%s','now')"
        )
    if column in old_columns:
        return f'"{column}"'
    return "NULL"


async def _rebuild_legacy_table(conn) -> None:
    old_columns = {
        row._mapping["name"]
        for row in (await conn.execute(text("PRAGMA table_info(events_meta)"))).fetchall()
    }
    if not {"listing_id", "start_at_utc"}.issubset(old_columns):
        raise RuntimeError("events_meta has no listing_id/start_at_utc columns; automatic migration stopped")

    target_columns = (
        "listing_id",
        "start_at_utc",
        "start_date_local",
        "start_time_local",
        "timezone",
        "venue_text",
        "city_text",
        "lat",
        "lon",
        "price_text",
        "status",
        "created_at",
        "updated_at",
    )
    unexpected_columns = sorted(old_columns - set(target_columns))
    if unexpected_columns:
        raise RuntimeError(
            "events_meta has unknown columns; migration refused to avoid data loss: "
            + ", ".join(unexpected_columns)
        )

    objects = [
        (str(row._mapping["type"]), str(row._mapping["name"]), str(row._mapping["sql"]))
        for row in (
            await conn.execute(text("""
                SELECT type, name, sql
                FROM sqlite_master
                WHERE tbl_name=:table_name
                  AND type IN ('index','trigger')
                  AND sql IS NOT NULL
                ORDER BY type, name
            """), {"table_name": _TABLE})
        ).fetchall()
    ]

    await conn.execute(text(f'DROP TABLE IF EXISTS "{_NEW_TABLE}"'))
    await conn.execute(text(_create_table_sql(_NEW_TABLE)))

    names_sql = ", ".join(f'"{column}"' for column in target_columns)
    values_sql = ", ".join(_copy_expression(column, old_columns) for column in target_columns)
    await conn.execute(text(
        f'INSERT INTO "{_NEW_TABLE}" ({names_sql}) '
        f'SELECT {values_sql} FROM "{_TABLE}"'
    ))

    foreign_key_errors = (
        await conn.execute(text(f"PRAGMA foreign_key_check({_NEW_TABLE})"))
    ).fetchall()
    if foreign_key_errors:
        raise RuntimeError("events_meta migration found orphan listing references; transaction rolled back")

    await conn.execute(text(f'DROP TABLE "{_TABLE}"'))
    await conn.execute(text(f'ALTER TABLE "{_NEW_TABLE}" RENAME TO "{_TABLE}"'))

    # Recreate every explicit legacy index/trigger. If a custom object refers to
    # a removed/unknown column, its failure aborts and rolls back the rebuild.
    for _object_type, _object_name, object_sql in objects:
        await conn.execute(text(object_sql))


async def ensure_events_meta(*, db_engine: AsyncEngine | None = None) -> None:
    """Create or atomically migrate ``events_meta`` to moderation states.

    Legacy ``cancelled`` rows are retained as ``rejected``. The rebuild and
    integrity checks run in one SQLite transaction, so any failure restores the
    original table and its data.
    """
    target_engine = db_engine or engine
    if target_engine.dialect.name != "sqlite":
        raise RuntimeError("events_meta migration currently supports SQLite only")

    async with _MIGRATION_LOCK:
        async with target_engine.connect() as conn:
            # Python's sqlite3 legacy transaction mode does not reliably start
            # a transaction for DDL. Start one explicitly before any CREATE or
            # DROP so a failed rebuild cannot leave a scratch table behind.
            await conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                listing_exists = (
                    await conn.execute(text(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND lower(name)='listing'"
                    ))
                ).scalar_one_or_none()
                if not listing_exists:
                    raise RuntimeError(
                        "listing table is missing; run init_db before events_meta migration"
                    )

                exists = (
                    await conn.execute(
                        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"),
                        {"name": _TABLE},
                    )
                ).scalar_one_or_none()
                if not exists:
                    await conn.execute(text(_create_table_sql(_TABLE)))
                elif not await _table_is_current(conn):
                    await _rebuild_legacy_table(conn)

                await conn.execute(text(_INDEX_SQL))
                await conn.execute(text(_TRIGGER_SQL))

                foreign_key_errors = (
                    await conn.execute(text("PRAGMA foreign_key_check(events_meta)"))
                ).fetchall()
                if foreign_key_errors:
                    raise RuntimeError("events_meta foreign-key validation failed")
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()
