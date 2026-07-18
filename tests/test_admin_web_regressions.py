import base64
import json
import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import PlainTextResponse

import category_admin
from app.db_path import resolve_sqlite_path
from app.routers import admin_analytics
from app.web.app import youtube_player
from app.web.proxy_media import _collect_allowed_file_ids


class AdminSecurityTests(unittest.TestCase):
    def test_malformed_artist_urls_are_rejected(self):
        for value in (
            "httpNOPE",
            "javascript:alert(1)",
            'https://example.com/"onmouseover="alert(1)',
            "https://user:pass@example.com",
        ):
            with self.assertRaises(HTTPException, msg=value):
                category_admin._validated_http_url(value)
        self.assertEqual(
            category_admin._validated_http_url("https://example.com/music"),
            "https://example.com/music",
        )

    def test_log_formatter_redacts_bot_token(self):
        old_token = category_admin.BOT_TOKEN
        try:
            category_admin.BOT_TOKEN = "test-secret-token"
            formatter = category_admin._SecretRedactingFormatter("%(message)s")
            record = logging.LogRecord(
                "test", logging.ERROR, __file__, 1,
                "request https://api.telegram.org/bottest-secret-token/getFile",
                (), None,
            )
            rendered = formatter.format(record)
            self.assertNotIn("test-secret-token", rendered)
            self.assertIn("[REDACTED_BOT_TOKEN]", rendered)
        finally:
            category_admin.BOT_TOKEN = old_token

    def test_dynamic_names_are_json_encoded_inside_inline_handlers(self):
        self.assertIn("function jsArg(value)", category_admin.HTML)
        self.assertNotRegex(
            category_admin.HTML,
            r'on[a-z]+="[^"\n]*\$\{esc\(',
        )

    def test_category_with_archived_listing_cannot_be_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            conn = sqlite3.connect(db_path)
            conn.executescript("""
                CREATE TABLE category (id INTEGER PRIMARY KEY, parent_id INTEGER);
                CREATE TABLE listing (
                    id INTEGER PRIMARY KEY,
                    category_id INTEGER,
                    extra_category_id1 INTEGER,
                    extra_category_id2 INTEGER,
                    status TEXT,
                    is_sold INTEGER
                );
                INSERT INTO category(id, parent_id) VALUES (10, NULL);
                INSERT INTO listing(id, category_id, status, is_sold)
                VALUES (1, 10, 'archived', 1);
            """)
            conn.close()
            with patch.object(category_admin, "DB_PATH", db_path):
                with self.assertRaises(HTTPException) as ctx:
                    category_admin.delete_category(10)
            self.assertEqual(ctx.exception.status_code, 400)
            check = sqlite3.connect(db_path)
            self.assertEqual(check.execute("SELECT COUNT(*) FROM category").fetchone()[0], 1)
            check.close()

    def test_db_path_uses_database_url_relative_to_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(
                "os.environ",
                {"DATABASE_URL": "sqlite+aiosqlite:///./data/bot.db"},
                clear=False,
            ):
                self.assertEqual(
                    resolve_sqlite_path(root),
                    (root / "data" / "bot.db").resolve(),
                )


class AdminMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _request(
        ip: str,
        authorization: str | None = None,
        *,
        host: str | None = None,
        method: str = "GET",
        origin: str | None = None,
    ) -> Request:
        headers = []
        if authorization:
            headers.append((b"authorization", authorization.encode("ascii")))
        if host:
            headers.append((b"host", host.encode("ascii")))
        if origin:
            headers.append((b"origin", origin.encode("ascii")))
        return Request({
            "type": "http",
            "method": method,
            "path": "/",
            "headers": headers,
            "client": (ip, 12345),
            "server": ("localhost", 8001),
            "scheme": "http",
            "query_string": b"",
        })

    async def test_remote_admin_requires_credentials(self):
        call_next = AsyncMock(return_value=PlainTextResponse("ok"))
        with (
            patch.object(category_admin, "_ADMIN_USER", ""),
            patch.object(category_admin, "_ADMIN_PASSWORD", ""),
        ):
            response = await category_admin._ip_allowlist(
                self._request("100.64.1.2"), call_next
            )
        self.assertEqual(response.status_code, 503)
        call_next.assert_not_awaited()

    async def test_configured_basic_auth_also_protects_loopback(self):
        call_next = AsyncMock(return_value=PlainTextResponse("ok"))
        token = base64.b64encode(b"admin:correct-horse").decode("ascii")
        with (
            patch.object(category_admin, "_ADMIN_USER", "admin"),
            patch.object(category_admin, "_ADMIN_PASSWORD", "correct-horse"),
        ):
            denied = await category_admin._ip_allowlist(
                self._request("127.0.0.1"), call_next
            )
            allowed = await category_admin._ip_allowlist(
                self._request("127.0.0.1", f"Basic {token}"), call_next
            )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(allowed.status_code, 200)

    async def test_dns_rebinding_host_and_cross_site_write_are_rejected(self):
        call_next = AsyncMock(return_value=PlainTextResponse("ok"))
        with (
            patch.object(category_admin, "_ADMIN_USER", ""),
            patch.object(category_admin, "_ADMIN_PASSWORD", ""),
        ):
            rebound = await category_admin._ip_allowlist(
                self._request("127.0.0.1", host="attacker.example:8001"),
                call_next,
            )
            csrf = await category_admin._ip_allowlist(
                self._request(
                    "127.0.0.1",
                    host="127.0.0.1:8001",
                    method="POST",
                    origin="https://attacker.example",
                ),
                call_next,
            )
        self.assertEqual(rebound.status_code, 403)
        self.assertEqual(csrf.status_code, 403)


class WebRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_youtube_player_is_repo_served_and_has_csp(self):
        response = await youtube_player()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"youtube-nocookie.com", response.body)
        self.assertIn("content-security-policy", response.headers)

    def test_media_allowlist_contains_real_legacy_and_release_fields(self):
        listing = SimpleNamespace(
            photo_file_id="p1,p2",
            flex=json.dumps({
                "video": "legacy-video",
                "video_file_id": "v2",
                "videos": ["v3"],
                "photos": ["p3"],
            }),
        )
        allowed = _collect_allowed_file_ids(listing, "release-video")
        self.assertTrue(
            {"p1", "p2", "p3", "legacy-video", "v2", "v3", "release-video"}
            .issubset(allowed)
        )


class AnalyticsRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_city_analytics_renders_release_column_without_join_overcount_crash(self):
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=519335258),
            message=SimpleNamespace(chat=SimpleNamespace(id=1)),
        )
        with patch.object(
            admin_analytics,
            "_send_admin_analytics_message",
            AsyncMock(),
        ) as sender:
            await admin_analytics.admin_analytics_cities(callback)
        sender.assert_awaited_once()
        text = sender.await_args.args[1]
        self.assertIn("Аналитика по городам", text)


if __name__ == "__main__":
    unittest.main()
