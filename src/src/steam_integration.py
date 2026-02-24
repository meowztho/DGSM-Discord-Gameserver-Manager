import asyncio
import glob
import logging
import os
import shutil
import tarfile
import time as time_lib
import urllib.request
import zipfile
from collections import defaultdict
from typing import Dict, Tuple, Optional, List
from urllib.parse import urlparse

from config_store import BASE_DIR, STEAM_SESSIONS_DIR
from config_store import load_config
from config_store import get_config_value
import paths  # wichtig: Modul importieren, nicht einzelne Variablen
from platform_utils import is_windows, is_linux, normalize_user_path, executable_path_variants
from runtime_status import begin_operation, end_operation_success, end_operation_failed

# ----------------------------
# Konfiguration
# ----------------------------
DEFAULT_TIMEOUT = int(os.getenv("STEAMCMD_TIMEOUT", "7200"))  # 2h Default
DEFAULT_STEAMCMD_DOWNLOAD_URL_WINDOWS = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
DEFAULT_STEAMCMD_DOWNLOAD_URL_LINUX = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz"
_UPDATE_LOCKS: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _steamcmd_candidate_names() -> List[str]:
    if is_windows():
        return ["steamcmd.exe", "steamcmd", "steamcmd.sh"]
    return ["steamcmd.sh", "steamcmd", "steamcmd.exe"]


def _default_steamcmd_download_url() -> str:
    if is_windows():
        return DEFAULT_STEAMCMD_DOWNLOAD_URL_WINDOWS
    if is_linux():
        return DEFAULT_STEAMCMD_DOWNLOAD_URL_LINUX
    return DEFAULT_STEAMCMD_DOWNLOAD_URL_WINDOWS


def _resolve_steamcmd() -> Tuple[Optional[str], str]:
    default_dir = os.path.join(BASE_DIR, "steam")
    candidates: List[str] = []

    env_path_raw = (os.getenv("STEAMCMD_PATH") or "").strip().strip('"').strip("'")
    if env_path_raw:
        env_path = os.path.expanduser(normalize_user_path(env_path_raw))
        if os.path.isdir(env_path):
            for cmd_name in _steamcmd_candidate_names():
                candidates.append(os.path.join(env_path, cmd_name))
        else:
            for variant in executable_path_variants(env_path):
                candidates.append(os.path.expanduser(variant))

    for cmd_name in _steamcmd_candidate_names():
        candidates.append(os.path.join(default_dir, cmd_name))

    for cmd_name in _steamcmd_candidate_names():
        found = shutil.which(cmd_name)
        if found:
            candidates.append(found)

    for candidate in candidates:
        if not candidate:
            continue
        if os.path.isabs(candidate):
            if os.path.isfile(candidate):
                return candidate, os.path.dirname(candidate)
        else:
            found = shutil.which(candidate)
            if found:
                return found, os.path.dirname(found)
            if os.path.isfile(candidate):
                abs_path = os.path.abspath(candidate)
                return abs_path, os.path.dirname(abs_path)

    return None, default_dir


def _steamcmd_download_url() -> str:
    url = (os.getenv("STEAMCMD_DOWNLOAD_URL") or "").strip()
    if url:
        return url
    return _default_steamcmd_download_url()


def _download_and_extract_steamcmd(target_dir: str, url: str) -> None:
    os.makedirs(target_dir, exist_ok=True)
    parsed_name = os.path.basename(urlparse(url).path)
    archive_name = parsed_name or ("steamcmd.zip" if is_windows() else "steamcmd_linux.tar.gz")
    archive_path = os.path.join(target_dir, archive_name)
    tmp_path = archive_path + ".part"

    with urllib.request.urlopen(url, timeout=120) as response, open(tmp_path, "wb") as out:
        shutil.copyfileobj(response, out, length=1024 * 1024)
    os.replace(tmp_path, archive_path)

    lower_name = archive_name.lower()
    if lower_name.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as archive:
            archive.extractall(target_dir)
    elif lower_name.endswith((".tar.gz", ".tgz", ".tar")):
        with tarfile.open(archive_path, "r:*") as archive:
            archive.extractall(target_dir)
    else:
        # Fallback: erst zip, dann tar versuchen.
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                archive.extractall(target_dir)
        except zipfile.BadZipFile:
            with tarfile.open(archive_path, "r:*") as archive:
                archive.extractall(target_dir)

    try:
        os.remove(archive_path)
    except Exception:
        pass

    if not is_windows():
        for name in ("steamcmd.sh", "steamcmd", os.path.join("linux32", "steamcmd")):
            p = os.path.join(target_dir, name)
            if os.path.isfile(p):
                try:
                    os.chmod(p, 0o755)
                except Exception:
                    pass


def ensure_steamcmd_available() -> Tuple[bool, str]:
    steamcmd, steam_dir = _resolve_steamcmd()
    if steamcmd:
        return True, f"SteamCMD found: {steamcmd}"

    env_path_raw = (os.getenv("STEAMCMD_PATH") or "").strip().strip('"').strip("'")
    if env_path_raw:
        env_path = os.path.expanduser(normalize_user_path(env_path_raw))
        return False, f"SteamCMD not found at STEAMCMD_PATH='{env_path}'"

    url = _steamcmd_download_url()
    try:
        logging.info("SteamCMD missing, downloading to %s", steam_dir)
        _download_and_extract_steamcmd(steam_dir, url)
    except Exception as exc:
        return False, f"SteamCMD auto-download failed: {exc}"

    steamcmd, _steam_dir = _resolve_steamcmd()
    if steamcmd:
        return True, f"SteamCMD downloaded: {steamcmd}"
    return False, "SteamCMD downloaded, but executable could not be resolved"


def get_steamcmd_resolution() -> Tuple[Optional[str], str]:
    """Public helper für Diagnose/Statusanzeige."""
    return _resolve_steamcmd()


def _steam_runtime_dir() -> str:
    steamcmd, steam_dir = _resolve_steamcmd()
    if steamcmd:
        return os.path.dirname(steamcmd)
    return steam_dir


def _steamcmd_base_command(steamcmd_path: str) -> List[str]:
    if not is_windows() and steamcmd_path.lower().endswith(".sh") and not os.access(steamcmd_path, os.X_OK):
        return ["sh", steamcmd_path]
    return [steamcmd_path]


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
    """Bevorzugt 'preferred', sonst heuristische Suche nach plausiblen Startdateien."""
    if preferred:
        for variant in executable_path_variants(preferred):
            normalized = os.path.expanduser(variant)
            direct = normalized if os.path.isabs(normalized) else os.path.join(install_dir, normalized)
            if os.path.isfile(direct):
                return direct

        # Template-Kompatibilität: z. B. "PalServer.exe" -> "PalServer-Linux-*" erkennen.
        if is_linux():
            raw = normalize_user_path(preferred)
            stem = os.path.splitext(os.path.basename(raw))[0].lower()
            if stem:
                prefix_hits = glob.glob(os.path.join(install_dir, f"{stem}*"))
                prefix_hits.sort(key=lambda p: (("server" not in os.path.basename(p).lower()), len(p)))
                for hit in prefix_hits:
                    if not os.path.isfile(hit):
                        continue
                    low = os.path.basename(hit).lower()
                    if (
                        os.access(hit, os.X_OK)
                        or low.endswith((".sh", ".x86_64", ".run", ".bin"))
                        or "." not in os.path.basename(hit)
                    ):
                        return hit
                prefix_hits = glob.glob(os.path.join(install_dir, "**", f"{stem}*"), recursive=True)
                prefix_hits.sort(key=lambda p: (("server" not in os.path.basename(p).lower()), len(p)))
                for hit in prefix_hits:
                    if not os.path.isfile(hit):
                        continue
                    low = os.path.basename(hit).lower()
                    if (
                        os.access(hit, os.X_OK)
                        or low.endswith((".sh", ".x86_64", ".run", ".bin"))
                        or "." not in os.path.basename(hit)
                    ):
                        return hit

    if is_windows():
        patterns = ["*Server*.exe", "*Dedicated*.exe", "*.exe", "*.bat", "*.cmd"]
    else:
        patterns = [
            "*Server*.x86_64",
            "*server*.x86_64",
            "*Dedicated*.x86_64",
            "*dedicated*.x86_64",
            "*Server*.sh",
            "*server*.sh",
            "*Dedicated*.sh",
            "*dedicated*.sh",
            "*Server*",
            "*server*",
            "*.sh",
        ]
    for pat in patterns:
        hits = glob.glob(os.path.join(install_dir, pat))
        hits.sort(key=lambda p: (("server" not in os.path.basename(p).lower()), len(p)))
        for h in hits:
            if os.path.isfile(h):
                return h
    for pat in patterns:
        hits = glob.glob(os.path.join(install_dir, "**", pat), recursive=True)
        hits.sort(key=lambda p: (("server" not in os.path.basename(p).lower()), len(p)))
        for h in hits:
            if os.path.isfile(h):
                return h
    return None


def _install_looks_good(server_name: str, install_dir: str) -> bool:
    """Ordner hat Dateien und eine plausible Startdatei ist auffindbar."""
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


def _linux_steamcmd_dependency_hint(output: str) -> Optional[str]:
    if not is_linux():
        return None
    low = (output or "").lower()
    markers = (
        "linux32/steamcmd: cannot execute: required file not found",
        "error while loading shared libraries",
        "no such file or directory",
        "wrong elf class",
    )
    if not any(marker in low for marker in markers):
        return None
    return (
        "SteamCMD kann unter Linux nicht starten (meist fehlende 32-bit Laufzeitbibliotheken).\n"
        "Installiere in WSL/Ubuntu typischerweise:\n"
        "sudo dpkg --add-architecture i386\n"
        "sudo apt update\n"
        "sudo apt install -y libc6:i386 libstdc++6:i386 libgcc-s1:i386 lib32gcc-s1 lib32stdc++6 ca-certificates\n"
        "Danach DGSM/Update erneut starten."
    )


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
                ok_cmd, cmd_message = ensure_steamcmd_available()
                if not ok_cmd:
                    fail_reason = f"SteamCMD nicht gefunden ({cmd_message})"
                    return False, fail_reason
                steamcmd, steam_dir = _resolve_steamcmd()
                if not steamcmd:
                    fail_reason = "SteamCMD nicht gefunden (nach Auto-Download)"
                    return False, fail_reason

            cmd: List[str] = _steamcmd_base_command(steamcmd) + ["+force_install_dir", install_dir]
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

                dep_hint = _linux_steamcmd_dependency_hint(output)
                if dep_hint:
                    fail_reason = dep_hint
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
                ok_cmd, cmd_message = ensure_steamcmd_available()
                if not ok_cmd:
                    fail_reason = f"SteamCMD nicht gefunden ({cmd_message})"
                    return False, fail_reason
                steamcmd, steam_dir = _resolve_steamcmd()
                if not steamcmd:
                    fail_reason = "SteamCMD nicht gefunden (nach Auto-Download)"
                    return False, fail_reason

            install_dir = paths.SERVER_PATHS.get(server_name) or os.path.join(
                BASE_DIR, "steam", "GSM", "servers", str(app_id), "serverfiles"
            )

            cmd = _steamcmd_base_command(steamcmd) + [
                "+force_install_dir", install_dir,
                "+login", username, password,
                "+app_update", str(app_id), "validate", "+quit",
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

                dep_hint = _linux_steamcmd_dependency_hint(output)
                if dep_hint:
                    fail_reason = dep_hint
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
