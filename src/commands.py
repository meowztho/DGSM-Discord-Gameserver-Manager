import os
import json
import logging
import asyncio
from typing import Optional, List

import discord
from discord.commands import Option
from discord import OptionChoice
from discord.ext import commands

from context import bot
from config_store import (
    load_config,
    PLUGIN_TEMPLATES_DIR,
    CONFIG_PATH,
    DB_PATH,
)
from paths import (
    load_server_paths,
    load_server_configs,
    SERVER_PATHS,
    sanitize_instance_id,
)
from db import write_action_log
from ui import refresh_status_panel
from server_manager import server_processes
from steam_integration import get_steamcmd_resolution
from context import CHANNEL, safe_get_ip
from platform_utils import runtime_platform_label
import server_service
from cli_commands import execute_cli_command
from wgsm_import import format_import_summary, import_wgsm_plugin


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


def _trim_text(value: str, limit: int = 1700) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _render_cli_reply(command_line: str, payload: str, ok: bool) -> str:
    icon = "✅" if ok else "❌"
    cmd = _trim_text(command_line, 200)
    output = _trim_text(payload, 1700)
    if "\n" in output:
        return f"{icon} CLI `{cmd}`\n```text\n{output}\n```"
    return f"{icon} CLI `{cmd}`\n{output}"


async def refresh_main_panel(user=None):
    ch = bot.get_channel(CHANNEL)
    if ch:
        await refresh_status_panel(ch, user=user)
    _notify_desktop_ui_refresh()


def _notify_desktop_ui_refresh(reason: str = "") -> None:
    try:
        from desktop_ui import notify_external_refresh  # lazy import to avoid hard cycle at startup

        notify_external_refresh(reason)
    except Exception:
        pass


async def _admin_progress(label: str) -> None:
    _notify_desktop_ui_refresh(label)
    await refresh_main_panel()


async def _admin_reply(ctx, result, success_message: str = "") -> None:
    try:
        await refresh_main_panel()
    except Exception:
        logging.exception("Administrative final panel refresh failed")
    message = success_message if result.ok and success_message else result.message
    await safe_send(ctx, f"{'✅' if result.ok else '❌'} {message}", ephemeral=True)


async def template_autocomplete(ctx: discord.AutocompleteContext) -> List[OptionChoice]:
    try:
        items = server_service.list_templates()
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
        files = server_service.list_backup_files()
        if server_name:
            files.sort(key=lambda value: server_name not in value.lower())
        return [OptionChoice(value) for value in files if current in value.lower()][:25]
    except Exception:
        logging.exception("Backup autocomplete failed")
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
        "auto_start",
        "auto_update",
        "auto_restart",
        "stop_time",
        "restart_after_stop",
    ]
    all_settings_keys = [
        "executable",
        "executable_windows",
        "executable_linux",
        "parameters",
        "auto_start",
        "auto_update",
        "auto_restart",
        "stop_time",
        "restart_after_stop",
        "rest_api",
    ]
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
                        with open(sf, "r", encoding="utf-8-sig") as f:
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
            f"- Plattform: `{runtime_platform_label()}`\n"
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
    executable: Option(str, "Server executable (optional)", required=False, default=""),
    parameters: Option(str, "Startparameter (z.B. -port=25565 -maxplayers=20)", required=False),
    auto_start: Option(bool, "Automatischer Start beim DGSM-Start", default=False),
    auto_update: Option(bool, "Automatische Updates aktivieren", default=True),
    auto_restart: Option(bool, "Automatischer Neustart bei Absturz", default=True),
    stop_time: Option(str, "Tägliche Stoppzeit (HH:MM)", default="05:00"),
    restart_after_stop: Option(bool, "Neustart nach täglichem Stopp", default=False),
    username: Option(str, "Steam-Benutzername", required=False),
    password: Option(str, "Steam-Passwort", required=False, sensitive=True),
):
    await safe_defer(ctx, ephemeral=True)
    result = await server_service.create_template(
        template_name, app_id, executable or "", parameters or "",
        auto_update=auto_update, auto_restart=auto_restart, stop_time=stop_time,
        restart_after_stop=restart_after_stop, username=username or "",
        password=password or "", auto_start=auto_start, progress=_admin_progress,
    )
    await _admin_reply(ctx, result, f"Template '{template_name}' erstellt/aktualisiert.")


@bot.slash_command(name="importwgsm", description="Importiert ein WindowsGSM-Plugin als DGSM-Template")
@commands.has_role("Admin")
@commands.cooldown(1, 10, commands.BucketType.user)
async def import_windowsgsm_plugin(
    ctx: discord.ApplicationContext,
    source_url: Option(str, "HTTPS GitHub repository, .cs or plugin ZIP URL"),
    template_name: Option(str, "Optionaler DGSM-Template-Name", required=False, default=""),
):
    await safe_defer(ctx, ephemeral=True)
    try:
        report = await asyncio.to_thread(
            import_wgsm_plugin,
            source_url,
            PLUGIN_TEMPLATES_DIR,
            template_name=template_name or "",
            allow_local=False,
        )
        summary = format_import_summary(report)
        warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
        warning_text = ""
        if warnings:
            warning_text = "\n" + "\n".join(f"- {str(item)[:180]}" for item in warnings[:4])
        write_action_log("importwgsm", str(report.get("template_name", "-")), "success", summary)
        await safe_send(ctx, f"✅ WindowsGSM-Import abgeschlossen:\n`{summary}`{warning_text}", ephemeral=True)
    except Exception as exc:
        logging.exception("WindowsGSM plugin import failed")
        write_action_log("importwgsm", template_name or "-", "failed", str(exc))
        await safe_send(ctx, f"❌ WindowsGSM-Import fehlgeschlagen: {exc}", ephemeral=True)


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
    await safe_send(ctx, f"Starte Installation von **{name}**…", ephemeral=True)

    async def progress(label: str) -> None:
        if label == "start":
            await safe_send(ctx, f"Installation erfolgreich! ▶️ Starte **{name}**…", ephemeral=True)
        await _admin_progress(label)

    result = await server_service.add_server(name, template, instance_id or "", progress=progress)
    await _admin_reply(
        ctx, result,
        f"**{name}** aus Template '{template}' installiert und gestartet (Instanz: `{result.instance_id}`).",
    )


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
    if not key:
        return await safe_send(ctx, "ℹ️ Kein Eintrag – nichts geändert.", ephemeral=True)
    if value is None or value == "":
        return await safe_send(ctx, "❗ Bitte neuen Wert angeben.", ephemeral=True)
    import ast
    try:
        new_value = ast.literal_eval(value)
    except Exception:
        new_value = value
    if section == "config":
        result = await server_service.update_server_config(server, {key: new_value}, progress=_admin_progress)
    elif section == "settings":
        result = await server_service.update_server_settings(server, {key: new_value}, progress=_admin_progress)
    else:
        return await safe_send(ctx, "❌ Abschnitt muss config oder settings sein.", ephemeral=True)
    await _admin_reply(ctx, result, f"**{server}** {section}.{key} aktualisiert.")


@bot.slash_command(name="removeserver", description="Entfernt einen Server komplett")
@commands.has_role("Admin")
@commands.cooldown(1, 5, commands.BucketType.user)
async def remove_server(
    ctx: discord.ApplicationContext,
    name: Option(str, "Name des Servers", autocomplete=server_autocomplete),
    backup_before_delete: Option(bool, "Vor dem Löschen ein ZIP-Backup erstellen", default=True),
):
    await safe_defer(ctx, ephemeral=True)
    result = await server_service.remove_server(name, backup_before_delete, progress=_admin_progress)
    await _admin_reply(ctx, result)


@bot.slash_command(name="createbackup", description="Erstellt ein ZIP-Backup eines Servers")
@commands.has_role("Admin")
@commands.cooldown(1, 8, commands.BucketType.user)
async def create_backup(
    ctx: discord.ApplicationContext,
    name: Option(str, "Name des Servers", autocomplete=server_autocomplete),
):
    await safe_defer(ctx, ephemeral=True)
    result = await server_service.create_backup(name, progress=_admin_progress)
    await _admin_reply(ctx, result, f"Backup erstellt: `{server_service.backup_display(result.path)}`" if result.ok else "")


@bot.slash_command(name="restorebackup", description="Stellt ein ZIP-Backup eines Servers wieder her")
@commands.has_role("Admin")
@commands.cooldown(1, 8, commands.BucketType.user)
async def restore_backup(
    ctx: discord.ApplicationContext,
    name: Option(str, "Name des Servers", autocomplete=server_autocomplete),
    backup_file: Option(str, "Backup-Datei aus /steam/backup", autocomplete=backup_autocomplete),
    overwrite: Option(bool, "Zielordner nach vollständiger Backup-Prüfung ersetzen", default=False),
):
    await safe_defer(ctx, ephemeral=True)
    result = await server_service.restore_backup(name, backup_file, overwrite, progress=_admin_progress)
    await _admin_reply(ctx, result)


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


@bot.slash_command(name="cli", description="Optionale DGSM-CLI (Discord bleibt Hauptsteuerung)")
@commands.has_role("Admin")
@commands.cooldown(3, 6, commands.BucketType.user)
async def run_cli_command(
    ctx: discord.ApplicationContext,
    command: Option(str, "CLI-Kommando (help/list/status/start/stop/restart/update/refresh)"),
):
    await safe_defer(ctx, ephemeral=True)
    cmd = str(command or "").strip()
    if not cmd:
        return await safe_send(ctx, "❌ Bitte ein CLI-Kommando angeben.", ephemeral=True)

    actor = getattr(getattr(ctx, "author", None), "id", "unknown")
    result = await execute_cli_command(cmd, source=f"discord:{actor}")
    if result.refresh:
        await refresh_main_panel(user=getattr(ctx, "user", None))
    await safe_send(ctx, _render_cli_reply(cmd, result.message, result.ok), ephemeral=True)
