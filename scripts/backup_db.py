"""
Безопасный бэкап рабочей SQLite БД через sqlite3 backup API.

База работает в WAL-режиме: копирование файла (cp) даёт неполный снимок —
свежие коммиты лежат в dev.db-wal. Backup API читает согласованное состояние
включая WAL, не мешая работающему боту.

Запуск:  python scripts/backup_db.py
Результат: backups/dev_YYYY-MM-DD_HHMMSS.db (хранятся последние 10)
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db_path import config_value, resolve_sqlite_path

DB_PATH = resolve_sqlite_path(ROOT)
_backup_dir_raw = config_value(ROOT, "BACKUP_DIR", "backups") or "backups"
BACKUP_DIR = Path(_backup_dir_raw).expanduser()
if not BACKUP_DIR.is_absolute():
    BACKUP_DIR = (ROOT / BACKUP_DIR).resolve()
KEEP_LAST = 10


def main() -> int:
    if not DB_PATH.exists():
        print(f"ОШИБКА: {DB_PATH} не найдена")
        return 1

    BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    BACKUP_DIR.chmod(0o700)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dst_path = BACKUP_DIR / f"{DB_PATH.stem}_{stamp}.db"

    src = sqlite3.connect(DB_PATH, timeout=30)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    dst_path.chmod(0o600)

    # Проверка целостности копии
    check_conn = sqlite3.connect(dst_path)
    result = check_conn.execute("PRAGMA integrity_check").fetchone()[0]
    check_conn.close()
    size_kb = dst_path.stat().st_size // 1024
    try:
        display_path = dst_path.relative_to(ROOT)
    except ValueError:
        display_path = dst_path
    print(f"Бэкап: {display_path} ({size_kb} КБ)")
    print(f"integrity_check: {result}")
    if result != "ok":
        print("ОШИБКА: копия не прошла проверку целостности!")
        return 1

    # Ротация: оставляем последние KEEP_LAST
    backups = sorted(BACKUP_DIR.glob(f"{DB_PATH.stem}_*.db"))
    for old in backups[:-KEEP_LAST]:
        old.unlink()
        print(f"Удалён старый: {old.name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
