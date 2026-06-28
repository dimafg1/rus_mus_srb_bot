@echo off
chcp 65001 >nul
setlocal

REM === 1) ПУТЬ К ПРОЕКТУ (поменяйте под себя) ===
set "PROJECT_DIR=C:\dev\rus_mus_srb_bot"

REM === 2) Идём в проект ===
pushd "%PROJECT_DIR%"

REM === 3) Подхватить .env (опционально). Простая загрузка: KEY=VALUE, строки с # игнорируются
if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    set "line=%%A"
    if not "%%A"=="" if /i not "%%A:~0,1"=="#" (
      set "%%A=%%B"
    )
  )
)

REM === 4) Путь к poetry (если не в PATH, укажите полный путь ниже) ===
where poetry >nul 2>nul || (
  echo Poetry не найден в PATH. Укажите путь вручную:
  echo   set "POETRY=C:\Users\%USERNAME%\AppData\Roaming\Python\Scripts\poetry.exe"
  goto :run
)
for /f "delims=" %%P in ('where poetry') do set "POETRY=%%P"

:run
REM === 5) Запускаем два ОТДЕЛЬНЫХ окна с логами ===
start "BOT" cmd /k "%POETRY% run python -m app.main"
start "WEB" cmd /k "%POETRY% run uvicorn app.web.app:app --reload --port 8080"

popd
endlocal
