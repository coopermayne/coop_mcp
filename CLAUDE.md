# CLAUDE.md

Orientation for working in this repo with Claude Code. Read this first.

## What this is

A single-user **journal web app plus a trainer MCP server**, one process, one SQLite
DB. The user writes the journal by talking to the web app's own chat; Claude captures
entries and resolves *who* they mean to stable person records, so later "everything
about Tom my father" is an exact lookup that never pulls in the other Tom. Training
(and a small daily water/protein log) is done entirely through the **trainer
connector** in Claude.

**One MCP server, one in-process tool registry.** `trainer_mcp` is the only MCP
endpoint — at `/trainer/mcp` on the main origin when authless, or on its own host
(`TRAINER_PUBLIC_URL`) with its own Google OAuth. The journal's people/entry tools live
on a second FastMCP instance, `mcp`, that is NOT served at all: it exists so
`webapp/chat.py` can lift tool schemas from the docstrings and call the functions
in-process. (There used to be a journal connector at `/mcp` — first carrying the
eating log and notes/collections, with the journal tools hidden — and a third
`teacher_mcp` learning server; the connector, the eating log beyond water/protein,
notes & collections, and the teacher were all removed. Their tables stay in existing
DBs, dormant.) `webapp/combined.py` composes the trainer and the UI onto one origin.

**The web app is the journal, and only the journal.** The user trains entirely
through the trainer connector in Claude (planning the week, reporting sets between
lifts, progress and advice, water/protein, weigh-ins), so the web training UI — the
`/workouts` hub, the `/trainer` plan cards, `/weight`, `/graphs`, the `/food`
water/protein page and the trainer chat panel — was removed. Every workflow those
pages served has a trainer tool: `complete_sets`, `remove_from_plan`, `add_exercise`,
`import_weigh_ins`, `set_intake_targets`. `complete_sets` and `log_workout` return
`new_prs` (see the `workouts` row) because there's no screen to celebrate on. The
`trainer_mcp` instructions open by saying the conversation IS the interface (plan as a
table, ids never shown, short mid-session replies). Don't add trainer UI to the web
app. The one non-journal web endpoint left is `/api/today.json` (the SwiftBar
water/protein widget).

## The one architectural rule

**There is no LLM inside the server, and there must never be one.** The server is a
deterministic data + candidate-matching layer. The contextual judgment ("which Tom?")
is done by Claude in the conversation, using the candidates the server returns. When
adding features, keep that split: the server generates candidates / stores / retrieves;
the model decides. Don't add model calls, embeddings services, or NER inside the server.

The rule is about `server.py`, not the whole repo. The **webapp does contain an LLM** —
`webapp/chat.py` is the web app acting as an *MCP client*, driving the same
`@mcp.tool()` functions over the Anthropic API (in-process, no transport) so the
phone/browser gets conversational capture for the journal proper. That preserves
the split rather than breaking it: the model still does the judgment, `server.py` stays
the deterministic data layer with no LLM inside it. So `anthropic` in
`webapp/requirements.txt` is expected — it lives on the client side of the line.

The same split governs the **trainer** (and its water/protein log): the server stores
workouts and intake and computes deterministic aggregates (muscle recency, day totals),
but deciding the next weight, what to rest, which exercises to program, and how to coach
form is the *model's* job, done in conversation from the data the retrieval tools return.
There is no exercise-selection or progression logic in the server either.

## Other load-bearing design decisions

- **People, not names.** A reference resolves to a person *entity* (`people`), not a
  string. One person has many surface forms (`aliases`); one string can mean several
  people. Never normalize names in text — resolve mentions to entities. A *group*
  reference ("my parents", "the kids") is NOT its own mention: the bare group word can't
  resolve to one person, so at capture the model passes the specific people it can
  identify by name instead (leaning on each person's relationships in their `summary` —
  e.g. it knows Robin's parents are Karl and Nina), and just omits/asks when it can't
  tell who they are. This is a pure contract decision (docstring + server
  `instructions`) — no multi-person mention row, no relationship graph, no collective-
  expansion machinery; the `mentions` table stays one-row-one-person.
- **Capture never blocks.** `add_journal_entry` always saves, even if every mention is
  ambiguous. Unresolved mentions sit in the queue (`status='pending'`) for later.
- **One note per topic.** A single conversation often spans several unrelated threads
  (family dinner, then a rough meeting with the boss). Each unrelated thread is its OWN
  entry — Claude calls `add_journal_entry` once per topic — so a note's people don't
  bleed across contexts and a later `get_person_history` stays scoped (the boss query
  surfaces the meeting, never the dinner that was merely told the same day). This is
  purely a model/contract decision (lives in the docstring + server `instructions`): the
  schema already allows many entries per `entry_date`, and the entries are fully
  independent — no shared conversation id, no cross-linking. Granularity: a single event
  with several people stays one entry; split only genuinely separate threads.
- **Within-day order is explicit, not insertion order.** Because a day is many one-per-
  topic entries and people recount a day out of sequence, entries carry a `day_position`
  (within-day chronological rank, 1=earliest). `add_journal_entry` APPENDS (server sets
  the next position deterministically — no LLM); the model then calls `reorder_entries`
  (entry_date, ids earliest-first) to lay the day out chronologically, both right after
  capture and whenever the user says "move X before Y". This is the same split as
  everywhere: the server stores/renumbers, the *model* judges the timeline (contract in
  the `add_journal_entry`/`reorder_entries` docstrings + server `instructions`). Legacy
  pre-feature rows stay `day_position`-NULL (no back-fill UPDATE, to avoid churning the
  `entries_fts` triggers) and keep their old id order — NULL sorts first in the feed's
  ascending order, last in the newest-first lists; a freshly captured entry gets a real
  position and appends below them.
- **Two-layer entry storage.** `entries.body` is Claude's cleaned, concise version (the
  journal proper, what search/history show). `entries.raw_body` is the user's verbatim
  words, hidden, returned only via `get_entry`. Mentions are matched against the *raw*
  surface form, not the cleaned text. When a conversation is split into several notes,
  each note's `raw_body` holds only the verbatim slice about that topic — not the whole
  transcript duplicated onto every entry.
- **It gets quieter over time.** Linking a mention with `learn_alias=True` stores the
  surface form (including transcription errors) as a learned alias, so it auto-matches
  next time.
- **All user-facing dates are Pacific.** The user lives on Pacific time, so `today()`
  and every date default (`entry_date`, `food_date`, `workout_date`) plus recency
  math roll over at Pacific midnight, via `PACIFIC = ZoneInfo("America/Los_Angeles")` —
  never the server's UTC midnight. `created_at` stays UTC (an unambiguous storage
  timestamp, not a user date) — but a UTC stamp that gets SHOWN has to be converted,
  and `pacific_day()` is the one way to do it. Slicing `ts[:10]` off a stored
  timestamp looks like the same thing and isn't: it yields the UTC day, which is
  already tomorrow for the seven or eight hours after Pacific 4/5pm, so anything
  saved in the evening rendered (and sorted) a day ahead. Anything
  turning `created_at`/`updated_at` into a date the user reads goes through the
  helper. Both briefings return `now` (`current_clock()`) — which
  precomputes `date`/`yesterday`/`tomorrow` so the model uses those exact strings rather
  than doing its own +/-1 arithmetic (an off-by-one source) — so it can anchor
  "today"/"yesterday"/"tomorrow" before defaulting or back-dating. The webapp `/chat`
  reinforces this: each turn carries a live Pacific anchor (same precomputed dates) as an
  uncached system block, stamps the current user turn with today's date, and rolls a
  thread over to a fresh transcript when the Pacific day advances (a chat session id lives
  in the long-lived cookie, so a thread spans days — stale dates in the history must not
  pull "today" back). `tzdata` is a dependency so `zoneinfo` resolves on the slim Docker
  image.
- **Tool docstrings are the model-facing contract.** Claude reads them to decide when to
  ask vs. link vs. queue (e.g. the score thresholds). If you change a tool's behavior,
  update its docstring in the same edit — it's not just documentation. Two things split
  off from the prose, though, and shouldn't drift back into it:
  - **STRUCTURE lives in the schema, not the docstring.** The nested payloads
    (`MentionLink`, `LoggedExercise`/`LoggedSet`, `PlannedExercise`/`PlannedSet`) are
    TypedDicts, so FastMCP emits real nested JSON Schema and a typo'd or mistyped key is
    rejected by the CLIENT with the exact field path — instead of, as before, sailing
    through a `list[dict]` and silently no-op'ing in the loop. Docstrings describe
    JUDGMENT (what a good target is, when to split an entry); the schema describes shape.
    Add a field to a payload = add it to the TypedDict, not to the prose. The one
    deliberate exception is `update_contact`'s blob, which stays free-form `dict`: it's
    extensible by design, and a TypedDict would emit `additionalProperties: false`.
    Value-RANGE checks (rpe 1-10, no negative reps) stay in `_bad_set` — JSON Schema
    bounds wouldn't produce the actionable error text the model needs.
  - **Each rule is stated ONCE, in its owner.** Server `instructions` hold cross-tool
    policy (the three capture rules, Pacific dates, the active-exercises policy, the signed-weight
    convention); a tool's docstring holds its own mechanics. Where both wanted to say it,
    the other side now points at the owner rather than restating it — restating is how
    the two drift apart.
- **Tool annotations are declared, not defaulted.** Every tool carries one of four
  annotation sets — `READ_ONLY`, `WRITE`, `WRITE_IDEMPOTENT`, `DESTRUCTIVE` — so a client
  can tell `get_briefing` from `delete_record` without reading prose. This matters because
  the MCP default for `destructiveHint` is TRUE: an unannotated tool looks dangerous.
  `openWorldHint` is False everywhere (one local SQLite file, no network — the no-LLM
  rule showing up in the protocol). They're advisory metadata; the real guard is
  `AllowlistMiddleware`.
- **Destructive tools.** The journal has one narrow delete, `journal_delete_entry`;
  the TRAINER has a kind-scoped `delete_record` (`workout`/`set`/`intake`) — one
  connector, kinds that don't overlap. Both call the shared `_delete_record` helper,
  so the table mapping and the set-renumbering live in one place. Weigh-ins are not a
  kind — they're import-only (see `body_weight`).
## Files

- `server.py` — everything: schema, matching, both FastMCP instances (`trainer_mcp` =
  training + water/protein, the one served MCP; `mcp` = the journal tools, an
  in-process registry for the web chat), all tools, the trainer's auth wiring, the
  shared `_delete_record` helper, and the stdio/http entrypoint (runs the trainer).
- `webapp/combined.py` — single-process entrypoint (the Dockerfile's `CMD`): serves the
  browser UI (`/app`, with `/` redirecting there) and `/health` on the main origin, and
  the trainer MCP either on its own host (`TRAINER_PUBLIC_URL` set → Starlette `Host`
  routing) or grafted at `/trainer/mcp` on the main origin (authless fallback).
- `webapp/app.py` — the FastAPI UI: the journal's pages (feed, entry, people, person,
  groups, pending mentions with their inline resolver), login + the journal lock, the
  `/chat` panel mount, the backup export, and `/api/today.json`.
- `webapp/data.py` — the UI's read-query layer (the SQL behind the browse pages; keeps
  `app.py` thin). Read-only — writes go through `server.py`'s tools.
- `webapp/chat.py` — the in-app AI chat: web-app-as-MCP-client agent loop (see the
  architectural-rule note). Its one agent, `journal`, lifts its system prompt + tool
  schemas live from the journal FastMCP instance's `instructions` + tool docstrings, so
  changing a docstring updates the chat. Off unless `ANTHROPIC_API_KEY` is set; model
  via `CHAT_MODEL`.
- `webapp/templates/`, `webapp/static/` — Jinja templates and PWA assets (icons,
  `chat.js`, manifest); the app is an installable PWA.
  **THEME is two attributes on `<html>`, and the split is the design.**
  `data-theme-choice` is what the user PICKED (`system|light|dark`, stored in
  `localStorage` under `theme-choice`); `data-theme` is what that RESOLVES to
  right now (`light|dark`) and is what every dark rule keys off. The picker is an explicit THREE-way in the nav menu
  because a two-state toggle cannot store one: a toggle can only say "not the
  OS", so it has to GUESS whether a tap meant "dark right now" or "dark from now
  on". Guessing wrong is what left the app sitting white on a Mac that had gone
  dark months after one harmless tap, with nothing on screen to say why. Two
  consequences worth not undoing. The segment FILL keys off the choice, not the
  resolved theme, so System stays visibly selected whichever way the OS is
  leaning. And the old `theme` key is DROPPED on read rather than migrated —
  written by that toggle, its value records no intent that can be read back, and
  a bare `"light"` in it is indistinguishable from a deliberate one. `static/vendor/`
  holds the third-party JS, self-hosted rather than CDN'd: `marked` and DOMPurify.
  Styles are COMPILED
  Tailwind (`static/tailwind.css`, checked in — no CDN, the app styles itself
  offline); after adding/removing classes in templates or static JS, rebuild:
  `cd webapp && npx -y tailwindcss@3.4.17 -i tailwind.input.css -o static/tailwind.css --minify`
  (config + why in `webapp/tailwind.config.js`). Inter and `marked` are
  self-hosted (`static/fonts/`, `static/vendor/`) for the same reason.
- `webapp/requirements.txt` — the UI's extra deps (fastapi, uvicorn, jinja2, authlib,
  httpx, and `anthropic` for the chat); install alongside the root `requirements.txt`,
  which it imports `server.py` from.
- `scripts/` — `seed_dev.py` (load throwaway dev data), `gen_icons.py` (regenerate
  the PWA icon set), `swiftbar/` (the menu-bar water/protein plugin), `backup/`.
- `requirements.txt` — `fastmcp>=3.3`, `jellyfish>=1.1`, `tzdata` (for Pacific zoneinfo).
- `Dockerfile` — HTTP mode, DB on `/data` volume, healthcheck.
- `README.md` — setup, Coolify deploy, auth steps, first-deploy checklist, tool table.

## Stack

- **`fastmcp`** (the standalone v3 package — NOT the old `mcp.server.fastmcp`). Import is
  `from fastmcp import FastMCP`. Tools are plain functions with `@mcp.tool()`; under v3
  the decorator leaves them directly callable, which the tests rely on.
- **`jellyfish`** for phonetic + edit-distance matching.
- **SQLite** (stdlib `sqlite3`), single file, FTS5 for entry search.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# local trainer over stdio (Claude Desktop):
.venv/bin/python server.py
# remote, trainer + UI in one process (this is what the Dockerfile runs):
MCP_TRANSPORT=http PORT=8000 JOURNAL_DB=./journal.db .venv/bin/python webapp/combined.py
#   trainer: /trainer/mcp (or its own host)   ·   UI: /app   ·   /health
```

Env vars: `JOURNAL_DB` (path), `MCP_TRANSPORT` (`stdio`|`http`), `PORT`, `MCP_HOST`.
Trainer auth (set all to protect; unset = authless for dev/staging only):
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `JOURNAL_ALLOWED_EMAILS` (comma-separated;
normally just yours), and `TRAINER_PUBLIC_URL` (bare origin of the trainer's own host,
no trailing slash, no `/mcp` — enables the trainer on its own subdomain; unset =
trainer falls back to `/trainer/mcp` on the main origin, authless only). Google
redirect URIs: `<TRAINER_PUBLIC_URL>/auth/callback` for the trainer and
`<PUBLIC_URL>/app/auth/callback` for the web app's own login. `PUBLIC_URL` is the web
app's bare origin. Webapp-only:
`ANTHROPIC_API_KEY` (enables the `/chat` surface; unset = chat off, rest of the app runs
normally), `CHAT_MODEL` (chat agent model, defaults to `claude-sonnet-4-6`), `SHOW_LOGOUT`
(show the logout control in the UI), and `BACKUP_TOKEN` (strong random token that unlocks
the headless backup download at `GET /export/journal.db` for a cron `curl` — bearer /
`X-Backup-Token` / `?token=`; unset = browser-session-only; see README "Backup &
restore"), and `WIDGET_TOKEN` (unlocks `GET /api/today.json` — today's water/protein
sums only — for an ambient display like the SwiftBar plugin in `scripts/swiftbar/`; a
SEPARATE token from `BACKUP_TOKEN` on purpose, since it lives on every device that
wants a glanceable figure while `BACKUP_TOKEN` downloads the whole journal; see README
"Menu-bar macros"). The web app auto-loads `.env` (see `.env.example`); shell-exported
vars win.

## Test

No test suite yet. The working pattern: load the module and call tools directly against
a throwaway DB.

```bash
JOURNAL_DB=/tmp/t.db python3 - <<'PY'
import importlib.util, os
os.environ["JOURNAL_DB"]="/tmp/t.db"
spec=importlib.util.spec_from_file_location("server","server.py")
S=importlib.util.module_from_spec(spec); spec.loader.exec_module(S)
S.init_db()
pid=S.save_person(canonical_name="Tom", role="father", aliases=["Dad"])["person_id"]
e=S.add_journal_entry(body="Dad came by.", raw_body="dad came by", mentions=["dad"])
print([(c["name"],c["score"]) for c in e["mentions"][0]["candidates"]])
PY
```

Smoke-test HTTP boot + handshake: start `webapp/combined.py` with `MCP_TRANSPORT=http`.
Authless, POST an `initialize` JSON-RPC call to `/trainer/mcp` (Accept:
`application/json, text/event-stream`) and expect `200`. With `TRAINER_PUBLIC_URL` set,
the trainer moves to its own host — send `Host: <trainer-host>` to `/mcp`; with auth on
it should `401` with a `WWW-Authenticate` whose `resource_metadata` pointer resolves to
`200` at that host's root. `/health` should be `200` regardless of auth.

If a feature changes the schema, add a migration in `init_db()` (it runs `CREATE TABLE
IF NOT EXISTS` then `ALTER TABLE ADD COLUMN` for new columns) — existing DBs must keep
working.

## Data model (tables)

- `people` — entities. `canonical_name`, `role` (the human disambiguator), `notes`,
  `summary` (rolling profile for context — the durable KEY FACTS about a person:
  relationships (parents/partner/kids/siblings, recorded BY NAME, since there is no
  relationship graph — so this is the only place they live, letting the model resolve
  "her parents" / "his brother" to the right people), employment,
  school, birthday, where they live, major life events. The model keeps it current
  AT LINK TIME — whenever it links a mention it folds in any new key fact the entry
  revealed (read-before-write); nothing regenerates summaries automatically.
  `get_briefing` surfaces it ONLY for people mentioned in the last `people_days`
  (everyone else arrives summary-less in its compact `roster`), and `get_person_history`
  returns the FULL summary for read-before-write, same as `contact`), `contact` — a free-form JSON blob holding
  multi-valued contact info (emails, phones, addresses, websites, …), written via
  `update_contact` with a shallow per-top-level-key merge (so adding phones never touches
  addresses; lists are replaced wholesale, so the model READS via `get_person_history`
  then writes the full list back; a key set to `null` is dropped). The legacy single-
  valued `email`/`phone`/`address` columns are folded into `contact` once on migration
  and are otherwise dormant. `get_person_history` returns the blob (and the full
  `summary`) so the model can read-before-write; there is NO LLM in this path — the
  server just merges JSON.
- `aliases` — surface forms per person; `phonetic_key` (metaphone), `source`
  (`manual`|`learned`).
- `entries` — `body` (clean), `raw_body` (verbatim), `entry_date` (day it's *about*,
  distinct from `created_at`), `day_position` (within-day chronological rank, 1=earliest;
  set on append by the server, rewritten by `reorder_entries`; NULL on legacy rows —
  sorts first ascending / last newest-first), `kind` (`'log'` = an interaction/observation/fact, the
  default and back-fill for pre-feature rows; `'thought'` = a personal reflection not
  anchored to a specific interaction). The model classifies each entry at capture
  (contract lives in `add_journal_entry`'s docstring + server `instructions`; no LLM in
  the server — it just stores the flag). Thoughts stay in the journal feed and FTS, but
  are EXCLUDED from per-person views (`get_person_history`, `get_related_people`) so the
  CRM spine stays a record of real interactions. The webapp `/journal` feed filters on
  it (All / Thoughts / Log via `?kind=`). FTS5 mirror `entries_fts`. `search_entries`
  does NOT hand the model's words straight to `MATCH` — FTS5 parses that as query
  SYNTAX, so an apostrophe or a `?` ("Tom's", "how was my week?") is a syntax error,
  not a search. `_fts_query` tokenizes and quotes each term into a literal (terms
  ANDed); `raw_query=True` opts back into real FTS5 syntax (OR/NEAR/prefix*) and
  returns any syntax error as a correctable `{"error": …}` rather than raising.
  Each literal is a PREFIX query (`"term"*`). The load-bearing reason is CJK:
  unicode61 tokenizes a run of Chinese as ONE token, so a title like 宫保鸡丁 was
  reachable only by typing
  the whole name (`宫保` matched nothing), and the same character fixes the
  everyday English case (`doubanji` → doubanjiang). It is NOT substring matching —
  a word's TAIL still misses; that needs the trigram tokenizer and an FTS rebuild.
- `mentions` — one per reference in an entry; `surface_form`, `person_id` (NULL while
  pending), `status`, `context_snippet`.
- `groups` + `person_groups` — explicit circles (family, colleagues, …), many-to-many.
- `drinks` — LEGACY, dormant. Alcohol was folded into `intake_items` and is no longer
  tracked at all.
  The TABLE is kept as the fold-in migration's source and the one copy of the
  per-day `kind` ("beer, wine"), which the item rows have no column for; the
  CODE that read and wrote it (`log_drinks`/`get_drink_summary`/`update_drink`,
  and `_delete_record`'s `"drink"` kind) is DELETED. Dormant data costs nothing;
  dormant code is a trap — a live-looking reader of this table is what left the
  /graphs drinks series empty for months after the fold. Nothing reads it.
- `intake_items` — the WATER/PROTEIN log, on the TRAINER server: **one row per thing
  consumed**. It used to be a full food tracker (calories, macros, sodium, fiber,
  alcohol) on the journal connector, with a fuzzy past-food lookup, an eating-profile
  prose layer and a Telegram bot; the user stopped tracking food, so it was cut to
  the two figures still kept (`NUTRIENTS = ("protein_g", "water_oz")`) and moved onto
  `trainer_mcp` as `log_intake` / `get_intake` / `update_intake` +
  `delete_record(kind="intake")`. The other nutrient columns are DORMANT — kept with
  their history, never read or written; `_LIVE_INTAKE` filters every read to rows
  carrying one of the two live figures, so a legacy calories-only row is invisible
  rather than an empty line. No time-of-day: `position` is the server-assigned order
  within the day (the same append as entries' `day_position`).
  **Day totals are DERIVED, never stored** (`SUM ... GROUP BY food_date`) — the
  load-bearing decision that survived the cut: correcting one item is one UPDATE and
  every total follows, with no arithmetic asked of the model. A figure no item carries
  is ABSENT from the day's totals rather than 0 ("not logged" ≠ zero). Range-checked by
  the shared `_bad_nutrients` (no negatives; a per-item `NUTRIENT_MAX` typo guard,
  since one absurd row silently skews the day). Write returns carry `day_totals` plus
  `targets`, and `get_fitness_briefing` carries `intake_today`, so the trainer answers
  "how's my water" from the DB, never from a chat-side tally.
  **Targets** are `INTAKE_TARGET_DEFAULTS` overridden by what's stored in
  `settings.eating_profile.targets`, written by the trainer tool `set_intake_targets`
  (0 drops an override back to the default); `_day_targets` is the one read of them.
  A target is just a target — no ceiling/floor direction anywhere. (The rest of the
  eating_profile blob — goal/context prose, `targets_note` — is dormant from the
  food-tracker days.) There is no web page for it; `/api/today.json` (WIDGET_TOKEN)
  serves today's two sums + targets for the SwiftBar plugin.
- `nutrition` — LEGACY, dormant. The first shape of the intake log: one row per day.
  Its rows fold into `intake_items` once on the first `init_db` (spelled-out legacy
  column list, so the fold stays lossless); the table is kept, not dropped.
- `exercises` — the user's OWN movements, nothing else. **There is no library.** It used
  to be ~870 movements pre-loaded from free-exercise-db with technique notes, cautions,
  rep images and a three-layer rotation ⊆ hearted ⊆ library curation; the user retired
  all of it ("I don't need to be storing a ton of exercises I don't do"), and the reason
  is worth keeping: reference data about movements is what the MODEL already knows, so
  storing it bought a closed catalog the user had to curate in a browser before they
  could log anything new. Now a row is just `name`, `category` (`strength`|`cardio`),
  `archived`, and a `note`, plus its muscles.
  **Two states.** ACTIVE (`archived=0`) is what the trainer programs from — the
  briefing's `exercises`. ARCHIVED is what the user did and stopped, kept as a record
  rather than deleted, with `note` saying why ("bugged my left shoulder"); the model
  reads it (`list_exercises`) before proposing something new, so it neither pitches a
  lift that was dropped for pain nor forgets one worth bringing back. `archive_exercise`
  moves a lift between the two on the user's say-so; LOGGING an archived lift
  reactivates it automatically (it's being done again). Archiving never deletes — sets,
  history and PRs stay linked.
  **Born on the fly.** Every planning/logging path resolves names through ONE helper,
  `_resolve_or_create`: a known name (fuzzy, AKA, or a 2+-word shorthand whose words
  all appear in exactly one name — "bench press" → Barbell Bench Press) is that row; a
  name close to an existing one (`ADD_NEAR_DUP`, 0.88) comes back `unmatched` with
  candidates unless the item says `new: true`, because a near-twin is usually the same
  lift misspoken and a duplicate row would split its history in two; an unknown name is
  CREATED when the item carries `muscles` (or category cardio), and otherwise comes back
  `unmatched` asking for them — muscles are required because recency can't count a lift
  that maps to none. `add_exercise` is the same thing ahead of time. The returns name
  what happened (`created`/`reactivated`/`unmatched`) so nothing changes silently.
  **The migration** (`_prune_exercise_library`, flag-guarded, runs once): the rotation
  became ACTIVE; anything with a logged/planned set or a heart became ARCHIVED; every
  other library row was DELETED (muscles and AKAs cascade). The reference columns
  (`slug`, `force`, `level`, `mechanic`, `equipment`, `technique_notes`,
  `common_mistakes`, `cautions`, `video_link`, `image_link`, `image_link_end`) were
  nulled and are dormant; `in_rotation`/`hearted` are dormant MIRRORS of `archived`
  (`1 - archived`, kept in step by `_set_archived`) so no stale reader disagrees.
- `exercise_aliases` — AKAs per exercise, now READ-ONLY: `_resolve_exercise` and
  `_match_exercises` still score against whatever survived the prune, but nothing
  writes new ones (the user's own name for a lift is its name).
- `exercise_muscles` — normalizes muscle→exercise so per-muscle recency/volume is a
  plain GROUP BY. New rows use two tiers (primary|secondary); legacy rows may carry a
  `tertiary`, which `list_exercises` folds into secondary. Recency/volume count all
  tiers equally. Canonical muscle list is `MUSCLES`, enforced by `_bad_muscles`.
- `workouts` + `sets` — session + per-set `weight_lbs`/`reps`/`rpe` (1-10 RPE), plus
  `duration_seconds`/`distance_miles` for cardio (running/walking/rowing — all NULL for
  lifts, weight/reps NULL for cardio). A planned set also carries `target_rpe` — the
  difficulty the trainer programs for it (1-10), the target twin of the actual `rpe`.
  The two-level log mirroring entries/mentions. A
  *planned* session (`status='active'`, from `start_workout_plan`) is UNDATED — its
  `workout_date` is the `''` not-yet-done sentinel until `finish_workout` stamps it with
  the day it was actually completed (so a plan started late and finished after Pacific
  midnight dates to the finish day, not the start). A direct `log_workout` is already
  done, so it dates immediately. Active workouts are excluded from all history/briefing
  aggregates by `status`, so the empty date never leaks. (`log_workout` is the
  immediate-done path; the empty sentinel only ever exists on an in-progress plan.)
  **A WEEK can be planned at once — MANY rows are `active` simultaneously, one per
  day.** The trainer lays out "Tue/Thu/Sat" (or "the rest of the week" after today's
  session is done) as one `start_workout_plan` call per day, each carrying a
  `planned_date`. That column is the day a plan is FOR, and it's deliberately a
  SECOND date rather than an early write to `workout_date`: intent and history are
  different facts, and conflating them would start counting unfinished plans in every
  aggregate that keys off `workout_date`. So `planned_date` is only intent — a
  session is still stamped with the day it was actually COMPLETED, and one done a day
  late lands on the day it was done. NULL `planned_date` = an unscheduled "next
  session" (the ad-hoc "build me something now"), which is also what a plan predating
  the column reads as; there's no back-fill.
  Two consequences of many-active. `_current_plan` is the ordering that decides which
  plan a caller who named none gets: next-due first, with an unscheduled plan
  competing as TODAY's (else an ad-hoc session would queue behind Friday) and ties on
  the oldest id. Every plan tool keeps an optional `workout_id` and falls back to it,
  because "the active plan" is no longer a thing that exists. And recovery gets a gap the server refuses to
  paper over: `muscle_recency` counts COMPLETED work only, so the days already
  programmed this week are invisible to it. Rather than fold plans into recency —
  which would make a factual "days since last trained" partly hypothetical —
  `get_fitness_briefing` returns them as a separate `upcoming` list (workout_id,
  planned_date, focus, exercise names, set count) and the model reads the two
  together. Same split as everywhere: the server states both facts, the model judges.
  The other thing that shifts per day is `get_fitness_briefing(as_of=…)`, which
  re-anchors `days_since` to the day you're planning FOR, so what's "due" reflects the
  extra rest; planning a week is that, one day at a time.
  **A personal best** is a deterministic fact: weight EXCEEDS the heaviest ever for
  that movement, or TIES it and beats the most reps done at it. No e1rm (an estimate
  isn't a thing that happened); cardio never counts; the first weighted set of a
  movement never counts. `_new_bests` applies it per batch and `complete_sets` /
  `log_workout` return the result as `new_prs`; `get_personal_records` is the read.
  Cardio exercises carry no `exercise_muscles` rows, so they're summarized by
  `get_fitness_briefing`'s `cardio_recency` (minutes/miles, last 7 days) rather than
  `muscle_recency`. A set also carries `ex_position` — its exercise's slot in the
  workout (all the exercise's sets share it; NULL = insertion order). `_plan_payload`
  orders exercises by it, so the active plan honors a user-chosen order; it's set by
  `reorder_plan` (a trainer tool, names → order, so chat can sequence the session) via
  the deterministic `reorder_plan_exercises` helper. Newly-added exercises keep
  `ex_position` NULL and fall in after the positioned ones.
- `body_weight` — bodyweight readings, one row per weigh-in, keyed by `weigh_date`
  (weight is a daily metric you may log on rest days too, and the point is the trend).
  The latest reading on a day is "the" weight for that day; a day with no row simply
  wasn't weighed. `get_fitness_briefing` surfaces the latest reading + 30-day change.
  There is NO weight-goal/target logic in the server — the coaching is the model's.
  **A weigh-in is a MORNING reading taken by a CONNECTED SCALE, and there is exactly
  ONE way one gets in: the trainer tool `import_weigh_ins`**, where the model reads the
  scale app's export the user attaches and passes its rows. A number the user merely
  says is not a reading (the trainer's instructions say so) — a hand-typed 186 that
  disagrees with the scale's 185.4 is a fork, not a correction; a wrong reading is
  fixed AT THE SCALE'S APP and re-exported. (The web `/weight` upload page and its
  stdlib .xlsx parser were removed with the rest of the web training UI.)
  The load-bearing consequence of import-only is IDEMPOTENCE. Exports OVERLAP — the
  scale app hands you "the last 30 days", not the delta — so re-importing must insert
  only what's new. `source_key` is the reading's identity in its export
  (`"wyze:2026.08.22 06:39 AM"`, the vendor plus the stamp), RE-RENDERED from the parsed
  stamp in the export's own format (`%Y.%m.%d %I:%M %p`) rather than trusted as given,
  since a spreadsheet reader may hand the cell back as ISO. UNIQUE but NULLABLE so the
  hand-entered rows that predate the scale (all NULL) don't collide. The index is
  created in `init_db`, NOT in `SCHEMA`, because the schema script runs BEFORE the
  `ALTER TABLE` that adds the column. A re-import reports `imported: 0` calmly. The
  stamp is the scale app's LOCAL time — the user's own — so its calendar day IS the
  Pacific day; a stamp no known format parses SKIPS its row rather than guessing.
  Only weight is stored; the export's other columns (body fat, BMR, …) are dropped.
- `collections` + `items` (+ `items_fts`) — DORMANT. The notes & collections layer
  (an inbox of notes promotable into model-defined collections, with list/table/
  cards/map views) was removed with the journal connector; existing DBs keep the
  tables and rows, nothing reads or writes them, and `SCHEMA` no longer creates them.
- `settings` — generic JSON KV; holds `profile` (`goals`, `split`, `session`,
  `injuries`, `coaching`, free-form beyond those) merged via `update_profile` and surfaced by `get_fitness_briefing`,
  and `eating_profile`, whose one live key is `targets` — the flat {nutrient: number}
  water/protein goals, written by `set_intake_targets`, validated by `_bad_targets`
  (a real nutrient key, a positive number), and read through `_day_targets`. Its other keys (goal/context prose, `targets_note` with its
  `{calories}` placeholders) are dormant from the food-tracker days.
  **The trainer has exactly TWO homes for guidance: the code's `instructions` for the
  RULES, the `profile` for the PERSON.** It used to have five — the instructions, a
  `DEFAULT_COACHING` string in code that filled `coaching` when empty, the profile,
  `webapp/chat.py`'s trainer blurb (which carried its own "21-26 working sets"
  sizing rule, contradicting the default, so the trainer coached differently on the
  web panel than on the connector), and whatever the user pasted into the Claude
  project's custom instructions. The split now is by WHAT a line is. A rule that pairs
  with tool code (the active-exercises policy, signed weights, a mentioned weight isn't
  a weigh-in) is CONTRACT: in git, in `trainer_mcp`'s `instructions`, not editable
  from a textarea. Anything about the user — goals, split, session size, injuries,
  coaching tone — is PREFERENCE: in `profile`, delivered on every
  `get_fitness_briefing` (so a change lands next session on every surface with
  nothing to redeploy, where `instructions` only reaches a connector at its
  initialize handshake). There are NO generic preference defaults in code: an empty
  profile makes the trainer ASK (the instructions' SETUP rule) rather than coach to
  a stranger's numbers, because a default the user never chose is exactly the kind of
  quiet second copy this cleanup removed. `profile.coaching` is written by
  `update_profile` (the model, when the user asks in so many words); `update_profile`
  drops a key sent as null.
- `subjects` + `facets` + `attempts` + `learn_fts` — DORMANT. The teacher server's
  spaced-repetition log was removed (it lives on as a separate project); existing DBs
  keep the tables, nothing reads them.

## Matching (in `find_candidates` / `score_surface_against_alias`)

Exact alias = 1.0; otherwise Jaro-Winkler, floored to 0.88 when Metaphone keys match
(catches sound-alike transcription noise). Candidates below **0.6** are dropped. Returns
top scorer per person. The emergent "who's talked about together" graph
(`get_related_people`) is a self-join over `mentions` — no tagging, no extra storage.

## Auth flow (when enabled)

`GoogleProvider` makes the trainer its own OAuth 2.1 authorization server (PKCE +
Dynamic Client Registration) that proxies Google; Claude discovers it via the 401's
`resource_metadata` pointer and self-registers, so no client ID/secret is entered in
Claude's connector UI. `AllowlistMiddleware` then rejects any authenticated account
whose email isn't in `JOURNAL_ALLOWED_EMAILS` — a valid Google login alone is not
enough.

The trainer runs on its **own host**: set `TRAINER_PUBLIC_URL=https://<trainer-host>`,
its provider takes that base_url, and `combined.py` routes that hostname (Starlette
`Host(...)`, which dispatches by Host header WITHOUT prefixing paths, unlike `Mount`) to
the trainer app at its root — a complete OAuth server at its own origin (`/mcp`,
`/.well-known/*`, `/authorize`, `/auth/callback`). The Google client needs
`<trainer-host>/auth/callback` as a redirect URI. (Build the MCP app at the root, never
as a sub-mount, which would prefix the discovery docs.) A `GoogleProvider` is
single-resource — building its app writes `_resource_url` onto the instance — so if a
second MCP server is ever added, give it its own `_build_auth()` and its own host; two
full OAuth servers can't share an origin.

With `TRAINER_PUBLIC_URL` unset (local/authless), `combined.py` grafts the trainer's
`/trainer/mcp` endpoint + its protected-resource metadata onto the main origin.

The web app's own login is separate (authlib, `<PUBLIC_URL>/app/auth/callback`).

## Gotchas

- Use the standalone `fastmcp`, not `mcp.server.fastmcp` — the auth providers live in v3.
- `PUBLIC_URL` / `TRAINER_PUBLIC_URL` must be bare origins. A trailing slash or `/mcp`
  breaks OAuth discovery.
- Claude Desktop launches configs with a minimal PATH — point its config at the venv
  python by absolute path, not `python`.
- The allowlist reads the `email` claim; if it rejects after a correct login, verify the
  claim key/scope before changing logic.
- Don't reformat tool return shapes casually — they're tuned to be token-compact
  (IDs + minimal fields; truncated bodies). Bloating them degrades the conversation.

## Things deliberately NOT built (don't assume they exist)

Typed person-to-person relationship graph (relationships are kept as free text in a
person's `summary` instead — see the `people` row above; the model reads them from
the briefing/`get_person_history` to resolve relational references like "her parents"
to the right people, with no structured edges to traverse or keep in sync); vCard import/export (would map onto the
`contact` blob); Google Contacts sync; automated `summary` regeneration. See README
"Notes / next steps". (Contact info IS multi-valued now — the free-form `contact` JSON
blob, edited via `update_contact`.)
