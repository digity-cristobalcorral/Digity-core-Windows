#!/usr/bin/env python3
# Dashboard control daemon (START/STOP fan-out + EXO RAW launcher)
#
# Purpose:
# - Receives START/STOP commands over UDP:5005 from a dashboard.
# - Sends unified "record_start"/"record_stop" control payload to all producers:
#     * POV camera control (UDP)
#     * SCENE camera control (UDP)
#     * SCENE2 camera control (UDP)
#     * PUPILS control (UDP)
#     * EXO RAW control (UDP)
# - Launches/stops exo_capture_raw.py as a child process (the serial gateway recorder).
#
# Key point:
# - Generate ONE host_ts_start per session and inject it into every record_start payload.

import socket
import subprocess
import signal
import json
import platform
import threading
import os
import time
import sys
import tempfile
from pathlib import Path

# ---------- Configuration ----------

UDP_PORT = 5005

_HERE     = Path(__file__).parent
_ROOT     = _HERE.parent
LOCK_ROOT = str(_ROOT / "tmp" / "locks")

import sys as _sys_; _sys_.path.insert(0, str(_ROOT))
from core.platform_helpers import get_default_serial_port as _default_serial_port
from core.platform_helpers import get_default_data_dir as _default_data_dir
os.makedirs(LOCK_ROOT, exist_ok=True)

# Camera-like producers
CAMERA_CTRL = {
    "pov":    {"host": "127.0.0.1", "port": 9050},
    "scene":  {"host": "127.0.0.1", "port": 9051},
    "scene2": {"host": "127.0.0.1", "port": 9054},
    "pupils": {"host": "127.0.0.1", "port": 9053},
}

# EXO control endpoint (exo_capture_raw listens here)
EXO_CTRL = {"host": "127.0.0.1", "port": 9052}

# Path to the EXO producer script (relative to project root)
EXO_SCRIPT = str(_ROOT / "producer" / "exo_capture.py")

# Defaults for EXO RAW
# - serial/baud MUST match your gateway USB serial settings
DEFAULT_EXO = {
    "serial": _default_serial_port(),
    "baud": "921600",
    "timeout": "0.2",
    "base_dir": str(_default_data_dir()),
    # optional performance knobs
    "chunk": "65536",
    "file_buffer_mb": "4",
    "fsync_every_bytes": "0",
    # optional UDP forward (normally OFF)
    # "udp_forward": "0",
    # "udp_ip": "127.0.0.1",
    # "udp_port": "9001",
}

DEFAULT_META = {
    "station_id": "station1",
    "session_id": "s1",
    "user_id": "anon",
    "task_type": "default",
    "hand": "right",
}

# Whitelisted EXO params accepted from dashboard meta
# (These become CLI args for exo_capture_raw.py)
ALLOWED_EXO_PARAMS = {
    "serial", "baud", "timeout",
    "base_dir",
    "chunk", "file_buffer_mb", "fsync_every_bytes",
    "user_id", "session_id", "task_type", "station_id", "hand",
}

# ---------- Runtime state ----------

_state_lock = threading.Lock()
exo_proc = None
current_meta = {}
CURRENT_HOST_TS_START = None
CURRENT_HOST_TS_END = None

# ---------- Helper functions ----------

def send_camera_ctrl(name: str, payload: dict, timeout=0.05):
    entry = CAMERA_CTRL[name]
    host, port = entry["host"], entry["port"]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(json.dumps(payload).encode("utf-8"), (host, port))
        print(f"[CTRL→{name}@{host}:{port}] {payload}")
    except Exception as e:
        print(f"[CTRL→{name}] send error: {e}")
    finally:
        s.close()

def send_exo_ctrl(payload: dict, timeout=0.05):
    host, port = EXO_CTRL["host"], EXO_CTRL["port"]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(json.dumps(payload).encode("utf-8"), (host, port))
        print(f"[CTRL→exo@{host}:{port}] {payload}")
    except Exception as e:
        print(f"[CTRL→exo] send error: {e}")
    finally:
        s.close()

def make_lock(station_id, session_id, user_id=None):
    suffix = f"{station_id}__{session_id}" + (f"__{user_id}" if user_id else "")
    path = os.path.join(LOCK_ROOT, f"{suffix}.lock")
    try:
        with open(path, "w") as f:
            f.write("locked\n")
        print(f"[LOCK] created {path}")
    except Exception as e:
        print(f"[LOCK] create error: {e}")

def remove_lock(station_id, session_id, user_id=None):
    suffix = f"{station_id}__{session_id}" + (f"__{user_id}" if user_id else "")
    path = os.path.join(LOCK_ROOT, f"{suffix}.lock")
    try:
        os.remove(path)
        print(f"[LOCK] removed {path}")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[LOCK] remove error: {e}")

def stream_output(prefix: str, proc: subprocess.Popen):
    try:
        for line in proc.stdout:
            print(f"{prefix}:", line.rstrip())
    except Exception:
        pass

def build_exo_args(meta: dict):
    """
    Build CLI args for exo_capture_raw.py.

    We merge:
      DEFAULT_EXO + dashboard meta overrides (allowed keys only)

    Note:
    - exo_capture_raw.py already receives record_start/record_stop over UDP:9052.
    - We launch it once, and then we just send control packets.
    """
    merged = {}
    merged.update(DEFAULT_EXO)

    # Accept overrides from dashboard meta
    for k in ALLOWED_EXO_PARAMS:
        if k in meta:
            merged[k] = str(meta[k])

    args = [sys.executable, EXO_SCRIPT]

    # Map known keys to CLI flags of exo_capture_raw.py
    # (We only pass what that script understands)
    if "serial" in merged: args += ["--serial", merged["serial"]]
    if "baud" in merged: args += ["--baud", merged["baud"]]
    if "timeout" in merged: args += ["--timeout", merged["timeout"]]
    if "base_dir" in merged: args += ["--base_dir", merged["base_dir"]]

    if "chunk" in merged: args += ["--chunk", merged["chunk"]]
    if "file_buffer_mb" in merged: args += ["--file_buffer_mb", merged["file_buffer_mb"]]
    if "fsync_every_bytes" in merged: args += ["--fsync_every_bytes", merged["fsync_every_bytes"]]


    return args, merged

def start_exo_producer(meta: dict):
    """
    Launch exo_capture_raw.py. If already running, no-op.
    """
    global exo_proc
    if exo_proc is not None:
        print("[EXO] already running")
        return

    args, merged = build_exo_args(meta)
    print(f"[EXO] launching: {' '.join(args)}")

    # Optional: also pass meta via env (not required, but can be useful for logs)
    env = os.environ.copy()
    env["EXO_USER_ID"] = str(meta.get("user_id", ""))
    env["EXO_SESSION_ID"] = str(meta.get("session_id", ""))
    env["EXO_TASK_TYPE"] = str(meta.get("task_type", ""))
    env["EXO_HAND"] = str(meta.get("hand", ""))
    env["EXO_STATION_ID"] = str(meta.get("station_id", ""))

    try:
        exo_proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        threading.Thread(target=stream_output, args=("[exo_raw]", exo_proc), daemon=True).start()
    except Exception as e:
        print(f"[EXO] start error: {e}")
        exo_proc = None

def stop_exo_producer():
    """
    Stop exo_capture_raw.py gracefully; fallback to kill.
    """
    global exo_proc
    if exo_proc is None:
        return
    print("[EXO] stopping...")
    try:
        if platform.system() == "Windows":
            exo_proc.terminate()
        else:
            exo_proc.send_signal(signal.SIGINT)
        exo_proc.wait(timeout=10)
    except Exception as e:
        print(f"[EXO] stop error: {e}")
        try:
            exo_proc.kill()
        except Exception:
            pass
    finally:
        exo_proc = None
        print("[EXO] stopped.")

# ---------- Main loop ----------

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", UDP_PORT))
print(f"Listening for commands on UDP port {UDP_PORT}...")

try:
    while True:
        try:
            data, _ = sock.recvfrom(65535)
            cmd_obj = json.loads(data.decode("utf-8", errors="ignore"))

            cmd = cmd_obj.get("cmd")
            meta_in = cmd_obj.get("meta", {}) or {}
            meta = dict(DEFAULT_META)
            meta.update(meta_in)

            if cmd == "start":
                with _state_lock:
                    try:
                        CURRENT_HOST_TS_START = float(meta.get("host_ts_start", time.time()))
                    except Exception:
                        CURRENT_HOST_TS_START = time.time()
                    current_meta = meta

                make_lock(meta.get("station_id"), meta.get("session_id"), meta.get("user_id"))

                # EXO runs as persistent service — just send it the UDP command below.

                # Send record_start to all producers
                start_payload = {
                    "cmd": "record_start",
                    **meta,
                    "host_ts_start": CURRENT_HOST_TS_START,
                }
                for name in ("pov", "scene", "scene2", "pupils"):
                    send_camera_ctrl(name, start_payload)
                send_exo_ctrl(start_payload)

                print("Recording started.")

            elif cmd == "stop":
                with _state_lock:
                    try:
                        CURRENT_HOST_TS_END = float(meta.get("host_ts_end", time.time()))
                    except Exception:
                        CURRENT_HOST_TS_END = time.time()
                    snap_meta = dict(current_meta)
                    snap_ts_start = CURRENT_HOST_TS_START
                    snap_ts_end = CURRENT_HOST_TS_END

                stop_payload = {
                    "cmd": "record_stop",
                    "host_ts_end": snap_ts_end,
                    "host_ts_start": snap_ts_start,
                    **snap_meta,
                }
                for name in ("pov", "scene", "scene2", "pupils"):
                    send_camera_ctrl(name, stop_payload)
                send_exo_ctrl(stop_payload)

                # EXO runs as persistent service — do not stop its process.

                sid = snap_meta.get("session_id", DEFAULT_META["session_id"])
                st  = snap_meta.get("station_id", DEFAULT_META["station_id"])
                uid = snap_meta.get("user_id", DEFAULT_META["user_id"])
                remove_lock(st, sid, uid)

                with _state_lock:
                    CURRENT_HOST_TS_START = None
                    CURRENT_HOST_TS_END = None
                    current_meta = {}

                print("Recording stopped.")

            else:
                print(f"[WARN] Unknown cmd: {cmd}")

        except KeyboardInterrupt:
            print("\n[INFO] Ctrl+C received → stopping everything...")
            try:
                stop_payload = {"cmd": "record_stop"}
                for name in CAMERA_CTRL:
                    send_camera_ctrl(name, stop_payload)
                send_exo_ctrl(stop_payload)
                stop_exo_producer()
            finally:
                break

        except Exception as e:
            print("Error in daemon:", e)

finally:
    try:
        sock.close()
    except Exception:
        pass
