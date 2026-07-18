"""Resolve the SQLite file consistently for bot-side maintenance tools."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote


SQLITE_URL_PREFIXES = (
    "sqlite+aiosqlite:///",
    "sqlite+pysqlite:///",
    "sqlite:///",
)


def dotenv_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return None


def config_value(
    root: Path,
    key: str,
    default: str | None = None,
    *,
    env_files: tuple[str, ...] = (".env",),
) -> str | None:
    """Read process env first, then named dotenv files under ``root``."""
    process_value = os.getenv(key)
    if process_value is not None:
        return process_value
    for filename in env_files:
        value = dotenv_value(root / filename, key)
        if value is not None:
            return value
    return default


def resolve_sqlite_path(root: Path) -> Path:
    """Resolve DATABASE_PATH/DATABASE_URL, falling back to ``root/dev.db``.

    Relative SQLite paths are resolved from the project root, not from the
    caller's current working directory. This keeps the bot, admin and backup
    utility on the same database after deployment.
    """
    root = root.resolve()
    env_file = root / ".env"
    explicit = os.getenv("DATABASE_PATH") or dotenv_value(env_file, "DATABASE_PATH")
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_absolute() else (root / path).resolve()

    url = os.getenv("DATABASE_URL") or dotenv_value(env_file, "DATABASE_URL")
    if not url:
        return root / "dev.db"

    for prefix in SQLITE_URL_PREFIXES:
        if url.startswith(prefix):
            raw_path = unquote(url[len(prefix):].split("?", 1)[0])
            if raw_path == ":memory:" or raw_path.startswith("file:"):
                raise RuntimeError("In-memory SQLite URLs do not have a backup file")
            path = Path(raw_path).expanduser()
            return path if path.is_absolute() else (root / path).resolve()
    raise RuntimeError("Only SQLite DATABASE_URL values are supported by this utility")


def absolutize_sqlite_url(url: str, root: Path) -> str:
    """Make a file-backed SQLite URL independent from process cwd."""
    root = root.resolve()
    for prefix in SQLITE_URL_PREFIXES:
        if not url.startswith(prefix):
            continue
        raw = url[len(prefix):]
        raw_path, separator, query = raw.partition("?")
        decoded_path = unquote(raw_path)
        if decoded_path == ":memory:" or decoded_path.startswith("file:"):
            return url
        path = Path(decoded_path).expanduser()
        if not path.is_absolute():
            path = (root / path).resolve()
        suffix = f"?{query}" if separator else ""
        return f"{prefix}{path.as_posix()}{suffix}"
    return url


def sqlite_url_for_path(path: Path) -> str:
    """Build the async SQLAlchemy URL for an absolute SQLite file path."""
    return f"sqlite+aiosqlite:///{path.resolve().as_posix()}"
