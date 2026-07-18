import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

import category_admin
from app.web import proxy_media
from app.web.contact_redirect import _SECTION_BY_LISTING_TYPE
from app.web.proxy_media import _validated_range_header
from app.db_path import absolutize_sqlite_url, config_value
from app.routers.admin_panel import (
    _normalized_category_name as normalized_telegram_category_name,
    _normalized_category_slug as normalized_telegram_category_slug,
)
from scripts.migrate_release_fks import migrate


class RangeRegressionTests(unittest.TestCase):
    def test_public_proxy_accepts_suffix_and_rejects_invalid_ranges(self):
        self.assertEqual(_validated_range_header("bytes=-500"), "bytes=-500")
        self.assertEqual(_validated_range_header("BYTES=10-20"), "bytes=10-20")
        for value in (
            "bytes=-",
            "bytes=20-10",
            "bytes=0-1,3-4",
            "items=0-1",
            "bytes=" + "9" * 100 + "-",
        ):
            with self.assertRaises(HTTPException, msg=value) as ctx:
                _validated_range_header(value)
            self.assertEqual(ctx.exception.status_code, 416)
            self.assertEqual(ctx.exception.headers["Cache-Control"], "private, no-store")

    def test_admin_proxy_slices_suffix_range_from_end(self):
        self.assertEqual(category_admin._parse_byte_range("bytes=-3", 10), (7, 9))
        self.assertEqual(category_admin._parse_byte_range("bytes=4-", 10), (4, 9))
        self.assertEqual(category_admin._parse_byte_range("bytes=4-99", 10), (4, 9))
        with self.assertRaises(HTTPException) as ctx:
            category_admin._parse_byte_range("bytes=10-", 10)
        self.assertEqual(ctx.exception.status_code, 416)
        self.assertEqual(ctx.exception.headers["Content-Range"], "bytes */10")


class PublicProxyLifecycleTests(unittest.IsolatedAsyncioTestCase):
    class _Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class _Db:
        def __init__(self, listing):
            self.listing = listing

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _query):
            return PublicProxyLifecycleTests._Result(self.listing)

    class _Content:
        async def iter_chunked(self, _size):
            yield b"first"
            yield b"second"

    class _Upstream:
        def __init__(self, status, headers=None):
            self.status = status
            self.headers = headers or {}
            self.content = PublicProxyLifecycleTests._Content()
            self.released = False

        def release(self):
            self.released = True

    class _ClientSession:
        def __init__(self, upstream):
            self.upstream = upstream
            self.closed = False

        async def get(self, _url, headers=None):
            self.request_headers = headers or {}
            return self.upstream

        async def close(self):
            self.closed = True

    @staticmethod
    def _request(range_header: str | None = None) -> Request:
        headers = []
        if range_header:
            headers.append((b"range", range_header.encode("ascii")))
        return Request({
            "type": "http",
            "method": "GET",
            "path": "/rus_mus_srb_bot/media/telegram/video/file-id",
            "headers": headers,
            "query_string": b"",
            "client": ("127.0.0.1", 1234),
            "server": ("localhost", 8080),
            "scheme": "http",
        })

    async def _call(self, upstream, range_header="bytes=0-"):
        listing = SimpleNamespace(
            id=1,
            owner_id=2,
            type="market",
            photo_file_id="file-id",
            flex=None,
        )
        session = self._ClientSession(upstream)
        self.last_session = session
        auth = SimpleNamespace(listing_id=1, owner_id=2, purpose="media")
        with (
            patch.object(proxy_media, "SessionLocal", return_value=self._Db(listing)),
            patch.object(proxy_media, "verify_token", return_value=auth),
            patch.object(proxy_media, "tg_file_url", return_value="https://telegram/file"),
            patch.object(proxy_media.aiohttp, "ClientSession", return_value=session),
        ):
            result = await proxy_media.proxy_telegram_media(
                "video",
                "file-id",
                self._request(range_header),
                "signed-token",
            )
        return result, session

    async def test_upstream_416_is_returned_as_416_and_resources_are_closed(self):
        upstream = self._Upstream(416, {"Content-Range": "bytes */10"})
        with self.assertRaises(HTTPException) as ctx:
            await self._call(upstream, "bytes=50-")
        self.assertEqual(ctx.exception.status_code, 416)
        self.assertEqual(ctx.exception.headers["Content-Range"], "bytes */10")
        self.assertEqual(ctx.exception.headers["Cache-Control"], "private, no-store")
        self.assertTrue(upstream.released)
        self.assertTrue(self.last_session.closed)

    async def test_stream_resources_live_until_body_is_consumed(self):
        upstream = self._Upstream(
            206,
            {
                "Content-Type": "video/mp4",
                "Content-Range": "bytes 0-10/11",
                "Content-Length": "11",
            },
        )
        response, session = await self._call(upstream)
        self.assertFalse(session.closed)
        self.assertFalse(upstream.released)
        chunks = [chunk async for chunk in response.body_iterator]
        self.assertEqual(chunks, [b"first", b"second"])
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertTrue(session.closed)
        self.assertTrue(upstream.released)

    async def test_get_file_rejects_valid_json_with_wrong_shape(self):
        class Response:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def json(self, **_kwargs):
                return []

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def get(self, *_args, **_kwargs):
                return Response()

        with (
            patch.object(proxy_media, "_get_bot_token", return_value="token"),
            patch.object(proxy_media.aiohttp, "ClientSession", return_value=Session()),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await proxy_media.tg_file_url("file-id")
        self.assertEqual(ctx.exception.status_code, 502)


class CategoryIntegrityTests(unittest.TestCase):
    def test_photo_limits_match_telegram_card_capabilities(self):
        self.assertEqual(category_admin._listing_photo_limit("market"), 3)
        self.assertEqual(category_admin._listing_photo_limit("service"), 3)
        self.assertEqual(category_admin._listing_photo_limit("events"), 1)
        self.assertEqual(category_admin._listing_photo_limit("release"), 1)
        self.assertEqual(category_admin._listing_photo_limit("vacancy"), 0)

    @staticmethod
    def _create_category_db(path: Path) -> None:
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE category (
                id INTEGER PRIMARY KEY,
                slug TEXT NOT NULL,
                name TEXT NOT NULL,
                parent_id INTEGER,
                order_num INTEGER DEFAULT 0
            );
            INSERT INTO category(id, slug, name, parent_id) VALUES
                (30, 'market', 'Market', NULL),
                (100, 'event', 'Events', NULL),
                (10, 'parent', 'Parent', 30),
                (11, 'child', 'Child', 10);
        """)
        conn.close()

    def test_category_cannot_be_moved_below_its_descendant(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "categories.db"
            self._create_category_db(db_path)

            with patch.object(category_admin, "DB_PATH", db_path):
                with self.assertRaises(HTTPException) as ctx:
                    category_admin.update_category(
                        10,
                        category_admin.CatUpdate(parent_id=11),
                    )
            self.assertEqual(ctx.exception.status_code, 400)

            check = sqlite3.connect(db_path)
            try:
                self.assertEqual(
                    check.execute("SELECT parent_id FROM category WHERE id=10").fetchone()[0],
                    30,
                )
            finally:
                check.close()

    def test_any_root_is_immutable_and_slug_input_is_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "categories.db"
            self._create_category_db(db_path)
            with patch.object(category_admin, "DB_PATH", db_path):
                with self.assertRaises(HTTPException):
                    category_admin.update_category(
                        100,
                        category_admin.CatUpdate(parent_id=30),
                    )
                category_admin.update_category(
                    11,
                    category_admin.CatUpdate(name="  New child  ", slug="  CHILD_NEW  "),
                )
                with self.assertRaises(HTTPException):
                    category_admin.create_category(
                        category_admin.CatCreate(
                            name="Duplicate",
                            slug=" CHILD_NEW ",
                            parent_id=30,
                        )
                    )
                for name, slug in (("   ", "valid"), ("Name", "   "), ("Name", "bad slug")):
                    with self.assertRaises(HTTPException):
                        category_admin.create_category(
                            category_admin.CatCreate(
                                name=name,
                                slug=slug,
                                parent_id=30,
                            )
                        )

            check = sqlite3.connect(db_path)
            try:
                self.assertEqual(
                    check.execute("SELECT name, slug FROM category WHERE id=11").fetchone(),
                    ("New child", "child_new"),
                )
                self.assertIsNone(
                    check.execute("SELECT parent_id FROM category WHERE id=100").fetchone()[0]
                )
            finally:
                check.close()

    def test_telegram_admin_uses_the_same_name_and_slug_rules(self):
        self.assertEqual(normalized_telegram_category_name("  Rock  "), "Rock")
        self.assertEqual(normalized_telegram_category_slug("  ROCK_ALT  "), "rock_alt")
        for value in ("", "   ", "bad slug", "x" * 101):
            with self.assertRaises(ValueError):
                normalized_telegram_category_slug(value)
        for value in ("", "   ", "bad\nname", "x" * 201):
            with self.assertRaises(ValueError):
                normalized_telegram_category_name(value)


class DatabasePathCwdTests(unittest.TestCase):
    def test_local_dotenv_fallback_keeps_process_env_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("LOG_DIR=from-dotenv\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(config_value(root, "LOG_DIR", "default"), "from-dotenv")
            with patch.dict(os.environ, {"LOG_DIR": "from-process"}, clear=True):
                self.assertEqual(config_value(root, "LOG_DIR", "default"), "from-process")

    def test_relative_sqlite_url_is_absolutized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                absolutize_sqlite_url(
                    "sqlite+aiosqlite:///./data/bot.db?timeout=30",
                    root,
                ),
                f"sqlite+aiosqlite:///{(root / 'data/bot.db').resolve().as_posix()}?timeout=30",
            )

    def test_database_import_from_another_cwd_still_targets_repo_db(self):
        root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env.pop("DATABASE_URL", None)
        env.pop("DATABASE_PATH", None)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(root), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from app.database import engine; print(engine.url.database)",
                ],
                cwd=tmp,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
        self.assertEqual(Path(result.stdout.strip()), (root / "dev.db").resolve())

    def test_bot_text_reader_honors_absolute_database_path_from_any_cwd(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "texts.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "CREATE TABLE BotText "
                    "(code TEXT PRIMARY KEY, text_ru TEXT, text_en TEXT)"
                )
                conn.execute(
                    "INSERT INTO BotText(code, text_ru, text_en) "
                    "VALUES ('probe', 'верный файл', 'right file')"
                )
                conn.commit()
            finally:
                conn.close()

            env = os.environ.copy()
            env["DATABASE_PATH"] = str(db_path)
            env["PYTHONPATH"] = os.pathsep.join(
                [str(root), env.get("PYTHONPATH", "")]
            ).rstrip(os.pathsep)
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import asyncio; from app.routers.utils import get_text; "
                    "print(asyncio.run(get_text('probe')))",
                ],
                cwd=Path(tmp) / "..",
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
        self.assertEqual(result.stdout.strip(), "верный файл")

class ReleaseForeignKeyMigrationTests(unittest.TestCase):
    @staticmethod
    def _create_legacy_schema(path: Path) -> None:
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE listing (id INTEGER PRIMARY KEY);
            CREATE TABLE artist (id INTEGER PRIMARY KEY);
            CREATE TABLE release_meta (
                id INTEGER NOT NULL PRIMARY KEY,
                listing_id INTEGER NOT NULL UNIQUE REFERENCES listing(id),
                artist_id INTEGER NOT NULL REFERENCES artist(id) ON DELETE CASCADE,
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
            CREATE TABLE release_track (
                id INTEGER NOT NULL PRIMARY KEY,
                listing_id INTEGER NOT NULL REFERENCES listing(id),
                position INTEGER NOT NULL,
                title VARCHAR(255),
                file_id TEXT NOT NULL,
                file_unique_id VARCHAR(64),
                duration INTEGER,
                file_name VARCHAR(255),
                mime_type VARCHAR(64)
            );
            INSERT INTO listing VALUES (1);
            INSERT INTO artist VALUES (2);
            INSERT INTO release_meta
                (id, listing_id, artist_id, release_type, status, created_at)
                VALUES (3, 1, 2, 'single', 'published', '2026-01-01');
            INSERT INTO release_track
                (id, listing_id, position, title, file_id)
                VALUES (4, 1, 1, 'Track', 'file-id');
        """)
        conn.close()

    def test_wrong_delete_actions_are_rebuilt_and_data_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            self._create_legacy_schema(path)

            self.assertTrue(migrate(path))
            self.assertFalse(migrate(path))

            conn = sqlite3.connect(path)
            try:
                meta_fks = {
                    row[3]: (row[2], row[4], row[6])
                    for row in conn.execute("PRAGMA foreign_key_list(release_meta)")
                }
                track_fks = {
                    row[3]: (row[2], row[4], row[6])
                    for row in conn.execute("PRAGMA foreign_key_list(release_track)")
                }
                self.assertEqual(meta_fks["listing_id"], ("listing", "id", "CASCADE"))
                self.assertEqual(meta_fks["artist_id"], ("artist", "id", "RESTRICT"))
                self.assertEqual(track_fks["listing_id"], ("listing", "id", "CASCADE"))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM release_meta").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM release_track").fetchone()[0], 1)
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                conn.close()

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.db"
            with self.assertRaises(FileNotFoundError):
                migrate(path)
            self.assertFalse(path.exists())

    def test_current_fk_schema_with_orphan_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orphan.db"
            self._create_legacy_schema(path)
            self.assertTrue(migrate(path))

            conn = sqlite3.connect(path)
            try:
                conn.execute("PRAGMA foreign_keys=OFF")
                conn.execute(
                    "INSERT INTO release_track "
                    "(id, listing_id, position, title, file_id) "
                    "VALUES (5, 999, 2, 'Orphan', 'orphan-file')"
                )
                conn.commit()
            finally:
                conn.close()

            with self.assertRaisesRegex(RuntimeError, "validation failed"):
                migrate(path)

    def test_unexpected_columns_are_refused_without_data_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "extra-column.db"
            self._create_legacy_schema(path)
            conn = sqlite3.connect(path)
            try:
                conn.execute("ALTER TABLE release_meta ADD COLUMN custom_note TEXT")
                conn.execute("UPDATE release_meta SET custom_note='keep-me' WHERE id=3")
                conn.commit()
            finally:
                conn.close()

            with self.assertRaisesRegex(RuntimeError, "Unexpected release_meta schema"):
                migrate(path)

            check = sqlite3.connect(path)
            try:
                self.assertEqual(
                    check.execute(
                        "SELECT custom_note FROM release_meta WHERE id=3"
                    ).fetchone()[0],
                    "keep-me",
                )
            finally:
                check.close()

    def test_custom_release_trigger_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trigger.db"
            self._create_legacy_schema(path)
            conn = sqlite3.connect(path)
            try:
                conn.executescript("""
                    CREATE TABLE release_audit (track_id INTEGER);
                    CREATE TRIGGER release_track_audit
                    AFTER INSERT ON release_track
                    BEGIN
                        INSERT INTO release_audit(track_id) VALUES (NEW.id);
                    END;
                """)
            finally:
                conn.close()

            self.assertTrue(migrate(path))
            check = sqlite3.connect(path)
            try:
                check.execute(
                    "INSERT INTO release_track "
                    "(id, listing_id, position, title, file_id) "
                    "VALUES (5, 1, 2, 'Second', 'second-file')"
                )
                self.assertEqual(
                    check.execute("SELECT track_id FROM release_audit").fetchall(),
                    [(5,)],
                )
            finally:
                check.close()


class DeployPrivacyTests(unittest.TestCase):
    def test_signed_query_tokens_are_not_written_to_access_logs(self):
        root = Path(__file__).resolve().parents[1]
        web_unit = (root / "deploy/systemd/rus-mus-srb-web.service").read_text()
        nginx = (root / "deploy/nginx/rus-mus-srb.conf").read_text()
        self.assertIn("--no-access-log", web_unit)
        self.assertIn("access_log off;", nginx)
        self.assertIn("error_log /dev/null crit;", nginx)

    def test_python_service_output_is_unbuffered(self):
        root = Path(__file__).resolve().parents[1]
        for unit in (root / "deploy/systemd").glob("*.service"):
            self.assertIn(
                "Environment=PYTHONUNBUFFERED=1",
                unit.read_text(),
                msg=unit.name,
            )

    def test_release_contact_analytics_uses_existing_section_name(self):
        self.assertEqual(_SECTION_BY_LISTING_TYPE["release"], "releases")


if __name__ == "__main__":
    unittest.main()
