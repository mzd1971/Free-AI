"""Use Py-FSRS human recall estimates as a heuristic for assistant memory aging."""

from datetime import datetime, timedelta, timezone
from math import isfinite

from fsrs import Card, Rating, Scheduler, State
from fsrs.scheduler import STABILITY_MIN


_SCHEDULER = Scheduler(learning_steps=(), relearning_steps=(), enable_fuzzing=False)
_MIN_ARCHIVE_AGE = timedelta(days=30)
_HIGH_IMPORTANCE = 0.7


def _utc(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('A timezone-aware datetime is required.')
    return value.astimezone(timezone.utc)


def _card(state_json):
    if not state_json:
        return None
    try:
        card = Card.from_json(state_json)
    except (KeyError, OverflowError, RecursionError, TypeError, ValueError):
        return None
    if (card.state != State.Review or card.step is not None
            or card.last_review is None or card.last_review.utcoffset() is None
            or card.due.utcoffset() is None
            or card.stability is None or not isfinite(card.stability) or card.stability < STABILITY_MIN
            or card.difficulty is None or not 1 <= card.difficulty <= 10):
        return None
    return card


def new_state(now: datetime) -> str:
    """Initialize a newly recorded memory with a successful FSRS review."""
    review_time = _utc(now)
    card, _ = _SCHEDULER.review_card(Card(due=review_time), Rating.Good, review_datetime=review_time)
    return card.to_json()


def reinforce(state_json: str, now: datetime) -> str:
    """Record explicit confirmation; ordinary retrieval must not reinforce a claim."""
    card = _card(state_json)
    review_time = _utc(now)
    if card is None or review_time <= card.last_review:
        return state_json
    card, _ = _SCHEDULER.review_card(card, Rating.Good, review_datetime=review_time)
    return card.to_json()


def retention(state_json: str | None, now: datetime) -> float:
    """Return the FSRS score and keep unscorable memories eligible for recall."""
    card = _card(state_json)
    current_time = _utc(now)
    if card is None:
        return 1.0
    return min(1.0, max(0.0, _SCHEDULER.get_card_retrievability(card, current_datetime=current_time)))


def _importance(record):
    value = record.get('importance', 0.5)
    if type(value) in (float, int) and 0.0 <= value <= 1.0:
        return value
    return None


def should_archive(record, now: datetime) -> bool:
    """Check whether an active, unprotected AI memory may leave automatic recall."""
    if (record.get('status') != 'active' or record.get('source') != 'agent'
            or record.get('pinned') or record.get('verified')):
        return False
    importance = _importance(record)
    card = _card(record.get('retention_state'))
    if importance is None or card is None:
        return False
    try:
        updated = _utc(datetime.fromisoformat(record.get('updated') or record.get('created')))
    except (OverflowError, TypeError, ValueError):
        return False
    current_time = _utc(now)
    if current_time - max(updated, card.last_review) < _MIN_ARCHIVE_AGE:
        return False
    # With the default FSRS parameters, a fresh Good card crosses 0.60
    # after about two months and 0.45 after about fourteen months.
    threshold = 0.45 if importance >= _HIGH_IMPORTANCE else 0.60
    return _SCHEDULER.get_card_retrievability(card, current_datetime=current_time) < threshold


def priority(record, now: datetime) -> float:
    """Combine importance and FSRS for ordering eligible memories."""
    importance = _importance(record)
    if importance is None:
        importance = 0.5
    return 0.6 * importance + 0.4 * retention(record.get('retention_state'), now)
