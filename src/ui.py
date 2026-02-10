import logging
import discord

from context import user_has_permission
from paths import SERVER_PATHS, load_server_paths, load_server_configs
from server_manager import server_processes, start_server, stop_server
from db import write_action_log
from config_store import get_config_value
from runtime_status import get_operation_status


# ---------- robust interaction helpers ----------
async def safe_inter_defer(inter: discord.Interaction, ephemeral: bool = True) -> bool:
    try:
        if inter.response.is_done():
            return True
        await inter.response.defer(ephemeral=ephemeral)
        return True
    except discord.NotFound:
        return False
    except Exception as e:
        logging.warning(f"safe_inter_defer: {e}")
        return False


async def safe_inter_send(inter: discord.Interaction, content: str, *, ephemeral: bool = True):
    try:
        if inter.response.is_done():
            return await inter.followup.send(content, ephemeral=ephemeral)
        else:
            return await inter.response.send_message(content, ephemeral=ephemeral)
    except discord.NotFound:
        # Fallback: normaler Channel-Post (nicht-ephemeral möglich)
        try:
            if ephemeral:
                logging.warning("safe_inter_send fallback: ephemeral content suppressed in public channel.")
                return await inter.channel.send(
                    "Antwort konnte nicht privat zugestellt werden. Bitte Aktion erneut ausführen."
                )
            return await inter.channel.send(content)
        except Exception as e:
            logging.error(f"safe_inter_send fallback failed: {e}")
            raise


# ---------- basic channel utils ----------
async def clean_channel(channel):
    try:
        await channel.purge(limit=100)
    except Exception as e:
        logging.error(f"Kanalbereinigung fehlgeschlagen: {e}")


async def get_server_status(name: str) -> str:
    state, detail = get_operation_status(name)
    if state == "busy":
        label = (detail or "").strip().lower()
        if label == "update":
            return "🟡 Update läuft"
        if label == "backup":
            return "🟡 Backup läuft"
        if label == "start":
            return "🟡 Start läuft"
        if label == "stop":
            return "🟡 Stopp läuft"
        if label == "restore":
            return "🟡 Restore läuft"
        return "🟡 Aktiv"
    if state == "failed":
        msg = " ".join((detail or "").split())
        if msg:
            if len(msg) > 72:
                msg = msg[:72].rstrip() + "..."
            return f"🔴 Fehler: {msg}"
        return "🔴 Fehler"

    try:
        proc = server_processes.get(name)
        if proc and proc.is_running():
            return "🟢 Läuft"
    except Exception:
        pass
    return "🚫 Gestoppt"


async def disable_all_buttons(message):
    try:
        view = discord.ui.View()
        for row in message.components:
            for comp in row.children:
                if comp.type == 2:
                    view.add_item(discord.ui.Button(label=comp.label, style=discord.ButtonStyle.gray, disabled=True, row=comp.row))
        await message.edit(view=view)
    except Exception as e:
        logging.error(f"Button-Deaktivierung fehlgeschlagen: {e}")


# ---------- buttons & view ----------
class RefreshButton(discord.ui.Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.blurple, label="🔄 Aktualisieren", row=0)

    async def callback(self, inter: discord.Interaction):
        await safe_inter_defer(inter, ephemeral=True)
        await clean_channel(inter.channel)
        from context import safe_get_ip
        await update_status_message(inter.channel, safe_get_ip(), user=inter.user)


class ServerButton(discord.ui.Button):
    def __init__(self, name, style, row, disabled=False):
        super().__init__(style=style, label=name, row=row, disabled=disabled)
        self.name = name

    async def callback(self, inter: discord.Interaction):
        if not user_has_permission(inter.user):
            write_action_log("access_denied", self.name, "failed", f"User:{inter.user}")
            return await safe_inter_send(inter, "❌ Keine Berechtigung!", ephemeral=True)
        await safe_inter_defer(inter, ephemeral=True)
        if self.name not in server_processes:
            ok = await start_server(self.name)
            write_action_log("start", self.name, "success" if ok else "failed")
            msg = f"✅ {self.name} gestartet!" if ok else "❌ Fehler!"
        else:
            ok = await stop_server(self.name)
            write_action_log("stop", self.name, "success" if ok else "failed")
            msg = f"✅ {self.name} gestoppt!" if ok else "❌ Fehler!"
        await safe_inter_send(inter, msg, ephemeral=True)
        await clean_channel(inter.channel)
        from context import safe_get_ip
        await update_status_message(inter.channel, safe_get_ip(), user=inter.user)


class UpdateButton(discord.ui.Button):
    def __init__(self, name, row, disabled=False):
        super().__init__(style=discord.ButtonStyle.gray, label=f"{name} - Update", row=row, disabled=disabled)
        self.name = name

    async def callback(self, inter: discord.Interaction):
        if not user_has_permission(inter.user):
            write_action_log("access_denied", self.name, "failed", f"User:{inter.user}")
            return await safe_inter_send(inter, "❌ Keine Berechtigung!", ephemeral=True)
        if self.name in server_processes:
            return await safe_inter_send(inter, "❌ Update nicht möglich: Server läuft!", ephemeral=True)
        await safe_inter_defer(inter, ephemeral=True)
        from steam_integration import run_update
        ok, _msg = await run_update(self.name)
        write_action_log("update", self.name, "success" if ok else "failed")
        await safe_inter_send(inter, f"✅ Update {'erfolgreich' if ok else 'fehlgeschlagen'} für {self.name}", ephemeral=True)
        await clean_channel(inter.channel)
        from context import safe_get_ip
        await update_status_message(inter.channel, safe_get_ip(), user=inter.user)


class ServerControlView(discord.ui.View):
    def __init__(self, access: bool):
        super().__init__(timeout=None)
        self.access = access

    async def create_buttons(self):
        self.add_item(RefreshButton())
        row = 1
        cnt = 0
        for n in SERVER_PATHS:
            style = discord.ButtonStyle.green if n not in server_processes else discord.ButtonStyle.red
            self.add_item(ServerButton(n, style, row=row, disabled=not self.access))
            cnt += 1
            if get_config_value(n, "app_id"):
                if cnt >= 5:
                    row += 1
                    cnt = 0
                self.add_item(UpdateButton(n, row=row, disabled=not self.access))
                cnt += 1
            if cnt >= 5:
                row += 1
                cnt = 0


# ---------- status message ----------
async def update_status_message(channel, ip_address, user=None):
    load_server_paths()
    load_server_configs()

    embed = discord.Embed(title="⚙️ Server Management", color=0x3498db)
    from context import DOMAIN
    embed.add_field(name="🌐 IP", value=f"{ip_address}\n ({DOMAIN})", inline=False)
    embed.add_field(name="ℹ️ Hinweis", value="Buttons nur für Admin/Player", inline=False)

    status_lines = []
    for n in SERVER_PATHS:
        status = await get_server_status(n)
        status_lines.append(f"• **{n}**: {status}")

    embed.add_field(name="📊 Status", value="\n".join(status_lines) or "Keine Server", inline=False)
    has_access = user_has_permission(user) if user else False
    view = ServerControlView(has_access)
    await view.create_buttons()

    await channel.send(embed=embed, view=view)
