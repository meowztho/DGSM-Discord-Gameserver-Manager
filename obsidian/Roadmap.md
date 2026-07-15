---
tags:
  - dgsm
  - roadmap
  - tech-debt
---

# Roadmap

## Beobachtete Ansatzpunkte

Diese Punkte leiten sich direkt aus der aktuellen Codebasis ab und sind als Arbeitsliste fuer kuenftige Aufraeum- oder Ausbauarbeiten gedacht.

## 1. Gemeinsame Service-Schicht staerker herausziehen

`commands.py` und `desktop_ui.py` enthalten ueberschneidende Ablaufe fuer:

- Backup
- Restore
- Template-Anlage
- Server-Anlage
- Entfernen von Servern

Ein gemeinsames Service-Modul wuerde weniger Duplikation und weniger Drift zwischen Discord und Desktop-UI bedeuten.

## 2. `desktop_ui.py` zerlegen

Die Datei vereint:

- UI-Aufbau
- Logging
- Systemmetriken
- CLI-Integration
- Backup/Restore-Workflows
- Template- und Server-Management

Sinnvolle Split-Kandidaten:

- `desktop_ui/widgets`
- `desktop_ui/actions`
- `desktop_ui/metrics`
- `desktop_ui/theme`

## 3. Pfad- und Archiv-Sicherheit gezielt testen

Es gibt bereits Schutzlogik fuer:

- sichere Zielpfade
- ZIP-Sicherheitspruefungen
- Retry bei Zugriff verweigert

Das waere ein guter Kandidat fuer automatisierte Tests, weil hier echte Betriebs- und Sicherheitsrisiken stecken.

## 4. Encoding und Textkonsistenz verbessern

Mehrere Dateien zeigen Mischungen aus:

- Deutsch und Englisch
- teils fehlerhaft dargestellten Umlauten in Logs oder Kommentaren

Eine saubere UTF-8-Konsolidierung wuerde Wartung und Nutzerfuehrung verbessern.

## 5. Runtime- und Build-Artefakte strikter trennen

Im Repository liegen neben Source-Dateien auch:

- `build/`
- `dist/`
- `__pycache__`
- Laufzeitdaten in `src/`

Langfristig waere eine klarere Trennung zwischen Source, Release-Artefakten und lokalen Runtime-Dateien hilfreich.

## 6. Mehr Modulgrenzen im Core

`server_manager.py` und `steam_integration.py` sind bereits sinnvolle Kerne, aber es koennte noch sauberer getrennt werden in:

- Prozesssteuerung
- Installationslogik
- Konfigurationsmutation
- Status- und Event-Bus

## Vorschlag fuer den naechsten technischen Schritt

Wenn du aufraeumen willst, ist der beste erste Hebel vermutlich:

1. gemeinsame Backup/Restore-Logik in ein neues Service-Modul ziehen
2. danach `desktop_ui.py` an diese Services anbinden
3. erst dann die UI-Datei in mehrere Dateien splitten
