#!/bin/zsh
set -e

# === 1) Абсолютный путь к проекту (поменяйте под себя) ===
cd "/Users/d/dev/rus_mus_srb_bot"

# === 2) Подхватить переменные из .env (если используете) ===
if [ -f ".env" ]; then
  set -a
  source .env
  set +a
fi

# === 3) Полный путь к poetry (узнайте в терминале: which poetry) ===
POETRY="/usr/local/bin/poetry"

# === 4) Запуск: бот в фоне, веб — в переднем плане (видны логи) ===
"$POETRY" run python -m app.main &
"$POETRY" run uvicorn app.web.app:app --reload --port 8080
