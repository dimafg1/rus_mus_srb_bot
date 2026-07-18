import inspect
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.events_meta import ensure_events_meta
from app.routers import events_add, events_admin, events_view


_LISTING_SQL = "CREATE TABLE listing (id INTEGER PRIMARY KEY)"
_LEGACY_SQL = """
    CREATE TABLE events_meta (
        listing_id INTEGER PRIMARY KEY REFERENCES listing(id)
            ON DELETE CASCADE ON UPDATE CASCADE,
        start_at_utc INTEGER NOT NULL,
        start_date_local TEXT,
        start_time_local TEXT,
        timezone TEXT NOT NULL DEFAULT 'Europe/Belgrade',
        venue_text TEXT,
        city_text TEXT,
        lat REAL,
        lon REAL,
        price_text TEXT,
        status TEXT NOT NULL DEFAULT 'published',
        created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
        updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
        CHECK (status IN ('published','cancelled'))
    )
"""


class EventsMetaMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(prefix="events-meta-", suffix=".db")
        os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")

    async def asyncTearDown(self):
        await self.engine.dispose()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    async def _create_legacy(self):
        async with self.engine.begin() as conn:
            await conn.execute(text(_LISTING_SQL))
            await conn.execute(text(_LEGACY_SQL))
            await conn.execute(text("INSERT INTO listing(id) VALUES (1), (2)"))
            await conn.execute(text("""
                INSERT INTO events_meta (
                    listing_id, start_at_utc, start_date_local,
                    start_time_local, status
                ) VALUES
                    (1, 100, '2026-01-01', '12:00', 'published'),
                    (2, 200, '2026-01-02', '13:00', 'cancelled')
            """))
            await conn.execute(text(
                "CREATE INDEX idx_events_meta_start ON events_meta(start_at_utc)"
            ))

    async def test_legacy_migration_preserves_rows_and_is_idempotent(self):
        await self._create_legacy()

        await ensure_events_meta(db_engine=self.engine)
        await ensure_events_meta(db_engine=self.engine)

        async with self.engine.connect() as conn:
            rows = (await conn.execute(text("""
                SELECT listing_id, start_at_utc, start_date_local,
                       start_time_local, status
                FROM events_meta ORDER BY listing_id
            """))).all()
            self.assertEqual(rows, [
                (1, 100, "2026-01-01", "12:00", "published"),
                (2, 200, "2026-01-02", "13:00", "rejected"),
            ])
            schema = (await conn.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='events_meta'"
            ))).scalar_one()
            self.assertIn("'pending'", schema)
            self.assertIn("'rejected'", schema)
            self.assertNotIn("'cancelled'", schema)
            self.assertFalse((await conn.execute(text(
                "PRAGMA foreign_key_check(events_meta)"
            ))).all())

    async def test_clean_database_bootstrap_creates_queryable_table(self):
        async with self.engine.begin() as conn:
            await conn.execute(text(_LISTING_SQL))

        await ensure_events_meta(db_engine=self.engine)

        async with self.engine.begin() as conn:
            await conn.execute(text("INSERT INTO listing(id) VALUES (1)"))
            await conn.execute(text("""
                INSERT INTO events_meta(listing_id, start_at_utc, status)
                VALUES (1, 123, 'pending')
            """))
            status = (await conn.execute(text(
                "SELECT status FROM events_meta WHERE listing_id=1"
            ))).scalar_one()
            self.assertEqual(status, "pending")

    async def test_orphan_failure_rolls_back_without_scratch_table(self):
        async with self.engine.begin() as conn:
            await conn.execute(text(_LISTING_SQL))
            await conn.execute(text(_LEGACY_SQL))
            await conn.execute(text("""
                INSERT INTO events_meta(listing_id, start_at_utc, status)
                VALUES (999, 100, 'published')
            """))

        with self.assertRaises(RuntimeError):
            await ensure_events_meta(db_engine=self.engine)

        async with self.engine.connect() as conn:
            objects = (await conn.execute(text("""
                SELECT name FROM sqlite_master
                WHERE type='table' AND name LIKE 'events_meta%'
                ORDER BY name
            """))).scalars().all()
            self.assertEqual(objects, ["events_meta"])
            self.assertEqual((await conn.execute(text(
                "SELECT status FROM events_meta WHERE listing_id=999"
            ))).scalar_one(), "published")

    async def test_malformed_apparent_current_schema_is_rebuilt(self):
        async with self.engine.begin() as conn:
            await conn.execute(text(_LISTING_SQL))
            await conn.execute(text("""
                CREATE TABLE events_meta (
                    listing_id INTEGER,
                    start_at_utc INTEGER NOT NULL,
                    start_date_local TEXT,
                    start_time_local TEXT,
                    timezone TEXT NOT NULL DEFAULT 'Europe/Belgrade',
                    venue_text TEXT, city_text TEXT, lat REAL, lon REAL,
                    price_text TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at INTEGER,
                    updated_at INTEGER,
                    CHECK (status IN ('pending','published','rejected')),
                    FOREIGN KEY(listing_id) REFERENCES listing(id)
                        ON DELETE CASCADE ON UPDATE CASCADE
                )
            """))
            await conn.execute(text("INSERT INTO listing(id) VALUES (1)"))
            await conn.execute(text("""
                INSERT INTO events_meta(listing_id, start_at_utc, status)
                VALUES (1, 100, 'pending')
            """))

        await ensure_events_meta(db_engine=self.engine)

        async with self.engine.connect() as conn:
            columns = {
                row[1]: row
                for row in (await conn.execute(text("PRAGMA table_info(events_meta)"))).all()
            }
            self.assertEqual(columns["listing_id"][3], 1)  # NOT NULL
            self.assertEqual(columns["listing_id"][5], 1)  # PRIMARY KEY
            self.assertEqual(columns["created_at"][3], 1)
            self.assertIn("strftime", columns["created_at"][4])
            self.assertEqual(columns["updated_at"][3], 1)

    async def test_unknown_legacy_column_is_refused_without_scratch_or_data_loss(self):
        async with self.engine.begin() as conn:
            await conn.execute(text(_LISTING_SQL))
            await conn.execute(text(_LEGACY_SQL))
            await conn.execute(text("ALTER TABLE events_meta ADD COLUMN custom_note TEXT"))
            await conn.execute(text("INSERT INTO listing(id) VALUES (1)"))
            await conn.execute(text("""
                INSERT INTO events_meta(
                    listing_id, start_at_utc, status, custom_note
                ) VALUES (1, 100, 'published', 'keep-me')
            """))

        with self.assertRaisesRegex(RuntimeError, "unknown columns"):
            await ensure_events_meta(db_engine=self.engine)

        async with self.engine.connect() as conn:
            self.assertEqual(
                (await conn.execute(text(
                    "SELECT custom_note FROM events_meta WHERE listing_id=1"
                ))).scalar_one(),
                "keep-me",
            )
            self.assertIsNone((await conn.execute(text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='events_meta__new'"
            ))).scalar_one_or_none())

    async def test_bare_database_is_refused_without_dangling_fk_table(self):
        with self.assertRaisesRegex(RuntimeError, "listing table is missing"):
            await ensure_events_meta(db_engine=self.engine)

        async with self.engine.connect() as conn:
            self.assertIsNone((await conn.execute(text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='events_meta'"
            ))).scalar_one_or_none())


class EventsRouterRegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_event_card_uses_only_one_cover_from_legacy_csv(self):
        self.assertEqual(events_view._first_photo_id("cover-1,cover-2"), "cover-1")
        self.assertIsNone(events_view._first_photo_id(""))

    async def test_seed_child_event_does_not_replace_root_category(self):
        fd, path = tempfile.mkstemp(prefix="events-category-", suffix=".db")
        os.close(fd)
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            async with engine.begin() as conn:
                await conn.execute(text("""
                    CREATE TABLE category (
                        id INTEGER PRIMARY KEY,
                        slug TEXT NOT NULL,
                        name TEXT NOT NULL,
                        parent_id INTEGER,
                        fields TEXT
                    )
                """))
                await conn.execute(text("""
                    INSERT INTO category(id, slug, name, parent_id)
                    VALUES (1, 'org', 'Организация', NULL),
                           (2, 'event', 'Организация мероприятий', 1)
                """))
                root_id = await events_add._event_root_category_id(conn)
                rows = (await conn.execute(text("""
                    SELECT id, parent_id FROM category
                    WHERE lower(slug)='event' ORDER BY id
                """))).all()
            self.assertNotEqual(root_id, 2)
            self.assertEqual(rows, [(2, 1), (root_id, None)])
        finally:
            await engine.dispose()
            os.unlink(path)

    async def test_malformed_admin_callbacks_are_rejected(self):
        callback = SimpleNamespace(
            data="admin:event:pub:broken",
            from_user=SimpleNamespace(id=1),
            answer=AsyncMock(),
        )
        with (
            patch.object(events_admin, "is_admin", return_value=True),
            patch.object(events_admin, "_set_status", AsyncMock()) as set_status,
        ):
            await events_admin.admin_event_publish(callback)
        set_status.assert_not_awaited()
        callback.answer.assert_awaited_once_with(
            "Некорректная или устаревшая кнопка.", show_alert=True
        )

    def test_startup_orders_events_bootstrap_after_model_tables(self):
        from app import main

        source = inspect.getsource(main.main)
        self.assertLess(source.index("await init_db()"), source.index("await ensure_events_meta()"))

    def test_deduplicated_module_keeps_required_helpers(self):
        self.assertTrue(hasattr(events_add, "AfishaEditStates"))
        self.assertTrue(callable(events_add._other_city_id))
        self.assertTrue(callable(events_add._event_root_category_id))
        self.assertTrue(callable(events_add._mark_event_pending))


if __name__ == "__main__":
    unittest.main()
