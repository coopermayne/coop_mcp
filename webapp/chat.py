"""
In-app AI chat: the web app as an MCP *client*.

This is the one place the web app writes *prose* (the journal). Browse pages
(`app.py`) stay read-only — including intake, whose only write path is these tools.
Here an Anthropic-powered agent loop drives the SAME `@mcp.tool()` functions that
Claude Desktop calls over the connector — in-process, no MCP transport. The split
that governs the whole project holds: the model does the judgment ("which Tom?"),
`server.py` stays a deterministic data layer with no LLM inside it.

**Toolset-scoped agents.** Each chat surface is bound to ONE FastMCP instance and a
lean slice of its tools, so a conversation loads only what it needs (smaller tool
surface = less latency, the same reason the MCP servers are split):

  - `journal` — the journal server's people/entry/intake tools. Lives as a slide-in
    panel on the journal page.
  - `trainer` — the trainer server's workout tools. Its own page, linked from the
    workout page. (Wired here; the page is a later round.)

The system prompt and tool definitions are not hand-written: they're lifted
straight from the live server — each instance's `instructions` is the system
prompt, and each tool's docstring + signature become the Anthropic tool schema via
`list_tools()`. Change a docstring in `server.py` and the chat updates with it.
The one exception is the journal surface's system prompt: the `mcp` instance's
own `instructions` are the CONNECTOR-facing subset (its people/entry tools are
hidden from MCP clients by HiddenToolsMiddleware), so this chat — which drives
the full tool set in-process, bypassing that middleware — takes
`server.JOURNAL_CHAT_INSTRUCTIONS`, the full contract, instead.

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
    "about people and past days. Food, alcohol and water are logged with intake_log, one "
    "call per item — this panel and the connector are the only ways in, so if the user "
    "mentions eating or drinking, log it."
)
_TRAINER_BLURB = (
    "\n\nYou are running inside the trainer's own web app — the user is talking to you "
    "on their phone, often mid-workout. You're reachable from two screens. On a SESSION "
    "page (/trainer/{id}) a live PLAN CARD sits beside this chat showing that session's "
    "routine and letting them tap sets done; your job there is to fill and adjust that "
    "plan. On the TRAINING page (/workouts) they see their upcoming sessions above their "
    "history — that's where a week gets planned. Be concise; they're between sets.\n\n"
    "Planning a WEEK: several sessions can be planned at once, one per day. Call "
    "start_workout_plan once per day with that day's `planned_date`, deciding each from "
    "a get_fitness_briefing whose `as_of` is that day. The briefing's `upcoming` shows "
    "what you've already programmed — read it, because muscle_recency counts completed "
    "work only and won't know Tuesday's chest work when you plan Thursday. Give every "
    "plan a `focus`: it's the only title its row has on their Training page. If they've "
    "already trained today and want 'the rest of the week', plan the remaining days "
    "only.\n\n"
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
    "done — pass its `workout_id` when it isn't the session they're standing on. Call "
    "get_workout_plan (with a `workout_id` from `upcoming` for a specific day) if you "
    "need to see the current state.\n\n"
    "Technique questions: answer with a clear, specific walkthrough — setup, the "
    "movement, tempo, what it should feel like, and the common mistakes to avoid — "
    "drawing on the exercise's saved technique_notes/common_mistakes/cautions (via the "
    "find_exercises tool) and your own knowledge. If you give durable cues for an exercise, "
    "consider saving them with save_exercise so they're there next time."
)

# --------------------------------------------------------------------------- #
# Exercise-add agent — a WEBAPP-ONLY surface that can CREATE library exercises.
# --------------------------------------------------------------------------- #
# The library is closed to the model everywhere else: server.create_exercise is
# deliberately NOT a FastMCP tool, so the journal/trainer connectors (Claude Desktop,
# phone) can never grow the catalog — only the website's trusted code path can. This
# agent is that path with an LLM in front of it: it lives here in the web app (the
# client side of the no-LLM-in-the-server line, exactly like the rest of chat.py) and
# is reachable only by the authenticated user on the library page. Its two tools are
# hand-written here rather than lifted from a server instance, precisely so creating an
# exercise stays off the MCP tool surface.

_EXERCISE_INSTRUCTIONS = (
    "You are the exercise-library assistant inside the user's training web app. Your one "
    "job: take whatever the user tells you about a NEW exercise and add a single, well-"
    "formed record to their library — filling EVERY field properly, from their info plus "
    "your own knowledge of the movement.\n\n"
    "The library is a closed catalog of strength/cardio movements. A record's fields:\n"
    "- name: the canonical movement name in Title Case (e.g. \"Bulgarian Split Squat\").\n"
    "- category: a coarse bucket — usually strength, stretching, plyometrics, or cardio "
    "(or a simple split label like push/pull/legs/core if that's how the user frames it).\n"
    "- equipment: barbell, dumbbell, machine, cable, kettlebell, body only, bands, etc.\n"
    "- force: exactly one of push | pull | static.\n"
    "- level: difficulty — exactly one of beginner | intermediate | expert.\n"
    "- mechanic: compound | isolation.\n"
    "- muscles / secondary_muscles / tertiary_muscles: the muscles worked, in three "
    "EMPHASIS tiers — primary = what the lift is FOR, secondary = real assistance, "
    "tertiary = lightly involved. Use ONLY these canonical labels: "
    + ", ".join(server.MUSCLES) + ".\n"
    "- technique_notes: a concise setup + execution walkthrough (the key cues).\n"
    "- common_mistakes: the usual form errors.\n"
    "- cautions: injury / safety caveats.\n"
    "- aliases: common alternative names this movement is searched/spoken by (AKAs), "
    "lowercased — e.g. a Romanian Deadlift gets ['rdl','stiff leg deadlift']. Fill the "
    "obvious ones from your own knowledge so the lift is findable by whatever the user "
    "calls it; don't repeat the canonical name.\n"
    "- video_link: optional, only if the user hands you a URL. Never ask for images.\n\n"
    "How to work:\n"
    "1. ALWAYS call check_library FIRST with the movement name (and any obvious variant) "
    "before creating anything. If an exact or essentially-identical entry already exists, "
    "STOP and tell the user it's already in the library, naming it — do NOT create a "
    "duplicate. A close but genuinely DIFFERENT movement in the results (e.g. they want "
    "Hack Squat and only Back Squat is on file) is fine — note it and carry on.\n"
    "2. Fill EVERY field. Use what the user told you; infer the rest from your own "
    "knowledge — the muscles and their tiers, force, level, mechanic, equipment, and a "
    "genuinely useful technique walkthrough, common mistakes, and cautions. Don't leave "
    "fields blank just because the user didn't mention them — filling them is the point.\n"
    "3. Ask a follow-up ONLY when something is genuinely ambiguous and would change the "
    "record — which variation they mean (barbell vs dumbbell), or a load-bearing detail "
    "you truly can't infer. Don't interrogate the user over things you can reason out; "
    "propose sensible values and proceed.\n"
    "4. PREVIEW before saving — do NOT call create_exercise yet. Lay out the full record "
    "for the user to review: name, category, equipment, force, level, mechanic, the three "
    "muscle tiers, aliases (AKAs), technique_notes, common_mistakes, and cautions (use a compact "
    "field-by-field layout, e.g. a markdown list or table, so every field is visible). "
    "Lead the preview with the dedup result from check_library: either confirm nothing "
    "close enough is already in the library, or name the close-but-different entries you "
    "found and why this is distinct. Then ask the user to confirm — save it as shown, or "
    "tell you what to change.\n"
    "5. SAVE ONLY ON CONFIRMATION. When the user approves, call create_exercise with "
    "exactly the previewed values; it's added to the library and their hearted FAVORITES "
    "(the superset their rotation is drawn from) automatically — not the small active "
    "rotation, which they curate deliberately. If they ask for changes, revise the preview "
    "and ask again — re-preview and re-confirm each round until they approve. Never save a "
    "version the user hasn't signed off on.\n"
    "6. After saving, confirm briefly what landed — the name, the primary muscle emphasis, "
    "and that it's in their favorites now (they can star it into the active rotation on the "
    "library page). Keep it to a couple of lines.\n\n"
    "Add ONE exercise per request unless the user clearly lists several. Be concise and "
    "practical — they're on their phone."
)


def _exercise_check(name: str) -> dict:
    """Look `name` up in the closed library so the agent never makes a duplicate: returns
    any exact/confident match (full enough to recognise) plus the closest existing
    entries by fuzzy/phonetic score."""
    with server.db() as conn:
        row = server._resolve_exercise(conn, name)
        match = None
        if row:
            match = {"exercise_id": row["id"], "name": row["name"],
                     "category": row["category"], "equipment": row["equipment"],
                     "muscles": server._muscles_for(conn, row["id"]),
                     "in_rotation": bool(row["in_rotation"]),
                     "hearted": bool(row["hearted"])}
        candidates = server._match_exercises(conn, name)
    return {"query": name, "exact_match": match, "candidates": candidates}


def _exercise_create(name: str, category=None, equipment=None, muscles=None,
                     secondary_muscles=None, tertiary_muscles=None,
                     technique_notes=None, common_mistakes=None, cautions=None,
                     force=None, level=None, mechanic=None, aliases=None,
                     video_link=None) -> dict:
    """Create the exercise through the website's trusted path (server.create_exercise),
    landing it in the user's HEARTED superset (favorites bench) — not the small rotation,
    which they curate deliberately to ~10-14 so progress on each lift is easy to track.
    Image links are intentionally omitted — this surface doesn't handle images."""
    return server.create_exercise(
        name=name, hearted=True, category=category, equipment=equipment,
        muscles=muscles, secondary_muscles=secondary_muscles,
        tertiary_muscles=tertiary_muscles, technique_notes=technique_notes,
        common_mistakes=common_mistakes, cautions=cautions, force=force, level=level,
        mechanic=mechanic, aliases=aliases, video_link=video_link)


def _exercise_tools():
    """The exercise-add agent's tool schemas + name→fn dispatch. Hand-written (not lifted
    from a FastMCP instance) so the create path stays off the MCP tool surface."""
    muscle_enum = {"type": "array", "items": {"type": "string", "enum": server.MUSCLES}}
    tools = [
        {
            "name": "check_library",
            "description": (
                "Search the existing library for a movement BEFORE creating it. Returns "
                "any exact/confident match and the closest existing entries, so you can "
                "avoid duplicates. Always call this first."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "The exercise name (or a close variant) to look up."},
                },
                "required": ["name"],
            },
        },
        {
            "name": "create_exercise",
            "description": (
                "Add one new exercise to the library (and the user's hearted favorites — "
                "the superset their rotation is drawn from, not the active rotation itself). "
                "Fill every field you reasonably can. Only call this after check_library "
                "shows it's not already on file."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "Canonical movement name, Title Case."},
                    "category": {"type": "string",
                                 "description": "Coarse bucket, e.g. strength, cardio, stretching, plyometrics."},
                    "equipment": {"type": "string",
                                  "description": "e.g. barbell, dumbbell, machine, cable, kettlebell, body only."},
                    "force": {"type": "string", "enum": ["push", "pull", "static"]},
                    "level": {"type": "string", "enum": ["beginner", "intermediate", "expert"]},
                    "mechanic": {"type": "string", "enum": ["compound", "isolation"]},
                    "muscles": {**muscle_enum, "description": "PRIMARY muscles — what the lift is for."},
                    "secondary_muscles": {**muscle_enum, "description": "Real assistance muscles."},
                    "tertiary_muscles": {**muscle_enum, "description": "Lightly-involved muscles."},
                    "technique_notes": {"type": "string",
                                        "description": "Concise setup + execution walkthrough / key cues."},
                    "common_mistakes": {"type": "string", "description": "The usual form errors."},
                    "cautions": {"type": "string", "description": "Injury / safety caveats."},
                    "aliases": {"type": "array", "items": {"type": "string"},
                                "description": "Common alternative names this movement is "
                                "searched/spoken by (AKAs), e.g. ['rdl','stiff leg deadlift']. "
                                "Lowercased; don't repeat the canonical name."},
                    "video_link": {"type": "string",
                                   "description": "Optional URL, only if the user provides one."},
                },
                "required": ["name"],
            },
        },
    ]
    dispatch = {"check_library": _exercise_check, "create_exercise": _exercise_create}
    return tools, dispatch


# The agent registry. A server-bound entry binds a chat surface to one FastMCP instance
# and an optional set of tool names to exclude; an `instructions` key overrides the
# instance's own (the journal instance's are connector-facing — see the module
# docstring). A webapp-defined entry instead carries its own `instructions` + a `tools`
# builder (see the exercise-add agent above). Extend, don't special-case.
_AGENTS = {
    "journal":  {"server": server.mcp, "instructions": server.JOURNAL_CHAT_INSTRUCTIONS,
                 "exclude": set(), "blurb": _JOURNAL_BLURB},
    "trainer":  {"server": server.trainer_mcp, "exclude": set(), "blurb": _TRAINER_BLURB},
    "exercise": {"instructions": _EXERCISE_INSTRUCTIONS, "tools": _exercise_tools, "blurb": ""},
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
    out = list(messages)
    for i in range(len(out) - 1, -1, -1):
        m = out[i]
        if m["role"] == "user" and isinstance(m["content"], str):
            out[i] = {**m, "content": f"[Sent on {server.today()} (Pacific)] {m['content']}"}
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

# Write tools mutate the DB; everything else is read-only retrieval. Used only to
# label the tool chips the UI shows, never to gate execution.
_WRITE_TOOLS = {
    "add_journal_entry", "update_entry", "save_person", "link_mentions",
    "merge_people", "delete_record", "journal_delete_entry",
    "intake_log", "intake_update", "intake_delete", "intake_set_profile",
    "notes_save", "notes_update", "notes_delete", "notes_file",
    "collections_save", "collections_delete",
    "log_workout", "update_workout", "update_set", "save_exercise",
    "log_bodyweight", "update_profile",
    "start_workout_plan", "complete_set", "swap_exercise", "add_to_plan",
    "finish_workout", "create_exercise",
}


async def _ensure_tools(agent: str):
    """Build the Anthropic tool list + dispatch map for `agent` once. A server-bound
    agent lifts them from its FastMCP instance's list_tools() minus its excluded names;
    a webapp-defined agent (the exercise-add surface) supplies its own via a `tools`
    builder, keeping its create path off the MCP tool surface. A cache_control breakpoint
    on the last tool caches the whole (large, static) tool-schema block across turns."""
    if agent in _TOOLS:
        return
    cfg = _AGENTS[agent]
    if "tools" in cfg:
        tools, dispatch = cfg["tools"]()
    else:
        exclude = cfg["exclude"]
        # run_middleware=False: this is the in-process webapp surface, already behind
        # the browser session's Google auth — there is no MCP access token here, so
        # letting AllowlistMiddleware.on_message run would reject the empty email.
        # The middleware guards the MCP wire surface, which this call never touches.
        tool_objs = await cfg["server"].list_tools(run_middleware=False)
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
    elif name == "delete_record":  # trainer server's kind-scoped delete
        summary = f"Deleted a {g('kind', 'record')}"
    elif name == "journal_delete_entry":
        href = "/journal"
        summary = "Deleted an entry"
    # Eating log.
    elif name == "intake_log":
        href = "/food"
        it = g("item")
        summary = f"Logged {it}" if it else "Logged intake"
    elif name == "intake_update":
        href = "/food"
        summary = "Corrected a logged item"
    elif name == "intake_delete":
        href = "/food"
        summary = "Deleted a logged item"
    elif name == "intake_set_profile":
        summary = "Updated the eating profile"
    elif name == "intake_summary":
        summary = "Loaded the eating log"
    elif name == "intake_find_past":
        summary = "Looked up a past meal"
    # Notes & collections.
    elif name == "notes_save":
        href = "/collections"
        t = g("title")
        summary = f"Saved {t}" if t else "Saved a note"
    elif name in ("notes_update", "notes_file"):
        href = "/collections"
        summary = "Updated a note" if name == "notes_update" else "Filed a note"
    elif name == "notes_delete":
        href = "/collections"
        summary = "Deleted a note"
    elif name in ("notes_get", "notes_list", "notes_search"):
        summary = "Looked up notes"
    elif name == "notes_geocode":
        summary = f"Located {g('query')}" if g("query") else "Looked up a place"
    elif name == "collections_save":
        href = "/collections"
        nm = g("name")
        summary = f"Saved the {nm} collection" if nm else "Saved a collection"
    elif name == "collections_delete":
        href = "/collections"
        summary = "Deleted a collection"
    elif name in ("collections_list", "collections_list_icons"):
        summary = "Looked up collections"
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
        # /graphs, not /workouts: the weigh-in is a morning reading now, and the
        # trend chart is where it's read (and entered).
        href = "/graphs"
        summary = "Logged bodyweight"
    elif name == "save_exercise":
        summary = "Saved an exercise"
    # Exercise-add agent (the library page's AI add).
    elif name == "check_library":
        summary = "Checked the library"
    elif name == "create_exercise":
        href = "/trainer/library"
        if r.get("error"):
            # A failed create (e.g. a name clash the model missed) shouldn't read as a
            # write — that's what triggers the page reload on the library surface.
            kind, summary = "read", "Exercise not added"
        else:
            nm = r.get("name") or g("name")
            summary = f"Added {nm}" if nm else "Added an exercise"
    elif name == "get_fitness_briefing":
        summary = "Loaded training context"
    # Trainer plan tools. Several sessions can be planned at once, so a chip links to
    # the one this call actually touched (its payload carries the workout_id) rather
    # than to a bare /trainer that would resolve to whichever is next due.
    elif name in ("start_workout_plan", "complete_set", "swap_exercise", "add_to_plan",
                  "finish_workout", "get_workout_plan"):
        wid = r.get("workout_id")
        href = f"/trainer/{wid}" if wid else "/workouts"
        summary = {
            "start_workout_plan": "Built a routine",
            "complete_set": "Logged a set",
            "swap_exercise": "Swapped an exercise",
            "add_to_plan": "Added to the plan",
            "finish_workout": "Finished the workout",
            "get_workout_plan": "Loaded the plan",
        }[name]
        # A finished session isn't a plan page any more — it's history.
        if name == "finish_workout":
            href = "/workouts"

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
    convo_key = _convo_key(agent, session_id, context)
    _maybe_rollover(convo_key)  # new Pacific day → drop the stale transcript
    messages = _CONVERSATIONS.setdefault(convo_key, [])
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
