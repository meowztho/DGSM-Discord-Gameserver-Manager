import json
import os
import sys
import logging
import shutil
import tempfile
from typing import Any, Dict

def _runtime_base_dir() -> str:
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates = [
            os.path.join(exe_dir, "src"),
            os.path.join(os.path.dirname(exe_dir), "src"),
            os.path.join(os.path.dirname(os.path.dirname(exe_dir)), "src"),
            os.path.join(os.getcwd(), "src"),
            exe_dir,
        ]
        for candidate in candidates:
            try:
                if os.path.isdir(candidate):
                    return os.path.abspath(candidate)
            except Exception:
                continue
        return os.path.abspath(exe_dir)
    return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = _runtime_base_dir()
CONFIG_PATH = os.path.join(BASE_DIR, "server_config.json")
CONFIG_BACKUP_PATH = f"{CONFIG_PATH}.bak"
PID_CACHE = os.path.join(BASE_DIR, "server_pids.json")
PLUGIN_TEMPLATES_DIR = os.path.join(BASE_DIR, "plugin_templates")
STEAM_SESSIONS_DIR = os.path.join(BASE_DIR, "steam_sessions")
DB_PATH = os.path.join(BASE_DIR, "server_logs.db")

os.makedirs(STEAM_SESSIONS_DIR, mode=0o700, exist_ok=True)

CONFIG_CACHE = None
CONFIG_LAST_MODIFIED = 0

# --- optionale Validierung mit pydantic (wenn installiert) ---
try:
    from pydantic import BaseModel, Field, ValidationError

    class ServerEntry(BaseModel):
        app_id: str
        executable: str | None = None
        username: str | None = None
        instance_id: str | None = None
        install_dir: str | None = None
        password: str | None = None  # verschlüsselt gespeichert

    class ConfigModel(BaseModel):
        log_retention_days: int = Field(default=7, ge=1, le=3650)
        server_paths: Dict[str, ServerEntry] = Field(default_factory=dict)

    def _validate_and_normalize(cfg_dict: Dict[str, Any]) -> Dict[str, Any]:
        model = ConfigModel(**cfg_dict)
        # zurück in primitives (dict), nicht pydantic-Objekte
        out = {
            "log_retention_days": model.log_retention_days,
            "server_paths": {
                name: entry.model_dump()
                for name, entry in model.server_paths.items()
            },
        }
        return out

except Exception:
    ValidationError = Exception  # type: ignore

    def _validate_and_normalize(cfg_dict: Dict[str, Any]) -> Dict[str, Any]:
        # Fallback: keine Validierung, nur minimaler Schutz
        if "log_retention_days" not in cfg_dict:
            cfg_dict["log_retention_days"] = 7
        if "server_paths" not in cfg_dict or not isinstance(cfg_dict["server_paths"], dict):
            cfg_dict["server_paths"] = {}
        return cfg_dict


def load_config():
    global CONFIG_CACHE, CONFIG_LAST_MODIFIED
    if not os.path.exists(CONFIG_PATH):
        CONFIG_CACHE = {"log_retention_days": 7, "server_paths": {}}
        return CONFIG_CACHE
    current_mtime = os.path.getmtime(CONFIG_PATH)
    if CONFIG_CACHE is None or current_mtime > CONFIG_LAST_MODIFIED:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            CONFIG_CACHE = _validate_and_normalize(raw)
            CONFIG_LAST_MODIFIED = current_mtime
        except ValidationError as e:
            logging.critical(f"Konfigurationsfehler (Schema): {e}")
            CONFIG_CACHE = {"log_retention_days": 7, "server_paths": {}}
        except Exception as e:
            logging.critical(f"Konfigurationsfehler: {e}")
            CONFIG_CACHE = {"log_retention_days": 7, "server_paths": {}}
    return CONFIG_CACHE


def save_config(cfg):
    global CONFIG_CACHE, CONFIG_LAST_MODIFIED
    cfg = _validate_and_normalize(cfg)

    config_dir = os.path.dirname(CONFIG_PATH) or "."
    fd, tmp_path = tempfile.mkstemp(prefix="server_config_", suffix=".tmp", dir=config_dir)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)
            f.flush()
            os.fsync(f.fileno())

        if os.path.exists(CONFIG_PATH):
            try:
                shutil.copy2(CONFIG_PATH, CONFIG_BACKUP_PATH)
            except Exception as e:
                logging.warning(f"Config-Backup konnte nicht geschrieben werden: {e}")

        os.replace(tmp_path, CONFIG_PATH)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    CONFIG_CACHE = cfg
    CONFIG_LAST_MODIFIED = os.path.getmtime(CONFIG_PATH)


from security import decrypt_value  # late import


def get_config_value(server_name: str, key: str):
    cfg = load_config()
    val = cfg["server_paths"].get(server_name, {}).get(key)
    if key == "password" and val:
        return decrypt_value(val)
    return val


def get_log_retention_days():
    return load_config().get("log_retention_days", 7)
