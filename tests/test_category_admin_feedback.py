"""Веб-админка: вкладка «Обратная связь» (category_admin.py).

Роут-функции вызываются напрямую (не через TestClient/ASGI) — middleware
IP-фильтра/Basic Auth в файле рассчитан на реальный HTTP-запрос, а не на
TestClient; прямой вызов чист и не требует сетевых допущений, как и
существующий test_category_admin_db.py.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import category_admin


def _make_db(tmp_dir: str) -> Path:
    db_path = Path(tmp_dir) / "admin.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            message TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            is_read INTEGER NOT NULL DEFAULT 0,
            needs_reply INTEGER NOT NULL DEFAULT 0,
            answered_at DATETIME,
            answer_text TEXT
        )
    """)
    conn.execute("CREATE TABLE menu (code TEXT, text TEXT, callback_data TEXT)")
    conn.execute("CREATE TABLE botmessage (id INTEGER PRIMARY KEY, chat_id INTEGER, "
                 "message_id INTEGER, created_at TEXT)")
    conn.commit()
    conn.close()
    return db_path


def _insert_feedback(db_path, **kw):
    conn = sqlite3.connect(db_path)
    defaults = dict(user_id=17, username="tester", message="вопрос",
                     is_read=0, needs_reply=0, answered_at=None, answer_text=None)
    defaults.update(kw)
    cur = conn.execute(
        "INSERT INTO feedback (user_id, username, message, is_read, needs_reply, "
        "answered_at, answer_text) VALUES (?,?,?,?,?,?,?)",
        (defaults["user_id"], defaults["username"], defaults["message"],
         defaults["is_read"], defaults["needs_reply"], defaults["answered_at"],
         defaults["answer_text"]),
    )
    conn.commit()
    fid = cur.lastrowid
    conn.close()
    return fid


class FeedbackListTests(unittest.TestCase):
    def test_list_all_and_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(tmp)
            _insert_feedback(db_path, message="первое")
            _insert_feedback(db_path, message="второе", needs_reply=1)
            with patch.object(category_admin, "DB_PATH", db_path):
                data = category_admin.feedback_list(unanswered=0, offset=0, limit=20)
        self.assertEqual(data["total"], 2)
        self.assertEqual(len(data["rows"]), 2)
        self.assertEqual(data["rows"][0]["message"], "второе")  # новые сверху

    def test_unanswered_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(tmp)
            _insert_feedback(db_path, message="без запроса")
            _insert_feedback(db_path, message="ждёт ответа", needs_reply=1)
            _insert_feedback(db_path, message="уже отвечено", needs_reply=1,
                              answered_at="2026-07-20 10:00:00")
            with patch.object(category_admin, "DB_PATH", db_path):
                data = category_admin.feedback_list(unanswered=1, offset=0, limit=20)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["rows"][0]["message"], "ждёт ответа")

    def test_pagination_offset_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(tmp)
            for i in range(5):
                _insert_feedback(db_path, message=f"msg{i}")
            with patch.object(category_admin, "DB_PATH", db_path):
                data = category_admin.feedback_list(unanswered=0, offset=2, limit=2)
        self.assertEqual(data["total"], 5)
        self.assertEqual(len(data["rows"]), 2)


class FeedbackGetTests(unittest.TestCase):
    def test_marks_is_read_on_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(tmp)
            fid = _insert_feedback(db_path, message="вопрос", is_read=0)
            with patch.object(category_admin, "DB_PATH", db_path):
                d = category_admin.feedback_get(fid)
                self.assertEqual(d["message"], "вопрос")
                conn = sqlite3.connect(db_path)
                is_read = conn.execute("SELECT is_read FROM feedback WHERE id=?", (fid,)).fetchone()[0]
                conn.close()
        self.assertEqual(is_read, 1)

    def test_not_found_raises_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(tmp)
            with patch.object(category_admin, "DB_PATH", db_path):
                with self.assertRaises(category_admin.HTTPException) as ctx:
                    category_admin.feedback_get(999)
        self.assertEqual(ctx.exception.status_code, 404)


class FeedbackReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_reply_marks_answered_and_saves_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(tmp)
            fid = _insert_feedback(db_path, user_id=2002, message="мой вопрос")
            with (
                patch.object(category_admin, "DB_PATH", db_path),
                patch.object(category_admin, "_send_telegram_message",
                             AsyncMock(return_value={"ok": True, "result": {"message_id": 555}})),
            ):
                body = category_admin.FeedbackReplyBody(text="Вот ответ")
                result = await category_admin.feedback_reply(fid, body)
                # Проверяем аргументы отправки: сначала вопрос, потом ответ
                sent_text = category_admin._send_telegram_message.await_args.args[1]

            self.assertTrue(result["ok"])
            self.assertLess(sent_text.index("мой вопрос"), sent_text.index("Вот ответ"))

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT answered_at, answer_text FROM feedback WHERE id=?", (fid,)
            ).fetchone()
            bm = conn.execute("SELECT chat_id, message_id FROM botmessage").fetchone()
            conn.close()
        self.assertIsNotNone(row[0])
        self.assertEqual(row[1], "Вот ответ")
        self.assertEqual(bm, (2002, 555))  # зарегистрировано для чат-гигиены бота

    async def test_delivery_failure_does_not_mark_answered(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(tmp)
            fid = _insert_feedback(db_path, user_id=2002, message="вопрос")
            with (
                patch.object(category_admin, "DB_PATH", db_path),
                patch.object(category_admin, "_send_telegram_message",
                             AsyncMock(return_value={"ok": False, "description": "blocked"})),
            ):
                body = category_admin.FeedbackReplyBody(text="ответ")
                result = await category_admin.feedback_reply(fid, body)

            self.assertFalse(result["ok"])
            self.assertIn("blocked", result["detail"])
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT answered_at, answer_text FROM feedback WHERE id=?", (fid,)
            ).fetchone()
            conn.close()
        self.assertIsNone(row[0])
        self.assertIsNone(row[1])

    async def test_empty_text_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(tmp)
            fid = _insert_feedback(db_path)
            with patch.object(category_admin, "DB_PATH", db_path):
                with self.assertRaises(category_admin.HTTPException) as ctx:
                    await category_admin.feedback_reply(fid, category_admin.FeedbackReplyBody(text="   "))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_not_found_raises_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(tmp)
            with patch.object(category_admin, "DB_PATH", db_path):
                with self.assertRaises(category_admin.HTTPException) as ctx:
                    await category_admin.feedback_reply(999, category_admin.FeedbackReplyBody(text="x"))
        self.assertEqual(ctx.exception.status_code, 404)


class FeedbackDeleteTests(unittest.TestCase):
    def test_delete_removes_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(tmp)
            fid = _insert_feedback(db_path)
            with patch.object(category_admin, "DB_PATH", db_path):
                result = category_admin.feedback_delete(fid)
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT id FROM feedback WHERE id=?", (fid,)).fetchone()
            conn.close()
        self.assertTrue(result["ok"])
        self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()
