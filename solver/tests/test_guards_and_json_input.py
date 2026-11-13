"""Regression tests for guards and history depth that follow the active rules, carry-over with the hours rule off,
changed-cell counts, pin contradictions in the precheck, duplicate JSON keys and deep nesting."""
from __future__ import annotations

import io
import csv

import pytest

from nurse_roster.__main__ import main
from nurse_roster.export import write_csv
from nurse_roster.model import build_model, solve
from nurse_roster.precheck import needed_history, precheck
from nurse_roster.problem import Headcount, Nurse, Problem, Weights
from nurse_roster.rules import active
from tests.rules_helper import only
from tests.er_fixture import request_of, write_request
from tests.test_core import hours_general, mini, quiet


# ---- N01: guards follow the rules that actually run ------------------------------------------
def test_zero_weight_rules_impose_no_cross_field_guards():
    p = mini(1, ["D"], [Nurse("a")], score_target=0.9, score_balance=True, score_band_hi=0.78)
    assert not active(p, "score_spread") and not active(p, "score_deficit")
    p.check()
    p.general.weights.score_spread = 1                         # band on, deficit still off
    p.check()
    p.general.weights.score_deficit = 2
    with pytest.raises(ValueError, match="must not exceed"):
        p.check()
    p = mini(1, ["D"], [Nurse("a")], night_rest_days=2, night_rest_hard=False)
    assert not active(p, "night_rest")
    p.check()                                                  # no N shift needed by an off rule
    p.general.weights.night_rest = 5
    with pytest.raises(ValueError, match="night_shift 'N' is not a department shift"):
        p.check()


def test_history_depth_follows_active_rules():
    p = mini(3, ["D"], [Nurse("a"), Nurse("b", previous=[""])])
    assert needed_history(p.department, p.general) == 0
    assert not any("history covers" in i for i in precheck(p))
    p.general.forbidden_successions_enabled = True
    assert needed_history(p.department, p.general) == 1
    assert any("a: history covers 0" in i for i in precheck(p))


# ---- N02: the helper itself carries nothing while the hours rule is off ------------------------
def test_carry_over_round_trips_with_the_hours_rule_off():
    p = mini(31, ["D"], [Nurse("a")])
    n = p.nurses[0]
    assert p.carry_over(n, 0) == 0
    n.hours_balance = p.carry_over(n, 0)
    p.check()
    buf = io.StringIO()
    write_csv(p, [[-1] * 31], buf)
    rows = list(csv.reader(io.StringIO(buf.getvalue())))
    assert rows[1][rows[0].index("carry_over")] == "0"


# ---- N03: changed cells are counted, whatever the change weight or the pins -------------------
def test_changed_cells_are_counted_independently_of_the_penalty(tmp_path, capsys):
    p = mini(30, ["D"], [Nurse("a", unavailable=list(range(30))), Nurse("b")],
             headcounts=[Headcount("D", 1, 1, 1, 1)], headcount_enabled=True)
    p.published = [["D"] + [""] * 29, [""] + ["D"] * 29]       # b has to cover day 1 instead of a
    f = str(tmp_path / "r.json")
    write_request(request_of(p, "2026-06", time_limit=2), f)
    assert main(["solve", f, "--explain"]) == 0
    assert "2 cell(s) changed" in capsys.readouterr().out      # change weight is 0 here
    p.nurses[1].pinned = {0: "D"}
    p.general.weights.change = 3
    write_request(request_of(p, "2026-06", time_limit=2), f)
    assert main(["solve", f, "--explain"]) == 0
    assert "2 cell(s) changed" in capsys.readouterr().out      # pinned cells count as changed


# ---- N04: direct pin contradictions are explained before the solve ----------------------------
def test_precheck_explains_pin_conflicts():
    p = mini(1, ["D", "N"], [Nurse("a", pinned={0: "D"}), Nurse("b", pinned={0: "D"})],
             headcounts=[Headcount("D", 1, 1, 1, 1), Headcount("N", 1, 1, 1, 1)], headcount_enabled=True)
    issues = precheck(p)
    assert any("2 nurses are pinned to D" in i for i in issues)
    assert any("N needs 1 but only 0" in i for i in issues)
    assert solve(build_model(p), p, time_limit=2).status == "INFEASIBLE"
    p = mini(7, ["D"], [Nurse("a", pinned={d: "" for d in range(7)})], first_weekday=5)
    p.general = hours_general()                               # hours rule on: pinned off all week,
    issues = precheck(p)                                      # her 48 h are a fact, not an infeasibility
    assert any("48 h owed but at most 0 h fit her free days" in i for i in issues), issues
    sol = solve(build_model(p), p, time_limit=2)
    assert sol.status == "OPTIMAL" and sol.hard_errors == [] and sol.assignment == [[-1] * 7]
    assert sol.analysis.elapsed == [] and sol.penalty_breakdown["avoidable_shortfall"] == 0


# ---- N05: exact duplicate JSON keys are an error ---------------------------------------------
def test_duplicate_json_keys_are_rejected(tmp_path):
    f = tmp_path / "p.json"
    f.write_text('{"days":1,"first_weekday":0,"nurses":[{"id":"a","pinned":{"0":"D","0":"N"}}]}')
    with pytest.raises(ValueError, match="repeats the key '0'"):
        Problem.from_json(str(f))
    f.write_text('{"days":1,"first_weekday":0,"days":2,"nurses":[{"id":"a"}]}')
    with pytest.raises(ValueError, match="repeats the key 'days'"):
        Problem.from_json(str(f))


# ---- N06: deep nesting is an input error ------------------------------------------------------
def test_deeply_nested_json_is_an_input_error(tmp_path, capsys):
    f = tmp_path / "deep.json"
    f.write_text("[" * 10000 + "0" + "]" * 10000)
    assert main(["solve", str(f)]) == 4
    assert "request: JSON nested too deeply" in capsys.readouterr().err
