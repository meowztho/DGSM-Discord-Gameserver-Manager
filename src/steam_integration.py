import asyncio
import glob
import logging
import os
import shutil
import time as time_lib
from collections import defaultdict
from typing import Dict, Tuple, Optional, List

from config_store import BASE_DIR, STEAM_SESSIONS_DIR
from config_store import load_config
from config_store import get_config_value
import paths  # wichtig: Modul importieren, nicht einzelne Variablen
from runtime_status import begin_operation, end_operation_success, end_operation_failed

# ----------------------------
# Konfiguration
# ----------------------------
DEFAULT_TIMEOUT = int(os.getenv("STEAMCMD_TIMEOUT", "7200"))  # 2h Default
_UPDATE_LOCKS: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _resolve_steamcmd() -> Tuple[Optional[str], str]:
    default_dir = os.path.join(BASE_DIR, "steam")
    candidates: List[str] = []

    env_path = (os.getenv("STEAMCMD_PATH") or "").strip().strip('"').strip("'")
    if env_path:
        if os.path.isdir(env_path):
            candidates.append(os.path.join(env_path, "steamcmd.exe"))
            candidates.append(os.path.join(env_path, "steamcmd"))
        else:
            candidates.append(env_path)

    candidates.append(os.path.join(default_dir, "steamcmd.exe"))
    candidates.append(os.path.join(default_dir, "steamcmd"))

    for cmd_name in ("steamcmd.exe", "steamcmd"):
        found = shutil.which(cmd_name)
        if found:
            candidates.append(found)

    for candidate in candidates:
        if not candidate:
            continue
        if os.path.isabs(candidate):
            if os.path.exists(candidate):
                return candidate, os.path.dirname(candidate)
        else:
            found = shutil.which(candidate)
            if found:
                return found, os.path.dirname(found)
            if os.path.exists(candidate):
                abs_path = os.path.abspath(candidate)
                return abs_path, os.path.dirname(abs_path)

    return None, default_dir


def get_steamcmd_resolution() -> Tuple[Optional[str], str]:
    """Public helper für Diagnose/Statusanzeige."""
    return _resolve_steamcmd()


def _steam_runtime_dir() -> str:
    steamcmd, steam_dir = _resolve_steamcmd()
    if steamcmd:
        return os.path.dirname(steamcmd)
    return steam_dir


# ----------------------------
# Session files
# ----------------------------
def save_steam_session(server_name: str):
    session_dir = os.path.join(STEAM_SESSIONS_DIR, server_name)
    os.makedirs(session_dir, exist_ok=True)
    steam_dir = _steam_runtime_dir()
    patterns = ["ssfn*", "config.vdf", "steamapps/*.vdf", "appcache/appinfo.vdf"]
    for pattern in patterns:
        for file in glob.glob(os.path.join(steam_dir, pattern)):
            try:
                shutil.copy2(file, session_dir)
            except Exception as e:
                logging.warning(f"Session-Datei nicht kopiert: {file} - {e}")


def restore_steam_session(server_name: str) -> bool:
    session_dir = os.path.join(STEAM_SESSIONS_DIR, server_name)
    if not os.path.exists(session_dir):
        return False
    steam_dir = _steam_runtime_dir()
    if not os.path.isdir(steam_dir):
        return False
    for file in os.listdir(session_dir):
        try:
            shutil.copy2(os.path.join(session_dir, file), steam_dir)
        except Exception as e:
            logging.warning(f"Session-Datei nicht wiederhergestellt: {file} - {e}")
    return True


# ----------------------------
# Post-Install-Checks
# ----------------------------
def _find_executable_in_dir(install_dir: str, preferred: Optional[str]) -> Optional[str]:
    """Bevorzugt 'preferred', sonst heuristische Suche nach *Server*.exe, *Dedicated*.exe, *.exe"""
    if preferred:
        direct = os.path.join(install_dir, preferred)
        if os.path.isfile(direct):
            return direct
    patterns = ["*Server*.exe", "*Dedicated*.exe", "*.exe"]
    for pat in patterns:
        hits = glob.glob(os.path.join(install_dir, pat))
        hits.sort(key=lambda p: (("server" not in os.path.basename(p).lower()), len(p)))
        for h in hits:
            if os.path.isfile(h):
                return h
    return None


def _install_looks_good(server_name: str, install_dir: str) -> bool:
    """Ordner hat Dateien und eine plausible EXE ist auffindbar."""
    try:
        total = 0
        for _, _, files in os.walk(install_dir):
            total += len(files)
            if total > 10:
                break
        if total <= 0:
            return False
        preferred = get_config_value(server_name, "executable")
        exe = _find_executable_in_dir(install_dir, preferred)
        return exe is not None
    except Exception:
        return False


def is_update_running(server_name: str) -> bool:
    lock = _UPDATE_LOCKS.get(server_name)
    return bool(lock and lock.locked())


def _output_has_success_marker(output: str) -> bool:
    low = output.lower()
    return "success! app '" in low and "fully installed" in low


def _is_known_fatal_update_state(output: str) -> bool:
    low = output.lower()
    if "state is 0x606" in low:
        return True
    return ("error! app '" in low) and ("after update job" in low)


# ----------------------------
# SteamCMD
# ----------------------------
async def run_update(server_name: str) -> Tuple[bool, str]:
    lock = _UPDATE_LOCKS[server_name]
    if lock.locked():
        logging.info(f"[STEAMCMD] {server_name}: paralleles Update abgelehnt (läuft bereits).")
        return False, "Update läuft bereits"

    async with lock:
        success = False
        fail_reason = ""
        begin_operation(server_name, "update")

        try:
            session_restored = restore_steam_session(server_name)
            if session_restored:
                logging.info(f"Steam-Session für {server_name} wiederhergestellt")

            load_config()
            paths.load_server_paths()
            app_id = get_config_value(server_name, "app_id")
            if not app_id:
                fail_reason = "Keine App-ID konfiguriert"
                return False, fail_reason

            install_dir = paths.SERVER_PATHS.get(server_name) or os.path.join(
                BASE_DIR, "steam", "GSM", "servers", str(app_id), "serverfiles"
            )
            os.makedirs(install_dir, exist_ok=True)

            user = get_config_value(server_name, "username")
            pw = get_config_value(server_name, "password")

            steamcmd, steam_dir = _resolve_steamcmd()
            if not steamcmd:
                fail_reason = "SteamCMD nicht gefunden (STEAMCMD_PATH oder PATH prüfen)"
                return False, fail_reason

            cmd: List[str] = [steamcmd, "+force_install_dir", install_dir]
            if user and pw:
                cmd.extend(["+login", user, pw])
            else:
                cmd.extend(["+login", "anonymous"])
            cmd.extend(["+app_update", str(app_id), "validate", "+quit"])

            start_time = time_lib.time()
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=steam_dir if os.path.isdir(steam_dir) else BASE_DIR,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=DEFAULT_TIMEOUT)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                fail_reason = f"Timeout nach {DEFAULT_TIMEOUT/60:.0f} Minuten (SteamCMD beendet)"
                return False, fail_reason

            output = stdout.decode(errors="ignore") + stderr.decode(errors="ignore")
            duration = time_lib.time() - start_time
            logging.info(f"[STEAMCMD] {server_name} - Dauer: {duration:.2f}s\nOutput:\n{output}")

            if process.returncode != 0:
                if _is_known_fatal_update_state(output):
                    fail_reason = (
                        "SteamCMD meldet blockierten/parallelen Update-Job "
                        "(z. B. state 0x606). Bitte später erneut versuchen."
                    )
                    return False, fail_reason

                if _install_looks_good(server_name, install_dir) and _output_has_success_marker(output):
                    logging.warning(
                        f"[STEAMCMD] Non-zero Exit ({process.returncode}), aber Success-Marker gefunden: {server_name}"
                    )
                    save_steam_session(server_name)
                    success = True
                    return True, f"Installation offenbar ok (Exit {process.returncode})."

                mappings = {
                    "No subscription": "Account besitzt keine Lizenz",
                    "Steam Guard": "2FA benötigt",
                    "Invalid Password": "Ungültige Anmeldedaten",
                    "Not logged in": "Anmeldung fehlgeschlagen",
                    "0x202": "Verbindungsproblem",
                    "App not released": "App nicht verfügbar",
                    "Invalid platform": "Falsche Plattform",
                    "missing dependency": "Fehlende Abhängigkeiten",
                    "Access Denied": "Zugriffsrechte problem",
                    "Disk write failure": "Speicherplatzproblem",
                }
                error_msg = f"Fehlercode: {process.returncode}"
                for k, v in mappings.items():
                    if k in output:
                        error_msg = f"{v} | {error_msg}"
                        break

                fail_reason = f"{error_msg}\nAusgabe: {output[:1000]}"
                return False, fail_reason

            save_steam_session(server_name)
            success = True
            return True, f"Erfolgreich in {duration:.2f}s installiert"

        except Exception as e:
            logging.error(f"Kritischer Installationsfehler: {e}", exc_info=True)
            fail_reason = f"Systemfehler: {e}"
            return False, fail_reason

        finally:
            if success:
                end_operation_success(server_name)
            else:
                end_operation_failed(server_name, fail_reason or "Update fehlgeschlagen")


async def run_update_with_credentials(server_name: str, username: str, password: str) -> Tuple[bool, str]:
    lock = _UPDATE_LOCKS[server_name]
    if lock.locked():
        logging.info(f"[STEAMCMD] {server_name}: paralleles Login-Update abgelehnt (läuft bereits).")
        return False, "Update läuft bereits"

    async with lock:
        success = False
        fail_reason = ""
        begin_operation(server_name, "update")

        try:
            paths.load_server_paths()
            app_id = get_config_value(server_name, "app_id")
            if not app_id:
                fail_reason = "Keine App-ID konfiguriert"
                return False, fail_reason

            steamcmd, steam_dir = _resolve_steamcmd()
            if not steamcmd:
                fail_reason = "SteamCMD nicht gefunden (STEAMCMD_PATH oder PATH prüfen)"
                return False, fail_reason

            install_dir = paths.SERVER_PATHS.get(server_name) or os.path.join(
                BASE_DIR, "steam", "GSM", "servers", str(app_id), "serverfiles"
            )

            cmd = [
                steamcmd, "+force_install_dir", install_dir,
                "+login", username, password,
                "+app_update", str(app_id), "validate", "+quit"
            ]

            start_time = time_lib.time()
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=steam_dir if os.path.isdir(steam_dir) else BASE_DIR,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=DEFAULT_TIMEOUT)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                fail_reason = f"Timeout nach {DEFAULT_TIMEOUT/60:.0f} Minuten (SteamCMD beendet)"
                return False, fail_reason

            output = stdout.decode(errors="ignore") + stderr.decode(errors="ignore")
            duration = time_lib.time() - start_time
            logging.info(f"[STEAMCMD] {server_name} - Dauer: {duration:.2f}s\nOutput:\n{output}")

            if process.returncode != 0:
                if _is_known_fatal_update_state(output):
                    fail_reason = (
                        "SteamCMD meldet blockierten/parallelen Update-Job "
                        "(z. B. state 0x606). Bitte später erneut versuchen."
                    )
                    return False, fail_reason

                if _install_looks_good(server_name, install_dir) and _output_has_success_marker(output):
                    logging.warning(
                        f"[STEAMCMD] Non-zero Exit ({process.returncode}) nach Login, aber Success-Marker gefunden: {server_name}"
                    )
                    save_steam_session(server_name)
                    success = True
                    return True, f"Installation offenbar ok (Exit {process.returncode})."

                error_msg = f"Fehlercode: {process.returncode}"
                if "Steam Guard" in output:
                    error_msg = "2FA-Code ungültig oder abgelaufen"
                fail_reason = f"{error_msg}\nAusgabe: {output[:500]}"
                return False, fail_reason

            save_steam_session(server_name)
            success = True
            return True, f"Erfolgreich mit Login in {duration:.2f}s installiert"

        finally:
            if success:
                end_operation_success(server_name)
            else:
                end_operation_failed(server_name, fail_reason or "Update fehlgeschlagen")
