"""Regression tests for history warnings, carried hours balances and advance credit, a model bound to its problem, the
Friday share under overlapping coverage, malformed imports, generator bounds, extreme rest parameters and the precheck's
capacity cap."""
from __future__ import annotations

import json
import time

import pytest

from nurse_roster import fairness as F
from nurse_roster.__main__ import main
from tests.er_fixture import dep, generate, nurse, raw, request_of, write_request
from nurse_roster.model import build_model, solve
from nurse_roster.precheck import precheck
from nurse_roster.problem import Headcount, Nurse, Problem
from nurse_roster.validate import validate
from tests.test_core import hours_general, mini, quiet


def hours_on(p: Problem, cap: int = 0, **g) -> Problem:
    p.general = hours_general(overtime_month_cap=cap, **g)
    return p


# ---- 1: a tail of the recommended depth can still hide an obligation --------------------------
def test_history_content_warnings():
    p = mini(3, ["D", "N"], [Nurse("a", previous=[""] * 8)], max_consecutive_shifts=3,
             double_cycle_rest=2, switches_hard=True, max_switches=0)
    assert any("no worked day" in i for i in precheck(p)), precheck(p)
    p = mini(3, ["D", "N"], [Nurse("a", previous=[""] * 8, last_shift_kind="day")], max_consecutive_shifts=3,
             double_cycle_rest=2, switches_hard=True, max_switches=0)
    assert not any("unknown" in i for i in precheck(p))     # `last_shift_kind` carries the kind
    p = mini(3, ["D", "N", "C"], [Nurse("a", previous=["C", "C"])], night_rest_days=2,
             max_consecutive_shifts=0)                        # the rest memory is explicit (`rest_owed`),
    assert not any("unknown" in i for i in precheck(p)), precheck(p)   # never unknown
    p.nurses[0].rest_owed = 1
    assert validate(p, [[0, -1, -1]])[0] == \
        ["night rest: a D day 0 has only 0 day(s) off, 1 still owed to a night before her history"]
    assert validate(p, [[-1, 0, -1]])[0] == []


# ---- 2: an exported balance imports again -----------------------------------------------------
def test_exported_hours_balance_round_trips_for_a_restricted_nurse():
    n = Nurse("d", allowed_shifts=["D"], unavailable=[0, 1])
    p = hours_on(mini(31, ["D", "N", "A"], [n]))
    assert p.required_hours(n) == 200
    sol = solve(build_model(p), p, time_limit=10, workers=4)
    worked = sum(12 for v in sol.assignment[0] if v >= 0)
    assert sol.status == "OPTIMAL" and worked == 192 and p.carry_over(n, worked) == 8
    hours_on(mini(31, ["D", "N", "A"], [Nurse("d", allowed_shifts=["D"], hours_balance=8)])).check()
    hours_on(mini(31, ["D", "N", "A"], [Nurse("d", hours_balance=8)])).check()   # may work A (8 h) now,
    hours_on(mini(31, ["D", "N", "A"], [Nurse("d", hours_balance=12)])).check()  # but the debt came from a
    hours_on(mini(31, ["D", "N", "A"], [Nurse("d", hours_balance=24)])).check()  # 12 h month, a
                                                                                 # longer one since retired,
                                                                                 # or a month that
                                                                                 # could not reach its hours


# ---- 3: an advance credit the month cannot absorb is carried, not turned into overtime -----------
def test_unused_advance_credit_is_carried():
    n = Nurse("a", hours_balance=-11, unavailable=list(range(31)))
    p = hours_on(mini(31, ["D", "N"], [n]))
    assert p.required_hours(n) == 0 and p.unused_credit(n) == 11 and p.carry_over(n, 0) == -11
    sol = solve(build_model(p), p, time_limit=10, workers=4)
    assert sol.status == "OPTIMAL" and sol.assignment == [[-1] * 31]
    assert validate(p, sol.assignment)[1]["nurse_overtime"] == 0
    assert not any("capacity" in i for i in precheck(p))


# ---- 4: a model is bound to the problem it was built from ----------------------------------------
def test_solve_rejects_a_problem_edited_after_build():
    p = mini(1, ["D", "N"], [Nurse("a")], headcounts=[Headcount("D", 1, 1, 1, 1)], headcount_enabled=True)
    rm = build_model(p)
    p.department.headcounts[0].weekday_min = 0
    with pytest.raises(ValueError, match="build_model again"):
        solve(rm, p, time_limit=5, workers=2)


# ---- 5: overlapping coverage counts people, not headcounts, in the Friday share ------------------
def test_friday_share_with_overlapping_coverage():
    p = mini(1, ["D", "N", "C"], [Nurse("c", allowed_shifts=["C"])], first_weekday=4, fair_weekends=True,
             headcounts=[Headcount("D", 1, 1, 1, 1, also_counts=["C"]), Headcount("N", 1, 1, 1, 1, also_counts=["C"])],
             headcount_enabled=True)
    assert F.friday_expected_m(p) == 1000
    assert F.balances_out(p, p.nurses[0], [2])[1] == 0.0
    p.general.weights.fair_weekend = 1
    assert validate(p, [[2]])[1]["fair_weekend"] == 0


# ---- 6: malformed imports are ValueErrors, and the CLI reports them -----------------------------
@pytest.mark.parametrize("over", [
    {"department": dep(shifts=[["D"], "N"])},
    {"department": dep(fair_shifts=[{}])},
    {"department": dep(headcounts=[{"shift": "D", "weekday_min": 1, "weekday_max": 1, "weekend_min": 1,
                                    "weekend_max": 1, "also_counts": [{}]}])},
    {"nurses": [nurse("a", score=10 ** 400)]},
    {"nurses": [nurse("a", ytd_overtime=10 ** 400)]},
])
def test_malformed_imports_are_value_errors(over, tmp_path):
    d = raw(nurses=[nurse("a")])
    d.update(over)
    with pytest.raises(ValueError):
        Problem.from_dict(d)
    r = request_of(mini(30, ["D", "N"], [Nurse("a")]), "2026-06")   # the same blocks, as a request
    r.update(over)
    f = tmp_path / "r.json"
    write_request(r, f)
    assert main(["solve", str(f)]) == 4                       # malformed input


# ---- 7: generator bounds apply before anything is allocated ----------------------------------------
def test_generator_bounds_apply_first():
    t = time.perf_counter()
    with pytest.raises(ValueError):
        generate(10 ** 9)
    with pytest.raises(ValueError):
        generate(5, days=10 ** 9, ramadan=True)
    assert time.perf_counter() - t < 0.5


# ---- 8: extreme rest parameters do not blow up model construction -------------------------------
def test_rest_gaps_are_clamped_to_the_horizon():
    """The gap ranges are clamped to the horizon (K = 7 with 7 rest days spans 56 days on a
    1-day month); the look-back bound also refuses rules a carried history could not hold, so
    a K of 1000 (7007 days) never reaches the model."""
    p = mini(1, ["D", "N", "C"], [Nurse(f"n{i}") for i in range(25)], night_rest_days=7,
             max_consecutive_shifts=7)
    t = time.perf_counter()
    rm = build_model(p)
    assert time.perf_counter() - t < 0.5 and rm.stats["variables"] == 100
    with pytest.raises(ValueError, match="look back 7007 days"):
        mini(1, ["D", "N", "C"], [Nurse("a")], night_rest_days=7, max_consecutive_shifts=1000).check()


# ---- 9: the summed headcount maximum is a cap only when every usable shift has a headcount -------
def test_precheck_capacity_cap_needs_full_coverage():
    p = hours_on(mini(2, ["D", "N"], [Nurse("n", allowed_shifts=["N"])], headcounts=[Headcount("D", 0, 0, 0, 0)],
                      headcount_enabled=True), hours_per_day=12)
    issues = precheck(p)
    assert not any(i.startswith("capacity: staff must work") for i in issues), issues
    assert any(i.startswith("N: no headcount names it") for i in issues)
    assert solve(build_model(p), p, time_limit=5, workers=2).status == "OPTIMAL"
