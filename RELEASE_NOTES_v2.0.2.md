# DGSM v2.0.2 Release Notes

Release date: 2026-02-17

## Highlights

- Added Linux runtime support across server management logic while preserving Windows behavior.
- Desktop UI can now run on Linux environments with a graphical session (PySide6 required).
- Kept Discord as the primary control path while the desktop UI mirrors the same backend logic.

## Cross-Platform Server Startup

- Added runtime OS detection and OS-aware executable resolution.
- Template and server executable fields now support:
  - `executable` (shared default)
  - `executable_windows`
  - `executable_linux`
- If no executable is configured, DGSM auto-detects a matching start file and writes a hint back into config/settings.
- Improved fallback executable lookup for both Windows (`.exe`) and Linux start files.

## Template and Config Normalization

- Normalized template JSON schema across `src/plugin_templates`.
- `/createtemplate` and desktop template creation now accept templates without `executable`.
- `/addserver` install flow now persists detected executable hints automatically.
- Added settings/autocomplete support for `executable_windows` and `executable_linux`.

## SteamCMD Improvements

- Added OS-specific default SteamCMD download URLs:
  - Windows: `steamcmd.zip`
  - Linux: `steamcmd_linux.tar.gz`
- Added Linux tar.gz extraction and executable permission handling for SteamCMD files.
- Improved SteamCMD executable discovery for `steamcmd.exe`, `steamcmd`, and `steamcmd.sh`.
- Added clearer Linux dependency hinting when SteamCMD cannot execute due to missing runtime requirements.

## Runtime and Startup Fixes

- Fixed Linux/WSL direct script startup by normalizing `Main.py` (shebang, LF line endings, no BOM).
- Preserved Windows-specific console hide behavior while enabling Linux desktop UI startup when a display is available.
- Expanded diagnostics with runtime platform visibility.

## Compatibility Notes

- Existing templates remain usable; OS-specific executable fields are optional.
- Headless Linux servers should use Discord-only mode because the desktop UI requires a graphical session.
