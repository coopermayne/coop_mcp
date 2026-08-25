"""SQLite storage.

Three tables. `subjects` are the things you're learning about (a person, a myth,
a word). `facets` are the individually-recallable aspects of a subject, and they
are the unit of scheduling — you can know a figure's dates cold while blanking
on what they actually did. `attempts` is the audit trail: every question asked,
your verbatim answer, and the grade it earned.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# The journal's file, resolved the same way server.py resolves DB_PATH — one
# shared database, so the learning tables ride the journal's backup story.
DEFAULT_DB_PATH = Path(os.path.expanduser("~/journal.db"))

# Held apart from SCHEMA because the `kind` migration has to recreate this exact
# table under a different name, and one definition is the only way the rebuilt
# table cannot drift from the declared one.
ATTEMPTS_TABLE = """
CREATE TABLE {name} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    facet_id    TEXT NOT NULL REFERENCES facets (id) ON DELETE CASCADE,
    reviewed_at TEXT NOT NULL,
    rating      INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 4),
    prompt      TEXT,
    response    TEXT,
    critique    TEXT,
    -- 'review' is a graded recall test. 'study' is being taught something you
    -- never knew. 'encounter' is passive comprehension: the word went past in
    -- conversation and was or wasn't followed. Only reviews count toward
    -- retention -- learning a new card is not a failure, and inferring a word
    -- from context is weaker evidence than recalling it cold, so neither
    -- belongs in a number meant to measure recall.
    kind        TEXT NOT NULL DEFAULT 'review'
                CHECK (kind IN ('review', 'study', 'encounter')),
    -- list mode: which reference points the answer actually covered.
    covered     TEXT NOT NULL DEFAULT '[]',
    -- The facet's FSRS card as it stood BEFORE this attempt advanced it. This
    -- is what makes undo possible: FSRS is not invertible from the resulting
    -- card, so a misgraded review can only be taken back by restoring the
    -- state it replaced. NULL on attempts recorded before this column existed.
    prev_card   TEXT
)
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS subjects (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    type        TEXT NOT NULL,
    context     TEXT,
    tags        TEXT NOT NULL DEFAULT '[]',
    source      TEXT NOT NULL DEFAULT 'manual',
    created_at  TEXT NOT NULL,
    archived    INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_subjects_title
    ON subjects (lower(title));

CREATE TABLE IF NOT EXISTS facets (
    id           TEXT PRIMARY KEY,
    subject_id   TEXT NOT NULL REFERENCES subjects (id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    grading_mode TEXT NOT NULL CHECK (grading_mode IN ('recall', 'list', 'open')),
    -- recall: the target answer. list: a JSON array of points. open: unused.
    reference    TEXT,
    -- open/list: how to judge an answer that has no single correct form.
    criteria     TEXT,
    cue          TEXT,
    -- Context-only facets are stored and shown alongside siblings, never quizzed.
    scheduled    INTEGER NOT NULL DEFAULT 1,
    -- Intake throttle: facets wait here until the daily budget lets them in.
    released     INTEGER NOT NULL DEFAULT 0,
    released_at  TEXT,
    fsrs_card    TEXT NOT NULL,
    due          TEXT NOT NULL,
    state        TEXT NOT NULL,
    reps         INTEGER NOT NULL DEFAULT 0,
    lapses       INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    -- Set when you declare you already know this one. The facet is unscheduled
    -- at the same time; this column is what says it was skipped rather than
    -- never meant to be quizzed.
    known_at     TEXT,
    UNIQUE (subject_id, name)
);

CREATE INDEX IF NOT EXISTS idx_facets_due
    ON facets (scheduled, released, due);
CREATE INDEX IF NOT EXISTS idx_facets_subject ON facets (subject_id);
CREATE INDEX IF NOT EXISTS idx_facets_released ON facets (released_at);

{attempts_table};

CREATE INDEX IF NOT EXISTS idx_attempts_facet
    ON attempts (facet_id, reviewed_at DESC);
CREATE INDEX IF NOT EXISTS idx_attempts_time ON attempts (reviewed_at DESC);

-- One row per subject, rebuilt on write. Manual upkeep beats triggers here
-- because a document spans a subject and all of its facets.
CREATE VIRTUAL TABLE IF NOT EXISTS learn_fts USING fts5 (
    subject_id UNINDEXED, title, body
);
""".format(attempts_table=ATTEMPTS_TABLE.format(name="IF NOT EXISTS attempts").strip())


def db_path() -> Path:
    return Path(os.environ.get("JOURNAL_DB", DEFAULT_DB_PATH))


# Which database this process has already built and migrated. Schema creation
# is idempotent, but running it on every call meant every read paid for an
# executescript and two PRAGMA table_info probes. Keyed on the path rather than
# a bare flag, so pointing JOURNAL_DB somewhere new still initializes it.
_initialized: Path | None = None


def connect() -> sqlite3.Connection:
    """Open a connection. Prefer `session()`, which also closes it."""
    global _initialized

    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if _initialized != path:
        conn.executescript(SCHEMA)
        _migrate(conn)
        _initialized = path
    return conn


@contextmanager
def session():
    """A connection that is closed when the block ends.

    `with connect() as conn:` looks like it does this and does not: sqlite3's
    connection context manager governs a transaction, and closes nothing. Every
    call therefore left its connection open until the garbage collector reached
    it, which for a stdio server that runs for weeks is a lot of file
    descriptors held on the strength of refcounting.
    """
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn):
    """Run a multi-statement write as one unit.

    `connect()` opens in autocommit (`isolation_level = None`), which means
    sqlite3's own `with conn:` block commits nothing and rolls back nothing --
    it is a no-op that reads exactly like a transaction. A write that fails
    partway therefore leaves the part that already succeeded, which is how a
    rejected `capture()` used to strand a subject row with half its facets.

    Autocommit stays, because the migration dance in this module drives its own
    BEGIN/COMMIT and reads cleanly that way. Writers opt in here instead.
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


ATTEMPTS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_attempts_facet ON attempts (facet_id, reviewed_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_attempts_time ON attempts (reviewed_at DESC)",
)

ATTEMPTS_COLUMNS = (
    "id, facet_id, reviewed_at, rating, prompt, response, critique, kind, covered"
)


def _migrate(conn) -> None:
    """Bring an older database up to the current schema."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(attempts)")}
    if "kind" not in cols:
        conn.execute(
            "ALTER TABLE attempts ADD COLUMN kind TEXT NOT NULL DEFAULT 'review'"
        )

    _widen_attempt_kinds(conn)

    # Facets you already knew before the deck did. Marking one sets scheduled =
    # 0, which is what actually keeps it out of the queue; `known_at` records
    # WHY it is unscheduled, so a word you claimed to know stays tellable apart
    # from a context facet that was never meant to be quizzed -- and stays
    # reversible if the claim turns out to be optimistic.
    #
    # The feature that WRITES this is shelved along with the rest of the
    # language-learning side (see store.known_words). The column is not: facets
    # already marked known carry it, and a migration that stopped running would
    # leave those marks unreadable on any fresh database. Additive, nullable,
    # and inert when nothing sets it -- so it stays.
    fcols = {r["name"] for r in conn.execute("PRAGMA table_info(facets)")}
    if "known_at" not in fcols:
        conn.execute("ALTER TABLE facets ADD COLUMN known_at TEXT")

    # Added after `kind`, and deliberately after _widen_attempt_kinds above:
    # that rebuild copies ATTEMPTS_COLUMNS by name, which is the pre-prev_card
    # set, so a database going through both paths in one connect() still
    # copies cleanly and picks the new column up here.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(attempts)")}
    if "prev_card" not in cols:
        conn.execute("ALTER TABLE attempts ADD COLUMN prev_card TEXT")


def _widen_attempt_kinds(conn) -> None:
    """Let `kind` hold 'encounter' as well as 'review' and 'study'.

    SQLite cannot alter a CHECK constraint in place, so the table is rebuilt.
    Two different databases need this and only one of them looks like it does:
    a database created from an older SCHEMA carries CHECK (kind IN ('review',
    'study')) and would reject an encounter outright, while one that got `kind`
    from the ALTER above has no constraint at all and would accept any string.
    Rebuilding both leaves every database with the same enforced set.
    """
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'attempts'"
    ).fetchone()
    if sql is None or "encounter" in sql["sql"]:
        return

    # The 12-step ALTER dance: FKs off, swap inside a transaction, FKs back on.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(ATTEMPTS_TABLE.format(name="attempts_new"))
        conn.execute(
            f"INSERT INTO attempts_new ({ATTEMPTS_COLUMNS}) "
            f"SELECT {ATTEMPTS_COLUMNS} FROM attempts"
        )
        conn.execute("DROP TABLE attempts")
        conn.execute("ALTER TABLE attempts_new RENAME TO attempts")
        for stmt in ATTEMPTS_INDEXES:
            conn.execute(stmt)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def reindex(conn, subject_id: str) -> None:
    """Refresh the search document for one subject."""
    subject = conn.execute("SELECT * FROM subjects WHERE id = ?", (subject_id,)).fetchone()
    if subject is None:
        conn.execute("DELETE FROM learn_fts WHERE subject_id = ?", (subject_id,))
        return
    facets = conn.execute(
        "SELECT name, reference, criteria FROM facets WHERE subject_id = ?", (subject_id,)
    ).fetchall()

    parts = [subject["context"] or "", " ".join(json.loads(subject["tags"]))]
    for f in facets:
        parts.append(f["name"])
        parts.append(f["reference"] or "")
        parts.append(f["criteria"] or "")

    conn.execute("DELETE FROM learn_fts WHERE subject_id = ?", (subject_id,))
    conn.execute(
        "INSERT INTO learn_fts (subject_id, title, body) VALUES (?, ?, ?)",
        (subject_id, subject["title"], " ".join(p for p in parts if p)),
    )
