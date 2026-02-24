import json
import os
import re
from typing import Any, Dict, List

from platform_utils import is_windows, is_linux


_STOP_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    txt = str(value).strip().lower()
    if txt in ("1", "true", "yes", "on"):
        return True
    if txt in ("0", "false", "no", "off"):
        return False
    return default


def _as_str(value: Any) -> str:
    return str(value or "").strip()


def _as_parameters(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        out: List[str] = []
        for item in value:
            txt = _as_str(item)
            if txt:
                out.append(txt)
        return out
    txt = _as_str(value)
    if not txt:
        return []
    return [part for part in txt.split() if part]


def current_os_executable_key() -> str:
    if is_windows():
        return "executable_windows"
    if is_linux():
        return "executable_linux"
    return ""


def normalize_server_settings(raw: Dict[str, Any]) -> Dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    executable = _as_str(src.get("executable"))
    executable_windows = _as_str(src.get("executable_windows") or src.get("executable_win"))
    executable_linux = _as_str(src.get("executable_linux") or src.get("executable_unix"))

    if not executable:
        executable = executable_windows or executable_linux

    stop_time = _as_str(src.get("stop_time") or "05:00")
    if stop_time and not _STOP_TIME_RE.match(stop_time):
        stop_time = "05:00"

    return {
        "executable": executable,
        "executable_windows": executable_windows,
        "executable_linux": executable_linux,
        "parameters": _as_parameters(src.get("parameters")),
        "auto_update": _as_bool(src.get("auto_update"), True),
        "auto_restart": _as_bool(src.get("auto_restart"), True),
        "stop_time": stop_time,
        "restart_after_stop": _as_bool(src.get("restart_after_stop"), False),
    }


def with_detected_executable(settings: Dict[str, Any], executable: str) -> Dict[str, Any]:
    out = normalize_server_settings(settings)
    exe = _as_str(executable)
    if not exe:
        return out

    if not _as_str(out.get("executable")):
        out["executable"] = exe
    os_key = current_os_executable_key()
    if os_key and not _as_str(out.get(os_key)):
        out[os_key] = exe
    return out


def normalize_template_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    settings = normalize_server_settings(src)

    out = {
        "app_id": _as_str(src.get("app_id")),
        "executable": settings["executable"],
        "executable_windows": settings["executable_windows"],
        "executable_linux": settings["executable_linux"],
        "auto_update": settings["auto_update"],
        "auto_restart": settings["auto_restart"],
        "stop_time": settings["stop_time"],
        "restart_after_stop": settings["restart_after_stop"],
        "parameters": settings["parameters"],
    }

    username = _as_str(src.get("username"))
    password = _as_str(src.get("password"))
    if username and password:
        out["username"] = username
        out["password"] = password

    return out


def template_settings_from_config(template_cfg: Dict[str, Any]) -> Dict[str, Any]:
    return normalize_server_settings(template_cfg)


def template_effective_executable(template_cfg: Dict[str, Any]) -> str:
    cfg = normalize_template_config(template_cfg)
    if is_windows() and _as_str(cfg.get("executable_windows")):
        return _as_str(cfg.get("executable_windows"))
    if is_linux() and _as_str(cfg.get("executable_linux")):
        return _as_str(cfg.get("executable_linux"))
    return _as_str(cfg.get("executable"))


def build_template_config(
    app_id: str,
    executable: str,
    parameters: List[str],
    auto_update: bool,
    auto_restart: bool,
    stop_time: str,
    restart_after_stop: bool,
    username: str = "",
    password: str = "",
) -> Dict[str, Any]:
    raw: Dict[str, Any] = {
        "app_id": _as_str(app_id),
        "executable": _as_str(executable),
        "parameters": parameters,
        "auto_update": bool(auto_update),
        "auto_restart": bool(auto_restart),
        "stop_time": _as_str(stop_time) or "05:00",
        "restart_after_stop": bool(restart_after_stop),
    }
    if _as_str(username) and _as_str(password):
        raw["username"] = _as_str(username)
        raw["password"] = _as_str(password)
    return normalize_template_config(raw)


def read_template_config(template_dir: str) -> Dict[str, Any]:
    cfg_path = os.path.join(template_dir, "config.json")
    if not os.path.isfile(cfg_path):
        return normalize_template_config({})
    with open(cfg_path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    return normalize_template_config(raw)


def write_template_files(template_dir: str, template_cfg: Dict[str, Any]) -> None:
    os.makedirs(template_dir, exist_ok=True)
    cfg = normalize_template_config(template_cfg)
    settings = template_settings_from_config(cfg)

    with open(os.path.join(template_dir, "config.json"), "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, indent=4)
    with open(os.path.join(template_dir, "server_settings.json"), "w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=4)
