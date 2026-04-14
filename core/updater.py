"""
Auto-updater for Digity Core.

How it works:
  1. On startup the app fetches a small JSON manifest from DIGITY_UPDATE_URL.
  2. If the remote version is newer than the local version.txt, the dashboard
     shows an "Update available" banner.
  3. The user (or an auto-apply policy) calls apply() to download a source-only
     ZIP (~50 KB) and extract it over the install directory.
  4. On next restart the new code runs automatically.

Manifest JSON format (host this file at DIGITY_UPDATE_URL):
  {
    "version": "1.0.1",
    "zip_url": "https://github.com/<owner>/<repo>/releases/download/v1.0.1/update-1.0.1.zip",
    "notes":   "Fixed sessions browser on Windows"
  }

Environment:
  DIGITY_UPDATE_URL  — URL to the manifest JSON.
                       Leave empty to disable update checks entirely.

Recommended hosting: GitHub Releases (free).
  - Create release  v1.0.1
  - Attach update-1.0.1.zip  (built by  build/make_update_zip.bat)
  - Attach latest.json       (the manifest above)
  - Set DIGITY_UPDATE_URL to the raw download URL of latest.json
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

# Root of the installed application (parent of core/)
INSTALL_DIR  = Path(__file__).parent.parent
VERSION_FILE = INSTALL_DIR / "version.txt"
UPDATE_URL   = os.environ.get("DIGITY_UPDATE_URL", "").strip()


# ── Public API ─────────────────────────────────────────────────────────────────

def get_current_version() -> str:
    """Read the local version from version.txt. Returns '0.0.0' if missing."""
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "0.0.0"


def check() -> dict:
    """
    Fetch the remote manifest and compare versions.

    Returns:
      {
        "available":  bool,
        "current":    str,   # e.g. "1.0.0"
        "latest":     str,   # e.g. "1.0.1"
        "notes":      str,   # release notes
        "zip_url":    str,   # download URL for the update ZIP
        "error":      str,   # non-empty if the check itself failed
      }
    """
    current = get_current_version()
    base = {"available": False, "current": current, "latest": current,
            "notes": "", "zip_url": "", "error": ""}

    if not UPDATE_URL:
        base["error"] = "DIGITY_UPDATE_URL not set"
        return base

    manifest = _fetch_manifest()
    if manifest is None:
        base["error"] = "Could not reach update server"
        return base

    latest = str(manifest.get("version", current)).strip()
    base["latest"]    = latest
    base["notes"]     = manifest.get("notes", "")
    base["zip_url"]   = manifest.get("zip_url", "")
    base["available"] = _version_tuple(latest) > _version_tuple(current)
    return base


def apply(zip_url: str) -> None:
    """
    Download the update ZIP from zip_url and extract it over INSTALL_DIR.
    Clears all __pycache__ directories so new .pyc files are rebuilt on restart.

    Raises on any error (network, bad zip, permission denied, etc.).
    """
    import urllib.request

    log.info("Downloading update from %s", zip_url)
    with urllib.request.urlopen(zip_url, timeout=60) as resp:
        data = resp.read()

    log.info("Extracting update (%d KB)...", len(data) // 1024)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # Security: only extract relative paths, skip any absolute or traversal paths
        for member in zf.infolist():
            target = (INSTALL_DIR / member.filename).resolve()
            try:
                target.relative_to(INSTALL_DIR.resolve())
            except ValueError:
                log.warning("Skipping unsafe zip entry: %s", member.filename)
                continue
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(member.filename))

    # Clear bytecode cache — old .pyc files will cause ImportErrors with new source
    for cache_dir in INSTALL_DIR.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)

    log.info("Update applied to %s. Restart the app to use the new version.", INSTALL_DIR)


# ── Internal ───────────────────────────────────────────────────────────────────

def _fetch_manifest() -> dict | None:
    try:
        import urllib.request
        with urllib.request.urlopen(UPDATE_URL, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.debug("Update manifest fetch failed: %s", exc)
        return None


def _version_tuple(v: str) -> tuple[int, ...]:
    """Convert '1.2.3' → (1, 2, 3) for comparison."""
    try:
        return tuple(int(x) for x in v.strip().split("."))
    except Exception:
        return (0,)
