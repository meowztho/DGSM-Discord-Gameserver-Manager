import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional, Tuple


FAILED_TTL_SECONDS = 900

_STATUS: Dict[str, dict] = {}
_OPERATION_OWNERS: Dict[str, asyncio.Task] = {}
_OPERATION_ROOTS: Dict[str, set[Path]] = {}


class ServerOperationBusy(RuntimeError):
    """Another task owns an operation that can change this server."""


@asynccontextmanager
async def server_operation(server_name: str):
    """Admit one task per server/filesystem scope; allow same-task nesting.

    Task identity is deliberate: spawned child tasks must not inherit permission
    to mutate a server while their parent is still operating on it.
    """
    # Lazy dependency keeps status queries independent of configuration startup.
    from paths import load_config, server_operation_roots

    task = asyncio.current_task()
    owner = _OPERATION_OWNERS.get(server_name)
    if owner is not None and owner is not task:
        raise ServerOperationBusy(f"Für {server_name} läuft bereits eine Aktion.")
    cfg = load_config()
    roots = server_operation_roots(server_name, cfg)
    roots.update(_OPERATION_ROOTS.get(server_name, set()))
    # No await between resource discovery, collision checks and admission. This
    # also discovers entries registered by an active add operation after its
    # initial name admission, before it yielded to its filesystem worker.
    for active_name, active_task in _OPERATION_OWNERS.items():
        active_roots = _OPERATION_ROOTS.setdefault(active_name, set())
        active_roots.update(server_operation_roots(active_name, cfg))
        if active_task is not task and any(
            first == second or first in second.parents or second in first.parents
            for first in roots for second in active_roots
        ):
            raise ServerOperationBusy(
                f"Für {active_name} läuft bereits eine Aktion im gleichen Serverordner."
            )
    nested = owner is task
    _OPERATION_ROOTS.setdefault(server_name, set()).update(roots)
    if not nested:
        _OPERATION_OWNERS[server_name] = task
    try:
        yield
    finally:
        if not nested and _OPERATION_OWNERS.get(server_name) is task:
            _OPERATION_OWNERS.pop(server_name, None)
            _OPERATION_ROOTS.pop(server_name, None)


async def drain_task(task: asyncio.Task):
    """Wait for in-flight work before releasing its server, despite cancellation.

    Callers retain and re-raise their original cancellation after cleanup. A
    worker thread cannot be killed safely, so its owner must await its exit.
    """
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


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

