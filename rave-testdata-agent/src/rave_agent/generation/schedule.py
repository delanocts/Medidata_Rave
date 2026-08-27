"""Deterministic visit scheduling (FR-6.5).

When a visit happens is arithmetic, not clinical judgement, so it is computed
here rather than asked of the model. Two failures made that necessary:

* Every subject screened on the same day. The first form of the first visit has
  no context yet, so its prompt is byte-identical between subjects apart from
  the subject ID, and the model is close to deterministic - 23 of 25 subjects
  came back with `2024-03-01`.

* Visits a few days apart shared one date. `Screening (Day -30)`,
  `Day -27` and `Day -15` all landed on the same day, because the only anchor in
  the prompt was another visit's date and nothing said how far apart they are.

Rave publishes `target_days` for almost no folder - one of thirty-seven in the
study this was written against - so the protocol day is taken from the visit
name, which carries it by convention.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta

# `Day 4`, `Day -30`, `(Day 420)`. A name like "Day 3 post Tx1 (Day 4)" holds
# two: the relative one in the prose and the protocol day in brackets at the
# end. The last is the protocol day, which is the one visits are scheduled on.
_DAY_IN_NAME = re.compile(r"\bday\s*(-?\d+)", re.IGNORECASE)


def day_offset(folder_name: str) -> int | None:
    """The protocol day a visit name declares, or None if it names none.

    Unscheduled folders - a discontinuation, a log of adverse events, a device
    handout - genuinely have no protocol day, and must not be given one.
    """
    matches = _DAY_IN_NAME.findall(folder_name or "")
    return int(matches[-1]) if matches else None


def enrolment_date(subject_id: str, first_date: date, window_days: int) -> date:
    """This subject's Day 1, spread deterministically across the window.

    A real cohort enrols over months; one screening date shared by everybody is
    the most obviously synthetic thing a dataset can carry. The offset is
    derived from the subject ID with a stable hash rather than a random draw, so
    a subject regenerated tomorrow keeps the date it already has in Rave.
    `hash()` is unsuitable - it is salted per process.
    """
    if window_days < 1:
        return first_date
    digest = hashlib.sha256(subject_id.encode("utf-8")).digest()
    return first_date + timedelta(days=int.from_bytes(digest[:4], "big") % window_days)


def visit_date(anchor: date, offset: int) -> date:
    """The date of the visit at protocol day `offset`, given Day 1 = `anchor`.

    Day 1 is the reference day and there is no Day 0, so Day 2 is the day after
    Day 1 and Day -1 is the day before it. Day 4 is therefore three days after
    Day 1, not four - the CDISC convention.
    """
    return anchor + timedelta(days=offset - 1 if offset >= 1 else offset)
