# DGSM v2.0.4 Release Notes

Release date: 2026-07-10

## Highlights

- Added a generic REST API bridge with read polling and disabled-by-default command allowlists.
- Added Palworld REST API template configuration without changing Discord control flow.
- Desktop UI and Web UI can show extra REST data on selected server cards.
- Added a static WindowsGSM plugin importer for expanding the DGSM template catalog.

## WindowsGSM Plugin Compatibility

- Desktop UI, Web UI, and the admin-only Discord `/importwgsm` command use one shared importer.
- SteamCMD-based WindowsGSM plugins are normalized into regular DGSM templates, including safe Steam beta arguments.
- WindowsGSM C# is parsed as text and is never compiled, loaded, copied into the template, or executed.
- Custom/non-Steam installers are imported as blocked review templates until a native DGSM `install.py` adapter is supplied.
- Every generated template contains `windowsgsm_import.json` with source hash, detected fields, compatibility status, and warnings.
- Remote imports require HTTPS, public internet hosts, bounded downloads, and bounded archive/source sizes.
- DGSM retains its own install, update, start, stop, permission, logging, and Discord control paths.

## REST API Bridge

- New per-server `rest_api` block in `server_settings.json`.
- Supports configurable GET endpoints, Basic Auth, timeouts, short caching, and display field mapping.
- Disabled or missing REST configuration is ignored, so existing servers continue to work unchanged.
- REST actions remain disabled by default. Explicitly configured commands are available through the shared CLI dispatcher.
- API lifecycle endpoints such as stop and shutdown are always rejected so DGSM retains process ownership.
- Requests are restricted to configured relative POST paths, bounded typed arguments, the server's existing base URL, and existing authentication.

## Palworld

- Palworld template now includes disabled dummy REST settings for `info`, `metrics`, `players`, and `settings`.
- When enabled for a server, useful values such as players, max players, FPS, uptime, and version can be shown in UI/Web UI.
- Discord status output is intentionally unchanged to avoid noisy refreshes and fragile live-data posting.

## UI and Web UI

- Selected server cards can expand with REST summary data when available.
- Web UI mirrors the Desktop UI REST summary behavior.
- Disabled dummy REST settings stay hidden until explicitly enabled.
- Resource tiles now prioritize DGSM CPU/RAM, aggregated game-server CPU/RAM, server disk I/O, API player counts, running servers, and DGSM uptime.
- Running server cards show their own CPU, RAM, and disk-I/O load.
- API player totals are included only for enabled and currently available server APIs.
- Discord `/cli`, Desktop UI and Web UI share `api <server> <command>` dispatching and action logging.
- Discord lists all server start/stop buttons first and groups update buttons afterwards for a clearer control panel.
- Desktop settings fields are no longer reloaded by every status refresh, preserving cursor position and unsaved input.
- Periodic Desktop UI status refreshes reuse the loaded configuration instead of rereading all server JSON files every 1.2 seconds.
- Deferred Discord button responses are completed through their original private interaction to avoid unnecessary public fallback warnings.
- Desktop layout now uses a compact two-row header and gives the server list more vertical space on both small windows and large displays.

## Build and Robustness

- Preserved and normalized the existing GitHub templates for 7 Days to Die, Rust, and Valheim during repository integration.
- Updated `build.bat` to include the PyInstaller hidden import needed for `pkg_resources.extern`.
- Server settings readers now tolerate UTF-8 files with or without BOM.
- REST summary formatting now preserves numeric zero values.
- REST polling now uses conservative caching and error backoff to avoid repeated requests against fragile game APIs.
- Web clients disconnecting during a response no longer produce repeated exception traces.
