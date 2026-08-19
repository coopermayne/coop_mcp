# Journal backups

The whole life log is one SQLite file on the Coolify volume. These three pieces make
a second copy exist on a machine that isn't the server, and prove it's restorable.

| | |
|---|---|
| `journal-backup.sh` | pulls a verified copy to `~/backups/journal`, prunes old ones |
| `com.coopermayne.journal-backup.plist` | LaunchAgent — runs the above daily at 12:30 |
| `restore-drill.sh` | proves a backup actually restores (run quarterly + before schema changes) |

## Config

Credentials live OUTSIDE the repo, at `~/.config/journal-backup/env` (chmod 600):

```sh
JOURNAL_HOST="https://YOUR-DOMAIN"        # bare origin, no trailing slash
BACKUP_TOKEN="…"                       # must match BACKUP_TOKEN in Coolify
BACKUP_DIR="$HOME/backups/journal"
KEEP_DAYS=30
```

## Install

The plist ships as a template with `__REPO__` / `__HOME__` placeholders (no absolute
paths committed), so installing means filling them in:

```sh
sed -e "s|__REPO__|$(pwd)|g" -e "s|__HOME__|$HOME|g" \
    scripts/backup/com.coopermayne.journal-backup.plist \
    > ~/Library/LaunchAgents/com.coopermayne.journal-backup.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.coopermayne.journal-backup.plist
```

`RunAtLoad` is on, so bootstrapping takes a backup immediately. The installed copy
pins the repo path — moving the repo means re-running the `sed` above. Scheduled for 12:30, not overnight:
this is a laptop that sleeps, and launchd runs a *missed* calendar job once on wake,
so midday (lid most often open) beats 3am.

Check on it: `tail ~/Library/Logs/journal-backup.log`, or
`launchctl print gui/$(id -u)/com.coopermayne.journal-backup | grep "last exit"`.

## What the script refuses to do

A backup you can't trust is worse than a missing one, because it stops you looking for
the problem. So nothing is stored until it's verified, and each of these was tested by
simulating the failure against a local server:

- **Download failed / host unreachable** → nonzero exit, macOS notification, nothing written.
- **Response isn't a SQLite file** (a 502 HTML page lands in the body) → `integrity_check`
  catches it before the file reaches the backup dir.
- **Server came back on a fresh, empty volume** → the export is *valid* and nearly empty.
  Naively this stores it and then prunes the good copies. The script compares entry count
  against the newest existing backup and **refuses anything more than 10% smaller**,
  keeping what it already has.
- **Pruning too far** → age-based deletion never drops below 7 backups, and the first
  backup of each month is kept forever (~2MB each — a cheap archive for a bad edit
  noticed months later, which pure daily rotation would have already discarded).

## Restore

```sh
./scripts/backup/restore-drill.sh                 # newest local backup
./scripts/backup/restore-drill.sh path/to/x.db    # or a specific one
```

Runs on a copy, never the original. Beyond opening the file it runs the real
`init_db()` migrations twice (a redeploy re-runs them) and boots the actual web app
against the result — the two things standing between a stored file and a working app
on restore day. Real restore is `cp` onto the path `JOURNAL_DB` points at, then restart.

## Rotating the token

`BACKUP_TOKEN` is a read-only, backup-only credential — it can download the entire
journal and nothing else. Rotate by changing it in **both** places, then redeploy:
Coolify env for the journal service, and `~/.config/journal-backup/env`. Verify with
`./scripts/backup/journal-backup.sh && tail -1 ~/Library/Logs/journal-backup.log`.

## Not covered yet

Backups live on ONE laptop. That's a real second location and closes the volume-loss
hole, but it does not survive losing the laptop. An encrypted off-site copy (or a
second destination path in the script) is the next increment.
