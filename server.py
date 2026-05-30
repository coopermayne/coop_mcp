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

import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

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
mcp = FastMCP("journal", auth=_auth)
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


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


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
      - No candidate >= 0.6: likely a new person. Ask, then create_person and link
        — or leave it pending if the user says they'll explain later.

    Args:
        body: The cleaned, structured journal entry.
        raw_body: The user's verbatim input. Optional but recommended.
        mentions: Surface forms of people referenced, e.g. ["Tom", "Robin"].
        entry_date: Day the entry is ABOUT as YYYY-MM-DD. Defaults to today.
    """
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
    linked = []
    with db() as conn:
        for ln in links:
            mid, pid = ln["mention_id"], ln["person_id"]
            m = conn.execute("SELECT surface_form FROM mentions WHERE id=?", (mid,)).fetchone()
            if not m:
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
    return {"linked": linked}


@mcp.tool()
def create_person(canonical_name: str, role: Optional[str] = None,
                  notes: Optional[str] = None,
                  aliases: Optional[list[str]] = None,
                  summary: Optional[str] = None,
                  email: Optional[str] = None,
                  phone: Optional[str] = None,
                  address: Optional[str] = None,
                  groups: Optional[list[str]] = None) -> dict:
    """Create a person (an entity). `role` is the disambiguator the user will rely
    on later, e.g. "father", "law school friend". `summary` is a short rolling
    profile for context. `groups` are circle names like ["family"] — created if new.
    Add known aliases and contact fields up front. Returns the new person_id."""
    with db() as conn:
        pid = conn.execute(
            """INSERT INTO people(canonical_name, role, notes, summary, email, phone,
               address, created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (canonical_name, role, notes, summary, email, phone, address, now()),
        ).lastrowid
        for a in (aliases or []):
            conn.execute(
                """INSERT OR IGNORE INTO aliases(person_id, surface_form, phonetic_key,
                   source) VALUES (?,?,?, 'manual')""",
                (pid, a, phonetic(a)),
            )
        _set_groups(conn, pid, groups)
    return {"person_id": pid, "canonical_name": canonical_name, "role": role}


@mcp.tool()
def update_person(person_id: int, role: Optional[str] = None,
                  notes: Optional[str] = None, summary: Optional[str] = None,
                  email: Optional[str] = None, phone: Optional[str] = None,
                  address: Optional[str] = None,
                  groups: Optional[list[str]] = None) -> dict:
    """Update fields on a person. Only non-null args are written. Use `summary` to
    keep a short rolling profile current, and `groups` to set circle membership
    (replaces existing membership; group names are created if new)."""
    fields = {"role": role, "notes": notes, "summary": summary,
              "email": email, "phone": phone, "address": address}
    sets = {k: v for k, v in fields.items() if v is not None}
    with db() as conn:
        if sets:
            cols = ", ".join(f"{k}=?" for k in sets)
            conn.execute(f"UPDATE people SET {cols} WHERE id=?",
                         (*sets.values(), person_id))
        if groups is not None:
            conn.execute("DELETE FROM person_groups WHERE person_id=?", (person_id,))
            _set_groups(conn, person_id, groups)
    return {"person_id": person_id, "updated": list(sets) + (["groups"] if groups is not None else [])}


@mcp.tool()
def add_alias(person_id: int, surface_form: str) -> dict:
    """Manually attach an alias to an existing person."""
    with db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO aliases(person_id, surface_form, phonetic_key,
               source) VALUES (?,?,?, 'manual')""",
            (person_id, surface_form, phonetic(surface_form)),
        )
    return {"person_id": person_id, "alias": surface_form}


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
    """Compact registry of known people (id, name, role, groups, alias count).
    Optionally filter by a name/role fragment or a group name. Load this for
    context when starting a session."""
    with db() as conn:
        rows = conn.execute(
            """SELECT p.id, p.canonical_name, p.role,
                      (SELECT COUNT(*) FROM aliases a WHERE a.person_id=p.id) AS aliases
               FROM people p ORDER BY p.canonical_name"""
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
                           "role": r["role"], "groups": grps, "aliases": r["aliases"]})
    return {"people": people, "count": len(people)}


@mcp.tool()
def get_person_history(person_id: int, limit: int = 50,
                       since: Optional[str] = None,
                       max_chars: int = 600) -> dict:
    """Every entry that mentions this person, newest first — the payoff query.
    This is an indexed lookup on the entity, so 'everything about Tom my father'
    never pulls in the other Tom. Bodies are truncated to max_chars."""
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
    recent entries. Call this at the start of a conversation so you know who and
    what the user is likely talking about."""
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
        "people": roster,
        "people_count": len(roster),
        "groups": grp,
        "pending_mentions": pending,
        "recent_entries": [
            {"entry_id": r["id"], "entry_date": r["entry_date"],
             "body": _truncate(r["body"], 200)} for r in recent
        ],
    }


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
