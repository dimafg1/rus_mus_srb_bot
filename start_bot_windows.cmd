@echo off

powershell -NoExit -Command "cd '%~dp0'; .\.venv\Scripts\python.exe -m app.main"