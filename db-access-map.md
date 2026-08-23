# How I actually use this — and whether the code agrees

Three lanes, each with one front door. Everything below is checked against
`server.py` / `webapp/chat.py`, not just intent.

| Lane | Where I do it | Behind the tap-lock? |
|---|---|---|
| **Journal** — entries, people | Web UI → journal chat | ✅ yes |
| **Training** — workouts, exercises | Web UI → **trainer** chat + plan card | ❌ no |
| **Food** — intake, notes, collections | **Claude, over MCP** | n/a |

---

## Lane 1 — Journal (web UI only)

Add entries by dumping text into the journal chat; ask about past entries and
people there too. Never through Claude.

**The code agrees, and it's enforced.** `HiddenToolsMiddleware`
(`CONNECTOR_HIDDEN_TOOLS`, `server.py:261`) drops all fifteen people/entry tools
from the connector's `tools/list` **and** rejects them on `tools/call`. The app
chat bypasses middleware (`list_tools(run_middleware=False)` + direct calls), so
it keeps them.

`add_journal_entry` · `update_entry` · `reorder_entries` · `get_entry` ·
`search_entries` · `link_mentions` · `list_pending_mentions` · `get_briefing` ·
`journal_delete_entry` · `save_person` · `update_contact` · `list_people` ·
`get_person_history` · `get_related_people` · `merge_people`

Tables: `entries` · `entries_fts` · `mentions` · `people` · `aliases` ·
`groups` · `person_groups`

> **This is the only capture with a single door.** Nothing else in the app is
> reachable from exactly one surface.

---

## Lane 2 — Training (web UI, separate portal)

Trainer chat on the trainer pages, plus tapping sets on the plan card.

**"I shouldn't be able to access the journal from there" — already true.** The
trainer agent binds to `server.trainer_mcp` (`webapp/chat.py:273`), a different
FastMCP instance. There is no journal tool in its list to exclude; the split is
structural, not a filter.

- **Chat tools** — `log_workout` · `start_workout_plan` · `add_to_plan` ·
  `get_workout_plan` · `complete_set` · `update_set` · `update_workout` ·
  `swap_exercise` · `reorder_plan` · `finish_workout` ·
  `get_exercise_history` · `get_personal_records` · `get_fitness_briefing` ·
  `find_exercises` · `save_exercise` · `set_rotation` · `set_hearted` ·
  `update_profile` · `delete_record(workout|set)`
- **Plan card, no chat** — tap-to-log sets, ↑/↓ reorder, remove, discard, finish
  (`complete_set`, `reorder_plan_exercises`, `remove_plan_exercise`,
  `discard_plan`, `clear_plan_set`, `pr_for_set`)
- **Library** — *+ Add an exercise* runs the webapp-only `exercise` agent
  (`create_exercise`); ★/♥ toggles; archive
- **Coaching popover** — `set_trainer_profile` → `settings.profile.coaching`

Tables: `workouts` · `sets` · `exercises` · `exercise_muscles` ·
`exercise_aliases` · `settings.profile`

---

## Lane 3 — Food, notes & collections (MCP only)

Photos and text to Claude; Claude writes over the connector. Never typed into
the web UI.

**The code agrees — `/food` content is strictly read-only.** No form, no
tappable ring, no `/intake` write route. The browser only renders what was
logged.

- **Eating** — `intake_log` · `intake_update` · `intake_delete` ·
  `intake_summary` · `intake_find_past` · `intake_set_profile`
- **Notes** — `notes_save` · `notes_update` · `notes_file` · `notes_get` ·
  `notes_list` · `notes_search` · `notes_delete` · `notes_geocode`
- **Collections** — `collections_list` · `collections_save` ·
  `collections_delete` · `collections_list_icons`

Tables: `intake_items` · `items` · `items_fts` · `collections` ·
`settings.eating_profile`

The web UI's job here is **reading**: `/food` rings, `/collections` grid, item
pages. Plus two popovers that write *goals and layout*, never content —
**Targets** (`set_nutrient_targets`) and **Display** (`set_collection_display`).

---

## What I left out

Four things that are part of how this gets used, with no lane above:

1. **Weigh-ins.** Daily smart-scale readings, uploaded periodically as the
   scale app's `.xlsx` on `/weight` → `import_bodyweight`. **Import is the only
   write path that exists** — every hand-entry route was deleted, and there's no
   MCP tool. The trainer can *read* the trend (`get_fitness_briefing`) and
   cannot log one.
2. **Reading on the website.** Food is captured in Claude but *read* on
   `/food`; same for `/collections`, `/graphs`, `/journal`, `/workouts`,
   `/weight`. Capture-here/read-there is the standing shape — it's why the
   connector's writes return a `url`.
3. **The plan card is a write surface without a chat.** Most sets get logged by
   tapping, not by talking to the trainer.
4. **Backups.** `GET /export/journal.db` (`snapshot_db`), pulled by the launchd
   cron job.

---

## Lane boundaries — all three now enforced

**Fixed 2026-08-23: the journal chat no longer carries the food tools.**
`_AGENTS["journal"]` now takes `"include": server.CONNECTOR_HIDDEN_TOOLS`, so
the panel and the connector are exact **complements** over one FastMCP instance:

```
journal instance ─┬─ connector /mcp  →  18 tools : intake_* notes_* collections_*
                  └─ app chat panel  →  15 tools : entries + people
                                        (0 overlap, verified)
```

Stated as the complement of one frozenset rather than a second hand-kept list,
so the halves can't drift: adding a journal tool means adding its name to
`CONNECTOR_HIDDEN_TOOLS` (already the rule) and it lands on both sides at once;
adding an intake or notes tool needs no chat change at all.

The prompt was trimmed to match — no describing tools that aren't there — and
`_JOURNAL_ONLY_BLOCK` handles the cost: a meal mentioned mid-journal is now
**part of the entry**, written into the note like any other detail, rather than
an apology or an offer to log it somewhere this panel can't reach.

**Trainer connector: kept.** `/trainer/mcp` stays live — its own instance, own
OAuth provider, own host — as the phone fallback, even though training happens
in the web UI today.

---

## Dormant — reachable by nothing

`drinks` (legacy; kept as fold-in source + per-day `kind`) ·
`nutrition` (folded into `intake_items`) ·
`collections.display_hint` (folded into `display` JSON) ·
`intake_items.at_time` (orphan column) ·
`items.tags` (dropped, `items_fts` rebuilt)
