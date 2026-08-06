# Journal MCP

A conversational journal with entity resolution. You talk; Claude captures entries
and resolves who you mean. The server is a deterministic data + matching layer —
no LLM inside it. The judgment ("which Tom?") happens in the conversation.

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
  Hallie's friends). Separately, `get_related_people` derives who gets talked about
  together straight from the journal — no tagging needed.
- **Session context in one call.** `get_briefing` hands Claude the people roster
  (with short per-person summaries), groups, pending count, and recent entries, so it
  knows who and what you're likely talking about before you explain.
- **"Tell you later" works.** Unresolved mentions sit in a pending queue until you
  feel like resolving them.

## Setup

```bash
cd journal-mcp
python3 -m venv .venv
.venv/bin/pip install fastmcp jellyfish
```

The database is a single SQLite file at `~/journal.db` (override with `JOURNAL_DB`).
It holds named real people in your life — keep it somewhere encrypted, or swap in
SQLCipher later if you want at-rest encryption.

## Register with Claude Desktop

Edit `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "journal": {
      "command": "/ABSOLUTE/PATH/journal-mcp/.venv/bin/python",
      "args": ["/ABSOLUTE/PATH/journal-mcp/server.py"],
      "env": { "JOURNAL_DB": "/ABSOLUTE/PATH/journal.db" }
    },
    "trainer": {
      "command": "/ABSOLUTE/PATH/journal-mcp/.venv/bin/python",
      "args": ["/ABSOLUTE/PATH/journal-mcp/server.py"],
      "env": { "JOURNAL_DB": "/ABSOLUTE/PATH/journal.db", "MCP_SERVER": "trainer" }
    }
  }
}
```

Use absolute paths. The two entries run the same `server.py` against the same DB; over
stdio each launch serves one MCP server, selected by `MCP_SERVER` (the journal+drinking
tools, or — with `MCP_SERVER=trainer` — the training tools). Register only `journal` if
you don't want the trainer tools loaded. Restart Claude Desktop; the tools appear in the
tools menu.

## Make the conversation flow (the efficient-integration half)

Put this in a dedicated Claude **Project's** custom instructions, so every chat in
that project follows the protocol without you re-explaining it. The tool docstrings
already carry most of it; this just sets the posture.

> You are my journaling assistant. When I tell you about my day:
> 1. Write a clean entry: turn my free-association into clear, concise, organized
>    prose that keeps the substance and my voice and drops the filler. Call
>    `add_journal_entry` with that as `body`, my original words verbatim as
>    `raw_body`, and the people I named as `mentions` (the surface form I actually
>    used, taken from my raw words). Do this first, always — never make me wait on
>    resolution.
> 2. For each returned mention, look at the candidates:
>    - One candidate ≥ 0.85 with the next one ≥ 0.15 behind → link it silently with
>      `link_mentions`. If my surface form wasn't already that person's alias, set
>      `learn_alias: true`.
>    - Two close candidates (e.g. both Toms) → ask me which one in one short
>      question, using context, then link.
>    - Nothing ≥ 0.6 → it's probably someone new; ask, then `save_person` (no
>      `person_id`) and link.
> 3. If I say I'll explain later, leave the mention pending — don't push.
> 4. At the start of a session, call `get_briefing` once to load context — who I
>    know (roster, roles, groups, summaries), the pending queue, and recent entries.
>    When I tell you something durable about a person, keep their `summary` current
>    with `save_person` (pass their `person_id`), and set their `groups` when I place
>    them in a circle.
> 5. When I ask what's happened with someone, use `get_person_history`; for topics
>    or events, use `search_entries`; for "who's connected to X", use
>    `get_related_people`.
> Keep confirmations to one line. Don't read entries back to me unless I ask.

## Drinking + personal trainer

The same codebase also tracks drinking and acts as a personal trainer — same rule as
the journal: **no LLM in the server.** It stores drinks/workouts and computes
deterministic aggregates (per-muscle recency, sober streaks); the coaching judgment
— next weight, what to rest, which exercises, how to explain form — happens in the
conversation, from what the retrieval tools return.

**The trainer is a separate MCP server.** Drinking stays on the journal server, but the
training tools live on their own FastMCP instance sharing the same DB. Connect it as its
own connector and give it its own Claude **Project**, so a journaling chat doesn't load
the workout tools and vice-versa — each conversation carries a smaller, more relevant
tool set. Use the trainer posture below as that project's custom instructions.

Where it's exposed depends on auth. Two full OAuth servers can't share one origin (their
`/authorize`, `/token`, `/auth/callback` paths collide), so in production the trainer
runs on its **own subdomain** — set `TRAINER_PUBLIC_URL=https://TRAINER-DOMAIN` and the
single process routes that hostname to the trainer server at its root (connector:
`https://TRAINER-DOMAIN/mcp`, clean root OAuth). Over stdio use `MCP_SERVER=trainer`.
With `TRAINER_PUBLIC_URL` unset (local/authless), the trainer falls back to
`/trainer/mcp` on the main origin. See "Remote deployment" for the subdomain steps.

- **Drinking.** `log_drinks` records standard drinks for a day (a beer/wine ≈ 1, a
  strong cocktail ≈ 1.5); call it as often as needed. `get_drink_summary` gives daily
  totals, averages, and the current sober streak. Sober days aren't stored — they're
  the gaps. To fix a mistake, `get_drink_summary(include_rows=True)` to find the row
  id, then `update_drink` (corrections go either direction, unlike re-logging) or
  `delete_record(kind="drink")`.
- **Eating.** A day has ONE eating section, the same daily shape as drinks. `log_food`
  appends what you ate to that day's running food list ("eggs and toast", later
  "chipotle bowl") and ADDS any nutrients passed with it; the numeric columns
  (calories, protein/carbs/fat, sodium, fiber) are optional and stay NULL until
  estimated, so a day described only in words never reads as zero calories.
  `get_nutrition` reads days back with per-nutrient averages over the days that carry
  them (each average has its own denominator); `update_nutrition(food_date=…)`
  replaces a day's values (read first — `summary` overwrites the whole list), and
  `delete_record(kind="nutrition")` drops a day. The estimating is the model's job:
  there's no food database in the server. The day's section renders under its entries
  on the journal feed.
- **A closed exercise library.** The catalog is a fixed set the trainer draws on but
  never grows: ~870 movements pre-loaded from free-exercise-db, plus any you add yourself
  through the AI **+ Add an exercise** panel at `/trainer/library` (describe a movement and
  it dedupes, fills in the muscles/difficulty/technique/cautions, and adds it). That panel
  runs on the website only — the journal/trainer connectors still can't create exercises.
  The AI programs only from what's
  there. `log_workout` records a session — the whole thing in one call, or set-by-set as
  it happens by passing the first call's `workout_id` back on each later call so the sets
  append to one session instead of fragmenting it. Exercise names resolve against the
  library (fuzzily, so a near-spelling lands); a name with no match is skipped and returned
  under `unmatched` with its closest `candidates`, never auto-created. The AI's
  `save_exercise` only *enriches* existing entries (technique/cautions/muscles/form clip)
  or toggles rotation. Fix a logged session with `get_exercise_history` (to get `set_id`s)
  + `update_set` or `delete_record(kind="set")`, `update_workout` to move/relabel it, or
  `delete_record(kind="workout")` for the whole session.
- **Progressive overload.** Each set stores `weight_lbs`/`reps`/`rpe` (1–10).
  `get_exercise_history` replays a lift session-by-session so the trainer can judge
  the next weight: all sets clean at RPE ≤ 8 → add weight; grinding at RPE 10 short of
  target → hold or deload.
- **What to work, what to rest.** `get_fitness_briefing` returns the profile (injury,
  split, goals), per-muscle recency (days since trained + last-7-day set volume), and
  recent sessions *with their notes* — enough to program the day and respect recovery,
  and to carry a "left shoulder twinge" logged last time into this session's plan.
- **Session notes as durable context.** A session's `notes` (set on `log_workout`, or
  added later with `update_workout`'s `append_note`, which adds a line without
  clobbering earlier ones) resurface in `get_fitness_briefing`, so observations made
  mid-workout become cautions next time.
- **Bodyweight + trend.** `log_bodyweight` records a weigh-in (returning the change vs
  the prior reading), and the latest reading + 30-day change ride along in
  `get_fitness_briefing`; the webapp's Training page shows the longer trend. Storage is
  date-keyed (its own daily metric, not a column on a workout), so you can weigh on rest
  days too. Fix a mistyped reading by deleting it (`delete_record(kind="weight")`, using
  the `bodyweight_id` from the log return) and re-logging.

Suggested posture for the **trainer project's** custom instructions (the drinking lines
belong with the journaling project, since `log_drinks`/`get_drink_summary` are on the
journal connector):

> When I talk about drinking, convert it to standard drinks and `log_drinks`. When I
> ask how I'm doing, use `get_drink_summary`. When I mention food, `log_food` it onto
> that day's eating section — estimate calories/protein only when I ask for numbers.
> When I train: at the start of a session call `get_fitness_briefing` to see what's
> recovered vs recently hit, my injuries, and my split, then recommend the day's work
> within those. Explain unfamiliar lifts from the catalog (`exercises`); before
> suggesting a weight, check `get_exercise_history` and apply progressive overload
> against RPE. Log with `log_workout` — either the finished session in one call, or
> set-by-set during the workout (reuse the returned `workout_id` so it stays one
> session). If I correct something afterward, use `get_exercise_history` to find the
> `set_id`, then `update_set` or `delete_record`. The exercise library is closed — program
> only movements it already holds (`exercises(muscle=…, rotation_only=True)` to pick,
> `exercises(similar_to=…)` for swaps); if a name doesn't resolve it comes back under
> `unmatched`/`candidates`, so use one of those or tell me to add the movement on the
> library page. Keep existing entries coached with `save_exercise` (muscles in their three
> emphasis tiers, equipment, technique, a form gif/video). Capture anything I mention about how it went in the session
> notes (`append_note`) — it's context for next time. After we log a session, remind me
> to weigh in and record it with `log_bodyweight`. Keep
> durable facts (injuries, split, goals) in `update_profile`.

## Remote deployment — phone access via Coolify

The local setup above connects only to Claude Desktop. To use the journal on your
**phone**, the server has to run as a remote MCP server: a public HTTPS endpoint that
Anthropic's cloud connects to. You add it once at claude.ai (you can't add a new
connector from the mobile app), then it works on iOS/Android. Requires a Pro or Max plan.

The server already supports this: set `MCP_TRANSPORT=http` and it serves Streamable
HTTP at `/mcp` (see the Dockerfile). On Coolify:

1. New resource → from this repo (or Dockerfile). Coolify builds the image.
2. Add a **persistent volume** mounted at `/data` so `journal.db` survives redeploys.
3. Give it a domain; Coolify provisions HTTPS via Let's Encrypt automatically.
4. **For the trainer, add a second domain** on the SAME Coolify application (Coolify
   accepts multiple domains per service) — e.g. `https://TRAINER-DOMAIN` — pointed at
   the same DNS, and set the env var `TRAINER_PUBLIC_URL=https://TRAINER-DOMAIN`. The
   one process then serves the journal on `YOUR-DOMAIN` and the trainer on
   `TRAINER-DOMAIN`, each with its own root OAuth.
5. Your MCP URLs are `https://YOUR-DOMAIN/mcp` (journal + drinking) and
   `https://TRAINER-DOMAIN/mcp` (training).
6. In a browser at claude.ai → Customize → Connectors → Add custom connector → paste a
   URL. Add the journal one for sure; add the trainer one as a SECOND connector if you
   want training in its own project. (With auth on, the trainer needs its own redirect
   URI in Google — see below.) Then enable each per-conversation via the "+" menu on
   your phone.

**Roll it out in two stages.** First deploy as-is (no auth) and connect it with only
**dummy data** to confirm the Claude-to-Coolify pipe works end to end. Do **not** put
real journal entries in until auth is on — the endpoint is public.

### Auth (required before real data)

Single-user app, so you don't need user management — just "only your Google account
gets in." The server uses FastMCP's `GoogleProvider`, which acts as a full OAuth 2.1
authorization server (with PKCE + Dynamic Client Registration) that proxies Google.
Claude discovers it automatically and self-registers, so you just paste the URL — no
client ID/secret in Claude's connector settings.

**1. Create a Google OAuth client** (Google Cloud Console → APIs & Services →
Credentials → Create OAuth client ID → Web application). Add these authorized redirect
URIs (one client serves both hosts):

```
https://YOUR-DOMAIN/auth/callback
https://TRAINER-DOMAIN/auth/callback     # only if you run the trainer subdomain
```

**2. Set these env vars in Coolify** (never in the image):

| Var | Value |
|---|---|
| `GOOGLE_CLIENT_ID` | from the Google client |
| `GOOGLE_CLIENT_SECRET` | from the Google client |
| `PUBLIC_URL` | `https://YOUR-DOMAIN` (no trailing slash, no `/mcp`) |
| `JOURNAL_ALLOWED_EMAILS` | your Gmail address (comma-separated for more than one) |
| `TRAINER_PUBLIC_URL` | `https://TRAINER-DOMAIN` — enables the trainer on its own host (omit to skip the trainer / keep it at `/trainer/mcp` authless) |

With those set, the server flips from authless to protected on restart. Verified
behavior: an unauthenticated request gets `401` with a `WWW-Authenticate` header
pointing to the discovery metadata; the protected-resource metadata is served at
`/.well-known/oauth-protected-resource/mcp`. After Google login, the allowlist
middleware checks the email claim and rejects any account not in
`JOURNAL_ALLOWED_EMAILS` — so a valid Google login alone is not enough.

Leave the env vars unset to run authless for local/staging tests (dummy data only).

> If the allowlist ever rejects you after a correct login, confirm Google is returning
> the `email` claim (the requested scopes include it); the check is in
> `AllowlistMiddleware`.

**Staying logged in across redeploys.** `GoogleProvider` is its own OAuth server: it
stores Claude's dynamically-registered client and your refresh tokens in an encrypted
file store. By default that lives under FastMCP's home dir, which is *inside the
container* and wiped on every push — so each deploy forces a fresh connector login. The
Dockerfile sets `FASTMCP_HOME=/data/fastmcp` to move that store onto the persistent
`/data` volume (same volume as `journal.db`), so registrations and tokens survive
redeploys. The JWT signing key needs no special handling — it's derived deterministically
from `GOOGLE_CLIENT_SECRET`, so it's stable as long as you don't rotate that secret. (The
first deploy after adding this var still logs you in once, since the store starts empty
at its new location.)


## First deploy — checklist

Two stages: prove the pipe works with no auth and fake data, then lock it down before
real entries.

**Stage 1 — authless smoke test**
- [ ] In Coolify, create a new resource from this repo (Dockerfile build pack).
- [ ] Add a persistent volume mounted at `/data`.
- [ ] Assign a domain; let Coolify provision HTTPS. Leave all `GOOGLE_*` vars unset.
- [ ] Deploy. Check `https://YOUR-DOMAIN/health` returns `{"status":"ok"}`.
- [ ] At claude.ai (in a browser) → Customize → Connectors → Add custom connector →
      paste `https://YOUR-DOMAIN/mcp`. It should connect with no login. (Authless, the
      trainer is at `https://YOUR-DOMAIN/trainer/mcp`; on the trainer subdomain it's
      `https://TRAINER-DOMAIN/mcp`.)
- [ ] On your phone, enable the connector via the "+" menu and add one throwaway
      entry about a fake person. Confirm it saves and reads back.
- [ ] Clear the test data (delete `/data/journal.db`; it recreates on next call).

**Stage 2 — turn on auth (before any real data)**
- [ ] Google Cloud Console → create an OAuth client (Web application), redirect URI
      `https://YOUR-DOMAIN/auth/callback`.
- [ ] In Coolify set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
      `PUBLIC_URL=https://YOUR-DOMAIN`, `JOURNAL_ALLOWED_EMAILS=you@gmail.com`. Redeploy.
- [ ] Confirm `/mcp` now returns 401 to an anonymous request, while `/health` still 200.
- [ ] In Claude, remove and re-add the connector; this time it sends you through Google
      sign-in. Log in with your allowlisted account.
- [ ] Add a real entry from your phone. You're live.

**Stage 3 — turn on the trainer (its own subdomain)**
- [ ] DNS: point `TRAINER-DOMAIN` at the same server as `YOUR-DOMAIN`.
- [ ] Coolify: add `https://TRAINER-DOMAIN` as a second domain on the SAME application;
      let it provision HTTPS.
- [ ] Google Cloud Console → add redirect URI `https://TRAINER-DOMAIN/auth/callback` to
      the SAME OAuth client.
- [ ] In Coolify set `TRAINER_PUBLIC_URL=https://TRAINER-DOMAIN`. Redeploy.
- [ ] Confirm `https://TRAINER-DOMAIN/mcp` returns 401 anonymously and
      `https://TRAINER-DOMAIN/.well-known/oauth-protected-resource/mcp` returns 200.
- [ ] In Claude, add a second custom connector `https://TRAINER-DOMAIN/mcp`, sign in,
      give it its own Project with the trainer posture as instructions.

If the connector shows "disconnected" after adding Google: usually the redirect URI in
Google doesn't exactly match the host's `/auth/callback`, or `PUBLIC_URL` /
`TRAINER_PUBLIC_URL` has a trailing slash or includes `/mcp` (it should be the bare origin).

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

## Web frontend

`webapp/` is a small browser UI for reviewing what's been recorded — journal entries
(with FTS search), workout sessions, drinking trends, and people. The browse/reading
pages are **read-only**: they read the **same** SQLite DB and reuse `server.py`'s
retrieval functions directly (the single source of truth for data shapes), so they never
duplicate query logic. Writes are confined to two purpose-built surfaces: the **journal
chat panel** (AI) and the **day-header drink counter** (no AI) — see below.

Stack: FastAPI + Jinja2, server-rendered. Design deliberately mirrors the
`workout_tracker` app — Inter, white/black + grayscale, thin-bordered cards,
uppercase `tracking-widest` labels, stat-tile grids.

Pages: dashboard · journal (+ `?q=` search, + AI chat panel, + per-day drink counter) ·
entry detail · workouts · graphs · people · person detail.

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

- **`journal`** — the journal server's people/entry tools, *minus* the drink tools
  (drinks are direct entry now). Lives as a **slide-in panel** on the `/journal` page
  (near-fullscreen on mobile, a right-edge side panel on desktop). Posts to
  `/chat/journal/send`.
- **`trainer`** — the trainer server's workout tools. Will get its **own page** linked
  from the workout page (longer, workout-length conversations). Wired in `chat.py`; the
  page itself is a later round.

Env: `ANTHROPIC_API_KEY` (required to enable), `CHAT_MODEL` (default
`claude-sonnet-4-6`). Adds `anthropic` to `webapp/requirements.txt`. The web app also
auto-loads a git-ignored `.env` at the project root (a tiny zero-dep loader in
`app.py`; a real shell/Coolify var still wins, a present-but-blank one yields to `.env`).

### Direct drinks entry — no AI

Drinks are a number per day, so they live **on the journal feed** rather than a page of
their own (there is no `/drinking` page): each day header carries a right-aligned drink
counter, and tapping it opens a stepper modal. Saving POSTs the day's **absolute** total
to `/drinking/day`, which corrects an existing day through `server.update_drink`, creates
a new one through `server.log_drinks`, or deletes the row when the total is 0 (a day with
no row is sober). A day with drinks but no entries still renders a bare day header, so a
drink-only day — today included — is always reachable. `kind`/`notes` stay in the DB and
stay writable from chat; the counter is just the number. Trends live on `/graphs`. No LLM.

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

**Same process as the MCP server.** In production one container runs both:
`webapp/combined.py` mounts the MCP app at the origin root (so `/mcp` and its
root-level OAuth — `/.well-known/*`, `/auth/callback` — are unchanged) and the UI under
**`/app`**. The UI's own login callback is therefore `/app/auth/callback`, distinct from
the MCP's. The UI honors a mount prefix via the ASGI `root_path`, so the same templates
also work when run standalone at the root (below).

**Run the UI standalone, locally** (authless — for local/dummy data only):

```bash
.venv/bin/pip install -r webapp/requirements.txt   # web deps; also uses server.py's deps
JOURNAL_DB=./journal.db .venv/bin/python webapp/app.py     # http://localhost:8001/
```

**Run exactly like production** (MCP + UI in one process):

```bash
JOURNAL_DB=./journal.db MCP_TRANSPORT=http PORT=8000 .venv/bin/python webapp/combined.py
# connector: http://localhost:8000/mcp   ·   UI: http://localhost:8000/app
```

UI env vars: `SESSION_SECRET` (set a random value in prod), `WEB_BASE_URL` (public
origin — used to build the OAuth redirect; **defaults to `PUBLIC_URL`** since that's the
same bare origin, so you usually don't set it), `JOURNAL_ALLOWED_EMAILS`,
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`. With the `GOOGLE_*` vars unset the UI runs
authless; set them to gate it behind Google sign-in + the email allowlist (the *same*
allowlist the MCP server uses). Standalone-only: `PORT` (default 8001), `WEB_HOST`.

**Deploy (Coolify) — no new service needed.** The existing MCP service already builds the
root `Dockerfile`, which now installs the web deps and runs `webapp/combined.py`. To turn
the UI on after redeploying:

- Google Cloud Console → add **one** redirect URI to your existing OAuth client:
  `https://YOUR-DOMAIN/app/auth/callback` (the MCP's `https://YOUR-DOMAIN/auth/callback`
  stays as-is).
- On the service, add just a random `SESSION_SECRET`. `GOOGLE_CLIENT_ID/SECRET`,
  `JOURNAL_ALLOWED_EMAILS` and `PUBLIC_URL` are already set for the MCP server and are
  reused (the UI's `WEB_BASE_URL` defaults to `PUBLIC_URL`).
- Redeploy. The connector keeps working at `/mcp`; the journal UI is at
  `https://YOUR-DOMAIN/app` and bounces anonymous visitors to `/app/login`.

Note `WEB_BASE_URL` (not `PUBLIC_URL`) — the webapp uses standard browser OAuth, separate
from the MCP server's OAuth-provider flow, so its env vars don't collide if you run both
in one Coolify project.

## Tools

Split across two connectors. The **journal** server (`YOUR-DOMAIN/mcp`) carries the
journal + drinking tools through `update_drink`, plus a `delete_record` scoped to
`entry`/`drink`. The **trainer** server (`TRAINER-DOMAIN/mcp`, or `/trainer/mcp` on the
main origin when authless) carries `save_exercise` through `update_profile`, plus its
own `delete_record` scoped to `workout`/`set`/`weight`. Both hit the same DB.

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
| `get_briefing` | One-call session context (roster, groups, pending, recent) |
| `get_entry` | Fetch one entry, including the verbatim `raw_body` on demand |
| `update_entry` | Edit an entry's date (`entry_date`), cleaned `body`, or `raw_body`; pass `mentions` to reconcile who it references (adds/removes mention rows, keeps resolved links) |
| `reorder_entries` | Set a day's within-day chronological order (entries append on save; reorder so the day reads earliest-first, or to move one) |
| `search_entries` | Full-text search for topics/events |
| `log_drinks` | Log standard drinks for a day (rows accumulate; sober days are gaps) |
| `get_drink_summary` | Daily totals + rolling stats and sober streak; `include_rows=True` adds individual rows with ids for editing |
| `update_drink` | Correct a logged drink in either direction (incl. downward) or move its day |
| `save_exercise` | Enrich an existing catalog entry (technique, mistakes, cautions, equipment, level/mechanic, form gif/video, muscles in primary/secondary/tertiary tiers) or toggle `in_rotation`/`hearted`. Cannot create — the catalog is closed to the AI; new exercises are added on the `/trainer/library` form |
| `set_rotation` | Add/remove an exercise from the rotation (the small ~10–14 pool the trainer programs from; adding also hearts it) |
| `set_hearted` | Add/remove an exercise from the hearted superset (the wider favorites bench the rotation is drawn from; un-hearting also drops it from the rotation) |
| `exercises` | Read the closed catalog — a full record when you name/id one (else `candidates`), a filtered list (muscle/equipment/category/`rotation_only`/`hearted_only`) with `level`/`mechanic`, or `similar_to=<lift>` for like-for-like swap peers |
| `log_workout` | Record a session; one call, or pass `workout_id` to append set-by-set; names resolve against the closed catalog, unmatched ones come back under `unmatched`/`candidates` (never auto-created) |
| `update_workout` | Edit session metadata (move date, focus, feeling, notes); `append_note` adds a line without clobbering earlier notes |
| `update_set` | Correct one logged set (find `set_id` via `get_exercise_history`) |
| `log_bodyweight` | Record a weigh-in (lbs); returns the change vs the prior reading |
| `get_exercise_history` | Per-session weight/reps/rpe (+ `set_id`/`workout_id`) for one lift — progressive overload + edit discovery |
| `get_fitness_briefing` | One-call trainer context: profile + per-muscle recency + recent sessions (with notes) + latest bodyweight |
| `delete_record` | Delete one record by `kind` + `id` — irreversible, cascades/renumbers as needed. On the journal connector `kind` is `entry`/`drink`; on the trainer connector it's `workout`/`set`/`weight` |
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
- **Deferred on purpose:** a typed relationship graph (edges like "X is Hallie's
  friend"). Groups + the emergent co-mention query cover most of the "networking"
  value without it; add edges only when you actually hit a question they can't answer.
- **Optional later:** a one-way pull from Google Contacts (People API) to backfill
  contact fields, treating your DB as the enrichment layer. Avoid two-way sync.
- Possible v2: vCard import/export, and a scheduled job that regenerates each
  person's `summary` from their recent entries.

<!-- ci: auto-deploy webhook test c6fd7db -->
