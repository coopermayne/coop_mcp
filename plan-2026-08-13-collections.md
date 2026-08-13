# Plan: Notes & Collections layer (v2)

**Date: 2026-08-13** · Status: agreed direction, not yet started

## The idea in one paragraph

Extend the app with a single flexible layer for everything that doesn't need bespoke
schema: free-form **notes** that can be promoted into structured **collections**
(recipes, vacation spots, exercises, whatever comes next), managed entirely through
conversation over MCP. The existing architectural rule extends from content judgment
("which Tom?") to *structural* judgment ("does this deserve a schema yet?"): the model
decides when a pile of notes becomes a collection; the server stores, indexes, and
renders deterministically. Alongside this, the **trainer subsystem retires** — workouts
become a text log, exercises become a small collection, and the second MCP
server/connector/OAuth host goes away.

## The promotion ladder

```
note  →  collection item  →  bespoke table
```

- Everything starts as a note in a default inbox.
- When a cluster forms (~10 vacation spots), the model proposes a collection, defines
  its fields, and migrates the notes — tool calls, no code changes, no DDL.
- A collection graduates to a bespoke table ONLY when a real computation or invariant
  demands it (sums, exact indexed lookups, integrity rules). That step is deliberate,
  human-plus-code, written as a migration in `init_db()` — **never DDL over MCP**.

The dividing line, stated once: **a domain earns bespoke schema when the server
computes something deterministic over it or enforces an invariant; collections are for
things the server only stores, searches, and renders.** Corollary from the aliases
discussion: a JSON field is fine while it's only read *through* its item; the moment a
field needs to be the *starting point* of an indexed cross-cutting query, that's the
promotion signal.

## What stays, what moves, what dies

| Domain | Fate | Why |
|---|---|---|
| `people` / `aliases` / `mentions` | **bespoke, untouched** | the matching engine is the product |
| `entries` + FTS | **bespoke, untouched** | ordering, two-layer bodies, search contracts |
| `intake_items` | **bespoke, untouched** | arithmetic — derived day totals are the point |
| `groups` / `person_groups` | **bespoke, untouched** | FKs into the entity graph; trivial |
| `settings` | **bespoke, untouched** | eating `targets` feed the rings |
| `body_weight` | **bespoke for now** | 10 lines of schema feeding a trend line; fold into collections only if generic views ever grow a number-over-time chart |
| `exercises` | **→ collection** (~15–20 items) | catalog machinery served retired features |
| `workouts` + `sets` | **→ workout-log collection** (text) | the model plans from ~2 weeks of text; structure served the retired /trainer card |
| `exercise_muscles`, `exercise_aliases` | **deleted, not migrated** | served the recency GROUP BY and fuzzy resolution, both retiring |
| trainer `profile` in settings | **→ a pinned note** | was always prose in a JSON coat |
| `drinks`, `nutrition` (legacy) | **drop** (after snapshot) | migrations ran long ago; only unique shred is `drinks.kind` per day — archive to a note or accept the loss |

Alternative considered and set aside: doing the flexible layer in **Notion** via its MCP
connector (zero build, great views; but loses the people engine, degrades food, weaker
search, token-fat API, and the journal moves off infrastructure we control). Remains a
valid cheap experiment for new domains if the native layer stalls.

## Collections layer design

### Schema (two tables + FTS)

- `collections` — `id`, `name` (unique), `description`, `fields` (JSON list of
  `{key, label, type}` — types stay simple: text/number/date/select), `display_hint`
  (list | table | gallery | checklist), `created_at`.
- `items` — `id`, `collection_id` (NULL = inbox note), `title`, `body` (markdown),
  `data` (JSON blob keyed by the collection's field keys), `tags`, `created_at`,
  `updated_at`. FTS5 mirror over `title` + `body` (same trigger pattern as
  `entries_fts`).

No per-collection tables, no DDL at runtime. Promoting notes is data movement, fully
reversible; the original note text is preserved as the item's body.

### Tool surface (~6 tools, one server)

- `list_collections()` — names + descriptions + field lists; briefing-grade payload
  (cheap, always consulted before deciding where something goes).
- `create_collection(name, description, fields, display_hint)` — **fuzzy-matches
  existing names and returns "did you mean?" instead of creating a near-duplicate**
  (the `_resolve_exercise` trick). Model confirms with the user before creating.
- `save_item(collection?, title, body, data?, tags?)` — no collection = inbox note.
  Capture never blocks, same rule as the journal.
- `update_item(id, …)` — read-before-write, same contract as `summary`/`contact`.
- `move_to_collection(id, collection, data)` — the promotion primitive; model extracts
  fields from the note text, one call per note.
- `search_items(query, collection?)` — FTS through `_fts_query` (reuse the existing
  literal-quoting), optional collection scope.
- `delete_record(kind="item")` — extends the existing kind-scoped delete.

Judgment rules live in docstrings + server `instructions`, per house style: when a note
is enough vs. when to propose a collection; always confirm before creating one; always
read an item before rewriting fields.

### Webapp

One generic collection browser: renders any collection from its `fields` +
`display_hint` (list/table/detail views), inbox notes list, FTS search box. Body
markdown rendered with the self-hosted `marked`. **Wiki-links**: `[[Title]]` in a body
resolves at *render time* by title match — clickable when it matches, plain text when
it doesn't. No foreign keys, no join table, nothing breaks on rename/delete.

## Trainer retirement

### Target shape

- **`exercises` collection** — title = lift name; field `status`: `doing` / `someday` /
  `shelved`; body = the user's cues ("closer grip felt better", "skip when shoulder is
  cranky"), plus any video link worth keeping.
- **`workout-log` collection** — one item per session; fields `{date, focus}`; body is
  the text block, e.g.:

  ```
  Squat        225  5/5/5    hard — last set grindy, knee fine
  RDL          185  8/8/8    med
  Leg press    270  12/12    easy — bump to 290

  felt strong, slept well. gym was packed, skipped calves.
  ```

- **No DB links from log to exercises.** ~18 distinct names + the "write names as they
  appear in the exercises collection" contract makes FTS ("squat" scoped to
  workout-log) exact enough. Links would rebuild the resolution machinery being
  retired. `[[Squat]]` wiki-links are optional flavor, not part of the format.
- **Capture flow**: do the workout, voice-dump how it went, model writes the block —
  the journal's raw→clean two-layer pattern, applied to training.
- **Programming contract** (server `instructions`): read the last ~2 weeks of
  workout-log items + the exercises collection + the profile note before proposing a
  session. RPE words: easy ≈5, med ≈7, hard ≈9.

### Migration script — `scripts/migrate_trainer.py` (run once, deterministic, no LLM)

1. **Exercises**: `SELECT … WHERE hearted=1` (covers rotation + everything ever
   logged, since logging hearts). Map `in_rotation=1` → `doing`, hearted-only →
   `someday`. Body assembled from `technique_notes` + `common_mistakes` + `cautions` +
   `video_link`. The other ~850 catalog rows are left behind.
2. **Workout log**: for each `status='done'` workout, render sets grouped by exercise
   in `ex_position` order into the exact text shape above (RPE → easy/med/hard words),
   append `feeling`/`notes`, `date` from `workout_date`. Migrated history is
   indistinguishable from future entries — full continuity.
3. Print counts (sessions in/items out, exercises in/out); spot-check a few sessions
   against the old UI before trusting it.

**Known lossy edge (accepted)**: per-set timestamps and the plan-vs-actual distinction
(`target_*`, skipped sets) flatten into the text. Weights, reps, effort, and notes —
everything actually reread — survive.

### Retirements

- **Server**: the entire `trainer_mcp` FastMCP instance and its tools; the second
  Google auth provider; `TRAINER_PUBLIC_URL`, the trainer host, its redirect URI, its
  connector. One server, one origin, one OAuth setup remain.
- **Webapp**: `/trainer` card, `/trainer/library`, `trainer.js`, their routes. The
  bodyweight trend stays (table stays bespoke). The webapp-defined `exercise` chat
  agent + `create_exercise` path retires with the library page.
- **Old tables**: left in place, dormant (`drinks`/`nutrition` pattern) for the first
  few months → rollback is "re-enable old code", not "restore data". Dropping them
  later is optional cleanup. Snapshot the DB (existing backup path) before Step 2.

## Sequencing

1. **Build the collections layer** (schema + tools + generic browser). Migration in
   `init_db()` per house convention.
2. **Live with it on a low-stakes domain** (recipes, vacation spots) for a week or
   two — prove the capture/promotion contracts before pointing it at training.
3. **Trainer migration**: snapshot DB → run `migrate_trainer.py` → verify counts +
   spot-checks.
4. **Retire trainer tool surface + webapp pages**; move the programming contract into
   the main server's `instructions`.
5. Later, deliberately: drop dormant tables; revisit `body_weight` if generic views
   grow a chart; delete `drinks`/`nutrition` after archiving `drinks.kind` if wanted.

## Open questions

- Exact `fields` type list for v1 (text/number/date/select is the working set — resist
  more until a real collection needs it).
- Whether the inbox needs its own webapp surface or just lives in search + a "recent
  notes" list.
- Whether `[[wiki-links]]` ship in v1 of the browser or wait for the first real want.
