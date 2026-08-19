#!/bin/bash
#
# Pull a backup of the journal DB off the server and keep it on this machine.
#
# The whole life log is one SQLite file on the Coolify volume. This job is the
# only thing that makes a second copy exist, so it is written to FAIL LOUDLY and
# to refuse anything it can't verify — a backup you can't trust is worse than a
# missing one, because it stops you looking for the problem.
#
# Config (NOT in the repo): ~/.config/journal-backup/env, chmod 600.
#   JOURNAL_HOST, BACKUP_TOKEN, BACKUP_DIR, KEEP_DAYS
#
# Install: see scripts/backup/README.md
set -uo pipefail

CONFIG="${JOURNAL_BACKUP_CONFIG:-$HOME/.config/journal-backup/env}"
LOG="$HOME/Library/Logs/journal-backup.log"
mkdir -p "$(dirname "$LOG")"

log()  { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }
note() { # a macOS notification — this job is unattended, so failure has to be visible
  /usr/bin/osascript -e "display notification \"$2\" with title \"$1\"" >/dev/null 2>&1 || true
}
die() { log "FAIL: $*"; note "Journal backup failed" "$*"; exit 1; }

[ -r "$CONFIG" ] || die "no config at $CONFIG"
set -a; . "$CONFIG"; set +a

: "${JOURNAL_HOST:?}" ; : "${BACKUP_TOKEN:?}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/journal}"
KEEP_DAYS="${KEEP_DAYS:-30}"
MIN_KEEP=7          # never prune below this many, whatever the age rule says
mkdir -p "$BACKUP_DIR"

stamp="$(date +%F)"
dest="$BACKUP_DIR/journal-$stamp.db"
tmp="$(mktemp "${TMPDIR:-/tmp}/journal-backup.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

# ---- pull -----------------------------------------------------------------
code=$(curl -fsS --max-time 300 --retry 3 --retry-delay 10 \
            -H "Authorization: Bearer $BACKUP_TOKEN" \
            -o "$tmp" -w '%{http_code}' \
            "$JOURNAL_HOST/app/export/journal.db" 2>>"$LOG") \
  || die "download failed (http ${code:-?}) — see $LOG"
[ "$code" = "200" ] || die "unexpected http $code"

# ---- verify BEFORE it is allowed to count as a backup ----------------------
# An unreadable or truncated file that lands in the backup dir looks like
# protection and isn't. Everything below runs on the temp copy.
/usr/bin/sqlite3 "$tmp" "pragma integrity_check;" 2>>"$LOG" | grep -qx ok \
  || die "integrity_check failed on the downloaded file"

entries=$(/usr/bin/sqlite3 "$tmp" "select count(*) from entries;" 2>/dev/null) || die "can't read entries table"
people=$(/usr/bin/sqlite3  "$tmp" "select count(*) from people;"  2>/dev/null) || die "can't read people table"
[ "${entries:-0}" -gt 0 ] || die "downloaded DB has 0 entries — refusing to store it"

# Guard against the server coming back on a FRESH volume: if that happens the
# export is valid, small, and nearly empty, and a naive job would store it and
# then prune the good copies. Compare against the newest backup we already hold.
prev=$(ls -1 "$BACKUP_DIR"/journal-*.db 2>/dev/null | tail -1)
if [ -n "$prev" ]; then
  prev_entries=$(/usr/bin/sqlite3 "$prev" "select count(*) from entries;" 2>/dev/null || echo 0)
  floor=$(( prev_entries * 90 / 100 ))
  if [ "$entries" -lt "$floor" ]; then
    die "entries dropped $prev_entries -> $entries (>10%). Kept the old backup, stored nothing."
  fi
fi

# ---- store atomically ------------------------------------------------------
mv "$tmp" "$dest" || die "could not move into $BACKUP_DIR"
chmod 600 "$dest"
trap - EXIT
size=$(du -h "$dest" | cut -f1)
log "OK  $dest  ($size, $entries entries, $people people)"
date +%s > "$BACKUP_DIR/.last-success"

# ---- prune -----------------------------------------------------------------
# Age-based, with two protections: keep the most recent MIN_KEEP no matter what,
# and keep the FIRST backup of every month forever (2MB each — a cheap archive
# that survives a bad edit noticed months later, which daily rotation would not).
total=$(ls -1 "$BACKUP_DIR"/journal-*.db 2>/dev/null | wc -l | tr -d ' ')
if [ "$total" -gt "$MIN_KEEP" ]; then
  seen_months=""
  for f in $(ls -1 "$BACKUP_DIR"/journal-*.db | sort); do
    base=$(basename "$f" .db); d="${base#journal-}"; month="${d%-*}"
    case " $seen_months " in *" $month "*) monthly=no ;; *) monthly=yes; seen_months="$seen_months $month" ;; esac
    [ "$monthly" = yes ] && continue                       # first-of-month: archive
    [ "$f" = "$dest" ] && continue
    if [ -n "$(find "$f" -mtime +"$KEEP_DAYS" 2>/dev/null)" ]; then
      remaining=$(ls -1 "$BACKUP_DIR"/journal-*.db | wc -l | tr -d ' ')
      [ "$remaining" -le "$MIN_KEEP" ] && break
      rm -f "$f" && log "pruned $(basename "$f")"
    fi
  done
fi
exit 0
