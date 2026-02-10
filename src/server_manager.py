import asyncio
import logging
import os
import signal
import sys
import subprocess
from datetime import datetime
from typing import Dict, List, Optional, Iterable
import glob
from collections import defaultdict

import psutil

from paths import SERVER_PATHS, SERVER_CONFIGS, load_server_paths, load_server_configs
from config_store import get_config_value
from db import write_action_log
from pidcache import save_pids, load_pids
from runtime_status import begin_operation, end_operation_success, end_operation_failed
from steam_integration import run_update  # für Auto-Update

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


def _is_within_path(base_dir: str, candidate: str) -> bool:
    try:
        base_abs = os.path.normcase(os.path.abspath(base_dir))
        cand_abs = os.path.normcase(os.path.abspath(candidate))
        return os.path.commonpath([base_abs, cand_abs]) == base_abs
    except Exception:
        return False


def _resolve_executable(server_name: str) -> Optional[str]:
    base = SERVER_PATHS.get(server_name)
    if not base:
        return None

    # 1) settings.json
    settings = SERVER_CONFIGS.get(server_name, {})
    exe_settings = settings.get("executable")
    if exe_settings:
        candidate = os.path.join(base, exe_settings)
        if os.path.isfile(candidate):
            return candidate

    # 2) server_config.json
    exe_config = get_config_value(server_name, "executable")
    if exe_config:
        candidate = os.path.join(base, exe_config)
        if os.path.isfile(candidate):
            return candidate

    # 3) heuristische Suche
    patterns = ["*Server*.exe", "*Dedicated*.exe", "*.exe"]
    for pat in patterns:
        hits = glob.glob(os.path.join(base, pat))
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


async def auto_update_if_enabled(server_name: str):
    """Update ausführen, wenn auto_update: true."""
    cfg = SERVER_CONFIGS.get(server_name, {})
    if cfg.get("auto_update", False):
        ok, msg = await run_update(server_name)
        write_action_log("auto_update", server_name, "success" if ok else "failed", msg or "")
        logging.info(f"[AUTO-UPDATE] {server_name}: {'OK' if ok else 'FAIL'} | {msg}")


async def start_server(server_name: str) -> bool:
    """Startet den Server, macht vorher ein Update wenn auto_update aktiviert ist."""
    success = False
    fail_reason = ""
    begin_operation(server_name, "start")
    try:
        load_server_paths()
        load_server_configs()

        async with server_locks[server_name]:
            if server_name not in SERVER_PATHS:
                fail_reason = "path not found"
                write_action_log("start", server_name, "failed", fail_reason)
                logging.error(f"[START] Pfad für {server_name} nicht gefunden.")
                return False

            # Läuft schon?
            if server_name in server_processes and server_processes[server_name].is_running():
                logging.info(f"[START] {server_name} läuft bereits.")
                success = True
                return True

            # Erst Update, wenn aktiviert
            await auto_update_if_enabled(server_name)

            cmd = _server_command(server_name)
            if not cmd:
                fail_reason = f"Exe nicht gefunden in {SERVER_PATHS.get(server_name)}"
                write_action_log("start", server_name, "failed", fail_reason)
                logging.error(f"[START] {server_name}: {fail_reason}")
                return False

            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=SERVER_PATHS[server_name],
                    creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0),
                )
                server_processes[server_name] = psutil.Process(proc.pid)
                save_pids(server_processes)
                write_action_log("start", server_name, "success", f"PID {proc.pid}")
                logging.info(f"[START] {server_name} gestartet. PID {proc.pid}")
                success = True
                return True
            except Exception as e:
                fail_reason = str(e)
                write_action_log("start", server_name, "failed", fail_reason)
                logging.exception(f"[START] Fehler beim Start von {server_name}")
                return False
    finally:
        if success:
            end_operation_success(server_name)
        else:
            end_operation_failed(server_name, fail_reason or "Start fehlgeschlagen")
# -----------------------------
#       STOP - HELFER
# -----------------------------
def _list_server_related_processes(server_path: str) -> List[psutil.Process]:
    """
    Sucht alle Prozesse, die sehr wahrscheinlich zu diesem Server gehören:
    - exe() liegt unter server_path
    - oder cwd() liegt unter server_path
    """
    related: List[psutil.Process] = []
    server_path = os.path.normcase(os.path.abspath(server_path))
    for p in psutil.process_iter(attrs=["pid", "exe", "cwd"]):
        try:
            exe = p.info.get("exe") or ""
            cwd = p.info.get("cwd") or ""
            if exe:
                if _is_within_path(server_path, exe):
                    related.append(psutil.Process(p.info["pid"]))
                    continue
            if cwd:
                if _is_within_path(server_path, cwd):
                    related.append(psutil.Process(p.info["pid"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue
    # Doppelte PIDs entfernen
    by_pid = {p.pid: p for p in related}
    return list(by_pid.values())


async def _wait_gone(proc: psutil.Process, timeout: float) -> bool:
    try:
        await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=timeout)
        return True
    except (asyncio.TimeoutError, psutil.NoSuchProcess):
        return not proc.is_running()
    except Exception:
        return False


async def _win_taskkill(pid: int, force: bool) -> None:
    """taskkill auf Windows, mit/ohne /F, immer /T für Kindprozesse."""
    args = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        args.append("/F")
    subprocess.run(args, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def _terminate_tree_windows(pids: Iterable[int]) -> None:
    """Killt eine Menge PIDs inkl. Kindprozesse auf Windows, erst weich dann hart."""
    for pid in list(set(pids)):
        await _win_taskkill(pid, force=False)
    await asyncio.sleep(1)
    for pid in list(set(pids)):
        await _win_taskkill(pid, force=True)


# -----------------------------
#            STOP
# -----------------------------
async def stop_server(server_name: str) -> bool:
    success = False
    fail_reason = ""
    begin_operation(server_name, "stop")
    try:
        async with server_locks[server_name]:
            base = SERVER_PATHS.get(server_name)
            if not base:
                fail_reason = "path not found"
                return False

            # 1) Primär: unser getrackter Hauptprozess
            proc = server_processes.get(server_name)

            # 2) Zusätzlich: alle Prozesse, die im Serverordner laufen (falls Wrapper/Entkopplung)
            related = _list_server_related_processes(base)

            # Wenn gar nichts bekannt/gefunden: nichts zu stoppen
            if proc is None and not related:
                fail_reason = "keine Prozesse gefunden"
                return False

            killed = False

            try:
                # --- Versuche zuerst den Hauptprozess sauber zu stoppen ---
                if proc:
                    try:
                        proc = psutil.Process(proc.pid)  # refresh
                    except psutil.NoSuchProcess:
                        proc = None

                if proc and sys.platform == "win32":
                    # CTRL_BREAK NUR für eigene Kinder
                    is_child = (proc.ppid() == os.getpid())
                    if is_child:
                        try:
                            proc.send_signal(signal.CTRL_BREAK_EVENT)
                            if await _wait_gone(proc, timeout=10):
                                killed = True
                        except (psutil.NoSuchProcess, OSError):
                            pass

                    if not killed:
                        # sanft
                        try:
                            proc.terminate()
                            if await _wait_gone(proc, timeout=8):
                                killed = True
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass

                    if not killed:
                        # taskkill-Baum für den Hauptprozess
                        await _terminate_tree_windows([proc.pid])

                elif proc and sys.platform != "win32":
                    try:
                        proc.terminate()
                        if await _wait_gone(proc, timeout=10):
                            killed = True
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                    if not killed:
                        os.system(f"pkill -TERM -P {proc.pid}")
                        if await _wait_gone(proc, timeout=5):
                            killed = True
                    if not killed:
                        proc.kill()
                        await _wait_gone(proc, timeout=5)

                # --- Jetzt alle übrigen "verlorenen" Prozesse im Serverordner killen ---
                if sys.platform == "win32":
                    await _terminate_tree_windows([p.pid for p in related])
                else:
                    for p in related:
                        try:
                            p.terminate()
                        except Exception:
                            pass
                    await asyncio.sleep(1.0)
                    # zur Sicherheit hart
                    for p in related:
                        try:
                            if p.is_running():
                                p.kill()
                        except Exception:
                            pass

                # --- Verifikation: lebt noch irgendwas im Serverordner? ---
                still = _list_server_related_processes(base)
                if proc:
                    try:
                        if proc.is_running():
                            still.append(proc)
                    except Exception:
                        pass

                if still:
                    # Nichts entfernt! -> STOP fehlgeschlagen
                    fail_reason = f"{len(still)} Prozesse übrig"
                    write_action_log("stop", server_name, "failed", fail_reason)
                    logging.warning(f"[STOP] {server_name}: {len(still)} Prozesse laufen noch.")
                    return False

                # Erfolg
                server_processes.pop(server_name, None)
                save_pids(server_processes)
                write_action_log("stop", server_name, "success")
                logging.info(f"[STOP] {server_name} gestoppt.")
                success = True
                return True

            except Exception as e:
                # Letzter Versuch auf Windows: Full force
                if sys.platform == "win32":
                    try:
                        if proc:
                            await _win_taskkill(proc.pid, force=True)
                    except Exception:
                        pass
                fail_reason = str(e)
                write_action_log("stop", server_name, "failed", fail_reason)
                logging.exception(f"[STOP] Fehler beim Stop von {server_name}")
                return False
    finally:
        if success:
            end_operation_success(server_name)
        else:
            end_operation_failed(server_name, fail_reason or "Stop fehlgeschlagen")

async def recover_running_servers():
    """Lädt PIDs aus Cache und übernimmt laufende Server in den Prozess-Tracker."""
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
            # AccessDenied kann nach Neustarts vorkommen - dann kein Adoptieren
            write_action_log("recovery", name, "failed", f"Prozess nicht übernommen: {e}")


async def monitor_servers():
    """
    - Crash-Watch (auto_restart)
    - Auto-Stop zu stop_time
    - Beim Auto-Stop: Update (wenn auto_update) + Restart (wenn restart_after_stop)
    """
    last_reload = 0
    loop = asyncio.get_event_loop()

    while True:
        await asyncio.sleep(30)

        # Config-Reload alle 5 Minuten
        if (loop.time() - last_reload) > 300:
            load_server_configs()
            last_reload = loop.time()

        # Crash-Watch
        for name in list(server_processes.keys()):
            try:
                if not server_processes[name].is_running():
                    server_processes.pop(name)
                    cfg = SERVER_CONFIGS.get(name, {})
                    if cfg.get("auto_restart", True):
                        await start_server(name)
            except Exception:
                server_processes.pop(name, None)

        # Auto-Stop + Auto-Update
        now = datetime.now().strftime("%H:%M")
        for name in list(SERVER_PATHS.keys()):
            cfg = SERVER_CONFIGS.get(name, {})
            st = cfg.get("stop_time")
            if not st:
                continue

            if now == st and name not in daily_stopped_servers:
                if name in server_processes:
                    await stop_server(name)
                daily_stopped_servers.add(name)

                await auto_update_if_enabled(name)

                if cfg.get("restart_after_stop", False):
                    await asyncio.sleep(60)
                    await start_server(name)

            if now == "00:00":
                daily_stopped_servers.discard(name)


async def graceful_stop_all():
    """Stoppt alle Server beim Beenden des Bots."""
    for name in list(server_processes.keys()):
        try:
            await stop_server(name)
        except Exception:
            pass

