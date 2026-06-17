---
tags:
  - dgsm
  - runtime
  - flow
---

# Runtime Flow

## Standardstart mit Discord

Der normale Bootpfad in [../src/Main.py](../src/Main.py) sieht so aus:

1. SteamCMD-Pruefung oder Auto-Download via [../src/steam_integration.py](../src/steam_integration.py)
2. Initialisierung von SQLite und Log-Retention
3. Laden von Serverpfaden und `server_settings.json` je Server
4. Rekonstruktion bereits laufender Prozesse
5. Auto-Start: Server mit `auto_start: true` werden gestartet (`auto_start_servers()`, idempotent pro Prozess, Log `[AUTO-START]`)
6. Start des Monitor-Loops fuer Serverzustand
7. Aufbau oder Refresh des Discord-Statuspanels
8. Start der optionalen Desktop-UI
9. Start des taeglichen Refresh-Tasks um `06:00`

Schritte 1, 4 und 5 sind einzeln gekapselt: ein Fehler (z. B. SteamCMD auf frischem Host) verhindert den restlichen Bot-Start nicht.

## UI-only Modus

Wenn `DISCORD_TOKEN` oder `DISCORD_CHANNEL` fehlen, startet DGSM nicht als Bot, sondern lokal:

- SteamCMD und Runtime-Dateien werden trotzdem vorbereitet
- Recovery und Auto-Start (`auto_start: true`) laufen auch hier
- die Desktop-UI wird direkt gestartet
- es laeuft kein Discord-Bot
- der Prozess bleibt aktiv, solange die UI offen ist

Das ist in [../src/Main.py](../src/Main.py) explizit als `_run_ui_only()` umgesetzt.

## Serverstart

Der typische Startpfad ist:

1. Command oder Button loest Aktion aus
2. [../src/server_manager.py](../src/server_manager.py) laedt frische Pfade und Settings
3. ein Per-Server-Lock verhindert Doppelstarts
4. optionales Auto-Update wird vorher ausgefuehrt
5. Executable wird heuristisch oder konfiguriert aufgeloest
6. Prozess wird gestartet und im PID-Cache gemerkt
7. Runtime-Status und UI werden aktualisiert

## Serverstop

Stop-Logik nutzt `psutil`, gespeicherte PIDs und baumartiges Terminieren:

- zuerst bekannte Prozesse identifizieren
- unter Windows bei Bedarf `taskkill`
- Prozessbaum kontrolliert beenden
- Status, Logs und Cache aktualisieren

## Update- und Installationspfad

- `run_update` in [../src/steam_integration.py](../src/steam_integration.py) verzweigt anhand der `app_id`:
  numerisch -> SteamCMD; buchstabenbasiert (`minecraft_vanilla|_fabric|_bedrock`) -> Custom-Installer in [../src/custom_install.py](../src/custom_install.py)
- SteamCMD wird aus `STEAMCMD_PATH`, `src/steam/` oder dem Systempfad gesucht
- fehlt SteamCMD, wird es standardmaessig nach `src/steam/` geladen
- Sessions koennen pro Server in `src/steam_sessions/` gesichert werden
- nach erfolgreicher Installation werden Executables heuristisch erkannt

### Custom-Install (Nicht-Steam)

- Provider per (buchstabenbasierter) `app_id`: `minecraft_vanilla|_fabric|_bedrock`, `custom_url`
- Universelles Plugin-Modell: liegt ein `install.py` im Server-/Template-Ordner, fuehrt DGSM dessen `install(serverfiles, ctx)` aus (Vorrang vor Providern). `ctx` bietet stabile Helfer (download, ensure_jre, extract, run_logged, ...). Neue Nicht-Steam-Spiele ohne Core-Eingriff.
- `Hytale` ist so ein Plugin (Logik aus WindowsGSM.Hytale portiert): JRE 25 + offizielles Downloader-Tool automatisch; DGSM startet den Downloader und streamt die Geraete-Login-URL ins Log, danach headless. Start `java -jar Server/HytaleServer.jar --assets Assets.zip --bind <IP>:5520`
- Minecraft Vanilla/Fabric/Bedrock ohne SteamCMD; benoetigte Java-Version aus dem Mojang-Manifest, Temurin-JRE nach `serverfiles/jre`
- `custom_url`: generischer Direktdownload (zip/tar.gz/.exe/.jar) fuer beliebige Server (z. B. Hytale). Parameter in `dgsm_install.json` (von /addserver unveraendert in die Instanz kopiert), optional JRE-Bereitstellung
- Downloads nutzen ein gebuendeltes `certifi`-CA-Bundle (sonst `CERTIFICATE_VERIFY_FAILED` im PyInstaller-Build) und Retries gegen CDN-Aussetzer

## Backup- und Restore-Pfad

Backup/Restore wird sowohl in Discord als auch in der Desktop-UI angeboten:

- ZIP-Backups landen standardmaessig in `src/steam/backup`
- Archiv-Inhalte werden vor dem Restore auf sichere Zielpfade geprueft
- Loeschvorgaenge enthalten Retry- und Permission-Handling

## Laufende Beobachtung

- `monitor_servers()` prueft den Zustand aktiver Server; bei Absturz Neustart, wenn `auto_restart` aktiv
- Auto-Restart-Backoff: >= 5 Neustarts in < 300s -> `auto_restart` wird pausiert (`auto_restart_suspended`), Log/Action-Log-Eintrag; ein manueller Start reaktiviert. Verhindert Endlos-Neustart bei defekter/fehlender Startdatei.
- `runtime_status.py` meldet laufende Operationen wie `start`, `stop`, `update`, `backup` oder `restore`
- Discord-Panel und Desktop-UI koennen dadurch denselben Aktivitaetszustand anzeigen

## Naechster Leseschritt

Weiter mit [[Module-Map]] fuer die Zuordnung der Dateien.
