import paramiko
import time
import subprocess
import hashlib
import sys
import os

from config import MASTER, BACKUP   # kredensial dari .env (bukan hardcoded)

# Windows: console default cp1252 → emoji bikin UnicodeEncodeError. Paksa UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------------------------------------------------------------------
# MODE: SYNC HANYA `logical-systems`
# Script mengambil HANYA stanza `logical-systems` dari Master, lalu di Backup
# mengganti stanza itu saja (delete + load merge). Base config Backup (host-name,
# interface manajemen, routing, dsb.) TIDAK disentuh — beda dari `load override`
# yang menimpa seluruh konfigurasi.
# ---------------------------------------------------------------------------


def ssh_command(device, command):
    """Execute an SSH command and return the output."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(device["host"], username=device["username"], password=device["password"], timeout=10)
        stdin, stdout, stderr = client.exec_command(command)
        output = stdout.read().decode("utf-8")
        client.close()
        return output
    except Exception as e:
        print(f"❌ ERROR: Failed to connect to {device['host']}: {e}")
        return None


def fetch_logical_systems(device):
    """Ambil HANYA stanza `logical-systems` dari perangkat.

    `show configuration logical-systems` menampilkan ISI di bawah logical-systems
    (tanpa pembungkus `logical-systems { }`), jadi hasilnya perlu dibungkus lagi
    agar bisa di-`load` kembali sebagai file konfigurasi.
    """
    out = ssh_command(device, "show configuration logical-systems | no-more")
    if out is None:
        return None
    return out.strip()


def validate_config(config):
    """Validate the configuration format (kurung buka/tutup seimbang)."""
    open_braces = config.count("{")
    close_braces = config.count("}")
    if open_braces != close_braces:
        print(f"❌ ERROR: Configuration braces are not properly closed! {open_braces} '{{' vs {close_braces} '}}'")
        return False
    return True


def sync_config():
    """Sinkronkan HANYA `logical-systems` dari Master ke Backup."""
    print("📥 Fetching `logical-systems` configuration from Master...")
    ls = fetch_logical_systems(MASTER)

    if ls is None:
        print("❌ Failed to fetch configuration from Master.")
        return
    if not ls:
        print("⚠️  Master tidak punya `logical-systems` — tidak ada yang disync. Dibatalkan.")
        return

    # Bungkus kembali isi stanza ke dalam `logical-systems { ... }` agar loadable.
    final_config = "logical-systems {\n" + ls + "\n}\n"

    # Validasi sebelum kirim ke Backup.
    if not validate_config(final_config):
        print("❌ Synchronization aborted due to incorrect configuration format.")
        return

    # Simpan ke file sementara.
    config_file = "final_config.txt"
    with open(config_file, "w") as file:
        file.write(final_config)

    print("✅ `logical-systems` config validated, sending to backup device...")

    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(BACKUP["host"], username=BACKUP["username"], password=BACKUP["password"], timeout=10)

        sftp = client.open_sftp()
        sftp.put(config_file, "/var/tmp/final_config.txt")
        sftp.close()

        print("📤 Configuration file successfully sent to Backup device.")

        # Verifikasi isi file di Backup sebelum diterapkan.
        print("🔍 Verifying file contents on Backup device...")
        remote_file_content = ssh_command(BACKUP, "cat /var/tmp/final_config.txt")
        print(f"📄 File contents:\n{remote_file_content}")

        # Terapkan HANYA stanza logical-systems:
        #   delete logical-systems  → hapus yang lama di Backup
        #   load merge <file>       → masukkan yang baru dari Master
        # Sisa konfigurasi Backup tetap utuh. commit confirmed 5 = jaring pengaman
        # (auto-rollback dalam 5 menit bila `commit` konfirmasi tak dijalankan).
        print("🛠 Applying `logical-systems` (delete + load merge)...")
        ssh_interactive(BACKUP, [
            "configure",
            "delete logical-systems",
            "load merge /var/tmp/final_config.txt",
            "show | compare",
            "commit confirmed 5",
            "commit",
            "exit"
        ])

        print("✅ `logical-systems` synchronization completed successfully!")
        # Jalankan notifikasi bila sukses.
        subprocess.run(["python", "notif.py"], check=True)
        client.close()
    except Exception as e:
        print(f"❌ ERROR: Failed to send configuration to Backup: {e}")


def ssh_interactive(device, commands):
    """Execute an interactive SSH session for Juniper."""
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(device["host"], username=device["username"], password=device["password"], timeout=10)
        channel = client.invoke_shell()

        for cmd in commands:
            channel.send(cmd + "\n")
            time.sleep(2)
            while channel.recv_ready():
                output = channel.recv(65535).decode("utf-8")
                print(f"Output [{cmd}]:\n{output}")

        channel.close()
        client.close()
    except Exception as e:
        print(f"❌ ERROR: Failed to run interactive session: {e}")


def get_latest_commit(device):
    """Ambil baris commit TERBARU dari `show system commit` (indeks 0).

    Dipakai mode --watch untuk mendeteksi commit baru di Master. Baris commit
    diawali indeks angka, mis. `0   2026-07-06 14:00:00 UTC by user via cli`.
    """
    out = ssh_command(device, "show system commit | no-more")
    if out is None:
        return None
    for line in out.splitlines():
        s = line.strip()
        if s and s[0].isdigit():
            return s
    return ""


def watch_and_sync(interval):
    """Pantau Master; begitu ada COMMIT BARU yang MENGUBAH logical-systems → sync.

    Tak menyentuh router (murni polling via SSH). Baseline saat start TIDAK
    langsung disync — hanya commit berikutnya yang memicu (biar tak kaget).
    """
    print(f"👀 Watch aktif — cek commit Master {MASTER['host']} tiap {interval} dtk. Ctrl+C untuk berhenti.")
    last_commit = get_latest_commit(MASTER)
    last_ls_hash = hashlib.sha256((fetch_logical_systems(MASTER) or "").encode()).hexdigest()
    print(f"   Baseline commit: {last_commit or '(gagal baca — cek koneksi)'}")

    while True:
        try:
            time.sleep(interval)
            cur = get_latest_commit(MASTER)
            if cur is None:
                print("⚠️  Gagal baca commit Master (koneksi?) — coba lagi nanti.")
                continue
            if cur == last_commit:
                continue                                   # belum ada commit baru
            print(f"🔔 Commit baru di Master: {cur}")
            last_commit = cur
            # Hanya sync bila logical-systems benar-benar berubah.
            ls = fetch_logical_systems(MASTER) or ""
            h = hashlib.sha256(ls.encode()).hexdigest()
            if h == last_ls_hash:
                print("   `logical-systems` tak berubah sejak sync terakhir → skip.")
                continue
            sync_config()
            last_ls_hash = h
        except KeyboardInterrupt:
            print("\n👋 Watch dihentikan.")
            break
        except Exception as e:
            print(f"⚠️  watch error: {e}")


def run_every(interval):
    """Jalankan sync_config() secara PERIODIK tiap `interval` detik (tanpa henti).

    Beda dari --watch: mode ini sync SETIAP siklus tanpa peduli ada commit baru
    atau tidak. Dipakai bila ingin sinkron terjadwal, mis. tiap 1 jam.
    """
    print(f"⏰ Sync periodik aktif — jalan tiap {interval} dtk. Ctrl+C untuk berhenti.")
    while True:
        try:
            sync_config()
            print(f"😴 Menunggu {interval} dtk sampai sync berikutnya...")
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n👋 Sync periodik dihentikan.")
            break
        except Exception as e:
            print(f"⚠️  periodic sync error: {e}")
            time.sleep(interval)


if __name__ == "__main__":
    # `python sync-juniper.py`             → sync sekali lalu keluar
    # `python sync-juniper.py --watch [N]` → pantau, sync otomatis tiap Master commit
    # `python sync-juniper.py --every [N]` → sync PERIODIK tiap N dtk (default 3600 = 1 jam)
    if len(sys.argv) > 1 and sys.argv[1] in ("--watch", "-w"):
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.getenv("WATCH_INTERVAL", "30"))
        watch_and_sync(interval)
    elif len(sys.argv) > 1 and sys.argv[1] in ("--every", "-e"):
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.getenv("SYNC_INTERVAL", "3600"))
        run_every(interval)
    else:
        sync_config()
