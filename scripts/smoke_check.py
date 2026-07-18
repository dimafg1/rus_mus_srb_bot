# scripts/smoke_check.py
"""Дымовая проверка: импортирует все модули приложения.

Ловит ImportError, NameError на уровне модуля, синтаксические ошибки —
то, что компиляция не видит, а пользователь встретит первым же нажатием.
Запуск: python scripts/smoke_check.py (код возврата 0 = всё импортируется).
"""
import importlib
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED = []
SEEN = set()


def try_import(name: str) -> None:
    if name in SEEN:
        return
    SEEN.add(name)
    try:
        importlib.import_module(name)
        print(f"  ok  {name}")
    except Exception:
        FAILED.append(name)
        print(f"FAIL  {name}")
        traceback.print_exc(limit=3)


def app_modules() -> list[str]:
    """Find modules by path, including namespace dirs without __init__.py."""
    result = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(ROOT).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts.pop()
        result.append(".".join(parts))
    return result


if __name__ == "__main__":
    try_import("app.main")   # тянет реальные подключения и ловит конфликты роутеров
    for module_name in app_modules():
        try_import(module_name)
    try_import("category_admin")
    if FAILED:
        print(f"\nПровалено: {len(FAILED)}: {', '.join(FAILED)}")
        sys.exit(1)
    print("\nВсе модули импортируются чисто.")
