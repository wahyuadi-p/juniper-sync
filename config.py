"""Central config loader — reads credentials from environment / .env file.

Dipakai bersama oleh sync-juniper.py dan notif.py supaya TIDAK ada lagi
kredensial hardcoded di source code. Pakai python-dotenv bila tersedia;
kalau tidak, jatuh ke parser .env minimal (zero-dependency).
"""
import os

try:
    from dotenv import load_dotenv           # pip install python-dotenv
    load_dotenv()
except ImportError:
    # Fallback: parser .env sederhana (tanpa dependency tambahan).
    _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(_p):
        with open(_p, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

MASTER = {
    "host": os.getenv("MASTER_HOST", ""),
    "username": os.getenv("MASTER_USER", ""),
    "password": os.getenv("MASTER_PASS", ""),
}
BACKUP = {
    "host": os.getenv("BACKUP_HOST", ""),
    "username": os.getenv("BACKUP_USER", ""),
    "password": os.getenv("BACKUP_PASS", ""),
}
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
