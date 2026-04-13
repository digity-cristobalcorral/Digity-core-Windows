@echo off
rem ─────────────────────────────────────────────────────────────────────────────
rem  Digity Core — Windows installer build script
rem
rem  Run this ONCE on a Windows build machine to produce the installer exe.
rem  The resulting installer is completely self-contained — clients need
rem  nothing pre-installed.
rem
rem  Prerequisites (build machine only, NOT required by clients):
rem    1. Python 3.11+    https://python.org/downloads/
rem    2. Inno Setup 6+   https://jrsoftware.org/isinfo.php
rem
rem  Usage:
rem    cd digity-core\build
rem    build_windows.bat
rem
rem  Output: build\output\DigityCore-Setup-1.0.0.exe  (~150 MB)
rem ─────────────────────────────────────────────────────────────────────────────
setlocal enabledelayedexpansion

set SCRIPT_DIR=%~dp0
set PROJECT_DIR=%SCRIPT_DIR%..\
set DIST_DIR=%SCRIPT_DIR%dist
set PY_DIR=%DIST_DIR%\python

echo.
echo   ============================================
echo     Digity Core  ^|  Windows Installer Build
echo   ============================================
echo.

rem ── Check prerequisites ──────────────────────────────────────────────────────
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python not found in PATH.
    echo         Install from: https://python.org/downloads/
    exit /b 1
)
for /f "delims=" %%V in ('python --version 2^>^&1') do echo [OK] %%V

set "ISCC="
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if exist "C:\Program Files\Inno Setup 6\ISCC.exe"       set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
rem  Fallback: search PATH (works if Inno Setup added itself to PATH)
if "!ISCC!"=="" (
    for /f "delims=" %%P in ('where ISCC 2^>nul') do set "ISCC=%%P"
)
rem  Fallback: search common install roots for any Inno Setup version folder
if "!ISCC!"=="" (
    for /d %%D in ("C:\Program Files (x86)\Inno Setup*" "C:\Program Files\Inno Setup*") do (
        if exist "%%D\ISCC.exe" set "ISCC=%%D\ISCC.exe"
    )
)
if "!ISCC!"=="" (
    echo [ERROR] Inno Setup not found.
    echo         Install from: https://jrsoftware.org/isinfo.php
    exit /b 1
)
echo [OK] Inno Setup: !ISCC!

echo.

rem ── Step 1: Clean previous build ─────────────────────────────────────────────
echo [1/5] Cleaning previous build...
if exist "%DIST_DIR%" rmdir /s /q "%DIST_DIR%"
mkdir "%DIST_DIR%"
mkdir "%PY_DIR%"

rem ── Step 2: Set up self-contained Python ──────────────────────────────────────
echo [2/5] Downloading Python 3.11 embeddable distribution...
rem  ~25 MB — the entire Python runtime, no system install needed.
powershell -NoProfile -Command ^
  "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip' -OutFile '%DIST_DIR%\py_embed.zip' -UseBasicParsing"
if %ERRORLEVEL% NEQ 0 (echo [ERROR] Download failed & exit /b 1)

powershell -NoProfile -Command "Expand-Archive -Path '%DIST_DIR%\py_embed.zip' -DestinationPath '%PY_DIR%'"
del /q "%DIST_DIR%\py_embed.zip"

rem  Enable site-packages (commented out by default in embeddable Python)
for %%F in ("%PY_DIR%\python*._pth") do (
    powershell -NoProfile -Command ^
      "(Get-Content '%%F') -replace '#import site','import site' | Set-Content '%%F'"
)

rem  Bootstrap pip
echo       Bootstrapping pip...
powershell -NoProfile -Command ^
  "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%PY_DIR%\get-pip.py' -UseBasicParsing"
"%PY_DIR%\python.exe" "%PY_DIR%\get-pip.py" --quiet
del /q "%PY_DIR%\get-pip.py"

rem ── Step 3: Install Python packages ──────────────────────────────────────────
echo [3/5] Installing Python packages (may take a few minutes)...
"%PY_DIR%\python.exe" -m pip install -r "%PROJECT_DIR%requirements.txt" ^
    --no-warn-script-location -q
if %ERRORLEVEL% NEQ 0 (echo [ERROR] pip install failed & exit /b 1)

rem  Remove pip itself from the bundle (not needed at runtime, saves ~3 MB)
"%PY_DIR%\python.exe" -m pip uninstall pip setuptools wheel -y -q 2>nul

rem ── Step 4: Copy project files ───────────────────────────────────────────────
echo [4/5] Copying project files...
for %%F in ("%PROJECT_DIR%*.py")          do copy "%%F" "%DIST_DIR%\" >nul
copy "%PROJECT_DIR%requirements.txt"          "%DIST_DIR%\" >nul
xcopy "%PROJECT_DIR%app"      "%DIST_DIR%\app\"      /E /I /Y /Q >nul
xcopy "%PROJECT_DIR%core"     "%DIST_DIR%\core\"     /E /I /Y /Q >nul
xcopy "%PROJECT_DIR%producer" "%DIST_DIR%\producer\" /E /I /Y /Q >nul
xcopy "%PROJECT_DIR%tools"    "%DIST_DIR%\tools\"    /E /I /Y /Q >nul
copy "%SCRIPT_DIR%launch.bat" "%DIST_DIR%\" >nul
if %ERRORLEVEL% NEQ 0 (echo [ERROR] File copy failed & exit /b 1)

rem ── Step 5: Compile installer ─────────────────────────────────────────────────
echo [5/5] Compiling installer...
if not exist "%SCRIPT_DIR%output" mkdir "%SCRIPT_DIR%output"
"!ISCC!" "%SCRIPT_DIR%installer.iss"
if %ERRORLEVEL% NEQ 0 (echo [ERROR] Inno Setup failed & exit /b 1)

echo.
echo   ============================================
echo     Build complete!
echo     Installer: build\output\DigityCore-Setup-1.0.0.exe
echo   ============================================
echo.
