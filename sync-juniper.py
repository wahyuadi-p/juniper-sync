import paramiko
import time
import subprocess

from config import MASTER, BACKUP   # kredensial dari .env (bukan hardcoded)

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
        subprocess.run(["python3", "notif.py"], check=True)
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


if __name__ == "__main__":
    sync_config()
