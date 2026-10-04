# CLAUDE.md

Orientation for working in this repo with Claude Code. Read this first.

## What this is

A single-user **conversational journal** exposed to Claude as an **MCP server**. The
user talks about their day; Claude captures entries and resolves *who* they mean to
stable person records, so later "everything about Tom my father" is an exact lookup
that never pulls in the other Tom. Runs locally over stdio (Claude Desktop) or as a
remote HTTP server behind Google auth (phone access via claude.ai connectors).

**Three MCP servers, one process, one DB.** The training feature is a *second* FastMCP
instance — `trainer_mcp`, exposed at its own endpoint `/trainer/mcp` — separate from
the journal+notes server (`mcp` at `/mcp`); the learning feature is a *third*,
`teacher_mcp` at `/teacher/mcp` (a spaced-repetition log, ported from the standalone
`teacher` repo — its logic lives in the `learning/` package, only the thin
`@teacher_mcp.tool()` wrappers live in `server.py`; see the `subjects`/`facets` row in
the data model). The trainer also carries the small daily **water/protein log** (see
`intake_items`) — it used to be a full food tracker on the journal connector and was
cut down and moved there. All live in `server.py` and share the
same SQLite DB; each has its OWN Google auth provider (providers are single-resource —
see the auth section). Each is its own connector → its own Claude project, so a
conversation loads only that slice's tools (smaller tool surface = less latency, the
reason for the split). It's purely an MCP-layer division: the webapp still imports this
module's functions unchanged. `webapp/combined.py` composes the endpoints onto one
origin (a secondary server moves to its own host when its `*_PUBLIC_URL` is set).

**The trainer is MCP-only now; the app is legacy for it.** The user trains entirely
through the trainer connector in Claude (planning the week, reporting sets between
lifts, progress and advice) and keeps the web app for the journal. So nothing a
training workflow needs may live only behind a webapp page: the four things that did
each got a tool — `complete_sets` (a batch, because a conversation reports a whole
exercise at once where the card tapped one set; the single-set `complete_set` stays as
the card's plain helper), `remove_from_plan` (the card's per-exercise delete),
`add_exercise` (the library's add panel — the library itself is gone now, see the
`exercises` row) and
`import_weigh_ins` (the `/weight` upload — see `body_weight`). `complete_sets` and
`log_workout` return `new_prs` (`_new_bests`, `pr_for_set`'s rule applied per batch)
because the confetti that used to announce a best has no screen to land on. The
`trainer_mcp` instructions open by saying the conversation IS the interface (plan as a
table, ids never shown, short mid-session replies). The trainer also carries the
water/protein log (see `intake_items`). The `/trainer`, `/workouts`,
`/weight` pages still work and still read the same DB; don't build new trainer UI there.

**The journal connector is notes & collections only.** The user does all journal
capture through the app's own chat, so the journal server's people/entry tools
(`add_journal_entry` … `get_briefing`; the `CONNECTOR_HIDDEN_TOOLS` set) are hidden
from MCP clients by `HiddenToolsMiddleware` — dropped from `tools/list`, rejected on
`tools/call`. **The app chat is the exact COMPLEMENT of that, not a superset.** It
bypasses the middleware (so it *could* see everything) and then narrows its tool list
to `CONNECTOR_HIDDEN_TOOLS` itself (`_AGENTS["journal"]["include"]`): the connector
gets notes/collections, the panel gets people + entries, neither gets the other's.
That mirrors how the app is used — the journal is written in the app's chat, saved
notes are captured in Claude — and it's stated as the complement of one frozenset so
the halves can't drift: adding a journal tool means adding its name there (already
the rule) and it lands on both sides at once, while a notes tool needs no chat change
at all. Deleting an entry needs no special gate — it's its own tool
(`journal_delete_entry`), hidden like the rest, rather than a `kind` on a shared
delete. Same split for the model-facing prose: the `mcp` instance's `instructions`
are the CONNECTOR text (collections), while the chat's journal agent takes
`JOURNAL_CHAT_INSTRUCTIONS`, the journal contract alone (it drops the collections
block and adds `_JOURNAL_ONLY_BLOCK`, which tells the model a meal or a lift
mentioned in passing is part of the ENTRY — write it down, don't offer to log it
somewhere this panel can't reach) — shared blocks are composed into both strings so
the surfaces can't drift. Adding a journal tool = adding its name to
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
phone/browser gets conversational capture for the journal proper — the people/entry
tools the MCP connector hides, and only those (see `HiddenToolsMiddleware` above). That preserves
the split rather than breaking it: the model still does the judgment, `server.py` stays
the deterministic data layer with no LLM inside it. So `anthropic` in
`webapp/requirements.txt` is expected — it lives on the client side of the line.

**One tool leaves the machine, and it's still the same split.** `notes_geocode`
asks OpenStreetMap's Nominatim what an address is at, because a `location` field
now REQUIRES coordinates (the map view can't plot an address) and the model
doesn't always know them. It fits the rule rather than bending it: it returns
CANDIDATES and never picks — exactly `find_candidates` for people and
`_match_exercises` for lifts — and it never writes, so the model passes the
numbers it chose to `notes_save`/`notes_file` itself. Two deliberate limits.
It is NOT on the write path: geocoding inside `_bad_location` would put someone
else's server between the user and a saved note, and capture must never block on
that; a failed lookup is a returned `{"error": …}` the model works around with
coordinates it knows. And it's the ONE tool with `openWorldHint: True`
(`READ_EXTERNAL`) — flagged honestly, because a client can't tell a local lookup
from a remote one by reading prose. Nominatim is free and keyless; the policy
(identifying User-Agent, ≤1 req/sec) is why `_geocode_wait` throttles and
`GEOCODE_USER_AGENT` is settable. TLS trust goes through `certifi` when it's
importable — a stock macOS python has no CA bundle wired into `ssl`, so without
it dev fails `CERTIFICATE_VERIFY_FAILED` while the Docker image works, a
difference that only ever shows up on the machine the code is written on.

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
  already tomorrow for the seven or eight hours after Pacific 4/5pm, so every
  collection item saved in the evening rendered (and sorted) a day ahead. Anything
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
  `openWorldHint` is False everywhere but one (one local SQLite file, no network — the
  no-LLM rule showing up in the protocol); the exception is `notes_geocode`, which wears
  a fifth set, `READ_EXTERNAL`, because it asks OpenStreetMap (see the architectural-rule
  section). They're advisory metadata; the real guard is
  `AllowlistMiddleware`.
- **Connector tool names are `domain_verb`, and the domain prefix is load-bearing.**
  Every tool the journal connector advertises is prefixed `notes_*` or
  `collections_*` (`notes_search`, `collections_save`, …): a single note vs. the
  collection it's filed in. Clients render `tools/list` in name order, so the prefix
  makes the list group itself by domain. (It mattered more when the connector also
  carried the eating log — an intake item and a collection item were easy to confuse;
  that log now lives on the trainer, unprefixed.) The MCP name is set with
  `@mcp.tool(name=…)` and the PYTHON function keeps its original name — the webapp
  calls these functions directly, so renaming only the wire name keeps that surface
  untouched. Note the one asymmetry: `webapp/chat.py` dispatches by the MCP name (it
  lifts tools from `list_tools`), so its `_WRITE_TOOLS` set and `_tool_chip` branches
  key off the wire names. The trainer server is a single domain on its own connector
  and needs no prefix. Adding a connector tool = giving it a domain prefix.
- **Destructive tools are narrow, not kind-scoped — on the journal side.** The journal
  server has three deletes (`journal_delete_entry`, `notes_delete`,
  `collections_delete`) rather than one `delete_record(kind=…)`. A `kind` string is a
  thing the model can get wrong on an irreversible call, and it forced the awkward
  case where ONE kind (`entry`) had to be blocked on the connector while the others
  stayed — which was a special case inside `HiddenToolsMiddleware.on_call_tool`. As
  separate tools, hiding the journal delete is just its name in
  `CONNECTOR_HIDDEN_TOOLS`, like every other journal tool. They all still call the
  shared `_delete_record` helper, so the table mapping and the set-renumbering live in
  one place. The TRAINER keeps its kind-scoped `delete_record`
  (`workout`/`set`/`intake`): one connector, kinds that don't overlap. Weigh-ins used
  to be a kind there and no longer are — they're import-only now (see `body_weight`).
- **A write says where the thing now lives.** Capture happens in a Claude conversation;
  the data is READ in the web app — two different screens, which is the standing
  awkwardness of the whole setup. So the connector's write tools return a `url`
  (`_app_url`: `PUBLIC_URL` + the `/app` mount, per `webapp/combined.py`) —
  `notes_save`/`notes_file` → `/item/{id}`, `collections_save` → the collection page,
  and the trainer's `log_intake` → `/food` — and one tap replaces a context switch.
  `PUBLIC_URL` unset (stdio, dev) OMITS the key rather than emitting a dead link. Two deliberate limits: the
  policy line lives in `_APP_LINK_BLOCK`, composed into the CONNECTOR `instructions`
  ONLY — the in-app chat gets the same `url` back but already renders its own local
  chip, and pointing the user at the page they're standing on is noise — and the links
  sit on capture paths, not corrections (`update_intake` returns totals, no url), since
  the returns are tuned token-compact and a link per call is exactly the bloat that
  warning is about.

## Files

- `server.py` — everything: schema, matching, all three FastMCP instances (`mcp` =
  journal+notes, `trainer_mcp` = training + water/protein, `teacher_mcp`), all tools,
  shared auth wiring, the shared `_delete_record` helper (the trainer exposes it as a
  kind-scoped `delete_record`; the journal splits it into three narrow tools — see the
  naming convention above),
  and the stdio/http entrypoint (`MCP_SERVER` picks which server stdio runs).
- `webapp/combined.py` — single-process entrypoint (the Dockerfile's `CMD`): serves the
  journal MCP + browser UI (`/app`) on the main origin, and the trainer MCP either on
  its own host (`TRAINER_PUBLIC_URL` set → Starlette `Host` routing) or grafted at
  `/trainer/mcp` on the main origin (authless fallback).
- `webapp/app.py` — the FastAPI UI: routes + page rendering for the browser app (mostly
  read-only browse pages, plus the
  handful of website-only write carve-outs (`/food/targets`, `/weight` and its `/{id}`
  edit + delete, `/graphs/goal`, `/trainer/profile`, a collection's `/display`) and the
  `/chat` panel mount).
- `webapp/data.py` — the UI's read-query layer (the SQL behind the browse pages; keeps
  `app.py` thin). Read-only — writes go through `server.py`'s tools.
- `webapp/chat.py` — the in-app AI chat: web-app-as-MCP-client agent loop (see the
  architectural-rule note). Server-bound agents (`journal`, `trainer`) lift their system
  prompt + tool schemas live from a FastMCP instance's `instructions` + tool docstrings,
  so changing a docstring updates the chat. (Exception: the journal agent's system
  prompt is `server.JOURNAL_CHAT_INSTRUCTIONS`, not the instance's `instructions` —
  those are the connector-facing HALF, and this panel is the other one; see the
  hidden-tools note above. A server-bound agent narrows its lifted tools with
  `exclude` (drop these) or `include` (keep only these SET of names) — the journal
  panel passes the frozenset.) (The webapp-defined `exercise` agent that backed the
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
  right now (`light|dark`) and is what every dark rule keys off — including the
  map's MutationObserver. The picker is an explicit THREE-way in the nav menu
  because a two-state toggle cannot store one: a toggle can only say "not the
  OS", so it has to GUESS whether a tap meant "dark right now" or "dark from now
  on". Guessing wrong is what left the app sitting white on a Mac that had gone
  dark months after one harmless tap, with nothing on screen to say why. Two
  consequences worth not undoing. The segment FILL keys off the choice, not the
  resolved theme, so System stays visibly selected whichever way the OS is
  leaning. And the old `theme` key is DROPPED on read rather than migrated —
  written by that toggle, its value records no intent that can be read back, and
  a bare `"light"` in it is indistinguishable from a deliberate one. `static/vendor/`
  holds the third-party JS/CSS, self-hosted rather than CDN'd: `marked`, uPlot,
  and `leaflet.min.js`/`.css` (loaded ONLY on a collection's map view). Styles are COMPILED
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
  grouping. The pack matters because the MODEL picks the name: `collections_list_icons()` ships
  the set over MCP and `_bad_icon` rejects anything else with the closest matches, so
  it can't invent a Lucide name the app doesn't carry. Lucide because the nav bar's
  hand-written icons already are Lucide strokes. Re-run the script (needs npm once)
  only to add names or move Lucide versions — nothing fetches at runtime.
- `learning/` — the teacher server's logic (spaced-repetition store, FSRS wrapper,
  SQLite layer, facet templates), ported near-verbatim from the standalone `teacher`
  repo. **Edit it like a vendored library**: it has NO tests, and scheduling
  correctness is the one property you can't check by using it (a wrong interval looks
  right until the card comes back months later) — so changes beyond the three port
  seams (JOURNAL_DB path, Pacific `day_start()`, the `learn_fts` rename) need a reason.
  It keeps its OWN idempotent schema + migrations in `learning/db.py` (run from
  `init_db()` and on first connect) rather than folding into `SCHEMA` — the
  attempts-table rebuild migration stays with the code that owns it. `server.py` wraps
  its store as `teacher_mcp`'s tools; `webapp/data.py` reads it for `/learn`.
- `scripts/` — `import_teacher.py` (one-shot, idempotent copy of a standalone
  teacher repo's DB into the journal DB — ids and FSRS state preserved),
  `seed_dev.py` (load throwaway dev data), `gen_icons.py` (regenerate
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
.venv/bin/python server.py                       # journal+notes (default)
MCP_SERVER=trainer .venv/bin/python server.py    # trainer
MCP_SERVER=teacher .venv/bin/python server.py    # teacher (the learning log)
# remote, all endpoints in one process (this is what the Dockerfile runs):
MCP_TRANSPORT=http PORT=8000 JOURNAL_DB=./journal.db .venv/bin/python webapp/combined.py
#   journal: /mcp   ·   trainer: /trainer/mcp   ·   teacher: /teacher/mcp   ·   UI: /app
```

Env vars: `JOURNAL_DB` (path), `MCP_TRANSPORT` (`stdio`|`http`), `MCP_SERVER`
(`journal`|`trainer`|`teacher`, stdio only — which server a bare `server.py` launch runs),
`PORT`, `MCP_HOST`. Auth (set all to protect; unset = authless for dev/staging only):
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `PUBLIC_URL` (bare origin, no trailing
slash, no `/mcp`), `JOURNAL_ALLOWED_EMAILS` (comma-separated; normally just yours), and
`TRAINER_PUBLIC_URL` (bare origin of the trainer's own host — enables the trainer on its
own subdomain; unset = trainer falls back to `/trainer/mcp` on the main origin, authless
only), and `TEACHER_PUBLIC_URL` (same, for the teacher server / `/teacher/mcp`).
Google redirect URIs: `<PUBLIC_URL>/auth/callback` and, per secondary host that is
set, `<TRAINER_PUBLIC_URL>/auth/callback` / `<TEACHER_PUBLIC_URL>/auth/callback`. See the auth section. Webapp-only:
`ANTHROPIC_API_KEY` (enables the `/chat` surface; unset = chat off, rest of the app runs
normally), `CHAT_MODEL` (chat agent model, defaults to `claude-sonnet-4-6`), `SHOW_LOGOUT`
(show the logout control in the UI), `GEOCODE_USER_AGENT` (the User-Agent
`notes_geocode` sends to Nominatim; a generic default, override to identify your
deploy), and `BACKUP_TOKEN` (strong random token that unlocks
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
  Each literal is a PREFIX query (`"term"*`) — shared by `search_entries` and
  `notes_search`. The load-bearing reason is CJK: unicode61 tokenizes a run of
  Chinese as ONE token, so a recipe titled 宫保鸡丁 was reachable only by typing
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
  **Targets** live in `settings.eating_profile.targets` and have ONE door:
  `server.set_nutrient_targets` (NON-tool, website-only) behind `/food`'s **Targets**
  popover, merging per nutrient with `None` handing a goal back to the
  `data.NUTRIENT_TARGETS` default. The model only reads them. A target is just a
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
  `server.import_bodyweight`, a NON-tool website-only path like `set_nutrient_targets`
  and `set_archived`). Everything else is DELETED, not left dark: the entry form, the
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
- `collections` + `items` — the FLEXIBLE layer (design: `plan-2026-08-13-collections.md`):
  everything the user wants kept that doesn't need bespoke schema (recipes, trip
  ideas, …). An item with `collection_id` NULL is an **inbox note** — the capture
  default; a collection is model-proposed, user-approved (`collections_save` blocks
  near-duplicate names with "did you mean?" candidates unless `force=True`), wears an
  `icon` (a name from the vendored Lucide set — see `icons.py`; NULL draws the default
  folder — written ONLY by `collections_save`: the glyph is part of what a collection IS,
  so it's the model's like `fields`, not a rendering pref the Display popover touches), and
  carries its shape as METADATA: `fields` (JSON `[{key,label,type,options?,unit?}]`,
  types text|number|date|select|url|bool|rating|multiselect|location. A field's
  type is SEMANTIC — it says what the value MEANS, and the app renders it as that
  thing: a `url` as its host (a real link on the item page), a `bool` as a checked
  label ("✓ Cooked" — the one type whose value can't speak without its name), a
  `rating` as stars (0-5 in halves, no configurable max), a `multiselect` as one
  badge per value, a `location` as a pin that opens a maps app (showing its
  short label in lists and cards, but the FULL STREET ADDRESS on the item
  page — where the pin is the one thing on screen and a label alone won't
  say where it goes; the label still leads when the address doesn't already
  contain it). The five beyond
  the original four extend the SHAPE axis the model already owns rather than
  adding a collection-level "kind": a kind would fight both existing axes and
  make a collection state its shape twice — this is the same move `unit` on a
  number already made. A location is `{label?, address?, lat, lng}` and the
  COORDINATES ARE REQUIRED (`_bad_location`), because a collection of places
  renders as a MAP (the fourth `display.view` — see the map-view row below) and
  an address alone is a place with nowhere to go. The model supplies them from
  its own knowledge, or from `notes_geocode` (below). Values written before that
  rule keep an address and no numbers: they render everywhere else, the map
  names them under itself as unplottable (`data._item_pins` returns them rather
  than dropping them silently), and they re-validate — i.e. start failing with
  an actionable error — the next time their item is written. There is no
  back-fill UPDATE and no boot-time geocode: fixing one is a model call, not a
  migration.
  Which types can be arranged by lives in `GROUPABLE_TYPES`/`SORTABLE_TYPES`,
  once, read by both the `set_collection_display` gate and the Display popover's
  two selects (`data.groupable_fields`/`sortable_fields`) so the UI can't offer
  an arrangement the save then refuses: `url` and `location` are NEITHER (a
  bucket per URL is a bucket per item; a coordinate pair has no order), and
  `multiselect` groups but doesn't sort — it fans an item into one bucket per
  value, the single place an item appears twice on a page, which is exactly why
  there's no one value to sort it by. A `select` MUST carry a non-empty,
  duplicate-free
  `options` list, since `_bad_data` can only constrain a value when options exist
  and the webapp groups in their declared order: an optionless select silently
  degraded into a text field that merely CLAIMED to be a closed set (a
  `multiselect` is that same closed set with several values, so it shares the
  branch); a `unit`
  ("min", "nights") is NUMBER-ONLY and rejected elsewhere, because its whole job
  is letting the renderer drop the label — a figure with a unit says what it is
  ("240 min"), a bare one has to be introduced ("Time: 240"), and on a type with
  no figure there'd be nowhere to put it). Because
  `fields` REPLACES the list, an edit that forgets a field un-declares it and
  STRANDS its values on every item — invisible (only declared fields render) and
  blocking (`notes_file` validates the whole merged blob). `collections_save`
  doesn't delete those values, but it now reports them as `stranded`
  {key: item count} with the fix, since silence is exactly how the
  `featured_image` orphans survived 16 items unnoticed. Keeping a field but
  RETYPING it fails the same way one step over — the values sit there and
  quietly stop validating, on a page that still renders them fine — so those
  come back as `mistyped` {key: item count} (`_mistyped_keys`, one key at a time
  since `_bad_data` stops at the first error). Both are advisory, never
  blocking, like `unfilled_fields`; and `macros.field_text` prints an
  impossible-for-its-type value rather than raising, because after a retype it
  meets them as a matter of course. Shape is the model's; LAYOUT is not — the legacy
  `display_hint` column is dormant, the view lives in the webapp-only `display`
  JSON (see the popover below). Items hold markdown `body` (the prose), a `featured_image_url` (the
  FEATURED IMAGE — a first-class items COLUMN, not a declared field, so every
  item carries one whether or not it's filed and no collection has to declare
  an image field; http/https only, since the webapp drops it straight into an
  `<img src>`, and `""` clears it via `notes_update`. Rendered as a thumbnail on
  every item row — collection page (at that collection's `image_size`, see the
  popover below), inbox, search — each with `onerror="this.remove()"` so a dead
  URL leaves nothing rather than a broken-image box. On the ITEM page it's a
  tile too (`.item-hero`), not the full-width hero it started as: a recipe's
  photos were pushing the method a screen down, so the page reads as prose with
  pictures in it rather than an image with prose under it. Every prose image
  (`.chat-md img`, so the journal feed and the chat transcript get it too) is
  likewise a uniform tile — fixed box + `object-fit`, several per row like a
  contact sheet whatever their native aspect — and the full picture is one
  click away in the `#lightbox` overlay (base.html: ONE delegated listener in
  the CAPTURE phase, so it also covers images `marked` renders after load, and
  it stops the click as well as preventing it — an image can sit inside a link
  (the person page's entry_card), and expanding one must not navigate). Collections predating the column DECLARED their own
  "featured image" field, so the URL rendered as a badge with the link spelled
  out — `_fold_image_fields` (runs every boot, idempotent) lifts those values
  onto the column, un-declares the field, and `_norm_fields` now REFUSES an
  image-ish field so it can't come back. The fold sweeps image-ish keys found
  in the BLOB, not just still-DECLARED ones, because the two come apart: drop
  the declaration by hand and the values STRAND — invisible (the app renders
  only declared fields) and poisonous (`notes_file` validates the whole merged
  blob and rejects the unknown key), on exactly the collection a
  declaration-keyed sweep would skip. `_bad_data` checks the null-drop BEFORE
  the unknown-key check for the same reason: a null asks to REMOVE a key, which
  is the one thing a stranded orphan needs), a `data` JSON blob validated
  against the collection's fields (unknown key / bad type / bad select value come back
  as actionable errors — facts with no field stay in the body). An item had `tags`
  too, and they're GONE: a third way to structure a thing, next to the collection
  it sits in and that collection's fields, but nothing ever filtered by one — they
  rendered as inert badges and their only real job was padding the FTS mirror with
  words the title and body already carried. Dropped rather than made filterable
  (`init_db` drops the column and rebuilds `items_fts`, which names its columns);
  "fields stay few" argues the same way for tags. Promotion
  (note → collection item) is `notes_file`: pure data movement, reversible,
  NO DDL — the bespoke-table rung of the ladder stays a deliberate human+code
  migration in `init_db()`, never an MCP call. `items_fts` (title/body, same
  trigger pattern as `entries_fts`) backs `notes_search`, through `_fts_query` so
  punctuation is safe. `notes_update` merges `data` per key (null drops) but replaces
  the body wholesale (read-before-write via `notes_get`). Deletes are two tools:
  `notes_delete` (gone for good) and `collections_delete` (shell only — FK is ON DELETE
  SET NULL, so its items demote to inbox notes). Collections are addressed by NAME
  everywhere else (`notes_save`, `notes_file`), so `collections_list` and
  `collections_save` both return the `id` that this one kind needs — without it a
  collection was undeletable over MCP — reachable by name but not by handle.
  The write returns carry three FRAMES, for the capture-here/read-there split. `notes_save`/`notes_file` report `unfilled_fields`
  (`_unfilled_fields`) — declared fields the item has no value for, the exact mirror
  of `stranded` (values with no field) and reported for the same reason: the return
  said only where the item landed, so nothing ever mentioned that a collection wanted
  a cook time. Advisory, NEVER an error — fields are optional and capture must not
  block on them. And a save that lands in the INBOX reports `inbox_count`: capture-
  first-file-second makes the inbox the default, so it grows invisibly and filing
  happens only if the user thinks to look. It is the inbox's `day_totals`.
  The third is `featured_image` (`_missing_image_note`), on a FILED item with an
  empty picture slot — the same advisory shape one axis over: the featured image
  is the field every collection has without declaring one, so `unfilled_fields`
  never mentioned it and items were landing picture-less by default, on pages
  (rows, thumbnails, the cards view) that are mostly picture. It carries the
  collection's own COVERAGE ("1 of 8 others here have one") rather than a flat
  scold, because whether a picture belongs in THIS collection is a fact about the
  collection and the server doesn't get a vote — nine of eleven says one thing,
  nought of eleven says the opposite, and the model reads it the way it reads
  candidates. Inbox notes are exempt (a scrap like "call the dentist" has no
  picture and nagging on every one is how an advisory stops being read), and it's
  advisory like the others — capture never blocks on an image. The prose pushes
  the same way from two homes: `_COLLECTIONS_BLOCK`'s fourth rule (items are meant
  to have pictures) and the `notes_save`/`notes_file` docstrings, both of which
  pair the encouragement with its one hard limit — NEVER invent a URL. A guessed
  image URL renders as nothing (`onerror="this.remove()"`), so a plausible fake is
  strictly worse than an empty slot: say the picture is missing, or ask for a link.
  `notes_file` grew a `featured_image_url=` for this — promotion is the moment
  you've just read the note and have its source in hand, and it would otherwise
  take a second `notes_update` call; passing None there leaves the existing
  picture alone, the same "None means unchanged" as `notes_update`.
  The webapp browses it at
  `/collections` (+ per-collection and per-item pages, rendered generically from the
  collection's own fields + view prefs — no per-domain view code), OUTSIDE the
  journal lock like `/food`, strictly read-only for CONTENT like everything else.
  Collections are a PRIMARY section: the fourth icon on the nav strip (so the number
  shortcuts run 1-4 in nav order, then 5 graphs / 6 trainer), and `/collections` is a
  GRID of icon cards rather than a list — the icon is what you aim at, and a stack of
  near-identical text rows made every collection look alike.
  A collection's NAME is stored exactly as written and rendered with no
  text-transform anywhere; only the LOOKUP lowercases (`_resolve_collection`,
  `lower(name)=?`). Folding the case at the door instead — which is what
  `collections_save` used to do — threw the capitalization away and left the two
  read surfaces to invent their own: the grid title-cased with CSS (so "Trip ideas"
  came back "Trip Ideas", capitalizing a word nobody wrote) while the collection
  page printed the stored lowercase, and one collection wore two spellings
  depending on which page you were standing on. Re-saving with new capitalization
  RE-CASES the row, which is the migration path for anything created earlier.
  A declared field renders as its VALUE, not `LABEL: value` — inside a collection
  the value almost always names its own field ("ITALIAN" under a chef-hat called
  Recipes), and the prefix wrapped a badge row onto two lines to say nothing. The
  label moves to the `title` tooltip. Two exceptions, both fields whose value
  can't speak for itself: a bare NUMBER, which keeps its label unless the field
  declares a `unit` — which says it shorter — and a BOOL, which IS its label
  ("✓ Cooked", muted when false). `macros.field_badge`/`field_text` own those
  rules plus date formatting and every semantic type's rendering, so all three
  views and the item page agree. `field_badge(linkify=…)` is OFF by default and
  that's structural, not a preference: list rows and cards wrap the whole item in
  an `<a>` to `/item/{id}`, and an anchor inside an anchor is illegal HTML — so
  only `item.html`, whose rows aren't links, passes True, and it's the only page
  where a url or a location is clickable.
  The one thing the browser writes is PRESENTATION: each collection page has a
  **Display** popover (`view` = list|table|cards|map, webapp-only: it was a model-written
  `display_hint` column until that guess proved worthless — the first popover
  visit overwrote it, so one concern had two homes and only the browser's ever
  won. `init_db` folds the old column into the JSON once and it's dormant after,
  kept not dropped; which declared fields show as table
  columns / list badges; `group_by`/`sort_by`/`sort_dir`; and the row extras —
  notes-preview, updated (default OFF: a collection is usually saved in a batch,
  so the stamp repeats identically down every row and spends a line per item
  saying nothing that tells them apart), and `image_size` (`off|small|medium|large`, the
  featured image's thumbnail edge, a step smaller in the denser table view —
  and in `cards`, where the picture is the card's whole top edge rather than a
  tile beside the text, the same pref sizes the CARD (the grid's minimum column)
  instead;
  it replaced a `show_image` BOOLEAN, folded in by `init_db`, because a size
  and a visibility flag ask the same question twice and can disagree — "off"
  is just the small end. The px values live in `collection.html` as an inline
  style, not Tailwind size classes: the size is stored DATA, and a class per
  option would make the compiled stylesheet carry every one)) saved
  to a webapp-only `display` JSON column via `POST /collections/{name}/display` →
  `server.set_collection_display` — a NON-tool, website-only path like
  `set_archived`, invisible to the model and to tool returns. Those prefs are
  PER-COLLECTION and persist in the DB, so a collection stays arranged the way the
  user left it, on every device — no ARRANGEMENT lives in the browser (folding,
  below, is the one thing that does, and deliberately). Arrangement is
  resolved in `data.collection_page`, which always hands the template `groups`
  (one unlabeled bucket when ungrouped), each bucket pre-sorted, so every view
  just loops; a bucket for items MISSING the grouped value sorts last, and a
  `select` field groups in its own declared `options` order. Two things about
  grouping are decided THERE rather than per view, and they're joined on purpose.
  A bucketing where EVERY bucket holds one item collapses back to ungrouped: six
  trip ideas grouped by region gave six bands, each ~90px of heading introducing a
  single row, so the page became mostly furniture. And when the bands DO survive,
  the grouped field stops rendering per item — the band already says "California",
  so a `REGION: CALIFORNIA` badge under it, or a Status column repeating its
  heading down the whole table, is the same word twice. The field is dropped ONLY
  when a band is there to carry it, which is why one rule can't move without the
  other: apply the hiding to a degenerate grouping and the value vanishes entirely.
  A labelled group's
  band is a TOGGLE — the stack folds away — in all three views, since the point
  of naming buckets is being able to put the ones you're not reading away.
  Which labels are folded is the ONE piece of collection view state kept in
  `localStorage` rather than the `display` JSON, and the split is by tempo, not
  by accident: the stored prefs say how the collection is ARRANGED (worth
  syncing to every device), while a fold is where you are in a scan right now,
  flipped several times a minute — a POST per chevron is the wrong tempo. Keyed
  by label, so a fold survives a re-sort. The table view pays for it in markup:
  collapsing means hiding a run of `<tr>`s, so each group there is its own pair
  of `<tbody>`s (band, then rows) — valid HTML, columns still aligned. A wide
  table scrolls INSIDE itself so the page never scrolls sideways, which is right
  and was also silent: on a phone the trailing columns simply weren't there, with
  nothing to say a swipe would reach them (measured at 430px, a five-column table
  hid 37% of its width). The `.hscroll`/`.hscroll-cue` pair in `base.html` fades
  the right edge while content remains past it — so it doubles as the "that's the
  end" signal — and any page can opt in by wrapping a scroller and dropping the
  span in.
  The fourth view, `map`, is the only one a collection can be INELIGIBLE for:
  it needs a `location` field to have anything to plot, so the popover offers it
  only when `data.can_map` (and `set_collection_display` refuses it otherwise —
  the same one-rule-two-users pairing as `groupable_fields`/`sortable_fields`,
  with a third `and c.can_map` in the template so a collection whose location
  field is later dropped falls back to the list rather than rendering an empty
  world). It's the app's ONE network dependency: **Leaflet** is vendored into
  `static/vendor/` like `marked` and uPlot, but the TILES come over the wire —
  free, keyless, and the one thing on any page that won't draw offline (the
  rest of the page still does). Loaded only on the map view, since it's 145KB
  no other view has a use for. Pins are `L.circleMarker`s, not Leaflet's
  default teardrop: the default is a PNG pair that would have to be vendored
  and recolored, while an SVG circle is styled like everything else.

  The basemap is **CARTO Positron** (OSM data, CARTO's style) rather than OSM's
  own standard tiles, and all three reasons came out of looking at the same
  view in both. It's already the page's palette — near-white land, gray line
  work — where the standard style is beige-and-blue and only goes gray under a
  filter that muddies it. Its labels are ENGLISH worldwide (CHINA, JAPAN,
  GERMANY) where the standard style prints each country's own name (中国, 日本,
  Deutschland), which is right for a world map and wrong for one person's list
  of places. And it draws country borders at all. Dark mode swaps to the same
  map's DARK build, not an `invert()` of the light one — inverting turns the
  water muddy brown and the labels grey-on-grey. A `grayscale(1)` takes the
  last blue out of the water; it must NOT be paired with a contrast boost,
  since the borders are LIGHT gray and more contrast pushes them to white,
  erasing the very thing the zoomed-out view is short of.

  Two layers, not one: the LABELS are a separate tile layer that switches on at
  zoom 5. Zoomed out, Positron's text is neither ours nor English — continents
  come through in mixed scripts (亚洲, AMÉRICA, "AMÉRICA DO SUL;AMÉRICA DEL
  SUR" as a single label) — and none of it is what this map is for. So the wide
  view is pure line drawing and the words arrive at the zoom where they start
  being country and street names. Drawing country names OURSELVES at the wide
  zooms was tried and removed: Natural Earth ships label points and its own
  per-country `MIN_LABEL`, so the names were English and progressively
  disclosed for free — but a point that doesn't know what else is on the map
  collides with the thing the map is FOR, and the labels landed on top of pins
  and half off the edge of the pane. Real label placement means measuring boxes
  and resolving overlaps against the pins on every pan, which is a lot of
  machinery for names the reader already knows.

  Country borders are OUR line drawing on top, not the basemap's, because the
  basemap's fade as you zoom OUT — exactly the view where an outline is the
  only thing saying what you're looking at. Natural Earth's 110m LAND
  boundaries (public domain), stripped of every property and rounded to 3
  decimals: 77KB, 20KB over the wire, vendored like everything else. Land
  borders only — coastlines are the basemap's job, and drawing our own over
  them would double every shoreline. Fetched (so it caches across collections)
  and added before the pins so markers sit on top; a failed fetch is silent on
  purpose, since the map is usable without the outlines and a missing
  decoration must not take the pins down with it.

  The borders have to answer the dateline normalization above. A raster layer
  wraps ITSELF, so the tiles never noticed; a vector layer is drawn once,
  exactly where you put it — so with
  the view centered past 180 for a Pacific-spanning collection, the Americas
  lost their outlines while Asia kept its. So the borders are built as three
  copies of the world (a lap west, home, a lap east — enough for any view a
  minZoom-2 map can show), on a CANVAS renderer, since 331 features times three
  is a thousand paths: a lot of SVG nodes for a decoration and nothing at all
  for a canvas. The theme is watched with a
  MutationObserver rather than read once at load: the nav's toggle flips
  `data-theme` live, and a map that read it at startup would sit white on a
  dark page until reload. GROUPING IS IGNORED here and that's structural, not a gap — a
  band is a horizontal rule with a stack under it, and a map has no stacks; the
  popover still shows the arrangement controls because they're what the other
  three views will use when you switch back. Pins are built from the FLAT item
  list, never the groups, since a multiselect grouping fans one item into
  several buckets and the same restaurant twice on a map is just a thicker dot.
  A `checklist` hint
  was dropped (it rendered exactly like `list`, and an item has no done-state to
  check), migrated to `list` in `init_db`; `cards` earns its place the way that
  one didn't — it's the one view where the IMAGE leads instead of accompanying
  (a grid of picture-on-top cards, auto-fill columns), so a collection that gets
  LOOKED at rather than read reads as a contact sheet. An item with no featured
  image still draws a placeholder tile wearing the collection's icon: skipping
  the box would sit that card short and ragged its row.
  `/collections` also carries a title-only search across every collection AND the
  inbox (`data.search_item_titles`, plain LIKE) — a "where did I file that"
  lookup, deliberately not the model's FTS `notes_search`. The three
  judgment rules (capture first/file second; structure proposed, never imposed;
  fields stay few) live in the journal server `instructions`.
- `settings` — generic JSON KV; holds `profile` (`goals`, `split`, `session`,
  `injuries`, `coaching`, free-form beyond those) merged via `update_profile` and surfaced by `get_fitness_briefing`,
  and `eating_profile`, whose one live key is `targets` — the flat {nutrient: number}
  water/protein goals, written only by `server.set_nutrient_targets` (/food's
  Targets popover), validated by `_bad_targets` (a real nutrient key, a positive
  number — the rings silently skip anything else, so an unvalidated write would
  report success while the ring kept the old number), and read by the rings
  (`data.nutrient_targets()`, merged over the display defaults) and the trainer's
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
- `subjects` + `facets` + `attempts` + `learn_fts` — the LEARNING log (the teacher
  server; logic in `learning/`, schema owned by `learning/db.py`, not `SCHEMA`). A
  *subject* is a thing being learned (a myth, a word, a case); a *facet* is one
  recallable aspect of it and the unit of FSRS scheduling, because facets fail
  independently. Facets grade in one of three modes (`recall`/`list`/`open`), are
  STAGED on capture and released a few per Pacific day (`TEACHER_NEW_PER_DAY`), and
  `scheduled=0` marks background context that is never quizzed. `attempts` is the
  audit trail — every prompt, the user's VERBATIM answer, the grade, and the FSRS
  card as it stood before (`prev_card`, what makes `undo_last` possible); `kind`
  separates graded reviews from being taught (`study`) and from conversational
  `encounter`s, and only reviews count toward retention. The same architectural
  split as everywhere: the server schedules and records; composing questions and
  judging answers is the model's, at review time — reference answers are deliberately
  withheld from `next_card`/`due` so they can't leak into question wording.
  A subject can also carry an `article` — model-written background reading
  (markdown, hotlinked images) for the webapp's subject page. It is a SEPARATE
  LAYER from the facets on purpose: the article is where detail and big picture
  live, the facets stay the few tested key points — and it follows the same
  answer-withholding rule as everything else (never returned by `next_card`/
  `due`/`at_risk`; `get_subject` returns it in full, everything else carries a
  `has_article` flag so returns stay compact). Written via
  `update_subject(article=…)` (wholesale replace, "" clears) or at `capture`;
  indexed into `learn_fts`; the contract (engaging wiki-style prose, real image
  URLs only — never guessed, Wikimedia preferred) lives in the teacher
  `instructions` ARTICLES block.
  The webapp reads it at `/learn` (nav menu, shortcut 7) — subjects grouped by type,
  and a per-subject wiki page showing the article (rendered via the shared
  `data-md`/marked pipeline, so images get the uniform tiles + lightbox), the
  facets under a Key-points band, schedule state, and recent
  attempts. Read-only like `/food`, outside the journal lock, no chat panel: the ONE
  write path is the teacher connector's tools. `scripts/import_teacher.py` folds a
  standalone teacher repo's DB in (idempotent, preserves ids + FSRS state).

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

The teacher server is the same story a third time: its own provider
(`_build_auth(TEACHER_PUBLIC_URL)`), its own host when `TEACHER_PUBLIC_URL` is set
(redirect URI `<teacher-host>/auth/callback`), authless `/teacher/mcp` graft otherwise
— `combined.py`'s `_secondary_routes` is the one place that pattern lives now.

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
