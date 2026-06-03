# Starter exercise-library prompt

Paste the block below into your **trainer** connector (Claude Desktop / claude.ai) or
the in-app `/chat`. It drives the trainer AI to populate your real library via
`save_exercise`, writing to your live DB. It's idempotent — re-running it updates the
same records (known names update, unknown create), so you can run it again to extend
or refine. The trainer generates all content; nothing is hardcoded in the repo.

---

Build out my exercise library. First call `get_fitness_briefing` so you know my saved
profile (split, goals, and especially any injury cautions) and which exercises already
exist. Then, working through the list below, call `save_exercise` once per movement and
fill each one in **fully** so nothing shows "No saved technique notes yet":

- `muscles`, `secondary_muscles`, `tertiary_muscles` — emphasis tiers (primary = what
  the lift is for, secondary = real assistance, tertiary = lightly involved). Use only
  the canonical labels: chest, upper back, lats, traps, shoulders, biceps, triceps,
  forearms, abs, obliques, lower back, glutes, quads, hamstrings, calves. Cardio gets no
  muscle tiers.
- `category` (e.g. push / pull / legs / core / cardio) and `equipment` (barbell,
  dumbbell, kettlebell, machine, cable, bodyweight, etc.).
- `technique_notes` — concise setup + execution cues, the way you'd coach me through a
  working set.
- `common_mistakes` — the 2-3 errors you'd actually catch me making.
- `cautions` — injury-aware caveats; fold in anything from my profile.
- `video_link` and/or `image_link` — a good demo video and/or a looping form gif if you
  have a reliable one; otherwise leave blank rather than guess a dead URL.

Start with the lifts I actually train, then add common movements worth introducing.
Cover at least these (swap in my real ones where you know them):

- **Push:** barbell bench press, incline dumbbell press, overhead press, dips,
  triceps pushdown
- **Pull:** deadlift, barbell row, pull-up, lat pulldown, face pull, barbell/dumbbell curl
- **Legs:** back squat, front squat, Romanian deadlift, leg press, walking lunge,
  standing calf raise
- **Core:** plank, hanging leg raise, cable crunch
- **Cardio:** running, walking, rowing

Before you start, ask me which lifts I'm currently running and anything you should NOT
program around my injuries — then proceed. Go a handful at a time and tell me what you
saved.
