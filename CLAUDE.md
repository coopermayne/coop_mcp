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
the journal+drinking server (`mcp` at `/mcp`). Both live in `server.py` and share the
same SQLite DB; each has its OWN Google auth provider (providers are single-resource —
see the auth section). Each is its own connector → its own Claude project, so a
conversation loads only that half's tools (smaller tool surface = less latency, the
reason for the split). It's purely an MCP-layer division: the webapp still imports this
module's functions unchanged. `webapp/combined.py` composes both endpoints onto one
origin.

## The one architectural rule

**There is no LLM inside the server, and there must never be one.** The server is a
deterministic data + candidate-matching layer. The contextual judgment ("which Tom?")
is done by Claude in the conversation, using the candidates the server returns. When
adding features, keep that split: the server generates candidates / stores / retrieves;
the model decides. Don't add model calls, embeddings services, or NER inside the server.

The rule is about `server.py`, not the whole repo. The **webapp does contain an LLM** —
`webapp/chat.py` is the web app acting as an *MCP client*, driving the same
`@mcp.tool()` functions over the Anthropic API (in-process, no transport) so the
phone/browser gets the same conversational capture Claude Desktop does. That preserves
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
  e.g. it knows Hallie's parents are Jeff and Jody), and just omits/asks when it can't
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
  timestamp, not a user date). Both briefings return `now` (`current_clock()`) so the
  model can anchor "today"/"yesterday" before defaulting or back-dating. `tzdata` is a
  dependency so `zoneinfo` resolves on the slim Docker image.
- **Tool docstrings are the model-facing contract.** Claude reads them to decide when to
  ask vs. link vs. queue (e.g. the score thresholds). If you change a tool's behavior,
  update its docstring in the same edit — it's not just documentation.

## Files

- `server.py` — everything: schema, matching, both FastMCP instances (`mcp` =
  journal+drinking, `trainer_mcp` = training), all tools, shared auth wiring, the
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
  so changing a docstring updates the chat. The `exercise` agent is different: it's a
  WEBAPP-DEFINED agent (its `instructions` and its two tools — `check_library`,
  `create_exercise` — are hand-written here, NOT lifted from a server) so the
  exercise-creation path stays OFF the MCP tool surface. It backs the library page's
  "+ Add an exercise" panel and is the one place an LLM can grow the catalog — reachable
  only by the authenticated user, never by the journal/trainer connectors. Off unless
  `ANTHROPIC_API_KEY` is set; model via `CHAT_MODEL`.
- `webapp/templates/`, `webapp/static/` — Jinja templates and PWA assets (icons,
  `chat.js`, manifest); the app is an installable PWA.
- `webapp/requirements.txt` — the UI's extra deps (fastapi, uvicorn, jinja2, authlib,
  httpx, and `anthropic` for the chat); install alongside the root `requirements.txt`,
  which it imports `server.py` from.
- `scripts/` — `seed_dev.py` (load throwaway dev data) and `gen_icons.py` (regenerate
  the PWA icon set).
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
.venv/bin/python server.py                       # journal+drinking (default)
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
restore"). The web app auto-loads `.env` (see `.env.example`); shell-exported vars win.

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
  `get_briefing` surfaces a short preview; `get_person_history` returns the FULL
  summary for read-before-write, same as `contact`), `contact` — a free-form JSON blob holding
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
  distinct from `created_at`). FTS5 mirror `entries_fts`.
- `mentions` — one per reference in an entry; `surface_form`, `person_id` (NULL while
  pending), `status`, `context_snippet`.
- `groups` + `person_groups` — explicit circles (family, colleagues, …), many-to-many.
- `drinks` — exactly one row per day (`drink_date` is unique); `standard_drinks`
  (REAL), `kind`, `notes`. `log_drinks` upserts: the first log of a day creates the
  row, later logs accumulate onto it (`standard_drinks` add up, `kind` merges into a
  deduped list via `_merge_kinds`) — so it takes the increment, not the running total;
  `update_drink` sets absolutes (corrections/overwrites). Sober days aren't stored — a
  day is sober if it has no row. `get_drink_summary` aggregates (daily totals, sober
  streak) in SQL; the webapp `/drinking` page edits/deletes past days inline.
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
  `exercises(similar_to=…)` returns like-for-like swap peers (shared primary muscle + same
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
  lifts, weight/reps NULL for cardio). The two-level log mirroring entries/mentions. A
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
- `settings` — generic JSON KV; holds `profile` (injury, split, goals) merged via
  `update_profile` and surfaced by `get_fitness_briefing`.

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
