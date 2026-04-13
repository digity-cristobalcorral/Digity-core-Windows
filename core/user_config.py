"""
User-editable configuration stored at ~/.glove/config.json.

Call load() to get current values; call save(data) to persist changes.
All producers read from here at startup so services only need a restart
to pick up new values — no code editing required.
"""
import json
from pathlib import Path

from core.platform_helpers import get_config_dir, get_default_serial_port

CONFIG_PATH = get_config_dir() / "config.json"

DEFAULTS: dict = {
    "station_name":       "Station 1",
    "camera_pov_serial":  "843112072148",
    "camera_pov2_serial": "818312070414",
    "exo_serial_port":    get_default_serial_port(),
    "exo_baud":           921600,
}


def load() -> dict:
    """Return merged config (defaults + saved overrides)."""
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text())
            cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
        except Exception:
            pass
    return cfg


def save(data: dict) -> None:
    """Persist only known keys to ~/.glove/config.json."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    current = load()
    current.update({k: v for k, v in data.items() if k in DEFAULTS})
    CONFIG_PATH.write_text(json.dumps(current, indent=2))
