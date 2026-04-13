"""
Central configuration for Glove Core.
All ports, paths, and service definitions live here.
"""
import os
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent.parent
PRODUCER_DIR  = BASE_DIR / "producer"
LOG_DIR       = BASE_DIR / "logs"
DATA_DIR      = Path(os.environ.get("GLOVE_DATA_DIR", "/mnt/data"))
CALIB_FILE    = Path.home() / ".glove" / "calibration.json"

# ── Web dashboard ─────────────────────────────────────────────────────────────
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = int(os.environ.get("GLOVE_DASHBOARD_PORT", 5000))

# ── Inter-process UDP ports ───────────────────────────────────────────────────
DAEMON_UDP_PORT    = 5005   # station_daemon receives commands
CAMERA_POV_PORT    = 9050
CAMERA_POV2_PORT   = 9054
EXO_UDP_PORT       = 9052
EXO_TELEMETRY_PORT = 9002   # exo → dashboard telemetry
POV_PREVIEW_RGB    = 9013
POV_PREVIEW_DEPTH  = 9014
POV2_PREVIEW_RGB   = 9023
POV2_PREVIEW_DEPTH = 9024

# ── ZMQ ───────────────────────────────────────────────────────────────────────
ZMQ_PUB_ADDR      = "tcp://0.0.0.0:5555"     # raw sensor frames (all interfaces)
ZMQ_JOINTS_ADDR   = "tcp://0.0.0.0:5556"     # processed joints (Phase 2)
ZMQ_STATUS_PORT   = 5557                      # zmq_publisher → dashboard subscriber status

# ── Serial (EXO / glove) ──────────────────────────────────────────────────────
EXO_SERIAL_PORT = os.environ.get("GLOVE_SERIAL_PORT", "/dev/ttyUSB0")
EXO_BAUD        = int(os.environ.get("GLOVE_BAUD", 921600))

# ── Service definitions ───────────────────────────────────────────────────────
# Each entry describes one managed subprocess.
# 'cmd' is relative to PRODUCER_DIR unless it's an absolute path.
SERVICES: dict[str, dict] = {
    "station_daemon": {
        "label":       "Station Daemon",
        "description": "Command router — fans out start/stop to all producers",
        "script":      str(BASE_DIR / "app" / "station_daemon.py"),
        "args":        [],
        "autostart":   True,
        "color":       "blue",
    },
    "camera_pov": {
        "label":       "Camera POV 1",
        "description": "RealSense D435i — primary POV (serial 843112072148)",
        "script":      str(PRODUCER_DIR / "camera_pov.py"),
        "args":        [],
        "autostart":   True,
        "color":       "green",
    },
    "camera_pov2": {
        "label":       "Camera POV 2",
        "description": "RealSense D435i — secondary POV (serial 818312070414)",
        "script":      str(PRODUCER_DIR / "camera_pov2.py"),
        "args":        [],
        "autostart":   True,
        "color":       "green",
    },
    "exo_capture": {
        "label":       "EXO / Glove Sensor",
        "description": "ESP32 serial capture + ZMQ publisher",
        "script":      str(PRODUCER_DIR / "exo_capture.py"),
        "args":        [
            "--serial", EXO_SERIAL_PORT,
            "--baud",   str(EXO_BAUD),
        ],
        "autostart":   True,
        "color":       "purple",
    },
    "zmq_publisher": {
        "label":       "ZMQ Publisher",
        "description": "Publishes sensor frames to Unity / Isaac / ROS2",
        "script":      str(BASE_DIR / "core" / "zmq_publisher.py"),
        "args":        [],
        "autostart":   True,
        "color":       "orange",
    },
}
