# rus_mus_srb_bot

Telegram-бот русскоязычного музыкального сообщества Сербии: барахолка,
услуги, вакансии, афиша, релизы и исполнители.

Python 3.13 · aiogram 3.x · SQLite · FastAPI.

## Локальная установка

```bash
poetry install
```

Проект работает в Poetry application mode и не устанавливается как отдельный
Python-пакет. После изменения зависимостей обязательно выполнять `poetry lock`
и коммитить `poetry.lock` вместе с `pyproject.toml`.

Минимальный `.env`:

```dotenv
BOT_TOKEN=token-from-botfather
DATABASE_URL=sqlite+aiosqlite:///./dev.db
WEBAPP_BASE=https://example.org/rus_mus_srb_bot
RUS_MUS_SRB_BOT_SIGNING_KEY=long-random-value
LOG_LEVEL=INFO
```

| Переменная | Назначение |
|---|---|
| `BOT_TOKEN` | токен Telegram-бота |
| `DATABASE_URL` | URL SQLite для бота и FastAPI |
| `DATABASE_PATH` | необязательный явный путь к SQLite для sync-админки и backup |
| `WEBAPP_BASE` | публичная база URL web-контура |
| `RUS_MUS_SRB_BOT_SIGNING_KEY` | подпись contact/media URL |
| `LOG_DIR` | каталог файловых логов; локально `./logs` |
| `BACKUP_DIR` | каталог резервных копий; локально `./backups` |
| `CATEGORY_ADMIN_HOST` | bind web-админки; по умолчанию `127.0.0.1` |
| `CATEGORY_ADMIN_USER`, `CATEGORY_ADMIN_PASSWORD` | обязательны для удалённого доступа к админке |
| `CATEGORY_ADMIN_ALLOWED_HOSTS` | DNS-имена удалённой админки через запятую |
| `CATEGORY_ADMIN_UPLOAD_CHAT_ID` | Telegram chat ID для загрузки медиа через web-админку |

Файлы окружения должны иметь права `600` и не должны попадать в Git.

После создания `.env` проверьте, что все модули импортируются:

```bash
poetry run python scripts/smoke_check.py
```

## Запуск

```bash
poetry run python -m app.main
poetry run python category_admin.py
poetry run uvicorn app.web.app:app --host 127.0.0.1 --port 8080
```

- polling-бот должен работать только в одном экземпляре;
- category admin по умолчанию доступен только на localhost:8001;
- при удалённом bind админка требует HTTP Basic credentials;
- web-процесс обслуживает подписанные contact/media URL и YouTube player;
- `GET /healthz` проверяет соединение web-процесса с БД.

## Проверки

```bash
poetry check --lock
poetry run python -m unittest discover -s tests -v
poetry run python scripts/smoke_check.py
poetry run pip check
poetry run python scripts/backup_db.py
```

`dev.db` работает в WAL-режиме. Нельзя переносить только файл БД через `cp`:
используйте `scripts/backup_db.py`, который применяет SQLite backup API и затем
выполняет `PRAGMA integrity_check`.

## Развёртывание на Linux

Шаблоны находятся в `deploy/`:

- `deploy/systemd/rus-mus-srb-bot.service` — polling;
- `deploy/systemd/rus-mus-srb-web.service` — redirect/media web;
- `deploy/systemd/rus-mus-srb-admin.service` — локальная админка;
- `deploy/systemd/rus-mus-srb-backup.{service,timer}` — ежедневный backup;
- `deploy/nginx/rus-mus-srb.conf` — reverse proxy web-контура;
- `deploy/bot.env.example` — перечень production-переменных.

Шаблоны предполагают:

```text
код:       /opt/rus_mus_srb_bot
venv:      /opt/rus_mus_srb_bot/.venv
БД:        /var/lib/rus-mus-srb-bot/bot.db
логи:      /var/log/rus-mus-srb-bot
бэкапы:    /var/backups/rus-mus-srb-bot
env:       /etc/rus-mus-srb-bot/bot.env
пользователь systemd: rusmus
```

Перед первым стартом необходимо создать эти каталоги, назначить владельца
`rusmus`, установить права `700` для каталогов и `600` для env/БД/бэкапа.
Unit-файлы ожидают виртуальное окружение внутри каталога проекта:

```bash
cd /opt/rus_mus_srb_bot
POETRY_VIRTUALENVS_IN_PROJECT=true poetry install --only main
```

Перед переключением сервера остановите локальный polling, сделайте свежий
backup, восстановите его на сервере и проверьте `integrity_check`. До запуска
трёх процессов на восстановленной БД один раз выполните (повторный запуск
безопасен):

```bash
/opt/rus_mus_srb_bot/.venv/bin/python scripts/migrate_release_fks.py
```

Access/error logs location с подписанными URL и uvicorn access log отключены в
шаблонах: contact и media URL содержат краткоживущие токены в query string,
которые нельзя писать в nginx log или systemd journal. Доступность проверяется
отдельным `/healthz`, для которого nginx-логирование остаётся включённым.

Документация для разработки — `CLAUDE.md`; стратегия проекта —
`docs/strategy/00_strategy_map.md`.
