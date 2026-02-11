import os
import json
import shlex
import logging
import asyncio
import zipfile
from datetime import datetime
from typing import Optional, List

import discord
from discord.commands import Option
from discord import OptionChoice
from discord.ext import commands

from context import bot
from security import encrypt_value
from config_store import (
    save_config,
    load_config,
    PLUGIN_TEMPLATES_DIR,
    BASE_DIR,
    CONFIG_PATH,
    DB_PATH,
)
from config_store import get_config_value
from paths import (
    load_server_paths,
    load_server_configs,
    SERVER_PATHS,
    server_root,
    sanitize_instance_id,
)
from db import write_action_log
from ui import refresh_status_panel
from server_manager import start_server, stop_server, server_processes
from steam_integration import run_update, run_update_with_credentials, get_steamcmd_resolution
from context import CHANNEL, safe_get_ip
from runtime_status import (
    begin_operation,
    end_operation_success,
    end_operation_failed,
    clear_server_status,
    get_operation_status,
)


# ---------- helper: robust defer & reply ----------
async def safe_defer(ctx: discord.ApplicationContext, ephemeral: bool = True) -> bool:
    """Versucht zu deferren, gibt False zurück wenn das Interaction-Token schon ungültig ist."""
    try:
        interaction = getattr(ctx, "interaction", None)
        if getattr(ctx, "responded", False) or (
            interaction is not None and interaction.response.is_done()
        ):
            return True
        await ctx.defer(ephemeral=ephemeral)
        return True
    except discord.NotFound:
        # Unknown interaction – Token bereits ungültig
        return False
    except discord.HTTPException as e:
        if getattr(e, "code", None) == 40060:
            # Bereits bestätigt/deferred -> ok
            return True
        logging.warning(f"safe_defer HTTPException: {e}")
        return False
    except Exception as e:
        logging.warning(f"safe_defer: {e}")
        return False


async def safe_send(ctx: discord.ApplicationContext, content: Optional[str] = None,
                    *, embed: Optional[discord.Embed] = None, ephemeral: bool = True):
    """
    Antwortet robust:
    - wenn noch nicht geantwortet: ctx.respond(...)
    - wenn deferred/geantwortet:   ctx.followup.send(...)
    - bei Unknown interaction:     ctx.channel.send(...) (nicht-ephemeral)
    """
    message = content or ""
    interaction = getattr(ctx, "interaction", None)
    ch = getattr(ctx, "channel", None)

    async def _channel_fallback():
        if not ch:
            return None
        try:
            if ephemeral:
                logging.warning("safe_send fallback: ephemeral content suppressed in public channel.")
                return await ch.send("Antwort konnte nicht privat zugestellt werden. Bitte Befehl erneut ausführen.")
            return await ch.send(message, embed=embed)
        except Exception as inner:
            logging.warning(f"safe_send channel fallback failed: {inner}")
            return None

    try:
        is_done = bool(getattr(ctx, "responded", False))
        if not is_done and interaction is not None:
            is_done = interaction.response.is_done()

        if is_done:
            return await ctx.followup.send(message, embed=embed, ephemeral=ephemeral)
        return await ctx.respond(message, embed=embed, ephemeral=ephemeral)
    except discord.NotFound:
        return await _channel_fallback()
    except discord.HTTPException as e:
        code = getattr(e, "code", None)
        if code == 40060:
            try:
                return await ctx.followup.send(message, embed=embed, ephemeral=ephemeral)
            except Exception:
                return await _channel_fallback()
        if code == 10062:
            return await _channel_fallback()
        logging.warning(f"safe_send HTTPException: {e}")
        return await _channel_fallback()
    except Exception as e:
        logging.warning(f"safe_send failed: {e}")
        return await _channel_fallback()


async def refresh_main_panel(user=None):
    ch = bot.get_channel(CHANNEL)
    if ch:
        await refresh_status_panel(ch, user=user)


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


def _delete_root_from_server_path(path: str) -> str:
    p = os.path.abspath(path)
    if os.path.basename(p).lower() == "serverfiles":
        return os.path.dirname(p)
    return p


def _create_directory_backup(path: str, server_name: str) -> str:
    import shutil

    backup_dir = _backup_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_base = os.path.join(backup_dir, f"{sanitize_instance_id(server_name)}-{stamp}")
    return shutil.make_archive(backup_base, "zip", root_dir=path)


def _backup_dir() -> str:
    backup_dir = os.path.join(BASE_DIR, "steam", "backup")
    os.makedirs(backup_dir, exist_ok=True)
    return backup_dir


def _legacy_backup_dir() -> str:
    return os.path.join(BASE_DIR, "backups")


def _backup_search_dirs() -> List[str]:
    dirs = [_backup_dir()]
    legacy = _legacy_backup_dir()
    if os.path.isdir(legacy):
        dirs.append(legacy)
    return dirs


def _backup_display_path(path: str) -> str:
    filename = os.path.basename(path)
    full = os.path.abspath(path)
    try:
        primary = os.path.abspath(_backup_dir())
        if os.path.commonpath([primary, full]) == primary:
            return f"/steam/backup/{filename}"
    except Exception:
        pass
    try:
        legacy = os.path.abspath(_legacy_backup_dir())
        if os.path.commonpath([legacy, full]) == legacy:
            return f"/backups/{filename}"
    except Exception:
        pass
    return filename


def _resolve_backup_path(backup_file: str) -> Optional[str]:
    backup_name = os.path.basename(str(backup_file or "").strip())
    if not backup_name:
        return None
    for backup_dir in _backup_search_dirs():
        candidate = os.path.abspath(os.path.join(backup_dir, backup_name))
        backup_root = os.path.abspath(backup_dir)
        try:
            if os.path.commonpath([backup_root, candidate]) != backup_root:
                continue
        except Exception:
            continue
        if os.path.isfile(candidate):
            return candidate
    return None


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


# --------------- autocomplete ---------------
async def template_autocomplete(ctx: discord.AutocompleteContext) -> List[OptionChoice]:
    try:
        items = [d for d in os.listdir(PLUGIN_TEMPLATES_DIR) if os.path.isdir(os.path.join(PLUGIN_TEMPLATES_DIR, d))]
        return [OptionChoice(t) for t in items if ctx.value.lower() in t.lower()][:25]
    except Exception as e:
        logging.error(f"Template Autocomplete error: {e}")
        return []


async def server_autocomplete(ctx: discord.AutocompleteContext) -> List[OptionChoice]:
    cfg = load_config()
    return [OptionChoice(s) for s in cfg["server_paths"].keys() if ctx.value.lower() in s.lower()]


async def backup_autocomplete(ctx: discord.AutocompleteContext) -> List[OptionChoice]:
    try:
        current = (ctx.value or "").lower()
        server_name = sanitize_instance_id(str(ctx.options.get("name", "") or "")).lower()
        files = []
        seen = set()
        for backup_dir in _backup_search_dirs():
            for entry in os.listdir(backup_dir):
                full = os.path.join(backup_dir, entry)
                key = entry.lower()
                if key in seen:
                    continue
                if os.path.isfile(full) and key.endswith(".zip"):
                    seen.add(key)
                    files.append(entry)
        files.sort(reverse=True)
        if server_name:
            matching = [f for f in files if server_name in f.lower()]
            non_matching = [f for f in files if server_name not in f.lower()]
            files = matching + non_matching
        return [OptionChoice(f) for f in files if current in f.lower()][:25]
    except Exception as e:
        logging.error(f"Backup Autocomplete error: {e}")
        return []


async def section_autocomplete(ctx: discord.AutocompleteContext) -> List[OptionChoice]:
    return [OptionChoice("config"), OptionChoice("settings")]


async def key_autocomplete(ctx: discord.AutocompleteContext) -> List[OptionChoice]:
    current = ctx.value
    name = ctx.options.get("server")
    section = ctx.options.get("section")
    if not name or not section:
        return []
    all_config_keys = [
        "app_id",
        "executable",
        "username",
        "password",
        "instance_id",
        "install_dir",
        "auto_update",
        "auto_restart",
        "stop_time",
        "restart_after_stop",
    ]
    all_settings_keys = ["executable", "parameters", "auto_update", "auto_restart", "stop_time", "restart_after_stop"]
    try:
        if section == "config":
            cfg = load_config()
            existing = list(cfg["server_paths"].get(name, {}).keys())
            keys = list(set(existing + all_config_keys))
        else:
            keys = all_settings_keys.copy()
            load_server_paths()
            if name in SERVER_PATHS:
                sf = os.path.join(SERVER_PATHS[name], "server_settings.json")
                if os.path.exists(sf):
                    try:
                        with open(sf, "r", encoding="utf-8") as f:
                            existing = json.load(f)
                        keys = list(set(keys + list(existing.keys())))
                    except Exception:
                        pass
        filtered = [k for k in keys if current.lower() in k.lower()]
        filtered.sort()
        return [OptionChoice(k, k) for k in filtered][:25]
    except Exception as e:
        logging.error(f"Autocomplete-Fehler: {e}")
        return []


# --------------- commands ---------------
@bot.slash_command(name="diag", description="Zeigt eine kurze Systemdiagnose für DGSM")
@commands.has_role("Admin")
@commands.cooldown(2, 10, commands.BucketType.user)
async def diag(ctx: discord.ApplicationContext):
    await safe_defer(ctx, ephemeral=True)
    try:
        cfg = load_config()
        load_server_paths()
        load_server_configs()
        steamcmd_path, steam_dir = get_steamcmd_resolution()

        server_lines = []
        running_count = 0
        for name, path in SERVER_PATHS.items():
            path_ok = os.path.isdir(path)
            settings_ok = os.path.isfile(os.path.join(path, "server_settings.json"))
            running = False
            try:
                proc = server_processes.get(name)
                running = bool(proc and proc.is_running())
            except Exception:
                running = False
            if running:
                running_count += 1
            server_lines.append(
                f"- `{name}` | path:{'ok' if path_ok else 'missing'} | "
                f"settings:{'ok' if settings_ok else 'missing'} | running:{'yes' if running else 'no'}"
            )

        max_lines = 20
        details = "\n".join(server_lines[:max_lines]) if server_lines else "- Keine Server eingetragen"
        if len(server_lines) > max_lines:
            details += f"\n- ... {len(server_lines) - max_lines} weitere"

        message = (
            "**DGSM Diagnose**\n"
            f"- Config: `{CONFIG_PATH}`\n"
            f"- Logs DB: `{DB_PATH}`\n"
            f"- SteamCMD: `{steamcmd_path or 'nicht gefunden'}`\n"
            f"- Steam Verzeichnis: `{steam_dir}`\n"
            f"- Domain-IP Lookup: `{safe_get_ip()}`\n"
            f"- Server eingetragen: `{len(cfg.get('server_paths', {}))}`\n"
            f"- Server laufend: `{running_count}`\n\n"
            f"**Serverstatus**\n{details}"
        )
        await safe_send(ctx, message, ephemeral=True)
    except Exception:
        logging.exception("diag failed")
        await safe_send(ctx, "Diagnose fehlgeschlagen. Details stehen im Bot-Log.", ephemeral=True)


@bot.slash_command(name="createtemplate", description="Erstellt oder aktualisiert ein Server-Template")
@commands.has_role("Admin")
@commands.cooldown(1, 5, commands.BucketType.user)
async def create_template(
    ctx: discord.ApplicationContext,
    template_name: str,
    app_id: str,
    executable: str,
    parameters: Option(str, "Startparameter (z.B. -port=25565 -maxplayers=20)", required=False),
    auto_update: Option(bool, "Automatische Updates aktivieren", default=True),
    auto_restart: Option(bool, "Automatischer Neustart bei Absturz", default=True),
    stop_time: Option(str, "Tägliche Stoppzeit (HH:MM)", default="05:00"),
    restart_after_stop: Option(bool, "Neustart nach täglichem Stopp", default=False),
    username: Option(str, "Steam-Benutzername", required=False),
    password: Option(str, "Steam-Passwort", required=False, sensitive=True),
):
    await safe_defer(ctx, ephemeral=True)
    try:
        param_list = shlex.split(parameters) if parameters else []
        template_dir = os.path.join(PLUGIN_TEMPLATES_DIR, template_name)
        os.makedirs(template_dir, exist_ok=True)
        config = {
            "app_id": app_id,
            "executable": executable,
            "auto_update": auto_update,
            "auto_restart": auto_restart,
            "stop_time": stop_time,
            "restart_after_stop": restart_after_stop,
            "parameters": param_list,
        }
        if username and password:
            config["username"] = username
            config["password"] = password
        with open(os.path.join(template_dir, "config.json"), "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
        with open(os.path.join(template_dir, "server_settings.json"), "w", encoding="utf-8") as f:
            json.dump({
                "executable": executable,
                "parameters": param_list,
                "auto_update": auto_update,
                "auto_restart": auto_restart,
                "stop_time": stop_time,
                "restart_after_stop": restart_after_stop,
            }, f, indent=4)
        await safe_send(ctx, f"✅ Template '{template_name}' erstellt/aktualisiert.", ephemeral=True)
    except Exception as e:
        await safe_send(ctx, f"❌ Fehler: {e}", ephemeral=True)


@bot.slash_command(name="addserver", description="Installiert einen neuen Server aus einem Template")
@commands.has_role("Admin")
@commands.cooldown(1, 5, commands.BucketType.user)
async def add_server(
    ctx: discord.ApplicationContext,
    name: str,
    template: Option(str, "Vorlage für den Server", autocomplete=template_autocomplete),
    instance_id: Option(str, "Optionale Instanz-ID (z.B. pve-2)", required=False),
):
    await safe_defer(ctx, ephemeral=True)
    try:
        template_dir = os.path.join(PLUGIN_TEMPLATES_DIR, template)
        if not os.path.exists(template_dir):
            return await safe_send(ctx, f"❌ Template '{template}' existiert nicht!", ephemeral=True)
        cfg_path = os.path.join(template_dir, "config.json")
        if not os.path.exists(cfg_path):
            return await safe_send(ctx, f"❌ Template '{template}' hat keine config.json!", ephemeral=True)
        with open(cfg_path, "r", encoding="utf-8") as f:
            tcfg = json.load(f)
        for req in ("app_id", "executable"):
            if req not in tcfg:
                return await safe_send(ctx, f"❌ Template unvollständig. Fehlend: {req}", ephemeral=True)

        cfg = load_config()
        if name in cfg["server_paths"]:
            return await safe_send(ctx, f"Server '{name}' existiert bereits.", ephemeral=True)

        app_id = str(tcfg["app_id"])
        executable = tcfg["executable"]
        if instance_id:
            chosen_instance_id = sanitize_instance_id(instance_id)
            if _instance_id_exists(cfg, app_id, chosen_instance_id):
                return await safe_send(
                    ctx,
                    f"Instanz-ID '{chosen_instance_id}' ist für App-ID {app_id} bereits vergeben.",
                    ephemeral=True,
                )
        else:
            chosen_instance_id = _make_instance_id(cfg, app_id, name)

        instance_id = chosen_instance_id
        instance_root = server_root(app_id, instance_id=instance_id)
        server_dir = os.path.join(str(instance_root), "serverfiles")
        os.makedirs(server_dir, exist_ok=True)

        for item in os.listdir(template_dir):
            if item == "config.json":
                continue
            src = os.path.join(template_dir, item)
            dst = os.path.join(server_dir, item)
            if os.path.isdir(src):
                from shutil import copytree
                copytree(src, dst, dirs_exist_ok=True)
            else:
                from shutil import copy2
                copy2(src, dst)

        s_settings = {
            "executable": executable,
            "parameters": tcfg.get("parameters", []),
            "auto_update": tcfg.get("auto_update", True),
            "auto_restart": tcfg.get("auto_restart", True),
            "stop_time": tcfg.get("stop_time", "05:00"),
            "restart_after_stop": tcfg.get("restart_after_stop", False),
        }
        with open(os.path.join(server_dir, "server_settings.json"), "w", encoding="utf-8") as f:
            json.dump(s_settings, f, indent=4)

        entry = {"app_id": app_id, "executable": executable, "instance_id": instance_id}
        if tcfg.get("username") and tcfg.get("password"):
            entry["username"] = tcfg["username"]
            entry["password"] = encrypt_value(tcfg["password"])
        cfg["server_paths"][name] = entry
        save_config(cfg)

        load_server_paths()
        load_server_configs()

        async def _run_update_with_panel() -> tuple[bool, str]:
            update_task = asyncio.create_task(run_update(name))
            for _ in range(20):
                state, _detail = get_operation_status(name)
                if state == "busy" or update_task.done():
                    break
                await asyncio.sleep(0.05)
            await refresh_main_panel()
            return await update_task

        await safe_send(ctx, f"Starte Installation von **{name}** (Instanz: `{instance_id}`)...", ephemeral=True)
        ok, message = await _run_update_with_panel()
        if not ok:
            # try anonymous fallback
            if "No subscription" in message or "besitzt keine Lizenz" in message:
                if "username" in cfg["server_paths"][name]:
                    cfg["server_paths"][name].pop("username", None)
                    cfg["server_paths"][name].pop("password", None)
                    save_config(cfg)
                ok, message = await _run_update_with_panel()
        if not ok:
            disk_free = 0.0
            try:
                import shutil
                disk_free = shutil.disk_usage(server_dir).free / (1024 ** 3)
            except Exception:
                pass
            debug = (
                f"❌ **Fehler bei Installation von {name}**\n"
                f"App-ID: `{app_id}`\n"
                f"Installationsverzeichnis: `{server_dir}`\n"
                f"Verfügbarer Speicher: `{disk_free:.2f} GB`\n"
                f"Fehlermeldung: ```{message}```"
            )
            await safe_send(ctx, debug, ephemeral=True)
            write_action_log("install", name, "failed", message)
            await refresh_main_panel()
            return

        await safe_send(ctx, f"✅ Installation erfolgreich! ▶️ Starte **{name}**…", ephemeral=True)
        if not await start_server(name):
            write_action_log("start_after_add", name, "failed")
            await refresh_main_panel()
            return await safe_send(ctx, "❌ Start fehlgeschlagen!", ephemeral=True)

        write_action_log("start_after_add", name, "success")
        await refresh_main_panel()
        await safe_send(
            ctx,
            f"**{name}** aus Template '{template}' installiert und gestartet (Instanz: `{instance_id}`).",
            ephemeral=True,
        )

    except Exception as e:
        logging.exception("Fehler in add_server")
        await safe_send(ctx, f"❌ Kritischer Fehler: {e}", ephemeral=True)


@bot.slash_command(name="updateserver", description="Aktualisiert einen Eintrag in Config oder Settings")
@commands.has_role("Admin")
@commands.cooldown(3, 4, commands.BucketType.user)
async def update_server(
    ctx: discord.ApplicationContext,
    server: Option(str, "Server-Name", autocomplete=server_autocomplete),
    section: Option(str, "config oder settings", autocomplete=section_autocomplete),
    key: Option(str, "Welcher Eintrag?", autocomplete=key_autocomplete, required=False),
    value: Option(str, "Neuer Wert als Python-Literal", required=False),
):
    await safe_defer(ctx, ephemeral=True)
    cfg = load_config()
    if server not in cfg["server_paths"]:
        return await safe_send(ctx, "❌ Server nicht gefunden!", ephemeral=True)
    if not key:
        return await safe_send(ctx, "ℹ️ Kein Eintrag – nichts geändert.", ephemeral=True)
    if not value:
        return await safe_send(ctx, "❗ Bitte neuen Wert angeben.", ephemeral=True)
    import ast
    try:
        new_val = ast.literal_eval(value)
    except Exception:
        new_val = value
    if section == "config":
        entry = cfg["server_paths"][server]
        entry[key] = encrypt_value(str(new_val)) if key == "password" else new_val
        save_config(cfg)
        load_server_paths()
        load_server_configs()
    else:
        sf = os.path.join(SERVER_PATHS[server], "server_settings.json")
        s_cfg = json.load(open(sf, "r", encoding="utf-8")) if os.path.exists(sf) else {}
        s_cfg[key] = new_val
        json.dump(s_cfg, open(sf, "w", encoding="utf-8"), indent=4)
        load_server_configs()
    write_action_log("updateserver", server, "success", f"{section}.{key}")
    await safe_send(ctx, f"✅ **{server}** {section}.{key} → {value}", ephemeral=True)


@bot.slash_command(name="removeserver", description="Entfernt einen Server komplett")
@commands.has_role("Admin")
@commands.cooldown(1, 5, commands.BucketType.user)
async def remove_server(
    ctx: discord.ApplicationContext,
    name: Option(str, "Name des Servers", autocomplete=server_autocomplete),
    backup_before_delete: Option(bool, "Vor dem Löschen ein ZIP-Backup erstellen", default=True),
):
    await safe_defer(ctx, ephemeral=True)
    try:
        cfg = load_config()
        if name not in cfg["server_paths"]:
            return await safe_send(ctx, "Server nicht gefunden!", ephemeral=True)

        load_server_paths()
        target_server_path = SERVER_PATHS.get(name)
        if not target_server_path:
            app_id = str(cfg["server_paths"][name].get("app_id", ""))
            target_server_path = os.path.join(str(server_root(app_id)), "serverfiles")

        if name in server_processes:
            await stop_server(name)
            await safe_send(ctx, f"Server **{name}** gestoppt.", ephemeral=True)

        delete_root = _delete_root_from_server_path(target_server_path)
        delete_root_norm = os.path.normcase(os.path.abspath(delete_root))
        shared_with = None
        for other_name, other_path in SERVER_PATHS.items():
            if other_name == name:
                continue
            other_root = os.path.normcase(os.path.abspath(_delete_root_from_server_path(other_path)))
            if other_root == delete_root_norm or other_root.startswith(delete_root_norm + os.sep):
                shared_with = other_name
                break

        if os.path.exists(delete_root):
            import shutil
            if shared_with:
                await safe_send(
                    ctx,
                    f"Ordner bleibt erhalten, da auch `{shared_with}` ihn nutzt: `{delete_root}`",
                    ephemeral=True,
                )
            else:
                if backup_before_delete:
                    try:
                        archive_path = _create_directory_backup(delete_root, name)
                        await safe_send(ctx, f"Backup erstellt: `{_backup_display_path(archive_path)}`", ephemeral=True)
                    except Exception as backup_error:
                        logging.exception("Backup vor remove fehlgeschlagen")
                        return await safe_send(
                            ctx,
                            f"Backup fehlgeschlagen ({backup_error}). Entfernen wurde abgebrochen.",
                            ephemeral=True,
                        )
                shutil.rmtree(delete_root)
                await safe_send(ctx, f"Server-Verzeichnis gelöscht: `{delete_root}`", ephemeral=True)

        del cfg["server_paths"][name]
        save_config(cfg)
        load_server_paths()
        load_server_configs()
        clear_server_status(name)
        await refresh_main_panel()
        await safe_send(ctx, f"Server **{name}** komplett entfernt.", ephemeral=True)
    except Exception as e:
        await safe_send(ctx, f"Fehler beim Entfernen: {e}", ephemeral=True)


@bot.slash_command(name="createbackup", description="Erstellt ein ZIP-Backup eines Servers")
@commands.has_role("Admin")
@commands.cooldown(1, 8, commands.BucketType.user)
async def create_backup(
    ctx: discord.ApplicationContext,
    name: Option(str, "Name des Servers", autocomplete=server_autocomplete),
):
    await safe_defer(ctx, ephemeral=True)

    cfg = load_config()
    if name not in cfg["server_paths"]:
        return await safe_send(ctx, "❌ Server nicht gefunden!", ephemeral=True)

    load_server_paths()
    target_server_path = SERVER_PATHS.get(name)
    if not target_server_path:
        entry = cfg["server_paths"].get(name, {})
        app_id = str(entry.get("app_id", "")).strip()
        if not app_id:
            return await safe_send(ctx, "❌ App-ID für den Server fehlt.", ephemeral=True)
        instance_id = entry.get("instance_id")
        if isinstance(instance_id, str) and instance_id.strip():
            target_server_path = os.path.join(str(server_root(app_id, instance_id=instance_id)), "serverfiles")
        else:
            target_server_path = os.path.join(str(server_root(app_id)), "serverfiles")

    backup_root = _delete_root_from_server_path(target_server_path)
    if not os.path.exists(backup_root):
        return await safe_send(ctx, f"❌ Backup-Pfad existiert nicht: `{backup_root}`", ephemeral=True)

    success = False
    fail_reason = ""
    status_finalized = False
    begin_operation(name, "backup")
    await refresh_main_panel()
    try:
        archive_path = _create_directory_backup(backup_root, name)
        write_action_log("createbackup", name, "success", os.path.basename(archive_path))
        success = True
        end_operation_success(name)
        status_finalized = True

        await refresh_main_panel()
        await safe_send(ctx, f"✅ Backup erstellt: `{_backup_display_path(archive_path)}`", ephemeral=True)
    except Exception as e:
        fail_reason = str(e)
        write_action_log("createbackup", name, "failed", fail_reason)
        logging.exception("create_backup fehlgeschlagen")
        await safe_send(ctx, f"❌ Backup fehlgeschlagen: {e}", ephemeral=True)
    finally:
        if not status_finalized:
            if success:
                end_operation_success(name)
            else:
                end_operation_failed(name, fail_reason or "Backup fehlgeschlagen")
            await refresh_main_panel()


@bot.slash_command(name="restorebackup", description="Stellt ein ZIP-Backup eines Servers wieder her")
@commands.has_role("Admin")
@commands.cooldown(1, 8, commands.BucketType.user)
async def restore_backup(
    ctx: discord.ApplicationContext,
    name: Option(str, "Name des Servers", autocomplete=server_autocomplete),
    backup_file: Option(str, "Backup-Datei aus /steam/backup", autocomplete=backup_autocomplete),
    overwrite: Option(bool, "Zielordner vorher löschen", default=False),
):
    await safe_defer(ctx, ephemeral=True)

    cfg = load_config()
    if name not in cfg["server_paths"]:
        return await safe_send(ctx, "❌ Server nicht gefunden!", ephemeral=True)

    archive_path = _resolve_backup_path(backup_file)
    if not archive_path:
        return await safe_send(ctx, "❌ Backup-Datei nicht gefunden.", ephemeral=True)

    proc = server_processes.get(name)
    try:
        if proc and proc.is_running():
            return await safe_send(ctx, "❌ Restore nicht möglich: Server läuft.", ephemeral=True)
    except Exception:
        pass

    load_server_paths()
    target_server_path = SERVER_PATHS.get(name)
    if not target_server_path:
        entry = cfg["server_paths"].get(name, {})
        app_id = str(entry.get("app_id", "")).strip()
        if not app_id:
            return await safe_send(ctx, "❌ App-ID für den Server fehlt.", ephemeral=True)
        instance_id = entry.get("instance_id")
        if isinstance(instance_id, str) and instance_id.strip():
            target_server_path = os.path.join(str(server_root(app_id, instance_id=instance_id)), "serverfiles")
        else:
            target_server_path = os.path.join(str(server_root(app_id)), "serverfiles")

    restore_root = _delete_root_from_server_path(target_server_path)
    restore_root_norm = os.path.normcase(os.path.abspath(restore_root))
    if overwrite:
        for other_name, other_path in SERVER_PATHS.items():
            if other_name == name:
                continue
            other_root = os.path.normcase(os.path.abspath(_delete_root_from_server_path(other_path)))
            if other_root == restore_root_norm or other_root.startswith(restore_root_norm + os.sep):
                return await safe_send(
                    ctx,
                    f"❌ overwrite abgelehnt: Ordner wird auch von `{other_name}` genutzt.",
                    ephemeral=True,
                )

    if os.path.exists(restore_root) and not overwrite:
        try:
            if any(os.scandir(restore_root)):
                return await safe_send(
                    ctx,
                    "❌ Zielordner ist nicht leer. Nutze `overwrite=true`, um zuerst zu löschen.",
                    ephemeral=True,
                )
        except Exception:
            pass

    success = False
    fail_reason = ""
    status_finalized = False
    begin_operation(name, "restore")
    await refresh_main_panel()
    try:
        import shutil

        if os.path.exists(restore_root) and overwrite:
            shutil.rmtree(restore_root)
        os.makedirs(restore_root, exist_ok=True)

        with zipfile.ZipFile(archive_path, "r") as archive:
            if not _zip_members_are_safe(archive, restore_root):
                fail_reason = "Backup enthält ungültige Pfade"
                await safe_send(ctx, "❌ Backup enthält ungültige Pfade und wurde nicht entpackt.", ephemeral=True)
                return
            archive.extractall(restore_root)

        load_server_paths()
        load_server_configs()
        write_action_log("restorebackup", name, "success", os.path.basename(archive_path))
        success = True
        end_operation_success(name)
        status_finalized = True

        await refresh_main_panel()
        await safe_send(ctx, f"✅ Backup wiederhergestellt: `{_backup_display_path(archive_path)}`", ephemeral=True)
    except Exception as e:
        fail_reason = str(e)
        write_action_log("restorebackup", name, "failed", fail_reason)
        logging.exception("restore_backup fehlgeschlagen")
        await safe_send(ctx, f"❌ Restore fehlgeschlagen: {e}", ephemeral=True)
    finally:
        if not status_finalized:
            if success:
                end_operation_success(name)
            else:
                end_operation_failed(name, fail_reason or "Restore fehlgeschlagen")
            await refresh_main_panel()


@bot.slash_command(name="showserverconfig", description="Zeigt Konfiguration eines Servers")
@commands.has_role("Admin")
async def show_server_config(ctx: discord.ApplicationContext, name: str):
    await safe_defer(ctx, ephemeral=True)
    cfg = load_config()
    if name not in cfg["server_paths"]:
        return await safe_send(ctx, "❌ Server nicht gefunden!", ephemeral=True)
    d = cfg["server_paths"][name]
    msg = f"**{name}**\nAppID: {d.get('app_id')}\nExec: {d.get('executable')}"
    if "username" in d:
        msg += "\nUser: {0}\nPass: [GESCHÜTZT]".format(d["username"])
    await safe_send(ctx, msg, ephemeral=True)


@bot.slash_command(name="logs", description="Letzte Logs anzeigen")
@commands.has_role("Admin")
@commands.cooldown(5, 4, commands.BucketType.user)
async def show_logs(ctx: discord.ApplicationContext, server: Optional[str] = None, action: Optional[str] = None, limit: int = 15):
    await safe_defer(ctx, ephemeral=True)
    if limit > 15:
        limit = 15
    import sqlite3
    from config_store import DB_PATH
    q = "SELECT timestamp, action, server, status, details FROM logs"
    params, conds = [], []
    if server:
        conds.append("server=?"); params.append(server)
    if action:
        conds.append("action=?"); params.append(action)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC LIMIT ?"; params.append(limit)
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute(q, params); rows = c.fetchall(); conn.close()
    if not rows:
        return await safe_send(ctx, "Keine Logs.", ephemeral=True)
    eb = discord.Embed(title="📜 Logs", color=0x2ecc71)
    for ts, act, srv, st, det in rows:
        eb.add_field(name=f"{ts} | {act.upper()} | {srv}", value=f"Status: {st}\n{det or '-'}", inline=False)
    await safe_send(ctx, embed=eb, ephemeral=True)
