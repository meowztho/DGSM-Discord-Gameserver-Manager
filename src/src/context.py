import os
import sys
import logging
import socket
from dotenv import load_dotenv, find_dotenv
import discord
from discord.ext import commands


def _runtime_base_dir() -> str:
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates = [
            os.path.join(exe_dir, "src"),
            os.path.join(os.path.dirname(exe_dir), "src"),
            os.path.join(os.path.dirname(os.path.dirname(exe_dir)), "src"),
            os.path.join(os.getcwd(), "src"),
            exe_dir,
        ]
        for candidate in candidates:
            try:
                if os.path.isdir(candidate):
                    return os.path.abspath(candidate)
            except Exception:
                continue
        return os.path.abspath(exe_dir)
    return os.path.dirname(os.path.abspath(__file__))


_RUNTIME_BASE = _runtime_base_dir()
_ENV_PATH = os.path.join(_RUNTIME_BASE, ".env")

# 1) .env zuerst laden - bevorzugt aus Runtime-Base (src)
load_dotenv(_ENV_PATH if os.path.isfile(_ENV_PATH) else find_dotenv())

from security import ensure_env_values  # nach load_dotenv importieren!

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(_RUNTIME_BASE, "bot.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

def _safe_int(value, default: int = 0) -> int:
    try:
        txt = str(value or "").strip()
        return int(txt) if txt else default
    except Exception:
        return default


# Entschlüsselte ENV-Werte (ohne Setup-Prompt, damit UI-only Start möglich bleibt)
env = ensure_env_values(prompt_missing=False)
TOKEN = str(env.get("DISCORD_TOKEN", "") or "").strip()
CHANNEL = _safe_int(env.get("DISCORD_CHANNEL", ""), default=0)
DOMAIN = str(env.get("DOMAIN", "") or "").strip()
ADMIN_CHANNEL = _safe_int(env.get("ADMIN_CHANNEL", CHANNEL), default=CHANNEL)

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
    if not DOMAIN:
        return "Nicht konfiguriert"
    try:
        return socket.gethostbyname(DOMAIN)
    except Exception:
        return "Nicht erreichbar"

# Slash-Commands registrieren
import commands as _commands  # noqa: F401
