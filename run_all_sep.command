#!/bin/zsh
set -e

# === 1) Абсолютный путь к проекту (поменяйте под себя!) ===
PROJECT_DIR="/Users/d/dev/rus_mus_srb_bot"

# === 2) Путь к poetry (узнайте: which poetry) ===
POETRY="/usr/local/bin/poetry"

# === 3) Команды для бота и веба ===
BOT_CMD="cd $PROJECT_DIR && $POETRY run python -m app.main"
WEB_CMD="cd $PROJECT_DIR && $POETRY run uvicorn app.web.app:app --reload --port 8080"

# === 4) Открываем два отдельных окна Terminal через osascript ===
osascript <<EOF
tell application "Terminal"
    do script "$BOT_CMD"
    do script "$WEB_CMD"
end tell
EOF
