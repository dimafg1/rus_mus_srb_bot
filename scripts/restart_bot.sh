#!/bin/zsh
# Перезапуск бота. Работает и из SSH-сессии (nohup — процесс переживёт отключение).
cd "$(dirname "$0")/.."
PY=/Users/d/Library/Caches/pypoetry/virtualenvs/rus-mus-srb-bot-uk7w-_v4-py3.13/bin/python

if pkill -f "app/main.py" 2>/dev/null; then
  echo "Старый процесс бота остановлен"
  sleep 2
fi

mkdir -p logs
nohup "$PY" app/main.py >> logs/bot_console.log 2>&1 &
echo "Бот запущен (pid $!). Логи: logs/bot.log и logs/bot_console.log"
