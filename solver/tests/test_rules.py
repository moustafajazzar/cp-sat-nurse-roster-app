"""Registry, explanation and per-rule constraint tests (Timefold-style `penalizes_by`):
each rule gets a tiny fixture and an exact amount, and each soft rule is also solved with
only its weight on so the solver's objective must equal the validator's amount."""
from __future__ import annotations

import json

import pytest

from nurse_roster.explain import diff, explain, report
from tests.er_fixture import generate
from nurse_roster.model import build_model, solve
from nurse_roster.problem import Headcount, Nurse, SkillNeed, Weights
from nurse_roster.rules import HARD, RULES, active, level, soft_ids, weight
from nurse_roster.validate import CHECKS, validate
from tests.test_core import hours_general, mini, quiet
from tests.rules_helper import penalises, violates, only


# ---- registry ---------------------------------------------------------------------------------
def test_registry_covers_weights_checks_and_model_terms():
    assert {RULES[rid].weight for rid in soft_ids()} == set(Weights.__slots__)
    assert set(CHECKS) == set(RULES)
    p = generate(6, 7, 0, 0, False, [])
    rm = build_model(p)
    assert set(rm.penalties) == set(soft_ids())
    _, br, _ = validate(p, [[-1] * 7 for _ in p.nurses])
    assert set(br) == set(soft_ids())


def test_level_follows_department_flags():
    p = mini(3, ["D", "N"], [Nurse("a")], night_rest_days=2, night_rest_hard=True)
    assert level(p, "night_rest") == HARD and weight(p, "night_rest") == 0 and active(p, "night_rest")
    p.department.night_rest_hard = False
    assert level(p, "night_rest") == "soft" and not active(p, "night_rest")   # quiet weights
    p.general.weights.night_rest = 5
    assert active(p, "night_rest") and weight(p, "night_rest") == 5
    p.department.night_rest_days = 0
    assert not active(p, "night_rest")


# ---- explanation ------------------------------------------------------------------------------
def test_explain_matches_validate_and_sums_per_nurse():
    p = generate(25, 10, 0, 1, False, [])
    sol = solve(build_model(p), p, time_limit=3)      # any roster will do
    assert sol.assignment and sol.analysis is not None
    err, br, tot = validate(p, sol.assignment)
    an = sol.analysis
    assert {rid: an.rules[rid].weighted for rid in soft_ids()} == br == sol.penalty_breakdown
    assert an.total == tot == sum(an.nurses.values()) == sol.objective
    assert an.errors == err
    assert diff(an, an)["rules"] == {} and diff(an, an)["nurses"] == {}
    txt = report(an)
    assert txt.startswith(f"cost {tot}")


def test_explain_attributes_department_rules_to_nobody(tmp_path):
    p = mini(2, ["D"], [Nurse("a", score=0.5), Nurse("b", score=0.5)],
             weights=Weights(**{**{f: 0 for f in Weights.__slots__}, "score_deficit": 2}),
             headcounts=[Headcount("D", 0, 2, 0, 2)], score_target=0.7)
    a = [[0, -1], [0, -1]]
    an = explain(p, a)
    assert an.rules["score_deficit"].raw == 40 and an.total == 80
    assert an.nurses == {"a": 0, "b": 0, "": 80}
    m = an.rules["score_deficit"].matches[0]
    assert (m.nurse, m.day, m.amount) == (None, 0, 40)
    an.to_json(str(tmp_path / "x.json"))
    data = json.loads((tmp_path / "x.json").read_text())
    assert data["total"] == 80 and data["rules"]["score_deficit"]["matches"][0]["day"] == 0


def test_diff_reports_moved_cost():
    p = mini(3, ["D"], [Nurse("a", unavailable=[1]), Nurse("b")],
             weights=only("excess_weekend", 6), max_weekends=0, first_weekday=4)   # day 0 is Friday
    before = explain(p, [[-1, -1, 0], [0, -1, -1]])
    after = explain(p, [[0, -1, 0], [-1, -1, -1]])
    d = diff(before, after)
    assert d["total"] == {"": 0} and d["nurses"] == {"a": 6, "b": -6}
    assert "unavailable: a works day 1" not in after.errors         # day 1 is off in both
    assert before.rules["excess_weekend"].weighted == 6


# ---- hard rules, exact matches ---------------------------------------------------------------
def test_unavailable_and_allowed_shifts():
    p = mini(3, ["D", "N"], [Nurse("a", unavailable=[1], allowed_shifts=["D"])])
    assert violates(p, [[0, 0, 1]], "unavailable") == ["unavailable: a works day 1"]
    assert violates(p, [[0, 0, 1]], "allowed_shifts") == ["shift restriction: a works N on day 2"]
    assert violates(p, [[0, -1, 0]], "unavailable") == [] == violates(p, [[0, -1, 0]], "allowed_shifts")


def test_succession_including_the_month_boundary():
    p = mini(3, ["D", "N"], [Nurse("a", previous=["N"])], no_mixed_blocks=True)
    assert violates(p, [[0, -1, -1]], "succession") == ["succession: a N on day -1 -> D on day 0"]
    assert violates(p, [[-1, 0, 1]], "succession") == ["succession: a D on day 1 -> N on day 2"]
    assert violates(p, [[-1, 1, 1]], "succession") == []


def test_hours_bounds_both_sides():
    p = mini(5, ["D"], [Nurse("a")], first_weekday=5)     # Sat..Wed: 5 x 8 h = 40 h owed, D = 12 h
    p.general = hours_general(overtime_month_cap=10)
    assert violates(p, [[0, 0, -1, -1, -1]], "hours_bounds") == []   # under-hours is the soft, dominant
    assert penalises(p, [[0, 0, -1, -1, -1]], "avoidable_shortfall") == 5 * 1000   # avoidable_shortfall:
    assert violates(p, [[0, 0, 0, 0, 0]], "hours_bounds") == ["overtime: a 20 > cap 10"]   # 16 h short, 5 h
    assert violates(p, [[0, 0, 0, -1, -1]], "hours_bounds") == []   # 36 h: 4 short, carried   # beyond the
    assert penalises(p, [[0, 0, 0, -1, -1]], "avoidable_shortfall") == 0                      # remainder


def test_consecutive_and_double_cycle():
    p = mini(7, ["D"], [Nurse("a", previous=["D", "D"])], max_consecutive_shifts=2, double_cycle_rest=2)
    assert violates(p, [[0, -1, -1, -1, -1, -1, -1]], "consecutive") == ["consecutive: a 3 shifts in a row at day 0"]
    #                 hist D D | off D D off D : cycle, 1 off, cycle, then a worked day in the rest
    assert violates(p, [[-1, 0, 0, -1, 0, -1, -1]], "double_cycle") == ["double cycle: a needs 2 days off after day 2"]
    assert violates(p, [[-1, 0, 0, -1, -1, 0, 0]], "double_cycle") == []


def test_headcount_team_leader_gender():
    nurses = [Nurse("a", gender="F", team_leader=True), Nurse("b", gender="M")]
    p = mini(2, ["D"], nurses, headcounts=[Headcount("D", 2, 2, 2, 2)], headcount_enabled=True,
             require_team_leader=True, require_both_genders=True)
    a = [[0, -1], [0, 0]]
    assert violates(p, a, "headcount") == ["headcount: day 1 D = 1, want 2..2"]
    assert violates(p, a, "team_leader") == ["team leader: day 1 D has none"]
    assert violates(p, a, "gender") == ["gender: day 1 D has only {'M'}"]


def test_night_rest_hard_or_soft():
    p = mini(4, ["D", "N"], [Nurse("a")], night_rest_days=2, night_rest_hard=True)
    a = [[1, -1, 0, -1]]
    assert violates(p, a, "night_rest") == ["night rest: a D day 2 has only 1 day(s) off since N day 0, needs 2"]
    p.department.night_rest_hard = False
    p.general.weights.night_rest = 5
    assert violates(p, a, "night_rest") == [] and penalises(p, a, "night_rest") == 5


def test_shift_switch_hard_or_soft():
    p = mini(4, ["D", "N"], [Nurse("a", previous=["N"])], max_switches=1, switches_hard=True)
    a = [[0, 1, 0, -1]]                                   # N|D N D: 3 switches
    assert violates(p, a, "shift_switch") == ["switches: a changes night/day 3 times, max 1"]
    p.department.switches_hard = False
    p.general.weights.shift_switch = 8
    assert penalises(p, a, "shift_switch") == 16


# ---- soft rules, exact amounts, and the model agrees ----------------------------------------
def _hours(p, **g):
    p.first_weekday = 5                                   # Sat..Wed: five 8 h days
    p.general = hours_general(weights=p.general.weights, **g)
    return p


@pytest.mark.parametrize("rid,amount,p,a", [
    ("nurse_overtime", 20, _hours(mini(5, ["D"], [Nurse("a")], weights=only("nurse_overtime", 1)),
                                  overtime_month_cap=40), [[0, 0, 0, 0, 0]]),       # 60 − 40
    ("nurse_shortfall", 4, _hours(mini(5, ["D"], [Nurse("a")], weights=only("nurse_shortfall", 1))),
     [[0, 0, 0, -1, -1]]),                                                           # 40 − 36
    ("dept_overtime", 10, _hours(mini(5, ["D"], [Nurse("a")], weights=only("dept_overtime", 1)),
                                 overtime_month_cap=40, dept_overtime_cap=10), [[0, 0, 0, 0, 0]]),
    ("excess_weekend", 6, mini(3, ["D"], [Nurse("a")], weights=only("excess_weekend", 6),
                               max_weekends=0, first_weekday=4), [[0, -1, -1]]),
    ("fair_weekend", 20, mini(8, ["D"], [Nurse("a"), Nurse("b")], weights=only("fair_weekend", 1),
                              headcounts=[Headcount("D", 1, 1, 1, 1)], fair_weekends=True,
                              fair_tolerance=0.0, first_weekday=4), [[0] * 8, [-1] * 8]),
    ("fair_day", 15, mini(4, ["D", "N"], [Nurse("a")], weights=only("fair_day", 1),
                          headcounts=[Headcount("D", 0, 1, 0, 1), Headcount("N", 0, 1, 0, 1)],
                          fair_shifts=["D", "N"], fair_tolerance=0.5), [[0, 0, 0, 0]]),
    ("fair_night", 15, mini(4, ["D", "N"], [Nurse("a")], weights=only("fair_night", 1),
                            headcounts=[Headcount("D", 0, 1, 0, 1), Headcount("N", 0, 1, 0, 1)],
                            fair_shifts=["D", "N"], fair_tolerance=0.5), [[0, 0, 0, 0]]),
    ("score_deficit", 80, mini(2, ["D"], [Nurse("a", score=0.5), Nurse("b", score=0.5)],
                               weights=only("score_deficit", 2), headcounts=[Headcount("D", 0, 2, 0, 2)],
                               score_target=0.7), [[0, -1], [0, -1]]),
    ("undesired_day", 4, mini(3, ["D"], [Nurse("a", undesired_days=[0, 2])], weights=only("undesired_day", 2)),
     [[0, 0, 0]]),
    ("desired_day", 1, mini(3, ["D"], [Nurse("a", desired_days=[1], undesired_days=[0])],
                            weights=only("desired_day", 1)), [[0, -1, -1]]),
    ("score_spread", 20, mini(1, ["D"], [Nurse("a", score=0.9), Nurse("b", score=0.9)],
                              weights=only("score_spread", 1), headcounts=[Headcount("D", 0, 2, 0, 2)],
                              score_balance=True, score_band_hi=0.8), [[0], [0]]),
])
def test_soft_rule_amount(rid, amount, p, a):
    assert penalises(p, a, rid) == amount
    an = explain(p, a)
    assert an.total == amount and not an.errors


def test_each_soft_rule_alone_matches_the_solver():
    """Solving with one weight on: the solver's objective is the validator's amount for that
    rule (per-rule drift check, not only the aggregate one `run()` does)."""
    for rid in soft_ids():
        p = generate(25, 5, 0, 3, False, [])
        p.general.weights = only(rid, 3)
        if rid == "night_rest":
            p.department.night_rest_hard = False
        if rid == "shift_switch":
            p.department.switches_hard = False
        if rid == "skill_need":                               # three holders, one expiring, for 2 a day
            p.department.skill_needs_hard = False
            p.department.skill_needs = [SkillNeed("D", "ICU", 2, 2)]
            for nu, last in zip(p.nurses[:3], (None, None, 2)):
                nu.skills = {"ICU": last}
        if rid == "excess_full_run":                          # none allowed: every full run is priced
            p.department.max_full_runs = 0
        if rid == "overtime_above_share":                     # nothing free above a share
            p.department.share_overtime, p.department.overtime_tolerance = True, 0
        sol = solve(build_model(p), p, time_limit=4, workers=4)
        assert sol.status in ("OPTIMAL", "FEASIBLE"), (rid, sol.status, sol.message)
        assert penalises(p, sol.assignment, rid) == sol.objective, rid
        if sol.status == "OPTIMAL":
            assert sol.solver_objective == sol.objective, rid
        else:                      # one-directional slack: the solver can only overstate
            assert sol.solver_objective >= sol.objective, rid


# ---- requests ---------------------------------------------------------------------------------
def test_preferences_must_be_consistent():
    with pytest.raises(ValueError, match="both desired and undesired"):
        mini(3, ["D"], [Nurse("a", desired_days=[1], undesired_days=[1])]).check()
    p = mini(3, ["D"], [Nurse("a", unavailable=[1], desired_days=[1], undesired_days=[0])],
             weights=Weights(**{**{f: 0 for f in Weights.__slots__}, "undesired_day": 2, "desired_day": 1}))
    p.check()                                              # leave added after the request: fine
    assert explain(p, [[-1, -1, -1]]).total == 0           # the superseded request is not charged
    with pytest.raises(ValueError, match="desired_days day 3 outside"):
        mini(3, ["D"], [Nurse("a", desired_days=[3])]).check()


def test_preferences_steer_the_solver():
    """Two nurses, one D per day: the one who asked off day 0 and asked for day 1 gets it."""
    p = mini(2, ["D"], [Nurse("a", undesired_days=[0], desired_days=[1]), Nurse("b")],
             weights=Weights(**{**{f: 0 for f in Weights.__slots__}, "undesired_day": 2, "desired_day": 1}),
             headcounts=[Headcount("D", 1, 1, 1, 1)], headcount_enabled=True)
    sol = solve(build_model(p), p, time_limit=5)
    assert sol.status == "OPTIMAL" and sol.objective == 0
    assert sol.assignment[0] == [-1, 0] and sol.assignment[1] == [0, -1]


def test_generator_requests_are_consistent_and_exercised():
    p = generate(60, 31, 0, 5, False, [])
    p.check()
    asked = [nu for nu in p.nurses if nu.undesired_days or nu.desired_days]
    assert asked and all(len(nu.undesired_days) == 2 and len(nu.desired_days) == 1 for nu in asked)
