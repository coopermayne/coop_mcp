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

The same split governs the **trainer** and **drinking** features: the server stores
workouts/drinks and computes deterministic aggregates (muscle recency, drink streaks),
but deciding the next weight, what to rest, which exercises to program, and how to coach
form is the *model's* job, done in conversation from the data the retrieval tools return.
There is no exercise-selection or progression logic in the server either.

## Other load-bearing design decisions

- **People, not names.** A reference resolves to a person *entity* (`people`), not a
  string. One person has many surface forms (`aliases`); one string can mean several
  people. Never normalize names in text — resolve mentions to entities.
- **Capture never blocks.** `add_journal_entry` always saves, even if every mention is
  ambiguous. Unresolved mentions sit in the queue (`status='pending'`) for later.
- **Two-layer entry storage.** `entries.body` is Claude's cleaned, concise version (the
  journal proper, what search/history show). `entries.raw_body` is the user's verbatim
  words, hidden, returned only via `get_entry`. Mentions are matched against the *raw*
  surface form, not the cleaned text.
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
- `webapp/combined.py` — single-process entrypoint (the Dockerfile's `CMD`): mounts the
  read-only UI at `/app` and composes BOTH MCP servers onto one origin (journal at
  `/mcp`, trainer grafted in at `/trainer/mcp`, sharing the root OAuth routes).
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
slash, no `/mcp`), `JOURNAL_ALLOWED_EMAILS` (comma-separated; normally just yours).
The journal's OAuth callback is `<PUBLIC_URL>/auth/callback` (unchanged). See the auth
section for the trainer endpoint's same-origin OAuth caveat.

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

Smoke-test HTTP boot + handshake: start `webapp/combined.py` with `MCP_TRANSPORT=http`,
then POST an `initialize` JSON-RPC call to BOTH `/mcp` and `/trainer/mcp` (Accept:
`application/json, text/event-stream`) and expect `200`; with auth on, expect `401`
plus a `WWW-Authenticate` header whose `resource_metadata` pointer resolves to `200`
(the trainer's must resolve at the root — that's the thing the route-grafting in
`combined.py` exists to get right). `/health` should be `200` regardless of auth.

If a feature changes the schema, add a migration in `init_db()` (it runs `CREATE TABLE
IF NOT EXISTS` then `ALTER TABLE ADD COLUMN` for new columns) — existing DBs must keep
working.

## Data model (tables)

- `people` — entities. `canonical_name`, `role` (the human disambiguator), `notes`,
  `summary` (rolling profile for context), `email`/`phone`/`address` (vCard-aligned).
- `aliases` — surface forms per person; `phonetic_key` (metaphone), `source`
  (`manual`|`learned`).
- `entries` — `body` (clean), `raw_body` (verbatim), `entry_date` (day it's *about*,
  distinct from `created_at`). FTS5 mirror `entries_fts`.
- `mentions` — one per reference in an entry; `surface_form`, `person_id` (NULL while
  pending), `status`, `context_snippet`.
- `groups` + `person_groups` — explicit circles (family, colleagues, …), many-to-many.
- `drinks` — one row per drinking occasion; `standard_drinks` (REAL), `kind`, `notes`.
  Sober days aren't stored — a day is sober if it has no row. `get_drink_summary`
  aggregates (daily totals, sober streak) in SQL.
- `exercises` — the exercise catalog (stable entities, like people): `technique_notes`,
  `common_mistakes`, `cautions`, `video_link`. Starts empty; `log_workout` auto-stubs a
  bare record for any unknown name, which `save_exercise` later enriches.
- `exercise_muscles` — normalizes muscle→exercise (`role` primary|secondary) so
  per-muscle recency/volume is a plain GROUP BY. Canonical muscle list is `MUSCLES`.
- `workouts` + `sets` — session + per-set `weight_lbs`/`reps`/`rpe` (1-10 RPE), the
  two-level log mirroring entries/mentions.
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

The journal's OAuth authorization-server routes (`/authorize`, `/token`, `/register`,
`/auth/callback`, the AS metadata) serve once at the root; `combined.py` grafts in only
the trainer's two unique routes (its `/trainer/mcp` endpoint + its protected-resource
metadata), so the trainer authenticates against the root AS but validates against its
own resource. ⚠️ This same-origin two-resource setup relies on the root AS issuing a
token whose audience matches the trainer resource; FastMCP can't cleanly co-host two
full OAuth servers on one origin (their `/authorize` etc. paths collide). If the trainer
connector won't authenticate, the robust fix is to give it its own **subdomain** (its
own origin → its own clean root OAuth). The journal endpoint does not depend on any of
this — its provider/resource are isolated.

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

Typed person-to-person relationship graph; multi-valued contacts (one email/phone/address
each — promote to a `contact_methods` table if needed); vCard import/export; Google
Contacts sync; automated `summary` regeneration. See README "Notes / next steps".
