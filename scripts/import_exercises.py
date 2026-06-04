"""
Import the exercise catalog from the open, public-domain **free-exercise-db**
(https://github.com/yuhonas/free-exercise-db, Unlicense) into the LIBRARY — slug, name,
force, level, mechanic, equipment, category, primary/secondary muscles, step-by-step
technique notes, and a demo image. ~870 exercises. Our `exercises` schema is lined up
with this dataset, so the fields map 1:1. Each record is written through
`server.save_exercise` (the one catalog write path), so muscle tiers, the /trainer/library
page, and per-muscle recency all line up exactly as if the trainer had entered them.

This populates the searchable LIBRARY only; everything lands with in_rotation=0. The
ROTATION (the curated pool the trainer programs from) is built afterwards — by logging
workouts, by set_rotation, or by toggling rows on the library page.

Run it against your LIVE DB (the throwaway dev DB here won't reach production):

    JOURNAL_DB=/data/journal.db .venv/bin/python scripts/import_exercises.py

By default it SKIPS exercises that already exist (so it never clobbers anything you or
the trainer have already curated) and pulls the dataset over the network. Useful flags:

    --reset            DESTRUCTIVE: wipe the whole exercise library AND all workout
                       history (workouts + sets + exercise_muscles) before importing,
                       for a clean reseed. Bodyweight readings are preserved (a separate
                       daily metric, not workout history). Honors --dry-run (reports the
                       counts it would delete without touching anything).
    --overwrite        update exercises that already exist (re-sync from the dataset)
    --dry-run          show what would be written, touch nothing
    --limit N          import only the first N (after filtering) — handy for a test run
    --category C        only entries whose dataset category is C (strength, cardio, ...)
    --no-images        don't set image_link
    --source PATH/URL  read the dataset JSON from a local file or an alternate URL

Notes on the source:
  * Each movement ships TWO still photos — a start and a finish frame. We import both
    (image_link + image_link_end), and the library alternates them (~1s) into a crude
    rep animation so a single frozen frame isn't all you get. image_link alone still
    renders as a still (and a self-looping gif there works too, if you add one later).
  * common_mistakes / cautions aren't in the dataset, so they're left empty for the
    trainer to add in conversation (and re-running without --overwrite won't disturb them).
"""
import argparse
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server  # noqa: E402  (reads JOURNAL_DB at import — set it in the environment)

SOURCE_URL = "https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/dist/exercises.json"
IMAGE_BASE = "https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/exercises/"

# server.MUSCLES deliberately mirrors this dataset's muscle vocabulary, so importing is a
# straight lowercase passthrough — no remapping, nothing dropped. We still validate against
# server.MUSCLES so a future change to the source vocabulary surfaces loudly instead of
# silently storing a label that won't aggregate.
CANONICAL = {m.lower() for m in server.MUSCLES}

# Light tidy of the dataset's equipment names toward how the docstring examples read.
EQUIPMENT_MAP = {
    "body only": "bodyweight",
    "kettlebells": "kettlebell",
    "e-z curl bar": "ez-bar",
    "foam roll": "foam roller",
    "bands": "resistance band",
    "exercise ball": "exercise ball",
    "medicine ball": "medicine ball",
}


def map_muscles(names):
    """Lowercase + dedupe a source muscle list, keeping only labels in the canonical set.
    Returns (kept, dropped) — dropped should be empty unless the source vocab drifts."""
    kept, dropped = [], []
    for n in names or []:
        key = n.strip().lower()
        if key in CANONICAL:
            if key not in kept:
                kept.append(key)
        elif key:
            dropped.append(key)            # source vocab drifted from server.MUSCLES
    return kept, dropped


def technique_notes(instructions):
    steps = [s.strip() for s in (instructions or []) if s and s.strip()]
    return "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) or None


def load_source(source):
    if os.path.exists(source):
        with open(source, encoding="utf-8") as f:
            return json.load(f)
    req = urllib.request.Request(source, headers={"User-Agent": "coop_mcp-importer"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser(description="Import free-exercise-db into the library.")
    ap.add_argument("--source", default=SOURCE_URL, help="dataset JSON URL or local path")
    ap.add_argument("--reset", action="store_true",
                    help="DESTRUCTIVE: wipe all exercises + workout history before importing "
                         "(bodyweight preserved); honors --dry-run")
    ap.add_argument("--overwrite", action="store_true", help="update exercises that already exist")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--limit", type=int, help="import only the first N (after filtering)")
    ap.add_argument("--category", help="only entries with this dataset category")
    ap.add_argument("--no-images", action="store_true", help="don't set image_link")
    args = ap.parse_args()

    server.init_db()

    if args.reset:
        # Clean-slate reseed. Deleting workouts cascades to sets (ON DELETE CASCADE on
        # workout_id); deleting exercises cascades to exercise_muscles. body_weight is a
        # separate daily metric (not workout history) and is intentionally left intact.
        with server.db() as conn:
            nx = conn.execute("SELECT COUNT(*) FROM exercises").fetchone()[0]
            nw = conn.execute("SELECT COUNT(*) FROM workouts").fetchone()[0]
            ns = conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
            if args.dry_run:
                print(f"  [reset] would delete {nx} exercises, {nw} workouts, {ns} sets "
                      "(bodyweight preserved)")
            else:
                conn.execute("DELETE FROM workouts")   # cascades to sets
                conn.execute("DELETE FROM exercises")  # cascades to exercise_muscles
                print(f"reset: deleted {nx} exercises, {nw} workouts, {ns} sets "
                      "(bodyweight preserved)")

    data = load_source(args.source)
    print(f"loaded {len(data)} exercises from {args.source}")

    with server.db() as conn:
        existing = {r["name"].lower() for r in conn.execute("SELECT name FROM exercises")}

    created = updated = skipped = 0
    dropped_muscles: set[str] = set()
    n = 0
    for ex in data:
        name = (ex.get("name") or "").strip()
        if not name:
            continue
        if args.category and (ex.get("category") or "").lower() != args.category.lower():
            continue
        if args.limit is not None and n >= args.limit:
            break
        n += 1

        is_existing = name.lower() in existing
        if is_existing and not args.overwrite:
            skipped += 1
            continue

        primary, d1 = map_muscles(ex.get("primaryMuscles"))
        secondary, d2 = map_muscles(ex.get("secondaryMuscles"))
        dropped_muscles.update(d1)
        dropped_muscles.update(d2)

        equipment = ex.get("equipment")
        if equipment:
            equipment = EQUIPMENT_MAP.get(equipment.lower(), equipment)

        # free-exercise-db ships two frames per movement (start, finish); we keep both so
        # the library can alternate them into a crude rep animation. Older/odd entries with
        # a single frame just leave image_link_end None and render as a still.
        images = ex.get("images") or []
        image_link = None if (args.no_images or not images) else IMAGE_BASE + images[0]
        image_link_end = (None if (args.no_images or len(images) < 2)
                          else IMAGE_BASE + images[1])

        # in_rotation is left at its default (0): the import builds the LIBRARY; the
        # rotation is curated afterwards (set_rotation / logging / the library page).
        kwargs = dict(
            name=name,
            slug=ex.get("id"),
            category=ex.get("category"),
            force=ex.get("force"),
            level=ex.get("level"),
            mechanic=ex.get("mechanic"),
            equipment=equipment,
            muscles=primary or None,
            secondary_muscles=secondary or None,
            technique_notes=technique_notes(ex.get("instructions")),
            image_link=image_link,
            image_link_end=image_link_end,
        )

        if args.dry_run:
            tag = "update" if is_existing else "create"
            print(f"  [{tag}] {name} — {equipment or '?'} | "
                  f"P:{primary or '-'} S:{secondary or '-'}")
        else:
            res = server.create_exercise(**kwargs)
            if res.get("created"):
                created += 1
            else:
                updated += 1

    verb = "would write" if args.dry_run else "wrote"
    print(f"{verb}: {created} created, {updated} updated, {skipped} skipped (already present)")
    if dropped_muscles:
        print(f"unmapped source muscles dropped: {sorted(dropped_muscles)}")


if __name__ == "__main__":
    main()
