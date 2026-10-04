# How I actually use this — and whether the code agrees

Two lanes, each with one front door. Checked against `server.py`,
`webapp/combined.py` and `webapp/chat.py`, not just intent.

| Lane | Where I do it | Behind the tap-lock? |
|---|---|---|
| **Journal** — entries, people | Web UI → journal chat | ✅ yes |
| **Training** — workouts, exercises, weigh-ins, water & protein | **Claude, over the trainer connector** (the web trainer pages still work, legacy) | ❌ no |

---

## Lane 1 — Journal (web UI only)

Add entries by dumping text into the journal chat; ask about past entries and
people there too. Never through Claude.

**The code agrees, structurally.** The journal tools live on `server.mcp`, a
FastMCP instance that is **not served at all** — `webapp/combined.py` mounts no
journal endpoint. The only thing that drives it is the web app's chat
(`_AGENTS["journal"]`), which lifts the schemas with `list_tools` and calls the
functions in-process. There is no connector to hide anything from.

`add_journal_entry` · `link_mentions` · `save_person` · `update_contact` ·
`list_pending_mentions` · `list_people` · `get_person_history` ·
`search_entries` · `get_entry` · `update_entry` · `reorder_entries` ·
`journal_delete_entry` · `merge_people` · `get_related_people` · `get_briefing`

Tables: `entries` · `entries_fts` · `mentions` · `people` · `aliases` ·
`groups` · `person_groups`

---

## Lane 2 — Training (trainer connector)

`/trainer/mcp`, or its own host with its own Google OAuth when
`TRAINER_PUBLIC_URL` is set — the one MCP server. The journal is not reachable
from it: `server.trainer_mcp` is a different instance with no journal tool on it.

- **Tools** — `log_intake` · `get_intake` · `update_intake` · `list_exercises` ·
  `add_exercise` · `update_exercise` · `archive_exercise` · `log_workout` ·
  `get_exercise_history` · `get_personal_records` · `update_workout` ·
  `update_set` · `start_workout_plan` · `get_workout_plan` · `complete_sets` ·
  `remove_from_plan` · `swap_exercise` · `add_to_plan` · `reorder_plan` ·
  `finish_workout` · `import_weigh_ins` · `get_fitness_briefing` ·
  `update_profile` · `delete_record(workout|set|intake)`
- **Web carve-outs (website-only writes)** — the `/trainer` Coaching popover
  (`set_trainer_profile`), the `/food` Targets popover (`set_nutrient_targets` →
  `settings.eating_profile.targets`), the `/weight` scale-export upload
  (`import_bodyweight`), the `/graphs` weight goal.

Tables: `workouts` · `sets` · `exercises` · `exercise_muscles` ·
`exercise_aliases` · `body_weight` · `intake_items` (water_oz, protein_g) ·
`settings.profile` · `settings.eating_profile`

---

## Other

1. **Reading on the website.** Training and water/protein are captured in
   Claude but *read* on `/workouts`, `/food`, `/graphs`, `/weight` —
   capture-here/read-there is why `log_intake` returns a `url`.
2. **Backups.** `GET /export/journal.db` (`snapshot_db`), pulled by the launchd
   cron job.

---

## Removed (2026-10-04)

The journal connector at `/mcp` (it last carried only notes & collections), the
notes & collections layer itself, the full food tracker (now just water +
protein, on the trainer), the teacher learning server, and the Telegram bots.

## Dormant — reachable by nothing

`drinks` · `nutrition` · `intake_items` calories/carbs_g/fat_g/sodium_mg/
fiber_g/standard_drinks + the orphan `at_time` · `settings.eating_profile` prose
keys (only `targets` is read) · `collections` · `items` · `items_fts` ·
`subjects` · `facets` · `attempts` · `learn_fts`. Kept with their rows; nothing
reads or writes them, and fresh DBs don't create the collections/learning ones.
