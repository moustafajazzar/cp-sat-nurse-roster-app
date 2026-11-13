"""Core-module regression tests: absent and unknown fields, night rest, malformed input, the boundary switch, fairness
feasibility, score spread over overlapping shifts, switch semantics, CSV formula escaping, the validator's shape checks,
exact fit under the cycle rules, history depth and the calendar range."""
from __future__ import annotations

import io
import itertools
import json
from dataclasses import replace

import pytest

from nurse_roster.export import csv_safe, write_csv
from nurse_roster.calendar import month_info
from nurse_roster.model import build_model, solve
from nurse_roster.precheck import max_shifts_fit, needed_history, precheck
from nurse_roster.problem import DepartmentRules, GeneralRules, Headcount, Nurse, Problem, Weights
from nurse_roster.validate import validate
from tests.er_fixture import dep, er_department, er_general, nurse, raw


def quiet() -> Weights:
    return Weights(**{f: 0 for f in Weights.__slots__})


def hours_general(**over) -> GeneralRules:
    """The ER general rules with successions off and no penalties: the hours rule on its own."""
    return replace(er_general(), **{"forbidden_successions_enabled": False,
                                    "weights": replace(quiet(), avoidable_shortfall=1000), **over})


def mini(days: int, shifts: list[str], nurses: list[Nurse], weights: Weights | None = None,
         first_weekday: int = 0, **dep) -> Problem:
    """Tiny problem with every rule off unless asked for."""
    g = hours_general(hours_rule_enabled=False, weights=weights or quiet())
    base = dict(shifts=shifts, headcounts=[], headcount_enabled=False, require_team_leader=False,
                require_both_genders=False, score_target=0.0, score_target_weekend=0.0,
                score_balance=False, night_rest_days=0, fair_weekends=False, fair_shifts=[],
                max_consecutive_shifts=0, double_cycle_rest=0, no_mixed_blocks=False,
                max_switches=99, carry_fairness=False)
    base.update(dep)
    return Problem(days=days, first_weekday=first_weekday, nurses=nurses, general=g,
                   department=replace(er_department(), **base))


def force(p: Problem, row: list[int | None], n: int = 0):
    """Build the model and pin nurse n's month days: shift index, -1 = off, None = free."""
    rm = build_model(p)
    P = p.history_len()
    for d, v in enumerate(row):
        if v is None:
            continue
        if v < 0:
            rm.model.add(rm.off[n][P + d] == 1)
        else:
            rm.model.add(rm.x[n][P + d][v] == 1)
    return solve(rm, p, time_limit=10, workers=4)


# ---- F01: an omitted JSON field is an error, never a silent default (schema 3) -------------
def test_absent_fields_are_errors():
    d = raw(days=1, nurses=[nurse("N1")])
    p = Problem.from_dict(d)
    assert [hc.shift for hc in p.department.headcounts] == ["D", "N"]
    errors, _, _ = validate(p, [[-1]])
    assert any(e.startswith("headcount") for e in errors)
    without = dict(d["department"]); del without["headcounts"]
    with pytest.raises(ValueError, match=r"department: missing field\(s\) \['headcounts'\]"):
        Problem.from_dict({**d, "department": without})
    p2 = Problem.from_dict(raw(days=1, nurses=[nurse("N1")],
                               department=dep(headcounts=[], fair_shifts=[])))   # no headcount:
    assert p2.department.headcounts == []                      # explicit empty stays empty;
    with pytest.raises(ValueError, match="no headcount"):      # fair shares need one
        Problem.from_dict(raw(days=1, nurses=[nurse("N1")], department=dep(headcounts=[])))
    for path, node, strip in (("general", lambda b: b["general"], "hours_per_day"),
                              ("general.weights", lambda b: b["general"]["weights"], "change"),
                              ("department", lambda b: b["department"], "night_shift"),
                              (r"nurses\[0\]", lambda b: b["nurses"][0], "score")):
        bad = json.loads(json.dumps(d))
        del node(bad)[strip]
        with pytest.raises(ValueError, match=rf"{path}: missing field\(s\) \['{strip}'\]"):
            Problem.from_dict(bad)
    for key in ("version", "general", "department"):
        with pytest.raises(ValueError, match=f"problem: missing field\\(s\\) \\['{key}'\\]"):
            Problem.from_dict({k: v for k, v in d.items() if k != key})


def test_unknown_field_is_an_error():
    with pytest.raises(ValueError, match="unknown field"):
        Problem.from_dict(raw(days=1, nurses=[nurse("N1", nam="x")]))


# ---- F06: night rest counts off days, not calendar days -----------------------------------
def test_night_rest_requires_off_days_with_third_shift():
    nurse = Nurse("A")
    p = mini(4, ["D", "N", "C"], [nurse], night_rest_days=2, night_rest_hard=True,
             max_consecutive_shifts=3)
    assert force(p, [1, 2, -1, 0]).status == "INFEASIBLE"      # N, C, off, D: one off day
    assert force(p, [1, -1, -1, 0]).status == "OPTIMAL"        # N, off, off, D
    errors, _, _ = validate(p, [[1, 2, -1, 0]])
    assert any("night rest" in e for e in errors)
    assert validate(p, [[1, -1, -1, 0]])[0] == []


def test_soft_penalty_counts_one_per_offending_day():
    w = quiet(); w.night_rest = 5
    nurse = Nurse("A")
    p = mini(4, ["D", "N", "C"], [nurse], weights=w, night_rest_days=2, night_rest_hard=False,
             max_consecutive_shifts=3)
    sol = force(p, [1, 2, -1, 0])
    errors, pen, total = validate(p, sol.assignment)
    assert errors == [] and pen["night_rest"] == 5 and total == sol.objective


# ---- F08 / F09: malformed input is rejected before anything runs ---------------------------
def test_out_of_range_indices_rejected():
    with pytest.raises(ValueError, match="unavailable"):
        mini(7, ["D", "N"], [Nurse("A", unavailable=[-1])]).check()
    with pytest.raises(ValueError, match="unavailable"):
        build_model(mini(7, ["D", "N"], [Nurse("A", unavailable=[7])]))
    with pytest.raises(ValueError, match="public_holidays"):
        p = mini(7, ["D", "N"], [Nurse("A")]); p.public_holidays = [9]; precheck(p)


def test_empty_eligibility_rejected():
    with pytest.raises(ValueError, match="allowed_shifts is empty"):
        mini(7, ["D", "N"], [Nurse("A", allowed_shifts=[])]).check()


# ---- F10: single-shift nurses still count the boundary switch -----------------------------
def test_boundary_switch_for_single_shift_nurse():
    nurse = Nurse("A", allowed_shifts=["N"], previous=["D", ""])
    p = mini(2, ["D", "N"], [nurse], switches_hard=True, max_switches=0)
    assert force(p, [-1, 1]).status == "INFEASIBLE"
    errors, _, _ = validate(p, [[-1, 1]])
    assert any(e.startswith("switches") for e in errors)
    assert force(p, [-1, -1]).status == "OPTIMAL"


# ---- F11: a soft fairness term never makes the model infeasible ---------------------------
def test_friday_fairness_target_beyond_month_is_feasible():
    w = quiet(); w.fair_weekend = 1
    p = mini(31, ["D", "N"], [Nurse("A")], weights=w, fair_weekends=True,
             headcounts=[Headcount("D", 7, 9, 8, 9), Headcount("N", 7, 9, 8, 9)])
    sol = solve(build_model(p), p, time_limit=10, workers=4)
    assert sol.status in ("OPTIMAL", "FEASIBLE")
    assert validate(p, sol.assignment)[0] == []


# ---- F12: score-spread enumeration covers overlapping shifts -------------------------------
def test_spread_mode_allows_overlapping_coverage():
    w = quiet(); w.score_spread = 1
    nurses = [Nurse("A", score=0.9), Nurse("B", score=0.5)]
    for mode in ("band", "spread"):
        p = mini(1, ["D", "A"], nurses, weights=w, headcount_enabled=True, score_balance=True,
                 score_balance_mode=mode, headcounts=[Headcount("D", 2, 2, 2, 2, also_counts=["A"])])
        rm = build_model(p)
        rm.model.add(rm.x[0][0][0] == 1)
        rm.model.add(rm.x[1][0][1] == 1)
        assert solve(rm, p, time_limit=10, workers=4).status == "OPTIMAL", mode


# ---- F13: switch = night work <-> any other work, same in model and validator --------------
def test_switch_semantics_agree_with_three_shifts():
    p = mini(2, ["D", "N", "A"], [Nurse("A")], switches_hard=True, max_switches=0)
    sol = force(p, [2, 0])                                     # A then D: both day work
    assert sol.status == "OPTIMAL" and validate(p, sol.assignment)[0] == []
    assert force(p, [2, 1]).status == "INFEASIBLE"             # A then N
    assert any(e.startswith("switches") for e in validate(p, [[2, 1]])[0])


# ---- F14: CSV cells cannot start a spreadsheet formula ------------------------------------
def test_csv_formula_prefix_neutralised():
    assert csv_safe("=1+2") == "'=1+2" and csv_safe("N01") == "N01" and csv_safe(12) == 12
    p = mini(2, ["D", "N"], [Nurse("=1+2")])
    buf = io.StringIO()
    write_csv(p, [[0, -1]], buf)
    assert "\n'=1+2," in buf.getvalue() and "\n=1+2," not in buf.getvalue()


# ---- F15: validator checks the matrix shape and domain first --------------------------------
def test_validator_rejects_malformed_matrix():
    p = mini(2, ["D", "N"], [Nurse("A")])
    for bad in ([[-1, -1, 0]], [[-1]], [[-1, -1], [0, 0]], [[-1, 2]], [[-1, -2]], [[0.0, 1]], [[True, 0]]):
        errors, pen, total = validate(p, bad)
        assert errors and all(e.startswith("matrix") for e in errors), bad
        assert total == 0
    assert validate(p, [[0, -1]])[0] == []


# ---- F16: exact fit under the cycle rules ------------------------------------------------
def test_fit_examples():
    assert max_shifts_fit(9, set(), 3, 2) == 7                  # DDD o DD o DD
    assert max_shifts_fit(10, {3, 7, 8}, 3, 2) == 7             # DDD o DDD oo D, leave gives the rest
    assert max_shifts_fit(5, set(), 0, 0) == 5


@pytest.mark.parametrize("K,R", [(2, 2), (3, 2), (2, 3), (3, 3)])
def test_fit_matches_brute_force(K, R):
    """DP vs enumeration of every work/off string, validity judged by the validator."""
    days = 8
    for leave_bits in range(0, 1 << days, 37):                  # a sample of leave patterns
        leave = {d for d in range(days) if leave_bits >> d & 1}
        p = mini(days, ["D", "N"], [Nurse("A", unavailable=sorted(leave))],
                 max_consecutive_shifts=K, double_cycle_rest=R)
        best = 0
        for bits in itertools.product((0, -1), repeat=days):
            row = list(bits)
            if any(row[d] >= 0 for d in leave):
                continue
            if validate(p, [row])[0] == []:
                best = max(best, sum(v >= 0 for v in row))
        assert max_shifts_fit(days, leave, K, R) == best, (leave, K, R)


def test_fit_uses_history_state():
    # history WWW o WWW -> at least 2 off days before working again
    assert max_shifts_fit(3, set(), 3, 2, ["D", "D", "D", "", "D", "D", "D"]) == 1
    assert max_shifts_fit(3, set(), 3, 2, ["D", "D"]) == 2       # 1 more then off, then 1


# ---- F05 (core side): history depth needed by the pattern rules ---------------------------
def test_history_depth_warning():
    assert needed_history(er_department()) == 8               # 2K + R with K=3, R=2
    p = mini(5, ["D", "N"], [Nurse("A", previous=["D", "D", ""])], max_consecutive_shifts=3,
             double_cycle_rest=2)
    assert any("history covers 3" in i for i in precheck(p))


# ---- F17: calendar range -----------------------------------------------------------------
def test_month_info_range():
    with pytest.raises(ValueError, match="Ramadan dates"):
        month_info(2100, 1)
    with pytest.raises(ValueError, match="Ramadan dates"):
        month_info(2077, 11)
    assert month_info(2026, 3).days == 31
