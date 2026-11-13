"""Full runs: a run is a nurse's consecutive worked days with a non-worked
day on each side, leave included; a full run is one of exactly the longest run (`max_consecutive_shifts`). Each of a
nurse's full runs beyond the unit's `max_full_runs` costs `excess_full_run` once. A full run counts in the month that holds
its last day, one reaching the month's last day included. Constraint-verifier style: tiny months counted by
hand, the model held to the validator, then the request's fields and refusals, then the ER sample."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from nurse_roster.explain import explain, report
from nurse_roster.model import build_model, solve
from nurse_roster.problem import Headcount, Nurse, Problem
from nurse_roster.request import load_request
from nurse_roster.rules import active
from nurse_roster.validate import validate
from tests.er_fixture import raw, version_1
from tests.rules_helper import only, penalises
from tests.test_core import force, mini

W, OFF = 0, -1
ER = Path(__file__).parent / "fixtures" / "er_request_2026-03.json"
FIXTURES = Path(__file__).parents[1] / "service_tests" / "fixtures"


def unit(nurses: list[Nurse], days: int, *, cap: int | None = 1, longest: int = 3, price: int = 4, **dep) -> Problem:
    """One shift, every other rule off but the longest run (hard); a full run above `cap` priced at `price`."""
    return mini(days, ["D"], nurses, weights=only("excess_full_run", price), max_consecutive_shifts=longest,
                max_full_runs=cap, **dep)


def runs(*pattern: str) -> list[int]:
    """"WWW." -> [0, 0, 0, -1]."""
    return [W if c == "W" else OFF for c in "".join(pattern)]


# ---- the price, counted by hand ------------------------------------------------------------------
@pytest.mark.parametrize("row, cap, price", [
    (runs("WWW.", "WWW.", "WWW."), 1, 4 * 2),     # three full runs, one allowed: two above at 4
    (runs("WWW.", "WWW.", "WWW."), 3, 0),         # at the cap
    (runs("WWW.", "WWW.", "WWW."), 5, 0),         # under it
    (runs("WWW.", "WWW.", "WWW."), 0, 4 * 3),     # none allowed
    (runs("WW.W", "W.WW", ".WW."), 0, 0),         # runs of 2 and 1 are not full
    (runs("WWW.", "WW..", "WWW."), 1, 4),         # two full runs and one of 2
])
def test_each_full_run_above_the_cap_costs_the_weight_once(row, cap, price):
    p = unit([Nurse("a")], 12, cap=cap)

    assert penalises(p, [row], "excess_full_run") == price
    assert explain(p, [row]).errors == []


def test_the_solver_and_the_validator_agree_on_a_grid():
    p = unit([Nurse("a")], 12, cap=1)
    row = runs("WWW.", "WWW.", "WWW.")

    sol = force(p, row)

    assert sol.status == "OPTIMAL" and sol.solver_objective == sol.objective == 8
    assert validate(p, [row])[2] == 8


def test_the_solver_breaks_the_work_into_shorter_runs_where_it_can():
    """Two nurses, one on every day: with the cap at 0 the solver finds a month of runs of two, at no cost."""
    p = unit([Nurse("a"), Nurse("b")], 12, cap=0, headcount_enabled=True, headcounts=[Headcount("D", 1, 1, 1, 1)])

    sol = solve(build_model(p), p, time_limit=10, workers=4)

    assert sol.status == "OPTIMAL" and sol.objective == sol.solver_objective == 0
    assert explain(p, sol.assignment).rules["excess_full_run"].matches == []


# ---- what counts ---------------------------------------------------------------------------------
def test_leave_ends_a_run():
    p = unit([Nurse("a", unavailable=[3, 7])], 12, cap=0)

    assert penalises(p, [runs("WWW.", "WWW.", "WW..")], "excess_full_run") == 4 * 2


def test_a_run_longer_than_the_longest_run_is_not_a_full_run():
    """Only a recorded fact can be longer (the hard rule forbids it in the free days); it is not priced as a full run."""
    p = unit([Nurse("a")], 8, cap=0)

    an = explain(p, [runs("WWWW", "....")])

    assert an.rules["excess_full_run"].weighted == 0 and an.errors   # the longest run's own violation


@pytest.mark.parametrize("previous, row, count", [
    (["", "D", "D"], runs("W.", "......"), 1),       # begun in the history, ended on day 0: this month's
    (["", "", "D"], runs("WW.", "....."), 1),
    (["D", "D", "D"], runs("........"), 0),          # ended on the history's last day: last month's
    (["D", "D", "D", ""], runs("WWW.", "...."), 1),  # the history's run is last month's; day 0's is this one's
    (["D", "D"], runs("W.", "......"), 1),           # the start of her history bounds the run
])
def test_a_run_counts_in_the_month_that_holds_its_last_day(previous, row, count):
    p = unit([Nurse("a", previous=previous)], 8, cap=0)

    assert penalises(p, [row], "excess_full_run") == 4 * count


def test_a_history_shift_the_unit_no_longer_works_is_a_worked_day_of_the_run():
    """"A" is no shift of this unit's: worked, of no kind, in the model as in the validator."""
    p = unit([Nurse("a", previous=["", "A", "D"])], 8, cap=0)
    row = runs("W.", "......")

    sol = force(p, row)

    assert penalises(p, [row], "excess_full_run") == 4
    assert sol.status == "OPTIMAL" and sol.solver_objective == sol.objective == 4


@pytest.mark.parametrize("row, count", [
    (runs(".....", "WWW"), 1),                      # reaches the month's last day: it cannot grow, so it is this month's
    (runs("....", "WWW."), 1),                      # ends the day before it
    (runs("WWW.", ".WWW"), 2),
    (runs("......", "WW"), 0),                      # a run of 2 at the month's end is not full
])
def test_a_run_reaching_the_months_last_day_counts_in_that_month(row, count):
    p = unit([Nurse("a")], 8, cap=0)

    assert penalises(p, [row], "excess_full_run") == 4 * count


def test_the_solver_prices_a_run_reaching_the_months_last_day_as_the_validator_does():
    p = unit([Nurse("a")], 8, cap=0)
    row = runs(".....", "WWW")

    sol = force(p, row)

    assert sol.status == "OPTIMAL" and sol.solver_objective == sol.objective == 4


def test_the_solver_cannot_fill_the_days_before_a_short_history_to_dodge_the_price():
    """b's longer history pads a's horizon with days before her own; they bound her run like an off day, in the model as
    in the validator, so a's run from the start of her history to day 0 is priced."""
    p = unit([Nurse("a", previous=["D", "D"]), Nurse("b", previous=["", "", "", ""])], 8, cap=0)
    row = runs("W.", "......")

    sol = force(p, row)

    assert penalises(p, [row, runs("........")], "excess_full_run") == 4
    assert sol.status == "OPTIMAL" and sol.solver_objective == sol.objective == 4


@pytest.mark.parametrize("longest, cap, price", [
    (0, 0, 4),          # no longest run: no full run
    (1, 0, 4),          # every run of one would be full: nothing is priced
    (3, None, 4),       # no cap
    (3, 0, 0),          # the weight at 0
])
def test_nothing_is_priced_without_a_longest_run_above_one_a_cap_or_a_weight(longest, cap, price):
    p = unit([Nurse("a")], 12, cap=cap, longest=longest, price=price)
    row = runs("W.W.", "W.W.", "W.W.") if longest == 1 else runs("WWW.", "WWW.", "WWW.")

    assert not active(p, "excess_full_run")
    assert penalises(p, [row], "excess_full_run") == 0
    assert validate(p, [row])[2] == 0


# ---- the words -----------------------------------------------------------------------------------
def test_the_match_names_the_nurse_her_count_and_the_cap_and_her_explanation_carries_it():
    p = unit([Nurse("a"), Nurse("b")], 12, cap=1)
    a = [runs("WWW.", "WWW.", "WWW."), runs("WWW.", "WW..", "....")]

    an = explain(p, a)

    [m] = an.rules["excess_full_run"].matches
    assert (m.nurse, m.day, m.amount) == ("a", None, 2)
    assert m.text == "a: 3 full runs of 3 shifts, max 1"
    assert an.nurses == {"a": 8, "b": 0, "": 0}
    assert "excess_full_run 8 = 2 x 4  (full run above the maximum)" in report(an)


# ---- the request ---------------------------------------------------------------------------------
def sample(name: str = "four_nurses_skills.json") -> dict:
    """"v1" is the four-nurse sample written as version 1."""
    if name == "v1":
        return version_1(json.loads((FIXTURES / "four_nurses.json").read_text()))
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.parametrize("name", ["v1", "four_nurses.json", "four_nurses_skills.json"])   # versions 1 and 2
def test_an_input_without_the_fields_has_no_cap_and_no_weight(name):
    req = sample(name)
    req["department"].pop("max_full_runs", None)
    req["general"]["weights"].pop("excess_full_run", None)
    before = copy.deepcopy(req)

    p = load_request(req).problem

    assert req == before                                           # the stored input is not touched
    assert p.department.max_full_runs is None and p.general.weights.excess_full_run == 0
    assert not active(p, "excess_full_run")


@pytest.mark.parametrize("name", ["v1", "four_nurses.json", "four_nurses_skills.json"])
@pytest.mark.parametrize("cap", [0, 3, 31, None])
def test_either_version_takes_the_cap_and_the_weight(name, cap):
    req = sample(name)
    req["department"]["max_full_runs"] = cap
    req["general"]["weights"]["excess_full_run"] = 4

    p = load_request(req).problem

    assert p.department.max_full_runs == cap and p.general.weights.excess_full_run == 4


@pytest.mark.parametrize("name", ["v1", "four_nurses.json", "four_nurses_skills.json"])
@pytest.mark.parametrize("path, value, text", [
    (["department", "max_full_runs"], -1, "request: department.max_full_runs must be a whole number within 0..31 or null, got -1"),
    (["department", "max_full_runs"], 32, "request: department.max_full_runs must be a whole number within 0..31 or null, got 32"),
    (["department", "max_full_runs"], 2.5, "request: department.max_full_runs must be a whole number within 0..31 or null, got 2.5"),
    (["department", "max_full_runs"], 3.0, "request: department.max_full_runs must be a whole number within 0..31 or null, got 3.0"),
    (["department", "max_full_runs"], True, "request: department.max_full_runs must be a whole number within 0..31 or null, got True"),
    (["department", "max_full_runs"], "3", "request: department.max_full_runs must be a whole number within 0..31 or null, got '3'"),
    (["general", "weights", "excess_full_run"], -1, "request: general.weights.excess_full_run must be an integer within"),
    (["general", "weights", "excess_full_run"], True, "request: general.weights.excess_full_run must be an integer within"),
])
def test_a_bad_cap_or_weight_is_refused_with_its_field(name, path, value, text):
    req = sample(name)
    node = req
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value

    with pytest.raises(ValueError) as e:
        load_request(req)
    assert str(e.value).startswith(text)


def test_a_problem_file_must_carry_both_fields():
    """The problem file (schema 6) names every rule field, as every schema since 3; only the request may leave them out."""
    d = raw()
    del d["department"]["max_full_runs"]
    with pytest.raises(ValueError, match=r"department: missing field\(s\) \['max_full_runs'\]"):
        Problem.from_dict(d)
    d = raw()
    del d["general"]["weights"]["excess_full_run"]
    with pytest.raises(ValueError, match=r"general.weights: missing field\(s\) \['excess_full_run'\]"):
        Problem.from_dict(d)


# ---- the ER sample -------------------------------------------------------------------------------
def full_runs(p: Problem, a: list[list[int]]) -> dict[str, int]:
    """Each nurse's full runs, read off her history and month as text, apart from the validator's counter: every maximal
    stretch of exactly the longest run of W, followed by a day off or the month's end, whose last day is a month day."""
    K, out = p.department.max_consecutive_shifts, {}
    for nu, row in zip(p.nurses, a):
        text = "".join("W" if s else "." for s in nu.previous) + "".join("W" if v >= 0 else "." for v in row)
        ends = [m.end() - 1 for m in re.finditer(r"(?<!W)W+(?=\.|$)", text) if m.end() - m.start() == K]
        out[nu.id] = sum(1 for e in ends if e >= len(nu.previous))
    return out


def test_with_the_cap_set_a_solve_of_the_er_sample_gives_no_nurse_more_full_runs_than_the_cap():
    """The ER sample (25 nurses, longest run 3) keeps every hard rule, and the hours a nurse owes at the dominant
    avoidable-shortfall price, but no other soft price, so the month allows the cap of 2 exactly when a roster costs 0, and
    the solver proves it in under a second. With the ER month's every price on, whether a 10 s search reaches the cap
    depends on the weight and the machine, which a test on GitHub's runners cannot hold."""
    req = json.loads(ER.read_text())
    req["department"]["max_full_runs"] = 2
    req["general"]["weights"] = {w: 0 for w in req["general"]["weights"]} | {"avoidable_shortfall": 1000, "excess_full_run": 4}
    p = load_request(req).problem

    sol = solve(build_model(p), p, time_limit=30, workers=2)

    assert sol.status == "OPTIMAL" and sol.objective == 0 and sol.analysis is not None and sol.analysis.errors == []
    counted = full_runs(p, sol.assignment)
    assert max(counted.values()) <= 2, counted
    assert sum(counted.values()) > 0                                      # the month has full runs, at most two each
    assert validate(p, sol.assignment)[2] == sol.objective
