import sqlite3
from datetime import datetime, timedelta
from config_store import DB_PATH

_DB = None


def init_db():
    global _DB
    _DB = sqlite3.connect(DB_PATH)
    c = _DB.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            action TEXT,
            server TEXT,
            status TEXT,
            details TEXT
        )
        """
    )
    _DB.commit()


def write_action_log(action: str, server: str, status: str, details: str = ""):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c = _DB.cursor()
    c.execute(
        "INSERT INTO logs (timestamp, action, server, status, details) VALUES (?, ?, ?, ?, ?)",
        (ts, action, server, status, details),
    )
    _DB.commit()


def cleanup_old_logs(days: int):
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    c = _DB.cursor()
    c.execute("DELETE FROM logs WHERE timestamp < ?", (cutoff,))
    _DB.commit()
