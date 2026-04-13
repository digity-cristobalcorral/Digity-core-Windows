#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
exo_capture_raw.py  (Local recorder, RAW binary, controlled by UDP)

What it does
- Reads raw bytes from the ESP32 gateway via USB serial (no parsing).
- Keeps reading continuously to avoid serial buffer overruns.
- When RECORDING is ON (UDP control), writes *exactly* those bytes to:
    .../session/<user>_<session>_<station>/sensors/stream.raw

Additionally (recommended for UI / debugging)
- Sends lightweight JSON telemetry to UDP 9002 (or configured port):
    rx_bytes, avg_rx_kbps, recording flag, output path, host_ts_start, meta

Control protocol (UDP JSON on CONTROL_PORT):
  {"cmd":"record_start", "user_id":"u1", "session_id":"...", "task_type":"...", "station_id":"...", "host_ts_start": 123.4}
  {"cmd":"record_stop",  "host_ts_end": 456.7}

Notes
- stream.raw contains ONLY the raw serial bytes (no timestamps inserted inside).
- Synchronisation is achieved via host_ts_start stored externally (telemetry + info file).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

# Make core/ importable regardless of cwd
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from core import humi_protocol
    _PARSER_OK = True
except Exception as _e:
    print(f"[WARN] humi_protocol not available, sensor parsing disabled: {_e}")
    _PARSER_OK = False

try:
    from serial import Serial, SerialException  # PySerial
except Exception as e:
    print(f"[FATAL] Could not import PySerial (Serial/SerialException). Error: {e}")
    print("Fix: pip install pyserial and ensure there is no local 'serial.py' shadowing it.")
    raise


# -------------------- CLI --------------------
p = argparse.ArgumentParser()
p.add_argument("--serial", default="/dev/ttyUSB0")
p.add_argument("--baud", type=int, default=921600)
p.add_argument("--timeout", type=float, default=0.2, help="Serial read timeout (seconds)")

# Metadata defaults (may be overridden by meta_file / meta_json / env / control packets)
p.add_argument("--user_id", default="anon")
p.add_argument("--session_id", default="s1")  # may be replaced at record_start if not provided
p.add_argument("--task_type", default="default")
p.add_argument("--station_id", default="station1")

# Optional metadata overrides
p.add_argument("--meta_file", default=None, help="Path to JSON with metadata overrides")
p.add_argument("--meta_json", default=None, help="Inline JSON string with metadata overrides")

# Control channel
p.add_argument("--control_host", default="127.0.0.1")
p.add_argument("--control_port", type=int, default=9052)

# Storage base
p.add_argument("--base_dir", default="/mnt/data", help="Base directory for session storage (default: /mnt/data)")

# RAW capture tuning
p.add_argument("--chunk", type=int, default=65536, help="Serial read size per call")
p.add_argument("--file_buffer_mb", type=int, default=4, help="File buffer size (MB)")

# Optional durability (slower)
p.add_argument("--fsync_every_bytes", type=int, default=102400,
               help="flush+fsync every N bytes (0 disables). Default 100KB.")


# Recommended: JSON telemetry for UI
p.add_argument("--telemetry_udp_ip", default="127.0.0.1")
p.add_argument("--telemetry_udp_port", type=int, default=9002, help="JSON telemetry port for Flask/UI")
p.add_argument("--telemetry_every_s", type=float, default=0.25, help="How often to send telemetry JSON")

args = p.parse_args()

print("[ARGS] argv:", " ".join(sys.argv))
print("[ARGS] parsed:", {k: getattr(args, k) for k in vars(args) if k not in {"meta_json"}})


# -------------------- Overrides --------------------
def _merge_overrides(a):
    # meta_file
    if a.meta_file:
        try:
            with open(a.meta_file, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for k, v in cfg.items():
                if hasattr(a, k) and v is not None:
                    setattr(a, k, v)
            print(f"[META] merged from file {a.meta_file} -> keys={list(cfg.keys())}")
        except Exception as e:
            print(f"[META] meta_file error: {e}")

    # meta_json
    if a.meta_json:
        try:
            cfg = json.loads(a.meta_json)
            for k, v in cfg.items():
                if hasattr(a, k) and v is not None:
                    setattr(a, k, v)
            print(f"[META] merged from inline JSON -> keys={list(cfg.keys())}")
        except Exception as e:
            print(f"[META] meta_json error: {e}")

    # env vars
    env_map = {
        "EXO_USER_ID": "user_id",
        "EXO_SESSION_ID": "session_id",
        "EXO_TASK_TYPE": "task_type",
        "EXO_STATION_ID": "station_id",
        "EXO_BASE_DIR": "base_dir",
        "EXO_SERIAL": "serial",
        "EXO_BAUD": "baud",
    }
    for env_key, arg_key in env_map.items():
        if env_key in os.environ:
            val = os.environ[env_key]
            if arg_key == "baud":
                try:
                    setattr(a, arg_key, int(val))
                except Exception:
                    pass
            else:
                setattr(a, arg_key, val)

    return a


args = _merge_overrides(args)

print("[META] effective:", {
    "user_id": args.user_id,
    "session_id": args.session_id,
    "task_type": args.task_type,
    "station_id": args.station_id,
    "base_dir": args.base_dir,
})


# -------------------- Runtime state --------------------
_shutdown = threading.Event()
_state_lock = threading.Lock()

RECORDING = False
HOST_TS_START: Optional[float] = None
HOST_TS_END: Optional[float] = None

REC_META: Dict[str, str] = {
    "user_id": str(args.user_id),
    "session_id": str(args.session_id),
    "task_type": str(args.task_type),
    "station_id": str(args.station_id),
}

BASE_DIR = args.base_dir
SESSION_ROOT = os.path.join(BASE_DIR, "session")
os.makedirs(SESSION_ROOT, exist_ok=True)

CURRENT_SESSION: Optional[Dict[str, str]] = None

RAW_FH = None
RAW_PATH: Optional[str] = None

_since_fsync = 0


# Telemetry JSON socket (for Flask/UI and ZMQ publisher)
tele_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# Streaming parse buffer (accumulates serial bytes between reads)
_parse_buf = b""


# -------------------- Helpers --------------------
def _session_id_local_now() -> str:
    now = datetime.now()
    mmm = f"{int(now.microsecond/1000):03d}"
    return now.strftime("%Y_%m_%dT%H_%M_%S_") + mmm


def ensure_dir(pth: str):
    os.makedirs(pth, exist_ok=True)


def ensure_session_layout(user_id: str, session_id: str, station_id: str) -> dict:
    """
    Create session layout:

    /mnt/data/session/<user>_<session>_<station>/
      sensors/
      info/

    NOTE:
    - EXO raw is written directly into sensors/ (no EXO subfolder).
    """
    name = f"{user_id}_{session_id}"
    if station_id:
        name += f"_{station_id}"

    session_dir = os.path.join(SESSION_ROOT, name)
    sensors_dir = os.path.join(session_dir, "sensors")
    info_dir = os.path.join(session_dir, "info")

    for d in (session_dir, sensors_dir, info_dir):
        ensure_dir(d)

    return {"dir": session_dir, "sensors_dir": sensors_dir, "info_dir": info_dir}


def _write_exo_info_json(session: dict):
    """
    Writes a small info file so post-processing can sync streams.
    This is OUTSIDE stream.raw (stream.raw stays pure bytes).
    """
    info_path = os.path.join(session["info_dir"], "exo_info.json")
    payload = {
        "host_ts_start": HOST_TS_START,
        "host_ts_end": HOST_TS_END,
        "rec_meta": dict(REC_META),
        "raw_path": RAW_PATH,
    }
    try:
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def open_raw_file(session: dict) -> str:
    """
    Open RAW file in:
      .../sensors/stream.raw

    Open in append mode for crash-safety.
    Use large buffering for throughput.
    Closes any previously open handle before opening a new one.
    """
    global RAW_FH, _since_fsync
    _close_raw_file()  # ensure previous handle is released
    _since_fsync = 0
    buf_bytes = max(1, int(args.file_buffer_mb)) * 1024 * 1024
    raw_path = os.path.join(session["sensors_dir"], "stream.raw")
    RAW_FH = open(raw_path, "ab", buffering=buf_bytes)
    return raw_path


def _close_raw_file():
    global RAW_FH, RAW_PATH
    try:
        if RAW_FH:
            RAW_FH.flush()
            RAW_FH.close()
    except Exception:
        pass
    RAW_FH = None
    RAW_PATH = None


def _start_record(meta_from_ctrl: Optional[Dict[str, Any]]):
    """
    Start recording:
    - Merge metadata
    - Set HOST_TS_START from control (shared with cameras)
    - Create session folder
    - Open stream.raw
    - Write info/exo_info.json for sync
    """
    global RECORDING, HOST_TS_START, HOST_TS_END, REC_META, CURRENT_SESSION, RAW_PATH

    with _state_lock:
        HOST_TS_END = None

        # Merge incoming meta
        if meta_from_ctrl:
            for k in ("user_id", "session_id", "task_type", "station_id"):
                v = meta_from_ctrl.get(k)
                if v is not None:
                    REC_META[k] = str(v)

        # If session_id not provided -> generate
        if (not meta_from_ctrl) or ("session_id" not in meta_from_ctrl):
            REC_META["session_id"] = _session_id_local_now()

        # Start timestamp (should come from station daemon)
        try:
            HOST_TS_START = float((meta_from_ctrl or {}).get("host_ts_start", time.time()))
        except Exception:
            HOST_TS_START = time.time()

        CURRENT_SESSION = ensure_session_layout(
            REC_META["user_id"], REC_META["session_id"], REC_META["station_id"]
        )

        try:
            RAW_PATH = open_raw_file(CURRENT_SESSION)
            RECORDING = True
            _write_exo_info_json(CURRENT_SESSION)
            print(f"[REC] START  session_dir={CURRENT_SESSION['dir']}")
            print(f"[REC] RAW    path={RAW_PATH}")
        except Exception as e:
            RECORDING = False
            _close_raw_file()
            print(f"[REC] ERROR could not open raw file: {e}")


def _stop_record(host_ts_end: Optional[float] = None):
    """
    Stop recording:
    - Set HOST_TS_END
    - Update info/exo_info.json
    - Close stream.raw
    """
    global RECORDING, HOST_TS_END

    with _state_lock:
        try:
            HOST_TS_END = float(host_ts_end) if host_ts_end is not None else time.time()
        except Exception:
            HOST_TS_END = time.time()

        if RECORDING:
            print(f"[REC] STOP   host_ts_end={HOST_TS_END}")

        # Write/update info file before closing
        if CURRENT_SESSION is not None:
            _write_exo_info_json(CURRENT_SESSION)

        RECORDING = False
        _close_raw_file()


def _send_telemetry(now: float, total_rx: int, avg_rate_kbps: float):
    """
    Send lightweight JSON telemetry for UI.
    """
    with _state_lock:
        rec = RECORDING
        out = RAW_PATH
        meta = dict(REC_META)
        hstart = HOST_TS_START
        hend = HOST_TS_END

    msg = {
        "type": "exo_raw_telemetry",
        "host_ts": now,
        "rx_bytes": total_rx,
        "avg_rx_kbps": avg_rate_kbps,
        "recording": rec,
        "out": out,
        "host_ts_start": hstart,
        "host_ts_end": hend,
        "meta": meta,
    }
    try:
        tele_sock.sendto(
            json.dumps(msg).encode("utf-8"),
            (args.telemetry_udp_ip, args.telemetry_udp_port),
        )
    except Exception:
        pass


def _send_sensor_frame(frame: dict) -> None:
    """Forward a parsed HUMI sensor frame to the telemetry UDP port (picked up by zmq_publisher)."""
    try:
        msg = {"type": "sensor_frame", "frame": frame}
        tele_sock.sendto(
            json.dumps(msg).encode("utf-8"),
            (args.telemetry_udp_ip, args.telemetry_udp_port),
        )
    except Exception:
        pass


def control_server():
    """
    UDP JSON control receiver.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((args.control_host, args.control_port))
    except Exception as e:
        print(f"[CTRL] bind error udp://{args.control_host}:{args.control_port}: {e}")
        return

    s.settimeout(0.2)
    print(f"[CTRL] listening udp://{args.control_host}:{args.control_port}")

    while not _shutdown.is_set():
        try:
            data, _ = s.recvfrom(16_384)
        except socket.timeout:
            continue
        except Exception:
            if _shutdown.is_set():
                break
            continue

        try:
            cmd = json.loads(data.decode("utf-8", errors="ignore"))
        except Exception:
            continue

        if cmd.get("cmd") == "record_start":
            meta = dict(cmd)
            if "meta" in cmd and isinstance(cmd["meta"], dict):
                meta.update(cmd["meta"])
            _start_record(meta_from_ctrl=meta)

        elif cmd.get("cmd") == "record_stop":
            _stop_record(host_ts_end=cmd.get("host_ts_end"))

    try:
        s.close()
    except Exception:
        pass


def serial_loop():
    """
    Read raw bytes from serial forever.
    - Always read (avoid overruns).
    - If recording -> write bytes to stream.raw
    - Always send JSON telemetry to UDP 9002 (configurable)
    - Parse HUMI binary frames in real-time and forward to UDP (→ ZMQ)
    """
    global _since_fsync, _parse_buf

    ser: Optional[Serial] = None
    t0 = time.time()
    total_rx = 0

    last_tele = 0.0

    while not _shutdown.is_set():
        if ser is None:
            try:
                ser = Serial(args.serial, args.baud, timeout=args.timeout)
                print(f"[SER] connected {args.serial} @ {args.baud}")
            except SerialException:
                print(f"[SER] not available: {args.serial}. retrying in 2s...")
                time.sleep(2.0)
                continue
            except Exception as e:
                print(f"[SER] open error: {e}. retrying in 2s...")
                time.sleep(2.0)
                continue

        try:
            data = ser.read(args.chunk)
            if data:
                total_rx += len(data)

                # Write to disk only when recording
                with _state_lock:
                    if RECORDING and RAW_FH is not None:
                        RAW_FH.write(data)
                        _since_fsync += len(data)

                        if args.fsync_every_bytes > 0 and _since_fsync >= args.fsync_every_bytes:
                            RAW_FH.flush()
                            try:
                                os.fsync(RAW_FH.fileno())
                            except Exception:
                                pass
                            _since_fsync = 0

                # Parse HUMI binary frames and forward via UDP → ZMQ
                if _PARSER_OK:
                    _parse_buf += data
                    try:
                        frames, _parse_buf = humi_protocol.parse_stream(_parse_buf)
                        for f in frames:
                            _send_sensor_frame(f)
                    except Exception:
                        pass
                    # Bound buffer: drop oldest bytes if no sync found for >8KB
                    if len(_parse_buf) > 8192:
                        print(f"[WARN] parse buffer overflow ({len(_parse_buf)} bytes), dropping oldest 4KB — possible framing error")
                        _parse_buf = _parse_buf[-4096:]

            now = time.time()
            if (now - last_tele) >= max(0.05, args.telemetry_every_s):
                avg_rate = (total_rx / (now - t0)) if (now - t0) > 0 else 0.0
                _send_telemetry(now, total_rx, avg_rate / 1024.0)
                last_tele = now

        except SerialException as e:
            # Common causes:
            # - device unplugged / reset
            # - another process opened the same port
            print(f"[SER] error: {e}. reconnecting...")
            try:
                if ser:
                    ser.close()
            except Exception:
                pass
            ser = None
            time.sleep(1.0)

        except Exception as e:
            print(f"[SER] unexpected: {e}")
            time.sleep(0.02)

    try:
        if ser:
            ser.close()
    except Exception:
        pass


def on_sigint(sig, frame):
    print("\n[SYS] stopping...")
    _shutdown.set()
    try:
        _stop_record()
    finally:
        sys.exit(0)


signal.signal(signal.SIGINT, on_sigint)


def main():
    threading.Thread(target=control_server, daemon=True).start()
    serial_loop()


if __name__ == "__main__":
    main()
