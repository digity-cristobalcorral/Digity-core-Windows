@echo off
title Digity Core
cd /D "%~dp0"
python\python.exe main.py --app
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Digity Core crashed  (code %ERRORLEVEL%^)
    echo         See logs\ for details.
    pause
)
