@echo off
rem ─────────────────────────────────────────────────────────────────────────────
rem  Digity Core — Fast rebuild (source files only, no Python re-download)
rem
rem  Use this after code changes when build\dist\python\ already exists.
rem  Skips downloading Python and reinstalling packages (~5 min saved).
rem
rem  Usage:
rem    cd digity-core\build
rem    rebuild_fast.bat
rem
rem  Output: build\output\DigityCore-Setup-1.0.0.exe
rem ─────────────────────────────────────────────────────────────────────────────
setlocal enabledelayedexpansion

set SCRIPT_DIR=%~dp0
set PROJECT_DIR=%SCRIPT_DIR%..\
set DIST_DIR=%SCRIPT_DIR%dist

echo.
echo   ============================================
echo     Digity Core  ^|  Fast Rebuild
echo   ============================================
echo.

rem ── Check that dist\python exists ────────────────────────────────────────────
if not exist "%DIST_DIR%\python\python.exe" (
    echo [ERROR] build\dist\python\ not found.
    echo         Run build_windows.bat first to do a full build.
    pause & exit /b 1
)
echo [OK] Python runtime found at dist\python\

rem ── Sync source files ────────────────────────────────────────────────────────
echo [1/2] Syncing source files...

for %%F in ("%PROJECT_DIR%*.py")   do copy /Y "%%F" "%DIST_DIR%\" >nul
copy /Y "%PROJECT_DIR%requirements.txt" "%DIST_DIR%\" >nul
copy /Y "%PROJECT_DIR%version.txt"      "%DIST_DIR%\" >nul

xcopy "%PROJECT_DIR%app"      "%DIST_DIR%\app\"      /E /I /Y /Q >nul
xcopy "%PROJECT_DIR%core"     "%DIST_DIR%\core\"     /E /I /Y /Q >nul
xcopy "%PROJECT_DIR%producer" "%DIST_DIR%\producer\" /E /I /Y /Q >nul
xcopy "%PROJECT_DIR%tools"    "%DIST_DIR%\tools\"    /E /I /Y /Q >nul

echo [OK] Source files synced.

rem ── Find Inno Setup ──────────────────────────────────────────────────────────
set "ISCC="
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if exist "C:\Program Files\Inno Setup 6\ISCC.exe"       set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
if "!ISCC!"=="" for /f "delims=" %%P in ('where ISCC 2^>nul') do set "ISCC=%%P"
if "!ISCC!"=="" for /d %%D in ("C:\Program Files (x86)\Inno Setup*" "C:\Program Files\Inno Setup*") do if exist "%%D\ISCC.exe" set "ISCC=%%D\ISCC.exe"

rem ── Compile installer ────────────────────────────────────────────────────────
if not exist "%SCRIPT_DIR%output" mkdir "%SCRIPT_DIR%output"

if "!ISCC!"=="" (
    echo.
    echo [!] Inno Setup not found — compile manually:
    echo     1. Abre Inno Setup
    echo     2. File ^> Open ^> %SCRIPT_DIR%installer.iss
    echo     3. Build ^> Compile
    echo.
) else (
    echo [2/2] Compiling installer...
    "!ISCC!" "%SCRIPT_DIR%installer.iss"
    if %ERRORLEVEL% NEQ 0 (echo [ERROR] Inno Setup failed & pause & exit /b 1)
    echo.
    echo   ============================================
    echo     Listo!
    echo     Installer: build\output\DigityCore-Setup-1.0.0.exe
    echo   ============================================
    echo.
)

pause
