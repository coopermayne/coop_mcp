# Journal + Trainer

A conversational journal with entity resolution, plus a personal-trainer MCP server.
You write the journal by talking to the web app's own chat; Claude captures entries
and resolves who you mean. Training (and a small daily water/protein log) happens in
Claude through the trainer connector. The server is a deterministic data + matching
layer — no LLM inside it. The judgment ("which Tom?") happens in the conversation.

## What it does

- **Capture never blocks.** Every entry is saved immediately, even if every person
  in it is ambiguous.
- **Clean journal, raw fallback.** Claude writes each entry as structured, concise
  prose (`body`) — that's what you read and search. Your verbatim words are kept
  underneath (`raw_body`), hidden from normal views but retrievable via `get_entry`
  if the cleaned version ever dropped something.
- **People, not names.** Each reference resolves to a stable person *entity*. "Dad",
  "Tom", "Thom" can all point to person #1; the other Tom is person #2. Retrieval is
  an indexed lookup on the entity, so "everything about Tom my father" never drags in
  the other Tom.
- **It gets quieter over time.** Confirm that a garbled transcription meant a given
  person and that surface form is stored as a learned alias — it auto-matches next time.
- **Contacts live on the person.** Each person carries a free-form `contact` blob —
  emails, phones, addresses, websites, anything — multi-valued, edited via
  `update_contact`. No separate contacts app.
- **Circles + emergent network.** Assign people to groups (family, colleagues,
  Robin's friends). Separately, `get_related_people` derives who gets talked about
  together straight from the journal — no tagging needed.
- **Session context in one call.** `get_briefing` hands Claude the last two weeks of
  entries plus a profile of everyone mentioned in the last week, so it writes new
  entries knowing what's already going on. Everyone else arrives as a compact roster
  (name/role, no summary) — enough to recognize a name, without paying for every
  profile on every session. Widen either window for a longer catch-up.
- **"Tell you later" works.** Unresolved mentions sit in a pending queue until you
  feel like resolving them.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r webapp/requirements.txt
```

The database is a single SQLite file at `~/journal.db` (override with `JOURNAL_DB`).
It holds named real people in your life — keep it somewhere encrypted, or swap in
SQLCipher later if you want at-rest encryption.

## Register the trainer with Claude Desktop

Edit `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "trainer": {
      "command": "/ABSOLUTE/PATH/journal-mcp/.venv/bin/python",
      "args": ["/ABSOLUTE/PATH/journal-mcp/server.py"],
      "env": { "JOURNAL_DB": "/ABSOLUTE/PATH/journal.db" }
    }
  }
}
```

Use absolute paths. Over stdio a bare `server.py` launch serves the trainer — the only
MCP server. (The journal has no connector: it's written in the web app's chat.)
Restart Claude Desktop; the tools appear in the tools menu.

## Personal trainer (+ water & protein)

The same codebase also acts as a personal trainer — same rule as the journal: **no LLM
in the server.** It stores workouts (and a small daily water/protein log) and computes
deterministic aggregates (per-muscle recency, day totals); the coaching judgment
— next weight, what to rest, which exercises, how to explain form — happens in the
conversation, from what the retrieval tools return.

**The trainer is the one MCP server.** Connect it as a connector in its own Claude
**Project**. In production it runs on its **own subdomain** — set
`TRAINER_PUBLIC_URL=https://TRAINER-DOMAIN` and the single process routes that hostname
to the trainer server at its root (connector: `https://TRAINER-DOMAIN/mcp`, clean root
OAuth). Over stdio a bare `server.py` launch runs it. With `TRAINER_PUBLIC_URL` unset (local/authless), the trainer falls back to
`/trainer/mcp` on the main origin. See "Remote deployment" for the subdomain steps.

- **Water and protein — the one intake log kept.** `log_intake` logs ONE thing consumed
  with `water_oz` and/or `protein_g` (plus an optional label like "protein shake"); it's
  one row per item, and **day totals are derived, never stored** — a SUM over the day's
  items — so "that shake was 30g, not 50" is `update_intake(item_id, protein_g=30)` with
  no recomputing, and removing one is `delete_record(kind="intake")`. `get_intake` reads
  days back with ids, totals and averages; today's totals also ride along in
  `get_fitness_briefing` as `intake_today`. Daily targets are set on the web app's
  `/food` page (**Targets** popover; blank hands a goal back to its default) or by
  asking the trainer (`set_intake_targets`). The page itself is read-only: two rings plus the day's items.
  (This used to be a full food tracker — calories, macros, sodium, fiber, alcohol. Those
  columns and the legacy `drinks`/`nutrition` tables are kept, dormant, with their
  history; nothing reads them.)

- **Your exercises, no library.** The trainer knows only the movements you do (active)
  and have done (archived, with a note on why you stopped). Nothing is pre-loaded and no
  technique data is stored; form tips come from Claude in conversation. A new movement is
  created on the fly the first time it's planned or logged (Claude supplies its muscles),
  or ahead of time with `add_exercise`. "I'm done with X" archives it; logging an archived
  lift brings it back. `log_workout` records a session in one call or set-by-set (reuse
  the returned `workout_id`). Fix a logged session with `get_exercise_history` (to get
  `set_id`s) + `update_set` or `delete_record(kind="set")`, `update_workout` to
  move/relabel it, or `delete_record(kind="workout")` for the whole session.
- **Progressive overload.** Each set stores `weight_lbs`/`reps`/`rpe` (1–10).
  `get_exercise_history` replays a lift session-by-session so the trainer can judge
  the next weight: all sets clean at RPE ≤ 8 → add weight; grinding at RPE 10 short of
  target → hold or deload.
- **What to work, what to rest.** `get_fitness_briefing` returns the profile (injury,
  split, goals), per-muscle recency (days since trained + last-7-day set volume), and
  recent sessions *with their notes* — enough to program the day and respect recovery,
  and to carry a "left shoulder twinge" logged last time into this session's plan.
- **Plan a day or a week.** `start_workout_plan` lays out a session as pending sets with
  targets, which you then tap done on the web app. Pass a `planned_date` and call it once
  per day to lay out a whole week (or just the rest of one) — several plans sit waiting at
  a time, listed as *Upcoming* above your history on the Training page, each tapping
  through to its own logging card. A `planned_date` is only intent: a session is recorded
  under the day it's actually finished, so one done a day late lands on the day you did
  it. `update_workout(planned_date=…)` moves a plan (`""` unschedules it), and
  `get_fitness_briefing`'s `upcoming` is how the trainer avoids programming Tuesday's
  chest work again on Thursday — per-muscle recency counts *completed* work only.
- **Session notes as durable context.** A session's `notes` (set on `log_workout`, or
  added later with `update_workout`'s `append_note`, which adds a line without
  clobbering earlier ones) resurface in `get_fitness_briefing`, so observations made
  mid-workout become cautions next time.
- **Bodyweight is IMPORTED, not typed.** A connected scale records every morning to its
  vendor's app; every so often you export that app's spreadsheet and upload it with
  **Import scale export** on `/weight`. That is the only door — no form, no MCP write
  tool, no per-row edit — because a reading is a measurement, and a second way to state
  one is a second version of the truth. To fix a bad reading, fix it in the scale's app
  and re-export. The import is idempotent (each reading is keyed by its timestamp in the
  export), so re-uploading an overlapping file inserts only what's new and says so;
  overlapping exports are the expected way to use it. Storage is date-keyed (its own
  daily metric, not a column on a workout), so rest-day weigh-ins are just readings.
  The latest reading + 30-day change ride along in `get_fitness_briefing`, and `/graphs`
  plots the trend against the goal.

**Where the trainer's instructions live — two places, nothing else.** The RULES (how the
tools work, what to call when) are the server's own instructions, in code. Everything
about YOU (goals, split, session size, injuries, how you like to be coached) is your
profile in the database, which Claude reads at the start of every training conversation
and updates when you tell it something. If the profile is empty, Claude asks before it
plans anything. So leave the Claude project's custom instructions blank; anything you'd
put there belongs in your profile, where you change it by just saying so.

## Remote deployment — phone access via Coolify

One container serves the **web app** (journal, water & protein, training history,
graphs) at `https://YOUR-DOMAIN/app` and the **trainer MCP server** for Claude. The
trainer connector works on your phone once added at claude.ai (you can't add a new
connector from the mobile app). On Coolify:

1. New resource → from this repo (Dockerfile build pack). Coolify builds the image.
2. Add a **persistent volume** mounted at `/data` so `journal.db` survives redeploys.
   The trainer's OAuth state lives there too — see "Staying logged in" below.
3. Give it a domain (`YOUR-DOMAIN`) for the web app; Coolify provisions HTTPS.
4. **Add a second domain** on the SAME Coolify application for the trainer — e.g.
   `https://TRAINER-DOMAIN` — and set `TRAINER_PUBLIC_URL=https://TRAINER-DOMAIN`. The
   one process then routes that host to the trainer at its root, with its own OAuth.
5. At claude.ai → Customize → Connectors → Add custom connector →
   `https://TRAINER-DOMAIN/mcp`. Put it in its own Project; enable it per conversation
   via the "+" menu on your phone.

Without `TRAINER_PUBLIC_URL` (local/authless) the trainer is at
`https://YOUR-DOMAIN/trainer/mcp` instead — dummy data only; the endpoint is public.

### Auth (required before real data)

Single-user, so just "only your Google account gets in." The trainer uses FastMCP's
`GoogleProvider`, a full OAuth 2.1 authorization server (PKCE + Dynamic Client
Registration) that proxies Google; Claude discovers it and self-registers, so you just
paste the URL. The web app has its own Google login.

**1. Create a Google OAuth client** (Google Cloud Console → APIs & Services →
Credentials → Create OAuth client ID → Web application) with these redirect URIs:

```
https://TRAINER-DOMAIN/auth/callback     # the trainer connector
https://YOUR-DOMAIN/app/auth/callback    # the web app's login
```

**2. Set these env vars in Coolify** (never in the image):

| Var | Value |
|---|---|
| `GOOGLE_CLIENT_ID` | from the Google client |
| `GOOGLE_CLIENT_SECRET` | from the Google client |
| `PUBLIC_URL` | `https://YOUR-DOMAIN` (no trailing slash) — the web app's origin |
| `JOURNAL_ALLOWED_EMAILS` | your Gmail address (comma-separated for more than one) |
| `TRAINER_PUBLIC_URL` | `https://TRAINER-DOMAIN` (no trailing slash, no `/mcp`) |

With those set, the trainer flips from authless to protected on restart: an
unauthenticated request gets `401` with a `WWW-Authenticate` header pointing to
`/.well-known/oauth-protected-resource/mcp`, and after Google login the allowlist
middleware rejects any account not in `JOURNAL_ALLOWED_EMAILS`.

> If the allowlist ever rejects you after a correct login, confirm Google is returning
> the `email` claim; the check is in `AllowlistMiddleware`.

**Staying logged in across redeploys.** `GoogleProvider` stores Claude's
dynamically-registered client and your refresh tokens in an encrypted file store under
FastMCP's home dir, which by default is *inside the container* and wiped on every push.
The Dockerfile sets `FASTMCP_HOME=/data/fastmcp` to keep it on the persistent volume.
The JWT signing key is derived from `GOOGLE_CLIENT_SECRET`, so it's stable as long as
you don't rotate that secret.

If the connector shows "disconnected" after adding Google: usually the redirect URI in
Google doesn't exactly match `https://TRAINER-DOMAIN/auth/callback`, or
`TRAINER_PUBLIC_URL` has a trailing slash or includes `/mcp`. `https://YOUR-DOMAIN/health`
should return `{"status":"ok"}` regardless of auth.

## Backup & restore

The whole life log is one SQLite file (`JOURNAL_DB`, on the `/data` volume in prod). If
the volume is lost, so is everything — so pull a copy *off the box* on a schedule.

**Download** a backup at `GET /export/journal.db`. There's no button for this in the UI
— it's just a URL you hit with curl (or a browser tab). The server builds the file with
SQLite's `VACUUM INTO`, so it's a consistent, self-contained snapshot — schema, every row,
and the FTS5 search index — taken inside a read transaction, safe to grab while the app is
live. The download is named `journal-YYYY-MM-DD.db` (Pacific date).

Two ways to authenticate:

- **In the browser** — a logged-in session just works (paste the URL in a tab).
- **Headless (cron from another machine)** — set `BACKUP_TOKEN` to a strong random value
  (`openssl rand -hex 32`) and present it. No Google login, no cookie jar. This is a
  read-only, backup-only credential, kept separate from your account login — so a leaked
  cron token can pull backups but can't touch anything else. Leave `BACKUP_TOKEN` unset to
  disable the headless path entirely (browser/session only).

```bash
# daily backup cron on any machine you trust (survives the server — the point):
curl -fsS -H "Authorization: Bearer $BACKUP_TOKEN" \
     https://YOUR-DOMAIN/app/export/journal.db \
     -o "backups/journal-$(date +%F).db"
```

(The token can also go in an `X-Backup-Token:` header or, less ideally since it lands in
logs, a `?token=` query param. Note the `/app` prefix on the combined deployment; a
standalone `webapp/app.py` serves it at `/export/journal.db`.)

**Restore** is "drop it in and restart" — no dump to replay:

```bash
cp journal-2026-06-04.db /data/journal.db   # the path JOURNAL_DB points at
# restart the app; init_db() runs its IF-NOT-EXISTS migrations and you're back.
```

(Authless/local: the route is open, same as the rest of the dev app.)

## Menu-bar macros

Today's protein and water in the macOS menu bar, via
[SwiftBar](https://github.com/swiftbar/SwiftBar) — a glanceable nudge, so the gap
between "should log that" and actually logging it is one glance wide.

Two pieces: a read-only endpoint on the server, and a plugin script on the Mac.

**1. Server.** `GET /api/today.json` returns today's water/protein sums plus the display
targets they're read against — no entries, no people, no items. Set `WIDGET_TOKEN` to
a strong random value (`openssl rand -hex 32`) and restart:

```bash
curl -H "Authorization: Bearer $WIDGET_TOKEN" https://YOUR-DOMAIN/app/api/today.json
```

```json
{"date": "2026-08-06",
 "nutrients": {"protein_g": {"total": 92, "target": 150},
               "water_oz":  {"total": 48, "target": 128}}}
```

`total` is `null` when nothing logged carries that figure — the same distinction the
`/food` rings draw between "0 so far" and "not logged", so a client can show an unknown
state rather than claiming a zero. Units aren't included: they're a rendering choice that lives in `macros.html`, and a
second server-side copy is how the two drift.

> **`WIDGET_TOKEN` is deliberately NOT `BACKUP_TOKEN`.** This token sits on every device
> that wants a number on screen; `BACKUP_TOKEN` downloads the entire journal. Keep the
> blast radius of the widely-copied credential at one day of nutrient sums. The two are
> not interchangeable — each endpoint accepts only its own.

**2. Mac.** `scripts/swiftbar/macros.1m.py` is the plugin — a **template**. Copy it into
your SwiftBar plugin folder and fill in `JOURNAL_URL` and `TOKEN` at the top of *that*
copy:

```bash
cp scripts/swiftbar/macros.1m.py ~/path/to/swiftbar-plugins/
$EDITOR ~/path/to/swiftbar-plugins/macros.1m.py   # set JOURNAL_URL and TOKEN
```

> **Copy, not a symlink — because this repo is public.** A symlink would auto-update on
> `git pull`, but it would also make the file you edit the file you commit, and one
> `git add -A` would publish your token. Git history keeps a secret even after a later
> commit removes the line, so this is a mistake you can't quietly undo. The tracked copy
> stays a template with empty placeholders; re-copy it when the plugin changes.

(`JOURNAL_URL` / `JOURNAL_WIDGET_TOKEN` in the environment override the constants, if you
prefer to keep the file pristine.)

The **refresh interval is the filename** — `macros.1m.py` polls every minute; rename
to `.1m.` / `.15m.` to change it. The dropdown carries a Refresh item for right after you
log something. It shows each figure — gauge, figure and target, no color, since a
target is just a target — and links back to the journal. If the server is unreachable the
menu bar goes quiet (grey dashes) and the detail lands in the dropdown — a bar that shouts
on every dropped wifi connection is a bar you learn to ignore.

## Web frontend

`webapp/` is a small browser UI for reviewing what's been recorded — journal entries
(with FTS search), workout sessions, water/protein, and people. The browse/reading
pages are **read-only**: they read the **same** SQLite DB and reuse `server.py`'s
retrieval functions directly (the single source of truth for data shapes), so they never
duplicate query logic. Writes are confined to a few purpose-built surfaces: the AI
**chat panels**, plus small settings carve-outs (targets, goals, display prefs).

Stack: FastAPI + Jinja2, server-rendered. Design deliberately mirrors the
`workout_tracker` app — Inter, white/black + grayscale, thin-bordered cards,
uppercase `tracking-widest` labels, stat-tile grids.

Pages: journal (+ `?q=` search, + AI chat panel) · water & protein (`/food`) ·
entry detail · workouts · graphs · people · person detail · weight.

### In-app AI chat — toolset-scoped

Off by default; turns on only when `ANTHROPIC_API_KEY` is set. It's the web app acting as
an MCP *client*: an agent loop (`webapp/chat.py`) streams `anthropic.messages` and
dispatches each `tool_use` to the **same** `@mcp.tool()` functions in `server.py` — so
the project's rule holds, there's still no LLM *inside* the server; the model lives in the
web app exactly like Claude Desktop does. The system prompt and tool schemas are lifted
live from each server's `instructions` + `list_tools()`, so editing a docstring in
`server.py` updates the chat with no duplication. Conversations are in-memory per
`(agent, session)` (lost on restart — fine for a single user). Tool calls surface as
chips linking to the affected page.

Each surface is bound to **one** toolset (smaller tool surface = less latency, the same
reason the MCP servers are split):

- **`journal`** — the journal server's people/entry tools. Lives as a **slide-in panel** on the `/journal` page
  (near-fullscreen on mobile, a right-edge side panel on desktop). Posts to
  `/chat/journal/send`.
- **`trainer`** — the trainer server's workout + water/protein tools. Will get its **own page** linked
  from the workout page (longer, workout-length conversations). Wired in `chat.py`; the
  page itself is a later round.

Env: `ANTHROPIC_API_KEY` (required to enable), `CHAT_MODEL` (default
`claude-sonnet-4-6`). Adds `anthropic` to `webapp/requirements.txt`. The web app also
auto-loads a git-ignored `.env` at the project root (a tiny zero-dep loader in
`app.py`; a real shell/Coolify var still wins, a present-but-blank one yields to `.env`).

**Installable as a PWA.** The UI ships a web app manifest
(`/manifest.webmanifest`) and a service worker (`/sw.js`), both generated
per-request so their `start_url`/`scope`/icon paths carry the mount prefix
(works mounted at `/app` or standalone at `/`). On a phone, open the site and
use **Add to Home Screen** — it then launches full-screen (`display:
standalone`) with its own icon (`webapp/static/icon-*.png`, regenerate with
`python scripts/gen_icons.py`). The service worker is network-first for pages —
it never caches authed HTML, just shows a small offline page when the network
drops — and stale-while-revalidates the static icons; bump `VERSION` in the
worker to retire old caches. Both the manifest and worker are unauthenticated
(they carry no journal data) so install works before sign-in.

**Same process as the trainer MCP server.** In production one container runs both:
`webapp/combined.py` serves the UI under **`/app`** (the root redirects there), `/health`,
and the trainer — on its own host, or at `/trainer/mcp` when authless. The UI's login
callback is `/app/auth/callback`. The UI honors a mount prefix via the ASGI
`root_path`, so the same templates also work when run standalone at the root (below).

**Run the UI standalone, locally** (authless — for local/dummy data only):

```bash
JOURNAL_DB=./journal.db .venv/bin/python webapp/app.py     # http://localhost:8001/
```

**Run exactly like production** (trainer + UI in one process):

```bash
JOURNAL_DB=./journal.db MCP_TRANSPORT=http PORT=8000 .venv/bin/python webapp/combined.py
# trainer: http://localhost:8000/trainer/mcp   ·   UI: http://localhost:8000/app
```

UI env vars: `SESSION_SECRET` (set a random value in prod), `WEB_BASE_URL` (public
origin — used to build the OAuth redirect; **defaults to `PUBLIC_URL`**),
`JOURNAL_ALLOWED_EMAILS`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`. With the
`GOOGLE_*` vars unset the UI runs authless; set them to gate it behind Google sign-in +
the email allowlist (the *same* allowlist the trainer uses). Standalone-only: `PORT`
(default 8001), `WEB_HOST`.

## Tools

Two surfaces. The **journal** tools (`add_journal_entry` through `get_briefing` below)
are driven only by the web app's own chat, in-process — there is no journal connector.
The **trainer** server (`TRAINER-DOMAIN/mcp`, or `/trainer/mcp` on the main origin when
authless) carries the rest, including the water/protein log, plus its own
`delete_record` scoped to `workout`/`set`/`intake`. Both hit the same DB.

| Tool | Purpose |
|---|---|
| `add_journal_entry` | Save an entry + return candidate matches per named person |
| `link_mentions` | Resolve pending mentions to people (with optional alias learning) |
| `save_person` | Create or update a person — omit `person_id` to create, pass it to edit; `aliases` adds/learns surface forms, `groups` sets circles |
| `update_contact` | Merge contact details (emails/phones/addresses/websites/…) into a person's free-form JSON blob; shallow per-key merge, read-then-write for lists |
| `list_pending_mentions` | The "tell you later" queue |
| `list_people` | Compact registry; filter by name/role or group |
| `get_person_history` | Every entry about one person — the payoff query |
| `get_related_people` | Emergent network: who's mentioned alongside this person |
| `get_briefing` | One-call session context, scoped to recent: entries from the last `days` (14) + summaries for people mentioned in the last `people_days` (7), plus a compact roster of everyone else, groups, and the pending count |
| `get_entry` | Fetch one entry, including the verbatim `raw_body` on demand |
| `update_entry` | Edit an entry's date (`entry_date`), cleaned `body`, or `raw_body`; pass `mentions` to reconcile who it references (adds/removes mention rows, keeps resolved links) |
| `reorder_entries` | Set a day's within-day chronological order (entries append on save; reorder so the day reads earliest-first, or to move one) |
| `search_entries` | Full-text search for topics/events. Plain words are tokenized and quoted before hitting FTS5 (so apostrophes/punctuation are safe, terms ANDed); `raw_query=True` passes FTS5 syntax through for OR/NEAR/prefix\* |
| `journal_delete_entry` | Delete one entry and its mentions — irreversible. App chat only; not advertised on the connector |
| `log_intake` | *(trainer)* Log ONE thing consumed — `water_oz` and/or `protein_g`, optional label; returns the day's totals + targets |
| `get_intake` | *(trainer)* Water/protein days back: items *with ids*, day totals, averages, targets |
| `set_intake_targets` | *(trainer)* Set the daily water/protein targets when asked (0 = back to default); also behind `/food`'s Targets popover |
| `update_intake` | *(trainer)* Correct one logged item by id — the day's totals re-derive themselves |
| `list_exercises` | Your exercises: `active` (what the trainer programs from) and `archived` (done before, with a note on why you stopped), each with muscles, last done, session count |
| `add_exercise` | Add a movement ahead of using it (planning/logging a new name with its `muscles` creates it on the fly anyway); refuses an existing name, asks "did you mean?" on a near-duplicate unless `new=True` |
| `update_exercise` | Rename an exercise (history follows), fix its muscles, category or note |
| `archive_exercise` | Take a lift out of the program with a reason (`note`), or bring one back; nothing is deleted |
| `log_workout` | Record a session; one call, or pass `workout_id` to append set-by-set; names resolve against the closed catalog, unmatched ones come back under `unmatched`/`candidates` (never auto-created) |
| `update_workout` | Edit session metadata (move date, focus, feeling, notes); `planned_date` moves a *planned* session to another day (`""` unschedules); `append_note` adds a line without clobbering earlier notes |
| `update_set` | Correct one logged set (find `set_id` via `get_exercise_history`), or retarget a pending one |
| `start_workout_plan` | Lay out a session (today or a `planned_date`) as pending sets with target weight/reps/RPE |
| `complete_sets` | Mark any number of planned sets done in one call (omitted numbers default to the targets); returns the plan + `new_prs` |
| `swap_exercise` / `add_to_plan` / `remove_from_plan` / `reorder_plan` | Edit a plan mid-session |
| `get_workout_plan` / `finish_workout` | Read a plan; close it out (pending sets skipped, session dated) |
| `get_personal_records` | Heaviest / best-e1RM / cardio bests per lift |
| `import_weigh_ins` | Load rows from the scale app's export (attached to the conversation); idempotent on the reading's timestamp |
| `get_exercise_history` | Per-session weight/reps/rpe (+ `set_id`/`workout_id`) for one lift — progressive overload + edit discovery |
| `get_fitness_briefing` | One-call trainer context: profile + per-muscle recency + recent sessions (with notes) + latest bodyweight + today's water/protein (`intake_today`) + `upcoming` (sessions already planned, not yet done) |
| `delete_record` | *(trainer)* Delete one record by `kind` + `id` — `workout`/`set`/`intake` (weigh-ins are import-only and not deletable here) — irreversible, cascades/renumbers as needed. The journal's delete is its own narrow tool, `journal_delete_entry` |
| `update_profile` | Merge durable training facts (injury, split, goals) into the JSON profile |

## Notes / next steps

- Matching is Jaro-Winkler + Metaphone (sounds-alike floor at 0.88). Tune the 0.6
  candidate floor in `find_candidates` if you get too much/little.
- `entry_date` is the day an entry is *about*, separate from `created_at`, so
  back-dating ("yesterday I…") sorts correctly in history. Use `update_entry` to
  correct it after the fact.
- **All user-facing dates are Pacific** (`America/Los_Angeles`): `today()` and every
  date default roll over at Pacific midnight, not the server's UTC midnight. Both
  briefings return `now` (current Pacific date/time) so the model can anchor
  "today"/"yesterday" correctly. `created_at` stays UTC — it's a storage timestamp.
- Contact info is a free-form JSON `contact` blob per person (multi-valued — several
  phones, two addresses, websites), edited via `update_contact` with a shallow per-key
  merge. The legacy single-valued `email`/`phone`/`address` columns are folded into it
  on migration and otherwise unused. A future vCard import/export would map onto this blob.
- **Deferred on purpose:** a typed relationship graph (edges like "X is Robin's
  friend"). Groups + the emergent co-mention query cover most of the "networking"
  value without it; add edges only when you actually hit a question they can't answer.
- **Optional later:** a one-way pull from Google Contacts (People API) to backfill
  contact fields, treating your DB as the enrichment layer. Avoid two-way sync.
- Possible v2: vCard import/export, and a scheduled job that regenerates each
  person's `summary` from their recent entries.

<!-- ci: auto-deploy webhook test c6fd7db -->
