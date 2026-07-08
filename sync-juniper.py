import paramiko
import time
import subprocess
import hashlib
import sys
import os
from datetime import datetime

from config import MASTER, BACKUP, COMMIT_TRIGGER_MODE, TRANSFER_MODE   # kredensial & opsi dari .env (bukan hardcoded)

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
        client.connect(device["host"], port=device.get("port", 22), username=device["username"], password=device["password"], timeout=10)
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


def fetch_logical_systems_patch(device):
    """Ambil DELTA `logical-systems` dari commit TERAKHIR sebagai patch.

    `show configuration logical-systems | compare rollback 1` menghasilkan diff
    format patch dengan header ABSOLUT (mis. `[edit logical-systems BGP-BGP
    interfaces]`) — bisa langsung dimakan `load patch` di Backup, sehingga yang
    dikirim & diterapkan HANYA baris yang berubah (bukan seluruh stanza).

    Batasan: `rollback 1` hanya menangkap 1 commit terakhir. Bila ada 2+ commit
    di antara dua polling, delta commit sebelumnya bisa terlewat — dikoreksi
    oleh jadwal harian full sync (`--auto`).

    Return:
        str  → blok patch (di-strip)
        ""   → commit terakhir tak menyentuh logical-systems (tak ada delta)
        None → gagal koneksi ke Master
    """
    out = ssh_command(device, "show configuration logical-systems | compare rollback 1 | no-more")
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


def sync_config(full=False):
    """Sinkronkan HANYA `logical-systems` dari Master ke Backup.

    full=False (default, dipakai commit-trigger):
        HANYA `load merge` → aditif. Perubahan & penambahan dari Master masuk,
        tapi logical-system yang dihapus di Master TIDAK ikut hilang di Backup.
    full=True (dipakai jadwal harian 00:00):
        `delete logical-systems` + `load merge` → timpa penuh. Backup jadi
        cerminan persis `logical-systems` Master (penghapusan ikut tercermin).
    """
    mode = "delete + load merge (full)" if full else "load merge (aditif)"
    print(f"📥 Fetching `logical-systems` configuration from Master... [mode: {mode}]")
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

    # Simpan salinan lokal (audit/debug). TIDAK dikirim ke Backup — pengiriman
    # kini via `load merge terminal` (paste langsung), bukan transfer file.
    with open("final_config.txt", "w") as file:
        file.write(final_config)

    print("✅ `logical-systems` config validated, sending to backup device...")

    # Kirim & terapkan HANYA stanza logical-systems (via SFTP/terminal sesuai
    # TRANSFER_MODE). Sisa konfigurasi Backup (host-name, interface, routing,
    # dsb.) SELALU tetap utuh.
    #   full=True  → `delete logical-systems` + load merge (timpa penuh)
    #   full=False → load merge saja (aditif; hapusan di Master tak ikut)
    # commit confirmed 5 = jaring pengaman (auto-rollback dalam 5 menit bila
    # `commit` konfirmasi tak dijalankan).
    pre_cmds = ["delete logical-systems"] if full else []
    print(f"🛠 Applying `logical-systems` ({mode}) via {TRANSFER_MODE}...")
    if push_config(BACKUP, "merge", final_config, pre_cmds):
        print("✅ `logical-systems` synchronization completed successfully!")
        # Jalankan notifikasi bila sukses.
        subprocess.run(["python3", "notif.py"], check=True)
    else:
        print("❌ ERROR: Failed to apply configuration to Backup.")


def sync_config_commit_trigger():
    """Sync commit-trigger: kirim DELTA saja via `load patch`, fallback ke merge.

    Dipakai oleh --watch dan --auto saat mendeteksi commit baru. Hanya baris yang
    berubah (delta commit terakhir) yang dikirim & diterapkan ke Backup. Bila
    `load patch` ditolak (Backup drift) → fallback ke `sync_config(full=False)`
    (kirim seluruh stanza + `load merge`, aditif) yang lebih tahan drift.

    Bila COMMIT_TRIGGER_MODE != "patch", langsung pakai perilaku lama (merge).
    """
    if COMMIT_TRIGGER_MODE != "patch":
        print(f"⚙️  COMMIT_TRIGGER_MODE={COMMIT_TRIGGER_MODE} → pakai load merge (perilaku lama).")
        sync_config(full=False)
        return

    print("📥 Fetching DELTA `logical-systems` from Master... [mode: load patch (delta)]")
    patch = fetch_logical_systems_patch(MASTER)

    if patch is None:
        print("❌ Failed to fetch delta from Master.")
        return
    if not patch:
        print("⚠️  Commit terakhir tak menyentuh `logical-systems` — tak ada delta. Skip.")
        return

    print(f"✅ Delta patch siap ({len(patch.splitlines())} baris), mengirim ke Backup via {TRANSFER_MODE}...")

    # Kirim delta via `load patch` (SFTP/terminal sesuai TRANSFER_MODE).
    print("🛠 Applying delta via `load patch`...")
    if push_config(BACKUP, "patch", patch):
        print("✅ Delta `logical-systems` synchronization completed successfully!")
        subprocess.run(["python3", "notif.py"], check=True)
    else:
        print("↩️  Patch ditolak (Backup drift?) — fallback ke `load merge` full (aditif)...")
        sync_config(full=False)


def push_config(device, load_kind, content, pre_cmds=None, remote_path="/var/tmp/juniper_sync_load.txt"):
    """Kirim & terapkan config ke perangkat, via SFTP atau terminal (TRANSFER_MODE).

    load_kind : "merge" atau "patch".
    content   : teks config/patch yang akan di-load.
    pre_cmds  : perintah configure sebelum load (mis. ["delete logical-systems"]).
    remote_path: path file di perangkat saat mode SFTP.

    Mode transfer:
      sftp     → upload file via SFTP lalu `load merge <file>`. Andal untuk config
                 besar (tak ada korupsi paste). Butuh sftp-server aktif di Backup.
      terminal → paste isi via `load merge terminal` baris-demi-baris. Fallback
                 bila SFTP tak tersedia.

    Bagian commit (show|compare → commit check → commit confirmed 5 → commit) SAMA
    untuk kedua mode. Return True bila di-commit; False bila gagal (tanpa commit).
    """
    def drain(channel, wait=2):
        time.sleep(wait)
        buf = ""
        while channel.recv_ready():
            buf += channel.recv(65535).decode("utf-8")
        return buf

    def read_until(channel, markers, timeout=45, idle=3):
        """Baca sampai salah satu `markers` muncul, ATAU channel diam `idle` dtk.

        Lebih andal daripada tunggu-tetap: pada config besar echo bisa lama, jadi
        kita berhenti hanya saat penanda hasil terlihat atau perangkat benar-benar
        diam. `timeout` = batas keras total agar tak menggantung selamanya.
        """
        buf = ""
        waited = 0.0
        quiet = 0.0
        while waited < timeout:
            time.sleep(0.5)
            waited += 0.5
            chunk = ""
            while channel.recv_ready():
                chunk += channel.recv(65535).decode("utf-8", "replace")
            if chunk:
                buf += chunk
                quiet = 0.0
                low = buf.lower()
                if any(m in low for m in markers):
                    break
            else:
                quiet += 0.5
                if quiet >= idle:
                    break
        return buf

    use_sftp = TRANSFER_MODE != "terminal"
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(device["host"], port=device.get("port", 22), username=device["username"], password=device["password"], timeout=10)

        # --- Mode SFTP: upload file dulu, lalu `load merge <file>` (dari shell). ---
        if use_sftp:
            try:
                sftp = client.open_sftp()
                with sftp.open(remote_path, "w") as rf:
                    rf.write(content if content.endswith("\n") else content + "\n")
                sftp.close()
                print(f"📤 Config terkirim via SFTP → {device['host']}:{remote_path}")
            except Exception as e:
                print(f"❌ ERROR: SFTP gagal ({e}). Pastikan `sftp-server` aktif atau set TRANSFER_MODE=terminal.")
                client.close()
                return False

        channel = client.invoke_shell()
        drain(channel, 1)  # buang banner login

        # Matikan pager & wrap: cegah prompt `---(more)---` / baris terbungkus yang
        # membuat penanda `load complete` tak terbaca.
        channel.send("set cli screen-length 0\n")
        channel.send("set cli screen-width 0\n")
        drain(channel, 1)

        channel.send("configure\n")
        print(f"Output [configure]:\n{drain(channel)}")

        for cmd in (pre_cmds or []):
            channel.send(cmd + "\n")
            print(f"Output [{cmd}]:\n{drain(channel)}")

        if use_sftp:
            # File sudah di perangkat → load langsung dari file (tanpa risiko paste).
            load_cmd = f"load {load_kind} {remote_path}"
            channel.send(load_cmd + "\n")
        else:
            # Mode terminal: kirim config BARIS DEMI BARIS. Mengirim seluruh blob
            # sekaligus membanjiri buffer sesi → sebagian byte hilang → token kosong
            # `''` & "syntax error". Per-baris + jeda kecil = flow-control sederhana.
            load_cmd = f"load {load_kind} terminal"
            channel.send(load_cmd + "\n")
            drain(channel, 1)
            for line in content.splitlines():
                channel.send(line + "\n")
                time.sleep(0.05)                 # beri perangkat waktu meng-echo
                if channel.recv_ready():         # buang echo agar buffer tak menumpuk
                    channel.recv(65535)
            # Ctrl-D HARUS di awal baris kosong agar Junos mengakhiri input.
            channel.send("\n")
            time.sleep(1)
            channel.send("\x04")

        # Tunggu sampai Junos konfirmasi hasil load (atau diam) — bukan hitungan tetap.
        load_out = read_until(channel, ["load complete", "error", "syntax error", "unknown command"])
        print(f"Output [{load_cmd}]:\n{load_out}")

        low = load_out.lower()
        # Junos sukses selalu cetak "load complete". Bila ada 'error'/'syntax'/
        # 'unknown', atau JUSTRU tak ada 'load complete' → anggap gagal & rollback.
        if "error" in low or "syntax" in low or "unknown command" in low or "load complete" not in low:
            print("⚠️  `load` gagal / tak konfirmasi 'load complete' — rollback, tanpa commit.")
            channel.send("rollback\n")
            print(f"Output [rollback]:\n{drain(channel)}")
            channel.send("exit\n")
            drain(channel, 1)
            channel.close()
            client.close()
            return False

        # `commit check` dulu (validasi tanpa apply). Bila gagal → rollback, tak
        # commit. `commit confirmed 5` = jaring pengaman: bila `commit` konfirmasi
        # berikutnya gagal terkirim, Junos auto-rollback dalam 5 menit sehingga
        # Backup tak tertinggal dalam kondisi setengah jadi.
        channel.send("show | compare\n")
        print(f"Output [show | compare]:\n{read_until(channel, ['[edit]'])}")

        channel.send("commit check\n")
        check_out = read_until(channel, ["configuration check succeeds", "check-out failed", "error"])
        print(f"Output [commit check]:\n{check_out}")
        if "error" in check_out.lower() or "check-out failed" in check_out.lower():
            print("⚠️  `commit check` gagal — rollback, tanpa commit.")
            channel.send("rollback\n")
            print(f"Output [rollback]:\n{drain(channel)}")
            channel.send("exit\n")
            drain(channel, 1)
            channel.close()
            client.close()
            return False

        channel.send("commit confirmed 5\n")
        print(f"Output [commit confirmed 5]:\n{read_until(channel, ['commit confirmed', 'commit complete', 'error'])}")
        channel.send("commit\n")
        print(f"Output [commit]:\n{read_until(channel, ['commit complete', 'error'])}")
        channel.send("exit\n")
        drain(channel, 1)

        channel.close()
        client.close()
        return True
    except Exception as e:
        print(f"❌ ERROR: Failed to push config via terminal to {device['host']}: {e}")
        return False


def ssh_interactive(device, commands):
    """Execute an interactive SSH session for Juniper."""
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(device["host"], port=device.get("port", 22), username=device["username"], password=device["password"], timeout=10)
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
            sync_config_commit_trigger()   # kirim delta (load patch), fallback merge
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


def parse_daily_time(s):
    """Parse 'HH:MM' -> (hour, minute). Fallback ke 00:00 kalau format salah."""
    try:
        hh, mm = s.strip().split(":")
        return int(hh), int(mm)
    except Exception:
        print(f"⚠️  DAILY_SYNC_TIME '{s}' tidak valid, pakai default 00:00")
        return 0, 0


def auto_sync(interval, daily_time):
    """Gabungan commit-trigger (seperti --watch) + jadwal harian jam tetap.

    - Tiap `interval` detik, cek commit baru di Master → sync bila `logical-systems`
      berubah (persis seperti --watch).
    - Sekali sehari, begitu jam sistem melewati `daily_time` (format HH:MM, default
      00:00 / tengah malam), paksa full sync sebagai jaring pengaman meski tak ada
      commit baru. Jam bisa diubah kapan saja lewat .env (DAILY_SYNC_TIME) tanpa
      ubah kode.
    """
    hour, minute = parse_daily_time(daily_time)
    print(f"🤖 Auto-sync aktif — commit-trigger tiap {interval} dtk + jadwal harian jam {hour:02d}:{minute:02d}. Ctrl+C untuk berhenti.")
    last_commit = get_latest_commit(MASTER)
    last_ls_hash = hashlib.sha256((fetch_logical_systems(MASTER) or "").encode()).hexdigest()
    last_daily_sync_date = None
    print(f"   Baseline commit: {last_commit or '(gagal baca — cek koneksi)'}")

    while True:
        try:
            time.sleep(interval)
            now = datetime.now()

            # --- Jadwal harian jam tetap (jaring pengaman) ---
            scheduled_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now >= scheduled_today and last_daily_sync_date != now.date():
                print(f"⏰ Jadwal harian ({hour:02d}:{minute:02d}) tercapai — full sync (delete + load merge)...")
                sync_config(full=True)
                last_daily_sync_date = now.date()
                last_commit = get_latest_commit(MASTER)
                last_ls_hash = hashlib.sha256((fetch_logical_systems(MASTER) or "").encode()).hexdigest()
                continue

            # --- Commit-trigger (sama seperti --watch) ---
            cur = get_latest_commit(MASTER)
            if cur is None:
                print("⚠️  Gagal baca commit Master (koneksi?) — coba lagi nanti.")
                continue
            if cur == last_commit:
                continue                                   # belum ada commit baru
            print(f"🔔 Commit baru di Master: {cur}")
            last_commit = cur
            ls = fetch_logical_systems(MASTER) or ""
            h = hashlib.sha256(ls.encode()).hexdigest()
            if h == last_ls_hash:
                print("   `logical-systems` tak berubah sejak sync terakhir → skip.")
                continue
            sync_config_commit_trigger()   # commit-trigger → delta (load patch), fallback merge
            last_ls_hash = h
        except KeyboardInterrupt:
            print("\n👋 Auto-sync dihentikan.")
            break
        except Exception as e:
            print(f"⚠️  auto-sync error: {e}")


if __name__ == "__main__":
    # `python sync-juniper.py`             → sync sekali (aditif: load merge, TANPA delete)
    # `python sync-juniper.py --full`      → MIRROR sekali (delete logical-systems + load merge)
    # `python sync-juniper.py --watch [N]` → pantau, sync otomatis tiap Master commit
    # `python sync-juniper.py --every [N]` → sync PERIODIK tiap N dtk (default 3600 = 1 jam)
    # `python sync-juniper.py --auto [N]`  → commit-trigger + jadwal harian jam tetap (DAILY_SYNC_TIME)
    if len(sys.argv) > 1 and sys.argv[1] in ("--full", "-f"):
        # Mirror penuh: hapus logical-systems lama di Backup lalu muat ulang dari
        # Master. Menghilangkan sisa config lama & risiko double/konflik merge.
        sync_config(full=True)
    elif len(sys.argv) > 1 and sys.argv[1] in ("--watch", "-w"):
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.getenv("WATCH_INTERVAL", "30"))
        watch_and_sync(interval)
    elif len(sys.argv) > 1 and sys.argv[1] in ("--every", "-e"):
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.getenv("SYNC_INTERVAL", "3600"))
        run_every(interval)
    elif len(sys.argv) > 1 and sys.argv[1] in ("--auto", "-a"):
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.getenv("WATCH_INTERVAL", "30"))
        daily_time = os.getenv("DAILY_SYNC_TIME", "00:00")
        auto_sync(interval, daily_time)
    else:
        sync_config()
