"""Installer für Nicht-Steam-Spiele (Minecraft: Vanilla, Fabric, Bedrock).

Anbindung:
- Eine Steam-App-ID besteht ausschließlich aus Ziffern. Sobald eine "App-ID"
  einen Buchstaben enthält, ist es KEIN Steam-Server und SteamCMD wird nie
  aufgerufen (siehe steam_integration.run_update). Der Buchstaben-Wert dient
  hier zugleich als Provider-Kennung, z. B.:
      minecraft_vanilla | minecraft_fabric | minecraft_bedrock

- Der Installer legt nur Dateien im serverfiles-Ordner ab. Welche Datei
  gestartet wird (executable/parameters) bestimmt weiterhin das Template bzw.
  die server_settings.json. Damit läuft der Start identisch zu allen anderen
  Servern.

- Java (für Vanilla/Fabric) wird als Temurin-JRE automatisch nach
  serverfiles/jre entpackt. Der Nutzer muss nichts am Host installieren.
"""

import asyncio
import io
import json
import logging
import os
import shutil
import ssl
import time
import urllib.request
import zipfile
from typing import Tuple

from platform_utils import is_windows


def _build_ssl_context() -> ssl.SSLContext:
    """SSL-Kontext mit explizitem CA-Bundle.

    Im PyInstaller-Build steht der OS-Zertifikatsspeicher nicht zuverlässig
    zur Verfügung (CERTIFICATE_VERIFY_FAILED, u. a. beim Adoptium->GitHub
    Redirect). Daher bevorzugt das mitgelieferte certifi-Bundle nutzen.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_SSL_CTX = _build_ssl_context()


# ----------------------------------------------------------------------------
# Konfiguration
# ----------------------------------------------------------------------------
_DOWNLOAD_TIMEOUT = int(os.getenv("CUSTOM_INSTALL_TIMEOUT", "1800"))  # 30 min Gesamt
_HTTP_TIMEOUT = int(os.getenv("CUSTOM_INSTALL_HTTP_TIMEOUT", "300"))  # 5 min/Datei
_USER_AGENT = "DGSM/CustomInstaller (+https://fabricmc.net)"
_RETRIES = int(os.getenv("CUSTOM_INSTALL_RETRIES", "3"))

# Fallback-Java-Major, falls die Mojang-Metadaten keine Angabe enthalten.
# Im Normalfall wird die benötigte Java-Version aus dem Versions-Manifest
# gelesen (javaVersion.majorVersion), damit neue MC-Versionen automatisch die
# passende JRE bekommen (z. B. MC 26.2 => Java 25).
_JAVA_MAJOR = os.getenv("CUSTOM_INSTALL_JAVA", "21")

_MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
_FABRIC_META = "https://meta.fabricmc.net/v2"
_BEDROCK_LINKS = "https://net-secondary.web.minecraft-services.net/api/v1.0/download/links"


def is_steam_app_id(app_id: str) -> bool:
    """Nur rein numerische IDs sind Steam-App-IDs."""
    app_id = str(app_id or "").strip()
    return app_id.isdigit()


def provider_from_app_id(app_id: str) -> str:
    return str(app_id or "").strip().lower()


# ----------------------------------------------------------------------------
# HTTP-Helfer (blockierend -> via asyncio.to_thread aufrufen)
# ----------------------------------------------------------------------------
def _retry(fn, what: str):
    """Wiederholt fn() bei transienten Netzwerkfehlern (z. B. CDN trennt
    Verbindung). So bleibt die Installation nicht an einem Aussetzer hängen."""
    last = None
    for attempt in range(1, _RETRIES + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            logging.warning("[CUSTOM] %s fehlgeschlagen (Versuch %d/%d): %s",
                            what, attempt, _RETRIES, exc)
            if attempt < _RETRIES:
                time.sleep(2 * attempt)
    raise last


def _http_get_bytes(url: str) -> bytes:
    def _do() -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT, context=_SSL_CTX) as resp:
            return resp.read()

    return _retry(_do, f"GET {url}")


def _http_get_json(url: str) -> dict:
    return json.loads(_http_get_bytes(url).decode("utf-8", errors="ignore"))


def _download_to(url: str, dest_path: str) -> None:
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp = dest_path + ".part"

    def _do() -> None:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT, context=_SSL_CTX) as resp, open(tmp, "wb") as out:
            shutil.copyfileobj(resp, out, length=1024 * 1024)

    _retry(_do, f"Download {os.path.basename(dest_path)}")
    os.replace(tmp, dest_path)


def _extract_zip_bytes(data: bytes, target_dir: str) -> None:
    os.makedirs(target_dir, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(target_dir)


# ----------------------------------------------------------------------------
# Java / JRE
# ----------------------------------------------------------------------------
def _adoptium_os_arch() -> Tuple[str, str]:
    import platform as _pf

    os_name = "windows" if is_windows() else "linux"
    machine = (_pf.machine() or "").lower()
    if machine in ("arm64", "aarch64"):
        arch = "aarch64"
    elif machine in ("x86_64", "amd64", "x64"):
        arch = "x64"
    else:
        arch = "x64"
    return os_name, arch


def _jre_marker(serverfiles: str) -> str:
    return os.path.join(serverfiles, "jre", ".dgsm_java_major")


def _ensure_jre(serverfiles: str, java_major=None) -> None:
    """Lädt eine Temurin-JRE nach serverfiles/jre, falls noch nicht vorhanden
    oder die geforderte Major-Version abweicht."""
    major = str(java_major or _JAVA_MAJOR)
    jre_dir = os.path.join(serverfiles, "jre")
    java_exe = os.path.join(jre_dir, "bin", "java.exe" if is_windows() else "java")
    marker = _jre_marker(serverfiles)

    if os.path.isfile(java_exe):
        installed = ""
        try:
            with open(marker, "r", encoding="utf-8") as f:
                installed = f.read().strip()
        except Exception:
            installed = ""
        if installed == major:
            logging.info("[CUSTOM] JRE %s bereits vorhanden: %s", major, java_exe)
            return
        logging.info("[CUSTOM] JRE-Wechsel %s -> %s, ersetze %s", installed or "?", major, jre_dir)

    os_name, arch = _adoptium_os_arch()
    url = (
        f"https://api.adoptium.net/v3/binary/latest/{major}/ga/"
        f"{os_name}/{arch}/jre/hotspot/normal/eclipse"
    )
    logging.info("[CUSTOM] Lade JRE %s (%s/%s) ...", major, os_name, arch)
    data = _http_get_bytes(url)

    # Temurin liefert .zip (Windows) bzw. .tar.gz (Linux).
    tmp_extract = os.path.join(serverfiles, "_jre_tmp")
    if os.path.isdir(tmp_extract):
        shutil.rmtree(tmp_extract, ignore_errors=True)
    os.makedirs(tmp_extract, exist_ok=True)

    if is_windows():
        _extract_zip_bytes(data, tmp_extract)
    else:
        import tarfile

        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            tf.extractall(tmp_extract)

    # Das Archiv enthält genau einen Top-Level-Ordner (z. B. jdk-21.0.4+7-jre).
    entries = [os.path.join(tmp_extract, e) for e in os.listdir(tmp_extract)]
    inner = next((e for e in entries if os.path.isdir(e)), None)
    if not inner:
        shutil.rmtree(tmp_extract, ignore_errors=True)
        raise RuntimeError("JRE-Archiv hatte kein erwartetes Wurzelverzeichnis")

    if os.path.isdir(jre_dir):
        shutil.rmtree(jre_dir, ignore_errors=True)
    shutil.move(inner, jre_dir)
    shutil.rmtree(tmp_extract, ignore_errors=True)

    if not is_windows():
        try:
            os.chmod(os.path.join(jre_dir, "bin", "java"), 0o755)
        except Exception:
            pass
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write(major)
    except Exception:
        pass
    logging.info("[CUSTOM] JRE %s installiert: %s", major, jre_dir)


def _write_eula(serverfiles: str) -> None:
    with open(os.path.join(serverfiles, "eula.txt"), "w", encoding="utf-8") as f:
        f.write("# Akzeptiert via DGSM (https://aka.ms/MinecraftEULA)\neula=true\n")


# ----------------------------------------------------------------------------
# Minecraft-Versionsauflösung
# ----------------------------------------------------------------------------
def _latest_release() -> Tuple[str, dict]:
    """(version_id, version_meta) der aktuellen Release-Version."""
    manifest = _http_get_json(_MOJANG_MANIFEST)
    release = manifest.get("latest", {}).get("release")
    entry = next((v for v in manifest.get("versions", []) if v.get("id") == release), None)
    if not entry:
        raise RuntimeError("Konnte aktuelle Minecraft-Release nicht ermitteln")
    meta = _http_get_json(entry["url"])
    return release, meta


# ----------------------------------------------------------------------------
# Provider-Installer (blockierend)
# ----------------------------------------------------------------------------
def _install_vanilla(serverfiles: str) -> str:
    os.makedirs(serverfiles, exist_ok=True)
    version, meta = _latest_release()
    server_url = meta.get("downloads", {}).get("server", {}).get("url")
    if not server_url:
        raise RuntimeError(f"Version {version} bietet keinen Server-Download")
    _download_to(server_url, os.path.join(serverfiles, "server.jar"))
    _ensure_jre(serverfiles, (meta.get("javaVersion") or {}).get("majorVersion"))
    _write_eula(serverfiles)
    return f"Minecraft (Vanilla) {version} installiert"


def _install_fabric(serverfiles: str) -> str:
    os.makedirs(serverfiles, exist_ok=True)
    version, meta = _latest_release()

    loaders = _http_get_json(f"{_FABRIC_META}/versions/loader/{version}")
    loader = next((l for l in loaders if l.get("loader", {}).get("stable")), loaders[0])
    loader_ver = loader["loader"]["version"]

    installers = _http_get_json(f"{_FABRIC_META}/versions/installer")
    installer = next((i for i in installers if i.get("stable")), installers[0])
    installer_ver = installer["version"]

    launch_url = (
        f"{_FABRIC_META}/versions/loader/{version}/{loader_ver}/{installer_ver}/server/jar"
    )
    _download_to(launch_url, os.path.join(serverfiles, "fabric-server-launch.jar"))

    # Vanilla-Server-Jar mitliefern, damit der Start ohne weiteren Download
    # auskommt; Fabric findet sie über die Launcher-Properties.
    server_url = meta.get("downloads", {}).get("server", {}).get("url")
    if server_url:
        _download_to(server_url, os.path.join(serverfiles, "server.jar"))
        with open(
            os.path.join(serverfiles, "fabric-server-launcher.properties"),
            "w",
            encoding="utf-8",
        ) as f:
            f.write("serverJar=server.jar\n")

    _ensure_jre(serverfiles, (meta.get("javaVersion") or {}).get("majorVersion"))
    _write_eula(serverfiles)
    return f"Minecraft (Fabric) {version} / Loader {loader_ver} installiert"


def _install_bedrock(serverfiles: str) -> str:
    os.makedirs(serverfiles, exist_ok=True)
    want = "serverBedrockWindows" if is_windows() else "serverBedrockLinux"
    data = _http_get_json(_BEDROCK_LINKS)
    links = data.get("result", {}).get("links", [])
    entry = next((l for l in links if l.get("downloadType") == want), None)
    if not entry or not entry.get("downloadUrl"):
        raise RuntimeError("Bedrock-Download-Link nicht gefunden (API-Format geändert?)")

    payload = _http_get_bytes(entry["downloadUrl"])

    # Vorhandene Welt-/Konfigdateien nicht überschreiben.
    keep = {"worlds", "server.properties", "permissions.json", "allowlist.json", "whitelist.json"}
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        for member in zf.namelist():
            top = member.split("/", 1)[0]
            dest = os.path.join(serverfiles, member)
            if top in keep and os.path.exists(dest):
                continue
            zf.extract(member, serverfiles)
    return "Minecraft (Bedrock) installiert"


_INSTALLERS = {
    "minecraft_vanilla": _install_vanilla,
    "minecraft_fabric": _install_fabric,
    "minecraft_bedrock": _install_bedrock,
}


def supported_providers() -> list:
    return sorted(_INSTALLERS.keys())


async def run_custom_install(server_name: str, app_id: str, install_dir: str) -> Tuple[bool, str]:
    """Dispatch anhand der (nicht-numerischen) App-ID = Provider-Kennung.

    Gibt (ok, message) zurück. Status (begin/end_operation) verwaltet der
    Aufrufer (steam_integration.run_update), damit der Update-Button identisch
    funktioniert.
    """
    provider = provider_from_app_id(app_id)
    installer = _INSTALLERS.get(provider)
    if not installer:
        return False, (
            f"Unbekannter Installer '{provider}'. "
            f"Verfügbar: {', '.join(supported_providers())}"
        )

    try:
        message = await asyncio.wait_for(
            asyncio.to_thread(installer, install_dir),
            timeout=_DOWNLOAD_TIMEOUT,
        )
        logging.info("[CUSTOM] %s: %s", server_name, message)
        return True, message
    except asyncio.TimeoutError:
        return False, f"Timeout nach {_DOWNLOAD_TIMEOUT // 60} Minuten beim Download"
    except Exception as exc:  # noqa: BLE001 - Fehler an UI weiterreichen
        logging.exception("[CUSTOM] Installationsfehler für %s", server_name)
        return False, f"Installationsfehler: {exc}"
