"""
Flask + SocketIO server — serves the dashboard and exposes a JSON API
for recording control and service management.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import secrets
import socket
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

from core.config import (
    DAEMON_UDP_PORT,
    EXO_BAUD,
    EXO_SERIAL_PORT,
    POV_PREVIEW_RGB,
    POV2_PREVIEW_RGB,
    DATA_DIR,
    ZMQ_STATUS_PORT,
)
from core.service_manager import ServiceManager
from core import user_config

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR   = Path(__file__).parent / "static"

# Seconds without preview frames before camera is considered disconnected
CAM_TIMEOUT = 4.0

# RealSense USB vendor ID (used to detect cameras via /sys)
REALSENSE_VENDOR = "8086"


class HardwareMonitor:
    """
    Tracks physical hardware presence per service:

    - exo_capture  → serial device file exists (/dev/ttyUSB0 or configured port)
    - camera_pov   → RealSense USB device detected OR preview frames arriving
    - camera_pov2  → same as camera_pov

    Emits 'device_status' via SocketIO when connection state changes.
    """

    def __init__(self, socketio: SocketIO):
        self._socketio = socketio
        self._state:    dict[str, bool] = {
            "exo_capture": False,
            "camera_pov":  False,
            "camera_pov2": False,
        }
        self._last_frame: dict[str, float] = {
            "camera_pov":  0.0,
            "camera_pov2": 0.0,
        }
        self._lock = threading.Lock()

    def touch_camera(self, key: str) -> None:
        """Call when a preview frame arrives from a camera."""
        with self._lock:
            self._last_frame[key] = time.time()

    def get_status(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._state)

    def start(self) -> None:
        threading.Thread(target=self._poll_loop, daemon=True, name="hw-monitor").start()

    # ── Internal ───────────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while True:
            time.sleep(1)
            updates = {}

            # EXO / glove: serial device file present?
            exo_connected = Path(EXO_SERIAL_PORT).exists()
            updates["exo_capture"] = exo_connected

            # Cameras: USB RealSense devices present + recent preview frames
            rs_count = self._count_realsense_devices()
            now = time.time()
            with self._lock:
                cam1_frame = (now - self._last_frame["camera_pov"])  < CAM_TIMEOUT
                cam2_frame = (now - self._last_frame["camera_pov2"]) < CAM_TIMEOUT

            # At least 1 RealSense → pov connected; 2 → both connected
            # Also accept recent frames as proof of connection
            updates["camera_pov"]  = rs_count >= 1 or cam1_frame
            updates["camera_pov2"] = rs_count >= 2 or cam2_frame

            # Emit only on state change
            with self._lock:
                for key, connected in updates.items():
                    if connected != self._state[key]:
                        self._state[key] = connected
                        self._socketio.emit("device_status", {
                            "key":       key,
                            "connected": connected,
                        })

    def _count_realsense_devices(self) -> int:
        """Count connected RealSense cameras via /sys/bus/usb/devices."""
        try:
            count = 0
            for vendor_file in glob.glob("/sys/bus/usb/devices/*/idVendor"):
                try:
                    if Path(vendor_file).read_text().strip() == REALSENSE_VENDOR:
                        count += 1
                except OSError:
                    pass
            return count
        except Exception:
            return 0


def create_app(manager: ServiceManager) -> tuple[Flask, SocketIO]:
    app = Flask(
        __name__,
        template_folder=str(TEMPLATE_DIR),
        static_folder=str(STATIC_DIR),
    )
    app.config["SECRET_KEY"] = os.environ.get("GLOVE_SECRET_KEY") or secrets.token_hex(32)
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

    hw = HardwareMonitor(socketio)
    hw.start()

    # ── Pages ──────────────────────────────────────────────────────────────────

    @app.route("/")
    def dashboard():
        return render_template("dashboard.html")

    @app.route("/setup")
    def setup():
        return render_template("setup.html")

    @app.route("/hand")
    def hand_viewer():
        return render_template("hand_viewer.html")

    # ── Task list API ──────────────────────────────────────────────────────────

    TASKS_FILE = Path(__file__).parent / "tasks.json"

    def _load_tasks() -> list[str]:
        try:
            data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
            return [str(t) for t in data if str(t).strip()]
        except Exception:
            return []

    def _save_tasks(tasks: list[str]) -> None:
        TASKS_FILE.write_text(
            json.dumps(tasks, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @app.route("/tasks", methods=["GET"])
    def get_tasks():
        return jsonify(_load_tasks())

    @app.route("/tasks", methods=["POST"])
    def add_task():
        data = request.get_json(force=True, silent=True) or {}
        name = str(data.get("name", "")).strip()
        if not name:
            return jsonify({"error": "name required"}), 400
        tasks = _load_tasks()
        if name not in tasks:
            tasks.append(name)
            _save_tasks(tasks)
        return jsonify(tasks)

    @app.route("/tasks/<path:name>", methods=["DELETE"])
    def delete_task(name: str):
        tasks = _load_tasks()
        tasks = [t for t in tasks if t != name]
        _save_tasks(tasks)
        return jsonify(tasks)

    # ── Recording API ──────────────────────────────────────────────────────────

    @app.route("/start", methods=["POST"])
    def start_recording():
        meta = request.get_json(force=True, silent=True) or {}
        meta["host_ts_start"] = time.time()
        # station_daemon expects: {"cmd": "start", "meta": {...}}
        _udp_send(DAEMON_UDP_PORT, {"cmd": "start", "meta": meta})
        socketio.emit("recording_state", {"recording": True, "meta": meta})
        return jsonify({"ok": True})

    @app.route("/stop", methods=["POST"])
    def stop_recording():
        # station_daemon expects: {"cmd": "stop", "meta": {"host_ts_end": ...}}
        _udp_send(DAEMON_UDP_PORT, {"cmd": "stop", "meta": {"host_ts_end": time.time()}})
        socketio.emit("recording_state", {"recording": False})
        return jsonify({"ok": True})

    # ── Service management API ─────────────────────────────────────────────────

    @app.route("/services", methods=["GET"])
    def list_services():
        return jsonify(manager.get_all_status())

    @app.route("/services/<key>", methods=["GET"])
    def get_service(key: str):
        svc = manager.get_status(key)
        if svc is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(svc)

    @app.route("/services/<key>/start", methods=["POST"])
    def start_service(key: str):
        ok = manager.start_service(key)
        return jsonify({"ok": ok, "service": manager.get_status(key)})

    @app.route("/services/<key>/stop", methods=["POST"])
    def stop_service(key: str):
        manager.stop_service(key)
        return jsonify({"ok": True, "service": manager.get_status(key)})

    @app.route("/services/<key>/restart", methods=["POST"])
    def restart_service(key: str):
        ok = manager.restart_service(key)
        return jsonify({"ok": ok, "service": manager.get_status(key)})

    @app.route("/services/<key>/logs", methods=["GET"])
    def get_logs(key: str):
        lines = request.args.get("lines", 100, type=int)
        return jsonify({"lines": manager.get_log_tail(key, lines)})

    @app.route("/devices", methods=["GET"])
    def device_status():
        return jsonify(hw.get_status())

    # ── User config API ───────────────────────────────────────────────────────

    @app.route("/api/config", methods=["GET"])
    def get_config():
        return jsonify(user_config.load())

    @app.route("/api/config", methods=["POST"])
    def post_config():
        data = request.get_json(force=True, silent=True) or {}
        user_config.save(data)
        restarted = []
        if "camera_pov_serial" in data:
            manager.restart_service("camera_pov")
            restarted.append("camera_pov")
        if "camera_pov2_serial" in data:
            manager.restart_service("camera_pov2")
            restarted.append("camera_pov2")
        if "exo_serial_port" in data or "exo_baud" in data:
            manager.restart_service("exo_capture")
            restarted.append("exo_capture")
        return jsonify({"ok": True, "restarted": restarted})

    # ── Sessions file browser API ──────────────────────────────────────────────

    SESSIONS_DIR = Path("/mnt/data/session")
    _SESSIONS_DIR_RESOLVED = SESSIONS_DIR.resolve()

    def _resolve_session_path(subpath: str) -> Path | None:
        """Resolve subpath inside SESSIONS_DIR. Returns None if path escapes the directory."""
        target = (SESSIONS_DIR / subpath).resolve()
        try:
            target.relative_to(_SESSIONS_DIR_RESOLVED)
            return target
        except ValueError:
            return None

    @app.route("/session", methods=["GET"])
    def list_sessions():
        subpath = request.args.get("path", "")
        base = _resolve_session_path(subpath)
        if base is None:
            return jsonify({"error": "forbidden"}), 403
        if not base.exists():
            return jsonify({"entries": [], "path": subpath, "exists": False})
        entries = []
        try:
            for item in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                stat = item.stat()
                entry = {
                    "name":  item.name,
                    "path":  str(item.relative_to(SESSIONS_DIR)),
                    "is_dir": item.is_dir(),
                    "size":  stat.st_size,
                    "mtime": stat.st_mtime,
                }
                if item.is_dir():
                    # Count files and total size inside
                    try:
                        children = list(item.rglob("*"))
                        entry["file_count"] = sum(1 for c in children if c.is_file())
                        entry["total_size"] = sum(c.stat().st_size for c in children if c.is_file())
                    except Exception:
                        entry["file_count"] = 0
                        entry["total_size"] = 0
                entries.append(entry)
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        return jsonify({"entries": entries, "path": subpath, "exists": True})

    @app.route("/session/download", methods=["GET"])
    def download_session_file():
        subpath = request.args.get("path", "")
        target = _resolve_session_path(subpath)
        if target is None:
            return jsonify({"error": "forbidden"}), 403
        if not target.is_file():
            return jsonify({"error": "not a file"}), 404
        from flask import send_file
        return send_file(target, as_attachment=True, download_name=target.name)

    @app.route("/session/preview", methods=["GET"])
    def preview_session_file():
        subpath = request.args.get("path", "")
        target = _resolve_session_path(subpath)
        if target is None:
            return jsonify({"error": "forbidden"}), 403
        if not target.is_file():
            return jsonify({"error": "not a file"}), 404
        PREVIEW_EXTS = {".json", ".jsonl", ".txt", ".log", ".csv", ".yaml", ".yml"}
        if target.suffix.lower() not in PREVIEW_EXTS:
            return jsonify({"error": "preview not supported for this file type"}), 415
        try:
            content = target.read_text(errors="replace")
            if len(content) > 200_000:
                content = content[:200_000] + "\n… (truncated)"
            return jsonify({"content": content, "name": target.name})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/session/delete", methods=["POST"])
    def delete_session_entry():
        import shutil
        data    = request.get_json(force=True, silent=True) or {}
        subpath = data.get("path", "")
        if not subpath:
            return jsonify({"error": "forbidden"}), 403
        target = _resolve_session_path(subpath)
        if target is None:
            return jsonify({"error": "forbidden"}), 403
        if not target.exists():
            return jsonify({"error": "not found"}), 404
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── SocketIO events ────────────────────────────────────────────────────────

    @socketio.on("connect")
    def on_connect():
        socketio.emit("services_snapshot", manager.get_all_status())
        socketio.emit("devices_snapshot",  hw.get_status())

    @socketio.on("request_services")
    def on_request_services():
        socketio.emit("services_snapshot", manager.get_all_status())
        socketio.emit("devices_snapshot",  hw.get_status())

    # ── Serial debug ───────────────────────────────────────────────────────────
    _serial_debug = {"active": False, "thread": None}

    @socketio.on("serial_debug_start")
    def on_serial_debug_start():
        if _serial_debug["active"]:
            return
        _serial_debug["active"] = True
        log.info("Serial debug started on %s", EXO_SERIAL_PORT)

        def _read():
            try:
                import serial as pyserial
            except ImportError:
                socketio.emit("serial_debug_line", {"line": "ERROR: pyserial not installed"})
                _serial_debug["active"] = False
                return

            try:
                ser = pyserial.Serial(EXO_SERIAL_PORT, EXO_BAUD, timeout=0.5)
            except Exception as exc:
                socketio.emit("serial_debug_line", {"line": f"ERROR opening {EXO_SERIAL_PORT}: {exc}"})
                _serial_debug["active"] = False
                return

            socketio.emit("serial_debug_line", {"line": f"[opened {EXO_SERIAL_PORT} @ {EXO_BAUD} baud]"})
            buf = b""
            while _serial_debug["active"]:
                try:
                    chunk = ser.read(256)
                    if chunk:
                        buf += chunk
                        # Emit up to 3 lines at a time
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            text = line.decode("utf-8", errors="replace").strip()
                            if text:
                                socketio.emit("serial_debug_line", {"line": text})
                        # If no newline and buffer > 80 bytes, emit raw hex
                        if len(buf) > 80:
                            socketio.emit("serial_debug_line", {
                                "line": "[binary] " + buf[:64].hex()
                            })
                            buf = buf[64:]
                except Exception as exc:
                    socketio.emit("serial_debug_line", {"line": f"[error] {exc}"})
                    break
            ser.close()
            socketio.emit("serial_debug_line", {"line": "[closed]"})
            _serial_debug["active"] = False

        t = threading.Thread(target=_read, daemon=True, name="serial-debug")
        _serial_debug["thread"] = t
        t.start()

    @socketio.on("serial_debug_stop")
    def on_serial_debug_stop():
        _serial_debug["active"] = False
        log.info("Serial debug stopped")

    # ── UDP telemetry bridges → SocketIO ──────────────────────────────────────
    # EXO hardware is detected via serial port file — no need to touch on data.
    # Camera hardware uses preview frames as fallback for frame-based detection.
    # NOTE: EXO_TELEMETRY_PORT (9002) is NOT bridged here. zmq_publisher.py owns
    # that port exclusively. sensor data reaches the dashboard via the ZMQ bridge
    # below, which subscribes to zmq_publisher on port 5555 instead.
    _start_udp_bridge(socketio, ZMQ_STATUS_PORT, "zmq_status")
    _start_preview_bridge(socketio, POV_PREVIEW_RGB,  "pov_preview_rgb",
                          on_data=lambda: hw.touch_camera("camera_pov"))
    _start_preview_bridge(socketio, POV2_PREVIEW_RGB, "pov2_preview_rgb",
                          on_data=lambda: hw.touch_camera("camera_pov2"))

    # ZMQ sensor bridge → SocketIO "hand_frame"
    # Subscribes to zmq_publisher (port 5555) so we don't compete with it
    # for the shared UDP 9002 port (SO_REUSEPORT only delivers to one socket).
    _start_zmq_sensor_bridge(socketio)

    return app, socketio


# ── Helpers ────────────────────────────────────────────────────────────────────

def _udp_send(port: int, payload: dict, host: str = "127.0.0.1") -> None:
    try:
        data = json.dumps(payload).encode()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(data, (host, port))
    except Exception as exc:
        log.warning("UDP send to port %d failed: %s", port, exc)


def _start_udp_bridge(
    socketio: SocketIO,
    port: int,
    event: str,
    bufsize: int = 65536,
    on_data=None,
) -> None:
    def _run():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.settimeout(1.0)
        try:
            sock.bind(("0.0.0.0", port))
            log.info("UDP bridge listening on port %d → event '%s'", port, event)
        except OSError as exc:
            log.warning("Could not bind UDP port %d: %s", port, exc)
            return
        while True:
            try:
                data, _ = sock.recvfrom(bufsize)
                payload = json.loads(data)
                socketio.emit(event, payload)
                if on_data:
                    on_data()
            except socket.timeout:
                continue
            except Exception as exc:
                log.debug("UDP bridge %d error: %s", port, exc)

    threading.Thread(target=_run, daemon=True, name=f"udp-bridge-{port}").start()


def _start_preview_bridge(
    socketio: SocketIO,
    port: int,
    event: str,
    on_data=None,
) -> None:
    import base64

    def _run():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.settimeout(1.0)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError as exc:
            log.warning("Could not bind preview port %d: %s", port, exc)
            return
        while True:
            try:
                data, _ = sock.recvfrom(65536)
                b64 = base64.b64encode(data).decode()
                socketio.emit(event, {"data": b64})
                if on_data:
                    on_data()
            except socket.timeout:
                continue
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True, name=f"preview-{port}").start()


def _start_zmq_sensor_bridge(socketio: SocketIO) -> None:
    """
    Subscribe to ZMQ 'sensor' topic (zmq_publisher port 5555) and forward frames
    via SocketIO as both 'hand_frame' and 'exo_telemetry'.

    This is the ONLY path for sensor data into the dashboard. server.py does NOT
    bind UDP 9002 — zmq_publisher owns that port exclusively so it receives every
    packet. We then subscribe to zmq_publisher as a normal ZMQ consumer.
    """
    def _run():
        try:
            import zmq as _zmq
        except ImportError:
            log.warning("pyzmq not installed — hand viewer will have no sensor data")
            return

        import time as _time

        ctx = _zmq.Context()
        sub = ctx.socket(_zmq.SUB)
        sub.connect("tcp://127.0.0.1:5555")
        sub.setsockopt_string(_zmq.SUBSCRIBE, "sensor")
        sub.setsockopt(_zmq.RCVTIMEO, 500)
        log.info("ZMQ sensor bridge connected (port 5555) → SocketIO 'hand_frame' + 'exo_telemetry'")

        frame_count = 0
        byte_count = 0
        dropped = 0
        window_start = _time.monotonic()

        while True:
            try:
                _topic, data = sub.recv_multipart()
                payload = json.loads(data)
                socketio.emit("hand_frame", payload)

                frame_count += 1
                byte_count += len(data)

                now = _time.monotonic()
                elapsed = now - window_start
                if elapsed >= 1.0:
                    hz = round(frame_count / elapsed, 1)
                    kb_s = round(byte_count / elapsed / 1024, 1)
                    socketio.emit("exo_telemetry", {
                        "hz":     hz,
                        "kb_s":   kb_s,
                        "frames": frame_count,
                        "dropped": dropped,
                    })
                    frame_count = 0
                    byte_count = 0
                    window_start = now

            except _zmq.Again:
                now = _time.monotonic()
                if now - window_start >= 1.0:
                    socketio.emit("exo_telemetry", {"hz": 0, "kb_s": 0, "frames": 0, "dropped": 0})
                    frame_count = 0
                    byte_count = 0
                    window_start = now
            except Exception as exc:
                log.debug("ZMQ sensor bridge error: %s", exc)

    threading.Thread(target=_run, daemon=True, name="zmq-sensor-bridge").start()
