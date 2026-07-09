# Juniper Sync

> ⚠️ **This project is under active development and intended for testing purposes only.**

Synchronize the **`logical-systems`** configuration from a **Master** Juniper device to a **Backup** Juniper device over SSH. Unlike a full `load override`, this tool touches **only** the `logical-systems` stanza — the Backup device's own base configuration (host-name, management interface, routing, etc.) is always left intact. A Telegram notification is sent on success.

## How It Works

```
┌────────────┐       SSH        ┌──────────────┐
│   Master   │ ──────────────── │  This Script │
│ (Juniper)  │  Fetch config    │   (Python)   │
└────────────┘                  └──────┬───────┘
                                       │  logical-systems only
                                       │  (merge / patch / diff)
                                       ▼
                                ┌─────────────┐       Telegram
                                │   Backup    │ ──────────────── 📩 Notification
                                │  (Juniper)  │  load + commit confirmed
                                └─────────────┘
```

The Backup device is always applied through the same commit safety net:
`load` → `show | compare` → `commit check` → `commit confirmed 5` → `commit`.
If any step fails, the script rolls back and never commits, so the Backup is never
left half-applied.

## Sync Modes

The tool offers several strategies, each suited to a different situation:

| Mode | CLI | What it does |
|---|---|---|
| **Merge** (additive) | `python3 sync-juniper.py` | Fetch Master's `logical-systems`, `load merge` into Backup. Additions/changes flow in, but items **deleted** on the Master do **not** disappear on the Backup. |
| **Full mirror** | `--full` / `-f` | `delete logical-systems` + `load merge`. Backup becomes an exact mirror of the Master's `logical-systems` (deletions are reflected). Names in `PRESERVE_LOGICAL_SYSTEMS` are kept. |
| **Diff** (reconcile) | `--diff` / `-d` | Compare Master vs Backup as `set` lines, then **add** what is missing and **delete** what is extra on the Backup — honoring `EXCLUDE_FILE`. |
| **Preview** | `--preview` / `-n` | Read-only. Computes and prints the diff plan (saved to `diff_apply.txt`) **without touching** the Backup. |

### Diff mode details

Diff mode makes the Backup match the Master (outside of exclusions), in both
directions:

- **Add** — `set` lines present on the Master but missing on the Backup.
- **Delete** — lines present on the Backup but absent on the Master. Nested
  deletes are **collapsed to the highest safe container** (e.g. four leaf lines
  under a BGP group become a single `delete ... group BGP-CORE`). This mirrors how
  the Master itself deletes — atomically — so the Backup never passes through an
  invalid intermediate state (such as an external neighbor left without a
  `peer-as`, which `commit check` would reject).

### Exclusions (`exclude.conf`)

The diff mode reads an exclusion file (default `exclude.conf`, override via
`EXCLUDE_FILE`). Each line is a path **prefix**; any Backup config at or below that
prefix is protected. The **leading verb sets the protection scope**:

| Line | Scope | Effect |
|---|---|---|
| `deactivate logical-systems BGP-SCRIPT` | `state` | Only the **activate/deactivate status** is protected. The stanza's **contents still sync** to the Master — useful for a standby LS that is deactivated by a VRRP event-policy but whose config must stay up to date. |
| `logical-systems BGP-SCRIPT` | `all` | The **entire** path (contents **and** status) is never added or removed. |

Blank lines and lines starting with `#` are ignored. See `exclude.conf.example`.

## Continuous Modes

### Watch — sync on Master commit

Polls the Master and syncs automatically whenever a **new commit changes
`logical-systems`** (no router-side setup required):

```bash
python3 sync-juniper.py --watch          # poll every 30s (default)
python3 sync-juniper.py --watch 10       # poll every 10s
# or set WATCH_INTERVAL=15 in .env
```

Each interval reads `show system commit`. When the top commit index changes **and**
the `logical-systems` hash differs from the last sync, it triggers a sync. The
baseline at startup is not synced (only subsequent commits trigger). Stop with
`Ctrl+C`.

### Every — periodic sync

Runs a sync on a fixed schedule every N seconds, regardless of whether there is a
new commit. Default 3600s = 1 hour:

```bash
python3 sync-juniper.py --every          # every 3600s (1 hour, default)
python3 sync-juniper.py --every 1800     # every 30 minutes
# or set SYNC_INTERVAL=3600 in .env
```

### Auto — commit-trigger + daily safety net

Combines **watch** with a daily scheduled sync at a fixed time (default midnight
`00:00`), so a full sync still happens once a day even if no new commit is
detected:

```bash
python3 sync-juniper.py --auto           # poll commits every 30s + daily sync
python3 sync-juniper.py --auto 10        # poll every 10s + daily sync
# schedule via .env: DAILY_SYNC_TIME=00:00   (HH:MM, 24h)
```

**Both triggers follow `COMMIT_TRIGGER_MODE`** for consistency:

| `COMMIT_TRIGGER_MODE` | Commit-trigger sends | Daily schedule runs |
|---|---|---|
| `diff` | `sync_config_diff()` — reconcile, honors `EXCLUDE_FILE` | `sync_config_diff()` — same |
| `patch` (default) | delta only via `load patch` (`compare rollback 1`), auto-fallback to `load merge` on reject | full mirror (`delete` + `load merge`) |
| `merge` | whole stanza + `load merge` (additive) | full mirror |

> With `COMMIT_TRIGGER_MODE=diff`, exclusions are honored **around the clock** —
> including at the daily run — so a `deactivate` state protected by scope `state`
> is never overwritten.
>
> Note: `patch` mode's `rollback 1` captures only the **last** commit. If 2+
> commits land between two polls, some delta may be missed on the patch path — this
> is exactly what the daily full sync guards against.

**Real-time alternatives (require router-side setup):**
- **`event-options`** — an event policy on the Master triggers an op-script sync on `UI_COMMIT_COMPLETED`.
- **Syslog** — the Master ships syslog to a host; a listener triggers on `UI_COMMIT_COMPLETED`.
- **`transfer-on-commit`** — `set system archival configuration transfer-on-commit` uploads config on every commit; a file-watcher triggers the sync.

## Project Structure

```
juniper-sync/
├── sync-juniper.py       # Main synchronization script
├── config.py             # Central config loader (reads .env)
├── notif.py              # Telegram notification module
├── test_diff.py          # Unit tests for the pure diff logic (no SSH)
├── exclude.conf.example  # Sample exclusion file for diff mode
├── .env.example          # Sample environment configuration
└── requirements.txt      # Python dependencies
```

## Prerequisites

- **Python** 3.8 or higher
- **Network access** to both Master and Backup Juniper devices via SSH
- A **Telegram Bot Token** and **Chat ID** for notifications (optional)
- SSH enabled on both devices; `sftp-server` enabled on the Backup for SFTP transfer mode

## Installation

1. **Clone the repository**

   ```bash
   git clone https://github.com/wahyuadi-p/juniper-sync.git
   cd juniper-sync
   ```

2. **Create a virtual environment** (optional)

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

## Configuration

All settings live in a `.env` file — **no credentials are hardcoded** in the
source. Copy the sample and fill it in:

```bash
cp .env.example .env
cp exclude.conf.example exclude.conf   # only needed for diff mode
```

### `.env` variables

| Variable | Default | Description |
|---|---|---|
| `MASTER_HOST` / `MASTER_USER` / `MASTER_PASS` | — | Master device SSH host / username / password |
| `MASTER_PORT` | `22` | Master SSH port |
| `BACKUP_HOST` / `BACKUP_USER` / `BACKUP_PASS` | — | Backup device SSH host / username / password |
| `BACKUP_PORT` | `22` | Backup SSH port |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | Telegram notification credentials |
| `COMMIT_TRIGGER_MODE` | `patch` | `diff` \| `patch` \| `merge` (see [Auto](#auto--commit-trigger--daily-safety-net)) |
| `TRANSFER_MODE` | `sftp` | `sftp` (upload then `load <file>`) or `terminal` (paste line by line) |
| `PRESERVE_LOGICAL_SYSTEMS` | — | Comma-separated LS names kept during `--full` mirror (Backup-only standby LS) |
| `EXCLUDE_FILE` | `exclude.conf` | Path to the diff-mode exclusion file |
| `WATCH_INTERVAL` | `30` | `--watch` / `--auto` poll interval (seconds) |
| `SYNC_INTERVAL` | `3600` | `--every` interval (seconds) |
| `DAILY_SYNC_TIME` | `00:00` | `--auto` daily schedule (HH:MM, 24h) |

## Testing

The core diff logic is pure (no SSH), so it is covered by unit tests:

```bash
python3 test_diff.py     # exit 0 = all pass
```

These exercise every case: adds, container-collapsed deletes, value changes,
activate/deactivate status, and exclusions in both `all` and `state` scope.

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `paramiko` | 5.0.0 | SSH and SFTP connectivity to Juniper devices |
| `requests` | 2.33.1 | Sending Telegram API notifications |
| `python-dotenv` | 1.0.1 | `.env` loading (a zero-dependency fallback parser is built in if absent) |

## ⚠️ Known Limitations

- `patch` mode's `rollback 1` only captures the last commit; the daily `--auto` sync compensates.
- Diff mode reconciles `logical-systems` only — other stanzas on the Backup are never touched.
- No **logging to file** — all output goes to stdout.
- Rollback relies on Juniper's built-in `commit confirmed` timeout (5 minutes).

## License

This project is licensed under the [MIT License](LICENSE).
