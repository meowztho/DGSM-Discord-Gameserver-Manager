import json
from typing import Dict
from config_store import PID_CACHE
from psutil import Process


def save_pids(processes: Dict[str, Process]):
    data = {name: proc.pid for name, proc in processes.items() if proc.is_running()}
    with open(PID_CACHE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def load_pids() -> Dict[str, int]:
    try:
        with open(PID_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}
