"""Regression tests for a proven-optimal roster returned as is even when an earlier incumbent validated cheaper, since
that can only mean the model and the validator disagree, which `run()` must report."""
from __future__ import annotations

import threading

import pytest

import nurse_roster.model as model
from nurse_roster.__main__ import main
from nurse_roster.model import build_model, solve
from nurse_roster.problem import Headcount, Nurse, Problem
from tests.rules_helper import only
from tests.er_fixture import request_of, write_request
from tests.test_core import mini


def one_d() -> Problem:
    """One nurse, one D needed per day, June 2026, day 1 undesired: optimum 2, always found."""
    return mini(30, ["D"], [Nurse("a", undesired_days=[1])], headcounts=[Headcount("D", 1, 1, 1, 1)],
                headcount_enabled=True, weights=only("undesired_day", 2))


def one_d_request(tmp_path) -> str:
    f = str(tmp_path / "r.json")
    write_request(request_of(one_d(), "2026-06", time_limit=5), f)
    return f


@pytest.fixture
def drift(monkeypatch):
    """A model / validator disagreement: the validator charges the proven-optimal roster 10 more
    than the solver's objective, while every incumbent on the way keeps its true cost."""
    real = model._solution

    def charged(status, *args):
        sol = real(status, *args)
        if status == "OPTIMAL":
            sol.objective += 10
        return sol
    monkeypatch.setattr(model, "_solution", charged)


# ---- 1: an earlier incumbent must not hide an OPTIMAL mismatch ------------------------------------
def test_optimal_roster_is_kept_when_an_earlier_incumbent_validated_cheaper(drift):
    p = one_d()
    for extra in (dict(on_solution=lambda s: None), dict(stop=threading.Event())):   # both paths
        sol = solve(build_model(p), p, time_limit=5, **extra)                        # validate every
        assert sol.improvements >= 1                            # incumbent: the swap had one to pick
        assert sol.status == "OPTIMAL"                          # used to come back FEASIBLE at 2
        assert sol.objective == 12 and sol.solver_objective == 2   # the disagreement stays visible


def test_cli_reports_the_mismatch_with_progress_on(tmp_path, capsys, drift):
    f = one_d_request(tmp_path)
    assert main(["solve", f, "--progress"]) == 3                  # used to exit 0 with "slack"
    assert "model and validator disagree" in capsys.readouterr().out
    assert main(["solve", f]) == 3                                # as it always did without it


def test_cli_progress_still_exits_0_without_drift(tmp_path, capsys):
    f = one_d_request(tmp_path)
    assert main(["solve", f, "--progress"]) == 0
    assert "cost matches the solver objective" in capsys.readouterr().out
