"""Shared administrative operations used by Discord, desktop and web adapters."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Awaitable, Callable, Optional

from config_store import BASE_DIR, PLUGIN_TEMPLATES_DIR, load_config, save_config
from db import write_action_log
from paths import (
    load_server_configs, load_server_paths, sanitize_instance_id, server_files_for_entry,
    server_root, server_root_for_entry as _entry_root,
)
from runtime_status import (
    ServerOperationBusy, begin_operation, drain_task, end_operation_failed,
    end_operation_success, server_operation,
)
from security import encrypt_value
from server_manager import ensure_server_executable_hint, is_server_running, start_server, stop_server
from steam_integration import run_update
from template_utils import (
    build_template_config, current_os_executable_key, parse_parameters, read_template_config,
    template_effective_executable, template_settings_from_config, write_template_files,
)


Progress = Optional[Callable[[str], Awaitable[None]]]
_STOP_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_APP_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_BOOL_SETTINGS = {"auto_start", "auto_update", "auto_restart", "restart_after_stop"}


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str
    path: str = ""
    instance_id: str = ""


async def _thread(function, *args):
    """Do not release an operation gate while its filesystem worker is alive."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await drain_task(task)
        except Exception:
            logging.exception("Administrative filesystem worker failed during cancellation")
        raise


async def _progress(callback: Progress, label: str) -> None:
    if callback:
        try:
            await callback(label)
        except Exception:
            logging.exception("Administrative progress display failed")


def _log(action: str, name: str, result: ActionResult) -> None:
    try:
        write_action_log(action, name, "success" if result.ok else "failed", result.message)
    except Exception:
        logging.exception("Administrative action log failed")


async def _perform(name: str, label: str, operation, progress: Progress) -> ActionResult:
    try:
        async with server_operation(name):
            begin_operation(name, label)
            try:
                await _progress(progress, label)
                result = await operation()
            except asyncio.CancelledError:
                end_operation_failed(name, f"{label} cancelled")
                raise
            except Exception as exc:
                logging.exception("%s failed for %s", label, name)
                result = ActionResult(False, str(exc))
            if result.ok:
                end_operation_success(name)
            else:
                end_operation_failed(name, result.message)
            _log(label, name, result)
            return result
    except ServerOperationBusy as exc:
        return ActionResult(False, str(exc))


def _reload() -> None:
    load_server_paths()
    load_server_configs()


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _overlaps(first: Path, second: Path) -> bool:
    return _inside(first, second) or _inside(second, first)


def _safe_root(path: Path) -> Path:
    root = path.resolve()
    base = Path(BASE_DIR).resolve()
    protected = [Path(PLUGIN_TEMPLATES_DIR).resolve(), base / "steam" / "backup", base / "backups"]
    for key in ("SystemRoot", "WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        if os.environ.get(key):
            protected.append(Path(os.environ[key]).resolve())
    if (root == Path(root.anchor) or _inside(base, root) or _inside(Path.home().resolve(), root)
            or any(_overlaps(root, protected_path) for protected_path in protected)):
        raise ValueError(f"Unsafe server root: {root}")
    if root.exists() and not root.is_dir():
        raise ValueError(f"Server root is not a directory: {root}")
    return root


def _target(name: str, *, allow_shared: bool = False) -> tuple[Path, Path, list[str]]:
    cfg = load_config()
    entry = cfg.get("server_paths", {}).get(name)
    if not isinstance(entry, dict):
        raise ValueError(f"Server not found: {name}")
    files = server_files_for_entry(name, entry).resolve()
    root = _safe_root(_entry_root(name, entry))
    shared = [
        other for other, info in cfg.get("server_paths", {}).items()
        if other != name and isinstance(info, dict) and _overlaps(root, _entry_root(other, info))
    ]
    if shared and not allow_shared:
        raise ValueError(f"Server folder overlaps another server: {', '.join(shared)}")
    return files, root, shared


def _template_path(name: str) -> Path:
    value = str(name or "").strip()
    if not value or value in {".", ".."} or any(ch in value for ch in '/\\:'):
        raise ValueError("Template name must be one directory name")
    base = Path(PLUGIN_TEMPLATES_DIR).resolve()
    target = (base / value).resolve()
    if target == base or not _inside(target, base):
        raise ValueError("Template path leaves the templates directory")
    return target


def list_templates() -> list[str]:
    base = Path(PLUGIN_TEMPLATES_DIR)
    if not base.is_dir():
        return []
    return sorted((item.name for item in base.iterdir() if item.is_dir()), key=str.lower)


def _backup_dirs() -> list[Path]:
    base = Path(BASE_DIR)
    return [base / "steam" / "backup", base / "backups"]


def list_backup_files() -> list[str]:
    files = {}
    for directory in _backup_dirs():
        if directory.is_dir():
            for item in directory.iterdir():
                if item.is_file() and item.suffix.lower() == ".zip":
                    files.setdefault(item.name.lower(), item.name)
    return sorted(files.values(), reverse=True)


def _resolve_backup(name: str) -> Path:
    filename = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    if not filename or not filename.lower().endswith(".zip"):
        raise ValueError("Backup ZIP file required")
    for directory in _backup_dirs():
        root = directory.resolve()
        candidate = (root / filename).resolve()
        if candidate != root and _inside(candidate, root) and candidate.is_file():
            return candidate
    raise ValueError("Backup file not found")


def backup_display(path: str) -> str:
    candidate = Path(path).resolve()
    for directory, display in zip(_backup_dirs(), ("/steam/backup", "/backups")):
        if _inside(candidate, directory.resolve()):
            return f"{display}/{candidate.name}"
    return str(candidate)


def _write_json(path: Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=4)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _settings_patch(values: dict) -> dict:
    patch = copy.deepcopy(values)
    for key in _BOOL_SETTINGS & patch.keys():
        if not isinstance(patch[key], bool):
            raise ValueError(f"{key} must be true or false")
    if "stop_time" in patch:
        value = str(patch["stop_time"] or "").strip()
        if value and not _STOP_TIME_RE.fullmatch(value):
            raise ValueError("Invalid stop time. Use HH:MM or leave empty.")
        patch["stop_time"] = value
    if "parameters" in patch:
        value = patch["parameters"]
        if isinstance(value, str):
            value = parse_parameters(value)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("Parameters must be text or a list of strings")
        patch["parameters"] = value
    for key in ("executable", "executable_windows", "executable_linux", "executable_win", "executable_unix"):
        if key in patch:
            if not isinstance(patch[key], str):
                raise ValueError(f"{key} must be text")
            patch[key] = patch[key].strip()
    if "rest_api" in patch and not isinstance(patch["rest_api"], dict):
        raise ValueError("rest_api must be an object")
    return patch


async def create_template(template_name: str, app_id: str, executable: str, parameters,
                          auto_update: bool = True, auto_restart: bool = True,
                          stop_time: str = "05:00", restart_after_stop: bool = False,
                          username: str = "", password: str = "", auto_start: bool = False,
                          *, progress: Progress = None) -> ActionResult:
    async def operation():
        directory = _template_path(template_name)
        application = str(app_id or "").strip()
        if not _APP_ID_RE.fullmatch(application):
            raise ValueError("App ID must be a Steam ID or a simple custom installer identifier")
        settings = _settings_patch(dict(executable=executable, parameters=parameters or [],
                                       auto_start=auto_start, auto_update=auto_update,
                                       auto_restart=auto_restart, stop_time=stop_time,
                                       restart_after_stop=restart_after_stop))
        cfg = build_template_config(app_id=application, username=username or "", password=password or "", **settings)
        await _thread(write_template_files, str(directory), cfg)
        return ActionResult(True, f"Template '{directory.name}' saved")
    return await _perform(f"template:{str(template_name).strip().casefold()}", "createtemplate", operation, progress)


def _choose_instance(cfg: dict, app_id: str, preferred: str, explicit: bool) -> str:
    used = {
        sanitize_instance_id(entry["instance_id"]).casefold()
        for entry in cfg.get("server_paths", {}).values()
        if isinstance(entry, dict) and str(entry.get("app_id")) == app_id and entry.get("instance_id")
    }
    base = sanitize_instance_id(preferred)
    chosen = base
    number = 2
    while chosen.casefold() in used:
        if explicit:
            raise ValueError(f"Instance ID '{base}' already exists for app_id {app_id}")
        chosen = f"{base}-{number}"
        number += 1
    return chosen


def _copy_template(template: Path, files: Path, cfg: dict) -> None:
    files.mkdir(parents=True, exist_ok=False)
    for item in template.iterdir():
        if item.name in {"config.json", "server_settings.json"}:
            continue
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError("Template must not contain symbolic links or junctions")
        if item.is_dir():
            # Reject links in subdirectories as well before copying any foreign target.
            for nested in item.rglob("*"):
                if nested.is_symlink() or (hasattr(nested, "is_junction") and nested.is_junction()):
                    raise ValueError("Template must not contain symbolic links or junctions")
            shutil.copytree(item, files / item.name)
        else:
            shutil.copy2(item, files / item.name)
    _write_json(files / "server_settings.json", template_settings_from_config(cfg))


async def add_server(name: str, template_name: str, instance_id: str = "", start_after_install: bool = True,
                     *, progress: Progress = None) -> ActionResult:
    name = str(name or "").strip()
    async def operation():
        if not name:
            raise ValueError("Server name required")
        template = _template_path(template_name)
        if not (template / "config.json").is_file():
            raise ValueError("Template config.json not found")
        tcfg = await _thread(read_template_config, str(template))
        app_id = str(tcfg.get("app_id", "")).strip()
        if not _APP_ID_RE.fullmatch(app_id):
            raise ValueError("Template has an invalid app_id")
        # Reserve the entry before yielding to the filesystem worker. Other producers
        # then see this instance, and a failed install remains visible/retryable.
        cfg = copy.deepcopy(load_config())
        if name in cfg.get("server_paths", {}):
            raise ValueError(f"Server '{name}' already exists")
        chosen = _choose_instance(cfg, app_id, str(instance_id or "").strip() or name, bool(str(instance_id or "").strip()))
        root = _safe_root(server_root(app_id, instance_id=chosen))
        for other, info in cfg.get("server_paths", {}).items():
            if _overlaps(root, _entry_root(other, info)):
                raise ValueError(f"Server folder overlaps '{other}'")
        if root.exists():
            raise ValueError(f"Instance folder already exists: {root}")
        entry = {"app_id": app_id, "instance_id": chosen}
        executable = template_effective_executable(tcfg)
        if executable:
            entry["executable"] = executable
        if tcfg.get("username") and tcfg.get("password"):
            entry.update(username=tcfg["username"], password=encrypt_value(tcfg["password"]))
        cfg.setdefault("server_paths", {})[name] = entry
        save_config(cfg)
        _reload()
        await _progress(progress, "install")
        await _thread(_copy_template, template, root / "serverfiles", tcfg)
        _reload()
        ok, message = await run_update(name)
        if not ok and ("No subscription" in message or "besitzt keine Lizenz" in message):
            cfg = copy.deepcopy(load_config())
            cfg["server_paths"][name].pop("username", None)
            cfg["server_paths"][name].pop("password", None)
            save_config(cfg)
            ok, message = await run_update(name)
        if not ok:
            try:
                free = shutil.disk_usage(root / "serverfiles").free / (1024 ** 3)
                free_text = f"{free:.2f} GB"
            except OSError:
                free_text = "unavailable"
            return ActionResult(
                False,
                f"Install failed: {message}\nApp ID: {app_id}\n"
                f"Install directory: {root / 'serverfiles'}\nFree space: {free_text}",
                instance_id=chosen,
            )
        # This owner also commits shared config; keep that short mutation on the
        # event-loop thread rather than racing another server's config commit.
        ensure_server_executable_hint(name)
        if start_after_install:
            await _progress(progress, "start")
            if not await start_server(name):
                return ActionResult(False, "Installed but start failed", instance_id=chosen)
        return ActionResult(True, f"Server '{name}' installed" + (" and started" if start_after_install else ""), instance_id=chosen)
    return await _perform(name, "install", operation, progress)


def _directory_backup(root: Path, name: str) -> str:
    directory = _backup_dirs()[0]
    directory.mkdir(parents=True, exist_ok=True)
    basename = directory / f"{sanitize_instance_id(name)}-{datetime.now():%Y%m%d-%H%M%S-%f}"
    temporary = directory / f".{basename.name}-{uuid.uuid4().hex}"
    try:
        archive = shutil.make_archive(str(temporary), "zip", root_dir=str(root))
        target = str(basename) + ".zip"
        os.replace(archive, target)
        return target
    finally:
        temporary_zip = Path(str(temporary) + ".zip")
        if temporary_zip.exists():
            temporary_zip.unlink()


async def create_backup(name: str, *, progress: Progress = None) -> ActionResult:
    async def operation():
        _, root, _ = _target(name, allow_shared=True)
        if not root.is_dir():
            raise ValueError(f"Backup path missing: {root}")
        archive = await _thread(_directory_backup, root, name)
        return ActionResult(True, f"Backup created: {backup_display(archive)}", path=archive)
    return await _perform(name, "backup", operation, progress)


def _extract_archive(archive_path: Path, staging: Path) -> None:
    seen = set()
    with zipfile.ZipFile(archive_path, "r") as archive:
        for info in archive.infolist():
            raw = info.filename.replace("\\", "/")
            member = PurePosixPath(raw)
            parts = member.parts
            if (not parts or member.is_absolute() or any(part in {".", ".."} or ":" in part
                    or part.endswith((".", " ")) for part in parts)):
                raise ValueError(f"Backup contains an invalid path: {info.filename}")
            mode = info.external_attr >> 16
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError(f"Backup contains a link or special file: {info.filename}")
            identity = "/".join(parts).casefold()
            if identity in seen:
                raise ValueError(f"Backup contains duplicate paths: {info.filename}")
            seen.add(identity)
            destination = staging.joinpath(*parts)
            if not _inside(destination.resolve(), staging.resolve()):
                raise ValueError(f"Backup path leaves the staging directory: {info.filename}")
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as source, destination.open("xb") as output:
                    # Reading to EOF verifies the ZIP entry's CRC before commit.
                    shutil.copyfileobj(source, output)


def _restore_archive(archive: Path, root: Path, overwrite: bool) -> str:
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{root.name}.restore-stage-", dir=root.parent))
    original = root.with_name(f".{root.name}.restore-original-{uuid.uuid4().hex}")
    moved_original = False
    try:
        _extract_archive(archive, staging)
        if root.exists():
            if not overwrite and any(root.iterdir()):
                raise ValueError("Target folder is not empty. Use overwrite.")
            os.replace(root, original)
            moved_original = True
        try:
            os.replace(staging, root)
        except BaseException:
            if moved_original:
                try:
                    os.replace(original, root)
                except OSError as rollback_error:
                    raise RuntimeError(f"Restore commit failed; original data is preserved at {original}. Rollback failed: {rollback_error}") from rollback_error
            raise
        # Keep the complete previous directory as an explicit recovery point. Do
        # not partially erase it when Windows denies access to individual files.
        return str(original) if moved_original else ""
    finally:
        if staging.exists():
            try:
                shutil.rmtree(staging)
            except OSError:
                logging.warning("Restore staging directory could not be removed: %s", staging, exc_info=True)


async def restore_backup(name: str, backup_file: str, overwrite: bool = False,
                         *, progress: Progress = None) -> ActionResult:
    async def operation():
        _, root, _ = _target(name)
        if is_server_running(name):
            raise ValueError("Restore not possible while server is running")
        archive = _resolve_backup(backup_file)
        recovery = await _thread(_restore_archive, archive, root, overwrite)
        _reload()
        message = f"Backup restored: {backup_display(str(archive))}"
        if recovery:
            message += f". Previous data retained at {recovery}"
        return ActionResult(True, message, path=str(archive))
    return await _perform(name, "restore", operation, progress)


def _remove_tree(root: Path) -> None:
    def retry_readonly(function, failed_path, error):
        if not isinstance(error, PermissionError) or not _inside(Path(failed_path).resolve(), root):
            raise error
        mode = stat.S_IWRITE | stat.S_IREAD
        if os.path.isdir(failed_path):
            mode |= stat.S_IEXEC
        os.chmod(failed_path, mode)
        result = function(failed_path)
        if hasattr(result, "close"):
            result.close()

    for attempt in range(5):
        try:
            shutil.rmtree(root, onexc=retry_readonly)
            return
        except FileNotFoundError:
            if not root.exists():
                return
            raise
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.35 * (attempt + 1))


async def remove_server(name: str, backup_before_delete: bool = True,
                        *, progress: Progress = None) -> ActionResult:
    async def operation():
        _, root, shared = _target(name, allow_shared=True)
        if is_server_running(name):
            if not await stop_server(name) or is_server_running(name):
                raise ValueError("Remove aborted: server could not be stopped")
        if root.exists() and not shared:
            if backup_before_delete:
                await _thread(_directory_backup, root, name)
            await _thread(_remove_tree, root)
        cfg = copy.deepcopy(load_config())
        cfg["server_paths"].pop(name, None)
        save_config(cfg)
        _reload()
        message = "Server removed"
        if shared:
            message += f" (folder kept, shared with {', '.join(shared)})"
        return ActionResult(True, message)
    return await _perform(name, "remove", operation, progress)


async def update_server_config(name: str, values: dict, *, progress: Progress = None) -> ActionResult:
    async def operation():
        allowed = {"app_id", "executable", "username", "password", "instance_id", "install_dir"}
        if set(values) - allowed:
            raise ValueError("Unknown config key; runtime options belong in settings")
        cfg = copy.deepcopy(load_config())
        entry = cfg.get("server_paths", {}).get(name)
        if not isinstance(entry, dict):
            raise ValueError(f"Server not found: {name}")
        patch = dict(values)
        # Discord accepts Python literals, so an unquoted Steam ID arrives as
        # an integer. Canonicalize that representation before field validation.
        # bool is deliberately excluded even though it subclasses int.
        if type(patch.get("app_id")) is int:
            patch["app_id"] = str(patch["app_id"])
        for key, value in patch.items():
            if not isinstance(value, str) and value is not None:
                raise ValueError(f"{key} must be text")
        if "app_id" in patch and not _APP_ID_RE.fullmatch(str(patch["app_id"] or "")):
            raise ValueError("Invalid app_id")
        if patch.get("instance_id"):
            patch["instance_id"] = sanitize_instance_id(patch["instance_id"])
        if "password" in patch and patch["password"]:
            patch["password"] = encrypt_value(patch["password"])
        entry.update(patch)
        if {"app_id", "instance_id", "install_dir"} & patch.keys():
            if is_server_running(name):
                raise ValueError("Stop the server before changing its storage path")
            root = _safe_root(_entry_root(name, entry))
            for other, info in cfg["server_paths"].items():
                if other != name and _overlaps(root, _entry_root(other, info)):
                    raise ValueError(f"Server folder overlaps '{other}'")
        save_config(cfg)
        _reload()
        return ActionResult(True, "Configuration saved")
    return await _perform(name, "updateserver", operation, progress)


def _merge_settings(path: Path, patch: dict) -> None:
    current = {}
    if path.exists():
        with path.open("r", encoding="utf-8-sig") as handle:
            current = json.load(handle)
        if not isinstance(current, dict):
            raise ValueError("Server settings must be an object")
    current.update(patch)
    if "executable" in patch:
        os_key = current_os_executable_key()
        if os_key and os_key not in patch:
            current[os_key] = patch["executable"]
    _write_json(path, current)


async def update_server_settings(name: str, values: dict, *, progress: Progress = None) -> ActionResult:
    async def operation():
        files, _, _ = _target(name)
        patch = _settings_patch(values)
        await _thread(_merge_settings, files / "server_settings.json", patch)
        _reload()
        return ActionResult(True, "Settings saved")
    return await _perform(name, "updatesettings", operation, progress)
