"""DGSM-Template-Plugin: Hytale Dedicated Server.

Logik portiert aus WindowsGSM.Hytale von raziel7893 (MIT):
https://github.com/Raziel7893/WindowsGSM.Hytale

DGSM ruft beim Install/Update `install(serverfiles, ctx)` auf. `ctx` bietet
stabile Helfer:
    ctx.log                      Logger
    ctx.is_windows()             -> bool
    ctx.download(url, dest)      Datei laden
    ctx.http_get_bytes(url)      -> bytes
    ctx.extract_zip_file(zip, dir)
    ctx.extract_zip_bytes(data, dir)
    ctx.ensure_jre(dir, major)   Temurin-JRE nach dir/jre
    ctx.write_eula(dir)
    ctx.run_logged(cmd, cwd, timeout, prefix) -> (returncode, ausgabe_zeilen)

Der eigentliche Server ist nur ueber das offizielle Downloader-Tool mit
einmaligem Geraete-Login (OAuth) erreichbar. DGSM startet den Downloader
selbst und streamt die Login-URL ins Log; nach der Browser-Bestaetigung werden
die Credentials gecacht und kuenftige Updates laufen vollautomatisch.
"""

import os
import shutil

DOWNLOADER_URL = "https://downloader.hytale.com/hytale-downloader.zip"
JAVA_MAJOR = "25"
DOWNLOADER_TIMEOUT = int(os.getenv("HYTALE_DOWNLOADER_TIMEOUT", "1200"))


def _downloader_name(ctx):
    return "hytale-downloader-windows-amd64.exe" if ctx.is_windows() else "hytale-downloader-linux-amd64"


def _write_setup_note(serverfiles, installer_dir, ctx):
    exe = _downloader_name(ctx)
    run = (exe if ctx.is_windows() else f"./{exe}") + (
        ' -download-path "Hytale.zip" -credentials-path ".hytale-downloader-credentials.json" -skip-update-check'
    )
    note = (
        "Hytale - Login (OAuth)\n"
        "======================\n"
        "DGSM startet den Downloader bei Install/Update automatisch und zeigt die\n"
        "Login-URL im Log/Live-Log. Falls noetig kannst du den Download auch manuell\n"
        "ausloesen:\n\n"
        f"  cd \"{installer_dir}\"\n  {run}\n\n"
        "Login-URL im Browser oeffnen, mit Hytale-Account bestaetigen. Danach\n"
        "erzeugt das Tool 'Hytale.zip' + Credentials; in DGSM erneut 'Update'\n"
        "druecken. Kuenftige Updates laufen dann ohne Login.\n\n"
        "Start: java -jar Server/HytaleServer.jar --assets Assets.zip --bind <IP>:5520\n"
        "Port 5520/UDP ggf. in Router/Firewall freigeben.\n"
    )
    with open(os.path.join(serverfiles, "HYTALE_SETUP.txt"), "w", encoding="utf-8") as f:
        f.write(note)


def install(serverfiles, ctx):
    os.makedirs(serverfiles, exist_ok=True)
    installer_dir = os.path.join(serverfiles, "installer")
    os.makedirs(installer_dir, exist_ok=True)

    # 1) Java 25 bereitstellen
    ctx.ensure_jre(serverfiles, JAVA_MAJOR)

    # 2) Downloader-Tool sicherstellen
    downloader = os.path.join(installer_dir, _downloader_name(ctx))
    if not os.path.isfile(downloader):
        dl_zip = os.path.join(installer_dir, "hytale-downloader.zip")
        ctx.download(DOWNLOADER_URL, dl_zip)
        ctx.extract_zip_file(dl_zip, installer_dir)
        if not ctx.is_windows():
            try:
                os.chmod(downloader, 0o755)
            except Exception:
                pass

    creds = os.path.join(installer_dir, ".hytale-downloader-credentials.json")
    hytale_zip = os.path.join(installer_dir, "Hytale.zip")

    # 3) Downloader ausfuehren -> Server-Bundle laden (Login-URL wird gestreamt)
    auth_lines = []
    if os.path.isfile(downloader):
        if not os.path.isfile(creds):
            ctx.log.warning(
                "[HYTALE] Erstmaliger Login noetig - die gleich ausgegebene Login-URL "
                "im Browser oeffnen und mit dem Hytale-Account bestaetigen."
            )
        rc, auth_lines = ctx.run_logged(
            [downloader, "-download-path", hytale_zip,
             "-skip-update-check", "-credentials-path", creds],
            installer_dir, DOWNLOADER_TIMEOUT, "[HYTALE]",
        )
        ctx.log.info("[HYTALE] Downloader beendet (rc=%s)", rc)

    # 4) Hytale.zip entpacken -> Server/ + Assets.zip (sauberer Stand)
    if os.path.isfile(hytale_zip):
        shutil.rmtree(os.path.join(serverfiles, "Server"), ignore_errors=True)
        try:
            os.remove(os.path.join(serverfiles, "Assets.zip"))
        except FileNotFoundError:
            pass
        except Exception:
            pass
        ctx.extract_zip_file(hytale_zip, serverfiles)

    if os.path.isfile(os.path.join(serverfiles, "Server", "HytaleServer.jar")):
        return "Hytale installiert/aktualisiert (Server-Dateien vorhanden)"

    # 5) Kein Bundle -> Login noch offen
    _write_setup_note(serverfiles, installer_dir, ctx)
    url = next((l for l in auth_lines if "http" in l.lower()), "")
    hint = f" Login-URL: {url}" if url else ""
    return (
        "Hytale: Java + Downloader bereit, Server-Bundle fehlt noch - Login im "
        "Browser bestaetigen, dann erneut 'Update' druecken." + hint
    )
