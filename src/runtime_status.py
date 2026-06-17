import time
from typing import Dict, Optional, Tuple


FAILED_TTL_SECONDS = 900

_STATUS: Dict[str, dict] = {}


def begin_operation(server_name: str, label: str = "") -> None:
    state = _STATUS.setdefault(
        server_name,
        {"busy_count": 0, "label": "", "failed_at": 0.0, "failed_msg": ""},
    )
    state["busy_count"] += 1
    if label:
        state["label"] = label


def end_operation_success(server_name: str) -> None:
    state = _STATUS.get(server_name)
    if not state:
        return
    state["busy_count"] = max(0, int(state.get("busy_count", 0)) - 1)
    if state["busy_count"] == 0:
        state["failed_at"] = 0.0
        state["failed_msg"] = ""
        state["label"] = ""


def end_operation_failed(server_name: str, message: str = "") -> None:
    state = _STATUS.setdefault(
        server_name,
        {"busy_count": 0, "label": "", "failed_at": 0.0, "failed_msg": ""},
    )
    state["busy_count"] = max(0, int(state.get("busy_count", 0)) - 1)
    state["failed_at"] = time.time()
    state["failed_msg"] = str(message or "").strip()
    if state["busy_count"] == 0:
        state["label"] = ""


def get_operation_status(server_name: str) -> Tuple[Optional[str], Optional[str]]:
    state = _STATUS.get(server_name)
    if not state:
        return None, None

    if int(state.get("busy_count", 0)) > 0:
        return "busy", state.get("label") or None

    failed_at = float(state.get("failed_at", 0.0) or 0.0)
    if failed_at > 0:
        age = time.time() - failed_at
        if age <= FAILED_TTL_SECONDS:
            return "failed", state.get("failed_msg") or None
        state["failed_at"] = 0.0
        state["failed_msg"] = ""

    return None, None


def clear_server_status(server_name: str) -> None:
    _STATUS.pop(server_name, None)

