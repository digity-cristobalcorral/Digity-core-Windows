#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Glove-Core — Installation script
#  Supported: Debian 12 (bookworm), Ubuntu 22.04 / 24.04
#  Usage:  sudo bash install.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
ok()     { echo -e "${GREEN}[✓]${NC} $*"; }
warn()   { echo -e "${YELLOW}[!]${NC} $*"; }
err()    { echo -e "${RED}[✗]${NC} $*" >&2; }
header() { echo -e "\n${BLUE}${BOLD}── $* ──${NC}"; }
die()    { err "$*"; exit 1; }

# ── Root check ────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Run as root:  sudo bash install.sh"

# ── Resolve real user (not root) ──────────────────────────────────────────────
REAL_USER="${SUDO_USER:-}"
[[ -z "$REAL_USER" ]] && die "Run with sudo, not as root directly:  sudo bash install.sh"
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
DATA_DIR="${GLOVE_DATA_DIR:-/mnt/data}"

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  ╔══════════════════════════════════════════╗"
echo -e "  ║       Glove-Core  Installer              ║"
echo -e "  ╚══════════════════════════════════════════╝${NC}"
echo ""
ok "Project : $PROJECT_DIR"
ok "User    : $REAL_USER  ($REAL_HOME)"
ok "Data    : $DATA_DIR"

# ── OS detection ─────────────────────────────────────────────────────────────
header "OS check"
# shellcheck source=/dev/null
source /etc/os-release
case "${ID}" in
    debian)  ok "Detected Debian ${VERSION_ID:-?} (${VERSION_CODENAME:-?})" ;;
    ubuntu)  ok "Detected Ubuntu ${VERSION_ID:-?} (${VERSION_CODENAME:-?})" ;;
    *)
        ID_LIKE="${ID_LIKE:-}"
        if [[ "$ID_LIKE" == *"debian"* ]]; then
            warn "Debian-derivative: $PRETTY_NAME  — continuing."
        else
            warn "Untested OS: $PRETTY_NAME  — continuing anyway."
        fi
        ;;
esac

# ── System packages ───────────────────────────────────────────────────────────
header "System packages (apt)"
apt-get update -qq

# Core build + runtime
apt-get install -y --no-install-recommends \
    python3 python3-dev python3-pip python3-venv \
    build-essential pkg-config \
    git curl wget ca-certificates \
    libssl-dev libffi-dev \
    usbutils

# USB / serial
apt-get install -y --no-install-recommends \
    libusb-1.0-0 libusb-1.0-0-dev

# OpenCV headless runtime dependencies
apt-get install -y --no-install-recommends \
    libsm6 libxext6 libgl1 libglib2.0-0

# Qt5 + WebEngine (required by pywebview --app mode)
apt-get install -y --no-install-recommends \
    libqt5core5a libqt5gui5 libqt5network5 \
    libqt5widgets5 libqt5webchannel5 \
    libqt5webengine5 libqt5webenginewidgets5 \
    libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 \
    libxcb-keysyms1 libxcb-render-util0 libxcb-xinerama0 \
    libxcb-randr0 libxcb-shape0 libxcb-sync1 \
    libxcb-util1 libxcb-xfixes0 \
    2>/dev/null || warn "Some Qt packages not found — desktop (--app) mode may be limited."

ok "System packages done."

# ── udev rules ───────────────────────────────────────────────────────────────
header "udev rules"
cat > /etc/udev/rules.d/99-glove-core.rules << 'UDEV'
# ── ESP32 gateway (serial) ──────────────────────────────────────────────────
# CH340 / CH341
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", \
    MODE="0666", GROUP="dialout", SYMLINK+="ttyESP32"
# CP2102 / CP2104
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", \
    MODE="0666", GROUP="dialout"
# FTDI FT232
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", \
    MODE="0666", GROUP="dialout"

# ── Intel RealSense (USB) ───────────────────────────────────────────────────
SUBSYSTEM=="usb", ATTRS{idVendor}=="8086", MODE="0666", GROUP="video"
SUBSYSTEM=="usb_device", ATTRS{idVendor}=="8086", MODE="0666", GROUP="video"
UDEV

udevadm control --reload-rules && udevadm trigger
ok "udev rules → /etc/udev/rules.d/99-glove-core.rules"

# ── User groups ───────────────────────────────────────────────────────────────
header "User groups"
for grp in dialout video; do
    if getent group "$grp" > /dev/null 2>&1; then
        usermod -aG "$grp" "$REAL_USER"
        ok "Added $REAL_USER → $grp"
    else
        warn "Group '$grp' not found — skipping."
    fi
done

# ── Data directory ────────────────────────────────────────────────────────────
header "Data directory"
mkdir -p "$DATA_DIR/session"
chown -R "$REAL_USER:$REAL_USER" "$DATA_DIR" 2>/dev/null || \
    warn "Could not chown $DATA_DIR (mount point? check manually)."
chmod -R 775 "$DATA_DIR"
ok "Data directory: $DATA_DIR"

# ── Project support dirs ──────────────────────────────────────────────────────
mkdir -p "$PROJECT_DIR/logs" "$PROJECT_DIR/tmp/locks"
chown -R "$REAL_USER:$REAL_USER" \
    "$PROJECT_DIR/logs" \
    "$PROJECT_DIR/tmp"

# ── Python virtual environment ────────────────────────────────────────────────
header "Python virtual environment"
PY_BIN="$(command -v python3)"
PY_VER="$("$PY_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_MAJOR="$("$PY_BIN" -c 'import sys; print(sys.version_info.major)')"
PY_MINOR="$("$PY_BIN" -c 'import sys; print(sys.version_info.minor)')"

if [[ "$PY_MAJOR" -lt 3 ]] || { [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt 10 ]]; }; then
    err "Python $PY_VER detected — Glove-Core requires Python 3.10 or newer."
    err "Install a newer Python version and re-run this script."
    err "  Ubuntu 22.04+:  sudo apt install python3.11"
    err "  Or use deadsnakes PPA:  sudo add-apt-repository ppa:deadsnakes/ppa"
    exit 1
fi
ok "Python $PY_VER  ($PY_BIN)"

if [[ ! -d "$VENV_DIR" ]]; then
    sudo -u "$REAL_USER" "$PY_BIN" -m venv "$VENV_DIR"
    ok "Created venv: $VENV_DIR"
else
    ok "venv already exists: $VENV_DIR"
fi

# ── Python packages ───────────────────────────────────────────────────────────
header "Python packages"
VENV_PIP="$VENV_DIR/bin/pip"

sudo -u "$REAL_USER" "$VENV_PIP" install --upgrade pip setuptools wheel -q
ok "pip upgraded."

sudo -u "$REAL_USER" "$VENV_PIP" install \
    --requirement "$PROJECT_DIR/requirements.txt" \
    --no-warn-script-location

ok "All packages from requirements.txt installed."

# ── Launch script ─────────────────────────────────────────────────────────────
header "Launch scripts"
ok "To start:  source .venv/bin/activate && python3 main.py --app"

# ── Verify key imports ────────────────────────────────────────────────────────
header "Verifying installation"
VENV_PY="$VENV_DIR/bin/python3"
VERIFY_FAIL=0
check_import() {
    local pkg="$1"
    if sudo -u "$REAL_USER" "$VENV_PY" -c "import $pkg" 2>/dev/null; then
        ok "$pkg"
    else
        warn "Could not import '$pkg' — check manually."
        VERIFY_FAIL=1
    fi
}

check_import flask
check_import flask_socketio
check_import zmq
check_import serial
check_import numpy
check_import cv2
check_import orjson
check_import webview
check_import PyQt5.QtCore

# RealSense is optional (camera may not be connected at install time)
if sudo -u "$REAL_USER" "$VENV_PY" -c "import pyrealsense2" 2>/dev/null; then
    ok "pyrealsense2"
else
    warn "pyrealsense2 not importable — RealSense cameras will not work until SDK loads (may be a runtime issue, not an install issue)."
fi

# ── Final summary ─────────────────────────────────────────────────────────────
echo ""
if [[ $VERIFY_FAIL -eq 0 ]]; then
    echo -e "${GREEN}${BOLD}  ╔══════════════════════════════════════════════════════╗"
    echo -e "  ║  Installation complete!                              ║"
else
    echo -e "${YELLOW}${BOLD}  ╔══════════════════════════════════════════════════════╗"
    echo -e "  ║  Installation finished with warnings — see above.    ║"
fi
echo -e "${BOLD}  ╠══════════════════════════════════════════════════════╣"
echo -e "  ║                                                      ║"
echo -e "  ║  Start:                                              ║"
echo -e "  ║    cd $PROJECT_DIR"
echo -e "  ║    source .venv/bin/activate                         ║"
echo -e "  ║    python3 main.py --app                             ║"
echo -e "  ║                                                      ║"
echo -e "  ║  Browser mode (no display):                          ║"
echo -e "  ║    python3 main.py                                   ║"
echo -e "  ║    → open  http://localhost:5000                     ║"
echo -e "  ║                                                      ║"
echo -e "  ║  Hardware:                                           ║"
echo -e "  ║    ESP32 gateway → /dev/ttyUSB0  (921600 baud)       ║"
echo -e "  ║    RealSense D435i → USB 3.0 port                    ║"
echo -e "  ║                                                      ║"
echo -e "  ║  IMPORTANT: log out + log in for group changes       ║"
echo -e "  ║  (dialout / video) to take effect.                   ║"
echo -e "  ║                                                      ║"
echo -e "  ╚══════════════════════════════════════════════════════╝${NC}"
echo ""
