"""
Read-only data layer for the journal web frontend.

The MCP server (`server.py`) is the single source of truth for how journal data
is shaped, matched and aggregated. We reuse its retrieval functions directly
(they stay plain-callable under fastmcp v3) and only add the handful of reads the
MCP contract doesn't expose: a recent-entry list, an entry's resolved people,
full workouts-with-sets, person detail, a gap-filled drink series, and the
dashboard roll-up. Nothing here writes.
"""

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
            "ORDER BY entry_date DESC, id DESC LIMIT ? OFFSET ?",
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


def list_days(limit_entries: int = 120) -> dict:
    """Recent entries grouped into days, newest day first; within a day the entries
    read top-to-bottom in the order they were recorded (so the day reads as one
    block of prose split into per-topic paragraphs). Each entry carries its resolved
    people so the feed can link names inline. Bodies are returned in full — entries
    are now small per-topic notes, not the day's whole dump."""
    with server.db() as conn:
        rows = conn.execute(
            "SELECT id, entry_date, body FROM entries "
            "ORDER BY entry_date DESC, id ASC LIMIT ?",
            (limit_entries,),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"]
        entries = [
            {"entry_id": r["id"], "entry_date": r["entry_date"], "body": r["body"]}
            for r in rows
        ]
        people = _people_for_entries(conn, [e["entry_id"] for e in entries])
    days: list[dict] = []
    for e in entries:
        e["people"] = people.get(e["entry_id"], [])
        if not days or days[-1]["date"] != e["entry_date"]:
            days.append({"date": e["entry_date"], "entries": []})
        days[-1]["entries"].append(e)
    return {"days": days, "total": total}


def entry_with_people(entry_id: int):
    """Cleaned entry plus the people resolved within it. The verbatim raw_body
    is a hidden backup — not fetched or shown on the web."""
    e = server.get_entry(entry_id, include_raw=False)
    if "error" in e:
        return None
    with server.db() as conn:
        rows = conn.execute(
            """SELECT m.surface_form, m.status, p.id AS pid, p.canonical_name, p.role
               FROM mentions m LEFT JOIN people p ON p.id = m.person_id
               WHERE m.entry_id = ? ORDER BY m.id""",
            (entry_id,),
        ).fetchall()
    e["mentions"] = [
        {
            "surface_form": r["surface_form"],
            "status": r["status"],
            "person_id": r["pid"],
            "name": r["canonical_name"],
            "role": r["role"],
        }
        for r in rows
    ]
    return e


def pending_mentions(limit: int = 200) -> list:
    """The resolution queue, enriched for the web view: each pending mention with
    its surface form, context snippet, entry_date, entry_id (so you can jump to
    the entry), and the same candidate matches `list_pending_mentions` returns —
    this UI is read-only, so resolution still happens in conversation with Claude.
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

def person_detail(person_id: int, history_limit: int = 60):
    """Everything the read UI shows for one person."""
    with server.db() as conn:
        p = conn.execute(
            "SELECT id, canonical_name, role, summary, notes, email, phone, address "
            "FROM people WHERE id = ?",
            (person_id,),
        ).fetchone()
        if not p:
            return None
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
        "email": p["email"],
        "phone": p["phone"],
        "address": p["address"],
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
    """Recent sessions with their sets grouped by exercise (first-seen order)."""
    out = []
    with server.db() as conn:
        ws = conn.execute(
            "SELECT id, workout_date, focus, feeling, notes FROM workouts "
            "ORDER BY workout_date DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        for w in ws:
            srows = conn.execute(
                """SELECT s.weight_lbs, s.reps, s.rpe, s.note,
                          e.id AS eid, e.name AS ename, e.category
                   FROM sets s JOIN exercises e ON e.id = s.exercise_id
                   WHERE s.workout_id = ? ORDER BY s.id""",
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
                     "rpe": s["rpe"], "note": s["note"]}
                )
            exercises = [by_ex[i] for i in order]
            out.append({
                "workout_id": w["id"],
                "date": w["workout_date"],
                "focus": w["focus"],
                "feeling": w["feeling"],
                "notes": w["notes"],
                "exercises": exercises,
                "exercise_count": len(exercises),
                "set_count": len(srows),
            })
    return out


# --------------------------------------------------------------------------- #
# Drinking
# --------------------------------------------------------------------------- #

def drinking(days: int = 30) -> dict:
    """Drink summary plus a gap-filled day-by-day series for the chart."""
    s = server.get_drink_summary(days=days)
    by_date = {d["date"]: d["total"] for d in s["daily"]}
    start = date.fromisoformat(s["since"])
    end = date.fromisoformat(s["until"])
    series = []
    cur = start
    while cur <= end:
        iso = cur.isoformat()
        series.append({"date": iso, "total": by_date.get(iso, 0.0)})
        cur += timedelta(days=1)
    s["series"] = series
    s["peak"] = max([x["total"] for x in series] + [1.0])
    return s


def recent_drinks(limit: int = 30) -> list:
    """Individual drinking occasions (kind/notes the summary doesn't carry)."""
    with server.db() as conn:
        rows = conn.execute(
            "SELECT drink_date, standard_drinks, kind, notes FROM drinks "
            "ORDER BY drink_date DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #

def dashboard() -> dict:
    """One roll-up for the landing page."""
    with server.db() as conn:
        entries_n = conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"]
        people_n = conn.execute("SELECT COUNT(*) AS n FROM people").fetchone()["n"]
        pending_n = conn.execute(
            "SELECT COUNT(*) AS n FROM mentions WHERE status='pending'"
        ).fetchone()["n"]
        workouts_n = conn.execute("SELECT COUNT(*) AS n FROM workouts").fetchone()["n"]
        last_workout = conn.execute(
            "SELECT MAX(workout_date) AS d FROM workouts"
        ).fetchone()["d"]
    drink = server.get_drink_summary(days=30)
    return {
        "entries_count": entries_n,
        "people_count": people_n,
        "pending_mentions": pending_n,
        "workouts_count": workouts_n,
        "last_workout": last_workout,
        "days_since_workout": server._days_since(last_workout),
        "sober_streak": drink.get("current_sober_streak"),
        "drinks_30d": drink.get("total_standard_drinks"),
        "recent_entries": list_entries(limit=5)["entries"],
        "last_session": (workouts_full(limit=1) or [None])[0],
    }
