import json
import os
from pathlib import Path
from typing import Dict, Optional

from config_store import load_config, BASE_DIR

SERVER_PATHS: Dict[str, str] = {}
SERVER_CONFIGS: Dict[str, dict] = {}


def sanitize_instance_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(value))
    cleaned = cleaned.strip("_")
    return cleaned or "instance"


def server_root(app_id: str, instance_id: Optional[str] = None) -> Path:
    """Basisordner eines Steam-Servers (GSM-Struktur)."""
    base = Path(BASE_DIR) / "steam" / "GSM" / "servers" / str(app_id)
    if instance_id:
        return base / "instances" / sanitize_instance_id(instance_id)
    return base


def server_files(app_id: str, instance_id: Optional[str] = None) -> Path:
    """serverfiles/-Ordner einer App-ID, optional instanzbezogen."""
    return server_root(app_id, instance_id=instance_id) / "serverfiles"


def server_files_for_entry(name: str, info: dict) -> Path:
    if not isinstance(info, dict):
        return server_files(sanitize_instance_id(name))

    install_dir = info.get("install_dir")
    if isinstance(install_dir, str) and install_dir.strip():
        p = Path(install_dir.strip())
        if not p.is_absolute():
            p = Path(BASE_DIR) / p
        return p

    app_id = str(info.get("app_id", "")).strip()
    instance_id = info.get("instance_id")
    if isinstance(instance_id, str) and instance_id.strip():
        return server_files(app_id, instance_id=instance_id)

    # Legacy-Layout (ohne Instanz-ID) bleibt bewusst erhalten.
    return server_files(app_id)


def load_server_paths():
    """Update in-place so existing imports see changes."""
    cfg = load_config()
    new_map = {
        name: str(server_files_for_entry(name, info))
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
