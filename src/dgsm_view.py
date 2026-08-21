from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from cli_commands import execute_cli_command
from config_store import DB_PATH
from paths import SERVER_CONFIGS, SERVER_PATHS, load_server_configs, load_server_paths
from rest_bridge import collect_rest_snapshot
from runtime_status import get_operation_status
from server_manager import server_processes
import dgsm_operations as dgsm_ops

try:
    import psutil
except Exception:
    psutil = None  # type: ignore[assignment]

LIVE_LOG_LINES: deque[str] = deque(maxlen=1200)
_ACTION_FEEDBACK: Dict[str, Dict[str, str | int]] = {}
_REFRESH_CALLBACK: Optional[Callable[[], Awaitable[None]]] = None
_LOG_HANDLER_INSTALLED = False
_APP_STARTED_TS = time.time()
_PROC_NET_SAMPLE: Dict[int, Tuple[int, int]] = {}
_PROC_NET_SAMPLE_TS = 0.0
_PROC_CPU_SAMPLE: Dict[int, float] = {}
_PROC_CPU_SAMPLE_TS = 0.0
_BOT_CPU_SAMPLE: Optional[float] = None
_BOT_CPU_SAMPLE_TS = 0.0
_BOT_IO_SAMPLE: Optional[Tuple[int, int]] = None
_BOT_IO_SAMPLE_TS = 0.0
_METRICS_CACHE: Dict[str, str] = {}
_METRICS_CACHE_TS = 0.0
_METRICS_CACHE_SECONDS = 2.0
_SERVER_RESOURCE_CACHE: Dict[str, Dict[str, object]] = {}
_METRICS_LOCK = threading.Lock()


class _UiLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            line = str(record.getMessage())
        LIVE_LOG_LINES.append(line)


def clean(msg: str, limit: int = 220) -> str:
    text = " ".join(str(msg or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def find_logo_file() -> Optional[str]:
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


def _human_bytes(value: float) -> str:
    amount = max(0.0, float(value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024.0 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024.0
    return f"{amount:.1f} TB"


def _format_uptime(seconds: float) -> str:
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _is_within_path(base_dir: str, candidate: str) -> bool:
    try:
        base = os.path.normcase(os.path.abspath(base_dir))
        path = os.path.normcase(os.path.abspath(candidate))
        return os.path.commonpath([base, path]) == base
    except Exception:
        return False


def _is_running(name: str) -> bool:
    try:
        proc = server_processes.get(name)
        return bool(proc and proc.is_running())
    except Exception:
        return False


def _running_server_roots_by_name(rows: Optional[List[Dict[str, object]]] = None) -> Dict[str, str]:
    names: set[str] = set()
    if rows:
        for row in rows:
            name = str(row.get("name", "") or "").strip()
            if not name:
                continue
            state = str(row.get("state", "stopped")).lower()
            if bool(row.get("running", False)) or state == "running":
                names.add(name)
    if not names:
        for name, proc in list(server_processes.items()):
            try:
                if proc is not None and proc.is_running():
                    names.add(str(name))
            except Exception:
                continue
    return {
        name: os.path.normcase(os.path.abspath(SERVER_PATHS[name]))
        for name in names if SERVER_PATHS.get(name)
    }


def _collect_server_process_metrics(scope_roots: Dict[str, str]) -> Dict[str, Dict[str, object]]:
    global _PROC_NET_SAMPLE, _PROC_NET_SAMPLE_TS, _PROC_CPU_SAMPLE, _PROC_CPU_SAMPLE_TS
    if psutil is None:
        return {}
    if not scope_roots:
        _PROC_CPU_SAMPLE = {}
        _PROC_NET_SAMPLE = {}
        _PROC_CPU_SAMPLE_TS = time.time()
        _PROC_NET_SAMPLE_TS = _PROC_CPU_SAMPLE_TS
        return {}
    now = time.time()
    current_cpu: Dict[int, float] = {}
    current_io: Dict[int, Tuple[int, int]] = {}
    process_servers: Dict[int, str] = {}
    resources: Dict[str, Dict[str, object]] = {
        name: {"cpu": 0.0, "rss": 0, "read_bps": 0.0, "write_bps": 0.0} for name in scope_roots
    }
    try:
        for proc in psutil.process_iter(attrs=["pid", "exe", "cwd"]):
            try:
                exe = str(proc.info.get("exe") or "")
                cwd = str(proc.info.get("cwd") or "")
                server_name = ""
                for name, root in scope_roots.items():
                    if (exe and _is_within_path(root, exe)) or (cwd and _is_within_path(root, cwd)):
                        server_name = name
                        break
                if not server_name:
                    continue
                pid = int(proc.pid)
                process_servers[pid] = server_name
                cpu_times = proc.cpu_times()
                current_cpu[pid] = float(getattr(cpu_times, "user", 0.0) or 0.0) + float(getattr(cpu_times, "system", 0.0) or 0.0)
                mem = proc.memory_info()
                resources[server_name]["rss"] = int(resources[server_name]["rss"]) + int(getattr(mem, "rss", 0) or 0)
                io = proc.io_counters()
                current_io[pid] = (int(getattr(io, "read_bytes", 0) or 0), int(getattr(io, "write_bytes", 0) or 0))
            except Exception:
                continue
    except Exception:
        return resources
    elapsed_cpu = now - _PROC_CPU_SAMPLE_TS
    if _PROC_CPU_SAMPLE and elapsed_cpu > 0:
        for pid, total_now in current_cpu.items():
            prev_total = _PROC_CPU_SAMPLE.get(pid)
            server_name = process_servers.get(pid)
            if prev_total is not None and server_name and total_now >= prev_total:
                resources[server_name]["cpu"] = float(resources[server_name]["cpu"]) + total_now - prev_total
        cpu_count = int(psutil.cpu_count(logical=True) or 1)
        for values in resources.values():
            cpu = (float(values["cpu"]) / elapsed_cpu) * 100.0 / max(1, cpu_count)
            values["cpu"] = max(0.0, min(100.0, cpu))
    _PROC_CPU_SAMPLE = current_cpu
    _PROC_CPU_SAMPLE_TS = now
    elapsed_io = now - _PROC_NET_SAMPLE_TS
    if _PROC_NET_SAMPLE and elapsed_io > 0:
        for pid, (read_now, write_now) in current_io.items():
            prev = _PROC_NET_SAMPLE.get(pid)
            server_name = process_servers.get(pid)
            if not prev or not server_name:
                continue
            read_prev, write_prev = prev
            if read_now >= read_prev:
                resources[server_name]["read_bps"] = float(resources[server_name]["read_bps"]) + (read_now-read_prev)/elapsed_io
            if write_now >= write_prev:
                resources[server_name]["write_bps"] = float(resources[server_name]["write_bps"]) + (write_now-write_prev)/elapsed_io
    _PROC_NET_SAMPLE = current_io
    _PROC_NET_SAMPLE_TS = now
    return resources


def _collect_bot_process_metrics() -> Dict[str, object]:
    global _BOT_CPU_SAMPLE, _BOT_CPU_SAMPLE_TS, _BOT_IO_SAMPLE, _BOT_IO_SAMPLE_TS
    if psutil is None:
        return {"cpu": 0.0, "rss": 0, "read_bps": 0.0, "write_bps": 0.0}
    try:
        proc = psutil.Process(os.getpid())
        now = time.time()
        cpu_times = proc.cpu_times()
        total_cpu = float(getattr(cpu_times, "user", 0.0) or 0.0) + float(getattr(cpu_times, "system", 0.0) or 0.0)
        cpu = 0.0
        elapsed = now - _BOT_CPU_SAMPLE_TS
        if _BOT_CPU_SAMPLE is not None and elapsed > 0 and total_cpu >= _BOT_CPU_SAMPLE:
            cpu = ((total_cpu - _BOT_CPU_SAMPLE) / elapsed) * 100.0 / max(1, int(psutil.cpu_count(logical=True) or 1))
        _BOT_CPU_SAMPLE, _BOT_CPU_SAMPLE_TS = total_cpu, now
        rss = int(getattr(proc.memory_info(), "rss", 0) or 0)
        read_bps = write_bps = 0.0
        io = proc.io_counters()
        current = (int(getattr(io, "read_bytes", 0) or 0), int(getattr(io, "write_bytes", 0) or 0))
        elapsed_io = now - _BOT_IO_SAMPLE_TS
        if _BOT_IO_SAMPLE is not None and elapsed_io > 0:
            if current[0] >= _BOT_IO_SAMPLE[0]: read_bps = (current[0]-_BOT_IO_SAMPLE[0])/elapsed_io
            if current[1] >= _BOT_IO_SAMPLE[1]: write_bps = (current[1]-_BOT_IO_SAMPLE[1])/elapsed_io
        _BOT_IO_SAMPLE, _BOT_IO_SAMPLE_TS = current, now
        return {"cpu": max(0.0, min(100.0, cpu)), "rss": rss, "read_bps": read_bps, "write_bps": write_bps}
    except Exception:
        return {"cpu": 0.0, "rss": 0, "read_bps": 0.0, "write_bps": 0.0}


def _number(value: object) -> Optional[int]:
    try:
        if value is None or isinstance(value, bool): return None
        return int(float(str(value).strip()))
    except Exception:
        return None


def _api_player_metric(rows: Optional[List[Dict[str, object]]]) -> str:
    active = maximum = 0
    found = False
    for row in rows or []:
        rest = row.get("rest")
        if not isinstance(rest, dict) or not rest.get("available"): continue
        sections = rest.get("sections")
        if not isinstance(sections, dict): continue
        for section in sections.values():
            if not isinstance(section, dict): continue
            for key, value in section.items():
                low = str(key).lower()
                n = _number(value)
                if n is None: continue
                if low in {"players", "player_count", "current_players", "online_players"}:
                    active += n; found = True
                elif low in {"max_players", "player_limit", "slots"}:
                    maximum += n
    if not found: return "-"
    return f"{active} / {maximum}" if maximum else str(active)


def collect_system_metrics(rows: Optional[List[Dict[str, object]]] = None) -> Dict[str, str]:
    global _METRICS_CACHE, _METRICS_CACHE_TS, _SERVER_RESOURCE_CACHE
    running = sum(1 for row in rows or [] if str(row.get("state", "")) == "running")
    total_servers = len(rows or [])
    now = time.time()
    with _METRICS_LOCK:
        if _METRICS_CACHE and now - _METRICS_CACHE_TS < _METRICS_CACHE_SECONDS:
            metrics = dict(_METRICS_CACHE)
            metrics["players"] = _api_player_metric(rows)
            metrics["servers"] = f"{running} / {total_servers}"
        else:
            server_resources = _collect_server_process_metrics(_running_server_roots_by_name(rows))
            bot_resources = _collect_bot_process_metrics()
            _SERVER_RESOURCE_CACHE = server_resources
            server_cpu = sum(float(item.get("cpu", 0.0) or 0.0) for item in server_resources.values())
            server_rss = sum(int(item.get("rss", 0) or 0) for item in server_resources.values())
            server_read = sum(float(item.get("read_bps", 0.0) or 0.0) for item in server_resources.values())
            server_write = sum(float(item.get("write_bps", 0.0) or 0.0) for item in server_resources.values())
            metrics = {
                "dgsm": f"{float(bot_resources['cpu']):.1f}% CPU | {_human_bytes(int(bot_resources['rss']))}",
                "game_servers": f"{server_cpu:.1f}% CPU | {_human_bytes(server_rss)}",
                "server_io": f"R {_human_bytes(server_read)}/s | W {_human_bytes(server_write)}/s",
                "players": _api_player_metric(rows),
                "servers": f"{running} / {total_servers}",
                "uptime": _format_uptime(now - _APP_STARTED_TS),
            }
            _METRICS_CACHE = dict(metrics)
            _METRICS_CACHE_TS = now
        for row in rows or []:
            name = str(row.get("name", "") or "")
            row["resources"] = dict(_SERVER_RESOURCE_CACHE.get(name, {}))
        return metrics


def status_for(name: str) -> Tuple[str, str, str]:
    state, detail = get_operation_status(name)
    if state == "busy":
        label = (detail or "").strip().lower()
        labels = {"start": ("STARTING","Start in progress"), "stop": ("STOPPING","Stop in progress"), "update": ("UPDATING","Update in progress"), "backup": ("BACKUP","Backup in progress"), "restore": ("RESTORE","Restore in progress")}
        if label in labels:
            title, text = labels[label]; return title, "updating", text
        return "WORKING", "updating", "Operation in progress"
    if state == "failed": return "FAILED", "failed", clean(detail or "Last operation failed", 140)
    if _is_running(name): return "RUNNING", "running", "Server process is active"
    return "STOPPED", "stopped", "Server process is not active"


def settings_for(name: str) -> Dict[str, str | bool]:
    cfg = SERVER_CONFIGS.get(name, {})
    params = cfg.get("parameters", [])
    params_text = " ".join(str(x) for x in params) if isinstance(params, list) else (params if isinstance(params, str) else "")
    return {"executable": str(cfg.get("executable", "") or ""), "parameters": params_text, "auto_start": bool(cfg.get("auto_start", False)), "auto_update": bool(cfg.get("auto_update", False)), "auto_restart": bool(cfg.get("auto_restart", True)), "restart_after_stop": bool(cfg.get("restart_after_stop", False)), "stop_time": str(cfg.get("stop_time", "") or "")}


def feedback(name: str, status: str, message: str) -> None:
    _ACTION_FEEDBACK[name] = {"status": status, "message": clean(message), "timestamp": int(time.time())}


async def collect_servers(reload_config: bool = False) -> List[Dict[str, object]]:
    if reload_config:
        load_server_paths(); load_server_configs()
    out: List[Dict[str, object]] = []
    tasks: List[Tuple[Dict[str, object], asyncio.Task]] = []
    for index, name in enumerate(sorted(SERVER_PATHS.keys(), key=lambda x: str(x).lower()), start=1):
        status_label, state_key, detail = status_for(name)
        fb = _ACTION_FEEDBACK.get(name)
        running = _is_running(name)
        row = {"id": index, "name": name, "status": status_label, "state": state_key, "detail": detail, "running": running, "feedback": f"[{fb.get('status')}] {fb.get('message')}" if fb else "", "settings": settings_for(name), "resources": dict(_SERVER_RESOURCE_CACHE.get(name, {}))}
        out.append(row)
        cfg = SERVER_CONFIGS.get(name, {})
        if isinstance(cfg, dict): tasks.append((row, asyncio.create_task(collect_rest_snapshot(name, cfg, running))))
    for row, task in tasks:
        try: row["rest"] = await task
        except Exception as exc:
            row["rest"] = {"configured": False, "enabled": False, "available": False, "status": "error", "summary": [], "sections": {}, "errors": {"bridge": clean(str(exc),120)}, "updated_at": None}
    return out


def collect_history(limit: int = 90) -> List[Tuple[str, str, str, str, str]]:
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor(); cur.execute("SELECT timestamp, action, server, status, details FROM logs ORDER BY id DESC LIMIT ?", (limit,))
        return cur.fetchall()
    except Exception:
        logging.exception("DGSM view: could not load history")
        return []
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


def bootstrap_log_lines() -> None:
    if LIVE_LOG_LINES: return
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")
    if not os.path.isfile(path): return
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle.readlines()[-260:]: LIVE_LOG_LINES.append(line.rstrip("\r\n"))
    except Exception:
        logging.exception("DGSM view: could not bootstrap bot.log history")


def install_live_log_handler() -> None:
    global _LOG_HANDLER_INSTALLED
    if _LOG_HANDLER_INSTALLED: return
    handler = _UiLogHandler(level=logging.INFO)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    _LOG_HANDLER_INSTALLED = True


def configure_refresh_callback(callback: Optional[Callable[[], Awaitable[None]]]) -> None:
    global _REFRESH_CALLBACK
    if callback is not None: _REFRESH_CALLBACK = callback


async def safe_refresh(timeout: float = 8.0) -> None:
    if _REFRESH_CALLBACK is None: return
    try: await asyncio.wait_for(_REFRESH_CALLBACK(), timeout=timeout)
    except asyncio.TimeoutError: logging.warning("DGSM view: Discord refresh timeout after %.1fs", timeout)
    except Exception: logging.exception("DGSM view: Discord refresh failed")


async def run_cli_user_command(command_line: str, source: str) -> Tuple[bool, str]:
    result = await execute_cli_command(command_line, source=source)
    if result.refresh: await safe_refresh()
    return result.ok, result.message


def list_backup_files() -> List[str]: return dgsm_ops.list_backup_files()
def list_templates() -> List[str]: return dgsm_ops.list_templates()

dgsm_ops.configure_hooks(feedback=feedback, refresh=safe_refresh)
