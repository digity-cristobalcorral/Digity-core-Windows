# Glove-Core

Data capture and monitoring system for the Digity exoskeleton glove.
Streams sensor data from an ESP32 glove via serial, records synchronized RealSense D435i video,
and exposes a real-time web dashboard for session control.

---

## Requirements

| | Minimum |
|---|---|
| OS | Debian 12 / Ubuntu 22.04 / Ubuntu 24.04 |
| Python | 3.10 or newer |
| GPU (optional) | Any — CUDA not required for capture |
| Hardware | ESP32 gateway (USB serial), Intel RealSense D435i |

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/digity-cristobalcorral/digity-core.git
cd digity-core
```

### 2. Run the installer

```bash
sudo bash install.sh
```

The installer will:
- Install all system packages (apt)
- Register udev rules for the ESP32 and RealSense camera
- Add your user to the `dialout` and `video` groups
- Create `/mnt/data/session` for session storage
- Create a Python virtual environment at `.venv/`
- Install all Python dependencies from `requirements.txt`
- Generate `run.sh` and `run-app.sh` launch scripts

> **Important:** After installation, **log out and log back in** so group changes (`dialout`, `video`) take effect.

---

## Starting the app

### Browser mode (recommended)

```bash
./run.sh
```

Open [http://localhost:5000](http://localhost:5000) in your browser.

### Desktop window mode

```bash
./run-app.sh
```

Requires a graphical session (X11 or Wayland) and Qt5 WebEngine.

---

## Device configuration

Open **Setup** (top navigation) to configure your hardware:

| Setting | Description |
|---|---|
| Station Name | Label shown in session filenames |
| Camera POV 1 Serial | RealSense D435i serial number for the primary camera |
| Camera POV 2 Serial | RealSense D435i serial number for the secondary camera |
| EXO Serial Port | USB serial port for the ESP32 gateway (e.g. `/dev/ttyUSB0`) |
| EXO Baud Rate | Serial baud rate (default `921600`) |

Settings are saved to `~/.glove/config.json` and applied immediately — affected services restart automatically.

To find your RealSense serial number:

```bash
rs-enumerate-devices | grep Serial
```

---

## Recording sessions

1. Open the **Dashboard** at [http://localhost:5000](http://localhost:5000)
2. Fill in User ID, Session ID, Task, and Station fields
3. Click **Start Recording** — all connected producers begin capturing simultaneously
4. Click **Stop Recording** to end the session

Session data is stored at `/mnt/data/session/<user>_<session>_<station>/`:

```
<session>/
  frames/
    pov/
      <ts>_pov_rgb.png
      <ts>_pov_depth.png
      <ts>_pov.json
  sensors/
    stream.raw
  info/
    session_meta.json
```

---

## Hardware setup

### ESP32 gateway

Connect the ESP32 via USB. It should appear as `/dev/ttyUSB0` (CH340) or `/dev/ttyACM0`.
The udev rule also creates the symlink `/dev/ttyESP32`.

Verify:
```bash
ls /dev/ttyUSB* /dev/ttyESP32 2>/dev/null
```

### RealSense D435i

Connect via a USB 3.0 port. Verify detection:
```bash
rs-enumerate-devices
```

If the camera is not detected, check that the Intel RealSense SDK is installed:
```bash
sudo apt install librealsense2-utils
```

---

## Troubleshooting

### Permission denied on /dev/ttyUSB0

Log out and log back in after installation so the `dialout` group takes effect. Verify:
```bash
groups | grep dialout
```

### Camera not detected

Ensure you are using a USB 3.0 port (blue). USB 2.0 is not supported by the D435i at full resolution.

### Services not starting

Check the service logs in the **Setup** page under Services Health, or view log files directly:
```bash
ls digity-core/logs/
```

### Port 5000 already in use

Set a custom port before starting:
```bash
GLOVE_DASHBOARD_PORT=5001 ./run.sh
```

---

## Project structure

```
digity-core/
  main.py                  # Entry point
  app/
    server.py              # Flask + SocketIO — REST API and dashboard server
    station_daemon.py      # UDP coordinator — fans out record start/stop
    templates/
      dashboard.html       # Main recording dashboard
      setup.html           # Device configuration and diagnostics
      hand_viewer.html     # Real-time hand joint visualization
  core/
    config.py              # Ports, paths, service definitions
    user_config.py         # User settings (~/.glove/config.json)
    service_manager.py     # Subprocess lifecycle management
    zmq_publisher.py       # ZMQ XPUB — streams sensor data to Unity / ROS2
    humi_protocol.py       # Binary HUMI sensor protocol parser
  producer/
    camera_pov.py          # RealSense D435i — primary POV capture
    camera_pov2.py         # RealSense D435i — secondary POV capture
    exo_capture.py         # ESP32 serial capture + raw recorder
  tools/
    prepare_genesis_dataset.py  # Prepares pix2pix training data from sessions
  install.sh               # Client installation script
  requirements.txt         # Python dependencies
```

---

## Data directory

By default, sessions are saved to `/mnt/data/session`. To use a different path:

```bash
GLOVE_DATA_DIR=/path/to/data ./run.sh
```

Or set it permanently in your shell profile:
```bash
echo 'export GLOVE_DATA_DIR=/path/to/data' >> ~/.bashrc
```
