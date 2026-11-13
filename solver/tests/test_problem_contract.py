"""Regression tests for the problem contract and its arithmetic: types, names a rule refers to, sizes and magnitudes,
fairness shares, the precheck's capacity and leave explanations, the reported cost, and the API and CLI rejecting bad
arguments cleanly."""
from __future__ import annotations

import io
import json

import pytest

from nurse_roster import fairness as F
from nurse_roster.__main__ import main
from nurse_roster.export import write_csv
from nurse_roster.model import build_model, solve
from nurse_roster.precheck import fewest_shifts, precheck
from nurse_roster.problem import (MAX_DAYS, MAX_HISTORY, Headcount, Nurse, Problem,
                                  Weights)
from nurse_roster.validate import validate
from tests.er_fixture import dep as F_dep, gen as F_gen, nurse, raw, request_of, write_request
from tests.test_core import hours_general, mini, quiet


def dep(**over) -> dict:
    return raw(department=F_dep(**over))


def gen(**over) -> dict:
    return raw(general=F_gen(**over))


def hc(**over) -> dict:
    d = {"shift": "D", "weekday_min": 1, "weekday_max": 1, "weekend_min": 1, "weekend_max": 1}
    d.update(over)
    return d


# ---- types: flags must be booleans, arrays must be lists, objects must be objects ------------
@pytest.mark.parametrize("bad", [
    raw(nurses=[nurse("A", team_leader="no")]),
    raw(nurses=[nurse("A", allowed_shifts="DN")]),
    raw(nurses=[nurse("A", previous="DN")]),
    raw(nurses=[nurse("A", unavailable=5)]),
    raw(nurses=[nurse("A", breastfeeding=1)]),
    raw(nurses=5),
    raw(nurses=[]),
    raw(nurses=[{k: v for k, v in nurse("A").items() if k != "id"}]),   # no id
    raw(general=[]),
    raw(general=[["weights", 1]]),
    gen(hours_rule_enabled="false"),
    gen(forbidden_successions=["DC"]),                        # a string is not a pair
    gen(forbidden_successions=[["d", "C"]]),                  # unknown shift name
    raw(first_weekday=True),
    dep(night_rest_hard="false"),
    dep(headcount_enabled=0.0),
    dep(shifts="DN"),
    dep(headcounts=[{"shift": "D"}]),                         # bounds missing
    dep(headcounts=[hc(also_counts="N")]),
])
def test_types_are_checked_and_raise_value_error(bad):
    with pytest.raises(ValueError):
        Problem.from_dict(bad)


# ---- names a rule refers to must resolve ------------------------------------------------------
def test_night_and_day_shift_must_exist_while_night_rules_are_on():
    with pytest.raises(ValueError, match="night_shift 'n'"):
        Problem.from_dict(dep(night_shift="n"))
    with pytest.raises(ValueError, match="night_shift 'N'"):
        Problem.from_dict(dep(shifts=["D", "A"], headcounts=[hc()], fair_shifts=["D"]))
    with pytest.raises(ValueError, match="must differ"):
        Problem.from_dict(dep(day_shift="N"))
    # a day-only unit may drop the night shift once every night rule is switched off
    p = mini(3, ["A", "B"], [Nurse("x")], night_shift="N", day_shift="D")
    p.check()


def test_fair_shift_rules():
    with pytest.raises(ValueError, match="repeat"):
        Problem.from_dict(dep(fair_shifts=["D", "D"]))
    with pytest.raises(ValueError, match="no headcount"):
        Problem.from_dict(dep(headcounts=[hc()]))             # N fair, no headcount


def test_score_target_above_band_is_rejected():
    with pytest.raises(ValueError, match="score_band_hi"):
        Problem.from_dict(dep(score_target=0.9, score_band_hi=0.8))


# ---- sizes and magnitudes ---------------------------------------------------------------------
@pytest.mark.parametrize("bad, text", [
    (raw(days=MAX_DAYS + 1), "days"),
    (raw(days=10 ** 9), "days"),
    (raw(nurses=[nurse("A", previous=[""] * (MAX_HISTORY + 1))]), "history"),
    (gen(weights={**F_gen()["weights"], "fair_day": 10 ** 18}), "weights.fair_day"),
    (gen(shift_hours={"D": 1000, "N": 12}), "shift_hours"),
    (raw(nurses=[nurse("A", hours_balance=-10 ** 7)]), "hours_balance"),
    (raw(nurses=[nurse("A", ytd_overtime=-5)]), "ytd_overtime"),
    (gen(breastfeeding_reduction=9), "breastfeeding_reduction"),
    (gen(ramadan_hours_per_day=9), "ramadan_hours_per_day"),
])
def test_bounds(bad, text):
    with pytest.raises(ValueError, match=text):
        Problem.from_dict(bad)


def test_required_hours_cannot_go_negative_through_the_contract():
    p = Problem.from_dict(raw(nurses=[nurse("A", hours_balance=-11, breastfeeding=True)],
                              general=F_gen(breastfeeding_reduction=6)))
    assert all(p.required_hours(n) >= 0 for n in p.nurses)


# ---- fairness arithmetic ----------------------------------------------------------------------
def two_shift(weights=None, **over) -> Problem:
    nurses = [Nurse("A", gender="M", team_leader=True), Nurse("B"), Nurse("C", gender="M")]
    kw = dict(headcounts=[Headcount("D", 2, 2, 2, 2), Headcount("N", 1, 1, 1, 1)], headcount_enabled=True,
              fair_shifts=["D", "N"])
    kw.update(over)
    return mini(7, ["D", "N"], nurses, weights=weights, **kw)


def test_shares_sum_to_one_thousand_and_model_matches_validator():
    p = two_shift(Weights(**{**{f: 0 for f in Weights.__slots__}, "fair_day": 1, "fair_night": 1}))
    sh = F.share_m(p)
    assert sh == {"D": 667, "N": 333} and sum(sh.values()) == 1000
    sol = solve(build_model(p), p, time_limit=10, workers=4)
    assert sol.status == "OPTIMAL"
    errors, pen, total = validate(p, sol.assignment)
    assert errors == [] and total == sol.objective == sol.solver_objective


def test_night_balance_only_moves_when_nights_are_a_fair_shift():
    p = two_shift(fair_shifts=["D"])
    n = p.nurses[0]
    nights_only = [1] * 7
    assert F.balances_out(p, n, nights_only)[0] == n.night_balance == 0.0
    p2 = two_shift(fair_shifts=["D", "N"])
    assert F.balances_out(p2, p2.nurses[0], nights_only)[0] == pytest.approx(7 - 0.333 * 7, abs=0.01)


def test_friday_balance_is_prorated_for_leave():
    p = two_shift(fair_weekends=True, first_weekday=4)                # day 0 is a Friday
    on_leave = Nurse("L", unavailable=list(range(7)))
    p.nurses.append(on_leave)
    assert F.friday_expected_m(p, on_leave) == 0
    assert F.balances_out(p, on_leave, [-1] * 7)[1] == 0.0
    assert F.friday_expected_m(p, p.nurses[0]) == F.friday_expected_m(p) > 0
    # the in-month target uses the same prorated expectation, so a month of leave costs nothing
    p.general.weights.fair_weekend = 1
    errors, pen, _ = validate(p, [[-1] * 7] * 4)
    assert pen["fair_weekend"] == 3 * -(-(F.friday_expected_m(p) - 500) // 100)   # the 3 present nurses


# ---- precheck explains attribute capacity, leave clustering, unbounded shifts -----------------
def test_precheck_team_leader_capacity_and_leave_clustering():
    p = mini(7, ["D", "N"], [Nurse("T", team_leader=True), Nurse("x", gender="M"), Nurse("y", gender="M"),
                             Nurse("z"), Nurse("w")],
             headcounts=[Headcount("D", 1, 2, 1, 2), Headcount("N", 1, 2, 1, 2)], headcount_enabled=True,
             require_team_leader=True)
    issues = precheck(p)
    assert any(i.startswith("team leaders: 14 anchor shifts") for i in issues), issues
    assert solve(build_model(p), p, time_limit=5, workers=2).status == "INFEASIBLE"

    p = mini(7, ["D", "N"], [Nurse("T1", team_leader=True, unavailable=[3]), Nurse("T2", team_leader=True, unavailable=[3]),
                             Nurse("x"), Nurse("y"), Nurse("z"), Nurse("w")],
             headcounts=[Headcount("D", 1, 2, 1, 2), Headcount("N", 1, 2, 1, 2)], headcount_enabled=True,
             require_team_leader=True)
    issues = precheck(p)
    assert "day 3: no team leader available for D" in issues and "day 3: no team leader available for N" in issues

    p = mini(3, ["D", "N"], [Nurse("a", unavailable=[0]), Nurse("b", unavailable=[0]), Nurse("c")],
             headcounts=[Headcount("D", 2, 2, 2, 2), Headcount("N", 0, 1, 0, 1)], headcount_enabled=True)
    issues = precheck(p)
    assert "day 0: D needs 2 but only 1 eligible nurses are not on leave" in issues
    assert "day 0: headcounts need 2 nurses but only 1 are not on leave" in issues


def test_precheck_warns_on_a_shift_no_headcount_governs_and_empty_history():
    p = mini(3, ["D", "N", "A"], [Nurse("a", previous=["D"] * 5), Nurse("b")],
             headcounts=[Headcount("D", 0, 1, 0, 1), Headcount("N", 0, 1, 0, 1)], headcount_enabled=True,
             max_consecutive_shifts=3)
    issues = precheck(p)
    assert any(i.startswith("A: no headcount names it") for i in issues)
    assert any(i.startswith("b: history covers 0 day(s)") for i in issues)
    assert not any(i.startswith("a:") for i in issues)


def test_precheck_hours_use_every_allowed_shift_length():
    assert fewest_shifts([8, 12], 15, 22, 7) == 2             # D + C = 20 h
    assert fewest_shifts([12], 15, 22, 7) is None             # 12 or 24 h only
    g = hours_general(hours_per_day=11, overtime_month_cap=0)
    p = mini(2, ["D", "N", "C"], [Nurse("a")])
    p.general = g
    assert p.required_hours(p.nurses[0]) == 22
    assert not any("cannot be met" in i for i in precheck(p))
    assert solve(build_model(p), p, time_limit=5, workers=2).status != "INFEASIBLE"
    p.general.hours_per_day = 20                              # 40 h in 2 days of 12/8 h shifts: the
    assert any("40 h owed but at most 24 h fit her free days" in i for i in precheck(p))   # free days
    sol = solve(build_model(p), p, time_limit=5, workers=2)   # cannot reach it, so the shortfall is a
    assert sol.status == "OPTIMAL" and sol.hard_errors == []  # recorded fact, not an infeasibility
    worked = sum(g.shift_hours[p.department.shifts[v]] for v in sol.assignment[0] if v >= 0)
    assert worked >= 24 - 7                                   # the dominant weight pulls to what she can
    assert sol.analysis.elapsed == [] and sol.penalty_breakdown["avoidable_shortfall"] == 0   # reach


# ---- reported cost is the roster's cost -------------------------------------------------------
def test_solution_cost_is_the_validators_and_messages_explain_no_roster():
    p = two_shift(Weights(**{**{f: 0 for f in Weights.__slots__}, "fair_day": 1}))
    sol = solve(build_model(p), p, time_limit=10, workers=4)
    errors, pen, total = validate(p, sol.assignment)
    assert sol.penalty_breakdown == pen and sol.objective == total and sol.hard_errors == []
    p2 = mini(2, ["D", "N"], [Nurse("a")], headcounts=[Headcount("D", 2, 2, 2, 2)], headcount_enabled=True)
    sol2 = solve(build_model(p2), p2, time_limit=5, workers=2)
    assert sol2.status == "INFEASIBLE" and "hard rules" in sol2.message


def test_solve_rejects_bad_parameters_and_hints():
    p = two_shift()
    rm = build_model(p)
    with pytest.raises(ValueError, match="time_limit"):
        solve(rm, p, time_limit=-1)
    with pytest.raises(ValueError, match="workers"):
        solve(rm, p, workers=0)
    with pytest.raises(ValueError, match="hint"):
        solve(rm, p, hint=[[0] * 8] * 3)                      # row longer than the month
    with pytest.raises(ValueError, match="hint"):
        solve(rm, p, hint=[[0] * 7])                          # fewer rows than nurses


# ---- API robustness -----------------------------------------------------------------------------
def test_write_csv_rejects_bad_matrix_and_to_json_takes_a_stream():
    p = two_shift()
    with pytest.raises(ValueError, match="matrix"):
        write_csv(p, [[0] * 3], io.StringIO())
    buf = io.StringIO()
    p.to_json(buf)
    assert Problem.from_dict(json.loads(buf.getvalue())) == p


def test_cli_rejects_bad_arguments_cleanly(tmp_path, capsys):
    assert main(["solve", str(tmp_path / "missing.json")]) == 4
    with pytest.raises(SystemExit):
        main(["solve", "r.json", "--workers", "0"])
    r = request_of(mini(30, ["D"], [Nurse("a")]), "2026-06")
    r["month"] = "2026-6"
    write_request(r, tmp_path / "r.json")
    assert main(["solve", str(tmp_path / "r.json")]) == 4
    err = capsys.readouterr().err
    assert "error:" in err and "error: request: month must be YYYY-MM" in err
