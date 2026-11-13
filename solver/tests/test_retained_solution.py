"""Regression tests for the retained solution, the fingerprint checked on the snapshot, and a governed but unusable
shift in the precheck."""
from __future__ import annotations

import pytest

from nurse_roster.model import build_model, solve
from nurse_roster.precheck import precheck
from nurse_roster.problem import Headcount, Nurse
from nurse_roster.validate import validate
from tests.rules_helper import only
from tests.test_core import mini


def test_callback_cannot_corrupt_the_retained_solution():
    p = mini(1, ["D"], [Nurse("a")], headcount_enabled=True, headcounts=[Headcount("D", 1, 1, 1, 1)])

    def vandalise(s):
        s.objective = -1
        s.assignment[0][0] = -1
    sol = solve(build_model(p), p, time_limit=2, workers=1, on_solution=vandalise)
    assert sol.assignment == [[0]] and sol.objective == 0 and not sol.hard_errors
    assert validate(p, sol.assignment)[0] == []


def test_fingerprint_is_checked_on_the_snapshot(monkeypatch):
    """An edit slipped in between the check and the copy must not reach the scoring."""
    import copy as _copy
    import nurse_roster.model as M
    p = mini(1, ["D"], [Nurse("a")], headcount_enabled=True, headcounts=[Headcount("D", 1, 1, 1, 1)],
             weights=only("undesired_day", 2))
    rm = build_model(p)
    real = _copy.deepcopy

    def racing_copy(obj, *a, **k):
        if obj is p:
            p.nurses[0].undesired_days = [0]                 # the caller edits as the copy is taken
        return real(obj, *a, **k)
    monkeypatch.setattr(M.copy, "deepcopy", racing_copy)
    with pytest.raises(ValueError, match="build_model again"):   # the snapshot differs from the model
        solve(rm, p, time_limit=2, workers=1)


def test_governed_but_unusable_shift_is_not_reported_unbounded():
    p = mini(1, ["D", "N"], [Nurse("a", allowed_shifts=["D"])], headcount_enabled=True,
             headcounts=[Headcount("D", 1, 1, 1, 1), Headcount("N", 0, 0, 0, 0)])
    assert not any("no headcount names it" in i for i in precheck(p))
    p = mini(1, ["D", "N"], [Nurse("a")], headcount_enabled=True, headcounts=[Headcount("D", 1, 1, 1, 1)])
    assert any("N: no headcount names it" in i for i in precheck(p))
