"""
Telegram bots — a third front end on the agent loop in webapp/chat.py.

The browser panel and the MCP connectors already drive the same `@mcp.tool()`
functions; this adds a phone-native way in. Four bots, four handles, four
separate chats, each narrowed to one domain (see chat.py's `_AGENTS`):

    journal → people + entries      intake → the eating log
    notes   → notes & collections   trainer → the trainer instance

**This is not a new architecture.** Every turn goes through `chat.run_turn`, so
the rule that governs the project holds unchanged: no LLM in server.py, the model
does the judgment, the server stays the deterministic data layer. No new pip
dependency (httpx is already here), no schema change, no second process.

A bot whose token is unset simply does not start, so the four ship one at a time.
MODE is `webhook` (prod), `polling` (dev — the only mode that can talk to a
laptop's journal_dev.db) or `off`, the default: this feature is off unless
configured, exactly as the chat surface is off unless ANTHROPIC_API_KEY is set.

Security lives in two places and only one of them is the webhook. See `_authorized`.
"""

import asyncio
import base64
import hashlib
import logging
import os
import re
import time
from typing import Optional

import httpx

import chat
import data
import server

log = logging.getLogger("telegram")


def _setup_logging() -> None:
    """Make this module's own INFO lines visible, and make sure httpx's are NOT.

    Two halves, and the second is the load-bearing one. Under uvicorn the root
    logger has no handler, so `log.info` at boot ("webhook registered", "polling",
    which bots came up) goes nowhere and a deploy looks silent either way. But
    reaching for a global logging.basicConfig(INFO) to fix that would be a
    CREDENTIAL LEAK: httpx logs every request line at INFO, and a Bot API URL
    carries the token in its PATH — so every call would write
    `POST https://api.telegram.org/bot<TOKEN>/sendMessage` into the deploy log,
    where tokens are exactly what you don't want sitting. So this raises only our
    own logger, and pins httpx's down whatever anyone else configures later.
    """
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(levelname)s [telegram] %(message)s"))
        log.addHandler(h)
    log.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

API = "https://api.telegram.org"

# bot key → (token env var, chat.py agent name). The keys are the URL segment in
# the webhook path, so keep them lowercase and boring.
BOTS = {
    "journal": ("TELEGRAM_TOKEN_JOURNAL", "tg_journal"),
    "intake":  ("TELEGRAM_TOKEN_INTAKE",  "tg_intake"),
    "notes":   ("TELEGRAM_TOKEN_NOTES",   "tg_notes"),
    "trainer": ("TELEGRAM_TOKEN_TRAINER", "tg_trainer"),
}

MODE = (os.environ.get("TELEGRAM_MODE") or "off").strip().lower()
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
# Seconds after which the bot deletes its OWN replies; unset = keep them. See `_send`.
REPLY_TTL = int(os.environ.get("TELEGRAM_REPLY_TTL") or 0)
# Telegram's own cap is 4096 characters per message.
MAX_MSG = 4096
# Cap on what we hand the model from one message — a paste of a whole article is a
# runaway turn, not a journal entry.
MAX_INPUT = 4000


def _tokens() -> dict:
    """Configured bots only: bot key → token. A token is a credential, so this is
    read from the environment on each call rather than cached in a module global
    that ends up in a traceback."""
    out = {}
    for key, (env, _agent) in BOTS.items():
        if tok := (os.environ.get(env) or "").strip():
            out[key] = tok
    return out


def _allowed_chat_ids() -> frozenset:
    """Numeric Telegram ids permitted to talk to any bot. Parsed leniently (blanks
    and junk dropped) but never widened: an unparseable var yields an empty set,
    which by `_authorized` means the bots talk to NOBODY."""
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
    out = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            log.warning("TELEGRAM_ALLOWED_CHAT_IDS: ignoring non-numeric %r", part)
    return frozenset(out)


def enabled() -> bool:
    """Whether the feature is on at all. Requires a mode, at least one token, AND a
    working chat surface — a bot with no ANTHROPIC_API_KEY behind it would accept
    messages and answer every one with a setup error."""
    return MODE in ("webhook", "polling") and bool(_tokens()) and chat.ENABLED


# --------------------------------------------------------------------------- #
# Authorization — the most important code in the feature
# --------------------------------------------------------------------------- #

def _authorized(msg: dict) -> bool:
    """Whether this message may reach the agent loop. FAIL CLOSED: an unset
    allowlist talks to nobody.

    This is deliberately the OPPOSITE of server.AllowlistMiddleware, where an empty
    ALLOWED_EMAILS means authless dev. There, Google auth still stands behind it.
    Here there is nothing behind it — the webhook is in PUBLIC_PATHS (Telegram
    carries no session cookie) and never crosses the MCP wire, so neither RequireAuth
    nor AllowlistMiddleware runs. This check is the ONLY identity gate on a path that
    writes to the journal, which is why an empty var must mean silence rather than
    "open to everyone who found the handle".

    Keyed on the NUMERIC id, never `from.username`: usernames are changeable and
    recyclable, the id is assigned by Telegram and a sender cannot spoof it. The
    from == chat equality means "a private chat with that person" and rules out a
    group the bot was added to (which carries a negative chat id) — belt to the
    braces of BotFather's /setjoingroups Disable.
    """
    allowed = _allowed_chat_ids()
    if not allowed:
        return False
    chat_id = (msg.get("chat") or {}).get("id")
    from_id = (msg.get("from") or {}).get("id")
    return chat_id in allowed and from_id == chat_id


# --------------------------------------------------------------------------- #
# Telegram API
# --------------------------------------------------------------------------- #

_client: Optional[httpx.AsyncClient] = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=10.0))
    return _client


async def _api(bot: str, method: str, **params):
    """One Telegram Bot API call. Returns the `result` payload, or None on failure —
    every caller here is a side effect (send a message, set a webhook) and none of
    them should be able to take a turn down, so failures are logged and swallowed.
    The token never reaches the log: it's in the URL, so only `method` is named."""
    token = _tokens().get(bot)
    if not token:
        return None
    try:
        r = await _http().post(f"{API}/bot{token}/{method}", json=params)
        body = r.json()
    except Exception as e:
        log.warning("%s/%s failed: %s", bot, method, e)
        return None
    if not body.get("ok"):
        log.warning("%s/%s error: %s", bot, method, body.get("description"))
        return None
    return body.get("result")


# The ONLY tags Telegram renders. Anything else in an HTML-mode message is a 400,
# which is why _html() is a whitelist rather than a pass-through.
_TAGS = ("b", "strong", "i", "em", "u", "s", "code", "pre", "blockquote")
_TAG_RE = re.compile(r"&lt;(/?(?:" + "|".join(_TAGS) + r"))&gt;", re.I)
_LINK_RE = re.compile(r'&lt;a href=(?:"|&quot;)(https?://[^"\s&]+)(?:"|&quot;)&gt;', re.I)
_CLOSE_A_RE = re.compile(r"&lt;/a&gt;", re.I)

# Markdown the MODEL writes, which we render — see `_inline`. It writes markdown
# because that is what a language model produces without being fought; asking for
# raw HTML tags meant a list of allowed tags in the prompt and a model that
# occasionally reached for markdown anyway.
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_MD_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S)
_MD_ITAL = re.compile(r"(?<![\*\w])\*(?=\S)([^*]+?)(?<=\S)\*(?!\*)", re.S)
_MD_CODE = re.compile(r"`([^`]+)`")


def _html(text: str) -> str:
    """Make `text` safe for parse_mode=HTML while keeping the handful of tags the
    model is allowed to use.

    Escape-then-restore, rather than trusting the model's output. Blanket-escaping
    would turn its <b> into visible &lt;b&gt;; blanket-trusting means one bare "&"
    in "mac & cheese" — or one unknown tag — returns a 400 that eats the whole
    reply, which is exactly the MarkdownV2 failure this was supposed to avoid. So:
    escape everything, then un-escape only the whitelist.

    This can still emit UNBALANCED tags if the model opens one and never closes it,
    which Telegram also rejects — hence the plain-text retry in `_send`. HTML is the
    safer parse mode, not a safe one.
    """
    out = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    out = _TAG_RE.sub(r"<\1>", out)
    out = _LINK_RE.sub(r'<a href="\1">', out)
    return _CLOSE_A_RE.sub("</a>", out)


def _inline(text: str) -> str:
    """One line of the model's markdown as Telegram HTML.

    Runs AFTER _html has escaped the text, so the tags this emits are the only ones
    in the string and a stray "<" in the prose can't become markup. Deliberately
    just the four inline forms a log ever needs — bold a number, italicise an aside,
    code a value, link a page. Anything fancier is prose.
    """
    out = _html(text)
    out = _MD_LINK.sub(r'<a href="\2">\1</a>', out)
    out = _MD_CODE.sub(r"<code>\1</code>", out)
    out = _MD_BOLD.sub(r"<b>\1</b>", out)
    return _MD_ITAL.sub(r"<i>\1</i>", out)


# --------------------------------------------------------------------------- #
# Rich messages — the model's markdown as native Telegram structure
# --------------------------------------------------------------------------- #

_H_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_UL_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_HR_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$")
_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_SEP_RE = re.compile(r"^[\s|:-]+$")


# --------------------------------------------------------------------------- #
# Rich messages — the model's markdown, parsed by Telegram
# --------------------------------------------------------------------------- #

# Block-level markdown, used ONLY to decide whether a reply deserves the rich
# format. The rendering itself is Telegram's: `rich_message.markdown` takes the
# text as written. An earlier version of this file hand-built the block tree —
# ~100 lines of table/list/heading parsing plus UTF-16 entity offsets — which was
# all reimplementation of a parser the API already has, and which could only ever
# produce the four block types it knew about. Handing over the markdown gets block
# quotes, task lists, spoilers and the rest for free.
_H_RE = re.compile(r"^#{1,3}\s+\S", re.M)
_ROW_RE = re.compile(r"^\s*\|.+\|\s*$", re.M)
_UL_RE = re.compile(r"^\s*[-*+]\s+\S", re.M)
_OL_RE = re.compile(r"^\s*\d+[.)]\s+\S", re.M)
_QUOTE_RE = re.compile(r"^\s*>\s+\S", re.M)
# <details><summary>…</summary>…</details> is the ONE HTML form Telegram's rich
# markdown honours (GitHub's `> [!NOTE]` degrades to a plain quote, and `:::details`
# prints literally — both checked). It has to be detected here as well as the block
# forms: a collapsible wrapping ordinary prose has no heading, table or list in it,
# so without this the reply would fall through to the TEXT tier, where _html escapes
# the tags and the user sees literal <details> in their chat.
_DETAILS_RE = re.compile(r"<details\b", re.I)


def _rich(text: str):
    """A `rich_message` payload for `text`, or None if it's ordinary prose.

    Returning None for prose is the point, not a limitation. A rich message is a
    heavier object than a text message, and wrapping "Logged the burrito — 900 cal"
    in one buys nothing; the format earns its place when there is a TABLE, HEADING,
    LIST or QUOTE to render — the read-back case ("what did I eat this week", a
    workout plan) that reads badly as prose today.

    Inline emphasis alone does NOT qualify: a sentence with a bold number is still
    a sentence, and the plain-text tier renders **bold** perfectly well via _inline.
    """
    if any(r.search(text) for r in (_H_RE, _ROW_RE, _UL_RE, _OL_RE, _QUOTE_RE,
                                    _DETAILS_RE)):
        return {"markdown": text}
    return None


async def _send(bot: str, chat_id: int, text: str) -> None:
    """Deliver one reply, in the richest form it actually needs.

    Three tiers, each falling back to the next, because a rejected message is a LOST
    message and this path carries the only copy of what the model just said:

      1. RICH, when the model produced a table/heading/list/quote (`_rich`). One
         object, no 4096-char chunking, and the read-back answers that used to
         arrive as a wall of prose arrive as a table.
      2. HTML text, for ordinary prose — inline markdown rendered by `_inline`.
      3. PLAIN text, tags stripped, if Telegram rejects the markup.

    The tiers exist because each is more capable and more brittle than the one under
    it. Rich messages are a newer, stricter API (an unsupported block, or a list
    whose items are shaped wrong, is a 400); HTML can be unbalanced; plain text
    cannot fail. Worse-looking beats missing, every time.
    """
    text = text.strip() or "(no reply)"

    if rich := _rich(text):
        sent = await _api(bot, "sendRichMessage", chat_id=chat_id, rich_message=rich)
        if sent:
            _schedule_expiry(bot, chat_id, sent)
            return
        log.warning("%s: rich send rejected — falling back to text", bot)

    for chunk in _chunks(text):
        sent = await _api(bot, "sendMessage", chat_id=chat_id, text=_inline(chunk),
                          parse_mode="HTML", disable_web_page_preview=True)
        if sent is None:
            log.warning("%s: HTML send rejected — retrying as plain text", bot)
            sent = await _api(bot, "sendMessage", chat_id=chat_id,
                              text=_strip_tags(_inline(chunk)),
                              disable_web_page_preview=True)
        _schedule_expiry(bot, chat_id, sent)


def _schedule_expiry(bot: str, chat_id: int, sent) -> None:
    """Arm the reply-TTL delete for a message we just sent, if TTL is on."""
    if sent and REPLY_TTL:
        asyncio.create_task(_expire(bot, chat_id, sent.get("message_id")))


def _strip_tags(text: str) -> str:
    """Plain-text form of a reply whose markup Telegram refused: drop the tags, keep
    the words. An <a href="url">label</a> becomes "label: url", since the URL is the
    half that can't be recovered from the label."""
    text = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"\2: \1", text, flags=re.I | re.S)
    return re.sub(r"</?[a-z][^>]*>", "", text, flags=re.I)


def _chunks(text: str) -> list:
    """Split at MAX_MSG, preferring a paragraph break, then a line break, then a
    space, so a split lands between thoughts rather than mid-word."""
    out = []
    while len(text) > MAX_MSG:
        window = text[:MAX_MSG]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut < MAX_MSG // 2:      # no sensible break — hard-split rather than loop
            cut = MAX_MSG
        out.append(text[:cut].strip())
        text = text[cut:].lstrip()
    out.append(text)
    return [c for c in out if c]


async def _expire(bot: str, chat_id: int, message_id: Optional[int]) -> None:
    """Delete one of our own replies after REPLY_TTL seconds.

    Telegram is a transport; the record lives in SQLite. This HALVES the at-rest
    exposure rather than eliminating it — a bot can reliably delete only its OWN
    outgoing messages, so the user's typed messages stay in the cloud thread. Worth
    having, not worth describing as privacy.
    """
    if not message_id:
        return
    await asyncio.sleep(REPLY_TTL)
    await _api(bot, "deleteMessage", chat_id=chat_id, message_id=message_id)


async def _typing(bot: str, chat_id: int) -> None:
    """Hold the "typing…" indicator for the length of a turn. It expires after 5
    seconds, so it's refreshed every 4. This is the whole reason a 20-second
    multi-tool turn reads as thinking rather than as a broken bot."""
    try:
        while True:
            await _api(bot, "sendChatAction", chat_id=chat_id, action="typing")
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


# --------------------------------------------------------------------------- #
# Photos — a plate, estimated, without storing anything
# --------------------------------------------------------------------------- #

# Anthropic's per-image ceiling is 5MB of base64; Telegram's largest PhotoSize is
# normally well under that, but a cap belongs on anything that comes off the wire.
MAX_IMAGE_BYTES = 3_500_000


async def _photo_content(bot: str, msg: dict):
    """A Telegram photo as Anthropic content blocks, or None if it can't be fetched.

    Photos are NOT stored, and that is what makes this cheap. Telegram's file URLs
    embed the bot token and expire, so keeping one as an item's featured image would
    need real blob storage and a decision to go with it — but ESTIMATING a meal
    needs the bytes only for the length of one turn. So they're fetched, passed to
    the model inline, and dropped. Nothing new in the DB, no new external service.

    The largest PhotoSize is the last in the array. The caption rides along as the
    text block, so "half of this" reaches the model with the picture.
    """
    sizes = msg.get("photo") or []
    if not sizes:
        return None
    info = await _api(bot, "getFile", file_id=sizes[-1]["file_id"])
    if not info or not info.get("file_path"):
        log.warning("%s: getFile failed for a photo", bot)
        return None
    token = _tokens().get(bot)
    try:
        r = await _http().get(f"{API}/file/bot{token}/{info['file_path']}")
        r.raise_for_status()
        data = r.content
    except Exception as e:
        log.warning("%s: photo download failed: %s", bot, e)
        return None
    if len(data) > MAX_IMAGE_BYTES:
        log.warning("%s: photo too large (%d bytes)", bot, len(data))
        return None

    ext = info["file_path"].rsplit(".", 1)[-1].lower()
    media = {"png": "image/png", "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
    caption = (msg.get("caption") or "").strip()
    return [
        {"type": "image", "source": {"type": "base64", "media_type": media,
                                     "data": base64.b64encode(data).decode()}},
        {"type": "text", "text": caption or "(photo, no caption)"},
    ]


# --------------------------------------------------------------------------- #
# Update handling
# --------------------------------------------------------------------------- #

# Seen update ids, bounded. Dedupe is CORRECTNESS, not tidiness: Telegram retries
# any update it doesn't get a fast 200 for, and a retried update logs the burrito
# twice. It also kills replay of a captured update.
_SEEN: dict = {}
_SEEN_MAX = 512

# One lock per chat. `chat.run_turn` appends to a shared list in _CONVERSATIONS;
# two messages three seconds apart would interleave and corrupt the transcript into
# an invalid tool_use/tool_result pairing, which the API then 400s on forever. This
# is the one genuinely new failure mode the feature introduces — a browser can't
# easily produce it, since the panel won't send while a turn is in flight.
_LOCKS: dict = {}


def _lock_for(chat_id: int) -> asyncio.Lock:
    return _LOCKS.setdefault(chat_id, asyncio.Lock())


def _fresh(update_id: Optional[int]) -> bool:
    """False if we've already handled this update. Ids are kept with a timestamp and
    trimmed oldest-first so the set can't grow without bound in a long-lived
    process."""
    if update_id is None:
        return True
    if update_id in _SEEN:
        return False
    _SEEN[update_id] = time.time()
    if len(_SEEN) > _SEEN_MAX:
        for old in sorted(_SEEN, key=_SEEN.get)[: len(_SEEN) - _SEEN_MAX]:
            _SEEN.pop(old, None)
    return True


def _session_id(bot: str, chat_id: int) -> str:
    """One thread per (bot, chat). Each gets chat.py's Pacific-day rollover for
    free, so a thread that spans midnight doesn't drag yesterday's date forward."""
    return f"tg:{bot}:{chat_id}"


async def handle_update(bot: str, update: dict) -> None:
    """Handle one Telegram update. Never raises: this runs as a detached task off a
    webhook that has already returned 200, so an exception here has nowhere to go
    but the log."""
    try:
        if not _fresh(update.get("update_id")):
            log.info("%s: duplicate update %s ignored", bot, update.get("update_id"))
            return
        # `message` only. Edited messages, channel posts, callbacks and the rest are
        # ignored on purpose: an edited "2 beers" would have to un-log the first one,
        # and there is no such operation.
        msg = update.get("message")
        if not isinstance(msg, dict):
            return

        if not _authorized(msg):
            # Silent by design — a reply confirms the bot is live and worth probing.
            # Logged, because the numeric id is exactly what you'd need to allowlist
            # yourself from a new account.
            log.warning("%s: rejected chat_id=%s from_id=%s", bot,
                        (msg.get("chat") or {}).get("id"), (msg.get("from") or {}).get("id"))
            return

        chat_id = msg["chat"]["id"]

        # A photo is a turn like any other, just with an image block in front of the
        # caption. Handled before the text check because a photo legitimately has no
        # `text` at all.
        if msg.get("photo"):
            content = await _photo_content(bot, msg)
            if content is None:
                await _send(bot, chat_id, "Couldn't fetch that photo — try again?")
                return
            await _run(bot, chat_id, content)
            return

        text = (msg.get("text") or "").strip()
        if not text:
            # Photos are handled above. Voice is the one that's still missing and
            # the one most wanted on a phone, but it needs a transcription service —
            # a second external dependency, and its own decision. Say so rather than
            # sitting silent, which reads as a broken bot.
            await _send(bot, chat_id, "Text and photos work — voice isn't wired up yet.")
            return

        if text.startswith("/"):
            await _command(bot, chat_id, text)
            return

        if len(text) > MAX_INPUT:
            text = text[:MAX_INPUT]
            log.info("%s: truncated a %d-char message", bot, len(msg["text"]))

        await _run(bot, chat_id, text)
    except Exception:
        log.exception("%s: update handling failed", bot)


async def _command(bot: str, chat_id: int, text: str) -> None:
    """The three slash commands. Everything else gets pointed at /help rather than
    being passed to the model, which would answer as if it were prose."""
    cmd = text.split()[0].lstrip("/").split("@")[0].lower()
    agent = BOTS[bot][1]
    if cmd in ("new", "reset", "clear"):
        chat.reset(agent, _session_id(bot, chat_id))
        await _send(bot, chat_id, "Fresh thread.")
    elif cmd == "whoami":
        await _send(bot, chat_id, f"chat id: {chat_id}")
    elif cmd in ("help", "start"):
        await _send(bot, chat_id, _help_text(bot))
    else:
        await _send(bot, chat_id, f"Unknown command. {_help_text(bot)}")


def _help_text(bot: str) -> str:
    """What this bot owns and what its siblings do — the same four-way split the
    model is told about in chat.py's `_tg_blurb`, read off the same dict so the two
    can't drift."""
    others = "\n".join(f"  {k} bot — {v}" for k, v in chat._TG_DOMAINS.items() if k != bot)
    return (f"I handle {chat._TG_DOMAINS[bot]}.\n\nThe others:\n{others}\n\n"
            "/new starts a fresh thread. /whoami shows your chat id.")


async def _turn(bot: str, chat_id: int, text) -> tuple:
    """Drive one agent turn to completion. Returns (reply, chips, error).

    Split out of `_run` because the SCHEDULED nudge path needs exactly this and
    then makes a different decision about it — a nudge may decline to say anything
    (see `_nudge`), where a reply to a person never should.

    Assumes the caller holds the chat's lock.
    """
    agent = BOTS[bot][1]
    typing = asyncio.create_task(_typing(bot, chat_id))
    parts, preamble, chips, called, error = [], "", [], [], None
    try:
        async for ev in chat.run_turn(agent, _session_id(bot, chat_id), text):
            if ev["type"] == "text":
                parts.append(ev["text"])
            elif ev["type"] == "tool":
                called.append(ev["name"])
                if ev.get("kind") == "write":
                    chips.append(ev)
                # A tool call means everything said so far was the model narrating
                # its way TOWARDS an answer ("Let me check past burritos first..."),
                # not the answer. The browser panel can show that as progress because
                # it streams each hop with a chip between them; one Telegram message
                # can't — the hops concatenate into a run-on monologue with no space
                # at the seam ("log both items.No history yet."), burying the actual
                # answer in the last sentence. So each tool call DISCARDS the
                # narration before it, and what's left at the end is the reply.
                if parts:
                    preamble = "".join(parts)   # fallback, see below
                    parts.clear()
            elif ev["type"] == "error":
                error = ev.get("message") or "something went wrong"
    except Exception as e:
        log.exception("%s: turn failed", bot)
        error = str(e)
    finally:
        typing.cancel()

    # Normally the post-tool text IS the answer. A model that calls a tool and then
    # says nothing is the one case where dropping the narration would leave an empty
    # reply, so fall back to it rather than send "(no reply)".
    reply = "".join(parts).strip() or preamble.strip()

    # One line per turn. Without it a WORKING bot and one that never received the
    # message look identical in the log — both silent — which is the first thing you
    # need to tell apart when something isn't answering. Lengths and tool names only,
    # never the text: this log carries journal prose and meals otherwise.
    size = f"{len(text)}ch" if isinstance(text, str) else "photo"
    log.info("%s: turn in=%s out=%dch tools=[%s]%s", bot, size, len(reply),
             ",".join(called), " ERROR" if error else "")
    return reply, chips, error


async def _run(bot: str, chat_id: int, text) -> None:
    """One user turn, start to finish: hold the chat's lock so concurrent messages
    can't interleave, run it, send what came back as a single message."""
    async with _lock_for(chat_id):
        reply, chips, error = await _turn(bot, chat_id, text)
        if error:
            await _send(bot, chat_id, f"Sorry — {error}")
            return
        await _send(bot, chat_id, reply + _links(chips))
        await _places(bot, chat_id, chips)


async def _places(bot: str, chat_id: int, chips: list) -> None:
    """Send a tappable MAP for any place the turn just saved or read back.

    A `map` block rather than sendVenue: it renders as a real inline map instead of
    a compact card, and tapping it still offers Google/Apple/Bing/OSM — so it does
    everything the venue did and looks like part of the conversation. It has to be
    its OWN message regardless, because `rich_message` honours `markdown` OR
    `blocks` and never both, so a map can't be folded into the reply above it.

    Collections already require coordinates on a `location` field — the webapp's map
    view can't plot an address — so the data for a pin is sitting there, and an
    address rendered as text is the one thing a phone can't act on.

    Only the notes bot has locations, and only a turn that touched an item can know
    one; the chips say which items those were, and the values are read from the DB
    rather than from anything the model wrote, so a hallucinated coordinate can't
    open in a maps app. At most two, since a map is a whole message.
    """
    if bot != "notes":
        return
    sent = 0
    for item_id in [c["item_id"] for c in chips if c.get("item_id")]:
        for place in data.item_locations(item_id):
            label = place["title"]
            if place["address"] and place["address"] not in label:
                label = f"{label} — {place['address']}"
            await _api(bot, "sendRichMessage", chat_id=chat_id, rich_message={"blocks": [
                {"type": "paragraph", "text": label},
                {"type": "map", "zoom": 15,
                 "location": {"latitude": place["lat"], "longitude": place["lng"]}},
            ]})
            sent += 1
            if sent >= 2:
                return


def _links(chips: list) -> str:
    """A trailing line of links to what the turn WROTE, deduped, reads omitted.

    This is the capture-here/read-there split that the tools' own `url` returns
    answer for the connector, and it matters more here than anywhere: Telegram is
    the most different screen from the app there is. `chat._tool_chip` already
    computes a relative href for the browser panel; PUBLIC_URL + /app makes it
    absolute, the same job `base_path` does in a template. With PUBLIC_URL unset
    (dev) there is no link to give, and a dead one is worse than none.
    """
    by_href = {}
    for c in chips:
        if href := c.get("href"):
            # Grouped by href, not deduped on it: two intake_log calls both point at
            # /food, and dropping the second turned "a burrito and a beer" into a
            # line that named only the burrito. One line per destination, naming
            # everything that landed there.
            summaries = by_href.setdefault(href, [])
            if c["summary"] not in summaries:
                summaries.append(c["summary"])
    # A real anchor, not a pasted URL: parse_mode=HTML means the label can carry the
    # link, so the line reads "Logged burrito, Logged beer" and taps through.
    out = [f'<a href="{url}">{", ".join(s)}</a>'
           for href, s in by_href.items() if (url := server._app_url(href))]
    return ("\n\n" + "\n".join(out)) if out else ""


# --------------------------------------------------------------------------- #
# Scheduled nudges — the one thing this transport can do that the others can't
# --------------------------------------------------------------------------- #

# A connector and the PWA are both PULL: they answer when opened. Telegram is the
# only surface here that can start the conversation, which is what makes a log that
# reminds you different from one you have to remember to open.
#
# A nudge is a real agent turn, not a canned string — the same split as everywhere.
# The server decides WHEN (a clock), the model decides WHETHER and WHAT, reading the
# day out of the DB with its own tools. That's why each prompt ends by authorizing
# SILENCE: a reminder that fires whether or not it has anything to say is the kind
# of notification people turn off within a week, and the model can see the day's
# numbers well enough to judge. It lands in the normal thread, so a reply to a nudge
# just continues the conversation.
_NUDGE_PROMPTS = {
    "intake": (
        "[scheduled check-in — the user did not send this] Read today's intake and "
        "the profile's targets. Send ONE short line only if something is genuinely "
        "worth saying: nothing logged at all today, well short on a target with the "
        "day nearly over, or a target already well past. Don't recap a day that's "
        "going fine and don't ask an open question."),
    "journal": (
        "[scheduled check-in — the user did not send this] If nothing has been "
        "written for today, invite them to say how the day went, in one short warm "
        "line. If the day already has entries, say nothing."),
    "trainer": (
        "[scheduled check-in — the user did not send this] Read the briefing. Say "
        "one short line only if there's a session planned for today that hasn't "
        "been done, or a muscle group is well overdue. Otherwise say nothing."),
    "notes": (
        "[scheduled check-in — the user did not send this] If the inbox has a "
        "handful of unfiled notes that clearly cluster, suggest ONE collection for "
        "them in a short line. Otherwise say nothing."),
}

# What the model replies when it decides a nudge isn't warranted. Checked as a
# prefix, since a model asked for one word occasionally adds a sentence explaining
# itself — which the user must not receive either way.
_SKIP = "SKIP"

# Two rules every nudge prompt ends with. The length one is a NUMBER on purpose:
# "one short line" produced 914 characters in the first live test, because short is
# uncalibrated — the model has no idea what a phone notification should weigh, and
# an unprompted message that needs scrolling is worse than no message. The silence
# rule is the other half; see the block comment above.
_NUDGE_MAX = 200

_NUDGE_RULE = (f"\n\nHARD LIMIT: under {_NUDGE_MAX} characters, one sentence, no preamble and "
               f"no sign-off — this arrives as a phone notification the user did not "
               f"ask for. If there is nothing worth sending, reply with exactly "
               f"{_SKIP} and nothing else — it will not be delivered.")


def _nudges() -> list:
    """Parse TELEGRAM_NUDGES: comma-separated `bot@HH:MM`, Pacific, e.g.
    `intake@16:00,journal@21:00`. Unset = no nudges, which is the default: a bot
    that messages you first is opt-in, not something that starts happening because
    you deployed."""
    out = []
    for part in (os.environ.get("TELEGRAM_NUDGES") or "").split(","):
        part = part.strip()
        if not part:
            continue
        bot, _, hhmm = part.partition("@")
        bot, hhmm = bot.strip().lower(), hhmm.strip()
        if bot not in BOTS or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", hhmm):
            log.warning("TELEGRAM_NUDGES: ignoring %r (want bot@HH:MM, bot one of %s)",
                        part, "/".join(BOTS))
            continue
        if bot not in _NUDGE_PROMPTS:
            log.warning("TELEGRAM_NUDGES: no prompt defined for %r", bot)
            continue
        out.append((bot, hhmm))
    return out


async def _nudge(bot: str, hhmm: str) -> None:
    """Fire one scheduled nudge at every allowlisted chat."""
    for chat_id in sorted(_allowed_chat_ids()):
        # The same lock a user turn takes: a nudge landing mid-conversation would
        # interleave with it and corrupt the transcript, exactly as two fast
        # messages would. It waits its turn instead.
        async with _lock_for(chat_id):
            reply, chips, error = await _turn(bot, chat_id,
                                              _NUDGE_PROMPTS[bot] + _NUDGE_RULE)
            if error:
                log.warning("%s: nudge %s failed: %s", bot, hhmm, error)
                continue
            if not reply or reply.strip().upper().startswith(_SKIP):
                log.info("%s: nudge %s — nothing to say", bot, hhmm)
                continue
            # The budget lives in the PROMPT, which is a request rather than a
            # guarantee — so notice when it's ignored instead of trusting it. Not
            # truncated: cutting a sentence in half to hit a number sends something
            # worse than the thing that was too long. This is the signal to reword
            # the prompt, and the only reason a length drift would ever be noticed
            # (the message content is deliberately never logged).
            if len(reply) > 2 * _NUDGE_MAX:
                log.warning("%s: nudge %s ran to %dch (budget %d) — prompt may need "
                            "rewording", bot, hhmm, len(reply), _NUDGE_MAX)
            await _send(bot, chat_id, reply + _links(chips))
            log.info("%s: nudge %s sent (%dch)", bot, hhmm, len(reply))


async def _nudge_loop(schedule: list) -> None:
    """Fire each scheduled nudge once per Pacific day, at or after its time.

    Two deliberate properties. Times are PACIFIC, read from server.current_clock()
    like every other date in this project, so a nudge doesn't drift with the
    container's UTC clock or with DST. And anything already past when the process
    starts is marked as fired: a restart at 6pm must not deliver the 4pm nudge two
    hours late — a reminder at the wrong time is worse than one that never came,
    and a crash-looping container would otherwise nudge on every boot.
    """
    fired = {}
    boot = server.current_clock()
    for entry in schedule:
        if entry[1] <= boot["time"]:
            fired[entry] = boot["date"]
    log.info("nudges scheduled: %s",
             ", ".join(f"{b}@{t}" for b, t in schedule) or "none")
    while True:
        try:
            await asyncio.sleep(30)
            clock = server.current_clock()
            for entry in schedule:
                if fired.get(entry) == clock["date"]:
                    continue
                if clock["time"] >= entry[1]:
                    fired[entry] = clock["date"]     # mark BEFORE running, so a
                    await _nudge(*entry)             # failure can't retry in a loop
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("nudge loop error")
            await asyncio.sleep(60)


# --------------------------------------------------------------------------- #
# Transport — webhook (prod) or long-polling (dev)
# --------------------------------------------------------------------------- #

def webhook_path(bot: str) -> str:
    """Mount-relative path for a bot's webhook.

    The last segment is DERIVED from the secret by a one-way hash, never the secret
    itself, and that distinction is the whole point. Every request to this URL is
    written to uvicorn's access log verbatim:

        POST /telegram/webhook/intake/<segment> 200 OK

    so whatever sits in that slot is public to anyone who can read a deploy log.
    Putting WEBHOOK_SECRET there directly — which this did at first — published the
    value the route authenticates with, and a forger who reads one log line can then
    post updates that pass the header check and set the sender id to yours, i.e.
    write to the journal as you. "The path isn't the real auth" is only true while
    the path isn't ALSO the real auth.

    Hashing keeps both properties: the segment stays unguessable (scanners don't
    reach the handler) while the secret behind the header stays unpublished. Salted
    per bot so one bot's URL says nothing about another's.
    """
    seg = hashlib.sha256(f"{WEBHOOK_SECRET}:{bot}".encode()).hexdigest()[:32]
    return f"/telegram/webhook/{bot}/{seg}"


def _public_webhook_url(bot: str) -> Optional[str]:
    """The absolute URL Telegram should POST to. The UI is mounted at /app (see
    webapp/combined.py), and this route lives on the UI app — so the webhook is
    under /app too. `server._app_url` is the one place that join is written."""
    return server._app_url(webhook_path(bot))


async def _register(bot: str) -> None:
    """Point a bot at our webhook, but only if it isn't already there: getWebhookInfo
    first, setWebhook only on a difference. A redeploy is then a no-op rather than a
    re-register, which keeps Telegram from resetting the pending-update queue every
    time the container restarts."""
    url = _public_webhook_url(bot)
    if not url:
        log.error("%s: PUBLIC_URL unset — cannot register a webhook", bot)
        return
    info = await _api(bot, "getWebhookInfo") or {}
    if info.get("url") == url:
        log.info("%s: webhook already registered", bot)
        return
    ok = await _api(bot, "setWebhook", url=url, secret_token=WEBHOOK_SECRET,
                    allowed_updates=["message"], drop_pending_updates=True)
    log.info("%s: setWebhook %s", bot, "ok" if ok else "FAILED")


async def _poll(bot: str) -> None:
    """Long-poll one bot. Dev only, and the reason it exists: webhook mode needs a
    public URL, so it cannot talk to a laptop running against journal_dev.db.

    Telegram refuses getUpdates while a webhook is set, so polling has to clear one
    first — and that is precisely the footgun this guards against. A bot's token is
    the same string everywhere, so starting a laptop in polling mode with the
    PRODUCTION token would unregister the deployment's webhook and quietly take
    delivery over. Production then goes silent with nothing in its own logs to say
    why, because the messages stop arriving rather than failing.

    So a webhook that already exists is treated as someone else's: refuse to poll,
    say whose URL it is, and stop. Dev never registers webhooks, so anything found
    here belongs to a real deployment. Clearing it stays possible, but only as a
    deliberate act (deleteWebhook by hand), never as a side effect of starting a
    dev server. The real fix is separate bot tokens for local work — BotFather
    costs a minute per bot — and the log says so.
    """
    info = await _api(bot, "getWebhookInfo") or {}
    if url := info.get("url"):
        log.error("%s: NOT polling — a webhook is registered at %s. Polling would "
                  "delete it and hijack that deployment's messages. Use a separate "
                  "dev bot token, or clear it deliberately with deleteWebhook.",
                  bot, url)
        return
    await _api(bot, "deleteWebhook", drop_pending_updates=True)
    offset = None
    log.info("%s: polling", bot)
    while True:
        try:
            updates = await _api(bot, "getUpdates", offset=offset, timeout=30,
                                 allowed_updates=["message"])
            for u in updates or []:
                offset = u["update_id"] + 1
                await handle_update(bot, u)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Never let a transient network blip end the loop; back off so a hard
            # outage doesn't become a hot retry.
            log.warning("%s: poll error: %s", bot, e)
            await asyncio.sleep(5)


_TASKS: list = []


async def startup() -> None:
    """Bring the bots up. Called from the app lifespan; a no-op unless configured.

    The tool-partition check runs FIRST and is allowed to raise: the bots' tool
    lists are derived from the domain prefixes, and if that derivation has stopped
    being exact, a bot is quietly missing a tool. Better a failed boot than a
    feature that looks fine and can't log your lunch.
    """
    _setup_logging()
    if not enabled():
        if MODE in ("webhook", "polling") and not chat.ENABLED:
            log.warning("TELEGRAM_MODE=%s but ANTHROPIC_API_KEY is unset — bots off", MODE)
        return
    if MODE == "webhook" and not WEBHOOK_SECRET:
        log.error("webhook mode needs TELEGRAM_WEBHOOK_SECRET — bots off")
        return
    if not _allowed_chat_ids():
        # Not fatal: the bots start and refuse everyone, which is the documented
        # fail-closed behaviour. Loud, because it is almost certainly a mistake.
        log.warning("TELEGRAM_ALLOWED_CHAT_IDS is empty — the bots will talk to "
                    "nobody. /whoami can't help you here (it's behind the same "
                    "check); get your numeric id from @userinfobot.")

    await chat.assert_tool_partition()

    bots = sorted(_tokens())
    for bot in bots:
        await _api(bot, "setMyCommands", commands=[
            {"command": "new", "description": "Start a fresh thread"},
            {"command": "help", "description": "What this bot handles"},
            {"command": "whoami", "description": "Show your chat id"},
        ])
        if MODE == "webhook":
            await _register(bot)
        else:
            _TASKS.append(asyncio.create_task(_poll(bot)))
    if schedule := _nudges():
        _TASKS.append(asyncio.create_task(_nudge_loop(schedule)))
    log.info("%s mode, bots: %s", MODE, ", ".join(bots))


async def shutdown() -> None:
    """Cancel the polling tasks and close the HTTP client. Webhooks are left
    registered on purpose — a redeploy is a restart, not a teardown, and
    unregistering would drop every message sent while the container is down."""
    for t in _TASKS:
        t.cancel()
    for t in _TASKS:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    _TASKS.clear()
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None
