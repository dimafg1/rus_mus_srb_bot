#!/bin/zsh
# Перезапуск веб-админки (порт 8001). Работает и из SSH-сессии.
cd "$(dirname "$0")/.."
PY=/Users/d/Library/Caches/pypoetry/virtualenvs/rus-mus-srb-bot-uk7w-_v4-py3.13/bin/python

if pkill -f "category_admin.py" 2>/dev/null; then
  echo "Старая админка остановлена"
  sleep 2
fi

mkdir -p logs
nohup "$PY" category_admin.py >> logs/admin_console.log 2>&1 &
echo "Админка запущена (pid $!): http://localhost:8001 | http://100.104.29.69:8001"
