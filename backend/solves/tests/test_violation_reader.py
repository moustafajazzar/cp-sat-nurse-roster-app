"""What a violation says for the app, read from its fields, or from the solver's words in a result or a close stored before it
stated them."""
from __future__ import annotations

import pytest

from solves.result import headcount_of, shift_day, skill_need_of

# One violation of each shift-day rule, as results and closes stored before the fields hold it, beside the fields the solver
# states for the same violation now.
STORED = [
    ({"rule": "headcount", "nurse": None, "date": "2026-06-09", "text": "headcount: day 8 D = 0, want 1..1"},
     {"shift": "D", "present": 0, "minimum": 1, "maximum": 1}),
    ({"rule": "headcount", "nurse": None, "date": "2026-06-09", "text": "headcount: day 8 N = 3, want 1..2"},
     {"shift": "N", "present": 3, "minimum": 1, "maximum": 2}),
    ({"rule": "team_leader", "nurse": None, "date": "2026-06-09", "text": "team leader: day 8 N has none"}, {"shift": "N"}),
    ({"rule": "gender", "nurse": None, "date": "2026-06-09", "text": "gender: day 8 D has only {'F'}"}, {"shift": "D"}),
    ({"rule": "skill_need", "nurse": None, "date": "2026-06-09",
      "text": "skill need: day 8 N has 0 holding ICU, needs 1 (1 short)"},
     {"shift": "N", "skill": "ICU", "present": 0, "minimum": 1}),
]


@pytest.mark.parametrize("stored,fields", STORED, ids=[f"{stored['rule']} on {fields['shift']}" for stored, fields in STORED])
def test_the_stored_words_give_the_facts_the_fields_state(stored, fields):
    reworded = stored | fields | {"text": "words no reader depends on"}

    assert shift_day(stored) == shift_day(reworded) == ("2026-06-09", fields["shift"])
    if stored["rule"] == "headcount":
        assert headcount_of(stored) == headcount_of(reworded) == (fields["present"], fields["minimum"], fields["maximum"])


def test_a_skill_need_says_its_skill_and_how_many_it_is_short_from_its_fields_alone():
    """Its reader judges fresh answers only, so a stored need's words are not read for its figures."""
    need = {"rule": "skill_need", "nurse": None, "date": "2026-06-09", "text": "words no reader depends on", "shift": "N",
            "skill": "ICU", "present": 1, "minimum": 3}
    stored, _ = STORED[-1]

    assert skill_need_of(need) == ("ICU", 2)
    assert skill_need_of(stored) is None


def test_a_violation_of_no_shift_day_is_about_none():
    worked = {"rule": "allowed_shifts", "nurse": "Emad", "date": "2026-06-15", "text": "shift restriction: Emad works N on day 14"}
    hours = {"rule": "hours_bounds", "nurse": "Amal", "date": None, "text": "overtime: Amal 60 > cap 48"}

    assert shift_day(worked) is None and shift_day(hours) is None
