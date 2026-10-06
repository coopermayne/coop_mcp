"""
Journal MCP server.

This module defines two FastMCP instances sharing one SQLite DB: `trainer_mcp` (the
training tools plus the water/protein log — the one MCP server, served by
webapp/combined.py at /trainer/mcp or its own host, and over stdio by a bare
`server.py` launch), and `mcp` (the journal's people/entry tools — NOT served; an
in-process tool registry driven by the web app's own chat, webapp/chat.py).

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

import io
import json
import os
import re
import sqlite3
import zipfile
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional
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
    """One exercise of a completed session: its name plus the sets performed. A name
    the user has never done before is created on the fly — give `muscles` (primary,
    canonical labels) and optionally `secondary_muscles`, or category "cardio". `new`
    confirms a name that's close to an existing one really is a different movement.
    `mechanic` (compound | isolation) is stored on a newly created lift; see
    add_exercise."""
    name: str
    sets: NotRequired[list[LoggedSet]]
    muscles: NotRequired[list[str]]
    secondary_muscles: NotRequired[list[str]]
    category: NotRequired[Literal["strength", "cardio"]]
    mechanic: NotRequired[Literal["compound", "isolation"]]
    new: NotRequired[bool]


class PlannedSet(TypedDict):
    """One PROGRAMMED set — the targets the user works toward, actuals filled in later
    by complete_set."""
    target_weight_lbs: NotRequired[Optional[float]]
    target_reps: NotRequired[Optional[int]]
    target_rpe: NotRequired[Optional[float]]
    note: NotRequired[Optional[str]]


class PlannedExercise(TypedDict):
    """One exercise in a plan. Either give an explicit `sets` list, or use the
    shorthand `set_count` (+ the target_* fields) to expand N identical sets. A
    movement not on file yet is created on the fly from `muscles`/`secondary_muscles`
    (or category "cardio") — see LoggedExercise."""
    name: str
    sets: NotRequired[list[PlannedSet]]
    set_count: NotRequired[int]
    target_weight_lbs: NotRequired[Optional[float]]
    target_reps: NotRequired[Optional[int]]
    target_rpe: NotRequired[Optional[float]]
    muscles: NotRequired[list[str]]
    secondary_muscles: NotRequired[list[str]]
    category: NotRequired[Literal["strength", "cardio"]]
    mechanic: NotRequired[Literal["compound", "isolation"]]
    new: NotRequired[bool]


class SetResult(TypedDict):
    """One planned set reported done, for complete_sets. Omitted weight_lbs/reps
    default to the set's targets; weight_lbs is SIGNED (negative = assisted)."""
    set_id: int
    weight_lbs: NotRequired[Optional[float]]
    reps: NotRequired[Optional[int]]
    rpe: NotRequired[Optional[float]]
    note: NotRequired[Optional[str]]


class ScaleReading(TypedDict):
    """One row of the scale app's export, for import_weigh_ins. `stamp` is the
    export's date-and-time cell copied VERBATIM (it's the reading's identity — see
    import_weigh_ins); give the weight in pounds or kilograms, whichever the export
    carries."""
    stamp: str
    weight_lbs: NotRequired[Optional[float]]
    weight_kg: NotRequired[Optional[float]]


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
    valid Google login alone is not enough; it must be *your* account.

    Enforced on `on_message`, which sits above EVERY MCP message, not just
    on_call_tool. Guarding only tool calls left the door open on the metadata
    surface: an authenticated non-allowlisted account couldn't read a single row,
    but it could still finish the handshake and enumerate tools/list — all 38 tool
    names, their full docstrings, and the server instructions. No journal data, but
    the shape of the whole journal. on_message closes tools/list, resources, prompts
    and anything a future protocol version adds, in one place.

    on_call_tool is KEPT below it deliberately: it's the check that's been running
    against the live connectors, and a tool call is the one path where a miss would
    expose actual entries. Two layers, cheap ones — both are a set lookup.
    """

    def _check(self) -> None:
        if not ALLOWED_EMAILS:
            return  # authless (local/dev): no token to check against
        tok = get_access_token()
        email = (tok.claims or {}).get("email", "").lower() if tok else ""
        if email not in ALLOWED_EMAILS:
            raise ToolError("Not authorized for this journal.")

    async def on_message(self, context, call_next):
        self._check()
        return await call_next(context)

    async def on_call_tool(self, context, call_next):
        self._check()
        return await call_next(context)


# ---------------------------------------------------------------------------
# The journal instance. It is NOT served as an MCP endpoint any more — the user
# writes the journal only in the web app's own chat, which drives these tools
# in-process (webapp/chat.py lifts their schemas with list_tools and calls the
# functions directly). FastMCP is kept as the tool REGISTRY: the decorators give
# the chat its schemas from the docstrings, exactly as they did when this was a
# connector. So: no auth, no middleware, and its instructions are the chat's
# system prompt.
# ---------------------------------------------------------------------------

def _pacific_block(anchor_tool: str) -> str:
    """The Pacific-dates rule, parameterized on the tool that returns `now`."""
    return f"""\
All dates in this log are Pacific (America/Los_Angeles) — the user lives and logs
on Pacific time. {anchor_tool} returns `now` (current Pacific date/time), with
`date`/`yesterday`/`tomorrow` precomputed: use those EXACT strings for
"today"/"yesterday"/"tomorrow" rather than computing or shifting dates yourself, and
resolve any bare day reference against them before defaulting or saving."""


# What the journal panel is not for — training and the water/protein log live on
# the trainer. Say so, rather than leaving the model to discover it by reaching for
# a tool that isn't there: a meal named in passing is part of the entry's story.
_JOURNAL_ONLY_BLOCK = """\
This panel captures the JOURNAL — entries and people — and nothing else. Workouts
and the water/protein log have their own surface and are NOT among your tools here.
So when a meal, a drink or a lift comes up in what the user is telling you, it is
part of the entry: write it into the note like any other detail. Don't offer to log
it, and don't tell the user where it belongs — they know."""


JOURNAL_CHAT_INSTRUCTIONS = f"""\
Single-user life log: a conversational journal — people are resolved to stable
entities, not name strings. The server only stores and matches —
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
    these summaries — get_briefing carries them for everyone recently mentioned — to
    resolve relational references
    like "her parents" or "his brother" to the right people.

Every entry is classified `kind`: "log" (an interaction/event/fact — the default) or
"thought" (a personal reflection). Thoughts stay in the feed and in search but are kept
out of per-person history, so the CRM view stays real interactions. add_journal_entry
tells them apart.

{_pacific_block("get_briefing")}

{_JOURNAL_ONLY_BLOCK}

Start a session with get_briefing: it loads the last two weeks of entries plus the
summaries of everyone mentioned in the last week, so you write in context rather than
from a blank slate. Everyone else comes back in a compact `roster` (no summary) —
still enough to resolve a name; pull their full profile with get_person_history when
they come up.
(Workouts and the water/protein log live on the separate `trainer` MCP server.)"""


mcp = FastMCP("journal", instructions=JOURNAL_CHAT_INSTRUCTIONS)

# The trainer is a SEPARATE MCP server living in the SAME process and sharing this DB,
# so a Claude project connected to it loads ONLY the training tools. It gets its OWN
# auth provider instance (providers are single-resource and must not be shared — see
# _build_auth). When TRAINER_PUBLIC_URL is set, the provider advertises that subdomain,
# and webapp/combined.py routes that host to this server (its own clean root OAuth);
# otherwise it falls back to /trainer/mcp on the journal origin (fine for authless/local).
_trainer_auth = _build_auth(os.environ.get("TRAINER_PUBLIC_URL"))
trainer_mcp = FastMCP("trainer", auth=_trainer_auth, instructions="""\
Personal-trainer log: a workout log (sessions + per-set weight/reps/rpe, plus
duration/distance for cardio like running and walking), the user's own exercises
(active and archived, with target muscles), their weigh-ins, and a small daily
water/protein log. The server only
stores and computes deterministic aggregates (per-muscle recency/volume, cardio
minutes/miles, personal records) — all coaching judgment (next weight, what to
program, what to rest, how to cue form) is yours.

THIS CONVERSATION IS THE WHOLE INTERFACE. There is no app screen behind it: the user
plans, trains, reports and reviews progress entirely by talking to you, often on a
phone between sets. So:
  - Show a plan as a compact table — exercise, sets × reps @ weight, target RPE — in
    the order it'll be done. Mid-session, keep replies short: confirm what was
    logged, then name the next set ("Next: Incline DB Press, 3×10 @ 50").
  - set_ids, workout_ids and exercise_ids are yours to track, never the user's: they
    report "did all three at 135, last one was hard", and you map that onto the
    pending sets from the last plan return.
  - Progress questions ("how's my bench going?", "what did I do last week?") are
    answered from get_exercise_history / get_personal_records / the briefing, as a
    short table or a sentence with the trend — the numbers, not vibes.

All dates here are Pacific (America/Los_Angeles). get_fitness_briefing returns `now`
(current Pacific date/time) with `date`/`yesterday`/`tomorrow` precomputed: use those
EXACT strings for "today"/"yesterday"/"tomorrow" rather than computing or shifting
dates yourself, and resolve any bare day reference against them before defaulting or
saving.

Start a training conversation with get_fitness_briefing to load the profile (see THE
USER'S PROFILE below), per-muscle recency, recent sessions (with their notes), the
week's already-planned sessions, and the latest bodyweight before recommending work.
Recent-session notes are durable context — read them so a "left shoulder twinge" last
time shapes what you program next. When the user tells you something like that during
or after a session, put it in the session's notes (finish_workout / update_workout) so
the next conversation sees it.

Two ways to record training:
  - PLAN-AS-YOU-LIFT (the live routine): start_workout_plan lays out a session as
    PENDING sets with target weights/reps/RPE; as the user reports sets, record them
    with complete_sets — one call per report, however many sets it covers (omitted
    numbers default to the targets). Ask how a set felt if they don't say: RPE is what
    the next weight is judged from. swap_exercise substitutes a busy/broken movement
    with its CLOSEST like-for-like peer — same movement pattern and role
    (compound→compound, isolation→isolation), not just any exercise sharing a muscle —
    add_to_plan tacks on more, remove_from_plan drops one, update_set retargets a
    pending set or corrects a logged one, reorder_plan resequences, and finish_workout
    closes it out (leftover pending sets are skipped). get_workout_plan returns the
    current state. Design the routine yourself from the briefing, choosing movements
    from the user's active `exercises` — progress what was easy (low RPE), hold/deload what was
    hard, and keep staple lifts so the tracked data stays comparable. How BIG a session
    should be, how much it should vary from the last one, and how the week's sessions
    divide their exercises up are the USER'S call, read from their profile.
  - POST-HOC (log what already happened): log_workout records a finished session (or
    appends to one) in a single call — use it when the user just tells you what they
    did rather than working a plan live. If what they did matches a plan that's still
    open, complete that plan's sets instead, so the plan doesn't linger as "upcoming".
Both write paths return `new_prs` when a set beat the user's previous best on that
movement — say so; it's earned. Only completed ('done') sets count toward recency,
history, and PRs; a planned-but-not-yet-done set doesn't, so the briefing stays honest
mid-session.

PLANNING AHEAD: several sessions can be planned at once — a whole week, or the rest of
one after today's is done — as ONE PLAN PER DAY, each carrying its `planned_date`. Lay a
week out with a start_workout_plan call per day, deciding each from a get_fitness_briefing
whose `as_of` is that day. Two things to hold onto while you do it. `muscle_recency` is
COMPLETED work only, so the days you just programmed aren't in it — read the briefing's
`upcoming` so Wednesday's chest work counts against Friday's. And a plan is INTENT, not
history: `planned_date` is the day it's meant for, while the day it's recorded under is
stamped by finish_workout, so a session done a day late lands on the day it was actually
done. When the user shows up to train, the briefing's `upcoming` tells you which plan is
today's; pull it with get_workout_plan(workout_id=…) and walk them through it. To move a
session, update_workout(planned_date=…); to scrap one, delete_record(kind="workout").

A session's `focus` (whichever tool writes it) is a SHORT kind-of-day label — a couple
of words like "Pull + Legs", "Push", "Upper", "Cardio". Never pack the lifts or muscle
list into it: it's the title of a session whose exercise rows already say what was done,
and on a week of plans it's what tells the days apart.

Weight on a lift is SIGNED added/removed load, not total bodyweight: 0 (or null) = plain
bodyweight, positive = weight added (a +25 weighted pull-up), and NEGATIVE = assistance,
the load a band or machine took OFF (an assisted pull-up at -20). This lets one movement
track a full assisted→bodyweight→weighted arc on a single number line, -20 → 0 → +20 as
the user gets stronger. Log negatives as given, program the next target along that line
(less assistance, then added load), and read movement toward 0 and beyond as progress.
Don't lean on estimated-1RM for assisted (negative-weight) sets — it isn't physically
meaningful below bodyweight; judge those by assistance level and RPE instead.

EXERCISES: there is no library. The user's exercises are the movements they DO —
`exercises` in the briefing (ACTIVE: the only pool you program from) — plus an ARCHIVE
of what they've done and stopped, each with a `note` that usually says why
(list_exercises). Nothing is pre-loaded and no technique data is stored: form cues,
common mistakes and cautions come from YOUR knowledge, in conversation, when they ask
(or when a new movement deserves a word), tuned to the profile's injuries.
  - The active set is theirs to curate. Don't slip new movements into a plan on your
    own: if a session calls for something they don't do, name the gap and suggest it —
    and before suggesting, read the archive, so you don't pitch a lift they already
    dropped for shoulder pain (or so you can say "you used to do X — want it back?").
  - A NEW movement they've agreed to is created on the fly: give it in the plan/log
    with its `muscles` (primary) and `secondary_muscles`, in these canonical labels:
    abdominals, abductors, adductors, biceps, calves, chest, forearms, glutes,
    hamstrings, lats, lower back, middle back, neck, quadriceps, shoulders, traps,
    triceps (cardio: category "cardio", no muscles), and its `mechanic` (compound |
    isolation; the app sizes rest between sets by it). Use the name the user uses.
    add_exercise does the same thing ahead of time. A name close to an existing one
    comes back `unmatched` with candidates — it's usually the same lift; use the
    existing name.
  - "I'm done with X" / "drop X" → archive_exercise with their reason as the `note`.
    "Bring back X" → archive_exercise(archived=False). Logging an archived lift brings
    it back on its own (it's being done again). Nothing is ever deleted; history and
    PRs survive archiving.
  - update_exercise fixes a name, the muscles, a note, or the mechanic. An active lift
    listed without a `mechanic` hasn't been classified: set it when you see one.

WEIGH-INS come from a connected scale. When the user attaches the scale app's export,
read it and pass every row to import_weigh_ins (it skips what's already on file). A
number they merely mention is not a reading — don't log it; the briefing's `bodyweight`
is the trend to coach from.

WATER AND PROTEIN are the one intake log kept — nothing else about food is tracked,
so never estimate or bring up calories or other macros. When the user mentions
drinking water or eating something with protein, call log_intake (water in fl oz,
protein in grams — estimate protein from the food when they don't give a number, and
say what you assumed). Day totals are SUMMED by the server, so answer "how's my water"
from log_intake's `day_totals` or get_fitness_briefing's `intake_today`, never from a
tally you've kept in conversation; read them against `targets`. Change a target only
when the user asks (set_intake_targets). Fix an item with update_intake, remove one
with delete_record(kind="intake"), look back over days with get_intake.

THE USER'S PROFILE is the ONE place everything about them lives — this text holds the
rules of the system, the profile holds the person. It comes back with every
get_fitness_briefing and is written with update_profile. Its keys are free-form, but
keep these: `goals`, `split` (how the week divides up, and which days they train),
`session` (how long / how big a session should be), `injuries` (and lifts to avoid),
and `coaching` — their own standing instructions about HOW you coach (tone, what to
push, what to nudge about, what to leave alone). Read all of it as instruction, not
background: it outranks any habit of yours. What it cannot do is loosen the rules
above (the active set stays theirs to curate; a mentioned weight isn't a weigh-in).
  - SETUP: if any of goals / split / session / coaching is missing, ask about the
    missing ones before you plan anything — a few plain questions, not a form — then
    save the answers and show them what you saved. Until they answer, program
    conservatively and say you're working without their preferences.
  - KEEPING IT CURRENT: a durable fact they state ("my knee's fine now", "I can only
    do three days this month") goes into the right key, told back in a line. Their
    `coaching` text changes only when they ask you to change how you coach, in so many
    words — never from a passing remark — and you write the whole new text and show it.
""")
if _trainer_auth is not None:
    trainer_mcp.add_middleware(AllowlistMiddleware())

@trainer_mcp.custom_route("/health", methods=["GET"])
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
-- LEGACY drinking tracker — one row per day, dormant. Alcohol is an intake item
-- now (standard_drinks on intake_items). Kept, not dropped: it's the fold-in
-- migration's source and the only copy of the per-day `kind` ("beer, wine"),
-- which an item row has no column for. NOTHING reads or writes it — the
-- log/summary/update functions that did are deleted; a stale reader here is
-- what made the /graphs drinks series go quiet after the fold.
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
-- Intake log — ONE ROW PER THING CONSUMED. Only water_oz and protein_g are
-- live now (the trainer's log_intake); the other nutrient columns are from the
-- full food-tracker days and are DORMANT — kept with their history, never read.
--
-- A day's totals are DERIVED (SUM ... GROUP BY food_date), never stored: a
-- stored total can drift from the items it claims to summarize, and correcting
-- one item would mean re-deriving it by hand. Correcting is instead a plain
-- UPDATE/DELETE on the item's id — no arithmetic anywhere.
--
-- Every nutrient column is per-item and optional; NULL means "not logged",
-- which is a different fact from zero.
-- ----------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS intake_items (
    id         INTEGER PRIMARY KEY,
    food_date  TEXT NOT NULL,    -- YYYY-MM-DD (Pacific) it was consumed
    position   INTEGER,          -- order logged within the day (1 = first)
    item       TEXT,             -- "chipotle bowl"; NULL for a bare tap ("+16oz")
    calories   REAL,             -- dormant; NULL = not logged
    protein_g  REAL,
    carbs_g    REAL,
    fat_g      REAL,
    sodium_mg  REAL,
    fiber_g    REAL,
    standard_drinks REAL,        -- dormant (alcohol, in standard drinks)
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
-- Personal trainer. `exercises` holds the user's OWN movements — stable entities
-- (like people), born on the fly the first time one is planned or logged. `archived`
-- splits them into the ACTIVE set (what the trainer programs from) and the archive
-- (what they've stopped, `note` saying why). Muscles are normalized into a child table
-- so "what's rested vs worked" is a plain SQL aggregate, not an LLM guess. The
-- slug/force/level/mechanic/equipment/technique/cautions/image columns and the
-- in_rotation/hearted flags are DORMANT leftovers of the retired pre-loaded library
-- (see _prune_exercise_library); nothing writes the reference columns any more.
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
    workout_date TEXT NOT NULL,       -- YYYY-MM-DD; '' while a plan is still active
    planned_date TEXT,                -- YYYY-MM-DD the plan is FOR; NULL = unscheduled
    focus        TEXT,                -- "Legs", "Arms", "Cardio"
    feeling      TEXT,                -- overall how the session felt
    notes        TEXT,
    status       TEXT NOT NULL DEFAULT 'done',  -- 'active' (planned/in progress) | 'done'
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
    target_rpe        REAL,           -- plan target difficulty (1-10); prefills the /trainer card's RPE buttons
    status      TEXT NOT NULL DEFAULT 'done',  -- 'pending' | 'done' | 'skipped'
    ex_position INTEGER,              -- exercise's slot in the workout (all its sets share it); NULL = insertion order
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_sets_workout ON sets(workout_id);
CREATE INDEX IF NOT EXISTS idx_sets_exercise ON sets(exercise_id);

-- Bodyweight readings — a standalone daily health metric (like drinks), keyed by
-- the day weighed, NOT tied to a workout row. Several readings on a day are allowed
-- (the latest is "the" weight for that day); a day with no row simply wasn't weighed.
--
-- Readings arrive by IMPORT, not by hand: a connected scale writes them to its own
-- app and the user uploads that app's export (see import_bodyweight). `source_key`
-- is the reading's identity IN THAT EXPORT — its full local timestamp — and it's
-- what makes re-uploading an overlapping export a no-op. UNIQUE, but nullable, so
-- the hand-entered rows that predate the scale (all NULL) don't collide: SQLite
-- lets a unique index hold any number of NULLs.
CREATE TABLE IF NOT EXISTS body_weight (
    id          INTEGER PRIMARY KEY,
    weigh_date  TEXT NOT NULL,        -- YYYY-MM-DD (Pacific): the day weighed
    weight_lbs  REAL NOT NULL,
    note        TEXT,
    source_key  TEXT,                 -- e.g. "wyze:2026.08.22 06:39 AM"; NULL = hand-entered
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bodyweight_date ON body_weight(weigh_date);
-- NB: the UNIQUE index on source_key is created in init_db, not here — this script
-- runs BEFORE the migration that adds the column, so an older DB would fail on it.

-- Generic JSON settings (trainer profile: injury, split, goals, preferences).
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- (The notes & collections layer's `collections` / `items` / `items_fts` tables
-- are no longer created: the feature was removed. Existing DBs keep them, and
-- their rows, dormant — nothing reads or writes them.)
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
        bwcols = [r["name"] for r in conn.execute("PRAGMA table_info(body_weight)")]
        if "source_key" not in bwcols:
            # Identity of an imported reading within its source export, so re-uploading
            # an overlapping file inserts only what's new. Rows predating the scale stay
            # NULL, which the unique index permits any number of.
            conn.execute("ALTER TABLE body_weight ADD COLUMN source_key TEXT")
        # Created HERE rather than in SCHEMA because SCHEMA runs before that ALTER.
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS "
                     "idx_bodyweight_source ON body_weight(source_key)")
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
        # The day a PLAN is intended for — several sessions can be planned at once (a
        # week laid out in one conversation), so a plan needs a day of its own. Distinct
        # from workout_date, which stays the day the session was actually COMPLETED and
        # is stamped only by finish_workout. NULL = an unscheduled "next session"; no
        # back-fill, so a plan that predates the column simply reads as unscheduled.
        if "planned_date" not in wcols:
            conn.execute("ALTER TABLE workouts ADD COLUMN planned_date TEXT")
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
            # The FULL column list the legacy table carried, spelled out — NUTRIENTS
            # is now just the two live figures, and this fold must stay lossless.
            legacy = ("calories", "protein_g", "carbs_g", "fat_g", "sodium_mg",
                      "fiber_g", "standard_drinks", "water_oz")
            for r in conn.execute("SELECT * FROM nutrition ORDER BY food_date").fetchall():
                conn.execute(
                    "INSERT INTO intake_items(food_date, position, item, note, "
                    + ", ".join(legacy) + ", created_at) VALUES (?,?,?,?"
                    + ",?" * len(legacy) + ",?)",
                    (r["food_date"], 1, r["summary"], r["notes"],
                     *(r[m] for m in legacy), r["created_at"]),
                )
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES "
                "('nutrition_split_into_items', 'true')"
            )
        # One-time cleanup: early sessions were titled like "Pull + Legs — Deadlift,
        # Lats, Back, Quads, Hamstrings, Biceps" — the lift list restated what the
        # session's own exercise rows already show right under the title, and it
        # overflowed the /workouts card heading. The contract (trainer server
        # instructions) now calls for a SHORT kind-of-day label, so strip the
        # " — ..." tail from existing rows once. Flag-guarded like the fold-ins
        # above, so a future title that legitimately carries an em dash isn't
        # re-stripped on every boot.
        done3 = conn.execute(
            "SELECT value FROM settings WHERE key='workout_focus_shortened'"
        ).fetchone()
        if not done3:
            conn.execute(
                "UPDATE workouts SET focus = substr(focus, 1, instr(focus, ' — ') - 1) "
                "WHERE focus LIKE '% — %'"
            )
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES "
                "('workout_focus_shortened', 'true')"
            )
        # The exercise LIBRARY is gone: the catalog is now just the movements the user
        # does (active) and has done (archived). `note` is the one new column — why a
        # lift was archived, or what they thought of it.
        if "note" not in xcols:
            conn.execute("ALTER TABLE exercises ADD COLUMN note TEXT")
        _prune_exercise_library(conn)


def _prune_exercise_library(conn: sqlite3.Connection) -> None:
    """One-time teardown of the ~870-movement pre-loaded library. Flag-guarded like the
    fold-ins above, because it DELETES: after it, a new exercise is born on the fly and
    must never be pruned by a later boot.

    What survives, and as what: the rotation becomes the ACTIVE set; anything with a
    logged or planned set, or that was hearted, is kept ARCHIVED — the record of what
    the user has tried, which is exactly what the model reads before suggesting
    something new. Everything else (library rows nobody ever touched) is deleted, its
    muscle and AKA rows cascading with it. The reference-data columns (technique,
    cautions, images, force/level/mechanic, equipment, slug) are nulled: tips are the
    model's to give in conversation, not stored data. in_rotation/hearted stay as
    dormant columns, mirrored from `archived` so a legacy reader sees the same truth."""
    if conn.execute(
        "SELECT 1 FROM settings WHERE key='exercise_library_pruned'"
    ).fetchone():
        return
    used = "EXISTS (SELECT 1 FROM sets s WHERE s.exercise_id = exercises.id)"
    conn.execute(f"DELETE FROM exercises WHERE in_rotation=0 AND hearted=0 AND NOT {used}")
    conn.execute("UPDATE exercises SET archived = CASE WHEN in_rotation=1 AND archived=0 "
                 "THEN 0 ELSE 1 END")
    conn.execute(
        """UPDATE exercises SET slug=NULL, force=NULL, level=NULL, mechanic=NULL,
           equipment=NULL, technique_notes=NULL, common_mistakes=NULL, cautions=NULL,
           video_link=NULL, image_link=NULL, image_link_end=NULL,
           in_rotation = 1 - archived, hearted = 1 - archived""")
    conn.execute("INSERT OR REPLACE INTO settings(key, value) "
                 "VALUES ('exercise_library_pruned', 'true')")


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


def pacific_day(ts: Optional[str]) -> str:
    """The Pacific calendar day a stored UTC timestamp (created_at/updated_at)
    falls on — the bridge between the two halves of the rule above.

    Storage stamps are UTC on purpose; every date the app SHOWS is Pacific. The
    two disagree for the seven or eight hours between Pacific 4/5pm and UTC
    midnight, so slicing the first ten characters off a stored timestamp — which
    is what the item views used to do — is an evening-only off-by-one: a note
    saved at 6pm Tuesday renders, and sorts, as Wednesday. Anything unparseable
    falls back to that slice rather than raising; a byline is not worth a 500."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return ts[:10]
    if dt.tzinfo is None:                      # legacy naive rows were UTC too
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(PACIFIC).strftime("%Y-%m-%d")


def _app_url(path: str) -> Optional[str]:
    """Absolute URL of a browser page, for handing back on a WRITE — the capture
    surface (a Claude conversation) and the viewing surface (the web app) are
    different places, so a write that names where the thing now lives closes that
    loop in one tap instead of a context switch. The UI is mounted at /app (see
    webapp/combined.py); PUBLIC_URL is the bare origin. Returns None when
    PUBLIC_URL is unset (stdio/dev) — callers OMIT the key rather than emitting a
    dead one."""
    base = (os.environ.get("PUBLIC_URL") or "").rstrip("/")
    return f"{base}/app{path}" if base else None


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

# How recently an identical entry must have been saved to count as a retry rather than
# a deliberate repeat. Generous enough to cover a connector retrying a timed-out call,
# short enough that genuinely re-saying a terse note ("called mom") days or even minutes
# later still records as its own entry.
RETRY_WINDOW_SECONDS = 120


def _recent_duplicate(conn, entry_date: str, body: str):
    """The id of an identical entry saved moments ago, or None.

    This server is reached over HTTP by a connector, where a response lost to a timeout
    is indistinguishable from a call that never landed — so a retry re-sends the same
    save and, without this, silently writes a SECOND entry: same text, same day, a new
    day_position, and a duplicate set of pending mentions to resolve twice. That's
    corruption you'd only notice much later, with no way to tell which copy was real.

    Matching on (entry_date, body) inside a short window rather than on an idempotency
    key, because the model doesn't mint one and the retried payload is byte-identical
    anyway. `body` is the cleaned text; two different raw_body slices that cleaned to
    the same body on the same day within two minutes is the retry case, not a real pair.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=RETRY_WINDOW_SECONDS)).isoformat()
    row = conn.execute(
        "SELECT id FROM entries WHERE entry_date=? AND body=? AND created_at >= ? "
        "ORDER BY id DESC LIMIT 1",
        (entry_date, body, cutoff),
    ).fetchone()
    return row["id"] if row else None


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

    ALWAYS call this to capture an entry — never block on resolution. Safe to retry: an
    identical body on the same day within two minutes is treated as a re-send, and comes
    back as the ORIGINAL entry flagged `duplicate_of_recent_save` with its still-pending
    mentions, rather than saving a second copy. To deliberately record the same words
    twice, vary the wording or resolve the first entry's mentions.

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
    from get_briefing / their relationships in the summaries (e.g. you know Robin's
    parents are Karl and Nina -> pass ["Karl", "Nina"]). The reference is relative to
    the speaker, so use the snippet to tell whose ("my parents" vs "Robin's parents").
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
        mentions: Surface forms of people referenced, e.g. ["Tom", "Robin"].
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
        # A connector retrying a timed-out call re-sends this identical payload; without
        # the guard it would save a second copy of the entry (see _recent_duplicate).
        if dup_id := _recent_duplicate(conn, entry_date, body):
            pend = conn.execute(
                """SELECT id, surface_form FROM mentions
                   WHERE entry_id=? AND status='pending' ORDER BY id""",
                (dup_id,),
            ).fetchall()
            return {
                "entry_id": dup_id, "entry_date": entry_date, "kind": kind,
                "duplicate_of_recent_save": True,
                "note": "An identical entry was saved moments ago, so this looks like a "
                        "retry and nothing new was written. Its pending mentions are "
                        "below — resolve those rather than re-saving.",
                "mentions": [
                    {"mention_id": m["id"], "surface_form": m["surface_form"],
                     "candidates": find_candidates(conn, m["surface_form"])}
                    for m in pend
                ],
            }
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
# Website-only mention resolution (NOT MCP tools — like
# import_bodyweight, these are reachable only by the authenticated user through the
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
    siblings BY NAME as you learn them (e.g. "Parents: Karl (father), Nina (mother).
    Brother: Theo."). The server has NO relationship graph, so this profile is the only
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
    and stray punctuation are safe and every term must appear (AND). Terms match
    as PREFIXES, not substrings — "birthd" finds birthday, the tail of a word
    finds nothing. Set
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

    Each term is a PREFIX query ("term"*). Two reasons, one of them load-bearing
    here: FTS5's unicode61 tokenizer treats a run of CJK as ONE token, so half the
    recipe titles ("宫保鸡丁") were reachable only by typing the whole name —
    "宫保" matched nothing. Prefixing fixes that and the everyday English case
    ("doubanji" now finds doubanjiang) in the same character. It does NOT give
    substring matching — a search for the TAIL of a word or a CJK name still
    misses; that needs the trigram tokenizer and an FTS rebuild.
    """
    return " ".join(f'"{t}"*' for t in re.findall(r"[\w']+", q or ""))


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
    """Shared delete implementation behind every delete tool on both servers (the
    journal's journal_delete_entry, the trainer's kind-scoped delete_record). Maps
    `kind` to its table, deletes the row, and (for sets) renumbers the remaining
    set_index so it stays contiguous."""
    tables = {"entry": "entries", "intake_item": "intake_items",
              "workout": "workouts", "set": "sets"}
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


@mcp.tool(name="journal_delete_entry", annotations=DESTRUCTIVE)
def journal_delete_entry(entry_id: int) -> dict:
    """Permanently delete one journal entry and its mentions (FTS stays in sync).
    Irreversible — confirm first. Find the id with get_entry/search_entries."""
    return _delete_record("entry", entry_id)


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
def get_briefing(days: int = 14, people_days: int = 7, max_entries: int = 80,
                 max_chars: int = 300) -> dict:
    """One-call session context, scoped to what's RECENT — call it at the start of a
    conversation, before capturing or answering.

    Returns:
      - `now` — current Pacific date/time with `date`/`yesterday`/`tomorrow`
        precomputed. All dates here are Pacific: anchor any day reference to these
        EXACT strings rather than computing dates yourself.
      - `recent_entries` — every entry from the last `days` Pacific days (default 14),
        newest first, so you write new entries with the last two weeks in view: what's
        ongoing, what's already been said, how the user has been describing things. If
        the window is empty (a gap since they last journaled) it falls back to the most
        recent handful anyway, flagged `outside_window`, so you're never blind.
      - `people` — the people MENTIONED in the last `people_days` days (default 7), each
        with their rolling `summary`. These are who the user is most likely to talk
        about next, and the summaries are what let you resolve "her parents" or tell two
        Toms apart. `mentioned_on` is their most recent mention date.
      - `roster` — everyone ELSE, compact (id, name, role, no summary). Enough to
        recognize and resolve a name that hasn't come up lately; call
        get_person_history for the full summary when one of them does come up.
      - `groups`, `pending_mentions` — circles, and the size of the resolution queue.

    The split is deliberate: summaries are the expensive part of this payload, so they
    go only to the people actually in play, while the roster keeps EVERY person
    resolvable. Widen either window when the user is catching up after a long gap
    (e.g. days=30, people_days=30); the caps (`max_entries`, `max_chars`) keep a busy
    stretch from crowding out the conversation, and `entries_truncated` says when one
    bit."""
    ref = date.fromisoformat(today())
    entry_cutoff = (ref - timedelta(days=max(days, 1) - 1)).isoformat()
    people_cutoff = (ref - timedelta(days=max(people_days, 1) - 1)).isoformat()
    order = ("ORDER BY e.entry_date DESC, e.day_position IS NULL, "
             "e.day_position DESC, e.id DESC")
    with db() as conn:
        rows = conn.execute(
            f"SELECT e.id, e.entry_date, e.body, e.kind FROM entries e "
            f"WHERE e.entry_date >= ? {order} LIMIT ?",
            (entry_cutoff, max_entries + 1),
        ).fetchall()
        truncated = len(rows) > max_entries
        rows = rows[:max_entries]
        outside = False
        if not rows:
            # Nothing in the window — the user hasn't journaled in a while. Hand back
            # the latest few anyway: stale context beats none when they resume.
            outside = True
            rows = conn.execute(
                f"SELECT e.id, e.entry_date, e.body, e.kind FROM entries e {order} LIMIT 5"
            ).fetchall()

        # People in play: mentioned (and resolved) inside the people window.
        active = conn.execute(
            """SELECT p.id, p.canonical_name, p.role, p.summary,
                      MAX(e.entry_date) AS mentioned_on
               FROM people p
               JOIN mentions m ON m.person_id = p.id AND m.status='resolved'
               JOIN entries e ON e.id = m.entry_id
               WHERE e.entry_date >= ?
               GROUP BY p.id ORDER BY mentioned_on DESC, p.canonical_name""",
            (people_cutoff,),
        ).fetchall()
        active_ids = {r["id"] for r in active}
        people = [
            {"person_id": r["id"], "name": r["canonical_name"], "role": r["role"],
             "groups": _groups_for(conn, r["id"]),
             "summary": _truncate(r["summary"], 300) if r["summary"] else None,
             "mentioned_on": r["mentioned_on"]}
            for r in active
        ]
        # Everyone else stays resolvable, but without the summary that dominates the cost.
        roster = [
            {"person_id": r["id"], "name": r["canonical_name"], "role": r["role"]}
            for r in conn.execute(
                "SELECT id, canonical_name, role FROM people ORDER BY canonical_name"
            ).fetchall()
            if r["id"] not in active_ids
        ]
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM mentions WHERE status='pending'"
        ).fetchone()["n"]
        grp = [r["name"] for r in conn.execute("SELECT name FROM groups ORDER BY name")]
    out = {
        "now": current_clock(),
        "window": {"entry_days": days, "people_days": people_days,
                   "since": entry_cutoff},
        "people": people,
        "roster": roster,
        "people_count": len(people) + len(roster),
        "groups": grp,
        "pending_mentions": pending,
        "recent_entries": [
            {"entry_id": r["id"], "entry_date": r["entry_date"], "kind": r["kind"],
             "body": _truncate(r["body"], max_chars)} for r in rows
        ],
    }
    if truncated:
        out["entries_truncated"] = (
            f"more than {max_entries} entries since {entry_cutoff}; showing the newest. "
            "Raise max_entries or narrow days for the rest.")
    if outside:
        out["outside_window"] = (
            f"no entries since {entry_cutoff}; showing the most recent instead.")
    return out


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
ADD_NEAR_DUP = 0.88    # add_exercise asks "did you mean?" at/above this
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
    """Rank the user's exercises against a spoken name, best first. Returns
    [{exercise_id, name, score, archived, primary}] with score >= EX_MATCH_FLOOR, so a
    caller that can't confidently resolve a name can hand back the closest real entries
    instead of guessing or creating a near-duplicate. Scored against the canonical name
    AND any AKAs. Archived movements are included (they're the user's history, and the
    small catalog makes hiding them pointless); active ones list first among equals."""
    rows = conn.execute("SELECT id, name, archived FROM exercises ORDER BY name").fetchall()
    amap = _alias_map(conn)

    def best(r) -> float:
        forms = [r["name"], *amap.get(r["id"], [])]
        return max(_score_exercise_name(name, f) for f in forms)

    scored = sorted(
        ((best(r), 1 - int(r["archived"]), r) for r in rows),
        key=lambda t: (t[0], t[1]), reverse=True,
    )
    return [{"exercise_id": r["id"], "name": r["name"], "score": sc,
             "archived": bool(r["archived"]),
             "primary": _muscles_for(conn, r["id"])["primary"]}
            for sc, _act, r in scored[:limit] if sc >= EX_MATCH_FLOOR]


def _resolve_exercise(conn: sqlite3.Connection, name: str):
    """Resolve a spoken name to ONE of the user's exercises (active or archived), or
    None. Tries exact on the name (case-insensitive), then an exact AKA, then a
    high-confidence fuzzy match with a clear lead. A merely plausible name returns None,
    leaving the caller to surface `_match_exercises` candidates — or, when nothing is
    close, to create the movement on the fly (_resolve_or_create)."""
    name = (name or "").strip()
    if not name:
        return None
    row = conn.execute(
        "SELECT * FROM exercises WHERE lower(name)=lower(?)", (name,),
    ).fetchone()
    if row:
        return row
    hits = conn.execute(
        """SELECT e.* FROM exercises e
           JOIN exercise_aliases a ON a.exercise_id = e.id
           WHERE a.alias = lower(?)""",
        (name,),
    ).fetchall()
    if len(hits) == 1:
        return hits[0]
    # Shorthand: every word of a 2+-word name appears in exactly ONE exercise's name
    # ("bench press" → Barbell Bench Press). Two hits (Barbell AND Dumbbell Bench Press)
    # is ambiguous and falls through to candidates.
    words = re.findall(r"[a-z0-9]+", name.lower())
    if len(words) >= 2:
        subset = [r for r in conn.execute("SELECT * FROM exercises").fetchall()
                  if set(words) <= set(re.findall(r"[a-z0-9]+", r["name"].lower()))]
        if len(subset) == 1:
            return subset[0]
    m = _match_exercises(conn, name, limit=2)
    if m and m[0]["score"] >= EX_CONFIDENT and (len(m) == 1 or m[0]["score"] - m[1]["score"] >= 0.06):
        return conn.execute("SELECT * FROM exercises WHERE id=?", (m[0]["exercise_id"],)).fetchone()
    return None


def _bad_muscles(*tiers: Optional[list[str]]) -> Optional[str]:
    bad = [m for t in tiers for m in (t or []) if m.strip().lower() not in MUSCLES]
    return f"unknown muscle label(s) {bad}; use one of {MUSCLES}" if bad else None


def _create_exercise(conn: sqlite3.Connection, name: str, muscles: Optional[list[str]],
                     secondary_muscles: Optional[list[str]] = None,
                     category: Optional[str] = None, note: Optional[str] = None,
                     mechanic: Optional[str] = None) -> int:
    """Insert a new ACTIVE exercise + its muscle links. Callers validate first."""
    eid = conn.execute(
        "INSERT INTO exercises(name, category, note, mechanic, in_rotation, hearted, "
        "archived, created_at) VALUES (?,?,?,?,1,1,0,?)",
        (name.strip(), (category or "strength").lower(), note,
         mechanic if mechanic in MECHANICS else None, now()),
    ).lastrowid
    _set_muscles(conn, eid, muscles or [], secondary_muscles or [])
    return eid


# Compound (several joints, heavy, slow to recover from) vs isolation (one joint). The
# model classifies a lift when it creates or updates it; the /trainer page reads it to
# size the rest target (an isolation set needs far less rest than a heavy compound one).
# It reuses the dormant library column of the same name; NULL = not classified yet.
MECHANICS = ("compound", "isolation")


def _set_archived(conn: sqlite3.Connection, eid: int, archived: bool) -> None:
    a = int(bool(archived))
    # in_rotation/hearted are dormant mirrors of `archived` (see _prune_exercise_library).
    conn.execute("UPDATE exercises SET archived=?, in_rotation=?, hearted=? WHERE id=?",
                 (a, 1 - a, 1 - a, eid))


def _resolve_or_create(conn: sqlite3.Connection, spec: dict, events: dict):
    """The one path every logging/planning tool resolves an exercise name through.
    Known name → that row (an ARCHIVED one is brought back to active, since it's being
    done again — noted under events["reactivated"]). Unknown name with nothing close and
    `muscles` given (or category 'cardio') → created on the fly, noted under
    events["created"]. Otherwise → None, with an entry under events["unmatched"]: either
    near-duplicate `candidates` to pick from, or a request for the muscles a new
    movement needs (recency can't count a lift with no muscles)."""
    name = (spec.get("name") or "").strip()
    if not name:
        return None
    row = _resolve_exercise(conn, name)
    if row:
        if row["archived"]:
            _set_archived(conn, row["id"], False)
            events.setdefault("reactivated", []).append(row["name"])
            row = conn.execute("SELECT * FROM exercises WHERE id=?", (row["id"],)).fetchone()
        return row
    near = [c for c in _match_exercises(conn, name) if c["score"] >= ADD_NEAR_DUP]
    if near and not spec.get("new"):
        events.setdefault("unmatched", []).append(
            {"name": name, "candidates": near,
             "fix": "re-send under one of these names, or with \"new\": true if it's "
                    "genuinely a different movement"})
        return None
    muscles, secondary = spec.get("muscles"), spec.get("secondary_muscles")
    cardio = (spec.get("category") or "").lower() == "cardio"
    if not muscles and not cardio:
        events.setdefault("unmatched", []).append(
            {"name": name, "fix": "new exercise — re-send with `muscles` (primary) and "
                                  "optionally `secondary_muscles`, or category 'cardio'"})
        return None
    if reason := _bad_muscles(muscles, secondary):
        events.setdefault("unmatched", []).append({"name": name, "fix": reason})
        return None
    eid = _create_exercise(conn, name, muscles, secondary, spec.get("category"),
                           mechanic=spec.get("mechanic"))
    events.setdefault("created", []).append(name)
    return conn.execute("SELECT * FROM exercises WHERE id=?", (eid,)).fetchone()


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
# Intake tools — water and protein, on the TRAINER server
# --------------------------------------------------------------------------- #

# The intake log used to be a full food tracker (calories, macros, sodium, fiber,
# alcohol) on the journal connector. It's now just the two daily figures the user
# still keeps — water and protein — and it lives on the trainer server, since both
# are training-adjacent and too small to justify a surface of their own.
#
# The TABLE is unchanged: one intake_items row per thing consumed, day totals DERIVED
# (summed), never stored, so a correction is one UPDATE and every total follows. The
# other nutrient columns (calories, carbs_g, fat_g, sodium_mg, fiber_g,
# standard_drinks) are DORMANT — kept with their history, never read or written by
# the code. A legacy row carrying none of the two live figures is simply invisible.
#
# NUTRIENTS drives every sum/round/render site, so adding a figure back is one tuple
# entry plus a unit label in macros.NUTRIENT_UNITS — the column is already there.
NUTRIENTS = ("protein_g", "water_oz")
# Per-ITEM sanity ceilings — a typo guard (a stray exponent, a doubled zero), not a
# judgment. It matters because day totals are DERIVED: one absurd row silently skews
# the day, nowhere near the item that caused it.
NUTRIENT_MAX = {"protein_g": 500, "water_oz": 512}
# Only rows carrying at least one live figure are part of the log.
_LIVE_INTAKE = "(" + " OR ".join(f"{m} IS NOT NULL" for m in NUTRIENTS) + ")"


def _bad_nutrients(values: dict) -> Optional[dict]:
    """Range-check per-item figures. Shared by log_intake and update_intake so the
    two can't drift. Same spirit as _bad_set: the error names the fix."""
    for k, v in values.items():
        if v is None:
            continue
        if v < 0:
            return {"error": f"{k} must not be negative, got {v}"}
        if k in NUTRIENT_MAX and v > NUTRIENT_MAX[k]:
            return {"error": f"{k}={v} is past the sane ceiling for ONE item "
                             f"({NUTRIENT_MAX[k]}) — check for a typo. Day totals "
                             "are summed from items, so a wrong one skews the day."}
    return None


def _item_row(r) -> dict:
    """One logged item, token-compact: only the figures it actually carries."""
    out = {"item_id": r["id"], "food_date": r["food_date"]}
    if r["item"]:
        out["item"] = r["item"]
    if r["note"]:
        out["note"] = r["note"]
    for m in NUTRIENTS:
        if r[m] is not None:
            out[m] = round(r[m], 1)
    return out


def _day_totals(rows) -> dict:
    """Sum a day's items per figure. One no item carries stays ABSENT (not 0) — the
    day simply wasn't logged for it, which is a different fact from zero."""
    totals = {}
    for m in NUTRIENTS:
        vals = [r[m] for r in rows if r[m] is not None]
        if vals:
            totals[m] = round(sum(vals), 1)
    return totals


def _day_rows(conn: sqlite3.Connection, d: str) -> list:
    return conn.execute(
        f"SELECT * FROM intake_items WHERE food_date=? AND {_LIVE_INTAKE} "
        "ORDER BY position, id", (d,),
    ).fetchall()


def _with_totals(out: dict, conn: sqlite3.Connection, d: str) -> dict:
    """Pair a write's return with where the day now stands, and the targets to read
    it against — a bare "64oz" is a number the model can report but not judge."""
    out["day_totals"] = _day_totals(_day_rows(conn, d))
    if targets := _day_targets(conn):
        out["targets"] = targets
    return out


@trainer_mcp.tool(name="log_intake", annotations=WRITE)
def log_intake(protein_g: Optional[float] = None, water_oz: Optional[float] = None,
               item: str = "", food_date: Optional[str] = None,
               note: Optional[str] = None) -> dict:
    """Log water and/or protein — ONE thing consumed per call ("a protein shake and
    a big glass of water" is two calls, or one if they came together). These are the
    only two intake figures tracked; don't estimate or mention calories or other
    macros.

    The returned `day_totals` are where the day ACTUALLY stands — read them back
    instead of keeping your own running tally (the app or another conversation may
    have logged the same day). `targets` holds the user's daily goals (see
    set_intake_targets), so read a total against its target rather than reporting a
    bare number.

    Water is in fluid ounces (128 = a gallon; a "glass" is ~12-16oz, a typical
    bottle 16.9oz). Protein is grams — estimate it from what they ate when they
    don't give a number (a chicken breast ~40g, a scoop of whey ~25g), and say
    what you assumed.

    Args:
        protein_g: Grams of protein in this item.
        water_oz: Fluid ounces of water.
        item: Optional short label in the user's own terms ("protein shake",
            "chicken breast"). Leave empty for a bare water top-up.
        food_date: Day consumed, YYYY-MM-DD (Pacific). Defaults to today.
        note: Optional context.
    """
    if err := _bad_date(food_date, "food_date"):
        return err
    item = (item or "").strip()
    nutrients = {"protein_g": protein_g, "water_oz": water_oz}
    if err := _bad_nutrients(nutrients):
        return err
    if all(v is None for v in nutrients.values()):
        return {"error": "nothing to log — pass protein_g and/or water_oz"}
    d = food_date or today()
    cols = ("food_date", "position", "item", "note", *NUTRIENTS, "created_at")
    with db() as conn:
        # Position is the order logged within the day — server-assigned, the same
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
        row = conn.execute("SELECT * FROM intake_items WHERE id=?", (rid,)).fetchone()
        out = _with_totals(_item_row(row), conn, d)
    if url := _app_url("/food"):
        out["url"] = url
    return out


@trainer_mcp.tool(name="get_intake", annotations=READ_ONLY)
def get_intake(days: int = 7, since: Optional[str] = None,
               until: Optional[str] = None, include_items: bool = True) -> dict:
    """Read the water/protein log back: per day, its items (with ids) and summed
    totals, plus the daily `targets`. Use it for "how have I been doing on water this
    week", and to find the `item_id` of something to correct (update_intake) or
    remove (delete_record kind="intake"). Today's totals also ride in on
    get_fitness_briefing, so you don't need this just to check the current day.

    Days with nothing logged are omitted — they're unlogged, not zero. `averages` are
    per figure over the days that carry it, each with its own denominator; check
    `logged_days` before calling one a weekly average.

    Args:
        days: Size of the trailing window in days (ignored if `since` is given).
        since: Start date YYYY-MM-DD (inclusive).
        until: End date YYYY-MM-DD (inclusive). Defaults to today.
        include_items: Include each day's individual items. Set False for totals only.
    """
    if err := _bad_date(since, "since") or _bad_date(until, "until"):
        return err
    # A backwards window matched nothing and read like "you logged nothing then" —
    # a different, more alarming fact than "you asked backwards". Say which it is.
    if since and until and since > until:
        return {"error": f"since {since!r} is after until {until!r} — the window "
                         "runs earliest to latest; swap them"}
    until = until or today()
    if since is None:
        since = date.fromordinal(
            date.fromisoformat(until).toordinal() - max(days, 1) + 1).isoformat()
    with db() as conn:
        rows = conn.execute(
            f"SELECT * FROM intake_items WHERE food_date BETWEEN ? AND ? AND {_LIVE_INTAKE} "
            "ORDER BY food_date DESC, position, id", (since, until),
        ).fetchall()
        targets = _day_targets(conn)
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
    out = {"since": since, "until": until, "logged_days": len(by_day),
           "days": out_days, "averages": averages}
    if targets:
        out["targets"] = targets
    return out


@trainer_mcp.tool(name="update_intake", annotations=WRITE_IDEMPOTENT)
def update_intake_item(item_id: int, protein_g: Optional[float] = None,
                       water_oz: Optional[float] = None, item: Optional[str] = None,
                       food_date: Optional[str] = None,
                       note: Optional[str] = None) -> dict:
    """Correct ONE logged intake item. Only the args you pass are written, and they
    REPLACE that item's values. "That shake was 30g, not 50" is one call — you never
    recompute the day, because its totals are summed from the items. `food_date`
    moves the item to another day. To remove it, delete_record(kind="intake").
    Returns the re-derived `day_totals` with the `targets`, same as log_intake.

    Args:
        item_id: The item to correct (from log_intake or get_intake).
        protein_g: Replacement grams of protein.
        water_oz: Replacement fluid ounces of water.
        item: Replacement label.
        food_date: Move it to this day, YYYY-MM-DD (Pacific).
        note: Replacement note.
    """
    if err := _bad_date(food_date, "food_date"):
        return err
    fields = {"item": item, "note": note, "food_date": food_date,
              "protein_g": protein_g, "water_oz": water_oz}
    if err := _bad_nutrients({m: fields[m] for m in NUTRIENTS}):
        return err
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
        out = _with_totals({**_item_row(cur), "updated": [k for k in sets if k != "position"]},
                           conn, cur["food_date"])
    return out


def _intake_today(conn: sqlite3.Connection) -> dict:
    """Today's water/protein for get_fitness_briefing: totals plus targets, so a
    training conversation opens already knowing where the day stands."""
    out = {"totals": _day_totals(_day_rows(conn, today()))}
    if targets := _day_targets(conn):
        out["targets"] = targets
    return out


def _get_eating_profile(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT value FROM settings WHERE key='eating_profile'").fetchone()
    return json.loads(row["value"]) if row else {}


# Default daily targets, used until the user sets their own with set_intake_targets.
INTAKE_TARGET_DEFAULTS = {"protein_g": 130, "water_oz": 88}


def _stored_targets(conn: sqlite3.Connection) -> dict:
    """Only the targets the user actually SET (settings → eating_profile → targets,
    written by set_intake_targets or the /food page's Targets popover), without the
    defaults. Malformed stored entries — and targets for nutrients no longer tracked —
    are SKIPPED: _bad_targets guards the write, but an old blob shouldn't fail a log
    call."""
    t = _get_eating_profile(conn).get("targets")
    if not isinstance(t, dict):
        return {}
    return {k: v for k, v in t.items()
            if k in NUTRIENTS and not isinstance(v, bool)
            and isinstance(v, (int, float)) and v > 0}


def _day_targets(conn: sqlite3.Connection) -> dict:
    """The daily targets in effect: INTAKE_TARGET_DEFAULTS overridden by whatever the
    user set. A target is just a target; there's no ceiling/floor direction anywhere."""
    return {**INTAKE_TARGET_DEFAULTS, **_stored_targets(conn)}


def _bad_targets(targets) -> Optional[str]:
    """Check a targets write: a real nutrient key carrying a positive number. The
    rings silently keep their default otherwise, so without this the save would
    report success while the ring went on reading the old number."""
    if targets is None:
        return None
    if not isinstance(targets, dict):
        return (f"targets must be a {{nutrient: number}} dict, got {targets!r}; "
                f"the nutrients are {list(NUTRIENTS)}")
    for k, v in targets.items():
        if k not in NUTRIENTS:
            near = max(NUTRIENTS,
                       key=lambda n: jellyfish.jaro_winkler_similarity(str(k), n))
            return (f"unknown nutrient {k!r} in targets — did you mean {near!r}? "
                    f"The nutrients are {list(NUTRIENTS)}")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            return (f"targets[{k!r}] must be a positive number, got {v!r} — a "
                    "daily target of zero or a string is not something the app "
                    "can render; drop the key instead")
    return None


@trainer_mcp.tool(name="set_intake_targets", annotations=WRITE_IDEMPOTENT)
def set_intake_targets(protein_g: Optional[float] = None,
                       water_oz: Optional[float] = None) -> dict:
    """Set the user's daily protein and/or water target — only when they ask to
    change a goal ("make my water goal 100oz"). Omit an argument to leave that
    target alone; pass 0 to drop the user's own number and fall back to the default.
    Returns the targets now in effect (the same `targets` every intake return
    carries).

    Args:
        protein_g: Daily protein target in grams.
        water_oz: Daily water target in fluid ounces (128 = a gallon).
    """
    asked = {k: v for k, v in (("protein_g", protein_g), ("water_oz", water_oz))
             if v is not None}
    if not asked:
        return {"error": "pass protein_g and/or water_oz"}
    if any(v < 0 for v in asked.values()):
        return {"error": "a target can't be negative — pass 0 to go back to the default"}
    if (err := _bad_targets({k: v for k, v in asked.items() if v > 0})):
        return {"error": err}
    with db() as conn:
        profile = _get_eating_profile(conn)
        cur = profile.get("targets")
        cur = dict(cur) if isinstance(cur, dict) else {}
        for k, v in asked.items():
            if v == 0:
                cur.pop(k, None)
            else:
                cur[k] = v
        if cur:
            profile["targets"] = cur
        else:
            profile.pop("targets", None)
        conn.execute(
            """INSERT INTO settings(key, value) VALUES ('eating_profile', ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (json.dumps(profile),),
        )
        return {"targets": _day_targets(conn)}


# --------------------------------------------------------------------------- #
# Trainer: the user's exercises
#
# There is no library. The catalog is the movements the user DOES (active) and has
# DONE (archived) — nothing pre-loaded, no technique/cautions/image reference data
# (the model coaches form from its own knowledge, in conversation). A new movement is
# born on the fly the first time it's planned or logged (_resolve_or_create), or
# explicitly with add_exercise. Archiving is how a lift leaves the program while
# staying on record — with a `note` saying why, so the model knows what's been tried.
# --------------------------------------------------------------------------- #

def _exercise_rows(conn: sqlite3.Connection, archived: Optional[bool]) -> list[dict]:
    where = "" if archived is None else f"WHERE e.archived={int(archived)}"
    rows = conn.execute(
        f"""SELECT e.id, e.name, e.category, e.archived, e.note, e.mechanic,
                   MAX(NULLIF(w.workout_date,'')) AS last_done,
                   COUNT(DISTINCT CASE WHEN s.status='done' THEN w.id END) AS sessions
            FROM exercises e
            LEFT JOIN sets s ON s.exercise_id = e.id AND s.status='done'
            LEFT JOIN workouts w ON w.id = s.workout_id AND w.status='done'
            {where}
            GROUP BY e.id ORDER BY e.name""").fetchall()
    out = []
    for r in rows:
        m = _muscles_for(conn, r["id"])
        item = {"exercise_id": r["id"], "name": r["name"],
                "category": r["category"] or "strength",
                "muscles": m["primary"], "last_done": r["last_done"],
                "sessions": r["sessions"]}
        if m["secondary"] or m["tertiary"]:
            item["secondary_muscles"] = m["secondary"] + m["tertiary"]
        if r["mechanic"]:
            item["mechanic"] = r["mechanic"]
        if r["note"]:
            item["note"] = r["note"]
        out.append(item)
    return out


@trainer_mcp.tool(annotations=READ_ONLY)
def list_exercises(include_archived: bool = True) -> dict:
    """The user's exercises: `active` (what they're currently training — the pool you
    program from) and `archived` (what they've done before and stopped), each with
    muscles, category, `last_done`, `sessions` (how many workouts it appeared in) and
    any `note` (for an archived lift, usually why it was dropped). Read the archive
    before suggesting something new: it's the record of what's been tried, liked, or
    abandoned — a lift archived for shoulder pain is not a fresh idea. The briefing
    already carries the active list; call this when you need the archive."""
    with db() as conn:
        out = {"active": _exercise_rows(conn, False)}
        if include_archived:
            out["archived"] = _exercise_rows(conn, True)
    return out


def _exercise_by(conn: sqlite3.Connection, name: Optional[str],
                 exercise_id: Optional[int]):
    if exercise_id is not None:
        return conn.execute("SELECT * FROM exercises WHERE id=?", (exercise_id,)).fetchone()
    return _resolve_exercise(conn, name or "")


@trainer_mcp.tool(annotations=WRITE)
def add_exercise(name: str, muscles: list[str],
                 secondary_muscles: Optional[list[str]] = None,
                 category: Optional[str] = None, note: Optional[str] = None,
                 mechanic: Optional[Literal["compound", "isolation"]] = None,
                 new: bool = False) -> dict:
    """Add a movement to the user's ACTIVE exercises ahead of using it ("I want to start
    doing landmine presses"). You rarely need this: planning or logging an unknown name
    with its `muscles` creates it on the fly. Use the user's own name for the lift.

    `muscles` = primary (what the lift is FOR), `secondary_muscles` = real assistance,
    both in the canonical labels: abdominals, abductors, adductors, biceps, calves,
    chest, forearms, glutes, hamstrings, lats, lower back, middle back, neck,
    quadriceps, shoulders, traps, triceps. `category` is "strength" (default) or
    "cardio" (cardio carries no muscles — pass muscles=[]). `mechanic` is "compound"
    (multi-joint: presses, rows, pull-ups, squats, leg press, hip thrust) or
    "isolation" (one joint: curls, raises, flyes, pushdowns, leg extensions/curls);
    give it for every strength lift, since the app sizes the rest between sets by it.

    A name that already exists is refused with the existing entry (an archived one:
    bring it back with archive_exercise(archived=False)). A near-duplicate is refused
    with `candidates` unless `new=True` — pass that only when it's genuinely a
    different movement."""
    name = (name or "").strip()
    if not name:
        return {"error": "name is required"}
    if reason := _bad_muscles(muscles, secondary_muscles):
        return {"error": reason}
    if mechanic is not None and mechanic not in MECHANICS:
        return {"error": f"mechanic must be one of {list(MECHANICS)}"}
    if not muscles and (category or "").lower() != "cardio":
        return {"error": "give at least one primary muscle (or category='cardio')"}
    with db() as conn:
        if row := _resolve_exercise(conn, name):
            return {"error": f"already on file as {row['name']!r}"
                             + (" (archived)" if row["archived"] else ""),
                    "exercise_id": row["id"], "name": row["name"],
                    "archived": bool(row["archived"])}
        near = [c for c in _match_exercises(conn, name) if c["score"] >= ADD_NEAR_DUP]
        if near and not new:
            return {"error": f"{name!r} looks close to an existing exercise — use that "
                             "one, or pass new=True if it's genuinely different",
                    "candidates": near}
        eid = _create_exercise(conn, name, muscles, secondary_muscles, category, note,
                               mechanic)
    return {"exercise_id": eid, "name": name, "created": True}


@trainer_mcp.tool(annotations=WRITE_IDEMPOTENT)
def update_exercise(name: Optional[str] = None, exercise_id: Optional[int] = None,
                    rename: Optional[str] = None,
                    muscles: Optional[list[str]] = None,
                    secondary_muscles: Optional[list[str]] = None,
                    category: Optional[str] = None,
                    note: Optional[str] = None,
                    mechanic: Optional[Literal["compound", "isolation"]] = None) -> dict:
    """Fix an exercise: `rename` it (history follows — sets point at the row, not the
    name), correct its `muscles`/`secondary_muscles` (passing either rewrites both
    tiers, so send both), change `category`, set its `note` ("" clears), or set its
    `mechanic` (compound | isolation, see add_exercise; a lift listed without one
    hasn't been classified yet). Only what you pass changes. To take a lift out of (or back into) the program, use
    archive_exercise."""
    if reason := _bad_muscles(muscles, secondary_muscles):
        return {"error": reason}
    with db() as conn:
        row = _exercise_by(conn, name, exercise_id)
        if not row:
            return {"error": f"no exercise {name or exercise_id!r}",
                    "candidates": _match_exercises(conn, name or "")}
        changed = []
        if rename and rename.strip() and rename.strip() != row["name"]:
            clash = conn.execute(
                "SELECT name FROM exercises WHERE lower(name)=lower(?) AND id<>?",
                (rename.strip(), row["id"])).fetchone()
            if clash:
                return {"error": f"{clash['name']!r} already exists"}
            conn.execute("UPDATE exercises SET name=? WHERE id=?", (rename.strip(), row["id"]))
            changed.append("name")
        if category is not None:
            conn.execute("UPDATE exercises SET category=? WHERE id=?",
                         (category.lower() or "strength", row["id"]))
            changed.append("category")
        if note is not None:
            conn.execute("UPDATE exercises SET note=? WHERE id=?", (note or None, row["id"]))
            changed.append("note")
        if mechanic is not None:
            if mechanic not in MECHANICS:
                return {"error": f"mechanic must be one of {list(MECHANICS)}"}
            conn.execute("UPDATE exercises SET mechanic=? WHERE id=?", (mechanic, row["id"]))
            changed.append("mechanic")
        if muscles is not None or secondary_muscles is not None:
            _set_muscles(conn, row["id"], muscles or [], secondary_muscles or [])
            changed.append("muscles")
        out_name = conn.execute("SELECT name FROM exercises WHERE id=?",
                                (row["id"],)).fetchone()["name"]
    return {"exercise_id": row["id"], "name": out_name, "updated": changed}


@trainer_mcp.tool(annotations=WRITE_IDEMPOTENT)
def archive_exercise(name: Optional[str] = None, exercise_id: Optional[int] = None,
                     archived: bool = True, note: Optional[str] = None) -> dict:
    """Take a lift OUT of the user's active program (archived=True), or bring an
    archived one back (archived=False). Nothing is deleted: its history, PRs and
    sessions stay, and it sits in the archive as a record of what's been tried. Pass a
    `note` with the reason when the user gives one ("bugged my left shoulder", "bored of
    it", "swapped for landmine press") — it's what makes the archive useful the next
    time you're choosing something new. Only on the user's say-so: the active set is
    theirs to curate. (Logging an archived lift brings it back automatically — it's
    being done again.)"""
    with db() as conn:
        row = _exercise_by(conn, name, exercise_id)
        if not row:
            return {"error": f"no exercise {name or exercise_id!r}",
                    "candidates": _match_exercises(conn, name or "")}
        _set_archived(conn, row["id"], archived)
        if note is not None:
            conn.execute("UPDATE exercises SET note=? WHERE id=?", (note or None, row["id"]))
    return {"exercise_id": row["id"], "name": row["name"], "archived": bool(archived)}


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
    Names resolve against the user's exercises (fuzzily, so a near-spelling lands on the
    right lift; an ARCHIVED one is brought back, reported under `reactivated`). A
    movement they've never done is CREATED on the fly when you pass its `muscles`
    (reported under `created`); without them, or when the name is close to an existing
    one, it's skipped and returned under `unmatched` with what to fix — the rest of the
    session still logs, so capture isn't lost. weight_lbs follows the SIGNED
    added/removed-load convention (see this server's instructions) and is null for cardio;
    rpe is 1-10 perceived exertion (10 = couldn't do another rep), which is how you judge
    whether to add weight next time. Returns the logged exercises (with their stored
    names) plus any `created`/`reactivated`/`unmatched`/`new_prs`.

    CARDIO (running, walking, rowing, cycling): log it as an exercise too, with
    category "cardio" if it's new (cardio carries no muscles, so it stays out of muscle
    recency and is summarized as cardio instead), and use a set per bout with
    `duration_seconds` and/or `distance_miles` instead of
    weight/reps. A 30-minute, 3.2-mile run is one set
    {"duration_seconds": 1800, "distance_miles": 3.2, "rpe": 6}. weight_lbs/reps stay
    null. Intervals can be one set each. Pass durations in SECONDS (25 min = 1500).

    Args:
        exercises: The exercises performed, each with the sets performed.
        workout_date: Day trained, YYYY-MM-DD. Defaults to today.
        focus: Short kind-of-day label, e.g. "Legs", "Pull + Legs", "Cardio" —
            never a lift list (the rule lives in the server instructions).
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
        results, events, set_ids = [], {}, []
        for ex in exercises:
            row = _resolve_or_create(conn, ex, events)
            if not row:
                continue
            eid = row["id"]
            # continue set numbering if this exercise already has sets in the session
            start = (conn.execute(
                "SELECT COALESCE(MAX(set_index),0) AS m FROM sets WHERE workout_id=? AND exercise_id=?",
                (wid, eid),
            ).fetchone()["m"]) + 1
            for i, s in enumerate(ex.get("sets") or [], start=start):
                set_ids.append(conn.execute(
                    """INSERT INTO sets(workout_id, exercise_id, set_index, weight_lbs,
                       reps, rpe, duration_seconds, distance_miles, note)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (wid, eid, i, s.get("weight_lbs"), s.get("reps"),
                     s.get("rpe"), s.get("duration_seconds"),
                     s.get("distance_miles"), s.get("note")),
                ).lastrowid)
            results.append({"exercise_id": eid, "name": row["name"],
                            "sets": len(ex.get("sets") or [])})
        prs = _new_bests(conn, set_ids)
    out = {"workout_id": wid, "workout_date": wd, "exercises": results,
           "appended": workout_id is not None, **events}
    if prs:
        out["new_prs"] = prs
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


def pr_for_set(set_id: int) -> Optional[dict]:
    """Is this just-logged set a personal best for its exercise? A plain helper, NOT an
    MCP tool: the model already reads bests through get_personal_records, while this
    answers the one question the /trainer card asks after a tap — so the page can throw
    confetti at the chip. Website-only, like clear_plan_set and set_archived. Returns
    {set_id, exercise_id, weight_lbs, reps} when it IS a best, else None.

    The rule, stated once, here: a set is a best when its weight EXCEEDS the heaviest
    ever logged for that movement, or TIES that weight and beats the most reps ever done
    at it. No e1rm — a formula's estimate isn't a thing that happened. `weight_lbs` is
    SIGNED, so plain `>` is right for assisted work too (a pull-up at -10 beats one at
    -20). Cardio never counts: a set with a NULL weight or NULL reps can't be a best
    here, and distance/duration bests live in get_personal_records. Neither does the
    FIRST weighted set of a movement — there was nothing to beat.

    `id<>?` is what makes this "was it a best BEFORE this set": the row is already
    written by the time we ask. Every OTHER done set counts, including earlier sets of
    the session in progress (matching get_personal_records, which doesn't filter on
    workout status), so the third set at the day's top weight doesn't re-announce the
    record the first one set. A set that isn't 'done' returns None, so the blank-reps
    path (clear_plan_set) can never celebrate.

    The caller dedupes: correcting a set re-asks this question and gets the same honest
    answer, so the browser is what remembers it already celebrated (see trainer.js).
    """
    with db() as conn:
        r = conn.execute(
            "SELECT exercise_id, status, weight_lbs, reps FROM sets WHERE id=?",
            (set_id,),
        ).fetchone()
        if not r or r["status"] != "done":
            return None
        w, reps, eid = r["weight_lbs"], r["reps"], r["exercise_id"]
        if w is None or reps is None:
            return None
        hit = {"set_id": set_id, "exercise_id": eid, "weight_lbs": w, "reps": reps}
        best = conn.execute(
            """SELECT MAX(weight_lbs) AS w FROM sets
               WHERE exercise_id=? AND status='done' AND id<>?
                 AND weight_lbs IS NOT NULL AND reps IS NOT NULL""",
            (eid, set_id),
        ).fetchone()["w"]
        if best is None:
            return None
        if w > best:
            return hit
        if w < best:
            return None
        best_reps = conn.execute(
            """SELECT MAX(reps) AS r FROM sets
               WHERE exercise_id=? AND status='done' AND id<>? AND weight_lbs=?
                 AND reps IS NOT NULL""",
            (eid, set_id, w),
        ).fetchone()["r"]
        return hit if (best_reps is not None and reps > best_reps) else None


def _new_bests(conn: sqlite3.Connection, set_ids: list[int]) -> list[dict]:
    """pr_for_set's rule, applied to a BATCH of just-written sets: per exercise, the
    batch's top set (heaviest, then most reps at that weight) is a best when it beats
    every done set OUTSIDE the batch. Excluding the whole batch rather than one row is
    the difference that matters here — two sets at a new top weight would otherwise
    each be "tied" by the other and neither would count. Same no-e1rm, no-cardio,
    no-first-ever rules. Returns [{exercise_id, name, weight_lbs, reps, previous_lbs}],
    which complete_sets and log_workout hand back so the model can call it out."""
    if not set_ids:
        return []
    ph = ",".join("?" for _ in set_ids)
    rows = conn.execute(
        f"""SELECT s.exercise_id, e.name, s.weight_lbs, s.reps FROM sets s
            JOIN exercises e ON e.id = s.exercise_id
            WHERE s.id IN ({ph}) AND s.status='done'
              AND s.weight_lbs IS NOT NULL AND s.reps IS NOT NULL""",
        set_ids,
    ).fetchall()
    top: dict[int, sqlite3.Row] = {}
    for r in rows:
        cur = top.get(r["exercise_id"])
        if cur is None or (r["weight_lbs"], r["reps"]) > (cur["weight_lbs"], cur["reps"]):
            top[r["exercise_id"]] = r
    out = []
    for eid, r in top.items():
        best = conn.execute(
            f"""SELECT MAX(weight_lbs) AS w FROM sets
                WHERE exercise_id=? AND status='done' AND id NOT IN ({ph})
                  AND weight_lbs IS NOT NULL AND reps IS NOT NULL""",
            (eid, *set_ids),
        ).fetchone()["w"]
        if best is None or r["weight_lbs"] < best:
            continue
        if r["weight_lbs"] == best:
            best_reps = conn.execute(
                f"""SELECT MAX(reps) AS r FROM sets
                    WHERE exercise_id=? AND status='done' AND id NOT IN ({ph})
                      AND weight_lbs=? AND reps IS NOT NULL""",
                (eid, *set_ids, best),
            ).fetchone()["r"]
            if best_reps is None or r["reps"] <= best_reps:
                continue
        out.append({"exercise_id": eid, "name": r["name"], "weight_lbs": r["weight_lbs"],
                    "reps": r["reps"], "previous_lbs": best})
    return out


@trainer_mcp.tool(annotations=WRITE_IDEMPOTENT)
def update_workout(workout_id: int, workout_date: Optional[str] = None,
                   focus: Optional[str] = None, feeling: Optional[str] = None,
                   notes: Optional[str] = None,
                   append_note: Optional[str] = None,
                   planned_date: Optional[str] = None) -> dict:
    """Edit a session's metadata. Only non-null args are written. Use `workout_date`
    (YYYY-MM-DD, Pacific) to move a COMPLETED session to the right day, or set focus/
    feeling/notes after the fact.

    `planned_date` moves a PLANNED session to a different day ("push Thursday's to
    Friday"); pass "" to unschedule it back to a plain next-session plan. It's intent
    only — a plan is still recorded under the day it's actually finished.

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
    if planned_date and (err := _bad_date(planned_date, "planned_date")):
        return err
    fields = {"workout_date": workout_date, "focus": focus,
              "feeling": feeling, "notes": notes}
    sets = {k: v for k, v in fields.items() if v is not None}
    # "" unschedules a plan (back to NULL), so it's read here rather than by the
    # non-null filter above, which would drop it.
    if planned_date is not None:
        sets["planned_date"] = planned_date or None
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
    (e.g. bump the planned weight or the expected difficulty) without completing it — to
    actually log a planned set as done, use complete_sets."""
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
        row = conn.execute("SELECT workout_id FROM sets WHERE id=?", (set_id,)).fetchone()
        if not row:
            return {"error": f"no set with id {set_id}"}
        cols = ", ".join(f"{k}=?" for k in sets)
        conn.execute(f"UPDATE sets SET {cols} WHERE id=?", (*sets.values(), set_id))
    # workout_id so a caller holding only a set_id can re-read the right session (the
    # web app does: several plans can be open at once, so "the current plan" won't do).
    return {"set_id": set_id, "workout_id": row["workout_id"], "updated": list(sets)}


# --------------------------------------------------------------------------- #
# Trainer: the active workout PLAN (today's routine, in progress)
#
# A plan is just a `workouts` row with status='active' whose `sets` are 'pending'
# (targets filled, actuals NULL). Completing a set fills its actuals and flips it to
# 'done', so the plan becomes the historical log as you work through it — one table,
# no plan<->log reconciliation. The model designs the routine in conversation (from
# get_fitness_briefing + get_exercise_history) and writes it here; the server just
# stores/serves it.
#
# SEVERAL plans can be active at once — a week laid out in one conversation is one
# active row per day, each carrying its `planned_date`. So a plan is addressed by id
# wherever the caller knows which one it means (every web-app route does); the tools
# keep an optional workout_id and fall back to _current_plan for the common "the one
# I'm doing now" case.
# --------------------------------------------------------------------------- #

def _current_plan(conn: sqlite3.Connection):
    """The plan to act on when the caller didn't name one: the NEXT DUE active session,
    or None. An unscheduled plan (planned_date NULL/'') competes as today's — an ad-hoc
    "build me something now" shouldn't queue behind Friday's plan — and ties break on the
    oldest id. Callers that must address one exact session (the web app's per-session
    pages) use _plan_row instead."""
    return conn.execute(
        """SELECT * FROM workouts WHERE status='active'
           ORDER BY COALESCE(NULLIF(planned_date,''), ?) ASC, id ASC LIMIT 1""",
        (today(),),
    ).fetchone()


def _plan_row(conn: sqlite3.Connection, workout_id: Optional[int]):
    """One workout by id, or _current_plan when no id was given — the resolution every
    plan tool shares."""
    if workout_id is None:
        return _current_plan(conn)
    return conn.execute("SELECT * FROM workouts WHERE id=?", (workout_id,)).fetchone()


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
                    exercises: list[PlannedExercise]) -> tuple[list[dict], dict]:
    """Append pending (planned) sets to a workout. Resolves each exercise through
    _resolve_or_create (the same path log_workout uses) and continues set numbering per
    exercise, writing targets + status='pending'. Returns (results, events): events
    carries `created`/`reactivated`/`unmatched` for the return."""
    results, events = [], {}
    for ex in exercises:
        row = _resolve_or_create(conn, ex, events)
        if not row:
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
    return results, events


def _plan_payload(conn: sqlite3.Connection, wid: int,
                  events: Optional[dict] = None) -> dict:
    """The plan for one workout: exercises ordered by `ex_position` (the user-set order,
    via reorder_plan or the /trainer reorder UX) and otherwise by insertion order, each
    with its sets (target + actual + status), plus a done/total progress count (skipped
    sets are excluded from the total). Used by every plan tool's return and by the web UI.

    `unmatched` (names this call couldn't resolve to a real catalog exercise, each with
    its closest `candidates`) is surfaced so the model re-issues them under a name that
    exists instead of inventing one — the catalog is closed to the assistant."""
    w = conn.execute(
        "SELECT id, workout_date, planned_date, focus, feeling, notes, status "
        "FROM workouts WHERE id=?",
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
               "workout_date": w["workout_date"], "planned_date": w["planned_date"],
               "focus": w["focus"],
               "feeling": w["feeling"], "notes": w["notes"], "status": w["status"],
               "progress": {"done": done, "total": total}, "exercises": exercises}
    if events:
        payload.update(events)
    return payload


def remove_plan_exercise(exercise_id: int, workout_id: Optional[int] = None) -> dict:
    """Drop one exercise from the active plan entirely — every set of it, planned or
    already done. Backs the /trainer page's per-exercise "..." menu (Delete option); the
    model substitutes via swap_exercise instead, so this stays a plain helper, not an MCP
    tool. Returns the updated plan."""
    with db() as conn:
        w = _plan_row(conn, workout_id)
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
        w = _plan_row(conn, workout_id)
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
        w = _plan_row(conn, workout_id)
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
                       notes: Optional[str] = None, replace: bool = False,
                       planned_date: Optional[str] = None,
                       workout_id: Optional[int] = None) -> dict:
    """Lay out a routine the user works through — today's session, or any day ahead.
    Call this once you've decided the session from a get_fitness_briefing (+
    get_exercise_history for the lifts you're choosing weights for). Each exercise
    becomes a row of PENDING sets with targets; the user completes them with complete_set
    as they go.

    `planned_date` (YYYY-MM-DD, Pacific) is the day the session is FOR — pass it whenever
    you're planning past today, and call this once PER DAY to lay out a week. Plan each
    day off a briefing whose `as_of` is that day, so recovery is counted as of when the
    session will actually happen, and read the briefing's `upcoming` so you count the work
    you've already programmed earlier in the week (it isn't in `muscle_recency`, which is
    completed work only). Omit `planned_date` for an unscheduled "next session"; either
    way the plan is DATED only when finish_workout stamps the day it was actually
    completed, so a session done a day late records the day it was done.

    ALWAYS set `focus` — it's the session's only title, and on a week of upcoming plans
    it's the one thing telling the days apart.

    Build a full session at the volume this server's instructions call for, across the
    muscle groups that are due.

    Each item in `exercises` is either explicit — {"name": "Bench Press", "sets":
    [{"target_weight_lbs": 100, "target_reps": 10, "target_rpe": 7}, …]} — or uses the
    shorthand for N identical sets: {"name": "Curls", "set_count": 3, "target_reps": 12,
    "target_weight_lbs": 25, "target_rpe": 8}.

    `target_rpe` (1-10) is the difficulty you're programming for each set — set it from
    your judgment of how hard that set should be, and ramp it across the exercise's sets
    when you intend a build-up, and show it with the plan so the user knows how hard
    each set is meant to feel (≈5 easy, ≈7 solid, ≈9 a grind). Optional.
    Program from the user's active `exercises` (in the briefing). A movement new to them
    — only with their OK — is created on the fly from its `muscles`; a name that can't
    be resolved or created comes back under `unmatched` with what to fix, while the rest
    of the routine lands.

    ONE plan per day. This APPENDS to the plan it lands on rather than creating a second
    one for the same day (focus/notes ignored on an append) — that's the plan for
    `planned_date` if you passed one, the plan named by `workout_id` if you passed that,
    else the next-due plan. `replace=True` discards that plan and starts it fresh. To lay
    out a week, call this once per day with each day's `planned_date`. Returns the full
    plan (see get_workout_plan)."""
    if err := _bad_planned(exercises):
        return err
    if err := _bad_date(planned_date, "planned_date"):
        return err
    with db() as conn:
        if workout_id is not None:
            active = _plan_row(conn, workout_id)
            if not active:
                return {"error": f"no workout with id {workout_id}"}
        elif planned_date:
            # One plan per day: land on the session already planned for that day, if any.
            active = conn.execute(
                "SELECT * FROM workouts WHERE status='active' AND planned_date=? "
                "ORDER BY id ASC LIMIT 1", (planned_date,),
            ).fetchone()
        else:
            active = _current_plan(conn)
        if active and replace:
            conn.execute("DELETE FROM workouts WHERE id=?", (active["id"],))
            active = None
        if active:
            wid = active["id"]
        else:
            # A plan has NO workout_date yet — it's an intended routine, not a thing that
            # happened. `planned_date` says which day it's FOR; the date it's recorded
            # under is stamped only at finish_workout, when the session is actually done
            # (so a plan started late and finished after midnight, or done a day off its
            # schedule, records the day it was completed). '' is the not-yet-done sentinel
            # (the column is NOT NULL); active workouts are excluded from all history /
            # briefing aggregates by status, so the empty date never leaks anywhere.
            wid = conn.execute(
                """INSERT INTO workouts(workout_date, planned_date, focus, notes,
                                        status, created_at)
                   VALUES ('',?,?,?, 'active', ?)""",
                (planned_date, focus, notes, now()),
            ).lastrowid
        results, events = _insert_planned(conn, wid, exercises)
        return _plan_payload(conn, wid, events=events)


@trainer_mcp.tool(annotations=READ_ONLY)
def get_workout_plan(workout_id: Optional[int] = None) -> dict:
    """An active workout plan: each exercise in order with its sets —
    `target_weight_lbs`/`target_reps` (the plan), `weight_lbs`/`reps`/`rpe` (the actuals,
    NULL until done), `status` ('pending'|'done'|'skipped'), and `set_id` (pass to
    complete_set/update_set) — plus a `progress` {done, total} count and the
    `planned_date` it's for. Defaults to the NEXT DUE plan; pass `workout_id` (from
    get_fitness_briefing's `upcoming`) to read a specific day of a planned week.
    Returns {"active": false} when there's no such plan. Read this to see what's left
    and what's been done before logging the next set or adjusting the routine."""
    with db() as conn:
        w = _plan_row(conn, workout_id)
        return _plan_payload(conn, w["id"]) if w else {"active": False}


def complete_set(set_id: int, weight_lbs: Optional[float] = None,
                 reps: Optional[int] = None, rpe: Optional[float] = None,
                 note: Optional[str] = None) -> dict:
    """Mark one planned set done — the legacy web card's one-tap path, a plain helper
    now, not an MCP tool: in a conversation the user reports a whole exercise (or a
    whole session) at once, which is complete_sets. Omitted `weight_lbs`/`reps` default
    to the set's targets. Flips the set to 'done' and returns the updated plan."""
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
        return _plan_payload(conn, r["workout_id"])


@trainer_mcp.tool(annotations=WRITE_IDEMPOTENT)
def complete_sets(sets: list[SetResult]) -> dict:
    """Mark planned sets done, recording what was actually lifted — as many as the user
    just reported, in ONE call ("did all three at 135, the last one was an 8" is three
    items). Omitted `weight_lbs`/`reps` default to the set's targets, so "did it as
    planned" needs only the set_id; add `rpe` (1-10) whenever the user says how it felt —
    that's what you judge the next weight from, so ask if they don't volunteer it.
    Find `set_id`s in get_workout_plan / the last plan return. `weight_lbs` is SIGNED
    (negative = assisted). Validates every set first and writes nothing if any is bad.
    (To CORRECT an already-logged set, use update_set; a set they skipped just stays
    pending and finish_workout marks it skipped.)

    Returns the updated plan, plus `new_prs` when a set beat the heaviest weight ever
    logged for that movement (or tied it for more reps) — tell the user; it's the one
    piece of good news the log can prove."""
    if not sets:
        return {"error": "pass at least one set"}
    with db() as conn:
        rows = {}
        for item in sets:
            r = conn.execute("SELECT * FROM sets WHERE id=?", (item["set_id"],)).fetchone()
            if not r:
                return {"error": f"no set with id {item['set_id']}"}
            w = item.get("weight_lbs")
            rp = item.get("reps")
            w = w if w is not None else r["target_weight_lbs"]
            rp = rp if rp is not None else r["target_reps"]
            if reason := _bad_set({"weight_lbs": w, "reps": rp, "rpe": item.get("rpe")}):
                return {"error": f"set {item['set_id']}: {reason}"}
            rows[item["set_id"]] = (r, w, rp)
        wids = {r["workout_id"] for r, _w, _rp in rows.values()}
        for item in sets:
            r, w, rp = rows[item["set_id"]]
            conn.execute(
                """UPDATE sets SET weight_lbs=?, reps=?, rpe=?, note=COALESCE(?, note),
                   status='done' WHERE id=?""",
                (w, rp, item.get("rpe"), item.get("note"), item["set_id"]),
            )
        prs = _new_bests(conn, list(rows))
        # Normally one session; if the batch spanned several, return the last one's plan
        # and name the rest, rather than silently showing only part of what was written.
        wid = rows[sets[-1]["set_id"]][0]["workout_id"]
        out = _plan_payload(conn, wid)
        if len(wids) > 1:
            out["also_updated_workouts"] = sorted(wids - {wid})
    if prs:
        out["new_prs"] = prs
    return out


@trainer_mcp.tool(annotations=DESTRUCTIVE)
def remove_from_plan(exercise: str, workout_id: Optional[int] = None) -> dict:
    """Drop one exercise from a plan entirely — every set of it, pending AND already
    done — for "take the curls off Thursday" or a movement added by mistake. To replace
    it with a peer instead, use swap_exercise (which keeps done sets in the log); to drop
    a single set, delete_record(kind="set"). Defaults to the next-due plan; pass
    `workout_id` for a specific day. Returns the updated plan."""
    with db() as conn:
        w = _plan_row(conn, workout_id)
        if not w:
            return {"error": "no active workout plan"}
        row = _resolve_exercise(conn, (exercise or "").strip())
        present = row and conn.execute(
            "SELECT 1 FROM sets WHERE workout_id=? AND exercise_id=? LIMIT 1",
            (w["id"], row["id"]),
        ).fetchone()
        if not present:
            return {"error": f"{exercise!r} isn't in this plan",
                    "plan_exercises": [e["name"] for e in
                                       _plan_payload(conn, w["id"])["exercises"]]}
    return remove_plan_exercise(row["id"], workout_id=w["id"])


@trainer_mcp.tool(annotations=DESTRUCTIVE)
def swap_exercise(from_exercise: str, to_exercise: str,
                  sets: Optional[list[PlannedSet]] = None,
                  to_muscles: Optional[list[str]] = None,
                  to_secondary_muscles: Optional[list[str]] = None,
                  workout_id: Optional[int] = None) -> dict:
    """Substitute an exercise in a plan — a busy/broken machine, a tweak, or preference.
    Pick the CLOSEST like-for-like replacement, not just anything that touches the same
    muscle: match the movement pattern (vertical pull→vertical pull, horizontal
    press→horizontal press), the role (compound→compound, isolation→isolation), and
    roughly the loading character. A Lat Pulldown's peer is a Close-/Neutral-Grip
    Pulldown or a Pull-up — NOT a Straight-Arm Pulldown. Prefer one of the user's active
    exercises; if the right peer is new to them, it's created on the fly from
    `to_muscles` (+ `to_secondary_muscles`).

    The PENDING sets of `from_exercise` become 'skipped' (already-done sets stay in the
    log) and `to_exercise` is added with fresh pending sets. By default those mirror the
    swapped-out targets — but those came from a DIFFERENT movement, so pass `sets`
    whenever the right weight differs (it usually does). Returns the updated plan."""
    with db() as conn:
        w = _plan_row(conn, workout_id)
        if not w:
            return {"error": "no active workout plan to swap in"}
        frm = _resolve_exercise(conn, from_exercise)
        if not frm:
            return {"error": f"{from_exercise!r} isn't in this plan"}
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
        spec = [{"name": to_exercise, "sets": sub_sets, "muscles": to_muscles,
                 "secondary_muscles": to_secondary_muscles}]
        if err := _bad_planned(spec):
            return err
        events: dict = {}
        if not _resolve_or_create(conn, spec[0], events):
            return {"error": f"can't swap to {to_exercise!r}", **events}
        conn.execute(
            "UPDATE sets SET status='skipped' WHERE workout_id=? AND exercise_id=? AND status='pending'",
            (w["id"], frm["id"]),
        )
        _results, more = _insert_planned(conn, w["id"], spec)
        for k, v in more.items():
            events.setdefault(k, []).extend(x for x in v if x not in events.get(k, []))
        return _plan_payload(conn, w["id"], events=events)


@trainer_mcp.tool(annotations=WRITE)
def add_to_plan(exercises: list[PlannedExercise], workout_id: Optional[int] = None) -> dict:
    """Append exercises (or extra sets of an exercise already present) to the active
    plan mid-session — e.g. "add some calf raises" or "give me one more drop set". Same
    `exercises` shape as start_workout_plan (so a new movement is created on the fly
    from its `muscles`). Errors if no plan is active. (To drop an exercise, use
    remove_from_plan; to retarget a pending set, update_set; to drop one set,
    delete_record(kind="set").)"""
    if err := _bad_planned(exercises):
        return err
    with db() as conn:
        w = _plan_row(conn, workout_id)
        if not w:
            return {"error": "no active workout plan; start one with start_workout_plan"}
        results, events = _insert_planned(conn, w["id"], exercises)
        return _plan_payload(conn, w["id"], events=events)


@trainer_mcp.tool(annotations=WRITE_IDEMPOTENT)
def reorder_plan(order: list[str], workout_id: Optional[int] = None) -> dict:
    """Reorder the exercises in the active plan. `order` is the exercise names in the
    sequence you want them done, e.g. ["Squat", "Bench Press", "Curls"] — names resolve
    against the plan's exercises fuzzily (same matching as everywhere). Any plan exercise
    you leave out keeps its place after the ones you listed. Use this when the user asks to
    move a lift earlier/later or to lay the session out in a particular order (warm-up
    compounds first, accessories last). Returns the updated plan in the new order."""
    with db() as conn:
        w = _plan_row(conn, workout_id)
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
    history (counting toward recency/PRs). Optionally
    record overall `feeling`/`notes`. If nothing was completed, the empty session is
    deleted instead. Returns a short summary."""
    with db() as conn:
        w = _plan_row(conn, workout_id)
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
# Bodyweight — imported from a connected scale's export (see below)
# --------------------------------------------------------------------------- #

# Bodyweight readings are IMPORTED, never typed. The user weighs in every morning on
# a connected scale, which writes to the scale vendor's own app; every so often that app
# exports a spreadsheet and the user uploads it on /weight. There is deliberately no
# other door — no form, no MCP write tool, no correcting a row by id — because a reading
# is now a fact produced by a device, and a second way to state it is a second version
# of the truth. To fix a bad reading, fix it at the scale's app and re-export.
#
# The one thing that has to hold under that model is IDEMPOTENCE: exports overlap (the
# user re-downloads the last 30 days, not the delta), so importing the same reading
# twice must insert once. `source_key` — the reading's own timestamp in the export —
# is what carries that, and it's why a re-upload reports `skipped` rather than doubling
# the log and quietly halving every "lowest ever" it disagrees with.

SCALE_EXPORT_SOURCE = "wyze"

# Column headers the export may carry, matched case-insensitively by PREFIX so a
# trailing unit ("Weight(lb)") or a vendor's spacing doesn't have to be guessed exactly.
_SCALE_DATE_HEADERS = ("date and time", "date/time", "date")
_SCALE_LB_HEADERS = ("weight(lb", "weight (lb")
_SCALE_KG_HEADERS = ("weight(kg", "weight (kg")

# "2026.08.22 06:39 AM" is what the export writes; the others are cheap insurance
# against a locale or a vendor update, since a stamp we can't parse costs the row.
_SCALE_TIME_FORMATS = (
    "%Y.%m.%d %I:%M %p", "%Y.%m.%d %H:%M", "%Y.%m.%d %I:%M:%S %p",
    "%Y-%m-%d %I:%M %p", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
    "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",      # a spreadsheet tool's ISO rendering
)

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _xlsx_rows(blob: bytes) -> list[list[str]]:
    """Every cell of an .xlsx's first worksheet, as strings, row by row.

    Hand-rolled on zipfile + the stdlib XML parser rather than pulling in openpyxl: the
    file is one small sheet of text, and this repo's whole shape is stdlib + three pins.
    Handles the two ways a string reaches a cell (an inline `<is>`, which is what the
    scale export writes, and a `<v>` index into sharedStrings, which most other writers
    use) and pads short rows out to their column letter, so a blank cell doesn't shift
    every value after it one column left.
    """
    import xml.etree.ElementTree as ET

    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = z.namelist()
        sheet = next((n for n in names if n.startswith("xl/worksheets/sheet")), None)
        if sheet is None:
            raise ValueError("no worksheet in this file")
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")):
                shared.append("".join(t.text or "" for t in si.iter(f"{NS}t")))
        root = ET.fromstring(z.read(sheet))

    def col_index(ref: str) -> int:
        n = 0
        for ch in ref:
            if not ch.isalpha():
                break
            n = n * 26 + (ord(ch.upper()) - 64)
        return n - 1

    rows: list[list[str]] = []
    for row in root.iter(f"{NS}row"):
        cells: list[str] = []
        for c in row.iter(f"{NS}c"):
            i = col_index(c.get("r") or "")
            if i < 0:
                i = len(cells)
            while len(cells) <= i:
                cells.append("")
            if c.get("t") == "s":                       # sharedStrings index
                v = c.find(f"{NS}v")
                idx = int(v.text) if v is not None and v.text else -1
                cells[i] = shared[idx] if 0 <= idx < len(shared) else ""
            else:                                        # inline string or plain value
                cells[i] = "".join(t.text or "" for t in c.iter(f"{NS}t")) or \
                           "".join(v.text or "" for v in c.iter(f"{NS}v"))
        rows.append(cells)
    return rows


def _parse_scale_export(blob: bytes) -> list[dict] | dict:
    """A connected-scale export → [{source_key, weigh_date, weight_lbs, at}], newest last.

    Header-DRIVEN rather than positional: the export leads with a merged title row, and a
    vendor that adds a body-composition column would silently shift a positional read onto
    the wrong number. So the header row is located by the one label that must be there
    (a date column), and weight is taken from whichever unit column exists — pounds when
    offered, kilograms converted when not, since a metric export is still a weigh-in.

    Everything past those two columns is DROPPED. The scale measures body fat, muscle
    mass, BMR and a dozen more, and none of them have anywhere to live here: `body_weight`
    is a weight log, the graph plots weight, and storing a column nothing reads is the
    dormant-data trap this repo has been bitten by before (see the `drinks` table).
    """
    try:
        rows = _xlsx_rows(blob)
    except ValueError as e:
        return {"error": str(e)}
    except Exception:
        return {"error": "could not read that file — it doesn't look like an .xlsx export"}

    head_i = date_i = lb_i = kg_i = None
    for i, row in enumerate(rows[:20]):
        cells = [(c or "").strip().lower() for c in row]
        d = next((j for j, c in enumerate(cells) if c in _SCALE_DATE_HEADERS), None)
        if d is None:
            continue
        head_i, date_i = i, d
        lb_i = next((j for j, c in enumerate(cells)
                     if c.startswith(_SCALE_LB_HEADERS)), None)
        kg_i = next((j for j, c in enumerate(cells)
                     if c.startswith(_SCALE_KG_HEADERS)), None)
        break
    if head_i is None:
        return {"error": "no 'Date and Time' column found — is this a scale export?"}
    if lb_i is None and kg_i is None:
        return {"error": "no weight column found (expected 'Weight(lb)' or 'Weight(kg)')"}

    out, seen = [], set()
    for row in rows[head_i + 1:]:
        stamp = (row[date_i] if date_i < len(row) else "").strip()
        raw = ""
        if lb_i is not None and lb_i < len(row):
            raw = (row[lb_i] or "").strip()
        to_lbs = 1.0
        if not raw and kg_i is not None and kg_i < len(row):
            raw, to_lbs = (row[kg_i] or "").strip(), 2.20462
        if not stamp or not raw:
            continue
        m = _NUM_RE.search(raw)                      # "185.4lb" → 185.4
        at = _parse_scale_stamp(stamp)
        if m is None or at is None:
            continue
        lbs = round(float(m.group()) * to_lbs, 1)
        if lbs <= 0:
            continue
        key = f"{SCALE_EXPORT_SOURCE}:{stamp}"
        if key in seen:                              # a file that repeats a stamp
            continue
        seen.add(key)
        # The stamp is the scale app's LOCAL time, which is the user's own — so its
        # calendar day IS the Pacific day, with no conversion to do or to get wrong.
        out.append({"source_key": key, "weigh_date": at.strftime("%Y-%m-%d"),
                    "weight_lbs": lbs, "at": at})
    out.sort(key=lambda r: r["at"])
    return out


def _parse_scale_stamp(value: str) -> Optional[datetime]:
    """The export's local timestamp → datetime, or None if no known format fits (that
    row is skipped rather than guessed at — a misread date is a reading on the wrong
    day, which is worse than a reading that never arrives)."""
    for fmt in _SCALE_TIME_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def import_bodyweight(blob: bytes) -> dict:
    """Load a connected-scale export into the weigh-in log. A plain helper, NOT an MCP
    tool — website-only, like set_archived: the user uploads a
    file on /weight, and the model has no file to hand and no business inventing one.

    Idempotent by `source_key`, so overlapping exports are the expected case rather than
    a hazard: re-uploading last month's file inserts nothing and says so. Returns
    `imported`/`skipped` counts, the date span of what landed, the latest reading, and
    `new_low` when one of the NEW readings beats every reading that was already there —
    the page's confetti cue, and the one fact the browser cannot derive from the rows it
    is about to re-render, since it never sees the all-time minimum.
    """
    parsed = _parse_scale_export(blob)
    if isinstance(parsed, dict):
        return parsed
    if not parsed:
        return {"error": "no readings found in that file"}
    return _insert_readings(parsed)


def _insert_readings(parsed: list[dict]) -> dict:
    """The write half both import doors share (the file upload and import_weigh_ins):
    INSERT OR IGNORE on source_key, then the counts + new_low report."""
    with db() as conn:
        before = conn.execute("SELECT MIN(weight_lbs) AS m FROM body_weight").fetchone()["m"]
        stamp, imported = now(), []
        for r in parsed:
            cur = conn.execute(
                """INSERT OR IGNORE INTO body_weight
                       (weigh_date, weight_lbs, note, source_key, created_at)
                   VALUES (?,?,?,?,?)""",
                (r["weigh_date"], r["weight_lbs"], None, r["source_key"], stamp),
            )
            if cur.rowcount:
                imported.append(r)

    out = {"imported": len(imported), "skipped": len(parsed) - len(imported),
           "readings": len(parsed)}
    if imported:
        out["first_date"] = imported[0]["weigh_date"]
        out["last_date"] = imported[-1]["weigh_date"]
        out["latest_lbs"] = imported[-1]["weight_lbs"]
        low = min(r["weight_lbs"] for r in imported)
        # Strictly less than, and never on a first-ever import (nothing to beat) — the
        # same rule the old hand-entry path applied one reading at a time.
        if before is not None and low < before:
            out["new_low"] = True
    return out


@trainer_mcp.tool(annotations=WRITE_IDEMPOTENT)
def import_weigh_ins(readings: list[ScaleReading]) -> dict:
    """Load weigh-ins from the user's connected-scale export — the file their scale's
    app produces, which they'll attach to the conversation. Read the sheet, and pass one
    item per row: the date-and-time cell as `stamp` (e.g. "2026.08.22 06:39 AM"; an
    ISO "2026-08-22 06:39:00" is fine too) and its weight column as `weight_lbs` (or
    `weight_kg` for a metric export). Ignore every other column (body fat, BMR, …) —
    this is a weight log.

    The stamp, to the minute, is the reading's identity, which is what makes this
    IDEMPOTENT: exports overlap (the app hands over "the last 30 days"), so pass the
    WHOLE sheet every time and only readings not already on file land — `imported: 0`
    on a re-send is the normal case, not an error. So a stamp is always the export's
    own, never estimated or made up.

    This is the ONLY way a weigh-in gets written. A number the user just SAYS ("I was
    184 this morning") is not one — readings come from the scale, and a wrong one is
    fixed in the scale's app and re-exported. Returns `imported`/`skipped` counts, the
    date span that landed, `latest_lbs`, `new_low` when a new reading beat the all-time
    low, and `unparsed` for any stamp that wasn't a recognisable date (those are
    skipped rather than guessed at)."""
    parsed, unparsed = [], []
    for r in readings or []:
        stamp = (r.get("stamp") or "").strip()
        at = _parse_scale_stamp(stamp) if stamp else None
        lbs = r.get("weight_lbs")
        if lbs is None and r.get("weight_kg") is not None:
            lbs = round(float(r["weight_kg"]) * 2.20462, 1)
        if at is None or lbs is None:
            unparsed.append(stamp or r)
            continue
        if not 50 <= float(lbs) <= 700:
            unparsed.append(stamp)
            continue
        # The key is RE-RENDERED in the export's own format rather than taken as
        # given: a spreadsheet reader may hand the cell back as "2026-08-22 06:39:00",
        # and the same reading under a second spelling would store twice — including
        # against rows the old file-upload path keyed as "wyze:2026.08.22 06:39 AM".
        key = f"{SCALE_EXPORT_SOURCE}:{at.strftime('%Y.%m.%d %I:%M %p')}"
        parsed.append({"source_key": key,
                       "weigh_date": at.date().isoformat(),
                       "weight_lbs": round(float(lbs), 1), "at": at})
    if not parsed:
        return {"error": "no usable readings — pass each row's date-and-time cell as "
                         "`stamp` and its weight", "unparsed": unparsed}
    parsed.sort(key=lambda r: r["at"])
    out = _insert_readings(parsed)
    if unparsed:
        out["unparsed"] = unparsed
    return out


@trainer_mcp.tool(annotations=READ_ONLY)
def get_fitness_briefing(recent_workouts: int = 5, as_of: Optional[str] = None) -> dict:
    """One-call trainer context. Returns the stored `profile` (goals, split, session,
    injuries, coaching — see this server's instructions; read it as instruction),
    per-muscle recency (days since each muscle was last trained + sets in the last 7
    days), a cardio rollup (per cardio exercise: days since last done + minutes/miles
    in the last 7 days), recent sessions (each with its `notes` — read them, a niggle
    logged last time is a caution this time), `bodyweight` (latest reading, days since,
    and 30-day change; negative = down), `intake_today` (water/protein so far),
    `upcoming` (sessions already PLANNED and not yet
    done — workout_id, planned_date, focus, exercise names, set count — next-due first),
    `exercises` — the user's ACTIVE exercises (muscles, last_done, sessions, note) — and
    `archived_exercises` (how many are in the archive; list_exercises shows them). Call this at the
    start of a training conversation to decide what to work and what to rest: muscles with
    the most days_since (and low recent volume) are recovered and due; ones trained in the
    last ~1-2 days should rest. Cardio is tracked separately because it carries no muscle
    mapping. BUILD SESSIONS FROM `exercises` — it's what the user actually trains; don't
    bring in anything else without asking. The recommendation itself is yours to make
    from this data.

    `as_of` is the day you're planning FOR (YYYY-MM-DD), defaulting to today. When the
    user wants TOMORROW's session, pass tomorrow's date (today + 1; today is in `now`):
    `days_since` then counts recovery as of that day — a muscle trained today reads 0 in a
    today briefing but 1 in a tomorrow one — so what's "due" already reflects the extra
    rest. Planning a WHOLE WEEK is that one day at a time, re-briefing per day.

    `muscle_recency` counts COMPLETED work only, so it can't see the days you've already
    programmed this week — that's what `upcoming` is for. Read it alongside the recency
    numbers: a muscle that reads "due" may already be booked for Wednesday.

    `intake_today` is today's water/protein totals with their `targets` (see
    log_intake) — always TODAY, whatever `as_of` says, since it's where the day
    stands rather than anything to plan from."""
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
        # The user's ACTIVE exercises — the pool the model programs from (the archive is
        # list_exercises' job; only its size rides along here).
        active = _exercise_rows(conn, False)
        archived_count = conn.execute(
            "SELECT COUNT(*) AS n FROM exercises WHERE archived=1").fetchone()["n"]
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
        # Sessions already PLANNED but not yet done, next-due first (same ordering as
        # _current_plan). They carry no completed sets, so they're invisible to
        # muscle_recency above — read them before programming another day of the week,
        # or you'll program the same muscles twice.
        upcoming_out = []
        for w in conn.execute(
            """SELECT id, planned_date, focus FROM workouts WHERE status='active'
               ORDER BY COALESCE(NULLIF(planned_date,''), ?) ASC, id ASC""",
            (ref,),
        ).fetchall():
            names = [r["name"] for r in conn.execute(
                """SELECT DISTINCT e.name FROM sets s JOIN exercises e ON e.id=s.exercise_id
                   WHERE s.workout_id=? AND s.status!='skipped' ORDER BY e.name""",
                (w["id"],),
            )]
            n = conn.execute(
                "SELECT COUNT(*) AS s FROM sets WHERE workout_id=? AND status!='skipped'",
                (w["id"],),
            ).fetchone()
            upcoming_out.append({"workout_id": w["id"], "planned_date": w["planned_date"],
                                 "focus": w["focus"], "exercises": names, "sets": n["s"]})
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
        intake_today = _intake_today(conn)
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
            "upcoming": upcoming_out, "exercises": active,
            "archived_exercises": archived_count,
            "intake_today": intake_today}


@trainer_mcp.tool(annotations=WRITE_IDEMPOTENT)
def update_profile(profile: dict) -> dict:
    """Merge keys into the user's profile — the one place their goals, split,
    session size, injuries and coaching preferences live (which keys, and when
    `coaching` may change, is in this server's instructions). Pass only the keys you're
    changing; each one REPLACES its old value wholesale, so to edit a key, read it from
    the briefing and send the whole new value. A key set to null is removed. E.g.
    {"injuries": "left shoulder: no overhead pressing"},
    {"split": "Mon upper / Wed lower / Fri full body"},
    {"session": "about an hour, 5-6 exercises"}. Returns the full profile."""
    with db() as conn:
        current = _get_profile(conn)
        current.update(profile)
        current = {k: v for k, v in current.items() if v is not None}
        conn.execute(
            """INSERT INTO settings(key, value) VALUES ('profile', ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (json.dumps(current),),
        )
    return {"profile": current}


def set_trainer_profile(coaching: Optional[str]) -> dict:
    """Set the trainer's `coaching` text from the WEBSITE — /trainer's Coaching
    popover. A NON-tool, website-only path like
    import_bodyweight: never a FastMCP tool, so no connector can reach it.

    It writes the SAME place update_profile does (settings → `profile` →
    `coaching`), because the point is one text feeding both connectors and the
    in-app chat — a second door onto it, not a second copy. It's a door worth
    having for the same reason the Targets popover is: this is the screen where you
    notice the coaching is off, and the alternative was editing a Python string and
    redeploying.

    Blank DROPS the key rather than storing an empty instruction (the trainer then
    asks about coaching preferences next time, per its instructions).
    Nothing else in the profile (injury, split, goals) is touched. There is no
    validation beyond that: it's prose for a model to read, and the one thing a
    guard could check — that the user meant it — is exactly what typing it means."""
    if coaching is not None and not isinstance(coaching, str):
        return {"error": f"coaching must be text, got {coaching!r}"}
    with db() as conn:
        profile = _get_profile(conn)
        if (coaching or "").strip():
            profile["coaching"] = coaching.strip()
        else:
            profile.pop("coaching", None)
        conn.execute(
            """INSERT INTO settings(key, value) VALUES ('profile', ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (json.dumps(profile),),
        )
    return {"coaching": profile.get("coaching", "")}


@trainer_mcp.tool(name="delete_record", annotations=DESTRUCTIVE)
def delete_training_record(kind: str, id: int) -> dict:
    """Permanently delete one training record. Irreversible — confirm first.

    `kind` selects what `id` refers to:
      - "workout" — a whole session (all its sets go too).
      - "set"     — one logged set (remaining sets for that exercise are renumbered
                    so set_index stays contiguous).
      - "intake"  — one logged water/protein item; the day's totals re-derive.
    Find workout/set ids with get_fitness_briefing or get_exercise_history, intake
    item ids with get_intake (or the `item_id` log_intake returned).

    Weigh-ins are NOT deletable here — they come from the scale's export and are
    corrected at the scale's app, then re-exported."""
    if kind not in ("workout", "set", "intake"):
        return {"error": f"unknown kind {kind!r}; this server deletes one of "
                         "['intake', 'set', 'workout']"}
    return _delete_record("intake_item" if kind == "intake" else kind, id)


if __name__ == "__main__":
    init_db()
    # The trainer is the one MCP server (the journal instance is an in-process tool
    # registry for the web app's chat, not an endpoint). HTTP mode here is for
    # smoke tests; in production webapp/combined.py serves it with the UI.
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "http":
        trainer_mcp.run(transport="http",
                        host=os.environ.get("MCP_HOST", "0.0.0.0"),
                        port=int(os.environ.get("PORT", "8000")),
                        path="/mcp")
    else:
        trainer_mcp.run()
