#!/usr/bin/env python3
"""
Digity — entry point.

Usage:
    python main.py              # browser mode (default)
    python main.py --app        # native desktop window (pywebview)
    python main.py --no-browser # headless, no UI opened
    python main.py --port 5000
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path

# ── Ensure project root is in path ────────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from core.config import DASHBOARD_HOST, DASHBOARD_PORT, LOG_DIR
from core.service_manager import ServiceManager

# ── Logging setup ─────────────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "glove-core.log"),
    ],
)
log = logging.getLogger("glove-core")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Digity — start all services + dashboard")
    p.add_argument("--app",        action="store_true", help="Open as native desktop window (pywebview)")
    p.add_argument("--no-browser", action="store_true", help="Don't open any UI automatically")
    p.add_argument("--port", type=int, default=DASHBOARD_PORT, help="Dashboard port (default 5000)")
    return p.parse_args()


def _run_flask(flask_app, socketio, host: str, port: int) -> None:
    """Run Flask/SocketIO in a background thread (used in --app mode)."""
    socketio.run(
        flask_app,
        host=host,
        port=port,
        use_reloader=False,
        log_output=False,
        allow_unsafe_werkzeug=True,
    )


def main() -> None:
    args = parse_args()

    if not 1 <= args.port <= 65535:
        log.error("Invalid port: %d (must be 1–65535)", args.port)
        sys.exit(1)

    log.info("=" * 60)
    log.info("  Digity starting")
    log.info("  Dashboard → http://localhost:%d", args.port)
    log.info("=" * 60)

    # ── Service manager ────────────────────────────────────────────────────────
    manager = ServiceManager()

    # ── Flask + SocketIO server ────────────────────────────────────────────────
    from app.server import create_app
    flask_app, socketio = create_app(manager)

    def on_service_change(svc_dict: dict) -> None:
        socketio.emit("service_update", svc_dict)

    manager.set_on_change(on_service_change)

    # ── Start all autostart services ───────────────────────────────────────────
    log.info("Starting services...")
    manager.start_all()

    # ── Graceful shutdown ──────────────────────────────────────────────────────
    def shutdown(sig=None, frame=None):
        log.info("Shutting down — stopping all services...")
        manager.stop_all()
        log.info("Goodbye.")
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    url = f"http://localhost:{args.port}"

    # ── Native desktop window mode ────────────────────────────────────────────
    if args.app:
        # Force Qt backend (PyQt5 + QtWebEngine) — GTK requires system gi package
        # which may not be present. Qt is already in requirements.txt.
        import os
        os.environ.setdefault("PYWEBVIEW_GUI", "qt")
        # Must be set before Qt/webengine initialises inside webview.start()
        os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")

        try:
            import webview
        except ImportError:
            log.error("pywebview not installed. Run: pip install pywebview")
            sys.exit(1)

        # Flask must run in a background thread; pywebview owns the main thread
        flask_thread = threading.Thread(
            target=_run_flask,
            args=(flask_app, socketio, DASHBOARD_HOST, args.port),
            daemon=True,
            name="flask",
        )
        flask_thread.start()

        # Wait until Flask is ready
        time.sleep(1.5)
        log.info("Opening desktop window → %s", url)

        window = webview.create_window(
            title="Digity",
            url=url,
            width=1280,
            height=800,
            min_size=(900, 600),
            background_color="#07090e",
        )

        # Shutdown services when window is closed
        window.events.closed += shutdown

        webview.start(debug=False)
        return

    # ── Browser mode (default) ────────────────────────────────────────────────
    if not args.no_browser:
        def _open():
            time.sleep(1.5)
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    # Flask blocks the main thread
    log.info("Dashboard running on http://%s:%d", DASHBOARD_HOST, args.port)
    _run_flask(flask_app, socketio, DASHBOARD_HOST, args.port)


if __name__ == "__main__":
    main()
