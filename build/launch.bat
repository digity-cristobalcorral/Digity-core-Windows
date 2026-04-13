@echo off
title Digity Core
cd /D "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python environment not found.
    echo         Run setup_venv.bat to set it up.
    pause
    exit /b 1
)

.venv\Scripts\python.exe main.py --app

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Digity Core exited with an error  (code %ERRORLEVEL%^)
    echo         Check logs\ for details.
    pause
)
