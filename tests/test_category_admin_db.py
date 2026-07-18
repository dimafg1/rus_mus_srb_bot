import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import category_admin


class CategoryAdminDbTests(unittest.TestCase):
    def test_context_commits_and_closes_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "admin.db"
            with patch.object(category_admin, "DB_PATH", db_path):
                with category_admin.db() as connection:
                    connection.execute("CREATE TABLE sample(value TEXT)")
                    connection.execute("INSERT INTO sample VALUES ('saved')")

                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")

                check = sqlite3.connect(db_path)
                try:
                    self.assertEqual(
                        check.execute("SELECT value FROM sample").fetchone()[0],
                        "saved",
                    )
                finally:
                    check.close()


if __name__ == "__main__":
    unittest.main()
