# Путь к проекту (поменяйте)
$PROJECT_DIR = "C:\dev\rus_mus_srb_bot"

# Если нужен .env — подхватим пары KEY=VALUE (простая загрузка)
$envFile = Join-Path $PROJECT_DIR ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -and -not $_.TrimStart().StartsWith("#")) {
            $pair = $_ -split "=", 2
            if ($pair.Count -eq 2) {
                $key = $pair[0].Trim()
                $val = $pair[1].Trim()
                if ($key) { [Environment]::SetEnvironmentVariable($key, $val, "Process") }
            }
        }
    }
}

# Команды
$BOT = "cd `"$PROJECT_DIR`" ; poetry run python -m app.main"
$WEB = "cd `"$PROJECT_DIR`" ; poetry run uvicorn app.web.app:app --reload --port 8080"

# Открыть две вкладки в Windows Terminal (wt.exe должен быть установлен)
# Первая вкладка: BOT, вторая: WEB
wt -w 0 nt -p "Windows PowerShell" powershell -NoExit -Command $BOT ; `
   nt -p "Windows PowerShell" powershell -NoExit -Command $WEB
