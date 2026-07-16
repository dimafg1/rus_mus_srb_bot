# rus_mus_srb_bot

Telegram-бот русскоязычного музыкального сообщества Сербии: барахолка
инструментов, услуги музыкантов, вакансии, афиша событий, релизы и
исполнители.

Python 3.13 · aiogram 3.x · SQLite (aiosqlite + SQLModel) · FastAPI.

## Установка

```bash
poetry install          # зависимости из pyproject.toml
```

Переменные окружения (`.env`):

| Переменная | Что это |
|---|---|
| `TOKEN` | токен Telegram-бота от BotFather |
| `DATABASE_URL` | по умолчанию `sqlite+aiosqlite:///./dev.db` |
| `WEBAPP_BASE` | база URL веб-страниц (TWA-плеер видео) |

## Запуск

```bash
python -m app.main            # бот
python category_admin.py      # локальный веб-админ (порт 8001)
uvicorn app.web.app:app       # веб-часть (редиректы /go, медиа-прокси)
```

На macOS есть обёртки `run_all.command` и т.п. (пути захардкожены под
машину владельца).

## Проверка

```bash
python scripts/smoke_check.py   # импорт всех модулей: ловит NameError/ImportError
python scripts/backup_db.py     # бэкап БД (sqlite backup API + integrity_check)
```

**Важно про данные:** `dev.db` в WAL-режиме — не копировать файлом,
только `scripts/backup_db.py`. БД, логи и бэкапы вне git.

Документация для разработки — `CLAUDE.md`; стратегия проекта —
`docs/strategy/00_strategy_map.md`.
