"""
Cross-platform helpers for paths, serial port detection, and hardware checks.
Used by core/config.py and core/user_config.py so the app works on both
Linux and Windows without code changes.
"""
from __future__ import annotations

import os
import platform
from pathlib import Path


def get_default_data_dir() -> Path:
    """Return the default data directory for the current OS.

    Override via the GLOVE_DATA_DIR environment variable on either platform.
    - Linux:   /mnt/data
    - Windows: %APPDATA%\\GloveCore\\data
    """
    env = os.environ.get("GLOVE_DATA_DIR")
    if env:
        return Path(env)
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "GloveCore" / "data"
    return Path("/mnt/data")


def get_config_dir() -> Path:
    """Return the user configuration directory for the current OS.

    - Linux:   ~/.glove/
    - Windows: %APPDATA%\\GloveCore\\
    """
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "GloveCore"
    return Path.home() / ".glove"


def get_default_serial_port() -> str:
    """Auto-detect the ESP32/glove serial port.

    Scans connected serial devices for known USB-serial chips (CH340, CP210x, FTDI).
    Falls back to the OS-appropriate default if nothing is found.
    """
    try:
        import serial.tools.list_ports
        keywords = ("CH340", "CP210", "FTDI", "CH341", "ESP32", "USB SERIAL", "USB-SERIAL")
        for port in serial.tools.list_ports.comports():
            desc = f"{port.description or ''} {port.hwid or ''}".upper()
            if any(kw in desc for kw in keywords):
                return port.device
    except Exception:
        pass
    return "COM3" if platform.system() == "Windows" else "/dev/ttyUSB0"


def serial_port_exists(port: str) -> bool:
    """Return True if the given serial port is present on the system.

    - Linux:   checks for the /dev/tty* device file
    - Windows: enumerates active COM ports via pyserial
    """
    if platform.system() == "Windows":
        try:
            import serial.tools.list_ports
            available = {p.device for p in serial.tools.list_ports.comports()}
            return port in available
        except Exception:
            return False
    return Path(port).exists()
