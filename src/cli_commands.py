from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from typing import List, Optional, Tuple

from db import write_action_log
from paths import SERVER_PATHS, load_server_configs, load_server_paths
from runtime_status import get_operation_status
from server_manager import server_processes, start_server, stop_server
from steam_integration import run_update


HELP_TEXT = (
    "DGSM CLI commands:\n"
    "  help\n"
    "  list\n"
    "  status [server]\n"
    "  start <server>\n"
    "  stop <server>\n"
    "  restart <server>\n"
    "  update <server>\n"
    "  refresh\n\n"
    "Hinweis: Discord bleibt die Hauptsteuerung, CLI ist optional."
)


@dataclass(slots=True)
class CliExecutionResult:
    ok: bool
    message: str
    refresh: bool = False


def _short(value: str, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _split_line(command_line: str) -> List[str]:
    raw = str(command_line or "").strip()
    if not raw:
        return []
    try:
        # Windows and Linux use slightly different quote parsing; this keeps both workable.
        return shlex.split(raw, posix=(os.name != "nt"))
    except ValueError:
        try:
            return shlex.split(raw, posix=False)
        except ValueError:
            return [raw]


def _load_names() -> List[str]:
    load_server_paths()
    load_server_configs()
    return sorted(SERVER_PATHS.keys(), key=str.lower)


def _resolve_server(user_value: str) -> Tuple[Optional[str], Optional[str]]:
    asked = str(user_value or "").strip()
    if not asked:
        return None, "Server name is required."

    names = _load_names()
    if not names:
        return None, "No servers configured."

    if asked in SERVER_PATHS:
        return asked, None

    lower_map = {name.lower(): name for name in names}
    low = asked.lower()
    if low in lower_map:
        return lower_map[low], None

    starts = [name for name in names if name.lower().startswith(low)]
    if len(starts) == 1:
        return starts[0], None
    if len(starts) > 1:
        return None, f"Server name is ambiguous: {', '.join(starts[:6])}"
    return None, f"Server not found: {asked}"


def _is_running(name: str) -> bool:
    try:
        proc = server_processes.get(name)
        return bool(proc and proc.is_running())
    except Exception:
        return False


def _state_line(name: str) -> str:
    state, detail = get_operation_status(name)
    if state == "busy":
        return f"{name}: busy ({_short(detail or 'operation in progress', 120)})"
    if state == "failed":
        return f"{name}: failed ({_short(detail or 'last operation failed', 120)})"
    if _is_running(name):
        return f"{name}: running (process active)"
    return f"{name}: stopped (process inactive)"


def _status_overview() -> str:
    names = _load_names()
    if not names:
        return "No servers configured."
    return "\n".join(_state_line(name) for name in names)


def _log(source: str, command_line: str, result: CliExecutionResult, server: str = "-") -> None:
    try:
        write_action_log(
            "cli",
            server or "-",
            "success" if result.ok else "failed",
            f"source={source} cmd={_short(command_line, 180)} msg={_short(result.message, 180)}",
        )
    except Exception:
        pass


async def execute_cli_command(command_line: str, source: str = "cli") -> CliExecutionResult:
    tokens = _split_line(command_line)
    if not tokens:
        result = CliExecutionResult(False, "No command provided. Use 'help'.")
        _log(source, command_line, result)
        return result

    cmd = str(tokens[0]).lower()
    args = tokens[1:]
    server = "-"

    if cmd in {"help", "?"}:
        result = CliExecutionResult(True, HELP_TEXT)
        _log(source, command_line, result)
        return result

    if cmd in {"list", "ls"}:
        result = CliExecutionResult(True, _status_overview())
        _log(source, command_line, result)
        return result

    if cmd in {"status", "stat"}:
        if not args:
            result = CliExecutionResult(True, _status_overview())
            _log(source, command_line, result)
            return result

        resolved, err = _resolve_server(args[0])
        server = str(args[0])
        if err:
            result = CliExecutionResult(False, err)
            _log(source, command_line, result, server=server)
            return result
        server = str(resolved)
        result = CliExecutionResult(True, _state_line(server))
        _log(source, command_line, result, server=server)
        return result

    if cmd == "refresh":
        result = CliExecutionResult(True, "Refresh queued", refresh=True)
        _log(source, command_line, result)
        return result

    if cmd in {"start", "stop", "restart", "update"}:
        if len(args) != 1:
            result = CliExecutionResult(False, f"Usage: {cmd} <server>")
            _log(source, command_line, result)
            return result

        resolved, err = _resolve_server(args[0])
        server = str(args[0])
        if err:
            result = CliExecutionResult(False, err)
            _log(source, command_line, result, server=server)
            return result
        server = str(resolved)

        if cmd == "start":
            ok = await start_server(server)
            result = CliExecutionResult(ok, f"{server}: {'started' if ok else 'start failed'}", refresh=True)
            _log(source, command_line, result, server=server)
            return result

        if cmd == "stop":
            ok = await stop_server(server)
            result = CliExecutionResult(ok, f"{server}: {'stopped' if ok else 'stop failed'}", refresh=True)
            _log(source, command_line, result, server=server)
            return result

        if cmd == "restart":
            if _is_running(server):
                if not await stop_server(server):
                    result = CliExecutionResult(False, f"{server}: restart failed while stopping", refresh=True)
                    _log(source, command_line, result, server=server)
                    return result
            ok = await start_server(server)
            result = CliExecutionResult(ok, f"{server}: {'restarted' if ok else 'restart failed'}", refresh=True)
            _log(source, command_line, result, server=server)
            return result

        ok, msg = await run_update(server)
        result = CliExecutionResult(
            ok,
            f"{server}: {_short(msg or ('update finished' if ok else 'update failed'), 260)}",
            refresh=True,
        )
        _log(source, command_line, result, server=server)
        return result

    result = CliExecutionResult(False, f"Unknown command: {cmd}. Use 'help'.")
    _log(source, command_line, result, server=server)
    return result
