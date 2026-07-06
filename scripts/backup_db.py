"""
Безопасный бэкап dev.db через sqlite3 backup API.

База работает в WAL-режиме: копирование файла (cp) даёт неполный снимок —
свежие коммиты лежат в dev.db-wal. Backup API читает согласованное состояние
включая WAL, не мешая работающему боту.

Запуск:  python scripts/backup_db.py
Результат: backups/dev_YYYY-MM-DD_HHMM.db (хранятся последние 10)
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "dev.db"
BACKUP_DIR = ROOT / "backups"
KEEP_LAST = 10


def main() -> int:
    if not DB_PATH.exists():
        print(f"ОШИБКА: {DB_PATH} не найдена")
        return 1

    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    dst_path = BACKUP_DIR / f"dev_{stamp}.db"

    src = sqlite3.connect(DB_PATH, timeout=30)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    # Проверка целостности копии
    check_conn = sqlite3.connect(dst_path)
    result = check_conn.execute("PRAGMA integrity_check").fetchone()[0]
    check_conn.close()
    size_kb = dst_path.stat().st_size // 1024
    print(f"Бэкап: {dst_path.relative_to(ROOT)} ({size_kb} КБ)")
    print(f"integrity_check: {result}")
    if result != "ok":
        print("ОШИБКА: копия не прошла проверку целостности!")
        return 1

    # Ротация: оставляем последние KEEP_LAST
    backups = sorted(BACKUP_DIR.glob("dev_*.db"))
    for old in backups[:-KEEP_LAST]:
        old.unlink()
        print(f"Удалён старый: {old.name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
