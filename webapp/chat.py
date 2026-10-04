"""
In-app AI chat: the web app as an MCP *client*.

This is the one place the web app writes *prose* (the journal). Browse pages
(`app.py`) stay read-only.
Here an Anthropic-powered agent loop drives the SAME `@mcp.tool()` functions that
Claude Desktop calls over the connector — in-process, no MCP transport. The split
that governs the whole project holds: the model does the judgment ("which Tom?"),
`server.py` stays a deterministic data layer with no LLM inside it.

**Toolset-scoped agents.** Each chat surface is bound to ONE FastMCP instance and a
lean slice of its tools, so a conversation loads only what it needs (smaller tool
surface = less latency, the same reason the MCP servers are split):

  - `journal` — the journal server's people/entry tools. Lives as a slide-in
    panel on the journal page.
  (The trainer is used through its MCP connector in Claude, not a panel here.)

The system prompt and tool definitions are not hand-written: they're lifted
straight from the live server — each instance's `instructions` is the system
prompt, and each tool's docstring + signature become the Anthropic tool schema via
`list_tools()`. Change a docstring in `server.py` and the chat updates with it.
The journal instance is not served as an MCP endpoint at all — this panel is the
only thing that drives it — so its `instructions` ARE the panel's system prompt.

Disabled unless ANTHROPIC_API_KEY is set; model defaults to Sonnet 4.6
(CHAT_MODEL overrides). Conversations live in memory, keyed by (agent, session) —
single-user app, lost on restart, which is fine for v1.
"""

import asyncio
import json
import os

import server  # the FastMCP instances + the tool functions they wrap

MODEL = os.environ.get("CHAT_MODEL", "claude-sonnet-4-6")
ENABLED = bool(os.environ.get("ANTHROPIC_API_KEY"))
MAX_TOKENS = 4096
# Safety rail on the agent loop: a single user turn shouldn't fan out into an
# unbounded chain of tool calls. Generous enough for capture + a few lookups.
MAX_TOOL_HOPS = 12

# A short, surface-specific addendum appended to each server's own instructions.
_JOURNAL_BLURB = (
    "\n\nYou are running inside the journal's own web app, in a chat panel on the "
    "journal page — the user is talking to you directly on their phone or laptop. "
    "Be concise and warm. After you capture something, say briefly what you "
    "recorded. Use the tools to both capture entries and answer recall questions "
    "about people and past days."
)


# The agent registry. A server-bound entry binds a chat surface to one FastMCP instance
# and narrows its tool list: `exclude` drops names, `include` keeps ONLY those names.
# An `instructions` key overrides the instance's own. A webapp-defined entry may
# instead carry its own `instructions` + a `tools` builder (none does today).
# Extend, don't special-case.
_AGENTS = {
    "journal":  {"server": server.mcp, "blurb": _JOURNAL_BLURB},
}


def is_agent(name: str) -> bool:
    return name in _AGENTS


def person_context(person_id: int):
    """Build a chat *context* for the journal surface pinned to one person — used by
    the chat panel on that person's profile page. Returns a dict with its own
    conversation `key` (so each person gets an isolated thread) and a `system`
    addendum, or None if the id is unknown.

    The system text tells the model which entity "this person / them / here" refers
    to and to write edits straight onto this person_id, so on a profile page the user
    can just say "her birthday is in May" or "she's now my manager" and it lands on
    the right record. Built server-side from the DB (never client-supplied prose) so
    the pinned identity can't be steered by injected text."""
    with server.db() as conn:
        row = conn.execute(
            "SELECT id, canonical_name, role FROM people WHERE id = ?",
            (person_id,),
        ).fetchone()
    if not row:
        return None
    who = row["canonical_name"] + (f" ({row['role']})" if row["role"] else "")
    return {
        "key": f"person:{row['id']}",
        "system": (
            f"The user is viewing the profile page for {who}, person_id={row['id']}. "
            "In this conversation, 'this person', 'them', 'they', 'her', 'him', and "
            "'here' refer to this person unless the user clearly names someone else. "
            "To add or correct their details — role, summary, notes, new aliases, "
            f"group membership — call save_person with person_id={row['id']} (UPDATE, "
            "don't create a new person); for contact details (emails, phones, "
            f"addresses, websites, …) call update_contact with person_id={row['id']}. "
            "Any journal "
            "entry the user dictates here should mention this person so their history "
            "links."
        ),
    }


# Lazily-built, then cached per agent for the process: Anthropic tool schemas +
# a name→fn dispatch map, both derived from the live server.
_TOOLS: dict[str, list] = {}
_DISPATCH: dict[str, dict] = {}
_client = None

# In-memory conversation store, keyed by (agent, session chat id). Each value is
# the Anthropic `messages` list (incl. tool_use/tool_result blocks).
_CONVERSATIONS: dict[tuple, list] = {}
# The Pacific date each conversation was last active on. A session id lives in the
# (long-lived) signed cookie, so a thread accumulates across days; when a new day's
# turn arrives we drop the stale transcript so day-old dates baked into the history
# can't pull "today" back to the previous day. See `_maybe_rollover`.
_CONV_DATE: dict[tuple, str] = {}


def _convo_key(agent: str, session_id: str, context: dict | None) -> tuple:
    return (agent, session_id, context["key"]) if context else (agent, session_id)


def _stamped(messages: list) -> list:
    """The transcript as sent to the model, with the latest user turn prefixed by
    today's Pacific date. The stored history stays clean (no prefix) — this only
    pins the freshest concrete date right next to the user's words, a cheap hedge
    against the model anchoring to an older date elsewhere in the context. Belt to
    the system anchor's suspenders."""
    stamp = f"[Sent on {server.today()} (Pacific)] "
    out = list(messages)
    for i in range(len(out) - 1, -1, -1):
        m = out[i]
        if m["role"] != "user":
            continue
        if isinstance(m["content"], str):
            out[i] = {**m, "content": stamp + m["content"]}
            break
        # A user-role LIST is normally the tool_result blocks fed back mid-turn —
        # not something the user "sent", and stamping one would both be meaningless
        # and stop the scan before it reached the real turn.
        blocks = m["content"]
        if isinstance(blocks, list) and blocks and _bget(blocks[0], "type") != "tool_result":
            out[i] = {**m, "content": [{"type": "text", "text": stamp.strip()}] + list(blocks)}
            break
    return out


def _maybe_rollover(key: tuple) -> None:
    """Start a fresh thread when the Pacific date has advanced since this
    conversation was last touched, so a new day never inherits the old day's dates
    from the transcript. Same-day reloads keep the thread intact."""
    cur = server.today()
    if _CONV_DATE.get(key) not in (None, cur):
        _CONVERSATIONS.pop(key, None)
    _CONV_DATE[key] = cur


# One lock per conversation, held for a whole turn. The browser path relied on
# client-side JS not sending while a turn was in flight — two tabs sharing the session cookie hit the same convo
# key and interleave their appends into an invalid tool_use/tool_result pairing
# the API then 400s on for the rest of the day. Keyed like _CONVERSATIONS;
# entries are tiny and per-session, dropped by the same rollover cadence as the
# transcript they guard (never explicitly — an asyncio.Lock is ~100 bytes).
_TURN_LOCKS: dict[tuple, asyncio.Lock] = {}


def _turn_lock(key: tuple) -> asyncio.Lock:
    return _TURN_LOCKS.setdefault(key, asyncio.Lock())


def _repair_tail(messages: list) -> None:
    """Heal a transcript a previous turn left mid-hop. If the consumer disconnects
    (phone locks, user navigates) while a tool is executing, the generator is
    cancelled between appending the assistant's tool_use turn and appending its
    tool_results — and the API rejects every subsequent request on that thread
    ("tool_use without tool_result") until /new or midnight. Before starting a
    turn, answer any dangling tool_use blocks with synthetic errored results so
    the thread is valid again; the model sees "interrupted" and moves on."""
    if not messages:
        return
    last = messages[-1]
    if last.get("role") != "assistant":
        return
    content = last.get("content") or []
    dangling = [b for b in content if _bget(b, "type") == "tool_use"]
    if not dangling:
        return
    messages.append({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": _bget(b, "id"),
         "content": json.dumps({"error": "interrupted — the user disconnected "
                                "before this tool call finished"}),
         "is_error": True}
        for b in dangling
    ]})


# Write tools mutate the DB; everything else is read-only retrieval. Used only to
# label the tool chips the UI shows, never to gate execution.
_WRITE_TOOLS = {
    "add_journal_entry", "update_entry", "reorder_entries", "save_person",
    "link_mentions", "merge_people", "update_contact",
    "journal_delete_entry",
}


async def _ensure_tools(agent: str):
    """Build the Anthropic tool list + dispatch map for `agent` once. A server-bound
    agent lifts them from its FastMCP instance's list_tools(), narrowed by its
    `include` allowlist and/or its `exclude` names;
    a webapp-defined agent would supply its own via a `tools` builder. A cache_control breakpoint
    on the last tool caches the whole (large, static) tool-schema block across turns."""
    if agent in _TOOLS:
        return
    cfg = _AGENTS[agent]
    if "tools" in cfg:
        tools, dispatch = cfg["tools"]()
    else:
        include, exclude = cfg.get("include"), cfg.get("exclude") or frozenset()
        # run_middleware=False: this is the in-process webapp surface, already behind
        # the browser session's Google auth — there is no MCP access token here, so
        # letting AllowlistMiddleware.on_message run would reject the empty email.
        # The middleware guards the MCP wire surface, which this call never touches.
        tool_objs = await cfg["server"].list_tools(run_middleware=False)
        tools, dispatch = [], {}
        for t in tool_objs:
            if include is not None and t.name not in include:
                continue
            if t.name in exclude:
                continue
            tools.append({
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.parameters,
            })
            dispatch[t.name] = t.fn
    if tools:
        tools[-1]["cache_control"] = {"type": "ephemeral"}
    _TOOLS[agent], _DISPATCH[agent] = tools, dispatch


def _client_singleton():
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic
        _client = AsyncAnthropic()
    return _client


def _system_blocks(agent: str, context: dict | None = None) -> list[dict]:
    """System prompt = the bound server's own model-facing instructions plus a
    surface-specific blurb (cached), then a live Pacific-time anchor and an optional
    page context (both uncached, after the breakpoint so they never bust the cache as
    the clock advances or the user moves between profile pages)."""
    cfg = _AGENTS[agent]
    clock = server.current_clock()
    # Server-bound agents take their system prompt from the live FastMCP instance's
    # instructions; a webapp-defined agent carries its own.
    instructions = cfg["instructions"] if "instructions" in cfg else cfg["server"].instructions
    blocks = [
        {
            "type": "text",
            "text": instructions + cfg["blurb"],
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": (
                f"Current moment — {clock['weekday']} {clock['date']}, "
                f"time {clock['time']} {clock['timezone']}. For dates use these EXACT "
                f"strings: today={clock['date']}, yesterday={clock['yesterday']}, "
                f"tomorrow={clock['tomorrow']}. Do NOT compute or shift dates yourself; "
                "resolve 'today'/'yesterday'/'tomorrow' and any bare day reference "
                "against these before defaulting or saving. This conversation may span "
                "several days — trust this line for the current date, not dates that "
                "appear earlier in the transcript."
            ),
        },
    ]
    if context and context.get("system"):
        blocks.append({"type": "text", "text": context["system"]})
    return blocks


# --------------------------------------------------------------------------- #
# Tool-call chips — a short, human label (+ optional link) per tool invocation
# --------------------------------------------------------------------------- #

def _tool_chip(name: str, args: dict, result: dict) -> dict:
    """Friendly summary of a single tool call for the UI. Writes link to the page
    where the user can see the effect; reads are labelled quietly."""
    kind = "write" if name in _WRITE_TOOLS else "read"
    href, summary = None, name.replace("_", " ")
    g = lambda k, d=None: (args or {}).get(k, d)
    r = result if isinstance(result, dict) else {}

    if name == "add_journal_entry":
        eid = r.get("entry_id")
        href = f"/entry/{eid}" if eid else "/journal"
        summary = "Saved a journal entry"
    elif name == "update_entry":
        eid = g("entry_id")
        href = f"/entry/{eid}" if eid else "/journal"
        summary = "Updated an entry"
    elif name == "save_person":
        pid = r.get("person_id")
        href = f"/person/{pid}" if pid else "/people"
        nm = g("canonical_name")
        summary = f"Saved {nm}" if nm else "Saved a person"
    elif name == "link_mentions":
        href = "/journal"
        if r.get("dismissed") and not r.get("linked"):
            summary = "Dismissed a mention"
        else:
            summary = "Linked a mention to a person"
    elif name == "merge_people":
        href = "/people"
        summary = "Merged two people"
    elif name == "journal_delete_entry":
        href = "/journal"
        summary = "Deleted an entry"
    elif name in ("search_entries", "get_entry"):
        summary = "Searched the journal"
    elif name in ("list_people", "get_person_history", "get_related_people"):
        summary = "Looked up people"
    elif name in ("get_briefing", "list_pending_mentions"):
        summary = "Loaded journal context"
    return {"name": name, "summary": summary, "kind": kind, "href": href}


# --------------------------------------------------------------------------- #
# The agent loop — an async generator of SSE-ready event dicts
# --------------------------------------------------------------------------- #

async def run_turn(agent: str, session_id: str, user_text, context: dict | None = None):
    """Run one user turn to completion for `agent`, yielding event dicts as they
    happen:
      {"type": "text", "text": ...}      streamed assistant prose
      {"type": "tool", ...}              a tool was called (chip payload)
      {"type": "done"}                   turn finished
      {"type": "error", "message": ...}  fatal error; turn aborts
    Conversation state is updated in place so the next turn has full context
    (including the tool_use/tool_result blocks). An optional `context` (see
    `person_context`) scopes the conversation to its own thread (via `context['key']`)
    and adds a page-specific system block."""
    if not ENABLED:
        yield {"type": "error", "message": "Chat is not configured (ANTHROPIC_API_KEY unset)."}
        return
    if not is_agent(agent):
        yield {"type": "error", "message": f"Unknown chat agent '{agent}'."}
        return
    try:
        await _ensure_tools(agent)
        client = _client_singleton()
    except Exception as e:  # import / setup failure
        yield {"type": "error", "message": f"Chat setup failed: {e}"}
        return

    tools, dispatch = _TOOLS[agent], _DISPATCH[agent]
    convo_key = _convo_key(agent, session_id, context)
    # The whole turn runs under the conversation's lock — a second sender (another
    # tab) queues rather than interleaving the shared transcript.
    async with _turn_lock(convo_key):
        _maybe_rollover(convo_key)  # new Pacific day → drop the stale transcript
        messages = _CONVERSATIONS.setdefault(convo_key, [])
        _repair_tail(messages)  # heal a previously interrupted turn's dangling tool_use
        messages.append({"role": "user", "content": user_text})

        try:
            for _hop in range(MAX_TOOL_HOPS):
                async with client.messages.stream(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=_system_blocks(agent, context),
                    tools=tools,
                    messages=_stamped(messages),
                ) as stream:
                    async for event in stream:
                        if (event.type == "content_block_delta"
                                and event.delta.type == "text_delta"):
                            yield {"type": "text", "text": event.delta.text}
                    final = await stream.get_final_message()

                # Persist the assistant turn verbatim (text + any tool_use blocks).
                messages.append({"role": "assistant", "content": final.content})

                tool_uses = [b for b in final.content if b.type == "tool_use"]
                if final.stop_reason != "tool_use" or not tool_uses:
                    yield {"type": "done"}
                    return

                # Execute each requested tool off the event loop (sqlite is sync), emit
                # a chip, and collect results to feed back in one user turn.
                tool_results = []
                for tu in tool_uses:
                    fn = dispatch.get(tu.name)
                    if fn is None:
                        result, is_err = {"error": f"unknown tool {tu.name}"}, True
                    else:
                        try:
                            result = await asyncio.to_thread(fn, **(tu.input or {}))
                            is_err = isinstance(result, dict) and "error" in result
                        except Exception as e:  # tool raised — let the model recover
                            result, is_err = {"error": str(e)}, True
                    yield {"type": "tool", **_tool_chip(tu.name, tu.input, result)}
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": json.dumps(result, default=str),
                        "is_error": is_err,
                    })
                messages.append({"role": "user", "content": tool_results})

            yield {"type": "text", "text": "\n\n_(stopped after too many tool steps.)_"}
            yield {"type": "done"}
        except Exception as e:
            yield {"type": "error", "message": str(e)}


def reset(agent: str, session_id: str, context: dict | None = None) -> None:
    key = _convo_key(agent, session_id, context)
    _CONVERSATIONS.pop(key, None)
    _CONV_DATE.pop(key, None)


def _bget(blk, k, default=None):
    """Read a field off a content block that may be an SDK object (assistant blocks
    from `final.content`) or a plain dict (the tool_result blocks we build)."""
    return blk.get(k, default) if isinstance(blk, dict) else getattr(blk, k, default)


def history(agent: str, session_id: str, context: dict | None = None) -> list[dict]:
    """The stored transcript for a session, rendered into UI-ready turns so a page
    reload can replay the still-active thread (the history lives server-side in
    `_CONVERSATIONS`, not in the browser). Applies the same new-day rollover as a
    send, so a stale day-old thread comes back empty rather than flashing up only to
    be cleared on the next message. Each turn is:
      {"role": "user", "text": ...}
      {"role": "assistant", "text": ..., "chips": [{summary, kind, href}, ...]}
    Tool chips are reconstructed by pairing each tool_use with its tool_result."""
    key = _convo_key(agent, session_id, context)
    _maybe_rollover(key)
    msgs = _CONVERSATIONS.get(key, [])

    # Index every tool_result by the tool_use id it answers, so a chip can carry the
    # same link/label it had live (e.g. add_journal_entry → /entry/<id>).
    results: dict[str, dict] = {}
    for m in msgs:
        if m["role"] == "user" and isinstance(m["content"], list):
            for blk in m["content"]:
                if _bget(blk, "type") == "tool_result":
                    try:
                        results[_bget(blk, "tool_use_id")] = json.loads(_bget(blk, "content") or "{}")
                    except Exception:
                        results[_bget(blk, "tool_use_id")] = {}

    turns: list[dict] = []
    for m in msgs:
        if m["role"] == "user" and isinstance(m["content"], str):
            turns.append({"role": "user", "text": m["content"]})
        elif m["role"] == "assistant":
            text_parts, chips = [], []
            for blk in m["content"]:
                t = _bget(blk, "type")
                if t == "text":
                    text_parts.append(_bget(blk, "text", ""))
                elif t == "tool_use":
                    c = _tool_chip(_bget(blk, "name"), _bget(blk, "input") or {},
                                   results.get(_bget(blk, "id"), {}))
                    chips.append({"summary": c["summary"], "kind": c["kind"], "href": c["href"]})
            turns.append({"role": "assistant", "text": "".join(text_parts), "chips": chips})
    return turns
