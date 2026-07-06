#!/bin/zsh
cd "/Users/d/dev/rus_mus_srb_bot"

# Освобождаем порт если занят
lsof -ti:8001 | xargs kill -9 2>/dev/null

# Запуск сервера в фоне
/usr/local/bin/poetry run python category_admin.py &
SERVER_PID=$!

# Ждём пока сервер поднимется
echo "Запускаем сервер..."
for i in {1..20}; do
  sleep 0.5
  if curl -s http://localhost:8001/ > /dev/null 2>&1; then
    break
  fi
done

# Открываем браузер
open "http://localhost:8001"

# Держим терминал открытым пока сервер работает
wait $SERVER_PID
