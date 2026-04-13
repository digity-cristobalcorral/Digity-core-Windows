#!/usr/bin/env python3
"""
Post-install setup: creates the Python virtual environment and installs
all dependencies from requirements.txt.

Called by the Inno Setup installer after copying files.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent  # install root (where main.py lives)


def run(*args, **kwargs):
    result = subprocess.run(args, **kwargs)
    if result.returncode != 0:
        print(f"[ERROR] Command failed: {' '.join(str(a) for a in args)}")
        sys.exit(result.returncode)


def main():
    venv    = HERE / ".venv"
    pip     = venv / "Scripts" / "pip.exe"
    reqs    = HERE / "requirements.txt"

    print("=" * 60)
    print("  Digity Core — Python environment setup")
    print("=" * 60)
    print(f"  Python : {sys.executable}")
    print(f"  Venv   : {venv}")
    print()

    print("[1/3] Creating virtual environment...")
    run(sys.executable, "-m", "venv", str(venv))

    print("[2/3] Upgrading pip...")
    run(str(pip), "install", "--upgrade", "pip", "-q")

    print("[3/3] Installing packages (this may take a few minutes)...")
    run(str(pip), "install", "-r", str(reqs), "--no-warn-script-location")

    print()
    print("=" * 60)
    print("  Setup complete!  Launch with launch.bat")
    print("=" * 60)


if __name__ == "__main__":
    main()
