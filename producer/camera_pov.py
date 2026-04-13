#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
POV RealSense D435i local recorder (COLOR, DEPTH, POV IMU)

This script replaces Kafka with local disk storage, using (almost) the same
directory layout and JSON sidecars as the batch consumer:

BASE_DIR = /home/digity/didacProject/data_kafka

For a given recording with:
  user_id    = "u1"
  session_id = "session1"
  station_id = "station1"

The session directory becomes:
  /home/digity/didacProject/data_kafka/session/u1_session1_station1/

Inside:

  u1_session1_station1/
    frames/
      pov/
        <ts>_pov_rgb.png
        <ts>_pov_depth.png
        <ts>_pov.json        # sidecar pairing rgb + depth (like consumer)
    sensors/
      POVIMU/
        accel.csv            # host_ts,t_ms,x,y,z,n
        gyro.csv             # host_ts,t_ms,x,y,z,n
    info/
      session_meta.json

Recording control via UDP JSON commands on CONTROL_PORT:

  # Start recording
  {"cmd": "record_start", "user_id": "u1", "session_id": "session1",
   "task_type": "grab", "station_id": "station1", "host_ts_start": 123456.0}

  # Stop recording
  {"cmd": "record_stop", "host_ts_end": 123789.0}

If "session_id" is omitted, a timestamp-based ID is generated.
"""

import os
import csv
import json
import time
import socket
from threading import Event, Thread, Lock
from collections import deque
from queue import Queue, Full, Empty
from datetime import datetime

import numpy as np
import cv2
import pyrealsense2 as rs

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

# RealSense device serial — read from ~/.glove/config.json (editable via Setup UI)
import sys as _sys; _sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))
from core.user_config import load as _load_cfg
DEVICE_SERIAL = _load_cfg().get("camera_pov_serial", "843112072148")
# Capture resolutions / fps
CAP_COLOR_W, CAP_COLOR_H, CAP_FPS = 1280, 720, 30
CAP_DEPTH_W, CAP_DEPTH_H          = 1280, 720

# Preview sizes (UDP HUD)
VIEW_COLOR_W, VIEW_COLOR_H = 950, 540
VIEW_DEPTH_W, VIEW_DEPTH_H = 848, 480

# Disable preview while recording to maximize CPU for PNG encoding
DISABLE_PREVIEW_WHEN_RECORDING = True

# Base directory where all sessions are stored (matching consumer layout)
BASE_DIR = "/mnt/data"
SESSION_ROOT = os.path.join(BASE_DIR, "session")
os.makedirs(SESSION_ROOT, exist_ok=True)

# UDP configuration
UDP_IP = "127.0.0.1"
VIEW_RGB_PORT   = 9013
VIEW_DEPTH_PORT = 9014
RAW_IMU_PORT    = 9015  # reserved (not used here)
VIEW_IMU_PORT   = 9016  # reserved (not used here)
CONTROL_HOST    = "127.0.0.1"
CONTROL_PORT    = 9050
UDP_MAX         = 65507

sock_view_rgb   = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock_view_depth = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock_raw_imu    = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock_view_imu   = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

ENABLE_IMU = False  # this POV camera has no motion module → do not create POVIMU or IMU CSVs


# ---------------------------------------------------------------------
# Global recording/session state
# ---------------------------------------------------------------------

RECORDING      = False
HOST_TS_START  = None
HOST_TS_END    = None

# Metadata for the current recording (mirrors producer headers)
REC_META = {
    "user_id": "",
    "session_id": "",
    "task_type": "",
    "station_id": "station1",
}

# Current session structure (paths)
CURRENT_SESSION = None  # dict returned by ensure_session_layout()

# Sequence number for frames (increments per COLOR+DEPTH pair)
FRAME_SEQ = 0

# ---------------------------------------------------------------------
# RealSense frame buffer & queues
# ---------------------------------------------------------------------

# Frameset buffer from RealSense callback
q_frames = deque(maxlen=2048)

# Disk queue:
# Each item = (session, stream, img_ndarray, host_ts, seq, meta)
DISK_Q = Queue(maxsize=16384)
disk_workers = []
disk_stop_evt = Event()

# Stats & counters
DROPPED_DEQUE  = 0
DROPPED_DISKQ  = 0
DROPPED_VIEW   = 0

frames_enqueued_in_session = 0
frames_sent_in_session     = 0
frames_in_session          = 0

# ---------------------------------------------------------------------
# IMU handling (CSV writer via worker)
# ---------------------------------------------------------------------

# Queue item: (ts_host, sensor, (x,y,z))
IMU_Q = Queue(maxsize=32768)
DROPPED_IMU_Q = 0

latest_gyro  = None
latest_accel = None

imu_in_gyro_cnt   = 0
imu_in_accel_cnt  = 0
imu_in_last_print = time.time()

# CSV file handles + writers for current session
accel_csv_file  = None
gyro_csv_file   = None
accel_writer    = None
gyro_writer     = None
imu_csv_lock    = Lock()

# ---------------------------------------------------------------------
# Pipeline / hotplug globals
# ---------------------------------------------------------------------

last_frame_ts   = 0.0
HOTPLUG_RESTART = Event()
RESTART_LOCK    = Lock()
PIPE            = None

# Global stop flag for all threads
stop_evt = Event()

# ---------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------

def ensure_dir(path: str):
    """Create directory if it does not exist."""
    os.makedirs(path, exist_ok=True)

def sanitize_ts_for_name(ts_val) -> str:
    """
    Convert a timestamp float to a filesystem-safe string (similar to consumer).
    Example: 1732461536.123456 -> "1732461536-123456"
    """
    if isinstance(ts_val, (int, float)):
        s = f"{float(ts_val):.6f}"
    else:
        s = str(ts_val)
    return s.replace(":", "-").replace(".", "-")

def rel_to_session_name(path: str, session: dict) -> str:
    """
    Return path relative to session folder (for JSON sidecars),
    to match consumer's relative paths.
    """
    if not path:
        return ""
    try:
        rel_inside = os.path.relpath(path, start=session["dir"])
        sess_name = os.path.basename(session["dir"])
        return os.path.join(sess_name, rel_inside).replace("\\", "/")
    except Exception:
        return path

def write_json(path: str, obj: dict):
    """Write a JSON file with UTF-8 encoding and indentation."""
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

# ---------------------------------------------------------------------
# Session layout (aligned with consumer's ensure_session)
# ---------------------------------------------------------------------

def ensure_session_layout(user_id: str, session_id: str, station_id: str) -> dict:
    """
    Create and return the session directory structure.

    Layout (IMU disabled for this POV camera):

      BASE_DIR/session/<user>_<session>_<station>/
        frames/
          pov/
        info/

    If ENABLE_IMU is set to True, we additionally create:

        sensors/
          POVIMU/
    """
    name = f"{user_id}_{session_id}"
    if station_id:
        name += f"_{station_id}"

    session_dir = os.path.join(SESSION_ROOT, name)

    # frames/pov
    frames_dir  = os.path.join(session_dir, "frames")
    pov_dir     = os.path.join(frames_dir, "pov")

    # info/
    info_dir    = os.path.join(session_dir, "info")

    # Always create frames + info
    for d in (frames_dir, pov_dir, info_dir):
        ensure_dir(d)

    # Only create sensors/POVIMU if IMU is enabled
    sensors_dir = None
    imu_dirs = {}

    if ENABLE_IMU:
        sensors_dir = os.path.join(session_dir, "sensors")
        povimu_dir  = os.path.join(sensors_dir, "POVIMU")
        ensure_dir(sensors_dir)
        ensure_dir(povimu_dir)
        imu_dirs = {"pov": povimu_dir}

    return {
        "dir": session_dir,
        "frames": {"pov": pov_dir},
        "sensors_dir": sensors_dir,
        "imu_dirs": imu_dirs,
        "info_dir": info_dir,
    }


def imu_csv_paths(session: dict) -> dict:
    """
    Return full paths to accel.csv and gyro.csv for the current session.

    When ENABLE_IMU is False, returns an empty dict so callers can skip IMU CSV creation.
    """
    if not ENABLE_IMU:
        return {}

    imu_dir = session["imu_dirs"].get("pov")
    if not imu_dir:
        return {}

    accel_path = os.path.join(imu_dir, "accel.csv")
    gyro_path  = os.path.join(imu_dir, "gyro.csv")
    return {"accel": accel_path, "gyro": gyro_path}


# ---------------------------------------------------------------------
# Encoding & UDP preview
# ---------------------------------------------------------------------

def send_view_jpeg(img_bgr, sock, port, init_q=80, min_q=45, step=5):
    """
    Send a single JPEG frame over UDP (local HUD).

    - Adapts JPEG quality to stay below UDP_MAX.
    - If needed, downsamples slightly as a last resort.
    """
    q = init_q
    while q >= min_q:
        ok, buf = cv2.imencode('.jpg', img_bgr,
                               [int(cv2.IMWRITE_JPEG_QUALITY), q])
        if not ok:
            return False
        b = buf.tobytes()
        if len(b) < UDP_MAX:
            try:
                sock.sendto(b, (UDP_IP, port))
                return True
            except Exception as e:
                print(f"[UDP] send error (port {port}, q={q}): {e}")
                return False
        q -= step

    # Fallback: small downscale + minimum quality
    try:
        h, w = img_bgr.shape[:2]
        scaled = cv2.resize(img_bgr,
                            (int(w * 0.95), int(h * 0.95)),
                            interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode('.jpg', scaled,
                               [int(cv2.IMWRITE_JPEG_QUALITY), min_q])
        if ok and len(buf) < UDP_MAX:
            sock.sendto(buf.tobytes(), (UDP_IP, port))
            return True
    except Exception as e:
        print(f"[UDP] fallback resize send error (port {port}): {e}")
    print(f"[UDP] dropped frame on port {port} (too large even after adapt).")
    return False

# ---------------------------------------------------------------------
# IMU handling
# ---------------------------------------------------------------------

def send_imu_sample(ts_host, sensor, xyz):
    """
    Lightweight IMU fan-out from the RealSense callback.

    - Updates latest gyro/accel values and incoming-rate counters.
    - If RECORDING is True, pushes sample into IMU_Q for CSV writing.

    ts_host: float, host timestamp (seconds)
    sensor: "gyro" or "accel"
    xyz:    (x, y, z) floats
    """
    global latest_gyro, latest_accel
    global imu_in_gyro_cnt, imu_in_accel_cnt, imu_in_last_print
    global DROPPED_IMU_Q

    if sensor == "gyro":
        latest_gyro = (ts_host, xyz)
        imu_in_gyro_cnt += 1
    elif sensor == "accel":
        latest_accel = (ts_host, xyz)
        imu_in_accel_cnt += 1

    # Once per second, log incoming IMU rates
    now = time.time()
    if now - imu_in_last_print >= 1.0:
        print(f"[IMU in] gyro={imu_in_gyro_cnt}/s accel={imu_in_accel_cnt}/s")
        imu_in_gyro_cnt = imu_in_accel_cnt = 0
        imu_in_last_print = now

    if RECORDING and ENABLE_IMU:
        try:
            IMU_Q.put_nowait((ts_host, sensor, xyz))
        except Full:
            DROPPED_IMU_Q += 1

def imu_worker():
    """
    Dedicated worker that writes IMU samples to CSV:

    sensors/POVIMU/accel.csv and gyro.csv

    CSV schema (same columns as consumer):
      host_ts, t_ms, x, y, z, n

    - host_ts: float seconds (host)
    - t_ms: host_ts * 1000.0 (milliseconds approximation)
    - x,y,z: IMU data
    - n: Euclidean norm of (x,y,z)
    """
    global accel_writer, gyro_writer

    while not stop_evt.is_set():
        try:
            ts_host, sensor, xyz = IMU_Q.get(timeout=0.1)
        except Empty:
            continue

        try:
            x, y, z = float(xyz[0]), float(xyz[1]), float(xyz[2])
            n = (x * x + y * y + z * z) ** 0.5
            t_ms = ts_host * 1000.0

            with imu_csv_lock:
                if sensor == "accel" and accel_writer is not None:
                    accel_writer.writerow(
                        [f"{ts_host:.6f}", f"{t_ms:.3f}",
                         f"{x:.9f}", f"{y:.9f}", f"{z:.9f}", f"{n:.9f}"]
                    )
                elif sensor == "gyro" and gyro_writer is not None:
                    gyro_writer.writerow(
                        [f"{ts_host:.6f}", f"{t_ms:.3f}",
                         f"{x:.9f}", f"{y:.9f}", f"{z:.9f}", f"{n:.9f}"]
                    )
        except Exception as e:
            print(f"[IMU] CSV write error: {e}")
        finally:
            IMU_Q.task_done()

# ---------------------------------------------------------------------
# RealSense device helpers
# ---------------------------------------------------------------------

def find_device(serial: str):
    """
    Return (device, serial, name, motion_sensor) or (None, None, None, None)
    if no suitable device is found.
    """
    ctx = rs.context()
    devices = list(ctx.query_devices())
    if not devices:
        return None, None, None, None

    chosen = None
    for dev in devices:
        try:
            s = dev.get_info(rs.camera_info.serial_number)
        except Exception:
            continue
        if not serial or s == serial:
            chosen = dev
            break

    if chosen is None:
        return None, None, None, None

    try:
        name = chosen.get_info(rs.camera_info.name)
    except Exception:
        name = "RealSense"

    motion_sensor = None
    for s in chosen.query_sensors():
        try:
            nm = s.get_info(rs.camera_info.name)
            if "Motion Module" in nm:
                motion_sensor = s
        except Exception:
            pass

    return chosen, chosen.get_info(rs.camera_info.serial_number), name, motion_sensor

def pick_imu_rates(motion_sensor, preferred_gyro=100, preferred_accel=63):
    """
    Select nearest supported FPS values for gyro and accel around desired targets.
    """
    if motion_sensor is None:
        return None, None

    gyro_fps, accel_fps = set(), set()
    try:
        for p in motion_sensor.get_stream_profiles():
            sp = p.as_motion_stream_profile()
            if sp.format() != rs.format.motion_xyz32f:
                continue
            if sp.stream_type() == rs.stream.gyro:
                gyro_fps.add(sp.fps())
            elif sp.stream_type() == rs.stream.accel:
                accel_fps.add(sp.fps())
    except Exception:
        return None, None

    if not gyro_fps or not accel_fps:
        return None, None

    def nearest(target, options):
        return min(options, key=lambda x: abs(x - target))

    return nearest(preferred_gyro, gyro_fps), nearest(preferred_accel, accel_fps)

# ---------------------------------------------------------------------
# RealSense pipeline start + hotplug callback
# ---------------------------------------------------------------------

def start_pipeline_with_callback():
    """
    Start RealSense pipeline with a unified callback.

    Returns:
      (ctx, pipe) on success
      (None, None) if stop_evt is set before a pipeline can be started
    """
    backoff = 1.0
    ctx = rs.context()

    def _on_devices_changed(_):
        HOTPLUG_RESTART.set()

    try:
        ctx.set_devices_changed_callback(_on_devices_changed)
    except Exception:
        pass

    while not stop_evt.is_set():
        dev, serial, name, motion_sensor = find_device(DEVICE_SERIAL)
        if not dev:
            print("⏳ Waiting for RealSense device ...")
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 5.0)
            continue

        print(f"🧩 Selected device: {name} (S/N: {serial})")

        gyro_hz, accel_hz = (None, None)
        if motion_sensor:
            gyro_hz, accel_hz = pick_imu_rates(
                motion_sensor,
                preferred_gyro=100,
                preferred_accel=63,
            )
            print(f"   IMU selected → gyro={gyro_hz} Hz, accel={accel_hz} Hz")

        cfg = rs.config()
        cfg.enable_device(serial)
        cfg.enable_stream(rs.stream.color, CAP_COLOR_W, CAP_COLOR_H,
                          rs.format.bgr8, CAP_FPS)
        cfg.enable_stream(rs.stream.depth, CAP_DEPTH_W, CAP_DEPTH_H,
                          rs.format.z16, CAP_FPS)
        if gyro_hz and accel_hz:
            cfg.enable_stream(rs.stream.gyro,  rs.format.motion_xyz32f, int(gyro_hz))
            cfg.enable_stream(rs.stream.accel, rs.format.motion_xyz32f, int(accel_hz))

        try:
            pipe = rs.pipeline()
            pipe.start(cfg, on_frame)
            print("✅ Pipeline started (POV).")
            time.sleep(0.5)
            return ctx, pipe
        except Exception as e:
            print(f"⚠️ Could not start pipeline: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 5.0)

    return None, None


def on_frame(frame):
    """
    Unified callback for RealSense data.

    - Framesets (COLOR + DEPTH) are stored in q_frames.
    - Motion frames (IMU) are forwarded to send_imu_sample().
    """
    global DROPPED_DEQUE, last_frame_ts
    try:
        if frame.is_frameset():
            fs = frame.as_frameset()
            fs.keep()
            if len(q_frames) == q_frames.maxlen:
                DROPPED_DEQUE += 1
            q_frames.append(fs)
            last_frame_ts = time.monotonic()
        else:
            handle_imu_frame(frame)
    except Exception:
        # Never kill the callback on error
        pass

def handle_imu_frame(f):
    """Handle IMU (gyro/accel) frames."""
    st = f.get_profile().stream_type()
    if f.is_motion_frame():
        md = f.as_motion_frame().get_motion_data()
        ts_host = time.time()
        if st == rs.stream.gyro:
            send_imu_sample(ts_host, "gyro", (md.x, md.y, md.z))
        elif st == rs.stream.accel:
            send_imu_sample(ts_host, "accel", (md.x, md.y, md.z))

def restart_pipeline():
    """
    Stop and restart the pipeline, clearing the frame backlog.
    Safe against periods where the device is unplugged (won't crash on None).
    """
    global PIPE, last_frame_ts

    with RESTART_LOCK:
        # Stop current pipeline (if any)
        try:
            if PIPE is not None:
                try:
                    PIPE.stop()
                except Exception:
                    pass
        finally:
            PIPE = None

        # Clear backlog
        try:
            q_frames.clear()
        except Exception:
            while q_frames:
                try:
                    q_frames.popleft()
                except Exception:
                    break

        last_frame_ts = time.monotonic()

        ctx, pipe = start_pipeline_with_callback()
        if pipe is None:
            print("⚠️ Pipeline restart aborted (stopping or no device).")
            return

        PIPE = pipe
        HOTPLUG_RESTART.clear()
        print("🔁 Pipeline restarted.")


def reconnect_watchdog(stale_secs=5.0, poll=0.5):
    """
    Watchdog that restarts the pipeline if:
      - no frames arrive for > stale_secs
      - or a hotplug event is signalled
    """
    global last_frame_ts
    last_frame_ts = time.monotonic()
    while not stop_evt.is_set():
        if HOTPLUG_RESTART.is_set():
            print("🧲 Hotplug event → restart pipeline.")
            restart_pipeline()
        else:
            idle = time.monotonic() - last_frame_ts
            if idle > stale_secs:
                print(f"🛠️ No frames for {idle:.1f}s → restart pipeline.")
                restart_pipeline()
        time.sleep(poll)

# ---------------------------------------------------------------------
# Disk writer worker (frames + sidecar JSON)
# ---------------------------------------------------------------------

def write_sidecar(session: dict,
                  host_ts: float,
                  seq: int,
                  rgb_path: str,
                  depth_path: str):
    """
    Write a JSON sidecar in frames/pov, pairing RGB + Depth for this frame.

    Sidecar structure mimics consumer's write_sidecar_if_complete() for POV:

      {
        "user_id": ...,
        "session_id": ...,
        "station_id": ...,
        "camera": "pov",
        "seq": ...,
        "host_ts": ...,
        "host_ts_start": ...,
        "host_ts_end": ...,
        "rgb":   {"present": true, "image_path": "<relative>"},
        "depth": {"present": true, "image_path": "<relative>"},
        "complete": true,
        "mode": "live-capture"
      }
    """
    ts_clean = sanitize_ts_for_name(host_ts)
    camera = "pov"
    out_dir = session["frames"][camera]

    sidecar = {
        "user_id":    REC_META["user_id"],
        "session_id": REC_META["session_id"],
        "station_id": REC_META["station_id"],
        "camera":     camera,
        "seq":        int(seq),
        "host_ts":    float(host_ts),
        "host_ts_start": HOST_TS_START,
        "host_ts_end":   HOST_TS_END,
        "rgb": {
            "present": bool(rgb_path),
            "image_path": rel_to_session_name(rgb_path, session) if rgb_path else "",
        },
        "depth": {
            "present": bool(depth_path),
            "image_path": rel_to_session_name(depth_path, session) if depth_path else "",
        },
        "complete": bool(rgb_path and depth_path),
        "mode": "live-capture",
    }

    json_path = os.path.join(out_dir, f"{ts_clean}_{camera}.json")
    write_json(json_path, sidecar)

def disk_worker():
    """
    Disk writer worker.

    For each queue item:
      - Saves COLOR or DEPTH PNG under frames/pov
      - When both are available for the same (seq, timestamp),
        the caller will create a sidecar JSON.

    Here, we receive one queue item per image, but color and depth are always
    enqueued together in the main loop, so they share the same seq and host_ts.
    The sidecar is written in the main loop, not here.
    """
    global frames_sent_in_session, frames_in_session

    while not stop_evt.is_set():
        try:
            session, stream, img, host_ts, seq, meta = DISK_Q.get(timeout=0.1)
        except Empty:
            continue

        try:
            camera = "pov"
            out_dir = session["frames"][camera]

            ts_clean = sanitize_ts_for_name(host_ts)
            if stream == "color":
                filename = os.path.join(out_dir, f"{ts_clean}_{camera}_rgb.png")
            else:
                filename = os.path.join(out_dir, f"{ts_clean}_{camera}_depth.png")

            ok = cv2.imwrite(filename, img)
            if not ok:
                print(f"[DISK] Failed to write image: {filename}")
            else:
                frames_sent_in_session += 1
                frames_in_session -= 1
        except Exception as e:
            print(f"[DISK] write error ({stream}): {e}")
        finally:
            DISK_Q.task_done()

# ---------------------------------------------------------------------
# Control server (record_start / record_stop)
# ---------------------------------------------------------------------

def control_server(stop_evt_local: Event):
    """
    Small UDP JSON control server.

    Commands:
      - record_start: creates session layout, opens IMU CSVs,
                      (re)starts disk workers, sets RECORDING=True
      - record_stop : stops recording, flushes IMU queue, closes IMU CSVs
    """
    global RECORDING, REC_META, HOST_TS_START, HOST_TS_END
    global CURRENT_SESSION, FRAME_SEQ
    global accel_csv_file, gyro_csv_file, accel_writer, gyro_writer
    global frames_in_session, frames_enqueued_in_session, frames_sent_in_session
    global last_frame_ts, disk_workers

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((CONTROL_HOST, CONTROL_PORT))
    s.settimeout(0.2)
    print(f"🎛️  POV control listening on udp://{CONTROL_HOST}:{CONTROL_PORT}")

    try:
        while not stop_evt_local.is_set():
            try:
                data, _ = s.recvfrom(16_384)
            except socket.timeout:
                continue
            except Exception:
                break

            try:
                cmd = json.loads(data.decode("utf-8", errors="ignore"))
            except Exception:
                continue

            if cmd.get("cmd") == "record_start":
                # Reset per-session counters
                HOST_TS_END = None
                frames_in_session = 0
                frames_enqueued_in_session = 0
                frames_sent_in_session = 0
                FRAME_SEQ = 0

                # Prevent watchdog from restarting pipeline right at start
                last_frame_ts = time.monotonic()

                try:
                    HOST_TS_START = float(cmd.get("host_ts_start", time.time()))
                except Exception:
                    HOST_TS_START = time.time()

                # Fill REC_META
                session_id = str(cmd.get("session_id", datetime.utcnow().strftime("%Y%m%d_%H%M%S")))
                REC_META = {
                    "user_id":    str(cmd.get("user_id", "unknown")),
                    "session_id": session_id,
                    "task_type":  str(cmd.get("task_type", "unspecified")),
                    "station_id": str(cmd.get("station_id", "station1")),
                }

                # Prepare session layout
                CURRENT_SESSION = ensure_session_layout(
                    REC_META["user_id"],
                    REC_META["session_id"],
                    REC_META["station_id"],
                )

                # Write simple session_meta.json into info/
                meta_path = os.path.join(CURRENT_SESSION["info_dir"], "session_meta.json")
                try:
                    write_json(
                        meta_path,
                        {
                            "host_ts_start": HOST_TS_START,
                            "rec_meta": REC_META,
                        },
                    )
                except Exception as e:
                    print(f"[META] Could not write session_meta.json: {e}")

                # Open IMU CSV files for this session (only if IMU is enabled)
                if ENABLE_IMU:
                    paths = imu_csv_paths(CURRENT_SESSION)

                    # accel.csv
                    try:
                        accel_path = paths.get("accel")
                        if accel_path:
                            accel_csv_file = open(accel_path, "w", newline="", encoding="utf-8")
                            accel_writer = csv.writer(accel_csv_file)
                            accel_writer.writerow(["host_ts", "t_ms", "x", "y", "z", "n"])
                    except Exception as e:
                        print(f"[IMU] Could not open accel.csv: {e}")
                        accel_csv_file = None
                        accel_writer = None

                    # gyro.csv
                    try:
                        gyro_path = paths.get("gyro")
                        if gyro_path:
                            gyro_csv_file = open(gyro_path, "w", newline="", encoding="utf-8")
                            gyro_writer = csv.writer(gyro_csv_file)
                            gyro_writer.writerow(["host_ts", "t_ms", "x", "y", "z", "n"])
                    except Exception as e:
                        print(f"[IMU] Could not open gyro.csv: {e}")
                        gyro_csv_file = None
                        gyro_writer = None


                # Clear any leftover frames from previous session
                try:
                    q_frames.clear()
                except Exception:
                    while q_frames:
                        try:
                            q_frames.popleft()
                        except Exception:
                            break

                # Start/restart disk workers
                needed_workers = 4
                for w in list(disk_workers):
                    if not w.is_alive():
                        try:
                            disk_workers.remove(w)
                        except ValueError:
                            pass
                while len(disk_workers) < needed_workers:
                    t = Thread(target=disk_worker, daemon=True)
                    t.start()
                    disk_workers.append(t)

                # Mark recording ON
                RECORDING = True
                print(f"⏺️  POV RECORDING START → {REC_META} | session_dir={CURRENT_SESSION['dir']}")

                # Optionally wait for first frame
                t_wait = time.time()
                while len(q_frames) == 0 and (time.time() - t_wait) < 1.5:
                    time.sleep(0.01)

            elif cmd.get("cmd") == "record_stop":
                try:
                    HOST_TS_END = float(cmd.get("host_ts_end", time.time()))
                except Exception:
                    HOST_TS_END = time.time()

                # Stop accepting new frames and IMU samples
                RECORDING = False

                expected = int(round(max(0.0, HOST_TS_END - HOST_TS_START) * CAP_FPS))

                print(
                    f"⏹️  POV RECORDING STOP (host_ts_end={HOST_TS_END}) "
                    f"| session_id={REC_META.get('session_id', '-')} "
                    f"| qsize_frames={DISK_Q.qsize()}/{DISK_Q.maxsize} "
                    f"| frames_expected≈{expected} enqueued={frames_enqueued_in_session} "
                    f"sent={frames_sent_in_session} | frames_in_session={frames_in_session}"
                )

                # Flush IMU queue and close CSV files
                try:
                    IMU_Q.join()
                except Exception:
                    pass

                with imu_csv_lock:
                    try:
                        if accel_csv_file is not None:
                            accel_csv_file.flush()
                            accel_csv_file.close()
                    except Exception:
                        pass
                    try:
                        if gyro_csv_file is not None:
                            gyro_csv_file.flush()
                            gyro_csv_file.close()
                    except Exception:
                        pass

                    # Reset IMU writers
                    accel_csv_file = None
                    gyro_csv_file = None
                    accel_writer = None
                    gyro_writer = None

                print(
                    f"🎯 STOP summary → frames_expected≈{expected} | enqueued={frames_enqueued_in_session} "
                    f"| sent={frames_sent_in_session} | pending={frames_in_session} "
                    f"| deque_drops={DROPPED_DEQUE} diskq_drops={DROPPED_DISKQ} imu_q_drops={DROPPED_IMU_Q}"
                )

    finally:
        try:
            s.close()
        except Exception:
            pass

# ---------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------

def main():
    """
    Main application loop:

    - Starts control server (record_start/record_stop)
    - Starts IMU worker
    - Starts RealSense pipeline and hotplug watchdog
    - Drains frames from q_frames
      - sends previews over UDP (when enabled)
      - when RECORDING, writes PNGs to disk and JSON sidecars per frame pair
    """
    global DROPPED_DISKQ, DROPPED_VIEW
    global PIPE, FRAME_SEQ
    global frames_in_session, frames_enqueued_in_session

    # Control server thread
    Thread(target=control_server, args=(stop_evt,), daemon=True).start()

    # IMU worker thread
    #Thread(target=imu_worker, daemon=True).start()

    # RealSense pipeline + watchdog
    _, pipe = start_pipeline_with_callback()
    PIPE = pipe
    Thread(target=reconnect_watchdog, args=(5.0, 0.5), daemon=True).start()

    t0 = time.time()
    rgb_cnt = 0
    depth_cnt = 0
    view_toggle = False

    try:
        while not stop_evt.is_set():
            backlog = len(q_frames)
            skip_preview = (backlog > (q_frames.maxlen * 0.25)) or (DISK_Q.qsize() > (DISK_Q.maxsize * 0.25))
            if DISABLE_PREVIEW_WHEN_RECORDING and RECORDING:
                skip_preview = True

            # Drain framesets
            while q_frames:
                fs = q_frames.popleft()
                try:
                    color_frame = fs.get_color_frame()
                    depth_frame = fs.get_depth_frame()
                except Exception:
                    break
                if not color_frame or not depth_frame:
                    continue

                color_bgr = np.asanyarray(color_frame.get_data())   # BGR8
                depth_raw = np.asanyarray(depth_frame.get_data())   # Z16

                # UDP preview (optionally reduced when recording)
                if not skip_preview:
                    if not RECORDING:
                        rgb_view = cv2.resize(color_bgr, (VIEW_COLOR_W, VIEW_COLOR_H),
                                             interpolation=cv2.INTER_AREA)
                        if send_view_jpeg(rgb_view, sock_view_rgb, VIEW_RGB_PORT,
                                          init_q=80, min_q=45, step=5):
                            rgb_cnt += 1
                        else:
                            DROPPED_VIEW += 1

                        NEAR_MM, FAR_MM = 250, 1200
                        depth_clipped = np.clip(depth_raw, NEAR_MM, FAR_MM)
                        depth_norm = ((depth_clipped - NEAR_MM) /
                                      (FAR_MM - NEAR_MM) * 255).astype(np.uint8)
                        depth_jet = cv2.applyColorMap(depth_norm, cv2.COLORMAP_JET)
                        depth_jet_view = cv2.resize(depth_jet,
                                                    (VIEW_DEPTH_W, VIEW_DEPTH_H),
                                                    interpolation=cv2.INTER_AREA)
                        if send_view_jpeg(depth_jet_view, sock_view_depth, VIEW_DEPTH_PORT,
                                          init_q=80, min_q=45, step=5):
                            depth_cnt += 1
                        else:
                            DROPPED_VIEW += 1
                    else:
                        view_toggle = not view_toggle
                        if view_toggle:
                            rgb_view = cv2.resize(color_bgr, (VIEW_COLOR_W, VIEW_COLOR_H),
                                                 interpolation=cv2.INTER_AREA)
                            if send_view_jpeg(rgb_view, sock_view_rgb, VIEW_RGB_PORT,
                                              init_q=65, min_q=40, step=10):
                                rgb_cnt += 1
                            else:
                                DROPPED_VIEW += 1
                        if FRAME_SEQ % 2 == 0:
                            NEAR_MM, FAR_MM = 250, 1200
                            depth_clipped = np.clip(depth_raw, NEAR_MM, FAR_MM)
                            depth_norm = ((depth_clipped - NEAR_MM) /
                                          (FAR_MM - NEAR_MM) * 255).astype(np.uint8)
                            depth_jet = cv2.applyColorMap(depth_norm, cv2.COLORMAP_JET)
                            depth_jet_view = cv2.resize(depth_jet,
                                                        (VIEW_DEPTH_W, VIEW_DEPTH_H),
                                                        interpolation=cv2.INTER_AREA)
                            if send_view_jpeg(depth_jet_view, sock_view_depth, VIEW_DEPTH_PORT,
                                              init_q=65, min_q=40, step=10):
                                depth_cnt += 1
                            else:
                                DROPPED_VIEW += 1

                # Local disk recording (frames + JSON sidecar)
                if RECORDING and CURRENT_SESSION is not None:
                    host_ts = time.time()
                    FRAME_SEQ += 1
                    seq = FRAME_SEQ
                    session = CURRENT_SESSION

                    # COLOR
                    enq_ok = False
                    while not enq_ok:
                        try:
                            DISK_Q.put(
                                (session, "color", color_bgr.copy(), host_ts, seq, REC_META.copy()),
                                timeout=0.05,
                            )
                            frames_in_session += 1
                            frames_enqueued_in_session += 1
                            enq_ok = True
                        except Full:
                            skip_preview = True
                            DROPPED_DISKQ += 1
                            time.sleep(0.001)

                    # DEPTH
                    enq_ok = False
                    while not enq_ok:
                        try:
                            DISK_Q.put(
                                (session, "depth", depth_raw.copy(), host_ts, seq, REC_META.copy()),
                                timeout=0.05,
                            )
                            frames_in_session += 1
                            frames_enqueued_in_session += 1
                            enq_ok = True
                        except Full:
                            skip_preview = True
                            DROPPED_DISKQ += 1
                            time.sleep(0.001)

                    # Build expected file paths and write sidecar immediately
                    ts_clean = sanitize_ts_for_name(host_ts)
                    pov_dir = session["frames"]["pov"]
                    rgb_path   = os.path.join(pov_dir, f"{ts_clean}_pov_rgb.png")
                    depth_path = os.path.join(pov_dir, f"{ts_clean}_pov_depth.png")
                    write_sidecar(session, host_ts, seq, rgb_path, depth_path)

            # 1 Hz heartbeat
            now = time.time()
            if now - t0 >= 1.0:
                print(
                    f"📤 POV stats: RGB={rgb_cnt}/s Depth={depth_cnt}/s "
                    f"REC={'ON' if RECORDING else 'OFF'} "
                    f"| drops deque={DROPPED_DEQUE} diskq={DROPPED_DISKQ} "
                    f"view={DROPPED_VIEW} imu_q={DROPPED_IMU_Q} "
                    f"| q_disk={DISK_Q.qsize()}/{DISK_Q.maxsize} "
                    f"backlog={len(q_frames)}/{q_frames.maxlen} "
                    f"| session_id={REC_META.get('session_id', '-') or '-'} "
                    f"| frames_enq={frames_enqueued_in_session} "
                    f"frames_sent={frames_sent_in_session} "
                    f"| preview={'OFF' if skip_preview else 'ON'}"
                )
                rgb_cnt = depth_cnt = 0
                t0 = now

            time.sleep(0.0005)

    except KeyboardInterrupt:
        print("\n🛑 Stop requested by user.")
    finally:
        stop_evt.set()
        try:
            if PIPE is not None:
                PIPE.stop()
        except Exception:
            pass

        # Flush queues
        try:
            DISK_Q.join()
        except Exception:
            pass
        try:
            IMU_Q.join()
        except Exception:
            pass

        # Close IMU CSVs if still open
        with imu_csv_lock:
            try:
                if accel_csv_file is not None:
                    accel_csv_file.flush()
                    accel_csv_file.close()
            except Exception:
                pass
            try:
                if gyro_csv_file is not None:
                    gyro_csv_file.flush()
                    gyro_csv_file.close()
            except Exception:
                pass

        print("✅ Clean shutdown.")

# ---------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------

if __name__ == "__main__":
    main()
