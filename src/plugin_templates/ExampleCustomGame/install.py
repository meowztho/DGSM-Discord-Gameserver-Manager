"""DGSM-Beispiel-Plugin (Vorlage zum Kopieren).

So fuegst du einen Nicht-Steam-Server mit eigener Install-Logik hinzu, ohne
den DGSM-Core anzufassen:

1. Kopiere diesen Ordner unter plugin_templates/<DeinSpiel>/.
2. In config.json: app_id auf einen NICHT-numerischen Wert setzen (z. B.
   "<deinspiel>") - das routet automatisch hierher statt zu SteamCMD. Trage
   executable + parameters fuer den Serverstart ein.
3. Diese install.py anpassen (siehe unten) und den Platzhalter-Guard entfernen.
4. In DGSM: /addserver template:<DeinSpiel>  (Discord, Desktop- oder Web-UI).
   DGSM kopiert das Template in den Serverordner und ruft install() auf -
   bei Erstinstallation und bei jedem Update-Klick.

DGSM ruft auf:  install(serverfiles, ctx) -> str (Statusmeldung)

`serverfiles` : absoluter Pfad zum serverfiles-Ordner dieser Server-Instanz.
`ctx`         : stabile Helfer-API (rueckwaertskompatibel):
    ctx.log                          -> Logger (log.info/warning/...)
    ctx.is_windows()                 -> bool
    ctx.download(url, dest_path)     -> Datei laden (mit Retry, certifi-SSL)
    ctx.http_get_bytes(url)          -> bytes
    ctx.http_get_json(url)           -> dict   (z. B. fuer Versions-APIs)
    ctx.extract_zip_bytes(data, dir) -> ZIP aus Bytes entpacken
    ctx.extract_zip_file(zip, dir)   -> ZIP-Datei entpacken
    ctx.ensure_jre(serverfiles, major) -> Temurin-JRE nach serverfiles/jre
                                          (major z. B. "21" oder "25")
    ctx.write_eula(serverfiles)      -> Minecraft-style eula.txt schreiben
    ctx.run_logged(cmd, cwd, timeout, prefix) -> (returncode, ausgabe_zeilen)
                                          startet ein Tool und streamt dessen
                                          Ausgabe ins Log (gut fuer Login-URLs)

Hinweise:
- Lege Startdatei + Parameter in config.json/server_settings.json fest, nicht
  hier. install() beschafft nur die Dateien in `serverfiles`.
- Fehler einfach per `raise` melden - DGSM faengt sie ab und zeigt sie an.
- Welt-/Konfigdateien beim Update nicht ueberschreiben (vorher pruefen).
"""

import os


def install(serverfiles, ctx):
    os.makedirs(serverfiles, exist_ok=True)

    # ----------------------------------------------------------------------
    # Platzhalter-Guard: entfernen, sobald du das Plugin angepasst hast.
    raise RuntimeError(
        "ExampleCustomGame: Vorlage - bitte install.py anpassen und diesen "
        "Guard entfernen."
    )
    # ----------------------------------------------------------------------

    # --- Beispiel A: Archiv (zip) laden und entpacken ---------------------
    # url = "https://example.com/myserver-win.zip"
    # zip_path = os.path.join(serverfiles, "download.zip")
    # ctx.download(url, zip_path)
    # ctx.extract_zip_file(zip_path, serverfiles)
    # os.remove(zip_path)

    # --- Beispiel B: einzelne Datei (.exe / .jar) laden -------------------
    # ctx.download("https://example.com/server.jar",
    #              os.path.join(serverfiles, "server.jar"))

    # --- Beispiel C: Java-Server (JRE mitliefern) -------------------------
    # ctx.ensure_jre(serverfiles, "21")   # -> executable = jre/bin/java.exe
    # ctx.write_eula(serverfiles)

    # --- Beispiel D: Tool ausfuehren und Ausgabe ins Log streamen ---------
    # rc, lines = ctx.run_logged(["mytool.exe", "--install"], serverfiles, 600, "[MYGAME]")

    # return "MyGame installiert/aktualisiert"
