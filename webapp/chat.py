"""
In-app AI chat: the web app as an MCP *client*.

This is the one place the web app writes *prose* (the journal). Browse pages
(`app.py`) stay read-only; drinks now have their own direct-entry form (no AI).
Here an Anthropic-powered agent loop drives the SAME `@mcp.tool()` functions that
Claude Desktop calls over the connector — in-process, no MCP transport. The split
that governs the whole project holds: the model does the judgment ("which Tom?"),
`server.py` stays a deterministic data layer with no LLM inside it.

**Toolset-scoped agents.** Each chat surface is bound to ONE FastMCP instance and a
lean slice of its tools, so a conversation loads only what it needs (smaller tool
surface = less latency, the same reason the MCP servers are split):

  - `journal` — the journal server's people/entry tools, MINUS the drink tools
    (drinks are direct data entry now). Lives as a slide-in panel on the journal
    page.
  - `trainer` — the trainer server's workout tools. Its own page, linked from the
    workout page. (Wired here; the page is a later round.)

The system prompt and tool definitions are not hand-written: they're lifted
straight from the live server — each instance's `instructions` is the system
prompt, and each tool's docstring + signature become the Anthropic tool schema via
`list_tools()`. Change a docstring in `server.py` and the chat updates with it.

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

# Drinks moved to a direct-entry form, so the journal chat drops these three tools
# from the journal server — keeping the conversation focused on capture + people.
_JOURNAL_DRINK_TOOLS = {"log_drinks", "get_drink_summary", "update_drink"}

# A short, surface-specific addendum appended to each server's own instructions.
_JOURNAL_BLURB = (
    "\n\nYou are running inside the journal's own web app, in a chat panel on the "
    "journal page — the user is talking to you directly on their phone or laptop. "
    "Be concise and warm. After you capture something, say briefly what you "
    "recorded. Use the tools to both capture entries and answer recall questions "
    "about people and past days. Drink logging is handled by a separate "
    "direct-entry form, not here — you have no drink tools, so if drinks come up, "
    "just point the user to the Drinking page rather than trying to log them."
)
_TRAINER_BLURB = (
    "\n\nYou are running inside the trainer's own web app, on a dedicated chat page "
    "linked from the workout page — the user is talking to you mid- or post-workout "
    "on their phone. Be concise. Log sets as they happen and surface the context "
    "(recency, last weights, notes) the briefing gives you."
)

# The agent registry. Each entry binds a chat surface to one FastMCP instance and
# an optional set of tool names to exclude. Extend, don't special-case.
_AGENTS = {
    "journal": {"server": server.mcp,         "exclude": _JOURNAL_DRINK_TOOLS, "blurb": _JOURNAL_BLURB},
    "trainer": {"server": server.trainer_mcp, "exclude": set(),                "blurb": _TRAINER_BLURB},
}


def is_agent(name: str) -> bool:
    return name in _AGENTS


# Lazily-built, then cached per agent for the process: Anthropic tool schemas +
# a name→fn dispatch map, both derived from the live server.
_TOOLS: dict[str, list] = {}
_DISPATCH: dict[str, dict] = {}
_client = None

# In-memory conversation store, keyed by (agent, session chat id). Each value is
# the Anthropic `messages` list (incl. tool_use/tool_result blocks).
_CONVERSATIONS: dict[tuple, list] = {}

# Write tools mutate the DB; everything else is read-only retrieval. Used only to
# label the tool chips the UI shows, never to gate execution.
_WRITE_TOOLS = {
    "add_journal_entry", "update_entry", "save_person", "link_mentions",
    "merge_people", "delete_record",
    "log_workout", "update_workout", "update_set", "save_exercise",
    "log_bodyweight", "update_profile",
}


async def _ensure_tools(agent: str):
    """Build the Anthropic tool list + dispatch map for `agent` once, from that
    server's list_tools() minus its excluded names. A cache_control breakpoint on
    the last tool caches the whole (large, static) tool-schema block across turns."""
    if agent in _TOOLS:
        return
    cfg = _AGENTS[agent]
    exclude = cfg["exclude"]
    tool_objs = await cfg["server"].list_tools()
    tools, dispatch = [], {}
    for t in tool_objs:
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


def _system_blocks(agent: str) -> list[dict]:
    """System prompt = the bound server's own model-facing instructions plus a
    surface-specific blurb (cached), then a live Pacific-time anchor (uncached,
    after the breakpoint so it never busts the cache as the clock advances)."""
    cfg = _AGENTS[agent]
    clock = server.current_clock()
    return [
        {
            "type": "text",
            "text": cfg["server"].instructions + cfg["blurb"],
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": (
                f"Current moment — date {clock['date']} ({clock['weekday']}), "
                f"time {clock['time']} {clock['timezone']}. Anchor 'today'/"
                "'yesterday' to this before defaulting or computing any date."
            ),
        },
    ]


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
        summary = "Linked a mention to a person"
    elif name == "merge_people":
        href = "/people"
        summary = "Merged two people"
    elif name == "delete_record":
        summary = f"Deleted a {g('kind', 'record')}"
    elif name in ("search_entries", "get_entry"):
        summary = "Searched the journal"
    elif name in ("list_people", "get_person_history", "get_related_people"):
        summary = "Looked up people"
    elif name in ("get_briefing", "list_pending_mentions"):
        summary = "Loaded journal context"
    # Trainer tools (used on the trainer page).
    elif name == "log_workout":
        href = "/workouts"
        summary = "Logged a workout"
    elif name in ("update_workout", "update_set"):
        href = "/workouts"
        summary = "Updated the workout"
    elif name == "log_bodyweight":
        href = "/workouts"
        summary = "Logged bodyweight"
    elif name == "save_exercise":
        summary = "Saved an exercise"
    elif name == "get_fitness_briefing":
        summary = "Loaded training context"

    return {"name": name, "summary": summary, "kind": kind, "href": href}


# --------------------------------------------------------------------------- #
# The agent loop — an async generator of SSE-ready event dicts
# --------------------------------------------------------------------------- #

async def run_turn(agent: str, session_id: str, user_text: str):
    """Run one user turn to completion for `agent`, yielding event dicts as they
    happen:
      {"type": "text", "text": ...}      streamed assistant prose
      {"type": "tool", ...}              a tool was called (chip payload)
      {"type": "done"}                   turn finished
      {"type": "error", "message": ...}  fatal error; turn aborts
    Conversation state for (agent, session_id) is updated in place so the next
    turn has full context (including the tool_use/tool_result blocks)."""
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
    messages = _CONVERSATIONS.setdefault((agent, session_id), [])
    messages.append({"role": "user", "content": user_text})

    try:
        for _hop in range(MAX_TOOL_HOPS):
            async with client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=_system_blocks(agent),
                tools=tools,
                messages=messages,
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


def reset(agent: str, session_id: str) -> None:
    _CONVERSATIONS.pop((agent, session_id), None)
