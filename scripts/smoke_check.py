# scripts/smoke_check.py
"""Дымовая проверка: импортирует все модули приложения.

Ловит ImportError, NameError на уровне модуля, синтаксические ошибки —
то, что компиляция не видит, а пользователь встретит первым же нажатием.
Запуск: python scripts/smoke_check.py (код возврата 0 = всё импортируется).
"""
import importlib
import pkgutil
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILED = []


def try_import(name: str) -> None:
    try:
        importlib.import_module(name)
        print(f"  ok  {name}")
    except Exception:
        FAILED.append(name)
        print(f"FAIL  {name}")
        traceback.print_exc(limit=3)


def walk(package_name: str) -> None:
    pkg = importlib.import_module(package_name)
    for m in pkgutil.walk_packages(pkg.__path__, prefix=package_name + "."):
        try_import(m.name)


if __name__ == "__main__":
    try_import("app.main")   # тянет все роутеры и подключения
    walk("app")              # плюс всё, что main не импортирует
    try_import("category_admin")
    if FAILED:
        print(f"\nПровалено: {len(FAILED)}: {', '.join(FAILED)}")
        sys.exit(1)
    print("\nВсе модули импортируются чисто.")
