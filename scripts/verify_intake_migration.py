"""Dry-run the intake migration against a COPY of a database and prove nothing is lost.

The intake log was rebuilt twice: alcohol moved off the standalone `drinks` table onto
a day-level `nutrition` row, and then day-level rows became per-item `intake_items`.
Both folds run automatically inside `init_db()` and are guarded by settings flags, so
they happen once — but "runs once" is not the same as "is correct on YOUR data", and
the production DB holds real drinking history.

This script answers that: it copies the DB, runs the migration on the copy, and
compares the alcohol totals before and after, per day and in aggregate. The original
file is never opened for writing.

    python3 scripts/verify_intake_migration.py /path/to/journal-backup.db

Exit status is 0 only if every day's alcohol survives with the same total.
"""

import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def day_totals(conn, table, date_col, col="standard_drinks"):
    """{date: total} for one alcohol-bearing table, skipping days with no figure."""
    try:
        rows = conn.execute(
            f"SELECT {date_col} AS d, ROUND(SUM({col}), 2) AS t FROM {table} "
            f"WHERE {col} IS NOT NULL GROUP BY {date_col}"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}  # table or column doesn't exist yet in this DB
    return {r[0]: r[1] for r in rows}


def main(src):
    if not os.path.exists(src):
        sys.exit(f"no such file: {src}")

    tmp = tempfile.mkdtemp(prefix="intake-verify-")
    copy = os.path.join(tmp, "journal.db")
    shutil.copy2(src, copy)
    print(f"working on a copy: {copy}\n(the original is never written to)\n")

    # BEFORE: alcohol as it stands today, from wherever it currently lives. A day can
    # legitimately appear in both tables if the first fold already ran in production —
    # take the larger of the two so the check can't be fooled by a partial migration.
    before_conn = sqlite3.connect(copy)
    legacy = day_totals(before_conn, "drinks", "drink_date")
    mid = day_totals(before_conn, "nutrition", "food_date")
    before = dict(legacy)
    for d, t in mid.items():
        before[d] = max(before.get(d, 0), t)
    before_conn.close()
    print(f"before: {len(legacy)} day(s) in `drinks`, {len(mid)} with alcohol in "
          f"`nutrition` → {len(before)} distinct drinking day(s), "
          f"{round(sum(before.values()), 2)} standard drinks total")

    # Run the real migration — the same init_db() the server runs at startup.
    os.environ["JOURNAL_DB"] = copy
    import importlib.util
    spec = importlib.util.spec_from_file_location("server", os.path.join(ROOT, "server.py"))
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    server.init_db()
    server.init_db()  # twice: the flags must make it a no-op the second time

    after_conn = sqlite3.connect(copy)
    after = day_totals(after_conn, "intake_items", "food_date")
    items = after_conn.execute(
        "SELECT COUNT(*) FROM intake_items WHERE standard_drinks IS NOT NULL"
    ).fetchone()[0]
    after_conn.close()
    print(f"after:  {len(after)} drinking day(s) across {items} item(s), "
          f"{round(sum(after.values()), 2)} standard drinks total\n")

    missing = sorted(d for d in before if d not in after)
    changed = sorted(d for d in before if d in after and abs(before[d] - after[d]) > 0.001)
    extra = sorted(d for d in after if d not in before)

    for d in missing:
        print(f"  LOST     {d}: {before[d]} drinks are not in intake_items")
    for d in changed:
        print(f"  CHANGED  {d}: {before[d]} → {after[d]}")
    for d in extra:
        print(f"  ADDED    {d}: {after[d]} (not in the source — unexpected)")

    if missing or changed or extra:
        print("\nFAILED — do not deploy; the numbers above don't reconcile.")
        return 1
    print("OK — every drinking day survives with the same total.")
    print("The `drinks` table is left untouched in the migrated DB, so the original "
          "rows are still there either way.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
