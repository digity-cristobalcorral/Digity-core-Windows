# Digity Core — Windows

Data capture and monitoring system for the Digity exoskeleton glove.  
Streams sensor data from an ESP32 glove via serial, records synchronized RealSense D435i video,
and exposes a real-time web dashboard for session control.

> **Windows-only build.** For Linux, see the main repository.

---

## Installation

Download `DigityCore-Setup-1.0.0.exe` and double-click it.

The installer:
- Requires no Python, no admin rights, no prior setup
- Installs to `C:\Users\<you>\AppData\Local\DigityCore\`
- Creates a Start Menu shortcut and an optional desktop shortcut
- Bundles a complete Python 3.11 runtime with all dependencies

After installation, launch **Digity Core** from the Start Menu or desktop shortcut.  
The dashboard opens in your browser at [http://localhost:5000](http://localhost:5000).

---

## Device configuration

Open **Setup** (top navigation) to configure your hardware:

| Setting | Description |
|---|---|
| Station Name | Label shown in session filenames |
| Camera POV 1 Serial | RealSense D435i serial number for the primary camera |
| Camera POV 2 Serial | RealSense D435i serial number for the secondary camera |
| EXO Serial Port | USB serial port for the ESP32 (e.g. `COM3`) |
| EXO Baud Rate | Serial baud rate (default `921600`) |

Settings are saved automatically. Affected services restart on save.

Config file: `%APPDATA%\GloveCore\config.json`

To find your RealSense serial number, open the **Intel RealSense Viewer** application.

---

## Recording sessions

1. Open the dashboard at [http://localhost:5000](http://localhost:5000)
2. Fill in User ID, Session ID, Task, and Station fields
3. Click **Start Recording** — all connected producers begin capturing simultaneously
4. Click **Stop Recording** to end the session

Session data is stored at:

```
%APPDATA%\GloveCore\data\session\<session>\
  frames\
    pov\
      <ts>_pov_rgb.png
      <ts>_pov_depth.png
      <ts>_pov.json
  sensors\
    stream.raw
  info\
    session_meta.json
```

Custom data path:
```bat
set GLOVE_DATA_DIR=D:\data
```

---

## Hardware setup

### ESP32

Connect the ESP32 via USB. It appears as `COM3` or similar — check **Device Manager → Ports** if unsure.

If Windows does not recognise the device, install the CH340 driver:  
[wch-ic.com/products/CH341.html](https://www.wch-ic.com/products/CH341.html)

### RealSense D435i

Connect via a **USB 3.0** port (blue). USB 2.0 is not supported at full resolution.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| App does not open | Run `launch.bat` from the install folder to see the error in the console |
| Serial port not found | Open Device Manager, find the ESP32 COM port, set it in Setup |
| CH340 driver missing | Install from [wch-ic.com](https://www.wch-ic.com/products/CH341.html) |
| Camera not detected | App starts without camera — connect RealSense and restart |
| Port 5000 in use | Set `GLOVE_DASHBOARD_PORT=5001` before launching |

Debug launcher (shows console errors):
```
C:\Users\<you>\AppData\Local\DigityCore\launch.bat
```

---

## Automatic updates

When a new version is available, a banner appears in the dashboard with an **Apply & restart** button.  
The app downloads and applies the update automatically — no reinstall needed.

---

## Project structure

```
digity-core/
  main.py                  # Entry point (--app opens dashboard)
  requirements.txt         # Python dependencies
  version.txt              # Current version number
  app/
    server.py              # Flask + SocketIO — REST API and dashboard server
    station_daemon.py      # UDP coordinator — fans out record start/stop
    templates/
      dashboard.html       # Main recording dashboard
      setup.html           # Device configuration and diagnostics
      hand_viewer.html     # Real-time hand joint visualization
    static/
      digity.ico           # App icon
  core/
    config.py              # Ports, paths, service definitions
    user_config.py         # User settings
    platform_helpers.py    # OS-specific paths and port detection
    service_manager.py     # Subprocess lifecycle management
    updater.py             # Auto-update logic
    zmq_publisher.py       # ZMQ XPUB — streams sensor data to Unity / ROS2
    humi_protocol.py       # Binary HUMI sensor protocol parser
  producer/
    camera_pov.py          # RealSense D435i — primary POV capture
    camera_pov2.py         # RealSense D435i — secondary POV capture
    exo_capture.py         # ESP32 serial capture + raw recorder
  build/
    build_windows.bat      # Full build — downloads Python + packages + compiles installer
    rebuild_fast.bat       # Fast rebuild — syncs source files + recompiles (no re-download)
    make_update_zip.bat    # Builds source-only update ZIP for GitHub Releases
    installer.iss          # Inno Setup 6 script
    launch.bat             # Debug launcher (shows console errors)
    launch.vbs             # Silent launcher (no console window)
```

---

## Building the installer (developers)

**Prerequisites:** Python 3.11+, [Inno Setup 6](https://jrsoftware.org/isinfo.php)

**Full build** (first time or after adding new dependencies):
```bat
cd build
build_windows.bat
```

**Fast rebuild** (after code-only changes):
```bat
cd build
rebuild_fast.bat
```

Output: `build\output\DigityCore-Setup-1.0.0.exe`

### Publishing an update

1. Bump `version.txt` (e.g. `1.0.1`)
2. Run `build\make_update_zip.bat` — generates `update-1.0.1.zip` and `latest.json`
3. Create a GitHub Release tagged `v1.0.1`
4. Upload both files as release assets

Installed clients check for updates automatically on launch.
