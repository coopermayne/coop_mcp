"""
Read-only data layer for the journal web frontend.

The MCP server (`server.py`) is the single source of truth for how journal data
is shaped, matched and aggregated. We reuse its retrieval functions directly
(they stay plain-callable under fastmcp v3) and only add the handful of reads the
MCP contract doesn't expose: a recent-entry list, an entry's resolved people,
person detail, and the calendar. Nothing here writes.
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
