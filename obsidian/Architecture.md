---
tags:
  - dgsm
  - architecture
---

# Architecture

## Zielbild

DGSM trennt Steuerung, Laufzeit und Infrastruktur recht klar:

- Steuerung ueber Discord Slash-Commands und Buttons
- Optional zusaetzliche lokale Desktop-UI
- Gemeinsame Service-Logik fuer Serverstart, Stop, Update und Backup
- Persistenz ueber JSON-Dateien und SQLite
- Installations- und Update-Pfad ueber SteamCMD

## Schichten

### 1. Boot und Runtime

- [../src/Main.py](../src/Main.py) ist der Einstiegspunkt.
- [../src/context.py](../src/context.py) laedt `.env`, initialisiert Logging und den Discord-Bot.
- [../src/runtime_status.py](../src/runtime_status.py) haelt laufende Operations-States fuer UI und Discord synchron.

### 2. Control Surfaces

- [../src/commands.py](../src/commands.py) definiert Slash-Commands wie Add, Update, Backup und Restore.
- [../src/ui.py](../src/ui.py) rendert das Discord-Statuspanel und bindet Button-Aktionen an dieselbe Serverlogik.
- [../src/desktop_ui.py](../src/desktop_ui.py) ist eine zweite Bedienoberflaeche fuer lokale Administration.

### 3. Server-Orchestrierung

- [../src/server_manager.py](../src/server_manager.py) startet, stoppt, ueberwacht und rekonstruiert laufende Serverprozesse.
- Per-Server-Locks verhindern doppelte Start- oder Stop-Vorgaenge.
- Prozesse werden ueber `psutil` verfolgt und in `server_pids.json` gespiegelt.

### 4. Installation und Updates

- [../src/steam_integration.py](../src/steam_integration.py) loest SteamCMD auf, laedt es bei Bedarf herunter und fuehrt Updates aus.
- [../src/template_utils.py](../src/template_utils.py) normalisiert Template-JSON und hilft bei plattformabhaengigen Executable-Eintraegen.
- [../src/paths.py](../src/paths.py) baut die effektiven Serverpfade aus `app_id`, `instance_id` oder `install_dir`.

### 5. Persistenz und Sicherheit

- [../src/config_store.py](../src/config_store.py) liest und schreibt `src/server_config.json` atomar.
- [../src/security.py](../src/security.py) kuemmert sich um ENV-Werte und verschluesselte Passwoerter.
- [../src/db.py](../src/db.py) schreibt Aktionslogs nach SQLite.

## Architektur-Muster

- Gemeinsame Backend-Logik wird von Discord, CLI und Desktop-UI wiederverwendet.
- Laufzeitdaten liegen unter `src/`, auch beim EXE-Betrieb.
- Templates sind dateibasiert und koennen ohne Datenbankmigration erweitert werden.
- Die Plattformlogik ist weitgehend auf Windows und Linux abstrahiert.

## Wichtige Spannungen im Design

- Die Desktop-UI enthaelt bereits viel Fachlogik und nicht nur Darstellung.
- `commands.py` und `desktop_ui.py` haben bei Backup/Restore und Template-Aktionen ueberschneidende Workflows.
- Das Projekt ist pragmatisch dateibasiert aufgebaut, nicht service- oder package-zentriert.

## Naechster Leseschritt

Weiter mit [[Runtime-Flow]] fuer den konkreten Start- und Operationsablauf.
