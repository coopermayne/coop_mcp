#!/bin/bash
#
# Prove a backup is restorable. Run this quarterly, and ALWAYS before deploying
# a change that migrates the schema.
#
#   ./scripts/backup/restore-drill.sh [path/to/journal-YYYY-MM-DD.db]
#
# Defaults to the newest local backup. Never touches the original: everything
# below runs on a copy. An untested backup is a belief, not a backup — this is
# what converts one into the other. It goes further than "does the file open":
# it runs the REAL init_db() migrations against the copy (twice, so a redeploy
# is covered) and boots the actual web app against it, because those are the two
# things that stand between a stored file and a working app on restore day.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 1

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/journal}"
SRC="${1:-$(ls -1t "$BACKUP_DIR"/journal-*.db 2>/dev/null | head -1)}"
[ -n "$SRC" ] && [ -r "$SRC" ] || { echo "no backup found (looked in $BACKUP_DIR)"; exit 1; }

PY="${PY:-.venv/bin/python}"; [ -x "$PY" ] || PY=python3
WORK="$(mktemp -d)"; COPY="$WORK/restored.db"
trap 'rm -rf "$WORK"' EXIT
cp "$SRC" "$COPY"
echo "drill on $(basename "$SRC")  ($(du -h "$SRC" | cut -f1))"
fail=0; step() { printf '  %-34s' "$1"; }
ok()   { echo "ok${1:+  $1}"; }
bad()  { echo "FAILED  $1"; fail=1; }

step "integrity_check"
[ "$(sqlite3 "$COPY" 'pragma integrity_check;')" = "ok" ] && ok || bad "corrupt"

step "row counts"
counts=$(sqlite3 "$COPY" "select (select count(*) from entries)||' entries, '||(select count(*) from people)||' people, '||(select count(*) from intake_items)||' intake, '||(select count(*) from sets)||' sets';")
[ -n "$counts" ] && ok "$counts" || bad "unreadable"

step "FTS index intact"
n=$(sqlite3 "$COPY" "select count(*) from entries_fts;" 2>/dev/null)
e=$(sqlite3 "$COPY" "select count(*) from entries;")
[ "$n" = "$e" ] && ok "$n rows" || bad "fts $n vs entries $e"

step "full-text search works"
hits=$(sqlite3 "$COPY" "select count(*) from entries_fts where entries_fts match 'the';" 2>/dev/null)
[ "${hits:-0}" -gt 0 ] && ok "$hits hits for 'the'" || bad "MATCH returned nothing"

# The real test: migrations. Restore day runs init_db() against this file.
step "init_db() migrations"
JOURNAL_DB="$COPY" "$PY" - <<'PY' >"$WORK/mig.log" 2>&1
import importlib.util, os
spec = importlib.util.spec_from_file_location("server", "server.py")
S = importlib.util.module_from_spec(spec); spec.loader.exec_module(S)
S.init_db(); S.init_db()   # twice — a redeploy re-runs it
print("MIGRATED")
PY
grep -q MIGRATED "$WORK/mig.log" && ok "ran twice, idempotent" || { bad "see below"; tail -15 "$WORK/mig.log"; }

step "data survived migration"
after=$(sqlite3 "$COPY" "select count(*) from entries;" 2>/dev/null)
[ "$after" = "$e" ] && ok "$after entries" || bad "entries $e -> $after"

# Boot the actual app against the restored file.
step "app boots + serves"
PORT=8877
JOURNAL_DB="$COPY" MCP_TRANSPORT=http PORT=$PORT ANTHROPIC_API_KEY= \
  "$PY" webapp/combined.py >"$WORK/app.log" 2>&1 &
pid=$!
for _ in $(seq 1 30); do sleep 1; curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break; done
h=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health")
# /food, not /journal: the journal page sits behind the lock and 307s to it, which
# proves routing but never renders a row. /food is outside the lock by design, so a
# 200 here means the restored DB was actually read and drawn.
f=$(curl -s -o "$WORK/food.html" -w '%{http_code}' "http://127.0.0.1:$PORT/app/food")
kill $pid 2>/dev/null; wait $pid 2>/dev/null
if [ "$h" = "200" ] && [ "$f" = "200" ] && grep -q "protein" "$WORK/food.html"; then
  ok "health $h, /app/food renders"
else
  bad "health $h, food $f — see $WORK/app.log"; tail -15 "$WORK/app.log"
fi

echo
[ $fail -eq 0 ] && echo "PASS — $(basename "$SRC") is restorable." \
                || { echo "FAIL — do not rely on this backup."; exit 1; }
