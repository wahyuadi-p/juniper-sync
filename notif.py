import requests
import time

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   # dari .env (bukan hardcoded)

def send_telegram_message(message):
    """Send a notification to Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, data=payload, timeout=10)
    except requests.exceptions.RequestException as e:
        # Server tanpa rute internet / Telegram diblok → jangan lempar traceback.
        # Notifikasi gagal bukan kegagalan sync; cukup warning.
        print(f"⚠️  Tidak bisa kirim Telegram (jaringan?): {e}")
        return
    if response.status_code == 200:
        print("✅ Notification sent to Telegram!")
    else:
        print(f"❌ Failed to send to Telegram: {response.text}")

if __name__ == "__main__":
    current_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    message = (
        "🔔 *Juniper Synchronization Notification*\n"
        "✅ Status: *Successful*\n"
        "🔄 `logical-systems` sync Master → Backup complete!\n"
        f"📅 Time: {current_time}"
    )
    send_telegram_message(message)
