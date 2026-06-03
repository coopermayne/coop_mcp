"""
In-app AI chat: the web app as an MCP *client*.

This is the one place the web app writes. Browse pages (`app.py`) stay read-only;
here an Anthropic-powered agent loop drives the SAME `@mcp.tool()` functions that
Claude Desktop calls over the connector — in-process, no MCP transport. The split
that governs the whole project holds: the model does the judgment ("which Tom?"),
`server.py` stays a deterministic data layer with no LLM inside it.

Scope is the **journal** FastMCP instance only (journal + people + drinking — 16
tools). The trainer is a separate server and a later phase.

The system prompt and tool definitions are not hand-written: they're lifted
straight from the live server — `mcp.instructions` is the system prompt, and each
tool's docstring + signature become the Anthropic tool schema via `list_tools()`.
Change a docstring in `server.py` and this chat updates with it.

Disabled unless ANTHROPIC_API_KEY is set; model defaults to Sonnet 4.6
(CHAT_MODEL overrides). Conversations live in memory, keyed by a session id —
single-user app, lost on restart, which is fine for v1.
"""

import asyncio
import json
import os

import server  # the journal FastMCP instance + the tool functions it wraps

MODEL = os.environ.get("CHAT_MODEL", "claude-sonnet-4-6")
ENABLED = bool(os.environ.get("ANTHROPIC_API_KEY"))
MAX_TOKENS = 4096
# Safety rail on the agent loop: a single user turn shouldn't fan out into an
# unbounded chain of tool calls. Generous enough for capture + a few lookups.
MAX_TOOL_HOPS = 12

# Lazily-built, then cached for the process: Anthropic tool schemas + a name→fn
# dispatch map, both derived from the live server. Async because list_tools() is.
_TOOLS: list[dict] | None = None
_DISPATCH: dict | None = None
_client = None

# In-memory conversation store, keyed by the session's chat id. Each value is the
# Anthropic `messages` list (user/assistant turns incl. tool_use/tool_result).
_CONVERSATIONS: dict[str, list] = {}


# --------------------------------------------------------------------------- #
# Tool wiring — lifted from the live journal server
# --------------------------------------------------------------------------- #

# Write tools mutate the DB; everything else is read-only retrieval. Used only to
# label the tool chips the UI shows (writes get a prominent chip + a link to the
# affected page; reads render quietly), never to gate execution.
_WRITE_TOOLS = {
    "add_journal_entry", "update_entry", "log_drinks", "update_drink",
    "save_person", "link_mentions", "merge_people", "delete_record",
}


async def _ensure_tools():
    """Build the Anthropic tool list + dispatch map once, from mcp.list_tools().
    A cache_control breakpoint on the last tool caches the whole (large, static)
    tool-schema block across turns."""
    global _TOOLS, _DISPATCH
    if _TOOLS is not None:
        return
    tool_objs = await server.mcp.list_tools()
    tools, dispatch = [], {}
    for t in tool_objs:
        tools.append({
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.parameters,
        })
        dispatch[t.name] = t.fn
    if tools:
        tools[-1]["cache_control"] = {"type": "ephemeral"}
    _TOOLS, _DISPATCH = tools, dispatch


def _client_singleton():
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic
        _client = AsyncAnthropic()
    return _client


def _system_blocks() -> list[dict]:
    """System prompt = the server's own model-facing instructions (cached) plus a
    live Pacific-time anchor (uncached, after the breakpoint so it never busts the
    cache as the clock advances)."""
    clock = server.current_clock()
    return [
        {
            "type": "text",
            "text": (
                server.mcp.instructions
                + "\n\nYou are running inside the journal's own web app as a chat "
                "assistant — the user is talking to you directly on their phone or "
                "laptop. Be concise and warm. After you capture something, say "
                "briefly what you recorded. Use the tools to both capture and to "
                "answer recall questions about people, entries, and drinking."
            ),
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
    elif name == "log_drinks":
        n = r.get("logged", g("standard_drinks"))
        href = "/drinking"
        summary = f"Logged {server_num(n)} standard drink" + ("" if n == 1 else "s")
    elif name == "update_drink":
        href = "/drinking"
        summary = "Updated a drink"
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
    elif name == "get_drink_summary":
        summary = "Checked drinking stats"
    elif name in ("get_briefing", "list_pending_mentions"):
        summary = "Loaded journal context"

    return {"name": name, "summary": summary, "kind": kind, "href": href}


def server_num(x) -> str:
    """Compact number for chip labels: 2 not 2.0, 1.5 stays 1.5."""
    try:
        f = float(x)
        return f"{f:g}"
    except (TypeError, ValueError):
        return str(x)


# --------------------------------------------------------------------------- #
# The agent loop — an async generator of SSE-ready event dicts
# --------------------------------------------------------------------------- #

async def run_turn(session_id: str, user_text: str):
    """Run one user turn to completion, yielding event dicts as they happen:
      {"type": "text", "text": ...}      streamed assistant prose
      {"type": "tool", ...}              a tool was called (chip payload)
      {"type": "done"}                   turn finished
      {"type": "error", "message": ...}  fatal error; turn aborts
    Conversation state for `session_id` is updated in place so the next turn has
    full context (including the tool_use/tool_result blocks)."""
    if not ENABLED:
        yield {"type": "error", "message": "Chat is not configured (ANTHROPIC_API_KEY unset)."}
        return
    try:
        await _ensure_tools()
        client = _client_singleton()
    except Exception as e:  # import / setup failure
        yield {"type": "error", "message": f"Chat setup failed: {e}"}
        return

    messages = _CONVERSATIONS.setdefault(session_id, [])
    messages.append({"role": "user", "content": user_text})

    try:
        for _hop in range(MAX_TOOL_HOPS):
            async with client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=_system_blocks(),
                tools=_TOOLS,
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
                fn = _DISPATCH.get(tu.name)
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


def reset(session_id: str) -> None:
    _CONVERSATIONS.pop(session_id, None)
