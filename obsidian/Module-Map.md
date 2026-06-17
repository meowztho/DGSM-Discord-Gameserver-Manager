---
tags:
  - dgsm
  - modules
  - code-map
---

# Module Map

## Einstieg und Runtime

| Datei | Rolle |
| --- | --- |
| [../src/Main.py](../src/Main.py) | Bootsequenz, Discord-vs-UI-only, taeglicher Task |
| [../src/context.py](../src/context.py) | ENV-Laden, Logging, Bot-Initialisierung, Berechtigungen |
| [../src/runtime_status.py](../src/runtime_status.py) | Laufende Operationszustande fuer UI und Bot |

## Commands und Oberflaechen

| Datei | Rolle |
| --- | --- |
| [../src/commands.py](../src/commands.py) | Slash-Commands, Backup/Restore, Templates, Serververwaltung |
| [../src/ui.py](../src/ui.py) | Discord-Statuspanel, Buttons, Refresh-Verhalten |
| [../src/cli_commands.py](../src/cli_commands.py) | Kompakter Kommando-Parser fuer `/cli` und UI-CLI |
| [../src/desktop_ui.py](../src/desktop_ui.py) | Lokale Qt-Oberflaeche, Live-Log, Metriken, Admin-Workflows |

## Server und Plattform

| Datei | Rolle |
| --- | --- |
| [../src/server_manager.py](../src/server_manager.py) | Start, Stop, Monitoring, PID-Recovery, Auto-Start beim Boot |
| [../src/steam_integration.py](../src/steam_integration.py) | SteamCMD-Aufloesung, Download, Update, Session-Dateien; Dispatch zu Custom-Install bei buchstabenbasierter `app_id` |
| [../src/custom_install.py](../src/custom_install.py) | Nicht-Steam-Installer (Minecraft, custom_url), Temurin-JRE, sowie Template-Plugin-Hook (`install.py` + ctx-Helfer-API) |
| [../src/platform_utils.py](../src/platform_utils.py) | Windows/Linux-Erkennung und Executable-Varianten |
| [../src/paths.py](../src/paths.py) | Ableitung effektiver Serverpfade aus Config-Eintraegen |

## Konfiguration und Daten

| Datei | Rolle |
| --- | --- |
| [../src/config_store.py](../src/config_store.py) | Atomisches Lesen/Schreiben von `server_config.json` |
| [../src/security.py](../src/security.py) | Schluesselverwaltung, Encrypt/Decrypt, ENV-Schutz |
| [../src/db.py](../src/db.py) | SQLite-Logging |
| [../src/pidcache.py](../src/pidcache.py) | Persistenz laufender Prozess-IDs |

## Templates und Hilfsfunktionen

| Datei | Rolle |
| --- | --- |
| [../src/template_utils.py](../src/template_utils.py) | Normalisierung von Template- und Settings-Dateien |
| [../src/fritzbox_test.py](../src/fritzbox_test.py) | Separates Hilfsskript fuer Router-/Port-Mapping-Tests |

## Auffaellige Hotspots

- `desktop_ui.py` ist sehr gross und kombiniert UI, Metriken und Fachlogik.
- `commands.py` ist der zweitgroesste Orchestrierungsblock fuer Discord-seitige Admin-Workflows.
- `server_manager.py` und `steam_integration.py` bilden den eigentlichen Laufzeitkern.

## Naechster Leseschritt

Weiter mit [[Operations]] fuer Pfade, Dateien und Setup-Details.
