from __future__ import annotations

"""Neutral DGSM application operations shared by non-Discord surfaces.

This module owns cross-surface DGSM use-case orchestration. Discord-specific
interaction/channel choreography intentionally remains in commands.py and ui.py.
The optional hooks preserve UI feedback/refresh behavior without making the
application operations depend on a concrete UI.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import shlex
import stat
import zipfile
from datetime import datetime
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from config_store import BASE_DIR, PLUGIN_TEMPLATES_DIR, load_config, save_config
from db import write_action_log
from paths import SERVER_CONFIGS, SERVER_PATHS, load_server_configs, load_server_paths, sanitize_instance_id, server_root
from runtime_status import begin_operation, clear_server_status, end_operation_failed, end_operation_success, get_operation_status
from server_manager import server_processes, start_server, stop_server, ensure_server_executable_hint
from steam_integration import run_update
from security import encrypt_value
from template_utils import (
    build_template_config,
    read_template_config,
    template_effective_executable,
    template_settings_from_config,
    write_template_files,
)
from wgsm_import import format_import_summary, import_wgsm_plugin

_STOP_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

FeedbackHook = Callable[[str, str, str], None]
RefreshHook = Callable[[], Awaitable[None]]
ProgressHook = Callable[[str, Dict[str, object]], Awaitable[None]]
_feedback_hook: Optional[FeedbackHook] = None
_refresh_hook: Optional[RefreshHook] = None

async def _emit_progress(progress: Optional[ProgressHook], event: str, **data: object) -> None:
    if progress is None:
        return
    try:
        await progress(event, data)
    except Exception:
        logging.exception("DGSM operation progress hook failed: %s", event)

def configure_hooks(*, feedback: Optional[FeedbackHook] = None, refresh: Optional[RefreshHook] = None) -> None:
    global _feedback_hook, _refresh_hook
    _feedback_hook = feedback
    _refresh_hook = refresh

def _emit_feedback(name: str, status: str, message: str) -> None:
    hook = _feedback_hook
    if hook is None:
        return
    try:
        hook(name, status, message)
    except Exception:
        logging.exception("DGSM operation feedback hook failed")

async def _refresh() -> None:
    hook = _refresh_hook
    if hook is None:
        return
    try:
        await hook()
    except Exception:
        logging.exception("DGSM operation refresh hook failed")

def _schedule_refresh() -> None:
    if _refresh_hook is None:
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_refresh())
    except Exception:
        pass

def _is_running(name: str) -> bool:
    try:
        proc = server_processes.get(name)
        return bool(proc and proc.is_running())
    except Exception:
        return False

async def _wait_busy_or_done(name: str, task: asyncio.Task) -> None:
    for _ in range(32):
        state, _detail = get_operation_status(name)
        if state == "busy" or task.done():
            break
        await asyncio.sleep(0.05)

async def run_action(name: str, action: str) -> Tuple[bool, str]:
    ok = False
    message = ""
    try:
        if action == "start":
            task = asyncio.create_task(start_server(name))
            await _wait_busy_or_done(name, task)
            await _refresh()
            ok = await task
            message = "Server started" if ok else "Start failed"

        elif action == "stop":
            task = asyncio.create_task(stop_server(name))
            await _wait_busy_or_done(name, task)
            await _refresh()
            ok = await task
            message = "Server stopped" if ok else "Stop failed"

        elif action == "update":
            task = asyncio.create_task(run_update(name))
            await _wait_busy_or_done(name, task)
            await _refresh()
            ok, msg = await task
            message = msg or ("Update completed" if ok else "Update failed")

        elif action == "restart":
            if _is_running(name):
                stop_task = asyncio.create_task(stop_server(name))
                await _wait_busy_or_done(name, stop_task)
                await _refresh()
                if not await stop_task:
                    ok = False
                    message = "Restart failed while stopping"
                    _emit_feedback(name, "failed", message)
                    return ok, message

            start_task = asyncio.create_task(start_server(name))
            await _wait_busy_or_done(name, start_task)
            await _refresh()
            ok = await start_task
            message = "Server restarted" if ok else "Restart failed while starting"
            write_action_log("ui_restart", name, "success" if ok else "failed", message)

        else:
            message = f"Unknown action: {action}"

        _emit_feedback(name, "success" if ok else "failed", message)
        return ok, message
    except Exception as exc:
        logging.exception("Desktop UI action failed: %s %s", action, name)
        message = str(exc)
        _emit_feedback(name, "failed", message)
        _schedule_refresh()
        return False, message
    finally:
        _schedule_refresh()


async def save_settings(
    name: str,
    executable: str,
    parameters: str,
    auto_update: bool,
    auto_restart: bool,
    restart_after_stop: bool,
    stop_time: str,
    auto_start: bool = False,
) -> Tuple[bool, str]:
    def _fail(message: str) -> Tuple[bool, str]:
        _emit_feedback(name, "failed", message)
        try:
            write_action_log("ui_updatesettings", name, "failed", message)
        except Exception:
            pass
        _schedule_refresh()
        return False, message

    try:
        load_server_paths()
        if name not in SERVER_PATHS:
            return _fail(f"Unknown server: {name}")

        normalized_stop_time = str(stop_time or "").strip()
        if normalized_stop_time and not _STOP_TIME_RE.match(normalized_stop_time):
            return _fail("Invalid stop time. Use HH:MM or leave empty.")

        params_text = str(parameters or "").strip()
        try:
            param_list = shlex.split(params_text, posix=False) if params_text else []
        except ValueError as exc:
            return _fail(f"Invalid parameters: {exc}")

        path = os.path.join(SERVER_PATHS[name], "server_settings.json")
        cfg: Dict[str, object] = {}
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8-sig") as handle:
                    raw = json.load(handle)
                if isinstance(raw, dict):
                    cfg = raw
            except Exception:
                cfg = {}

        cfg["executable"] = str(executable or "").strip()
        cfg["parameters"] = param_list
        cfg["auto_start"] = bool(auto_start)
        cfg["auto_update"] = bool(auto_update)
        cfg["auto_restart"] = bool(auto_restart)
        cfg["restart_after_stop"] = bool(restart_after_stop)
        cfg["stop_time"] = normalized_stop_time

        with open(path, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, indent=4)

        load_server_configs()
        write_action_log(
            "ui_updatesettings",
            name,
            "success",
            (
                f"auto_start={auto_start} auto_update={auto_update} auto_restart={auto_restart} "
                f"restart_after_stop={restart_after_stop} stop_time='{normalized_stop_time or '-'}'"
            ),
        )
        _emit_feedback(name, "success", "Settings saved")
        _schedule_refresh()
        return True, "Settings saved"
    except Exception as exc:
        logging.exception("Desktop UI save settings failed: %s", name)
        return _fail(str(exc))


def _backup_dir() -> str:
    path = os.path.join(BASE_DIR, "steam", "backup")
    os.makedirs(path, exist_ok=True)
    return path


def _legacy_backup_dir() -> str:
    return os.path.join(BASE_DIR, "backups")


def _backup_search_dirs() -> List[str]:
    dirs = [_backup_dir()]
    legacy = _legacy_backup_dir()
    if os.path.isdir(legacy):
        dirs.append(legacy)
    return dirs


def list_backup_files() -> List[str]:
    files: List[str] = []
    seen = set()
    for d in _backup_search_dirs():
        try:
            for entry in os.listdir(d):
                key = entry.lower()
                if key in seen:
                    continue
                if key.endswith(".zip") and os.path.isfile(os.path.join(d, entry)):
                    files.append(entry)
                    seen.add(key)
        except Exception:
            continue
    files.sort(reverse=True)
    return files


def _resolve_backup_path(name: str) -> Optional[str]:
    filename = os.path.basename(str(name or "").strip())
    if not filename:
        return None
    for d in _backup_search_dirs():
        path = os.path.abspath(os.path.join(d, filename))
        root = os.path.abspath(d)
        try:
            if os.path.commonpath([root, path]) != root:
                continue
        except Exception:
            continue
        if os.path.isfile(path):
            return path
    return None


def _backup_display(path: str) -> str:
    filename = os.path.basename(path)
    full = os.path.abspath(path)
    primary = os.path.abspath(_backup_dir())
    legacy = os.path.abspath(_legacy_backup_dir())
    try:
        if os.path.commonpath([primary, full]) == primary:
            return f"/steam/backup/{filename}"
    except Exception:
        pass
    try:
        if os.path.commonpath([legacy, full]) == legacy:
            return f"/backups/{filename}"
    except Exception:
        pass
    return filename


def _zip_members_are_safe(zip_ref: zipfile.ZipFile, target_dir: str) -> bool:
    target_root = os.path.abspath(target_dir)
    for member in zip_ref.namelist():
        target_path = os.path.abspath(os.path.join(target_dir, member))
        try:
            if os.path.commonpath([target_root, target_path]) != target_root:
                return False
        except Exception:
            return False
    return True


def _delete_root_from_server_path(path: str) -> str:
    p = os.path.abspath(path)
    if os.path.basename(p).lower() == "serverfiles":
        return os.path.dirname(p)
    return p


def _rmtree_onerror(func, path, exc_info) -> None:  # type: ignore[no-untyped-def]
    err = exc_info
    if isinstance(exc_info, tuple):
        err = exc_info[1] if len(exc_info) > 1 else None
    if isinstance(err, PermissionError):
        try:
            mode = stat.S_IWRITE | stat.S_IREAD
            if os.path.isdir(path):
                mode |= stat.S_IEXEC
            os.chmod(path, mode)
            result = func(path)
            if hasattr(result, "close"):
                try:
                    result.close()
                except Exception:
                    pass
            return
        except Exception:
            pass
    if err is not None:
        raise err
    raise RuntimeError(f"Could not remove path: {path}")


def _blocking_rmtree(path: str) -> None:
    if os.name == "nt":
        try:
            shutil.rmtree(path, onexc=_rmtree_onerror)  # Python 3.12+
            return
        except TypeError:
            shutil.rmtree(path, onerror=_rmtree_onerror)
            return
    shutil.rmtree(path)


async def _rmtree_with_retry(path: str, retries: int = 5, base_delay: float = 0.35) -> None:
    last_exc: Optional[BaseException] = None
    for attempt in range(retries):
        try:
            await asyncio.to_thread(_blocking_rmtree, path)
            return
        except FileNotFoundError:
            return
        except PermissionError as exc:
            last_exc = exc
        except OSError as exc:
            if getattr(exc, "winerror", None) == 5:
                last_exc = exc
            else:
                raise
        await asyncio.sleep(base_delay * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"Could not delete '{path}'")


def _is_access_denied_error(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if getattr(exc, "winerror", None) == 5:
        return True
    msg = str(exc).lower()
    if "winerror 5" in msg or "access denied" in msg or "zugriff verweigert" in msg:
        return True
    return False


def _chmod_tree_writable(path: str) -> None:
    root = os.path.abspath(path)
    if not os.path.exists(root):
        return
    for base, dirs, files in os.walk(root, topdown=False):
        for name in files:
            p = os.path.join(base, name)
            try:
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            except Exception:
                pass
        for name in dirs:
            p = os.path.join(base, name)
            try:
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            except Exception:
                pass
    try:
        mode = stat.S_IWRITE | stat.S_IREAD
        if os.path.isdir(root):
            mode |= stat.S_IEXEC
        os.chmod(root, mode)
    except Exception:
        pass


def _extract_zip_archive_safe(archive_path: str, target_dir: str, skip_access_denied: bool = False) -> List[str]:
    skipped: List[str] = []
    root = os.path.abspath(target_dir)
    with zipfile.ZipFile(archive_path, "r") as archive:
        if not _zip_members_are_safe(archive, root):
            raise RuntimeError("Backup archive contains invalid paths")
        for info in archive.infolist():
            name = info.filename or ""
            if not name:
                continue
            dest = os.path.abspath(os.path.join(root, name))
            try:
                if os.path.commonpath([root, dest]) != root:
                    raise RuntimeError("Backup archive contains invalid paths")
            except Exception:
                raise RuntimeError("Backup archive contains invalid paths")

            if info.is_dir() or name.endswith("/"):
                try:
                    os.makedirs(dest, exist_ok=True)
                    os.chmod(dest, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
                except Exception as exc:
                    if skip_access_denied and _is_access_denied_error(exc):
                        skipped.append(name)
                        continue
                    raise
                continue

            parent = os.path.dirname(dest)
            try:
                os.makedirs(parent, exist_ok=True)
                os.chmod(parent, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            except Exception as exc:
                if skip_access_denied and _is_access_denied_error(exc):
                    skipped.append(name)
                    continue
                raise
            try:
                if os.path.isdir(dest):
                    _blocking_rmtree(dest)
                elif os.path.exists(dest):
                    try:
                        os.chmod(dest, stat.S_IWRITE | stat.S_IREAD)
                    except Exception:
                        pass
                    os.remove(dest)
            except Exception as exc:
                if skip_access_denied and _is_access_denied_error(exc):
                    skipped.append(name)
                    continue
                raise

            try:
                with archive.open(info, "r") as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
            except Exception as exc:
                if skip_access_denied and _is_access_denied_error(exc):
                    skipped.append(name)
                    continue
                raise
    return skipped


def _create_directory_backup(path: str, server_name: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(_backup_dir(), f"{sanitize_instance_id(server_name)}-{stamp}")
    return shutil.make_archive(base, "zip", root_dir=path)


def _make_instance_id(cfg: dict, app_id: str, preferred: str) -> str:
    base = sanitize_instance_id(preferred)
    used = set()
    for entry in cfg.get("server_paths", {}).values():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("app_id")) != str(app_id):
            continue
        iid = entry.get("instance_id")
        if isinstance(iid, str) and iid.strip():
            used.add(sanitize_instance_id(iid))
    if base not in used:
        return base
    n = 2
    while f"{base}-{n}" in used:
        n += 1
    return f"{base}-{n}"


def _instance_id_exists(cfg: dict, app_id: str, instance_id: str) -> bool:
    wanted = sanitize_instance_id(instance_id)
    for entry in cfg.get("server_paths", {}).values():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("app_id")) != str(app_id):
            continue
        existing = entry.get("instance_id")
        if isinstance(existing, str) and sanitize_instance_id(existing) == wanted:
            return True
    return False


def _target_server_path(server_name: str, cfg: Optional[dict] = None) -> Optional[str]:
    if server_name in SERVER_PATHS:
        return SERVER_PATHS[server_name]
    if cfg is None:
        cfg = load_config()
    entry = cfg.get("server_paths", {}).get(server_name)
    if not isinstance(entry, dict):
        return None
    app_id = str(entry.get("app_id", "")).strip()
    if not app_id:
        return None
    iid = entry.get("instance_id")
    if isinstance(iid, str) and iid.strip():
        return os.path.join(str(server_root(app_id, instance_id=iid)), "serverfiles")
    return os.path.join(str(server_root(app_id)), "serverfiles")


# Public neutral primitives used by surface-specific orchestration.
# They deliberately contain no Discord/Qt/Web presentation behavior so a surface
# can preserve its own tested sequencing while sharing the actual filesystem rules.
def backup_display_path(path: str) -> str:
    return _backup_display(path)


def resolve_backup_path(backup_file: str) -> Optional[str]:
    return _resolve_backup_path(backup_file)


def delete_root_from_server_path(path: str) -> str:
    return _delete_root_from_server_path(path)


async def rmtree_with_retry(path: str, retries: int = 5, base_delay: float = 0.35) -> None:
    await _rmtree_with_retry(path, retries=retries, base_delay=base_delay)


def is_access_denied_error(exc: BaseException) -> bool:
    return _is_access_denied_error(exc)


def chmod_tree_writable(path: str) -> None:
    _chmod_tree_writable(path)


def extract_zip_archive_safe(archive_path: str, target_dir: str, skip_access_denied: bool = False) -> List[str]:
    return _extract_zip_archive_safe(archive_path, target_dir, skip_access_denied)


def create_directory_backup(path: str, server_name: str) -> str:
    return _create_directory_backup(path, server_name)


def zip_members_are_safe(zip_ref: zipfile.ZipFile, target_dir: str) -> bool:
    return _zip_members_are_safe(zip_ref, target_dir)

def list_templates() -> List[str]:
    out: List[str] = []
    try:
        for entry in os.listdir(PLUGIN_TEMPLATES_DIR):
            full = os.path.join(PLUGIN_TEMPLATES_DIR, entry)
            if os.path.isdir(full):
                out.append(entry)
    except Exception:
        return []
    out.sort(key=str.lower)
    return out


async def create_template(
    template_name: str,
    app_id: str,
    executable: str,
    parameters: str,
    auto_update: bool,
    auto_restart: bool,
    stop_time: str,
    restart_after_stop: bool,
    username: str = "",
    password: str = "",
    auto_start: bool = False,
    *,
    surface_hooks: bool = True,
) -> Tuple[bool, str]:
    name = sanitize_instance_id(template_name)
    if not template_name.strip():
        return False, "Template name required"
    if not app_id.strip():
        return False, "Steam App ID required"

    stop = str(stop_time or "").strip()
    if stop and not _STOP_TIME_RE.match(stop):
        return False, "Invalid stop time. Use HH:MM or leave empty."

    params_text = str(parameters or "").strip()
    try:
        param_list = shlex.split(params_text) if params_text else []
    except Exception as exc:
        return False, f"Invalid parameters: {exc}"

    try:
        template_dir = os.path.join(PLUGIN_TEMPLATES_DIR, template_name.strip())
        cfg = build_template_config(
            app_id=app_id.strip(),
            executable=executable.strip(),
            parameters=param_list,
            auto_start=bool(auto_start),
            auto_update=bool(auto_update),
            auto_restart=bool(auto_restart),
            stop_time=stop,
            restart_after_stop=bool(restart_after_stop),
            username=username.strip(),
            password=password.strip(),
        )
        write_template_files(template_dir, cfg)
        write_action_log("createtemplate", name, "success", f"app_id={app_id.strip()}")
        if surface_hooks:
            _schedule_refresh()
        return True, f"Template '{template_name.strip()}' saved"
    except Exception as exc:
        write_action_log("createtemplate", name, "failed", str(exc))
        if surface_hooks:
            _schedule_refresh()
        return False, str(exc)

async def import_wgsm_plugin_action(
    source: str,
    template_name: str = "",
    *,
    allow_local: bool = True,
) -> Tuple[bool, str]:
    source_value = str(source or "").strip()
    if not source_value:
        return False, "WindowsGSM plugin source required"
    try:
        report = await asyncio.to_thread(
            import_wgsm_plugin,
            source_value,
            PLUGIN_TEMPLATES_DIR,
            template_name=str(template_name or "").strip(),
            allow_local=allow_local,
        )
        message = format_import_summary(report)
        write_action_log("ui_importwgsm", str(report.get("template_name", "-")), "success", message)
        _schedule_refresh()
        return True, message
    except Exception as exc:
        logging.exception("WindowsGSM plugin import failed")
        write_action_log("ui_importwgsm", str(template_name or "-") or "-", "failed", str(exc))
        return False, str(exc)


async def add_server(
    name: str,
    template_name: str,
    instance_id: str = "",
    start_after_install: bool = True,
    *,
    progress: Optional[ProgressHook] = None,
    surface_hooks: bool = True,
) -> Tuple[bool, str]:
    server_name = name.strip()
    template_value = template_name.strip()
    if not server_name:
        return False, "Server name required"
    if not template_value:
        return False, "Template required"

    try:
        template_dir = os.path.join(PLUGIN_TEMPLATES_DIR, template_value)
        if not os.path.exists(template_dir):
            return False, f"Template '{template_value}' does not exist"
        cfg_path = os.path.join(template_dir, "config.json")
        if not os.path.isfile(cfg_path):
            return False, f"Template '{template_value}' has no config.json"

        tcfg = read_template_config(template_dir)
        write_template_files(template_dir, tcfg)
        app_id = str(tcfg.get("app_id", "")).strip()
        executable = template_effective_executable(tcfg)
        if not app_id:
            return False, "Template missing app_id"

        cfg = load_config()
        if server_name in cfg.get("server_paths", {}):
            return False, f"Server '{server_name}' already exists"

        if instance_id.strip():
            chosen = sanitize_instance_id(instance_id)
            if _instance_id_exists(cfg, app_id, chosen):
                return False, f"Instance ID '{chosen}' already exists for app_id {app_id}"
        else:
            chosen = _make_instance_id(cfg, app_id, server_name)

        server_dir = os.path.join(str(server_root(app_id, instance_id=chosen)), "serverfiles")
        os.makedirs(server_dir, exist_ok=True)
        for item in os.listdir(template_dir):
            if item == "config.json":
                continue
            src = os.path.join(template_dir, item)
            dst = os.path.join(server_dir, item)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)

        settings = template_settings_from_config(tcfg)
        with open(os.path.join(server_dir, "server_settings.json"), "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=4)

        entry = {"app_id": app_id, "instance_id": chosen}
        if executable:
            entry["executable"] = executable
        if tcfg.get("username") and tcfg.get("password"):
            entry["username"] = str(tcfg["username"])
            entry["password"] = encrypt_value(str(tcfg["password"]))
        cfg.setdefault("server_paths", {})[server_name] = entry
        save_config(cfg)
        load_server_paths()
        load_server_configs()

        await _emit_progress(progress, "install_start", name=server_name, instance_id=chosen, app_id=app_id, server_dir=server_dir)

        async def _run_update_once() -> Tuple[bool, str]:
            task = asyncio.create_task(run_update(server_name))
            await _wait_busy_or_done(server_name, task)
            await _emit_progress(progress, "install_busy", name=server_name)
            if surface_hooks:
                await _refresh()
            return await task

        ok_update, msg_update = await _run_update_once()
        if not ok_update and ("No subscription" in msg_update or "besitzt keine Lizenz" in msg_update):
            current = load_config()
            server_entry = current.get("server_paths", {}).get(server_name, {})
            if isinstance(server_entry, dict) and "username" in server_entry:
                server_entry.pop("username", None)
                server_entry.pop("password", None)
                save_config(current)
            ok_update, msg_update = await _run_update_once()

        if not ok_update:
            disk_free = 0.0
            try:
                disk_free = shutil.disk_usage(server_dir).free / (1024 ** 3)
            except Exception:
                pass
            write_action_log("install", server_name, "failed", msg_update)
            await _emit_progress(progress, "install_failed", name=server_name, instance_id=chosen, app_id=app_id, server_dir=server_dir, disk_free=disk_free, message=msg_update)
            if surface_hooks:
                _schedule_refresh()
            return False, f"Install failed: {msg_update}"

        await asyncio.to_thread(ensure_server_executable_hint, server_name)
        if start_after_install:
            await _emit_progress(progress, "install_success_start", name=server_name, instance_id=chosen)
            ok_start = await start_server(server_name)
            if not ok_start:
                write_action_log("start_after_add", server_name, "failed")
                await _emit_progress(progress, "start_failed", name=server_name, instance_id=chosen)
                if surface_hooks:
                    _schedule_refresh()
                return False, "Installed but start failed"
            write_action_log("start_after_add", server_name, "success")

        write_action_log("addserver", server_name, "success", f"template={template_value} instance={chosen}")
        await _emit_progress(progress, "complete", name=server_name, instance_id=chosen, template=template_value, started=bool(start_after_install))
        if surface_hooks:
            _schedule_refresh()
        return True, f"Server '{server_name}' installed"
    except Exception as exc:
        write_action_log("addserver", server_name or "-", "failed", str(exc))
        await _emit_progress(progress, "critical_error", name=server_name, message=str(exc))
        if surface_hooks:
            _schedule_refresh()
        return False, str(exc)

async def create_backup(
    server_name: str,
    *,
    progress: Optional[ProgressHook] = None,
    surface_hooks: bool = True,
) -> Tuple[bool, str]:
    name = server_name.strip()
    if not name:
        return False, "Server required"
    load_server_paths()
    cfg = load_config()
    if name not in cfg.get("server_paths", {}) and name not in SERVER_PATHS:
        return False, "Server not found"
    target = _target_server_path(name, cfg)
    if not target:
        return False, "Server path not found"
    backup_root = _delete_root_from_server_path(target)
    if not os.path.exists(backup_root):
        return False, f"Backup path missing: {backup_root}"

    success = False
    fail_reason = ""
    status_finalized = False
    begin_operation(name, "backup")
    await _emit_progress(progress, "begin", name=name, backup_root=backup_root)
    if surface_hooks:
        await _refresh()
    try:
        archive = await asyncio.to_thread(_create_directory_backup, backup_root, name)
        write_action_log("createbackup", name, "success", os.path.basename(archive))
        success = True
        end_operation_success(name)
        status_finalized = True
        await _emit_progress(progress, "success", name=name, archive_path=archive, display_path=_backup_display(archive))
        if surface_hooks:
            await _refresh()
        return True, f"Backup created: {_backup_display(archive)}"
    except Exception as exc:
        fail_reason = str(exc)
        write_action_log("createbackup", name, "failed", fail_reason)
        logging.exception("create_backup failed")
        await _emit_progress(progress, "failed", name=name, message=fail_reason)
        return False, fail_reason
    finally:
        if not status_finalized:
            if success:
                end_operation_success(name)
            else:
                end_operation_failed(name, fail_reason or "Backup failed")
            if surface_hooks:
                await _refresh()
        if surface_hooks:
            _schedule_refresh()

async def restore_backup(
    name: str,
    backup_file: str,
    overwrite: bool = False,
    *,
    progress: Optional[ProgressHook] = None,
    surface_hooks: bool = True,
) -> Tuple[bool, str]:
    server_name = name.strip()
    if not server_name:
        return False, "Server required"
    cfg = load_config()
    if server_name not in cfg.get("server_paths", {}):
        return False, "Server not found"
    archive_path = _resolve_backup_path(backup_file)
    if not archive_path:
        return False, "Backup file not found"
    if _is_running(server_name):
        return False, "Server is running"

    load_server_paths()
    load_server_configs()
    target = _target_server_path(server_name, cfg)
    if not target:
        return False, "Server path not found"
    restore_root = _delete_root_from_server_path(target)
    restore_norm = os.path.normcase(os.path.abspath(restore_root))
    if overwrite:
        for other, other_path in SERVER_PATHS.items():
            if other == server_name:
                continue
            other_root = os.path.normcase(os.path.abspath(_delete_root_from_server_path(other_path)))
            if other_root == restore_norm or other_root.startswith(restore_norm + os.sep):
                return False, f"Overwrite refused: path also used by {other}"
    if os.path.exists(restore_root) and not overwrite:
        try:
            if any(os.scandir(restore_root)):
                return False, "Target directory is not empty"
        except Exception:
            pass

    success = False
    fail_reason = ""
    status_finalized = False
    begin_operation(server_name, "restore")
    await _emit_progress(progress, "begin", name=server_name, archive_path=archive_path, restore_root=restore_root)
    if surface_hooks:
        await _refresh()
    try:
        skipped_locked: List[str] = []
        if os.path.exists(restore_root) and overwrite:
            try:
                await asyncio.to_thread(_chmod_tree_writable, restore_root)
                await _rmtree_with_retry(restore_root)
            except Exception as cleanup_exc:
                if _is_access_denied_error(cleanup_exc):
                    logging.warning("Restore cleanup fallback for %s due to locked path: %s", server_name, cleanup_exc)
                else:
                    raise
        os.makedirs(restore_root, exist_ok=True)
        last_error: Optional[BaseException] = None
        for attempt in range(2):
            try:
                items = await asyncio.to_thread(_extract_zip_archive_safe, archive_path, restore_root, attempt >= 1)
                for item in items:
                    if item not in skipped_locked:
                        skipped_locked.append(item)
                if items:
                    write_action_log("restorebackup", server_name, "warning", f"skipped locked entries: {', '.join(items[:6])}")
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if _is_access_denied_error(exc):
                    await asyncio.to_thread(_chmod_tree_writable, restore_root)
                    await asyncio.sleep(0.45)
                    continue
                raise
        if last_error is not None:
            raise last_error

        load_server_paths()
        load_server_configs()
        write_action_log("restorebackup", server_name, "success", os.path.basename(archive_path))
        success = True
        end_operation_success(server_name)
        status_finalized = True
        await _emit_progress(progress, "success", name=server_name, archive_path=archive_path, display_path=_backup_display(archive_path), skipped=skipped_locked)
        if surface_hooks:
            await _refresh()
        return True, f"Backup restored: {_backup_display(archive_path)}"
    except Exception as exc:
        fail_reason = str(exc)
        if _is_access_denied_error(exc):
            try:
                await asyncio.to_thread(_chmod_tree_writable, restore_root)
            except Exception:
                pass
            fail_reason = f"{fail_reason} (access denied). Close file handles and retry with overwrite enabled."
        write_action_log("restorebackup", server_name, "failed", fail_reason)
        logging.exception("restore_backup failed")
        await _emit_progress(progress, "failed", name=server_name, message=fail_reason)
        return False, fail_reason
    finally:
        if not status_finalized:
            if success:
                end_operation_success(server_name)
            else:
                end_operation_failed(server_name, fail_reason or "Restore failed")
            if surface_hooks:
                await _refresh()
        if surface_hooks:
            _schedule_refresh()

async def remove_server(
    name: str,
    backup_before_delete: bool = True,
    *,
    progress: Optional[ProgressHook] = None,
    surface_hooks: bool = True,
) -> Tuple[bool, str]:
    server_name = name.strip()
    if not server_name:
        return False, "Server required"
    try:
        cfg = load_config()
        if server_name not in cfg.get("server_paths", {}):
            return False, "Server not found"
        load_server_paths()
        target = _target_server_path(server_name, cfg)
        if not target:
            return False, "Server path not found"

        if _is_running(server_name):
            await stop_server(server_name)
            await _emit_progress(progress, "stopped", name=server_name)

        delete_root = _delete_root_from_server_path(target)
        delete_norm = os.path.normcase(os.path.abspath(delete_root))
        shared_with: Optional[str] = None
        for other_name, other_path in SERVER_PATHS.items():
            if other_name == server_name:
                continue
            other_root = os.path.normcase(os.path.abspath(_delete_root_from_server_path(other_path)))
            if other_root == delete_norm or other_root.startswith(delete_norm + os.sep):
                shared_with = other_name
                break

        if os.path.exists(delete_root):
            if shared_with:
                await _emit_progress(progress, "shared_path", name=server_name, shared_with=shared_with, delete_root=delete_root)
            else:
                if backup_before_delete:
                    try:
                        archive = await asyncio.to_thread(_create_directory_backup, delete_root, server_name)
                    except Exception as exc:
                        logging.exception("Backup before remove failed")
                        await _emit_progress(progress, "backup_failed", name=server_name, message=str(exc))
                        return False, f"Backup failed: {exc}"
                    await _emit_progress(progress, "backup_created", name=server_name, archive_path=archive, display_path=_backup_display(archive))
                await asyncio.to_thread(_chmod_tree_writable, delete_root)
                await _rmtree_with_retry(delete_root)
                await _emit_progress(progress, "directory_deleted", name=server_name, delete_root=delete_root)

        current = load_config()
        current.get("server_paths", {}).pop(server_name, None)
        save_config(current)
        load_server_paths()
        load_server_configs()
        clear_server_status(server_name)
        write_action_log("removeserver", server_name, "success", "removed")
        await _emit_progress(progress, "complete", name=server_name)
        if surface_hooks:
            _schedule_refresh()
        return True, f"Server '{server_name}' removed"
    except Exception as exc:
        write_action_log("removeserver", server_name or "-", "failed", str(exc))
        await _emit_progress(progress, "failed", name=server_name, message=str(exc))
        if surface_hooks:
            _schedule_refresh()
        return False, str(exc)


def update_server_value(
    server: str,
    section: str,
    key: str,
    value: object,
    *,
    surface_hooks: bool = True,
) -> Tuple[bool, str]:
    cfg = load_config()
    if server not in cfg.get("server_paths", {}):
        return False, "Server not found"
    if section == "config":
        entry = cfg["server_paths"][server]
        entry[key] = encrypt_value(str(value)) if key == "password" else value
        save_config(cfg)
        load_server_paths()
        load_server_configs()
    elif section == "settings":
        load_server_paths()
        if server not in SERVER_PATHS:
            return False, "Server path not found"
        sf = os.path.join(SERVER_PATHS[server], "server_settings.json")
        settings: Dict[str, object] = {}
        if os.path.exists(sf):
            with open(sf, "r", encoding="utf-8-sig") as handle:
                raw = json.load(handle)
            if isinstance(raw, dict):
                settings = raw
        settings[key] = value
        with open(sf, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=4)
        load_server_configs()
    else:
        return False, "Invalid section"
    write_action_log("updateserver", server, "success", f"{section}.{key}")
    if surface_hooks:
        _schedule_refresh()
    return True, f"{section}.{key} updated"
