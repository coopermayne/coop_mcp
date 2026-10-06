# CLAUDE.md

Orientation for working in this repo with Claude Code. Read this first.

## What this is

A single-user **journal web app plus a trainer MCP server**, one process, one SQLite
DB. The user writes the journal by talking to the web app's own chat; Claude captures
entries and resolves *who* they mean to stable person records, so later "everything
about Tom my father" is an exact lookup that never pulls in the other Tom. Training
(and a small daily water/protein log) is planned through the **trainer connector**
in Claude and logged set-by-set in the web app.

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

**Training has two surfaces over one DB: the trainer connector and the web app.** The
user tried training through the connector alone and it didn't work well between sets,
so the web training UI (`/workouts`, `/trainer/{id}`, `/weight`, `/graphs`, `/food`)
was brought back and is the primary place to WORK a session: the `/trainer/{id}` page
is built for logging between sets (see the `workouts` row). The connector stays the
place to PLAN, review progress and get advice, and it can still do everything the
pages do: `complete_sets` (a batch, because a conversation reports a whole exercise at
once; the single-set `complete_set` stays as the card's plain helper),
`remove_from_plan`, `add_exercise`, `import_weigh_ins`, `set_intake_targets`.
`complete_sets` and `log_workout` return `new_prs` (`_new_bests`, `pr_for_set`'s rule
applied per batch) so a best logged in conversation is still announced. The
`trainer_mcp` instructions open by saying the conversation IS the interface (plan as a
table, ids never shown, short mid-session replies). The trainer also carries the
water/protein log (see `intake_items`). Improving the web training UI is fair game.

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
- **A write says where the thing now lives.** Training happens in a Claude
  conversation while the data is READ in the web app, so `log_intake` returns a `url`
  (`_app_url`: `PUBLIC_URL` + the `/app` mount, per `webapp/combined.py`) → `/food`.
  `PUBLIC_URL` unset (stdio, dev) OMITS the key rather than emitting a dead link, and
  corrections (`update_intake`) return totals, no url — the returns are tuned
  token-compact.

## Files

- `server.py` — everything: schema, matching, both FastMCP instances (`trainer_mcp` =
  training + water/protein, the one served MCP; `mcp` = the journal tools, an
  in-process registry for the web chat), all tools, the trainer's auth wiring, the
  shared `_delete_record` helper, and the stdio/http entrypoint (runs the trainer).
- `webapp/combined.py` — single-process entrypoint (the Dockerfile's `CMD`): serves the
  browser UI (`/app`, with `/` redirecting there) and `/health` on the main origin, and
  the trainer MCP either on its own host (`TRAINER_PUBLIC_URL` set → Starlette `Host`
  routing) or grafted at `/trainer/mcp` on the main origin (authless fallback).
- `webapp/app.py` — the FastAPI UI: routes + page rendering for the browser app (mostly
  read-only browse pages, plus the
  handful of website-only write carve-outs (`/food/targets`, `/weight` and its `/{id}`
  edit + delete, `/graphs/goal`, `/trainer/profile`) and the
  `/chat` panel mount).
- `webapp/data.py` — the UI's read-query layer (the SQL behind the browse pages; keeps
  `app.py` thin). Read-only — writes go through `server.py`'s tools.
- `webapp/chat.py` — the in-app AI chat: web-app-as-MCP-client agent loop (see the
  architectural-rule note). Server-bound agents (`journal`, `trainer`) lift their system
  prompt + tool schemas live from a FastMCP instance's `instructions` + tool docstrings,
  so changing a docstring updates the chat. (A server-bound agent can narrow its
  lifted tools with `exclude` or `include`; neither uses one today.) (The webapp-defined `exercise` agent that backed the
  library's add panel is deleted with the library.) Off unless `ANTHROPIC_API_KEY` is
  set; model via `CHAT_MODEL`.
- `webapp/templates/`, `webapp/static/` — Jinja templates and PWA assets (icons,
  `chat.js`, manifest); the app is an installable PWA. `static/confetti.js` is the
  app's one celebratory flourish (`window.Confetti.burst(el)`, thrown at a lifting PR
  or an all-time-low weigh-in — see the `sets` and `body_weight` rows): hand-written
  rather than vendored, loaded on every page because it costs nothing until called, and
  driven by requestAnimationFrame on a canvas rather than a CSS animation for the same
  Low Power Mode reason as the rep-loop crossfade.
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
  holds the third-party JS/CSS, self-hosted rather than CDN'd: `marked`, DOMPurify and
  uPlot. Styles are COMPILED
  Tailwind (`static/tailwind.css`, checked in — no CDN, the app styles itself
  offline); after adding/removing classes in templates or static JS, rebuild:
  `cd webapp && npx -y tailwindcss@3.4.17 -i tailwind.input.css -o static/tailwind.css --minify`
  (config + why in `webapp/tailwind.config.js`). Inter and `marked` are
  self-hosted (`static/fonts/`, `static/vendor/`) for the same reason.
  **Asset links are FINGERPRINTED: write `{{ base }}/static/{{ static_v('x.js') }}`,
  never a bare `/static/x.js`.** The service worker serves `/static/` cache-first, so
  an unversioned link reached a phone only on the load after the next one, and an
  installed PWA could sit on old JS for days. `static_v` appends a hash of the file's
  contents (`app.py`, hashed once per process), and the worker's `VERSION` is
  `ASSET_BUILD`, a hash over every static file, so a deploy that changes an asset
  installs a new worker that drops the old cache. Its `PRECACHE` entries use the
  same fingerprinted URLs (the `__V:path__` placeholders).
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
app's bare origin (also used for the `url` that `log_intake` returns). Webapp-only:
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
  **Targets** live in `settings.eating_profile.targets`, written by ONE function,
  the trainer tool `set_intake_targets` (0 hands a goal back to the
  `INTAKE_TARGET_DEFAULTS` default), which `/food`'s **Targets** popover also calls
  (a blank box sends `null`, mapped to 0). `_day_targets` is the one merged read;
  `_stored_targets` is the set-only view the popover needs. A target is just a
  target — no ceiling/floor direction anywhere. (The rest of the eating_profile blob —
  goal/context prose, `targets_note` — is dormant from the food-tracker days.)
  The webapp page is `/food` ("Water & protein" in the nav): outside the journal lock,
  no chat panel, STRICTLY READ-ONLY for content — one line per item and two rings
  (`macros.eating_block` / `nutrient_ring`, unit labels in `macros.NUTRIENT_UNITS`),
  each item/ring opening the shared display-only detail modal
  (`templates/_detail_modal.html`). `/api/today.json` (WIDGET_TOKEN) serves the same
  two sums for the SwiftBar plugin.
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
  difficulty the trainer programs for it (1-10), the target twin of the actual `rpe`. The
  /trainer/{id} session page is WORKOUT MODE (`trainer.js` + `trainer.html`): one fixed
  full-viewport screen (site nav hidden, no page scroll, `100dvh`), because it's used
  standing at a rack between sets and nothing should move under the thumb. Top to
  bottom: a top bar (back, focus, progress, whole-plan, chat, ⋯ menu), a horizontal
  exercise STRIP (tap to jump when a machine is busy), the STAGE (rest clock, "Set n of
  m", a "PR attempt"/"Top set" badge, the exercise, target, set note, a "Last … · Best
  …" line, this exercise's set chips, and a "Then: …" line), and the DOCK pinned to the
  bottom: weight (−5/−2.5/+2.5/+5) and reps steppers and a row of RPE 6-10 buttons
  labelled with reps left, where ONE tap rates the set AND logs it. After a log it stays
  on the exercise while sets remain, then advances; if a set was done at a different
  weight than planned and the next set has the same target, the dock starts from what
  was actually lifted. Everything else is a bottom SHEET: the whole plan (reorder,
  Replace via chat, Remove), editing a done set (Save / Clear set), the trainer's
  notes, and the menu (Coaching preferences, Finish, Delete plan). RPE replaced
  Easy/Med/Hard (stored as 5/7/9), which couldn't tell an 8 from a grind to failure.
  Logging starts a REST CLOCK that counts UP, with a target sized by the RPE (9+ → 3:00,
  8 → 2:30, else 1:30) shown as a quiet "/ 3:00" beside it; passing it turns the clock
  yellow (no sound, the color is the cue). It's a start timestamp in localStorage so it
  survives a reload or a locked phone, and the page holds a screen wake lock while a
  session is open. "Last/Best" comes from `data.with_history`, a webapp-only enrichment
  of the plan payload (like `_with_pr`) kept off the model-facing `_plan_payload`. A
  BODYWEIGHT-BASED exercise (any planned, logged or historical weight ≤ 0: pull-ups,
  dips) shows load signed relative to bodyweight ("−40" assisted, "+25" added, "BW"
  for 0) and labels its weight field that way.
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
  the oldest id. Every plan tool keeps an optional `workout_id` and falls back to it;
  the WEBAPP always passes one (see `/trainer/{id}` below), because "the active plan"
  is no longer a thing that exists. And recovery gets a gap the server refuses to
  paper over: `muscle_recency` counts COMPLETED work only, so the days already
  programmed this week are invisible to it. Rather than fold plans into recency —
  which would make a factual "days since last trained" partly hypothetical —
  `get_fitness_briefing` returns them as a separate `upcoming` list (workout_id,
  planned_date, focus, exercise names, set count) and the model reads the two
  together. Same split as everywhere: the server states both facts, the model judges.
  The other thing that shifts per day is `get_fitness_briefing(as_of=…)`, which
  re-anchors `days_since` to the day you're planning FOR, so what's "due" reflects the
  extra rest; planning a week is that, one day at a time.
  **The UI is a hub and per-session pages.** `/workouts` ("Training") is the hub: the
  upcoming plans (`data.upcoming_plans`) listed ABOVE the completed history
  (`data.workouts_full`), and each row links into `/trainer/{workout_id}` — the
  tap-to-log plan card for that day. There is no "Trainer" link any more, because a
  singleton `/trainer` can't name which of five plans it means; a bare `/trainer`
  redirects to `_current_plan` (or to the hub when nothing is planned), so the `6`
  shortcut and any old link still land somewhere sensible. The upcoming rows are
  DELIBERATELY condensed to day + focus + counts: nothing has been lifted yet, so
  there are no set chips and no muscle diagram to draw, and a stack of full cards for
  work that hasn't happened would outweigh the history under it. That makes `focus`
  load-bearing — it's the only title a row has, which is why the trainer contract
  insists on one. Every trainer write route carries the id in its PATH
  (`/trainer/{id}/finish`, `/reorder`, `/discard`, `/plan.json`,
  `/exercise/{eid}/remove`) and `trainer.js` builds them from the plan payload's
  `workout_id`; the two SET-scoped routes keep their flat URLs, since a `set_id`
  already identifies its workout — but they must return THAT set's plan, which is why
  `update_set` now returns a `workout_id` at all. The trainer chat panel is on BOTH
  surfaces: the hub is where a week gets planned, a session page is where it gets
  tweaked mid-workout. Its `onWrite` forks on that — the session page re-renders the
  card in place, the hub has no card and reloads, because the upcoming list is
  server-rendered and a chat that just added Thursday must not leave the page stale.
  **A personal best is a deterministic fact, computed by `pr_for_set` — a NON-tool,
  website-only path like `set_archived` and `clear_plan_set`.** It answers one question
  the /trainer card asks after a tap ("was the set just logged a best?") so the page can
  throw confetti at the chip; the MODEL already has `get_personal_records`, which is why
  this isn't a tool and why the rule sits beside it rather than in `webapp/data.py` — two
  "heaviest ever" queries in one repo is exactly how they drift apart. The rule: weight
  EXCEEDS the heaviest ever for that movement, or TIES it and beats the most reps done at
  it. No e1rm (an estimate isn't a thing that happened); cardio never counts; the first
  weighted set of a movement never counts. The flag reaches the browser as a webapp-only
  `celebrate` key merged on by `webapp/app.py`'s `_with_pr` — `_plan_payload` is the
  return of five MCP tools, so a key added THERE would ride along on every model-facing
  plan return. DEDUPING is the
  browser's (`trainer.js`), not the server's: a corrected set that is still the heaviest
  ever IS still a best, and the data layer should keep saying so.
  Cardio exercises carry no `exercise_muscles` rows, so they're summarized by
  `get_fitness_briefing`'s `cardio_recency` (minutes/miles, last 7 days) rather than
  `muscle_recency`. A set also carries `ex_position` — its exercise's slot in the
  workout (all the exercise's sets share it; NULL = insertion order). `_plan_payload`
  orders exercises by it, so the active plan honors a user-chosen order; it's set by
  `reorder_plan` (a trainer tool, names → order, so chat can sequence the session) and by
  the deterministic `reorder_plan_exercises` helper behind the /trainer card's reorder UX
  (↑/↓ arrows → `POST /trainer/reorder` with exercise ids). Newly-added exercises keep
  `ex_position` NULL and fall in after the positioned ones.
- `body_weight` — bodyweight readings, one row per weigh-in, keyed by `weigh_date`
  (the drinks pattern, not a `workouts` column: weight is a daily metric you may log on
  rest days too, and the point is the trend). The latest reading on a day is "the"
  weight for that day; a day with no row simply wasn't weighed. `get_fitness_briefing`
  surfaces the latest reading + 30-day change; the longer trend lives in the webapp, not
  a dedicated server tool. There is NO weight-goal/target logic in the server — the
  coaching is the model's, as everywhere else.
  **A weigh-in is a MORNING reading taken by a CONNECTED SCALE, and there is exactly
  ONE way one gets in: importing the scale app's export.** It has two doors now, both
  imports: the `/weight` upload, and the trainer tool `import_weigh_ins`, where the
  model reads the export the user attaches and passes its rows. Same `source_key`
  identity, but the tool RE-RENDERS the key from the parsed stamp in the export's own
  format (`%Y.%m.%d %I:%M %p`) rather than trusting the string, since a spreadsheet
  reader may hand the cell back as ISO and a second spelling of one reading would store
  it twice. A number the user merely says is still not a reading. The user weighs in every
  morning on a smart scale that writes to its vendor's app; every so often they upload
  that app's `.xlsx` on `/weight` (**Import scale export** → `POST /weight/import` →
  `server.import_bodyweight`, a NON-tool website-only path like `set_archived`). Everything else is DELETED, not left dark: the entry form, the
  per-row ✎ and ×, `log_bodyweight` (the trainer's one weigh-in WRITE tool),
  `set_bodyweight`, `POST /weight`, `POST /weight/{id}`, `POST /weight/{id}/delete`, the
  `"weight"` kind in `_delete_record` and in the trainer's `delete_record`, and the
  `log_bodyweight` branch in `webapp/chat.py`'s tool chips. The reason is one rule: a
  reading is a MEASUREMENT now, and a second door onto a measurement is a second version
  of the truth — a hand-typed 186 that disagrees with the scale's 185.4 is not a
  correction, it's a fork. A wrong reading is fixed AT THE SCALE'S APP and re-exported.
  The trainer's instructions say so (a mentioned weight is not a reading).
  The load-bearing consequence of import-only is IDEMPOTENCE, and it's why the table
  grew a column. Exports OVERLAP — the scale app hands you "the last 30 days", not the
  delta since your last upload — so re-importing must insert only what's new.
  `source_key` is the reading's identity in its export (`"wyze:2026.08.22 06:39 AM"`,
  the vendor plus the stamp), UNIQUE but NULLABLE so the hand-entered rows that predate
  the scale (all NULL) don't collide — SQLite allows any number of NULLs in a unique
  index. The index is created in `init_db`, NOT in `SCHEMA`, because the schema script
  runs BEFORE the `ALTER TABLE` that adds the column and would fail on an existing DB.
  A re-upload reports `imported: 0` calmly rather than erroring; it's the normal way
  this is used, not a mistake.
  The parser (`_xlsx_rows` + `_parse_scale_export`) is stdlib `zipfile` + `ElementTree`
  rather than openpyxl — one small sheet of text doesn't earn a fourth pin — and reads
  both ways a string reaches a cell (an inline `<is>`, which is what this scale writes,
  and a `<v>` index into `sharedStrings`, which most other writers use). It is
  header-DRIVEN, not positional: the export leads with a merged title row, and a vendor
  adding a column would silently shift a positional read onto the wrong number, so the
  header row is found by its date column and weight is taken from `Weight(lb)` — or
  `Weight(kg)` converted, since a metric export is still a weigh-in. Values arrive as
  strings WITH units (`"185.4lb"`), so the number is pulled out by regex. The stamp is
  the scale app's LOCAL time, which is the user's own, so its calendar day IS the
  Pacific day with no conversion to get wrong; a stamp no known format parses SKIPS its
  row rather than guessing (a misread date is a reading on the wrong day, which is worse
  than a reading that never arrives).
  **Every other column is DROPPED.** The scale exports body fat, muscle mass, body
  water, bone mass, BMR, metabolic age and a dozen more; `body_weight` is a weight log,
  the graph plots weight, and a stored column nothing reads is the dormant-data trap
  this repo already has a scar from (see `drinks`). Adding one later means a consumer
  first.
  The upload goes up as the RAW request body, not multipart — one upload in the whole
  app doesn't justify adding `python-multipart` for a single route. The page is
  upload-then-reload (the `/food` Targets shape) rather than patching rows in: each row
  shows its delta against the next-older reading, so a live patch would mean a second
  copy of that arithmetic in JS. The ONE thing that must not be cut off by that reload
  is the confetti, so a new low holds it ~2.4s — but only when a burst actually STARTED.
  `Confetti.burst()` returns whether it threw anything, because it declines under
  `prefers-reduced-motion`, and holding the page for an animation nobody will see is a
  dead wait inflicted on exactly the person who asked for less motion.
  `new_low` on the import return is the one fact the browser cannot derive from the rows
  it is about to re-render: nothing else on this path ever sees the all-time minimum.
  It's true when any of the NEWLY imported readings beats every reading that was already
  there, and never on a first-ever import (nothing to beat) — the same rule the deleted
  hand-entry path applied one reading at a time. It's also the graphs row's confetti cue.
  **The log has its OWN PAGE, `/weight`** — the import box on top, every reading below,
  read-only. Its own page rather than a strip on `/graphs` because a daily habit is a
  destination, not a widget above someone else's chart, and because the reading is the
  RECORD while the trend is what's derived from it; `/graphs` keeps the chart and the
  goal and links here (as does the `/workouts` header, next to Library — not because
  weighing is training, but because that's the header you look in for "the other thing I
  log"). It used to live on the `/trainer` plan card — a box under the sets, submitted
  only when you tapped Finish — which tied a DAILY measurement to whether you happened to
  train that day; that box, `POST /trainer/{id}/bodyweight`, `_with_bodyweight` and
  `data.bodyweight_on` are all long deleted, and the plan card is about sets.
  The list is deliberately EVERY ROW (`data.bodyweight_log`), not `graph_data`'s
  one-point-per-day: a scale can record twice in a morning (a re-weigh, someone else
  stepping on it), and the second reading vanishes from a day view — latest wins — while
  still sitting in the table owning `MIN(weight_lbs)`, which is the figure every "lowest
  ever" is measured against. Seeing it is now the only thing you can do about it from
  here, which is the accepted cost of one door. The per-session "Weight:" line on
  `/workouts` stays — it's a same-day join (`data.workouts_full`), so it reads as what
  you weighed that morning.
- `collections` + `items` (+ `items_fts`) — DORMANT. The notes & collections layer
  (an inbox of notes promotable into model-defined collections, with list/table/
  cards/map views) was removed with the journal connector; existing DBs keep the
  tables and rows, nothing reads or writes them, and `SCHEMA` no longer creates them.
- `settings` — generic JSON KV; holds `profile` (`goals`, `split`, `session`,
  `injuries`, `coaching`, free-form beyond those) merged via `update_profile` and surfaced by `get_fitness_briefing`,
  and `eating_profile`, whose one live key is `targets` — the flat {nutrient: number}
  water/protein goals, written by `set_intake_targets` (the trainer tool, also behind
  /food's Targets popover), validated by `_bad_targets` (a real nutrient key, a positive
  number — the rings silently skip anything else, so an unvalidated write would
  report success while the ring kept the old number), and read by the rings
  (`data.nutrient_targets()` → `_day_targets`, merged over `INTAKE_TARGET_DEFAULTS`) and the trainer's
  intake returns. Its other keys (goal/context prose, `targets_note` with its
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
  quiet second copy this cleanup removed. Surface blurbs (`_TRAINER_BLURB`)
  describe the SCREEN only — never how to train. `profile.coaching`
  still has two doors onto one copy: `update_profile` (the model, when the user asks
  in so many words) and `server.set_trainer_profile` (the /trainer page's legacy
  **Coaching** popover); `update_profile` drops a key sent as null.
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
