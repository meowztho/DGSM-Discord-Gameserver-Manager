import asyncio
import glob
import logging
import os
import shutil
import time as time_lib
from typing import Tuple, Optional, List

from config_store import BASE_DIR, STEAM_SESSIONS_DIR
from config_store import load_config
from config_store import get_config_value
import paths  # wichtig: Modul importieren, nicht einzelne Variablen

# ----------------------------
# Konfiguration
# ----------------------------
DEFAULT_TIMEOUT = int(os.getenv("STEAMCMD_TIMEOUT", "7200"))  # 2h Default

# ----------------------------
# Session files
# ----------------------------
def save_steam_session(server_name: str):
    session_dir = os.path.join(STEAM_SESSIONS_DIR, server_name)
    os.makedirs(session_dir, exist_ok=True)
    steam_dir = os.path.join(BASE_DIR, "steam")
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
    steam_dir = os.path.join(BASE_DIR, "steam")
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

# ----------------------------
# SteamCMD
# ----------------------------
async def run_update(server_name: str) -> Tuple[bool, str]:
    """
    Falls steamcmd mit non-zero-Exit rausgeht, aber die Dateien da sind + EXE gefunden wurde,
    behandeln wir das als Erfolg (mit Warnung). Timeout ist über STEAMCMD_TIMEOUT konfigurierbar.
    """
    try:
        session_restored = restore_steam_session(server_name)
        if session_restored:
            logging.info(f"Steam-Session für {server_name} wiederhergestellt")

        load_config()
        paths.load_server_paths()
        app_id = get_config_value(server_name, "app_id")
        if not app_id:
            return False, "Keine App-ID konfiguriert"

        install_dir = paths.SERVER_PATHS.get(server_name) or os.path.join(
            BASE_DIR, "steam", "GSM", "servers", str(app_id), "serverfiles"
        )
        os.makedirs(install_dir, exist_ok=True)

        user = get_config_value(server_name, "username")
        pw = get_config_value(server_name, "password")

        steamcmd = os.path.join(BASE_DIR, "steam", "steamcmd.exe")
        if not os.path.exists(steamcmd):
            return False, f"SteamCMD nicht gefunden unter {steamcmd}"

        cmd: List[str] = [steamcmd, "+force_install_dir", install_dir]
        if user and pw:
            cmd.extend(["+login", user, pw])
        else:
            cmd.extend(["+login", "anonymous"])
        cmd.extend(["+app_update", str(app_id), "validate", "+quit"])

        start_time = time_lib.time()
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=os.path.join(BASE_DIR, "steam"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=DEFAULT_TIMEOUT)
        except asyncio.TimeoutError:
            if _install_looks_good(server_name, install_dir):
                logging.warning(f"[STEAMCMD] Timeout, aber Dateien vorhanden → Erfolg: {server_name}")
                save_steam_session(server_name)
                return True, f"Timeout nach {DEFAULT_TIMEOUT/60:.0f} Min., aber Dateien vorhanden – weiter."
            process.kill()
            await process.communicate()
            return False, f"Timeout nach {DEFAULT_TIMEOUT/60:.0f} Minuten"

        output = stdout.decode(errors="ignore") + stderr.decode(errors="ignore")
        duration = time_lib.time() - start_time
        logging.info(f"[STEAMCMD] {server_name} - Dauer: {duration:.2f}s\nOutput:\n{output}")

        if process.returncode != 0:
            if _install_looks_good(server_name, install_dir):
                logging.warning(f"[STEAMCMD] Non-zero Exit ({process.returncode}), aber Installation plausibel vollständig: {server_name}")
                save_steam_session(server_name)
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
            return False, f"{error_msg}\nAusgabe: {output[:1000]}"

        save_steam_session(server_name)
        return True, f"Erfolgreich in {duration:.2f}s installiert"

    except Exception as e:
        logging.error(f"Kritischer Installationsfehler: {e}", exc_info=True)
        return False, f"Systemfehler: {e}"

async def run_update_with_credentials(server_name: str, username: str, password: str) -> Tuple[bool, str]:
    paths.load_server_paths()
    app_id = get_config_value(server_name, "app_id")
    if not app_id:
        return False, "Keine App-ID konfiguriert"

    steamcmd = os.path.join(BASE_DIR, "steam", "steamcmd.exe")
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
        cwd=os.path.join(BASE_DIR, "steam"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=DEFAULT_TIMEOUT)
    except asyncio.TimeoutError:
        if _install_looks_good(server_name, install_dir):
            logging.warning(f"[STEAMCMD] Timeout (login), aber Dateien vorhanden → Erfolg: {server_name}")
            save_steam_session(server_name)
            return True, f"Timeout, aber Dateien vorhanden – weiter."
        process.kill()
        await process.communicate()
        return False, "Timeout"

    output = stdout.decode(errors="ignore") + stderr.decode(errors="ignore")
    duration = time_lib.time() - start_time
    logging.info(f"[STEAMCMD] {server_name} - Dauer: {duration:.2f}s\nOutput:\n{output}")

    if process.returncode != 0:
        if _install_looks_good(server_name, install_dir):
            logging.warning(f"[STEAMCMD] Non-zero Exit ({process.returncode}) nach Login, aber Installation plausibel vollständig: {server_name}")
            save_steam_session(server_name)
            return True, f"Installation offenbar ok (Exit {process.returncode})."
        error_msg = f"Fehlercode: {process.returncode}"
        if "Steam Guard" in output:
            error_msg = "2FA-Code ungültig oder abgelaufen"
        return False, f"{error_msg}\nAusgabe: {output[:500]}"

    save_steam_session(server_name)
    return True, f"Erfolgreich mit Login in {duration:.2f}s installiert"
