# CLAUDE.md

Orientation for working in this repo with Claude Code. Read this first.

## What this is

A single-user **conversational journal** exposed to Claude as an **MCP server**. The
user talks about their day; Claude captures entries and resolves *who* they mean to
stable person records, so later "everything about Tom my father" is an exact lookup
that never pulls in the other Tom. Runs locally over stdio (Claude Desktop) or as a
remote HTTP server behind Google auth (phone access via claude.ai connectors).

**Two MCP servers, one process, one DB.** The training feature is a *second* FastMCP
instance — `trainer_mcp`, exposed at its own endpoint `/trainer/mcp` — separate from
the journal+eating server (`mcp` at `/mcp`). Both live in `server.py` and share the
same SQLite DB; each has its OWN Google auth provider (providers are single-resource —
see the auth section). Each is its own connector → its own Claude project, so a
conversation loads only that half's tools (smaller tool surface = less latency, the
reason for the split). It's purely an MCP-layer division: the webapp still imports this
module's functions unchanged. `webapp/combined.py` composes both endpoints onto one
origin.

**The journal connector is intake + collections only.** The user does all journal
capture through the app's own chat, so the journal server's people/entry tools
(`add_journal_entry` … `get_briefing`; the `CONNECTOR_HIDDEN_TOOLS` set) are hidden
from MCP clients by `HiddenToolsMiddleware` — dropped from `tools/list`, rejected on
`tools/call`, plus a gate on `delete_record(kind="entry")`. The app chat is untouched
because it bypasses middleware (`list_tools(run_middleware=False)` + direct function
calls). Same split for the model-facing prose: the `mcp` instance's `instructions`
are the CONNECTOR text (intake + collections; `get_intake` carries `now` since
`get_briefing` is hidden there), while the chat's journal agent takes
`JOURNAL_CHAT_INSTRUCTIONS`, the full contract — shared blocks are composed into
both strings so the surfaces can't drift. Adding a journal tool = adding its name to
`CONNECTOR_HIDDEN_TOOLS` too.

## The one architectural rule

**There is no LLM inside the server, and there must never be one.** The server is a
deterministic data + candidate-matching layer. The contextual judgment ("which Tom?")
is done by Claude in the conversation, using the candidates the server returns. When
adding features, keep that split: the server generates candidates / stores / retrieves;
the model decides. Don't add model calls, embeddings services, or NER inside the server.

The rule is about `server.py`, not the whole repo. The **webapp does contain an LLM** —
`webapp/chat.py` is the web app acting as an *MCP client*, driving the same
`@mcp.tool()` functions over the Anthropic API (in-process, no transport) so the
phone/browser gets the full conversational capture — including the journal tools the
MCP connector hides (see `HiddenToolsMiddleware` above). That preserves
the split rather than breaking it: the model still does the judgment, `server.py` stays
the deterministic data layer with no LLM inside it. So `anthropic` in
`webapp/requirements.txt` is expected — it lives on the client side of the line.

The same split governs the **trainer** and **drinking** features: the server stores
workouts/drinks and computes deterministic aggregates (muscle recency, drink streaks),
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
  and every date default (`entry_date`, `drink_date`, `workout_date`) plus streak/recency
  math roll over at Pacific midnight, via `PACIFIC = ZoneInfo("America/Los_Angeles")` —
  never the server's UTC midnight. `created_at` stays UTC (an unambiguous storage
  timestamp, not a user date). Both briefings return `now` (`current_clock()`) — which
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
    policy (the three capture rules, Pacific dates, the rotation policy, the signed-weight
    convention); a tool's docstring holds its own mechanics. Where both wanted to say it,
    the other side now points at the owner rather than restating it — restating is how
    the two drift apart.
- **Tool annotations are declared, not defaulted.** Every tool carries one of four
  annotation sets — `READ_ONLY`, `WRITE`, `WRITE_IDEMPOTENT`, `DESTRUCTIVE` — so a client
  can tell `get_briefing` from `delete_record` without reading prose. This matters because
  the MCP default for `destructiveHint` is TRUE: an unannotated tool looks dangerous.
  `openWorldHint` is False everywhere (one local SQLite file, no network — the no-LLM rule
  showing up in the protocol). They're advisory metadata; the real guard is
  `AllowlistMiddleware`.

## Files

- `server.py` — everything: schema, matching, both FastMCP instances (`mcp` =
  journal+eating, `trainer_mcp` = training), all tools, shared auth wiring, the
  shared `_delete_record` helper (each server exposes a kind-scoped `delete_record`),
  and the stdio/http entrypoint (`MCP_SERVER` picks which server stdio runs).
- `webapp/combined.py` — single-process entrypoint (the Dockerfile's `CMD`): serves the
  journal MCP + browser UI (`/app`) on the main origin, and the trainer MCP either on
  its own host (`TRAINER_PUBLIC_URL` set → Starlette `Host` routing) or grafted at
  `/trainer/mcp` on the main origin (authless fallback).
- `webapp/app.py` — the FastAPI UI: routes + page rendering for the browser app (mostly
  read-only browse pages — including the `/trainer/library` exercise library — plus the
  direct drinks-entry form and the `/chat` panel mount).
- `webapp/data.py` — the UI's read-query layer (the SQL behind the browse pages; keeps
  `app.py` thin). Read-only — writes go through `server.py`'s tools.
- `webapp/chat.py` — the in-app AI chat: web-app-as-MCP-client agent loop (see the
  architectural-rule note). Server-bound agents (`journal`, `trainer`) lift their system
  prompt + tool schemas live from a FastMCP instance's `instructions` + tool docstrings,
  so changing a docstring updates the chat. (Exception: the journal agent's system
  prompt is `server.JOURNAL_CHAT_INSTRUCTIONS`, not the instance's `instructions` —
  those are the connector-facing subset; see the hidden-tools note above.) The `exercise` agent is different: it's a
  WEBAPP-DEFINED agent (its `instructions` and its two tools — `check_library`,
  `create_exercise` — are hand-written here, NOT lifted from a server) so the
  exercise-creation path stays OFF the MCP tool surface. It backs the library page's
  "+ Add an exercise" panel and is the one place an LLM can grow the catalog — reachable
  only by the authenticated user, never by the journal/trainer connectors. Off unless
  `ANTHROPIC_API_KEY` is set; model via `CHAT_MODEL`.
- `webapp/templates/`, `webapp/static/` — Jinja templates and PWA assets (icons,
  `chat.js`, manifest); the app is an installable PWA. Styles are COMPILED
  Tailwind (`static/tailwind.css`, checked in — no CDN, the app styles itself
  offline); after adding/removing classes in templates or static JS, rebuild:
  `cd webapp && npx -y tailwindcss@3.4.17 -i tailwind.input.css -o static/tailwind.css --minify`
  (config + why in `webapp/tailwind.config.js`). Inter and `marked` are
  self-hosted (`static/fonts/`, `static/vendor/`) for the same reason.
- `webapp/requirements.txt` — the UI's extra deps (fastapi, uvicorn, jinja2, authlib,
  httpx, and `anthropic` for the chat); install alongside the root `requirements.txt`,
  which it imports `server.py` from.
- `icons.py` — GENERATED (`scripts/build_icon_set.py`): the collection icon set, a
  curated ~130-name subset of **Lucide** vendored as raw SVG shapes, plus its
  grouping. The pack matters because the MODEL picks the name: `list_icons()` ships
  the set over MCP and `_bad_icon` rejects anything else with the closest matches, so
  it can't invent a Lucide name the app doesn't carry. Lucide because the nav bar's
  hand-written icons already are Lucide strokes. Re-run the script (needs npm once)
  only to add names or move Lucide versions — nothing fetches at runtime.
- `scripts/` — `seed_dev.py` (load throwaway dev data), `gen_icons.py` (regenerate
  the PWA icon set), and `build_icon_set.py` (regenerate `icons.py`, above).
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
# local (Claude Desktop, stdio) — stdio runs ONE server; pick it with MCP_SERVER:
.venv/bin/python server.py                       # journal+eating (default)
MCP_SERVER=trainer .venv/bin/python server.py    # trainer
# remote, both endpoints in one process (this is what the Dockerfile runs):
MCP_TRANSPORT=http PORT=8000 JOURNAL_DB=./journal.db .venv/bin/python webapp/combined.py
#   journal connector: /mcp   ·   trainer connector: /trainer/mcp   ·   UI: /app
```

Env vars: `JOURNAL_DB` (path), `MCP_TRANSPORT` (`stdio`|`http`), `MCP_SERVER`
(`journal`|`trainer`, stdio only — which server a bare `server.py` launch runs),
`PORT`, `MCP_HOST`. Auth (set all to protect; unset = authless for dev/staging only):
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `PUBLIC_URL` (bare origin, no trailing
slash, no `/mcp`), `JOURNAL_ALLOWED_EMAILS` (comma-separated; normally just yours), and
`TRAINER_PUBLIC_URL` (bare origin of the trainer's own host — enables the trainer on its
own subdomain; unset = trainer falls back to `/trainer/mcp` on the main origin, authless
only). Google redirect URIs: `<PUBLIC_URL>/auth/callback` and, if the trainer host is
set, `<TRAINER_PUBLIC_URL>/auth/callback`. See the auth section. Webapp-only:
`ANTHROPIC_API_KEY` (enables the `/chat` surface; unset = chat off, rest of the app runs
normally), `CHAT_MODEL` (chat agent model, defaults to `claude-sonnet-4-6`), `SHOW_LOGOUT`
(show the logout control in the UI), and `BACKUP_TOKEN` (strong random token that unlocks
the headless backup download at `GET /export/journal.db` for a cron `curl` — bearer /
`X-Backup-Token` / `?token=`; unset = browser-session-only; see README "Backup &
restore"), and `WIDGET_TOKEN` (unlocks `GET /api/today.json` — today's nutrient sums
only — for an ambient display like the SwiftBar plugin in `scripts/swiftbar/`; a
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
Authless, POST an `initialize` JSON-RPC call to `/mcp` and `/trainer/mcp` (Accept:
`application/json, text/event-stream`) and expect `200`. With auth on (and
`TRAINER_PUBLIC_URL` set), the trainer moves to its own host — send `Host:
<trainer-host>` to `/mcp`; both hosts should `401` with a `WWW-Authenticate` whose
`resource_metadata` pointer resolves to `200` at THAT host's root. `/health` should be
`200` regardless of auth.

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
- `mentions` — one per reference in an entry; `surface_form`, `person_id` (NULL while
  pending), `status`, `context_snippet`.
- `groups` + `person_groups` — explicit circles (family, colleagues, …), many-to-many.
- `drinks` — LEGACY, dormant. Alcohol is an intake item now (see `intake_items`);
  this table and `server.log_drinks`/`get_drink_summary`/`update_drink` are kept only
  as the fold-in migration's source and the one copy of the per-day `kind`
  ("beer, wine"), which the item rows have no column for. Nothing reads it.
- `intake_items` — the INTAKE log: **one row per thing consumed**. A sandwich is a
  row, a beer is a row, a 12oz glass of water is a row — food, alcohol and water are
  the same kind of fact, so they share one table, one tool path, and one set of
  columns. There is no per-nutrient special case anywhere above this table.
  `log_intake` inserts ONE item (the model calls it once per thing; `position` is the
  server-assigned order within the day, the same append as entries' `day_position`);
  there is deliberately NO time-of-day on an item — a day is a day, and `position`
  already carries the sequence (a short-lived `at_time` column was tried and removed;
  a DB that ran that version keeps the orphan column, which nothing reads — every
  INSERT here names its columns, so it's inert either way);
  `update_intake_item` edits one by id; `delete_record(kind="intake_item")` removes
  one.
  **Day totals are DERIVED, never stored** (`SUM ... GROUP BY food_date`). That's the
  load-bearing decision: a stored total drifts from the items it claims to summarize,
  and correcting one item would mean re-deriving the day by hand — i.e. asking an LLM
  to do arithmetic. With sums, "that bowl was 600, not 1100" is one UPDATE and every
  total follows. The nutrient columns (`NUTRIENTS` — calories, protein/carbs/fat,
  `sodium_mg`, `fiber_g`, `standard_drinks`, `water_oz`) are per-item and OPTIONAL,
  staying NULL until filled in, so a day described only in words is "unestimated",
  never a zero-calorie day; a nutrient no item carries is ABSENT from the day's
  totals rather than 0. `get_intake`'s averages are per nutrient over the days
  that carry it, so each has its OWN denominator (returned with `logged_days`). The
  `NUTRIENTS` tuple drives every sum/average/render site, so adding a nutrient is one
  tuple entry + an `ALTER TABLE` in `init_db` + a unit label in `macros.eating_block`.
  Same split as everywhere: turning "a chipotle bowl" into calories is the MODEL's
  estimate, made in conversation; there is no food database in the server — but the
  intake log IS its own food database for repeats: `find_past_items(query)` fuzzily
  searches everything ever logged (token-level scoring — exact/substring/Jaro-
  Winkler/phonetic per query word — grouped by identical item text, latest numbers
  win, ranked by match quality with a small ~30-day-half-life recency bonus so this
  week's leftovers outrank last month's near-twin). The model REUSES those settled
  numbers when it judges a hit is genuinely the same thing, estimates fresh when it
  isn't — deliberately a read tool + model judgment, NOT a curated catalog: a
  `foods` entity table was tried and rejected because the diet varies within brands
  (which Chobani?) and a wrong-but-confident auto-resolve is worse than a
  re-estimate; the log needs no gardening and is always as current as the last
  meal. And day-so-far questions are answered from the DB, never from a chat-side
  running tally (`log_intake` returns `day_totals` on every write; other clients —
  the phone app, another conversation — may be writing the same day); both
  contracts live in the server `instructions` + the intake docstrings.
  The webapp shows the intake log on its OWN `/food` page (`data.food_days` +
  `macros.eating_block`) — one line per item: the item (led by its circled index) and
  its calories + protein. Those two figures are on the line because they're what the
  day is steered by; the other five would just rebuild the rings in worse form, so
  they stay one click away in the detail modal.
  The journal feed carries entries — plus, at the right edge of each day's date, a
  fork-and-knife button opening that day's DISHES, no figures
  (`data._intake_names_by_day`): "that was the night at Gjelina" is how you place a
  day, and putting macros there would turn the journal into a tracker. Bare
  water/alcohol taps have no text, so they're dropped from that list. The modal
  itself is one shared partial (`templates/_detail_modal.html`, included by both
  pages) with a single delegated handler, so a page adds a target just by rendering
  `data-detail` on an element. `/food` is deliberately OUTSIDE the journal lock
  (glancing at macros shouldn't need the knock) and has no chat panel. STRICTLY
  READ-ONLY: intake has exactly ONE write path, the MCP tools (the in-app chat panel
  counts — it calls the same functions). There is no form, no tappable ring, no
  /intake write route; the browser only renders. Water and alcohol rings still always
  render, dashed when unlogged, because "no water yet" is worth seeing on a day you
  mean to hit a gallon. Each nutrient renders as a ring with BOTH
  its summed figure (unit included: "1400mg") and a short label ("sod") inside it
  (`macros.nutrient_ring`), read against `data.nutrient_targets()` — the stored
  eating profile's `targets` numbers merged over the `data.NUTRIENT_TARGETS`
  defaults, resolved per render. The NUMBERS live in the DB now (settings
  `eating_profile`, written by `update_eating_profile`) so the rings and the model
  coach against the same goals; what stays display-only is DIRECTION — which
  nutrient is a ceiling vs a floor is a webapp reading, never stored. A `ceiling`
  target (sodium, calories, alcohol)
  turns the ring clay once passed; a floor one (protein, carbs, fiber, water)
  doesn't, and an untargeted nutrient (fat) draws a DASHED track with no arc — an
  empty solid ring would read as "0% of goal" rather than "no goal set". A tapped
  top-up has no text, so it's named from what it carries ("16oz water"). Four
  things a rendered day can't say for itself — an
  item's OWN nutrients (the rings show the day summed), a ring's TARGET (an arc can
  only imply it), what the day's DRINKS cost in calories (a count of standard drinks
  is the same "2" for two light beers and two margaritas — `data._day_nutrition`
  derives it by summing the calories of the items carrying alcohol, and withholds
  the share-of-day figure while any of them is unestimated, since a partial numerator
  over the same partial denominator overstates it), and the day's item NOTES — are
  all one CLICK away, into a single
  display-only modal in `food.html`. Each click target carries its whole payload
  as JSON in `data-detail` (`{kicker, title, rows, note}`), so the modal needs no
  fetch and no lookup, and one delegated handler serves them all. Hover just firms
  the text/figure to black — no tooltips, no underlines, nothing that would fight
  the prose. The circled "i" (`macros.note_button`) at the end of the ring row is
  the notes' target.
- `nutrition` — LEGACY, dormant. The first shape of the intake log: one row per day,
  with a "; "-joined summary string and stored day totals. Superseded by
  `intake_items` (see above) because a stored total can't be corrected without
  arithmetic. Its rows fold into `intake_items` once, one item per day, on the first
  `init_db` after this change; the table is kept, not dropped.
- `exercises` — the exercise catalog (stable entities, like people): `slug`, `force`,
  `level` (difficulty), `mechanic` (compound/isolation), `equipment`, `technique_notes`,
  `common_mistakes`, `cautions`, `video_link`, `image_link` + `image_link_end` (the rep's
  start and finish frames — the library crossfades the two into a looping rep animation
  rather than showing one frozen still), `in_rotation`, `hearted`, and
  `archived`. PRE-LOADED
  with ~870 movements from free-exercise-db (`scripts/import_exercises.py`); the schema
  mirrors that dataset. **Three nested layers** (`rotation ⊆ hearted ⊆ library`): the whole
  catalog is the **LIBRARY** (browsable at `/trainer/library`); `hearted=1` marks the
  **SUPERSET**, the user's bench of favorite movements; `in_rotation=1` marks the small
  curated **ROTATION** (the user keeps it to ~10–14 so progress on each lift is easy to
  track), the only pool the trainer programs from. The rotation is drawn from the hearted
  superset — every few months the user swaps some of the rotation out for other hearted
  lifts. `set_rotation`/`set_hearted` curate the two pools (mirrored by the library page's
  ★/♥ toggles); the invariant `in_rotation ⇒ hearted` is enforced everywhere either flag is
  written — adding to the rotation hearts it, un-hearting drops it from the rotation, archiving
  clears BOTH, and the website's **+ Add an exercise** panel lands new movements in the
  *hearted superset* (not the rotation, which stays a deliberate hand-curated ~14). **Logging
  a movement hearts it but NEVER adds it to the rotation** — the rotation is the user's
  control over their progression, so it grows ONLY on an explicit `set_rotation` request (in
  chat, after clear confirmation) or via the website's ★ toggle, never automatically from
  training. The
  catalog is **CLOSED to the model**: the logging/planning tools resolve a name against it
  (fuzzily — exact, then spacing/punct-insensitive, then a high-confidence typo match;
  `EX_CONFIDENT` is high so Hack/Back Squat surfaces as a candidate, not a silent
  mis-resolve) and a name with no match is SKIPPED and returned under `unmatched` with its
  closest `candidates`, never auto-created. New exercises enter ONLY through the website's
  **+ Add an exercise** panel on `/trainer/library` — an AI helper (the webapp-defined
  `exercise` chat agent in `webapp/chat.py`) that dedupes, fills every field from the
  user's words plus its own knowledge, and calls `create_exercise` — plus the bulk
  importer. Both are NON-tool paths the *connectors* can't reach: `create_exercise` is
  never a FastMCP tool, so the journal/trainer servers still can't grow the catalog; only
  the authenticated user, through the website, can. The model-facing `save_exercise` only
  *enriches* existing rows or toggles `in_rotation`.
  `find_exercises(similar_to=…)` returns like-for-like swap peers (shared primary muscle + same
  mechanic). **Archiving** (`archived=1`) is a SOFT delete — the library page's "remove"
  control (`server.set_archived`, another NON-tool, website-only path; it also clears
  `in_rotation`). An archived movement is invisible everywhere the catalog is *discovered*
  — the library, name search, name-resolution, swap peers, the rotation — so the model has
  no idea it exists, exactly as if deleted; but the row and any past-workout links it's
  referenced by survive, and by-id history (`get_exercise_history`, etc.) still resolves.
  Restore from the library's **Archived** view, or by re-adding the same name on the add
  form (`create_exercise` reuses & un-archives the row rather than colliding on the UNIQUE
  name).
- `exercise_aliases` — AKAs (common alternative names) per exercise, the `aliases` table's
  twin for the catalog: one canonical row, many surface forms ("rdl"→Romanian Deadlift,
  "bench"→Barbell Bench Press). `_resolve_exercise`/`_match_exercises` score against the
  canonical name AND its AKAs (archived rows still excluded), so a lift resolves and the
  library search surfaces it by whatever the user calls it. Stored lowercased; an AKA never
  creates a row (catalog stays closed). Set via `save_exercise`/`create_exercise`'s
  `aliases=`; the default-library AKAs are seeded by `scripts/seed_exercise_akas.py` (keyed
  by exact NAME, not id — ids differ between dev and prod — so it's safe to run against
  production; merge, idempotent).
- `exercise_muscles` — normalizes muscle→exercise so per-muscle recency/volume is a
  plain GROUP BY. `role` is one of three EMPHASIS tiers — primary|secondary|tertiary
  ("how hard" each muscle is worked, e.g. a thruster = shoulders primary, quads/glutes
  secondary, triceps tertiary); recency/volume count all tiers equally. Canonical muscle
  list is `MUSCLES`.
- `workouts` + `sets` — session + per-set `weight_lbs`/`reps`/`rpe` (1-10 RPE), plus
  `duration_seconds`/`distance_miles` for cardio (running/walking/rowing — all NULL for
  lifts, weight/reps NULL for cardio). A planned set also carries `target_rpe` — the
  difficulty the trainer programs for it (1-10), the target twin of the actual `rpe`. The
  /trainer card surfaces difficulty as Easy/Med/Hard buttons (mapped Easy≈5, Med≈7,
  Hard≈9, in `trainer.js`), prefilled from `target_rpe` on a pending set (or the actual
  `rpe` when correcting a done one) — the user confirms a feel instead of typing a number,
  and weight is a `[−5][−1][−.5] (n) [+.5][+1][+5]` stepper over a still-editable field.
  The two-level log mirroring entries/mentions. A
  *planned* session (`status='active'`, from `start_workout_plan`) is UNDATED — its
  `workout_date` is the `''` not-yet-done sentinel until `finish_workout` stamps it with
  the day it was actually completed (so a plan started late and finished after Pacific
  midnight dates to the finish day, not the start). A direct `log_workout` is already
  done, so it dates immediately. Active workouts are excluded from all history/briefing
  aggregates by `status`, so the empty date never leaks. (`log_workout` is the
  immediate-done path; the empty sentinel only ever exists on an in-progress plan.)
  Because a plan is undated until finish, **planning ahead needs no extra date plumbing** —
  "make tomorrow's session" is just `start_workout_plan` now, finished (and so dated)
  tomorrow. The only thing that shifts by a day is recovery: `get_fitness_briefing(as_of=…)`
  re-anchors `days_since` to the day you're planning FOR (default today; pass tomorrow's
  date for a tomorrow plan), so what's "due" already reflects the extra rest. The server
  just changes the reference date for the subtraction — the programming judgment is still
  the model's.
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
  weight for that day; a day with no row simply wasn't weighed. `log_bodyweight` adds
  one (returning `change_lbs` vs the prior weigh-in) and `get_fitness_briefing` surfaces
  the latest reading + 30-day change; the longer trend lives in the webapp (which joins
  the reading onto each session by date, shown inline, and as a header trend), not a
  dedicated server tool. There is NO weight-goal/target logic in the server — the
  coaching is the model's, as everywhere else.
- `collections` + `items` — the FLEXIBLE layer (design: `plan-2026-08-13-collections.md`):
  everything the user wants kept that doesn't need bespoke schema (recipes, trip
  ideas, …). An item with `collection_id` NULL is an **inbox note** — the capture
  default; a collection is model-proposed, user-approved (`save_collection` blocks
  near-duplicate names with "did you mean?" candidates unless `force=True`), wears an
  `icon` (a name from the vendored Lucide set — see `icons.py`; NULL draws the default
  folder — written ONLY by `save_collection`: the glyph is part of what a collection IS,
  so it's the model's like `fields`, not a rendering pref the Display popover touches), and
  carries its shape as METADATA: `fields` (JSON `[{key,label,type,options?}]`, types
  text|number|date|select). Shape is the model's; LAYOUT is not — the legacy
  `display_hint` column is dormant, the view lives in the webapp-only `display`
  JSON (see the popover below). Items hold markdown `body` (the prose), a `featured_image_url` (the
  FEATURED IMAGE — a first-class items COLUMN, not a declared field, so every
  item carries one whether or not it's filed and no collection has to declare
  an image field; http/https only, since the webapp drops it straight into an
  `<img src>`, and `""` clears it via `update_item`. Rendered as a thumbnail on
  every item row — collection page, inbox, search — and full-width on the item
  page, each with `onerror="this.remove()"` so a dead URL leaves nothing rather
  than a broken-image box. Collections predating the column DECLARED their own
  "featured image" field, so the URL rendered as a badge with the link spelled
  out — `_fold_image_fields` (runs every boot, idempotent) lifts those values
  onto the column, un-declares the field, and `_norm_fields` now REFUSES an
  image-ish field so it can't come back), a `data` JSON blob validated
  against the collection's fields (unknown key / bad type / bad select value come back
  as actionable errors — facts with no field stay in the body). An item had `tags`
  too, and they're GONE: a third way to structure a thing, next to the collection
  it sits in and that collection's fields, but nothing ever filtered by one — they
  rendered as inert badges and their only real job was padding the FTS mirror with
  words the title and body already carried. Dropped rather than made filterable
  (`init_db` drops the column and rebuilds `items_fts`, which names its columns);
  "fields stay few" argues the same way for tags. Promotion
  (note → collection item) is `move_to_collection`: pure data movement, reversible,
  NO DDL — the bespoke-table rung of the ladder stays a deliberate human+code
  migration in `init_db()`, never an MCP call. `items_fts` (title/body, same
  trigger pattern as `entries_fts`) backs `search_items`, through `_fts_query` so
  punctuation is safe. `update_item` merges `data` per key (null drops) but replaces
  the body wholesale (read-before-write via `get_item`). `delete_record` gained two
  kinds: `"item"` (gone for good) and `"collection"` (shell only — FK is ON DELETE
  SET NULL, so its items demote to inbox notes). Collections are addressed by NAME
  everywhere else (`save_item`, `move_to_collection`), so `list_collections` and
  `save_collection` both return the `id` that this one kind needs — without it a
  collection was undeletable over MCP — reachable by name but not by handle.
  The webapp browses it at
  `/collections` (+ per-collection and per-item pages, rendered generically from the
  collection's own fields + view prefs — no per-domain view code), OUTSIDE the
  journal lock like `/food`, strictly read-only for CONTENT like everything else.
  Collections are a PRIMARY section: the fourth icon on the nav strip (so the number
  shortcuts run 1-4 in nav order, then 5 graphs / 6 trainer), and `/collections` is a
  GRID of icon cards rather than a list — the icon is what you aim at, and a stack of
  near-identical text rows made every collection look alike.
  The one thing the browser writes is PRESENTATION: each collection page has a
  **Display** popover (`view` = list|table, webapp-only: it was a model-written
  `display_hint` column until that guess proved worthless — the first popover
  visit overwrote it, so one concern had two homes and only the browser's ever
  won. `init_db` folds the old column into the JSON once and it's dormant after,
  kept not dropped; which declared fields show as table
  columns / list badges; `group_by`/`sort_by`/`sort_dir`; and the row extras —
  notes-preview/updated/featured-image) saved
  to a webapp-only `display` JSON column via `POST /collections/{name}/display` →
  `server.set_collection_display` — a NON-tool, website-only path like
  `set_archived`, invisible to the model and to tool returns. Those prefs are
  PER-COLLECTION and persist in the DB, so a collection stays arranged the way the
  user left it, on every device — nothing lives in the browser. Arrangement is
  resolved in `data.collection_page`, which always hands the template `groups`
  (one unlabeled bucket when ungrouped), each bucket pre-sorted, so both views
  just loop; a bucket for items MISSING the grouped value sorts last, and a
  `select` field groups in its own declared `options` order. The views are two,
  not three: a `checklist` hint was dropped (it rendered exactly like `list`, and
  an item has no done-state to check), migrated to `list` in `init_db`.
  `/collections` also carries a title-only search across every collection AND the
  inbox (`data.search_item_titles`, plain LIKE) — a "where did I file that"
  lookup, deliberately not the model's FTS `search_items`. The three
  judgment rules (capture first/file second; structure proposed, never imposed;
  fields stay few) live in the journal server `instructions`.
- `settings` — generic JSON KV; holds `profile` (injury, split, goals) merged via
  `update_profile` and surfaced by `get_fitness_briefing`, and `eating_profile`
  (its journal-side twin: durable eating facts — goals, stats, coaching context —
  plus the one structured key `targets`, a flat {nutrient: number} dict of daily
  goals) merged via `update_eating_profile` and surfaced by `get_intake`, so a new
  conversation needs no pasted preamble. The webapp's rings read `targets` too
  (`data.nutrient_targets()` merges it over the display defaults) — one source of
  truth for goals, ceiling/floor direction still webapp-only.

## Matching (in `find_candidates` / `score_surface_against_alias`)

Exact alias = 1.0; otherwise Jaro-Winkler, floored to 0.88 when Metaphone keys match
(catches sound-alike transcription noise). Candidates below **0.6** are dropped. Returns
top scorer per person. The emergent "who's talked about together" graph
(`get_related_people`) is a self-join over `mentions` — no tagging, no extra storage.

## Auth flow (when enabled)

`GoogleProvider` makes the server its own OAuth 2.1 authorization server (PKCE + Dynamic
Client Registration) that proxies Google; Claude discovers it via the 401's
`resource_metadata` pointer and self-registers, so no client ID/secret is entered in
Claude's connector UI. `AllowlistMiddleware.on_call_tool` then rejects any authenticated
account whose email isn't in `JOURNAL_ALLOWED_EMAILS` — a valid Google login alone is
not enough. Google redirect URI is `<PUBLIC_URL>/auth/callback`.

**Two endpoints, a provider EACH (never shared).** A `GoogleProvider` is single-
resource: building its HTTP app calls `set_mcp_path()`, which writes `_resource_url`
*onto the provider instance*, and that is what incoming tokens are validated against. If
both servers share one provider object, building the second app overwrites the first's
`_resource_url`, and the first endpoint then rejects all of its own tokens ("auth
failed / server configuration issue"). So `server.py` builds a fresh provider per server
(`_build_auth()` called twice) — journal keeps `_resource_url=/mcp`, trainer keeps
`/trainer/mcp`. Each advertises its own protected-resource metadata (`.../mcp`,
`.../trainer/mcp`), both resolving at the root because `combined.py` builds each MCP app
at the root (NOT as a Starlette sub-mount, which would prefix the discovery docs).

**Two full OAuth servers can't share one origin** — their `/authorize`, `/token`,
`/auth/callback` paths collide, and a same-origin token-reuse hack does NOT work in
practice (verified: the trainer connector fails to authenticate that way). So the
trainer runs on its **own host**: set `TRAINER_PUBLIC_URL=https://<trainer-host>`, give
its provider that base_url, and `combined.py` routes that hostname (Starlette `Host(...)`,
which dispatches by Host header WITHOUT prefixing paths, unlike `Mount`) to the trainer
app at its root. The trainer then has a complete, isolated OAuth server at its own origin
(`/mcp`, `/.well-known/*`, `/authorize`, `/auth/callback`). The Google client just needs
`<trainer-host>/auth/callback` added as a redirect URI. The journal host is untouched.

With `TRAINER_PUBLIC_URL` unset (local/authless), `combined.py` falls back to grafting
the trainer's `/trainer/mcp` endpoint + its protected-resource metadata onto the main
origin — fine when there's no OAuth, so the collision is moot.

## Gotchas

- Use the standalone `fastmcp`, not `mcp.server.fastmcp` — the auth providers live in v3.
- `PUBLIC_URL` must be the bare origin. A trailing slash or `/mcp` breaks OAuth discovery.
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
