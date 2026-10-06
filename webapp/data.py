"""
Read-only data layer for the journal web frontend.

The MCP server (`server.py`) is the single source of truth for how journal data
is shaped, matched and aggregated. We reuse its retrieval functions directly
(they stay plain-callable under fastmcp v3) and only add the handful of reads the
MCP contract doesn't expose: a recent-entry list, an entry's resolved people,
full workouts-with-sets, person detail, and the dashboard roll-up. Nothing here writes.
"""

import calendar as _cal
import os
import sys
from datetime import date, timedelta

# server.py lives one directory up; make it importable however we're launched.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import server  # noqa: E402  (path set above)


# --------------------------------------------------------------------------- #
# Journal
# --------------------------------------------------------------------------- #

def list_entries(limit: int = 40, offset: int = 0, max_chars: int = 320) -> dict:
    """Recent entries, newest first — the journal proper (cleaned `body`)."""
    with server.db() as conn:
        rows = conn.execute(
            "SELECT id, entry_date, body FROM entries "
            "ORDER BY entry_date DESC, day_position IS NULL, day_position DESC, id DESC "
            "LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"]
    return {
        "entries": [
            {
                "entry_id": r["id"],
                "entry_date": r["entry_date"],
                "body": server._truncate(r["body"], max_chars),
            }
            for r in rows
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


def _people_for_entries(conn, entry_ids: list[int]) -> dict:
    """Resolved people per entry, each carrying the name `forms` to look for in the
    body when linking names inline: the canonical name, the surface form actually
    used, and the person's aliases. Keyed by entry_id; pending mentions are skipped
    (nothing to link to)."""
    if not entry_ids:
        return {}
    qs = ",".join("?" * len(entry_ids))
    rows = conn.execute(
        f"""SELECT m.entry_id, m.surface_form, p.id AS pid, p.canonical_name, p.role
            FROM mentions m JOIN people p ON p.id = m.person_id
            WHERE m.entry_id IN ({qs}) ORDER BY m.entry_id, m.id""",
        entry_ids,
    ).fetchall()
    pids = sorted({r["pid"] for r in rows})
    aliases: dict[int, list[str]] = {}
    if pids:
        ps = ",".join("?" * len(pids))
        for a in conn.execute(
            f"SELECT person_id, surface_form FROM aliases WHERE person_id IN ({ps})", pids
        ):
            aliases.setdefault(a["person_id"], []).append(a["surface_form"])
    by_entry: dict[int, dict[int, dict]] = {}
    for r in rows:
        people = by_entry.setdefault(r["entry_id"], {})
        person = people.get(r["pid"])
        if person is None:
            person = {
                "person_id": r["pid"],
                "name": r["canonical_name"],
                "role": r["role"],
                "forms": set(),
            }
            person["forms"].update(
                f for f in [r["canonical_name"], *aliases.get(r["pid"], [])] if f
            )
            people[r["pid"]] = person
        if r["surface_form"]:
            person["forms"].add(r["surface_form"])
    return {eid: list(people.values()) for eid, people in by_entry.items()}


def attach_people(entries: list[dict]) -> list[dict]:
    """Attach each entry's resolved people (for inline linking) in place."""
    with server.db() as conn:
        people = _people_for_entries(conn, [e["entry_id"] for e in entries])
    for e in entries:
        e["people"] = people.get(e["entry_id"], [])
    return entries


def all_entry_dates(kind: str | None = None) -> list[str]:
    """Every distinct entry_date in the journal, newest first — the full set the
    sidebar calendar marks, independent of how deep the feed is currently loaded.
    `kind` ("thought"/"log") narrows it to the dates that have a matching entry, so
    the calendar tracks the active feed filter. Cheap: distinct dates only, no bodies."""
    kw = ""
    if kind == "thought":
        kw = " WHERE kind = 'thought'"
    elif kind == "log":
        kw = " WHERE kind != 'thought'"
    with server.db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT entry_date FROM entries" + kw +
            " ORDER BY entry_date DESC"
        ).fetchall()
    return [r["entry_date"] for r in rows]


def list_days(limit_entries: int = 120, since: str | None = None,
              kind: str | None = None) -> dict:
    """Entries grouped into days, newest day first; within a day the entries read
    top-to-bottom in chronological order (`day_position`, set at capture and adjustable
    via server.reorder_entries; legacy NULL-position entries fall back to insertion id
    order), so the day reads as one block of prose split into per-topic paragraphs in the
    sequence the events happened. Each entry carries its resolved people so
    the feed can link names inline. Bodies are returned in full — entries are now small
    per-topic notes, not the day's whole dump.

    The feed loads the `limit_entries` newest entries by default. `since` (an ISO date)
    instead loads EVERY entry on/after that date: the "load older" button and the
    calendar's day deep-links pass it to pull history past the default window,
    *cumulatively* (always from today back to `since`, so already-shown days stay shown
    and their `#day-…` anchors keep working). Returns `oldest` (oldest day loaded),
    `has_more` (older entries exist below it), and `next_since` (the cursor the "load
    older" button requests to pull roughly `limit_entries` more — a date strictly older
    than `oldest`, so re-loading also completes any day the default LIMIT split)).

    `kind` filters the feed: None/"all" = every entry; "thought" = only personal
    reflections; "log" = everything EXCEPT reflections (interactions/observations).
    The same predicate drives the paging/`has_more` math so "load older" and the
    calendar stay consistent with the active view."""
    # Build a reusable kind predicate so every query below filters identically.
    kw = ""
    if kind == "thought":
        kw = " AND kind = 'thought'"
    elif kind == "log":
        kw = " AND kind != 'thought'"
    with server.db() as conn:
        if since:
            rows = conn.execute(
                "SELECT id, entry_date, body, kind FROM entries "
                "WHERE entry_date >= ?" + kw +
                " ORDER BY entry_date DESC, day_position IS NOT NULL, day_position ASC, id ASC",
                (since,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, entry_date, body, kind FROM entries "
                "WHERE 1=1" + kw +
                " ORDER BY entry_date DESC, day_position IS NOT NULL, day_position ASC, id ASC "
                "LIMIT ?",
                (limit_entries,),
            ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM entries WHERE 1=1" + kw
        ).fetchone()["n"]
        entries = [
            {"entry_id": r["id"], "entry_date": r["entry_date"], "body": r["body"],
             "kind": r["kind"]}
            for r in rows
        ]
        people = _people_for_entries(conn, [e["entry_id"] for e in entries])
        oldest = entries[-1]["entry_date"] if entries else None
        has_more, next_since = False, None
        if oldest:
            has_more = conn.execute(
                "SELECT 1 FROM entries WHERE entry_date < ?" + kw + " LIMIT 1",
                (oldest,),
            ).fetchone() is not None
            if has_more:
                row = conn.execute(
                    "SELECT entry_date FROM entries WHERE entry_date < ?" + kw +
                    " ORDER BY entry_date DESC, id ASC LIMIT 1 OFFSET ?",
                    (oldest, limit_entries - 1),
                ).fetchone()
                next_since = row["entry_date"] if row else conn.execute(
                    "SELECT MIN(entry_date) AS d FROM entries WHERE 1=1" + kw
                ).fetchone()["d"]
    days: list[dict] = []
    for e in entries:
        e["people"] = people.get(e["entry_id"], [])
        if not days or days[-1]["date"] != e["entry_date"]:
            days.append({"date": e["entry_date"], "entries": []})
        days[-1]["entries"].append(e)
    if kind is None:
        _fill_empty_days(days)
    return {"days": days, "total": total, "oldest": oldest,
            "has_more": has_more, "next_since": next_since}


def _fill_empty_days(days: list[dict], span_cap: int = 400) -> None:
    """Insert a bare block for every calendar day inside the loaded window that has
    no entries, in place.

    A day with nothing written is still a day you want to reach: it's where
    "write about this day" opens the chat. Without this the feed silently skips
    quiet days (an empty Tuesday simply isn't there, so there's nothing to tap).
    The window runs from today back to the oldest loaded day — never older, so it
    doesn't imply history that hasn't been paged in — and is capped at `span_cap`
    days as a guard against a single ancient outlier rendering years of blanks.

    Only the unfiltered feed fills: a kind filter is a scoped view of entries, so
    empty days there would be noise."""
    if not days:
        return
    newest = max(date.fromisoformat(d["date"]) for d in days)
    oldest_d = min(date.fromisoformat(d["date"]) for d in days)
    top = max(newest, date.fromisoformat(server.today()))
    if (top - oldest_d).days > span_cap:
        oldest_d = top - timedelta(days=span_cap)
    have = {d["date"] for d in days}
    cur = top
    while cur >= oldest_d:
        iso = cur.isoformat()
        if iso not in have:
            days.append({"date": iso, "entries": []})
        cur -= timedelta(days=1)
    days.sort(key=lambda x: x["date"], reverse=True)


# Daily targets for the /food rings. The defaults live in the server
# (INTAKE_TARGET_DEFAULTS) and the numbers the user SETS live in the DB (settings →
# eating_profile → targets, written by the trainer's set_intake_targets or /food's
# Targets popover), so the rings, the widget and the trainer read the same goals.
# A target is just a target — no ceiling/floor direction anywhere.
NUTRIENT_TARGETS = server.INTAKE_TARGET_DEFAULTS


def nutrient_targets() -> dict:
    """The live targets, {nutrient: number}: defaults overridden per nutrient by any
    well-formed number stored in the eating profile."""
    with server.db() as conn:
        return server._day_targets(conn)


def stored_targets() -> dict:
    """Only the targets actually SET, without the defaults merged in. The /food
    popover needs the two apart: a number you chose belongs in the input, an
    inherited default is only a placeholder — and clearing the box hands it back."""
    with server.db() as conn:
        return server._stored_targets(conn)


def stored_coaching() -> str:
    """The trainer `coaching` text from the profile (there is no default behind it —
    an empty one makes the trainer ask about coaching preferences)."""
    with server.db() as conn:
        text = server._get_profile(conn).get("coaching")
    return text if isinstance(text, str) else ""


def _day_nutrition(items: list) -> dict:
    """One day's intake, shaped for the templates: summed totals plus the item rows.
    Totals are SUMMED here from the item rows rather than read from a stored column:
    the sum is the only version that can't drift from the items shown beside it. A
    figure no item carries is absent, not 0 — "not logged" is a different fact."""
    n = {m: round(sum(x[m] for x in items if x[m] is not None), 1)
         for m in server.NUTRIENTS
         if any(x[m] is not None for x in items)}
    n["items"] = [
        {"id": r["id"], "text": r["item"], "note": r["note"],
         **{m: r[m] for m in server.NUTRIENTS if r[m] is not None}}
        for r in items
    ]
    n["notes"] = "; ".join(r["note"] for r in items if r["note"]) or None
    return n


def food_days(since: str | None = None, limit_days: int = 30) -> dict:
    """The water/protein log's own feed: days with intake, newest first, each
    carrying the `nutrition` dict (_day_nutrition). Journal entries don't appear here and intake
    no longer appears on /journal — the two logs are separate pages.

    Default window is the `limit_days` most recent logged days; `since` (ISO date)
    instead loads every logged day on/after it, cumulatively — the same "load older"
    cursor contract as list_days, so the button works identically."""
    # Only rows carrying water or protein: the other nutrient columns are dormant,
    # and a legacy calories-only day would otherwise render as an empty block.
    LIVE = server._LIVE_INTAKE
    with server.db() as conn:
        if since:
            dates = [r["food_date"] for r in conn.execute(
                "SELECT DISTINCT food_date FROM intake_items "
                f"WHERE {LIVE} AND food_date >= ? ORDER BY food_date DESC", (since,))]
        else:
            dates = [r["food_date"] for r in conn.execute(
                f"SELECT DISTINCT food_date FROM intake_items WHERE {LIVE} "
                "ORDER BY food_date DESC LIMIT ?", (limit_days,))]
        total = conn.execute(
            f"SELECT COUNT(DISTINCT food_date) AS n FROM intake_items WHERE {LIVE}").fetchone()["n"]
        oldest = dates[-1] if dates else None
        rows = conn.execute(
            f"SELECT * FROM intake_items WHERE {LIVE} AND food_date >= ? "
            "ORDER BY food_date DESC, position, id", (oldest,)
        ).fetchall() if oldest else []
        has_more, next_since = False, None
        if oldest:
            has_more = conn.execute(
                f"SELECT 1 FROM intake_items WHERE {LIVE} AND food_date < ? LIMIT 1", (oldest,)
            ).fetchone() is not None
            if has_more:
                row = conn.execute(
                    f"SELECT DISTINCT food_date FROM intake_items WHERE {LIVE} AND food_date < ? "
                    "ORDER BY food_date DESC LIMIT 1 OFFSET ?",
                    (oldest, limit_days - 1),
                ).fetchone()
                next_since = row["food_date"] if row else conn.execute(
                    f"SELECT MIN(food_date) AS d FROM intake_items WHERE {LIVE}").fetchone()["d"]
    by_date: dict = {}
    for r in rows:
        by_date.setdefault(r["food_date"], []).append(r)
    days = [{"date": d, "nutrition": _day_nutrition(items)}
            for d, items in by_date.items()]
    days.sort(key=lambda x: x["date"], reverse=True)
    return {"days": days, "total": total, "oldest": oldest,
            "has_more": has_more, "next_since": next_since}


def calendar_months(entry_dates: list[str], today: str | None = None) -> list[dict]:
    """Sidebar calendar data for the journal feed: one entry per month spanned by
    the given entry_dates, newest month first. Each month carries weeks of seven
    day cells with `has_entry` / `in_month` / `is_today` flags so the template
    avoids date math. Sunday-first to match the user's locale convention. Empty
    input → []."""
    if not entry_dates:
        return []
    entry_set = set(entry_dates)
    parsed = sorted({date.fromisoformat(d) for d in entry_set})
    earliest, latest = parsed[0].replace(day=1), parsed[-1].replace(day=1)
    today_d = date.fromisoformat(today) if today else None
    cal = _cal.Calendar(firstweekday=6)  # Sunday
    out: list[dict] = []
    cur = latest
    while cur >= earliest:
        weeks = []
        for week in cal.monthdatescalendar(cur.year, cur.month):
            row = []
            for d in week:
                iso = d.isoformat()
                row.append({
                    "date": iso, "day": d.day,
                    "in_month": d.month == cur.month,
                    "has_entry": iso in entry_set,
                    "is_today": d == today_d,
                })
            weeks.append(row)
        out.append({"label": cur.strftime("%b %Y"), "weeks": weeks})
        cur = (cur - timedelta(days=1)).replace(day=1)
    return out


def entry_with_people(entry_id: int):
    """Cleaned entry plus the people resolved within it. The verbatim raw_body
    is a hidden backup — not fetched or shown on the web."""
    e = server.get_entry(entry_id, include_raw=False)
    if "error" in e:
        return None
    with server.db() as conn:
        rows = conn.execute(
            """SELECT m.id, m.surface_form, m.status, p.id AS pid, p.canonical_name, p.role
               FROM mentions m LEFT JOIN people p ON p.id = m.person_id
               WHERE m.entry_id = ? ORDER BY m.id""",
            (entry_id,),
        ).fetchall()
        e["mentions"] = [
            {
                "mention_id": r["id"],
                "surface_form": r["surface_form"],
                "status": r["status"],
                "person_id": r["pid"],
                "name": r["canonical_name"],
                "role": r["role"],
                # Candidate matches so the inline resolver can offer them (pending only).
                "candidates": (server.find_candidates(conn, r["surface_form"])
                               if r["pid"] is None else []),
            }
            for r in rows
        ]
    return e


def pending_mentions(limit: int = 200) -> list:
    """The resolution queue, enriched for the web view: each pending mention with
    its surface form, context snippet, entry_date, entry_id (so you can jump to
    the entry), and the same candidate matches `list_pending_mentions` returns.
    The page's inline resolver can pin these to people (link/new/dismiss)
    via the /mention/* endpoints; chat with Claude still resolves them too.
    """
    out = server.list_pending_mentions(limit=limit)["pending"]
    with server.db() as conn:
        ids = {p["mention_id"] for p in out}
        if ids:
            qs = ",".join("?" * len(ids))
            entry_by_mention = {
                r["mid"]: r["eid"] for r in conn.execute(
                    f"SELECT id AS mid, entry_id AS eid FROM mentions WHERE id IN ({qs})",
                    list(ids),
                )
            }
        else:
            entry_by_mention = {}
    for p in out:
        p["entry_id"] = entry_by_mention.get(p["mention_id"])
    return out


# --------------------------------------------------------------------------- #
# People
# --------------------------------------------------------------------------- #

def groups_overview() -> list:
    """All groups with member counts, sorted by size descending. Empty groups
    (no current members) drop out so the page reflects what's actually wired up."""
    with server.db() as conn:
        rows = conn.execute(
            """SELECT g.name, COUNT(pg.person_id) AS n
               FROM groups g LEFT JOIN person_groups pg ON pg.group_id = g.id
               GROUP BY g.id HAVING n > 0
               ORDER BY n DESC, g.name"""
        ).fetchall()
    return [{"name": r["name"], "member_count": r["n"]} for r in rows]


def group_members(name: str) -> dict | None:
    """Members of one group with the same compact shape as /people rows (id, name,
    role, last_mentioned, alias count, other groups). Returns None if the group
    doesn't exist."""
    with server.db() as conn:
        g = conn.execute("SELECT id, name FROM groups WHERE name=?", (name,)).fetchone()
        if not g:
            return None
        rows = conn.execute(
            """SELECT p.id, p.canonical_name, p.role,
                      (SELECT COUNT(*) FROM aliases a WHERE a.person_id=p.id) AS aliases,
                      (SELECT MAX(e.entry_date) FROM mentions m
                         JOIN entries e ON e.id = m.entry_id
                        WHERE m.person_id = p.id) AS last_mentioned
               FROM people p JOIN person_groups pg ON pg.person_id = p.id
               WHERE pg.group_id = ?
               ORDER BY last_mentioned IS NULL, last_mentioned DESC, p.canonical_name""",
            (g["id"],),
        ).fetchall()
        members = []
        for r in rows:
            other = [og for og in server._groups_for(conn, r["id"]) if og != g["name"]]
            members.append({"person_id": r["id"], "name": r["canonical_name"],
                            "role": r["role"], "aliases": r["aliases"],
                            "last_mentioned": r["last_mentioned"], "groups": other})
    return {"name": g["name"], "members": members, "count": len(members)}


def person_detail(person_id: int, history_limit: int = 100_000):
    """Everything the read UI shows for one person. `history_limit` defaults
    effectively unbounded — the browse page lists ALL of a person's entries (the
    small default on the MCP tool is for the token-budgeted conversation, not here)."""
    with server.db() as conn:
        p = conn.execute(
            "SELECT id, canonical_name, role, summary, notes "
            "FROM people WHERE id = ?",
            (person_id,),
        ).fetchone()
        if not p:
            return None
        contact = server._get_contact(conn, person_id)
        aliases = conn.execute(
            "SELECT surface_form, source FROM aliases WHERE person_id = ? "
            "ORDER BY source, surface_form",
            (person_id,),
        ).fetchall()
        groups = server._groups_for(conn, person_id)
    hist = server.get_person_history(person_id, limit=history_limit)
    related = server.get_related_people(person_id)
    return {
        "person_id": p["id"],
        "name": p["canonical_name"],
        "role": p["role"],
        "summary": p["summary"],
        "notes": p["notes"],
        "contact": contact,
        "groups": groups,
        "aliases": [{"surface_form": a["surface_form"], "source": a["source"]} for a in aliases],
        "history": hist.get("entries", []),
        "history_count": hist.get("count", 0),
        "related": related.get("related", []),
    }


# --------------------------------------------------------------------------- #
# Training
# --------------------------------------------------------------------------- #

def workouts_full(limit: int = 20, since: str | None = None) -> dict:
    """Recent COMPLETED sessions with their done sets grouped by exercise (first-seen
    order). Planned sessions (status='active') and their pending/skipped sets are
    excluded — those are upcoming_plans(), listed above this on the same page.

    Default window is the `limit` most recent sessions; `since` (ISO date) instead
    loads every session on/after it, cumulatively — the same "load older" cursor
    contract as list_days/food_days, so the button works identically. Returns
    {"sessions", "has_more", "next_since"}."""
    out = []
    with server.db() as conn:
        if since:
            ws = conn.execute(
                "SELECT id, workout_date, focus, feeling, notes FROM workouts "
                "WHERE status='done' AND workout_date >= ? "
                "ORDER BY workout_date DESC, id DESC", (since,),
            ).fetchall()
        else:
            ws = conn.execute(
                "SELECT id, workout_date, focus, feeling, notes FROM workouts "
                "WHERE status='done' ORDER BY workout_date DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        oldest = ws[-1]["workout_date"] if ws else None
        has_more, next_since = False, None
        if oldest:
            has_more = conn.execute(
                "SELECT 1 FROM workouts WHERE status='done' AND workout_date < ? LIMIT 1",
                (oldest,),
            ).fetchone() is not None
            if has_more:
                row = conn.execute(
                    "SELECT workout_date FROM workouts WHERE status='done' "
                    "AND workout_date < ? ORDER BY workout_date DESC, id DESC "
                    "LIMIT 1 OFFSET ?", (oldest, limit - 1),
                ).fetchone()
                next_since = row["workout_date"] if row else conn.execute(
                    "SELECT MIN(workout_date) AS d FROM workouts WHERE status='done'"
                ).fetchone()["d"]
        for w in ws:
            srows = conn.execute(
                """SELECT s.weight_lbs, s.reps, s.rpe,
                          s.duration_seconds, s.distance_miles, s.note,
                          e.id AS eid, e.name AS ename, e.category
                   FROM sets s JOIN exercises e ON e.id = s.exercise_id
                   WHERE s.workout_id = ? AND s.status='done' ORDER BY s.id""",
                (w["id"],),
            ).fetchall()
            order, by_ex = [], {}
            for s in srows:
                if s["eid"] not in by_ex:
                    by_ex[s["eid"]] = {
                        "exercise_id": s["eid"],
                        "name": s["ename"],
                        "category": s["category"],
                        "sets": [],
                    }
                    order.append(s["eid"])
                by_ex[s["eid"]]["sets"].append(
                    {"weight_lbs": s["weight_lbs"], "reps": s["reps"],
                     "rpe": s["rpe"], "duration_seconds": s["duration_seconds"],
                     "distance_miles": s["distance_miles"], "note": s["note"]}
                )
            exercises = [by_ex[i] for i in order]
            # muscles this session actually hit — each with its strongest emphasis
            # tier (colors the mini body diagram in the day's title block) and the
            # exercises that worked it, ordered by how hard each worked it
            # (primary contributors first). Shape per muscle:
            #   {"tier": "primary", "exercises": [{"name": ..., "role": ...}, ...]}
            # The muscle modal's hover caption reads the exercise list.
            mrows = conn.execute(
                """SELECT DISTINCT em.muscle, em.role, e.name,
                          CASE em.role WHEN 'primary' THEN 1
                                       WHEN 'secondary' THEN 2 ELSE 3 END AS rank
                   FROM sets s
                   JOIN exercise_muscles em ON em.exercise_id = s.exercise_id
                   JOIN exercises e ON e.id = s.exercise_id
                   WHERE s.workout_id = ? AND s.status='done'
                   ORDER BY em.muscle, rank, e.name""",
                (w["id"],),
            ).fetchall()
            tier_name = {1: "primary", 2: "secondary", 3: "tertiary"}
            muscles: dict = {}
            for r in mrows:
                # rows arrive rank-ordered, so the first row for a muscle carries
                # its strongest tier
                entry = muscles.setdefault(
                    r["muscle"], {"tier": tier_name[r["rank"]], "exercises": []}
                )
                entry["exercises"].append({"name": r["name"], "role": r["role"]})
            # latest bodyweight reading on the day of this session, if any
            bw = conn.execute(
                "SELECT weight_lbs FROM body_weight WHERE weigh_date=? ORDER BY id DESC LIMIT 1",
                (w["workout_date"],),
            ).fetchone()
            out.append({
                "workout_id": w["id"],
                "date": w["workout_date"],
                "focus": w["focus"],
                "feeling": w["feeling"],
                "notes": w["notes"],
                "bodyweight": bw["weight_lbs"] if bw else None,
                "muscles": muscles,
                "exercises": exercises,
                "exercise_count": len(exercises),
                "set_count": len(srows),
            })
    return {"sessions": out, "has_more": has_more, "next_since": next_since}


def all_workout_dates() -> list[str]:
    """Every distinct completed-session date, newest first — the full set the
    Training page's sidebar calendar marks, independent of how deep the history is
    currently loaded (the all_entry_dates pattern; a calendar built from the loaded
    page alone goes blank past the first screen). Cheap: distinct dates only."""
    with server.db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT workout_date FROM workouts WHERE status='done' "
            "ORDER BY workout_date DESC").fetchall()
    return [r["workout_date"] for r in rows]


def active_plan(workout_id: int | None = None) -> dict:
    """One workout plan for a /trainer session page, straight from the server (see
    server.get_workout_plan): {"active": False} or the full plan with exercises, sets
    (target + actual + status), and a done/total progress count. `workout_id` names the
    session (every /trainer route carries it now that a week can be planned at once);
    omitted, it's the next-due plan. Carries the page-only `history` (with_history)."""
    return with_history(server.get_workout_plan(workout_id=workout_id))


def with_history(plan: dict) -> dict:
    """Add a `history` map to a /trainer plan payload: per exercise id, the LAST
    completed session's sets (date + weight/reps/rpe) and the BEST set before this
    session (heaviest weight, most reps at it — the same rule as server._new_bests).
    It's what you glance at between sets to pick a weight and to know what a PR
    attempt has to beat. A webapp-only enrichment, like app._with_pr: _plan_payload
    is the model-facing return of every plan tool, and the model already has
    get_exercise_history, so the extra rows stay off the connector. Both look-ups
    exclude this session's own sets, so logging a set here doesn't move its own
    "last"/"best" mid-workout. Cardio carries no weight, so it gets only `last`."""
    if not isinstance(plan, dict) or not plan.get("active"):
        return plan
    wid = plan.get("workout_id")
    ids = [ex["exercise_id"] for ex in plan.get("exercises", [])]
    hist = {}
    if not ids:
        plan["history"] = hist
        return plan
    with server.db() as conn:
        for eid in ids:
            last = conn.execute(
                """SELECT w.id, w.workout_date FROM sets s JOIN workouts w ON w.id = s.workout_id
                   WHERE s.exercise_id=? AND s.status='done' AND w.status='done' AND w.id != ?
                   ORDER BY w.workout_date DESC, w.id DESC LIMIT 1""",
                (eid, wid),
            ).fetchone()
            entry = {}
            if last:
                rows = conn.execute(
                    """SELECT weight_lbs, reps, rpe, duration_seconds, distance_miles
                       FROM sets WHERE workout_id=? AND exercise_id=? AND status='done'
                       ORDER BY set_index""",
                    (last["id"], eid),
                ).fetchall()
                entry["last"] = {"date": last["workout_date"],
                                 "sets": [dict(r) for r in rows]}
            best = conn.execute(
                """SELECT weight_lbs, reps FROM sets
                   WHERE exercise_id=? AND status='done' AND workout_id != ?
                     AND weight_lbs IS NOT NULL AND reps IS NOT NULL
                   ORDER BY weight_lbs DESC, reps DESC LIMIT 1""",
                (eid, wid),
            ).fetchone()
            if best:
                entry["best"] = dict(best)
            mech = conn.execute("SELECT mechanic FROM exercises WHERE id=?", (eid,)).fetchone()
            if mech and mech["mechanic"]:
                entry["mechanic"] = mech["mechanic"]  # sizes the page's rest target
            if entry:
                hist[str(eid)] = entry
    plan["history"] = hist
    return plan


def upcoming_plans() -> list:
    """Sessions PLANNED but not yet done, next-due first — the Training page's upcoming
    list, above the completed history from workouts_full. Deliberately counts only: the
    row says date, focus and how big the session is, and the sets themselves are one tap
    away on the session's own page (nothing is logged yet, so there'd be nothing to show).
    An unscheduled plan (planned_date NULL) sorts as today's, matching the server's
    _current_plan. `done_count` is what's already been logged, so a part-finished session
    can say so."""
    with server.db() as conn:
        ws = conn.execute(
            """SELECT id, planned_date, focus FROM workouts WHERE status='active'
               ORDER BY COALESCE(NULLIF(planned_date,''), ?) ASC, id ASC""",
            (server.today(),),
        ).fetchall()
        out = []
        for w in ws:
            # Skipped sets are excluded the same way _plan_payload's progress count
            # excludes them — a swapped-out movement isn't part of the session anymore.
            n = conn.execute(
                """SELECT COUNT(DISTINCT exercise_id) AS e, COUNT(*) AS s,
                          SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS d
                   FROM sets WHERE workout_id=? AND status!='skipped'""",
                (w["id"],),
            ).fetchone()
            out.append({
                "workout_id": w["id"],
                "planned_date": w["planned_date"],
                "focus": w["focus"],
                "exercise_count": n["e"],
                "set_count": n["s"],
                "done_count": n["d"] or 0,
            })
    return out


def bodyweight_log() -> list[dict]:
    """EVERY weigh-in row, newest first — the /weight log under its import box.

    Deliberately NOT graph_data()'s `weight` series, which is one point per DAY (latest
    reading wins). That collapse is right for a chart and wrong here: a scale can record
    twice in a morning (a re-weigh, someone else stepping on it), and the second reading
    VANISHES from the day view while still sitting in the table owning MIN(weight_lbs) —
    which is the figure every "lowest ever" is measured against."""
    with server.db() as conn:
        rows = conn.execute(
            """SELECT id, weigh_date, weight_lbs, note FROM body_weight
               ORDER BY weigh_date DESC, id DESC"""
        ).fetchall()
    out = [{"id": r["id"], "date": r["weigh_date"], "lbs": r["weight_lbs"],
            "note": r["note"], "change": None} for r in rows]
    # Each row's delta against the next-OLDER reading. Newest-first, so that's the row
    # after it. Consecutive readings rather than day-over-day: two on one morning are two
    # facts, and flattening them here would hide the re-weigh that a correction looks like.
    for i in range(len(out) - 1):
        out[i]["change"] = round(out[i]["lbs"] - out[i + 1]["lbs"], 1)
    return out


# --------------------------------------------------------------------------- #
# Graphs
# --------------------------------------------------------------------------- #

def graph_data() -> dict:
    """Everything the /graphs page plots, in one bootstrap payload (the data is a
    single user's history — small enough to ship whole and filter client-side).

    - weight: one point per weighed day (latest reading wins). Read-only — readings
      arrive by importing the scale's export on /weight, and this page just plots
      them against the goal.
    - exercises: per strength exercise (has at least one done, weighted set on a
      done workout), one point per session date with the deterministic aggregates
      the page can plot: heaviest set (`top`), best Epley est. 1RM (`e1rm` =
      weight * (1 + reps/30)), and total volume (`vol` = Σ weight*reps). Cardio
      sets (weight NULL) don't produce points, so pure-cardio movements are
      absent. Judgment about what the numbers mean stays with the reader/model —
      this is arithmetic only.
    - active_ids: the user's active (non-archived) exercises.
    - weight_goal: the goal from the trainer profile (settings key 'profile',
      the same blob get_fitness_briefing surfaces, so the trainer chat sees it
      too): {target_lbs, target_date?, start_lbs?, start_date?} or None. The
      start_* anchor is the latest weigh-in at the moment the goal was set —
      the fixed point the page draws the pace line from. Written by the
      /graphs/goal route via server.update_profile; no goal logic lives here.
    """
    with server.db() as conn:
        goal = server._get_profile(conn).get("weight_goal") or None
        if goal and not isinstance(goal.get("target_lbs"), (int, float)):
            goal = None
        weight = [
            {"date": r["weigh_date"], "lbs": r["weight_lbs"]}
            for r in conn.execute(
                """SELECT weigh_date, weight_lbs FROM body_weight b
                   WHERE id = (SELECT MAX(id) FROM body_weight
                               WHERE weigh_date = b.weigh_date)
                   ORDER BY weigh_date"""
            )
        ]
        ex_rows = conn.execute(
            """SELECT s.exercise_id, e.name, w.workout_date AS date,
                      MAX(s.weight_lbs) AS top,
                      ROUND(MAX(s.weight_lbs * (1 + COALESCE(s.reps, 1) / 30.0)), 1) AS e1rm,
                      ROUND(SUM(s.weight_lbs * COALESCE(s.reps, 1)), 1) AS vol
               FROM sets s
               JOIN workouts w ON w.id = s.workout_id
               JOIN exercises e ON e.id = s.exercise_id
               WHERE s.status = 'done' AND w.status = 'done'
                     AND w.workout_date != '' AND s.weight_lbs IS NOT NULL
               GROUP BY s.exercise_id, w.workout_date
               ORDER BY e.name, w.workout_date""",
        ).fetchall()
        active_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM exercises WHERE archived=0 ORDER BY name"
            )
        ]
    exercises: list[dict] = []
    by_id: dict[int, dict] = {}
    for r in ex_rows:
        ex = by_id.get(r["exercise_id"])
        if ex is None:
            ex = {"exercise_id": r["exercise_id"], "name": r["name"], "points": []}
            by_id[r["exercise_id"]] = ex
            exercises.append(ex)
        ex["points"].append({"date": r["date"], "top": r["top"],
                             "e1rm": r["e1rm"], "vol": r["vol"]})
    return {"weight": weight, "exercises": exercises,
            "active_ids": active_ids, "today": server.today(),
            "weight_goal": goal}
