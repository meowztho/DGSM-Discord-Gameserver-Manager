
from __future__ import annotations

import asyncio
import shutil
import json
import logging
import os
import re
import shlex
import sqlite3
import stat
import threading
import time
import zipfile
from collections import deque
from datetime import datetime
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from config_store import DB_PATH, BASE_DIR, PLUGIN_TEMPLATES_DIR, load_config, save_config
from db import write_action_log
from paths import SERVER_CONFIGS, SERVER_PATHS, load_server_configs, load_server_paths, sanitize_instance_id, server_root
from runtime_status import (
    begin_operation,
    clear_server_status,
    end_operation_failed,
    end_operation_success,
    get_operation_status,
)
from server_manager import server_processes, start_server, stop_server
from steam_integration import run_update
from security import encrypt_value

try:
    import psutil
except Exception:
    psutil = None  # type: ignore[assignment]

try:
    from PySide6.QtCore import QObject, Qt, QTimer, Signal
    from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QDialog,
        QFrame,
        QGraphicsDropShadowEffect,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QSplitter,
        QVBoxLayout,
        QWidget,
    )
except Exception:
    QApplication = None  # type: ignore[assignment]
    QIcon = None  # type: ignore[assignment]
    QPixmap = None  # type: ignore[assignment]


_LOOP: Optional[asyncio.AbstractEventLoop] = None
_REFRESH_CALLBACK: Optional[Callable[[], Awaitable[None]]] = None
_START_LOCK = threading.Lock()
_STARTED = False
_CONSOLE_HIDDEN = False
_LOG_HANDLER_INSTALLED = False

_QT_APP = None
_QT_WINDOW = None
_QT_PUMP_TASK: Optional[asyncio.Task] = None

_ACTION_FEEDBACK: Dict[str, Dict[str, str | int]] = {}
_LIVE_LOG_LINES: deque[str] = deque(maxlen=1200)
_STOP_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_APP_STARTED_TS = time.time()

_THEME = {
    "bg": "#131826",
    "surface1": "#1b2235",
    "surface2": "#20283d",
    "surface3": "#252f47",
    "surface4": "#2b3652",
    "text": "#edf2ff",
    "muted": "#a8b4d1",
    "accent": "#4f81ff",
    "accent2": "#67d4ff",
    "ok": "#3ecf8e",
    "warn": "#ffb34a",
    "danger": "#ff5f76",
    "edge_light": "#49577a",
    "edge_dark": "#131a2b",
    "console_bg": "#0f1420",
    "console_text": "#d3e2ff",
}


class _UiLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            line = str(record.getMessage())
        _LIVE_LOG_LINES.append(line)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _clean(msg: str, limit: int = 220) -> str:
    text = " ".join(str(msg or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _find_logo_file() -> Optional[str]:
    base = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base, "Logo.png"),
        os.path.join(base, "logo.png"),
        os.path.join(base, "..", "docs", "Logo.png"),
        os.path.join(base, "..", "docs", "logo.png"),
        os.path.join(base, "docs", "images", "Logo.png"),
        os.path.join(base, "docs", "images", "logo.png"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _load_logo_icon() -> Optional[QIcon]:
    if QIcon is None:
        return None
    path = _find_logo_file()
    if not path:
        return None
    icon = QIcon(path)
    if icon.isNull():
        return None
    return icon


def _trim_transparent_logo(pix: QPixmap) -> QPixmap:
    try:
        image = pix.toImage()
    except Exception:
        return pix
    if image.isNull() or not image.hasAlphaChannel():
        return pix

    w = image.width()
    h = image.height()
    if w <= 1 or h <= 1:
        return pix

    def _row_has_visible(y: int) -> bool:
        for x in range(w):
            if image.pixelColor(x, y).alpha() > 0:
                return True
        return False

    def _col_has_visible(x: int, y0: int, y1: int) -> bool:
        for y in range(y0, y1 + 1):
            if image.pixelColor(x, y).alpha() > 0:
                return True
        return False

    top = 0
    while top < h and not _row_has_visible(top):
        top += 1
    if top >= h:
        return pix

    bottom = h - 1
    while bottom >= top and not _row_has_visible(bottom):
        bottom -= 1

    left = 0
    while left < w and not _col_has_visible(left, top, bottom):
        left += 1

    right = w - 1
    while right >= left and not _col_has_visible(right, top, bottom):
        right -= 1

    if left <= 0 and top <= 0 and right >= w - 1 and bottom >= h - 1:
        return pix
    rect = image.copy(left, top, right - left + 1, bottom - top + 1)
    return QPixmap.fromImage(rect)


def _load_logo_pixmap(height: int = 42) -> Optional[QPixmap]:
    if QPixmap is None:
        return None
    path = _find_logo_file()
    if not path:
        return None
    pix = QPixmap(path)
    if pix.isNull():
        return None
    pix = _trim_transparent_logo(pix)
    if height > 0:
        return pix.scaledToHeight(height, Qt.TransformationMode.SmoothTransformation)
    return pix


def _set_windows_app_user_model_id() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        app_id = "DGSM.DesktopUI"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        logging.exception("Desktop UI: could not set AppUserModelID")


def _human_bytes(value: float) -> str:
    amount = float(max(0.0, value))
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    idx = 0
    while amount >= 1024.0 and idx < len(units) - 1:
        amount /= 1024.0
        idx += 1
    if amount >= 100 or idx == 0:
        return f"{amount:.0f}{units[idx]}"
    return f"{amount:.1f}{units[idx]}"


def _format_uptime(seconds: float) -> str:
    total = int(max(0, seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d {hours:02d}h"
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _collect_system_metrics(rows: Optional[List[Dict[str, object]]] = None) -> Dict[str, str]:
    running = 0
    if rows:
        running = sum(1 for row in rows if str(row.get("state", "")) == "running")
    metrics = {
        "cpu": "n/a",
        "ram": "n/a",
        "disk": "n/a",
        "network": "n/a",
        "servers": str(running),
        "uptime": _format_uptime(time.time() - _APP_STARTED_TS),
    }
    if psutil is None:
        return metrics

    try:
        cpu = psutil.cpu_percent(interval=None)
        metrics["cpu"] = f"{cpu:.0f}%"
    except Exception:
        pass
    try:
        mem = psutil.virtual_memory()
        metrics["ram"] = f"{mem.percent:.0f}%"
    except Exception:
        pass
    try:
        disk = psutil.disk_usage(BASE_DIR)
        metrics["disk"] = f"{disk.percent:.0f}% ({_human_bytes(disk.free)} free)"
    except Exception:
        pass
    try:
        net = psutil.net_io_counters()
        metrics["network"] = f"↑ {_human_bytes(net.bytes_sent)} ↓ {_human_bytes(net.bytes_recv)}"
    except Exception:
        pass
    return metrics


def _bootstrap_log_lines() -> None:
    if _LIVE_LOG_LINES:
        return
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            lines = handle.readlines()[-260:]
        for line in lines:
            _LIVE_LOG_LINES.append(line.rstrip("\r\n"))
    except Exception:
        logging.exception("Desktop UI: could not bootstrap bot.log history")


def _install_live_log_handler() -> None:
    global _LOG_HANDLER_INSTALLED
    if _LOG_HANDLER_INSTALLED:
        return
    handler = _UiLogHandler(level=logging.INFO)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    _LOG_HANDLER_INSTALLED = True


def _hide_console_window() -> None:
    global _CONSOLE_HIDDEN
    if os.name != "nt":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
            _CONSOLE_HIDDEN = True
    except Exception:
        logging.exception("Desktop UI: hide console failed")


def _show_console_window() -> None:
    global _CONSOLE_HIDDEN
    if os.name != "nt" or not _CONSOLE_HIDDEN:
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 5)
    except Exception:
        logging.exception("Desktop UI: show console failed")
    finally:
        _CONSOLE_HIDDEN = False


def _is_running(name: str) -> bool:
    try:
        proc = server_processes.get(name)
        return bool(proc and proc.is_running())
    except Exception:
        return False


def _status_for(name: str) -> Tuple[str, str, str]:
    state, detail = get_operation_status(name)
    if state == "busy":
        label = (detail or "").strip().lower()
        if label == "start":
            return "STARTING", "updating", "Start in progress"
        if label == "stop":
            return "STOPPING", "updating", "Stop in progress"
        if label == "update":
            return "UPDATING", "updating", "Update in progress"
        if label == "backup":
            return "BACKUP", "updating", "Backup in progress"
        if label == "restore":
            return "RESTORE", "updating", "Restore in progress"
        return "WORKING", "updating", "Operation in progress"

    if state == "failed":
        return "FAILED", "failed", _clean(detail or "Last operation failed", 140)

    if _is_running(name):
        return "RUNNING", "running", "Server process is active"
    return "STOPPED", "stopped", "Server process is not active"


def _feedback(name: str, status: str, message: str) -> None:
    _ACTION_FEEDBACK[name] = {
        "status": status,
        "message": _clean(message),
        "timestamp": int(time.time()),
    }


def _settings_for(name: str) -> Dict[str, str | bool]:
    cfg = SERVER_CONFIGS.get(name, {})
    params = cfg.get("parameters", [])
    if isinstance(params, list):
        params_text = " ".join(str(x) for x in params)
    elif isinstance(params, str):
        params_text = params
    else:
        params_text = ""

    return {
        "executable": str(cfg.get("executable", "") or ""),
        "parameters": params_text,
        "auto_update": bool(cfg.get("auto_update", False)),
        "auto_restart": bool(cfg.get("auto_restart", True)),
        "restart_after_stop": bool(cfg.get("restart_after_stop", False)),
        "stop_time": str(cfg.get("stop_time", "") or ""),
    }


async def _refresh_discord_panel() -> None:
    if _REFRESH_CALLBACK is None:
        return
    try:
        await _REFRESH_CALLBACK()
    except Exception:
        logging.exception("Desktop UI: Discord refresh callback failed")


async def _safe_refresh_discord_panel(timeout: float = 8.0) -> None:
    try:
        await asyncio.wait_for(_refresh_discord_panel(), timeout=timeout)
    except asyncio.TimeoutError:
        logging.warning("Desktop UI: Discord refresh timeout after %.1fs", timeout)
    except Exception:
        logging.exception("Desktop UI: Discord refresh failed")


def _schedule_discord_refresh() -> None:
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_safe_refresh_discord_panel())
    except Exception:
        pass


def notify_external_refresh(reason: str = "") -> None:
    if not _STARTED:
        return
    loop = _LOOP
    if loop is None:
        return

    def _kick() -> None:
        window = _QT_WINDOW
        if window is None:
            return
        try:
            if reason:
                window.set_status(_clean(f"Discord sync: {reason}", 200))
            window.refresh_data()
        except Exception:
            logging.exception("Desktop UI external refresh failed")

    try:
        loop.call_soon_threadsafe(_kick)
    except Exception:
        pass

async def _wait_busy_or_done(name: str, task: asyncio.Task) -> None:
    for _ in range(32):
        state, _detail = get_operation_status(name)
        if state == "busy" or task.done():
            break
        await asyncio.sleep(0.05)


async def _collect_servers() -> List[Dict[str, object]]:
    load_server_paths()
    load_server_configs()

    out: List[Dict[str, object]] = []
    names = sorted(SERVER_PATHS.keys(), key=lambda item: str(item).lower())
    for index, name in enumerate(names, start=1):
        status_label, state_key, detail = _status_for(name)
        fb = _ACTION_FEEDBACK.get(name)
        feedback = f"[{fb.get('status')}] {fb.get('message')}" if fb else ""

        out.append(
            {
                "id": index,
                "name": name,
                "status": status_label,
                "state": state_key,
                "detail": detail,
                "running": _is_running(name),
                "feedback": feedback,
                "settings": _settings_for(name),
            }
        )
    return out


def _collect_history(limit: int = 90) -> List[Tuple[str, str, str, str, str]]:
    conn = None
    rows: List[Tuple[str, str, str, str, str]] = []
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "SELECT timestamp, action, server, status, details FROM logs ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
    except Exception:
        logging.exception("Desktop UI: could not load history")
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass
    return rows


async def _run_action(name: str, action: str) -> Tuple[bool, str]:
    ok = False
    message = ""
    try:
        if action == "start":
            task = asyncio.create_task(start_server(name))
            await _wait_busy_or_done(name, task)
            await _safe_refresh_discord_panel()
            ok = await task
            message = "Server started" if ok else "Start failed"

        elif action == "stop":
            task = asyncio.create_task(stop_server(name))
            await _wait_busy_or_done(name, task)
            await _safe_refresh_discord_panel()
            ok = await task
            message = "Server stopped" if ok else "Stop failed"

        elif action == "update":
            task = asyncio.create_task(run_update(name))
            await _wait_busy_or_done(name, task)
            await _safe_refresh_discord_panel()
            ok, msg = await task
            message = msg or ("Update completed" if ok else "Update failed")

        elif action == "restart":
            if _is_running(name):
                stop_task = asyncio.create_task(stop_server(name))
                await _wait_busy_or_done(name, stop_task)
                await _safe_refresh_discord_panel()
                if not await stop_task:
                    ok = False
                    message = "Restart failed while stopping"
                    _feedback(name, "failed", message)
                    return ok, message

            start_task = asyncio.create_task(start_server(name))
            await _wait_busy_or_done(name, start_task)
            await _safe_refresh_discord_panel()
            ok = await start_task
            message = "Server restarted" if ok else "Restart failed while starting"
            write_action_log("ui_restart", name, "success" if ok else "failed", message)

        else:
            message = f"Unknown action: {action}"

        _feedback(name, "success" if ok else "failed", message)
        return ok, message
    except Exception as exc:
        logging.exception("Desktop UI action failed: %s %s", action, name)
        message = str(exc)
        _feedback(name, "failed", message)
        _schedule_discord_refresh()
        return False, message
    finally:
        _schedule_discord_refresh()


async def _save_settings(
    name: str,
    executable: str,
    parameters: str,
    auto_update: bool,
    auto_restart: bool,
    restart_after_stop: bool,
    stop_time: str,
) -> Tuple[bool, str]:
    def _fail(message: str) -> Tuple[bool, str]:
        _feedback(name, "failed", message)
        try:
            write_action_log("ui_updatesettings", name, "failed", message)
        except Exception:
            pass
        _schedule_discord_refresh()
        return False, message

    try:
        load_server_paths()
        if name not in SERVER_PATHS:
            return _fail(f"Unknown server: {name}")

        normalized_stop_time = str(stop_time or "").strip()
        if normalized_stop_time and not _STOP_TIME_RE.match(normalized_stop_time):
            return _fail("Invalid stop time. Use HH:MM or leave empty.")

        params_text = str(parameters or "").strip()
        try:
            param_list = shlex.split(params_text, posix=False) if params_text else []
        except ValueError as exc:
            return _fail(f"Invalid parameters: {exc}")

        path = os.path.join(SERVER_PATHS[name], "server_settings.json")
        cfg: Dict[str, object] = {}
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    raw = json.load(handle)
                if isinstance(raw, dict):
                    cfg = raw
            except Exception:
                cfg = {}

        cfg["executable"] = str(executable or "").strip()
        cfg["parameters"] = param_list
        cfg["auto_update"] = bool(auto_update)
        cfg["auto_restart"] = bool(auto_restart)
        cfg["restart_after_stop"] = bool(restart_after_stop)
        cfg["stop_time"] = normalized_stop_time

        with open(path, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, indent=4)

        load_server_configs()
        write_action_log(
            "ui_updatesettings",
            name,
            "success",
            (
                f"auto_update={auto_update} auto_restart={auto_restart} "
                f"restart_after_stop={restart_after_stop} stop_time='{normalized_stop_time or '-'}'"
            ),
        )
        _feedback(name, "success", "Settings saved")
        _schedule_discord_refresh()
        return True, "Settings saved"
    except Exception as exc:
        logging.exception("Desktop UI save settings failed: %s", name)
        return _fail(str(exc))


def _backup_dir() -> str:
    path = os.path.join(BASE_DIR, "steam", "backup")
    os.makedirs(path, exist_ok=True)
    return path


def _legacy_backup_dir() -> str:
    return os.path.join(BASE_DIR, "backups")


def _backup_search_dirs() -> List[str]:
    dirs = [_backup_dir()]
    legacy = _legacy_backup_dir()
    if os.path.isdir(legacy):
        dirs.append(legacy)
    return dirs


def _list_backup_files() -> List[str]:
    files: List[str] = []
    seen = set()
    for d in _backup_search_dirs():
        try:
            for entry in os.listdir(d):
                key = entry.lower()
                if key in seen:
                    continue
                if key.endswith(".zip") and os.path.isfile(os.path.join(d, entry)):
                    files.append(entry)
                    seen.add(key)
        except Exception:
            continue
    files.sort(reverse=True)
    return files


def _resolve_backup_path(name: str) -> Optional[str]:
    filename = os.path.basename(str(name or "").strip())
    if not filename:
        return None
    for d in _backup_search_dirs():
        path = os.path.abspath(os.path.join(d, filename))
        root = os.path.abspath(d)
        try:
            if os.path.commonpath([root, path]) != root:
                continue
        except Exception:
            continue
        if os.path.isfile(path):
            return path
    return None


def _backup_display(path: str) -> str:
    filename = os.path.basename(path)
    full = os.path.abspath(path)
    primary = os.path.abspath(_backup_dir())
    legacy = os.path.abspath(_legacy_backup_dir())
    try:
        if os.path.commonpath([primary, full]) == primary:
            return f"/steam/backup/{filename}"
    except Exception:
        pass
    try:
        if os.path.commonpath([legacy, full]) == legacy:
            return f"/backups/{filename}"
    except Exception:
        pass
    return filename


def _zip_members_are_safe(zip_ref: zipfile.ZipFile, target_dir: str) -> bool:
    target_root = os.path.abspath(target_dir)
    for member in zip_ref.namelist():
        target_path = os.path.abspath(os.path.join(target_dir, member))
        try:
            if os.path.commonpath([target_root, target_path]) != target_root:
                return False
        except Exception:
            return False
    return True


def _delete_root_from_server_path(path: str) -> str:
    p = os.path.abspath(path)
    if os.path.basename(p).lower() == "serverfiles":
        return os.path.dirname(p)
    return p


def _rmtree_onerror(func, path, exc_info) -> None:  # type: ignore[no-untyped-def]
    err = exc_info
    if isinstance(exc_info, tuple):
        err = exc_info[1] if len(exc_info) > 1 else None
    if isinstance(err, PermissionError):
        try:
            mode = stat.S_IWRITE | stat.S_IREAD
            if os.path.isdir(path):
                mode |= stat.S_IEXEC
            os.chmod(path, mode)
            result = func(path)
            if hasattr(result, "close"):
                try:
                    result.close()
                except Exception:
                    pass
            return
        except Exception:
            pass
    if err is not None:
        raise err
    raise RuntimeError(f"Could not remove path: {path}")


def _blocking_rmtree(path: str) -> None:
    if os.name == "nt":
        try:
            shutil.rmtree(path, onexc=_rmtree_onerror)  # Python 3.12+
            return
        except TypeError:
            shutil.rmtree(path, onerror=_rmtree_onerror)
            return
    shutil.rmtree(path)


async def _rmtree_with_retry(path: str, retries: int = 5, base_delay: float = 0.35) -> None:
    last_exc: Optional[BaseException] = None
    for attempt in range(retries):
        try:
            await asyncio.to_thread(_blocking_rmtree, path)
            return
        except FileNotFoundError:
            return
        except PermissionError as exc:
            last_exc = exc
        except OSError as exc:
            if getattr(exc, "winerror", None) == 5:
                last_exc = exc
            else:
                raise
        await asyncio.sleep(base_delay * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"Could not delete '{path}'")


def _is_access_denied_error(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if getattr(exc, "winerror", None) == 5:
        return True
    msg = str(exc).lower()
    if "winerror 5" in msg or "access denied" in msg or "zugriff verweigert" in msg:
        return True
    return False


def _chmod_tree_writable(path: str) -> None:
    root = os.path.abspath(path)
    if not os.path.exists(root):
        return
    for base, dirs, files in os.walk(root, topdown=False):
        for name in files:
            p = os.path.join(base, name)
            try:
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            except Exception:
                pass
        for name in dirs:
            p = os.path.join(base, name)
            try:
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            except Exception:
                pass
    try:
        mode = stat.S_IWRITE | stat.S_IREAD
        if os.path.isdir(root):
            mode |= stat.S_IEXEC
        os.chmod(root, mode)
    except Exception:
        pass


def _extract_zip_archive_safe(archive_path: str, target_dir: str, skip_access_denied: bool = False) -> List[str]:
    skipped: List[str] = []
    root = os.path.abspath(target_dir)
    with zipfile.ZipFile(archive_path, "r") as archive:
        if not _zip_members_are_safe(archive, root):
            raise RuntimeError("Backup archive contains invalid paths")
        for info in archive.infolist():
            name = info.filename or ""
            if not name:
                continue
            dest = os.path.abspath(os.path.join(root, name))
            try:
                if os.path.commonpath([root, dest]) != root:
                    raise RuntimeError("Backup archive contains invalid paths")
            except Exception:
                raise RuntimeError("Backup archive contains invalid paths")

            if info.is_dir() or name.endswith("/"):
                try:
                    os.makedirs(dest, exist_ok=True)
                    os.chmod(dest, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
                except Exception as exc:
                    if skip_access_denied and _is_access_denied_error(exc):
                        skipped.append(name)
                        continue
                    raise
                continue

            parent = os.path.dirname(dest)
            try:
                os.makedirs(parent, exist_ok=True)
                os.chmod(parent, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            except Exception as exc:
                if skip_access_denied and _is_access_denied_error(exc):
                    skipped.append(name)
                    continue
                raise
            try:
                if os.path.isdir(dest):
                    _blocking_rmtree(dest)
                elif os.path.exists(dest):
                    try:
                        os.chmod(dest, stat.S_IWRITE | stat.S_IREAD)
                    except Exception:
                        pass
                    os.remove(dest)
            except Exception as exc:
                if skip_access_denied and _is_access_denied_error(exc):
                    skipped.append(name)
                    continue
                raise

            try:
                with archive.open(info, "r") as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
            except Exception as exc:
                if skip_access_denied and _is_access_denied_error(exc):
                    skipped.append(name)
                    continue
                raise
    return skipped


def _create_directory_backup(path: str, server_name: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(_backup_dir(), f"{sanitize_instance_id(server_name)}-{stamp}")
    return shutil.make_archive(base, "zip", root_dir=path)


def _make_instance_id(cfg: dict, app_id: str, preferred: str) -> str:
    base = sanitize_instance_id(preferred)
    used = set()
    for entry in cfg.get("server_paths", {}).values():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("app_id")) != str(app_id):
            continue
        iid = entry.get("instance_id")
        if isinstance(iid, str) and iid.strip():
            used.add(sanitize_instance_id(iid))
    if base not in used:
        return base
    n = 2
    while f"{base}-{n}" in used:
        n += 1
    return f"{base}-{n}"


def _instance_id_exists(cfg: dict, app_id: str, instance_id: str) -> bool:
    wanted = sanitize_instance_id(instance_id)
    for entry in cfg.get("server_paths", {}).values():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("app_id")) != str(app_id):
            continue
        existing = entry.get("instance_id")
        if isinstance(existing, str) and sanitize_instance_id(existing) == wanted:
            return True
    return False


def _target_server_path(server_name: str, cfg: Optional[dict] = None) -> Optional[str]:
    if server_name in SERVER_PATHS:
        return SERVER_PATHS[server_name]
    if cfg is None:
        cfg = load_config()
    entry = cfg.get("server_paths", {}).get(server_name)
    if not isinstance(entry, dict):
        return None
    app_id = str(entry.get("app_id", "")).strip()
    if not app_id:
        return None
    iid = entry.get("instance_id")
    if isinstance(iid, str) and iid.strip():
        return os.path.join(str(server_root(app_id, instance_id=iid)), "serverfiles")
    return os.path.join(str(server_root(app_id)), "serverfiles")


def _list_templates() -> List[str]:
    out: List[str] = []
    try:
        for entry in os.listdir(PLUGIN_TEMPLATES_DIR):
            full = os.path.join(PLUGIN_TEMPLATES_DIR, entry)
            if os.path.isdir(full):
                out.append(entry)
    except Exception:
        return []
    out.sort(key=str.lower)
    return out


async def _create_template_action(
    template_name: str,
    app_id: str,
    executable: str,
    parameters: str,
    auto_update: bool,
    auto_restart: bool,
    stop_time: str,
    restart_after_stop: bool,
    username: str = "",
    password: str = "",
) -> Tuple[bool, str]:
    name = sanitize_instance_id(template_name)
    if not template_name.strip():
        return False, "Template name required"
    if not app_id.strip():
        return False, "Steam App ID required"
    if not executable.strip():
        return False, "Executable required"

    stop = str(stop_time or "").strip()
    if stop and not _STOP_TIME_RE.match(stop):
        return False, "Invalid stop time. Use HH:MM or leave empty."

    params_text = str(parameters or "").strip()
    try:
        param_list = shlex.split(params_text) if params_text else []
    except Exception as exc:
        return False, f"Invalid parameters: {exc}"

    try:
        template_dir = os.path.join(PLUGIN_TEMPLATES_DIR, template_name.strip())
        os.makedirs(template_dir, exist_ok=True)
        config = {
            "app_id": app_id.strip(),
            "executable": executable.strip(),
            "auto_update": bool(auto_update),
            "auto_restart": bool(auto_restart),
            "stop_time": stop,
            "restart_after_stop": bool(restart_after_stop),
            "parameters": param_list,
        }
        if username.strip() and password.strip():
            config["username"] = username.strip()
            config["password"] = password.strip()

        with open(os.path.join(template_dir, "config.json"), "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=4)
        with open(os.path.join(template_dir, "server_settings.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "executable": executable.strip(),
                    "parameters": param_list,
                    "auto_update": bool(auto_update),
                    "auto_restart": bool(auto_restart),
                    "stop_time": stop,
                    "restart_after_stop": bool(restart_after_stop),
                },
                handle,
                indent=4,
            )
        write_action_log("ui_createtemplate", name, "success", f"app_id={app_id.strip()}")
        _schedule_discord_refresh()
        return True, f"Template '{template_name.strip()}' saved"
    except Exception as exc:
        write_action_log("ui_createtemplate", name, "failed", str(exc))
        _schedule_discord_refresh()
        return False, str(exc)


async def _add_server_action(
    name: str,
    template_name: str,
    instance_id: str = "",
    start_after_install: bool = True,
) -> Tuple[bool, str]:
    server_name = name.strip()
    if not server_name:
        return False, "Server name required"
    if not template_name.strip():
        return False, "Template required"

    try:
        template_dir = os.path.join(PLUGIN_TEMPLATES_DIR, template_name.strip())
        cfg_path = os.path.join(template_dir, "config.json")
        if not os.path.isfile(cfg_path):
            return False, "Template config.json not found"
        with open(cfg_path, "r", encoding="utf-8") as handle:
            tcfg = json.load(handle)

        app_id = str(tcfg.get("app_id", "")).strip()
        executable = str(tcfg.get("executable", "")).strip()
        if not app_id or not executable:
            return False, "Template missing app_id/executable"

        cfg = load_config()
        if server_name in cfg.get("server_paths", {}):
            return False, f"Server '{server_name}' already exists"

        if instance_id.strip():
            chosen = sanitize_instance_id(instance_id)
            if _instance_id_exists(cfg, app_id, chosen):
                return False, f"Instance ID '{chosen}' already exists for app_id {app_id}"
        else:
            chosen = _make_instance_id(cfg, app_id, server_name)

        server_dir = os.path.join(str(server_root(app_id, instance_id=chosen)), "serverfiles")
        os.makedirs(server_dir, exist_ok=True)

        for item in os.listdir(template_dir):
            if item == "config.json":
                continue
            src = os.path.join(template_dir, item)
            dst = os.path.join(server_dir, item)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)

        settings = {
            "executable": executable,
            "parameters": tcfg.get("parameters", []),
            "auto_update": tcfg.get("auto_update", True),
            "auto_restart": tcfg.get("auto_restart", True),
            "stop_time": tcfg.get("stop_time", "05:00"),
            "restart_after_stop": tcfg.get("restart_after_stop", False),
        }
        with open(os.path.join(server_dir, "server_settings.json"), "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=4)

        entry = {"app_id": app_id, "executable": executable, "instance_id": chosen}
        if tcfg.get("username") and tcfg.get("password"):
            entry["username"] = str(tcfg["username"])
            entry["password"] = encrypt_value(str(tcfg["password"]))
        cfg.setdefault("server_paths", {})[server_name] = entry
        save_config(cfg)
        load_server_paths()
        load_server_configs()

        ok_update, msg_update = await run_update(server_name)
        if not ok_update:
            write_action_log("ui_addserver", server_name, "failed", msg_update)
            _schedule_discord_refresh()
            return False, f"Install failed: {msg_update}"

        if start_after_install:
            ok_start = await start_server(server_name)
            if not ok_start:
                write_action_log("ui_addserver", server_name, "failed", "installed, start failed")
                _schedule_discord_refresh()
                return False, "Installed but start failed"

        write_action_log("ui_addserver", server_name, "success", f"template={template_name} instance={chosen}")
        _schedule_discord_refresh()
        return True, f"Server '{server_name}' installed"
    except Exception as exc:
        write_action_log("ui_addserver", name or "-", "failed", str(exc))
        _schedule_discord_refresh()
        return False, str(exc)


async def _create_backup_action(server_name: str) -> Tuple[bool, str]:
    name = server_name.strip()
    if not name:
        return False, "Server required"
    load_server_paths()
    cfg = load_config()
    if name not in cfg.get("server_paths", {}) and name not in SERVER_PATHS:
        return False, "Server not found"

    target = _target_server_path(name, cfg)
    if not target:
        return False, "Server path not found"
    backup_root = _delete_root_from_server_path(target)
    if not os.path.exists(backup_root):
        return False, f"Backup path missing: {backup_root}"

    success = False
    fail_reason = ""
    status_finalized = False
    begin_operation(name, "backup")
    await _safe_refresh_discord_panel()
    try:
        archive = await asyncio.to_thread(_create_directory_backup, backup_root, name)
        write_action_log("ui_createbackup", name, "success", os.path.basename(archive))
        _feedback(name, "success", f"Backup created: {_backup_display(archive)}")
        success = True
        end_operation_success(name)
        status_finalized = True
        await _safe_refresh_discord_panel()
        return True, f"Backup created: {_backup_display(archive)}"
    except Exception as exc:
        fail_reason = str(exc)
        write_action_log("ui_createbackup", name, "failed", fail_reason)
        _feedback(name, "failed", fail_reason)
        return False, fail_reason
    finally:
        if not status_finalized:
            if success:
                end_operation_success(name)
            else:
                end_operation_failed(name, fail_reason or "Backup failed")
            await _safe_refresh_discord_panel()


async def _restore_backup_action(name: str, backup_file: str, overwrite: bool = False) -> Tuple[bool, str]:
    server_name = name.strip()
    if not server_name:
        return False, "Server required"
    load_server_paths()
    cfg = load_config()
    if server_name not in cfg.get("server_paths", {}) and server_name not in SERVER_PATHS:
        return False, "Server not found"

    archive_path = _resolve_backup_path(backup_file)
    if not archive_path:
        return False, "Backup file not found"

    if _is_running(server_name):
        return False, "Restore not possible while server is running"

    target = _target_server_path(server_name, cfg)
    if not target:
        return False, "Server path not found"
    restore_root = _delete_root_from_server_path(target)
    restore_root_norm = os.path.normcase(os.path.abspath(restore_root))
    if overwrite:
        for other_name, other_path in SERVER_PATHS.items():
            if other_name == server_name:
                continue
            other_root = os.path.normcase(os.path.abspath(_delete_root_from_server_path(other_path)))
            if other_root == restore_root_norm or other_root.startswith(restore_root_norm + os.sep):
                return False, f"Overwrite denied: folder is shared with {other_name}"
    if os.path.exists(restore_root) and not overwrite:
        try:
            if any(os.scandir(restore_root)):
                return False, "Target folder is not empty. Use overwrite."
        except Exception:
            pass

    success = False
    fail_reason = ""
    status_finalized = False
    begin_operation(server_name, "restore")
    await _safe_refresh_discord_panel()
    try:
        merge_fallback = False
        skipped_locked_entries: List[str] = []
        if os.path.exists(restore_root) and overwrite:
            try:
                await asyncio.to_thread(_chmod_tree_writable, restore_root)
                await _rmtree_with_retry(restore_root)
            except Exception as cleanup_exc:
                if _is_access_denied_error(cleanup_exc):
                    merge_fallback = True
                    logging.warning("Desktop UI restore cleanup fallback for %s: %s", server_name, cleanup_exc)
                else:
                    raise
        os.makedirs(restore_root, exist_ok=True)
        last_extract_error: Optional[BaseException] = None
        for attempt in range(2):
            try:
                skip_locked = attempt >= 1
                skipped_items = await asyncio.to_thread(
                    _extract_zip_archive_safe,
                    archive_path,
                    restore_root,
                    skip_locked,
                )
                if skipped_items:
                    skipped_locked_entries.extend(item for item in skipped_items if item not in skipped_locked_entries)
                    write_action_log(
                        "ui_restorebackup",
                        server_name,
                        "warning",
                        f"skipped locked entries: {', '.join(skipped_items[:6])}",
                    )
                last_extract_error = None
                break
            except Exception as extract_exc:
                last_extract_error = extract_exc
                if _is_access_denied_error(extract_exc):
                    await asyncio.to_thread(_chmod_tree_writable, restore_root)
                    await asyncio.sleep(0.45)
                    continue
                if attempt >= 1:
                    raise
        if last_extract_error is not None:
            raise last_extract_error
        load_server_paths()
        load_server_configs()
        write_action_log("ui_restorebackup", server_name, "success", os.path.basename(archive_path))
        _feedback(server_name, "success", f"Backup restored: {_backup_display(archive_path)}")
        success = True
        end_operation_success(server_name)
        status_finalized = True
        await _safe_refresh_discord_panel()
        if skipped_locked_entries:
            return True, f"Backup restored with skipped locked paths: {', '.join(skipped_locked_entries[:4])}"
        return True, f"Backup restored: {_backup_display(archive_path)}"
    except Exception as exc:
        fail_reason = str(exc)
        if _is_access_denied_error(exc):
            try:
                await asyncio.to_thread(_chmod_tree_writable, restore_root)
            except Exception:
                pass
            fail_reason = (
                f"{fail_reason} (access denied). Close file handles in the target folder "
                "and retry with overwrite enabled."
            )
        write_action_log("ui_restorebackup", server_name, "failed", fail_reason)
        _feedback(server_name, "failed", fail_reason)
        return False, fail_reason
    finally:
        if not status_finalized:
            if success:
                end_operation_success(server_name)
            else:
                end_operation_failed(server_name, fail_reason or "Restore failed")
            await _safe_refresh_discord_panel()


async def _remove_server_action(name: str, backup_before_delete: bool = True) -> Tuple[bool, str]:
    server_name = name.strip()
    if not server_name:
        return False, "Server required"

    try:
        cfg = load_config()
        if server_name not in cfg.get("server_paths", {}):
            return False, "Server not found"

        if _is_running(server_name):
            await stop_server(server_name)

        target = _target_server_path(server_name, cfg)
        if not target:
            return False, "Server path not found"
        delete_root = _delete_root_from_server_path(target)
        delete_norm = os.path.normcase(os.path.abspath(delete_root))

        load_server_paths()
        shared_with = None
        for other_name, other_path in SERVER_PATHS.items():
            if other_name == server_name:
                continue
            other_root = os.path.normcase(os.path.abspath(_delete_root_from_server_path(other_path)))
            if other_root == delete_norm or other_root.startswith(delete_norm + os.sep):
                shared_with = other_name
                break

        if os.path.exists(delete_root) and not shared_with:
            if backup_before_delete:
                archive_path = await asyncio.to_thread(_create_directory_backup, delete_root, server_name)
                write_action_log("ui_removebackup", server_name, "success", os.path.basename(archive_path))
            await _rmtree_with_retry(delete_root)

        cfg = load_config()
        cfg.get("server_paths", {}).pop(server_name, None)
        save_config(cfg)
        load_server_paths()
        load_server_configs()
        clear_server_status(server_name)

        write_action_log("ui_removeserver", server_name, "success", f"shared_with={shared_with or '-'}")
        _schedule_discord_refresh()
        if shared_with:
            return True, f"Server removed (folder kept, shared with {shared_with})"
        return True, "Server removed"
    except Exception as exc:
        write_action_log("ui_removeserver", server_name or "-", "failed", str(exc))
        _schedule_discord_refresh()
        return False, str(exc)


def _build_qss() -> str:
    t = _THEME
    return f"""
QMainWindow#mainWindow {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {t['surface1']}, stop:1 {t['bg']});
}}
QWidget {{ color: {t['text']}; font-family: 'Segoe UI'; font-size: 9pt; }}
QFrame#topBar,QFrame#serverListCard,QFrame#settingsCard,QFrame#settingsInnerCard,QFrame#historyCard,QFrame#logCard,QFrame#serverCard,QFrame#statusBar {{
    background: {t['surface2']}; border-radius: 16px;
    border-top: 1px solid {t['edge_light']}; border-left: 1px solid {t['edge_light']};
    border-right: 1px solid {t['edge_dark']}; border-bottom: 1px solid {t['edge_dark']};
}}
QFrame#metricsRow {{ background: transparent; border: none; }}
QFrame#metricTile {{
    background: {t['surface2']}; border-radius: 14px;
    border-top: 1px solid {t['edge_light']}; border-left: 1px solid {t['edge_light']};
    border-right: 1px solid {t['edge_dark']}; border-bottom: 1px solid {t['edge_dark']};
}}
QFrame#serverCard[selected='true'] {{ background: {t['surface3']}; border-top: 1px solid #6f8fd8; border-left: 1px solid #6f8fd8; }}
QLabel#title {{ font-size: 14pt; font-weight: 700; }}
QLabel#brandLogo {{ background: transparent; }}
QLabel#subtitle,QLabel#serverDetail,QLabel#serverFeedback,QLabel#selectedLine,QLabel#statusText {{ color: {t['muted']}; }}
QLabel#summary {{ color: {t['accent2']}; font-weight: 600; }}
QLabel#sectionTitle {{ font-size: 10pt; font-weight: 700; }}
QLabel#metricTitle {{ color: {t['muted']}; font-size: 8pt; font-weight: 600; }}
QLabel#metricValue {{ color: {t['accent2']}; font-size: 10pt; font-weight: 700; }}
QLabel#meta {{ color: {t['muted']}; font-size: 8pt; font-weight: 600; }}
QLabel#serverName {{ font-size: 10pt; font-weight: 700; }}
QLabel#statusBadge {{ border-radius: 9px; padding: 3px 9px; font-size: 8pt; font-weight: 700; }}
QLabel#statusBadge[state='running'] {{ background: #1a3b2f; color: {t['ok']}; border: 1px solid #2f6a4f; }}
QLabel#statusBadge[state='stopped'] {{ background: #2a3042; color: {t['muted']}; border: 1px solid #3c4764; }}
QLabel#statusBadge[state='updating'] {{ background: #463521; color: {t['warn']}; border: 1px solid #7f5b2b; }}
QLabel#statusBadge[state='failed'] {{ background: #4d2028; color: {t['danger']}; border: 1px solid #8e3a49; }}
QLineEdit,QPlainTextEdit {{ background: {t['surface3']}; border-radius: 12px; border: 1px solid #57688d; padding: 8px; }}
QPlainTextEdit#logOutput {{ background: {t['console_bg']}; color: {t['console_text']}; }}
QPushButton {{ background: {t['surface4']}; border-radius: 14px; border: 1px solid #5a6a90; padding: 7px 14px; font-weight: 700; }}
QPushButton:hover {{ background: #344361; }}
QPushButton:pressed {{ background: #253249; }}
QPushButton:disabled {{ color: #7883a1; background: #2a3247; }}
QPushButton#primaryButton {{ background: {t['accent']}; color: #ffffff; }}
QPushButton#dangerButton {{ background: {t['danger']}; color: #ffffff; }}
QPushButton#ghostButton {{ color: {t['accent2']}; }}
QCheckBox::indicator {{ width: 15px; height: 15px; border-radius: 7px; border: 1px solid #5a6a90; background: {t['surface3']}; }}
QCheckBox::indicator:checked {{ background: {t['accent']}; border: 1px solid {t['accent']}; }}
QScrollArea {{ border: none; background: transparent; }}
"""


def _apply_soft_shadow(widget: QWidget, blur: int = 24, dx: int = 5, dy: int = 5, alpha: int = 150) -> None:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(dx, dy)
    effect.setColor(QColor(8, 13, 24, alpha))
    widget.setGraphicsEffect(effect)

if QApplication is not None:

    class _UiSignals(QObject):
        status_line = Signal(str)


    class TopControlBar(QFrame):
        action_requested = Signal(str)

        def __init__(self) -> None:
            super().__init__()
            self.setObjectName("topBar")
            self.setMinimumHeight(142)
            _apply_soft_shadow(self, blur=30, dx=6, dy=6, alpha=170)

            root = QHBoxLayout(self)
            root.setContentsMargins(14, 12, 14, 12)
            root.setSpacing(12)

            left = QVBoxLayout()
            left.setSpacing(4)
            self.brand_logo = QLabel()
            self.brand_logo.setObjectName("brandLogo")
            self.brand_logo.setMinimumHeight(112)
            self.brand_logo.setMaximumHeight(124)
            self.brand_logo.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.brand_logo.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            logo = _load_logo_pixmap(112)
            if logo is not None:
                self.brand_logo.setPixmap(logo)
            else:
                self.brand_logo.setText("DGSM Desktop")
                self.brand_logo.setObjectName("title")
            self.summary_label = QLabel("Running 0 | Stopped 0 | Updating 0")
            self.summary_label.setObjectName("summary")
            left.addWidget(self.brand_logo)
            left.addWidget(self.summary_label)
            root.addLayout(left, 1)

            actions = QHBoxLayout()
            self.buttons: Dict[str, QPushButton] = {}
            self._add(actions, "start", "Start", "primaryButton")
            self._add(actions, "stop", "Stop", "dangerButton")
            self._add(actions, "restart", "Restart")
            self._add(actions, "update", "Update")
            self._add(actions, "tools", "Tools")
            self._add(actions, "save", "Save CFG")
            self._add(actions, "refresh", "Refresh", "ghostButton")

            right = QVBoxLayout()
            right.addLayout(actions)
            self.selected_label = QLabel("Selected: -")
            self.selected_label.setObjectName("selectedLine")
            right.addWidget(self.selected_label, alignment=Qt.AlignmentFlag.AlignRight)
            root.addLayout(right)

        def _add(self, layout: QHBoxLayout, key: str, label: str, object_name: str = "") -> None:
            btn = QPushButton(label)
            if object_name:
                btn.setObjectName(object_name)
            btn.clicked.connect(lambda _checked=False, v=key: self.action_requested.emit(v))
            layout.addWidget(btn)
            self.buttons[key] = btn

        def set_summary(self, running: int, stopped: int, updating: int) -> None:
            self.summary_label.setText(f"Running {running} | Stopped {stopped} | Updating {updating}")

        def set_selected(self, text: str) -> None:
            self.selected_label.setText(text)

        def set_button_states(self, states: Dict[str, bool]) -> None:
            for key, button in self.buttons.items():
                button.setEnabled(bool(states.get(key, False)))


    class MetricsPanel(QFrame):
        def __init__(self) -> None:
            super().__init__()
            self.setObjectName("metricsRow")
            row = QHBoxLayout(self)
            row.setContentsMargins(2, 0, 2, 0)
            row.setSpacing(8)
            self.values: Dict[str, QLabel] = {}
            self._add_metric(row, "cpu", "CPU")
            self._add_metric(row, "ram", "RAM")
            self._add_metric(row, "disk", "Disk")
            self._add_metric(row, "network", "Network")
            self._add_metric(row, "servers", "Running")
            self._add_metric(row, "uptime", "Uptime")

        def _add_metric(self, row: QHBoxLayout, key: str, title: str) -> None:
            tile = QFrame()
            tile.setObjectName("metricTile")
            _apply_soft_shadow(tile, blur=20, dx=4, dy=4, alpha=145)
            layout = QVBoxLayout(tile)
            layout.setContentsMargins(10, 8, 10, 8)
            layout.setSpacing(3)
            title_lbl = QLabel(title)
            title_lbl.setObjectName("metricTitle")
            value_lbl = QLabel("--")
            value_lbl.setObjectName("metricValue")
            layout.addWidget(title_lbl)
            layout.addWidget(value_lbl)
            row.addWidget(tile)
            self.values[key] = value_lbl

        def update_metrics(self, data: Dict[str, str]) -> None:
            for key, lbl in self.values.items():
                lbl.setText(str(data.get(key, "--")))


    class ServerCard(QFrame):
        selected = Signal(str)
        action_requested = Signal(str, str)

        def __init__(self, row: Dict[str, object]) -> None:
            super().__init__()
            self.server_name = str(row.get("name", ""))
            self.setObjectName("serverCard")
            self.setProperty("selected", "false")
            _apply_soft_shadow(self, blur=22, dx=4, dy=4, alpha=160)

            root = QHBoxLayout(self)
            root.setContentsMargins(12, 10, 12, 10)
            root.setSpacing(12)

            left = QVBoxLayout()
            top = QHBoxLayout()
            self.name_label = QLabel(self.server_name)
            self.name_label.setObjectName("serverName")
            top.addWidget(self.name_label)
            self.status_badge = QLabel("STOPPED")
            self.status_badge.setObjectName("statusBadge")
            top.addWidget(self.status_badge)
            top.addStretch(1)
            left.addLayout(top)

            self.detail_label = QLabel("-")
            self.detail_label.setObjectName("serverDetail")
            self.feedback_label = QLabel("")
            self.feedback_label.setObjectName("serverFeedback")
            left.addWidget(self.detail_label)
            left.addWidget(self.feedback_label)
            root.addLayout(left, 1)

            actions = QHBoxLayout()
            self.start_btn = self._btn("Start", "start", "primaryButton")
            self.stop_btn = self._btn("Stop", "stop", "dangerButton")
            self.restart_btn = self._btn("Restart", "restart")
            self.update_btn = self._btn("Update", "update")
            self.backup_btn = self._btn("Backup", "backup", "ghostButton")
            self.restore_btn = self._btn("Restore", "restore")
            self.remove_btn = self._btn("Delete", "remove", "dangerButton")
            for b in (
                self.start_btn,
                self.stop_btn,
                self.restart_btn,
                self.update_btn,
                self.backup_btn,
                self.restore_btn,
                self.remove_btn,
            ):
                actions.addWidget(b)
            root.addLayout(actions)
            self.update_row(row)

        def _btn(self, label: str, action: str, object_name: str = "") -> QPushButton:
            btn = QPushButton(label)
            btn.setFixedHeight(31)
            if object_name:
                btn.setObjectName(object_name)
            btn.clicked.connect(lambda _checked=False, a=action: self.action_requested.emit(self.server_name, a))
            return btn

        def update_row(self, row: Dict[str, object]) -> None:
            self.server_name = str(row.get("name", ""))
            state = str(row.get("state", "stopped"))
            status = str(row.get("status", "STOPPED"))
            running = bool(row.get("running", False))
            busy = state == "updating"

            self.name_label.setText(self.server_name)
            self.detail_label.setText(_clean(str(row.get("detail", "")), 140))
            self.feedback_label.setText(_clean(str(row.get("feedback", "")), 180))

            badge = "running" if state == "running" else ("updating" if state == "updating" else ("failed" if state == "failed" else "stopped"))
            self.status_badge.setProperty("state", badge)
            self.status_badge.setText(status)
            self.status_badge.style().unpolish(self.status_badge)
            self.status_badge.style().polish(self.status_badge)

            self.start_btn.setEnabled((not busy) and (not running))
            self.stop_btn.setEnabled((not busy) and running)
            self.restart_btn.setEnabled(not busy)
            self.update_btn.setEnabled((not busy) and (not running))
            self.backup_btn.setEnabled(not busy)
            self.restore_btn.setEnabled((not busy) and (not running))
            self.remove_btn.setEnabled(not busy)

        def set_selected(self, selected: bool) -> None:
            self.setProperty("selected", "true" if selected else "false")
            self.style().unpolish(self)
            self.style().polish(self)

        def mousePressEvent(self, event) -> None:  # type: ignore[override]
            self.selected.emit(self.server_name)
            super().mousePressEvent(event)


    class SettingsPanel(QFrame):
        changed = Signal()
        save_requested = Signal()

        def __init__(self) -> None:
            super().__init__()
            self.setObjectName("settingsCard")
            _apply_soft_shadow(self, blur=24, dx=5, dy=5, alpha=160)
            self._suspend = False

            outer = QVBoxLayout(self)
            outer.setContentsMargins(10, 10, 10, 10)
            outer.setSpacing(0)

            self.scroll = QScrollArea()
            self.scroll.setWidgetResizable(True)
            self.scroll.setFrameShape(QFrame.Shape.NoFrame)
            self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            outer.addWidget(self.scroll, 1)

            content = QWidget()
            root = QVBoxLayout(content)
            root.setContentsMargins(2, 2, 2, 2)
            root.setSpacing(8)
            self.scroll.setWidget(content)

            title = QLabel("Server Settings")
            title.setObjectName("sectionTitle")
            self.server_label = QLabel("Selected: -")
            self.server_label.setObjectName("selectedLine")
            self.unsaved_label = QLabel("")
            self.unsaved_label.setObjectName("statusText")
            root.addWidget(title)
            root.addWidget(self.server_label)
            root.addWidget(self.unsaved_label)

            form_wrap = QFrame()
            form_wrap.setObjectName("settingsInnerCard")
            form_layout = QVBoxLayout(form_wrap)
            form_layout.setContentsMargins(10, 10, 10, 10)
            form_layout.setSpacing(8)

            for text, attr in (("Executable", "exec_input"), ("Parameters", "params_input"), ("Daily stop (HH:MM)", "stop_input")):
                lbl = QLabel(text)
                lbl.setObjectName("meta")
                form_layout.addWidget(lbl)
                inp = QLineEdit()
                setattr(self, attr, inp)
                form_layout.addWidget(inp)
            self.stop_input.setMaximumWidth(190)

            checks = QHBoxLayout()
            checks.setSpacing(10)
            self.auto_restart = QCheckBox("Auto restart")
            self.auto_update = QCheckBox("Auto update")
            self.restart_after_stop = QCheckBox("Restart after stop")
            for c in (self.auto_restart, self.auto_update, self.restart_after_stop):
                checks.addWidget(c)
            checks.addStretch(1)
            form_layout.addLayout(checks)

            save_row = QHBoxLayout()
            save_row.addStretch(1)
            self.save_btn = QPushButton("Save Settings")
            self.save_btn.setObjectName("primaryButton")
            self.save_btn.clicked.connect(self.save_requested.emit)
            save_row.addWidget(self.save_btn)
            form_layout.addLayout(save_row)
            root.addWidget(form_wrap)

            for widget in (self.exec_input, self.params_input, self.stop_input):
                widget.textChanged.connect(self._emit_changed)
            for widget in (self.auto_update, self.auto_restart, self.restart_after_stop):
                widget.toggled.connect(self._emit_changed)

        def _emit_changed(self, *_args) -> None:
            if not self._suspend:
                self.changed.emit()

        def set_dirty(self, dirty: bool) -> None:
            self.unsaved_label.setText("UNSAVED CHANGES" if dirty else "")

        def load_settings(self, name: str, settings: Dict[str, object]) -> None:
            self._suspend = True
            try:
                self.server_label.setText(f"Selected: {name}")
                self.unsaved_label.setText("")
                self.exec_input.setText(str(settings.get("executable", "") or ""))
                self.params_input.setText(str(settings.get("parameters", "") or ""))
                self.stop_input.setText(str(settings.get("stop_time", "") or ""))
                self.auto_update.setChecked(bool(settings.get("auto_update", False)))
                self.auto_restart.setChecked(bool(settings.get("auto_restart", True)))
                self.restart_after_stop.setChecked(bool(settings.get("restart_after_stop", False)))
            finally:
                self._suspend = False

        def clear(self) -> None:
            self._suspend = True
            try:
                self.server_label.setText("Selected: -")
                self.unsaved_label.setText("")
                self.exec_input.setText("")
                self.params_input.setText("")
                self.stop_input.setText("")
                self.auto_update.setChecked(False)
                self.auto_restart.setChecked(True)
                self.restart_after_stop.setChecked(False)
            finally:
                self._suspend = False

        def payload(self) -> Dict[str, object]:
            return {
                "executable": self.exec_input.text().strip(),
                "parameters": self.params_input.text().strip(),
                "auto_update": bool(self.auto_update.isChecked()),
                "auto_restart": bool(self.auto_restart.isChecked()),
                "restart_after_stop": bool(self.restart_after_stop.isChecked()),
                "stop_time": self.stop_input.text().strip(),
            }

        def set_history_rows(self, rows: List[Tuple[str, str, str, str, str]]) -> None:
            return


    class HistoryPanel(QFrame):
        def __init__(self) -> None:
            super().__init__()
            self.setObjectName("historyCard")
            _apply_soft_shadow(self, blur=24, dx=5, dy=5, alpha=160)
            self._signature: Tuple[int, str, str] = (0, "", "")

            root = QVBoxLayout(self)
            root.setContentsMargins(10, 10, 10, 10)
            root.setSpacing(6)

            title = QLabel("Action History")
            title.setObjectName("sectionTitle")
            root.addWidget(title)

            self.history_text = QPlainTextEdit()
            self.history_text.setReadOnly(True)
            self.history_text.setMaximumBlockCount(280)
            self.history_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
            root.addWidget(self.history_text, 1)

        def set_rows(self, rows: List[Tuple[str, str, str, str, str]]) -> None:
            lines = [
                f"{ts} | {action}/{server} | {status} | {_clean(details or '-', 120)}"
                for ts, action, server, status, details in rows
            ]
            signature = (len(lines), lines[0] if lines else "", lines[-1] if lines else "")
            if signature == self._signature:
                return
            self._signature = signature
            sb = self.history_text.verticalScrollBar()
            old_val = sb.value()
            old_max = sb.maximum()
            at_bottom = old_val >= max(0, old_max - 2)
            self.history_text.setPlainText("\n".join(lines))
            new_max = sb.maximum()
            if at_bottom:
                sb.setValue(new_max)
            else:
                shift = max(0, new_max - old_max)
                sb.setValue(min(new_max, old_val + shift))


    class LogPanel(QFrame):
        clear_requested = Signal()

        def __init__(self) -> None:
            super().__init__()
            self.setObjectName("logCard")
            _apply_soft_shadow(self, blur=24, dx=5, dy=5, alpha=165)
            self._signature: Tuple[int, str] = (0, "")

            root = QVBoxLayout(self)
            root.setContentsMargins(12, 12, 12, 12)
            root.setSpacing(8)

            header = QHBoxLayout()
            title = QLabel("Live Log")
            title.setObjectName("sectionTitle")
            header.addWidget(title)
            header.addStretch(1)
            self.copy_btn = QPushButton("Copy")
            self.clear_btn = QPushButton("Clear")
            self.auto_scroll = QCheckBox("Autoscroll")
            self.auto_scroll.setChecked(True)
            self.copy_btn.clicked.connect(self.copy_all)
            self.clear_btn.clicked.connect(self.clear_requested.emit)
            header.addWidget(self.copy_btn)
            header.addWidget(self.clear_btn)
            header.addWidget(self.auto_scroll)
            root.addLayout(header)

            self.output = QPlainTextEdit()
            self.output.setObjectName("logOutput")
            self.output.setReadOnly(True)
            font = QFont("Cascadia Mono", 9)
            font.setStyleHint(QFont.StyleHint.Monospace)
            self.output.setFont(font)
            root.addWidget(self.output, 1)

        def copy_all(self) -> None:
            data = self.output.toPlainText().strip()
            if data:
                QApplication.clipboard().setText(data)

        def set_lines(self, lines: List[str]) -> None:
            signature = (len(lines), lines[-1] if lines else "")
            if signature == self._signature:
                return
            self._signature = signature
            self.output.setPlainText("\n".join(lines))
            if self.auto_scroll.isChecked():
                sb = self.output.verticalScrollBar()
                sb.setValue(sb.maximum())

    class DgsmQtMainWindow(QMainWindow):
        def __init__(self, loop: asyncio.AbstractEventLoop):
            super().__init__()
            self.loop = loop

            self.rows_cache: List[Dict[str, object]] = []
            self.rows_by_name: Dict[str, Dict[str, object]] = {}
            self.card_widgets: Dict[str, ServerCard] = {}
            self.selected_server: Optional[str] = None
            self.settings_loaded_for: Optional[str] = None
            self.settings_dirty = False
            self.action_in_flight = False
            self._refresh_task: Optional[asyncio.Task] = None
            self._closing = False
            self._layout_guard_scheduled = False

            self.signals = _UiSignals()
            self.signals.status_line.connect(self._set_status_line)

            self.setObjectName("mainWindow")
            self.setWindowTitle("DGSM Desktop Control")
            icon = _load_logo_icon()
            if icon is not None:
                self.setWindowIcon(icon)
            self._build_ui()
            self.setStyleSheet(_build_qss())

            screen = QGuiApplication.primaryScreen()
            if screen is not None:
                geom = screen.availableGeometry()
                w = max(980, min(1540, int(geom.width() * 0.9)))
                h = max(660, min(980, int(geom.height() * 0.9)))
                self.resize(w, h)
            else:
                self.resize(1280, 840)
            self.setMinimumSize(760, 520)

            QTimer.singleShot(120, self._init_split_sizes)
            QTimer.singleShot(150, self.refresh_data)
            self.refresh_timer = QTimer(self)
            self.refresh_timer.timeout.connect(self.refresh_data)
            self.refresh_timer.start(1200)

            self.console_timer = QTimer(self)
            self.console_timer.timeout.connect(self._console_tick)
            self.console_timer.start(500)

            self.metrics_timer = QTimer(self)
            self.metrics_timer.timeout.connect(self._update_metrics)
            self.metrics_timer.start(2000)
            self._update_metrics()
            QTimer.singleShot(220, self._guard_layout)

            if _env_bool("DGSM_HIDE_CONSOLE_WHEN_UI", True):
                QTimer.singleShot(280, _hide_console_window)

        def _build_ui(self) -> None:
            root = QWidget()
            outer = QVBoxLayout(root)
            outer.setContentsMargins(14, 14, 14, 14)
            outer.setSpacing(10)

            self.topbar = TopControlBar()
            self.topbar.action_requested.connect(self._handle_top_action)
            outer.addWidget(self.topbar)

            self.metrics_panel = MetricsPanel()
            outer.addWidget(self.metrics_panel)

            self.vertical_split = QSplitter(Qt.Orientation.Vertical)
            self.vertical_split.setChildrenCollapsible(False)
            outer.addWidget(self.vertical_split, 1)

            self.top_split = QSplitter(Qt.Orientation.Horizontal)
            self.top_split.setChildrenCollapsible(False)
            self.vertical_split.addWidget(self.top_split)

            self.server_list_card = QFrame()
            self.server_list_card.setObjectName("serverListCard")
            self.server_list_card.setMinimumWidth(320)
            self.server_list_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            _apply_soft_shadow(self.server_list_card, blur=26, dx=5, dy=5, alpha=170)
            server_layout = QVBoxLayout(self.server_list_card)
            server_layout.setContentsMargins(12, 12, 12, 12)
            server_title = QLabel("Servers")
            server_title.setObjectName("sectionTitle")
            server_layout.addWidget(server_title)

            self.server_scroll = QScrollArea()
            self.server_scroll.setWidgetResizable(False)
            self.server_scroll.setFrameShape(QFrame.Shape.NoFrame)
            self.server_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.server_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.server_host = QWidget()
            self.server_host.setMinimumWidth(520)
            self.server_cards_layout = QVBoxLayout(self.server_host)
            self.server_cards_layout.setContentsMargins(2, 2, 2, 2)
            self.server_cards_layout.setSpacing(10)
            self.server_cards_layout.addStretch(1)
            self.server_scroll.setWidget(self.server_host)
            server_layout.addWidget(self.server_scroll, 1)
            self.top_split.addWidget(self.server_list_card)

            self.history_panel = HistoryPanel()
            self.history_panel.setMinimumWidth(240)
            self.top_split.addWidget(self.history_panel)
            self.top_split.setStretchFactor(0, 2)
            self.top_split.setStretchFactor(1, 1)

            self.bottom_split = QSplitter(Qt.Orientation.Horizontal)
            self.bottom_split.setChildrenCollapsible(False)
            self.vertical_split.addWidget(self.bottom_split)

            self.settings_panel = SettingsPanel()
            self.settings_panel.changed.connect(self._on_settings_changed)
            self.settings_panel.save_requested.connect(self.on_savecfg)
            self.bottom_split.addWidget(self.settings_panel)

            self.log_panel = LogPanel()
            self.log_panel.clear_requested.connect(self.on_clear_console)
            self.bottom_split.addWidget(self.log_panel)
            self.bottom_split.setStretchFactor(0, 1)
            self.bottom_split.setStretchFactor(1, 2)
            self.vertical_split.setStretchFactor(0, 3)
            self.vertical_split.setStretchFactor(1, 4)

            self.status_card = QFrame()
            self.status_card.setObjectName("statusBar")
            status_layout = QHBoxLayout(self.status_card)
            status_layout.setContentsMargins(12, 8, 12, 8)
            self.status_label = QLabel("Ready")
            self.status_label.setObjectName("statusText")
            status_layout.addWidget(self.status_label)
            outer.addWidget(self.status_card)

            self.setCentralWidget(root)

        def _init_split_sizes(self) -> None:
            self._enforce_split_bounds(force_defaults=True)

        def _schedule_layout_guard(self) -> None:
            if self._layout_guard_scheduled:
                return
            self._layout_guard_scheduled = True
            QTimer.singleShot(0, self._guard_layout)

        def resizeEvent(self, event) -> None:  # type: ignore[override]
            super().resizeEvent(event)
            self._schedule_layout_guard()
            self._update_server_host_width()

        def _fit_to_screen_if_needed(self) -> None:
            screen = self.screen() or QGuiApplication.primaryScreen()
            if screen is None:
                return
            geom = screen.availableGeometry()
            max_w = max(self.minimumWidth(), geom.width() - 4)
            max_h = max(self.minimumHeight(), geom.height() - 4)
            target_w = min(self.width(), max_w)
            target_h = min(self.height(), max_h)
            if target_w != self.width() or target_h != self.height():
                self.resize(target_w, target_h)

        def _enforce_split_bounds(self, force_defaults: bool = False) -> None:
            # Keep panels usable even after long runtime or RDP resolution changes.
            h = max(1, self.vertical_split.height())
            top_sizes = self.vertical_split.sizes()
            if len(top_sizes) != 2 or force_defaults or sum(top_sizes) <= 0:
                top_h = int(max(220, min(h * 0.42, h - 280)))
                current_top = top_h
                current_bottom = max(260, h - top_h)
            else:
                current_top = int(top_sizes[0])
                current_bottom = int(top_sizes[1])
                top_h = current_top
            min_top = 180
            min_bottom = 260
            top_h = max(min_top, min(top_h, max(min_top, h - min_bottom)))
            bottom_h = max(min_bottom, h - top_h)
            if force_defaults or abs(top_h - current_top) > 6 or abs(bottom_h - current_bottom) > 6:
                self.vertical_split.setSizes([top_h, bottom_h])

            tw = max(1, self.top_split.width())
            split_sizes = self.top_split.sizes()
            if len(split_sizes) != 2 or force_defaults or sum(split_sizes) <= 0:
                server_w = int(max(320, min(tw * 0.66, tw - 260)))
                current_left = server_w
                current_right = max(220, tw - server_w)
            else:
                current_left = int(split_sizes[0])
                current_right = int(split_sizes[1])
                server_w = current_left
            min_left = 300
            min_right = 220
            max_left = max(min_left, tw - min_right)
            server_w = max(min_left, min(server_w, max_left))
            right_w = max(min_right, tw - server_w)
            if force_defaults or abs(server_w - current_left) > 6 or abs(right_w - current_right) > 6:
                self.top_split.setSizes([server_w, right_w])

            bw = max(1, self.bottom_split.width())
            bottom_sizes = self.bottom_split.sizes()
            if len(bottom_sizes) != 2 or force_defaults or sum(bottom_sizes) <= 0:
                settings_w = int(max(320, min(bw * 0.40, bw - 360)))
                current_settings = settings_w
                current_console = max(320, bw - settings_w)
            else:
                current_settings = int(bottom_sizes[0])
                current_console = int(bottom_sizes[1])
                settings_w = current_settings
            min_settings = 300
            min_console = 320
            max_settings = max(min_settings, bw - min_console)
            settings_w = max(min_settings, min(settings_w, max_settings))
            console_w = max(min_console, bw - settings_w)
            if force_defaults or abs(settings_w - current_settings) > 6 or abs(console_w - current_console) > 6:
                self.bottom_split.setSizes([settings_w, console_w])

        def _guard_layout(self) -> None:
            self._layout_guard_scheduled = False
            if self._closing:
                return
            try:
                self._fit_to_screen_if_needed()
                self._enforce_split_bounds(force_defaults=False)
                self._update_server_host_width()
            except Exception:
                logging.exception("Desktop UI layout guard failed")

        def _update_server_host_width(self) -> None:
            if self._closing:
                return
            viewport = self.server_scroll.viewport()
            viewport_width = max(320, viewport.width()) if viewport is not None else 520
            card_width = 0
            for card in self.card_widgets.values():
                card_width = max(card_width, card.sizeHint().width())
            needed_width = max(viewport_width, card_width + 24)
            if self.server_host.minimumWidth() != needed_width:
                self.server_host.setMinimumWidth(needed_width)
            self.server_host.resize(needed_width, self.server_host.sizeHint().height())

        def closeEvent(self, event) -> None:  # type: ignore[override]
            global _QT_WINDOW, _STARTED
            self._closing = True
            try:
                self.refresh_timer.stop()
                self.console_timer.stop()
                self.metrics_timer.stop()
                layout_timer = getattr(self, "layout_guard_timer", None)
                if layout_timer is not None:
                    layout_timer.stop()
            except Exception:
                pass
            try:
                if self._refresh_task is not None and not self._refresh_task.done():
                    self._refresh_task.cancel()
            except Exception:
                pass
            _QT_WINDOW = None
            _STARTED = False
            _show_console_window()
            super().closeEvent(event)

        def _set_status_line(self, text: str) -> None:
            self.status_label.setText(_clean(text, 280))

        def set_status(self, text: str) -> None:
            self.signals.status_line.emit(text)

        def _handle_top_action(self, action: str) -> None:
            if action == "refresh":
                self.refresh_data()
                return
            if action == "save":
                self.on_savecfg()
                return
            if action == "tools":
                self.on_tools()
                return
            if not self.selected_server:
                self.set_status("Select a server first")
                return
            self._queue_server_action(self.selected_server, action)

        def _on_card_selected(self, name: str) -> None:
            if self.selected_server != name:
                if self.settings_dirty and self.settings_loaded_for and self.settings_loaded_for != name:
                    self.set_status(f"Unsaved changes for {self.settings_loaded_for} were discarded")
                self.selected_server = name
                self.settings_dirty = False
                self.settings_loaded_for = None
                self._load_selected_settings(force=True)
            self._update_card_selection()
            self._update_header_and_buttons()

        def _on_card_action(self, name: str, action: str) -> None:
            if self.selected_server != name:
                self._on_card_selected(name)
            if action in {"start", "stop", "restart", "update"}:
                self._queue_server_action(name, action)
                return
            if action == "backup":
                self._queue_async("CREATE BACKUP", lambda: _create_backup_action(name), timeout=120.0)
                return
            if action == "restore":
                backups = _list_backup_files()
                if not backups:
                    self.set_status("No backup files available")
                    return
                selected_backup, ok = QInputDialog.getItem(
                    self,
                    "Restore Backup",
                    f"Backup file for {name}",
                    backups,
                    0,
                    False,
                )
                if not ok or not str(selected_backup).strip():
                    return
                overwrite = (
                    QMessageBox.question(
                        self,
                        "Overwrite target?",
                        "Overwrite existing files before restore?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    == QMessageBox.StandardButton.Yes
                )
                self._queue_async(
                    "RESTORE BACKUP",
                    lambda: _restore_backup_action(name, str(selected_backup), overwrite=overwrite),
                    timeout=180.0,
                )
                return
            if action == "remove":
                answer = QMessageBox.question(
                    self,
                    "Remove server",
                    f"Delete server '{name}' from config and disk?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
                backup_first = (
                    QMessageBox.question(
                        self,
                        "Create backup first?",
                        "Create a backup before delete?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.Yes,
                    )
                    == QMessageBox.StandardButton.Yes
                )
                self._queue_async(
                    "REMOVE SERVER",
                    lambda: _remove_server_action(name, backup_before_delete=backup_first),
                    timeout=180.0,
                )
                return

        def _on_settings_changed(self) -> None:
            if self.selected_server is None or self.action_in_flight:
                return
            self.settings_dirty = True
            self.settings_loaded_for = self.selected_server
            self.settings_panel.set_dirty(True)
            self._update_header_and_buttons()

        def _load_selected_settings(self, force: bool = False) -> None:
            name = self.selected_server
            if not name:
                self.settings_panel.clear()
                self.settings_dirty = False
                self.settings_loaded_for = None
                return
            if self.settings_dirty and self.settings_loaded_for == name and not force:
                self.settings_panel.set_dirty(True)
                return
            row = self.rows_by_name.get(name)
            if not row:
                self.settings_panel.clear()
                return
            settings = row.get("settings", {})
            if not isinstance(settings, dict):
                settings = {}
            self.settings_panel.load_settings(name, settings)
            self.settings_dirty = False
            self.settings_loaded_for = name
            self.settings_panel.set_dirty(False)

        def _queue_server_action(self, name: str, action: str) -> None:
            _feedback(name, "queued", f"{action} queued")
            self._render_server_cards()
            self._queue_async(action.upper(), lambda: _run_action(name, action))

        def _queue_async(self, label: str, make_coro: Callable[[], Awaitable[Tuple[bool, str]]], on_done=None, timeout: Optional[float] = None) -> None:
            if self.action_in_flight:
                self.set_status("Another task is already running")
                return
            self.action_in_flight = True
            self._update_header_and_buttons()
            self.set_status(f"{label} started...")

            async def runner() -> Tuple[bool, str]:
                try:
                    if timeout is not None:
                        return await asyncio.wait_for(make_coro(), timeout=timeout)
                    return await make_coro()
                except asyncio.TimeoutError:
                    return False, "timeout"
                except Exception as exc:
                    logging.exception("Desktop UI async task failed: %s", label)
                    return False, str(exc)

            task = asyncio.create_task(runner())

            def done_callback(done_task: asyncio.Task) -> None:
                ok = False
                line = f"{label} finished"
                try:
                    ok, msg = done_task.result()
                    line = f"{label} {'OK' if ok else 'FAILED'}: {msg}"
                except Exception as exc:
                    line = f"{label} FAILED: {exc}"
                    logging.exception("Desktop UI async callback failed: %s", label)
                self.action_in_flight = False
                if self._closing:
                    return
                if on_done is not None:
                    try:
                        on_done(ok)
                    except Exception:
                        logging.exception("Desktop UI on_done hook failed: %s", label)
                try:
                    self.set_status(line)
                except Exception:
                    logging.exception("Desktop UI status update failed: %s", label)
                try:
                    self.refresh_data()
                except Exception:
                    logging.exception("Desktop UI refresh scheduling failed: %s", label)

            task.add_done_callback(done_callback)

        def on_savecfg(self) -> None:
            name = self.selected_server
            if not name:
                self.set_status("Select a server first")
                return
            if not self.settings_dirty:
                self.set_status("No unsaved settings changes")
                return

            payload = self.settings_panel.payload()
            _feedback(name, "queued", "settings queued")
            self._render_server_cards()

            def after_save(ok: bool) -> None:
                if ok:
                    self.settings_dirty = False
                    self.settings_loaded_for = name
                    self.settings_panel.set_dirty(False)
                self._update_header_and_buttons()

            self._queue_async(
                "SAVE CFG",
                lambda: _save_settings(
                    name=name,
                    executable=str(payload["executable"]),
                    parameters=str(payload["parameters"]),
                    auto_update=bool(payload["auto_update"]),
                    auto_restart=bool(payload["auto_restart"]),
                    restart_after_stop=bool(payload["restart_after_stop"]),
                    stop_time=str(payload["stop_time"]),
                ),
                on_done=after_save,
                timeout=25.0,
            )

        def on_clear_console(self) -> None:
            _LIVE_LOG_LINES.clear()
            self.log_panel.set_lines([])
            self.set_status("Console output cleared")

        def on_tools(self) -> None:
            existing = getattr(self, "_tools_dialog", None)
            if existing is not None and existing.isVisible():
                existing.raise_()
                existing.activateWindow()
                return

            dlg = QDialog(self)
            dlg.setWindowTitle("DGSM Tools")
            dlg.setModal(False)
            dlg.setMinimumWidth(360)
            layout = QVBoxLayout(dlg)
            layout.setContentsMargins(12, 12, 12, 12)
            layout.setSpacing(8)

            title = QLabel("Tools / Commands")
            title.setObjectName("sectionTitle")
            layout.addWidget(title)

            def _tool_button(label: str, callback) -> None:
                btn = QPushButton(label)
                btn.clicked.connect(callback)
                layout.addWidget(btn)

            _tool_button("Create Template", self._tool_create_template)
            _tool_button("Add Server", self._tool_add_server)
            _tool_button("Create Backup", self._tool_create_backup)
            _tool_button("Restore Backup", self._tool_restore_backup)
            _tool_button("Remove Server", self._tool_remove_server)

            close_btn = QPushButton("Close")
            close_btn.setObjectName("ghostButton")
            close_btn.clicked.connect(dlg.close)
            layout.addWidget(close_btn)

            self._tools_dialog = dlg
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()

        def _tool_create_template(self) -> None:
            name, ok = QInputDialog.getText(self, "Create Template", "Template name:")
            if not ok or not str(name).strip():
                return
            app_id, ok = QInputDialog.getText(self, "Create Template", "Steam App ID:")
            if not ok or not str(app_id).strip():
                return
            executable, ok = QInputDialog.getText(self, "Create Template", "Executable:")
            if not ok or not str(executable).strip():
                return
            params, ok = QInputDialog.getText(self, "Create Template", "Parameters (optional):")
            if not ok:
                return
            stop_time, ok = QInputDialog.getText(self, "Create Template", "Daily stop (HH:MM, optional):", text="05:00")
            if not ok:
                return

            auto_update = (
                QMessageBox.question(
                    self,
                    "Auto update",
                    "Enable auto update?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                == QMessageBox.StandardButton.Yes
            )
            auto_restart = (
                QMessageBox.question(
                    self,
                    "Auto restart",
                    "Enable auto restart?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                == QMessageBox.StandardButton.Yes
            )
            restart_after_stop = (
                QMessageBox.question(
                    self,
                    "Restart after stop",
                    "Restart automatically after scheduled stop?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                == QMessageBox.StandardButton.Yes
            )

            self._queue_async(
                "CREATE TEMPLATE",
                lambda: _create_template_action(
                    template_name=str(name).strip(),
                    app_id=str(app_id).strip(),
                    executable=str(executable).strip(),
                    parameters=str(params).strip(),
                    auto_update=auto_update,
                    auto_restart=auto_restart,
                    stop_time=str(stop_time).strip(),
                    restart_after_stop=restart_after_stop,
                ),
                timeout=60.0,
            )

        def _tool_add_server(self) -> None:
            templates = _list_templates()
            if not templates:
                self.set_status("No templates found")
                return

            name, ok = QInputDialog.getText(self, "Add Server", "Server name:")
            if not ok or not str(name).strip():
                return
            template, ok = QInputDialog.getItem(self, "Add Server", "Template:", templates, 0, False)
            if not ok or not str(template).strip():
                return
            instance_id, ok = QInputDialog.getText(self, "Add Server", "Instance ID (optional):")
            if not ok:
                return
            start_after = (
                QMessageBox.question(
                    self,
                    "Start after install",
                    "Start server after install?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                == QMessageBox.StandardButton.Yes
            )

            self._queue_async(
                "ADD SERVER",
                lambda: _add_server_action(
                    name=str(name).strip(),
                    template_name=str(template).strip(),
                    instance_id=str(instance_id).strip(),
                    start_after_install=start_after,
                ),
                timeout=420.0,
            )

        def _tool_create_backup(self) -> None:
            servers = sorted(self.rows_by_name.keys(), key=str.lower)
            if not servers:
                self.set_status("No servers available")
                return
            selected, ok = QInputDialog.getItem(self, "Create Backup", "Server:", servers, 0, False)
            if not ok or not str(selected).strip():
                return
            self._queue_async(
                "CREATE BACKUP",
                lambda: _create_backup_action(str(selected)),
                timeout=120.0,
            )

        def _tool_restore_backup(self) -> None:
            servers = sorted(self.rows_by_name.keys(), key=str.lower)
            backups = _list_backup_files()
            if not servers:
                self.set_status("No servers available")
                return
            if not backups:
                self.set_status("No backup files available")
                return

            server_name, ok = QInputDialog.getItem(self, "Restore Backup", "Server:", servers, 0, False)
            if not ok or not str(server_name).strip():
                return
            backup_file, ok = QInputDialog.getItem(self, "Restore Backup", "Backup file:", backups, 0, False)
            if not ok or not str(backup_file).strip():
                return
            overwrite = (
                QMessageBox.question(
                    self,
                    "Overwrite target?",
                    "Overwrite existing files before restore?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                == QMessageBox.StandardButton.Yes
            )
            self._queue_async(
                "RESTORE BACKUP",
                lambda: _restore_backup_action(str(server_name), str(backup_file), overwrite=overwrite),
                timeout=180.0,
            )

        def _tool_remove_server(self) -> None:
            servers = sorted(self.rows_by_name.keys(), key=str.lower)
            if not servers:
                self.set_status("No servers available")
                return
            server_name, ok = QInputDialog.getItem(self, "Remove Server", "Server:", servers, 0, False)
            if not ok or not str(server_name).strip():
                return
            confirm = QMessageBox.question(
                self,
                "Confirm remove",
                f"Remove server '{server_name}'?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
            backup_first = (
                QMessageBox.question(
                    self,
                    "Create backup first?",
                    "Create backup before delete?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                == QMessageBox.StandardButton.Yes
            )
            self._queue_async(
                "REMOVE SERVER",
                lambda: _remove_server_action(str(server_name), backup_before_delete=backup_first),
                timeout=180.0,
            )

        def refresh_data(self) -> None:
            if self._closing:
                return
            if self._refresh_task is not None and not self._refresh_task.done():
                return
            self._refresh_task = asyncio.create_task(_collect_servers())

            def done_callback(task: asyncio.Task) -> None:
                if self._closing:
                    return
                try:
                    rows = task.result()
                except Exception as exc:
                    logging.exception("Desktop UI refresh failed")
                    self.set_status(f"Refresh failed: {exc}")
                    return
                self.rows_cache = rows
                self.rows_by_name = {str(row.get("name", "")): row for row in rows}
                self._render_server_cards()
                self.history_panel.set_rows(_collect_history(90))
                self._update_metrics()
                if not self.action_in_flight:
                    busy_names = [str(row.get("name", "")) for row in rows if str(row.get("state", "")) == "updating"]
                    if busy_names:
                        self.set_status(f"Discord operation running: {busy_names[0]}")

            self._refresh_task.add_done_callback(done_callback)

        def _render_server_cards(self) -> None:
            previous_selected = self.selected_server
            ordered_names = [str(row.get("name", "")) for row in self.rows_cache]
            current_names = list(self.card_widgets.keys())
            needs_rebuild = current_names != ordered_names

            if needs_rebuild:
                while self.server_cards_layout.count():
                    item = self.server_cards_layout.takeAt(0)
                    widget = item.widget()
                    if widget is not None:
                        widget.deleteLater()
                self.card_widgets.clear()

                for row in self.rows_cache:
                    card = ServerCard(row)
                    name = str(row.get("name", ""))
                    card.selected.connect(self._on_card_selected)
                    card.action_requested.connect(self._on_card_action)
                    card.set_selected(name == previous_selected)
                    self.server_cards_layout.addWidget(card)
                    self.card_widgets[name] = card
                self.server_cards_layout.addStretch(1)
            else:
                for row in self.rows_cache:
                    name = str(row.get("name", ""))
                    card = self.card_widgets.get(name)
                    if card is not None:
                        card.update_row(row)
            self._update_server_host_width()

            if previous_selected and previous_selected in self.rows_by_name:
                self.selected_server = previous_selected
            elif self.rows_cache:
                self.selected_server = str(self.rows_cache[0].get("name", ""))
            else:
                self.selected_server = None

            self._update_card_selection()
            if self.selected_server is None:
                self.settings_panel.clear()
                self.settings_dirty = False
                self.settings_loaded_for = None
            else:
                if self.settings_dirty and self.settings_loaded_for == self.selected_server:
                    self.settings_panel.set_dirty(True)
                else:
                    self._load_selected_settings(force=False)
            self._update_header_and_buttons()

        def _update_card_selection(self) -> None:
            for name, card in self.card_widgets.items():
                card.set_selected(name == self.selected_server)

        def _update_header_and_buttons(self) -> None:
            rows = self.rows_cache
            running = sum(1 for r in rows if str(r.get("state", "")) == "running")
            stopped = sum(1 for r in rows if str(r.get("state", "")) == "stopped")
            updating = sum(1 for r in rows if str(r.get("state", "")) == "updating")
            self.topbar.set_summary(running, stopped, updating)

            if self.selected_server:
                row = self.rows_by_name.get(self.selected_server, {})
                status = str(row.get("status", "STOPPED"))
                detail = _clean(str(row.get("detail", "")), 72)
                self.topbar.set_selected(f"Selected: {self.selected_server} | {status} | {detail}")
            else:
                self.topbar.set_selected("Selected: -")

            selected_row = self.rows_by_name.get(self.selected_server or "") if self.selected_server else None
            states = {
                "start": False,
                "stop": False,
                "restart": False,
                "update": False,
                "tools": True,
                "save": False,
                "refresh": True,
            }
            if self.action_in_flight:
                states["refresh"] = False
                states["tools"] = False
                self.topbar.set_button_states(states)
                return

            if selected_row:
                state = str(selected_row.get("state", "stopped"))
                running_now = bool(selected_row.get("running", False))
                busy = state == "updating"
                states["start"] = (not busy) and (not running_now)
                states["stop"] = (not busy) and running_now
                states["restart"] = not busy
                states["update"] = (not busy) and (not running_now)
                states["save"] = (not busy) and self.settings_dirty
            self.topbar.set_button_states(states)

        def _console_tick(self) -> None:
            if self._closing:
                return
            self.log_panel.set_lines(list(_LIVE_LOG_LINES))

        def _update_metrics(self) -> None:
            if self._closing:
                return
            self.metrics_panel.update_metrics(_collect_system_metrics(self.rows_cache))


async def _qt_event_pump() -> None:
    global _STARTED, _QT_PUMP_TASK
    try:
        while _STARTED and _QT_APP is not None:
            _QT_APP.processEvents()
            if _QT_WINDOW is None or not _QT_WINDOW.isVisible():
                break
            await asyncio.sleep(0.01)
    except asyncio.CancelledError:
        pass
    except Exception:
        logging.exception("[DESKTOP-UI] Qt event pump failed")
    finally:
        _show_console_window()
        _STARTED = False
        _QT_PUMP_TASK = None


def _start_qt_ui(loop: asyncio.AbstractEventLoop) -> None:
    global _QT_APP, _QT_WINDOW, _QT_PUMP_TASK, _STARTED
    try:
        _set_windows_app_user_model_id()
        app = QApplication.instance()
        if app is None:
            app = QApplication([])
            app.setStyle("Fusion")
        icon = _load_logo_icon()
        if icon is not None:
            app.setWindowIcon(icon)
        _QT_APP = app

        _bootstrap_log_lines()
        window = DgsmQtMainWindow(loop)
        if icon is not None:
            window.setWindowIcon(icon)
        _QT_WINDOW = window
        window.show()
        window.raise_()
        window.activateWindow()

        if _QT_PUMP_TASK is None or _QT_PUMP_TASK.done():
            _QT_PUMP_TASK = loop.create_task(_qt_event_pump())
    except Exception:
        _STARTED = False
        logging.exception("[DESKTOP-UI] runtime error")


def start_desktop_ui(
    loop: asyncio.AbstractEventLoop,
    refresh_callback: Optional[Callable[[], Awaitable[None]]] = None,
) -> bool:
    global _LOOP, _REFRESH_CALLBACK, _STARTED

    if os.name != "nt":
        logging.info("[DESKTOP-UI] not started (Windows only)")
        return False
    if not _env_bool("DGSM_DESKTOP_UI_ENABLED", True):
        logging.info("[DESKTOP-UI] disabled via DGSM_DESKTOP_UI_ENABLED")
        return False
    if QApplication is None:
        logging.error("[DESKTOP-UI] PySide6 not available")
        return False

    with _START_LOCK:
        if _STARTED:
            return True

        _LOOP = loop
        if refresh_callback is not None:
            _REFRESH_CALLBACK = refresh_callback

        _bootstrap_log_lines()
        _install_live_log_handler()

        _STARTED = True
        loop.call_soon_threadsafe(_start_qt_ui, loop)

    logging.info("[DESKTOP-UI] started")
    return True
