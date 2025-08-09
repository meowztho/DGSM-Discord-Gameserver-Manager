import os
import sys
import logging
import socket
from dotenv import load_dotenv, find_dotenv
import discord
from discord.ext import commands

# 1) .env zuerst laden – damit security.py beim Import bereits ENCRYPTION_KEY sieht
load_dotenv(find_dotenv())

from security import ensure_env_values  # nach load_dotenv importieren!

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

# Entschlüsselte ENV-Werte
env = ensure_env_values()
TOKEN = env["DISCORD_TOKEN"]
CHANNEL = int(env["DISCORD_CHANNEL"])
DOMAIN = env["DOMAIN"]
ADMIN_CHANNEL = int(env.get("ADMIN_CHANNEL", env["DISCORD_CHANNEL"]))

# Discord Bot
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

ALLOWED_ROLES = ["Admin", "Player"]

def user_has_permission(user) -> bool:
    try:
        return any(role.name in ALLOWED_ROLES for role in getattr(user, "roles", []))
    except Exception:
        return False

def safe_get_ip() -> str:
    try:
        return socket.gethostbyname(DOMAIN)
    except Exception:
        return "Nicht erreichbar"

# Slash-Commands registrieren
import commands as _commands  # noqa: F401
