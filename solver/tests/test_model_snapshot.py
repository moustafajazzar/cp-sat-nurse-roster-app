"""Regression tests for building and fingerprinting the model from one snapshot, and for a carried hours balance bounded
by the shift lengths of the month that produced it, not by the nurse's current eligibility."""
from __future__ import annotations

import copy

import pytest

import nurse_roster.model as model
from nurse_roster.model import build_model, solve
from nurse_roster.problem import MAX_HOURS, Nurse, Problem
from tests.rules_helper import only
from tests.test_core import hours_general, mini, quiet


# ---- 1: an input edited during the build cannot be certified by the fingerprint ------------------
def test_build_model_fingerprints_the_snapshot_it_was_built_from(monkeypatch):
    p = mini(1, ["D", "N"], [Nurse("a", allowed_shifts=["D"])], weights=only("change", 3))
    p.published = [["N"]]                                      # D-only, so the N is a change: cost 3
    frozen = copy.deepcopy(p)
    real = model.fingerprint

    def widened_meanwhile(q):                                  # another thread lifts the restriction
        p.nurses[0].allowed_shifts = ["D", "N"]                # between the constraints and the
        return real(q)                                         # fingerprint
    monkeypatch.setattr(model, "fingerprint", widened_meanwhile)
    rm = build_model(p)
    monkeypatch.setattr(model, "fingerprint", real)
    assert rm.fingerprint == model.fingerprint(frozen) != model.fingerprint(p)
    with pytest.raises(ValueError, match="differs from the one the model was built from"):
        solve(rm, p, time_limit=5)                             # used to be accepted: OPTIMAL at 3
    sol = solve(rm, frozen, time_limit=5)                      # the snapshot is what was built
    assert sol.status == "OPTIMAL" and sol.objective == 3 and sol.assignment[0][0] != 1
    assert solve(build_model(p), p, time_limit=5).objective == 0   # rebuilt for the edited input


def test_build_model_leaves_the_callers_problem_alone():
    p = mini(2, ["D"], [Nurse("a")])
    before = copy.deepcopy(p)
    rm = build_model(p)
    assert p == before and rm.fingerprint == model.fingerprint(p)


# ---- 2: a debt carried from a 12 h month survives a move to 8 h shifts ---------------------------
def month(nurse: Nurse) -> Problem:
    p = mini(31, ["D", "N", "C"], [nurse])                     # Mon 1st: four Fridays, 27 working days
    p.general = hours_general()                               # hours rule on
    return p


def test_carried_hours_survive_a_change_of_shift_eligibility():
    a = Nurse("a", breastfeeding=True, allowed_shifts=["D"])  # 27 x 7 h = 189 h owed, 12 h shifts
    p1 = month(a)
    assert p1.required_hours(a) == 189 and p1.max_shortfall(a) == 11
    assert p1.carry_over(a, 180) == 9                          # 15 D: a legal 9 h remainder
    b = Nurse("a", breastfeeding=True, allowed_shifts=["C"], hours_balance=9)   # now 8 h shifts
    p2 = month(b)
    p2.check()                                                 # used to be rejected (9 >= 8)
    assert p2.required_hours(b) == 198 and p2.max_shortfall(b) == 7   # the new remainder bound
    sol = solve(build_model(p2), p2, time_limit=10, workers=4)
    assert sol.status == "OPTIMAL" and sol.hard_errors == []
    worked = sum(8 for v in sol.assignment[0] if v >= 0)
    assert 198 - 7 <= worked <= 198 + p2.overtime_cap(b) and abs(p2.carry_over(b, worked)) < 8


@pytest.mark.parametrize("balance,ok", [(11, True), (-11, True), (12, True), (23, True), (-23, True),
                                        (24, True), (-24, True), (MAX_HOURS, True),
                                        (MAX_HOURS + 1, False), (-MAX_HOURS - 1, False)])
def test_hours_balance_bound_is_a_sanity_bound_only(balance, ok):
    p = month(Nurse("a", allowed_shifts=["C"], hours_balance=balance))   # C is 8 h, D/N are 12 h: no
    if ok:                                                 # shift length bounds the debt, and
        p.check()                                          # since a month whose recorded
    else:                                                  # days could not reach the requirement
        with pytest.raises(ValueError, match=f"hours_balance must be an integer within ±{MAX_HOURS}"):
            p.check()                                      # carries its whole debt,
                                                           # no shift length does either
