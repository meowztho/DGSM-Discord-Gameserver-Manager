import json
import os
from pathlib import Path
from typing import Dict

from config_store import load_config, BASE_DIR

SERVER_PATHS: Dict[str, str] = {}
SERVER_CONFIGS: Dict[str, dict] = {}


def server_root(app_id: str) -> Path:
    """Basisordner eines Steam-Servers (GSM-Struktur)."""
    return Path(BASE_DIR) / "steam" / "GSM" / "servers" / str(app_id)


def server_files(app_id: str) -> Path:
    """serverfiles/-Ordner einer App-ID."""
    return server_root(app_id) / "serverfiles"


def load_server_paths():
    """Update in-place so existing imports see changes."""
    cfg = load_config()
    new_map = {
        name: str(server_files(info["app_id"]))
        for name, info in cfg.get("server_paths", {}).items()
    }
    SERVER_PATHS.clear()
    SERVER_PATHS.update(new_map)


def load_server_configs():
    """Update in-place so existing imports see changes."""
    SERVER_CONFIGS.clear()
    for name, path in SERVER_PATHS.items():
        settings_file = os.path.join(path, "server_settings.json")
        if os.path.exists(settings_file):
            try:
                with open(settings_file, "r", encoding="utf-8") as f:
                    SERVER_CONFIGS[name] = json.load(f)
            except Exception:
                SERVER_CONFIGS[name] = {}
        else:
            SERVER_CONFIGS[name] = {}
