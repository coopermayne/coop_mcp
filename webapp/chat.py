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
    "\n\nYou are running inside the trainer's own web app, on the /trainer page — the "
    "user is talking to you on their phone, usually mid-workout. Beside this chat is a "
    "live PLAN CARD that shows today's routine and lets them tap sets done; your job is "
    "to fill and adjust that plan. Be concise — they're between sets.\n\n"
    "Building a routine: when they ask for a session ('give me a push day', 'what "
    "should I do today?'), FIRST call get_fitness_briefing (and get_exercise_history "
    "for the lifts you're picking weights for), THEN start_workout_plan with concrete "
    "target weights and reps. Program a full session — aim for ~21-26 working sets "
    "total, roughly 6-8 exercises at 3-4 sets each. Hit the muscle groups that are "
    "due/rested; respect "
    "profile injuries and any niggles in recent-session notes. Where last time was easy "
    "(RPE <=7-8, clean reps) nudge the weight up; where it was a grind (RPE 9-10 or "
    "missed reps) hold or back off. Keep their staple lifts so the progression data "
    "stays comparable — vary exercises only modestly, not every session.\n\n"
    "During the workout: they'll log most sets by tapping the card, but if they tell "
    "you ('did 10 at 100, felt like an 8') use complete_set. If a machine is taken or "
    "broken, use swap_exercise for the same muscle group (pass the right target weight "
    "for the substitute). add_to_plan to tack on more; finish_workout when they're "
    "done. Call get_workout_plan if you need to see the current state.\n\n"
    "Technique questions: answer with a clear, specific walkthrough — setup, the "
    "movement, tempo, what it should feel like, and the common mistakes to avoid — "
    "drawing on the exercise's saved technique_notes/common_mistakes/cautions (via the "
    "exercises tool) and your own knowledge. If you give durable cues for an exercise, "
    "consider saving them with save_exercise so they're there next time."
)

# The agent registry. Each entry binds a chat surface to one FastMCP instance and
# an optional set of tool names to exclude. Extend, don't special-case.
_AGENTS = {
    "journal": {"server": server.mcp,         "exclude": _JOURNAL_DRINK_TOOLS, "blurb": _JOURNAL_BLURB},
    "trainer": {"server": server.trainer_mcp, "exclude": set(),                "blurb": _TRAINER_BLURB},
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
            "To add or correct their details — role, summary, notes, email, phone, "
            "address, new aliases, group membership — call save_person with "
            f"person_id={row['id']} (UPDATE, don't create a new person). Any journal "
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

# Write tools mutate the DB; everything else is read-only retrieval. Used only to
# label the tool chips the UI shows, never to gate execution.
_WRITE_TOOLS = {
    "add_journal_entry", "update_entry", "save_person", "link_mentions",
    "merge_people", "delete_record",
    "log_workout", "update_workout", "update_set", "save_exercise",
    "log_bodyweight", "update_profile",
    "start_workout_plan", "complete_set", "swap_exercise", "add_to_plan",
    "finish_workout",
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


def _system_blocks(agent: str, context: dict | None = None) -> list[dict]:
    """System prompt = the bound server's own model-facing instructions plus a
    surface-specific blurb (cached), then a live Pacific-time anchor and an optional
    page context (both uncached, after the breakpoint so they never bust the cache as
    the clock advances or the user moves between profile pages)."""
    cfg = _AGENTS[agent]
    clock = server.current_clock()
    blocks = [
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
    # Trainer plan tools (the /trainer page).
    elif name == "start_workout_plan":
        href = "/trainer"
        summary = "Built today's routine"
    elif name == "complete_set":
        href = "/trainer"
        summary = "Logged a set"
    elif name == "swap_exercise":
        href = "/trainer"
        summary = "Swapped an exercise"
    elif name == "add_to_plan":
        href = "/trainer"
        summary = "Added to the plan"
    elif name == "finish_workout":
        href = "/trainer"
        summary = "Finished the workout"
    elif name == "get_workout_plan":
        href = "/trainer"
        summary = "Loaded the plan"

    return {"name": name, "summary": summary, "kind": kind, "href": href}


# --------------------------------------------------------------------------- #
# The agent loop — an async generator of SSE-ready event dicts
# --------------------------------------------------------------------------- #

async def run_turn(agent: str, session_id: str, user_text: str, context: dict | None = None):
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
    convo_key = (agent, session_id, context["key"]) if context else (agent, session_id)
    messages = _CONVERSATIONS.setdefault(convo_key, [])
    messages.append({"role": "user", "content": user_text})

    try:
        for _hop in range(MAX_TOOL_HOPS):
            async with client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=_system_blocks(agent, context),
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


def reset(agent: str, session_id: str, context: dict | None = None) -> None:
    key = (agent, session_id, context["key"]) if context else (agent, session_id)
    _CONVERSATIONS.pop(key, None)
