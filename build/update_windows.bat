@echo off
rem ─────────────────────────────────────────────────────────────────────────────
rem  Digity Core — quick source update
rem
rem  Copies only the Python source files to the installed app directory.
rem  Use this after every code change — fast (seconds, no download needed).
rem
rem  Use build_windows.bat only when:
rem    - Running for the first time
rem    - requirements.txt changed (new packages)
rem    - You need to generate a new .exe installer for clients
rem
rem  Usage (from repo root or build folder):
rem    build\update_windows.bat
rem ─────────────────────────────────────────────────────────────────────────────
setlocal

set SCRIPT_DIR=%~dp0
set PROJECT_DIR=%SCRIPT_DIR%..\
set INSTALL_DIR=%LOCALAPPDATA%\DigityCore

if not exist "%INSTALL_DIR%" (
    echo [ERROR] App not installed. Run build_windows.bat first.
    pause
    exit /b 1
)

echo.
echo   Updating Digity Core source files...
echo   Destination: %INSTALL_DIR%
echo.

copy /Y "%PROJECT_DIR%*.py"               "%INSTALL_DIR%\"          >nul
xcopy /E /I /Y /Q "%PROJECT_DIR%app"      "%INSTALL_DIR%\app\"      >nul
xcopy /E /I /Y /Q "%PROJECT_DIR%core"     "%INSTALL_DIR%\core\"     >nul
xcopy /E /I /Y /Q "%PROJECT_DIR%producer" "%INSTALL_DIR%\producer\" >nul
xcopy /E /I /Y /Q "%PROJECT_DIR%tools"    "%INSTALL_DIR%\tools\"    >nul

echo   Done. Relaunch Digity Core to apply changes.
echo.
