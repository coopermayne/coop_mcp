"""
Journal MCP server.

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


def _build_auth():
    """Return a Google OAuth provider if creds are set, else None (authless).

    Authless is for local dev / staging with dummy data only. Set GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET, PUBLIC_URL, and JOURNAL_ALLOWED_EMAILS in Coolify to protect
    the server before putting real entries in.
    """
    cid = os.environ.get("GOOGLE_CLIENT_ID")
    csec = os.environ.get("GOOGLE_CLIENT_SECRET")
    base = os.environ.get("PUBLIC_URL")  # e.g. https://journal.yourdomain.com
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
Single-user life log with three domains: a conversational journal (people are
resolved to stable entities, not name strings), a drinking tracker, and a personal
trainer (workouts + exercise catalog). The server only stores and matches — all
judgment (which person a mention means, next weight, what to program) is yours.

Two rules: capture never blocks — always save, leave ambiguous mentions pending for
later; and resolve mentions to person entities, don't normalize names in text.

All dates in this log are Pacific (America/Los_Angeles) — the user lives and logs
on Pacific time. Both briefings return `now` (current Pacific date/time); anchor
"today"/"yesterday" to it before defaulting or computing any date.

Start a session with get_briefing (people/journal) and/or get_fitness_briefing
(training) to load context before acting.""")
if _auth is not None:
    mcp.add_middleware(AllowlistMiddleware())


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
-- Drinking tracker. One row per logged drinking occasion; days with no row
-- are sober days. Aggregation (daily totals, streaks) is deterministic SQL.
-- ----------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS drinks (
    id              INTEGER PRIMARY KEY,
    drink_date      TEXT NOT NULL,    -- YYYY-MM-DD the drinks were consumed
    standard_drinks REAL NOT NULL,    -- in standard-drink units (beer/wine ~1, cocktail ~1.5)
    kind            TEXT,             -- optional: "beer", "wine", "cocktail"
    notes           TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drinks_date ON drinks(drink_date);

-- ----------------------------------------------------------------------- --
-- Personal trainer. `exercises` is a catalog of stable exercise ENTITIES
-- (like people): technique lives here so the model can coach form. Muscles
-- are normalized into a child table so "what's rested vs worked" is a plain
-- SQL aggregate, not an LLM guess. `workouts`/`sets` are the two-level log
-- (session + per-set weight/reps/rpe), mirroring entries/mentions. The
-- server stores and retrieves; progression judgment happens in conversation.
-- ----------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS exercises (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    category        TEXT,             -- 'strength' | 'cardio' | 'prehab' | 'mobility'
    equipment       TEXT,
    technique_notes TEXT,
    common_mistakes TEXT,
    cautions        TEXT,             -- injury / shoulder considerations
    video_link      TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exercise_muscles (
    exercise_id INTEGER NOT NULL REFERENCES exercises(id) ON DELETE CASCADE,
    muscle      TEXT NOT NULL,        -- canonical lowercase, e.g. 'chest', 'lats', 'quads'
    role        TEXT NOT NULL DEFAULT 'primary',  -- 'primary' | 'secondary'
    PRIMARY KEY (exercise_id, muscle)
);
CREATE INDEX IF NOT EXISTS idx_exmuscle_muscle ON exercise_muscles(muscle);

CREATE TABLE IF NOT EXISTS workouts (
    id           INTEGER PRIMARY KEY,
    workout_date TEXT NOT NULL,       -- YYYY-MM-DD
    focus        TEXT,                -- "Legs", "Arms", "Cardio"
    feeling      TEXT,                -- overall how the session felt
    notes        TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workouts_date ON workouts(workout_date);

CREATE TABLE IF NOT EXISTS sets (
    id          INTEGER PRIMARY KEY,
    workout_id  INTEGER NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
    exercise_id INTEGER NOT NULL REFERENCES exercises(id),
    set_index   INTEGER NOT NULL,     -- 1-based order within the exercise
    weight_lbs  REAL,                 -- NULL for bodyweight / cardio
    reps        INTEGER,
    rpe         REAL,                 -- 1-10 perceived exertion (10 = true failure)
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_sets_workout ON sets(workout_id);
CREATE INDEX IF NOT EXISTS idx_sets_exercise ON sets(exercise_id);

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

    Write `body` as a clean, structured, concise journal entry: organize the
    free-association into readable prose, keep the substance and the user's voice,
    drop filler. Pass the user's original words verbatim as `raw_body` so a faithful
    record is retained underneath (retrievable via get_entry; not shown in normal
    search or history). Extract the people referenced and pass each as a short
    surface form — what was actually SAID ("Tom", "Dad", a garbled transcription),
    taken from the raw words, not the cleaned-up name.

    Resolution guidance for the model after this returns:
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
        mentions: Surface forms of people referenced, e.g. ["Tom", "Hallie"].
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
                 body: Optional[str] = None, raw_body: Optional[str] = None) -> dict:
    """Edit an existing journal entry. Only non-null args are written.

    Use `entry_date` (YYYY-MM-DD, Pacific) to correct the day an entry is ABOUT —
    e.g. the user said "that was actually yesterday". Dates are Pacific time; resolve
    relative phrases ("yesterday") against the current Pacific date (see get_briefing's
    `now`) before passing a concrete date here. `body` replaces the cleaned journal
    text; `raw_body` replaces the verbatim original. This does NOT re-run people
    matching — to fix who's mentioned, leave the mention pending / use link_mentions."""
    if err := _bad_date(entry_date, "entry_date"):
        return err
    fields = {"entry_date": entry_date, "body": body, "raw_body": raw_body}
    sets = {k: v for k, v in fields.items() if v is not None}
    if not sets:
        return {"entry_id": entry_id, "updated": []}
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM entries WHERE id=?", (entry_id,)).fetchone()
        if not exists:
            return {"error": f"no entry with id {entry_id}"}
        cols = ", ".join(f"{k}=?" for k in sets)
        conn.execute(f"UPDATE entries SET {cols} WHERE id=?", (*sets.values(), entry_id))
    return {"entry_id": entry_id, "updated": list(sets)}


@mcp.tool()
def delete_record(kind: str, id: int) -> dict:
    """Permanently delete one record. Irreversible — confirm with the user first.

    `kind` selects what `id` refers to:
      - "entry"   — a journal entry (its mentions go too; FTS stays in sync).
      - "drink"   — one logged drink row (a day with no rows left is sober again).
      - "workout" — a whole training session (all its sets go too).
      - "set"     — one logged set (remaining sets for that exercise are renumbered
                    so set_index stays contiguous).
    Find ids with the matching read: get_entry/search_entries, get_drink_summary
    (include_rows=True), get_fitness_briefing, get_exercise_history."""
    tables = {"entry": "entries", "drink": "drinks", "workout": "workouts", "set": "sets"}
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

# Canonical muscle vocabulary — kept small and consistent so recency/volume
# aggregates line up. The model should map onto these labels when logging.
MUSCLES = [
    "chest", "upper back", "lats", "traps", "shoulders", "biceps", "triceps",
    "forearms", "abs", "obliques", "lower back", "glutes", "quads",
    "hamstrings", "calves",
]


def _days_since(d: Optional[str]) -> Optional[int]:
    if not d:
        return None
    try:
        return (date.fromisoformat(today()) - date.fromisoformat(d)).days
    except ValueError:
        return None


def _resolve_exercise(conn: sqlite3.Connection, name: str):
    """Case-insensitive lookup of a catalog exercise by name. Returns row or None."""
    return conn.execute(
        "SELECT * FROM exercises WHERE lower(name)=lower(?)", (name.strip(),)
    ).fetchone()


def _muscles_for(conn: sqlite3.Connection, exercise_id: int) -> dict:
    rows = conn.execute(
        "SELECT muscle, role FROM exercise_muscles WHERE exercise_id=? ORDER BY role, muscle",
        (exercise_id,),
    ).fetchall()
    return {
        "primary": [r["muscle"] for r in rows if r["role"] == "primary"],
        "secondary": [r["muscle"] for r in rows if r["role"] == "secondary"],
    }


def _set_muscles(conn: sqlite3.Connection, exercise_id: int,
                 primary: Optional[list[str]], secondary: Optional[list[str]]) -> None:
    """Replace an exercise's muscle links (only when at least one list is given)."""
    if primary is None and secondary is None:
        return
    conn.execute("DELETE FROM exercise_muscles WHERE exercise_id=?", (exercise_id,))
    for role, names in (("primary", primary or []), ("secondary", secondary or [])):
        for m in names:
            m = m.strip().lower()
            if m:
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
    ~2.0. "Two beers and a glass of wine" -> standard_drinks=3.0. You can call this
    more than once for the same day (rows accumulate); days with no row are sober.

    Args:
        standard_drinks: Total standard drinks for this occasion.
        drink_date: Day consumed, YYYY-MM-DD. Defaults to today.
        kind: Optional label, e.g. "beer", "wine", "cocktail".
        notes: Optional context, e.g. "dinner with Hallie".
    """
    if err := _bad_date(drink_date, "drink_date"):
        return err
    if standard_drinks <= 0:
        return {"error": f"standard_drinks must be positive, got {standard_drinks}"}
    d = drink_date or today()
    with db() as conn:
        rid = conn.execute(
            """INSERT INTO drinks(drink_date, standard_drinks, kind, notes, created_at)
               VALUES (?,?,?,?,?)""",
            (d, standard_drinks, kind, notes, now()),
        ).lastrowid
        day_total = conn.execute(
            "SELECT COALESCE(SUM(standard_drinks),0) AS t FROM drinks WHERE drink_date=?",
            (d,),
        ).fetchone()["t"]
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
    """Correct a logged drink row. Only non-null args are written.

    Use this to fix a mistake in either direction — e.g. lower `standard_drinks`
    from 3 to 1 (logging can only add, so corrections downward must go through here),
    or move it to the right day with `drink_date` (YYYY-MM-DD, Pacific). Find the
    `drink_id` with get_drink_summary(include_rows=True). To remove a row entirely use
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
        cols = ", ".join(f"{k}=?" for k in sets)
        conn.execute(f"UPDATE drinks SET {cols} WHERE id=?", (*sets.values(), drink_id))
    return {"drink_id": drink_id, "updated": list(sets)}


# --------------------------------------------------------------------------- #
# Trainer: exercise catalog
# --------------------------------------------------------------------------- #

@mcp.tool()
def save_exercise(name: Optional[str] = None, exercise_id: Optional[int] = None,
                  category: Optional[str] = None, equipment: Optional[str] = None,
                  muscles: Optional[list[str]] = None,
                  secondary_muscles: Optional[list[str]] = None,
                  technique_notes: Optional[str] = None,
                  common_mistakes: Optional[str] = None,
                  cautions: Optional[str] = None,
                  video_link: Optional[str] = None) -> dict:
    """Create or enrich a catalog exercise (a stable entity, like a person) — the one
    write tool for the catalog.

    Target it by `exercise_id`, or by `name` (resolved case-insensitively): a KNOWN
    name updates that exercise, an UNKNOWN name creates it. `log_workout` already
    auto-stubs unknown lifts, so the usual job here is enriching one with coaching
    content — how to do it (`technique_notes`), what to watch for (`common_mistakes`),
    injury caveats (`cautions`, e.g. the user's left-shoulder limits). Only non-null
    scalar fields are written. Providing `muscles` and/or `secondary_muscles` REPLACES
    those links wholesale (pass both to set both). Use the canonical muscle labels so
    recency lines up: chest, upper back, lats, traps, shoulders, biceps, triceps,
    forearms, abs, obliques, lower back, glutes, quads, hamstrings, calves. Returns the
    exercise_id and whether it was newly created."""
    with db() as conn:
        if exercise_id is not None:
            row = conn.execute("SELECT id FROM exercises WHERE id=?", (exercise_id,)).fetchone()
            if not row:
                return {"error": f"no exercise with id {exercise_id}"}
        elif name:
            row = _resolve_exercise(conn, name)
        else:
            return {"error": "pass name or exercise_id"}
        if row:
            eid = row["id"]
            created = False
            fields = {"category": category, "equipment": equipment,
                      "technique_notes": technique_notes, "common_mistakes": common_mistakes,
                      "cautions": cautions, "video_link": video_link}
            sets_ = {k: v for k, v in fields.items() if v is not None}
            if sets_:
                cols = ", ".join(f"{k}=?" for k in sets_)
                conn.execute(f"UPDATE exercises SET {cols} WHERE id=?", (*sets_.values(), eid))
        else:
            eid = conn.execute(
                """INSERT INTO exercises(name, category, equipment, technique_notes,
                   common_mistakes, cautions, video_link, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (name.strip(), category, equipment, technique_notes, common_mistakes,
                 cautions, video_link, now()),
            ).lastrowid
            created, sets_ = True, {}
        _set_muscles(conn, eid, muscles, secondary_muscles)
        out_name = conn.execute("SELECT name FROM exercises WHERE id=?", (eid,)).fetchone()["name"]
    if created:
        return {"exercise_id": eid, "name": out_name, "created": True}
    return {"exercise_id": eid, "name": out_name, "created": False,
            "updated": list(sets_)
                       + (["muscles"] if (muscles is not None or secondary_muscles is not None) else [])}


@mcp.tool()
def exercises(name: Optional[str] = None, exercise_id: Optional[int] = None,
              muscle: Optional[str] = None, equipment: Optional[str] = None,
              category: Optional[str] = None) -> dict:
    """Read the exercise catalog — one full record, or a filtered list.

    Pass `name` or `exercise_id` to get ONE exercise in full, including technique
    notes, common mistakes, and cautions, so you can coach proper form. Otherwise it
    returns the compact registry (id, name, category, equipment, muscles); narrow it
    with `muscle`, an `equipment` fragment, or `category` to pick exercises for a
    session."""
    with db() as conn:
        if name is not None or exercise_id is not None:
            if exercise_id is not None:
                r = conn.execute("SELECT * FROM exercises WHERE id=?", (exercise_id,)).fetchone()
            else:
                r = _resolve_exercise(conn, name)
            if not r:
                return {"error": "no matching exercise"}
            m = _muscles_for(conn, r["id"])
            return {"exercise_id": r["id"], "name": r["name"], "category": r["category"],
                    "equipment": r["equipment"], "muscles": m,
                    "technique_notes": r["technique_notes"],
                    "common_mistakes": r["common_mistakes"], "cautions": r["cautions"],
                    "video_link": r["video_link"]}
        rows = conn.execute("SELECT * FROM exercises ORDER BY name").fetchall()
        out = []
        for r in rows:
            m = _muscles_for(conn, r["id"])
            if muscle and muscle.strip().lower() not in (m["primary"] + m["secondary"]):
                continue
            if equipment and (not r["equipment"] or equipment.lower() not in r["equipment"].lower()):
                continue
            if category and r["category"] != category:
                continue
            out.append({"exercise_id": r["id"], "name": r["name"],
                        "category": r["category"], "equipment": r["equipment"],
                        "muscles": m})
    return {"exercises": out, "count": len(out)}


# --------------------------------------------------------------------------- #
# Trainer: logging + retrieval
# --------------------------------------------------------------------------- #

@mcp.tool()
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
          "muscles": ["quads", "glutes"],   # optional; seeds the catalog if NEW
          "sets": [
            {"weight_lbs": 180, "reps": 10, "rpe": 7, "note": ""},
            {"weight_lbs": 180, "reps": 10, "rpe": 8},
            {"weight_lbs": 180, "reps": 8,  "rpe": 9.5, "note": "last one was a grind"}
          ]
        }
    Names are resolved against the catalog case-insensitively; an unknown name is
    auto-stubbed as a new exercise (using `muscles` if given) so logging never
    blocks — flag any `created` exercises to the user afterward so they can add
    technique notes via save_exercise. weight_lbs is null for bodyweight/cardio.
    rpe is 1-10 perceived exertion (10 = couldn't do another rep): it's how you
    judge whether to add weight next time. Returns per-exercise ids and which were
    newly created.

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
        results = []
        for ex in exercises:
            name = (ex.get("name") or "").strip()
            if not name:
                continue
            row = _resolve_exercise(conn, name)
            created = False
            if row:
                eid = row["id"]
            else:
                eid = conn.execute(
                    "INSERT INTO exercises(name, created_at) VALUES (?,?)",
                    (name, now()),
                ).lastrowid
                _set_muscles(conn, eid, ex.get("muscles"), None)
                created = True
            # continue set numbering if this exercise already has sets in the session
            start = (conn.execute(
                "SELECT COALESCE(MAX(set_index),0) AS m FROM sets WHERE workout_id=? AND exercise_id=?",
                (wid, eid),
            ).fetchone()["m"]) + 1
            for i, s in enumerate(ex.get("sets") or [], start=start):
                conn.execute(
                    """INSERT INTO sets(workout_id, exercise_id, set_index, weight_lbs,
                       reps, rpe, note) VALUES (?,?,?,?,?,?,?)""",
                    (wid, eid, i, s.get("weight_lbs"), s.get("reps"),
                     s.get("rpe"), s.get("note")),
                )
            results.append({"exercise_id": eid, "name": name,
                            "sets": len(ex.get("sets") or []), "created": created})
    return {"workout_id": wid, "workout_date": wd, "exercises": results,
            "new_exercises": [r["name"] for r in results if r["created"]],
            "appended": workout_id is not None}


@mcp.tool()
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
                return {"error": f"no exercise named {name!r}"}
            exercise_id = r["id"]
        ex = conn.execute("SELECT name FROM exercises WHERE id=?", (exercise_id,)).fetchone()
        if not ex:
            return {"error": "no matching exercise"}
        rows = conn.execute(
            """SELECT w.id AS wid, w.workout_date, s.id AS sid, s.set_index,
                      s.weight_lbs, s.reps, s.rpe, s.note
               FROM sets s JOIN workouts w ON w.id = s.workout_id
               WHERE s.exercise_id=?
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
        sess["sets"].append({"set_id": r["sid"], "weight_lbs": r["weight_lbs"],
                             "reps": r["reps"], "rpe": r["rpe"], "note": r["note"]})
    return {"exercise_id": exercise_id, "name": ex["name"],
            "sessions": sessions[:limit], "count": min(len(sessions), limit)}


@mcp.tool()
def update_workout(workout_id: int, workout_date: Optional[str] = None,
                   focus: Optional[str] = None, feeling: Optional[str] = None,
                   notes: Optional[str] = None) -> dict:
    """Edit a session's metadata. Only non-null args are written. Use `workout_date`
    (YYYY-MM-DD, Pacific) to move a session to the right day, or set focus/feeling/
    notes after the fact. To change the SETS, use update_set, log_workout (with
    `workout_id` to append), or delete_record(kind="set"); to remove the whole session
    use delete_record(kind="workout")."""
    if err := _bad_date(workout_date, "workout_date"):
        return err
    fields = {"workout_date": workout_date, "focus": focus,
              "feeling": feeling, "notes": notes}
    sets = {k: v for k, v in fields.items() if v is not None}
    if not sets:
        return {"workout_id": workout_id, "updated": []}
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM workouts WHERE id=?", (workout_id,)).fetchone()
        if not exists:
            return {"error": f"no workout with id {workout_id}"}
        cols = ", ".join(f"{k}=?" for k in sets)
        conn.execute(f"UPDATE workouts SET {cols} WHERE id=?", (*sets.values(), workout_id))
    return {"workout_id": workout_id, "updated": list(sets)}


@mcp.tool()
def update_set(set_id: int, weight_lbs: Optional[float] = None,
               reps: Optional[int] = None, rpe: Optional[float] = None,
               note: Optional[str] = None) -> dict:
    """Correct a single logged set. Only non-null args are written, so this can't
    blank a field back to NULL (e.g. clear a weight to mark bodyweight) — delete the
    set with delete_record(kind="set") and re-log it for that. Find the `set_id` with
    get_exercise_history. `rpe` is 1-10."""
    if reason := _bad_set({"weight_lbs": weight_lbs, "reps": reps, "rpe": rpe}):
        return {"error": reason}
    fields = {"weight_lbs": weight_lbs, "reps": reps, "rpe": rpe, "note": note}
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


@mcp.tool()
def get_fitness_briefing(recent_workouts: int = 5) -> dict:
    """One-call trainer context. Returns the stored profile (injuries, split, goals),
    per-muscle recency (days since each muscle was last trained + sets in the last 7
    days), and recent sessions. Call this at the start of a training conversation to
    decide what to work and what to rest: muscles with the most days_since (and low
    recent volume) are recovered and due; ones trained in the last ~1-2 days should
    rest. The recommendation itself is yours to make from this data."""
    with db() as conn:
        profile = _get_profile(conn)
        mrows = conn.execute(
            """SELECT em.muscle,
                      MAX(w.workout_date) AS last_date,
                      SUM(CASE WHEN w.workout_date >= ? THEN 1 ELSE 0 END) AS sets_7d
               FROM sets s
               JOIN workouts w ON w.id = s.workout_id
               JOIN exercise_muscles em ON em.exercise_id = s.exercise_id
               GROUP BY em.muscle""",
            (date.fromordinal(date.fromisoformat(today()).toordinal() - 6).isoformat(),),
        ).fetchall()
        recent = conn.execute(
            "SELECT id, workout_date, focus, feeling FROM workouts ORDER BY workout_date DESC, id DESC LIMIT ?",
            (recent_workouts,),
        ).fetchall()
        recent_out = []
        for w in recent:
            n = conn.execute(
                "SELECT COUNT(DISTINCT exercise_id) AS e, COUNT(*) AS s FROM sets WHERE workout_id=?",
                (w["id"],),
            ).fetchone()
            recent_out.append({"workout_id": w["id"], "date": w["workout_date"],
                               "focus": w["focus"], "feeling": w["feeling"],
                               "exercises": n["e"], "sets": n["s"]})
    recency = sorted(
        ({"muscle": r["muscle"], "last_trained": r["last_date"],
          "days_since": _days_since(r["last_date"]), "sets_last_7d": r["sets_7d"]}
         for r in mrows),
        key=lambda m: (m["days_since"] is None, -(m["days_since"] or 0)),
    )
    return {"now": current_clock(), "profile": profile,
            "muscle_recency": recency, "recent_workouts": recent_out}


@mcp.tool()
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


if __name__ == "__main__":
    init_db()
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "http":
        # Remote mode (behind Coolify's HTTPS proxy). Connector URL: https://<domain>/mcp
        mcp.run(transport="http",
                host=os.environ.get("MCP_HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8000")),
                path="/mcp")
    else:
        mcp.run()
