---
tags:
  - dgsm
  - operations
  - setup
---

# Operations

## Vault Nutzung

- Oeffne den Repository-Root in Obsidian als Vault.
- Beginne mit [[00-Obsidian-Start]].
- Die Projekt-Notizen liegen gesammelt im Ordner `obsidian/`.

## Relevante ENV-Werte

Aus [../src/context.py](../src/context.py) und [../src/steam_integration.py](../src/steam_integration.py):

- `DISCORD_TOKEN`
- `DISCORD_CHANNEL`
- `ADMIN_CHANNEL`
- `DOMAIN`
- `STEAMCMD_PATH`
- `STEAMCMD_DOWNLOAD_URL`
- `STEAMCMD_TIMEOUT`

## Effektive Runtime-Basis

Die Runtime-Basis ist nicht der Repository-Root, sondern typischerweise `src/`.

Das bedeutet:

- aktive `.env` liegt in `src/.env`
- aktive Konfiguration liegt in `src/server_config.json`
- SQLite liegt in `src/server_logs.db`
- PID-Cache liegt in `src/server_pids.json`

Das ist wichtig, weil im Root ebenfalls Dateien liegen koennen, die nicht die aktive Laufzeitquelle sind.

## Wichtige Ordner

- `src/plugin_templates/` fuer Server-Templates
- `src/steam/` fuer SteamCMD, Backups und installierte Serverfiles
- `src/steam/GSM/servers/` fuer die eigentlichen Serverinstanzen
- `src/steam_sessions/` fuer gespeicherte Steam-Login-Dateien
- `docs/` fuer Screenshots und vorhandene Projektdoku

## Backup und Restore

- Standard-Backups liegen in `src/steam/backup`
- ein Legacy-Fallback `src/backups` wird im Code noch beruecksichtigt
- ZIP-Archive werden vor Extraktion auf sichere Zielpfade geprueft

## Release- und EXE-Betrieb

Laut [../README.md](../README.md) kann `dist/DGSM/DGSM.exe` genutzt werden, aber die Runtime-Daten bleiben in `src/`.

Praktisch heisst das:

- `dist/` ist Build-Ausgabe
- `src/` bleibt die relevante Daten- und Konfigurationsbasis
- `build/` und `dist/` sind fuer Code-Lesen meist nur Beiwerk

## Empfohlene Obsidian-Views

- Graph View fuer Beziehungen zwischen `Architecture`, `Runtime-Flow` und `Module-Map`
- Backlinks, um von Roadmap-Punkten zu Architektur- oder Modulnotizen zu springen
- Search fuer Dateinamen wie `server_manager`, `steam_integration`, `desktop_ui`

## Naechster Leseschritt

Weiter mit [[Roadmap]] fuer konkrete technische Ansatzpunkte.
