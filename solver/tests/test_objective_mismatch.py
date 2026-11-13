"""Regression test: `run()` reports a validated cost above the solver's objective as a model / validator mismatch
at any status, not only at OPTIMAL, since the solver's penalty variables are bounded from below
and can only overstate the cost (slack); a solver objective below it is impossible otherwise."""
from __future__ import annotations

import pytest

import nurse_roster.__main__ as cli
from nurse_roster.model import build_model, solve
from nurse_roster.request import load_request
from tests.er_fixture import request_of
from tests.test_optimal_mismatch import one_d


@pytest.mark.parametrize("status,cost,solver,rc", [
    ("FEASIBLE", 2, 2, 0),                 # agreement
    ("FEASIBLE", 2, 5, 0),                 # slack: the solver overstates a time-limit roster
    ("FEASIBLE", 5, 2, 3),                 # the validator charges more than the solver could have
    ("OPTIMAL", 5, 2, 3),                  #   seen: drift (used to exit 0 with a "slack" message)
    ("OPTIMAL", 2, 5, 3),                  # at OPTIMAL any gap is drift, as before
])
def test_cli_exit_3_when_the_solver_objective_is_below_the_validated_cost(monkeypatch, capsys,
                                                                         status, cost, solver, rc):
    req = load_request(request_of(one_d(), "2026-06"))         # the problem run() will solve (its
    p = req.problem                                            # month is part of the fingerprint)
    sol = solve(build_model(p), p, time_limit=5)                # a real roster of the problem,
    assert sol.status == "OPTIMAL" and sol.objective == 2      # then the scores run() will see
    sol.status, sol.objective, sol.solver_objective = status, float(cost), float(solver)
    def stub(*a, **k):                                         # the settings run() asked for
        sol.time_limit, sol.workers, sol.seed = k["time_limit"], k["workers"], k["seed"]
        return sol
    monkeypatch.setattr(cli, "solve", stub)
    assert cli.run(req, 1) == rc
    out = capsys.readouterr().out
    assert ("model and validator disagree" in out) == (rc == 3)
    if rc == 0:
        assert ("cost matches" if cost == solver else f"carries {solver - cost} of slack") in out
