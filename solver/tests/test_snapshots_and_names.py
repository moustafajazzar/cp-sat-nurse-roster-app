"""Regression tests for the solver's snapshot, the best validated roster, atomic re-plans, names that cannot survive the
CSV, pin and day duplicates, the weekend share with an unusable shift, spread-mode enumeration, the seed range and the
precheck's day types."""
from __future__ import annotations

import csv
import io
import threading
from dataclasses import asdict
from pathlib import Path

import pytest

from nurse_roster.export import write_csv
from nurse_roster.fairness import friday_expected_m
from tests.er_fixture import generate
from nurse_roster.model import build_model, solve
from nurse_roster.precheck import precheck
from nurse_roster.problem import Headcount, Nurse, Problem
from nurse_roster.validate import validate
from tests.rules_helper import only
from tests.test_core import mini


# ---- F02: the solver works on a snapshot of the problem ----------------------------------------
def test_problem_edited_during_the_search_does_not_change_the_result():
    p = mini(1, ["D"], [Nurse("a")], headcount_enabled=True,
             headcounts=[Headcount("D", 1, 1, 1, 1)], weights=only("undesired_day", 2))

    def mutate(_):
        p.department.headcounts[0].weekday_min = 0
        p.nurses[0].undesired_days = [0]

    s = solve(build_model(p), p, time_limit=2, workers=1, on_solution=mutate)
    assert s.status == "OPTIMAL" and s.objective == 0 == s.solver_objective and not s.hard_errors
    assert s.assignment == [[0]]                              # the problem as handed in
    with pytest.raises(ValueError, match="build_model again"):
        solve(build_model(mini(1, ["D"], [Nurse("a")])), p)  # the caller's copy did change


# ---- F03: the best validated roster is returned, not the solver's last incumbent ----------------
def test_progress_keeps_the_best_validated_roster():
    p = generate(25, 7, 0, 0, False, [])
    seen = []
    stop = threading.Event()

    def record(s):
        seen.append(s.objective)
        if len(seen) == 3:
            stop.set()
    sol = solve(build_model(p), p, time_limit=5, on_solution=record, stop=stop)
    assert seen == sorted(seen, reverse=True) and len(set(seen)) == len(seen)   # strict improvements
    assert sol.objective == min(seen) and sol.improvements == len(seen)
    assert validate(p, sol.assignment)[2] == sol.objective


# ---- F04: a failed replan leaves the problem untouched -----------------------------------------
def test_failed_replan_is_atomic(monkeypatch):
    p = mini(2, ["D"], [Nurse("a"), Nurse("b", pinned={1: ""})])
    p.published = [["D", "D"], ["D", "D"]]
    assert p.replan_from(2) == 1                              # a recorded fact is kept
    assert p.nurses[1].pinned == {0: "D", 1: ""}
    p = mini(2, ["D", "N"], [Nurse("a", allowed_shifts=["D"])])
    p.published = [["N", ""]]                                 # a shift she may no longer work: the
    assert p.replan_from(1) == 0                              # elapsed N is a fact
    assert p.nurses[0].pinned == {0: "N"} and p.planning_from == 1
    p.nurses[0].pinned[1] = "N"                               # a future pin still obeys the restriction
    with pytest.raises(ValueError, match="not in her allowed shifts"):
        p.check()
    # whatever makes the final check fail, nothing is left half-written
    p = mini(2, ["D"], [Nurse("a"), Nurse("b", pinned={1: ""})])
    p.published = [["D", "D"], ["D", "D"]]
    real, calls = Problem.check, []

    def check(self):
        calls.append(1)
        if len(calls) == 2:                                   # the completed snapshot check
            raise ValueError("boom")
        real(self)
    monkeypatch.setattr(Problem, "check", check)
    with pytest.raises(ValueError, match="boom"):
        p.replan_from(2)
    assert [dict(n.pinned) for n in p.nurses] == [{}, {1: ""}] and p.planning_from == 0


# ---- F05: names that cannot survive the CSV are refused at the input boundary ----------------
@pytest.mark.parametrize("shift,msg", [("L", "reserved"), ("=1+2", "leading"), ("'x", "leading")])
def test_reserved_and_formula_shift_names_are_rejected(shift, msg):
    p = mini(1, [shift], [Nurse("a")])
    p.general.shift_hours[shift] = 12
    with pytest.raises(ValueError, match=msg):
        p.check()


@pytest.mark.parametrize("nid", ["'a", " ", "a\nb"])
def test_unsafe_nurse_ids_are_rejected(nid):
    with pytest.raises(ValueError, match="nurse id must be"):
        mini(1, ["D"], [Nurse(nid)]).check()


@pytest.mark.parametrize("nid", ["=1+2", "-1", "+1", "@x"])
def test_formula_prefixed_ids_are_escaped_in_the_export(nid, tmp_path):
    """The export escapes them so a spreadsheet shows text, not a formula; they are allowed."""
    path = str(tmp_path / "r.csv")
    p = mini(1, ["D"], [Nurse(nid)])
    p.check()
    write_csv(p, [[0]], path)
    assert list(csv.reader(io.StringIO(Path(path).read_text())))[1][0] == "'" + nid


# ---- F06: colliding pin keys are an error, not a silent choice --------------------------------
def test_pin_key_collision_is_rejected():
    raw = asdict(mini(1, ["D", "N"], [Nurse("a")]))
    raw["nurses"][0]["pinned"] = {"0": "D", "00": "N"}
    with pytest.raises(ValueError, match="day 0 is given twice"):
        Problem.from_dict(raw)
    raw["nurses"][0]["pinned"] = {"0": "D"}
    assert Problem.from_dict(raw).nurses[0].pinned == {0: "D"}


# ---- F07: a day listed twice is rejected ------------------------------------------------------
@pytest.mark.parametrize("field", ["unavailable", "undesired_days", "desired_days"])
def test_duplicate_nurse_days_are_rejected(field):
    with pytest.raises(ValueError, match="lists a day twice"):
        mini(2, ["D"], [Nurse("a", **{field: [0, 0]})]).check()


def test_duplicate_calendar_days_are_rejected():
    p = mini(2, ["D"], [Nurse("a")])
    p.public_holidays = [1, 1]
    with pytest.raises(ValueError, match="listed twice"):
        p.check()
    p.public_holidays, p.ramadan_days = [], [0, 0]
    with pytest.raises(ValueError, match="listed twice"):
        p.check()


# ---- F08: an overlapping shift nobody may work does not dilute the weekend share ---------------
def test_unusable_also_counts_shift_is_ignored_in_the_share():
    p = mini(4, ["D", "N", "C"], [Nurse(str(i), allowed_shifts=["D", "N"]) for i in range(4)],
             headcounts=[Headcount("D", 1, 1, 1, 1, ["C"]), Headcount("N", 2, 2, 2, 2, ["C"])],
             headcount_enabled=True, fair_weekends=True, weights=only("fair_weekend", 1))
    p.general.weekend_weekdays = list(range(7))
    balanced = [[-1, 0, 0, 0], [0, -1, 1, 1], [1, 1, -1, 1], [1, 1, 1, -1]]
    skewed = [[0, 0, -1, -1], [-1, -1, 0, 0], [1, 1, 1, 1], [1, 1, 1, 1]]
    assert friday_expected_m(p, p.nurses[0]) == 3000          # 12 slots / 4 nurses
    assert [validate(p, a)[2] for a in (balanced, skewed)] == [0, 20]
    assert not any("needs" in i for i in precheck(p))


# ---- F10: spread mode enumerates only reachable headcounts ------------------------------------
def test_spread_mode_enumeration_is_clamped_to_the_staff():
    sizes = []
    for maximum in (1, 1000):
        p = mini(7, ["D", "N"], [Nurse("a")],
                 headcounts=[Headcount("D", 0, maximum, 0, maximum), Headcount("N", 0, maximum, 0, maximum)],
                 headcount_enabled=True, score_balance=True, score_balance_mode="spread",
                 weights=only("score_spread", 1))
        sizes.append(build_model(p).stats["variables"])
    assert sizes[0] == sizes[1]


# ---- F11: seed range ------------------------------------------------------------------------------
def test_seed_outside_int32_is_a_value_error():
    p = mini(1, ["D"], [Nurse("a")])
    rm = build_model(p)
    with pytest.raises(ValueError, match="32-bit"):
        solve(rm, p, seed=2 ** 31)
    assert solve(rm, p, seed=-2 ** 31, time_limit=2).status == "OPTIMAL"


# ---- F12: eligibility is checked over the day types the month has ---------------------------
def test_precheck_ignores_a_day_type_outside_the_horizon():
    p = mini(1, ["D"], [Nurse("a")], headcounts=[Headcount("D", 1, 1, 2, 2)], headcount_enabled=True)
    assert not any("headcount needs more" in i for i in precheck(p))
    p = mini(1, ["D"], [Nurse("a")], headcounts=[Headcount("D", 1, 1, 2, 2)], headcount_enabled=True,
             first_weekday=4)                                 # the one day is a Friday
    assert any("headcount needs more" in i for i in precheck(p))
