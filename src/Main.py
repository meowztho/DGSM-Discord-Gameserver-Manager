import asyncio
import logging
import signal
from datetime import time

from context import bot, CHANNEL, safe_get_ip
from db import init_db, cleanup_old_logs, write_action_log
from config_store import get_log_retention_days
from paths import load_server_paths, load_server_configs
from server_manager import recover_running_servers, monitor_servers

from discord.ext import tasks
from ui import clean_channel, update_status_message
from server_manager import graceful_stop_all


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
    try:
        init_db()
        cleanup_old_logs(get_log_retention_days())
        load_server_paths()
        load_server_configs()
        await recover_running_servers()
        asyncio.create_task(monitor_servers())
        ch = bot.get_channel(CHANNEL)
        if ch:
            await clean_channel(ch)
            await update_status_message(ch, safe_get_ip(), user=None)
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

if __name__ == "__main__":
    from context import TOKEN
    bot.run(TOKEN)
