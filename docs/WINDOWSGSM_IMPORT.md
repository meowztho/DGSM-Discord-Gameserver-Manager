# WindowsGSM Plugin Import

DGSM can convert declarative data from WindowsGSM C# plugins into its existing
`plugin_templates` format. The importer is intentionally static: it reads source
text but never compiles, loads, copies, or executes foreign C# code.

## Import Sources

- Desktop UI: local `.cs`, `.zip`, plugin folder, or public HTTPS URL
- Web UI: public HTTPS URL
- Discord: admin-only `/importwgsm source_url:<url> [name:<name>]`

GitHub repository and `blob` links are supported. Remote downloads are limited to
25 MB. Individual C# sources are limited to 2 MB, and archives/folders have count
and total-size limits.

## Compatibility Levels

### `steam_ready`

Plugins inheriting from `SteamCMDAgent` with a numeric `AppId` and `StartPath`
are converted into normal DGSM templates. DGSM imports documented defaults and
safe start arguments. Steam update suffixes are restricted to `-beta` and
`-betapassword` value pairs.

The resulting server continues through DGSM's existing SteamCMD, permissions,
logging, Discord, process start, and process stop paths.

### `review_required`

Plugins using custom `Install()`, `Update()`, or non-Steam delivery cannot be
translated safely from arbitrary C#. DGSM still creates an inspection template,
but its generated `install.py` raises a clear error. A developer must replace it
with a reviewed native DGSM adapter before installation is possible.

## Generated Files

- `config.json`: normalized install/update metadata
- `server_settings.json`: normalized runtime defaults
- `windowsgsm_import.json`: source reference, SHA-256 hash, extracted fields,
  compatibility status, and warnings
- `install.py`: only for `review_required`; blocks installation until reviewed

The original C# source is deliberately not copied into the generated template.

## Review Checklist

1. Read `windowsgsm_import.json` and resolve every warning that affects startup.
2. Verify executable path, ports, map, player limit, and generated parameters.
3. Keep credentials and secrets out of templates and source URLs.
4. For a custom installer, implement downloads and extraction with DGSM's native
   `install.py` helpers and retain the original source reference for auditing.
5. Add the server through DGSM only after the generated template is reviewed.

WindowsGSM-specific query handlers, configuration writers, console embedding, and
custom `Stop()` behavior are reported but not imported. DGSM remains authoritative
for those responsibilities.
