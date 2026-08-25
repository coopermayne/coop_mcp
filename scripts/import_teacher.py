"""One-shot import of a standalone teacher repo database into the journal DB.

The learning feature arrived here as a port of the separate `teacher` repo,
whose data lived in its own SQLite file. This copies that file's three tables
(subjects, facets, attempts) into the shared journal database, preserving ids
and FSRS state byte-for-byte, then rebuilds the learn_fts search index.

Idempotent: rows whose ids already exist are skipped (INSERT OR IGNORE on the
primary keys), so re-running reports 0 rather than erroring — same calm as
/weight's re-upload. The one thing it will NOT silently paper over is a title
collision under a DIFFERENT id (subjects are unique on lower(title)): that
subject is skipped and reported, because its facets would otherwise land
against a subject row that isn't theirs.

    JOURNAL_DB=./journal_dev.db python scripts/import_teacher.py ~/Code/teacher/data/teacher.db
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from learning import db as learn_db  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: import_teacher.py <path-to-teacher.db>")
    src_path = Path(sys.argv[1])
    if not src_path.exists():
        sys.exit(f"no such file: {src_path}")

    with learn_db.session() as conn:  # ensures schema + migrations first
        conn.execute("ATTACH DATABASE ? AS src", (str(src_path),))

        # Subjects whose title is already taken by a DIFFERENT id: skip theirs
        # and everything under them, loudly.
        collisions = conn.execute(
            """SELECT s.id, s.title FROM src.subjects s
               JOIN subjects d ON lower(d.title) = lower(s.title) AND d.id != s.id"""
        ).fetchall()
        skip_ids = {r["id"] for r in collisions}
        for r in collisions:
            print(f"SKIPPED (title collision): {r['title']!r} src id {r['id']}")

        def cols(table: str) -> str:
            """Columns present on BOTH sides, so an older source still copies."""
            s = [r["name"] for r in conn.execute(f"PRAGMA src.table_info({table})")]
            d = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            return ", ".join(c for c in s if c in d)

        with learn_db.transaction(conn):
            skip = tuple(skip_ids) or ("",)
            ph = ",".join("?" * len(skip))
            c = cols("subjects")
            n_subj = conn.execute(
                f"INSERT OR IGNORE INTO subjects ({c}) "
                f"SELECT {c} FROM src.subjects WHERE id NOT IN ({ph})", skip
            ).rowcount
            c = cols("facets")
            n_fac = conn.execute(
                f"INSERT OR IGNORE INTO facets ({c}) "
                f"SELECT {c} FROM src.facets WHERE subject_id NOT IN ({ph})", skip
            ).rowcount
            c = cols("attempts")
            n_att = conn.execute(
                f"INSERT OR IGNORE INTO attempts ({c}) "
                f"SELECT {c} FROM src.attempts "
                f"WHERE facet_id IN (SELECT id FROM facets)"
            ).rowcount
            # Rebuild search for every subject we might have touched.
            for r in conn.execute("SELECT id FROM subjects").fetchall():
                learn_db.reindex(conn, r["id"])

        conn.execute("DETACH DATABASE src")

        total = conn.execute("SELECT COUNT(*) n FROM subjects").fetchone()["n"]
        print(f"imported: {n_subj} subjects, {n_fac} facets, {n_att} attempts "
              f"(collection now {total} subjects)")


if __name__ == "__main__":
    main()
