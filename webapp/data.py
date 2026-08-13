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
    # Intake lives on its own /food page now (food_days below) — the journal feed
    # carries entries only, so no nutrition is attached here.
    if kind is None:
        _fill_empty_days(days)
    # …but WHAT was eaten rides along as a memory hook: "oh, Gjelina" is often how you
    # remember what a day was. Names only — the figures stay on /food, so the journal
    # reads as prose. Attached AFTER the fill, since a day with nothing written can
    # still have been a day you ate somewhere memorable.
    if days:
        eaten = _intake_names_by_day([d["date"] for d in days])
        for d in days:
            d["food"] = eaten.get(d["date"], [])
    return {"days": days, "total": total, "oldest": oldest,
            "has_more": has_more, "next_since": next_since}


def _intake_names_by_day(dates: list[str]) -> dict[str, list[dict]]:
    """{date: [{text}]} for the given days — what was eaten, nothing about it.

    A bare water top-up has no text of its own and would render as an empty line, so
    it's dropped here: this list is for recognizing a day, and "16oz water" isn't a
    thing you remember one by."""
    if not dates:
        return {}
    out: dict[str, list[dict]] = {}
    with server.db() as conn:
        rows = conn.execute(
            "SELECT food_date, item FROM intake_items "
            "WHERE food_date BETWEEN ? AND ? AND item IS NOT NULL AND item != '' "
            "ORDER BY food_date, position, id",
            (min(dates), max(dates)),
        ).fetchall()
    for r in rows:
        out.setdefault(r["food_date"], []).append({"text": r["item"]})
    return out


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


# DEFAULT daily targets the intake block's rings read against — the fallback when the
# stored eating profile doesn't name a number. The NUMBERS can come from the DB now
# (settings key 'eating_profile', its `targets` dict, written by the journal server's
# update_eating_profile) so the rings and the model coach against the SAME goals; what
# stays display-only is the DIRECTION: `ceiling` marks a number you're trying to stay
# UNDER (sodium, calories, alcohol) rather than reach — it only changes the ring's
# color once it's past the target, and it's a reading of the nutrient, not stored data.
# Read targets through nutrient_targets(), never this dict directly.
#
# The default macros are set so they add up to the calorie target rather than each
# being picked on its own (130p + 200c + 75f = 1,995 kcal): protein is fixed by muscle
# preservation, fat by a rough 0.35 g/lb floor, and carbs take the remainder — which
# is also why carbs is NOT a ceiling. It's the flex macro, and calories already has
# a ceiling ring to catch a genuine overshoot; a second warning color the moment
# carbs pass 200 would be noise. Fat has no default target, so it renders a dashed
# ring until the profile gives it one.
NUTRIENT_TARGETS = {
    "calories":  {"target": 2000, "ceiling": True},
    "protein_g": {"target": 130,  "ceiling": False},
    "carbs_g":   {"target": 200,  "ceiling": False},
    "sodium_mg": {"target": 2300, "ceiling": True},
    "fiber_g":   {"target": 30,   "ceiling": False},
    "water_oz":  {"target": 88,   "ceiling": False},
    "standard_drinks": {"target": 2, "ceiling": True},
}

# Ceiling-ness per nutrient, for overrides on a nutrient the defaults don't target
# (fat): anything unlisted reads as a floor — a ring never turns clay for passing it.
_CEILINGS = {k: v["ceiling"] for k, v in NUTRIENT_TARGETS.items()}


def nutrient_targets() -> dict:
    """The live targets: NUTRIENT_TARGETS defaults, overridden per nutrient by any
    number in the stored eating profile's `targets` (see update_eating_profile in
    server.py). Read per-request — a target changed in chat shows on the next page
    load. Only well-formed overrides count: a real nutrient key carrying a number;
    anything else in the blob is the model's business, not the rings'."""
    out = {k: dict(v) for k, v in NUTRIENT_TARGETS.items()}
    with server.db() as conn:
        stored = server._get_eating_profile(conn).get("targets") or {}
    for k, v in stored.items():
        if k in server.NUTRIENTS and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            out[k] = {"target": v, "ceiling": _CEILINGS.get(k, False)}
    return out


def _day_nutrition(items: list) -> dict:
    """One day's intake, shaped for the templates: summed totals plus the item rows.
    Totals are SUMMED here from the item rows (server.intake_items) rather than read
    from a stored column: the sum is the only version that can't drift from the items
    shown beside it. A nutrient no item carries is absent, not 0 — "unestimated" is a
    different fact from zero."""
    n = {m: round(sum(x[m] for x in items if x[m] is not None), 1)
         for m in server.NUTRIENTS
         if any(x[m] is not None for x in items)}
    n["items"] = [
        {"id": r["id"], "text": r["item"], "note": r["note"],
         # Which nutrients this item carries — drives the per-item detail modal.
         **{m: r[m] for m in server.NUTRIENTS if r[m] is not None}}
        for r in items
    ]
    # What the day's ALCOHOL cost in calories — derived from the items like every
    # other figure here, never stored. The drinks ring counts standard drinks, and
    # two of those can be 200 kcal or 900 depending on whether they were light beers
    # or margaritas, so the count alone can't say what drinking added to the day.
    # `unestimated` counts alcohol items carrying no calorie figure, so a partial sum
    # can admit it instead of reading as the whole truth — the same "unestimated is
    # not zero" rule the totals follow.
    booze = [x for x in items if x["standard_drinks"]]
    kcal = [x["calories"] for x in booze if x["calories"] is not None]
    if booze:
        missing = len(booze) - len(kcal)
        n["alcohol"] = {
            "calories": round(sum(kcal), 1) if kcal else None,
            "unestimated": missing,
            # Share is withheld while any drink is unestimated: a partial numerator
            # over the same partial denominator reads as a much bigger fraction than
            # it is (one estimated drink on a day of otherwise-unestimated drinks
            # would say "100% of the day's calories").
            "share": (round(100 * sum(kcal) / n["calories"])
                      if kcal and not missing and n.get("calories") else None),
        }
    n["notes"] = "; ".join(r["note"] for r in items if r["note"]) or None
    return n


def food_days(since: str | None = None, limit_days: int = 30) -> dict:
    """The food log's own feed: days with intake, newest first, each carrying the
    `nutrition` dict (_day_nutrition). Journal entries don't appear here and intake
    no longer appears on /journal — the two logs are separate pages.

    Default window is the `limit_days` most recent logged days; `since` (ISO date)
    instead loads every logged day on/after it, cumulatively — the same "load older"
    cursor contract as list_days, so the button works identically."""
    with server.db() as conn:
        if since:
            dates = [r["food_date"] for r in conn.execute(
                "SELECT DISTINCT food_date FROM intake_items "
                "WHERE food_date >= ? ORDER BY food_date DESC", (since,))]
        else:
            dates = [r["food_date"] for r in conn.execute(
                "SELECT DISTINCT food_date FROM intake_items "
                "ORDER BY food_date DESC LIMIT ?", (limit_days,))]
        total = conn.execute(
            "SELECT COUNT(DISTINCT food_date) AS n FROM intake_items").fetchone()["n"]
        oldest = dates[-1] if dates else None
        rows = conn.execute(
            "SELECT * FROM intake_items WHERE food_date >= ? "
            "ORDER BY food_date DESC, position, id", (oldest,)
        ).fetchall() if oldest else []
        has_more, next_since = False, None
        if oldest:
            has_more = conn.execute(
                "SELECT 1 FROM intake_items WHERE food_date < ? LIMIT 1", (oldest,)
            ).fetchone() is not None
            if has_more:
                row = conn.execute(
                    "SELECT DISTINCT food_date FROM intake_items WHERE food_date < ? "
                    "ORDER BY food_date DESC LIMIT 1 OFFSET ?",
                    (oldest, limit_days - 1),
                ).fetchone()
                next_since = row["food_date"] if row else conn.execute(
                    "SELECT MIN(food_date) AS d FROM intake_items").fetchone()["d"]
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


# --------------------------------------------------------------------------- #
# Notes & collections — the flexible layer's browse reads. Rendering is driven
# by each collection's own metadata (fields + display_hint), so a new collection
# gets a page without any code here changing. Writes stay on the MCP tools.
# --------------------------------------------------------------------------- #

def _item_view(r, fields: list[dict] | None = None) -> dict:
    """One item shaped for a template: parsed data (ordered by the collection's
    field order when known), tags as a list, updated day for the byline."""
    data_blob = server._item_data(r)
    if fields:
        ordered = [(f["key"], f["label"], data_blob.get(f["key"]))
                   for f in fields if f["key"] in data_blob]
    else:
        ordered = [(k, k.replace("_", " "), v) for k, v in data_blob.items()]
    return {"item_id": r["id"], "title": r["title"], "body": r["body"] or "",
            "data": ordered, "data_map": data_blob,
            "tags": [t.strip() for t in (r["tags"] or "").split(",") if t.strip()],
            "updated": (r["updated_at"] or "")[:10]}


def collections_overview() -> dict:
    """Every collection (with item count) plus the inbox notes — /collections."""
    with server.db() as conn:
        counts = {r["collection_id"]: r["n"] for r in conn.execute(
            "SELECT collection_id, COUNT(*) AS n FROM items GROUP BY collection_id")}
        colls = [{"name": r["name"], "description": r["description"] or "",
                  "display_hint": r["display_hint"], "count": counts.get(r["id"], 0)}
                 for r in conn.execute("SELECT * FROM collections ORDER BY name")]
        notes = [_item_view(r) for r in conn.execute(
            "SELECT * FROM items WHERE collection_id IS NULL ORDER BY updated_at DESC")]
    return {"collections": colls, "notes": notes}


def collection_page(name: str) -> dict | None:
    """One collection with its items, newest-updated first, or None."""
    with server.db() as conn:
        c = conn.execute("SELECT * FROM collections WHERE lower(name)=?",
                         ((name or "").strip().lower(),)).fetchone()
        if not c:
            return None
        fields = server._coll_fields(c)
        items = [_item_view(r, fields) for r in conn.execute(
            "SELECT * FROM items WHERE collection_id=? ORDER BY updated_at DESC",
            (c["id"],))]
    return {"name": c["name"], "description": c["description"] or "",
            "display_hint": c["display_hint"], "fields": fields, "rows": items}


def item_page(item_id: int) -> dict | None:
    """One item in full, with its collection context for the kicker/labels."""
    with server.db() as conn:
        r = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not r:
            return None
        c = conn.execute("SELECT * FROM collections WHERE id=?",
                         (r["collection_id"],)).fetchone() if r["collection_id"] else None
        view = _item_view(r, server._coll_fields(c) if c else None)
    view["collection"] = c["name"] if c else None
    view["created"] = (r["created_at"] or "")[:10]
    return view
