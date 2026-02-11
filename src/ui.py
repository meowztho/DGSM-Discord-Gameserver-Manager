import logging
import asyncio
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
    except discord.HTTPException as e:
        if getattr(e, "code", None) == 40060:
            return True
        logging.warning(f"safe_inter_defer HTTPException: {e}")
        return False
    except Exception as e:
        logging.warning(f"safe_inter_defer: {e}")
        return False


async def safe_inter_send(inter: discord.Interaction, content: str, *, ephemeral: bool = True):
    message = content or ""

    async def _channel_fallback():
        try:
            if ephemeral:
                logging.warning("safe_inter_send fallback: ephemeral content suppressed in public channel.")
                return await inter.channel.send(
                    "Antwort konnte nicht privat zugestellt werden. Bitte Aktion erneut ausführen."
                )
            return await inter.channel.send(message)
        except Exception as e:
            logging.warning(f"safe_inter_send channel fallback failed: {e}")
            return None

    try:
        if inter.response.is_done():
            return await inter.followup.send(message, ephemeral=ephemeral)
        else:
            return await inter.response.send_message(message, ephemeral=ephemeral)
    except discord.NotFound:
        return await _channel_fallback()
    except discord.HTTPException as e:
        code = getattr(e, "code", None)
        if code == 40060:
            try:
                return await inter.followup.send(message, ephemeral=ephemeral)
            except Exception:
                return await _channel_fallback()
        if code == 10062:
            return await _channel_fallback()
        logging.warning(f"safe_inter_send HTTPException: {e}")
        return await _channel_fallback()
    except Exception as e:
        logging.warning(f"safe_inter_send failed: {e}")
        return await _channel_fallback()


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


async def refresh_status_panel(channel, user=None):
    from context import safe_get_ip
    await clean_channel(channel)
    await update_status_message(channel, safe_get_ip(), user=user)


def _short_message(msg: str, limit: int = 320) -> str:
    text = " ".join(str(msg or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


# ---------- buttons & view ----------
class RefreshButton(discord.ui.Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.blurple, label="🔄 Aktualisieren", row=0)

    async def callback(self, inter: discord.Interaction):
        await safe_inter_defer(inter, ephemeral=True)
        await refresh_status_panel(inter.channel, user=inter.user)


class ServerButton(discord.ui.Button):
    def __init__(self, name, style, row, disabled=False):
        super().__init__(style=style, label=name, row=row, disabled=disabled)
        self.name = name

    async def callback(self, inter: discord.Interaction):
        if not user_has_permission(inter.user):
            write_action_log("access_denied", self.name, "failed", f"User:{inter.user}")
            return await safe_inter_send(inter, "❌ Keine Berechtigung!", ephemeral=True)
        await safe_inter_defer(inter, ephemeral=True)
        try:
            if inter.message:
                await disable_all_buttons(inter.message)
        except Exception:
            pass

        running = False
        try:
            proc = server_processes.get(self.name)
            running = bool(proc and proc.is_running())
        except Exception:
            running = False

        action = "stop" if running else "start"
        action_task = asyncio.create_task(stop_server(self.name) if running else start_server(self.name))

        # UI früh auf "busy" stellen, damit "🟡 Start läuft/🟡 Stopp läuft" sichtbar ist.
        for _ in range(20):
            state, _detail = get_operation_status(self.name)
            if state == "busy" or action_task.done():
                break
            await asyncio.sleep(0.05)
        await refresh_status_panel(inter.channel, user=inter.user)

        ok = await action_task
        write_action_log(action, self.name, "success" if ok else "failed")
        if action == "start":
            msg = f"✅ {self.name} gestartet!" if ok else "❌ Fehler beim Start!"
        else:
            msg = f"✅ {self.name} gestoppt!" if ok else "❌ Fehler beim Stop!"

        await safe_inter_send(inter, msg, ephemeral=True)
        await refresh_status_panel(inter.channel, user=inter.user)


class UpdateButton(discord.ui.Button):
    def __init__(self, name, row, disabled=False):
        super().__init__(style=discord.ButtonStyle.gray, label=f"{name} - Update", row=row, disabled=disabled)
        self.name = name

    async def callback(self, inter: discord.Interaction):
        if not user_has_permission(inter.user):
            write_action_log("access_denied", self.name, "failed", f"User:{inter.user}")
            return await safe_inter_send(inter, "❌ Keine Berechtigung!", ephemeral=True)
        await safe_inter_defer(inter, ephemeral=True)
        proc = server_processes.get(self.name)
        try:
            if proc and proc.is_running():
                await refresh_status_panel(inter.channel, user=inter.user)
                return await safe_inter_send(inter, "❌ Update nicht möglich: Server läuft!", ephemeral=True)
        except Exception:
            pass
        state, _detail = get_operation_status(self.name)
        if state == "busy":
            await refresh_status_panel(inter.channel, user=inter.user)
            return await safe_inter_send(inter, f"ℹ️ Für {self.name} läuft bereits eine Aktion.", ephemeral=True)

        from steam_integration import run_update

        try:
            if inter.message:
                await disable_all_buttons(inter.message)
        except Exception:
            pass

        update_task = asyncio.create_task(run_update(self.name))
        for _ in range(20):
            state, _detail = get_operation_status(self.name)
            if state == "busy" or update_task.done():
                break
            await asyncio.sleep(0.05)

        await refresh_status_panel(inter.channel, user=inter.user)

        ok, msg = await update_task
        detail = _short_message(msg)
        write_action_log("update", self.name, "success" if ok else "failed", detail)

        if ok:
            response = f"✅ Update erfolgreich für {self.name}: {detail}"
        elif "läuft bereits" in (detail or "").lower():
            response = f"ℹ️ {self.name}: {detail}"
        else:
            response = f"❌ Update fehlgeschlagen für {self.name}: {detail or 'Unbekannter Fehler'}"

        await safe_inter_send(inter, response, ephemeral=True)
        await refresh_status_panel(inter.channel, user=inter.user)
        return


class ServerControlView(discord.ui.View):
    def __init__(self, access: bool):
        super().__init__(timeout=None)
        self.access = access

    async def create_buttons(self):
        self.add_item(RefreshButton())
        row = 1
        cnt = 0
        for n in SERVER_PATHS:
            running = False
            try:
                proc = server_processes.get(n)
                running = bool(proc and proc.is_running())
            except Exception:
                running = False

            state, _detail = get_operation_status(n)
            busy = state == "busy"

            style = discord.ButtonStyle.green if not running else discord.ButtonStyle.red
            if busy:
                style = discord.ButtonStyle.gray

            button_disabled = (not self.access) or busy
            self.add_item(ServerButton(n, style, row=row, disabled=button_disabled))
            cnt += 1
            if get_config_value(n, "app_id"):
                if cnt >= 5:
                    row += 1
                    cnt = 0
                update_disabled = (not self.access) or busy or running
                self.add_item(UpdateButton(n, row=row, disabled=update_disabled))
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
