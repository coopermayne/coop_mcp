# Plan: four Telegram bots

Status: BUILT and pushed 2026-08-24 (commit `fb1e9d0`). Phases 1+2 done, plus a
rendering round that wasn't in this plan. **Deployed but the first production webhook
delivery is UNVERIFIED** — see §11 for exactly where things stand.

## What this is

Four Telegram bots — separate handles, separate chats, separate tool surfaces —
each a client of the agent loop that already exists in `webapp/chat.py`. One per
domain:

| Bot | Agent key | Toolset | Count |
|---|---|---|---|
| `@…journal_bot` | `tg_journal` | `CONNECTOR_HIDDEN_TOOLS` (people + entries) | 14 |
| `@…intake_bot`  | `tg_intake`  | `intake_*` | 6 |
| `@…notes_bot`   | `tg_notes`   | `notes_*` + `collections_*` | 12 |
| `@…trainer_bot` | `tg_trainer` | all of `trainer_mcp` | — |

**This is not a new architecture.** `webapp/chat.py` is already "the web app as an
MCP client" — an Anthropic agent loop calling `@mcp.tool()` functions in-process.
Telegram is a third front end on that same loop, beside the browser panel and the
connectors. The rule that governs the project holds unchanged: no LLM in
`server.py`, the model does the judgment, the server stays deterministic.

No new pip dependency (`httpx` is already in `webapp/requirements.txt`), no schema
change, no Dockerfile change, no second process.

### Why four and not one

The partition already exists in the code and is already load-bearing. Every
`@mcp.tool` on the journal instance is either in `CONNECTOR_HIDDEN_TOOLS` (14
unprefixed people/entry names) or carries an `intake_` / `notes_` / `collections_`
prefix — 32 tools, disjoint, no leftovers. CLAUDE.md already states the domain
prefix is load-bearing rather than cosmetic; this feature is the second consumer
of that fact.

So the bots' tool lists are DERIVED FROM THE NAMES, not listed by hand. A tool
added tomorrow lands in the right bot with no change here — the same property
that makes `CONNECTOR_HIDDEN_TOOLS` the one place the connector/panel split is
stated.

Four small surfaces also buy what splitting the MCP servers bought: an intake bot
choosing among 6 tools is faster and picks better than a 32-tool union would.

### What four bots cost

You have to pick the chat. "Burrito after the gym with Karl" is three bots'
business and each can only do its part. Mitigated (not solved) by telling each
bot what its siblings own, so it says "that's the journal bot" instead of quietly
dropping the half it can't reach. A fifth catch-all bot with the whole journal
instance remains possible later — it's one more row in `BOTS` — but is not in
this plan.

---

## 1. New file: `webapp/telegram.py` (~260 lines)

The whole feature lives here except for a route, four agent entries, two
instruction strings, and a lifespan hook.

### 1.1 Config

```python
BOTS = {                      # bot key → (token env var, chat.py agent name)
    "journal": ("TELEGRAM_TOKEN_JOURNAL", "tg_journal"),
    "intake":  ("TELEGRAM_TOKEN_INTAKE",  "tg_intake"),
    "notes":   ("TELEGRAM_TOKEN_NOTES",   "tg_notes"),
    "trainer": ("TELEGRAM_TOKEN_TRAINER", "tg_trainer"),
}
```

A bot whose token is unset simply does not start — so this ships one bot at a
time. `MODE = TELEGRAM_MODE` is `webhook` (prod), `polling` (dev) or `off`
(default). Off unless configured, exactly as the chat surface is off unless
`ANTHROPIC_API_KEY` is set.

`ALLOWED_CHAT_IDS` parsed once from `TELEGRAM_ALLOWED_CHAT_IDS`.

### 1.2 The authorization guard — the most important code in the feature

The webhook is in `PUBLIC_PATHS` (Telegram has no Google session) and never
crosses the MCP wire, so `RequireAuth` and `AllowlistMiddleware` both sit this one
out. The chat-id check is the ONLY identity gate.

```python
def _authorized(msg: dict) -> bool:
    """Fail CLOSED: an unset allowlist talks to nobody.

    Deliberately the OPPOSITE of server.AllowlistMiddleware, where an empty
    ALLOWED_EMAILS means authless dev — there, Google auth still stands behind
    it. Here there is nothing behind it.

    Keyed on the NUMERIC id, never `from.username`: usernames are changeable and
    recyclable, the id is assigned by Telegram and unspoofable by a sender. The
    from==chat equality means "a private chat with that person" and rules out a
    group the bot was added to (negative chat id).
    """
    if not ALLOWED_CHAT_IDS:
        return False
    chat_id = (msg.get("chat") or {}).get("id")
    return chat_id in ALLOWED_CHAT_IDS and (msg.get("from") or {}).get("id") == chat_id
```

Rejection is SILENT (no reply — a reply confirms the bot is live and worth
probing) but logs the id. The HTTP response is still `200`: a non-2xx makes
Telegram retry the same unauthorized update for hours.

### 1.3 Update handling

```
handle_update(bot_key, update) →
    msg = update["message"]  (ignore edited_message, callbacks, everything else)
    dedupe on update["update_id"]         # bounded set, ~512 ids
    _authorized(msg) or return
    text = msg.get("text");  non-text → one-line "text only for now" reply
    slash command? → _command(...)
    else → _run(bot_key, chat_id, text)
```

**Dedupe** matters for correctness, not tidiness: a Telegram retry of a delivered
update logs the burrito twice. It also kills replay of a captured update.

### 1.4 Bridging to the agent loop

```python
async with _lock_for(chat_id):                 # see Failure modes
    task = asyncio.create_task(_typing(bot_key, chat_id))   # every 4s
    try:
        parts = []
        async for ev in chat.run_turn(agent, f"tg:{bot_key}:{chat_id}", text):
            if ev["type"] == "text":  parts.append(ev["text"])
            elif ev["type"] == "tool" and ev["kind"] == "write":  chips.append(ev)
            elif ev["type"] == "error": ...
    finally:
        task.cancel()
    await _send(bot_key, chat_id, "".join(parts), chips)
```

Session id `tg:{bot_key}:{chat_id}` gives each bot its own thread, each with the
Pacific-day rollover `_maybe_rollover` already does. Nothing in `chat.py`'s state
handling needs to change.

### 1.5 Sending

- **No `parse_mode`.** The model writes markdown; MarkdownV2 needs 18 characters
  escaped and one stray `.` returns a 400 that silently eats the reply. Plain text
  is correct and Telegram auto-links bare URLs anyway. (The system prompt tells
  the model not to use markdown — see §2.)
- **Split at 4096 chars**, on a paragraph boundary where possible.
- **Typing indicator** via `sendChatAction`, refreshed every 4s (it expires at 5).
  This is what stops a 20-second multi-tool turn reading as a broken bot.
- **Write chips as links.** `chat._tool_chip` already returns `{summary, kind,
  href}` with a relative href; prefix `PUBLIC_URL + "/app"` (the same job
  `base_path` does for the browser) and append them as a short trailing line.
  Reads stay invisible.
- **Delete own replies** after `TELEGRAM_REPLY_TTL` seconds (default: unset =
  never). Telegram is a transport; the record lives in SQLite. Note the API only
  reliably allows a bot to delete its OWN outgoing messages in a private chat,
  within 48h — the user's own messages stay, so this halves the at-rest exposure
  rather than eliminating it. Verify before documenting it as a privacy feature.

### 1.6 Commands

| Command | Effect |
|---|---|
| `/new` | `chat.reset(agent, session_id)` — fresh thread |
| `/help` | one line: what this bot owns, what its siblings own |
| `/whoami` | echoes the numeric chat id (allowlisted callers only) |

Registered per bot with `setMyCommands` at boot, so each chat shows its own menu.

### 1.7 Transport

**Webhook (prod).** Registered at startup: `getWebhookInfo`, and `setWebhook` only
if the URL or secret differs — so a redeploy is a no-op rather than a re-register.
`allowed_updates=["message"]` narrows the surface for free.

**Polling (dev).** One `getUpdates` task per configured token, long-poll timeout
30s, offset persisted in memory. This is what makes the bots testable against
`journal_dev.db` on a laptop, which webhook mode cannot be.

Both call the same `handle_update`.

---

## 2. `server.py` — two new composed instruction strings

The blocks already exist and are already composed into two texts so the surfaces
can't drift (`_INTAKE_BLOCK`, `_COLLECTIONS_BLOCK`, `_APP_LINK_BLOCK`,
`_pacific_block()`, `_JOURNAL_ONLY_BLOCK`, `JOURNAL_CHAT_INSTRUCTIONS`). This adds
two more compositions of the same blocks — no new prose about how intake works.

```python
INTAKE_CHAT_INSTRUCTIONS = f"""…{_pacific_block("intake_summary")}

{_INTAKE_BLOCK}

{_APP_LINK_BLOCK}
…"""

NOTES_CHAT_INSTRUCTIONS  = f"""…{_pacific_block("…")}

{_COLLECTIONS_BLOCK}

{_APP_LINK_BLOCK}
…"""
```

Three decisions inside them:

- **`_APP_LINK_BLOCK` is IN, for all four bots.** The connector gets it because a
  Claude conversation writes while the user reads in the app; the in-app panel is
  excluded because pointing at the page you're standing on is noise. Telegram is
  the most different screen of all — a tappable link back to `/food` is worth more
  here than anywhere.
- **`_JOURNAL_ONLY_BLOCK` is OUT**, replaced by a parameterized
  `_siblings_block(bot)`. The existing block says "this panel captures the journal,
  don't offer to log the meal somewhere else" — right for the panel, wrong here,
  where the meal genuinely does have a sibling bot to go to. The new block names
  the siblings so the model can say where something belongs.
- **A shared `_TELEGRAM_BLOCK`**: replies are plain text (no markdown — it renders
  literally), the user is on a phone, be brief, one message per turn.

`intake_summary` stays the clock for the intake bot; `get_briefing` for the
journal bot. The notes bot has no `now`-carrying tool — it gets the Pacific facts
from `chat.py`'s live clock system block, which every surface already receives.

---

## 3. `webapp/chat.py` — four agent entries

```python
def _prefix(*ps):  return lambda n: n.startswith(ps)

"tg_journal": {"server": server.mcp, "instructions": server.JOURNAL_CHAT_INSTRUCTIONS,
               "include": server.CONNECTOR_HIDDEN_TOOLS, "blurb": _TG_JOURNAL_BLURB},
"tg_intake":  {"server": server.mcp, "instructions": server.INTAKE_CHAT_INSTRUCTIONS,
               "include": _prefix("intake_"), "blurb": _TG_INTAKE_BLURB},
"tg_notes":   {"server": server.mcp, "instructions": server.NOTES_CHAT_INSTRUCTIONS,
               "include": _prefix("notes_", "collections_"), "blurb": _TG_NOTES_BLURB},
"tg_trainer": {"server": server.trainer_mcp, "exclude": set(), "blurb": _TG_TRAINER_BLURB},
```

Two small mechanical changes:

1. `_ensure_tools` currently does `t.name not in include`. Make `include` accept a
   **set or a callable** so a prefix predicate works. Three lines.
2. A **boot-time invariant check**: the four journal-side tool sets must be
   disjoint and must cover `server.mcp`'s tool list. A tool added with no prefix
   and no `CONNECTOR_HIDDEN_TOOLS` entry then fails loudly at startup instead of
   being silently unreachable from every bot. Same "one rule, two users" guard as
   `groupable_fields` / `can_map`.

**Separate entries rather than reusing `journal` / `trainer`** because the BLURB
describes the surface: `_TRAINER_BLURB` talks about the plan card beside the chat
and the Training page above the history, none of which exists in Telegram. Tools
and server are reused; the framing is not.

`_WRITE_TOOLS` needs no change — it already covers everything these bots call.

---

## 4. `webapp/app.py` — one route (~30 lines)

```python
@app.post("/telegram/webhook/{bot_key}/{secret}")
async def telegram_webhook(request: Request, bot_key: str, secret: str):
    if not tg.enabled(): return JSONResponse({"ok": True})
    if not secrets.compare_digest(secret, tg.WEBHOOK_SECRET): return Response(status_code=403)
    hdr = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not secrets.compare_digest(hdr, tg.WEBHOOK_SECRET):   return Response(status_code=403)
    if bot_key not in tg.BOTS:                               return Response(status_code=403)
    asyncio.create_task(tg.handle_update(bot_key, await request.json()))
    return JSONResponse({"ok": True})            # 200 IMMEDIATELY — see below
```

- Add the path prefix to `PUBLIC_PATHS` (which currently holds exact paths — this
  needs a `startswith("/telegram/webhook/")` clause beside the `/static` one).
- **Return 200 before the work.** An agent turn can run 12 tool hops; Telegram
  retries anything it doesn't get a fast 200 for, and a retry means the meal is
  logged twice. The dedupe set in §1.3 is the second half of this.
- The route sits outside `LockGate` by construction (not a lock path).

---

## 5. `webapp/combined.py` — lifespan

Start the polling tasks (polling mode) or register the webhooks (webhook mode)
inside `_lifespan`, alongside the two MCP session managers; cancel/close on
shutdown. ~10 lines. `webapp/app.py` standalone gets the same via its own
lifespan so a laptop run without `combined.py` still works.

---

## 6. Security model

Two doors, and most people only think about the first.

**Door 1 — the webhook URL, open to the internet.** Secret path segment (not real
auth; keeps scanners out of the handler) + `X-Telegram-Bot-Api-Secret-Token`
compared with `secrets.compare_digest` (this is the check that proves the caller
is Telegram). Wrong header → 403, empty body. Source-IP filtering
(`149.154.160.0/20`, `91.108.4.0/22`) is possible but reads `X-Forwarded-For`
under `forwarded_allow_ips="*"`, so it adds a layer that trusts the proxy's word;
skipped.

**Door 2 — the bot itself.** Anyone who learns the handle can DM it, and those
arrive as legitimately-signed Telegram deliveries. The numeric allowlist in §1.2
is the whole answer. In BotFather also set `/setjoingroups` → Disable and
`/setprivacy` → Enable.

**If a bot token leaks:** the holder can read messages you send THAT bot
(`getUpdates`, or re-point `setWebhook`) and send messages as it. They CANNOT
write to the journal — the allowlist lives in this app, not in Telegram. Four
tokens means the blast radius of one is one domain, the same reasoning that keeps
`WIDGET_TOKEN` separate from `BACKUP_TOKEN`. Rotate with BotFather `/revoke`.

**What no configuration fixes:** bot chats are never end-to-end encrypted. Message
text sits in plaintext on Telegram's servers and in cloud chat history on every
signed-in device — outside Google auth, outside the knock. This is a marginal
change, not a categorical one: journal prose already goes to Anthropic's API on
every turn by design. The realistic threats are Telegram account takeover (fix:
cloud password / 2FA) and someone picking up an unlocked phone (which is exactly
what `LockGate` guards and Telegram routes around). Mitigations: reply TTL
(§1.5), and keeping the journal bot's read-back thin.

---

## 7. Failure modes to build against

1. **Concurrent turns in one chat.** `run_turn` appends to a shared list in
   `chat._CONVERSATIONS`. Two messages three seconds apart race and corrupt the
   transcript into an invalid tool_use/tool_result pairing, which the API then
   400s on forever. A per-chat `asyncio.Lock` held for the whole turn. This is the
   one genuinely new failure mode the feature introduces — the browser can't
   easily produce it.
2. **Webhook timeout → duplicate writes.** §4 + §1.3.
3. **Redeploy amnesia.** `_CONVERSATIONS` is in memory. Invisible in a browser
   panel; in Telegram the chat log is still on screen, so the bot appears to
   forget mid-conversation. Ship without persistence, add `settings`-table
   transcript storage only if it annoys.
4. **Runaway spend.** `MAX_TOOL_HOPS` bounds a turn; add a per-chat daily turn
   counter to bound a loop.
5. **Long/empty/non-text messages.** Cap input length before the model; voice,
   photo and document updates get a one-liner, not silence.

---

## 8. Env vars

```
TELEGRAM_MODE=webhook|polling|off        # default off
TELEGRAM_TOKEN_JOURNAL=…                 # any subset — unset bots don't start
TELEGRAM_TOKEN_INTAKE=…
TELEGRAM_TOKEN_NOTES=…
TELEGRAM_TOKEN_TRAINER=…
TELEGRAM_ALLOWED_CHAT_IDS=123456789      # numeric; empty = talks to nobody
TELEGRAM_WEBHOOK_SECRET=…                # openssl rand -hex 32
TELEGRAM_REPLY_TTL=                      # seconds; unset = keep replies
```

Setup: BotFather `/newbot` ×4 → tokens; `/setjoingroups` Disable, `/setprivacy`
Enable on each; `@userinfobot` for the numeric id (same id for all four bots — in
a private chat `chat.id` IS your user id); set the vars in Coolify; redeploy.

---

## 9. Testing

Manual, matching the repo's existing pattern (no test suite):

1. Polling mode against `journal_dev.db`, one bot: send "test", get a reply.
2. Allowlist: put a bogus id in `TELEGRAM_ALLOWED_CHAT_IDS`, confirm silence + a
   log line; empty the var, confirm it talks to nobody.
3. Webhook: wrong secret → 403; wrong header → 403; valid → `{"ok":true}` fast.
4. Duplicate `update_id` posted twice → one write.
5. Two messages in quick succession → two coherent replies, transcript intact.
6. Each bot reaches only its own tools (ask the intake bot to save a recipe → it
   should say where that lives, not fail).
7. Boot invariant: add a fake unprefixed tool to `mcp`, confirm startup fails.

---

## 10. Phasing

- **Phase 1** — `telegram.py`, webhook + polling, allowlist, one bot (`intake` —
  smallest surface, most frequent use), typing indicator, `/new`.
- **Phase 2** — the other three bots, sibling blocks, write-chip links,
  `setMyCommands`, reply TTL.
- **Phase 2.5 (unplanned, done)** — rendering. See §11.
- **Phase 3** — voice notes. The real prize for a journal on a phone, and the
  reason to keep the door open: Anthropic's API takes no audio, so this needs a
  transcription service (Whisper API most likely). That is a SECOND external
  network call — the repo has exactly one (`notes_geocode`) and argues hard for
  it. Defensible, belongs in `webapp/`, never in `server.py`, and deserves its own
  decision.
- **Not planned** — photos → `featured_image_url` (Telegram file URLs embed the
  bot token and expire; needs a real blob-storage decision first — note this is
  only about STORING one; estimating a meal from a photo is done, §11); inline
  keyboards; group chats.

---

## 11. Docs to update in the same change

- **CLAUDE.md** — the surface-partition story gains a third member. The
  "two MCP servers, one process" section and the `webapp/chat.py` file note both
  describe a two-way split that becomes a six-way one; the derived-by-prefix rule
  and the boot invariant belong beside the existing `CONNECTOR_HIDDEN_TOOLS`
  paragraph. Add `webapp/telegram.py` to Files.
- **README.md** — a Telegram section: BotFather steps, env vars, the allowlist,
  webhook vs polling.
- **.env.example** — the block from §8, with the fail-closed note on the allowlist.

---

## 12. Appendix: why Telegram and not WhatsApp

WhatsApp is the daily-driver app, which is the strongest argument any capture
tool can have — a journal you don't open isn't a journal. It still loses here,
and for one reason that is specific to THIS plan rather than a general knock.

### The decisive one: four bots needs four phone numbers

A WhatsApp Business Platform sender IS a phone number, and a number registered on
the Business Platform **cannot also be used in the normal WhatsApp app**. So four
separate handles means four provisioned, verified numbers — spare SIMs or VoIP
numbers, each registered through Meta Business Manager. Telegram's four handles
are four `/newbot` commands, free, about a minute each.

Collapsing to ONE WhatsApp bot with keyword routing ("intake: burrito") is
possible, but that discards exactly the thing this plan is for: four separate
chats with four separate threads and four small tool surfaces.

### The rest of the ledger

| | Telegram | WhatsApp Cloud API |
|---|---|---|
| Setup | BotFather, ~1 min/bot | Meta Business Manager, app, system-user token, number registration, webhook verify token; an afternoon, and it periodically breaks on token/policy churn |
| Cost | free | user-initiated service messages free; business-initiated template messages priced per message (country-dependent). Verify current rates — Meta's pricing has changed repeatedly |
| Proactive messages | send any time | only within 24h of the user's last message; outside that, **pre-approved templates only**. Kills a spontaneous nightly nudge or morning briefing unless templated |
| Multiple surfaces | trivial | one number each |
| Typing indicator | `sendChatAction`, clean | more limited; verify current Cloud API support |
| Formatting | plain text (this plan) or HTML | limited `*bold*` / `_italic_` |
| Already on your phone | new app | **yes — the real pro** |
| Mis-send risk | bot chats in a near-empty app | bot chats sit among family/friend threads. One wrong tap sends journal prose to a group chat |
| Encryption | not E2E | consumer chats are E2E, but **Cloud API messages are decrypted and processed on Meta's servers** — the E2E advantage mostly evaporates for a bot, and the data holder becomes Meta rather than Telegram |

The unofficial route (`whatsapp-web.js`, Baileys — drive your OWN number by
automating WhatsApp Web) is the only way to get the personal-number experience.
It is against WhatsApp's terms with a real ban risk, breaks on protocol changes,
and needs a headless Chromium + Node runtime inside a slim Python image. Not for
something holding a journal.

### The hedge, and it's cheap

The adoption argument is real and can't be settled by reasoning — it's settled by
whether the bots get used. So build for it: keep `telegram.py` split as

```
_run(agent, chat_key, text) -> (reply_text, chips)     # transport-agnostic core
_telegram_*                                            # thin wire adapter
```

The agent bridge, the per-chat locks, the dedupe, the allowlist and the command
handling are all transport-independent; only sending, receiving and webhook
validation are Telegram-shaped. That is maybe 20 lines of extra discipline now,
and it makes a later WhatsApp adapter — or an SMS one — a bounded job rather than
a rewrite.

**Recommendation:** Telegram for the four-bot design as planned. If it turns out
you never open Telegram, port the adapter to a single WhatsApp number and accept
keyword routing. Don't start on WhatsApp: paying the four-number tax before
knowing whether the split is even the right shape is the expensive order to do
this in.

*(Meta's pricing, verification requirements and Cloud API features change often
and this reflects knowledge as of early 2026 — worth confirming against current
docs before acting on the cost and typing-indicator rows.)*

---

## 11. Where this actually stands (2026-08-24)

Written at the end of the session that built it, for whoever picks it up next.

### Done and live in the code

| | Notes |
|---|---|
| All four bots | `journal`, `intake`, `notes`, `trainer`; tool lists derived from the prefixes |
| Webhook + polling | polling refuses to start if a webhook exists (see below) |
| Fail-closed allowlist | numeric chat id; empty = talks to nobody |
| Per-chat lock, update dedupe | the two correctness traps from §7 |
| `/new` `/help` `/whoami` | registered via `setMyCommands` |
| Typing indicator, reply TTL, write-chip links | §1.5, §1.6 |
| Boot-time partition check | `chat.assert_tool_partition()` |
| **Rich rendering** | markdown → native tables/lists/quotes/checkboxes/collapsibles |
| **Photos** | plate → estimate; bytes dropped, nothing stored |
| **Places** | tappable inline map block (replaced an earlier `sendVenue`) |
| **Nudges** | `TELEGRAM_NUDGES=bot@HH:MM`, model may reply `SKIP` |

### Verified live, against the real bots

Polling delivery, agent turns, tool calls writing to the DB, markdown tables in a
real reply, nudge firing, nudge declining with `SKIP`, map rendering and tapping
through to Google/Apple/Bing/OSM, collapsibles rendering.

### NOT verified

- **Production webhook delivery.** Everything above was exercised in POLLING mode
  against `journal_dev.db`. The route is unit-tested (403 on bad secret/header,
  200 on good) and `setWebhook` registers correctly, but no real Telegram → prod
  delivery has happened. First message after deploy is the test. If it's silent:
  check `PUBLIC_URL` is exact and slash-free, then the container log for
  `webhook mode, bots: …`, then `getWebhookInfo` for the real domain.
- **Voice** (phase 3) — untouched, still needs a transcription decision.
- **Streaming** — `sendRichMessageDraft` takes a `draft_id`; repeated calls with
  the same id are accepted, and `sendRichMessage` finalizes. What's NOT known is
  whether the draft updates in place and whether finalizing leaves a DUPLICATE
  message. If it duplicates, streaming costs a doubled message every turn and
  isn't worth it. That one observation is the whole blocker.

### Open chores (human, not code)

- `/setjoingroups` → **Disable** on all four bots in BotFather. Not load-bearing
  (`_authorized` rejects group chats anyway — a group id is negative and can't
  equal the sender's) but it closes the door at Telegram's end.
- **Separate dev bot tokens.** Polling now REFUSES to run when a webhook exists,
  so local dev against the production tokens will simply stop. Four more bots
  from BotFather is a minute and removes the question permanently.

### API facts that cost real time to find

None of these are in the docs; all were found by probing the live API, and each
was invisible until something rendered wrong.

1. **Telegram silently ignores unknown fields.** A probe that "accepts" a
   parameter proves NOTHING about whether it does anything. This burned us twice:
   `parse_mode` on a rich block (accepted, dropped, literal `<b>` tags in the
   chat) and three `details` label variants that all "worked" when only one did.
   The only reliable test is a real send and a look at the render.
2. **Rich blocks format via `entities`, never `parse_mode`.** Moot now that the
   markdown path does the parsing, which is most of why that path is better.
3. **`rich_message.markdown` and `rich_message.blocks` are mutually exclusive**,
   blocks winning silently. That's why a map is its own message.
4. **`<details><summary>` is the one HTML form the rich markdown honours.**
   GitHub's `> [!NOTE]` degrades to a plain quote; `:::details` prints literally.
5. **Block types that exist**: `paragraph`, `heading` (+`size` 1-3), `list`
   (+`ordered`, `items:[{blocks:[…]}]` — a `text` field parses and sends EMPTY),
   `table` (`cells:[[{text, is_header}]]`), `divider` (needs content beside it),
   `details` (`summary` + `blocks`), `map` (`location.latitude/longitude`, long
   form only), `collage`, `slideshow`, `anchor` (+`name`).
6. **`sendRichMessageDraft` needs `draft_id`.** Omitting it returns the
   misleading `RANDOM_ID_INVALID`.

### Deliberately rejected

- **Reply keyboards** — they replace the on-screen keyboard, hostile for a bot
  you mostly type prose at.
- **Command scopes** — single user, no admins to differentiate.
- **Hand-built rich block trees** — was implemented, then deleted (~100 lines of
  table/list parsing plus UTF-16 entity offsets) once `rich_message.markdown`
  proved to render better than the parser ever did.
- **`sendVenue`** — worked, but the `map` block does the same job, opens the same
  map-app chooser, and looks like part of the conversation.
- Payments, Stars, games, stickers, business/secretary mode, managed bots, guest
  bots, bot-to-bot, attachment menu, web login widget.

### Ideas parked, with reasons

- **Inline keyboards** for one-tap corrections (`✏️ fix` / `🗑 delete` under a
  logged item). The highest everyday value of anything not built — the main
  friction in a food log is a wrong estimate, and fixing one currently means
  typing a sentence. `callback_data` is 64 bytes (an item id fits); every press
  needs `answerCallbackQuery` or the client spins.
- **Deep linking** — `t.me/<bot>?start=entry_412` would let an app page link INTO
  a bot conversation about that thing, closing the capture-here/read-there loop
  in the other direction. 64-char parameter, arrives as `/start entry_412`.
- **Mini App via the menu button** — opens the PWA inside Telegram; Telegram signs
  `initData` with the bot token, so it could authenticate without Google OAuth in
  a webview. Real work, real payoff, deserves its own plan.
- **Inline mode** — `@<notes-bot> chicken` in any chat to search recipes and
  send one to a friend. The only feature that exposes this data outside the
  user's own chats, so it needs a deliberate yes.
- **`/settings`** — the docs list it as an expected global command alongside
  `/start` and `/help`, and nudge times and targets are things you'd plausibly
  want to change from the phone.
- **A test bot + Telegram's test environment** (`/bot<token>/test/METHOD`) — this
  session put ~20 junk messages in a real chat reverse-engineering the rich
  message API. That belongs somewhere else next time.
