@echo off
rem ─────────────────────────────────────────────────────────────────────────────
rem  Digity Core — Build a source-only update ZIP for client auto-update
rem
rem  Run this after every code change you want to push to clients.
rem  The ZIP contains ONLY Python source files (~50 KB), not the Python runtime
rem  or packages — so clients download and apply it in seconds.
rem
rem  Usage:
rem    cd digity-core\build
rem    make_update_zip.bat
rem
rem  Output: build\output\update-<version>.zip
rem
rem  After running:
rem    1. Create a GitHub Release tagged  v<version>
rem    2. Upload  update-<version>.zip  as a release asset
rem    3. Upload  latest.json           as a release asset
rem    4. Set DIGITY_UPDATE_URL in client environments to the raw URL of latest.json
rem       e.g.:
rem       https://github.com/<owner>/<repo>/releases/latest/download/latest.json
rem ─────────────────────────────────────────────────────────────────────────────
setlocal enabledelayedexpansion

set SCRIPT_DIR=%~dp0
set PROJECT_DIR=%SCRIPT_DIR%..\
set OUTPUT_DIR=%SCRIPT_DIR%output

rem ── Read version from version.txt ─────────────────────────────────────────────
set /p VERSION=<%PROJECT_DIR%version.txt
set VERSION=%VERSION: =%
if "%VERSION%"=="" (
    echo [ERROR] Could not read version from version.txt
    pause & exit /b 1
)
echo Version: %VERSION%

if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"

set ZIP_NAME=update-%VERSION%.zip
set ZIP_PATH=%OUTPUT_DIR%\%ZIP_NAME%
set JSON_PATH=%OUTPUT_DIR%\latest.json

rem ── Delete previous zip ───────────────────────────────────────────────────────
if exist "%ZIP_PATH%" del /q "%ZIP_PATH%"

rem ── Create the ZIP using PowerShell ──────────────────────────────────────────
echo Creating %ZIP_NAME%...
powershell -NoProfile -Command ^
  "Add-Type -Assembly System.IO.Compression.FileSystem; " ^
  "$zip = [System.IO.Compression.ZipFile]::Open('%ZIP_PATH%', 'Create'); " ^
  "function Add-Dir($src, $base) { " ^
  "  Get-ChildItem -Path $src -Recurse -File | Where-Object { $_.FullName -notmatch '\\\\__pycache__\\\\' } | ForEach-Object { " ^
  "    $rel = $_.FullName.Substring($base.Length).TrimStart('\\'); " ^
  "    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $_.FullName, $rel) | Out-Null " ^
  "  } " ^
  "} " ^
  "$base = '%PROJECT_DIR%'.TrimEnd('\'); " ^
  "Add-Dir '%PROJECT_DIR%app'      $base; " ^
  "Add-Dir '%PROJECT_DIR%core'     $base; " ^
  "Add-Dir '%PROJECT_DIR%producer' $base; " ^
  "Add-Dir '%PROJECT_DIR%tools'    $base; " ^
  "Get-ChildItem '%PROJECT_DIR%*.py' | ForEach-Object { [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $_.FullName, $_.Name) | Out-Null }; " ^
  "[System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, '%PROJECT_DIR%version.txt', 'version.txt') | Out-Null; " ^
  "$zip.Dispose()"

if not exist "%ZIP_PATH%" (
    echo [ERROR] ZIP creation failed.
    pause & exit /b 1
)
for %%F in ("%ZIP_PATH%") do set ZIP_SIZE=%%~zF
set /a ZIP_KB=%ZIP_SIZE% / 1024
echo [OK] %ZIP_NAME% (%ZIP_KB% KB)

rem ── Generate latest.json ──────────────────────────────────────────────────────
rem  Edit ZIP_URL below to match your actual GitHub repo before publishing.
set OWNER=digity-cristobalcorral
set REPO=Digity-core-Windows

(
echo {
echo   "version": "%VERSION%",
echo   "zip_url": "https://github.com/%OWNER%/%REPO%/releases/download/v%VERSION%/%ZIP_NAME%",
echo   "notes":   "Digity Core v%VERSION%"
echo }
) > "%JSON_PATH%"

echo [OK] latest.json written
echo.
echo   ============================================================
echo     Files ready to publish:
echo       %ZIP_PATH%
echo       %JSON_PATH%
echo.
echo     Steps:
echo       1. Edit ZIP_URL in latest.json if needed
echo       2. Create GitHub Release  v%VERSION%
echo       3. Upload both files as release assets
echo       4. Set DIGITY_UPDATE_URL env var on clients to:
echo          https://github.com/%OWNER%/%REPO%/releases/latest/download/latest.json
echo   ============================================================
echo.
