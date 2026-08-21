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
`tools/call`. Deleting an entry needs no special gate — it's its own tool
(`journal_delete_entry`), hidden like the rest, rather than a `kind` on a shared
delete. The app chat is untouched
because it bypasses middleware (`list_tools(run_middleware=False)` + direct function
calls). Same split for the model-facing prose: the `mcp` instance's `instructions`
are the CONNECTOR text (intake + collections; `intake_summary` carries `now` since
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
    policy (the three capture rules, Pacific dates, the rotation policy, the signed-weight
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
  The journal connector carries two unrelated domains at once — the eating log and
  the notes/collections layer — so every tool it advertises is prefixed `intake_*`,
  `notes_*` or `collections_*` (`intake_log`, `notes_search`, `collections_save`, …).
  Two reasons, both about the model rather than tidiness. First, **"item" was
  ambiguous**: an intake item (a beer) and a collection item (a recipe) are different
  things, and the old `find_past_items` (eating) sat in the tool list next to
  `search_items` (notes) with nothing to tell them apart. Second, clients render
  `tools/list` in name order, so the prefix makes the list group itself by domain.
  The MCP name is set with `@mcp.tool(name=…)` and the PYTHON function keeps its
  original name — the webapp calls these functions directly (`webapp/app.py`,
  `webapp/chat.py`), so renaming only the wire name keeps that surface untouched.
  Note the one asymmetry: `webapp/chat.py` dispatches by the MCP name (it lifts tools
  from `list_tools`), so its `_WRITE_TOOLS` set and `_tool_chip` branches key off the
  NEW names. The trainer server is a single domain on its own connector and needs no
  prefix. Adding a connector tool = giving it a domain prefix.
- **Destructive tools are narrow, not kind-scoped — on the journal side.** The journal
  server has four deletes (`journal_delete_entry`, `intake_delete`, `notes_delete`,
  `collections_delete`) rather than one `delete_record(kind=…)`. A `kind` string is a
  thing the model can get wrong on an irreversible call, and it forced the awkward
  case where ONE kind (`entry`) had to be blocked on the connector while the others
  stayed — which was a special case inside `HiddenToolsMiddleware.on_call_tool`. As
  separate tools, hiding the journal delete is just its name in
  `CONNECTOR_HIDDEN_TOOLS`, like every other journal tool. They all still call the
  shared `_delete_record` helper, so the table mapping and the set-renumbering live in
  one place. The TRAINER keeps its kind-scoped `delete_record` (`workout`/`set`/
  `weight`): one domain, one connector, nothing to disambiguate.
- **A write says where the thing now lives.** Capture happens in a Claude conversation;
  the data is READ in the web app — two different screens, which is the standing
  awkwardness of the whole setup. So the connector's write tools return a `url`
  (`_app_url`: `PUBLIC_URL` + the `/app` mount, per `webapp/combined.py`) — `intake_log`
  → `/food`, `notes_save`/`notes_file` → `/item/{id}`, `collections_save` → the
  collection page — and one tap replaces a context switch. `PUBLIC_URL` unset (stdio,
  dev) OMITS the key rather than emitting a dead link. Two deliberate limits: the
  policy line lives in `_APP_LINK_BLOCK`, composed into the CONNECTOR `instructions`
  ONLY — the in-app chat gets the same `url` back but already renders its own local
  chip, and pointing the user at the page they're standing on is noise — and the links
  sit on capture paths, not corrections (`intake_update` returns totals, no url), since
  the returns are tuned token-compact and a link per call is exactly the bloat that
  warning is about.

## Files

- `server.py` — everything: schema, matching, both FastMCP instances (`mcp` =
  journal+eating, `trainer_mcp` = training), all tools, shared auth wiring, the
  shared `_delete_record` helper (the trainer exposes it as a kind-scoped
  `delete_record`; the journal splits it into four narrow tools — see the naming
  convention below),
  and the stdio/http entrypoint (`MCP_SERVER` picks which server stdio runs).
- `webapp/combined.py` — single-process entrypoint (the Dockerfile's `CMD`): serves the
  journal MCP + browser UI (`/app`) on the main origin, and the trainer MCP either on
  its own host (`TRAINER_PUBLIC_URL` set → Starlette `Host` routing) or grafted at
  `/trainer/mcp` on the main origin (authless fallback).
- `webapp/app.py` — the FastAPI UI: routes + page rendering for the browser app (mostly
  read-only browse pages — including the `/trainer/library` exercise library — plus the
  handful of website-only write carve-outs (`/food/targets`, `/graphs/weight` and its
  `/{id}` edit + delete, `/graphs/goal`, `/trainer/profile`, a collection's `/display`)
  and the `/chat` panel mount).
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
  `chat.js`, manifest); the app is an installable PWA. `static/confetti.js` is the
  app's one celebratory flourish (`window.Confetti.burst(el)`, thrown at a lifting PR
  or an all-time-low weigh-in — see the `sets` and `body_weight` rows): hand-written
  rather than vendored, loaded on every page because it costs nothing until called, and
  driven by requestAnimationFrame on a canvas rather than a CSS animation for the same
  Low Power Mode reason as the rep-loop crossfade. `static/vendor/`
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
(show the logout control in the UI), `GEOCODE_USER_AGENT` (the User-Agent
`notes_geocode` sends to Nominatim; a generic default, override to identify your
deploy), and `BACKUP_TOKEN` (strong random token that unlocks
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
  Each literal is a PREFIX query (`"term"*`) — shared by `search_entries` and
  `notes_search`. The load-bearing reason is CJK: unicode61 tokenizes a run of
  Chinese as ONE token, so a recipe titled 宫保鸡丁 was reachable only by typing
  the whole name (`宫保` matched nothing), and the same character fixes the
  everyday English case (`doubanji` → doubanjiang). It is NOT substring matching —
  a word's TAIL still misses; that needs the trigram tokenizer and an FTS rebuild.
- `mentions` — one per reference in an entry; `surface_form`, `person_id` (NULL while
  pending), `status`, `context_snippet`.
- `groups` + `person_groups` — explicit circles (family, colleagues, …), many-to-many.
- `drinks` — LEGACY, dormant. Alcohol is an intake item now (see `intake_items`).
  The TABLE is kept as the fold-in migration's source and the one copy of the
  per-day `kind` ("beer, wine"), which the item rows have no column for; the
  CODE that read and wrote it (`log_drinks`/`get_drink_summary`/`update_drink`,
  and `_delete_record`'s `"drink"` kind) is DELETED. Dormant data costs nothing;
  dormant code is a trap — a live-looking reader of this table is what left the
  /graphs drinks series empty for months after the fold. Nothing reads it.
- `intake_items` — the INTAKE log: **one row per thing consumed**. A sandwich is a
  row, a beer is a row, a 12oz glass of water is a row — food, alcohol and water are
  the same kind of fact, so they share one table, one tool path, and one set of
  columns. There is no per-nutrient special case anywhere above this table.
  `intake_log` inserts ONE item (the model calls it once per thing; `position` is the
  server-assigned order within the day, the same append as entries' `day_position`);
  there is deliberately NO time-of-day on an item — a day is a day, and `position`
  already carries the sequence (a short-lived `at_time` column was tried and removed;
  a DB that ran that version keeps the orphan column, which nothing reads — every
  INSERT here names its columns, so it's inert either way);
  `intake_update` edits one by id; `intake_delete` removes one.
  **Day totals are DERIVED, never stored** (`SUM ... GROUP BY food_date`). That's the
  load-bearing decision: a stored total drifts from the items it claims to summarize,
  and correcting one item would mean re-deriving the day by hand — i.e. asking an LLM
  to do arithmetic. With sums, "that bowl was 600, not 1100" is one UPDATE and every
  total follows. The nutrient columns (`NUTRIENTS` — calories, protein/carbs/fat,
  `sodium_mg`, `fiber_g`, `standard_drinks`, `water_oz`) are per-item and OPTIONAL,
  staying NULL until filled in, so a day described only in words is "unestimated",
  never a zero-calorie day; a nutrient no item carries is ABSENT from the day's
  totals rather than 0. Range-checked by the shared `_bad_nutrients` (both
  `intake_log` and `intake_update`, so the two can't drift): no negatives, and a
  generous per-item ceiling (`NUTRIENT_MAX`) purely as a typo guard — since day
  totals are DERIVED, one absurd row silently skews that day and every average
  built on it, nowhere near the item that caused it. `intake_summary`'s averages are per nutrient over the days
  that carry it, so each has its OWN denominator (returned with `logged_days`). The
  `NUTRIENTS` tuple drives every sum/average/render site, so adding a nutrient is one
  tuple entry + an `ALTER TABLE` in `init_db` + a unit label in `macros.eating_block`.
  Same split as everywhere: turning "a chipotle bowl" into calories is the MODEL's
  estimate, made in conversation; there is no food database in the server — but the
  intake log IS its own food database for repeats: `intake_find_past(query)` fuzzily
  searches everything ever logged (token-level scoring — exact/substring/Jaro-
  Winkler/phonetic per query word — grouped by identical item text, latest numbers
  win, ranked by match quality with a small ~30-day-half-life recency bonus so this
  week's leftovers outrank last month's near-twin). Both similarity rules are
  LENGTH-GUARDED, because the defaults scored junk above the 0.74 floor and this
  tool's whole job is telling a genuine repeat from a near-twin: the substring
  rule needs 3+ characters (ungated it paid 0.92 for a single letter, so the query
  "a" scored a PERFECT 1.0 against "half a medium eggplant"), and Jaro-Winkler is
  damped when the two tokens' lengths are far apart (its prefix bonus scored
  "chipotel"≈"pot" at 0.792, which put hot pot vegetables top of a search for a
  chipotle bowl). The model REUSES those settled
  numbers when it judges a hit is genuinely the same thing, estimates fresh when it
  isn't — deliberately a read tool + model judgment, NOT a curated catalog: a
  `foods` entity table was tried and rejected because the diet varies within brands
  (which Chobani?) and a wrong-but-confident auto-resolve is worse than a
  re-estimate; the log needs no gardening and is always as current as the last
  meal. And day-so-far questions are answered from the DB, never from a chat-side
  running tally (`intake_log` returns `day_totals` on every write; other clients —
  the phone app, another conversation — may be writing the same day); both
  contracts live in the server `instructions` + the intake docstrings.
  Both write tools pair those totals with `targets` (`_day_targets`) — the stored
  `eating_profile.targets`, hoisted out of the profile that only `intake_summary`
  returned. The reason is that the CAPTURE surface and the VIEWING surface are
  different screens: a sum with nothing to read it against ("sodium 2100") is a
  number the model can report but not judge, so it either says nothing useful or
  spends a second `intake_summary` call on a goal already in the DB. Malformed
  entries are skipped exactly as `data.nutrient_targets()` skips them (`_bad_targets`
  guards the write; a hand-edited blob must not fail a log call). Just the numbers —
  there is no ceiling/floor direction to return, here or in the webapp (see the
  `intake_items` row).
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
  (glancing at macros shouldn't need the knock) and has no chat panel. Its CONTENT is
  STRICTLY READ-ONLY: intake has exactly ONE write path, the MCP tools (the in-app
  chat panel counts — it calls the same functions). There is no form, no tappable
  ring, no /intake write route; the browser only renders what was logged. The one
  thing it writes is the TARGETS popover (below) — goals, not intake, the same
  carve-out a collection page's Display popover makes. Water and alcohol rings still
  always
  render, dashed when unlogged, because "no water yet" is worth seeing on a day you
  mean to hit a gallon. Each nutrient renders as a ring with BOTH
  its summed figure (unit included: "1400mg") and a short label ("sod") inside it
  (`macros.nutrient_ring`), read against `data.nutrient_targets()` — the stored
  eating profile's `targets` numbers merged over the `data.NUTRIENT_TARGETS`
  defaults, resolved per render. The NUMBERS live in the DB (settings
  `eating_profile`) so the rings and the model coach against the same goals, and
  they have TWO DOORS onto that one row: `intake_set_profile` (the model, in chat)
  and `server.set_nutrient_targets` (the user, via `POST /food/targets` ← the page's
  **Targets** popover). Two doors, one copy — the second exists because the screen
  where you NOTICE a target is wrong is the one drawing the rings, and the only fix
  used to be opening a chat to say it in a sentence. The website door differs in two
  ways, both because it edits numbers rather than prose: it merges per NUTRIENT
  (`intake_set_profile` replaces each top-level key wholesale, which for a form would
  mean every unfilled box quietly clearing a goal), and `None` DROPS an override back
  to the default — so a blank input hands a goal back instead of zeroing it, and the
  popover shows a set number in the input with the default only as a placeholder.
  **A target is JUST A TARGET — there is no ceiling/floor direction anywhere**, and
  the deletion is the design. Direction used to be declared per nutrient
  (`data.NUTRIENT_CEILINGS`) and drove a second ring color, which meant every
  consumer had to carry the flag — the rings, `/api/today.json`, the SwiftBar
  plugin's red/green, the popover's cap/goal labels — while the MODEL, which sees
  these same numbers in `intake_log`'s `targets`, had no way to get it. So the app
  could turn a ring clay at 90g of fat while the chat read it as 120% of a goal and
  encouraged more. Rather than plumb direction to a fifth consumer, it's gone: one
  fill color, every nutrient, arc capped at a full circle. Where a target really is
  a cap, or is informational only, that goes in the eating profile's `targets_note`
  — edited in this page's own Targets popover, alongside the numbers it describes —
  which can phrase the nuance a boolean was flattening, and which both the rings'
  owner and the model already read. In WORDS, never repeating the figure: the note
  writes `{calories}` and the live number is substituted on read (see `settings`). An
  untargeted nutrient (fat, until you give it one) still draws a DASHED track with
  no arc: an empty solid ring would read as "0% of goal" rather than "no goal set".
  A tapped
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
  curated **ROTATION** (kept small enough that progress on each lift is easy to track —
  HOW small is the user's business, and the model is told to have no opinion about the
  count: a hard number in the prose is a number that goes stale the first time they
  re-curate, and a trainer that opens by offering to prune nine lifts is spending the
  session on the one thing it doesn't decide), the only pool the trainer programs from. The rotation is drawn from the hearted
  superset — every few months the user swaps some of the rotation out for other hearted
  lifts. `set_rotation`/`set_hearted` curate the two pools (mirrored by the library page's
  ★/♥ toggles); the invariant `in_rotation ⇒ hearted` is enforced everywhere either flag is
  written — adding to the rotation hearts it, un-hearting drops it from the rotation, archiving
  clears BOTH, and the website's **+ Add an exercise** panel lands new movements in the
  *hearted superset* (not the rotation, which stays deliberate and hand-curated). **Logging
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
  weight for that day; a day with no row simply wasn't weighed. `log_bodyweight` adds
  one (returning `change_lbs` vs the prior weigh-in, plus `new_low` when the reading is
  the lowest EVER — omitted otherwise, and never on the first reading, since there was
  nothing to beat) and `get_fitness_briefing` surfaces
  the latest reading + 30-day change; the longer trend lives in the webapp, not a
  dedicated server tool. There is NO weight-goal/target logic in the server — the
  coaching is the model's, as everywhere else.
  **A weigh-in is a MORNING reading, and the UI says so: it's entered on `/graphs`**
  (`POST /graphs/weight`, a website-only path like the goal form beside it), at the top
  of the page, above the line it moves. It used to live on the `/trainer` plan card —
  a box under the sets, submitted only when you tapped Finish — which tied a DAILY
  measurement to whether you happened to train that day, and put the reading behind a
  redirect. That box, its Finish prompt, the post-last-set scroll-to-weigh-in,
  `POST /trainer/{id}/bodyweight`, `_with_bodyweight` and `data.bodyweight_on` are all
  DELETED rather than left dark; the plan card is about sets. `DEFAULT_COACHING` had to
  move with them — it used to tell the trainer to nudge a weigh-in after each session,
  which is the same tie stated in prose.
  The graphs row states what's in and offers what's missing: a logged day reads as text
  with a ✎ to correct it, an unweighed one opens with the field ready. It's deliberately
  OUTSIDE the Bodyweight panel, which hides when that chip is off or nothing is plotted —
  the first weigh-in has to be enterable on an empty page. A successful log does NOT
  reload: it upserts today's point into the bootstrap `DATA.weight` and re-renders, so
  the line extends under you and a burst isn't cut off by a navigation. Logging twice in
  a day APPENDS (latest wins), so a correction is just logging again.
  `new_low` is the one place this table earns a key on a WRITE return, and it's there
  because the model can't derive it: `change_lbs` is a day-over-day delta and nothing
  else on this path ever sees the all-time minimum. It's also the graphs row's confetti
  cue.
  **A weigh-in can be CORRECTED or DELETED from the page** — the folded history list
  under the row (`data.bodyweight_log`, `POST /graphs/weight/{id}` →
  `server.set_bodyweight`, `POST /graphs/weight/{id}/delete` → the shared
  `_delete_record` on its existing `"weight"` kind, so there's one delete
  implementation, not a website copy). That list is deliberately EVERY ROW, not
  `graph_data`'s one-point-per-day: the failure mode this log actually has is a dropped
  leading digit (a 2 typed as a 1) that gets re-logged correctly seconds later, so
  the day view is already right — latest reading wins — while the bad row sits in the
  table owning `MIN(weight_lbs)` and silently disabling every "lowest ever" the app can
  spot. A collapsed day view can't show you a row you need to delete. The `160-230`
  yellow typo guard on the input is the other half of that lesson; it warns and never
  blocks, since a real reading can be anything. Correcting is website-only for the same
  reason it's a list: you pick a row id while LOOKING at the rows. Told "Tuesday was 204,
  not 104", the model just re-logs — which is why re-logging alone isn't enough. The per-session "Weight:" line on `/workouts` stays — it's a same-day join
  (`data.workouts_full`), so it now reads as what you weighed that morning.
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
  The write returns carry two FRAMES, for the same capture-here/read-there split
  the intake `targets` answer. `notes_save`/`notes_file` report `unfilled_fields`
  (`_unfilled_fields`) — declared fields the item has no value for, the exact mirror
  of `stranded` (values with no field) and reported for the same reason: the return
  said only where the item landed, so nothing ever mentioned that a collection wanted
  a cook time. Advisory, NEVER an error — fields are optional and capture must not
  block on them. And a save that lands in the INBOX reports `inbox_count`: capture-
  first-file-second makes the inbox the default, so it grows invisibly and filing
  happens only if the user thinks to look. It is the inbox's `day_totals`.
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
- `settings` — generic JSON KV; holds `profile` (injury, split, goals, and
  `coaching`) merged via `update_profile` and surfaced by `get_fitness_briefing`,
  and `eating_profile`
  (its journal-side twin: durable eating facts — goals, stats, coaching context —
  plus the one structured key `targets`, a flat {nutrient: number} dict of daily
  goals) merged via `intake_set_profile` and surfaced by `intake_summary`, so a new
  conversation needs no pasted preamble. The webapp's rings read `targets` too
  (`data.nutrient_targets()` merges it over the display defaults) — one source of
  truth for goals, and one number per nutrient with no direction attached. That's also why
  `targets` has a SECOND writer, `server.set_nutrient_targets` (a NON-tool,
  website-only path behind `/food`'s Targets popover — see `intake_items` above):
  the goals are the one part of the profile the user reads on a screen and wants to
  change there, and it writes this same key rather than keeping a display copy.
  It merges per nutrient (null drops back to the default) instead of replacing the
  key wholesale, since a form submits every box at once.
  **A number lives in `targets` and NOWHERE ELSE — the profile's PROSE must never
  spell one out.** It was learned the expensive way: with direction deliberately
  moved out of the schema and into prose ("a target is just a target"), nothing
  stopped the prose from restating the NUMBER as well — so the blob ended up
  carrying `targets.protein_g` 140, a `targets_note` saying "150 is a floor, 120 the
  hard minimum", and a fake-structured `protein_floor_g` of 120: three answers to one
  question, only the first of which the popover could change. Prose refers to a
  target by PLACEHOLDER instead — `{calories}` — and `_render_targets_prose`
  substitutes the live number into every string value on the way OUT
  (`intake_summary`'s `profile`); the stored blob keeps the placeholder, so the
  sentence follows the target instead of quoting it. A placeholder for an unset
  nutrient renders `(no target set)` rather than a number, because the server can't
  see the webapp's DISPLAY defaults (`data.NUTRIENT_TARGETS`) and inventing one here
  would be the second copy all over again. Two guards, both deterministic and shared
  by both doors: `_bad_targets_note` REJECTS an unknown placeholder (closest-name
  suggestion, the `_bad_icon` habit), and `_note_literals` returns `stale_prose`
  ADVISORY (never blocking, like `unfilled_fields`) when the note spells out a number
  a target already carries. So `targets_note` is the one PROSE key with two doors
  too — the popover edits it in the same round trip as the numbers, on purpose: the
  note and the numbers drifted apart precisely because changing one never showed you
  the other. There are no floors or ceilings anywhere, only targets. The rest of the
  blob is free-form, but `targets` is VALIDATED (`_bad_targets`, shared by both writers:
  a real nutrient key,
  a positive number, closest-name suggestion on a typo) precisely BECAUSE the
  rings read it: `nutrient_targets()` skips any override that isn't well-formed,
  so an unvalidated write reported success while the ring you were aiming at
  silently kept the old number, with nothing anywhere to say why.
  **`profile.coaching` is the trainer's user-editable prompt**, and it's the
  eating side's targets story told about prose. It holds the user's own standing
  instructions about HOW to coach — session size, tone, what to nudge — and it has
  the same two doors onto one copy: `update_profile` (the model, in chat) and
  `server.set_trainer_profile` (the user, via `POST /trainer/profile` ← the
  /trainer page's **Coaching** popover, a NON-tool website-only path like
  `set_nutrient_targets`). Defaults live in code (`DEFAULT_COACHING`) and are
  resolved on read by `_resolved_profile`, exactly as `nutrient_targets()` merges
  over `data.NUTRIENT_TARGETS`; a blank save DROPS the key, so the default can be
  handed BACK rather than only overwritten. Two of its lines (session sizing, and what
  to say about weigh-ins) used to sit in `trainer_mcp`'s `instructions` — the wrong home,
  for a reason worth keeping straight when the next knob comes along.
  `instructions` is CONTRACT: the rotation policy, the closed catalog, the signed-
  weight convention — rules that pair with tool code, belong in git, and must not
  be editable from a textarea. These were PREFERENCE, the part the user wants to
  tune between sessions. And the delivery differs: `instructions` reaches a
  connector only at its initialize handshake, so an edit there needs a redeploy,
  while the profile rides in on every `get_fitness_briefing` — the same text
  landing on the connector and the in-app chat with nothing to restart. So the
  instructions now POINT at the key (read it as instruction, not background; it
  can't loosen the rules; it's the user's to edit) instead of restating what it
  says.

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
