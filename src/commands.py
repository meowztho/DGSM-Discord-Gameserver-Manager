import os
import json
import shlex
import logging
import asyncio
from typing import Optional, List

import discord
from discord.commands import Option
from discord import OptionChoice
from discord.ext import commands

from context import bot
from security import encrypt_value
from config_store import save_config, load_config, PLUGIN_TEMPLATES_DIR
from config_store import get_config_value
from paths import load_server_paths, load_server_configs, SERVER_PATHS
from db import write_action_log
from ui import update_status_message, clean_channel
from server_manager import start_server, stop_server, server_processes
from steam_integration import run_update, run_update_with_credentials
from context import CHANNEL, safe_get_ip


# ---------- helper: robust defer & reply ----------
async def safe_defer(ctx: discord.ApplicationContext, ephemeral: bool = True) -> bool:
    """Versucht zu deferren, gibt False zurück wenn das Interaction-Token schon ungültig ist."""
    try:
        if getattr(ctx, "responded", False):
            return True
        await ctx.defer(ephemeral=ephemeral)
        return True
    except discord.NotFound:
        # Unknown interaction – Token bereits ungültig
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
    try:
        if getattr(ctx, "responded", False):
            return await ctx.followup.send(content or "", embed=embed, ephemeral=ephemeral)
        else:
            return await ctx.respond(content or "", embed=embed, ephemeral=ephemeral)
    except discord.NotFound:
        # Interaction-Token ungültig → Fallback auf normalen Channel-Post
        ch = ctx.channel
        if ch:
            return await ch.send(content or "", embed=embed)
        raise
    except Exception as e:
        logging.exception(f"safe_send failed: {e}")
        raise


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


async def section_autocomplete(ctx: discord.AutocompleteContext) -> List[OptionChoice]:
    return [OptionChoice("config"), OptionChoice("settings")]


async def key_autocomplete(ctx: discord.AutocompleteContext) -> List[OptionChoice]:
    current = ctx.value
    name = ctx.options.get("server")
    section = ctx.options.get("section")
    if not name or not section:
        return []
    all_config_keys = ["app_id", "executable", "username", "password", "auto_update", "auto_restart", "stop_time", "restart_after_stop"]
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

        app_id = tcfg["app_id"]
        executable = tcfg["executable"]
        server_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "steam", "GSM", "servers", str(app_id))
        server_dir = os.path.join(server_root, "serverfiles")
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

        cfg = load_config()
        entry = {"app_id": app_id, "executable": executable}
        if tcfg.get("username") and tcfg.get("password"):
            entry["username"] = tcfg["username"]
            entry["password"] = encrypt_value(tcfg["password"])
        cfg["server_paths"][name] = entry
        save_config(cfg)

        load_server_paths()
        load_server_configs()

        await safe_send(ctx, f"📥 Starte Installation von **{name}**…", ephemeral=True)
        ok, message = await run_update(name)
        if not ok:
            # try anonymous fallback
            if "No subscription" in message or "besitzt keine Lizenz" in message:
                if "username" in cfg["server_paths"][name]:
                    cfg["server_paths"][name].pop("username", None)
                    cfg["server_paths"][name].pop("password", None)
                    save_config(cfg)
                ok, message = await run_update(name)
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
            return

        await safe_send(ctx, f"✅ Installation erfolgreich! ▶️ Starte **{name}**…", ephemeral=True)
        if not await start_server(name):
            write_action_log("start_after_add", name, "failed")
            return await safe_send(ctx, "❌ Start fehlgeschlagen!", ephemeral=True)

        write_action_log("start_after_add", name, "success")
        ch = bot.get_channel(CHANNEL)
        if ch:
            await clean_channel(ch)
            await update_status_message(ch, safe_get_ip())
        await safe_send(ctx, f"🎉 **{name}** aus Template '{template}' installiert und gestartet!", ephemeral=True)

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
async def remove_server(ctx: discord.ApplicationContext, name: Option(str, "Name des Servers", autocomplete=server_autocomplete)):
    await safe_defer(ctx, ephemeral=True)
    try:
        cfg = load_config()
        if name not in cfg["server_paths"]:
            return await safe_send(ctx, "❌ Server nicht gefunden!", ephemeral=True)
        if name in server_processes:
            await stop_server(name)
            await safe_send(ctx, f"⏹️ Server **{name}** gestoppt", ephemeral=True)
        app_id = cfg["server_paths"][name]["app_id"]
        server_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "steam", "GSM", "servers", str(app_id))
        if os.path.exists(server_root):
            import shutil
            shutil.rmtree(server_root)
            await safe_send(ctx, f"🗑️ Server-Verzeichnis gelöscht: `{server_root}`", ephemeral=True)
        del cfg["server_paths"][name]
        save_config(cfg)
        load_server_paths()
        load_server_configs()
        ch = bot.get_channel(CHANNEL)
        if ch:
            await clean_channel(ch)
            await update_status_message(ch, safe_get_ip())
        await safe_send(ctx, f"✅ Server **{name}** komplett entfernt.", ephemeral=True)
    except Exception as e:
        await safe_send(ctx, f"❌ Fehler beim Entfernen: {e}", ephemeral=True)


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
