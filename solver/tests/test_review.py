"""Core regression tests: headcount counting, rest owed from a rule-breaking history, score thresholds, unknown root
fields, the precheck under overlapping coverage, and short horizons."""
from __future__ import annotations

import math

import pytest

from tests.er_fixture import generate, raw
from nurse_roster.model import build_model, solve
from nurse_roster.precheck import precheck
from nurse_roster.problem import Headcount, Nurse, Problem
from nurse_roster.validate import validate
from tests.test_core import force, mini, quiet


# ---- 1: a nurse can never be counted twice toward one headcount -----------------------------
def test_r1_also_counts_cannot_repeat_or_name_anchor():
    with pytest.raises(ValueError, match="also_counts"):
        mini(1, ["D", "N"], [Nurse("A")], headcounts=[Headcount("D", 2, 2, 2, 2, also_counts=["D"])]).check()
    with pytest.raises(ValueError, match="also_counts"):
        mini(1, ["D", "N", "C"], [Nurse("A")],
             headcounts=[Headcount("D", 2, 2, 2, 2, also_counts=["C", "C"])]).check()


# ---- 2: rest owed from history does not rely on a rule-abiding history ----------------------
def test_r2_history_breaking_consecutive_cap_still_enforces_rest():
    hist = ["N"] + ["C"] * 8 + [""]                            # 9 worked days in a row, 1 off
    p = mini(3, ["D", "N", "C"], [Nurse("A", previous=hist)], night_rest_days=2,
             night_rest_hard=True, max_consecutive_shifts=3)
    assert force(p, [0, None, None]).status == "INFEASIBLE"    # D on day 0: only 1 off since N
    assert any("night rest" in e for e in validate(p, [[0, -1, -1]])[0])
    sol = force(p, [-1, 0, None])                              # off, D: 2 off days since N
    assert sol.status == "OPTIMAL" and validate(p, sol.assignment)[0] == []
    sol = force(p, [2, -1, 0])                                 # C, off, D: history off + 1 = 2
    assert sol.status == "OPTIMAL" and validate(p, sol.assignment)[0] == []
    p2 = mini(3, ["D", "N", "C"], [Nurse("A", previous=["N"] + ["C"] * 9)], night_rest_days=2,
              night_rest_hard=True, max_consecutive_shifts=3)
    assert force(p2, [2, -1, 0]).status == "INFEASIBLE"        # no off day at all in history
    assert any("night rest" in e for e in validate(p2, [[2, -1, 0]])[0])


def test_r2_soft_mode_matches_validator_with_intermediate_days():
    w = quiet(); w.night_rest = 5
    for hist, row in ((["N"] + ["C"] * 8 + [""], [0, -1, -1]),  # D after 1 off
                      ([], [1, 0, -1, 0]),                     # N, D, off, D: first D only
                      ([], [1, 0, 0, -1])):                    # N, D, D: first D only
        p = mini(4, ["D", "N", "C"], [Nurse("A", previous=hist)], weights=w, night_rest_days=2,
                 night_rest_hard=False, max_consecutive_shifts=3)
        sol = force(p, row + [None] * (4 - len(row)))
        errors, pen, total = validate(p, sol.assignment)
        assert errors == [] and pen["night_rest"] == 5 and total == sol.objective, (hist, row)


def test_r2_earlier_day_shift_ends_the_rest_rule():
    p = mini(4, ["D", "N"], [Nurse("A", previous=["N", "D", ""])], night_rest_days=2,
             night_rest_hard=True, max_consecutive_shifts=3)
    # the history N -> D is the (past) violation; the month D is not a new one
    assert validate(p, [[0, -1, -1, -1]])[0] == []
    assert force(p, [0, None, None, None]).status == "OPTIMAL"


# ---- 3: score thresholds must be finite numbers within 0..1 -------------------------------
@pytest.mark.parametrize("field", ["score_target", "score_target_weekend", "score_band_hi", "score_band_hi_weekend"])
@pytest.mark.parametrize("value", [1.1, -0.1, math.nan, math.inf, "0.7"])
def test_r3_score_thresholds_validated(field, value):
    p = mini(1, ["D", "N"], [Nurse("A")])
    setattr(p.department, field, value)
    with pytest.raises(ValueError, match=field):
        build_model(p)


def test_r3_nan_score_rejected():
    with pytest.raises(ValueError, match="score"):
        mini(1, ["D", "N"], [Nurse("A", score=math.nan)]).check()


# ---- 4: unknown root fields are rejected like nested ones ---------------------------------
def test_r4_unknown_root_field_rejected():
    with pytest.raises(ValueError, match="public_holiday"):
        Problem.from_dict(raw(days=2, public_holiday=[0]))


# ---- 5: precheck understands overlapping coverage ------------------------------------------
def test_r5_precheck_counts_also_counts_eligibility():
    p = mini(1, ["D", "C"], [Nurse("A", allowed_shifts=["C"])], headcount_enabled=True,
             headcounts=[Headcount("D", 1, 1, 1, 1, also_counts=["C"])])
    assert not any("only 0 nurses" in i for i in precheck(p))
    sol = solve(build_model(p), p, time_limit=5, workers=2)
    assert sol.status == "OPTIMAL" and validate(p, sol.assignment)[0] == []


def test_r5_capacity_demand_not_overstated_for_shared_coverage():
    # one C shift covers both D and N demand: 1 nurse x 1 day can satisfy min 1 + min 1
    p = mini(1, ["D", "N", "C"], [Nurse("A")], headcount_enabled=True,
             headcounts=[Headcount("D", 1, 1, 1, 1, also_counts=["C"]), Headcount("N", 1, 1, 1, 1, also_counts=["C"])])
    assert not any(i.startswith("capacity") for i in precheck(p))


# ---- 6: short horizons generate ------------------------------------------------------------
@pytest.mark.parametrize("days", [1, 2, 4])
def test_r6_generate_short_horizon(days):
    p = generate(n_nurses=25, days=days)
    assert p.days == days and all(len(n.unavailable) <= days for n in p.nurses)
