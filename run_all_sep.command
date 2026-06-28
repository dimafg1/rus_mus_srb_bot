#!/bin/zsh
set -e

PROJECT_DIR="/Users/d/dev/rus_mus_srb_bot"
POETRY="/usr/local/bin/poetry"

# команды для двух окон Terminal через osascript
osascript <<EOF
tell application "Terminal"
    -- Окно 1: БОТ. Подхватываем ТОЛЬКО .env (без ключа веба)
    do script "cd $PROJECT_DIR && set -a; source .env; set +a; $POETRY run python -m app.main"

    -- Окно 2: ВЕБ. Подхватываем .env И .env.web (там лежит signing key)
    do script "cd $PROJECT_DIR && set -a; source .env; source .env.web; set +a; $POETRY run uvicorn app.web.app:app --reload --port 8080"
end tell
EOF
