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
                     rotation: bool = False) -> dict:
    """The exercise catalog as a browsable LIBRARY: every movement in full — muscles in
    their three emphasis tiers (primary/secondary/tertiary), equipment, technique notes,
    common mistakes, cautions, and a form gif/video — plus `in_rotation` (is it in the
    user's programming pool) and how much it's actually been trained (`sessions`,
    `last_done`). Optionally filtered by `muscle` (any tier), a name fragment `q`, or
    `rotation=True` to show only the rotation.

    Also returns `muscles` (the canonical list, for the filter chips), `rotation_count`,
    and the active `muscle`/`q`/`rotation` so the template can render the current filter.
    Read-only; writes go through server.save_exercise / server.set_rotation."""
    from urllib.parse import quote_plus
    muscle = (muscle or "").strip().lower() or None
    q = (q or "").strip() or None
    with server.db() as conn:
        rows = conn.execute("SELECT * FROM exercises ORDER BY name").fetchall()
        # one pass over the muscle map: {exercise_id: {primary:[], secondary:[], tertiary:[]}}
        mmap: dict[int, dict] = {}
        for mr in conn.execute("SELECT exercise_id, muscle, role FROM exercise_muscles ORDER BY muscle"):
            d = mmap.setdefault(mr["exercise_id"],
                                {"primary": [], "secondary": [], "tertiary": []})
            d.setdefault(mr["role"], []).append(mr["muscle"])
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
    for r in rows:
        in_rotation = bool(r["in_rotation"])
        if in_rotation:
            rotation_count += 1
        m = mmap.get(r["id"], {"primary": [], "secondary": [], "tertiary": []})
        all_m = m["primary"] + m["secondary"] + m["tertiary"]
        if rotation and not in_rotation:
            continue
        if muscle and muscle not in all_m:
            continue
        if q and q.lower() not in r["name"].lower():
            continue
        v = vol.get(r["id"], {"sessions": 0, "last_done": None})
        out.append({
            "exercise_id": r["id"], "name": r["name"], "category": r["category"],
            "equipment": r["equipment"], "muscles": m, "in_rotation": in_rotation,
            "level": r["level"], "mechanic": r["mechanic"], "force": r["force"],
            "technique_notes": r["technique_notes"],
            "common_mistakes": r["common_mistakes"], "cautions": r["cautions"],
            "video_link": r["video_link"], "image_link": r["image_link"],
            "youtube_search": "https://www.youtube.com/results?search_query="
                              + quote_plus((r["name"] or "") + " proper form technique"),
            "has_notes": bool(r["technique_notes"] or r["common_mistakes"] or r["cautions"]),
            "sessions": v["sessions"], "last_done": v["last_done"],
        })
    # rotation first, then most-trained, then alphabetical
    out.sort(key=lambda e: (not e["in_rotation"], -e["sessions"], e["name"].lower()))
    return {"exercises": out, "count": len(out), "muscles": server.MUSCLES,
            "rotation_count": rotation_count, "muscle": muscle, "q": q or "",
            "rotation": rotation}


def active_plan() -> dict:
    """The active workout plan for the /trainer page, straight from the server (see
    server.get_workout_plan): {"active": False} or the full plan with exercises, sets
    (target + actual + status), and a done/total progress count."""
    return server.get_workout_plan()


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
# Drinking
# --------------------------------------------------------------------------- #

def drinking(days: int = 30) -> dict:
    """Drink summary plus a gap-filled day-by-day series for the chart.

    Adds two headline averages:
      - avg_alltime: total drinks across all calendar days from the first ever logged
        day through today (sober days count as 0).
      - avg_6d_excl_today: last 6 calendar days NOT counting today, divided by 6.
    """
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

    today_d = date.fromisoformat(server.today())
    with server.db() as conn:
        agg = conn.execute(
            "SELECT MIN(drink_date) AS first, ROUND(SUM(standard_drinks),2) AS total FROM drinks"
        ).fetchone()
        first_iso, total_all = agg["first"], agg["total"] or 0.0
        if first_iso:
            span = (today_d - date.fromisoformat(first_iso)).days + 1
            s["avg_alltime"] = round(total_all / span, 2) if span > 0 else 0.0
        else:
            s["avg_alltime"] = 0.0
        six_start = (today_d - timedelta(days=6)).isoformat()
        six_end = (today_d - timedelta(days=1)).isoformat()
        r6 = conn.execute(
            "SELECT ROUND(SUM(standard_drinks),2) AS t FROM drinks WHERE drink_date BETWEEN ? AND ?",
            (six_start, six_end),
        ).fetchone()
        s["avg_6d_excl_today"] = round((r6["t"] or 0.0) / 6, 2)
    return s


def recent_drinks(limit: int = 30) -> list:
    """Recent drinking days (one row per day), with the `id` the edit/delete forms
    need plus the kind/notes the summary doesn't carry."""
    with server.db() as conn:
        rows = conn.execute(
            "SELECT id, drink_date, standard_drinks, kind, notes FROM drinks "
            "ORDER BY drink_date DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
