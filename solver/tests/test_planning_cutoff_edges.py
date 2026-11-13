"""Regression tests for the planning cutoff: night rest after a long elapsed run, the soft rest rule over a distant
elapsed day, the department's overtime penalty with recorded personal excess, and re-plans under a requested cutoff."""
from __future__ import annotations

import copy

import pytest

from nurse_roster.model import build_model, solve
from nurse_roster.problem import Nurse
from nurse_roster.validate import validate
from tests.rules_helper import only
from tests.test_core import force, hours_general, mini


REST_CASES = [
    pytest.param([], ["N", "C", "C", "C"], id="elapsed-night"),
    pytest.param(["N", "C", "C"], ["C"] * 5, id="historical-night"),
]


@pytest.mark.parametrize("previous,elapsed", REST_CASES)
@pytest.mark.parametrize("hard", [True, False], ids=["hard", "soft"])
def test_long_elapsed_run_does_not_discharge_night_rest(previous, elapsed, hard):
    cut = len(elapsed)
    nurse = Nurse("a", previous=previous, allowed_shifts=["D"],
                  pinned=dict(enumerate(elapsed)), desired_days=[cut + 1])
    weights = only("desired_day", 10)
    weights.night_rest = 7
    p = mini(cut + 3, ["D", "N", "C"], [nurse], weights=weights,
             max_consecutive_shifts=1, night_rest_days=2, night_rest_hard=hard)
    p.planning_from = cut
    prefix = [p.department.shifts.index(s) for s in elapsed]

    sol = solve(build_model(p), p, time_limit=5)
    assert sol.status == "OPTIMAL" and sol.hard_errors == []
    assert sol.assignment[0][cut:cut + 2] == ([-1, -1] if hard else [-1, 0])
    assert sol.penalty_breakdown["night_rest"] == (0 if hard else 7)
    assert sol.objective == sol.solver_objective == (10 if hard else 7)
    assert validate(p, sol.assignment)[0] == []

    # An all-off suffix is feasible; a D after just one off day is not enough rest.
    assert force(p, prefix + [-1, -1, -1]).status == "OPTIMAL"
    breach = force(p, prefix + [-1, 0, -1])
    assert breach.status == ("INFEASIBLE" if hard else "OPTIMAL")
    if not hard:
        assert breach.penalty_breakdown["night_rest"] == breach.solver_objective == 7
    rested = force(p, prefix + [-1, -1, 0])
    assert rested.status == "OPTIMAL" and rested.hard_errors == []
    assert rested.penalty_breakdown["night_rest"] == 0


@pytest.mark.parametrize("previous,elapsed", REST_CASES)
def test_soft_rest_still_counts_a_distant_elapsed_day(previous, elapsed):
    recorded = elapsed + ["", "D"]
    p = mini(len(recorded), ["D", "N", "C"],
             [Nurse("a", previous=previous, allowed_shifts=["D"], pinned=dict(enumerate(recorded)))],
             weights=only("night_rest", 7), max_consecutive_shifts=1,
             night_rest_days=2, night_rest_hard=False)
    p.planning_from = p.days
    sol = solve(build_model(p), p, time_limit=5)
    assert sol.status == "OPTIMAL" and sol.hard_errors == []
    assert sol.penalty_breakdown["night_rest"] == sol.objective == sol.solver_objective == 7


@pytest.mark.parametrize("count,department_cap", [(1, 0), (2, 5)])
def test_department_overtime_penalty_allows_recorded_personal_excess(count, department_cap):
    p = mini(6, ["D"], [Nurse(str(n), pinned={d: "D" for d in range(5)}) for n in range(count)])
    p.general = hours_general(overtime_month_cap=10, dept_overtime_cap=department_cap,
                              weights=only("dept_overtime", 3))
    p.planning_from = 5
    assert all(p.elapsed_hours(nu) == 60 and p.required_hours(nu) == 40 for nu in p.nurses)
    sol = solve(build_model(p), p, time_limit=5)
    assert sol.status == "OPTIMAL" and sol.assignment == [[0] * 5 + [-1]] * count
    assert sol.hard_errors == [] and validate(p, sol.assignment)[0] == []
    expected = 3 * (20 * count - department_cap)
    assert sol.penalty_breakdown["dept_overtime"] == sol.objective == sol.solver_objective == expected


def test_replan_accepts_existing_recorded_pins_under_the_requested_cutoff():
    p = mini(3, ["D", "N"], [Nurse("a", allowed_shifts=["D"], pinned={0: "N"}), Nurse("b")])
    p.published = [["D", "", ""], ["D", "", ""]]
    with pytest.raises(ValueError, match="not in her allowed shifts"):
        p.check()
    assert p.replan_from(1) == 1
    assert p.planning_from == 1
    assert [nu.pinned for nu in p.nurses] == [{0: "N"}, {0: "D"}]
    p.check()
    sol = solve(build_model(p), p, time_limit=5)
    assert sol.status == "OPTIMAL" and sol.hard_errors == []
    assert sol.assignment[0][0] == 1 and 1 not in sol.assignment[0][1:]
    assert sol.analysis.elapsed == ["shift restriction: a works N on day 0"]


@pytest.mark.parametrize("pins,published,leave,message", [
    ({0: "N", 1: "N"}, [["D", "", ""]], [], "not in her allowed shifts"),
    ({0: "X"}, [["D", "", ""]], [], "not a department shift"),
    ({True: "D"}, [["D", "", ""]], [], "pinned day True outside"),
    ({0: "N"}, [["D", ""]], [], r"published\[a\] must have 3 cells"),
    ({0: "N"}, [["D", "", "X"]], [], "not a department shift"),
    ({0: "N"}, [["D", "", ""]], [0], "on leave but pinned"),
    ([], [["D", "", ""]], [], "pinned must be an object"),
])
def test_replan_still_validates_structure_and_future_pins_atomically(pins, published, leave, message):
    p = mini(3, ["D", "N"], [Nurse("a", allowed_shifts=["D"], pinned=pins, unavailable=leave)])
    p.published = published
    before = copy.deepcopy(p)
    original_pins = p.nurses[0].pinned
    with pytest.raises(ValueError, match=message):
        p.replan_from(1)
    assert p == before and p.nurses[0].pinned is original_pins


def test_failed_replan_preserves_an_existing_cutoff():
    p = mini(3, ["D", "N"], [Nurse("a", allowed_shifts=["D"], pinned={0: "N", 1: "N"})])
    p.published = [["D", "D", ""]]
    p.planning_from = 2
    p.check()
    before = copy.deepcopy(p)
    original_pins = p.nurses[0].pinned
    with pytest.raises(ValueError, match="not in her allowed shifts"):
        p.replan_from(1)                                      # day 1 would become a future pin
    assert p == before and p.nurses[0].pinned is original_pins
