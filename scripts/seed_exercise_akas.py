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
    # ---- Chest / horizontal press ----
    "Barbell Bench Press - Medium Grip": ["bench", "bench press", "barbell bench", "flat bench", "flat bench press", "bb bench", "flat barbell bench press"],
    "Wide-Grip Barbell Bench Press": ["wide grip bench", "wide-grip bench press", "wide grip bench press"],
    "Dumbbell Bench Press": ["db bench", "dumbbell bench", "db bench press", "flat dumbbell press", "flat db press"],
    "Dumbbell Bench Press with Neutral Grip": ["neutral grip dumbbell press", "neutral grip db bench"],
    "Barbell Incline Bench Press - Medium Grip": ["incline bench", "incline bench press", "incline barbell bench", "incline barbell bench press"],
    "Incline Dumbbell Press": ["incline db press", "incline dumbbell bench", "incline db bench press", "incline dumbbell bench press"],
    "Decline Barbell Bench Press": ["decline bench", "decline bench press", "decline barbell bench"],
    "Decline Dumbbell Bench Press": ["decline db press", "decline dumbbell bench"],
    "Close-Grip Barbell Bench Press": ["close grip bench", "close-grip bench", "cgbp", "close grip bench press"],
    "Machine Bench Press": ["chest press machine", "machine chest press"],
    "Cable Chest Press": ["cable press"],
    "Cable Crossover": ["cable crossovers", "crossover", "cable crossover fly"],
    "Low Cable Crossover": ["low cable crossovers", "low to high cable fly"],
    "Dumbbell Flyes": ["dumbbell fly", "db fly", "db flyes", "chest fly", "chest flyes", "dumbbell chest fly"],
    "Incline Dumbbell Flyes": ["incline fly", "incline db fly", "incline dumbbell fly"],
    "Decline Dumbbell Flyes": ["decline fly", "decline db fly"],
    "Flat Bench Cable Flyes": ["cable fly", "cable flyes"],
    "Butterfly": ["pec deck", "pec deck fly", "machine chest fly", "pec fly"],
    "Dips - Chest Version": ["chest dips", "chest dip"],
    "Dips - Triceps Version": ["tricep dips", "tricep dip", "dips", "dip"],
    "Parallel Bar Dip": ["parallel bar dips", "bar dips"],
    "Pushups": ["push up", "push-up", "pushup", "push ups", "press up", "press ups"],
    "Push-Up Wide": ["wide push up", "wide pushups"],
    "Incline Push-Up": ["incline push up", "incline pushups"],
    "Decline Push-Up": ["decline push up", "decline pushups"],
    "Handstand Push-Ups": ["handstand push up", "hspu", "handstand pushup"],
    "Bent-Arm Dumbbell Pullover": ["dumbbell pullover", "db pullover"],
    "Straight-Arm Dumbbell Pullover": ["straight arm pullover"],
    "Svend Press": ["svend"],
    "Around The Worlds": ["around the world"],

    # ---- Back ----
    "Bent Over Barbell Row": ["barbell row", "bent over row", "bent-over row", "bb row", "bor", "barbell rows"],
    "Reverse Grip Bent-Over Rows": ["reverse grip row", "underhand barbell row", "yates row"],
    "Bent Over Two-Dumbbell Row": ["two dumbbell row", "bent over dumbbell row"],
    "One-Arm Dumbbell Row": ["db row", "dumbbell row", "single arm row", "one arm row", "one-arm row", "single-arm dumbbell row"],
    "Seated Cable Rows": ["seated cable row", "cable row", "seated row"],
    "T-Bar Row with Handle": ["t bar row", "t-bar row", "tbar row"],
    "Lying T-Bar Row": ["chest supported t-bar row", "lying t bar row"],
    "Wide-Grip Lat Pulldown": ["lat pulldown", "pulldown", "wide grip pulldown", "lat pull down", "lat pulldowns", "wide-grip pulldown"],
    "Close-Grip Front Lat Pulldown": ["close grip pulldown", "close-grip pulldown"],
    "V-Bar Pulldown": ["v bar pulldown", "v-handle pulldown"],
    "Underhand Cable Pulldowns": ["underhand pulldown", "reverse grip pulldown", "supinated pulldown"],
    "Pullups": ["pull up", "pull-up", "pullup", "pull ups", "pull-ups"],
    "Weighted Pull Ups": ["weighted pull up", "weighted pullups", "weighted pull-ups"],
    "V-Bar Pullup": ["v bar pull up", "v-bar pull up"],
    "Chin-Up": ["chin up", "chinup", "chins", "chin ups"],
    "Inverted Row": ["inverted rows", "bodyweight row", "australian pull up", "australian pullup"],
    "Upright Barbell Row": ["upright row", "barbell upright row", "upright rows"],
    "Upright Cable Row": ["cable upright row"],
    "Straight-Arm Pulldown": ["straight arm pulldown", "straight-arm pushdown", "stiff arm pulldown"],
    "Rope Straight-Arm Pulldown": ["rope straight arm pulldown"],
    "Rack Pulls": ["rack pull"],
    "Hyperextensions (Back Extensions)": ["back extension", "back extensions", "hyperextension", "hyperextensions", "hyper"],
    "Reverse Hyperextension": ["reverse hyper", "reverse hypers"],
    "Smith Machine Bent Over Row": ["smith machine row", "smith row"],
    "Incline Bench Pull": ["incline bench row", "chest supported barbell row"],
    "Dumbbell Incline Row": ["incline dumbbell row", "chest supported dumbbell row"],

    # ---- Legs: squats ----
    "Barbell Squat": ["squat", "back squat", "barbell back squat", "bb squat", "high bar squat"],
    "Wide Stance Barbell Squat": ["wide stance squat", "sumo squat"],
    "Front Barbell Squat": ["front squat", "barbell front squat"],
    "Front Squat (Clean Grip)": ["clean grip front squat"],
    "Barbell Full Squat": ["full squat", "deep squat", "atg squat"],
    "Hack Squat": ["hack squat machine", "machine hack squat"],
    "Barbell Hack Squat": ["barbell hack"],
    "Goblet Squat": ["goblet squats"],
    "Overhead Squat": ["ohs", "overhead squats"],
    "Box Squat": ["box squats"],
    "Zercher Squats": ["zercher squat"],
    "Karlerson Squats": ["jefferson squat", "jefferson deadlift"],
    "Split Squats": ["bulgarian split squat", "bulgarian split squats", "bss", "split squat", "rear foot elevated split squat", "rfess"],
    "Split Squat with Dumbbells": ["dumbbell split squat", "db split squat", "dumbbell bulgarian split squat"],
    "Smith Single-Leg Split Squat": ["smith bulgarian split squat", "smith split squat"],
    "Dumbbell Squat": ["db squat", "dumbbell squats"],
    "Weighted Sissy Squat": ["sissy squat", "sissy squats"],
    "Kettlebell Pistol Squat": ["pistol squat", "kb pistol squat"],
    "Smith Machine Pistol Squat": ["smith pistol squat"],
    "Plie Dumbbell Squat": ["plie squat", "sumo dumbbell squat"],

    # ---- Legs: press / extension / curl ----
    "Leg Press": ["leg press machine", "45 degree leg press"],
    "Narrow Stance Leg Press": ["close stance leg press"],
    "Leg Extensions": ["leg extension", "quad extension", "quad extensions"],
    "Single-Leg Leg Extension": ["single leg leg extension", "one leg extension"],
    "Lying Leg Curls": ["leg curl", "lying leg curl", "hamstring curl", "ham curl", "lying hamstring curl"],
    "Seated Leg Curl": ["seated leg curls", "seated hamstring curl"],
    "Standing Leg Curl": ["standing hamstring curl"],
    "Glute Ham Raise": ["ghr", "glute-ham raise", "ghd raise"],
    "Floor Glute-Ham Raise": ["nordic curl", "nordic ham curl", "nordic hamstring curl"],

    # ---- Legs: hinge / glutes / calves / lunges ----
    "Romanian Deadlift": ["rdl", "romanian dl", "romanian deadlifts"],
    "Romanian Deadlift from Deficit": ["deficit rdl"],
    "Stiff-Legged Barbell Deadlift": ["stiff leg deadlift", "stiff-legged deadlift", "sldl", "straight leg deadlift"],
    "Stiff-Legged Dumbbell Deadlift": ["dumbbell rdl", "db rdl", "dumbbell stiff leg deadlift"],
    "Barbell Deadlift": ["deadlift", "conventional deadlift", "bb deadlift", "dead lift"],
    "Sumo Deadlift": ["sumo", "sumo dl", "sumo deadlifts"],
    "Trap Bar Deadlift": ["hex bar deadlift", "trap bar dl", "hex bar dl"],
    "Deficit Deadlift": ["deficit deadlifts"],
    "Barbell Hip Thrust": ["hip thrust", "hip thrusts", "barbell hip thrusts"],
    "Barbell Glute Bridge": ["glute bridge", "barbell glute bridges"],
    "Single Leg Glute Bridge": ["one leg glute bridge"],
    "Glute Kickback": ["glute kickbacks", "donkey kick"],
    "One-Legged Cable Kickback": ["cable glute kickback", "cable kickback"],
    "Standing Calf Raises": ["standing calf raise", "calf raise", "calf raises"],
    "Seated Calf Raise": ["seated calf raises"],
    "Donkey Calf Raises": ["donkey calf raise", "donkey calves"],
    "Calf Press On The Leg Press Machine": ["leg press calf raise", "calf press leg press"],
    "Dumbbell Lunges": ["dumbbell lunge", "db lunge", "db lunges", "dumbbell walking lunge"],
    "Barbell Lunge": ["barbell lunges", "bb lunge"],
    "Bodyweight Walking Lunge": ["walking lunge", "walking lunges"],
    "Dumbbell Step Ups": ["step up", "step ups", "dumbbell step up", "db step up"],
    "Barbell Step Ups": ["barbell step up"],

    # ---- Shoulders: press ----
    "Barbell Shoulder Press": ["overhead press", "ohp", "military press", "strict press", "barbell overhead press", "shoulder press", "barbell ohp", "standing barbell press"],
    "Seated Barbell Military Press": ["seated military press", "seated barbell press", "seated ohp", "seated strict press"],
    "Machine Shoulder (Military) Press": ["machine shoulder press", "shoulder press machine", "machine press"],
    "Seated Dumbbell Press": ["seated db press", "seated dumbbell shoulder press", "seated db shoulder press"],
    "Dumbbell Shoulder Press": ["db shoulder press", "dumbbell press", "db press"],
    "Standing Dumbbell Press": ["standing db press", "standing dumbbell shoulder press"],
    "Arnold Dumbbell Press": ["arnold press", "arnolds"],
    "Cable Shoulder Press": ["cable ohp"],
    "Standing Barbell Press Behind Neck": ["behind the neck press", "btn press", "behind neck press"],
    "Push Press": ["push presses"],
    "Push Press - Behind the Neck": ["behind the neck push press"],

    # ---- Shoulders: raises / rear delt ----
    "Side Lateral Raise": ["lateral raise", "lat raise", "side raise", "lateral raises", "db lateral raise", "dumbbell lateral raise", "side lateral raises"],
    "Seated Side Lateral Raise": ["seated lateral raise", "seated side lateral"],
    "Cable Seated Lateral Raise": ["seated cable lateral raise"],
    "Front Dumbbell Raise": ["front raise", "front raises", "db front raise", "dumbbell front raise"],
    "Front Cable Raise": ["cable front raise"],
    "Front Plate Raise": ["plate front raise", "plate raise"],
    "Reverse Flyes": ["rear delt fly", "rear delt flye", "reverse fly", "rear delt raise", "reverse flye", "bent over reverse fly"],
    "Reverse Machine Flyes": ["reverse pec deck", "rear delt machine", "machine reverse fly", "pec deck rear delt"],
    "Cable Rear Delt Fly": ["cable reverse fly", "cable rear delt raise"],
    "Face Pull": ["face pulls"],
    "Band Pull Apart": ["band pull aparts", "pull apart"],
    "Cuban Press": ["cuban rotation"],
    "Bradford/Rocky Presses": ["bradford press", "rocky press"],

    # ---- Traps ----
    "Barbell Shrug": ["shrug", "shrugs", "barbell shrugs", "bb shrug"],
    "Dumbbell Shrug": ["db shrug", "dumbbell shrugs", "db shrugs"],
    "Cable Shrugs": ["cable shrug"],

    # ---- Biceps ----
    "Barbell Curl": ["bicep curl", "barbell bicep curl", "bb curl", "barbell curls", "biceps curl"],
    "Close-Grip Standing Barbell Curl": ["close grip barbell curl"],
    "Wide-Grip Standing Barbell Curl": ["wide grip barbell curl", "wide grip curl"],
    "Dumbbell Bicep Curl": ["db curl", "dumbbell curl", "db bicep curl", "dumbbell curls", "dumbbell biceps curl"],
    "Dumbbell Alternate Bicep Curl": ["alternating dumbbell curl", "alternate db curl", "alternating bicep curl"],
    "Seated Dumbbell Curl": ["seated db curl", "seated dumbbell curls"],
    "Hammer Curls": ["hammer curl", "hammers", "dumbbell hammer curl"],
    "Cross Body Hammer Curl": ["cross body hammer curls", "crossbody hammer curl"],
    "Incline Hammer Curls": ["incline hammer curl"],
    "Preacher Curl": ["preacher", "preacher curls", "barbell preacher curl"],
    "Machine Preacher Curls": ["preacher curl machine", "machine preacher curl"],
    "Cable Preacher Curl": ["cable preacher curls"],
    "EZ-Bar Curl": ["ez bar curl", "ez curl", "ez-bar curls"],
    "Close-Grip EZ Bar Curl": ["close grip ez curl"],
    "Concentration Curls": ["concentration curl", "db concentration curl"],
    "Cable Hammer Curls - Rope Attachment": ["rope hammer curl", "cable hammer curl", "rope curl"],
    "Spider Curl": ["spider curls"],
    "Zottman Curl": ["zottman curls"],
    "Zottman Preacher Curl": ["zottman preacher curls"],
    "Incline Dumbbell Curl": ["incline curl", "incline db curl", "incline dumbbell curls"],
    "High Cable Curls": ["high cable curl"],
    "Standing Biceps Cable Curl": ["cable curl", "standing cable curl", "cable bicep curl"],
    "Lying Cable Curl": ["lying cable curls"],
    "Reverse Barbell Curl": ["reverse curl", "reverse barbell curls", "reverse grip curl"],
    "Standing Dumbbell Reverse Curl": ["dumbbell reverse curl", "db reverse curl"],
    "Drag Curl": ["drag curls"],
    "Cable Wrist Curl": ["cable wrist curls"],

    # ---- Triceps ----
    "Triceps Pushdown": ["tricep pushdown", "pushdown", "cable pushdown", "tricep pressdown", "tricep pushdowns", "pressdown"],
    "Triceps Pushdown - Rope Attachment": ["rope pushdown", "tricep rope pushdown", "rope tricep pushdown", "rope pressdown"],
    "Triceps Pushdown - V-Bar Attachment": ["v bar pushdown", "v-bar pushdown"],
    "Reverse Grip Triceps Pushdown": ["reverse grip pushdown", "underhand pushdown"],
    "EZ-Bar Skullcrusher": ["skullcrusher", "skull crusher", "skullcrushers", "lying tricep extension", "ez bar skullcrusher"],
    "Lying Triceps Press": ["lying tricep press", "lying barbell tricep extension"],
    "Decline EZ Bar Triceps Extension": ["decline skullcrusher", "decline tricep extension"],
    "Tricep Dumbbell Kickback": ["tricep kickback", "tricep kickbacks", "db kickback", "dumbbell kickback", "triceps kickback"],
    "Standing Dumbbell Triceps Extension": ["overhead tricep extension", "db overhead extension", "standing tricep extension", "overhead dumbbell extension"],
    "Standing Overhead Barbell Triceps Extension": ["overhead barbell extension", "barbell overhead tricep extension"],
    "Seated Triceps Press": ["seated overhead tricep extension", "seated barbell tricep extension"],
    "Cable Rope Overhead Triceps Extension": ["overhead rope extension", "rope overhead tricep extension", "overhead cable extension"],
    "Bench Dips": ["bench dip", "tricep bench dips"],
    "Weighted Bench Dip": ["weighted bench dips"],
    "Dumbbell One-Arm Triceps Extension": ["one arm overhead extension", "single arm tricep extension"],
    "JM Press": ["jm presses"],
    "Tate Press": ["tate presses"],

    # ---- Core ----
    "Crunches": ["crunch"],
    "Cable Crunch": ["cable crunches", "kneeling cable crunch"],
    "Rope Crunch": ["rope crunches"],
    "Standing Rope Crunch": ["standing cable crunch"],
    "Hanging Leg Raise": ["hanging leg raises", "hanging knee raise", "hanging knee raises"],
    "Flat Bench Lying Leg Raise": ["lying leg raise", "leg raises", "flat bench leg raise"],
    "Front Leg Raises": ["front leg raise"],
    "Knee/Hip Raise On Parallel Bars": ["captains chair", "captain's chair leg raise", "vertical leg raise"],
    "Plank": ["planks", "front plank", "forearm plank"],
    "Side Bridge": ["side plank", "side planks"],
    "Reverse Crunch": ["reverse crunches"],
    "Decline Reverse Crunch": ["decline reverse crunches"],
    "Cable Reverse Crunch": ["cable reverse crunches"],
    "Cross-Body Crunch": ["cross body crunch", "bicycle crunch"],
    "Russian Twist": ["russian twists"],
    "Cable Russian Twists": ["cable russian twist"],
    "Seated Barbell Twist": ["barbell twist", "barbell russian twist"],
    "Plate Twist": ["plate twists", "weighted russian twist"],
    "Pallof Press": ["pallof", "pallof presses"],
    "Pallof Press With Rotation": ["pallof rotation"],
    "Sit-Up": ["situp", "sit ups", "situps", "sit-ups"],
    "Weighted Crunches": ["weighted crunch"],
    "Decline Crunch": ["decline crunches", "decline sit up"],
    "Oblique Crunches": ["oblique crunch"],
    "Dead Bug": ["dead bugs", "deadbug"],
    "Mountain Climbers": ["mountain climber", "mountain climb"],
    "Flutter Kicks": ["flutter kick"],
    "Scissor Kick": ["scissor kicks"],
    "Superman": ["supermans", "superman hold"],
    "Stomach Vacuum": ["vacuum", "stomach vacuums"],
    "Ab Roller": ["ab wheel", "ab roller wheel"],
    "Barbell Ab Rollout": ["barbell rollout", "ab rollout", "barbell ab rollouts"],
    "Exercise Ball Crunch": ["stability ball crunch", "swiss ball crunch", "ball crunch"],
    "Jackknife Sit-Up": ["jackknife situp", "jackknife"],
    "Weighted Sit-Ups - With Bands": ["weighted situps"],

    # ---- Olympic / power ----
    "Clean and Jerk": ["c&j", "clean & jerk"],
    "Clean and Press": ["clean & press"],
    "Power Clean": ["power cleans"],
    "Hang Clean": ["hang cleans"],
    "Clean": ["squat clean", "full clean"],
    "Power Snatch": ["power snatches"],
    "Snatch": ["full snatch", "squat snatch"],
    "Hang Snatch": ["hang snatches"],
    "Power Jerk": ["power jerks"],
    "Split Jerk": ["split jerks"],
    "Squat Jerk": ["squat jerks"],
    "Clean Pull": ["clean pulls"],
    "Snatch Pull": ["snatch pulls"],
    "Clean Deadlift": ["clean grip deadlift"],
    "Snatch Deadlift": ["snatch grip deadlift"],
    "Muscle Up": ["muscle ups", "muscleup"],
    "Kipping Muscle Up": ["kipping muscle ups"],

    # ---- Kettlebell ----
    "Kettlebell Thruster": ["thruster", "thrusters", "kb thruster"],
    "One-Arm Kettlebell Swings": ["kettlebell swing", "kb swing", "one arm kettlebell swing", "kettlebell swings", "one arm kb swing"],
    "Kettlebell Turkish Get-Up (Squat style)": ["turkish get up", "tgu", "turkish getup"],
    "Kettlebell Turkish Get-Up (Lunge style)": ["turkish get up lunge style"],
    "Kettlebell Windmill": ["kb windmill", "kettlebell windmills"],
    "Kettlebell Figure 8": ["kettlebell figure eight", "figure 8"],

    # ---- Strongman / loaded carries ----
    "Farmer's Walk": ["farmers walk", "farmers carry", "farmer carry", "farmer's carry", "farmers walks"],
    "Tire Flip": ["tire flips"],
    "Atlas Stones": ["atlas stone", "stone lift"],
    "Yoke Walk": ["yoke carry", "yoke walks"],
    "Sled Push": ["prowler push", "sled pushes"],
    "Prowler Sprint": ["prowler sprints"],
    "Sledgehammer Swings": ["sledgehammer", "sledge swings"],
    "Battling Ropes": ["battle ropes", "battle rope", "battling rope"],
    "Rope Climb": ["rope climbs"],
    "Sled Drag - Harness": ["sled drag", "sled pull"],
    "Log Lift": ["log press", "log clean and press"],

    # ---- Plyometric / jumps ----
    "Box Jump (Multiple Response)": ["box jump", "box jumps"],
    "Front Box Jump": ["front box jumps"],
    "Lateral Box Jump": ["lateral box jumps", "side box jump"],
    "Knee Tuck Jump": ["tuck jump", "tuck jumps"],
    "Standing Long Jump": ["broad jump", "standing broad jump"],
    "Depth Jump Leap": ["depth jump"],
    "Star Jump": ["star jumps"],
    "Rocket Jump": ["rocket jumps"],
    "Freehand Jump Squat": ["jump squat", "jump squats", "bodyweight jump squat"],
    "Weighted Jump Squat": ["weighted jump squats", "loaded jump squat"],
    "Bench Jump": ["bench jumps"],
    "Frog Hops": ["frog hop", "frog jumps"],

    # ---- Cardio ----
    "Running, Treadmill": ["treadmill run", "treadmill running", "run", "running"],
    "Jogging, Treadmill": ["jog", "jogging", "treadmill jog"],
    "Walking, Treadmill": ["treadmill walk", "walking", "walk"],
    "Trail Running/Walking": ["trail run", "trail running", "trail walk"],
    "Bicycling, Stationary": ["stationary bike", "exercise bike", "spin bike", "spin", "stationary cycling"],
    "Bicycling": ["cycling", "bike ride", "outdoor cycling"],
    "Recumbent Bike": ["recumbent", "recumbent cycling"],
    "Air Bike": ["assault bike", "fan bike", "airdyne"],
    "Rowing, Stationary": ["rower", "erg", "rowing machine", "row erg", "rowing"],
    "Rope Jumping": ["jump rope", "skipping", "skip rope", "jumping rope"],
    "Elliptical Trainer": ["elliptical", "cross trainer"],
    "Stairmaster": ["stair master", "stairmaster machine"],
    "Step Mill": ["stepmill", "stairmill"],
    "Skating": ["inline skating"],
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
