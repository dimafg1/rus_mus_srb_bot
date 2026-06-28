#!/bin/zsh
set -e

# === 1) Абсолютный путь к проекту ===
cd "/Users/d/dev/rus_mus_srb_bot"

# === 2) Полный путь к poetry ===
POETRY="/usr/local/bin/poetry"

# === 3) Запуск бота (в фоне), с .env ===
(
  set -a
  source .env
  set +a
  "$POETRY" run python -m app.main
) &

# === 4) Запуск веб-сервера (в переднем плане), с .env + .env.web ===
set -a
source .env
[ -f ".env.web" ] && source .env.web
set +a
"$POETRY" run uvicorn app.web.app:app --port 8080
