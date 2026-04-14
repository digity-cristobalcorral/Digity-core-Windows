# Glove-Core

Data capture and monitoring system for the Digity exoskeleton glove.
Streams sensor data from an ESP32 glove via serial, records synchronized RealSense D435i video,
and exposes a real-time web dashboard for session control.

---

## Platform support

| OS | Installation method |
|---|---|
| Debian 12 / Ubuntu 22.04+ | `install.sh` script |
| Windows 10 / 11 | Self-contained `.exe` installer — no prerequisites needed |

---

## Windows installation

Download `DigityCore-Setup-1.0.0.exe` and double-click it. That is all.

The installer:
- Requires no Python, no admin rights, no prior setup
- Installs to `C:\Users\<you>\AppData\Local\DigityCore\`
- Creates a Start Menu shortcut and an optional desktop shortcut
- Bundles a complete Python 3.11 runtime with all dependencies

After installation, launch **Digity Core** from the Start Menu or desktop shortcut.
The dashboard opens as a native desktop window at [http://localhost:5000](http://localhost:5000).

### Troubleshooting the Windows app

If the app does not open, run `launch.bat` from the install folder to see the error:

```
C:\Users\<you>\AppData\Local\DigityCore\launch.bat
```

Common issues on Windows:

| Symptom | Fix |
|---|---|
| Serial port not found | Open Device Manager, find the ESP32 (COM port under Ports), set it in Setup |
| CH340 driver missing | Install the CH340 driver from [wch-ic.com](https://www.wch-ic.com/products/CH341.html) |
| Camera not detected | App will start without camera; connect RealSense and restart |

### Building the installer (developers only)

Run this once on a Windows machine with Python 3.11+ and Inno Setup 6 installed:

```bat
cd build
build_windows.bat
```

Output: `build\output\DigityCore-Setup-1.0.0.exe`

---

## Linux installation

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

### 3. Start the app

```bash
source .venv/bin/activate
python3 main.py --app
```

This opens the dashboard as a native desktop window.

To use the browser instead (no display required):

```bash
source .venv/bin/activate
python3 main.py
```

Then open [http://localhost:5000](http://localhost:5000) in your browser.

---

## Device configuration

Open **Setup** (top navigation) to configure your hardware:

| Setting | Description |
|---|---|
| Station Name | Label shown in session filenames |
| Camera POV 1 Serial | RealSense D435i serial number for the primary camera |
| Camera POV 2 Serial | RealSense D435i serial number for the secondary camera |
| EXO Serial Port | USB serial port for the ESP32 (Linux: `/dev/ttyUSB0`, Windows: `COM3`) |
| EXO Baud Rate | Serial baud rate (default `921600`) |

Settings are saved automatically and applied immediately — affected services restart on save.

Config file location:
- Linux: `~/.glove/config.json`
- Windows: `%APPDATA%\GloveCore\config.json`

To find your RealSense serial number on Linux:

```bash
rs-enumerate-devices | grep Serial
```

On Windows, the serial number appears in the Intel RealSense Viewer application.

---

## Recording sessions

1. Open the **Dashboard** at [http://localhost:5000](http://localhost:5000)
2. Fill in User ID, Session ID, Task, and Station fields
3. Click **Start Recording** — all connected producers begin capturing simultaneously
4. Click **Stop Recording** to end the session

Session data is stored under the data directory:
- Linux: `/mnt/data/session/` (or `$GLOVE_DATA_DIR`)
- Windows: `%APPDATA%\GloveCore\data\session\` (or `GLOVE_DATA_DIR` env var)

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

Connect the ESP32 via USB.

- **Linux:** appears as `/dev/ttyUSB0` (CH340) or `/dev/ttyACM0`. The udev rule also creates `/dev/ttyESP32`.
- **Windows:** appears as `COM3` or similar. Check Device Manager → Ports if unsure.

If Windows does not recognise the device, install the CH340 driver: [wch-ic.com](https://www.wch-ic.com/products/CH341.html)

Verify on Linux:
```bash
ls /dev/ttyUSB* /dev/ttyESP32 2>/dev/null
```

### RealSense D435i

Connect via a USB 3.0 port (blue). USB 2.0 is not supported at full resolution.

Verify on Linux:
```bash
rs-enumerate-devices
```

If the camera is not detected on Linux:
```bash
sudo apt install librealsense2-utils
```

---

## Troubleshooting (Linux)

### Permission denied on /dev/ttyUSB0

Log out and back in after installation so the `dialout` group takes effect:
```bash
groups | grep dialout
```

### Services not starting

Check the service logs in the **Setup** page under Services Health, or view log files:
```bash
ls digity-core/logs/
```

### Port 5000 already in use

```bash
GLOVE_DASHBOARD_PORT=5001 ./run.sh
```

---

## Project structure

```
digity-core/
  main.py                  # Entry point (--app for desktop window, --web for browser)
  requirements.txt         # Python dependencies
  install.sh               # Linux installation script
  app/
    server.py              # Flask + SocketIO — REST API and dashboard server
    station_daemon.py      # UDP coordinator — fans out record start/stop
    templates/
      dashboard.html       # Main recording dashboard
      setup.html           # Device configuration and diagnostics
      hand_viewer.html     # Real-time hand joint visualization
  core/
    config.py              # Ports, paths, service definitions
    user_config.py         # User settings
    platform_helpers.py    # OS-specific paths and port detection (Linux/Windows)
    service_manager.py     # Subprocess lifecycle management
    zmq_publisher.py       # ZMQ XPUB — streams sensor data to Unity / ROS2
    humi_protocol.py       # Binary HUMI sensor protocol parser
  producer/
    camera_pov.py          # RealSense D435i — primary POV capture
    camera_pov2.py         # RealSense D435i — secondary POV capture
    exo_capture.py         # ESP32 serial capture + raw recorder
  tools/
    prepare_genesis_dataset.py   # Prepares pix2pix training data from sessions
    genesis_remove_glove.py      # SAM2 + LaMa glove removal pipeline
  build/                   # Windows installer (developers only)
    build_windows.bat      # Builds the self-contained Windows installer
    installer.iss          # Inno Setup 6 script
    launch.bat             # Debug launcher (shows console errors)
    launch.vbs             # Silent launcher (no console window)
```

---

## Data directory override

Custom data path via environment variable:

```bash
# Linux
GLOVE_DATA_DIR=/path/to/data ./run.sh

# Windows (Command Prompt)
set GLOVE_DATA_DIR=D:\data
python main.py --app
```
