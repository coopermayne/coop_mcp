"""
Backfill the start + finish form photos onto the exercises that shipped in the default
library import (free-exercise-db). The library card crossfades the two frames into a
looping rep animation; the original import only ever stored frame 0 (image_link), and
image_link_end (frame 1) is a newer column that's NULL on already-seeded DBs — so until
this runs, every card shows a single frozen still. This fills image_link_end (and any
missing image_link) from the dataset's two-frame `images` array.

WHY MATCH ON NAME, NOT ID
-------------------------
Same reason as scripts/seed_exercise_akas.py: the library was imported from a public
dataset, so a movement has the SAME *name* everywhere but its row `id` is whatever
autoincrement assigned on each machine — ids differ between this dev DB and production.
So every entry is keyed by the exercise's EXACT name (its stable identity), matched
case-insensitively. A dataset name not on file is reported and skipped — never created,
never fuzzily mismatched onto the wrong lift. That makes it SAFE to run against prod.

SURGICAL, UNLIKE --overwrite ON THE IMPORTER
--------------------------------------------
This writes ONLY image_link / image_link_end and nothing else, so your hand-edited
technique notes, muscle map, AKAs, rotation flags and archived state are untouched
(re-running the full importer with --overwrite would re-sync notes/muscles from the
dataset). By default it FILLS each column only where it's currently empty, so a still
or gif you pinned by hand survives; pass --force to resync both columns to the dataset
URLs. Idempotent either way.

Run it against your LIVE DB (the throwaway dev DB here won't reach production):

    JOURNAL_DB=/data/journal.db .venv/bin/python scripts/backfill_exercise_images.py

Flags:
    --dry-run          report what would change (matched / filled / skipped), write nothing
    --force            overwrite existing image_link/image_link_end too (resync to dataset),
                       not just fill empties — clobbers any image you pinned by hand
    --source PATH/URL  read the dataset JSON from a local file or alternate URL

Note: nothing is uploaded or hosted — image_link/image_link_end are just URLs pointing at
free-exercise-db's GitHub raw content, which the browser hot-links when a card renders.
"""
import argparse
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server  # noqa: E402  (reads JOURNAL_DB at import — set it in the environment)

# Same source + image base the original import used, so the URLs line up exactly.
SOURCE_URL = "https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/dist/exercises.json"
IMAGE_BASE = "https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/exercises/"


def load_source(src: str):
    if os.path.exists(src):
        with open(src, encoding="utf-8") as f:
            return json.load(f)
    with urllib.request.urlopen(src, timeout=60) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser(
        description="Backfill start/finish form photos onto the default exercise library.")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="resync both image columns to the dataset, not just fill empties")
    ap.add_argument("--source", default=SOURCE_URL, help="dataset JSON path or URL")
    args = ap.parse_args()

    server.init_db()

    data = load_source(args.source)
    print(f"loaded {len(data)} exercises from {args.source}")

    matched = 0
    rows_changed = 0
    cols_filled = 0
    unmatched: list[str] = []

    with server.db() as conn:
        for ex in data:
            name = (ex.get("name") or "").strip()
            images = ex.get("images") or []
            if not name or not images:
                continue
            start = IMAGE_BASE + images[0]
            finish = IMAGE_BASE + images[1] if len(images) > 1 else None

            # Exact, case-insensitive match on the NAME — never the id (differs from prod),
            # never fuzzy.
            row = conn.execute(
                "SELECT id, image_link, image_link_end FROM exercises WHERE lower(name)=lower(?)",
                (name,),
            ).fetchone()
            if not row:
                unmatched.append(name)
                continue
            matched += 1

            # Decide each column independently: with --force, set to the dataset URL; without,
            # only fill a column that's currently empty (so a hand-pinned image survives).
            updates: dict[str, str] = {}
            if start and (args.force or not row["image_link"]):
                if row["image_link"] != start:
                    updates["image_link"] = start
            if finish and (args.force or not row["image_link_end"]):
                if row["image_link_end"] != finish:
                    updates["image_link_end"] = finish
            if not updates:
                continue

            rows_changed += 1
            cols_filled += len(updates)
            if args.dry_run:
                print(f"  [~] {name}: {', '.join(sorted(updates))}")
                continue
            cols = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE exercises SET {cols} WHERE id=?",
                         (*updates.values(), row["id"]))

    verb = "would update" if args.dry_run else "updated"
    print(f"\n{matched} names matched; {verb} {rows_changed} rows ({cols_filled} image fields).")
    if unmatched:
        print("\nunmatched (not in library under this exact name — skipped, nothing created):")
        for n in sorted(unmatched):
            print(f"  - {n}")


if __name__ == "__main__":
    main()
