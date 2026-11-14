"""What a change newly breaks, between the violations of the roster in force and those of the grid the change would make."""
from __future__ import annotations

from rosters.approved import newly_broken


def headcount(present: int, shift: str = "D", date: str = "2026-06-09") -> dict:
    """A D or N shift-day that takes two or three nurses, holding `present`."""
    return {"rule": "headcount", "nurse": None, "date": date, "text": f"{present} work {shift}", "shift": shift,
            "present": present, "minimum": 2, "maximum": 3}


def skill_need(present: int, skill: str = "ICU", shift: str = "N", date: str = "2026-06-09") -> dict:
    """A shift-day that needs two nurses holding `skill`, with `present` of them on it, as the solver states it."""
    return {"rule": "skill_need", "nurse": None, "date": date, "shift": shift, "skill": skill, "present": present, "minimum": 2,
            "text": f"skill need: {date} {shift} has {present} holding {skill}, needs 2 ({2 - present} short)"}


OVERTIME = {"rule": "hours_bounds", "nurse": "Amal", "date": None, "text": "overtime: Amal 60 > cap 48"}
NO_LEADER = {"rule": "team_leader", "nurse": None, "date": "2026-06-09", "text": "team leader: 2026-06-09 D has none", "shift": "D"}


def test_what_the_roster_in_force_already_breaks_is_not_new():
    assert newly_broken([OVERTIME, NO_LEADER, headcount(0)], [OVERTIME, NO_LEADER, headcount(0)]) == []


def test_a_headcount_nearer_its_range_is_not_new():
    assert newly_broken([headcount(0)], [headcount(1)]) == []
    assert newly_broken([headcount(5)], [headcount(4)]) == []


def test_a_headcount_further_from_its_range_is_new():
    assert newly_broken([headcount(1)], [headcount(0)]) == [headcount(0)]
    assert newly_broken([headcount(4)], [headcount(5)]) == [headcount(5)]


def test_a_headcount_broken_on_another_shift_day_is_new():
    assert newly_broken([headcount(0, "D")], [headcount(0, "D"), headcount(1, "N")]) == [headcount(1, "N")]
    assert newly_broken([headcount(0)], [headcount(0), headcount(1, date="2026-06-10")]) == [headcount(1, date="2026-06-10")]


def test_another_rule_whose_words_differ_is_broken_further_so_new():
    further = OVERTIME | {"text": "overtime: Amal 72 > cap 48"}

    assert newly_broken([OVERTIME], [further]) == [further]


def test_the_same_rule_on_another_shift_of_the_day_is_new():
    on_nights = NO_LEADER | {"shift": "N", "text": "team leader: 2026-06-09 N has none"}

    assert newly_broken([NO_LEADER], [NO_LEADER, on_nights]) == [on_nights]


def test_a_roster_in_force_that_breaks_nothing_makes_everything_new():
    assert newly_broken([], [OVERTIME, headcount(1)]) == [OVERTIME, headcount(1)]


def test_a_skill_need_narrowed_is_not_new():
    assert newly_broken([skill_need(0)], [skill_need(1)]) == []


def test_a_skill_need_as_short_as_before_is_not_new():
    assert newly_broken([skill_need(1)], [skill_need(1)]) == []


def test_a_skill_need_widened_is_new():
    assert newly_broken([skill_need(1)], [skill_need(0)]) == [skill_need(0)]


def test_a_need_of_another_skill_on_the_same_shift_day_is_new():
    triage = skill_need(1, skill="Triage")

    assert newly_broken([skill_need(0)], [skill_need(1), triage]) == [triage]


def test_a_skill_need_on_another_shift_day_is_new():
    on_days = skill_need(1, shift="D")
    next_night = skill_need(1, date="2026-06-10")

    assert newly_broken([skill_need(0)], [skill_need(0), on_days, next_night]) == [on_days, next_night]


def test_a_skill_need_that_does_not_state_its_figures_is_compared_by_its_words():
    def worded(present: int) -> dict:
        return {key: value for key, value in skill_need(present).items() if key not in ("skill", "present", "minimum")}

    assert newly_broken([worded(1)], [worded(1)]) == []
    assert newly_broken([worded(0)], [worded(1)]) == [worded(1)]
