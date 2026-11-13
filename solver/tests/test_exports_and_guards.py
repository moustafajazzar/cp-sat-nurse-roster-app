"""Regression tests for the CSV without the hours rule, bounded names, the score-target guard, leave superseding
requests, `replan_from` against the request's baseline, a rule-breaking history, and the precheck's capacity."""
from __future__ import annotations

import csv
import io

import pytest

from nurse_roster.__main__ import main
from nurse_roster.export import write_csv
from nurse_roster.explain import explain
from nurse_roster.model import build_model, solve
from nurse_roster.precheck import max_shifts_fit, precheck
from nurse_roster.problem import MAX_NAME, Headcount, Nurse, Problem
from tests.rules_helper import only
from tests.er_fixture import er_general, request_of, write_request
from tests.test_core import mini, quiet


# ---- 1: no carry-over without the hours rule ----------------------------------------------------
def test_csv_exports_no_balance_when_the_hours_rule_is_off():
    p = mini(31, ["D"], [Nurse("a", pinned={d: "" for d in range(31)})])
    assert not p.general.hours_rule_enabled
    buf = io.StringIO()
    write_csv(p, [[-1] * 31], buf)
    row = list(csv.reader(io.StringIO(buf.getvalue())))[1]
    head = list(csv.reader(io.StringIO(buf.getvalue())))[0]
    assert row[head.index("overtime")] == "0" and row[head.index("carry_over")] == "0"
    p.nurses[0].hours_balance = 0
    p.check()


# ---- 2: names are bounded ----------------------------------------------------------------------
def test_names_are_bounded():
    with pytest.raises(ValueError, match=f"longer than {MAX_NAME}"):
        mini(1, ["D"], [Nurse("x" * (MAX_NAME + 1))]).check()


# ---- 3: the target-vs-band guard applies only while the band penalty is on -------------------
def test_score_target_above_band_is_fine_when_the_band_is_off():
    mini(1, ["D"], [Nurse("a")], score_target=0.9, score_balance=False).check()
    mini(1, ["D"], [Nurse("a")], score_target=0.9, score_balance=True, score_balance_mode="spread").check()
    mini(1, ["D"], [Nurse("a")], score_target=0.9, score_balance=True, score_balance_mode="band").check()
    live = er_general()
    with pytest.raises(ValueError, match="must not exceed score_band_hi"):
        p = mini(1, ["D"], [Nurse("a")], score_target=0.9, score_balance=True, score_balance_mode="band")
        p.general = live                                       # deficit and band weights on
        p.check()


# ---- 4: leave added after a request supersedes it ----------------------------------------------
def test_leave_supersedes_requests_without_blocking_or_charging():
    p = mini(2, ["D"], [Nurse("a", undesired_days=[1], desired_days=[0]), Nurse("b")],
             headcounts=[Headcount("D", 1, 1, 1, 1)], headcount_enabled=True,
             weights=only("desired_day", 1))
    p.published = [["D", ""], ["", "D"]]
    p.nurses[0].unavailable = [0, 1]                        # falls sick for both days
    p.replan_from(0)                                        # used to raise "on leave"
    sol = solve(build_model(p), p, time_limit=5)
    assert sol.status == "OPTIMAL" and sol.assignment[0] == [-1, -1]
    assert sol.penalty_breakdown["desired_day"] == 0        # day 0 request superseded, not charged
    assert explain(p, sol.assignment).rules["desired_day"].matches == []


# ---- 5, 6: replan_from with the request's baseline, and no clamping ----------------------------
def test_replan_from_uses_the_request_baseline_and_rejects_dates_beyond_the_month(tmp_path, capsys):
    p = mini(30, ["D"], [Nurse("a"), Nurse("b")], headcounts=[Headcount("D", 1, 1, 1, 1)], headcount_enabled=True)
    p.published = [["D", ""] * 15, ["", "D"] * 15]
    f = str(tmp_path / "r.json")
    write_request(request_of(p, "2026-06", time_limit=2, replan_from=1), f)
    assert main(["solve", f]) == 0
    out = capsys.readouterr().out
    assert "re-planning from 2026-06-02: 1 day(s) pinned to the published roster" in out
    r = request_of(p, "2026-06", time_limit=2, replan_from=1)
    r["replan_from"] = "2026-07-05"
    write_request(r, f)
    assert main(["solve", f]) == 4
    assert "replan_from: 2026-07-05 is outside 2026-06" in capsys.readouterr().err


# ---- 7: a rule-breaking history keeps its trailing obligations --------------------------------
def test_history_that_broke_a_rule_still_binds_the_month():
    hist = [""] * 5 + ["D"] * 4                              # four in a row with K = 3
    assert max_shifts_fit(1, set(), 3, 2, hist) == 0
    p = mini(1, ["D"], [Nurse("a", previous=hist)], headcounts=[Headcount("D", 1, 1, 1, 1)],
             headcount_enabled=True, max_consecutive_shifts=3, double_cycle_rest=2)
    assert any("capacity" in i for i in precheck(p))
    assert solve(build_model(p), p, time_limit=2).status == "INFEASIBLE"
    # a skipped rest is over once worked through: the new block counts from that day
    assert max_shifts_fit(3, set(), 3, 2, ["D", "D", "D", "", "D", "D", "D", "", "D"]) == 2


def test_capacity_never_underestimates_the_validator():
    """Over every short history and small rule pair, the DP's capacity is never below the
    best month the validator accepts (a false 'capacity' warning); overestimates are the
    documented conservative side."""
    import itertools
    from nurse_roster.validate import validate
    under = over = 0
    for K, R in itertools.product((1, 2, 3), repeat=2):
        for L in range(0, 6):
            for hist in itertools.product(["", "D"], repeat=L):
                p = mini(3, ["D"], [Nurse("a", previous=list(hist))], max_consecutive_shifts=K, double_cycle_rest=R)
                best = max(sum(a) for a in itertools.product([0, 1], repeat=3)
                           if not validate(p, [[0 if w else -1 for w in a]])[0])
                fit = max_shifts_fit(3, set(), K, R, list(hist))
                under += fit < best
                over += fit > best
    assert under == 0 and over <= 4
