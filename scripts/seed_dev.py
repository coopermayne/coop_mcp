"""
Seed the dev DB with mock data covering April-June 2026 — enough days/people for
the journal feed + calendar sidebar + people page to look populated. Idempotent
on people (looks them up by canonical name first); journal entries always append,
so re-running stacks duplicates. Run with:

    JOURNAL_DB=./journal_dev.db .venv/bin/python scripts/seed_dev.py
"""
import os
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server  # noqa: E402

server.init_db()


# --------------------------------------------------------------------------- #
# People

PEOPLE = [
    # (canonical_name, role, aliases, groups, summary)
    ("Hallie Black",   "wife",              ["Hallie", "Hal"],            ["Black Family", "Mayne Family"], "Cooper's wife. Architecture background; teaches at UCLA Extension."),
    ("Tom Mayne",      "father",            ["Dad", "Tom", "Pops"],       ["Mayne Family"],                 "Architect; lives in Santa Monica."),
    ("Blythe Mayne",   "mother",            ["Mom", "Mama", "Blythe"],    ["Mayne Family"],                 "Painter; teaches at Otis."),
    ("Sam Mayne",      "brother",           ["Sam", "Sammy"],             ["Mayne Family"],                 "Lives in Brooklyn; works in product."),
    ("Jody Black",     "mother-in-law",     ["Jody"],                     ["Black Family"],                 "Hallie's mom; based in Berkeley."),
    ("Jeff Black",     "father-in-law",     ["Jeff"],                     ["Black Family"],                 "Hallie's dad; retired civil engineer."),
    ("Ry Black",       "brother-in-law",    ["Ry", "Ryland"],             ["Black Family"],                 "Hallie's brother; software at a fintech."),
    ("Alex Chen",      "law school friend", ["Alex"],                     ["Friends"],                      "Met at Yale Law; works at a nonprofit in Oakland."),
    ("Maya Iyer",      "college friend",    ["Maya"],                     ["Friends"],                      "Climbing partner; works in product design."),
    ("Jordan Reese",   "neighbor",          ["Jordan"],                   ["Neighbors"],                    "Across-the-courtyard neighbor; has a beagle."),
    ("Priya Shah",     "manager",           ["Priya"],                    ["Work"],                         "Engineering manager; ex-Stripe."),
    ("Dev Krishnan",   "engineer",          ["Dev"],                      ["Work"],                         "Backend lead on the platform team."),
    ("Chris Park",     "designer",          ["Chris"],                    ["Work"],                         "Design partner on the new onboarding flow."),
    ("Dr. Patel",      "doctor",            ["Dr. Patel", "Patel"],       ["Care"],                         "Primary care; quarterly checkups."),
]

PID = {}
for name, role, aliases, groups, summary in PEOPLE:
    with server.db() as conn:
        row = conn.execute("SELECT id FROM people WHERE canonical_name=?", (name,)).fetchone()
    if row:
        PID[name] = row["id"]
        # ensure groups/aliases are present (save_person ADDS aliases, REPLACES groups)
        server.save_person(person_id=row["id"], aliases=aliases, groups=groups, summary=summary)
    else:
        r = server.save_person(canonical_name=name, role=role, aliases=aliases, groups=groups, summary=summary)
        PID[name] = r["person_id"]

print(f"people: {len(PID)}")


# --------------------------------------------------------------------------- #
# Journal entries
#
# Each tuple: (entry_date, body, mentions). Multiple entries on the same date
# exercise the per-topic split in the feed; days are spread across ~8 weeks so
# the sidebar calendar shows several months and a healthy density.

E = []

def add(d: date, body: str, mentions: list[str]):
    E.append((d.isoformat(), body, mentions))

apr5  = date(2026, 4, 5)
def days(*xs): return [apr5 + timedelta(days=x) for x in xs]

# Week of Apr 5
d = apr5
add(d, "Walked to the farmers market with Hallie. Picked up strawberries and that olive bread she likes. Quiet morning, no plans.", ["Hallie"])

d = apr5 + timedelta(days=2)
add(d, "Lunch with Alex in Oakland. He's thinking about leaving the nonprofit — burnout, mostly, plus the new ED is a mess. Walked along the lake after.", ["Alex"])
add(d, "Quick call with Priya about the Q2 roadmap. She wants to push the migration into May; I pushed back, we landed on staged rollout.", ["Priya"])

d = apr5 + timedelta(days=4)
add(d, "Dinner at Mom and Dad's. Sam was in town from Brooklyn — first time the three of us have been in the same room since Christmas. Mom made the lamb thing.", ["Mom", "Dad", "Sam"])

# Week of Apr 12
d = date(2026, 4, 13)
add(d, "Run before work, 4 miles. Legs felt heavy from Friday's session.", [])
add(d, "Standup ran long again. Dev demoed the new ingestion path — clean. Chris pulled me aside about the onboarding flow, he wants another design crit Thursday.", ["Dev", "Chris"])

d = date(2026, 4, 15)
add(d, "Hallie's birthday dinner at Gjelina. Maya and Jordan came; Maya brought a card she made. We split the lamb sausage and three desserts.", ["Hallie", "Maya", "Jordan"])

d = date(2026, 4, 17)
add(d, "Coffee with Jeff at Equator. He's reading a lot about retirement decumulation and wanted to talk through his approach. Smart guy.", ["Jeff"])

# Week of Apr 19
d = date(2026, 4, 19)
add(d, "Hike at Sugarloaf with Hallie and Ry. Wildflowers were peaking. Ry's training for a half marathon and set the pace the whole way up.", ["Hallie", "Ry"])

d = date(2026, 4, 21)
add(d, "Annual checkup with Dr. Patel. BP good, A1C edged up a tick. He wants me to cut back on the evening drinks.", ["Dr. Patel"])

d = date(2026, 4, 23)
add(d, "Long design review with Chris and Priya. Onboarding redesign is finally clicking — we cut three screens. Priya signed off.", ["Chris", "Priya"])
add(d, "Quick beer after work with Dev. He's debating taking the staff offer or trying to start something. I told him to take the staff job.", ["Dev"])

d = date(2026, 4, 25)
add(d, "Saturday morning with Hallie at the guest house. Coffee, both of us reading. She's deep in a Zadie Smith essay collection.", ["Hallie"])

# Week of Apr 26
d = date(2026, 4, 26)
add(d, "Brunch with Mom and Blythe and Hallie at Gjusta. The mom-and-mother-in-law summit. They both love the bakery, so it works as neutral ground.", ["Mom", "Hallie"])

d = date(2026, 4, 28)
add(d, "Hard conversation with Priya — Q2 priorities are slipping. I pushed for cutting the analytics revamp; she wants to keep it. Compromise: shrink scope.", ["Priya"])

d = date(2026, 4, 30)
add(d, "Sam called from Brooklyn — he might come out for a long weekend in late May. Apartment hunt is going badly.", ["Sam"])

# May
d = date(2026, 5, 2)
add(d, "Climbing with Maya at Stronghold. New problem on the slab wall I've been close to for a month — sent it, finally. She got a flash on V5.", ["Maya"])
add(d, "Dinner with Hallie. We talked about whether to look at houses in Mar Vista. Both nervous about the timing.", ["Hallie"])

d = date(2026, 5, 4)
add(d, "Mostly heads down. Dev unblocked the migration; we shipped behind a flag.", ["Dev"])

d = date(2026, 5, 6)
add(d, "Jordan stopped by with his beagle. Stayed for an hour talking about the building's parking situation. He's the unofficial mayor.", ["Jordan"])

d = date(2026, 5, 8)
add(d, "Drinks with Alex at the Alembic. He's interviewing at a climate think tank — better fit. Hopeful for him.", ["Alex"])

d = date(2026, 5, 10)
add(d, "Mother's Day. Brunch at Tom and Blythe's with Hallie and Jody. Both moms in one shot, Tom in good form. Dad showed off his latest model.", ["Tom", "Blythe", "Hallie", "Jody"])

d = date(2026, 5, 12)
add(d, "Standup, then a long unblocked stretch. Closed three bugs I'd been sitting on. Felt good.", [])

d = date(2026, 5, 14)
add(d, "Chris and I drove to Long Beach to user-test the new flow. Three sessions; the second one was brutal in a useful way. Notes ran 4 pages.", ["Chris"])

d = date(2026, 5, 16)
add(d, "Saturday: long run, then lunch with Maya. She's thinking about moving back to Chicago for a job. Bittersweet to hear.", ["Maya"])

d = date(2026, 5, 18)
add(d, "Mom called — Dad's having his hip looked at again. Years of running catching up. He's playing it down.", ["Mom", "Dad"])
add(d, "Dev shipped the analytics revamp v1. Priya was happy. I owe him a beer.", ["Dev", "Priya"])

d = date(2026, 5, 20)
add(d, "Beer with Ry — he's in town for a work thing. We caught up on his half-marathon training and his apartment search.", ["Ry"])

d = date(2026, 5, 22)
add(d, "Lunch walk with Jordan around the block. Building HOA stuff. He's running for board.", ["Jordan"])

d = date(2026, 5, 24)
add(d, "Saturday with Hallie at the guest house. She made a coffee cake. I worked on the journal app for a few hours.", ["Hallie"])

d = date(2026, 5, 25)
add(d, "Sam flew in for the long weekend. Drove him from LAX, dinner at El Coyote. Mom and Dad joined.", ["Sam", "Mom", "Dad"])

d = date(2026, 5, 26)
add(d, "Beach day with Sam and Hallie. Long walk to the pier, fish tacos, sunburn.", ["Sam", "Hallie"])
add(d, "Late call with Priya — incident in the migration, rolling back the flag. Three hours debugging with Dev. Resolved by midnight.", ["Priya", "Dev"])

d = date(2026, 5, 27)
add(d, "Post-mortem with the team. Dev was great about it. Chris joined to take notes for the design implications.", ["Dev", "Chris"])

d = date(2026, 5, 28)
add(d, "Dropped Sam at LAX. Quiet evening with Hallie. We watched the new season opener.", ["Sam", "Hallie"])

d = date(2026, 5, 30)
add(d, "Saturday: hike with Alex up to Inspiration Point. We talked through his think-tank offer. He's leaning yes.", ["Alex"])

# June (current week)
d = date(2026, 6, 1)
add(d, "Back to the guest house to work on the computer for a while next to Hallie.", ["Hallie"])
add(d, "Back home, I saw Thom in the main house and stopped to say hello while Hallie went to the guest house. Talked with Thom and Blythe a bit; had a Lillet and a cookie.", ["Thom", "Blythe", "Hallie"])

# Map every surface form we used (canonical name + every alias) → person_id, so we
# can auto-resolve pending mentions instead of leaving the model to do it. In real
# use Claude calls link_mentions in the conversation; here we just want clean data.
SURFACE_TO_PID = {}
for name, role, aliases, groups, summary in PEOPLE:
    for s in [name, *aliases]:
        SURFACE_TO_PID[s.lower()] = PID[name]

written = []
for ed, body, mentions in E:
    r = server.add_journal_entry(body=body, raw_body=body, mentions=mentions, entry_date=ed)
    written.append(r)

# Resolve every pending mention whose surface form maps to a known person.
with server.db() as conn:
    pending = conn.execute(
        "SELECT id, surface_form FROM mentions WHERE status='pending'"
    ).fetchall()
    resolved = 0
    for m in pending:
        pid = SURFACE_TO_PID.get(m["surface_form"].lower())
        if pid is not None:
            conn.execute(
                "UPDATE mentions SET person_id=?, status='resolved' WHERE id=?",
                (pid, m["id"]),
            )
            resolved += 1
print(f"entries written: {len(E)}; mentions auto-resolved: {resolved}")

# --------------------------------------------------------------------------- #
# Drinks — scattered, mostly social, a few sober stretches

DRINKS = [
    ("2026-04-05", 1.0, "wine", None),
    ("2026-04-07", 2.0, "beer", "dinner out"),
    ("2026-04-09", 1.5, "cocktail", None),
    ("2026-04-15", 3.0, "wine", "Hallie's birthday"),
    ("2026-04-17", 1.0, "beer", None),
    ("2026-04-23", 1.5, "beer", "with Dev"),
    ("2026-04-25", 1.0, "wine", None),
    ("2026-04-26", 2.0, "mimosa", "brunch"),
    ("2026-04-28", 1.5, "wine", None),
    ("2026-05-02", 1.0, "wine", "with Hallie"),
    ("2026-05-08", 2.5, "cocktail", "with Alex"),
    ("2026-05-10", 1.5, "mimosa", None),
    ("2026-05-14", 1.0, "beer", None),
    ("2026-05-16", 1.0, "wine", None),
    ("2026-05-20", 2.0, "beer", "with Ry"),
    ("2026-05-24", 1.0, "wine", None),
    ("2026-05-25", 3.0, "margarita", "El Coyote"),
    ("2026-05-26", 2.0, "beer", "beach day"),
    ("2026-05-30", 1.0, "beer", None),
    ("2026-06-01", 1.0, "wine", "Lillet"),
]
for dd, n, kind, notes in DRINKS:
    # Alcohol is a nutrient on the intake row now — same path as food.
    server.log_intake(item=kind, food_date=dd, standard_drinks=n, note=notes)
print(f"drinks logged: {len(DRINKS)}")


# --------------------------------------------------------------------------- #
# Workouts — a mix of pushes, pulls, legs, runs, walks across the period.

# Seed exercise catalog briefly so muscles aggregate correctly
EXERCISES = [
    # (name, category, [muscles], technique_notes) — muscle labels from the
    # canonical list in save_exercise's docstring so per-muscle recency lines up.
    ("Bench Press",       "push",   ["chest", "triceps", "shoulders"],            "horizontal push"),
    ("Overhead Press",    "push",   ["shoulders", "triceps"],                     "vertical push"),
    ("Incline DB Press",  "push",   ["chest", "shoulders", "triceps"],            "incline push"),
    ("Pull Up",           "pull",   ["lats", "biceps", "middle back"],            "vertical pull"),
    ("Barbell Row",       "pull",   ["lats", "middle back", "biceps"],            "horizontal pull"),
    ("Deadlift",          "pull",   ["hamstrings", "glutes", "lower back"],       "hinge"),
    ("Back Squat",        "legs",   ["quadriceps", "glutes"],                          "squat"),
    ("Romanian Deadlift", "legs",   ["hamstrings", "glutes"],                     "hinge"),
    ("Walking Lunge",     "legs",   ["quadriceps", "glutes"],                          "unilateral"),
    ("Running",           "cardio", [],                                            "outdoor run"),
    ("Walking",           "cardio", [],                                            "outdoor walk"),
]
# create_exercise is the library's write path (the catalog is closed to the AI's
# save_exercise, which can only enrich/archive existing rows).
for name, category, muscles, technique in EXERCISES:
    server.create_exercise(name=name, category=category, muscles=muscles,
                           technique_notes=technique)

WORKOUTS = [
    # (date, focus, feeling, notes, [(exercise, [{weight, reps, rpe} or {duration_seconds, distance_miles}])])
    ("2026-04-06", "push", "good", None, [
        ("Bench Press",     [{"weight_lbs": 155, "reps": 8, "rpe": 7},
                              {"weight_lbs": 175, "reps": 5, "rpe": 8},
                              {"weight_lbs": 175, "reps": 5, "rpe": 8.5}]),
        ("Overhead Press",  [{"weight_lbs": 95,  "reps": 8, "rpe": 7.5},
                              {"weight_lbs": 105, "reps": 5, "rpe": 8.5}]),
        ("Incline DB Press",[{"weight_lbs": 55,  "reps": 10, "rpe": 7},
                              {"weight_lbs": 55,  "reps": 10, "rpe": 7.5}]),
    ]),
    ("2026-04-08", "pull", "fine", None, [
        ("Pull Up",      [{"weight_lbs": 0, "reps": 8, "rpe": 7},
                          {"weight_lbs": 0, "reps": 7, "rpe": 8}]),
        ("Barbell Row",  [{"weight_lbs": 135, "reps": 8, "rpe": 7},
                          {"weight_lbs": 155, "reps": 6, "rpe": 8}]),
    ]),
    ("2026-04-10", "legs", "tired", "long day", [
        ("Back Squat",        [{"weight_lbs": 185, "reps": 5, "rpe": 7.5},
                                {"weight_lbs": 215, "reps": 3, "rpe": 8.5}]),
        ("Romanian Deadlift", [{"weight_lbs": 185, "reps": 8, "rpe": 7}]),
    ]),
    ("2026-04-13", "run", "good", "easy", [
        ("Running", [{"duration_seconds": 32 * 60, "distance_miles": 4.0}]),
    ]),
    ("2026-04-15", "walk", "light", "after dinner", [
        ("Walking", [{"duration_seconds": 45 * 60, "distance_miles": 2.4}]),
    ]),
    ("2026-04-17", "push", "good", None, [
        ("Bench Press",     [{"weight_lbs": 155, "reps": 8, "rpe": 7},
                              {"weight_lbs": 180, "reps": 4, "rpe": 8.5}]),
        ("Overhead Press",  [{"weight_lbs": 95,  "reps": 8, "rpe": 7.5}]),
    ]),
    ("2026-04-22", "pull", "good", None, [
        ("Deadlift",  [{"weight_lbs": 275, "reps": 5, "rpe": 8},
                       {"weight_lbs": 295, "reps": 3, "rpe": 8.5}]),
        ("Pull Up",   [{"weight_lbs": 0, "reps": 9, "rpe": 7.5}]),
    ]),
    ("2026-04-27", "legs", "good", None, [
        ("Back Squat", [{"weight_lbs": 195, "reps": 5, "rpe": 8}]),
        ("Walking Lunge", [{"weight_lbs": 30, "reps": 12, "rpe": 7}]),
    ]),
    ("2026-05-02", "climb", None, "Stronghold session", [
        ("Walking", [{"duration_seconds": 25 * 60, "distance_miles": 1.4}]),
    ]),
    ("2026-05-04", "push", "fine", None, [
        ("Bench Press",     [{"weight_lbs": 160, "reps": 8, "rpe": 7.5},
                              {"weight_lbs": 180, "reps": 5, "rpe": 8.5}]),
        ("Incline DB Press",[{"weight_lbs": 60,  "reps": 8, "rpe": 8}]),
    ]),
    ("2026-05-07", "run", "good", None, [
        ("Running", [{"duration_seconds": 48 * 60, "distance_miles": 6.0}]),
    ]),
    ("2026-05-11", "legs", "tired", None, [
        ("Back Squat", [{"weight_lbs": 200, "reps": 5, "rpe": 8.5}]),
        ("Romanian Deadlift", [{"weight_lbs": 205, "reps": 6, "rpe": 8}]),
    ]),
    ("2026-05-15", "push", "good", None, [
        ("Overhead Press", [{"weight_lbs": 100, "reps": 6, "rpe": 8}]),
        ("Bench Press",    [{"weight_lbs": 165, "reps": 8, "rpe": 8}]),
    ]),
    ("2026-05-18", "pull", "good", None, [
        ("Deadlift",  [{"weight_lbs": 300, "reps": 3, "rpe": 8.5}]),
        ("Barbell Row",  [{"weight_lbs": 155, "reps": 8, "rpe": 8}]),
    ]),
    ("2026-05-21", "run", "ok", None, [
        ("Running", [{"duration_seconds": 35 * 60, "distance_miles": 4.5}]),
    ]),
    ("2026-05-23", "walk", "light", "Saturday wander", [
        ("Walking", [{"duration_seconds": 65 * 60, "distance_miles": 3.2}]),
    ]),
    ("2026-05-28", "push", "good", None, [
        ("Bench Press",     [{"weight_lbs": 170, "reps": 5, "rpe": 8},
                              {"weight_lbs": 180, "reps": 5, "rpe": 9}]),
        ("Incline DB Press",[{"weight_lbs": 60,  "reps": 10, "rpe": 8.5}]),
    ]),
    ("2026-05-31", "pull", "good", None, [
        ("Pull Up",  [{"weight_lbs": 0, "reps": 10, "rpe": 7.5},
                       {"weight_lbs": 0, "reps": 9, "rpe": 8.5}]),
        ("Barbell Row", [{"weight_lbs": 165, "reps": 6, "rpe": 8.5}]),
    ]),
]
for wd, focus, feeling, notes, exes in WORKOUTS:
    server.log_workout(
        workout_date=wd, focus=focus, feeling=feeling, notes=notes,
        exercises=[{"name": n, "sets": sets} for n, sets in exes],
    )
print(f"workouts logged: {len(WORKOUTS)}")


# --------------------------------------------------------------------------- #
# Bodyweight — a gentle downward trend over the period (weight-loss journey),
# logged on training days near the workout.
WEIGHTS = [
    ("2026-04-12", 198.6), ("2026-04-26", 197.2), ("2026-05-10", 195.8),
    ("2026-05-18", 194.9), ("2026-05-28", 193.4), ("2026-05-31", 192.8),
]
for wd, lbs in WEIGHTS:
    server.log_bodyweight(weight_lbs=lbs, weigh_date=wd)
print(f"bodyweight readings logged: {len(WEIGHTS)}")


# --------------------------------------------------------------------------- #
# Profile
server.update_profile({
    "injuries": "occasional right shoulder twinge on overhead pressing",
    "split": "upper/lower with 1-2 runs per week",
    "goals": "stay healthy; nudge bench past 200lb by EOY; sub-25 5k",
})
print("profile updated.")


# --------------------------------------------------------------------------- #
# Collections + items
#
# Breadth over volume: the point is to exercise the RENDERING, so between them
# these four collections cover every field type (text/number/date/select), all
# three views, grouped and ungrouped, every image size, items with and without a
# featured image, and declared fields left unfilled. Plus loose inbox notes, so
# /collections has something in its bottom half.
#
# Idempotent: collections are looked up by name and items by (collection, title),
# so re-running edits in place rather than stacking duplicates.

# picsum gives a stable photo per seed, so a re-run doesn't reshuffle the grid.
def photo(seed: str) -> str:
    return f"https://picsum.photos/seed/{seed}/800/600"


COLLECTIONS = [
    ("Recipes", "Things worth cooking twice.", "chef-hat", [
        {"key": "cuisine", "label": "Cuisine", "type": "select",
         "options": ["Italian", "Sichuan", "Mexican", "Japanese", "Levantine", "American"]},
        {"key": "time_min", "label": "Time", "type": "number", "unit": "min"},
        {"key": "source", "label": "Source", "type": "text"},
    ]),
    ("Reading", "Books in, out, and abandoned.", "book-open", [
        {"key": "author", "label": "Author", "type": "text"},
        {"key": "status", "label": "Status", "type": "select",
         "options": ["Reading", "Finished", "Queued", "Abandoned"]},
        {"key": "finished", "label": "Finished", "type": "date"},
        {"key": "rating", "label": "Rating", "type": "number"},
    ]),
    ("Trip ideas", "Places to go when there's a week free.", "plane", [
        {"key": "region", "label": "Region", "type": "select",
         "options": ["California", "Southwest", "Pacific NW", "Mexico", "Japan", "Europe"]},
        {"key": "season", "label": "Best season", "type": "select",
         "options": ["Spring", "Summer", "Fall", "Winter"]},
        {"key": "nights", "label": "Nights", "type": "number", "unit": "nights"},
    ]),
    # Deliberately field-less: a collection can be just titles and prose, and the
    # page has to look composed with no badges, no columns, no images.
    ("Sayings", "Lines worth keeping.", "quote", []),
]

for name, desc, icon, fields in COLLECTIONS:
    server.save_collection(name=name, description=desc, icon=icon,
                           fields=fields, force=True)
print(f"collections: {len(COLLECTIONS)}")


# (collection, title, data, body, image-seed or None)
ITEMS = [
    # -- Recipes: images on most, one without, one missing every declared field.
    ("Recipes", "Sunday ragù", {"cuisine": "Italian", "time_min": 240, "source": "Marcella Hazan"},
     "Soffritto low and slow — 45 minutes before the meat goes anywhere near it.\n\n"
     "Milk first, then wine, then tomato. The milk is the whole trick; skip it and it's\n"
     "just a red sauce with beef in it.\n\n**Serves 6.** Freezes well in pint containers.", "ragu"),
    ("Recipes", "Mapo tofu", {"cuisine": "Sichuan", "time_min": 35, "source": "Fuchsia Dunlop"},
     "Doubanjiang is the whole dish — the Pixian stuff, not the generic chili bean paste.\n\n"
     "Fry it until the oil goes red before anything else joins. Silken tofu, cubed, slid in\n"
     "at the end and pushed around with the back of the ladle so it doesn't break.", "mapo"),
    ("Recipes", "Weeknight carnitas", {"cuisine": "Mexican", "time_min": 180, "source": "adapted from Kenji"},
     "Shoulder, orange, bay, lard. Oven at 275 until it shreds, then broil the tray for the\n"
     "crispy edges — that step is not optional.", "carnitas"),
    ("Recipes", "Shoyu tamago", {"cuisine": "Japanese", "time_min": 15, "source": "kitchen improv"},
     "Six and a half minutes, ice bath, then overnight in soy + mirin + a little dashi.", "tamago"),
    ("Recipes", "Muhammara", {"cuisine": "Levantine", "time_min": 20, "source": "Ottolenghi"},
     "Roasted red pepper, walnut, pomegranate molasses, aleppo. Should be coarse, not smooth —\n"
     "stop the processor while it still has texture.", "muhammara"),
    ("Recipes", "Buttermilk pancakes", {"cuisine": "American", "time_min": 25},
     "The one we actually make. Rest the batter 10 minutes or they're tough.", "pancakes"),
    # No image, no fields filled: the row that has to hold its own on a page of photos.
    ("Recipes", "Dad's grilled artichokes", {},
     "Steam first, then halve, scoop the choke, oil hard, and grill cut-side down until\n"
     "there are real black marks. Lemon aioli. Never written down before now.", None),

    # -- Reading: grouped by status, so several buckets + a missing-value bucket.
    ("Reading", "The Overstory", {"author": "Richard Powers", "status": "Finished",
                                  "finished": "2026-03-02", "rating": 5},
     "The first 150 pages are the best short-story collection I've read. It sags in the middle\n"
     "and then earns all of it back.", "overstory"),
    ("Reading", "Piranesi", {"author": "Susanna Clarke", "status": "Finished",
                             "finished": "2026-04-11", "rating": 5},
     "Read it in two sittings. Say nothing about it to anyone before they read it.", "piranesi"),
    ("Reading", "Seeing Like a State", {"author": "James C. Scott", "status": "Reading"},
     "Legibility as the thing states want and the thing that kills the local knowledge they\n"
     "were trying to organize. Halfway; the forestry chapter alone was worth it.", "state"),
    ("Reading", "The Power Broker", {"author": "Robert Caro", "status": "Reading"},
     "Year two. Genuinely might finish it this time — I'm past the Triborough.", None),
    ("Reading", "Intermezzo", {"author": "Sally Rooney", "status": "Queued"},
     "Hallie finished it in a weekend and has been waiting for me to catch up.", "intermezzo"),
    ("Reading", "The Master and Margarita", {"author": "Mikhail Bulgakov", "status": "Queued"},
     "", "margarita"),
    ("Reading", "Infinite Jest", {"author": "David Foster Wallace", "status": "Abandoned",
                                  "rating": 2},
     "Page 340, twice, four years apart. Calling it.", None),
    # Status left blank on purpose — this one has to land in the trailing bucket.
    ("Reading", "A Fine Balance", {"author": "Rohinton Mistry"},
     "Recommended by Maya. Haven't decided whether I'm up for it.", None),

    # -- Trip ideas: the cards view, so mostly photos.
    ("Trip ideas", "Lost Coast traverse", {"region": "California", "season": "Fall", "nights": 3},
     "Shelter Cove to Mattole, 25 miles, and the tide table decides the schedule rather than\n"
     "you. Need to book the shuttle well ahead.", "lostcoast"),
    ("Trip ideas", "Oaxaca for Día de Muertos", {"region": "Mexico", "season": "Fall", "nights": 6},
     "Everyone says go, and everyone says book a year out. Mezcal, mole, the markets.", "oaxaca"),
    ("Trip ideas", "Kiso Valley walk", {"region": "Japan", "season": "Spring", "nights": 5},
     "Magome to Tsumago on the old post road, staying in ryokan along the way. The easy version\n"
     "of a walking trip — bags get forwarded.", "kiso"),
    ("Trip ideas", "Olympic peninsula loop", {"region": "Pacific NW", "season": "Summer", "nights": 4},
     "Hoh rainforest, Rialto Beach, hot springs. Rent something with a real roof.", "olympic"),
    ("Trip ideas", "Marfa", {"region": "Southwest", "season": "Spring", "nights": 3},
     "Chinati, the lights, and not much else, which is the point.", "marfa"),
    ("Trip ideas", "Puglia in the shoulder season", {"region": "Europe", "nights": 8},
     "Masseria, orecchiette, nobody there in October. Season left blank because May works too.", "puglia"),

    # -- Sayings: no fields at all, no images. Pure prose rows.
    ("Sayings", "On finishing", {},
     "“The perfect is the enemy of the shipped.” — Priya, in about four different standups.", None),
    ("Sayings", "Dad on drawing", {},
     "“If you can't draw it small, you don't understand it yet.”", None),
    ("Sayings", "On travel", {},
     "“Two weeks in one place beats four cities in ten days.” Learned the expensive way.", None),
]

for coll, title, data, body, seed in ITEMS:
    with server.db() as conn:
        row = conn.execute(
            """SELECT i.id FROM items i JOIN collections c ON c.id = i.collection_id
               WHERE i.title=? AND lower(c.name)=?""", (title, coll.lower())).fetchone()
    img = photo(seed) if seed else None
    if row:
        server.update_item(item_id=row["id"], body=body, data=data,
                           featured_image_url=img or "")
    else:
        server.save_item(title=title, body=body, collection=coll, data=data,
                         featured_image_url=img)
print(f"collection items: {len(ITEMS)}")


# Loose inbox notes — capture-first-file-second, so the inbox is never empty.
NOTES = [
    ("Guest room paint", "Hallie's leaning toward the greyed-green. Get samples on the wall\n"
                         "before deciding — the north light in there kills anything cool.", None),
    ("Bike tune-up", "Rear derailleur is skipping under load in the 3 smallest cogs.", None),
    ("Gift ideas for Mom", "The Japanese pruning shears she kept picking up at the nursery.\n"
                           "Backup: a print from the Otis faculty show.", None),
    ("That wine from Gjelina", "Nerello Mascalese, Etna. Ask them next time — the label was\n"
                               "hand-drawn, mostly white, with a mountain on it.", "wine"),
    ("Podcast rec from Dev", "Something about the history of container shipping. He said the\n"
                             "third episode is the one.", None),
]
for title, body, seed in NOTES:
    with server.db() as conn:
        row = conn.execute(
            "SELECT id FROM items WHERE title=? AND collection_id IS NULL", (title,)).fetchone()
    img = photo(seed) if seed else None
    if row:
        server.update_item(item_id=row["id"], body=body, featured_image_url=img or "")
    else:
        server.save_item(title=title, body=body, featured_image_url=img)
print(f"inbox notes: {len(NOTES)}")


# Display prefs — the webapp-only half. One collection per view, so a pass
# through /collections shows all three without touching a popover.
server.set_collection_display("Recipes", view="cards", image_size="medium",
                              sort_by="title", sort_dir="asc", show_body=True)
server.set_collection_display("Reading", view="table", group_by="status",
                              sort_by="title", sort_dir="asc", image_size="small")
server.set_collection_display("Trip ideas", view="list", group_by="region",
                              image_size="medium", show_body=True, sort_by="title")
server.set_collection_display("Sayings", view="list", image_size="off", show_body=True)
print("display prefs set.")
