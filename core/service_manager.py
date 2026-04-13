"""
ServiceManager — manages the lifecycle of each Glove Core subprocess.

Each service runs as a child Python process. The manager can:
  - start / stop / restart individual services
  - report status (running, stopped, error, starting)
  - stream stdout/stderr to a per-service log file
  - broadcast status changes via a callback (hooked to SocketIO)
"""
from __future__ import annotations

import subprocess
import threading
import time
import sys
import logging
from io import TextIOWrapper
from pathlib import Path
from enum import Enum
from typing import Callable, IO

from core.config import SERVICES, LOG_DIR

log = logging.getLogger(__name__)


class ServiceStatus(str, Enum):
    STOPPED  = "stopped"
    STARTING = "starting"
    RUNNING  = "running"
    ERROR    = "error"


class ManagedService:
    def __init__(self, key: str, cfg: dict):
        self.key         = key
        self.label       = cfg["label"]
        self.description = cfg.get("description", "")
        self.script      = cfg["script"]
        self.args        = cfg.get("args", [])
        self.autostart   = cfg.get("autostart", False)
        self.color       = cfg.get("color", "gray")

        self.status: ServiceStatus = ServiceStatus.STOPPED
        self.pid: int | None       = None
        self.exit_code: int | None = None
        self.started_at: float     = 0.0
        self.restarts: int         = 0
        self.error_msg: str        = ""

        self._proc: subprocess.Popen | None = None
        self._log_fh: IO | None            = None
        self._log_path = LOG_DIR / f"{key}.log"

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self) -> bool:
        if self.status == ServiceStatus.RUNNING:
            return True

        self.status    = ServiceStatus.STARTING
        self.error_msg = ""
        self.exit_code = None

        cmd = [sys.executable, self.script] + self.args
        log.info("[%s] starting: %s", self.key, " ".join(cmd))

        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            if self._log_fh:
                try:
                    self._log_fh.close()
                except Exception:
                    pass
            self._log_fh = open(self._log_path, "a", encoding="utf-8")
            self._proc = subprocess.Popen(
                cmd,
                stdout=self._log_fh,
                stderr=self._log_fh,
                text=True,
            )
            self.pid        = self._proc.pid
            self.started_at = time.time()
            self.status     = ServiceStatus.RUNNING
            log.info("[%s] PID %d", self.key, self.pid)

            # Monitor thread — detects crash
            threading.Thread(
                target=self._monitor, daemon=True
            ).start()
            return True

        except Exception as exc:
            self.status    = ServiceStatus.ERROR
            self.error_msg = str(exc)
            log.error("[%s] failed to start: %s", self.key, exc)
            return False

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            log.info("[%s] stopping PID %d", self.key, self._proc.pid)
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self.status = ServiceStatus.STOPPED
        self.pid    = None
        if self._log_fh:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None

    def restart(self) -> bool:
        self.stop()
        time.sleep(0.5)
        self.restarts += 1
        return self.start()

    def to_dict(self) -> dict:
        uptime = int(time.time() - self.started_at) if self.status == ServiceStatus.RUNNING else 0
        return {
            "key":         self.key,
            "label":       self.label,
            "description": self.description,
            "status":      self.status.value,
            "pid":         self.pid,
            "uptime":      uptime,
            "restarts":    self.restarts,
            "exit_code":   self.exit_code,
            "error_msg":   self.error_msg,
            "color":       self.color,
            "log_path":    str(self._log_path),
        }

    # ── Internal ───────────────────────────────────────────────────────────────

    def _monitor(self) -> None:
        """Runs in background thread — updates status when process exits."""
        self._proc.wait()
        self.exit_code = self._proc.returncode
        if self.status != ServiceStatus.STOPPED:
            # Unexpected exit
            self.status    = ServiceStatus.ERROR
            self.error_msg = f"exited with code {self.exit_code}"
            log.warning("[%s] crashed: exit code %d", self.key, self.exit_code)
        self.pid = None


class ServiceManager:
    """
    Manages all defined services. Notifies via on_change callback
    whenever a service status changes.
    """

    def __init__(self):
        self._services: dict[str, ManagedService] = {
            key: ManagedService(key, cfg)
            for key, cfg in SERVICES.items()
        }
        self._on_change: Callable[[dict], None] | None = None
        self._poll_thread: threading.Thread | None = None

    def set_on_change(self, cb: Callable[[dict], None]) -> None:
        """Register callback called with service dict on any status change."""
        self._on_change = cb

    def start_all(self) -> None:
        for svc in self._services.values():
            if svc.autostart:
                svc.start()
        self._start_polling()

    def stop_all(self) -> None:
        for svc in self._services.values():
            svc.stop()

    def start_service(self, key: str) -> bool:
        svc = self._services.get(key)
        if not svc:
            return False
        ok = svc.start()
        self._notify(svc)
        return ok

    def stop_service(self, key: str) -> None:
        svc = self._services.get(key)
        if svc:
            svc.stop()
            self._notify(svc)

    def restart_service(self, key: str) -> bool:
        svc = self._services.get(key)
        if not svc:
            return False
        ok = svc.restart()
        self._notify(svc)
        return ok

    def get_status(self, key: str) -> dict | None:
        svc = self._services.get(key)
        return svc.to_dict() if svc else None

    def get_all_status(self) -> list[dict]:
        return [svc.to_dict() for svc in self._services.values()]

    def get_log_tail(self, key: str, lines: int = 100) -> list[str]:
        svc = self._services.get(key)
        if not svc:
            return []
        try:
            with open(svc._log_path) as f:
                return f.readlines()[-lines:]
        except FileNotFoundError:
            return []

    # ── Internal ───────────────────────────────────────────────────────────────

    def _notify(self, svc: ManagedService) -> None:
        if self._on_change:
            try:
                self._on_change(svc.to_dict())
            except Exception as exc:
                log.warning("on_change callback failed: %s", exc)

    def _start_polling(self) -> None:
        """Poll every second to detect crashes and push updates."""
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True
        )
        self._poll_thread.start()

    def _poll_loop(self) -> None:
        prev: dict[str, str] = {}
        while True:
            time.sleep(1)
            for svc in self._services.values():
                # If process exited unexpectedly, _monitor already set ERROR.
                # Just detect status transitions here.
                current = svc.status.value
                if prev.get(svc.key) != current:
                    prev[svc.key] = current
                    self._notify(svc)
