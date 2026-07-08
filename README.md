# Juniper Sync

> ⚠️ **This project is currently under active development and intended for testing purposes only.**

Automatically synchronize configuration from a **Master** Juniper device to a **Backup** Juniper device. The script fetches the running configuration from the Master, filters out specific sections (such as hostname and management interface), merges it with a mandatory base configuration for the Backup device, and applies it using `load override`. A Telegram notification is sent upon successful synchronization.

## How It Works

```
┌────────────┐       SSH        ┌─────────────┐
│   Master   │ ──────────────── │  This Script │
│ (Juniper)  │  Fetch config    │  (Python)    │
└────────────┘                  └──────┬───────┘
                                       │
                          Filter config │ (remove hostname,
                          ge-0/0/0)     │ merge mandatory config
                                       │
                                       ▼
                                ┌─────────────┐       Telegram
                                │   Backup    │ ──────────────── 📩 Notification
                                │  (Juniper)  │  load override
                                └─────────────┘
```

### Synchronization Flow

1. **Fetch** — Retrieve running configuration from the Master device via SSH.
2. **Filter** — Remove `host-name` and `ge-0/0/0` interface block from the fetched configuration.
3. **Merge** — Combine the filtered configuration with a mandatory base configuration (hostname, management interface, static route, etc.) specific to the Backup device.
4. **Validate** — Check that the final configuration has properly matched braces before applying.
5. **Transfer** — Upload the final configuration file to the Backup device via SFTP.
6. **Apply** — Execute `load override` and `commit confirmed` on the Backup device through an interactive SSH session.
7. **Notify** — Send a Telegram notification upon successful synchronization.

## Project Structure

```
juniper-sync/
├── sync-juniper.py     # Main synchronization script
├── notif.py            # Telegram notification module
└── requirements.txt    # Python dependencies
```

## Prerequisites

- **Python** 3.8 or higher
- **Network access** to both Master and Backup Juniper devices via SSH (port 22)
- **Telegram Bot Token** and **Chat ID** for notifications
- Both Juniper devices must have SSH enabled

## Installation

1. **Clone the repository**

   ```bash
   git clone https://github.com/your-username/juniper-sync.git
   cd juniper-sync
   ```

2. **Create a virtual environment** (optional)

   ```bash
   python -m venv venv
   source venv/bin/activate
   ```

3. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

## Configuration

Before running the script, update the following variables directly in the source files:

### `sync-juniper.py`

| Variable | Description |
|---|---|
| `MASTER["host"]` | IP address of the Master Juniper device |
| `MASTER["username"]` | SSH username for the Master device |
| `MASTER["password"]` | SSH password for the Master device |
| `BACKUP["host"]` | IP address of the Backup Juniper device |
| `BACKUP["username"]` | SSH username for the Backup device |
| `BACKUP["password"]` | SSH password for the Backup device |
| `MANDATORY_CONFIG` | Base configuration to always apply on the Backup device (hostname, management interface, static route, syslog, etc.) |

### `notif.py`

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | Target Telegram chat ID for notifications |

## Usage

Run the synchronization script once:

```bash
python sync-juniper.py
```

### Sync periodik (tiap 1 jam)

Mode **every**: skrip menjalankan sync **secara terjadwal** tiap N detik tanpa
peduli ada commit baru atau tidak. Default 3600 detik = 1 jam:

```bash
python sync-juniper.py --every          # sync tiap 3600 dtk (1 jam, default)
python sync-juniper.py --every 1800     # sync tiap 30 menit
# atau atur lewat .env: SYNC_INTERVAL=3600
```

Hentikan dengan `Ctrl+C`.

### Auto-sync saat Master commit

Mode **watch**: skrip memantau Master dan otomatis sync begitu ada **commit baru
yang mengubah `logical-systems`** (tanpa perlu ubah konfigurasi router):

```bash
python sync-juniper.py --watch          # cek tiap 30 dtk (default)
python sync-juniper.py --watch 10       # cek tiap 10 dtk
# atau atur lewat .env: WATCH_INTERVAL=15
```

Cara kerja: tiap interval, skrip baca `show system commit`. Bila indeks commit
teratas berubah **dan** hash `logical-systems` berbeda dari sync terakhir →
jalankan sync. Baseline saat start tidak langsung disync (hanya commit
berikutnya yang memicu). Hentikan dengan `Ctrl+C`.

#### Commit-trigger mengirim DELTA saja (`load patch`)

Saat commit-trigger (`--watch` / `--auto`) mendeteksi perubahan, yang dikirim ke
Backup **hanya baris yang berubah** — bukan seluruh stanza `logical-systems`.
Skrip mengambil delta dari Master via `show configuration logical-systems |
compare rollback 1` (format patch, header absolut) lalu menerapkannya di Backup
dengan `load patch`.

Bila patch **ditolak** (konteks Backup sudah _drift_ dari Master), skrip otomatis
**fallback** ke `load merge` full (kirim seluruh stanza, aditif) yang lebih tahan
drift. Diatur lewat `.env`:

```bash
COMMIT_TRIGGER_MODE=patch   # default: kirim delta via load patch (+fallback merge)
COMMIT_TRIGGER_MODE=merge   # perilaku lama: kirim seluruh stanza + load merge
```

> Catatan: `rollback 1` hanya menangkap **1 commit terakhir**. Bila ada 2+ commit
> di antara dua polling, sebagian delta bisa terlewat di jalur patch — inilah
> gunanya **jadwal harian full sync** (`--auto`) sebagai jaring pengaman.

### Auto-sync gabungan (commit-trigger + jadwal harian jam tetap)

Mode **auto**: gabungan mode **watch** di atas dengan jadwal sync harian di jam
tetap (default tengah malam `00:00`) sebagai jaring pengaman — jadi tetap ada
sync penuh sekali sehari meskipun tidak ada commit baru yang terdeteksi:

```bash
python sync-juniper.py --auto           # cek commit tiap 30 dtk (default) + sync jam 00:00
python sync-juniper.py --auto 10        # cek commit tiap 10 dtk + sync jam 00:00
# atau atur lewat .env: WATCH_INTERVAL=15
```

Jam sync harian diatur lewat `.env` (bisa diubah manual kapan saja, tanpa ubah kode):

```bash
DAILY_SYNC_TIME=00:00   # format HH:MM, 24 jam. Contoh lain: DAILY_SYNC_TIME=23:30
```

Cara kerja: tiap interval, skrip cek dua hal — (1) apakah jam sistem sudah
melewati `DAILY_SYNC_TIME` dan belum sync di hari itu → jalankan full sync;
kalau belum, (2) baru cek commit baru seperti mode `--watch`. Hentikan dengan
`Ctrl+C`.

**Alternatif real-time (butuh setelan di router):**
- **`event-options`** — event policy di Master pada event `UI_COMMIT_COMPLETED` menjalankan op-script sync.
- **Syslog** — Master kirim syslog ke host; listener memicu sync saat lihat `UI_COMMIT_COMPLETED`.
- **`transfer-on-commit`** — `set system archival configuration transfer-on-commit` mengunggah config tiap commit; file-watcher di host memicu sync.

### Expected Output

```
📥 Fetching configuration from Master...
✅ Configuration validated successfully, sending to backup device...
📤 Configuration file successfully sent to Backup device.
🔍 Verifying file contents on Backup device...
🛠 Applying configuration with `load override`...
✅ Synchronization completed successfully!
✅ Notification sent to Telegram!
```

## Filtered Configuration

The script automatically **removes** the following sections from the Master configuration before applying to the Backup device:

| Section | Reason |
|---|---|
| `host-name` | The Backup device must retain its own hostname |
| `ge-0/0/0` interface block | Management interface must remain unique per device |

These are then replaced by the `MANDATORY_CONFIG` block defined in the script.

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `paramiko` | 3.5.0 | SSH and SFTP connectivity to Juniper devices |
| `requests` | 2.33.1 | Sending Telegram API notifications |

## ⚠️ Known Limitations

- Credentials are currently **hardcoded** in the script (environment variables or a secrets manager are planned for future versions).
- The `MANDATORY_CONFIG` block is **static** and must be manually updated if the Backup device configuration changes.
- No **rollback mechanism** beyond Juniper's built-in `commit confirmed` timeout (5 minutes).
- No **logging to file** — all output is printed to stdout.

## License

This project is licensed under the [MIT License](LICENSE).
