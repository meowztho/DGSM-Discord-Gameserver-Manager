---
tags:
  - dgsm
  - obsidian
  - project-map
aliases:
  - DGSM Home
---

# DGSM Obsidian Home

Dieses Repository kann direkt als Obsidian-Vault genutzt werden. Oeffne in Obsidian einfach den Projektordner `DGSM-Discord-Gameserver-Manager-main` und starte mit dieser Datei.

## Startpunkte

- [[obsidian/Architecture]]
- [[obsidian/Runtime-Flow]]
- [[obsidian/Module-Map]]
- [[obsidian/Operations]]
- [[obsidian/Roadmap]]

## Projekt in einem Satz

DGSM ist ein Discord-gesteuerter Game-Server-Manager mit optionaler Desktop-UI, SteamCMD-Integration, JSON-Konfiguration und SQLite-Logging.

## Schnelle Orientierung

- Haupteinstieg: [src/Main.py](src/Main.py)
- Discord-Kontext und ENV-Laden: [src/context.py](src/context.py)
- Slash-Commands: [src/commands.py](src/commands.py)
- Status-Panel und Buttons: [src/ui.py](src/ui.py)
- Server-Lifecycle: [src/server_manager.py](src/server_manager.py)
- SteamCMD und Updates: [src/steam_integration.py](src/steam_integration.py)
- Desktop-UI: [src/desktop_ui.py](src/desktop_ui.py)
- Runtime-Konfiguration: [src/config_store.py](src/config_store.py)
- Pfadauflosung: [src/paths.py](src/paths.py)

## Laufzeitdateien

- ENV: `src/.env`
- Konfiguration: `src/server_config.json`
- PID-Cache: `src/server_pids.json`
- Logs DB: `src/server_logs.db`
- SteamCMD und Serverfiles: `src/steam/`
- Templates: `src/plugin_templates/`

## Lesereihenfolge

1. [[obsidian/Architecture]]
2. [[obsidian/Runtime-Flow]]
3. [[obsidian/Module-Map]]
4. [[obsidian/Operations]]
5. [[obsidian/Roadmap]]

## Bestehende Projektdoku

- [README.md](README.md)
- [RELEASE_NOTES_v2.0.4.md](RELEASE_NOTES_v2.0.4.md)
- [RELEASE_NOTES_v2.0.3.md](RELEASE_NOTES_v2.0.3.md)
- [RELEASE_NOTES_v2.0.2.md](RELEASE_NOTES_v2.0.2.md)
- [RELEASE_NOTES_v2.0.1.md](RELEASE_NOTES_v2.0.1.md)

## UI Eindruck

![[docs/bot_ui.png]]
