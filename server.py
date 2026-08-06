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
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import jellyfish
# typing_extensions, NOT typing: pydantic (which generates the tool schemas) refuses a
# typing.TypedDict on Python < 3.12, and the local venv is 3.11 while the image is 3.12.
from typing_extensions import NotRequired, TypedDict
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


# --------------------------------------------------------------------------- #
# Tool payload shapes
#
# The nested arguments a few tools take (a batch of mention links, a workout's
# exercises and their sets) are declared as TypedDicts rather than bare `dict`, so
# FastMCP generates a REAL nested JSON Schema for them instead of an opaque
# {"type": "object"}. That moves the shape out of prose and into the contract: the
# client validates key names and types before the call is made, a typo'd or missing
# field is caught there rather than silently no-op'ing in the loop below, and the
# docstrings no longer have to spell out every field (they describe judgment —
# what a good target is — while the schema describes structure).
#
# NotRequired marks the genuinely optional keys. These are structural types only;
# VALUE-range checks (rpe 1-10, no negative reps) stay in _bad_set, since JSON
# Schema bounds wouldn't produce the actionable error text the model needs.
#
# Deliberately NOT typed: update_contact's `contact` blob. It's free-form by
# design (arbitrary top-level keys — emails, phones, addresses, websites, whatever
# comes up — shallow-merged), and a TypedDict would emit additionalProperties:false
# and reject exactly the extensibility that's the point of that column.
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Tool annotations
#
# MCP behavior hints, so a client can tell a lookup from a deletion WITHOUT reading
# the docstring — that's what drives whether it asks the user before running a call.
# Every tool declares one of these four; without them a spec-following client must
# assume the worst (destructiveHint defaults to TRUE), so `get_briefing` and
# `delete_record` would look equally dangerous.
#
# openWorldHint is False everywhere: this server touches one local SQLite file and
# nothing else — no network, no external service. That's the architectural no-LLM
# rule showing up in the protocol.
#
# NOTE: hints are advisory metadata, NOT enforcement. The real guard is
# AllowlistMiddleware; nothing here restricts what a tool can do.
# --------------------------------------------------------------------------- #

READ_ONLY = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}
# A write whose repeat CHANGES things — calling it twice logs two entries.
WRITE = {"destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
# A write that settles on a value — calling it twice leaves the same state.
WRITE_IDEMPOTENT = {"destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
# Removes or overwrites data that can't be recovered from the call itself.
DESTRUCTIVE = {"destructiveHint": True, "idempotentHint": True, "openWorldHint": False}


class MentionLink(TypedDict):
    """One resolution in a link_mentions batch: pin a mention to a person, or drop it."""
    mention_id: int
    person_id: NotRequired[int]
    learn_alias: NotRequired[bool]
    dismiss: NotRequired[bool]


class LoggedSet(TypedDict):
    """One set that was actually performed. Lifts use weight_lbs/reps; cardio uses
    duration_seconds/distance_miles. weight_lbs is SIGNED (negative = assisted)."""
    weight_lbs: NotRequired[Optional[float]]
    reps: NotRequired[Optional[int]]
    rpe: NotRequired[Optional[float]]
    duration_seconds: NotRequired[Optional[int]]
    distance_miles: NotRequired[Optional[float]]
    note: NotRequired[Optional[str]]


class LoggedExercise(TypedDict):
    """One exercise of a completed session: a catalog name plus the sets performed."""
    name: str
    sets: NotRequired[list[LoggedSet]]


class PlannedSet(TypedDict):
    """One PROGRAMMED set — the targets the user works toward, actuals filled in later
    by complete_set."""
    target_weight_lbs: NotRequired[Optional[float]]
    target_reps: NotRequired[Optional[int]]
    target_rpe: NotRequired[Optional[float]]
    note: NotRequired[Optional[str]]


class PlannedExercise(TypedDict):
    """One exercise in a plan. Either give an explicit `sets` list, or use the
    shorthand `set_count` (+ the target_* fields) to expand N identical sets."""
    name: str
    sets: NotRequired[list[PlannedSet]]
    set_count: NotRequired[int]
    target_weight_lbs: NotRequired[Optional[float]]
    target_reps: NotRequired[Optional[int]]
    target_rpe: NotRequired[Optional[float]]


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
entities, not name strings) plus an eating log. The server only stores and matches —
the judgment (which person a mention means) is yours.

Three rules: capture never blocks — always save, leave ambiguous mentions pending
for later; resolve mentions to person entities, don't normalize names in text (for a
group reference like "my parents" or "the kids", just link the specific people you can
identify — leaning on their relationships in the briefing — and don't capture the bare
group word itself as a mention); and one note per topic — split unrelated threads from
the same conversation into separate entries so their people don't cross-contaminate
later lookups.

Two habits that keep the log worth having, each owned by the tool that does it —
its docstring has the details, so follow them there rather than improvising:
  - ORDER: entries append in the order you save them, but people recount a day out of
    sequence. Finish a day with reorder_entries when the save order isn't the order
    things happened.
  - PROFILES: each person's `summary` is their rolling profile of durable KEY FACTS —
    and, since there is no relationship graph, the ONLY place relationships live.
    Update it AT LINK TIME (see link_mentions); nothing does it automatically. Lean on
    these summaries — get_briefing surfaces them — to resolve relational references
    like "her parents" or "his brother" to the right people.

Every entry is classified `kind`: "log" (an interaction/event/fact — the default) or
"thought" (a personal reflection). Thoughts stay in the feed and in search but are kept
out of per-person history, so the CRM view stays real interactions. add_journal_entry
tells them apart.

All dates in this log are Pacific (America/Los_Angeles) — the user lives and logs
on Pacific time. get_briefing returns `now` (current Pacific date/time) with
`date`/`yesterday`/`tomorrow` precomputed: use those EXACT strings for "today"/
"yesterday"/"tomorrow" rather than computing or shifting dates yourself, and resolve
any bare day reference against them before defaulting or saving.

Intake is its own log, not a journal entry: when the user mentions eating or drinking,
call log_intake — ONCE PER ITEM ("a sandwich and a beer" is two calls) — instead of, or
as well as, writing an entry about it. Corrections go through update_intake_item by id;
day totals are summed from the items, so you never recompute a day yourself.

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
(current Pacific date/time) with `date`/`yesterday`/`tomorrow` precomputed: use those
EXACT strings for "today"/"yesterday"/"tomorrow" rather than computing or shifting
dates yourself, and resolve any bare day reference against them before defaulting or
saving.

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

Weight on a lift is SIGNED added/removed load, not total bodyweight: 0 (or null) = plain
bodyweight, positive = weight added (a +25 weighted pull-up), and NEGATIVE = assistance,
the load a band or machine took OFF (an assisted pull-up at -20). This lets one movement
track a full assisted→bodyweight→weighted arc on a single number line, -20 → 0 → +20 as
the user gets stronger. Log negatives as given, program the next target along that line
(less assistance, then added load), and read movement toward 0 and beyond as progress.
Don't lean on estimated-1RM for assisted (negative-weight) sets — it isn't physically
meaningful below bodyweight; judge those by assistance level and RPE instead.

The catalog has three nested layers — a LIBRARY, a hearted SUPERSET, and a ROTATION
(rotation ⊆ hearted ⊆ library):
  - LIBRARY: the whole catalog, PRE-LOADED with ~870 public-domain movements from
    free-exercise-db (via scripts/import_exercises.py), each carrying muscles, equipment,
    a demo image, and step-by-step technique. It's a reference the user searches/browses
    at /trainer/library. Almost any movement you mention is already here with full data —
    look it up with `find_exercises` rather than re-deriving it.
  - HEARTED SUPERSET (hearted): the user's bench of FAVORITE movements — a curated shortlist
    bigger than the rotation. It's where the rotation is drawn from: every few months the
    user swaps some of the rotation out for other hearted lifts. List it with
    find_exercises(hearted_only=True); add/remove with set_hearted. Heart a movement the user
    likes even when it's not currently programmed, so it's on the bench for the next swap.
  - ROTATION: the small subset (in_rotation, the user keeps it to ~10-14) they're ACTIVELY
    training, so progress on each lift is easy to track. The rotation is DELIBERATELY small and
    hand-curated — keeping it tight is how the user controls their progression, so treat it as
    fixed unless the user explicitly says to change it. get_fitness_briefing returns it as
    `rotation`, and it is the ONLY pool you PROGRAM ROUTINES FROM. Build start_workout_plan /
    log_workout sessions out of rotation movements. NEVER add a movement to the rotation on
    your own initiative. If a session seems to call for something NOT in the rotation, don't
    slip it in and don't quietly set_rotation — name the gap to the user, suggest the movement,
    and only call set_rotation AFTER they explicitly confirm they want it in the rotation (a
    vague "sounds good" about the workout is NOT permission to grow the rotation — ask
    plainly). Prefer working with what's already there; if you must propose an addition, pull
    from the hearted superset first (surface candidates from the wider library with
    find_exercises(muscle=…)). Adding to the rotation hearts it too. Logging a movement the user
    actually did hearts it automatically (onto the favorites bench) but does NOT add it to the
    rotation — the rotation only ever grows on an explicit set_rotation request or via the
    website, so it stays exactly the size the user chose; when it does drift past ~14 (e.g.
    after a curation), help the user prune back.

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
(plus save_exercise enrichment if the entry is bare). "Add it to my favorites" / "remember
this one" without committing it to the active rotation is set_hearted.

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
    contact        TEXT,   -- free-form JSON blob: emails/phones/addresses/websites/…
    email          TEXT,   -- legacy single-valued fields (folded into `contact` on migrate)
    phone          TEXT,
    address        TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE   -- "family", "colleagues", "Hallie's friends"
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
    kind       TEXT NOT NULL DEFAULT 'log',  -- 'log' (interaction/observation) | 'thought' (personal reflection)
    day_position INTEGER,       -- within-day chronological rank (1=earliest that day); NULL=legacy/insertion order
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
-- up, kinds merge into a deduped list).
--
-- A row with standard_drinks = 0 is meaningful: it's a day CONFIRMED sober,
-- distinct from a day with no row, which merely wasn't logged. Aggregates treat
-- both as sober (a 0 row is not a drinking day and doesn't break a streak) — the
-- difference is only that the UI can show "0" instead of an empty glass.
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
-- Intake log — ONE ROW PER THING CONSUMED. A sandwich is a row; a 12oz glass
-- of water is a row; a beer is a row. Food, alcohol and water are the same
-- shape because they're the same kind of fact, so there is no per-nutrient
-- special case anywhere above this table.
--
-- A day's totals are DERIVED (SUM ... GROUP BY food_date), never stored: a
-- stored total can drift from the items it claims to summarize, and correcting
-- one item would mean re-deriving it by hand. Correcting is instead a plain
-- UPDATE/DELETE on the item's id — no arithmetic anywhere.
--
-- Every nutrient column is per-item and optional; NULL means "not estimated"
-- (a day described only in words isn't a zero-calorie day). The model does the
-- estimating in conversation — there is no food database in here.
-- ----------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS intake_items (
    id         INTEGER PRIMARY KEY,
    food_date  TEXT NOT NULL,    -- YYYY-MM-DD (Pacific) it was consumed
    position   INTEGER,          -- order logged within the day (1 = first)
    item       TEXT,             -- "chipotle bowl"; NULL for a bare tap ("+16oz")
    calories   REAL,             -- all optional; NULL = not estimated
    protein_g  REAL,
    carbs_g    REAL,
    fat_g      REAL,
    sodium_mg  REAL,
    fiber_g    REAL,
    standard_drinks REAL,        -- alcohol, in standard drinks
    water_oz   REAL,             -- fluid ounces of water (128 = a gallon)
    note       TEXT,             -- how it sat, why an estimate is soft
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intake_date ON intake_items(food_date);

-- ----------------------------------------------------------------------- --
-- LEGACY day-level intake (one row per day, running summary + day totals).
-- Superseded by intake_items; kept as the migration's source, not read.
-- ----------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS nutrition (
    id         INTEGER PRIMARY KEY,
    food_date  TEXT NOT NULL,
    summary    TEXT,
    calories   REAL,
    protein_g  REAL,
    carbs_g    REAL,
    fat_g      REAL,
    sodium_mg  REAL,
    fiber_g    REAL,
    standard_drinks REAL,
    water_oz   REAL,
    notes      TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_nutrition_date ON nutrition(food_date);

-- ----------------------------------------------------------------------- --
-- Personal trainer. `exercises` is a catalog of stable exercise ENTITIES
-- (like people): technique lives here so the model can coach form. Muscles
-- are normalized into a child table so "what's rested vs worked" is a plain
-- SQL aggregate, not an LLM guess. Its columns mirror the free-exercise-db
-- dataset (slug/force/level/mechanic/equipment/category + primary/secondary
-- muscles + instructions) so the public-domain LIBRARY imports 1:1; our own
-- coaching layer (common_mistakes, cautions, video_link) sits on top. The
-- `in_rotation` flag marks the small subset the trainer actually programs from;
-- `hearted` is the wider favorites SUPERSET it's drawn from (rotation ⊆ hearted),
-- so the user can swap the rotation every few months from a pool they curate.
-- `workouts`/`sets` are the two-level log (session + per-set
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
    image_link      TEXT,             -- start frame (or a self-looping gif) of proper technique
    image_link_end  TEXT,             -- finish frame; with image_link the UI alternates the two
                                      -- (~1s) to animate the rep — free-exercise-db ships both
    in_rotation     INTEGER NOT NULL DEFAULT 0,  -- 1 = in the user's curated programming pool
    hearted         INTEGER NOT NULL DEFAULT 0,  -- 1 = in the user's favorites SUPERSET (the bench
                                                 -- the rotation is drawn from). in_rotation IMPLIES
                                                 -- hearted: every rotation lift is hearted, but a
                                                 -- hearted lift need not be in the (small) rotation
    archived        INTEGER NOT NULL DEFAULT 0,  -- 1 = soft-deleted: hidden everywhere the
                                                 -- catalog is discovered, row kept so past
                                                 -- workouts that reference it stay intact
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

-- AKAs: common alternative names a movement is searched/spoken by ("bench" -> Barbell
-- Bench Press, "RDL" -> Romanian Deadlift). Mirrors the people `aliases` table: one
-- canonical entity, many surface forms. Resolution and search score against these as
-- well as the canonical name, so a user finds a lift by whatever they call it. Stored
-- lowercased; the catalog stays the closed source of truth (an alias never creates a row).
CREATE TABLE IF NOT EXISTS exercise_aliases (
    exercise_id INTEGER NOT NULL REFERENCES exercises(id) ON DELETE CASCADE,
    alias       TEXT NOT NULL,        -- lowercased alternative name
    PRIMARY KEY (exercise_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_exercise_aliases_alias ON exercise_aliases(alias);

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
    target_rpe        REAL,           -- plan target difficulty (1-10); prefills the /trainer card's Easy/Med/Hard buttons
    status      TEXT NOT NULL DEFAULT 'done',  -- 'pending' | 'done' | 'skipped'
    ex_position INTEGER,              -- exercise's slot in the workout (all its sets share it); NULL = insertion order
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


def snapshot_db(dest: str) -> str:
    """Write a consistent point-in-time backup of the live DB to `dest`.

    Uses SQLite's `VACUUM INTO`, which copies the whole database — schema, every
    row, and the FTS5 index — inside a read transaction, so the snapshot is
    atomic even if writes land mid-copy. The result is a plain, self-contained
    SQLite file: restore is "drop it in at JOURNAL_DB and restart", nothing to
    replay. `dest` must NOT already exist (VACUUM INTO refuses to overwrite).
    Returns `dest`. This is a pure copy of the file we already own — no LLM, no
    external service — so it stays on the server side of the architectural line.
    """
    conn = db()
    try:
        conn.execute("VACUUM INTO ?", (dest,))
    finally:
        conn.close()
    return dest


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
        if "kind" not in ecols:
            # Existing entries are all interaction/observation logs (the only kind
            # before this feature), so the 'log' default back-fills them correctly.
            conn.execute("ALTER TABLE entries ADD COLUMN kind TEXT NOT NULL DEFAULT 'log'")
        if "day_position" not in ecols:
            # Within-day chronological rank (1=earliest that day). Legacy entries stay
            # NULL — no back-fill UPDATE (which would needlessly churn the entries_fts
            # triggers). NULL sorts FIRST in the feed's ascending order (so a legacy day
            # keeps its old id order at the top) and LAST in the newest-first lists; a
            # newly captured entry gets a real position and appends below the NULLs.
            conn.execute("ALTER TABLE entries ADD COLUMN day_position INTEGER")
        pcols = [r["name"] for r in conn.execute("PRAGMA table_info(people)")]
        for col in ("summary", "contact", "email", "phone", "address"):
            if col not in pcols:
                conn.execute(f"ALTER TABLE people ADD COLUMN {col} TEXT")
        # Fold the legacy single-valued email/phone/address columns into the JSON
        # `contact` blob. One-time and idempotent: only touches rows whose contact is
        # still empty, and contact becomes non-NULL after, so it never re-runs.
        for r in conn.execute(
            "SELECT id, email, phone, address FROM people WHERE contact IS NULL "
            "AND (email IS NOT NULL OR phone IS NOT NULL OR address IS NOT NULL)"
        ).fetchall():
            blob: dict = {}
            if r["email"]:
                blob["emails"] = [r["email"]]
            if r["phone"]:
                blob["phones"] = [r["phone"]]
            if r["address"]:
                blob["addresses"] = [r["address"]]
            conn.execute("UPDATE people SET contact=? WHERE id=?",
                         (json.dumps(blob), r["id"]))
        scols = [r["name"] for r in conn.execute("PRAGMA table_info(sets)")]
        for col, decl in (("duration_seconds", "INTEGER"), ("distance_miles", "REAL"),
                          ("target_weight_lbs", "REAL"), ("target_reps", "INTEGER"),
                          ("target_rpe", "REAL"),
                          ("status", "TEXT NOT NULL DEFAULT 'done'"),
                          ("ex_position", "INTEGER")):
            if col not in scols:
                conn.execute(f"ALTER TABLE sets ADD COLUMN {col} {decl}")
        wcols = [r["name"] for r in conn.execute("PRAGMA table_info(workouts)")]
        if "status" not in wcols:
            conn.execute("ALTER TABLE workouts ADD COLUMN status TEXT NOT NULL DEFAULT 'done'")
        xcols = [r["name"] for r in conn.execute("PRAGMA table_info(exercises)")]
        if "image_link" not in xcols:
            conn.execute("ALTER TABLE exercises ADD COLUMN image_link TEXT")
        if "image_link_end" not in xcols:
            conn.execute("ALTER TABLE exercises ADD COLUMN image_link_end TEXT")
        # Columns added when the catalog was lined up with free-exercise-db + rotation.
        for col, decl in (("slug", "TEXT"), ("force", "TEXT"), ("level", "TEXT"),
                          ("mechanic", "TEXT"),
                          ("in_rotation", "INTEGER NOT NULL DEFAULT 0"),
                          ("hearted", "INTEGER NOT NULL DEFAULT 0"),
                          ("archived", "INTEGER NOT NULL DEFAULT 0")):
            if col not in xcols:
                conn.execute(f"ALTER TABLE exercises ADD COLUMN {col} {decl}")
                # in_rotation IMPLIES hearted, so backfill the superset from the rotation
                # the first time the column appears — existing rotation lifts are favorites.
                if col == "hearted":
                    conn.execute("UPDATE exercises SET hearted=1 WHERE in_rotation=1")
        # Indexes live here (not in SCHEMA) so they're created only after their columns exist.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exercises_rotation ON exercises(in_rotation)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exercises_hearted ON exercises(hearted)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exercises_archived ON exercises(archived)")
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
        # Sodium and fiber joined the eating log after it shipped. Existing rows stay
        # NULL — the same "not estimated" state as any unfilled nutrient, so nothing
        # needs back-filling.
        ncols = [r["name"] for r in conn.execute("PRAGMA table_info(nutrition)")]
        for col in ("sodium_mg", "fiber_g", "standard_drinks", "water_oz"):
            if col not in ncols:
                conn.execute(f"ALTER TABLE nutrition ADD COLUMN {col} REAL")
        # Alcohol moved from its own `drinks` table into the eating log, where it's
        # just another daily nutrient with a target. Fold the old rows in ONCE (keyed
        # by date — both tables are one-row-per-day, so it's a merge, not a reshape),
        # then leave the `drinks` table dormant rather than dropping it: it costs
        # nothing and is the only copy of the per-day `kind` ("beer, wine"), which the
        # nutrition row has no column for. The settings flag makes this idempotent —
        # without it, re-running after the user edited a day would silently revert it.
        done = conn.execute(
            "SELECT value FROM settings WHERE key='drinks_folded_into_nutrition'"
        ).fetchone()
        if not done:
            for r in conn.execute(
                "SELECT drink_date, standard_drinks, kind, notes FROM drinks"
            ).fetchall():
                row = conn.execute(
                    "SELECT id, notes FROM nutrition WHERE food_date=?", (r["drink_date"],)
                ).fetchone()
                # The drink's kind/notes are the only prose it carried; keep them on the
                # day's notes so nothing is lost when the table goes quiet.
                note = _merge_notes(r["kind"], r["notes"])
                if row:
                    conn.execute(
                        "UPDATE nutrition SET standard_drinks=?, notes=? WHERE id=?",
                        (r["standard_drinks"], _merge_notes(row["notes"], note), row["id"]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO nutrition(food_date, notes, standard_drinks, created_at) "
                        "VALUES (?,?,?,?)",
                        (r["drink_date"], note, r["standard_drinks"], now()),
                    )
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES "
                "('drinks_folded_into_nutrition', 'true')"
            )
        # Second fold: the day-level `nutrition` rows become intake_items. Per-item
        # attribution genuinely isn't recoverable from a merged day (the summary was a
        # joined string and the numbers a running total), so each day converts to ONE
        # item carrying its text and totals — lossless, just not itemized. Everything
        # logged after this is a real per-item row. Same settings-flag guard.
        done2 = conn.execute(
            "SELECT value FROM settings WHERE key='nutrition_split_into_items'"
        ).fetchone()
        if not done2:
            for r in conn.execute("SELECT * FROM nutrition ORDER BY food_date").fetchall():
                conn.execute(
                    "INSERT INTO intake_items(food_date, position, item, note, "
                    + ", ".join(NUTRIENTS) + ", created_at) VALUES (?,?,?,?"
                    + ",?" * len(NUTRIENTS) + ",?)",
                    (r["food_date"], 1, r["summary"], r["notes"],
                     *(r[m] for m in NUTRIENTS), r["created_at"]),
                )
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES "
                "('nutrition_split_into_items', 'true')"
            )


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
    always knows what 'today'/'now' means before it defaults or computes dates.
    `yesterday`/`tomorrow` are precomputed (the model should use these exact strings
    rather than doing its own +/-1 day arithmetic, which is an off-by-one source)."""
    dt = datetime.now(PACIFIC)
    return {
        "date": dt.strftime("%Y-%m-%d"),
        "yesterday": (dt - timedelta(days=1)).strftime("%Y-%m-%d"),
        "tomorrow": (dt + timedelta(days=1)).strftime("%Y-%m-%d"),
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
    # weight_lbs is SIGNED: negative = assisted (band/machine took load off, e.g. an
    # assisted pull-up at -20), 0 = unassisted bodyweight, positive = added load. So no
    # lower bound here — a negative is a valid measurement, not bad data.
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

def _next_day_position(conn, entry_date: str) -> int:
    """Rank to give a newly inserted entry so it lands at the END of its day. Counts
    only positioned entries (NULL = legacy, which sort by id before any positioned row),
    so the first save of a day gets 1 and each later one appends. The model reorders the
    day afterward with reorder_entries if events weren't captured in chronological order."""
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM entries WHERE entry_date=? AND day_position IS NOT NULL",
        (entry_date,),
    ).fetchone()["n"]
    return n + 1


@mcp.tool(annotations=WRITE)
def add_journal_entry(body: str, raw_body: Optional[str] = None,
                      mentions: Optional[list[str]] = None,
                      entry_date: Optional[str] = None,
                      kind: str = "log") -> dict:
    """Save a journal entry and match any people named in it.

    ALWAYS call this to capture an entry — never block on resolution.

    LOG vs THOUGHT (`kind`). Classify every entry as one of two kinds:
      - "log" (the DEFAULT): a record of something that happened or that the user
        learned — an interaction with someone, an event, an observation, a fact about
        a person they know. This is the CRM/diary spine.
      - "thought": a personal reflection, musing, idea, opinion, feeling, plan, or
        introspection that ISN'T anchored to a specific interaction or a fact about
        someone — e.g. "I've been wondering whether I should change careers" or "lately
        I feel more at peace". Thoughts are kept out of per-person history (so the CRM
        view stays a record of real interactions), but still live in the same journal
        feed and are still full-text searchable.
    Judge by what the entry IS, not whether it names people: a thought can mention
    someone ("been thinking about how Tom always pushes me") and stays a "thought";
    a terse factual note about a person ("Tom got the job") is a "log". When a single
    conversation mixes both — recounting a dinner, then reflecting on it — split per
    ONE-NOTE-PER-TOPIC and give each its own `kind`. When genuinely unsure, default to
    "log". You may pass kind explicitly even when it's "log".

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

    CHRONOLOGICAL ORDER. This APPENDS to the end of its day, so a day reads in the
    order you SAVE — and people recount a day out of order (the evening phone call
    mentioned last actually happened mid-afternoon). After capturing several entries
    for one day, or adding to a day that already has some, call reorder_entries to lay
    the day out chronologically. Skip it when the save order already matches.

    Write `body` as a clean, structured, concise journal entry in MARKDOWN:
    organize the free-association into readable prose, keep the substance and the
    user's voice, drop filler. Format it for readability — separate distinct
    paragraphs with a BLANK LINE (a real line break in the string, NOT the literal
    two characters backslash-n), and use Markdown **bold**, *italics*, and `-`/`1.`
    lists where they genuinely help scanning (a run of names/places, a set of
    to-dos, distinct sub-topics within one event). Don't over-format a short note —
    plain prose is fine; reach for structure only when it earns its keep. Pass the
    user's original words verbatim as `raw_body` so a faithful record is retained
    underneath (retrievable via get_entry; not shown in normal search or history).
    When you split a conversation into several notes, each note's `raw_body` is the
    slice of the verbatim words about THAT topic — not
    the whole transcript repeated on every entry. Extract the people referenced
    and pass each as a short surface form — what was actually SAID ("Tom", "Dad",
    a garbled transcription), taken from the raw words, not the cleaned-up name.

    GROUP REFERENCES. When the user names a group rather than a person — "my
    parents", "the kids", "the in-laws" — don't pass the bare group word as a
    mention (it can't resolve to one person and would sit pending as dead weight).
    Instead pass the specific people you can identify by name, using who you know
    from get_briefing / their relationships in the summaries (e.g. you know Hallie's
    parents are Jeff and Jody -> pass ["Jeff", "Jody"]). The reference is relative to
    the speaker, so use the snippet to tell whose ("my parents" vs "Hallie's parents").
    If you can't tell who the group is, just leave them out and ask — capture never
    blocks.

    Resolution guidance for the model after this returns (each surface form resolves
    independently):
      - One candidate with score >= 0.85 and no other within 0.15: link it
        silently via link_mentions (set learn_alias=True if the surface form
        wasn't already an exact alias).
      - Two close candidates (e.g. two people named Tom): ask the user which one,
        using context, then link.
      - No candidate >= 0.6: likely a new person. Ask, then save_person (no
        person_id) and link — or leave it pending if the user says they'll explain
        later.
    Whenever you link, also keep that person's summary current — see link_mentions,
    which owns that rule.

    Args:
        body: The cleaned journal entry, written as Markdown (paragraphs split by
            blank lines; bold/italics/lists where they aid readability).
        raw_body: The user's verbatim input. Optional but recommended.
        mentions: Surface forms of people referenced, e.g. ["Tom", "Hallie"].
            For a group reference ("my parents"), pass the specific people you can
            identify by name, not the group word (see GROUP REFERENCES).
        entry_date: Day the entry is ABOUT as YYYY-MM-DD. Defaults to today.
        kind: "log" (interaction/observation/fact — the default) or "thought"
            (a personal reflection). See LOG vs THOUGHT above.
    """
    if err := _bad_date(entry_date, "entry_date"):
        return err
    if kind not in ("log", "thought"):
        return {"error": "kind must be 'log' or 'thought'"}
    entry_date = entry_date or today()
    snippet_source = raw_body or body
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO entries(body, raw_body, entry_date, kind, day_position, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (body, raw_body, entry_date, kind, _next_day_position(conn, entry_date), now()),
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
    return {"entry_id": entry_id, "entry_date": entry_date, "kind": kind,
            "mentions": results}


@mcp.tool(annotations=WRITE_IDEMPOTENT)
def link_mentions(links: list[MentionLink]) -> dict:
    """Resolve pending mentions to people.

    KEEP THE PROFILE CURRENT. Whenever you link a mention, glance at that person's
    summary and decide whether this entry revealed a durable KEY FACT about them
    worth recording — a key relationship (partner/spouse, parents, kids, siblings,
    by name), employment/role, school, birthday or other fixed date, where they
    live, a major life event. If so and the summary doesn't already capture it, read
    the full summary with get_person_history (read-before-write) and fold it in via
    save_person. Skip passing or transient details (a mood, a one-off plan) — the
    summary is a compact profile of stable facts, not a diary. This is the only way
    the profiles (and the relationships other lookups rely on) stay fresh — nothing
    updates them automatically.

    Args:
        links: One entry per mention you're resolving.
            `learn_alias` stores the mention's surface form as an alias on that
            person, so the same word (including a recurring transcription error)
            auto-matches next time — set it whenever the form wasn't already exact.
            `dismiss` (instead of a person_id) DROPS a mention that shouldn't
            resolve to anyone — a bare group word that slipped in, or transcription
            noise. The mention leaves the pending queue for good; the entry itself
            is untouched.
    """
    linked, dismissed, skipped = [], [], []
    with db() as conn:
        for ln in links:
            mid = ln["mention_id"]
            m = conn.execute("SELECT surface_form FROM mentions WHERE id=?", (mid,)).fetchone()
            if not m:
                skipped.append({"mention_id": mid, "reason": "no such mention"})
                continue
            if ln.get("dismiss"):
                conn.execute("DELETE FROM mentions WHERE id=?", (mid,))
                dismissed.append(mid)
                continue
            pid = ln.get("person_id")
            if pid is None:
                skipped.append({"mention_id": mid,
                                "reason": "pass a person_id to link, or dismiss=True to drop"})
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
    if dismissed:
        out["dismissed"] = dismissed
    if skipped:
        out["skipped"] = skipped
    return out


# --------------------------------------------------------------------------- #
# Website-only mention resolution (NOT MCP tools — like create_exercise /
# set_archived, these are reachable only by the authenticated user through the
# webapp, never by the journal connector). Claude resolves mentions in chat via
# link_mentions / save_person; these back the browse pages' inline resolver so the
# user can also pin people straight from the pending queue or an entry.
# --------------------------------------------------------------------------- #

def resolve_mention_web(mention_id: int, person_id: int,
                        learn_alias: bool = False) -> dict:
    """Pin a pending mention to a person — the inline resolver's link control.

    Sets the mention row to that person (status='resolved'); learn_alias stores the
    surface form as an alias so it auto-matches next time. Website-only; the catalog
    of who exists stays the model's to grow via save_person."""
    if not person_id:
        return {"error": "no person selected"}
    pid = int(person_id)
    with db() as conn:
        m = conn.execute(
            "SELECT id, entry_id, surface_form FROM mentions WHERE id=?",
            (mention_id,),
        ).fetchone()
        if not m:
            return {"error": "no such mention"}
        if not conn.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone():
            return {"error": f"no person with id {pid}"}
        conn.execute(
            "UPDATE mentions SET person_id=?, status='resolved' WHERE id=?",
            (pid, mention_id),
        )
        if learn_alias:
            conn.execute(
                """INSERT OR IGNORE INTO aliases(person_id, surface_form,
                   phonetic_key, source) VALUES (?,?,?, 'learned')""",
                (pid, m["surface_form"], phonetic(m["surface_form"])),
            )
        return {"ok": True, "entry_id": m["entry_id"], "person_id": pid}


def dismiss_mention_web(mention_id: int) -> dict:
    """Delete a stray mention — the inline resolver's Dismiss control. Use it for a
    mention that shouldn't resolve to anyone (a group word, or noise). Website-only;
    not an MCP tool."""
    with db() as conn:
        if not conn.execute("SELECT 1 FROM mentions WHERE id=?", (mention_id,)).fetchone():
            return {"error": "no such mention"}
        conn.execute("DELETE FROM mentions WHERE id=?", (mention_id,))
    return {"ok": True}


@mcp.tool(annotations=WRITE_IDEMPOTENT)
def save_person(person_id: Optional[int] = None, canonical_name: Optional[str] = None,
                role: Optional[str] = None, notes: Optional[str] = None,
                summary: Optional[str] = None,
                aliases: Optional[list[str]] = None,
                remove_aliases: Optional[list[str]] = None,
                groups: Optional[list[str]] = None) -> dict:
    """Create or update a person (an entity) — the one write tool for people.

    Omit `person_id` to CREATE (then `canonical_name` is required); pass `person_id`
    to UPDATE an existing person (only the non-null fields you pass are written). `role`
    is the disambiguator the user relies on later, e.g. "father", "law school friend";
    `summary` is a short rolling profile for context — and the home for this person's
    immediate relationships. Record their parents, partner/spouse, children and
    siblings BY NAME as you learn them (e.g. "Parents: Jeff (father), Jody (mother).
    Brother: Ry."). The server has NO relationship graph, so this profile is the only
    place that knowledge lives — and it's what lets you later read a relational
    reference ("her parents", "his brother", "my partner") and link the right people.
    Keep it current as relationships change.

    `aliases` are surface forms (incl. recurring transcription errors): on create they
    seed the person, on update they are ADDED — so this is also how you attach a new
    alias to someone later. `remove_aliases` is the inverse — pass surface forms to
    DETACH them from this person (case-insensitive match), e.g. to undo an alias that
    was learned or attached by mistake so it no longer auto-resolves to them. (The
    canonical_name itself isn't an alias row and can't be removed this way — change it
    by passing a new `canonical_name`.) `groups` are circle names like ["family"],
    created if new; passing `groups` REPLACES the person's circle membership. Returns
    the person_id and whether it was newly created.

    Contact details (emails, phones, addresses, websites, …) live in a separate
    multi-valued blob — write them with `update_contact`, not here."""
    with db() as conn:
        if person_id is None:
            if not canonical_name:
                return {"error": "canonical_name is required to create a person"}
            person_id = conn.execute(
                """INSERT INTO people(canonical_name, role, notes, summary, created_at)
                   VALUES (?,?,?,?,?)""",
                (canonical_name, role, notes, summary, now()),
            ).lastrowid
            created, updated = True, []
        else:
            if not conn.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone():
                return {"error": f"no person with id {person_id}"}
            created = False
            fields = {"canonical_name": canonical_name, "role": role, "notes": notes,
                      "summary": summary}
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
        removed_aliases = []
        for a in (remove_aliases or []):
            cur = conn.execute(
                """DELETE FROM aliases
                   WHERE person_id=? AND lower(surface_form)=lower(?)""",
                (person_id, a),
            )
            if cur.rowcount:
                removed_aliases.append(a)
        if groups is not None:
            conn.execute("DELETE FROM person_groups WHERE person_id=?", (person_id,))
            _set_groups(conn, person_id, groups)
    if created:
        return {"person_id": person_id, "created": True}
    out = {"person_id": person_id, "created": False,
           "updated": updated + (["aliases"] if aliases else [])
                      + (["groups"] if groups is not None else [])}
    if removed_aliases:
        out["removed_aliases"] = removed_aliases
    return out


@mcp.tool(annotations=WRITE_IDEMPOTENT)
def update_contact(person_id: int, contact: dict) -> dict:
    """Merge contact details into a person's CONTACT blob (free-form JSON) and return
    the merged result. This is the home for everything vCard-ish — emails, phones,
    addresses, websites, birthdays, handles — and it's MULTI-VALUED: one person can have
    several phones, two addresses, whatever.

    THE MERGE IS SHALLOW, by top-level key: passing {"phones": [...]} rewrites the whole
    phones list but leaves emails/addresses untouched. So to ADD one item to a list that
    already has entries, first READ the current blob (get_person_history returns it as
    `contact`), then write the FULL updated list back — otherwise you overwrite what was
    there. To DROP a category, pass it with a null value, e.g. {"phones": null}.

    Keep the shape renderable: top-level keys are category names; each value is a string,
    a list of strings, or a list of {"label","value"} objects — e.g.
    {"emails": ["tom@work.com"],
     "phones": [{"label": "mobile", "value": "555-0100"}],
     "addresses": [{"label": "home", "value": "12 Oak St, Portland OR"}],
     "websites": ["https://tom.example"]}. Within that, store whatever fits."""
    with db() as conn:
        if not conn.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone():
            return {"error": f"no person with id {person_id}"}
        current = _get_contact(conn, person_id)
        for k, v in contact.items():
            if v is None:
                current.pop(k, None)
            else:
                current[k] = v
        conn.execute("UPDATE people SET contact=? WHERE id=?",
                     (json.dumps(current) if current else None, person_id))
    return {"person_id": person_id, "contact": current}


@mcp.tool(annotations=READ_ONLY)
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


@mcp.tool(annotations=READ_ONLY)
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


@mcp.tool(annotations=READ_ONLY)
def get_person_history(person_id: int, limit: int = 50,
                       since: Optional[str] = None,
                       max_chars: int = 600) -> dict:
    """Every interaction/observation entry that mentions this person, newest first —
    the payoff query. This is an indexed lookup on the entity, so 'everything about
    Tom my father' never pulls in the other Tom. Personal-reflection entries
    (kind='thought') are EXCLUDED so this stays a record of real interactions, even
    if a reflection happened to name the person. Bodies are truncated to max_chars. Also returns the
    person's full `summary` (the rolling profile, incl. their relationships), `contact`
    blob, and `aliases` (every stored surface form, exact text) — read them here before
    editing them with save_person / update_contact, so you append rather than overwrite
    (the briefing only shows a short summary preview). The `aliases` list is the exact
    text to pass back to save_person's `remove_aliases` to detach a wrong one (you can't
    guess the stored spelling — read it here first)."""
    if err := _bad_date(since, "since"):
        return err
    sql = """SELECT DISTINCT e.id, e.entry_date, e.body
             FROM entries e JOIN mentions m ON m.entry_id=e.id
             WHERE m.person_id=? AND m.status='resolved' AND e.kind != 'thought'"""
    params: list = [person_id]
    if since:
        sql += " AND e.entry_date >= ?"
        params.append(since)
    sql += (" ORDER BY e.entry_date DESC, e.day_position IS NULL, "
            "e.day_position DESC, e.id DESC LIMIT ?")
    params.append(limit)
    with db() as conn:
        person = conn.execute(
            "SELECT canonical_name, role, summary FROM people WHERE id=?", (person_id,)
        ).fetchone()
        if not person:
            return {"error": f"no person with id {person_id}"}
        contact = _get_contact(conn, person_id)
        aliases = [r["surface_form"] for r in conn.execute(
            "SELECT surface_form FROM aliases WHERE person_id=? ORDER BY surface_form",
            (person_id,),
        ).fetchall()]
        rows = conn.execute(sql, params).fetchall()
    entries = [
        {"entry_id": r["id"], "entry_date": r["entry_date"],
         "body": _truncate(r["body"], max_chars)}
        for r in rows
    ]
    return {"person_id": person_id, "name": person["canonical_name"],
            "role": person["role"], "summary": person["summary"], "contact": contact,
            "aliases": aliases, "entries": entries, "count": len(entries)}


@mcp.tool(annotations=READ_ONLY)
def search_entries(query: str, limit: int = 20, max_chars: int = 400,
                   raw_query: bool = False) -> dict:
    """Full-text search over entry bodies (FTS5). Use for topics/events, not for
    people — use get_person_history for people.

    Pass `query` as PLAIN WORDS ("chipotle bowl", "Tom's birthday"). It's tokenized
    and each term is quoted before it reaches FTS5, so apostrophes, question marks
    and stray punctuation are safe and every term must appear (AND). Set
    `raw_query=True` to pass FTS5 syntax through verbatim instead — for OR, NEAR(),
    prefix* or "quoted phrases"; a syntax error then comes back as an error you can
    correct rather than a crash."""
    match = query if raw_query else _fts_query(query)
    if not match.strip():
        return {"results": [], "count": 0,
                "note": f"no searchable terms in {query!r}"}
    with db() as conn:
        try:
            rows = conn.execute(
                """SELECT e.id, e.entry_date, e.body, e.kind
                   FROM entries_fts f JOIN entries e ON e.id = f.rowid
                   WHERE entries_fts MATCH ? ORDER BY rank LIMIT ?""",
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            return {"error": f"invalid FTS5 search syntax in {match!r}: {exc}. "
                             "Retry with plain words and raw_query=False."}
    return {"results": [
        {"entry_id": r["id"], "entry_date": r["entry_date"], "kind": r["kind"],
         "body": _truncate(r["body"], max_chars)} for r in rows
    ], "count": len(rows)}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _fts_query(q: str) -> str:
    """Turn a natural-language query into a safe FTS5 MATCH expression.

    FTS5 parses MATCH as *query syntax*, so the punctuation ordinary phrasing carries
    is a syntax ERROR, not a search: "Tom's" trips on the apostrophe, "how was my
    week?" on the '?', a lone "AND" on the dangling operator. Handing the model's
    words straight to MATCH therefore raises OperationalError on completely reasonable
    searches. So: split out word characters (keeping apostrophes inside a token) and
    wrap each token in double quotes, which makes it a literal FTS5 phrase — every
    operator and every piece of punctuation stops being syntax. Nothing needs
    escaping, because the one character that IS special inside a double-quoted FTS5
    string is the double quote, and the token pattern can't produce one. Terms are
    ANDed, FTS5's default. Callers wanting real FTS5 syntax (OR, NEAR, prefix*) opt
    out via search_entries(raw_query=True).
    """
    return " ".join(f'"{t}"' for t in re.findall(r"[\w']+", q or ""))


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


def _get_contact(conn: sqlite3.Connection, person_id: int) -> dict:
    """A person's contact blob as a dict ({} if unset or malformed)."""
    row = conn.execute("SELECT contact FROM people WHERE id=?", (person_id,)).fetchone()
    if not row or not row["contact"]:
        return {}
    try:
        v = json.loads(row["contact"])
    except (ValueError, TypeError):
        return {}
    return v if isinstance(v, dict) else {}


@mcp.tool(annotations=READ_ONLY)
def get_entry(entry_id: int, include_raw: bool = True) -> dict:
    """Fetch one full entry. Set include_raw to also return the verbatim original
    (raw_body) — the hidden fallback record kept in case the cleaned version
    dropped a detail."""
    with db() as conn:
        r = conn.execute(
            "SELECT id, body, raw_body, entry_date, kind, created_at "
            "FROM entries WHERE id=?",
            (entry_id,),
        ).fetchone()
    if not r:
        return {"error": f"no entry with id {entry_id}"}
    out = {"entry_id": r["id"], "entry_date": r["entry_date"], "kind": r["kind"],
           "created_at": r["created_at"], "body": r["body"]}
    if include_raw:
        out["raw_body"] = r["raw_body"]
    return out


@mcp.tool(annotations=WRITE_IDEMPOTENT)
def update_entry(entry_id: int, entry_date: Optional[str] = None,
                 body: Optional[str] = None, raw_body: Optional[str] = None,
                 mentions: Optional[list[str]] = None,
                 kind: Optional[str] = None) -> dict:
    """Edit an existing journal entry. Only non-null args are written.

    `kind` reclassifies the entry between "log" (interaction/observation/fact) and
    "thought" (personal reflection) — see add_journal_entry's LOG vs THOUGHT note —
    e.g. when the user says "that was really just me thinking out loud".

    Use `entry_date` (YYYY-MM-DD, Pacific) to correct the day an entry is ABOUT —
    e.g. the user said "that was actually yesterday". Dates are Pacific time; resolve
    relative phrases ("yesterday") against the current Pacific date (see get_briefing's
    `now`) before passing a concrete date here. `body` replaces the cleaned journal
    text (Markdown — paragraphs split by blank lines, bold/italics/lists where
    useful, as in add_journal_entry); `raw_body` replaces the verbatim original.

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
    if kind is not None and kind not in ("log", "thought"):
        return {"error": "kind must be 'log' or 'thought'"}
    fields = {"entry_date": entry_date, "body": body, "raw_body": raw_body,
              "kind": kind}
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
            if "entry_date" in sets:
                # Moved to a different day — its old within-day rank is meaningless there,
                # so append it to the end of the new day (reorder_entries can re-place it).
                conn.execute("UPDATE entries SET day_position=NULL WHERE id=?", (entry_id,))
                conn.execute("UPDATE entries SET day_position=? WHERE id=?",
                             (_next_day_position(conn, sets["entry_date"]), entry_id))
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


@mcp.tool(annotations=WRITE_IDEMPOTENT)
def reorder_entries(entry_date: str, ordered_entry_ids: list[int]) -> dict:
    """Set the chronological order of a day's entries — the order they read top-to-bottom
    in the journal (earliest event first).

    Entries are appended in the order they're SAVED, which is often NOT the order events
    happened (people recount a day out of sequence). Pass `ordered_entry_ids` as that
    day's entry ids EARLIEST-FIRST and the server renumbers them. Use this:
      - right after capturing several entries for a day that came out of order;
      - after adding an entry to a day where it belongs earlier than existing ones;
      - when the user says to move one ("put the gym before dinner", "move the call to
        between leaving the Airbnb and getting home") — list the day's ids in the new
        order, with the moved one in its new slot.
    You don't have to list every id: any entry on the day you omit keeps its place AFTER
    the ones you listed (same as reorder_plan). Ids that aren't on `entry_date` are
    ignored. Returns the resulting order. Get the ids + bodies from get_briefing's recent
    entries, get_person_history, or search_entries."""
    if err := _bad_date(entry_date, "entry_date"):
        return err
    with db() as conn:
        day_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM entries WHERE entry_date=? "
            "ORDER BY day_position IS NOT NULL, day_position, id",
            (entry_date,),
        ).fetchall()]
        day_set = set(day_ids)
        ordered, seen = [], set()
        for eid in (ordered_entry_ids or []):
            if eid in day_set and eid not in seen:
                ordered.append(eid)
                seen.add(eid)
        for eid in day_ids:  # entries left out keep their current relative order, after
            if eid not in seen:
                ordered.append(eid)
                seen.add(eid)
        for pos, eid in enumerate(ordered, start=1):
            conn.execute("UPDATE entries SET day_position=? WHERE id=?", (pos, eid))
    return {"entry_date": entry_date, "order": ordered, "count": len(ordered)}


def _delete_record(kind: str, id: int) -> dict:
    """Shared delete implementation behind both servers' delete_record tools. Maps
    `kind` to its table, deletes the row, and (for sets) renumbers the remaining
    set_index so it stays contiguous."""
    tables = {"entry": "entries", "drink": "drinks", "intake_item": "intake_items",
              "workout": "workouts", "set": "sets", "weight": "body_weight"}
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


@mcp.tool(annotations=DESTRUCTIVE)
def delete_record(kind: str, id: int) -> dict:
    """Permanently delete one journal-side record. Irreversible — confirm first.

    `kind` selects what `id` refers to:
      - "entry" — a journal entry (its mentions go too; FTS stays in sync).
      - "intake_item" — one logged thing (a meal, a beer, a glass of water). The
        day's totals re-derive from what's left, so nothing else needs fixing.
    Find ids with get_entry/search_entries, or get_intake/log_intake for items.
    (Workouts and sets are deleted via the trainer server's own delete_record.)"""
    if kind not in ("entry", "intake_item"):
        return {"error": f"unknown kind {kind!r}; this server deletes one of "
                         "['entry', 'intake_item'] (use the trainer server "
                         "for workout/set)"}
    return _delete_record(kind, id)


@mcp.tool(annotations=DESTRUCTIVE)
def merge_people(survivor_person_id: int, loser_person_id: int) -> dict:
    """Merge two person records that turn out to be the same human — e.g. if "Tom"
    and "Tom Smith" were created before they were linked. All of the loser's
    aliases, mentions, and group memberships move onto the survivor (duplicates
    deduped; the loser's canonical name becomes an alias so the surface form stays
    discoverable). The loser is then deleted. Irreversible.

    This is a relational merge only — the survivor's role/notes/summary/contact are
    NOT overwritten. The loser's fields are returned as `discarded_fields` so you can
    decide whether any are worth copying onto the survivor via
    save_person/update_contact(person_id=survivor_person_id, …)."""
    if survivor_person_id == loser_person_id:
        return {"error": "survivor and loser must be different people"}
    with db() as conn:
        if not conn.execute("SELECT 1 FROM people WHERE id=?", (survivor_person_id,)).fetchone():
            return {"error": f"no person with id {survivor_person_id} (survivor)"}
        loser = conn.execute(
            """SELECT id, canonical_name, role, notes, summary, contact
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
        discarded = {k: loser[k] for k in ("role", "notes", "summary") if loser[k]}
        if loser["contact"]:
            try:
                discarded["contact"] = json.loads(loser["contact"])
            except (ValueError, TypeError):
                pass
    return {
        "survivor_person_id": survivor_person_id,
        "merged_person_id": loser_person_id,
        "merged_canonical_name": loser["canonical_name"],
        "moved": {"aliases": moved_aliases, "mentions": moved_mentions,
                  "groups": moved_groups},
        "discarded_fields": discarded,
    }


@mcp.tool(annotations=READ_ONLY)
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
               JOIN entries e ON e.id = m1.entry_id
               WHERE m1.person_id=? AND m1.status='resolved' AND m2.status='resolved'
                     AND e.kind != 'thought'
               GROUP BY p.id ORDER BY shared DESC LIMIT ?""",
            (person_id, limit),
        ).fetchall()
    return {"person_id": person_id, "related": [
        {"person_id": r["id"], "name": r["canonical_name"],
         "role": r["role"], "shared_entries": r["shared"]} for r in rows
    ]}


@mcp.tool(annotations=READ_ONLY)
def get_briefing(recent_entries: int = 5, recent_days: Optional[int] = None) -> dict:
    """One-call session context. Returns the people roster (id, name, role, groups,
    short summary), the pending-mention count, the list of groups, and the most
    recent entries — by default the last `recent_entries` (5); pass `recent_days`
    instead to get EVERY entry from the last N Pacific days (e.g. recent_days=7 for
    a week of catch-up context before a debrief). Also returns `now`: the current
    Pacific date/time — all dates in this log are Pacific, so use it to anchor
    "today"/"yesterday" before defaulting or computing any entry_date. Call this at
    the start of a conversation so you know who and what the user is likely talking
    about."""
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
        if recent_days:
            cutoff = (date.fromisoformat(today()) - timedelta(days=recent_days - 1)).isoformat()
            recent = conn.execute(
                "SELECT id, entry_date, body FROM entries WHERE entry_date >= ? "
                "ORDER BY entry_date DESC, day_position IS NULL, day_position DESC, id DESC "
                "LIMIT 100",
                (cutoff,),
            ).fetchall()
        else:
            recent = conn.execute(
                "SELECT id, entry_date, body FROM entries "
                "ORDER BY entry_date DESC, day_position IS NULL, day_position DESC, id DESC LIMIT ?",
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


def _days_since(d: Optional[str], ref: Optional[str] = None) -> Optional[int]:
    if not d:
        return None
    try:
        base = date.fromisoformat(ref) if ref else date.fromisoformat(today())
        return (base - date.fromisoformat(d)).days
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


def _tokens_ex(s: str) -> list[str]:
    """A name's words, lowercased and SORTED, so word order drops out: 'crunch cable'
    and 'Cable Crunch' both become ['cable', 'crunch']. Catalog names routinely get
    spoken back-to-front ('curl hammer', 'press incline db'), and order shouldn't cost a
    match."""
    return sorted(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _name_query_match(name: str, q: str) -> bool:
    """True if EVERY word of `q` appears (as a substring) in `name`, order-independent —
    so 'crunch cable' finds 'Cable Crunch' and a partial 'cable cru' still narrows. The
    library page's name filter; a stricter, browse-style match than the fuzzy resolver
    (no typo tolerance — it's filtering a list the user is reading, not resolving one
    spoken name)."""
    nl = (name or "").lower()
    toks = re.findall(r"[a-z0-9]+", (q or "").lower())
    return all(t in nl for t in toks) if toks else True


def _score_exercise_name(surface: str, name: str) -> float:
    """0..1 similarity of a spoken name to a catalog name. Exact, a punctuation/spacing-
    only difference, OR the same words in a different order all win (1.0); reordered-with-
    typos still scores via a token-sorted Jaro-Winkler; phonetic agreement floors it
    (transcription noise)."""
    s, a = (surface or "").lower().strip(), (name or "").lower().strip()
    if not s or not a:
        return 0.0
    st, at = _tokens_ex(s), _tokens_ex(a)
    if s == a or _norm_ex(s) == _norm_ex(a) or (st and st == at):
        return 1.0
    # order-insensitive fuzzy: compare the names word-sorted, so 'crunch cabel' (typo +
    # reordered) still lands near 'Cable Crunch' instead of being tanked by word order.
    jw = max(jellyfish.jaro_winkler_similarity(s, a),
             jellyfish.jaro_winkler_similarity(" ".join(st), " ".join(at)))
    if phonetic(surface) and phonetic(surface) == phonetic(name):
        jw = max(jw, 0.88)  # sounds-the-same floor
    return round(jw, 3)


def _alias_map(conn: sqlite3.Connection) -> dict[int, list[str]]:
    """{exercise_id: [alias, ...]} for the whole catalog, one query — so matching can
    score a name against every surface form without a per-row lookup."""
    out: dict[int, list[str]] = {}
    for r in conn.execute("SELECT exercise_id, alias FROM exercise_aliases"):
        out.setdefault(r["exercise_id"], []).append(r["alias"])
    return out


def _match_exercises(conn: sqlite3.Connection, name: str, limit: int = 5) -> list[dict]:
    """Rank the catalog against a spoken name, best first. Returns
    [{exercise_id, name, score, in_rotation, primary}] with score >= EX_MATCH_FLOOR, so a
    caller that can't confidently resolve a name can hand the user the closest real
    entries instead of guessing. Each exercise is scored against its canonical name AND
    its AKAs (best surface form wins), so searching 'rdl' or 'bench' surfaces the right
    lift. In-rotation movements are listed first among equals. Archived (soft-deleted)
    movements are never surfaced — they're invisible to discovery."""
    rows = conn.execute(
        "SELECT id, name, in_rotation FROM exercises WHERE archived=0 ORDER BY name"
    ).fetchall()
    amap = _alias_map(conn)

    def best(r) -> float:
        forms = [r["name"], *amap.get(r["id"], [])]
        return max(_score_exercise_name(name, f) for f in forms)

    scored = sorted(
        ((best(r), int(r["in_rotation"]), r) for r in rows),
        key=lambda t: (t[0], t[1]), reverse=True,
    )
    return [{"exercise_id": r["id"], "name": r["name"], "score": sc,
             "in_rotation": bool(r["in_rotation"]),
             "primary": _muscles_for(conn, r["id"])["primary"]}
            for sc, _rot, r in scored[:limit] if sc >= EX_MATCH_FLOOR]


def _resolve_exercise(conn: sqlite3.Connection, name: str, include_archived: bool = False):
    """Resolve a spoken name to ONE catalog row, or None. Tries exact on the canonical
    name (case-insensitive), then an exact AKA match (so 'RDL' lands on Romanian
    Deadlift), then a punctuation/spacing- or phonetics-tolerant high-confidence fuzzy
    match (so 'pullup' finds 'Pull Up' and a near-spelling lands on the right row instead
    of spawning a duplicate). A merely plausible name — below the confident bar, or with a
    near-tie runner-up — returns None, leaving the caller to surface `_match_exercises`
    candidates rather than silently pick the wrong lift. An AKA shared by more than one
    exercise is ambiguous, so it falls through to fuzzy/candidates rather than guessing.

    Archived (soft-deleted) movements stay invisible to the model: the default skips them,
    so logging/enriching/swapping by name can't reach an archived lift. The website's add
    form passes include_archived=True so re-adding a name that's archived re-uses (and
    restores) that row instead of colliding on the UNIQUE name."""
    name = (name or "").strip()
    if not name:
        return None
    row = conn.execute(
        "SELECT * FROM exercises WHERE lower(name)=lower(?)"
        + ("" if include_archived else " AND archived=0"),
        (name,),
    ).fetchone()
    if row:
        return row
    hits = conn.execute(
        """SELECT e.* FROM exercises e
           JOIN exercise_aliases a ON a.exercise_id = e.id
           WHERE a.alias = lower(?)"""
        + ("" if include_archived else " AND e.archived=0"),
        (name,),
    ).fetchall()
    if len(hits) == 1:
        return hits[0]
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


def _aliases_for(conn: sqlite3.Connection, exercise_id: int) -> list[str]:
    """The exercise's AKAs (common alternative names), sorted, lowercased as stored."""
    return [r["alias"] for r in conn.execute(
        "SELECT alias FROM exercise_aliases WHERE exercise_id=? ORDER BY alias",
        (exercise_id,),
    )]


def _set_aliases(conn: sqlite3.Connection, exercise_id: int,
                 aliases: Optional[list[str]], *, replace: bool = True) -> None:
    """Write an exercise's AKAs. No-op when `aliases` is None (leave them untouched).
    With replace=True (the default) the whole set is rewritten; replace=False merges the
    new ones onto whatever's there (the seed/import path, so a re-run never clobbers an
    AKA the user added by hand). Blanks and any form equal to the canonical name are
    dropped, everything is lowercased+deduped, so search treats them uniformly."""
    if aliases is None:
        return
    canon = conn.execute(
        "SELECT lower(name) AS n FROM exercises WHERE id=?", (exercise_id,)
    ).fetchone()
    canon_name = canon["n"] if canon else ""
    if replace:
        conn.execute("DELETE FROM exercise_aliases WHERE exercise_id=?", (exercise_id,))
    seen: set[str] = set()
    for a in aliases:
        a = (a or "").strip().lower()
        if a and a != canon_name and a not in seen:
            seen.add(a)
            conn.execute(
                "INSERT OR IGNORE INTO exercise_aliases(exercise_id, alias) VALUES (?,?)",
                (exercise_id, a),
            )


def _exercise_brief(conn: sqlite3.Connection, r) -> dict:
    return {"exercise_id": r["id"], "name": r["name"], "category": r["category"],
            "equipment": r["equipment"], "muscles": _muscles_for(conn, r["id"])}


def _get_profile(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT value FROM settings WHERE key='profile'").fetchone()
    return json.loads(row["value"]) if row else {}


# --------------------------------------------------------------------------- #
# Drinking — legacy `drinks` table, NOT MCP tools.
#
# Alcohol now lives on the nutrition row (see NUTRIENTS); these functions and the
# table they write are kept only so the fold-in migration has a source and old data
# stays readable. Nothing in the app calls them any more: the feed's drinks ring
# quick-add logs an intake item through log_intake, exactly like the model does. They never carried an @mcp.tool()
# decorator (a day's drinks are one number, faster to tap than to describe), so
# removing them later is a pure deletion — no tool surface to break.
# --------------------------------------------------------------------------- #

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
    So pass only the increment, not the running total. To correct a day down (or
    overwrite it) use update_drink, which sets absolutes.

    Pass `standard_drinks=0` to record a day CONFIRMED sober ("I didn't drink
    today"). That's different from a day with no row, which just means nothing was
    logged; both count as sober everywhere it matters (streaks, averages), so the 0
    is purely a record that the day was accounted for.

    Args:
        standard_drinks: Standard drinks to ADD for this day (the increment); 0
            marks the day sober without adding anything.
        drink_date: Day consumed, YYYY-MM-DD. Defaults to today.
        kind: Optional label, e.g. "beer", "wine", "cocktail".
        notes: Optional context, e.g. "dinner with Hallie".
    """
    if err := _bad_date(drink_date, "drink_date"):
        return err
    if standard_drinks < 0:
        return {"error": f"standard_drinks can't be negative, got {standard_drinks}"}
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


def get_drink_summary(days: int = 30, since: Optional[str] = None,
                      until: Optional[str] = None, include_rows: bool = False) -> dict:
    """Drinking trends over a window: per-day totals plus rolling stats.

    Use the default `days` window, or pass an explicit `since`/`until` range. Sober
    days are counted, not stored — except a day explicitly logged as 0, which does
    have a row (it means "confirmed sober" rather than "not logged"); it shows in
    `daily` with total 0 but is NOT a drinking day and doesn't touch the streak.
    `current_sober_streak` is the number of days since the last day with drinks
    (0 if the user drank today, null if never).

    Set `include_rows=True` to also get the individual drink rows WITH ids (`drinks`)
    — needed to FIX a logged drink: find the `drink_id` here, then pass it to
    update_drink or _delete_record("drink", id).

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
        # A 0 row is a confirmed-sober day, so it must not count as "last drink".
        last = conn.execute(
            "SELECT MAX(drink_date) AS d FROM drinks WHERE standard_drinks > 0"
        ).fetchone()["d"]
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
    drinking_days = sum(1 for d in daily if d["total"] > 0)  # a 0 row isn't one
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


def update_drink(drink_id: int, standard_drinks: Optional[float] = None,
                 drink_date: Optional[str] = None, kind: Optional[str] = None,
                 notes: Optional[str] = None) -> dict:
    """Correct a logged day's drink row. Only non-null args are written; the values
    are absolute (they REPLACE, not add — unlike log_drinks, which accumulates).

    Use this to fix a mistake in either direction — e.g. set `standard_drinks` to 1
    when the day was over-logged (log_drinks only adds, so corrections downward go
    through here), or to 0 to turn a mis-logged day into a CONFIRMED-sober one (the
    row stays, recording that the day was accounted for; deleting removes it
    entirely, back to "never logged"), relabel `kind`, or move the day with
    `drink_date` (YYYY-MM-DD, Pacific). Since a day has exactly one row, moving onto
    a day that already has one is refused — edit that day instead. Find the
    `drink_id` with get_drink_summary(include_rows=True). To remove a day entirely
    use _delete_record("drink", id)."""
    if err := _bad_date(drink_date, "drink_date"):
        return err
    if standard_drinks is not None and standard_drinks < 0:
        return {"error": f"standard_drinks can't be negative, got {standard_drinks}"}
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
# Intake tools (food, alcohol, water — one row per thing consumed)
# --------------------------------------------------------------------------- #

# The numeric nutrient columns, in the order they read back. One list so adding a
# nutrient later doesn't mean editing every sum/average/round site. Alcohol and water
# are in here deliberately: a beer and a sandwich are the same kind of fact, so they
# share one shape and one code path — no parallel table, no per-nutrient special case.
NUTRIENTS = ("calories", "protein_g", "carbs_g", "fat_g", "sodium_mg", "fiber_g",
             "standard_drinks", "water_oz")


def _item_row(r) -> dict:
    """One logged item, token-compact: only the nutrients it actually carries."""
    out = {"item_id": r["id"], "food_date": r["food_date"], "item": r["item"]}
    if r["note"]:
        out["note"] = r["note"]
    for m in NUTRIENTS:
        if r[m] is not None:
            out[m] = round(r[m], 1)
    return out


def _day_totals(rows) -> dict:
    """Sum a day's items per nutrient. A nutrient no item carries stays ABSENT (not 0)
    — the day simply wasn't estimated for it, which is a different fact from zero."""
    totals = {}
    for m in NUTRIENTS:
        vals = [r[m] for r in rows if r[m] is not None]
        if vals:
            totals[m] = round(sum(vals), 1)
    return totals


@mcp.tool(annotations=WRITE)
def log_intake(item: str = "", food_date: Optional[str] = None,
             calories: Optional[float] = None, protein_g: Optional[float] = None,
             carbs_g: Optional[float] = None, fat_g: Optional[float] = None,
             sodium_mg: Optional[float] = None, fiber_g: Optional[float] = None,
             standard_drinks: Optional[float] = None, water_oz: Optional[float] = None,
             note: Optional[str] = None) -> dict:
    """Log ONE thing consumed — a meal, a snack, a beer, a glass of water. Call it once
    per item, not once per day: "a sandwich and a beer" is TWO calls. Each becomes its
    own row, and the day's totals are summed from them, so nothing here is a running
    total you have to maintain.

    Keep `item` the thing itself, in the user's own terms, concise and concrete
    ("2 eggs, toast, black coffee", "chipotle bowl", "12oz water"). Put how it sat —
    cravings, feeling stuffed, skipped on purpose — in `note`.

    ALCOHOL AND WATER are items too, logged exactly like food: "two beers" is
    `item="two beers"` with `standard_drinks=2` (plus its calories); "a big glass of
    water" is `item="glass of water"` with `water_oz=16`. Convert to standard drinks
    before calling — a regular beer, a 5oz glass of wine, or a shot each count ~1.0;
    a strong cocktail ~1.5; a tallboy/double ~2.0. Water is in fluid ounces (128 = a
    gallon).

    The nutrient numbers are OPTIONAL and stay NULL until filled in. Estimate them
    when the user wants numbers tracked or asks how a day is adding up (that judgment
    is yours — the server does no food lookup); leave them out when they're just
    telling you what they ate. Don't pass 0 for "unknown": NULL reads as unestimated,
    0 as a real zero. A day the user says they didn't drink is `standard_drinks=0`
    with no other numbers — that's a record that the day was accounted for.

    To fix a logged item use update_intake_item (by `item_id`, which this returns and
    get_intake lists); to remove one, delete_record(kind="intake_item", id=...).
    Neither needs any arithmetic — the day's totals re-derive themselves.

    Args:
        item: What was consumed, e.g. "chipotle bowl". Optional only when logging a
            bare number, like a water top-up from the app.
        food_date: Day consumed, YYYY-MM-DD (Pacific). Defaults to today.
        calories: Optional calories for THIS item.
        protein_g: Optional grams of protein.
        carbs_g: Optional grams of carbs.
        fat_g: Optional grams of fat.
        sodium_mg: Optional milligrams of sodium.
        fiber_g: Optional grams of fiber.
        standard_drinks: Optional standard drinks of alcohol.
        water_oz: Optional fluid ounces of water.
        note: Optional context for this item.
    """
    if err := _bad_date(food_date, "food_date"):
        return err
    item = (item or "").strip()
    nutrients = {"calories": calories, "protein_g": protein_g, "carbs_g": carbs_g,
                 "fat_g": fat_g, "sodium_mg": sodium_mg, "fiber_g": fiber_g,
                 "standard_drinks": standard_drinks, "water_oz": water_oz}
    for k, v in nutrients.items():
        if v is not None and v < 0:
            return {"error": f"{k} must not be negative, got {v}"}
    if not item and all(v is None for v in nutrients.values()):
        return {"error": "nothing to log — pass an item and/or a nutrient amount"}
    d = food_date or today()
    cols = ("food_date", "position", "item", "note", *NUTRIENTS, "created_at")
    with db() as conn:
        # Position is the order logged within the day — the server assigns it, same
        # deterministic append as entries' day_position.
        nxt = conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS n FROM intake_items WHERE food_date=?",
            (d,),
        ).fetchone()["n"]
        rid = conn.execute(
            "INSERT INTO intake_items(" + ", ".join(cols) + ") VALUES ("
            + ",".join("?" * len(cols)) + ")",
            (d, nxt, item or None, note, *(nutrients[m] for m in NUTRIENTS), now()),
        ).lastrowid
        rows = conn.execute(
            "SELECT * FROM intake_items WHERE food_date=? ORDER BY position, id", (d,)
        ).fetchall()
        row = next(r for r in rows if r["id"] == rid)
    return {**_item_row(row), "day_totals": _day_totals(rows)}


@mcp.tool(annotations=READ_ONLY)
def get_intake(days: int = 14, since: Optional[str] = None,
                  until: Optional[str] = None, include_items: bool = True) -> dict:
    """Read the intake log back: per day, its items (with ids) and summed totals.

    Use this before commenting on how someone's been eating, and to find the `item_id`
    of the thing to correct — update_intake_item and delete_record both work by id, so
    a fix never involves recomputing a day. Days with nothing logged are omitted; they
    are NOT zero-calorie days.

    A day's totals sum only the items that carry each nutrient, and a nutrient no item
    carries is absent rather than 0. `averages` work the same way across days — each
    has its OWN denominator, so a week with protein on 7 days and sodium on 2 averages
    sodium over those 2. Check `logged_days` and the per-day rows before calling any
    of it a weekly average.

    Args:
        days: Size of the trailing window in days (ignored if `since` is given).
        since: Start date YYYY-MM-DD (inclusive).
        until: End date YYYY-MM-DD (inclusive). Defaults to today.
        include_items: Include each day's individual items. Set False for totals only.
    """
    if err := _bad_date(since, "since") or _bad_date(until, "until"):
        return err
    until = until or today()
    if since is None:
        since = date.fromordinal(
            date.fromisoformat(until).toordinal() - max(days, 1) + 1).isoformat()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM intake_items WHERE food_date BETWEEN ? AND ? "
            "ORDER BY food_date DESC, position, id", (since, until),
        ).fetchall()
    by_day: dict = {}
    for r in rows:
        by_day.setdefault(r["food_date"], []).append(r)
    out_days = []
    for d, items in by_day.items():
        day = {"food_date": d, "totals": _day_totals(items)}
        if include_items:
            day["items"] = [_item_row(r) for r in items]
        out_days.append(day)
    averages = {}
    for m in NUTRIENTS:
        vals = [t for t in (_day_totals(i).get(m) for i in by_day.values())
                if t is not None]
        if vals:
            averages[m] = round(sum(vals) / len(vals), 1)
    window_days = (date.fromisoformat(until).toordinal()
                   - date.fromisoformat(since).toordinal() + 1)
    return {
        "since": since, "until": until, "window_days": window_days,
        "logged_days": len(by_day),
        "days": out_days,
        "averages": averages,
    }


@mcp.tool(annotations=WRITE_IDEMPOTENT)
def update_intake_item(item_id: int, item: Optional[str] = None,
                       food_date: Optional[str] = None,
                       calories: Optional[float] = None, protein_g: Optional[float] = None,
                       carbs_g: Optional[float] = None, fat_g: Optional[float] = None,
                       sodium_mg: Optional[float] = None, fiber_g: Optional[float] = None,
                       standard_drinks: Optional[float] = None,
                       water_oz: Optional[float] = None,
                       note: Optional[str] = None) -> dict:
    """Correct ONE logged item. Only the args you pass are written, and they REPLACE
    that item's values (log_intake adds a new item; this edits an existing one).

    This is the whole correction path: "that bowl was 600, not 1100" is one call with
    `calories=600` — you do NOT recompute the day, because the day's totals are summed
    from the items. `food_date` moves the item to another day. To remove it entirely,
    delete_record(kind="intake_item", id=...). Find ids with get_intake.

    Args:
        item_id: The item to correct (from get_intake or log_intake).
        item: Replacement text for what it was.
        food_date: Move it to this day, YYYY-MM-DD (Pacific).
        calories: Replacement calories for this item.
        protein_g: Replacement grams of protein.
        carbs_g: Replacement grams of carbs.
        fat_g: Replacement grams of fat.
        sodium_mg: Replacement milligrams of sodium.
        fiber_g: Replacement grams of fiber.
        standard_drinks: Replacement standard drinks.
        water_oz: Replacement fluid ounces of water.
        note: Replacement note for this item.
    """
    if err := _bad_date(food_date, "food_date"):
        return err
    fields = {"item": item, "note": note, "food_date": food_date,
              "calories": calories, "protein_g": protein_g, "carbs_g": carbs_g,
              "fat_g": fat_g, "sodium_mg": sodium_mg, "fiber_g": fiber_g,
              "standard_drinks": standard_drinks, "water_oz": water_oz}
    for m in NUTRIENTS:
        if fields[m] is not None and fields[m] < 0:
            return {"error": f"{m} must not be negative, got {fields[m]}"}
    sets = {k: v for k, v in fields.items() if v is not None}
    if not sets:
        return {"item_id": item_id, "updated": []}
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM intake_items WHERE id=?", (item_id,)).fetchone()
        if not row:
            return {"error": f"no intake item with id {item_id}"}
        if food_date and food_date != row["food_date"]:
            # Moving days: append to the end of the destination day.
            sets["position"] = conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 AS n FROM intake_items "
                "WHERE food_date=?", (food_date,),
            ).fetchone()["n"]
        conn.execute(
            "UPDATE intake_items SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?",
            (*sets.values(), item_id),
        )
        cur = conn.execute("SELECT * FROM intake_items WHERE id=?", (item_id,)).fetchone()
        rows = conn.execute(
            "SELECT * FROM intake_items WHERE food_date=? ORDER BY position, id",
            (cur["food_date"],),
        ).fetchall()
    return {**_item_row(cur), "updated": [k for k in sets if k != "position"],
            "day_totals": _day_totals(rows)}


# --------------------------------------------------------------------------- #
# Trainer: exercise catalog
# --------------------------------------------------------------------------- #

def _upsert_exercise(*, name, exercise_id, category, equipment, muscles,
                     secondary_muscles, tertiary_muscles, technique_notes,
                     common_mistakes, cautions, video_link, image_link, image_link_end,
                     slug, force, level, mechanic, in_rotation, hearted=None, aliases=None,
                     allow_create: bool) -> dict:
    """Shared worker behind save_exercise (enrich-only, allow_create=False) and
    create_exercise (the website's add form, allow_create=True). New rows are born ONLY
    when allow_create is set, which the model-facing tool never passes — that's what keeps
    the catalog growable by the user's hand alone."""
    if in_rotation is not None:
        in_rotation = int(bool(in_rotation))
    if hearted is not None:
        hearted = int(bool(hearted))
    # rotation ⊆ hearted: putting a lift in the rotation pulls it into the superset too.
    if in_rotation:
        hearted = 1
    with db() as conn:
        if exercise_id is not None:
            row = conn.execute("SELECT id FROM exercises WHERE id=?", (exercise_id,)).fetchone()
            if not row:
                return {"error": f"no exercise with id {exercise_id}"}
        elif name:
            # The create path (allow_create) considers archived rows so re-adding a
            # movement reuses & restores it rather than colliding on the UNIQUE name.
            row = _resolve_exercise(conn, name, include_archived=allow_create)
        else:
            return {"error": "pass name or exercise_id"}
        # Writable scalar columns (everything but name/created_at and the muscle tiers).
        fields = {"slug": slug, "category": category, "force": force, "level": level,
                  "mechanic": mechanic, "equipment": equipment,
                  "technique_notes": technique_notes, "common_mistakes": common_mistakes,
                  "cautions": cautions, "video_link": video_link, "image_link": image_link,
                  "image_link_end": image_link_end, "in_rotation": in_rotation,
                  "hearted": hearted}
        sets_ = {k: v for k, v in fields.items() if v is not None}
        if row:
            eid = row["id"]
            created = False
            # Re-adding (via the website's add form) a movement that was archived brings
            # it back into the library along with whatever fields the add supplies.
            if allow_create and "archived" in row.keys() and row["archived"]:
                sets_["archived"] = 0
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
        _set_aliases(conn, eid, aliases)
        out_name = conn.execute("SELECT name FROM exercises WHERE id=?", (eid,)).fetchone()["name"]
    if created:
        return {"exercise_id": eid, "name": out_name, "created": True}
    muscles_changed = (muscles is not None or secondary_muscles is not None
                       or tertiary_muscles is not None)
    return {"exercise_id": eid, "name": out_name, "created": False,
            "updated": list(sets_) + (["muscles"] if muscles_changed else [])
                       + (["aka"] if aliases is not None else [])}


@trainer_mcp.tool(annotations=WRITE_IDEMPOTENT)
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
                  image_link_end: Optional[str] = None,
                  slug: Optional[str] = None,
                  force: Optional[str] = None,
                  level: Optional[str] = None,
                  mechanic: Optional[str] = None,
                  aliases: Optional[list[str]] = None,
                  in_rotation: Optional[bool] = None,
                  hearted: Optional[bool] = None) -> dict:
    """ENRICH an exercise already in the LIBRARY (browsable at /trainer/library), or set
    its rotation / hearted flag. The catalog is CLOSED to you: you CANNOT create exercises — the
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
    saved technique notes yet". Pass `in_rotation=True` to add it to the (small) programming
    pool, or `hearted=True` to add it to the wider favorites SUPERSET the rotation is drawn
    from — rotation IMPLIES hearted, so in_rotation=True hearts it too. Only set
    `in_rotation=True` on the user's EXPLICIT request to grow their rotation — never bundle it
    into a routine enrichment, since the user keeps that pool small to control progression.
    Prefer the dedicated `set_rotation` / `set_hearted` tools; logging a movement hearts it
    for you (onto the favorites bench) but never adds it to the rotation.

    MUSCLES come in three EMPHASIS tiers — `muscles` (primary: what the lift is for),
    `secondary_muscles` (real assistance), and `tertiary_muscles` (lightly involved) —
    so "how hard each muscle is worked" is captured, e.g. a Kettlebell Thruster is
    muscles=["shoulders"], secondary_muscles=["quadriceps","glutes"],
    tertiary_muscles=["triceps"]. Passing ANY tier REPLACES the whole mapping (a muscle
    listed in two tiers lands in the higher one), so send every tier you want kept. Use
    the canonical muscle labels so recency lines up (they mirror the imported library's
    vocabulary): abdominals, abductors, adductors, biceps, calves, chest, forearms,
    glutes, hamstrings, lats, lower back, middle back, neck, quadriceps, shoulders, traps,
    triceps. Returns the exercise_id and the fields updated.

    AKAs — pass `aliases` (a list of common alternative names this movement is searched or
    spoken by, e.g. ["rdl","stiff leg deadlift"] for a Romanian Deadlift) to make those
    surface the exercise in search and resolution. Lowercased on store; passing a list
    REPLACES the existing AKAs (send every one you want kept), and the canonical name is
    never stored as its own AKA. AKAs never create a new row — the catalog stays closed."""
    return _upsert_exercise(
        name=name, exercise_id=exercise_id, category=category, equipment=equipment,
        muscles=muscles, secondary_muscles=secondary_muscles, tertiary_muscles=tertiary_muscles,
        technique_notes=technique_notes, common_mistakes=common_mistakes, cautions=cautions,
        video_link=video_link, image_link=image_link, image_link_end=image_link_end,
        slug=slug, force=force, level=level,
        mechanic=mechanic, aliases=aliases, in_rotation=in_rotation, hearted=hearted,
        allow_create=False)


def create_exercise(name: str, category: Optional[str] = None, equipment: Optional[str] = None,
                    muscles: Optional[list[str]] = None,
                    secondary_muscles: Optional[list[str]] = None,
                    tertiary_muscles: Optional[list[str]] = None,
                    technique_notes: Optional[str] = None,
                    common_mistakes: Optional[str] = None,
                    cautions: Optional[str] = None,
                    video_link: Optional[str] = None,
                    image_link: Optional[str] = None,
                    image_link_end: Optional[str] = None,
                    slug: Optional[str] = None,
                    force: Optional[str] = None,
                    level: Optional[str] = None,
                    mechanic: Optional[str] = None,
                    aliases: Optional[list[str]] = None,
                    in_rotation: Optional[bool] = None,
                    hearted: Optional[bool] = None) -> dict:
    """Add a new exercise to the library — the trusted, NON-tool write path: the website's
    manual add form and the bulk importer (scripts/import_exercises.py) are the only
    callers, so the assistant (whose save_exercise can't create) never grows the catalog.
    A name already on file is updated rather than duplicated. `aliases` sets its AKAs
    (common alternative names — see save_exercise). `in_rotation`/`hearted` are left
    untouched by default (None) — the import keeps new library entries out of both pools,
    while the add panel passes hearted=True so a movement the user deliberately adds lands
    in their favorites SUPERSET (ready to promote into the small rotation when they curate
    it). rotation IMPLIES hearted. Returns like save_exercise."""
    return _upsert_exercise(
        name=name, exercise_id=None, category=category, equipment=equipment,
        muscles=muscles, secondary_muscles=secondary_muscles, tertiary_muscles=tertiary_muscles,
        technique_notes=technique_notes, common_mistakes=common_mistakes, cautions=cautions,
        video_link=video_link, image_link=image_link, image_link_end=image_link_end,
        slug=slug, force=force, level=level,
        mechanic=mechanic, aliases=aliases, in_rotation=in_rotation, hearted=hearted,
        allow_create=True)


def set_archived(exercise_id: int, archived: bool = True) -> dict:
    """Archive (soft-delete) or restore a library exercise — the website's "remove from
    library" control. The NON-tool path, like create_exercise: deliberately NOT a FastMCP
    tool, so the journal/trainer connectors can't archive, and the assistant has no idea an
    archived movement ever existed.

    Archiving HIDES the movement everywhere the catalog is DISCOVERED — the library page,
    name search, name-resolution (logging/enriching/swapping by name), swap suggestions,
    and the trainer's rotation — and drops it from the rotation AND the hearted superset,
    all WITHOUT deleting the
    row, so past workouts that reference it keep their links and history stays intact. It's
    a clean "acts deleted" without breaking the data. Pass archived=False to restore it to
    the library; re-adding a movement by the same name on the add form restores it too.
    Returns the exercise_id, name, and new archived state."""
    archived = int(bool(archived))
    with db() as conn:
        row = conn.execute(
            "SELECT id, name FROM exercises WHERE id=?", (exercise_id,)
        ).fetchone()
        if not row:
            return {"error": f"no exercise with id {exercise_id}"}
        if archived:
            # Archiving also drops it from the rotation AND the hearted superset, so a
            # hidden movement never lingers in any pool the trainer draws from.
            conn.execute(
                "UPDATE exercises SET archived=1, in_rotation=0, hearted=0 WHERE id=?",
                (exercise_id,)
            )
        else:
            conn.execute("UPDATE exercises SET archived=0 WHERE id=?", (exercise_id,))
    return {"exercise_id": row["id"], "name": row["name"], "archived": bool(archived)}


@trainer_mcp.tool(annotations=WRITE_IDEMPOTENT)
def set_rotation(name: Optional[str] = None, exercise_id: Optional[int] = None,
                 in_rotation: bool = True) -> dict:
    """Add an exercise to (or remove it from) the user's ROTATION — the small curated pool
    (the user keeps it to ~10-14) of movements they're actively training so progress on each
    is easy to track. The rotation is the ONLY set you program routines from. It's drawn from
    the wider HEARTED superset (see set_hearted) — a bench of favorites the user pulls from
    when they swap the rotation every few months — which in turn sits inside the browsable
    ~870-movement library. Target by `exercise_id` or `name` (case-insensitive); an unknown
    name is an error here (add it via the library first). Pass in_rotation=False to take it
    out (it STAYS hearted — pruning the rotation keeps it in the favorites bench). Adding to
    the rotation also hearts it (rotation ⊆ hearted). The rotation is deliberately small so the
    user can control their progression, so ONLY add to it on the user's EXPLICIT instruction —
    never on your own judgement while programming a session, and never read approval of a
    workout as approval to grow the pool. Logging a movement only hearts it (onto the favorites
    bench), never adds it to the rotation — this tool is the ONLY in-chat way the rotation
    grows, so reach for it on a clear request — e.g. "add Bulgarian split squats to my
    rotation" — and to prune back toward ~14."""
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
        # rotation ⊆ hearted: joining the rotation also hearts it; leaving stays hearted.
        if in_rotation:
            conn.execute("UPDATE exercises SET in_rotation=1, hearted=1 WHERE id=?", (row["id"],))
        else:
            conn.execute("UPDATE exercises SET in_rotation=0 WHERE id=?", (row["id"],))
        cur = conn.execute("SELECT in_rotation, hearted FROM exercises WHERE id=?",
                           (row["id"],)).fetchone()
    return {"exercise_id": row["id"], "name": row["name"],
            "in_rotation": bool(cur["in_rotation"]), "hearted": bool(cur["hearted"])}


@trainer_mcp.tool(annotations=WRITE_IDEMPOTENT)
def set_hearted(name: Optional[str] = None, exercise_id: Optional[int] = None,
                hearted: bool = True) -> dict:
    """Add an exercise to (or remove it from) the user's HEARTED superset — their bench of
    favorite movements. This is the wider pool the small ROTATION (see set_rotation) is
    drawn from: the user keeps the rotation to ~10-14 they actively train, and every few
    months swaps some out for others from this hearted bench, so it's worth hearting any
    movement they like even when it's not currently programmed. Target by `exercise_id` or
    `name` (case-insensitive); an unknown name is an error here (add it via the library
    first). Pass hearted=False to un-heart it — which also removes it from the rotation,
    since a rotation lift is always hearted (rotation ⊆ hearted). Logging a movement hearts
    it automatically. Reach for this when the user says they like / want to remember a
    movement without committing it to the active rotation yet."""
    hearted = int(bool(hearted))
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
        # rotation ⊆ hearted: un-hearting must drop it from the rotation too.
        if hearted:
            conn.execute("UPDATE exercises SET hearted=1 WHERE id=?", (row["id"],))
        else:
            conn.execute("UPDATE exercises SET hearted=0, in_rotation=0 WHERE id=?", (row["id"],))
        cur = conn.execute("SELECT in_rotation, hearted FROM exercises WHERE id=?",
                           (row["id"],)).fetchone()
    return {"exercise_id": row["id"], "name": row["name"],
            "in_rotation": bool(cur["in_rotation"]), "hearted": bool(cur["hearted"])}


@trainer_mcp.tool(annotations=READ_ONLY)
def find_exercises(name: Optional[str] = None, exercise_id: Optional[int] = None,
                   muscle: Optional[str] = None, equipment: Optional[str] = None,
                   category: Optional[str] = None, rotation_only: bool = False,
                   hearted_only: bool = False,
                   similar_to: Optional[str] = None) -> dict:
    """Read the exercise catalog — one full record, a filtered list, or swap peers.

    ONE record — pass `name` or `exercise_id` to get a movement in full: muscle emphasis
    tiers (primary/secondary/tertiary), `aka` (common alternative names it's known by),
    `level` (difficulty), `mechanic` (compound vs isolation), `force`, in_rotation, hearted,
    technique notes, common mistakes, cautions, and any video/image links, so you can
    coach proper form. `name` is resolved against the canonical name AND its AKAs (so
    "RDL" finds Romanian Deadlift); an unresolved `name` returns `candidates` (the closest
    real entries) — pick from those, don't guess.

    A LIST — narrow the registry with `muscle` (matches any emphasis tier), an `equipment`
    fragment, `category`, `rotation_only=True`, or `hearted_only=True`. Rows are compact
    (name, category, equipment, muscles, `level`, `mechanic`, in_rotation, hearted) —
    `level`/`mechanic` let you weigh difficulty and pick compounds before isolation. The
    full catalog is large (~870), so when picking exercises for a session prefer
    rotation_only=True — the small set the user actively trains from. Use hearted_only=True
    to see the wider favorites SUPERSET the rotation is drawn from — the bench to pull from
    when the user is swapping out their rotation.

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
                    "aka": _aliases_for(conn, r["id"]),
                    "force": r["force"], "level": r["level"], "mechanic": r["mechanic"],
                    "in_rotation": bool(r["in_rotation"]), "hearted": bool(r["hearted"]),
                    "technique_notes": r["technique_notes"],
                    "common_mistakes": r["common_mistakes"], "cautions": r["cautions"],
                    "video_link": r["video_link"], "image_link": r["image_link"],
                    "image_link_end": r["image_link_end"]}

        def _row(r):
            return {"exercise_id": r["id"], "name": r["name"], "category": r["category"],
                    "equipment": r["equipment"], "muscles": _muscles_for(conn, r["id"]),
                    "level": r["level"], "mechanic": r["mechanic"],
                    "in_rotation": bool(r["in_rotation"]), "hearted": bool(r["hearted"])}

        # Swap peers: shares a primary muscle, ranked by same mechanic / overlap / rotation.
        if similar_to is not None:
            target = _resolve_exercise(conn, similar_to)
            if not target:
                return {"error": f"no exercise matching {similar_to!r}",
                        "candidates": _match_exercises(conn, similar_to)}
            tp = set(_muscles_for(conn, target["id"])["primary"])
            peers = []
            for r in conn.execute("SELECT * FROM exercises WHERE archived=0 ORDER BY name").fetchall():
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

        rows = conn.execute("SELECT * FROM exercises WHERE archived=0 ORDER BY name").fetchall()
        out = []
        for r in rows:
            if rotation_only and not r["in_rotation"]:
                continue
            if hearted_only and not r["hearted"]:
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

@trainer_mcp.tool(annotations=WRITE)
def log_workout(exercises: list[LoggedExercise], workout_date: Optional[str] = None,
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

    A typical item is {"name": "Leg Press", "sets": [{"weight_lbs": 180, "reps": 10,
    "rpe": 7}, {"weight_lbs": 180, "reps": 8, "rpe": 9.5, "note": "a grind"}]} — the
    full field list is in the schema.
    Names resolve against the CLOSED catalog (fuzzily, so a near-spelling lands on the
    right lift). The model never invents an exercise: a name that doesn't match an
    existing one is SKIPPED and returned under `unmatched` with its closest `candidates`
    — re-log it under one of those, or have the user add the movement on the library page
    (the only way the catalog grows). The matched exercises still log (and get hearted onto
    the favorites bench — but are NOT added to the rotation, which grows only on an explicit
    set_rotation request), so capture isn't lost. weight_lbs follows the SIGNED
    added/removed-load convention (see this server's instructions) and is null for cardio;
    rpe is 1-10 perceived exertion (10 = couldn't do another rep), which is how you judge
    whether to add weight next time. Returns the logged exercises (with the catalog's
    canonical names) and any `unmatched`.

    CARDIO (running, walking, rowing, cycling): log it as an exercise too — pick the
    cardio movement from the catalog (those rows carry no muscles, so they stay out of
    muscle recency and are summarized as cardio instead) and use a set per bout with
    `duration_seconds` and/or `distance_miles` instead of
    weight/reps. A 30-minute, 3.2-mile run is one set
    {"duration_seconds": 1800, "distance_miles": 3.2, "rpe": 6}. weight_lbs/reps stay
    null. Intervals can be one set each. Pass durations in SECONDS (25 min = 1500).

    Args:
        exercises: The exercises performed, each with the sets performed.
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
            # A logged movement is one they actually do → it joins the hearted superset (the
            # bench of favorites). It does NOT auto-join the rotation: that small pool is
            # hand-curated so the user can control progression, and only grows on an explicit
            # set_rotation request or via the website. hearted is left untouched if already 0→1.
            conn.execute("UPDATE exercises SET hearted=1 WHERE id=?", (eid,))
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


@trainer_mcp.tool(annotations=READ_ONLY)
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


@trainer_mcp.tool(annotations=READ_ONLY)
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


@trainer_mcp.tool(annotations=WRITE_IDEMPOTENT)
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


@trainer_mcp.tool(annotations=WRITE_IDEMPOTENT)
def update_set(set_id: int, weight_lbs: Optional[float] = None,
               reps: Optional[int] = None, rpe: Optional[float] = None,
               duration_seconds: Optional[int] = None,
               distance_miles: Optional[float] = None,
               target_weight_lbs: Optional[float] = None,
               target_reps: Optional[int] = None,
               target_rpe: Optional[float] = None,
               note: Optional[str] = None) -> dict:
    """Correct a single set. Only non-null args are written, so this can't blank a
    field back to NULL (e.g. clear a weight to mark bodyweight) — delete the set with
    delete_record(kind="set") and re-log it for that. Find the `set_id` with
    get_exercise_history (logged sets) or get_workout_plan (the active plan). `rpe` is
    1-10. `weight_lbs` is SIGNED added/removed load (negative = assisted, 0 = bodyweight,
    positive = added). `duration_seconds`/`distance_miles` are the cardio fields (run/walk/row).
    `target_weight_lbs`/`target_reps`/`target_rpe` retarget a still-pending planned set
    (e.g. bump the planned weight, or the expected difficulty the user's Easy/Med/Hard
    buttons prefill from) without completing it — to actually log a planned set as done,
    use complete_set."""
    if reason := _bad_set({"weight_lbs": weight_lbs, "reps": reps, "rpe": rpe,
                           "duration_seconds": duration_seconds,
                           "distance_miles": distance_miles}):
        return {"error": reason}
    if reason := _bad_set({"weight_lbs": target_weight_lbs, "reps": target_reps,
                           "rpe": target_rpe}):
        return {"error": reason}
    fields = {"weight_lbs": weight_lbs, "reps": reps, "rpe": rpe,
              "duration_seconds": duration_seconds,
              "distance_miles": distance_miles,
              "target_weight_lbs": target_weight_lbs, "target_reps": target_reps,
              "target_rpe": target_rpe,
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


def _expand_planned_sets(ex: PlannedExercise) -> list[dict]:
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
                 "target_reps": ex.get("target_reps"),
                 "target_rpe": ex.get("target_rpe")} for _ in range(int(n))]
    return []


def _bad_planned(exercises: list[PlannedExercise]) -> Optional[dict]:
    """Validate the target numbers on every planned set, else None."""
    for ex in exercises:
        for s in _expand_planned_sets(ex):
            if reason := _bad_set({"weight_lbs": s.get("target_weight_lbs"),
                                   "reps": s.get("target_reps"),
                                   "rpe": s.get("target_rpe")}):
                return {"error": f"{ex.get('name','?')}: {reason}"}
    return None


def _insert_planned(conn: sqlite3.Connection, wid: int,
                    exercises: list[PlannedExercise]) -> tuple[list[dict], list[dict]]:
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
                   target_weight_lbs, target_reps, target_rpe, status, note)
                   VALUES (?,?,?,?,?,?, 'pending', ?)""",
                (wid, eid, i, s.get("target_weight_lbs"), s.get("target_reps"),
                 s.get("target_rpe"), s.get("note")),
            )
        results.append({"exercise_id": eid, "name": row["name"],
                        "planned_sets": len(planned)})
    return results, unmatched


def _plan_payload(conn: sqlite3.Connection, wid: int,
                  unmatched: Optional[list[dict]] = None) -> dict:
    """The plan for one workout: exercises ordered by `ex_position` (the user-set order,
    via reorder_plan or the /trainer reorder UX) and otherwise by insertion order, each
    with its sets (target + actual + status), plus a done/total progress count (skipped
    sets are excluded from the total). Used by every plan tool's return and by the web UI.

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
                  s.target_weight_lbs, s.target_reps, s.target_rpe,
                  s.weight_lbs, s.reps, s.rpe,
                  s.duration_seconds, s.distance_miles, s.ex_position, s.note
           FROM sets s JOIN exercises e ON e.id = s.exercise_id
           WHERE s.workout_id=? ORDER BY s.id""",
        (wid,),
    ).fetchall()
    exercises, by_eid, done, total = [], {}, 0, 0
    # Track each exercise's slot key: its ex_position if set, else where it was first
    # inserted — so a reordered plan honors the user's order and newly-added exercises
    # (ex_position NULL) fall in after the positioned ones, in insertion order.
    sort_key = {}
    for seen, r in enumerate(rows):
        ex = by_eid.get(r["exercise_id"])
        if ex is None:
            ex = {"exercise_id": r["exercise_id"], "name": r["name"], "sets": []}
            by_eid[r["exercise_id"]] = ex
            exercises.append(ex)
            pos = r["ex_position"]
            sort_key[r["exercise_id"]] = (0, pos) if pos is not None else (1, seen)
        ex["sets"].append({
            "set_id": r["id"], "set_index": r["set_index"], "status": r["status"],
            "target_weight_lbs": r["target_weight_lbs"], "target_reps": r["target_reps"],
            "target_rpe": r["target_rpe"],
            "weight_lbs": r["weight_lbs"], "reps": r["reps"], "rpe": r["rpe"],
            "duration_seconds": r["duration_seconds"], "distance_miles": r["distance_miles"],
            "note": r["note"],
        })
        if r["status"] == "done":
            done += 1
            total += 1
        elif r["status"] == "pending":
            total += 1
    exercises.sort(key=lambda ex: sort_key[ex["exercise_id"]])
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


def discard_plan(workout_id: Optional[int] = None) -> dict:
    """Delete the active workout plan outright — the session row and every set on it
    (planned or already logged), via the sets table's ON DELETE CASCADE. Backs the
    /trainer card's plan-level "..." menu (Delete plan): a routine built by mistake (or
    one the user just doesn't want) leaves no trace. Unlike finish_workout this keeps
    nothing and writes no history. The model never needs it (it rebuilds via
    start_workout_plan), so it stays a plain helper, not an MCP tool. Returns the
    empty-plan state."""
    with db() as conn:
        w = (conn.execute("SELECT * FROM workouts WHERE id=?", (workout_id,)).fetchone()
             if workout_id is not None else _active_workout(conn))
        if not w:
            return {"error": "no active workout plan"}
        conn.execute("DELETE FROM workouts WHERE id=?", (w["id"],))
        return {"active": False, "discarded": True, "workout_id": w["id"]}


def reorder_plan_exercises(order: list[int], workout_id: Optional[int] = None) -> dict:
    """Set the order of exercises in the active plan from a list of exercise_ids. Each
    exercise's sets get an `ex_position` matching its slot in `order`; exercises not named
    keep ex_position NULL and fall in after (in insertion order). Backs the /trainer page's
    "reorder" UX (the ↑/↓ arrows) and the reorder_plan tool. Returns the updated plan."""
    with db() as conn:
        w = (conn.execute("SELECT * FROM workouts WHERE id=?", (workout_id,)).fetchone()
             if workout_id is not None else _active_workout(conn))
        if not w:
            return {"error": "no active workout plan"}
        for pos, eid in enumerate(order):
            conn.execute("UPDATE sets SET ex_position=? WHERE workout_id=? AND exercise_id=?",
                         (pos, w["id"], int(eid)))
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


@trainer_mcp.tool(annotations=DESTRUCTIVE)
def start_workout_plan(exercises: list[PlannedExercise], focus: Optional[str] = None,
                       notes: Optional[str] = None, replace: bool = False) -> dict:
    """Lay out the next routine as a plan the user works through — usually today's, but
    if they asked to plan TOMORROW's session, decide it from a get_fitness_briefing whose
    `as_of` is tomorrow (so recovery is counted as of that day). Call this once you've
    decided the session from the briefing (+ get_exercise_history for the lifts you're
    choosing weights for). Each exercise becomes a row of PENDING sets with targets; the
    user completes them with complete_set as they go. The plan stays undated until
    finish_workout stamps the day it's actually completed, so planning ahead needs no date.

    Build a full session at the volume this server's instructions call for, across the
    muscle groups that are due.

    Each item in `exercises` is either explicit — {"name": "Bench Press", "sets":
    [{"target_weight_lbs": 100, "target_reps": 10, "target_rpe": 7}, …]} — or uses the
    shorthand for N identical sets: {"name": "Curls", "set_count": 3, "target_reps": 12,
    "target_weight_lbs": 25, "target_rpe": 8}.

    `target_rpe` (1-10) is the difficulty you're programming for each set — set it from
    your judgment of how hard that set should be, and ramp it across the exercise's sets
    when you intend a build-up. It prefills the Easy/Med/Hard buttons the user taps on the
    /trainer card (Easy ≈ 5, Med ≈ 7, Hard ≈ 9), so they confirm a feel rather than typing
    a number; they can still change it. Optional — omit it and the buttons start blank.
    Pick the movements from the library with `find_exercises(muscle=..., rotation_only=True)` —
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
            # A plan in progress has NO date yet — it's just today's intended routine,
            # not a thing that happened. The date is stamped only at finish_workout, when
            # the session is actually done (so a plan started late and finished after
            # midnight records the day it was completed). '' is the not-yet-done sentinel
            # (the column is NOT NULL); active workouts are excluded from all history /
            # briefing aggregates by status, so the empty date never leaks anywhere.
            wid = conn.execute(
                """INSERT INTO workouts(workout_date, focus, notes, status, created_at)
                   VALUES ('',?,?, 'active', ?)""",
                (focus, notes, now()),
            ).lastrowid
        results, unmatched = _insert_planned(conn, wid, exercises)
        return _plan_payload(conn, wid, unmatched=unmatched)


@trainer_mcp.tool(annotations=READ_ONLY)
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


@trainer_mcp.tool(annotations=WRITE_IDEMPOTENT)
def complete_set(set_id: int, weight_lbs: Optional[float] = None,
                 reps: Optional[int] = None, rpe: Optional[float] = None,
                 note: Optional[str] = None) -> dict:
    """Mark one planned set done, recording what was actually lifted. Omitted
    `weight_lbs`/`reps` default to the set's targets, so "did it as planned" needs only
    the set_id (add `rpe` 1-10 for how hard it felt — that's how you judge the next
    weight). Find `set_id` in get_workout_plan. `weight_lbs` is SIGNED: negative =
    assistance taken off (assisted pull-up at -20), 0 = bodyweight, positive = added
    load. Flips the set to 'done' and returns the updated plan. (To CORRECT an
    already-logged set, use update_set instead.)"""
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
        # Completing a set means the movement was actually trained → keep it in the hearted
        # superset. It does NOT auto-join the rotation (that pool stays hand-curated — grown
        # only by an explicit set_rotation request or via the website).
        conn.execute("UPDATE exercises SET hearted=1 WHERE id=?", (r["exercise_id"],))
        return _plan_payload(conn, r["workout_id"])


@trainer_mcp.tool(annotations=DESTRUCTIVE)
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
    `find_exercises(similar_to=from_exercise)` to see the catalog's closest peers (shared
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
                             "find_exercises(similar_to=...) or add it on the library page",
                    "candidates": _match_exercises(conn, to_exercise)}
        pend = conn.execute(
            """SELECT target_weight_lbs, target_reps, target_rpe FROM sets
               WHERE workout_id=? AND exercise_id=? AND status='pending'
               ORDER BY set_index""",
            (w["id"], frm["id"]),
        ).fetchall()
        if not pend:
            return {"error": f"no pending {from_exercise} sets to swap in this plan"}
        sub_sets = sets if sets else [
            {"target_weight_lbs": p["target_weight_lbs"], "target_reps": p["target_reps"],
             "target_rpe": p["target_rpe"]}
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


@trainer_mcp.tool(annotations=WRITE)
def add_to_plan(exercises: list[PlannedExercise], workout_id: Optional[int] = None) -> dict:
    """Append exercises (or extra sets of an exercise already present) to the active
    plan mid-session — e.g. "add some calf raises" or "give me one more drop set". Same
    `exercises` shape as start_workout_plan; pick the movements from the library with
    `find_exercises(...)`. Errors if no plan is active. Names resolve against the closed
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


@trainer_mcp.tool(annotations=WRITE_IDEMPOTENT)
def reorder_plan(order: list[str], workout_id: Optional[int] = None) -> dict:
    """Reorder the exercises in the active plan. `order` is the exercise names in the
    sequence you want them done, e.g. ["Squat", "Bench Press", "Curls"] — names resolve
    against the plan's exercises fuzzily (same matching as everywhere). Any plan exercise
    you leave out keeps its place after the ones you listed. Use this when the user asks to
    move a lift earlier/later or to lay the session out in a particular order (warm-up
    compounds first, accessories last). Returns the updated plan in the new order."""
    with db() as conn:
        w = (conn.execute("SELECT * FROM workouts WHERE id=?", (workout_id,)).fetchone()
             if workout_id is not None else _active_workout(conn))
        if not w:
            return {"error": "no active workout plan"}
        ids, seen = [], set()
        for name in order:
            row = _resolve_exercise(conn, (name or "").strip())
            if row and row["id"] not in seen:
                ids.append(row["id"])
                seen.add(row["id"])
    return reorder_plan_exercises(ids, workout_id=w["id"])


@trainer_mcp.tool(annotations=WRITE)
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
        # Stamp the date now — this is when the session was actually done. A plan carries
        # no date while in progress (the '' sentinel), so finishing is what dates it.
        wd = w["workout_date"] or today()
        if not w["workout_date"]:
            fields["workout_date"] = wd
        if feeling is not None:
            fields["feeling"] = feeling
        if notes is not None:
            fields["notes"] = notes
        cols = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE workouts SET {cols} WHERE id=?", (*fields.values(), w["id"]))
        return {"finished": True, "workout_id": w["id"], "workout_date": wd,
                "done_sets": done, "skipped_sets": skipped, "active": False}


# --------------------------------------------------------------------------- #
# Trainer: bodyweight
# --------------------------------------------------------------------------- #

@trainer_mcp.tool(annotations=WRITE)
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


@trainer_mcp.tool(annotations=READ_ONLY)
def get_fitness_briefing(recent_workouts: int = 5, as_of: Optional[str] = None) -> dict:
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
    recommendation itself is yours to make from this data.

    `as_of` is the day you're planning FOR (YYYY-MM-DD), defaulting to today. When the
    user wants TOMORROW's session, pass tomorrow's date (today + 1; today is in `now`):
    `days_since` then counts recovery as of that day — a muscle trained today reads 0 in a
    today briefing but 1 in a tomorrow one — so what's "due" already reflects the extra
    rest. Plan as normal off the re-anchored numbers; the plan itself stays undated until
    finish_workout stamps the day it's actually done."""
    ref = as_of or today()
    if err := _bad_date(as_of, "as_of"):
        return err
    week_ago = date.fromordinal(date.fromisoformat(ref).toordinal() - 6).isoformat()
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
            "SELECT id, name, category, equipment FROM exercises "
            "WHERE in_rotation=1 AND archived=0 ORDER BY name"
        ).fetchall()
        rot_primary: dict[int, list[str]] = {}
        for pr in conn.execute(
            "SELECT em.exercise_id, em.muscle FROM exercise_muscles em "
            "JOIN exercises e ON e.id = em.exercise_id "
            "WHERE e.in_rotation=1 AND e.archived=0 AND em.role='primary' ORDER BY em.muscle"
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
          "days_since": _days_since(r["last_date"], ref), "sets_last_7d": r["sets_7d"]}
         for r in mrows),
        key=lambda m: (m["days_since"] is None, -(m["days_since"] or 0)),
    )
    cardio = sorted(
        ({"exercise": r["name"], "last_done": r["last_date"],
          "days_since": _days_since(r["last_date"], ref),
          "minutes_last_7d": round((r["dur_7d"] or 0) / 60, 1),
          "miles_last_7d": round(r["dist_7d"] or 0, 2)}
         for r in crows),
        key=lambda c: (c["days_since"] is None, -(c["days_since"] or 0)),
    )
    return {"now": current_clock(), "profile": profile,
            "muscle_recency": recency, "cardio_recency": cardio,
            "bodyweight": bodyweight, "recent_workouts": recent_out,
            "rotation": rotation}


@trainer_mcp.tool(annotations=WRITE_IDEMPOTENT)
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


@trainer_mcp.tool(name="delete_record", annotations=DESTRUCTIVE)
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
