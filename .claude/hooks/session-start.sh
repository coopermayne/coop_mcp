#!/bin/bash
# SessionStart hook for Claude Code on the web.
#
# Prepares a ready-to-run dev environment for the coop_mcp journal/trainer MCP
# server: builds a .venv with the server + webapp Python deps, points the
# session at a throwaway seeded dev DB (JOURNAL_DB), and seeds that DB with mock
# people, journal entries, drinks, workouts and bodyweight so the server + UI
# come up already populated. After this runs, boot the whole thing with:
#
#   MCP_TRANSPORT=http PORT=8000 python webapp/combined.py
#       journal MCP: /mcp · trainer MCP: /trainer/mcp · UI: /app
#
# Idempotent: the venv + deps install is a no-op when already satisfied, and the
# DB is only seeded when it doesn't already exist (seed_dev.py APPENDS journal
# entries, so re-seeding an existing DB would stack duplicates).
set -euo pipefail

# Web-only: locally you manage your own venv / DBs, so don't touch them.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

VENV="$CLAUDE_PROJECT_DIR/.venv"
PY="$VENV/bin/python"

# 1. Dependencies in an isolated venv (the system Python here is Debian-managed
#    and refuses to upgrade its own packages). server deps (fastmcp, jellyfish,
#    tzdata) + webapp deps (fastapi, uvicorn, anthropic, ...). `install` (not a
#    locked sync) so the cached container state is reused on later sessions.
[ -d "$VENV" ] || python -m venv "$VENV"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet --no-cache-dir -r requirements.txt -r webapp/requirements.txt

# 2. Persist env for the whole session: put the venv on PATH (so `python` is the
#    venv's), and pin the throwaway dev DB so the server, seed scripts and any
#    ad-hoc `import server` all share it.
DEV_DB="$CLAUDE_PROJECT_DIR/journal_dev.db"
{
  echo "export PATH=\"$VENV/bin:\$PATH\""
  echo "export VIRTUAL_ENV=\"$VENV\""
  echo "export JOURNAL_DB=\"$DEV_DB\""
  echo "export PYTHONPATH=\"$CLAUDE_PROJECT_DIR\""
} >> "$CLAUDE_ENV_FILE"

# Report whether the webapp's in-app /chat surface will be on. The secret itself
# is NOT set here — add ANTHROPIC_API_KEY (and optionally CHAT_MODEL) in the web
# environment's config and it's injected into the container; combined.py reads
# it straight from the env (a real env var wins over the gitignored .env).
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "chat: enabled (ANTHROPIC_API_KEY present, model ${CHAT_MODEL:-claude-sonnet-4-6})"
else
  echo "chat: disabled (set ANTHROPIC_API_KEY in the web env config to enable /chat)"
fi

# 3. Seed the dev DB once. seed_dev.py is fully offline and self-contained (it
#    creates its own minimal exercise catalog for the workout history), so the
#    populated baseline always lands even with no network.
export JOURNAL_DB="$DEV_DB"
if [ ! -f "$DEV_DB" ]; then
  "$PY" scripts/seed_dev.py

  # Best-effort enrichment: pull the full ~870-movement exercise library +
  # AKAs from free-exercise-db. Needs outbound network; if the env's network
  # policy blocks it, the minimal seed_dev catalog above is enough to run.
  if "$PY" scripts/import_exercises.py >/dev/null 2>&1; then
    "$PY" scripts/seed_exercise_akas.py >/dev/null 2>&1 || true
    echo "seeded dev DB with full exercise library"
  else
    echo "exercise-library import skipped (no network?) — using seed_dev's minimal catalog"
  fi
else
  echo "dev DB already present at $DEV_DB — skipping seed"
fi
