"""
Journal MCP server.

This module defines TWO FastMCP servers sharing one SQLite DB and one Google auth
provider: `mcp` (the journal + drinking tools, at /mcp) and `trainer_mcp` (the training
tools, at /trainer/mcp). They're split so each is its own connector / Claude project and
a conversation loads only the relevant tool set. webapp/combined.py composes both onto
one origin; over stdio, MCP_SERVER picks which one a bare `server.py` launch runs.

Design contract:
  - This server is a DETERMINISTIC data + candidate-matching layer. It contains
    no LLM. Entity resolution ("which Tom?") is done by the model in the
    conversation, using the candidates this server returns.
  - Capture is never blocked: add_journal_entry always saves, even when every
    mention is ambiguous. Unresolved mentions sit in the pending queue until the
    user feels like resolving them ("I'll tell you later").
  - The system gets quieter over time: when a surface form is linked with
    learn_alias=True, it becomes a stored alias, so the same word (including a
    recurring transcription error) auto-matches strongly next time.
"""

import json
import os
import re
import sqlite3
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import jellyfish
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware

DB_PATH = os.environ.get("JOURNAL_DB", os.path.expanduser("~/journal.db"))

# Single-user allowlist: the Google account(s) permitted to use this journal.
ALLOWED_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("JOURNAL_ALLOWED_EMAILS", "").split(",")
    if e.strip()
}


def _build_auth(public_url: Optional[str] = None):
    """Return a Google OAuth provider if creds are set, else None (authless).

    Build a FRESH provider per MCP server — never share one object across both. A
    GoogleProvider is single-resource: building its HTTP app calls set_mcp_path(),
    which stores `_resource_url` ON THE INSTANCE and is what incoming tokens are
    validated against. Sharing one provider lets the second server's build clobber the
    first's `_resource_url`, so the first endpoint then rejects all its own tokens.
    Each server gets its own instance (same Google client + allowlist).

    `public_url` overrides the origin the provider advertises (defaults to PUBLIC_URL).
    The trainer passes its own subdomain (TRAINER_PUBLIC_URL) so its OAuth discovery +
    callback live at the root of its OWN origin — two full OAuth servers can't share one
    origin (their /authorize, /token, /auth/callback paths collide), so the trainer gets
    its own host. The Google client just needs that host's /auth/callback added as an
    authorized redirect URI.

    Authless is for local dev / staging with dummy data only. Set GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET, PUBLIC_URL, and JOURNAL_ALLOWED_EMAILS in Coolify to protect
    the server before putting real entries in.
    """
    cid = os.environ.get("GOOGLE_CLIENT_ID")
    csec = os.environ.get("GOOGLE_CLIENT_SECRET")
    base = public_url or os.environ.get("PUBLIC_URL")  # e.g. https://journal.yourdomain.com
    if cid and csec and base:
        from fastmcp.server.auth.providers.google import GoogleProvider
        return GoogleProvider(client_id=cid, client_secret=csec, base_url=base,
                              required_scopes=["openid", "email"])
    return None


class AllowlistMiddleware(Middleware):
    """Reject any authenticated Google account that isn't on the allowlist — so a
    valid Google login alone is not enough; it must be *your* account."""

    async def on_call_tool(self, context, call_next):
        if ALLOWED_EMAILS:
            tok = get_access_token()
            email = (tok.claims or {}).get("email", "").lower() if tok else ""
            if email not in ALLOWED_EMAILS:
                raise ToolError("Not authorized for this journal.")
        return await call_next(context)


_auth = _build_auth()
mcp = FastMCP("journal", auth=_auth, instructions="""\
Single-user life log: a conversational journal (people are resolved to stable
entities, not name strings) plus a drinking tracker. The server only stores and
matches — the judgment (which person a mention means) is yours.

Three rules: capture never blocks — always save, leave ambiguous mentions pending
for later; resolve mentions to person entities, don't normalize names in text (and
expand a plural like "my parents" into one mention per person — never a single one);
and one note per topic — split unrelated threads from the same conversation into
separate entries so their people don't cross-contaminate later lookups.

All dates in this log are Pacific (America/Los_Angeles) — the user lives and logs
on Pacific time. get_briefing returns `now` (current Pacific date/time); anchor
"today"/"yesterday" to it before defaulting or computing any date.

Start a session with get_briefing to load people/journal context before acting.
(Workouts/training live on a separate `trainer` MCP server.)""")
if _auth is not None:
    mcp.add_middleware(AllowlistMiddleware())

# The trainer is a SEPARATE MCP server living in the SAME process and sharing this DB,
# so a Claude project connected to it loads ONLY the training tools. It gets its OWN
# auth provider instance (providers are single-resource and must not be shared — see
# _build_auth). When TRAINER_PUBLIC_URL is set, the provider advertises that subdomain,
# and webapp/combined.py routes that host to this server (its own clean root OAuth);
# otherwise it falls back to /trainer/mcp on the journal origin (fine for authless/local).
_trainer_auth = _build_auth(os.environ.get("TRAINER_PUBLIC_URL"))
trainer_mcp = FastMCP("trainer", auth=_trainer_auth, instructions="""\
Personal-trainer log: a workout log (sessions + per-set weight/reps/rpe, plus
duration/distance for cardio like running and walking) and an exercise catalog
(technique, cautions, target muscles). The server only stores and computes
deterministic aggregates (per-muscle recency/volume, cardio minutes/miles) — all
coaching judgment (next weight, what to program, what to rest, how to cue form) is
yours.

All dates here are Pacific (America/Los_Angeles). get_fitness_briefing returns `now`
(current Pacific date/time); anchor "today"/"yesterday" to it before defaulting or
computing any date.

Start a training session with get_fitness_briefing to load the profile (injuries,
split, goals), per-muscle recency, recent sessions (with their notes), and the latest
bodyweight before recommending work. Recent-session notes are durable context — read
them so a "left shoulder twinge" last time shapes what you program next.

Two ways to record training:
  - PLAN-AS-YOU-LIFT (the live routine): start_workout_plan lays out today's session as
    PENDING sets with target weights/reps; the user completes them with complete_set as
    they go (omitted numbers default to the targets). swap_exercise substitutes a
    busy/broken movement with its CLOSEST like-for-like peer — same movement pattern and
    role (compound→compound, isolation→isolation), not just any exercise sharing a
    muscle — add_to_plan tacks on more, update_set
    retargets a pending set, and finish_workout closes it out (leftover pending sets are
    skipped). get_workout_plan returns the current state. Only ONE plan is active at a
    time. Design the routine yourself from the briefing, choosing movements from the
    user's `rotation` — progress what was easy (low RPE), hold/deload what was hard, and
    keep staple lifts so the tracked data stays comparable; vary exercises only modestly.
    Program a substantial session: aim for roughly 21-26 working sets total, built from
    about 6-8 different exercises at 3-4 sets each, spread across the muscle groups that
    are due.
  - POST-HOC (log what already happened): log_workout records a finished session (or
    appends to one) in a single call — use it when the user just tells you what they
    did rather than working a plan live.
Only completed ('done') sets count toward recency, history, and PRs; a planned-but-not-
yet-done set doesn't, so the briefing stays honest mid-session.

The catalog has two layers — a LIBRARY and a ROTATION:
  - LIBRARY: the whole catalog, PRE-LOADED with ~870 public-domain movements from
    free-exercise-db (via scripts/import_exercises.py), each carrying muscles, equipment,
    a demo image, and step-by-step technique. It's a reference the user searches/browses
    at /trainer/library. Almost any movement you mention is already here with full data —
    look it up with `exercises` rather than re-deriving it.
  - ROTATION: the curated subset (in_rotation) the user actually trains. get_fitness_
    briefing returns it as `rotation`, and it is the ONLY pool you PROGRAM ROUTINES FROM.
    Build start_workout_plan / log_workout sessions out of rotation movements. If you want
    something NOT in the rotation, don't silently slip it in — propose it to the user and,
    if they agree, set_rotation to add it first (you can surface candidates from the wider
    library with exercises(muscle=…)). Logging a movement the user actually did adds it to
    the rotation automatically, so the rotation grows from real training.

The catalog is CLOSED to you: you NEVER invent an exercise — the ~870-movement library
plus anything the user adds is the whole world, so program only names it already holds.
Names you pass to the logging/planning tools resolve fuzzily, so a near-spelling still
lands; but a name with no real match is SKIPPED and returned under `unmatched` with its
closest `candidates` — re-issue it under one of those, never as a new exercise. New
movements enter the catalog ONLY through the website's manual add form, so if the user
wants one that genuinely isn't there, point them to /trainer/library rather than creating
it. What you CAN do with save_exercise is keep an EXISTING entry coached — fill in
technique_notes (key cues), common_mistakes, cautions (especially the user's left-shoulder
limits), equipment, and the muscle EMPHASIS tiers (muscles = primary, secondary_muscles,
tertiary_muscles — e.g. a Kettlebell Thruster is shoulders primary, quadriceps/glutes
secondary, triceps tertiary) so the /trainer page doesn't show "No saved technique notes
yet". Use the canonical muscle labels (they mirror the library's vocabulary): abdominals,
abductors, adductors, biceps, calves, chest, forearms, glutes, hamstrings, lats, lower
back, middle back, neck, quadriceps, shoulders, traps, triceps. The user can also just ask
you to add a movement to their rotation ("add Bulgarian split squats"); that's set_rotation
(plus save_exercise enrichment if the entry is bare).

After a session is logged, nudge the user to weigh in and capture it with
log_bodyweight — it's a standing habit on their weight-loss journey, and the trend is
only useful if the readings are regular.""")
if _trainer_auth is not None:
    trainer_mcp.add_middleware(AllowlistMiddleware())


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    """Unauthenticated liveness probe for Coolify. Confirms the process and DB are up."""
    from starlette.responses import JSONResponse
    try:
        with db() as conn:
            conn.execute("SELECT 1")
        return JSONResponse({"status": "ok"})
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=503)


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id             INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    role           TEXT,
    notes          TEXT,
    summary        TEXT,   -- rolling profile Claude maintains, for fast context
    email          TEXT,   -- vCard-aligned contact fields
    phone          TEXT,
    address        TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE   -- "family", "colleagues", "Robin's friends"
);

CREATE TABLE IF NOT EXISTS person_groups (
    person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    group_id  INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    PRIMARY KEY (person_id, group_id)
);

CREATE TABLE IF NOT EXISTS aliases (
    id           INTEGER PRIMARY KEY,
    person_id    INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    surface_form TEXT NOT NULL,
    phonetic_key TEXT,
    source       TEXT NOT NULL DEFAULT 'manual',  -- 'manual' | 'learned'
    UNIQUE(person_id, surface_form)
);
CREATE INDEX IF NOT EXISTS idx_alias_phonetic ON aliases(phonetic_key);

CREATE TABLE IF NOT EXISTS entries (
    id         INTEGER PRIMARY KEY,
    body       TEXT NOT NULL,   -- cleaned, structured, concise: the journal proper
    raw_body   TEXT,            -- verbatim input, hidden fallback (NULL if none kept)
    entry_date TEXT NOT NULL,   -- the day the entry is ABOUT (YYYY-MM-DD)
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mentions (
    id              INTEGER PRIMARY KEY,
    entry_id        INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    surface_form    TEXT NOT NULL,
    context_snippet TEXT,
    person_id       INTEGER REFERENCES people(id),   -- NULL while pending
    status          TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'resolved'
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mention_person ON mentions(person_id);
CREATE INDEX IF NOT EXISTS idx_mention_status ON mentions(status);

CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts
    USING fts5(body, content='entries', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, body) VALUES('delete', old.id, old.body);
END;
CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, body) VALUES('delete', old.id, old.body);
    INSERT INTO entries_fts(rowid, body) VALUES (new.id, new.body);
END;

-- ----------------------------------------------------------------------- --
-- Drinking tracker. EXACTLY ONE row per day (drink_date is unique): the first
-- log of the day creates it, later logs accumulate onto it (standard_drinks add
-- up, kinds merge into a deduped list). Days with no row are sober days.
-- Aggregation (daily totals, streaks) is deterministic SQL.
-- ----------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS drinks (
    id              INTEGER PRIMARY KEY,
    drink_date      TEXT NOT NULL,    -- YYYY-MM-DD the drinks were consumed
    standard_drinks REAL NOT NULL,    -- in standard-drink units (beer/wine ~1, cocktail ~1.5)
    kind            TEXT,             -- merged label list, e.g. "beer, wine"
    notes           TEXT,
    created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_drinks_date ON drinks(drink_date);

-- ----------------------------------------------------------------------- --
-- Personal trainer. `exercises` is a catalog of stable exercise ENTITIES
-- (like people): technique lives here so the model can coach form. Muscles
-- are normalized into a child table so "what's rested vs worked" is a plain
-- SQL aggregate, not an LLM guess. Its columns mirror the free-exercise-db
-- dataset (slug/force/level/mechanic/equipment/category + primary/secondary
-- muscles + instructions) so the public-domain LIBRARY imports 1:1; our own
-- coaching layer (common_mistakes, cautions, video_link) sits on top. The
-- `in_rotation` flag marks the curated subset the trainer actually programs
-- from. `workouts`/`sets` are the two-level log (session + per-set
-- weight/reps/rpe), mirroring entries/mentions. The server stores and
-- retrieves; progression judgment happens in conversation.
-- ----------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS exercises (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    slug            TEXT,             -- free-exercise-db id; stable external key + image base
    category        TEXT,             -- 'strength' | 'cardio' | 'stretching' | 'plyometrics' | ...
    force           TEXT,             -- 'push' | 'pull' | 'static'
    level           TEXT,             -- 'beginner' | 'intermediate' | 'expert'
    mechanic        TEXT,             -- 'compound' | 'isolation' (also guides like-for-like swaps)
    equipment       TEXT,
    technique_notes TEXT,
    common_mistakes TEXT,
    cautions        TEXT,             -- injury / shoulder considerations
    video_link      TEXT,
    image_link      TEXT,             -- gif / still of proper technique (loops inline in the UI)
    in_rotation     INTEGER NOT NULL DEFAULT 0,  -- 1 = in the user's curated programming pool
    created_at      TEXT NOT NULL
);
-- NB: idx_exercises_rotation is created in init_db(), AFTER the ALTER that adds
-- in_rotation to pre-existing DBs (it can't live here or executescript fails on them).

CREATE TABLE IF NOT EXISTS exercise_muscles (
    exercise_id INTEGER NOT NULL REFERENCES exercises(id) ON DELETE CASCADE,
    muscle      TEXT NOT NULL,        -- canonical lowercase, e.g. 'chest', 'lats', 'quadriceps'
    role        TEXT NOT NULL DEFAULT 'primary',  -- 'primary' | 'secondary' | 'tertiary' (emphasis tier)
    PRIMARY KEY (exercise_id, muscle)
);
CREATE INDEX IF NOT EXISTS idx_exmuscle_muscle ON exercise_muscles(muscle);

CREATE TABLE IF NOT EXISTS workouts (
    id           INTEGER PRIMARY KEY,
    workout_date TEXT NOT NULL,       -- YYYY-MM-DD
    focus        TEXT,                -- "Legs", "Arms", "Cardio"
    feeling      TEXT,                -- overall how the session felt
    notes        TEXT,
    status       TEXT NOT NULL DEFAULT 'done',  -- 'active' (plan in progress) | 'done'
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workouts_date ON workouts(workout_date);

-- A `sets` row is BOTH the plan and the log: a planned set carries its targets
-- (target_weight_lbs/target_reps) with the actuals (weight_lbs/reps/rpe) NULL until
-- it's done. status: 'pending' (planned, not yet done) -> 'done' (actuals logged) ->
-- 'skipped' (left undone at finish, or swapped out). A plain log_workout call writes
-- 'done' sets directly. Only 'done' sets count toward recency/history/PR aggregates.
CREATE TABLE IF NOT EXISTS sets (
    id          INTEGER PRIMARY KEY,
    workout_id  INTEGER NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
    exercise_id INTEGER NOT NULL REFERENCES exercises(id),
    set_index   INTEGER NOT NULL,     -- 1-based order within the exercise
    weight_lbs  REAL,                 -- NULL for bodyweight / cardio / not-yet-done plan
    reps        INTEGER,
    rpe         REAL,                 -- 1-10 perceived exertion (10 = true failure)
    duration_seconds INTEGER,         -- cardio: time of the effort (run/walk/row); NULL for lifts
    distance_miles   REAL,            -- cardio: distance covered; NULL for lifts
    target_weight_lbs REAL,           -- plan target (lift); NULL for ad-hoc logged sets
    target_reps       INTEGER,        -- plan target (lift)
    status      TEXT NOT NULL DEFAULT 'done',  -- 'pending' | 'done' | 'skipped'
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_sets_workout ON sets(workout_id);
CREATE INDEX IF NOT EXISTS idx_sets_exercise ON sets(exercise_id);

-- Bodyweight readings — a standalone daily health metric (like drinks), keyed by
-- the day weighed, NOT tied to a workout row. Several readings on a day are allowed
-- (the latest is "the" weight for that day); a day with no row simply wasn't weighed.
CREATE TABLE IF NOT EXISTS body_weight (
    id          INTEGER PRIMARY KEY,
    weigh_date  TEXT NOT NULL,        -- YYYY-MM-DD (Pacific): the day weighed
    weight_lbs  REAL NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bodyweight_date ON body_weight(weigh_date);

-- Generic JSON settings (trainer profile: injury, split, goals, preferences).
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _merge_kinds(*kinds: Optional[str]) -> Optional[str]:
    """Union drink-`kind` labels into one deduped, order-preserving comma list.
    Each input may itself be a comma list ("beer, wine"); matching is
    case-insensitive so "beer" + "Beer" stays "beer". Returns None if empty."""
    seen: list[str] = []
    lowered = set()
    for k in kinds:
        if not k:
            continue
        for part in str(k).split(","):
            part = part.strip()
            if part and part.lower() not in lowered:
                seen.append(part)
                lowered.add(part.lower())
    return ", ".join(seen) or None


def _merge_notes(*notes: Optional[str]) -> Optional[str]:
    """Join non-empty notes with '; ', skipping exact duplicates. Returns None
    if nothing to keep (so empty appends don't litter a day's row)."""
    seen: list[str] = []
    for n in notes:
        if not n:
            continue
        n = str(n).strip()
        if n and n not in seen:
            seen.append(n)
    return "; ".join(seen) or None


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        # migrate older DBs
        ecols = [r["name"] for r in conn.execute("PRAGMA table_info(entries)")]
        if "raw_body" not in ecols:
            conn.execute("ALTER TABLE entries ADD COLUMN raw_body TEXT")
        pcols = [r["name"] for r in conn.execute("PRAGMA table_info(people)")]
        for col in ("summary", "email", "phone", "address"):
            if col not in pcols:
                conn.execute(f"ALTER TABLE people ADD COLUMN {col} TEXT")
        scols = [r["name"] for r in conn.execute("PRAGMA table_info(sets)")]
        for col, decl in (("duration_seconds", "INTEGER"), ("distance_miles", "REAL"),
                          ("target_weight_lbs", "REAL"), ("target_reps", "INTEGER"),
                          ("status", "TEXT NOT NULL DEFAULT 'done'")):
            if col not in scols:
                conn.execute(f"ALTER TABLE sets ADD COLUMN {col} {decl}")
        wcols = [r["name"] for r in conn.execute("PRAGMA table_info(workouts)")]
        if "status" not in wcols:
            conn.execute("ALTER TABLE workouts ADD COLUMN status TEXT NOT NULL DEFAULT 'done'")
        xcols = [r["name"] for r in conn.execute("PRAGMA table_info(exercises)")]
        if "image_link" not in xcols:
            conn.execute("ALTER TABLE exercises ADD COLUMN image_link TEXT")
        # Columns added when the catalog was lined up with free-exercise-db + rotation.
        for col, decl in (("slug", "TEXT"), ("force", "TEXT"), ("level", "TEXT"),
                          ("mechanic", "TEXT"),
                          ("in_rotation", "INTEGER NOT NULL DEFAULT 0")):
            if col not in xcols:
                conn.execute(f"ALTER TABLE exercises ADD COLUMN {col} {decl}")
        # Index lives here (not in SCHEMA) so it's created only after in_rotation exists.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exercises_rotation ON exercises(in_rotation)")
        # Muscle vocabulary now mirrors free-exercise-db (see MUSCLES); rename any rows
        # stored under the old labels so existing data still aggregates. OR IGNORE skips a
        # rename that would collide with a row already in the target tier; the trailing
        # DELETE then clears those now-redundant legacy rows. Idempotent (old labels gone
        # after the first run).
        for old, new in (("abs", "abdominals"), ("obliques", "abdominals"),
                         ("quads", "quadriceps"), ("upper back", "middle back")):
            conn.execute("UPDATE OR IGNORE exercise_muscles SET muscle=? WHERE muscle=?",
                         (new, old))
        conn.execute(
            "DELETE FROM exercise_muscles WHERE muscle IN ('abs','obliques','quads','upper back')")
        # Backfill any legacy rows that predate the status column: existing workouts
        # and sets are completed history, so they read back as 'done'.
        conn.execute("UPDATE workouts SET status='done' WHERE status IS NULL OR status=''")
        conn.execute("UPDATE sets SET status='done' WHERE status IS NULL OR status=''")
        # Drinks are now one row per day. Collapse any legacy multi-row days
        # (sum the drinks, merge the kinds/notes onto the earliest row) before
        # enforcing the unique index — an old DB's non-unique index survives the
        # IF NOT EXISTS in SCHEMA, so rebuild it here.
        dup_days = conn.execute(
            "SELECT drink_date FROM drinks GROUP BY drink_date HAVING COUNT(*) > 1"
        ).fetchall()
        for d in dup_days:
            rows = conn.execute(
                "SELECT id, standard_drinks, kind, notes FROM drinks "
                "WHERE drink_date=? ORDER BY id", (d["drink_date"],),
            ).fetchall()
            keep = rows[0]["id"]
            total = sum(r["standard_drinks"] for r in rows)
            kind = _merge_kinds(*(r["kind"] for r in rows))
            notes = _merge_notes(*(r["notes"] for r in rows))
            conn.execute(
                "UPDATE drinks SET standard_drinks=?, kind=?, notes=? WHERE id=?",
                (total, kind, notes, keep),
            )
            conn.execute(
                "DELETE FROM drinks WHERE drink_date=? AND id<>?", (d["drink_date"], keep),
            )
        conn.execute("DROP INDEX IF EXISTS idx_drinks_date")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_drinks_date ON drinks(drink_date)")


# All user-facing dates in this log are Pacific. The user lives and logs on
# Pacific time, so "today", entry_date defaults, drink/workout dates, and streak
# math must roll over at Pacific midnight — not at the server's UTC midnight.
# (created_at stays UTC: it's an unambiguous storage timestamp, not a user date.)
PACIFIC = ZoneInfo("America/Los_Angeles")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def today() -> str:
    """Current calendar date (YYYY-MM-DD) in Pacific time — the canonical
    'today' for every date field in this log."""
    return datetime.now(PACIFIC).strftime("%Y-%m-%d")


def current_clock() -> dict:
    """Current Pacific date/time, broken out for surfacing to the model so it
    always knows what 'today'/'now' means before it defaults or computes dates."""
    dt = datetime.now(PACIFIC)
    return {
        "date": dt.strftime("%Y-%m-%d"),
        "time": dt.strftime("%H:%M"),
        "weekday": dt.strftime("%A"),
        "timezone": "America/Los_Angeles (Pacific)",
        "iso": dt.isoformat(),
    }


# --------------------------------------------------------------------------- #
# Input validation. The server trusts the model for judgment, not for data
# hygiene: a malformed date or an impossible number must not be silently stored,
# because it corrupts the very summaries (sober streak, recency) this log exists
# to produce. These guards are deterministic and return a plain {"error": ...}.
# --------------------------------------------------------------------------- #

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _bad_date(value: Optional[str], field: str) -> Optional[dict]:
    """Return an error dict if `value` is a non-null but invalid date, else None.
    Requires strict YYYY-MM-DD that is also a real calendar date (Pacific)."""
    if value is None:
        return None
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return {"error": f"{field} must be YYYY-MM-DD, got {value!r}"}
    try:
        date.fromisoformat(value)
    except ValueError:
        return {"error": f"{field} is not a real calendar date: {value!r}"}
    return None


def _bad_set(s: dict) -> Optional[str]:
    """Return a reason string if a set's numbers are out of range, else None."""
    rpe = s.get("rpe")
    if rpe is not None and not (1 <= rpe <= 10):
        return f"rpe must be between 1 and 10, got {rpe}"
    reps = s.get("reps")
    if reps is not None and reps < 0:
        return f"reps must be >= 0, got {reps}"
    w = s.get("weight_lbs")
    if w is not None and w < 0:
        return f"weight_lbs must be >= 0, got {w}"
    dur = s.get("duration_seconds")
    if dur is not None and dur < 0:
        return f"duration_seconds must be >= 0, got {dur}"
    dist = s.get("distance_miles")
    if dist is not None and dist < 0:
        return f"distance_miles must be >= 0, got {dist}"
    return None


def phonetic(s: str) -> str:
    return jellyfish.metaphone(s or "")


# --------------------------------------------------------------------------- #
# Matching  (the deterministic half of resolution)
# --------------------------------------------------------------------------- #

def score_surface_against_alias(surface: str, alias: str) -> float:
    """0..1 similarity. Exact match wins; phonetic agreement floors the score."""
    s, a = surface.lower().strip(), alias.lower().strip()
    if not s or not a:
        return 0.0
    if s == a:
        return 1.0
    jw = jellyfish.jaro_winkler_similarity(s, a)
    if phonetic(surface) and phonetic(surface) == phonetic(alias):
        jw = max(jw, 0.88)  # sounds-the-same floor (handles transcription noise)
    return round(jw, 3)


def find_candidates(conn: sqlite3.Connection, surface: str, limit: int = 5):
    """Best score per person against all of that person's aliases + canonical name."""
    rows = conn.execute(
        """
        SELECT p.id AS person_id, p.canonical_name, p.role, a.surface_form AS alias
        FROM people p
        LEFT JOIN aliases a ON a.person_id = p.id
        """
    ).fetchall()
    best: dict[int, dict] = {}
    for r in rows:
        forms = [r["canonical_name"]]
        if r["alias"]:
            forms.append(r["alias"])
        sc = max(score_surface_against_alias(surface, f) for f in forms)
        cur = best.get(r["person_id"])
        if cur is None or sc > cur["score"]:
            label = r["canonical_name"]
            if r["role"]:
                label = f'{label} ({r["role"]})'
            best[r["person_id"]] = {
                "person_id": r["person_id"],
                "name": label,
                "score": sc,
            }
    out = sorted(best.values(), key=lambda c: c["score"], reverse=True)
    return [c for c in out if c["score"] >= 0.6][:limit]


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

@mcp.tool()
def add_journal_entry(body: str, raw_body: Optional[str] = None,
                      mentions: Optional[list[str]] = None,
                      entry_date: Optional[str] = None) -> dict:
    """Save a journal entry and match any people named in it.

    ALWAYS call this to capture an entry — never block on resolution.

    ONE NOTE PER TOPIC. A single conversation often spans several unrelated
    threads (e.g. dinner with the family, then a frustrating meeting with the
    boss). Save each unrelated thread as its OWN entry — call this tool once per
    topic — so each note is self-contained and its people don't bleed across
    contexts. This keeps later lookups clean: pulling history for the boss should
    surface the meeting, never the dinner that only happened to be told the same
    day. Granularity: a single event involving several people stays ONE entry
    (dinner with wife + parents = one note, all three mentioned together);
    split only genuinely separate events/threads. The entries are independent —
    there is no shared conversation id and they aren't cross-linked.

    Write `body` as a clean, structured, concise journal entry: organize the
    free-association into readable prose, keep the substance and the user's voice,
    drop filler. Pass the user's original words verbatim as `raw_body` so a faithful
    record is retained underneath (retrievable via get_entry; not shown in normal
    search or history). When you split a conversation into several notes, each
    note's `raw_body` is the slice of the verbatim words about THAT topic — not
    the whole transcript repeated on every entry. Extract the people referenced
    and pass each as a short surface form — what was actually SAID ("Tom", "Dad",
    a garbled transcription), taken from the raw words, not the cleaned-up name.

    COLLECTIVES / PLURALS. A plural reference names MORE THAN ONE person — "my
    parents", "the kids", "the in-laws", "Robin's folks" — so EXPAND it into one
    surface form per person it covers, using who you know from get_briefing /
    context (e.g. "my parents" -> ["Mom", "Dad"], which then link to both people).
    Never collapse a group down to a single person. If you only know some of the
    members, pass the ones you know and ask about the rest rather than dropping
    them — capture still never blocks. The reference is relative to the speaker, so
    use the snippet to tell whose: "my parents" and "Robin's parents" are
    different pairs; expand each to the right people.

    Resolution guidance for the model after this returns (each expanded surface
    form resolves independently):
      - One candidate with score >= 0.85 and no other within 0.15: link it
        silently via link_mentions (set learn_alias=True if the surface form
        wasn't already an exact alias).
      - Two close candidates (e.g. two people named Tom): ask the user which one,
        using context, then link.
      - No candidate >= 0.6: likely a new person. Ask, then save_person (no
        person_id) and link — or leave it pending if the user says they'll explain
        later.

    Args:
        body: The cleaned, structured journal entry.
        raw_body: The user's verbatim input. Optional but recommended.
        mentions: Surface forms of people referenced, e.g. ["Tom", "Robin"].
            Expand a plural/collective ("my parents") into one form per person
            (see COLLECTIVES) — never a single form for the whole group.
        entry_date: Day the entry is ABOUT as YYYY-MM-DD. Defaults to today.
    """
    if err := _bad_date(entry_date, "entry_date"):
        return err
    entry_date = entry_date or today()
    snippet_source = raw_body or body
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO entries(body, raw_body, entry_date, created_at) VALUES (?,?,?,?)",
            (body, raw_body, entry_date, now()),
        )
        entry_id = cur.lastrowid
        results = []
        for surface in (mentions or []):
            snippet = _snippet(snippet_source, surface)
            mid = conn.execute(
                """INSERT INTO mentions(entry_id, surface_form, context_snippet,
                   status, created_at) VALUES (?,?,?, 'pending', ?)""",
                (entry_id, surface, snippet, now()),
            ).lastrowid
            results.append({
                "mention_id": mid,
                "surface_form": surface,
                "candidates": find_candidates(conn, surface),
            })
    return {"entry_id": entry_id, "entry_date": entry_date, "mentions": results}


@mcp.tool()
def link_mentions(links: list[dict]) -> dict:
    """Resolve pending mentions to people.

    Args:
        links: list of {"mention_id": int, "person_id": int, "learn_alias": bool}.
            Set learn_alias True to store the mention's surface form as an alias on
            that person, so the same word (including a recurring transcription
            error) auto-matches next time.
    """
    linked, skipped = [], []
    with db() as conn:
        for ln in links:
            mid, pid = ln["mention_id"], ln["person_id"]
            m = conn.execute("SELECT surface_form FROM mentions WHERE id=?", (mid,)).fetchone()
            if not m:
                skipped.append({"mention_id": mid, "reason": "no such mention"})
                continue
            if not conn.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone():
                skipped.append({"mention_id": mid, "reason": f"no person with id {pid}"})
                continue
            conn.execute(
                "UPDATE mentions SET person_id=?, status='resolved' WHERE id=?",
                (pid, mid),
            )
            if ln.get("learn_alias"):
                conn.execute(
                    """INSERT OR IGNORE INTO aliases(person_id, surface_form,
                       phonetic_key, source) VALUES (?,?,?, 'learned')""",
                    (pid, m["surface_form"], phonetic(m["surface_form"])),
                )
            linked.append(mid)
    out = {"linked": linked}
    if skipped:
        out["skipped"] = skipped
    return out


@mcp.tool()
def save_person(person_id: Optional[int] = None, canonical_name: Optional[str] = None,
                role: Optional[str] = None, notes: Optional[str] = None,
                summary: Optional[str] = None, email: Optional[str] = None,
                phone: Optional[str] = None, address: Optional[str] = None,
                aliases: Optional[list[str]] = None,
                groups: Optional[list[str]] = None) -> dict:
    """Create or update a person (an entity) — the one write tool for people.

    Omit `person_id` to CREATE (then `canonical_name` is required); pass `person_id`
    to UPDATE an existing person (only the non-null fields you pass are written). `role`
    is the disambiguator the user relies on later, e.g. "father", "law school friend";
    `summary` is a short rolling profile for context.

    `aliases` are surface forms (incl. recurring transcription errors): on create they
    seed the person, on update they are ADDED — so this is also how you attach a new
    alias to someone later. `groups` are circle names like ["family"], created if new;
    passing `groups` REPLACES the person's circle membership. Returns the person_id and
    whether it was newly created."""
    with db() as conn:
        if person_id is None:
            if not canonical_name:
                return {"error": "canonical_name is required to create a person"}
            person_id = conn.execute(
                """INSERT INTO people(canonical_name, role, notes, summary, email, phone,
                   address, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                (canonical_name, role, notes, summary, email, phone, address, now()),
            ).lastrowid
            created, updated = True, []
        else:
            if not conn.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone():
                return {"error": f"no person with id {person_id}"}
            created = False
            fields = {"canonical_name": canonical_name, "role": role, "notes": notes,
                      "summary": summary, "email": email, "phone": phone, "address": address}
            sets = {k: v for k, v in fields.items() if v is not None}
            if sets:
                cols = ", ".join(f"{k}=?" for k in sets)
                conn.execute(f"UPDATE people SET {cols} WHERE id=?", (*sets.values(), person_id))
            updated = list(sets)
        for a in (aliases or []):
            conn.execute(
                """INSERT OR IGNORE INTO aliases(person_id, surface_form, phonetic_key,
                   source) VALUES (?,?,?, 'manual')""",
                (person_id, a, phonetic(a)),
            )
        if groups is not None:
            conn.execute("DELETE FROM person_groups WHERE person_id=?", (person_id,))
            _set_groups(conn, person_id, groups)
    if created:
        return {"person_id": person_id, "created": True}
    return {"person_id": person_id, "created": False,
            "updated": updated + (["aliases"] if aliases else [])
                       + (["groups"] if groups is not None else [])}


@mcp.tool()
def list_pending_mentions(limit: int = 50) -> dict:
    """The resolution queue: mentions the user hasn't pinned to a person yet.
    Each comes with its context snippet and fresh candidate matches so you can
    walk the user through them later."""
    with db() as conn:
        rows = conn.execute(
            """SELECT m.id, m.surface_form, m.context_snippet, e.entry_date
               FROM mentions m JOIN entries e ON e.id = m.entry_id
               WHERE m.status='pending' ORDER BY e.entry_date DESC, m.id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "mention_id": r["id"],
                "surface_form": r["surface_form"],
                "context": r["context_snippet"],
                "entry_date": r["entry_date"],
                "candidates": find_candidates(conn, r["surface_form"]),
            })
    return {"pending": out, "count": len(out)}


@mcp.tool()
def list_people(query: Optional[str] = None,
                group: Optional[str] = None) -> dict:
    """Compact registry of known people (id, name, role, groups, alias count,
    last_mentioned date). Sorted most-recently-mentioned first (people never
    mentioned fall to the end, alphabetical). Optionally filter by a name/role
    fragment or a group name. Load this for context when starting a session."""
    with db() as conn:
        rows = conn.execute(
            """SELECT p.id, p.canonical_name, p.role,
                      (SELECT COUNT(*) FROM aliases a WHERE a.person_id=p.id) AS aliases,
                      (SELECT MAX(e.entry_date) FROM mentions m
                         JOIN entries e ON e.id = m.entry_id
                        WHERE m.person_id = p.id) AS last_mentioned
               FROM people p
               ORDER BY last_mentioned IS NULL, last_mentioned DESC, p.canonical_name"""
        ).fetchall()
        people = []
        for r in rows:
            grps = _groups_for(conn, r["id"])
            if query and not (query.lower() in (r["canonical_name"] or "").lower()
                              or (r["role"] and query.lower() in r["role"].lower())):
                continue
            if group and group.lower() not in [g.lower() for g in grps]:
                continue
            people.append({"person_id": r["id"], "name": r["canonical_name"],
                           "role": r["role"], "groups": grps, "aliases": r["aliases"],
                           "last_mentioned": r["last_mentioned"]})
    return {"people": people, "count": len(people)}


@mcp.tool()
def get_person_history(person_id: int, limit: int = 50,
                       since: Optional[str] = None,
                       max_chars: int = 600) -> dict:
    """Every entry that mentions this person, newest first — the payoff query.
    This is an indexed lookup on the entity, so 'everything about Tom my father'
    never pulls in the other Tom. Bodies are truncated to max_chars."""
    if err := _bad_date(since, "since"):
        return err
    sql = """SELECT DISTINCT e.id, e.entry_date, e.body
             FROM entries e JOIN mentions m ON m.entry_id=e.id
             WHERE m.person_id=? AND m.status='resolved'"""
    params: list = [person_id]
    if since:
        sql += " AND e.entry_date >= ?"
        params.append(since)
    sql += " ORDER BY e.entry_date DESC, e.id DESC LIMIT ?"
    params.append(limit)
    with db() as conn:
        person = conn.execute(
            "SELECT canonical_name, role FROM people WHERE id=?", (person_id,)
        ).fetchone()
        rows = conn.execute(sql, params).fetchall()
    if not person:
        return {"error": f"no person with id {person_id}"}
    entries = [
        {"entry_id": r["id"], "entry_date": r["entry_date"],
         "body": _truncate(r["body"], max_chars)}
        for r in rows
    ]
    return {"person_id": person_id, "name": person["canonical_name"],
            "role": person["role"], "entries": entries, "count": len(entries)}


@mcp.tool()
def search_entries(query: str, limit: int = 20, max_chars: int = 400) -> dict:
    """Full-text search over entry bodies (FTS5). Use for topics/events, not for
    people — use get_person_history for people."""
    with db() as conn:
        rows = conn.execute(
            """SELECT e.id, e.entry_date, e.body
               FROM entries_fts f JOIN entries e ON e.id = f.rowid
               WHERE entries_fts MATCH ? ORDER BY rank LIMIT ?""",
            (query, limit),
        ).fetchall()
    return {"results": [
        {"entry_id": r["id"], "entry_date": r["entry_date"],
         "body": _truncate(r["body"], max_chars)} for r in rows
    ], "count": len(rows)}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _snippet(body: str, surface: str, window: int = 60) -> str:
    i = body.lower().find(surface.lower())
    if i == -1:
        return _truncate(body, 2 * window)
    start, end = max(0, i - window), min(len(body), i + len(surface) + window)
    return ("…" if start else "") + body[start:end] + ("…" if end < len(body) else "")


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n].rstrip() + "…"


def _set_groups(conn: sqlite3.Connection, person_id: int,
                names: Optional[list[str]]) -> None:
    for name in (names or []):
        name = name.strip()
        if not name:
            continue
        conn.execute("INSERT OR IGNORE INTO groups(name) VALUES (?)", (name,))
        gid = conn.execute("SELECT id FROM groups WHERE name=?", (name,)).fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO person_groups(person_id, group_id) VALUES (?,?)",
            (person_id, gid),
        )


def _groups_for(conn: sqlite3.Connection, person_id: int) -> list[str]:
    rows = conn.execute(
        """SELECT g.name FROM groups g JOIN person_groups pg ON pg.group_id=g.id
           WHERE pg.person_id=? ORDER BY g.name""",
        (person_id,),
    ).fetchall()
    return [r["name"] for r in rows]


@mcp.tool()
def get_entry(entry_id: int, include_raw: bool = True) -> dict:
    """Fetch one full entry. Set include_raw to also return the verbatim original
    (raw_body) — the hidden fallback record kept in case the cleaned version
    dropped a detail."""
    with db() as conn:
        r = conn.execute(
            "SELECT id, body, raw_body, entry_date, created_at FROM entries WHERE id=?",
            (entry_id,),
        ).fetchone()
    if not r:
        return {"error": f"no entry with id {entry_id}"}
    out = {"entry_id": r["id"], "entry_date": r["entry_date"],
           "created_at": r["created_at"], "body": r["body"]}
    if include_raw:
        out["raw_body"] = r["raw_body"]
    return out


@mcp.tool()
def update_entry(entry_id: int, entry_date: Optional[str] = None,
                 body: Optional[str] = None, raw_body: Optional[str] = None,
                 mentions: Optional[list[str]] = None) -> dict:
    """Edit an existing journal entry. Only non-null args are written.

    Use `entry_date` (YYYY-MM-DD, Pacific) to correct the day an entry is ABOUT —
    e.g. the user said "that was actually yesterday". Dates are Pacific time; resolve
    relative phrases ("yesterday") against the current Pacific date (see get_briefing's
    `now`) before passing a concrete date here. `body` replaces the cleaned journal
    text; `raw_body` replaces the verbatim original.

    `mentions` reconciles WHO the entry references when you change the text. As in
    add_journal_entry, the server does NOT read the text — YOU pass the full new list
    of surface forms for the entry, and the rows are reconciled deterministically:
      - a surface form already on the entry is KEPT (its resolved person-link is
        preserved — you don't re-link people you'd already sorted out);
      - a NEW surface form is added as a pending mention; its candidates come back in
        the result so you can resolve it with link_mentions (learn_alias as usual);
      - a surface form no longer in the list has its mention row REMOVED. (Any alias
        learned from it stays on the person — aliases are independent of the entry.)
    Omit `mentions` (leave it null) to edit text/date only and leave mentions
    untouched — the common typo/date fix. Pass [] to clear all of the entry's mentions."""
    if err := _bad_date(entry_date, "entry_date"):
        return err
    fields = {"entry_date": entry_date, "body": body, "raw_body": raw_body}
    sets = {k: v for k, v in fields.items() if v is not None}
    if not sets and mentions is None:
        return {"entry_id": entry_id, "updated": []}
    with db() as conn:
        row = conn.execute(
            "SELECT body, raw_body FROM entries WHERE id=?", (entry_id,)
        ).fetchone()
        if not row:
            return {"error": f"no entry with id {entry_id}"}
        if sets:
            cols = ", ".join(f"{k}=?" for k in sets)
            conn.execute(f"UPDATE entries SET {cols} WHERE id=?", (*sets.values(), entry_id))
        out = {"entry_id": entry_id, "updated": list(sets)}
        if mentions is not None:
            # Snippet against the new text where given, else the stored text.
            snippet_source = (raw_body if raw_body is not None else row["raw_body"]) \
                or (body if body is not None else row["body"]) or ""
            existing = [dict(m) for m in conn.execute(
                "SELECT id, surface_form FROM mentions WHERE entry_id=?", (entry_id,)
            ).fetchall()]
            kept, created = [], []
            for surface in mentions:
                key = surface.lower()
                match = next((m for m in existing if m["surface_form"].lower() == key), None)
                if match:  # already referenced — keep its link, refresh its snippet
                    existing.remove(match)
                    kept.append(match["id"])
                    conn.execute(
                        "UPDATE mentions SET context_snippet=? WHERE id=?",
                        (_snippet(snippet_source, surface), match["id"]),
                    )
                else:  # newly referenced — queue it for resolution
                    mid = conn.execute(
                        """INSERT INTO mentions(entry_id, surface_form, context_snippet,
                           status, created_at) VALUES (?,?,?, 'pending', ?)""",
                        (entry_id, surface, _snippet(snippet_source, surface), now()),
                    ).lastrowid
                    created.append({
                        "mention_id": mid,
                        "surface_form": surface,
                        "candidates": find_candidates(conn, surface),
                    })
            removed = [m["id"] for m in existing]  # no longer referenced
            for mid in removed:
                conn.execute("DELETE FROM mentions WHERE id=?", (mid,))
            out["mentions"] = {"created": created, "kept": kept, "removed": removed}
        return out


def _delete_record(kind: str, id: int) -> dict:
    """Shared delete implementation behind both servers' delete_record tools. Maps
    `kind` to its table, deletes the row, and (for sets) renumbers the remaining
    set_index so it stays contiguous."""
    tables = {"entry": "entries", "drink": "drinks", "workout": "workouts",
              "set": "sets", "weight": "body_weight"}
    table = tables.get(kind)
    if not table:
        return {"error": f"unknown kind {kind!r}; use one of {sorted(tables)}"}
    with db() as conn:
        ctx = None
        if kind == "set":
            ctx = conn.execute(
                "SELECT workout_id, exercise_id FROM sets WHERE id=?", (id,)
            ).fetchone()
            if not ctx:
                return {"error": f"no set with id {id}"}
        cur = conn.execute(f"DELETE FROM {table} WHERE id=?", (id,))
        if cur.rowcount == 0:
            return {"error": f"no {kind} with id {id}"}
        if kind == "set":
            remaining = conn.execute(
                "SELECT id FROM sets WHERE workout_id=? AND exercise_id=? ORDER BY set_index",
                (ctx["workout_id"], ctx["exercise_id"]),
            ).fetchall()
            for i, row in enumerate(remaining, start=1):
                conn.execute("UPDATE sets SET set_index=? WHERE id=?", (i, row["id"]))
    return {"kind": kind, "id": id, "deleted": True}


@mcp.tool()
def delete_record(kind: str, id: int) -> dict:
    """Permanently delete one journal-side record. Irreversible — confirm first.

    `kind` selects what `id` refers to:
      - "entry" — a journal entry (its mentions go too; FTS stays in sync).
      - "drink" — one day's drink row (that day becomes sober again).
    Find ids with get_entry/search_entries or get_drink_summary(include_rows=True).
    (Workouts and sets are deleted via the trainer server's own delete_record.)"""
    if kind not in ("entry", "drink"):
        return {"error": f"unknown kind {kind!r}; this server deletes one of "
                         "['drink', 'entry'] (use the trainer server for workout/set)"}
    return _delete_record(kind, id)


@mcp.tool()
def merge_people(survivor_person_id: int, loser_person_id: int) -> dict:
    """Merge two person records that turn out to be the same human — e.g. if "Tom"
    and "Tom Smith" were created before they were linked. All of the loser's
    aliases, mentions, and group memberships move onto the survivor (duplicates
    deduped; the loser's canonical name becomes an alias so the surface form stays
    discoverable). The loser is then deleted. Irreversible.

    This is a relational merge only — the survivor's role/notes/summary/email/
    phone/address are NOT overwritten. The loser's fields are returned as
    `discarded_fields` so you can decide whether any are worth copying onto the
    survivor via save_person(person_id=survivor_person_id, …)."""
    if survivor_person_id == loser_person_id:
        return {"error": "survivor and loser must be different people"}
    with db() as conn:
        if not conn.execute("SELECT 1 FROM people WHERE id=?", (survivor_person_id,)).fetchone():
            return {"error": f"no person with id {survivor_person_id} (survivor)"}
        loser = conn.execute(
            """SELECT id, canonical_name, role, notes, summary, email, phone, address
               FROM people WHERE id=?""", (loser_person_id,)
        ).fetchone()
        if not loser:
            return {"error": f"no person with id {loser_person_id} (loser)"}
        # The loser's canonical name becomes a manual alias on the survivor — keeps the
        # surface form discoverable for matching once the loser row is gone.
        conn.execute(
            """INSERT OR IGNORE INTO aliases(person_id, surface_form, phonetic_key, source)
               VALUES (?,?,?, 'manual')""",
            (survivor_person_id, loser["canonical_name"], phonetic(loser["canonical_name"])),
        )
        # Aliases: UNIQUE(person_id, surface_form), so INSERT OR IGNORE handles dups.
        conn.execute(
            """INSERT OR IGNORE INTO aliases(person_id, surface_form, phonetic_key, source)
               SELECT ?, surface_form, phonetic_key, source FROM aliases WHERE person_id=?""",
            (survivor_person_id, loser_person_id),
        )
        moved_aliases = conn.execute(
            "DELETE FROM aliases WHERE person_id=?", (loser_person_id,)
        ).rowcount
        moved_mentions = conn.execute(
            "UPDATE mentions SET person_id=? WHERE person_id=?",
            (survivor_person_id, loser_person_id),
        ).rowcount
        # person_groups: PRIMARY KEY (person_id, group_id), so INSERT OR IGNORE handles dups.
        conn.execute(
            """INSERT OR IGNORE INTO person_groups(person_id, group_id)
               SELECT ?, group_id FROM person_groups WHERE person_id=?""",
            (survivor_person_id, loser_person_id),
        )
        moved_groups = conn.execute(
            "DELETE FROM person_groups WHERE person_id=?", (loser_person_id,)
        ).rowcount
        conn.execute("DELETE FROM people WHERE id=?", (loser_person_id,))
        discarded = {k: loser[k] for k in
                     ("role", "notes", "summary", "email", "phone", "address") if loser[k]}
    return {
        "survivor_person_id": survivor_person_id,
        "merged_person_id": loser_person_id,
        "merged_canonical_name": loser["canonical_name"],
        "moved": {"aliases": moved_aliases, "mentions": moved_mentions,
                  "groups": moved_groups},
        "discarded_fields": discarded,
    }


@mcp.tool()
def get_related_people(person_id: int, limit: int = 10) -> dict:
    """Emergent network: people most often mentioned in the same entries as this
    person, ranked by shared-entry count. No tagging required — this is derived
    from the journal itself, surfacing who gets talked about together."""
    with db() as conn:
        rows = conn.execute(
            """SELECT p.id, p.canonical_name, p.role, COUNT(*) AS shared
               FROM mentions m1
               JOIN mentions m2 ON m2.entry_id = m1.entry_id
                    AND m2.person_id != m1.person_id
               JOIN people p ON p.id = m2.person_id
               WHERE m1.person_id=? AND m1.status='resolved' AND m2.status='resolved'
               GROUP BY p.id ORDER BY shared DESC LIMIT ?""",
            (person_id, limit),
        ).fetchall()
    return {"person_id": person_id, "related": [
        {"person_id": r["id"], "name": r["canonical_name"],
         "role": r["role"], "shared_entries": r["shared"]} for r in rows
    ]}


@mcp.tool()
def get_briefing(recent_entries: int = 5) -> dict:
    """One-call session context. Returns the people roster (id, name, role, groups,
    short summary), the pending-mention count, the list of groups, and the most
    recent entries. Also returns `now`: the current Pacific date/time — all dates in
    this log are Pacific, so use it to anchor "today"/"yesterday" before defaulting
    or computing any entry_date. Call this at the start of a conversation so you know
    who and what the user is likely talking about."""
    with db() as conn:
        prows = conn.execute(
            "SELECT id, canonical_name, role, summary FROM people ORDER BY canonical_name"
        ).fetchall()
        roster = [
            {"person_id": r["id"], "name": r["canonical_name"], "role": r["role"],
             "groups": _groups_for(conn, r["id"]),
             "summary": _truncate(r["summary"], 160) if r["summary"] else None}
            for r in prows
        ]
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM mentions WHERE status='pending'"
        ).fetchone()["n"]
        grp = [r["name"] for r in conn.execute("SELECT name FROM groups ORDER BY name")]
        recent = conn.execute(
            "SELECT id, entry_date, body FROM entries ORDER BY entry_date DESC, id DESC LIMIT ?",
            (recent_entries,),
        ).fetchall()
    return {
        "now": current_clock(),
        "people": roster,
        "people_count": len(roster),
        "groups": grp,
        "pending_mentions": pending,
        "recent_entries": [
            {"entry_id": r["id"], "entry_date": r["entry_date"],
             "body": _truncate(r["body"], 200)} for r in recent
        ],
    }


# --------------------------------------------------------------------------- #
# Drinking + trainer helpers
# --------------------------------------------------------------------------- #

# Canonical muscle vocabulary — kept consistent so recency/volume aggregates line up.
# Deliberately MIRRORS the free-exercise-db vocabulary (scripts/import_exercises.py), so
# the imported library, its per-muscle filters, and the model's own enrichment all use
# one shared label set with no mapping. The model should use these labels when logging.
MUSCLES = [
    "abdominals", "abductors", "adductors", "biceps", "calves", "chest",
    "forearms", "glutes", "hamstrings", "lats", "lower back", "middle back",
    "neck", "quadriceps", "shoulders", "traps", "triceps",
]


def _days_since(d: Optional[str]) -> Optional[int]:
    if not d:
        return None
    try:
        return (date.fromisoformat(today()) - date.fromisoformat(d)).days
    except ValueError:
        return None


# Forgiving name resolution for the catalog. The library is large (~870) and a movement
# is often referred to slightly off ("incline db press" vs "Incline Dumbbell Press"), so
# resolution tries exact, then a spacing/punctuation-insensitive match, then a
# high-confidence fuzzy/phonetic match — the same shape as person-alias matching.
EX_MATCH_FLOOR = 0.6   # below this it isn't even offered as a candidate
EX_CONFIDENT = 0.97    # at/above this (with a clear lead) we resolve silently — set high
                       # on purpose: a one-letter swap on a short name ('Hack Squat' vs
                       # 'Back Squat') scores ~0.93, and those are DIFFERENT lifts, so
                       # anything that uncertain comes back as a candidate to confirm
                       # rather than being silently mis-resolved.


def _norm_ex(s: str) -> str:
    """Collapse a name to letters+digits, so 'Pull-up' / 'Pull Up' / 'pullup' match."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _score_exercise_name(surface: str, name: str) -> float:
    """0..1 similarity of a spoken name to a catalog name. Exact, or a punctuation/
    spacing-only difference, wins; phonetic agreement floors it (transcription noise)."""
    s, a = (surface or "").lower().strip(), (name or "").lower().strip()
    if not s or not a:
        return 0.0
    if s == a or _norm_ex(s) == _norm_ex(a):
        return 1.0
    jw = jellyfish.jaro_winkler_similarity(s, a)
    if phonetic(surface) and phonetic(surface) == phonetic(name):
        jw = max(jw, 0.88)  # sounds-the-same floor
    return round(jw, 3)


def _match_exercises(conn: sqlite3.Connection, name: str, limit: int = 5) -> list[dict]:
    """Rank the catalog against a spoken name, best first. Returns
    [{exercise_id, name, score, in_rotation, primary}] with score >= EX_MATCH_FLOOR, so a
    caller that can't confidently resolve a name can hand the user the closest real
    entries instead of guessing. In-rotation movements are listed first among equals."""
    rows = conn.execute("SELECT id, name, in_rotation FROM exercises ORDER BY name").fetchall()
    scored = sorted(
        ((_score_exercise_name(name, r["name"]), int(r["in_rotation"]), r) for r in rows),
        key=lambda t: (t[0], t[1]), reverse=True,
    )
    return [{"exercise_id": r["id"], "name": r["name"], "score": sc,
             "in_rotation": bool(r["in_rotation"]),
             "primary": _muscles_for(conn, r["id"])["primary"]}
            for sc, _rot, r in scored[:limit] if sc >= EX_MATCH_FLOOR]


def _resolve_exercise(conn: sqlite3.Connection, name: str):
    """Resolve a spoken name to ONE catalog row, or None. Tries exact (case-insensitive),
    then a punctuation/spacing- or phonetics-tolerant high-confidence fuzzy match (so
    'pullup' finds 'Pull Up' and a near-spelling lands on the right row instead of
    spawning a duplicate). A merely plausible name — below the confident bar, or with a
    near-tie runner-up — returns None, leaving the caller to surface `_match_exercises`
    candidates rather than silently pick the wrong lift."""
    name = (name or "").strip()
    if not name:
        return None
    row = conn.execute(
        "SELECT * FROM exercises WHERE lower(name)=lower(?)", (name,)
    ).fetchone()
    if row:
        return row
    m = _match_exercises(conn, name, limit=2)
    if m and m[0]["score"] >= EX_CONFIDENT and (len(m) == 1 or m[0]["score"] - m[1]["score"] >= 0.06):
        return conn.execute("SELECT * FROM exercises WHERE id=?", (m[0]["exercise_id"],)).fetchone()
    return None


def _muscles_for(conn: sqlite3.Connection, exercise_id: int) -> dict:
    """Muscles an exercise trains, split into the three emphasis tiers (each a list,
    omitted-empty fine). primary = the muscle(s) the lift is *for*; secondary = real
    assistance; tertiary = lightly involved. Tiers are how much each muscle is worked,
    so the model and the library can rank them."""
    rows = conn.execute(
        "SELECT muscle, role FROM exercise_muscles WHERE exercise_id=? ORDER BY role, muscle",
        (exercise_id,),
    ).fetchall()
    return {
        "primary": [r["muscle"] for r in rows if r["role"] == "primary"],
        "secondary": [r["muscle"] for r in rows if r["role"] == "secondary"],
        "tertiary": [r["muscle"] for r in rows if r["role"] == "tertiary"],
    }


def _set_muscles(conn: sqlite3.Connection, exercise_id: int,
                 primary: Optional[list[str]], secondary: Optional[list[str]],
                 tertiary: Optional[list[str]] = None) -> None:
    """Replace an exercise's muscle links across the three emphasis tiers. Runs only
    when at least one tier list is given; passing some-but-not-all clears the omitted
    tiers (the whole mapping is rewritten), so send every tier you want kept. A muscle
    named in more than one tier lands in the first (primary > secondary > tertiary)."""
    if primary is None and secondary is None and tertiary is None:
        return
    conn.execute("DELETE FROM exercise_muscles WHERE exercise_id=?", (exercise_id,))
    seen: set[str] = set()
    for role, names in (("primary", primary or []), ("secondary", secondary or []),
                        ("tertiary", tertiary or [])):
        for m in names:
            m = m.strip().lower()
            if m and m not in seen:
                seen.add(m)
                conn.execute(
                    """INSERT OR IGNORE INTO exercise_muscles(exercise_id, muscle, role)
                       VALUES (?,?,?)""",
                    (exercise_id, m, role),
                )


def _exercise_brief(conn: sqlite3.Connection, r) -> dict:
    return {"exercise_id": r["id"], "name": r["name"], "category": r["category"],
            "equipment": r["equipment"], "muscles": _muscles_for(conn, r["id"])}


def _get_profile(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT value FROM settings WHERE key='profile'").fetchone()
    return json.loads(row["value"]) if row else {}


# --------------------------------------------------------------------------- #
# Drinking tools
# --------------------------------------------------------------------------- #

@mcp.tool()
def log_drinks(standard_drinks: float, drink_date: Optional[str] = None,
               kind: Optional[str] = None, notes: Optional[str] = None) -> dict:
    """Log alcohol consumption for a day, in STANDARD-DRINK units.

    Convert what the user describes into standard drinks before calling: one
    regular beer (12oz/5%), one glass of wine (5oz), or one shot of spirits each
    count as ~1.0; a strong cocktail or a large pour is ~1.5; a tallboy/double is
    ~2.0. "Two beers and a glass of wine" -> standard_drinks=3.0.

    A day has EXACTLY ONE row. The first call of the day creates it; later calls
    for the same day accumulate onto it — `standard_drinks` ADD up and `kind`
    merges into a deduped list (a beer then a wine logs as 2.0, kind "beer, wine").
    So pass only the increment, not the running total. Days with no row are sober;
    to correct a day down (or overwrite it) use update_drink, which sets absolutes.

    Args:
        standard_drinks: Standard drinks to ADD for this day (the increment).
        drink_date: Day consumed, YYYY-MM-DD. Defaults to today.
        kind: Optional label, e.g. "beer", "wine", "cocktail".
        notes: Optional context, e.g. "dinner with Robin".
    """
    if err := _bad_date(drink_date, "drink_date"):
        return err
    if standard_drinks <= 0:
        return {"error": f"standard_drinks must be positive, got {standard_drinks}"}
    d = drink_date or today()
    with db() as conn:
        existing = conn.execute(
            "SELECT id, standard_drinks, kind, notes FROM drinks WHERE drink_date=?", (d,),
        ).fetchone()
        if existing:
            day_total = existing["standard_drinks"] + standard_drinks
            conn.execute(
                "UPDATE drinks SET standard_drinks=?, kind=?, notes=? WHERE id=?",
                (day_total, _merge_kinds(existing["kind"], kind),
                 _merge_notes(existing["notes"], notes), existing["id"]),
            )
            rid = existing["id"]
        else:
            rid = conn.execute(
                """INSERT INTO drinks(drink_date, standard_drinks, kind, notes, created_at)
                   VALUES (?,?,?,?,?)""",
                (d, standard_drinks, kind, notes, now()),
            ).lastrowid
            day_total = standard_drinks
    return {"drink_id": rid, "drink_date": d, "logged": standard_drinks,
            "day_total": round(day_total, 2)}


@mcp.tool()
def get_drink_summary(days: int = 30, since: Optional[str] = None,
                      until: Optional[str] = None, include_rows: bool = False) -> dict:
    """Drinking trends over a window: per-day totals plus rolling stats.

    Use the default `days` window, or pass an explicit `since`/`until` range. Sober
    days (no logged drinks) are counted, not stored. `current_sober_streak` is the
    number of days since the last drink (0 if the user drank today, null if never).

    Set `include_rows=True` to also get the individual drink rows WITH ids (`drinks`)
    — needed when the user wants to FIX a logged drink ("last night was really 1, not
    3"): find the `drink_id` here, then pass it to update_drink or delete_record.

    Args:
        days: Size of the trailing window in days (ignored if `since` is given).
        since: Start date YYYY-MM-DD (inclusive).
        until: End date YYYY-MM-DD (inclusive). Defaults to today.
        include_rows: Also return individual drink rows (with `drink_id`) for editing.
    """
    if err := _bad_date(since, "since") or _bad_date(until, "until"):
        return err
    until = until or today()
    if since is None:
        since = date.fromordinal(
            date.fromisoformat(until).toordinal() - max(days, 1) + 1).isoformat()
    with db() as conn:
        rows = conn.execute(
            """SELECT drink_date, ROUND(SUM(standard_drinks),2) AS total
               FROM drinks WHERE drink_date BETWEEN ? AND ?
               GROUP BY drink_date ORDER BY drink_date DESC""",
            (since, until),
        ).fetchall()
        last = conn.execute("SELECT MAX(drink_date) AS d FROM drinks").fetchone()["d"]
        row_list = None
        if include_rows:
            rr = conn.execute(
                """SELECT id, drink_date, standard_drinks, kind, notes FROM drinks
                   WHERE drink_date BETWEEN ? AND ?
                   ORDER BY drink_date DESC, id DESC""",
                (since, until),
            ).fetchall()
            row_list = [{"drink_id": r["id"], "drink_date": r["drink_date"],
                         "standard_drinks": r["standard_drinks"], "kind": r["kind"],
                         "notes": r["notes"]} for r in rr]
    daily = [{"date": r["drink_date"], "total": r["total"]} for r in rows]
    window_days = date.fromisoformat(until).toordinal() - date.fromisoformat(since).toordinal() + 1
    total = round(sum(d["total"] for d in daily), 2)
    drinking_days = len(daily)
    out = {
        "since": since, "until": until, "window_days": window_days,
        "daily": daily,
        "total_standard_drinks": total,
        "drinking_days": drinking_days,
        "sober_days": max(window_days - drinking_days, 0),
        "avg_per_day": round(total / window_days, 2) if window_days else 0,
        "avg_per_drinking_day": round(total / drinking_days, 2) if drinking_days else 0,
        "current_sober_streak": _days_since(last),
    }
    if row_list is not None:
        out["drinks"] = row_list
    return out


@mcp.tool()
def update_drink(drink_id: int, standard_drinks: Optional[float] = None,
                 drink_date: Optional[str] = None, kind: Optional[str] = None,
                 notes: Optional[str] = None) -> dict:
    """Correct a logged day's drink row. Only non-null args are written; the values
    are absolute (they REPLACE, not add — unlike log_drinks, which accumulates).

    Use this to fix a mistake in either direction — e.g. set `standard_drinks` to 1
    when the day was over-logged (log_drinks only adds, so corrections downward go
    through here), relabel `kind`, or move the day with `drink_date` (YYYY-MM-DD,
    Pacific). Since a day has exactly one row, moving onto a day that already has one
    is refused — edit that day instead. Find the `drink_id` with
    get_drink_summary(include_rows=True). To remove a day entirely use
    delete_record(kind="drink", id=...)."""
    if err := _bad_date(drink_date, "drink_date"):
        return err
    if standard_drinks is not None and standard_drinks <= 0:
        return {"error": f"standard_drinks must be positive, got {standard_drinks}"}
    fields = {"standard_drinks": standard_drinks, "drink_date": drink_date,
              "kind": kind, "notes": notes}
    sets = {k: v for k, v in fields.items() if v is not None}
    if not sets:
        return {"drink_id": drink_id, "updated": []}
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM drinks WHERE id=?", (drink_id,)).fetchone()
        if not exists:
            return {"error": f"no drink with id {drink_id}"}
        if drink_date is not None:
            clash = conn.execute(
                "SELECT id FROM drinks WHERE drink_date=? AND id<>?", (drink_date, drink_id),
            ).fetchone()
            if clash:
                return {"error": f"{drink_date} already has a drink row (id {clash['id']}); "
                                 "edit that day instead"}
        cols = ", ".join(f"{k}=?" for k in sets)
        conn.execute(f"UPDATE drinks SET {cols} WHERE id=?", (*sets.values(), drink_id))
    return {"drink_id": drink_id, "updated": list(sets)}


# --------------------------------------------------------------------------- #
# Trainer: exercise catalog
# --------------------------------------------------------------------------- #

def _upsert_exercise(*, name, exercise_id, category, equipment, muscles,
                     secondary_muscles, tertiary_muscles, technique_notes,
                     common_mistakes, cautions, video_link, image_link, slug, force,
                     level, mechanic, in_rotation, allow_create: bool) -> dict:
    """Shared worker behind save_exercise (enrich-only, allow_create=False) and
    create_exercise (the website's add form, allow_create=True). New rows are born ONLY
    when allow_create is set, which the model-facing tool never passes — that's what keeps
    the catalog growable by the user's hand alone."""
    if in_rotation is not None:
        in_rotation = int(bool(in_rotation))
    with db() as conn:
        if exercise_id is not None:
            row = conn.execute("SELECT id FROM exercises WHERE id=?", (exercise_id,)).fetchone()
            if not row:
                return {"error": f"no exercise with id {exercise_id}"}
        elif name:
            row = _resolve_exercise(conn, name)
        else:
            return {"error": "pass name or exercise_id"}
        # Writable scalar columns (everything but name/created_at and the muscle tiers).
        fields = {"slug": slug, "category": category, "force": force, "level": level,
                  "mechanic": mechanic, "equipment": equipment,
                  "technique_notes": technique_notes, "common_mistakes": common_mistakes,
                  "cautions": cautions, "video_link": video_link, "image_link": image_link,
                  "in_rotation": in_rotation}
        sets_ = {k: v for k, v in fields.items() if v is not None}
        if row:
            eid = row["id"]
            created = False
            if sets_:
                cols = ", ".join(f"{k}=?" for k in sets_)
                conn.execute(f"UPDATE exercises SET {cols} WHERE id=?", (*sets_.values(), eid))
        elif allow_create:
            cols = ["name", *sets_, "created_at"]
            vals = [name.strip(), *sets_.values(), now()]
            ph = ", ".join("?" for _ in cols)
            eid = conn.execute(
                f"INSERT INTO exercises({', '.join(cols)}) VALUES ({ph})", vals,
            ).lastrowid
            created = True
        else:
            # Closed to the assistant: it can't conjure a new exercise. Hand back the
            # closest real entries so it programs from those (or asks the user to add it).
            return {"error": f"{name!r} isn't in the library — add it from the library "
                             "page first (the catalog is closed to the assistant)",
                    "candidates": _match_exercises(conn, name or "")}
        _set_muscles(conn, eid, muscles, secondary_muscles, tertiary_muscles)
        out_name = conn.execute("SELECT name FROM exercises WHERE id=?", (eid,)).fetchone()["name"]
    if created:
        return {"exercise_id": eid, "name": out_name, "created": True}
    muscles_changed = (muscles is not None or secondary_muscles is not None
                       or tertiary_muscles is not None)
    return {"exercise_id": eid, "name": out_name, "created": False,
            "updated": list(sets_) + (["muscles"] if muscles_changed else [])}


@trainer_mcp.tool()
def save_exercise(name: Optional[str] = None, exercise_id: Optional[int] = None,
                  category: Optional[str] = None, equipment: Optional[str] = None,
                  muscles: Optional[list[str]] = None,
                  secondary_muscles: Optional[list[str]] = None,
                  tertiary_muscles: Optional[list[str]] = None,
                  technique_notes: Optional[str] = None,
                  common_mistakes: Optional[str] = None,
                  cautions: Optional[str] = None,
                  video_link: Optional[str] = None,
                  image_link: Optional[str] = None,
                  slug: Optional[str] = None,
                  force: Optional[str] = None,
                  level: Optional[str] = None,
                  mechanic: Optional[str] = None,
                  in_rotation: Optional[bool] = None) -> dict:
    """ENRICH an exercise already in the LIBRARY (browsable at /trainer/library), or set
    its rotation flag. The catalog is CLOSED to you: you CANNOT create exercises — the
    library is pre-loaded from free-exercise-db, and any new movement is added by the user
    on the website's add form, never the chat. A `name` that isn't on file comes back as
    an error with `candidates` (the closest real entries); program from those or ask the
    user to add the movement on the library page.

    Target an existing entry by `exercise_id` or `name` (resolved fuzzily, so a near-
    spelling lands on the right row) and pass the fields to update — `technique_notes`
    (how to do it), `common_mistakes`, `cautions` (injury caveats, e.g. the user's left-
    shoulder limits), `equipment`, `category`, the dataset descriptors `force`
    (push/pull/static), `level`, `mechanic` (compound/isolation), and the muscle tiers.
    Only non-null fields are written — this is how you keep /trainer from showing "No
    saved technique notes yet". Pass `in_rotation=True` to add it to the programming pool
    (or prefer the dedicated `set_rotation` tool; logging a movement adds it for you).

    MUSCLES come in three EMPHASIS tiers — `muscles` (primary: what the lift is for),
    `secondary_muscles` (real assistance), and `tertiary_muscles` (lightly involved) —
    so "how hard each muscle is worked" is captured, e.g. a Kettlebell Thruster is
    muscles=["shoulders"], secondary_muscles=["quadriceps","glutes"],
    tertiary_muscles=["triceps"]. Passing ANY tier REPLACES the whole mapping (a muscle
    listed in two tiers lands in the higher one), so send every tier you want kept. Use
    the canonical muscle labels so recency lines up (they mirror the imported library's
    vocabulary): abdominals, abductors, adductors, biceps, calves, chest, forearms,
    glutes, hamstrings, lats, lower back, middle back, neck, quadriceps, shoulders, traps,
    triceps. Returns the exercise_id and the fields updated."""
    return _upsert_exercise(
        name=name, exercise_id=exercise_id, category=category, equipment=equipment,
        muscles=muscles, secondary_muscles=secondary_muscles, tertiary_muscles=tertiary_muscles,
        technique_notes=technique_notes, common_mistakes=common_mistakes, cautions=cautions,
        video_link=video_link, image_link=image_link, slug=slug, force=force, level=level,
        mechanic=mechanic, in_rotation=in_rotation, allow_create=False)


def create_exercise(name: str, category: Optional[str] = None, equipment: Optional[str] = None,
                    muscles: Optional[list[str]] = None,
                    secondary_muscles: Optional[list[str]] = None,
                    tertiary_muscles: Optional[list[str]] = None,
                    technique_notes: Optional[str] = None,
                    common_mistakes: Optional[str] = None,
                    cautions: Optional[str] = None,
                    video_link: Optional[str] = None,
                    image_link: Optional[str] = None,
                    slug: Optional[str] = None,
                    force: Optional[str] = None,
                    level: Optional[str] = None,
                    mechanic: Optional[str] = None,
                    in_rotation: Optional[bool] = None) -> dict:
    """Add a new exercise to the library — the trusted, NON-tool write path: the website's
    manual add form and the bulk importer (scripts/import_exercises.py) are the only
    callers, so the assistant (whose save_exercise can't create) never grows the catalog.
    A name already on file is updated rather than duplicated. `in_rotation` is left
    untouched by default (None) — the import keeps new library entries out of the rotation,
    while the add form passes in_rotation=True so a movement the user deliberately adds is
    ready to program. Returns like save_exercise."""
    return _upsert_exercise(
        name=name, exercise_id=None, category=category, equipment=equipment,
        muscles=muscles, secondary_muscles=secondary_muscles, tertiary_muscles=tertiary_muscles,
        technique_notes=technique_notes, common_mistakes=common_mistakes, cautions=cautions,
        video_link=video_link, image_link=image_link, slug=slug, force=force, level=level,
        mechanic=mechanic, in_rotation=in_rotation, allow_create=True)


@trainer_mcp.tool()
def set_rotation(name: Optional[str] = None, exercise_id: Optional[int] = None,
                 in_rotation: bool = True) -> dict:
    """Add an exercise to (or remove it from) the user's ROTATION — the curated pool of
    movements they actually train. The rotation is the ONLY set you program routines from
    (the wider catalog is a browsable reference library, ~870 movements, that the user
    searches to decide what to add). Target by `exercise_id` or `name` (case-insensitive);
    an unknown name is an error here (add it with save_exercise first). Pass
    in_rotation=False to take it out. Logging a movement the user actually did already
    flags it into rotation automatically, so reach for this when curating ahead of time —
    e.g. the user says "add Bulgarian split squats to my rotation" — or pruning."""
    in_rotation = int(bool(in_rotation))
    with db() as conn:
        if exercise_id is not None:
            row = conn.execute("SELECT id, name FROM exercises WHERE id=?", (exercise_id,)).fetchone()
        elif name:
            row = _resolve_exercise(conn, name)
        else:
            return {"error": "pass name or exercise_id"}
        if not row:
            return {"error": "no matching exercise (create it with save_exercise first)",
                    "candidates": _match_exercises(conn, name or "")}
        conn.execute("UPDATE exercises SET in_rotation=? WHERE id=?", (in_rotation, row["id"]))
    return {"exercise_id": row["id"], "name": row["name"], "in_rotation": bool(in_rotation)}


@trainer_mcp.tool()
def exercises(name: Optional[str] = None, exercise_id: Optional[int] = None,
              muscle: Optional[str] = None, equipment: Optional[str] = None,
              category: Optional[str] = None, rotation_only: bool = False,
              similar_to: Optional[str] = None) -> dict:
    """Read the exercise catalog — one full record, a filtered list, or swap peers.

    ONE record — pass `name` or `exercise_id` to get a movement in full: muscle emphasis
    tiers (primary/secondary/tertiary), `level` (difficulty), `mechanic` (compound vs
    isolation), `force`, in_rotation, technique notes, common mistakes, cautions, and any
    video/image links, so you can coach proper form. An unresolved `name` returns
    `candidates` (the closest real entries) — pick from those, don't guess.

    A LIST — narrow the registry with `muscle` (matches any emphasis tier), an `equipment`
    fragment, `category`, or `rotation_only=True`. Rows are compact (name, category,
    equipment, muscles, `level`, `mechanic`, in_rotation) — `level`/`mechanic` let you
    weigh difficulty and pick compounds before isolation. The full catalog is large
    (~870), so when picking exercises for a session prefer rotation_only=True — that's the
    set the user actually trains from.

    A SWAP — `similar_to=<exercise>` returns the closest like-for-like peers: shares a
    primary muscle, ranked by same `mechanic` (compound↔compound, isolation↔isolation),
    then shared-muscle overlap, then whether it's already in rotation (itself excluded).
    Use it when a machine's taken or a movement bugs a joint."""
    with db() as conn:
        if name is not None or exercise_id is not None:
            if exercise_id is not None:
                r = conn.execute("SELECT * FROM exercises WHERE id=?", (exercise_id,)).fetchone()
            else:
                r = _resolve_exercise(conn, name)
            if not r:
                return {"error": "no matching exercise",
                        "candidates": _match_exercises(conn, name or "")}
            m = _muscles_for(conn, r["id"])
            return {"exercise_id": r["id"], "name": r["name"], "category": r["category"],
                    "equipment": r["equipment"], "muscles": m,
                    "force": r["force"], "level": r["level"], "mechanic": r["mechanic"],
                    "in_rotation": bool(r["in_rotation"]),
                    "technique_notes": r["technique_notes"],
                    "common_mistakes": r["common_mistakes"], "cautions": r["cautions"],
                    "video_link": r["video_link"], "image_link": r["image_link"]}

        def _row(r):
            return {"exercise_id": r["id"], "name": r["name"], "category": r["category"],
                    "equipment": r["equipment"], "muscles": _muscles_for(conn, r["id"]),
                    "level": r["level"], "mechanic": r["mechanic"],
                    "in_rotation": bool(r["in_rotation"])}

        # Swap peers: shares a primary muscle, ranked by same mechanic / overlap / rotation.
        if similar_to is not None:
            target = _resolve_exercise(conn, similar_to)
            if not target:
                return {"error": f"no exercise matching {similar_to!r}",
                        "candidates": _match_exercises(conn, similar_to)}
            tp = set(_muscles_for(conn, target["id"])["primary"])
            peers = []
            for r in conn.execute("SELECT * FROM exercises ORDER BY name").fetchall():
                if r["id"] == target["id"]:
                    continue
                shared = tp & set(_muscles_for(conn, r["id"])["primary"])
                if not shared:
                    continue
                same_mech = bool(target["mechanic"]) and r["mechanic"] == target["mechanic"]
                peers.append((same_mech, len(shared), int(r["in_rotation"]), _row(r)))
            peers.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
            return {"similar_to": target["name"],
                    "exercises": [p[3] for p in peers], "count": len(peers)}

        rows = conn.execute("SELECT * FROM exercises ORDER BY name").fetchall()
        out = []
        for r in rows:
            if rotation_only and not r["in_rotation"]:
                continue
            m = _muscles_for(conn, r["id"])
            if muscle and muscle.strip().lower() not in (m["primary"] + m["secondary"] + m["tertiary"]):
                continue
            if equipment and (not r["equipment"] or equipment.lower() not in r["equipment"].lower()):
                continue
            if category and r["category"] != category:
                continue
            out.append(_row(r))
    return {"exercises": out, "count": len(out)}


# --------------------------------------------------------------------------- #
# Trainer: logging + retrieval
# --------------------------------------------------------------------------- #

@trainer_mcp.tool()
def log_workout(exercises: list[dict], workout_date: Optional[str] = None,
                focus: Optional[str] = None, feeling: Optional[str] = None,
                notes: Optional[str] = None, workout_id: Optional[int] = None) -> dict:
    """Record a training session — the whole thing in one call, or set-by-set as it
    happens.

    LOGGING AS YOU GO: to log a session incrementally (one exercise at a time during
    the workout), pass `workout_id` from the FIRST call's return on every later call
    so the sets append to the SAME session instead of creating a new one. Omit
    `workout_id` to start a new session (the default). Without this, separate calls
    for one workout fragment it into several sessions. New sets continue the set
    numbering per exercise. focus/feeling/notes on an append call are ignored — set
    them on the first call or with update_workout.

    Each item in `exercises` is:
        {
          "name": "Leg Press",
          "sets": [
            {"weight_lbs": 180, "reps": 10, "rpe": 7, "note": ""},
            {"weight_lbs": 180, "reps": 10, "rpe": 8},
            {"weight_lbs": 180, "reps": 8,  "rpe": 9.5, "note": "last one was a grind"}
          ]
        }
    Names resolve against the CLOSED catalog (fuzzily, so a near-spelling lands on the
    right lift). The model never invents an exercise: a name that doesn't match an
    existing one is SKIPPED and returned under `unmatched` with its closest `candidates`
    — re-log it under one of those, or have the user add the movement on the library page
    (the only way the catalog grows). The matched exercises still log (and join the
    rotation), so capture isn't lost. weight_lbs is null for bodyweight/cardio.
    rpe is 1-10 perceived exertion (10 = couldn't do another rep): it's how you
    judge whether to add weight next time. Returns the logged exercises (with the
    catalog's canonical names) and any `unmatched`.

    CARDIO (running, walking, rowing, cycling): log it as an exercise too — give it
    `muscles: []` (so it's a true cardio entry, not counted in muscle recency) and
    use a set per bout with `duration_seconds` and/or `distance_miles` instead of
    weight/reps. A 30-minute, 3.2-mile run is one set
    {"duration_seconds": 1800, "distance_miles": 3.2, "rpe": 6}. weight_lbs/reps stay
    null. Intervals can be one set each. Pass durations in SECONDS (25 min = 1500).

    Args:
        exercises: The exercises performed, each with its sets (see shape above).
        workout_date: Day trained, YYYY-MM-DD. Defaults to today.
        focus: Session focus, e.g. "Legs", "Arms", "Cardio".
        feeling: Overall how it felt / energy / soreness.
        notes: Anything else about the session.
        workout_id: Append to this existing session instead of starting a new one
            (see "LOGGING AS YOU GO" above).
    """
    if err := _bad_date(workout_date, "workout_date"):
        return err
    for ex in exercises:
        for s in (ex.get("sets") or []):
            if reason := _bad_set(s):
                return {"error": f"{ex.get('name','?')}: {reason}"}
    wd = workout_date or today()
    with db() as conn:
        if workout_id is not None:
            w = conn.execute(
                "SELECT id, workout_date FROM workouts WHERE id=?", (workout_id,)
            ).fetchone()
            if not w:
                return {"error": f"no workout with id {workout_id}"}
            wid = w["id"]
            wd = w["workout_date"]
        else:
            wid = conn.execute(
                "INSERT INTO workouts(workout_date, focus, feeling, notes, created_at) VALUES (?,?,?,?,?)",
                (wd, focus, feeling, notes, now()),
            ).lastrowid
        results, unmatched = [], []
        for ex in exercises:
            name = (ex.get("name") or "").strip()
            if not name:
                continue
            row = _resolve_exercise(conn, name)
            if not row:
                # Closed catalog: don't invent the exercise. Skip its sets and hand back
                # the nearest real names so the model re-logs under one that exists.
                unmatched.append({"name": name, "candidates": _match_exercises(conn, name)})
                continue
            eid = row["id"]
            # A logged movement is one they actually do → it joins the rotation (the pool
            # the trainer programs from). Pruning stays manual via set_rotation.
            conn.execute("UPDATE exercises SET in_rotation=1 WHERE id=?", (eid,))
            # continue set numbering if this exercise already has sets in the session
            start = (conn.execute(
                "SELECT COALESCE(MAX(set_index),0) AS m FROM sets WHERE workout_id=? AND exercise_id=?",
                (wid, eid),
            ).fetchone()["m"]) + 1
            for i, s in enumerate(ex.get("sets") or [], start=start):
                conn.execute(
                    """INSERT INTO sets(workout_id, exercise_id, set_index, weight_lbs,
                       reps, rpe, duration_seconds, distance_miles, note)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (wid, eid, i, s.get("weight_lbs"), s.get("reps"),
                     s.get("rpe"), s.get("duration_seconds"),
                     s.get("distance_miles"), s.get("note")),
                )
            results.append({"exercise_id": eid, "name": row["name"],
                            "sets": len(ex.get("sets") or [])})
    out = {"workout_id": wid, "workout_date": wd, "exercises": results,
           "appended": workout_id is not None}
    if unmatched:
        out["unmatched"] = unmatched
    return out


@trainer_mcp.tool()
def get_exercise_history(exercise_id: Optional[int] = None,
                         name: Optional[str] = None, limit: int = 10) -> dict:
    """Per-session performance for one exercise, newest first — the progressive-
    overload query. Each session lists its sets as weight/reps/rpe, so you can judge
    the next weight or rep target: e.g. all sets hit at RPE <=8 with clean form ->
    add weight; failures or RPE 10 short of target reps -> hold or deload. Pass
    either `exercise_id` or `name`.

    Each set also carries its `set_id` and `workout_id`, so this doubles as the
    discovery query for corrections: to fix a logged set ("my last squat was really
    185") find its `set_id` here and pass it to update_set or delete_record; to remove
    a whole session use its `workout_id` with delete_record(kind="workout")."""
    with db() as conn:
        if exercise_id is None and name is not None:
            r = _resolve_exercise(conn, name)
            if not r:
                return {"error": f"no exercise named {name!r}",
                        "candidates": _match_exercises(conn, name)}
            exercise_id = r["id"]
        ex = conn.execute("SELECT name FROM exercises WHERE id=?", (exercise_id,)).fetchone()
        if not ex:
            return {"error": "no matching exercise"}
        rows = conn.execute(
            """SELECT w.id AS wid, w.workout_date, s.id AS sid, s.set_index,
                      s.weight_lbs, s.reps, s.rpe, s.duration_seconds,
                      s.distance_miles, s.note
               FROM sets s JOIN workouts w ON w.id = s.workout_id
               WHERE s.exercise_id=? AND s.status='done'
               ORDER BY w.workout_date DESC, w.id DESC, s.set_index ASC""",
            (exercise_id,),
        ).fetchall()
    sessions: list[dict] = []
    seen: dict[int, dict] = {}
    for r in rows:
        sess = seen.get(r["wid"])
        if sess is None:
            sess = {"workout_id": r["wid"], "date": r["workout_date"], "sets": []}
            seen[r["wid"]] = sess
            sessions.append(sess)
        if len(sessions) > limit:
            continue
        st = {"set_id": r["sid"], "weight_lbs": r["weight_lbs"],
              "reps": r["reps"], "rpe": r["rpe"], "note": r["note"]}
        if r["duration_seconds"] is not None:
            st["duration_seconds"] = r["duration_seconds"]
        if r["distance_miles"] is not None:
            st["distance_miles"] = r["distance_miles"]
        sess["sets"].append(st)
    return {"exercise_id": exercise_id, "name": ex["name"],
            "sessions": sessions[:limit], "count": min(len(sessions), limit)}


@trainer_mcp.tool()
def get_personal_records(exercise_id: Optional[int] = None,
                         name: Optional[str] = None) -> dict:
    """Personal bests for one exercise — the data layer for "have I ever done X?"
    or "what's my heaviest Y?". Pass `exercise_id` or `name` (fuzzy-matched like
    get_exercise_history). Returns only the fields that apply to what's been
    logged; nothing for an empty exercise.

    Lift PRs (any set with weight+reps): `heaviest` (max weight_lbs), `most_reps`
    (most reps in a single set), `best_e1rm` (Epley estimate: w × (1 + reps/30)).
    Cardio PRs (sets with duration/distance): `longest_distance`,
    `longest_duration`, and `fastest_pace` (minutes per mile, only computed for
    sets where distance ≥ 1 mile, to avoid noisy warm-up bouts)."""
    with db() as conn:
        ex = (conn.execute("SELECT id, name FROM exercises WHERE id=?", (exercise_id,)).fetchone()
              if exercise_id is not None else _resolve_exercise(conn, name or ""))
        if not ex:
            return {"error": "no matching exercise",
                    "candidates": _match_exercises(conn, name or "")}
        rows = conn.execute(
            """SELECT s.id, s.weight_lbs, s.reps, s.rpe, s.duration_seconds,
                      s.distance_miles, w.workout_date
               FROM sets s JOIN workouts w ON w.id = s.workout_id
               WHERE s.exercise_id=? AND s.status='done'""",
            (ex["id"],),
        ).fetchall()

    def _brief(r: dict) -> dict:
        d = {"set_id": r["id"], "date": r["workout_date"]}
        for k in ("weight_lbs", "reps", "rpe", "duration_seconds", "distance_miles"):
            if r[k] is not None:
                d[k] = r[k]
        return d

    out = {"exercise_id": ex["id"], "name": ex["name"], "set_count": len(rows)}
    if heaviest := max((r for r in rows if r["weight_lbs"] is not None),
                       key=lambda r: r["weight_lbs"], default=None):
        out["heaviest"] = _brief(heaviest)
    if most_reps := max((r for r in rows if r["reps"] is not None),
                        key=lambda r: r["reps"], default=None):
        out["most_reps"] = _brief(most_reps)
    best_e1rm, best_e1rm_value = None, 0.0
    for r in rows:
        if r["weight_lbs"] and r["reps"]:
            e1rm = r["weight_lbs"] * (1 + r["reps"] / 30)
            if e1rm > best_e1rm_value:
                best_e1rm, best_e1rm_value = r, e1rm
    if best_e1rm:
        out["best_e1rm"] = {**_brief(best_e1rm),
                            "estimated_1rm_lbs": round(best_e1rm_value, 1)}
    if longest_dist := max((r for r in rows if r["distance_miles"] is not None),
                           key=lambda r: r["distance_miles"], default=None):
        out["longest_distance"] = _brief(longest_dist)
    if longest_dur := max((r for r in rows if r["duration_seconds"] is not None),
                          key=lambda r: r["duration_seconds"], default=None):
        out["longest_duration"] = _brief(longest_dur)
    paced = [(r, r["duration_seconds"] / r["distance_miles"] / 60) for r in rows
             if r["duration_seconds"] and r["distance_miles"] and r["distance_miles"] >= 1.0]
    if paced:
        fastest, mpm = min(paced, key=lambda x: x[1])
        out["fastest_pace"] = {**_brief(fastest), "minutes_per_mile": round(mpm, 2)}
    return out


@trainer_mcp.tool()
def update_workout(workout_id: int, workout_date: Optional[str] = None,
                   focus: Optional[str] = None, feeling: Optional[str] = None,
                   notes: Optional[str] = None,
                   append_note: Optional[str] = None) -> dict:
    """Edit a session's metadata. Only non-null args are written. Use `workout_date`
    (YYYY-MM-DD, Pacific) to move a session to the right day, or set focus/feeling/
    notes after the fact.

    `notes` REPLACES the note; `append_note` ADDS a line to whatever's already there
    (newline-joined) — use it to jot observations as they come up mid- or post-session
    ("right knee felt tight on the last set") without clobbering earlier notes. These
    notes resurface in get_fitness_briefing, so they're how a niggle today becomes a
    caution next session. Pass one or the other, not both.

    To change the SETS, use update_set, log_workout (with `workout_id` to append), or
    delete_record(kind="set"); to remove the whole session use
    delete_record(kind="workout")."""
    if err := _bad_date(workout_date, "workout_date"):
        return err
    fields = {"workout_date": workout_date, "focus": focus,
              "feeling": feeling, "notes": notes}
    sets = {k: v for k, v in fields.items() if v is not None}
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM workouts WHERE id=?", (workout_id,)).fetchone()
        if not exists:
            return {"error": f"no workout with id {workout_id}"}
        if append_note:
            cur = conn.execute("SELECT notes FROM workouts WHERE id=?", (workout_id,)).fetchone()
            existing = (cur["notes"] or "").strip()
            sets["notes"] = f"{existing}\n{append_note}".strip() if existing else append_note
        if not sets:
            return {"workout_id": workout_id, "updated": []}
        cols = ", ".join(f"{k}=?" for k in sets)
        conn.execute(f"UPDATE workouts SET {cols} WHERE id=?", (*sets.values(), workout_id))
    return {"workout_id": workout_id, "updated": list(sets)}


@trainer_mcp.tool()
def update_set(set_id: int, weight_lbs: Optional[float] = None,
               reps: Optional[int] = None, rpe: Optional[float] = None,
               duration_seconds: Optional[int] = None,
               distance_miles: Optional[float] = None,
               target_weight_lbs: Optional[float] = None,
               target_reps: Optional[int] = None,
               note: Optional[str] = None) -> dict:
    """Correct a single set. Only non-null args are written, so this can't blank a
    field back to NULL (e.g. clear a weight to mark bodyweight) — delete the set with
    delete_record(kind="set") and re-log it for that. Find the `set_id` with
    get_exercise_history (logged sets) or get_workout_plan (the active plan). `rpe` is
    1-10. `duration_seconds`/`distance_miles` are the cardio fields (run/walk/row).
    `target_weight_lbs`/`target_reps` retarget a still-pending planned set (e.g. bump
    the planned weight) without completing it — to actually log a planned set as done,
    use complete_set."""
    if reason := _bad_set({"weight_lbs": weight_lbs, "reps": reps, "rpe": rpe,
                           "duration_seconds": duration_seconds,
                           "distance_miles": distance_miles}):
        return {"error": reason}
    if reason := _bad_set({"weight_lbs": target_weight_lbs, "reps": target_reps}):
        return {"error": reason}
    fields = {"weight_lbs": weight_lbs, "reps": reps, "rpe": rpe,
              "duration_seconds": duration_seconds,
              "distance_miles": distance_miles,
              "target_weight_lbs": target_weight_lbs, "target_reps": target_reps,
              "note": note}
    sets = {k: v for k, v in fields.items() if v is not None}
    if not sets:
        return {"set_id": set_id, "updated": []}
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM sets WHERE id=?", (set_id,)).fetchone()
        if not exists:
            return {"error": f"no set with id {set_id}"}
        cols = ", ".join(f"{k}=?" for k in sets)
        conn.execute(f"UPDATE sets SET {cols} WHERE id=?", (*sets.values(), set_id))
    return {"set_id": set_id, "updated": list(sets)}


# --------------------------------------------------------------------------- #
# Trainer: the active workout PLAN (today's routine, in progress)
#
# A plan is just a `workouts` row with status='active' whose `sets` are 'pending'
# (targets filled, actuals NULL). Completing a set fills its actuals and flips it to
# 'done', so the plan becomes the historical log as you work through it — one table,
# no plan<->log reconciliation. The model designs the routine in conversation (from
# get_fitness_briefing + get_exercise_history) and writes it here; the server just
# stores/serves it. At most one active plan exists at a time.
# --------------------------------------------------------------------------- #

def _active_workout(conn: sqlite3.Connection):
    """The single in-progress plan (status='active'), or None."""
    return conn.execute(
        "SELECT * FROM workouts WHERE status='active' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def _expand_planned_sets(ex: dict) -> list[dict]:
    """Normalize an exercise's planned sets. Accepts an explicit `sets` list of
    {target_weight_lbs?, target_reps?, note?}, or the shorthand
    {set_count, target_reps?, target_weight_lbs?} which expands to that many identical
    planned sets."""
    sets = ex.get("sets")
    if sets:
        return sets
    n = ex.get("set_count")
    if n:
        return [{"target_weight_lbs": ex.get("target_weight_lbs"),
                 "target_reps": ex.get("target_reps")} for _ in range(int(n))]
    return []


def _bad_planned(exercises: list[dict]) -> Optional[dict]:
    """Validate the target numbers on every planned set, else None."""
    for ex in exercises:
        for s in _expand_planned_sets(ex):
            if reason := _bad_set({"weight_lbs": s.get("target_weight_lbs"),
                                   "reps": s.get("target_reps")}):
                return {"error": f"{ex.get('name','?')}: {reason}"}
    return None


def _insert_planned(conn: sqlite3.Connection, wid: int,
                    exercises: list[dict]) -> tuple[list[dict], list[dict]]:
    """Append pending (planned) sets to a workout. Resolves each exercise against the
    CLOSED catalog (no auto-stub) and continues set numbering per exercise — the same
    path log_workout uses, but writing targets + status='pending'. Returns
    (results, unmatched): names that don't resolve are skipped and returned with their
    closest candidates rather than invented."""
    results, unmatched = [], []
    for ex in exercises:
        name = (ex.get("name") or "").strip()
        if not name:
            continue
        row = _resolve_exercise(conn, name)
        if not row:
            unmatched.append({"name": name, "candidates": _match_exercises(conn, name)})
            continue
        eid = row["id"]
        planned = _expand_planned_sets(ex)
        start = (conn.execute(
            "SELECT COALESCE(MAX(set_index),0) AS m FROM sets WHERE workout_id=? AND exercise_id=?",
            (wid, eid),
        ).fetchone()["m"]) + 1
        for i, s in enumerate(planned, start=start):
            conn.execute(
                """INSERT INTO sets(workout_id, exercise_id, set_index,
                   target_weight_lbs, target_reps, status, note)
                   VALUES (?,?,?,?,?, 'pending', ?)""",
                (wid, eid, i, s.get("target_weight_lbs"), s.get("target_reps"), s.get("note")),
            )
        results.append({"exercise_id": eid, "name": row["name"],
                        "planned_sets": len(planned)})
    return results, unmatched


def _plan_payload(conn: sqlite3.Connection, wid: int,
                  unmatched: Optional[list[dict]] = None) -> dict:
    """The plan for one workout: exercises in insertion order, each with its sets
    (target + actual + status), plus a done/total progress count (skipped sets are
    excluded from the total). Used by every plan tool's return and by the web UI.

    `unmatched` (names this call couldn't resolve to a real catalog exercise, each with
    its closest `candidates`) is surfaced so the model re-issues them under a name that
    exists instead of inventing one — the catalog is closed to the assistant."""
    w = conn.execute(
        "SELECT id, workout_date, focus, feeling, notes, status FROM workouts WHERE id=?",
        (wid,),
    ).fetchone()
    if not w:
        return {"active": False}
    rows = conn.execute(
        """SELECT s.id, s.exercise_id, e.name, s.set_index, s.status,
                  s.target_weight_lbs, s.target_reps,
                  s.weight_lbs, s.reps, s.rpe, s.note
           FROM sets s JOIN exercises e ON e.id = s.exercise_id
           WHERE s.workout_id=? ORDER BY s.id""",
        (wid,),
    ).fetchall()
    exercises, by_eid, done, total = [], {}, 0, 0
    for r in rows:
        ex = by_eid.get(r["exercise_id"])
        if ex is None:
            ex = {"exercise_id": r["exercise_id"], "name": r["name"], "sets": []}
            by_eid[r["exercise_id"]] = ex
            exercises.append(ex)
        ex["sets"].append({
            "set_id": r["id"], "set_index": r["set_index"], "status": r["status"],
            "target_weight_lbs": r["target_weight_lbs"], "target_reps": r["target_reps"],
            "weight_lbs": r["weight_lbs"], "reps": r["reps"], "rpe": r["rpe"],
            "note": r["note"],
        })
        if r["status"] == "done":
            done += 1
            total += 1
        elif r["status"] == "pending":
            total += 1
    payload = {"active": w["status"] == "active", "workout_id": w["id"],
               "workout_date": w["workout_date"], "focus": w["focus"],
               "feeling": w["feeling"], "notes": w["notes"], "status": w["status"],
               "progress": {"done": done, "total": total}, "exercises": exercises}
    if unmatched:
        payload["unmatched"] = unmatched
    return payload


def remove_plan_exercise(exercise_id: int, workout_id: Optional[int] = None) -> dict:
    """Drop one exercise from the active plan entirely — every set of it, planned or
    already done. Backs the /trainer page's per-exercise "..." menu (Delete option); the
    model substitutes via swap_exercise instead, so this stays a plain helper, not an MCP
    tool. Returns the updated plan."""
    with db() as conn:
        w = (conn.execute("SELECT * FROM workouts WHERE id=?", (workout_id,)).fetchone()
             if workout_id is not None else _active_workout(conn))
        if not w:
            return {"error": "no active workout plan"}
        conn.execute("DELETE FROM sets WHERE workout_id=? AND exercise_id=?",
                     (w["id"], exercise_id))
        return _plan_payload(conn, w["id"])


def clear_plan_set(set_id: int) -> dict:
    """Clear a logged set from the /trainer plan card (the card's gesture: save with
    reps blank). A PLANNED set (one carrying a target) reverts to 'pending' — its actuals
    are blanked so it's a to-do again, the target kept; an ad-hoc set with no target is
    deleted outright. A plain helper, not an MCP tool — the model corrects sets with
    update_set / delete_record. Returns the updated plan."""
    with db() as conn:
        r = conn.execute(
            "SELECT workout_id, target_weight_lbs, target_reps FROM sets WHERE id=?",
            (set_id,),
        ).fetchone()
        if not r:
            return {"error": f"no set with id {set_id}"}
        wid = r["workout_id"]
        if r["target_weight_lbs"] is not None or r["target_reps"] is not None:
            conn.execute(
                """UPDATE sets SET weight_lbs=NULL, reps=NULL, rpe=NULL,
                   duration_seconds=NULL, distance_miles=NULL, note=NULL,
                   status='pending' WHERE id=?""",
                (set_id,),
            )
            return _plan_payload(conn, wid)
    # No target — an ad-hoc logged set; remove the row entirely (renumbers the rest).
    res = _delete_record("set", set_id)
    if isinstance(res, dict) and res.get("error"):
        return res
    with db() as conn:
        return _plan_payload(conn, wid)


@trainer_mcp.tool()
def start_workout_plan(exercises: list[dict], focus: Optional[str] = None,
                       notes: Optional[str] = None, replace: bool = False) -> dict:
    """Lay out today's routine as a plan the user works through — call this once you've
    decided the session from get_fitness_briefing (+ get_exercise_history for the lifts
    you're choosing weights for). Each exercise becomes a row of PENDING sets with
    targets; the user completes them with complete_set as they go.

    Build a full session: aim for ~21-26 working sets total, roughly 6-8 exercises at
    3-4 sets each, across the muscle groups that are due.

    Each item in `exercises` is either explicit:
        {"name": "Bench Press", "muscles": ["chest"],
         "sets": [{"target_weight_lbs": 100, "target_reps": 10},
                  {"target_weight_lbs": 100, "target_reps": 10},
                  {"target_weight_lbs": 100, "target_reps": 8}]}
    or the shorthand for N identical sets:
        {"name": "Curls", "muscles": ["biceps"], "set_count": 3,
         "target_reps": 12, "target_weight_lbs": 25}
    Pick the movements from the library with `exercises(muscle=..., rotation_only=True)` —
    the catalog is closed, so program only names it already holds (prefer the rotation).
    Names resolve fuzzily; a name with no real match is SKIPPED and returned under
    `unmatched` with its closest `candidates` — re-issue it under one of those, or have the
    user add the movement on the library page. Matched exercises are still planned, so the
    rest of the routine lands.

    Only ONE plan is active at a time. If a plan is already active, this APPENDS the new
    exercises to it (focus/notes ignored) — unless `replace=True`, which discards the
    current plan and starts fresh. Returns the full plan (see get_workout_plan)."""
    if err := _bad_planned(exercises):
        return err
    with db() as conn:
        active = _active_workout(conn)
        if active and replace:
            conn.execute("DELETE FROM workouts WHERE id=?", (active["id"],))
            active = None
        if active:
            wid = active["id"]
        else:
            wid = conn.execute(
                """INSERT INTO workouts(workout_date, focus, notes, status, created_at)
                   VALUES (?,?,?, 'active', ?)""",
                (today(), focus, notes, now()),
            ).lastrowid
        results, unmatched = _insert_planned(conn, wid, exercises)
        return _plan_payload(conn, wid, unmatched=unmatched)


@trainer_mcp.tool()
def get_workout_plan() -> dict:
    """The active workout plan (today's routine in progress): each exercise in order
    with its sets — `target_weight_lbs`/`target_reps` (the plan), `weight_lbs`/`reps`/
    `rpe` (the actuals, NULL until done), `status` ('pending'|'done'|'skipped'), and
    `set_id` (pass to complete_set/update_set) — plus a `progress` {done, total} count.
    Returns {"active": false} when no plan is in progress. Read this to see what's left
    and what's been done before logging the next set or adjusting the routine."""
    with db() as conn:
        w = _active_workout(conn)
        return _plan_payload(conn, w["id"]) if w else {"active": False}


@trainer_mcp.tool()
def complete_set(set_id: int, weight_lbs: Optional[float] = None,
                 reps: Optional[int] = None, rpe: Optional[float] = None,
                 note: Optional[str] = None) -> dict:
    """Mark one planned set done, recording what was actually lifted. Omitted
    `weight_lbs`/`reps` default to the set's targets, so "did it as planned" needs only
    the set_id (add `rpe` 1-10 for how hard it felt — that's how you judge the next
    weight). Find `set_id` in get_workout_plan. Flips the set to 'done' and returns the
    updated plan. (To CORRECT an already-logged set, use update_set instead.)"""
    with db() as conn:
        r = conn.execute("SELECT * FROM sets WHERE id=?", (set_id,)).fetchone()
        if not r:
            return {"error": f"no set with id {set_id}"}
        w = weight_lbs if weight_lbs is not None else r["target_weight_lbs"]
        rp = reps if reps is not None else r["target_reps"]
        if reason := _bad_set({"weight_lbs": w, "reps": rp, "rpe": rpe}):
            return {"error": reason}
        conn.execute(
            """UPDATE sets SET weight_lbs=?, reps=?, rpe=?, note=COALESCE(?, note),
               status='done' WHERE id=?""",
            (w, rp, rpe, note, set_id),
        )
        # Completing a set means the movement was actually trained → keep it in rotation.
        conn.execute("UPDATE exercises SET in_rotation=1 WHERE id=?", (r["exercise_id"],))
        return _plan_payload(conn, r["workout_id"])


@trainer_mcp.tool()
def swap_exercise(from_exercise: str, to_exercise: str,
                  sets: Optional[list[dict]] = None,
                  workout_id: Optional[int] = None) -> dict:
    """Substitute an exercise in the active plan — for a busy/broken machine, a tweak,
    or preference. Pick the CLOSEST like-for-like replacement, not just anything that
    touches the same muscle: match the movement pattern (vertical pull→vertical pull,
    horizontal press→horizontal press), the role (compound→compound, isolation→
    isolation), and roughly the loading character. A Lat Pulldown's peer is a Close-/
    Neutral-Grip Pulldown or a Pull-up — NOT a Straight-Arm Pulldown, which isolates
    the same lats but is a single-joint accessory and a different stimulus. Use
    `exercises(similar_to=from_exercise)` to see the catalog's closest peers (shared
    primary muscle, same mechanic) and pick `to_exercise` from there. Only drop to a
    narrower or different-pattern move when no true peer is available, and say so.
    The PENDING sets of `from_exercise` become 'skipped' (already-done sets stay in the
    log) and `to_exercise` is added with fresh pending sets.

    By default the substitute mirrors the count and targets of the swapped-out pending
    sets — but those targets came from a DIFFERENT exercise, so pass `sets`
    (e.g. [{"target_weight_lbs": 60, "target_reps": 10}, …]) whenever the right weight
    differs (it usually does). `to_exercise` MUST already be in the closed library — if it
    doesn't resolve, the swap is refused with the closest `candidates` (and nothing is
    skipped); pick one of those or have the user add it on the library page.
    Returns the updated plan."""
    with db() as conn:
        w = (conn.execute("SELECT * FROM workouts WHERE id=?", (workout_id,)).fetchone()
             if workout_id is not None else _active_workout(conn))
        if not w:
            return {"error": "no active workout plan to swap in"}
        frm = _resolve_exercise(conn, from_exercise)
        if not frm:
            return {"error": f"{from_exercise!r} isn't in this plan"}
        to = _resolve_exercise(conn, to_exercise)
        if not to:
            return {"error": f"{to_exercise!r} isn't in the library — pick a peer from "
                             "exercises(similar_to=...) or add it on the library page",
                    "candidates": _match_exercises(conn, to_exercise)}
        pend = conn.execute(
            """SELECT target_weight_lbs, target_reps FROM sets
               WHERE workout_id=? AND exercise_id=? AND status='pending'
               ORDER BY set_index""",
            (w["id"], frm["id"]),
        ).fetchall()
        if not pend:
            return {"error": f"no pending {from_exercise} sets to swap in this plan"}
        sub_sets = sets if sets else [
            {"target_weight_lbs": p["target_weight_lbs"], "target_reps": p["target_reps"]}
            for p in pend
        ]
        spec = [{"name": to["name"], "sets": sub_sets}]
        if err := _bad_planned(spec):
            return err
        conn.execute(
            "UPDATE sets SET status='skipped' WHERE workout_id=? AND exercise_id=? AND status='pending'",
            (w["id"], frm["id"]),
        )
        _results, unmatched = _insert_planned(conn, w["id"], spec)
        return _plan_payload(conn, w["id"], unmatched=unmatched)


@trainer_mcp.tool()
def add_to_plan(exercises: list[dict], workout_id: Optional[int] = None) -> dict:
    """Append exercises (or extra sets of an exercise already present) to the active
    plan mid-session — e.g. "add some calf raises" or "give me one more drop set". Same
    `exercises` shape as start_workout_plan; pick the movements from the library with
    `exercises(...)`. Errors if no plan is active. Names resolve against the closed
    catalog — a name that doesn't match comes back under `unmatched` with its closest
    `candidates` and is not added; re-issue it under one of those (or have the user add it
    on the library page). (To retarget an existing pending set, use update_set with
    target_weight_lbs/target_reps; to drop one, delete_record(kind="set").)"""
    if err := _bad_planned(exercises):
        return err
    with db() as conn:
        w = (conn.execute("SELECT * FROM workouts WHERE id=?", (workout_id,)).fetchone()
             if workout_id is not None else _active_workout(conn))
        if not w:
            return {"error": "no active workout plan; start one with start_workout_plan"}
        results, unmatched = _insert_planned(conn, w["id"], exercises)
        return _plan_payload(conn, w["id"], unmatched=unmatched)


@trainer_mcp.tool()
def finish_workout(workout_id: Optional[int] = None, feeling: Optional[str] = None,
                   notes: Optional[str] = None) -> dict:
    """Close out the active plan when the session is over. Remaining pending sets are
    marked 'skipped'; the session flips to 'done' and its completed sets become ordinary
    history (counting toward recency/PRs and showing on the workouts page). Optionally
    record overall `feeling`/`notes`. If nothing was completed, the empty session is
    deleted instead. Returns a short summary."""
    with db() as conn:
        w = (conn.execute("SELECT * FROM workouts WHERE id=?", (workout_id,)).fetchone()
             if workout_id is not None else _active_workout(conn))
        if not w:
            return {"error": "no active workout plan to finish"}
        done = conn.execute(
            "SELECT COUNT(*) AS c FROM sets WHERE workout_id=? AND status='done'",
            (w["id"],),
        ).fetchone()["c"]
        if done == 0:
            conn.execute("DELETE FROM workouts WHERE id=?", (w["id"],))
            return {"finished": True, "workout_id": w["id"], "deleted_empty": True,
                    "done_sets": 0, "active": False}
        skipped = conn.execute(
            "UPDATE sets SET status='skipped' WHERE workout_id=? AND status='pending'",
            (w["id"],),
        ).rowcount
        fields = {"status": "done"}
        if feeling is not None:
            fields["feeling"] = feeling
        if notes is not None:
            fields["notes"] = notes
        cols = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE workouts SET {cols} WHERE id=?", (*fields.values(), w["id"]))
        return {"finished": True, "workout_id": w["id"], "workout_date": w["workout_date"],
                "done_sets": done, "skipped_sets": skipped, "active": False}


# --------------------------------------------------------------------------- #
# Trainer: bodyweight
# --------------------------------------------------------------------------- #

@trainer_mcp.tool()
def log_bodyweight(weight_lbs: float, weigh_date: Optional[str] = None,
                   note: Optional[str] = None) -> dict:
    """Record a bodyweight reading, in POUNDS. The user weighs in around training (a
    standing habit on their weight-loss journey) — log it whenever they report a number.

    One reading per occasion; the latest reading on a day is treated as that day's
    weight, and a day with no reading just wasn't weighed (nothing is stored for it).
    Returns the reading plus `change_lbs` versus the previous weigh-in (negative = down).

    Args:
        weight_lbs: The scale reading in pounds.
        weigh_date: Day weighed, YYYY-MM-DD (Pacific). Defaults to today.
        note: Optional context, e.g. "morning, before coffee".
    """
    if err := _bad_date(weigh_date, "weigh_date"):
        return err
    if weight_lbs <= 0:
        return {"error": f"weight_lbs must be positive, got {weight_lbs}"}
    d = weigh_date or today()
    with db() as conn:
        rid = conn.execute(
            "INSERT INTO body_weight(weigh_date, weight_lbs, note, created_at) VALUES (?,?,?,?)",
            (d, weight_lbs, note, now()),
        ).lastrowid
        prev = conn.execute(
            """SELECT weight_lbs FROM body_weight
               WHERE weigh_date < ? ORDER BY weigh_date DESC, id DESC LIMIT 1""",
            (d,),
        ).fetchone()
    out = {"bodyweight_id": rid, "weigh_date": d, "weight_lbs": weight_lbs}
    if prev:
        out["change_lbs"] = round(weight_lbs - prev["weight_lbs"], 1)
    return out


@trainer_mcp.tool()
def get_fitness_briefing(recent_workouts: int = 5) -> dict:
    """One-call trainer context. Returns the stored profile (injuries, split, goals),
    per-muscle recency (days since each muscle was last trained + sets in the last 7
    days), a cardio rollup (per cardio exercise: days since last done + minutes/miles
    in the last 7 days), recent sessions (each with its `notes` — read them, a niggle
    logged last time is a caution this time), `bodyweight` (latest reading, days since,
    and 30-day change; negative = down), and `rotation` — the curated pool of movements
    the user trains (id, name, category, equipment, primary muscles). Call this at the
    start of a training conversation to decide what to work and what to rest: muscles with
    the most days_since (and low recent volume) are recovered and due; ones trained in the
    last ~1-2 days should rest. Cardio is tracked separately because it carries no muscle
    mapping. BUILD SESSIONS FROM `rotation` — it's the set the user actually trains; don't
    pull in movements outside it without asking (the full ~870-movement library is a
    reference the user curates from, via the library page or set_rotation). The
    recommendation itself is yours to make from this data."""
    week_ago = date.fromordinal(date.fromisoformat(today()).toordinal() - 6).isoformat()
    with db() as conn:
        profile = _get_profile(conn)
        mrows = conn.execute(
            """SELECT em.muscle,
                      MAX(w.workout_date) AS last_date,
                      SUM(CASE WHEN w.workout_date >= ? THEN 1 ELSE 0 END) AS sets_7d
               FROM sets s
               JOIN workouts w ON w.id = s.workout_id
               JOIN exercise_muscles em ON em.exercise_id = s.exercise_id
               WHERE s.status='done'
               GROUP BY em.muscle""",
            (week_ago,),
        ).fetchall()
        crows = conn.execute(
            """SELECT e.name,
                      MAX(w.workout_date) AS last_date,
                      SUM(CASE WHEN w.workout_date >= ? THEN COALESCE(s.duration_seconds,0) ELSE 0 END) AS dur_7d,
                      SUM(CASE WHEN w.workout_date >= ? THEN COALESCE(s.distance_miles,0) ELSE 0 END) AS dist_7d
               FROM sets s
               JOIN workouts w ON w.id = s.workout_id
               JOIN exercises e ON e.id = s.exercise_id
               WHERE (s.duration_seconds IS NOT NULL OR s.distance_miles IS NOT NULL)
                     AND s.status='done'
               GROUP BY e.id""",
            (week_ago, week_ago),
        ).fetchall()
        # The rotation: the curated pool the model programs from. Compact (id, name,
        # category, equipment, primary muscles) — one query for the muscles, grouped in.
        rot_rows = conn.execute(
            "SELECT id, name, category, equipment FROM exercises WHERE in_rotation=1 ORDER BY name"
        ).fetchall()
        rot_primary: dict[int, list[str]] = {}
        for pr in conn.execute(
            "SELECT em.exercise_id, em.muscle FROM exercise_muscles em "
            "JOIN exercises e ON e.id = em.exercise_id "
            "WHERE e.in_rotation=1 AND em.role='primary' ORDER BY em.muscle"
        ):
            rot_primary.setdefault(pr["exercise_id"], []).append(pr["muscle"])
        rotation = [{"exercise_id": r["id"], "name": r["name"], "category": r["category"],
                     "equipment": r["equipment"], "primary_muscles": rot_primary.get(r["id"], [])}
                    for r in rot_rows]
        # Recent history is COMPLETED sessions only; an in-progress plan (status
        # 'active') is surfaced separately via get_workout_plan.
        recent = conn.execute(
            "SELECT id, workout_date, focus, feeling, notes FROM workouts "
            "WHERE status='done' ORDER BY workout_date DESC, id DESC LIMIT ?",
            (recent_workouts,),
        ).fetchall()
        recent_out = []
        for w in recent:
            n = conn.execute(
                "SELECT COUNT(DISTINCT exercise_id) AS e, COUNT(*) AS s "
                "FROM sets WHERE workout_id=? AND status='done'",
                (w["id"],),
            ).fetchone()
            row = {"workout_id": w["id"], "date": w["workout_date"],
                   "focus": w["focus"], "feeling": w["feeling"],
                   "exercises": n["e"], "sets": n["s"]}
            if w["notes"]:
                row["notes"] = w["notes"]
            recent_out.append(row)
        # latest bodyweight + 30-day trend (negative change = losing)
        bw_latest = conn.execute(
            "SELECT weigh_date, weight_lbs FROM body_weight ORDER BY weigh_date DESC, id DESC LIMIT 1"
        ).fetchone()
        bodyweight = None
        if bw_latest:
            thirty_ago = date.fromordinal(date.fromisoformat(today()).toordinal() - 30).isoformat()
            base = conn.execute(
                """SELECT weight_lbs FROM body_weight WHERE weigh_date <= ?
                   ORDER BY weigh_date DESC, id DESC LIMIT 1""",
                (thirty_ago,),
            ).fetchone()
            bodyweight = {"latest_lbs": bw_latest["weight_lbs"],
                          "date": bw_latest["weigh_date"],
                          "days_since": _days_since(bw_latest["weigh_date"])}
            if base:
                bodyweight["change_30d_lbs"] = round(bw_latest["weight_lbs"] - base["weight_lbs"], 1)
    recency = sorted(
        ({"muscle": r["muscle"], "last_trained": r["last_date"],
          "days_since": _days_since(r["last_date"]), "sets_last_7d": r["sets_7d"]}
         for r in mrows),
        key=lambda m: (m["days_since"] is None, -(m["days_since"] or 0)),
    )
    cardio = sorted(
        ({"exercise": r["name"], "last_done": r["last_date"],
          "days_since": _days_since(r["last_date"]),
          "minutes_last_7d": round((r["dur_7d"] or 0) / 60, 1),
          "miles_last_7d": round(r["dist_7d"] or 0, 2)}
         for r in crows),
        key=lambda c: (c["days_since"] is None, -(c["days_since"] or 0)),
    )
    return {"now": current_clock(), "profile": profile,
            "muscle_recency": recency, "cardio_recency": cardio,
            "bodyweight": bodyweight, "recent_workouts": recent_out,
            "rotation": rotation}


@trainer_mcp.tool()
def update_profile(profile: dict) -> dict:
    """Merge fields into the stored trainer profile (JSON). Pass only the keys you
    want to change; existing keys are preserved. Use it to keep durable training
    facts current — e.g. {"injury": "left shoulder, avoid overhead pressing"},
    {"split": {"mon": "arms", "wed": "legs", "fri": "full body"}},
    {"goals": ["build strength", "lose weight"], "experience": "beginner"}.
    These are surfaced by get_fitness_briefing so you coach within them."""
    with db() as conn:
        current = _get_profile(conn)
        current.update(profile)
        conn.execute(
            """INSERT INTO settings(key, value) VALUES ('profile', ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (json.dumps(current),),
        )
    return {"profile": current}


@trainer_mcp.tool(name="delete_record")
def delete_training_record(kind: str, id: int) -> dict:
    """Permanently delete one training record. Irreversible — confirm first.

    `kind` selects what `id` refers to:
      - "workout" — a whole session (all its sets go too).
      - "set"     — one logged set (remaining sets for that exercise are renumbered
                    so set_index stays contiguous).
      - "weight"  — one bodyweight reading (its `bodyweight_id` is returned by
                    log_bodyweight; to fix a mistyped weigh-in, delete it and re-log).
    Find workout/set ids with get_fitness_briefing or get_exercise_history."""
    if kind not in ("workout", "set", "weight"):
        return {"error": f"unknown kind {kind!r}; this server deletes one of "
                         "['set', 'weight', 'workout'] (use the journal server for entry/drink)"}
    return _delete_record(kind, id)


if __name__ == "__main__":
    init_db()
    # stdio carries a single MCP stream, so a stdio launch runs ONE server, chosen by
    # MCP_SERVER (journal|trainer) — point each Claude Desktop config at the right one.
    # HTTP mode here is for single-server smoke tests; in production
    # webapp/combined.py serves BOTH MCP servers + the UI in one process.
    selected = trainer_mcp if os.environ.get("MCP_SERVER") == "trainer" else mcp
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "http":
        # Remote mode (behind Coolify's HTTPS proxy). Connector URL: https://<domain>/mcp
        selected.run(transport="http",
                     host=os.environ.get("MCP_HOST", "0.0.0.0"),
                     port=int(os.environ.get("PORT", "8000")),
                     path="/mcp")
    else:
        selected.run()
