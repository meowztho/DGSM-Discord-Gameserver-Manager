# DGSM v2.0.1 Release Notes

Release date: 2026-02-13

## Highlights

- Introduced the new Windows desktop dashboard based on PySide6 (Qt) with a modern dark Soft-UI style.
- Added robust UI-only startup mode when Discord environment values are not configured yet.
- Improved Discord/Desktop state sync so UI actions and Discord command actions stay aligned.
- Added remaining management tools and command coverage in the desktop layer.

## Reliability and Behavior Improvements

- Improved backup and restore flow handling and status transitions.
- Hardened long-running desktop UI behavior and resize handling.
- Improved runtime feedback and operation states for start/stop/update/backup/restore.
- Added safer SteamCMD handling with automatic download support when missing.

## Metrics and Monitoring

- Added expanded system metrics cards.
- Network metrics now include process-scoped values.
- CPU and RAM metrics now include process-scoped values.
- Process metrics are scoped to active server processes to avoid misleading values when no server is running.

## Branding and UX

- Updated desktop branding and icon handling.
- Improved logo loading behavior for title bar and taskbar compatibility.
- Added build-time executable icon support from project logo assets.

## Build and Distribution

- Added `build.bat` for reproducible Windows builds.
- Standardized PyInstaller build output to `dist/DGSM/`.
- Releases can now include both source code and prebuilt `dist` artifacts.

## Runtime Path Policy (Important)

In this release, runtime data stays in `src/` and is intentionally not bundled into the executable package.

Required runtime paths:

- `src/.env`
- `src/server_config.json`
- `src/server_pids.json`
- `src/plugin_templates/`
- `src/steam/`
- `src/steam/steamcmd.exe` (auto-downloaded if missing)
- `src/steam/GSM/servers/`

## Upgrade Notes

- Keep the repository `src/` folder next to `dist/` when using `dist/DGSM/DGSM.exe`.
- If Windows still shows an old executable icon, clear icon cache or rename the exe once.

