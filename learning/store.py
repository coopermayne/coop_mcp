"""Application logic.

Deliberately incurious about pedagogy: it stores facets, decides what is due,
and records what happened. Composing a question and judging an answer are the
model's job at review time, which is what lets the same item be asked a
different way every time it comes up.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import date, timedelta

from . import scheduler
from .db import reindex, session, transaction
from .templates import GRADING_MODES, template_for

HISTORY_DEPTH = 5


def _new_per_day() -> int:
    return int(os.environ.get("TEACHER_NEW_PER_DAY", "8"))


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def _subject_row(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "type": row["type"],
        "context": row["context"],
        "tags": json.loads(row["tags"]),
        "source": row["source"],
        "created_at": row["created_at"],
        "archived": bool(row["archived"]),
        # Flag only: the article text itself is returned by get_subject alone,
        # so search/capture/update returns stay compact.
        "has_article": bool(row["article"]),
    }


def _reference_out(row):
    """`list` facets store a JSON array; everything else stores plain text."""
    if row["reference"] is None:
        return None
    if row["grading_mode"] == "list":
        try:
            return json.loads(row["reference"])
        except json.JSONDecodeError:
            return [row["reference"]]
    return row["reference"]


def _scope(tag: str | None = None, subject_id: str | None = None) -> tuple[str, list]:
    """SQL fragment narrowing a facet query to one tag or one subject.

    Every caller composes this onto a WHERE clause that already aliases facets
    as `f` and subjects as `s`. With both arguments None it returns an empty
    string, which is what keeps the unfiltered calls byte-for-byte the queries
    they were before filtering existed.

    Tags are a JSON array on the subject, so matching goes through json_each
    rather than a LIKE against the serialized array -- 'vocab' must not match a
    subject tagged 'vocabulary'.
    """
    clauses, params = [], []
    if subject_id:
        clauses.append("f.subject_id = ?")
        params.append(subject_id)
    if tag:
        clauses.append(
            "EXISTS (SELECT 1 FROM json_each(s.tags) "
            "WHERE lower(json_each.value) = lower(?))"
        )
        params.append(tag)
    return "".join(f" AND {c}" for c in clauses), params


# An encounter is passive evidence, so it is deliberately worth less than the
# equivalent graded answer. Following a word in context earns Hard rather than
# Good: it moves the card forward but on a shorter interval than recalling it
# cold would, because recognising a word when the sentence around it has already
# narrowed the possibilities is the easier task.
ENCOUNTER_RATINGS = {True: 2, False: 1}

# Kinds that represent the user being asked and answering. `reviewed_today` and
# a facet's visible rating history count these and leave encounters out, so a
# passive brush past a word never reads as a session's worth of work.
GRADED_KINDS = ("review", "study")


# --- capture ---------------------------------------------------------------

def propose_facets(subject_type: str) -> dict:
    """The default facet breakdown for a type, as a starting point for drafting."""
    return {
        "type": subject_type,
        "known_type": subject_type in {"person", "myth", "word", "concept", "case", "event"},
        "facets": template_for(subject_type),
        "note": (
            "Draft a reference for each facet from what you actually know, then call "
            "capture(). Prefer splitting a compound fact into separate facets, each "
            "with its own cue. Where the points share one cue and belong together, "
            "keep them in a single 'list' facet -- but as an array, one point per "
            "entry. Reserve 'recall' for an answer that is genuinely one fact: a "
            "sentence carrying four of them is a list written as prose, and the user "
            "cannot be marked off point by point against it."
        ),
    }


def capture(
    title: str,
    subject_type: str,
    facets: list[dict],
    context: str | None = None,
    tags: list[str] | None = None,
    source: str = "manual",
    article: str | None = None,
) -> dict:
    """Store a subject and its facets. Facets start staged, not due."""
    title = (title or "").strip()
    if not title:
        raise ValueError("title cannot be empty")
    if not facets:
        raise ValueError("at least one facet is required; call propose_facets() first")

    # Every facet is checked before the subject row is written, so a rejected
    # capture leaves nothing behind to collide with the retry.
    specs = _validate_facets(facets, subject_type)

    now = scheduler.now()
    with session() as conn, transaction(conn):
        existing = conn.execute(
            "SELECT id FROM subjects WHERE lower(title) = lower(?)", (title,)
        ).fetchone()
        if existing:
            raise ValueError(
                f"'{title}' already exists (id {existing['id']}). "
                "Use add_facets() to extend it or update_subject() to edit it."
            )

        subject_id = _uid()
        conn.execute(
            """INSERT INTO subjects (id, title, type, context, tags, source, created_at, article)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (subject_id, title, subject_type, context, json.dumps(tags or []), source,
             now.isoformat(), article),
        )
        created = _write_facets(conn, subject_id, specs, now)
        reindex(conn, subject_id)
        subject = _subject_row(conn.execute("SELECT * FROM subjects WHERE id = ?", (subject_id,)).fetchone())

    subject["facets"] = created
    return subject


def _validate_facets(facets: list[dict], subject_type: str | None = None) -> list[dict]:
    """Check and normalize every facet spec BEFORE anything is written.

    Validation is separated from insertion because it used to be interleaved
    with it: facet three failing its grading contract left facets one and two,
    and the subject row above them, already committed. The subject was then
    unfindable (no search document was ever built for it) and re-capturing it
    was refused as a duplicate.

    Splitting the pass is what makes the write atomic in practice; the
    transaction around it is the backstop for everything else that can fail.
    """
    # A facet with no cue of its own inherits the one from its type's template,
    # so seeded and bulk-imported facets still get asked in the right register.
    template_cues = {}
    if subject_type:
        template_cues = {t["name"]: t["cue"] for t in template_for(subject_type)}

    seen = set()
    out = []
    for spec in facets:
        name = (spec.get("name") or "").strip()
        if not name:
            raise ValueError("every facet needs a name")
        # UNIQUE (subject_id, name) would catch this at the INSERT, but only as
        # an IntegrityError naming the constraint rather than the facet.
        if name.lower() in seen:
            raise ValueError(f"facet '{name}': duplicated within this call")
        seen.add(name.lower())

        mode = spec.get("grading_mode", "recall")
        if mode not in GRADING_MODES:
            raise ValueError(f"grading_mode must be one of {GRADING_MODES}, got {mode!r}")

        reference = spec.get("reference")
        scheduled = bool(spec.get("scheduled", True))

        if not scheduled:
            # Background context, never quizzed, so no grading contract to meet.
            if isinstance(reference, list):
                reference = json.dumps(reference)
        elif mode == "list":
            if not isinstance(reference, list) or not reference:
                raise ValueError(f"facet '{name}': list mode needs a non-empty array reference")
            reference = json.dumps(reference)
        elif mode == "recall":
            if not reference:
                raise ValueError(f"facet '{name}': recall mode needs a reference answer")
        elif mode == "open":
            if not spec.get("criteria"):
                raise ValueError(f"facet '{name}': open mode needs criteria to grade against")
            reference = None

        out.append({
            "name": name,
            "grading_mode": mode,
            "reference": reference,
            "criteria": spec.get("criteria"),
            "cue": spec.get("cue") or template_cues.get(name),
            "scheduled": scheduled,
        })
    return out


def _insert_facets(conn, subject_id: str, facets: list[dict], now,
                   subject_type: str | None = None) -> list[dict]:
    """Validate then write. For callers that have nothing to write first."""
    return _write_facets(conn, subject_id, _validate_facets(facets, subject_type), now)


def _write_facets(conn, subject_id: str, specs: list[dict], now) -> list[dict]:
    """Write already-validated facet specs. Call inside a transaction."""
    out = []
    for spec in specs:
        card = scheduler.new_card()
        fields = scheduler.card_fields(card)
        facet_id = _uid()
        conn.execute(
            """INSERT INTO facets
               (id, subject_id, name, grading_mode, reference, criteria, cue,
                scheduled, released, fsrs_card, due, state, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
            (
                facet_id,
                subject_id,
                spec["name"],
                spec["grading_mode"],
                spec["reference"],
                spec["criteria"],
                spec["cue"],
                int(spec["scheduled"]),
                scheduler.dump_card(card),
                fields["due"],
                fields["state"],
                now.isoformat(),
            ),
        )
        out.append({"id": facet_id, "name": spec["name"],
                    "grading_mode": spec["grading_mode"],
                    "scheduled": spec["scheduled"]})
    return out


def add_facets(subject_id: str, facets: list[dict]) -> dict:
    """Extend an existing subject."""
    now = scheduler.now()
    with session() as conn, transaction(conn):
        row = conn.execute("SELECT * FROM subjects WHERE id = ?", (subject_id,)).fetchone()
        if row is None:
            raise ValueError(f"no subject {subject_id!r}")
        created = _insert_facets(conn, subject_id, facets, now, row["type"])
        reindex(conn, subject_id)
    return {"subject_id": subject_id, "added": created}


# --- intake throttle -------------------------------------------------------

def _release_new(conn, now, tag: str | None = None, subject_id: str | None = None) -> int:
    """Let a few staged facets into rotation, capped per day.

    Capture is unlimited on purpose — friction there is what empties a deck.
    The queue is protected here instead, so a burst of capture never turns into
    an accusing backlog three days later.

    The BUDGET is global; only the SELECTION is scoped. Passing a tag therefore
    steers today's intake at a topic rather than granting that topic an intake of
    its own -- see the note in `release_more` for why the cap was left global.
    """
    since = scheduler.day_start().isoformat()
    # Archived subjects are excluded here as they are everywhere else: a facet
    # released today and then archived has stopped being today's intake, and
    # counting it would spend budget on material no longer in rotation.
    used = conn.execute(
        """SELECT COUNT(*) n FROM facets f JOIN subjects s ON s.id = f.subject_id
           WHERE f.released = 1 AND f.released_at >= ? AND s.archived = 0""",
        (since,),
    ).fetchone()["n"]
    budget = _new_per_day() - used
    if budget <= 0:
        return 0

    scope, scope_params = _scope(tag, subject_id)
    rows = conn.execute(
        f"""SELECT id FROM (
               SELECT f.id,
                      ROW_NUMBER() OVER (
                          PARTITION BY s.type ORDER BY s.created_at, f.created_at
                      ) AS type_rank,
                      ROW_NUMBER() OVER (
                          PARTITION BY f.subject_id ORDER BY f.created_at
                      ) AS facet_rank
               FROM facets f JOIN subjects s ON s.id = f.subject_id
               WHERE f.released = 0 AND f.scheduled = 1 AND s.archived = 0{scope}
           )
           ORDER BY type_rank ASC, facet_rank ASC
           LIMIT ?""",
        (*scope_params, budget),
    ).fetchall()
    with transaction(conn):
        for r in rows:
            conn.execute(
                "UPDATE facets SET released = 1, released_at = ?, due = ? WHERE id = ?",
                (now.isoformat(), now.isoformat(), r["id"]),
            )
    return len(rows)


# --- review ----------------------------------------------------------------

def _review_entry(conn, row) -> dict:
    """Everything needed to ASK about a facet, and nothing needed to answer it.

    Past prompts are included so the tutor can vary its phrasing; past responses
    deliberately are not, because a correct earlier answer would hand over the
    target.
    """
    history = conn.execute(
        f"""SELECT reviewed_at, rating, prompt, kind FROM attempts
           WHERE facet_id = ? AND kind IN ({','.join('?' * len(GRADED_KINDS))})
           ORDER BY reviewed_at DESC LIMIT ?""",
        (row["id"], *GRADED_KINDS, HISTORY_DEPTH),
    ).fetchall()
    # Encounters are counted, never rated. Their ratings are a scheduling
    # device, not a judgement of the answer, and showing one as "2/4" next to
    # graded results would invite the tutor to read it as one.
    encounters = conn.execute(
        """SELECT COUNT(*) n, SUM(rating > 1) followed FROM attempts
           WHERE facet_id = ? AND kind = 'encounter'""",
        (row["id"],),
    ).fetchone()
    siblings = conn.execute(
        """SELECT name, reference, criteria FROM facets
           WHERE subject_id = ? AND scheduled = 0""",
        (row["subject_id"],),
    ).fetchall()

    entry = {
        "facet_id": row["id"],
        "subject": row["title"],
        "subject_type": row["type"],
        "subject_context": row["context"],
        "tags": json.loads(row["tags"]),
        "facet": row["name"],
        "grading_mode": row["grading_mode"],
        "cue": row["cue"],
        "reps": row["reps"],
        "lapses": row["lapses"],
        "state": row["state"],
        "is_new": row["reps"] == 0,
        "previously_taught": any(h["kind"] == "study" for h in history),
        "past_prompts": [h["prompt"] for h in history if h["prompt"]],
        "recent_ratings": [f"{h['rating']}/{scheduler.RATING_SCALE}" for h in history],
        "encounters": encounters["n"],
        "encounters_followed": encounters["followed"] or 0,
        "background": {s["name"]: s["reference"] or s["criteria"] for s in siblings},
    }
    if row["grading_mode"] == "list":
        entry["expected_points"] = len(_reference_out(row) or [])
    return entry


def _due_count(conn, now, tag: str | None = None, subject_id: str | None = None) -> int:
    scope, scope_params = _scope(tag, subject_id)
    return conn.execute(
        f"""SELECT COUNT(*) n FROM facets f JOIN subjects s ON s.id = f.subject_id
           WHERE f.scheduled = 1 AND f.released = 1 AND s.archived = 0
             AND f.due <= ?{scope}""",
        (now.isoformat(), *scope_params),
    ).fetchone()["n"]


def _reviewed_today(conn, tag: str | None = None, subject_id: str | None = None) -> int:
    """Cards actually worked through today. Encounters are not cards worked.

    Scoped alongside its caller: in a session narrowed to one tag, a count of
    the whole day's work is not this session's progress, and reporting it as
    `reviewed_today` next to a scoped `remaining` compares two different decks.
    """
    scope, scope_params = _scope(tag, subject_id)
    return conn.execute(
        f"""SELECT COUNT(*) n FROM attempts a
            JOIN facets f ON f.id = a.facet_id
            JOIN subjects s ON s.id = f.subject_id
            WHERE a.reviewed_at >= ?
              AND a.kind IN ({','.join('?' * len(GRADED_KINDS))}){scope}""",
        (scheduler.day_start().isoformat(), *GRADED_KINDS, *scope_params),
    ).fetchone()["n"]


def _encountered_today(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) n FROM attempts WHERE reviewed_at >= ? AND kind = 'encounter'",
        (scheduler.day_start().isoformat(),),
    ).fetchone()["n"]


def release_more(count: int = 5, tag: str | None = None,
                 subject_id: str | None = None) -> dict:
    """Pull extra staged facets into rotation right now, past the daily cap.

    The cap protects you from a queue you can't service; it shouldn't stop you
    on a day you actually want to do more. Deliberately a separate call rather
    than an automatic top-up, so extra work is always something you asked for.

    `tag` / `subject_id` choose WHICH staged facets come in. The daily cap
    itself stays global; a tag narrows the pool, it does not open a second one.
    """
    count = max(1, min(int(count), 30))
    now = scheduler.now()
    scope, scope_params = _scope(tag, subject_id)
    with session() as conn:
        rows = conn.execute(
            f"""SELECT id FROM (
                   SELECT f.id,
                          ROW_NUMBER() OVER (
                              PARTITION BY s.type ORDER BY s.created_at, f.created_at
                          ) AS type_rank,
                          ROW_NUMBER() OVER (
                              PARTITION BY f.subject_id ORDER BY f.created_at
                          ) AS facet_rank
                   FROM facets f JOIN subjects s ON s.id = f.subject_id
                   WHERE f.released = 0 AND f.scheduled = 1 AND s.archived = 0{scope}
               )
               ORDER BY type_rank ASC, facet_rank ASC
               LIMIT ?""",
            (*scope_params, count),
        ).fetchall()
        with transaction(conn):
            for r in rows:
                conn.execute(
                    "UPDATE facets SET released = 1, released_at = ?, due = ? WHERE id = ?",
                    (now.isoformat(), now.isoformat(), r["id"]),
                )
        staged = conn.execute(
            f"""SELECT COUNT(*) n FROM facets f JOIN subjects s ON s.id = f.subject_id
                WHERE f.released = 0 AND f.scheduled = 1 AND s.archived = 0{scope}""",
            scope_params,
        ).fetchone()["n"]
        due_now = _due_count(conn, now, tag, subject_id)

    scoped = tag or subject_id
    return {
        "released": len(rows),
        "due_now": due_now,
        "still_staged": staged,
        "scope": scoped,
        "note": (
            "These count toward today's cap, so nothing more releases automatically "
            "today. Tomorrow resets to the normal daily intake."
            if rows else
            (f"Nothing left staged under {scoped!r}." if scoped
             else "Nothing left staged -- the whole collection is in rotation.")
        ),
    }


def next_card(tag: str | None = None, subject_id: str | None = None) -> dict:
    """The single most overdue facet, with progress.

    Serving one card at a time is deliberate. Handing over a list of eight and
    expecting them to be worked through in order across twenty turns is how
    cards get asked but never graded, or skipped outright.

    `tag` / `subject_id` narrow the session to one topic. With neither, this is
    the whole collection, exactly as before.
    """
    now = scheduler.now()
    scope, scope_params = _scope(tag, subject_id)
    with session() as conn:
        _release_new(conn, now, tag, subject_id)
        row = conn.execute(
            f"""SELECT f.*, s.title, s.type, s.context, s.tags
               FROM facets f JOIN subjects s ON s.id = f.subject_id
               WHERE f.scheduled = 1 AND f.released = 1 AND s.archived = 0
                 AND f.due <= ?{scope}
               ORDER BY f.due ASC LIMIT 1""",
            (now.isoformat(), *scope_params),
        ).fetchone()
        remaining = _due_count(conn, now, tag, subject_id)
        done = _reviewed_today(conn, tag, subject_id)

        if row is None:
            staged = conn.execute(
                f"""SELECT COUNT(*) n FROM facets f JOIN subjects s ON s.id = f.subject_id
                    WHERE f.released = 0 AND f.scheduled = 1 AND s.archived = 0{scope}""",
                scope_params,
            ).fetchone()["n"]
            scoped = tag or subject_id
            return {
                "card": None,
                "done": True,
                "reviewed_today": done,
                "still_staged": staged,
                "scope": scoped,
                "message": (
                    f"Nothing due{f' under {scoped!r}' if scoped else ''}. "
                    f"{done} reviewed today. "
                    + (
                        f"{staged} facets are staged but not yet introduced -- if the "
                        "user wants to keep going, call release_more() to bring some in."
                        if staged else "The whole collection is in rotation."
                    )
                ),
            }
        card = _review_entry(conn, row)

    return {
        "card": card,
        "done": False,
        "reviewed_today": done,
        "remaining_including_this": remaining,
        "rating_scale": f"1-{scheduler.RATING_SCALE}",
        "rating_anchors": scheduler.RATING_ANCHORS,
        "instructions": (
            "Ask about THIS facet only, phrased differently from past_prompts. Do not "
            "reveal or hint at the answer. Wait for the user's answer, then call check(), "
            "grade, and call record() before calling next_card() again. If they say they "
            "do not know it and want to learn it, call study() instead of grading. "
            f"Always show ratings with the scale, e.g. '3/{scheduler.RATING_SCALE}'."
        ),
    }


def due(limit: int = 20, tag: str | None = None, subject_id: str | None = None) -> dict:
    """An overview of what is waiting, for planning a session or answering
    "what's due?".

    Use next_card() to actually run a session -- it serves one facet at a time
    and keeps count, which is what stops cards being asked but never graded.
    Like next_card, this returns nothing needed to answer: no reference, no
    criteria, no past responses.

    `tag` / `subject_id` narrow it to one topic; with neither it is the whole
    collection, as before.
    """
    now = scheduler.now()
    limit = max(1, min(int(limit), 50))
    scope, scope_params = _scope(tag, subject_id)
    with session() as conn:
        released = _release_new(conn, now, tag, subject_id)
        rows = conn.execute(
            f"""SELECT f.*, s.title, s.type, s.context, s.tags
               FROM facets f JOIN subjects s ON s.id = f.subject_id
               WHERE f.scheduled = 1 AND f.released = 1 AND s.archived = 0
                 AND f.due <= ?{scope}
               ORDER BY f.due ASC
               LIMIT ?""",
            (now.isoformat(), *scope_params, limit),
        ).fetchall()

        items = [_review_entry(conn, row) for row in rows]
        remaining = _due_count(conn, now, tag, subject_id)

    return {
        "reviews": items,
        "count": len(items),
        "newly_released": released,
        "more_due_beyond_limit": max(0, remaining - len(items)),
        "scope": tag or subject_id,
        "rating_scale": f"1-{scheduler.RATING_SCALE}",
        "rating_anchors": scheduler.RATING_ANCHORS,
        "instructions": (
            "This is an overview. To run the session call next_card(), which serves "
            "one facet at a time and tracks progress."
        ),
    }


def at_risk(tag: str, n: int = 10, include_new: bool = False,
            subject_id: str | None = None) -> dict:
    """The facets under `tag` closest to being forgotten, most urgent first.

    Ranked on FSRS's own retrievability -- the same memory model that places due
    dates -- rather than a second decay curve invented for this call. A facet
    does not have to be due to be at risk: retrievability falls continuously,
    and the point of this tool is to catch a word on the way down.

    UNLIKE due() and next_card(), this RETURNS THE ANSWER. That is deliberate
    and it is the whole reason the tool exists: the caller is not setting a test,
    it is trying to work a fading word back into what it says next, which it
    cannot do without the word. The cost is that this tool must never be used to
    source a review question -- see the docstring on the server tool.

    Never-reviewed facets are excluded by default. FSRS reports retrievability 0
    for them, which is a placeholder meaning "no memory recorded yet" rather than
    a prediction of certain failure, so leaving them in would park every new word
    at the top of the list and bury the ones actually decaying.
    """
    n = max(1, min(int(n), 50))
    now = scheduler.now()
    scope, scope_params = _scope(tag, subject_id)
    with session() as conn:
        rows = conn.execute(
            f"""SELECT f.*, s.title, s.type, s.tags
               FROM facets f JOIN subjects s ON s.id = f.subject_id
               WHERE f.scheduled = 1 AND f.released = 1 AND s.archived = 0{scope}
                 {'' if include_new else 'AND f.reps > 0'}""",
            scope_params,
        ).fetchall()

        scored = []
        for row in rows:
            card = scheduler.load_card(row["fsrs_card"])
            r = scheduler.retrievability(card, now)
            scored.append((r, row))
        scored.sort(key=lambda pair: pair[0])

        out = []
        for r, row in scored[:n]:
            out.append({
                "facet_id": row["id"],
                "subject": row["title"],
                "facet": row["name"],
                "grading_mode": row["grading_mode"],
                "reference": _reference_out(row),
                "criteria": row["criteria"],
                "cue": row["cue"],
                "recall_probability": round(r, 3),
                "forgetting_probability": round(1 - r, 3),
                "overdue_days": -scheduler.interval_days(card, now),
                "reps": row["reps"],
                "lapses": row["lapses"],
            })

    return {
        "tag": tag,
        "at_risk": out,
        "considered": len(rows),
        "instructions": (
            "Ordered most-at-risk first. Use these in what you say next -- work the "
            "word into your own sentences so it is met in context, and call "
            "record_encounter() for each one that actually came up, saying whether "
            "the user followed it. Do NOT quiz from this list: you are holding the "
            "answers, so anything you ask from here is a prompt you have already "
            "seen the answer to. For a graded test call next_card()."
        ),
    }


# The language-learning half of this file used to sit here as a commented-out
# known_words(): the counterpart to at_risk(), supplying the vocabulary floor to
# build a sentence out of while at_risk() supplied the word to stretch toward.
# It is in git as of eec6afa if a language course is picked back up. The
# `known_at` column it read stays live in db.py -- facets already marked known
# carry it, and dropping the column would orphan them.


def record_encounter(facet_id: str, understood: bool, note: str | None = None) -> dict:
    """Log that a facet went past in conversation and was or wasn't followed.

    Passive comprehension, not a graded attempt. It advances the schedule --
    meeting a word in context is real exposure and should push the next review
    out -- but it is stored as kind='encounter' and stays out of retention,
    because inferring a word from a sentence that has already narrowed the
    possibilities is weaker evidence than recalling it cold.

    `understood=True` advances the card as Hard rather than Good, for the same
    reason. `understood=False` sends it back like a failed recall would, but
    does NOT increment `lapses`: that counter drives `weakest_facets`, and mixing
    passive misses into it would make one number mean two different things.
    """
    now = scheduler.now()
    rating = ENCOUNTER_RATINGS[bool(understood)]
    with session() as conn, transaction(conn):
        row = conn.execute("SELECT * FROM facets WHERE id = ?", (facet_id,)).fetchone()
        if row is None:
            raise ValueError(f"no facet {facet_id!r}")
        if not row["scheduled"]:
            raise ValueError(
                f"facet {facet_id!r} is background context, not a scheduled facet"
            )

        card = scheduler.review(scheduler.load_card(row["fsrs_card"]), rating, now)
        fields = scheduler.card_fields(card)
        conn.execute(
            """UPDATE facets SET fsrs_card = ?, due = ?, state = ?, reps = reps + 1
               WHERE id = ?""",
            (scheduler.dump_card(card), fields["due"], fields["state"], facet_id),
        )
        conn.execute(
            """INSERT INTO attempts
               (facet_id, reviewed_at, rating, prompt, response, critique, kind, prev_card)
               VALUES (?, ?, ?, ?, ?, ?, 'encounter', ?)""",
            (facet_id, now.isoformat(), rating, None,
             "(met in conversation - followed)" if understood
             else "(met in conversation - did not follow)", note, row["fsrs_card"]),
        )
        name = conn.execute(
            """SELECT s.title, f.name FROM facets f JOIN subjects s ON s.id = f.subject_id
               WHERE f.id = ?""", (facet_id,)).fetchone()
        today = _encountered_today(conn)

    return {
        "facet_id": facet_id,
        "subject": name["title"],
        "facet": name["name"],
        "outcome": "understood" if understood else "not understood",
        "next_due": fields["due"],
        "interval_days": scheduler.interval_days(card, now),
        "encounters_today": today,
        "note": "Excluded from retention stats -- context is not recall.",
    }


# The shape every graded response takes. Kept here, next to check()/record(),
# because a format described once in the server instructions is a format the
# model has drifted from by card six -- it needs to arrive with the answer.
#
# One shape for all three grading modes, deliberately. An earlier version let
# 'recall' and 'open' fall back to a prose one-liner on the grounds that they
# have no discrete point list, and that exemption swallowed the format: those
# cards still have several facts in their reference, and a sentence naming what
# was missed is exactly the thing the bullets exist to replace.
GRADED_RESPONSE_FORMAT = (
    "REPLY IN EXACTLY THIS SHAPE, whatever the grading_mode. Nothing before it, "
    "nothing after it.\n"
    "\n"
    "  **{rating}/4**\n"
    "  - ~~**{point they got}**: {one concise sentence}~~\n"
    "  - ~~**{point they got}**: {one concise sentence}~~\n"
    "  - **{point they missed}**: {one concise sentence}\n"
    "  - **{point they missed}**: {one concise sentence}\n"
    "\n"
    "ONE BULLET PER POINT, ALWAYS. Under 'list' the points are the reference "
    "points. Under 'recall' and 'open' the reference is not pre-split, so split "
    "it yourself -- one bullet per fact in it, at the granularity you would "
    "test on. There is no prose fallback for those modes: never answer with a "
    "'Note:' line, a summary sentence, or anything but the score and the "
    "bullets.\n"
    "\n"
    "Each bullet is a bolded label of a few words -- a name, a date, a term -- "
    "then a colon and ONE concise sentence of detail. Bold the label only.\n"
    "\n"
    "Points the user covered are struck through, WHOLE BULLET INCLUDING THE "
    "BOLD LABEL, as ~~**label**: detail~~. Points they missed or got wrong stay "
    "plain: those are the only part they have to read, and the detail on them "
    "is what they learn from this card, so make it carry its weight. On a 4/4 "
    "every bullet is struck through and the list still appears -- the full "
    "strikethrough IS the confirmation.\n"
    "\n"
    "No connecting prose. Do not restate the question, do not write 'You got X "
    "but missed Y', do not add a summary before or after. Longer explanation, "
    "etymology and background stay out unless the user asks a follow-up."
)

# Teaching is the same shape with nothing struck through, so what gets taught
# and what gets graded look alike -- the card is met twice in one format.
TEACHING_RESPONSE_FORMAT = (
    "REPLY IN EXACTLY THIS SHAPE. Nothing before it, nothing after it.\n"
    "\n"
    "  - **{short label}**: {one concise sentence, not a paragraph}\n"
    "  - **{short label}**: {one concise sentence}\n"
    "\n"
    "The graded format with nothing struck through, because nothing was "
    "recalled. One bullet per beat, figure or fact -- the same granularity as "
    "the card's reference list where it has one, and where it has none, the "
    "granularity you would grade on. Bold the label only, never the whole "
    "line. No lead-in and no wrap-up: the bullets are the entire answer."
)


def _point_key(point: str) -> str:
    """Loose match key for a reference point, so `covered` survives rewording."""
    return " ".join(str(point).lower().split())


def _coverage_history(conn, row) -> list[dict] | None:
    """How often each reference point of a `list` facet has been missed.

    `covered` has been written on every graded list attempt since the column
    existed and read by nothing, which made it the one stored field that could
    answer "which point of this do they always drop" -- the thing a facet
    reviewed eight times at 3/4 is actually hiding.

    Only ever returned from check(). The points ARE the answer, so this cannot
    go anywhere next_card() or due() can reach.
    """
    if row["grading_mode"] != "list":
        return None
    points = _reference_out(row) or []
    if not points:
        return None

    attempts = conn.execute(
        "SELECT covered FROM attempts WHERE facet_id = ? AND kind = 'review'",
        (row["id"],),
    ).fetchall()
    if not attempts:
        return None

    tally = {_point_key(p): 0 for p in points}
    for a in attempts:
        try:
            hit = json.loads(a["covered"] or "[]")
        except json.JSONDecodeError:
            continue
        for h in hit:
            key = _point_key(h)
            if key in tally:
                tally[key] += 1

    n = len(attempts)
    return [
        {"point": p, "covered": tally[_point_key(p)], "of": n,
         "missed": n - tally[_point_key(p)]}
        for p in points
    ]


def check(facet_id: str) -> dict:
    """Reveal the reference answer, after the user has already committed to theirs."""
    with session() as conn:
        row = conn.execute(
            """SELECT f.*, s.title FROM facets f JOIN subjects s ON s.id = f.subject_id
               WHERE f.id = ?""",
            (facet_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"no facet {facet_id!r}")
        coverage = _coverage_history(conn, row)
    return {
        "facet_id": facet_id,
        "subject": row["title"],
        "facet": row["name"],
        "grading_mode": row["grading_mode"],
        "reference": _reference_out(row),
        "criteria": row["criteria"],
        "rating_anchors": scheduler.RATING_ANCHORS,
        "past_coverage": coverage,
        "grading_note": (
            "list: rate by coverage of the reference points and pass `covered`. "
            "open: judge against criteria, not wording. "
            "Grade what they actually said, not what they seemed to mean."
        )
        + (
            " `past_coverage` counts how often each point has been covered "
            "across previous graded reviews of this facet. A point missed every "
            "time is the blind spot the rating alone hides -- spend the "
            "sentence on that bullet, not on the ones they always get."
            if coverage else ""
        ),
        "response_format": GRADED_RESPONSE_FORMAT,
    }


def record(
    facet_id: str,
    rating: int,
    prompt: str | None = None,
    response: str | None = None,
    critique: str | None = None,
    covered: list[str] | None = None,
    kind: str = "review",
) -> dict:
    """Log a graded attempt and advance the schedule."""
    now = scheduler.now()
    with session() as conn, transaction(conn):
        row = conn.execute("SELECT * FROM facets WHERE id = ?", (facet_id,)).fetchone()
        if row is None:
            raise ValueError(f"no facet {facet_id!r}")

        card = scheduler.review(scheduler.load_card(row["fsrs_card"]), rating, now)
        fields = scheduler.card_fields(card)
        conn.execute(
            """UPDATE facets SET fsrs_card = ?, due = ?, state = ?,
                   reps = reps + 1, lapses = lapses + ?
               WHERE id = ?""",
            (scheduler.dump_card(card), fields["due"], fields["state"],
             1 if rating == 1 else 0, facet_id),
        )
        conn.execute(
            """INSERT INTO attempts
               (facet_id, reviewed_at, rating, prompt, response, critique, covered,
                kind, prev_card)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (facet_id, now.isoformat(), rating, prompt, response, critique,
             json.dumps(covered or []), kind, row["fsrs_card"]),
        )
        updated = conn.execute("SELECT reps, lapses FROM facets WHERE id = ?", (facet_id,)).fetchone()

    return {
        "facet_id": facet_id,
        "rating": f"{rating}/{scheduler.RATING_SCALE}",
        "next_due": fields["due"],
        "interval_days": scheduler.interval_days(card, now),
        "reps": updated["reps"],
        "lapses": updated["lapses"],
        "response_format": GRADED_RESPONSE_FORMAT,
    }


def study(facet_id: str, note: str | None = None, days: float = 1) -> dict:
    """Record that a facet was TAUGHT rather than tested.

    For when the answer is "I don't know it" and the useful move is to learn the
    thing rather than score a miss. The card comes back soon, but at least a day
    out -- long enough that recalling it is real recall and not short-term echo.

    Kept separate from record() on purpose: being taught something new is not a
    failed recall, and counting it as one would drag retention down and make the
    numbers useless for spotting genuine trouble.
    """
    now = scheduler.now()
    with session() as conn, transaction(conn):
        row = conn.execute("SELECT * FROM facets WHERE id = ?", (facet_id,)).fetchone()
        if row is None:
            raise ValueError(f"no facet {facet_id!r}")

        card = scheduler.review(scheduler.load_card(row["fsrs_card"]), 1, now)
        card = scheduler.defer(card, max(days, 1))
        fields = scheduler.card_fields(card)
        # Note: reps advances, lapses does not. You cannot lapse on something
        # you never knew.
        conn.execute(
            """UPDATE facets SET fsrs_card = ?, due = ?, state = ?, reps = reps + 1
               WHERE id = ?""",
            (scheduler.dump_card(card), fields["due"], fields["state"], facet_id),
        )
        conn.execute(
            """INSERT INTO attempts
               (facet_id, reviewed_at, rating, prompt, response, critique, kind, prev_card)
               VALUES (?, ?, 1, ?, ?, ?, 'study', ?)""",
            (facet_id, now.isoformat(), None, "(did not know it - taught)", note,
             row["fsrs_card"]),
        )
        name = conn.execute(
            """SELECT s.title, f.name FROM facets f JOIN subjects s ON s.id = f.subject_id
               WHERE f.id = ?""", (facet_id,)).fetchone()

    return {
        "facet_id": facet_id,
        "subject": name["title"],
        "facet": name["name"],
        "outcome": "taught",
        "next_due": fields["due"],
        "interval_days": scheduler.interval_days(card, now),
        "note": "Excluded from retention stats -- it was taught, not tested.",
        "response_format": TEACHING_RESPONSE_FORMAT,
    }


# --- corrections -----------------------------------------------------------

def undo_last(facet_id: str) -> dict:
    """Take back the most recent attempt on a facet, schedule included.

    The escape hatch for a grade that was wrong: a 1/4 recorded on an answer
    the user actually gave, a record() that should have been a study(), an
    encounter logged against the wrong facet. Without this, a slip is
    permanent -- it moves the due date, it counts in retention, and no other
    tool can reach it.

    FSRS cannot be run backwards, so the restore works from `prev_card`: the
    card blob as it stood before the attempt, saved at write time. Attempts
    recorded before that column existed cannot be undone this way, and the
    error says so rather than half-undoing them.

    reps always goes back by one; `lapses` only when the attempt was the thing
    that incremented it (a graded review rated 1/4). Undoing twice walks back
    two attempts, which is intended -- there is no redo.
    """
    with session() as conn, transaction(conn):
        row = conn.execute("SELECT * FROM facets WHERE id = ?", (facet_id,)).fetchone()
        if row is None:
            raise ValueError(f"no facet {facet_id!r}")
        attempt = conn.execute(
            "SELECT * FROM attempts WHERE facet_id = ? ORDER BY reviewed_at DESC, id DESC LIMIT 1",
            (facet_id,),
        ).fetchone()
        if attempt is None:
            raise ValueError(f"facet {facet_id!r} has no attempts to undo")
        if attempt["prev_card"] is None:
            raise ValueError(
                "this attempt predates undo support, so the schedule it replaced was "
                "never saved and cannot be restored. update_facet() can still fix the "
                "facet's content."
            )

        card = scheduler.load_card(attempt["prev_card"])
        fields = scheduler.card_fields(card)
        # Mirrors record(): only a graded review rated 1/4 ever added a lapse.
        was_lapse = attempt["kind"] == "review" and attempt["rating"] == 1
        conn.execute(
            """UPDATE facets SET fsrs_card = ?, due = ?, state = ?,
                   reps = MAX(reps - 1, 0), lapses = MAX(lapses - ?, 0)
               WHERE id = ?""",
            (attempt["prev_card"], fields["due"], fields["state"],
             1 if was_lapse else 0, facet_id),
        )
        conn.execute("DELETE FROM attempts WHERE id = ?", (attempt["id"],))
        updated = conn.execute(
            """SELECT f.reps, f.lapses, f.due, s.title, f.name FROM facets f
               JOIN subjects s ON s.id = f.subject_id WHERE f.id = ?""",
            (facet_id,),
        ).fetchone()

    return {
        "facet_id": facet_id,
        "subject": updated["title"],
        "facet": updated["name"],
        "undone": {
            "kind": attempt["kind"],
            "reviewed_at": attempt["reviewed_at"],
            "rating": (f"{attempt['rating']}/{scheduler.RATING_SCALE}"
                       if attempt["kind"] == "review" else None),
        },
        "restored_due": updated["due"],
        "reps": updated["reps"],
        "lapses": updated["lapses"],
        "note": "The attempt is deleted, not marked void -- it leaves no trace in stats.",
    }


def delete_facet(facet_id: str) -> dict:
    """Delete a facet and its whole attempt history. Irreversible.

    Prefer update_facet(scheduled=False) for a facet that is merely wrong to
    quiz -- that keeps the history and the content, and is what `scheduled`
    exists for. Delete is for a facet that should never have been captured.
    """
    with session() as conn, transaction(conn):
        row = conn.execute(
            """SELECT f.*, s.title FROM facets f JOIN subjects s ON s.id = f.subject_id
               WHERE f.id = ?""",
            (facet_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"no facet {facet_id!r}")
        attempts = conn.execute(
            "SELECT COUNT(*) n FROM attempts WHERE facet_id = ?", (facet_id,)
        ).fetchone()["n"]
        conn.execute("DELETE FROM facets WHERE id = ?", (facet_id,))
        reindex(conn, row["subject_id"])

    return {"deleted": "facet", "facet_id": facet_id, "subject": row["title"],
            "facet": row["name"], "attempts_deleted": attempts}


def delete_subject(subject_id: str, confirm_title: str) -> dict:
    """Delete a subject, every facet under it, and their history. Irreversible.

    `confirm_title` must match the subject's title. The guard is against a
    wrong id rather than against intent: ids are opaque twelve-character hex,
    nothing distinguishes a right one from a near miss on sight, and the cost
    of getting it wrong is months of review history with no undo behind it.

    Prefer update_subject(archived=True) in almost every case. Archiving takes
    a subject out of review, search and intake while keeping what it recorded;
    this exists for material captured in error.
    """
    with session() as conn, transaction(conn):
        row = conn.execute("SELECT * FROM subjects WHERE id = ?", (subject_id,)).fetchone()
        if row is None:
            raise ValueError(f"no subject {subject_id!r}")
        if (confirm_title or "").strip().lower() != row["title"].lower():
            raise ValueError(
                f"confirm_title does not match: subject {subject_id!r} is "
                f"{row['title']!r}. Pass the title exactly to confirm the deletion."
            )
        counts = conn.execute(
            """SELECT COUNT(f.id) facets,
                      (SELECT COUNT(*) FROM attempts a JOIN facets f2 ON f2.id = a.facet_id
                       WHERE f2.subject_id = ?) attempts
               FROM facets f WHERE f.subject_id = ?""",
            (subject_id, subject_id),
        ).fetchone()
        # Facets and their attempts go by ON DELETE CASCADE; the FTS document
        # is a manual table and has to be cleared by hand.
        conn.execute("DELETE FROM subjects WHERE id = ?", (subject_id,))
        conn.execute("DELETE FROM learn_fts WHERE subject_id = ?", (subject_id,))

    return {"deleted": "subject", "subject_id": subject_id, "title": row["title"],
            "facets_deleted": counts["facets"], "attempts_deleted": counts["attempts"]}


# --- browse ----------------------------------------------------------------

def get_subject(subject_id: str) -> dict:
    """One subject, whole: full context, tags, and every facet with its content.

    The read path for a subject used as a STATE STORE rather than as review
    material -- a running course log, say, kept in `context` and rewritten each
    session. Nothing here is abridged, because a state store that hands back a
    summary of itself is not a state store.

    Unscheduled facets are included alongside scheduled ones; search() omits
    facet content entirely and next_card() shows only the facet it is asking
    about, so this is the only way to see a subject's full contents at once.
    That includes references, so treat it the way you treat check(): not
    something to call while a review question is still open.
    """
    with session() as conn:
        row = conn.execute("SELECT * FROM subjects WHERE id = ?", (subject_id,)).fetchone()
        if row is None:
            raise ValueError(
                f"no subject {subject_id!r} -- search() returns ids if you only have a title"
            )
        subject = _subject_row(row)
        facets = conn.execute(
            "SELECT * FROM facets WHERE subject_id = ? ORDER BY created_at", (subject_id,)
        ).fetchall()

    subject["facets"] = [
        {
            "id": f["id"],
            "name": f["name"],
            "grading_mode": f["grading_mode"],
            "reference": _reference_out(f),
            "criteria": f["criteria"],
            "cue": f["cue"],
            "scheduled": bool(f["scheduled"]),
            "released": bool(f["released"]),
            "due": f["due"],
            "state": f["state"],
            "reps": f["reps"],
            "lapses": f["lapses"],
        }
        for f in facets
    ]
    subject["facet_count"] = len(subject["facets"])
    subject["scheduled_count"] = sum(1 for f in subject["facets"] if f["scheduled"])
    subject["article"] = row["article"]
    return subject


def _fts_query(text: str) -> str | None:
    """Turn whatever the user typed into a safe FTS5 MATCH expression.

    MATCH takes a query language, not a string, and the failure mode is an
    exception rather than no results: a hyphen parses as a column filter
    ('foo-bar' -> "no such column: bar"), a trailing 'OR' is a syntax error,
    and an odd quote is an unterminated string. Every one of those is an
    ordinary thing to type into a search box.

    So each word is extracted and quoted as a literal. That gives up the FTS
    operators, which nothing here was documented as offering anyway, and buys
    a search that cannot crash on a title with a hyphen in it. Words are
    space-joined, which FTS5 reads as AND -- the same behaviour a plain
    multi-word query had before.

    Returns None when the text holds no searchable word at all.
    """
    words = re.findall(r"[^\W_]+(?:['’][^\W_]+)*", text, re.UNICODE)
    if not words:
        return None
    return " ".join(f'"{w}"' for w in words)


def search(query: str, limit: int = 15) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []
    match = _fts_query(query)
    if match is None:
        return []
    with session() as conn:
        rows = conn.execute(
            """SELECT s.* FROM learn_fts
               JOIN subjects s ON s.id = learn_fts.subject_id
               WHERE learn_fts MATCH ? AND s.archived = 0
               ORDER BY rank LIMIT ?""",
            (match, max(1, min(limit, 50))),
        ).fetchall()
        out = []
        for row in rows:
            subject = _subject_row(row)
            facets = conn.execute(
                "SELECT id, name, grading_mode, due, reps, lapses, scheduled FROM facets WHERE subject_id = ?",
                (row["id"],),
            ).fetchall()
            subject["facets"] = [dict(f) for f in facets]
            out.append(subject)
    return out


def update_subject(subject_id: str, title: str | None = None, context: str | None = None,
                   tags: list[str] | None = None, archived: bool | None = None,
                   article: str | None = None) -> dict:
    sets, params = [], []
    for col, val in (("title", title), ("context", context)):
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(val)
    if article is not None:
        # Replaced wholesale like context; an empty string clears it back to
        # "no article" (NULL) rather than storing a blank page.
        sets.append("article = ?")
        params.append(article or None)
    if tags is not None:
        sets.append("tags = ?")
        params.append(json.dumps(tags))
    if archived is not None:
        sets.append("archived = ?")
        params.append(int(archived))
    if not sets:
        raise ValueError("nothing to update")
    params.append(subject_id)

    with session() as conn, transaction(conn):
        conn.execute(f"UPDATE subjects SET {', '.join(sets)} WHERE id = ?", params)
        row = conn.execute("SELECT * FROM subjects WHERE id = ?", (subject_id,)).fetchone()
        if row is None:
            raise ValueError(f"no subject {subject_id!r}")
        reindex(conn, subject_id)
    return _subject_row(row)


def update_facet(facet_id: str, reference=None, criteria: str | None = None,
                 cue: str | None = None, scheduled: bool | None = None) -> dict:
    """Fix a badly-formed card. Scheduling state is preserved."""
    with session() as conn, transaction(conn):
        row = conn.execute("SELECT * FROM facets WHERE id = ?", (facet_id,)).fetchone()
        if row is None:
            raise ValueError(f"no facet {facet_id!r}")
        sets, params = [], []
        if reference is not None:
            if row["grading_mode"] == "list" and not isinstance(reference, list):
                raise ValueError("list mode requires an array reference")
            sets.append("reference = ?")
            params.append(json.dumps(reference) if isinstance(reference, list) else reference)
        for col, val in (("criteria", criteria), ("cue", cue)):
            if val is not None:
                sets.append(f"{col} = ?")
                params.append(val)
        if scheduled is not None:
            sets.append("scheduled = ?")
            params.append(int(scheduled))
        if not sets:
            raise ValueError("nothing to update")
        params.append(facet_id)
        conn.execute(f"UPDATE facets SET {', '.join(sets)} WHERE id = ?", params)
        reindex(conn, row["subject_id"])
        updated = conn.execute("SELECT * FROM facets WHERE id = ?", (facet_id,)).fetchone()
    return {"facet_id": facet_id, "name": updated["name"],
            "reference": _reference_out(updated), "criteria": updated["criteria"],
            "scheduled": bool(updated["scheduled"])}


def stats(days: int = 30) -> dict:
    now = scheduler.now()
    since = (now - timedelta(days=max(1, days))).isoformat()
    with session() as conn:
        # `facets` counted everything including context facets, while
        # `released` could only ever count scheduled ones -- two columns side
        # by side that were not measuring the same population, so `released`
        # always looked short of `facets` by however much background material
        # a type carried. Split out instead: the three now add up.
        by_type = conn.execute(
            """SELECT s.type,
                      COUNT(f.id) facets,
                      COALESCE(SUM(f.scheduled = 0), 0) context_only,
                      COALESCE(SUM(f.scheduled = 1 AND f.released = 1), 0) released,
                      COALESCE(SUM(f.scheduled = 1 AND f.released = 0), 0) staged,
                      COUNT(DISTINCT s.id) subjects
               FROM subjects s LEFT JOIN facets f ON f.subject_id = s.id
               WHERE s.archived = 0 GROUP BY s.type"""
        ).fetchall()
        due_now = conn.execute(
            """SELECT COUNT(*) n FROM facets f JOIN subjects s ON s.id = f.subject_id
               WHERE f.scheduled = 1 AND f.released = 1 AND s.archived = 0 AND f.due <= ?""",
            (now.isoformat(),),
        ).fetchone()["n"]
        staged = conn.execute(
            """SELECT COUNT(*) n FROM facets f JOIN subjects s ON s.id = f.subject_id
               WHERE f.released = 0 AND f.scheduled = 1 AND s.archived = 0"""
        ).fetchone()["n"]
        attempts = conn.execute(
            "SELECT rating FROM attempts WHERE reviewed_at >= ? AND kind = 'review'",
            (since,),
        ).fetchall()
        taught = conn.execute(
            "SELECT COUNT(*) n FROM attempts WHERE reviewed_at >= ? AND kind = 'study'",
            (since,),
        ).fetchone()["n"]
        met = conn.execute(
            """SELECT COUNT(*) n, SUM(rating > 1) followed FROM attempts
               WHERE reviewed_at >= ? AND kind = 'encounter'""",
            (since,),
        ).fetchone()
        weak = conn.execute(
            """SELECT s.title, f.name, f.lapses, f.reps FROM facets f
               JOIN subjects s ON s.id = f.subject_id
               WHERE f.lapses > 0 AND s.archived = 0
               ORDER BY f.lapses DESC, f.reps ASC LIMIT 10"""
        ).fetchall()
        # Points dropped most often across `list` reviews. Counts only, never
        # the point text: stats() is safe to call mid-session and must stay so.
        list_attempts = conn.execute(
            """SELECT a.covered, f.reference FROM attempts a
               JOIN facets f ON f.id = a.facet_id
               WHERE a.reviewed_at >= ? AND a.kind = 'review'
                 AND f.grading_mode = 'list'""",
            (since,),
        ).fetchall()
        # A facet passed many times in only one mode is a blind spot, not mastery.
        by_mode = conn.execute(
            """SELECT f.grading_mode, COUNT(*) n, AVG(a.rating) avg_rating
               FROM attempts a JOIN facets f ON f.id = a.facet_id
               WHERE a.reviewed_at >= ? AND a.kind = 'review'
               GROUP BY f.grading_mode""",
            (since,),
        ).fetchall()

    n = len(attempts)

    # Whether list cards are being answered whole or half-answered for a 3/4.
    points_expected = points_covered = 0
    for a in list_attempts:
        try:
            expected = json.loads(a["reference"] or "[]")
            hit = json.loads(a["covered"] or "[]")
        except json.JSONDecodeError:
            continue
        if not expected:
            continue
        points_expected += len(expected)
        keys = {_point_key(p) for p in expected}
        points_covered += sum(1 for h in hit if _point_key(h) in keys)

    return {
        "window_days": days,
        "by_type": [dict(r) for r in by_type],
        "due_now": due_now,
        "staged_not_yet_introduced": staged,
        "graded_reviews_in_window": n,
        "taught_in_window": taught,
        "encounters_in_window": met["n"],
        "encounters_followed": met["followed"] or 0,
        "rating_scale": f"1-{scheduler.RATING_SCALE}",
        "retention": round(sum(1 for a in attempts if a["rating"] >= 3) / n, 3) if n else None,
        "retention_note": (
            "Retention is graded reviews only. Taught cards and conversational "
            "encounters are counted above but excluded, so this stays a measure of "
            "cold recall."
        ),
        "by_grading_mode": [dict(r) for r in by_mode],
        "list_point_coverage": (
            round(points_covered / points_expected, 3) if points_expected else None
        ),
        "list_point_coverage_note": (
            "Share of reference points actually covered across graded 'list' "
            "reviews in the window. Well below the retention figure means list "
            "cards are passing on partial answers. check() shows which points, "
            "per facet, at grading time."
        ),
        "weakest_facets": [dict(r) for r in weak],
    }
