"""
Read-only data layer for the journal web frontend.

The MCP server (`server.py`) is the single source of truth for how journal data
is shaped, matched and aggregated. We reuse its retrieval functions directly
(they stay plain-callable under fastmcp v3) and only add the handful of reads the
MCP contract doesn't expose: a recent-entry list, an entry's resolved people,
full workouts-with-sets, person detail, a gap-filled drink series, and the
dashboard roll-up. Nothing here writes.
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
    # Floor for bare intake-only days: the start of what's actually loaded. An
    # explicit `since` IS that start; otherwise the default window reaches back to
    # `oldest` — unless there's nothing older at all (has_more False), in which case
    # the feed is complete and every logged day belongs on it.
    floor = since or (oldest if has_more else None)
    _attach_nutrition(days, floor, bare_days=kind is None)
    if kind is None:
        _fill_empty_days(days)
    return {"days": days, "total": total, "oldest": oldest,
            "has_more": has_more, "next_since": next_since}


def _fill_empty_days(days: list[dict], span_cap: int = 400) -> None:
    """Insert a bare block for every calendar day inside the loaded window that has
    neither entries nor an intake row, in place.

    A day with nothing written is still a day you want to reach: it's where the
    drinks counter is tapped and where "write about this day" opens the chat. Without
    this the feed silently skips quiet days (an empty Tuesday simply isn't there, so
    there's nothing to tap). The window runs from today back to the oldest loaded
    day — never older, so it doesn't imply history that hasn't been paged in — and
    is capped at `span_cap` days as a guard against a single ancient outlier
    rendering years of blanks.

    Only the unfiltered feed fills: a kind filter is a scoped view of entries, so
    empty days there would be noise (same reason bare intake days are skipped)."""
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


# Daily targets the intake block's rings read against. DISPLAY-ONLY, the same shape
# as graphs.js's DRINK_LIMIT: the server stores no goals (all coaching judgment lives
# in the conversation), so these are a webapp constant, not a settings row. `ceiling`
# marks a number you're trying to stay UNDER (sodium, calories, alcohol) rather than
# reach — it only changes the ring's color once it's past the target. Calories use the
# middle of the 2,200-2,400 band, since a ring can't show a range.
#
# The macros are set so they add up to the calorie target rather than each being
# picked on its own (150p + 250c + 75f = 2,275 kcal): protein is fixed by muscle
# preservation, fat by a rough 0.35 g/lb floor, and carbs take the remainder — which
# is also why carbs is NOT a ceiling. It's the flex macro, and calories already has
# a ceiling ring to catch a genuine overshoot; a second warning color the moment
# carbs pass 250 would be noise. Fat has no target yet, so it renders a dashed ring.
NUTRIENT_TARGETS = {
    "calories":  {"target": 2300, "ceiling": True},
    "protein_g": {"target": 150,  "ceiling": False},
    "carbs_g":   {"target": 250,  "ceiling": False},
    "sodium_mg": {"target": 2300, "ceiling": True},
    "fiber_g":   {"target": 30,   "ceiling": False},
    "water_oz":  {"target": 128,  "ceiling": False},  # a gallon
    "standard_drinks": {"target": 2, "ceiling": True},
}


def _attach_nutrition(days: list[dict], floor: str | None, bare_days: bool = True) -> None:
    """Hang each day's eating section onto the feed's day blocks, in place.

    A day gets a `nutrition` dict (summary, notes, and whichever nutrients were
    actually estimated) only when one was logged — days with none carry nothing,
    so the template can just test truthiness and the block stays absent rather
    than rendering an empty "Eating —". This is the ONLY intake attach now: alcohol
    and water are nutrients on this row, so the old, separate `_attach_drinks` is
    gone. A day with intake but no entries is inserted inside the loaded window so
    it's still reachable, and a kind-filtered feed skips those insertions.

    Also splits `summary` back into the `items` it was built from: log_food appends
    each new food with "; " (server._merge_notes), so splitting on that separator
    recovers one entry per thing eaten, which the feed renders as a numbered list."""
    cols = ("summary", "notes", *server.NUTRIENTS)
    with server.db() as conn:
        rows = conn.execute(
            "SELECT food_date, " + ", ".join(cols) + " FROM nutrition "
            + ("WHERE food_date >= ? " if floor else "")
            + "ORDER BY food_date",
            (floor,) if floor else (),
        ).fetchall()
    by_date = {}
    for r in rows:
        n = {c: r[c] for c in cols if r[c] is not None}
        n["items"] = [s.strip() for s in (n.get("summary") or "").split("; ") if s.strip()]
        by_date[r["food_date"]] = n
    for day in days:
        n = by_date.pop(day["date"], None)
        if n:
            day["nutrition"] = n
    if not bare_days or not by_date:
        return
    for d, n in by_date.items():
        days.append({"date": d, "entries": [], "nutrition": n})
    days.sort(key=lambda x: x["date"], reverse=True)


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

def workouts_full(limit: int = 20) -> list:
    """Recent COMPLETED sessions with their done sets grouped by exercise (first-seen
    order). An in-progress plan (status='active') and its pending/skipped sets are
    excluded — those live on the /trainer page, not the history browse."""
    out = []
    with server.db() as conn:
        ws = conn.execute(
            "SELECT id, workout_date, focus, feeling, notes FROM workouts "
            "WHERE status='done' ORDER BY workout_date DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
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
                "exercises": exercises,
                "exercise_count": len(exercises),
                "set_count": len(srows),
            })
    return out


def exercise_library(muscle: str | None = None, q: str | None = None,
                     rotation: bool = False, hearted: bool = False,
                     archived: bool = False) -> dict:
    """The exercise catalog as a browsable LIBRARY: every movement in full — muscles in
    their three emphasis tiers (primary/secondary/tertiary), equipment, technique notes,
    common mistakes, cautions, and a form gif/video — plus `in_rotation` (is it in the
    small programming pool), `hearted` (is it in the wider favorites SUPERSET the rotation
    is drawn from; rotation ⊆ hearted) and how much it's actually been trained (`sessions`,
    `last_done`). Optionally filtered by `muscle` (any tier), a name fragment `q`,
    `rotation=True` to show only the rotation, or `hearted=True` to show the superset.

    By default only LIVE movements are listed; `archived=True` instead shows the
    soft-deleted ones (the Archived view, where each row offers Restore). Archived
    movements are hidden from every other view — and from the trainer — but their rows and
    past-workout links are kept.

    Also returns `muscles` (the canonical list, for the filter chips), `rotation_count`,
    `hearted_count`, and the active `muscle`/`q`/`rotation`/`hearted`/`archived` so the
    template can render the current filter. Read-only; writes go through
    server.save_exercise / server.set_rotation / server.set_hearted / server.set_archived."""
    from urllib.parse import quote_plus
    muscle = (muscle or "").strip().lower() or None
    q = (q or "").strip() or None
    with server.db() as conn:
        rows = conn.execute(
            "SELECT * FROM exercises WHERE archived=? ORDER BY name", (int(archived),)
        ).fetchall()
        # one pass over the muscle map: {exercise_id: {primary:[], secondary:[], tertiary:[]}}
        mmap: dict[int, dict] = {}
        for mr in conn.execute("SELECT exercise_id, muscle, role FROM exercise_muscles ORDER BY muscle"):
            d = mmap.setdefault(mr["exercise_id"],
                                {"primary": [], "secondary": [], "tertiary": []})
            d.setdefault(mr["role"], []).append(mr["muscle"])
        # AKAs per exercise, so the name search also matches alternative names and the
        # card can show them ({exercise_id: [alias, ...]}).
        akamap: dict[int, list[str]] = {}
        for ar in conn.execute("SELECT exercise_id, alias FROM exercise_aliases ORDER BY alias"):
            akamap.setdefault(ar["exercise_id"], []).append(ar["alias"])
        # training volume per exercise (completed sets only): session count + last done
        vol: dict[int, dict] = {}
        for vr in conn.execute(
            """SELECT exercise_id,
                      COUNT(DISTINCT workout_id) AS sessions,
                      MAX(w.workout_date) AS last_done
               FROM sets s JOIN workouts w ON w.id = s.workout_id
               WHERE s.status='done' GROUP BY exercise_id"""
        ):
            vol[vr["exercise_id"]] = {"sessions": vr["sessions"], "last_done": vr["last_done"]}
    out = []
    rotation_count = 0
    hearted_count = 0
    for r in rows:
        in_rotation = bool(r["in_rotation"])
        is_hearted = bool(r["hearted"])
        if in_rotation:
            rotation_count += 1
        if is_hearted:
            hearted_count += 1
        m = mmap.get(r["id"], {"primary": [], "secondary": [], "tertiary": []})
        all_m = m["primary"] + m["secondary"] + m["tertiary"]
        aka = akamap.get(r["id"], [])
        if rotation and not in_rotation:
            continue
        if hearted and not is_hearted:
            continue
        if muscle and muscle not in all_m:
            continue
        if q and not server._name_query_match(r["name"], q) \
                and not any(q.lower() in a for a in aka):
            continue
        v = vol.get(r["id"], {"sessions": 0, "last_done": None})
        out.append({
            "exercise_id": r["id"], "name": r["name"], "category": r["category"],
            "equipment": r["equipment"], "muscles": m, "aka": aka, "in_rotation": in_rotation,
            "hearted": is_hearted,
            "level": r["level"], "mechanic": r["mechanic"], "force": r["force"],
            "technique_notes": r["technique_notes"],
            "common_mistakes": r["common_mistakes"], "cautions": r["cautions"],
            "video_link": r["video_link"], "image_link": r["image_link"],
            "image_link_end": r["image_link_end"],
            "youtube_search": "https://www.youtube.com/results?search_query="
                              + quote_plus((r["name"] or "") + " proper form technique"),
            "has_notes": bool(r["technique_notes"] or r["common_mistakes"] or r["cautions"]),
            "sessions": v["sessions"], "last_done": v["last_done"],
        })
    # rotation first, then the rest of the hearted superset, then most-trained, alphabetical
    out.sort(key=lambda e: (not e["in_rotation"], not e["hearted"], -e["sessions"], e["name"].lower()))
    return {"exercises": out, "count": len(out), "muscles": server.MUSCLES,
            "rotation_count": rotation_count, "hearted_count": hearted_count,
            "muscle": muscle, "q": q or "",
            "rotation": rotation, "hearted": hearted, "archived": archived}


def active_plan() -> dict:
    """The active workout plan for the /trainer page, straight from the server (see
    server.get_workout_plan): {"active": False} or the full plan with exercises, sets
    (target + actual + status), and a done/total progress count."""
    return server.get_workout_plan()


def bodyweight_on(d: str):
    """The latest bodyweight reading (lbs) logged on day `d`, or None if not weighed —
    backs the /trainer card's weigh-in box (does today's weight need entering yet). The
    latest reading on a day is "the" weight for that day, matching server.log_bodyweight."""
    with server.db() as conn:
        r = conn.execute(
            "SELECT weight_lbs FROM body_weight WHERE weigh_date=? ORDER BY id DESC LIMIT 1",
            (d,),
        ).fetchone()
    return r["weight_lbs"] if r else None


def muscle_breakdown() -> dict:
    """Per-muscle training breakdown for the /workouts body heatmap.

    Keyed by every muscle in `server.MUSCLES` — all of them are always present (zeroed
    when never trained) so the diagram can render cold regions too. Each value
    carries the recency/volume the diagram colors by (`days_since`, `last_trained`,
    `sets_last_7d`, `sets_last_14d`) plus the per-exercise breakdown the recency
    briefing doesn't expose: the top ~5 exercises that hit the muscle in the last
    14 days, with 14d/7d set counts, for the hover popup.

    A "set" counts once per (muscle, set) through exercise_muscles, matching how
    server.get_fitness_briefing computes muscle_recency. All dates are Pacific
    (server.today())."""
    today = server.today()
    seven_ago = (date.fromisoformat(today) - timedelta(days=6)).isoformat()
    fourteen_ago = (date.fromisoformat(today) - timedelta(days=13)).isoformat()
    out = {
        m: {"days_since": None, "last_trained": None,
            "sets_last_7d": 0, "sets_last_14d": 0, "exercises": []}
        for m in server.MUSCLES
    }
    with server.db() as conn:
        for r in conn.execute(
            """SELECT em.muscle,
                      MAX(w.workout_date) AS last_date,
                      SUM(CASE WHEN w.workout_date >= ? THEN 1 ELSE 0 END) AS sets_7d,
                      SUM(CASE WHEN w.workout_date >= ? THEN 1 ELSE 0 END) AS sets_14d
               FROM sets s
               JOIN workouts w ON w.id = s.workout_id
               JOIN exercise_muscles em ON em.exercise_id = s.exercise_id
               WHERE s.status='done'
               GROUP BY em.muscle""",
            (seven_ago, fourteen_ago),
        ).fetchall():
            m = out.get(r["muscle"])
            if m is None:   # a muscle outside the canonical list — ignore
                continue
            m["last_trained"] = r["last_date"]
            m["days_since"] = server._days_since(r["last_date"])
            m["sets_last_7d"] = r["sets_7d"] or 0
            m["sets_last_14d"] = r["sets_14d"] or 0
        # top exercises per muscle over the last 14 days (for the hover popup)
        for r in conn.execute(
            """SELECT em.muscle, e.name AS name,
                      SUM(CASE WHEN w.workout_date >= ? THEN 1 ELSE 0 END) AS sets_7d,
                      COUNT(*) AS sets_14d
               FROM sets s
               JOIN workouts w ON w.id = s.workout_id
               JOIN exercise_muscles em ON em.exercise_id = s.exercise_id
               JOIN exercises e ON e.id = s.exercise_id
               WHERE w.workout_date >= ? AND s.status='done'
               GROUP BY em.muscle, e.id
               ORDER BY em.muscle, sets_14d DESC, sets_7d DESC, e.name""",
            (seven_ago, fourteen_ago),
        ).fetchall():
            m = out.get(r["muscle"])
            if m is None or len(m["exercises"]) >= 5:
                continue
            m["exercises"].append(
                {"name": r["name"], "sets_14d": r["sets_14d"], "sets_7d": r["sets_7d"]}
            )
    return out


# --------------------------------------------------------------------------- #
# Graphs
# --------------------------------------------------------------------------- #

def graph_data() -> dict:
    """Everything the /graphs page plots, in one bootstrap payload (the data is a
    single user's history — small enough to ship whole and filter client-side).

    - weight: one point per weighed day (latest reading wins, matching
      bodyweight_on / server.log_bodyweight).
    - drinks: days with an alcohol figure on the intake row (nutrition.standard_
      drinks, including explicit 0s); the page gap-fills unlogged days to 0 so the
      line is honest about the calendar.
    - exercises: per strength exercise (has at least one done, weighted set on a
      done workout), one point per session date with the deterministic aggregates
      the page can plot: heaviest set (`top`), best Epley est. 1RM (`e1rm` =
      weight * (1 + reps/30)), and total volume (`vol` = Σ weight*reps). Cardio
      sets (weight NULL) don't produce points, so pure-cardio movements are
      absent. Judgment about what the numbers mean stays with the reader/model —
      this is arithmetic only.
    - rotation_ids: the current rotation, the page's default selection.
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
        # Alcohol lives on the intake row now, not the legacy `drinks` table.
        drinks = [
            {"date": r["food_date"], "total": r["standard_drinks"]}
            for r in conn.execute(
                "SELECT food_date, ROUND(standard_drinks,2) AS standard_drinks "
                "FROM nutrition WHERE standard_drinks IS NOT NULL ORDER BY food_date"
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
        rotation_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM exercises WHERE in_rotation=1 AND archived=0 ORDER BY name"
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
    return {"weight": weight, "drinks": drinks, "exercises": exercises,
            "rotation_ids": rotation_ids, "today": server.today(),
            "weight_goal": goal}
