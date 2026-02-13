import asyncio
import logging
import signal
from datetime import time

from context import bot, CHANNEL, TOKEN, safe_get_ip
from db import init_db, cleanup_old_logs, write_action_log
from config_store import get_log_retention_days
from paths import load_server_paths, load_server_configs
from server_manager import recover_running_servers, monitor_servers
from steam_integration import ensure_steamcmd_available

from discord.ext import tasks
from ui import clean_channel, update_status_message, refresh_status_panel
from server_manager import graceful_stop_all
from desktop_ui import start_desktop_ui, is_desktop_ui_active, is_desktop_ui_started


_monitor_task = None
_desktop_ui_started = False


@tasks.loop(time=time(hour=6, minute=0))
async def daily_update():
    ch = bot.get_channel(CHANNEL)
    if not ch:
        return
    try:
        await clean_channel(ch)
        await update_status_message(ch, safe_get_ip())
    except Exception as e:
        logging.error(f"Tägliches Update fehlgeschlagen: {e}")


@bot.event
async def on_ready():
    global _monitor_task, _desktop_ui_started
    try:
        ok_cmd, cmd_message = await asyncio.to_thread(ensure_steamcmd_available)
        if ok_cmd:
            logging.info("[STEAMCMD] %s", cmd_message)
        else:
            logging.warning("[STEAMCMD] %s", cmd_message)
        init_db()
        cleanup_old_logs(get_log_retention_days())
        load_server_paths()
        load_server_configs()
        await recover_running_servers()
        if _monitor_task is None or _monitor_task.done():
            _monitor_task = asyncio.create_task(monitor_servers())
        ch = bot.get_channel(CHANNEL)
        if ch:
            await clean_channel(ch)
            await update_status_message(ch, safe_get_ip(), user=None)
        if not _desktop_ui_started:
            async def _refresh_discord_from_desktop():
                channel = bot.get_channel(CHANNEL)
                if channel:
                    await refresh_status_panel(channel, user=None)

            started = start_desktop_ui(
                asyncio.get_running_loop(),
                refresh_callback=_refresh_discord_from_desktop,
            )
            _desktop_ui_started = bool(started)
        if not daily_update.is_running():
            daily_update.start()
        write_action_log("bot_start", "system", "success")
        logging.info(f"{bot.user} ist online!")
    except Exception as e:
        logging.critical(f"Kritischer Startfehler: {e}")


def _handle_shutdown(*_):
    logging.info("Graceful Shutdown initiiert…")
    asyncio.get_event_loop().create_task(graceful_stop_all())


signal.signal(signal.SIGINT, _handle_shutdown)
signal.signal(signal.SIGTERM, _handle_shutdown)


async def _run_ui_only() -> None:
    global _desktop_ui_started
    ok_cmd, cmd_message = await asyncio.to_thread(ensure_steamcmd_available)
    if ok_cmd:
        logging.info("[STEAMCMD] %s", cmd_message)
    else:
        logging.warning("[STEAMCMD] %s", cmd_message)

    init_db()
    cleanup_old_logs(get_log_retention_days())
    load_server_paths()
    load_server_configs()

    if not _desktop_ui_started:
        started = start_desktop_ui(asyncio.get_running_loop(), refresh_callback=None)
        _desktop_ui_started = bool(started)

    if not _desktop_ui_started:
        logging.error("[DESKTOP-UI] konnte nicht gestartet werden.")
        return

    # Avoid startup race: UI thread may need a moment until the first visible window exists.
    for _ in range(160):  # ~8s max
        if is_desktop_ui_active():
            break
        if not is_desktop_ui_started():
            break
        await asyncio.sleep(0.05)

    logging.warning("[BOOT] Discord ENV unvollständig. Starte im UI-only Modus.")
    while is_desktop_ui_started():
        await asyncio.sleep(0.4)


if __name__ == "__main__":
    if TOKEN and CHANNEL > 0:
        bot.run(TOKEN)
    else:
        try:
            asyncio.run(_run_ui_only())
        except KeyboardInterrupt:
            logging.info("Beendet.")
