"""Add the missing release foreign keys to an existing SQLite database.

The migration is idempotent and refuses to rebuild tables while orphan rows
exist. Always run ``scripts/backup_db.py`` first.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db_path import resolve_sqlite_path


_EXPECTED_COLUMNS = {
    "release_meta": {
        "id", "listing_id", "artist_id", "release_type", "release_date",
        "genre", "recorded_at", "links", "video_file_id",
        "video_file_unique_id", "status", "created_at",
    },
    "release_track": {
        "id", "listing_id", "position", "title", "file_id",
        "file_unique_id", "duration", "file_name", "mime_type",
    },
}
_STANDARD_INDEXES = {
    "ix_release_meta_listing_id",
    "ix_release_meta_artist_id",
    "ix_release_track_listing_id",
}


def _has_fk(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    target: str,
    target_column: str,
    on_delete: str,
) -> bool:
    return any(
        row[2] == target
        and row[3] == column
        and row[4] == target_column
        and (row[6] or "").upper() == on_delete.upper()
        for row in conn.execute(f"PRAGMA foreign_key_list({table})")
    )


def migrate(path: Path) -> bool:
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {path}")
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        # Проверка нужна и для уже "актуальной" схемы: SQLite позволяет
        # временно отключить FK и оставить orphan-строки в такой базе.
        existing_fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if existing_fk_errors:
            raise RuntimeError(
                f"Release FK validation failed before migration: "
                f"{len(existing_fk_errors)} violation(s)"
            )

        schema_is_current = (
            _has_fk(conn, "release_meta", "listing_id", "listing", "id", "CASCADE")
            and _has_fk(conn, "release_meta", "artist_id", "artist", "id", "RESTRICT")
            and _has_fk(conn, "release_track", "listing_id", "listing", "id", "CASCADE")
        )
        if schema_is_current:
            return False

        for table, expected in _EXPECTED_COLUMNS.items():
            actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if actual != expected:
                missing = sorted(expected - actual)
                extra = sorted(actual - expected)
                raise RuntimeError(
                    f"Unexpected {table} schema; migration refused "
                    f"(missing={missing}, extra={extra})"
                )

        # Явные нестандартные индексы/триггеры восстанавливаем после rebuild.
        # Стандартные три индекса создаются каноническим SQL ниже.
        custom_objects = [
            (row[0], row[1], row[2])
            for row in conn.execute("""
                SELECT type, name, sql
                FROM sqlite_master
                WHERE tbl_name IN ('release_meta', 'release_track')
                  AND type IN ('index', 'trigger')
                  AND sql IS NOT NULL
                ORDER BY type, name
            """)
            if row[1] not in _STANDARD_INDEXES
        ]

        orphan_meta = conn.execute("""
            SELECT COUNT(*) FROM release_meta rm
            LEFT JOIN listing l ON l.id=rm.listing_id
            LEFT JOIN artist a ON a.id=rm.artist_id
            WHERE l.id IS NULL OR a.id IS NULL
        """).fetchone()[0]
        orphan_tracks = conn.execute("""
            SELECT COUNT(*) FROM release_track rt
            LEFT JOIN listing l ON l.id=rt.listing_id
            WHERE l.id IS NULL
        """).fetchone()[0]
        if orphan_meta or orphan_tracks:
            raise RuntimeError(
                f"Release FK migration refused: meta orphans={orphan_meta}, "
                f"track orphans={orphan_tracks}"
            )

        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE release_meta_new (
                id INTEGER NOT NULL PRIMARY KEY,
                listing_id INTEGER NOT NULL UNIQUE
                    REFERENCES listing(id) ON DELETE CASCADE,
                artist_id INTEGER NOT NULL
                    REFERENCES artist(id) ON DELETE RESTRICT,
                release_type VARCHAR(16) NOT NULL,
                release_date VARCHAR(32),
                genre VARCHAR(64),
                recorded_at VARCHAR(128),
                links TEXT,
                video_file_id TEXT,
                video_file_unique_id VARCHAR(64),
                status VARCHAR(16) NOT NULL,
                created_at DATETIME NOT NULL
            );
            INSERT INTO release_meta_new
            SELECT id, listing_id, artist_id, release_type, release_date, genre,
                   recorded_at, links, video_file_id, video_file_unique_id,
                   status, created_at
            FROM release_meta;

            CREATE TABLE release_track_new (
                id INTEGER NOT NULL PRIMARY KEY,
                listing_id INTEGER NOT NULL
                    REFERENCES listing(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                title VARCHAR(255),
                file_id TEXT NOT NULL,
                file_unique_id VARCHAR(64),
                duration INTEGER,
                file_name VARCHAR(255),
                mime_type VARCHAR(64)
            );
            INSERT INTO release_track_new
            SELECT id, listing_id, position, title, file_id, file_unique_id,
                   duration, file_name, mime_type
            FROM release_track;

            DROP TABLE release_track;
            DROP TABLE release_meta;
            ALTER TABLE release_meta_new RENAME TO release_meta;
            ALTER TABLE release_track_new RENAME TO release_track;
            CREATE UNIQUE INDEX ix_release_meta_listing_id
                ON release_meta(listing_id);
            CREATE INDEX ix_release_meta_artist_id ON release_meta(artist_id);
            CREATE INDEX ix_release_track_listing_id ON release_track(listing_id);
        """)
        for _object_type, _object_name, object_sql in custom_objects:
            conn.execute(object_sql)
        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_errors:
            raise RuntimeError(f"foreign_key_check failed after migration: {len(fk_errors)} rows")
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    path = resolve_sqlite_path(ROOT)
    changed = migrate(path)
    print("Release foreign keys migrated." if changed else "Release foreign keys already present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
