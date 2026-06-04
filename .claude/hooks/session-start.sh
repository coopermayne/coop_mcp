#!/bin/bash
# SessionStart hook for Claude Code on the web.
#
# Provisions a ready-to-test environment: a Python venv with the server + webapp
# deps installed, and a seeded dev DB (full exercise library + AKAs + mock
# journal/people/workout data). Then exports PATH/JOURNAL_DB so the session can
# run `python server.py`, boot the webapp, and exercise the tools immediately.
#
# Web-only, idempotent, non-interactive. Noisy output goes to stderr so it lands
# in the hook log without polluting the session's context.
set -euo pipefail

# Only run in the remote (web) environment; local runs manage their own venv.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

VENV="$CLAUDE_PROJECT_DIR/.venv"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"
DB="$CLAUDE_PROJECT_DIR/journal_dev.db"

# --- 1. venv + dependencies (pip install benefits from the cached container) ---
if [ ! -x "$PY" ]; then
  echo "[session-start] creating venv" >&2
  python3 -m venv "$VENV"
fi
echo "[session-start] installing dependencies" >&2
"$PIP" install --quiet --upgrade pip >&2
"$PIP" install --quiet -r requirements.txt -r webapp/requirements.txt >&2

# --- 2. seed a dev DB (idempotent: only seed what isn't already there) ---
export JOURNAL_DB="$DB"

# count() helper: returns the row count for a table, or 0 if the DB/table is absent.
count() {
  "$PY" - "$1" <<'PY'
import os, sqlite3, sys
try:
    n = sqlite3.connect(os.environ["JOURNAL_DB"]).execute(
        f"SELECT COUNT(*) FROM {sys.argv[1]}").fetchone()[0]
except Exception:
    n = 0
print(n)
PY
}

# The exercise library is ~870 rows; if it's not populated, import it (network
# fetch of the public-domain free-exercise-db) and seed the AKAs. Both are
# idempotent on their own, but gating skips the ~17s network import on re-runs.
if [ "$(count exercises)" -lt 800 ]; then
  echo "[session-start] importing exercise library + AKAs" >&2
  "$PY" scripts/import_exercises.py >&2
  "$PY" scripts/seed_exercise_akas.py >&2
else
  echo "[session-start] exercise library already seeded; skipping" >&2
fi

# seed_dev.py APPENDS journal entries every run, so only seed when there are none.
if [ "$(count entries)" -eq 0 ]; then
  echo "[session-start] seeding mock journal/people/workout data" >&2
  "$PY" scripts/seed_dev.py >&2
else
  echo "[session-start] journal data already seeded; skipping" >&2
fi

# --- 3. persist environment for the session ---
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  {
    echo "export PATH=\"$VENV/bin:\$PATH\""
    echo "export VIRTUAL_ENV=\"$VENV\""
    echo "export JOURNAL_DB=\"$DB\""
  } >> "$CLAUDE_ENV_FILE"
fi

echo "[session-start] ready: venv + seeded dev DB at $DB" >&2
