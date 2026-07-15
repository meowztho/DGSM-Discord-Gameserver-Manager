# REST API Bridge

DGSM can poll useful game-server REST data and expose explicitly approved API
commands. Discord remains the primary control layer: Discord `/cli`, Desktop UI,
and Web UI all call the same `execute_cli_command()` dispatcher.

## Resource Metrics

The Desktop and Web metric rows show resources attributable to DGSM and managed
game-server processes instead of general host totals:

- DGSM CPU and resident memory
- Combined game-server CPU and resident memory
- Game-server process disk read/write rate
- Active/max players from enabled, available REST APIs
- Running/configured server count
- DGSM uptime

Running server cards also show their own CPU, RAM, and disk I/O. Process data is
collected in one scan and briefly cached so Desktop and Web refreshes do not cause
continuous process enumeration.

## Read Configuration

`rest_api.enabled` must be `true`. GET endpoints remain configured under
`rest_api.endpoints`, while `poll` selects the endpoints used for the cached UI
snapshot. Missing or disabled configuration remains invisible and does not affect
server operation.

## Command Allowlist

Actions require both `rest_api.enabled` and `rest_api.actions.enabled`. Every
command maps a short CLI name to one relative POST path and a bounded argument
schema. Example:

```json
{
  "actions": {
    "enabled": true,
    "commands": {
      "announce": {
        "method": "POST",
        "path": "/v1/api/announce",
        "arguments": [
          {
            "name": "message",
            "required": true,
            "consume_rest": true,
            "max_length": 500
          }
        ]
      },
      "save": {
        "method": "POST",
        "path": "/v1/api/save",
        "arguments": []
      }
    }
  }
}
```

Usage from every CLI surface:

```text
api Palworld list
api Palworld announce "Restart in 10 minutes"
api Palworld save
```

Supported argument types are `string` (default), `integer`, and `boolean`.
Optional `choices`, `minimum`, `maximum`, `max_length`, and `consume_rest` fields
further restrict input.

## Safety Boundary

- No arbitrary URL, method, header, or JSON body can be entered through the CLI.
- Only configured relative POST paths on the server's existing `base_url` are used.
- Basic Auth reuses the configured password or a named server parameter.
- Command names and paths related to start, stop, shutdown, restart, update,
  install, remove, or delete are rejected even if present in JSON.
- DGSM start/stop/restart/update commands continue through `server_manager.py` and
  `steam_integration.py`, preserving Discord state, PID tracking, and logging.
- API command results are logged once by the shared CLI dispatcher.

Game REST APIs should remain bound to localhost or a trusted LAN and should not be
published directly to the internet.
