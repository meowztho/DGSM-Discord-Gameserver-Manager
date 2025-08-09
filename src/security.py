import os
import base64
import hashlib
from cryptography.fernet import Fernet

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")

SENSITIVE_KEYS = [
    "DISCORD_TOKEN",
    "DISCORD_CHANNEL",
    "DISCORD_GUILD",
    "DOMAIN",
    "ADMIN_CHANNEL",
]

def derive_key_from_phrase(phrase: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(phrase.encode()).digest())

def _read_key_from_envfile() -> str | None:
    """Liest ENCRYPTION_KEY direkt aus .env, falls nicht in os.environ gesetzt."""
    if not os.path.exists(ENV_PATH):
        return None
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("ENCRYPTION_KEY="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        return None
    return None

def _get_or_create_key() -> bytes:
    # 1) Umgebung (wird in context.py per load_dotenv geladen)
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        # 2) Fallback: direkt aus .env lesen
        key = _read_key_from_envfile()
    if not key:
        # 3) Erstinstallation – einmalig Passphrase abfragen
        phrase = os.getenv("SETUP_PASSPHRASE") or input("[SETUP] Sicherheits-Phrase: ").strip()
        key_bytes = derive_key_from_phrase(phrase)
        try:
            with open(ENV_PATH, "a", encoding="utf-8") as f:
                f.write(f"\nENCRYPTION_KEY={key_bytes.decode()}\n")
        except Exception:
            pass
        return key_bytes
    return key.encode()

_ENCRYPTION_KEY = _get_or_create_key()
fernet = Fernet(_ENCRYPTION_KEY)

def encrypt_value(value: str) -> str:
    return fernet.encrypt(value.encode()).decode()

def decrypt_value(value: str) -> str:
    return fernet.decrypt(value.encode()).decode()

def ensure_env_values():
    """
    Stellt sicher, dass alle sensiblen Keys verfügbar sind.
    - Klartext -> verschlüsselt in .env schreiben.
    - Bereits verschlüsselt (gAAAA...) -> entschlüsseln.
    - Keine unnötigen Prompts bei Neustarts.
    """
    env_data = {}
    # vorhandenes .env einlesen (für Fälle, in denen os.environ leer ist)
    existing = {}
    if os.path.exists(ENV_PATH):
        try:
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    existing[k.strip()] = v.strip()
        except Exception:
            pass

    for key in SENSITIVE_KEYS:
        val = os.getenv(key) or existing.get(key)
        if not val:
            new_val = input(f"[SETUP] {key}: ").strip()
            enc = encrypt_value(new_val)
            try:
                with open(ENV_PATH, "a", encoding="utf-8") as f:
                    f.write(f"\n{key}={enc}\n")
            except Exception:
                pass
            env_data[key] = new_val
            continue

        if str(val).startswith("gAAAA"):
            env_data[key] = decrypt_value(val)
        else:
            enc = encrypt_value(val)
            try:
                with open(ENV_PATH, "a", encoding="utf-8") as f:
                    f.write(f"\n{key}={enc}\n")
            except Exception:
                pass
            env_data[key] = val

    return env_data
