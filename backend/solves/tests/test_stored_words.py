"""A stored result or close holds the solver's words as they were written: with days counted from 0 before the solver dated
them, and dated since. Its reader dates the first and leaves the second as they are."""
from __future__ import annotations

import datetime as dt

import pytest

from solves.result import in_dates

JUNE = dt.date(2026, 6, 1)

COUNTED = [
    ("headcount: day 8 D = 0, want 1..1", "headcount: 2026-06-09 D = 0, want 1..1"),
    ("night rest: Badr D day 0 has only 1 day(s) off since N day -2, needs 2",
     "night rest: Badr D 2026-06-01 has only 1 day(s) off since N 2026-05-30, needs 2"),
    ("succession: Amal N->D day -1", "succession: Amal N->D 2026-05-31"),
    ("day 9: no team leader available for D", "2026-06-10: no team leader available for D"),
    ("Amal: works day 3 (asked off)", "Amal: works 2026-06-04 (asked off)"),
]

DATED = [
    "headcount: 2026-06-09 D = 0, want 1..1",
    "night rest: Badr D 2026-06-01 has only 1 day(s) off since N 2026-05-30, needs 2",
    "succession: Amal N on 2026-05-31 -> D on 2026-06-01",
    "double cycle: Badr needs 2 days off after 2026-06-19",
    "consecutive: Amal 5 shifts in a row at 2026-06-01",
    "2026-06-10: headcounts need 2 nurses but only 0 are not on leave",
    "Badr: pinned N on 2026-06-22 and D on 2026-06-23 is a forbidden succession",
    "Dana: BLS, required to work, expires inside the month: its last day is 2026-06-24, so no shift after it",
    "following: Faten works D on 2026-06-08, when her preceptor Amal is off, while she is off on 2026-06-01, when Amal works D",
    "switches: Badr changes night/day 3 times, max 2",
    "Amal: history covers 4 day(s) but the pattern rules look back 9; month-boundary checks may miss a violation",
]


@pytest.mark.parametrize("stored,read", COUNTED)
def test_words_that_count_a_day_are_read_dated(stored, read):
    assert in_dates(stored, JUNE) == read


@pytest.mark.parametrize("stored", DATED)
def test_words_already_dated_are_read_as_they_are(stored):
    assert in_dates(stored, JUNE) == stored
