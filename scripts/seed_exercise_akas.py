"""
Seed common AKAs (alternative names) onto the exercises that shipped in the default
library import (free-exercise-db, via scripts/import_exercises.py). With AKAs in place,
searching/logging a movement by whatever the user calls it — "bench", "rdl", "ohp" —
resolves to the right catalog row instead of coming back empty.

WHY MATCH ON NAME, NOT ID
-------------------------
The library was imported from a public dataset, so the SAME exercise has the SAME *name*
everywhere, but its row `id` is whatever autoincrement happened to assign on each machine
— so ids differ between this dev DB and production. To stay safe against production this
script keys every entry by the exercise's EXACT name (the stable identity) and looks it
up with an exact, case-insensitive match. A name that isn't on file is simply reported
and skipped — never created, never fuzzily mismatched onto the wrong lift.

It's a MERGE, not a replace: each AKA is added with INSERT OR IGNORE, so re-running is
idempotent and it never clobbers an AKA you (or the add-exercise helper) set by hand.

Run it against your LIVE DB (the throwaway dev DB here won't reach production):

    JOURNAL_DB=/data/journal.db .venv/bin/python scripts/seed_exercise_akas.py

Flags:
    --dry-run   report what would be written (which names matched, which didn't), write nothing

Note: the AKA map below is keyed by the free-exercise-db names EXACTLY as imported (e.g.
"Barbell Bench Press - Medium Grip"). If you renamed an entry after import, its AKAs land
under `unmatched` — add the row's current name to the map (or set its AKAs from the
library page) and re-run.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server  # noqa: E402  (reads JOURNAL_DB at import — set it in the environment)

# Canonical name (EXACT, as imported from free-exercise-db) -> common AKAs people search
# or speak it by. Lowercased on store; the canonical name itself is never added as an AKA.
# Kept to the well-known movements where an alternative name is genuinely common — there's
# no value in inventing AKAs for the long tail of near-duplicate machine variants.
AKAS = {
    # ---- Chest / press ----
    "Barbell Bench Press - Medium Grip": ["bench", "bench press", "barbell bench", "flat bench", "flat bench press", "bb bench"],
    "Dumbbell Bench Press": ["db bench", "dumbbell bench", "db bench press", "flat dumbbell press"],
    "Barbell Incline Bench Press - Medium Grip": ["incline bench", "incline bench press", "incline barbell bench"],
    "Incline Dumbbell Press": ["incline db press", "incline dumbbell bench", "incline db bench press"],
    "Decline Barbell Bench Press": ["decline bench", "decline bench press"],
    "Decline Dumbbell Bench Press": ["decline db press", "decline dumbbell bench"],
    "Close-Grip Barbell Bench Press": ["close grip bench", "close-grip bench", "cgbp", "close grip bench press"],
    "Machine Bench Press": ["chest press machine", "machine chest press"],
    "Cable Chest Press": ["cable press"],
    "Dumbbell Flyes": ["dumbbell fly", "db fly", "db flyes", "chest fly", "chest flyes"],
    "Incline Dumbbell Flyes": ["incline fly", "incline db fly", "incline dumbbell fly"],
    "Flat Bench Cable Flyes": ["cable fly", "cable flyes", "cable crossover"],
    "Dips - Chest Version": ["chest dips", "chest dip"],
    "Dips - Triceps Version": ["tricep dips", "tricep dip", "dips", "dip"],

    # ---- Legs ----
    "Barbell Squat": ["squat", "back squat", "barbell back squat", "bb squat"],
    "Front Barbell Squat": ["front squat", "barbell front squat"],
    "Barbell Full Squat": ["full squat", "deep squat"],
    "Hack Squat": ["hack squat machine"],
    "Goblet Squat": ["goblet squats"],
    "Overhead Squat": ["ohs", "overhead squats"],
    "Split Squats": ["bulgarian split squat", "bulgarian split squats", "bss", "split squat"],
    "Leg Press": ["leg press machine"],
    "Leg Extensions": ["leg extension", "quad extension", "quad extensions"],
    "Lying Leg Curls": ["leg curl", "lying leg curl", "hamstring curl", "ham curl"],
    "Seated Leg Curl": ["seated leg curls", "seated hamstring curl"],
    "Romanian Deadlift": ["rdl", "romanian dl", "romanian deadlifts"],
    "Barbell Deadlift": ["deadlift", "conventional deadlift", "bb deadlift", "dead lift"],
    "Stiff-Legged Barbell Deadlift": ["stiff leg deadlift", "stiff-legged deadlift", "sldl"],
    "Sumo Deadlift": ["sumo", "sumo dl", "sumo deadlifts"],
    "Trap Bar Deadlift": ["hex bar deadlift", "trap bar dl"],
    "Barbell Hip Thrust": ["hip thrust", "hip thrusts", "barbell hip thrusts"],
    "Glute Kickback": ["glute kickbacks", "cable kickback", "donkey kick"],
    "Standing Calf Raises": ["standing calf raise", "calf raise", "calf raises"],
    "Seated Calf Raise": ["seated calf raises"],
    "Dumbbell Lunges": ["dumbbell lunge", "db lunge", "db lunges", "walking lunge"],
    "Barbell Lunge": ["barbell lunges", "bb lunge"],
    "Good Morning": ["good mornings"],

    # ---- Shoulders ----
    "Barbell Shoulder Press": ["overhead press", "ohp", "barbell overhead press", "shoulder press", "strict press", "barbell ohp"],
    "Standing Military Press": ["military press", "standing ohp", "standing overhead press"],
    "Seated Dumbbell Press": ["seated db press", "seated dumbbell shoulder press", "seated db shoulder press"],
    "Dumbbell Shoulder Press": ["db shoulder press", "dumbbell press", "db press"],
    "Arnold Dumbbell Press": ["arnold press", "arnolds"],
    "Cable Shoulder Press": ["cable ohp"],
    "Push Press": ["push presses"],
    "Side Lateral Raise": ["lateral raise", "lat raise", "side raise", "lateral raises", "db lateral raise"],
    "Front Dumbbell Raise": ["front raise", "front raises", "db front raise"],
    "Reverse Flyes": ["rear delt fly", "rear delt flye", "reverse fly", "rear delt raise", "reverse flye"],
    "Face Pull": ["face pulls"],
    "Barbell Shrug": ["shrug", "shrugs", "barbell shrugs", "bb shrug"],
    "Dumbbell Shrug": ["db shrug", "dumbbell shrugs", "db shrugs"],

    # ---- Back ----
    "Bent Over Barbell Row": ["barbell row", "bent over row", "bent-over row", "bb row", "bor"],
    "One-Arm Dumbbell Row": ["db row", "dumbbell row", "single arm row", "one arm row", "one-arm row"],
    "Seated Cable Rows": ["seated cable row", "cable row", "seated row"],
    "T-Bar Row with Handle": ["t bar row", "t-bar row", "tbar row"],
    "Wide-Grip Lat Pulldown": ["lat pulldown", "pulldown", "wide grip pulldown", "lat pull down", "lat pulldowns"],
    "Pullups": ["pull up", "pull-up", "pullup", "pull ups", "pull-ups"],
    "Chin-Up": ["chin up", "chinup", "chins", "chin ups"],
    "Inverted Row": ["inverted rows", "bodyweight row"],
    "Upright Barbell Row": ["upright row", "barbell upright row", "upright rows"],
    "Straight-Arm Pulldown": ["straight arm pulldown", "straight-arm pushdown"],

    # ---- Arms ----
    "Barbell Curl": ["bicep curl", "barbell bicep curl", "bb curl", "barbell curls"],
    "Dumbbell Bicep Curl": ["db curl", "dumbbell curl", "db bicep curl", "dumbbell curls"],
    "Hammer Curls": ["hammer curl", "hammers", "dumbbell hammer curl"],
    "Preacher Curl": ["preacher", "preacher curls"],
    "EZ-Bar Curl": ["ez bar curl", "ez curl", "ez-bar curls"],
    "Concentration Curls": ["concentration curl"],
    "Cable Hammer Curls - Rope Attachment": ["rope hammer curl", "cable hammer curl"],
    "Spider Curl": ["spider curls"],
    "Zottman Curl": ["zottman curls"],
    "Incline Dumbbell Curl": ["incline curl", "incline db curl", "incline dumbbell curls"],
    "Triceps Pushdown": ["tricep pushdown", "pushdown", "cable pushdown", "tricep pressdown", "tricep pushdowns"],
    "Triceps Pushdown - Rope Attachment": ["rope pushdown", "tricep rope pushdown", "rope tricep pushdown"],
    "EZ-Bar Skullcrusher": ["skullcrusher", "skull crusher", "skullcrushers", "lying tricep extension"],
    "Tricep Dumbbell Kickback": ["tricep kickback", "tricep kickbacks", "db kickback"],
    "Close-Grip EZ Bar Curl": ["close grip curl", "close-grip ez curl"],
    "Standing Dumbbell Triceps Extension": ["overhead tricep extension", "db overhead extension", "standing tricep extension"],
    "Bench Dips": ["bench dip"],
    "Cable Rope Overhead Triceps Extension": ["overhead rope extension", "rope overhead tricep extension"],

    # ---- Core ----
    "Cable Crunch": ["cable crunches", "kneeling cable crunch", "rope crunch"],
    "Crunches": ["crunch"],
    "Hanging Leg Raise": ["hanging leg raises", "hanging knee raise", "hanging knee raises"],
    "Plank": ["planks", "front plank"],
    "Reverse Crunch": ["reverse crunches"],
    "Cross-Body Crunch": ["cross body crunch", "bicycle crunch"],
    "Russian Twist": ["russian twists"],
    "Pallof Press": ["pallof", "pallof presses"],

    # ---- Olympic / power ----
    "Clean and Jerk": ["c&j", "clean & jerk"],
    "Power Clean": ["power cleans"],
    "Hang Clean": ["hang cleans"],
    "Snatch": ["snatches"],
    "Power Snatch": ["power snatches"],
    "Kettlebell Thruster": ["thruster", "thrusters", "kb thruster"],
}


def main():
    ap = argparse.ArgumentParser(description="Seed common AKAs onto the default exercise library.")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    server.init_db()

    matched = 0
    aliases_added = 0
    unmatched: list[str] = []

    with server.db() as conn:
        for name, akas in AKAS.items():
            # Exact, case-insensitive match on the NAME (the stable identity) — never the id,
            # which differs between this DB and production, and never a fuzzy match.
            row = conn.execute(
                "SELECT id FROM exercises WHERE lower(name)=lower(?)", (name,)
            ).fetchone()
            if not row:
                unmatched.append(name)
                continue
            matched += 1
            eid = row["id"]
            existing = {a["alias"] for a in conn.execute(
                "SELECT alias FROM exercise_aliases WHERE exercise_id=?", (eid,))}
            new = [a for a in akas if a.strip().lower() not in existing
                   and a.strip().lower() != name.lower()]
            if args.dry_run:
                if new:
                    print(f"  [+] {name}: {', '.join(new)}")
                continue
            # Merge (replace=False): add the AKAs, leaving any already present untouched.
            before = len(existing)
            server._set_aliases(conn, eid, akas, replace=False)
            after = conn.execute(
                "SELECT COUNT(*) AS c FROM exercise_aliases WHERE exercise_id=?", (eid,)
            ).fetchone()["c"]
            aliases_added += after - before

    verb = "would add" if args.dry_run else "added"
    print(f"\n{matched}/{len(AKAS)} names matched; {verb} {aliases_added} AKAs.")
    if unmatched:
        print(f"\nunmatched (not in library under this exact name — skipped, nothing created):")
        for n in sorted(unmatched):
            print(f"  - {n}")


if __name__ == "__main__":
    main()
