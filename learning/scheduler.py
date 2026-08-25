"""FSRS wrapper. Nothing outside this module touches card internals.

Two deliberate deviations from library defaults, both because this is a
once-a-day review habit rather than a cramming tool:

* `learning_steps=()` — a new card graduates straight to a day-scale interval
  instead of reappearing 1 and 10 minutes later. Sub-session repeats pad the
  queue without teaching much.
* `relearning_steps=(10 min,)` — a card you *failed*, by contrast, should come
  back before you close the session. Getting it right once after missing it is
  most of the value of noticing you missed it.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fsrs import Card, Rating, Scheduler

RATINGS = {1: Rating.Again, 2: Rating.Hard, 3: Rating.Good, 4: Rating.Easy}

RATING_SCALE = 4

# The denominator lives in the text so it survives into any surface that shows a
# rating. A bare "2" is unreadable without knowing the scale.
RATING_ANCHORS = {
    1: "1/4 - Blank, or wrong in substance. No meaningful recall.",
    2: "2/4 - Got there, but only with heavy prompting or after a long struggle.",
    3: "3/4 - Correct after a pause or with minor gaps.",
    4: "4/4 - Immediate, fluent, complete.",
}

_scheduler = Scheduler(
    desired_retention=float(os.environ.get("TEACHER_RETENTION", "0.9")),
    learning_steps=(),
    relearning_steps=(timedelta(minutes=10),),
)


def now() -> datetime:
    return datetime.now(timezone.utc)


PACIFIC = ZoneInfo("America/Los_Angeles")


def day_start() -> datetime:
    """Pacific midnight, expressed in UTC.

    Days must be bucketed in the user's timezone, not UTC. Reviewing at 7pm
    Pacific is 02:00 UTC the following day, so a UTC boundary silently merges an
    evening session with the next morning's and refuses to release new cards.
    PACIFIC rather than system-local (which the standalone teacher repo used),
    because in the Docker deploy "local" IS UTC — the exact bug this function
    exists to avoid — and every other user-facing date in this repo already
    rolls over at Pacific midnight.
    """
    pacific_midnight = datetime.now(PACIFIC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return pacific_midnight.astimezone(timezone.utc)


def new_card() -> Card:
    return Card()


def load_card(blob: str) -> Card:
    return Card.from_json(blob)


def dump_card(card: Card) -> str:
    return card.to_json()


def review(card: Card, rating: int, when: datetime | None = None) -> Card:
    if rating not in RATINGS:
        raise ValueError(f"rating must be 1-4, got {rating!r}")
    updated, _log = _scheduler.review_card(card, RATINGS[rating], review_datetime=when or now())
    return updated


def defer(card: Card, days: float, from_time: datetime | None = None) -> Card:
    """Push a card's due date out to at least `days` from now.

    FSRS sends a lapsed mature card back in ten minutes, which is right for a
    card you fumbled and wrong for one you were just taught. This only ever
    delays a card, never pulls it forward.
    """
    floor = (from_time or now()) + timedelta(days=days)
    if card.due < floor:
        card.due = floor
    return card


def retrievability(card: Card, when: datetime | None = None) -> float:
    """FSRS's own estimate that this card would be recalled right now, 0-1.

    This is the model the scheduler already uses to place due dates, asked
    directly instead of through a due date. `at_risk` ranks on it so that
    "going stale" means the same thing as it does everywhere else in the app --
    a second decay curve would drift from the one actually driving reviews.

    A card that has never been reviewed has no memory to decay and FSRS returns
    0 for it, which is a placeholder rather than a prediction. Callers ranking
    by this value have to decide what to do with those; see `store.at_risk`.
    """
    return _scheduler.get_card_retrievability(card, when or now())


def card_fields(card: Card) -> dict:
    """The columns we denormalize out of the card so the due query can use an index."""
    return {
        "due": card.due.astimezone(timezone.utc).isoformat(),
        "state": card.state.name,
    }


def interval_days(card: Card, from_time: datetime | None = None) -> float:
    delta = card.due - (from_time or now())
    return round(delta.total_seconds() / 86400, 2)
