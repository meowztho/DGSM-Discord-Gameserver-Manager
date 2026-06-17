# DGSM v2.0.3 Release Notes

Release date: 2026-02-24

## Highlights

- Added an optional DGSM CLI layer while keeping Discord as the primary control path.
- Added Discord slash command `/cli` for command-style server operations.
- Added a compact CLI input bar directly inside the desktop UI Live Log panel.

## Optional CLI Layer

- Introduced central CLI command handling shared between Discord and desktop UI.
- Supported commands:
  - `help`
  - `list`
  - `status [server]`
  - `start <server>`
  - `stop <server>`
  - `restart <server>`
  - `update <server>`
  - `refresh`
- CLI execution is restricted to DGSM commands only (no arbitrary shell execution).
- CLI actions are written to DGSM action logs.

## Desktop UI Changes

- Moved CLI input to the Live Log area for better discoverability.
- CLI bar width now follows the Live Log panel width automatically.
- CLI input is disabled while another long-running action is active.

## Build and Runtime Compatibility

- Updated `build.bat` to prefer `py -3.12` when available.
- This avoids Python 3.14 packaging/runtime issues with Discord dependencies (`audioop` removal).
- Existing build output layout remains unchanged (`dist/DGSM/`).

## Documentation

- Expanded README with a dedicated "Optional CLI Commands" section.
- Added CLI usage examples for Discord and desktop UI.

## Compatibility Notes

- Discord remains the authoritative control interface by design.
- Desktop UI and CLI are optional and use the same backend operation logic.
