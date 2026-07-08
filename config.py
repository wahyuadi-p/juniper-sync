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
    "port": int(os.getenv("MASTER_PORT", "22")),
}
BACKUP = {
    "host": os.getenv("BACKUP_HOST", ""),
    "username": os.getenv("BACKUP_USER", ""),
    "password": os.getenv("BACKUP_PASS", ""),
    "port": int(os.getenv("BACKUP_PORT", "22")),
}
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Cara commit-trigger (--watch / --auto) mengirim perubahan ke Backup:
#   "patch" (default) → kirim DELTA saja via `load patch` (show | compare
#                       rollback 1); fallback otomatis ke load merge full bila
#                       patch ditolak (Backup drift).
#   "merge"           → perilaku lama: kirim seluruh stanza logical-systems
#                       lalu `load merge` (aditif).
COMMIT_TRIGGER_MODE = os.getenv("COMMIT_TRIGGER_MODE", "patch").strip().lower()

# Cara mengirim config ke Backup:
#   "sftp"     (default) → upload file via SFTP lalu `load merge <file>`.
#                          Paling andal untuk config besar (tak ada korupsi
#                          paste). Butuh `set system services ssh sftp-server`
#                          aktif di Backup.
#   "terminal"           → paste isi config ke sesi SSH via `load merge
#                          terminal` (baris demi baris). Dipakai bila SFTP tak
#                          tersedia; tak butuh subsystem sftp-server.
TRANSFER_MODE = os.getenv("TRANSFER_MODE", "sftp").strip().lower()
