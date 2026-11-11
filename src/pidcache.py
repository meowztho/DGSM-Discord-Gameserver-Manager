import json
import math
import os
import tempfile
from typing import Dict
from config_store import PID_CACHE
from psutil import Process, Error


def process_identity(proc: Process) -> dict:
    """Capture the process instance, not just an operating-system PID slot."""
    if not proc.is_running():
        raise ValueError("Process is no longer running")
    created_at = proc.create_time()
    executable = proc.exe()
    if not math.isfinite(created_at) or created_at <= 0 or not executable:
        raise ValueError("Incomplete process identity")
    return {"pid": proc.pid, "created_at": created_at, "executable": executable}


def matches_process_identity(proc: Process, record: dict) -> bool:
    try:
        if type(record.get("pid")) is not int or record["pid"] <= 0:
            return False
        created_at = record.get("created_at")
        if type(created_at) not in (int, float) or not math.isfinite(created_at):
            return False
        executable = record.get("executable")
        if not isinstance(executable, str) or not executable:
            return False
        current = process_identity(proc)
        return (
            current["pid"] == record["pid"]
            and current["created_at"] == created_at
            and os.path.normcase(os.path.realpath(current["executable"]))
            == os.path.normcase(os.path.realpath(executable))
        )
    except (Error, OSError, TypeError, ValueError):
        return False


def save_pids(processes: Dict[str, Process]):
    data = {}
    for name, proc in list(processes.items()):
        try:
            data[name] = process_identity(proc)
        except (Error, OSError, TypeError, ValueError):
            continue
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=os.path.dirname(PID_CACHE),
            prefix=".server_pids-", suffix=".tmp", delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(data, handle)
        os.replace(temporary, PID_CACHE)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def load_pids() -> Dict[str, object]:
    try:
        with open(PID_CACHE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
