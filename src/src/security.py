import os
import sys
import base64
import hashlib
from cryptography.fernet import Fernet

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

BASE_DIR = _runtime_base_dir()
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
        # 3) Erstinstallation ohne Prompt: zufaelligen Fernet-Key erzeugen
        key_bytes = Fernet.generate_key()
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

def ensure_env_values(prompt_missing: bool = False):
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
            if prompt_missing:
                new_val = input(f"[SETUP] {key}: ").strip()
                enc = encrypt_value(new_val)
                try:
                    with open(ENV_PATH, "a", encoding="utf-8") as f:
                        f.write(f"\n{key}={enc}\n")
                except Exception:
                    pass
                env_data[key] = new_val
            else:
                env_data[key] = ""
            continue

        if str(val).startswith("gAAAA"):
            try:
                env_data[key] = decrypt_value(val)
            except Exception:
                env_data[key] = ""
        else:
            try:
                enc = encrypt_value(val)
                with open(ENV_PATH, "a", encoding="utf-8") as f:
                    f.write(f"\n{key}={enc}\n")
            except Exception:
                pass
            env_data[key] = val

    # Fehlende Keys immer bereitstellen, damit der Aufrufer robust bleibt.
    for key in SENSITIVE_KEYS:
        env_data.setdefault(key, "")

    return env_data
