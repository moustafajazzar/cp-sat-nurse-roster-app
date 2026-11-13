"""Regression tests for the planning
cutoff. Days before `Problem.planning_from` have happened: a hard rule they broke is a recorded
fact (reported in `Analysis.elapsed`, never enforced, never blocking), while an obligation that
reaches into the free days (rest after a night, the run limit, hours, switches) is enforced."""
from __future__ import annotations

import json

import pytest

from nurse_roster.__main__ import main
from nurse_roster.explain import explain, report
from tests.er_fixture import generate, raw, request_of, write_request
from nurse_roster.model import build_model, solve
from nurse_roster.precheck import precheck
from nurse_roster.problem import Headcount, Nurse, Problem
from nurse_roster.validate import validate
from tests.rules_helper import only
from tests.test_core import hours_general, mini, quiet


def one_d(days: int, nurses: list[Nurse], **kw) -> Problem:
    """A D-only department needing one D per day; every other rule off unless asked for."""
    return mini(days, ["D"], nurses, headcounts=[Headcount("D", 1, 1, 1, 1)], headcount_enabled=True, **kw)


# ---- an uncovered past day cannot block the re-plan ---------------------------------------------
def test_uncovered_past_day_does_not_block_the_replan():
    p = one_d(4, [Nurse("a", unavailable=[0]), Nurse("b")], weights=only("change", 3))
    p.published = [["D", "D", "", ""], ["", "", "D", "D"]]      # a was absent on day 0, nobody covered
    assert p.replan_from(1) == 1 and p.planning_from == 1
    assert not any("day 0" in i for i in precheck(p))
    sol = solve(build_model(p), p, time_limit=5)
    assert sol.status == "OPTIMAL"                             # used to be INFEASIBLE
    assert sol.assignment == [[-1, 0, -1, -1], [-1, -1, 0, 0]] and sol.penalty_breakdown["change"] == 0
    assert sol.hard_errors == [] and validate(p, sol.assignment)[0] == []
    assert sol.analysis.elapsed == ["headcount: day 0 D = 0, want 1..1"]
    assert "recorded before the planning cutoff (facts, not errors): 1" in report(sol.analysis)
    p.planning_from = 0                                        # control: the same facts without the
    assert solve(build_model(p), p, time_limit=5).status == "INFEASIBLE"   # cutoff block the month


def test_elapsed_per_day_rules_are_facts_in_the_validator():
    p = mini(2, ["D"], [Nurse("a", gender="M", pinned={0: "D"}), Nurse("b", team_leader=True, pinned={0: ""})],
             headcounts=[Headcount("D", 2, 2, 2, 2)], headcount_enabled=True, require_team_leader=True,
             require_both_genders=True)
    p.planning_from = 1
    an = explain(p, [[0, 0], [-1, 0]])
    assert an.errors == []
    assert an.elapsed == ["headcount: day 0 D = 1, want 2..2", "team leader: day 0 D has none",
                          "gender: day 0 D has only {'M'}"]
    assert all(m.elapsed for m in an.rules["headcount"].matches)
    an = explain(p, [[0, 0], [-1, -1]])                        # the same breaches on the free day
    assert len(an.elapsed) == 3 and len(an.errors) == 3 and "headcount: day 1 D = 1, want 2..2" in an.errors
    assert "team leader: day 1 D has none" in an.errors and "gender: day 1 D has only {'M'}" in an.errors


def test_a_forbidden_succession_wholly_in_the_past_is_a_fact():
    p = mini(3, ["D", "N"], [Nurse("a", pinned={0: "N", 1: "D"})], no_mixed_blocks=True)
    assert any("forbidden succession" in i for i in precheck(p))
    assert solve(build_model(p), p, time_limit=5).status == "INFEASIBLE"
    p.planning_from = 2
    assert not any("forbidden succession" in i for i in precheck(p))
    sol = solve(build_model(p), p, time_limit=5)
    assert sol.status == "OPTIMAL" and sol.hard_errors == []
    assert sol.analysis.elapsed == ["succession: a N->D day 0"]
    assert explain(p, [[1, 0, 1]]).errors == ["succession: a D->N day 1"]   # crossing the cutoff: an error


# ---- obligations crossing the cutoff stay enforced ---------------------------------------------
def test_rest_after_an_elapsed_night_is_enforced():
    p = mini(4, ["D", "N"], [Nurse("a", pinned={0: "N", 1: "N"}), Nurse("b", pinned={0: "D", 1: "D"})],
             headcounts=[Headcount("D", 1, 1, 1, 1), Headcount("N", 0, 1, 0, 1)], headcount_enabled=True,
             night_rest_days=2, night_rest_hard=True)
    p.planning_from = 2
    sol = solve(build_model(p), p, time_limit=5)
    assert sol.status == "OPTIMAL" and sol.hard_errors == []
    assert 0 not in sol.assignment[0][2:] and sol.assignment[1][2:] == [0, 0]   # a owes rest, b covers
    breach = [[1, 1, 0, -1], [0, 0, -1, 0]]
    text = "night rest: a D day 2 has only 0 day(s) off since N day 1, needs 2"
    assert explain(p, breach).errors == [text]                 # a D on the first free day: an error
    p.planning_from, p.nurses[0].pinned[2], p.nurses[1].pinned[2] = 3, "D", ""
    an = explain(p, breach)                                    # the same D, now elapsed: a fact
    assert an.errors == [] and an.elapsed == [text]
    assert solve(build_model(p), p, time_limit=5).status == "OPTIMAL"


def test_run_limit_crosses_the_cutoff_and_a_broken_past_run_does_not_block():
    p = one_d(6, [Nurse("a", pinned={d: "D" for d in range(4)}), Nurse("b", pinned={d: "" for d in range(4)})],
              max_consecutive_shifts=3)                         # a worked four in a row: one too many
    p.planning_from = 4
    sol = solve(build_model(p), p, time_limit=5)
    assert sol.status == "OPTIMAL" and sol.hard_errors == []
    assert sol.assignment[0][4] == -1 and sol.assignment[1][4] == 0   # day 4 off for a is still forced
    assert sol.analysis.elapsed == ["consecutive: a 4 shifts in a row at day 3"]
    an = explain(p, [[0, 0, 0, 0, 0, -1], [-1, -1, -1, -1, -1, 0]])
    assert an.errors == ["consecutive: a 5 shifts in a row at day 4"] and len(an.elapsed) == 1


def test_double_cycle_rest_crosses_the_cutoff():
    p = one_d(9, [Nurse("a", pinned={0: "D", 1: "D", 2: "", 3: "D", 4: "D"}),
                  Nurse("b", pinned={0: "", 1: "", 2: "D", 3: "", 4: ""})],
              max_consecutive_shifts=2, double_cycle_rest=2)   # W W O W W happened: two days off owed
    p.planning_from = 5
    sol = solve(build_model(p), p, time_limit=5)
    assert sol.status == "OPTIMAL" and sol.assignment[0][5:7] == [-1, -1] and sol.hard_errors == []
    p.nurses[0].pinned.update({5: "", 6: "D"}); p.nurses[1].pinned.update({5: "D", 6: ""})
    p.planning_from = 7                                        # the rest was broken on day 6: a fact
    sol = solve(build_model(p), p, time_limit=5)
    assert sol.status == "OPTIMAL" and sol.hard_errors == []
    assert sol.analysis.elapsed == ["double cycle: a needs 2 days off after day 4"]


def test_hours_already_over_the_cap_are_a_fact():
    p = mini(6, ["D"], [Nurse("a", pinned={d: "D" for d in range(5)})])
    p.general = hours_general(overtime_month_cap=10)
    p.planning_from = 5                                        # 60 h worked against 40 owed, cap 10
    assert p.elapsed_hours(p.nurses[0]) == 60 and p.required_hours(p.nurses[0]) == 40
    sol = solve(build_model(p), p, time_limit=5)
    assert sol.status == "OPTIMAL" and sol.assignment == [[0, 0, 0, 0, 0, -1]]   # no further shift
    assert sol.hard_errors == [] and sol.analysis.elapsed == ["overtime: a 20 > cap 10"]
    assert explain(p, [[0] * 6]).errors == ["overtime: a 32 > cap 10"]   # a shift on top: an error
    p.planning_from = 0
    assert solve(build_model(p), p, time_limit=5).status == "INFEASIBLE"


def test_switches_already_over_the_maximum_are_a_fact():
    p = mini(5, ["D", "N"], [Nurse("a", pinned={0: "D", 1: "N", 2: "D"})], max_switches=1, switches_hard=True)
    p.planning_from = 3
    sol = solve(build_model(p), p, time_limit=5)
    assert sol.status == "OPTIMAL" and 1 not in sol.assignment[0][3:]   # no third switch
    assert sol.analysis.elapsed == ["switches: a changes night/day 2 times, max 1"]
    assert explain(p, [[0, 1, 0, 1, -1]]).errors == ["switches: a changes night/day 3 times, max 1"]
    p.planning_from = 0
    assert solve(build_model(p), p, time_limit=5).status == "INFEASIBLE"


# ---- the contract -------------------------------------------------------------------------------
def test_elapsed_pins_may_break_a_shift_restriction_but_future_pins_may_not():
    p = mini(3, ["D", "N"], [Nurse("a", allowed_shifts=["D"], pinned={0: "N"})])   # N no longer allowed
    with pytest.raises(ValueError, match="not in her allowed shifts"):
        p.check()
    p.planning_from = 1
    p.check()
    sol = solve(build_model(p), p, time_limit=5)
    assert sol.status == "OPTIMAL" and sol.assignment[0][0] == 1 and sol.hard_errors == []
    assert sol.analysis.elapsed == ["shift restriction: a works N on day 0"]
    p.nurses[0].pinned[1] = "N"
    with pytest.raises(ValueError, match="not in her allowed shifts"):
        p.check()


def test_planning_from_contract_and_json_round_trip(tmp_path):
    p = one_d(3, [Nurse("a"), Nurse("b")])
    for bad in (4, -1, True, "1"):
        p.planning_from = bad
        with pytest.raises(ValueError, match="planning_from must be an integer within 0..3"):
            p.check()
    p.planning_from = 1
    with pytest.raises(ValueError, match="a: day 0 is before planning_from"):
        p.check()
    p.nurses[0].pinned, p.nurses[1].pinned = {0: "D"}, {0: ""}
    p.check()
    f = tmp_path / "p.json"
    p.to_json(str(f))
    assert json.loads(f.read_text())["planning_from"] == 1
    assert Problem.from_json(str(f)).planning_from == 1
    assert Problem.from_dict(raw(days=1)).planning_from == 0
    q = one_d(3, [Nurse("a"), Nurse("b")])
    q.published = [["D", "D", "D"], ["", "", ""]]
    q.replan_from(2)
    assert q.planning_from == 2
    with pytest.raises(ValueError, match="needs a published roster"):
        one_d(3, [Nurse("a")]).replan_from(1)


def test_cli_reports_elapsed_facts_and_still_succeeds(tmp_path, capsys):
    p = one_d(30, [Nurse("a", unavailable=[0]), Nurse("b")], weights=only("change", 3))
    p.published = [["D", "D"] + ["", ""] * 14, ["", ""] + ["D", "D"] * 14]
    f = str(tmp_path / "r.json")
    write_request(request_of(p, "2026-06", time_limit=2, replan_from=1), f)
    assert main(["solve", f]) == 0                             # not 2: no hard violation
    out = capsys.readouterr().out
    assert "recorded before 2026-06-02 (facts, not errors): 1 rule violation(s)" in out
    assert "headcount: day 0 D = 0, want 1..1" in out and "HARD VIOLATIONS" not in out


def test_model_and_validator_agree_on_a_realistic_replan():
    p = generate(25, 10, 0, 1, False, [])
    sol = solve(build_model(p), p, time_limit=10)
    assert sol.assignment
    sh = p.department.shifts
    p.published = [[sh[v] if v >= 0 else "" for v in row] for row in sol.assignment]
    worked = [n for n in range(len(p.nurses)) if sol.assignment[n][3] >= 0]
    p.nurses[worked[0]].unavailable = sorted(set(p.nurses[worked[0]].unavailable) | {3})   # sick, no cover
    p.replan_from(5)
    sol2 = solve(build_model(p), p, time_limit=20)
    assert sol2.status in ("OPTIMAL", "FEASIBLE") and sol2.hard_errors == []
    assert sol2.analysis.elapsed and all("day 3" in e for e in sol2.analysis.elapsed)
    errors, pen, total = validate(p, sol2.assignment)
    assert errors == [] and pen == sol2.penalty_breakdown and total == sol2.objective
    if sol2.status == "OPTIMAL":
        assert abs(sol2.objective - sol2.solver_objective) < 0.5       # soft costs agree, past included
