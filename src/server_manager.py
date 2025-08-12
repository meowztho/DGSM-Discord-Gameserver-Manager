import asyncio
import logging
import os
import signal
import sys
import subprocess
from datetime import datetime
from typing import Dict, List, Optional
import glob
from collections import defaultdict

import psutil

from paths import SERVER_PATHS, SERVER_CONFIGS, load_server_paths, load_server_configs
from config_store import get_config_value
from db import write_action_log
from pidcache import save_pids, load_pids
from steam_integration import run_update  # <-- neu: für Auto-Update

# Per-Server-Locks gegen Doppelstart/-stop
server_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

server_processes: Dict[str, psutil.Process] = {}
daily_stopped_servers = set()


def _normalize_params(p) -> List[str]:
    if p is None:
        return []
    if isinstance(p, list):
        return [str(x) for x in p]
    if isinstance(p, str):
        return [s for s in p.split() if s]
    return [str(p)]


def _resolve_executable(server_name: str) -> Optional[str]:
    """
    Finde die ausführbare Datei:
    1) server_settings.json -> "executable"
    2) server_config.json   -> "executable"
    3) Glob-Suche im serverfiles-Ordner (z. B. *Server*.exe)
    """
    base = SERVER_PATHS.get(server_name)
    if not base:
        return None

    # 1) settings
    settings = SERVER_CONFIGS.get(server_name, {})
    exe_settings = settings.get("executable")
    if exe_settings:
        candidate = os.path.join(base, exe_settings)
        if os.path.isfile(candidate):
            return candidate

    # 2) config
    exe_config = get_config_value(server_name, "executable")
    if exe_config:
        candidate = os.path.join(base, exe_config)
        if os.path.isfile(candidate):
            return candidate

    # 3) heuristische Suche
    patterns = ["*Server*.exe", "*Dedicated*.exe", "*.exe"]
    for pat in patterns:
        hits = glob.glob(os.path.join(base, pat))
        # bevorzuge Dateien mit 'Server' im Namen
        hits.sort(key=lambda p: (("server" not in p.lower()), len(p)))
        for h in hits:
            if os.path.isfile(h):
                return h

    return None


def _server_command(server_name: str) -> Optional[List[str]]:
    base = SERVER_PATHS.get(server_name)
    if not base:
        return None

    exe_path = _resolve_executable(server_name)
    if not exe_path:
        return None

    params = _normalize_params(SERVER_CONFIGS.get(server_name, {}).get("parameters", []))
    return [exe_path] + params


async def start_server(server_name: str) -> bool:
    # immer frisch laden
    load_server_paths()
    load_server_configs()

    async with server_locks[server_name]:
        if server_name not in SERVER_PATHS:
            write_action_log("start", server_name, "failed", "path not found")
            logging.error(f"[START] Pfad für {server_name} nicht gefunden.")
            return False

        # Wenn schon läuft, nicht doppelt starten
        if server_name in server_processes and server_processes[server_name].is_running():
            logging.info(f"[START] {server_name} läuft bereits (PID {server_processes[server_name].pid}).")
            return True

        cmd = _server_command(server_name)
        if not cmd:
            detail = (
                f"Exe nicht gefunden. Basis: {SERVER_PATHS.get(server_name)} | "
                f"config.executable={get_config_value(server_name, 'executable')} | "
                f"settings.executable={SERVER_CONFIGS.get(server_name, {}).get('executable')}"
            )
            write_action_log("start", server_name, "failed", detail)
            logging.error(f"[START] {server_name}: {detail}")
            return False

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=SERVER_PATHS[server_name],
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0),
            )
            server_processes[server_name] = psutil.Process(proc.pid)
            save_pids(server_processes)
            write_action_log("start", server_name, "success", f"PID {proc.pid} | CMD: {' '.join(cmd)}")
            logging.info(f"[START] {server_name} gestartet. PID {proc.pid}")
            return True
        except Exception as e:
            write_action_log("start", server_name, "failed", str(e))
            logging.exception(f"[START] Fehler beim Start von {server_name}")
            return False


async def stop_server(server_name: str) -> bool:
    async with server_locks[server_name]:
        if server_name not in server_processes:
            return False
        proc = server_processes.pop(server_name)
        try:
            if sys.platform == "win32":
                try:
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                    await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=10)
                except (asyncio.TimeoutError, psutil.NoSuchProcess):
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], check=False)
            else:
                try:
                    proc.terminate()
                    await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=10)
                except (asyncio.TimeoutError, psutil.NoSuchProcess):
                    os.system(f"pkill -TERM -P {proc.pid}")
            save_pids(server_processes)
            write_action_log("stop", server_name, "success")
            logging.info(f"[STOP] {server_name} gestoppt.")
            return True
        except Exception as e:
            write_action_log("stop", server_name, "failed", str(e))
            logging.exception(f"[STOP] Fehler beim Stop von {server_name}")
            return False


async def recover_running_servers():
    load_server_paths()
    pids = load_pids()
    for name, pid in pids.items():
        try:
            proc = psutil.Process(pid)
            if proc.is_running() and name in SERVER_PATHS:
                server_processes[name] = proc
                write_action_log("recovery", name, "success", f"PID {pid}")
            else:
                write_action_log("recovery", name, "failed", f"Ungültiger Prozess {pid}")
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            write_action_log("recovery", name, "failed", f"Prozess nicht gefunden: {e}")


async def monitor_servers():
    """
    - Crash-Watch (auto_restart)
    - Täglicher Stop um 'stop_time'
    - NEU: Auto-Update direkt NACH dem täglichen Stop, wenn auto_update=True
            und NUR wenn der Server nicht läuft.
    - Optionaler Neustart gemäß restart_after_stop
    """
    last_reload = 0
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(30)

        # Config-Reload alle 5 Minuten
        if (loop.time() - last_reload) > 300:
            load_server_configs()
            last_reload = loop.time()

        # Crash watch / Auto-Restart
        for name in list(server_processes.keys()):
            try:
                if not server_processes[name].is_running():
                    server_processes.pop(name)
                    cfg = SERVER_CONFIGS.get(name, {})
                    if cfg.get("auto_restart", True):
                        await start_server(name)
            except Exception:
                server_processes.pop(name, None)

        # Tägliche Wartung: Stop + Auto-Update (falls gewünscht) + optionaler Restart
        now = datetime.now().strftime("%H:%M")
        for name in list(SERVER_PATHS.keys()):
            cfg = SERVER_CONFIGS.get(name, {})
            st = cfg.get("stop_time")
            if not st:
                continue

            # genau zur Stopzeit einmal pro Tag stoppen
            if now == st and name not in daily_stopped_servers:
                # 1) Stoppen, falls läuft
                if name in server_processes:
                    await stop_server(name)
                daily_stopped_servers.add(name)

                # 2) Auto-Update: nur wenn aktiviert und Server jetzt wirklich aus
                if cfg.get("auto_update", True) and name not in server_processes:
                    ok, msg = await run_update(name)
                    write_action_log("auto_update", name, "success" if ok else "failed", msg if msg else "")
                    logging.info(f"[AUTO-UPDATE] {name}: {'OK' if ok else 'FAIL'} | {msg}")

                # 3) Optional wieder starten
                if cfg.get("restart_after_stop", False):
                    await asyncio.sleep(60)
                    await start_server(name)

            # um Mitternacht Reset der Tagesmarkierung
            if now == "00:00":
                daily_stopped_servers.discard(name)


async def graceful_stop_all():
    for name in list(server_processes.keys()):
        try:
            await stop_server(name)
        except Exception:
            pass
