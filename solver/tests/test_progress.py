"""Progress callback and cancel (Timefold: SolverManager.solve_and_listen / terminate_early)."""
from __future__ import annotations

import copy
import threading

import pytest

from nurse_roster.__main__ import main
from nurse_roster.problem import Headcount, Nurse
from tests.er_fixture import generate, request_of, write_request
from nurse_roster.model import build_model, solve
from nurse_roster.validate import validate
from tests.test_core import mini, quiet


def test_callback_gets_validated_improving_rosters():
    p = generate(25, 10, 0, 4, False, [])
    seen = []
    sol = solve(build_model(p), p, time_limit=5, on_solution=seen.append)
    assert sol.assignment and seen and sol.improvements == len(seen)
    for s in seen:
        assert s.status == "FEASIBLE" and not s.hard_errors
        assert s.objective == validate(p, s.assignment)[2] and s.analysis.total == s.objective
    assert seen[-1].objective >= sol.objective                # the final roster is the best


def test_stop_event_ends_the_search_at_the_next_incumbent():
    p = generate(25, 10, 0, 4, False, [])
    stop = threading.Event()
    seen = []

    def first(s):
        seen.append(s)
        stop.set()
    sol = solve(build_model(p), p, time_limit=30, on_solution=first, stop=stop)
    assert sol.status == "FEASIBLE" and sol.improvements == 1 and len(seen) == 1
    assert sol.solve_seconds < 10 and sol.assignment == seen[0].assignment
    with pytest.raises(ValueError, match="threading.Event"):
        solve(build_model(p), p, stop=object())


def test_cli_progress_lines(tmp_path, capsys):
    f = tmp_path / "r.json"
    p = generate(10, 30, 0, 4, False, [])
    p.department.headcounts = [Headcount("D", 2, 3, 2, 3), Headcount("N", 2, 3, 2, 3)]
    write_request(request_of(p, "2026-06", time_limit=5), f)
    rc = main(["solve", str(f), "--progress"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "  t=" in out and "improving rosters)" in out


def test_stopped_incumbent_survives_a_proof_of_optimality():
    """Ten nurses, D/N coverage 2..4, a run limit of 3 and no
    soft weight, so every feasible roster costs 0 and eight workers prove optimality at once.
    The callback stops at the first incumbent: that roster, not the solver's, is returned, as
    OPTIMAL, since it attains the proven optimum."""
    p = generate(10, 30, 0, 4, False, [])
    p.department.headcounts = [Headcount("D", 2, 4, 2, 4), Headcount("N", 2, 4, 2, 4)]
    p.general.weights = quiet()
    stop = threading.Event()
    seen = []

    def first(s):
        seen.append(s)
        stop.set()
    for _ in range(3):                                         # the race is timing-dependent
        stop.clear()
        seen.clear()
        sol = solve(build_model(p), p, time_limit=30, on_solution=first, stop=stop, workers=8)
        assert len(seen) == 1 and sol.improvements == 1 and sol.objective == 0
        assert sol.assignment == seen[0].assignment and sol.status in ("OPTIMAL", "FEASIBLE")
        assert sol.solver_objective == 0 and sol.stopped_by in ("proof", "cancelled")


def test_stopped_incumbent_survives_a_cheaper_proof():
    """With preference penalties the first incumbent costs more
    than the optimum eight workers prove meanwhile; the roster the callback stopped at is still
    the one returned, FEASIBLE under the proven bound (OPTIMAL only if it attains it), cause
    `cancelled`, and a result built from it says so."""
    p = generate(10, 30, 0, 4, False, [])
    p.department.headcounts = [Headcount("D", 2, 4, 2, 4), Headcount("N", 2, 4, 2, 4)]
    stop = threading.Event()
    seen = []

    def first(s):
        seen.append(s)
        stop.set()
    for _ in range(3):                                         # the race is timing-dependent
        stop.clear()
        seen.clear()
        sol = solve(build_model(p), p, time_limit=30, on_solution=first, stop=stop, workers=8)
        assert len(seen) == 1 and sol.improvements == 1 and sol.assignment == seen[0].assignment
        assert sol.objective == seen[0].objective and sol.stopped_by == "cancelled"
        assert sol.best_bound <= sol.objective + 0.5 and sol.hard_errors == []
        assert sol.status == ("OPTIMAL" if abs(sol.best_bound - sol.objective) < 0.5 else "FEASIBLE")
        assert sol.solver_objective >= sol.objective - 0.5    # never below the validated cost


def test_incumbent_selection_covers_every_branch():
    """The roster `solve` returns, with hand-built solutions.
    An incumbent validated below the proven optimum is model / validator disagreement and
    must reach exit 3 and ok=false, whatever the final roster's own numbers say."""
    from nurse_roster.model import _select
    from nurse_roster.request import load_request
    from nurse_roster.result import build_result, verdict

    req = load_request(request_of(mini(30, ["D"], [Nurse("a")]), "2026-06", time_limit=5))
    rm = build_model(req.problem)
    real = solve(rm, req.problem, **req.solver.solve_kwargs(2))
    assert real.status == "OPTIMAL" and real.mismatch == ""

    def sol(status, cost, solver_cost, bound):
        s = copy.deepcopy(real)
        s.status, s.objective, s.solver_objective, s.best_bound = status, float(cost), float(solver_cost), float(bound)
        return s
    # stopped, OPTIMAL, incumbent validated below the proof: the final roster, with the evidence
    out = _select("OPTIMAL", sol("OPTIMAL", 0, 3, 3), sol("FEASIBLE", 1, 9, 0), True, 1.0, 1)
    assert out.status == "OPTIMAL" and "an incumbent validated at 1 below the proven optimum 3" in out.mismatch
    assert verdict(out)[0] == 3 and not build_result(req, rm, out, precheck=[], workers=2)["ok"]
    # not stopped: the same disagreement is reported the same way
    out = _select("OPTIMAL", sol("OPTIMAL", 3, 3, 3), sol("FEASIBLE", 1, 9, 0), False, 1.0, 1)
    assert verdict(out) == (3, out.mismatch) and out.objective == 3
    # stopped, OPTIMAL, incumbent attains the proof: the incumbent, OPTIMAL, proven objective
    out = _select("OPTIMAL", sol("OPTIMAL", 3, 3, 3), sol("FEASIBLE", 3, 9, 0), True, 1.0, 1)
    assert (out.status, out.objective, out.solver_objective, out.best_bound, out.stopped_by) == ("OPTIMAL", 3, 3, 3, "cancelled")
    assert verdict(out) == (0, "") and build_result(req, rm, out, precheck=[], workers=2)["ok"]
    # stopped, OPTIMAL, incumbent above the proof: the incumbent, FEASIBLE under the proven bound
    out = _select("OPTIMAL", sol("OPTIMAL", 3, 3, 3), sol("FEASIBLE", 5, 7, 0), True, 1.0, 1)
    assert (out.status, out.objective, out.solver_objective, out.best_bound, out.stopped_by) == ("FEASIBLE", 5, 7, 3, "cancelled")
    res = build_result(req, rm, out, precheck=[], workers=2)
    assert verdict(out) == (0, "") and res["ok"] and res["solver"]["best_bound"] == 3 and not res["solver"]["deterministic"]
    # not stopped, OPTIMAL, incumbent above the proof: the final roster
    assert _select("OPTIMAL", sol("OPTIMAL", 3, 3, 3), sol("FEASIBLE", 5, 7, 0), False, 1.0, 1).objective == 3
    # a time limit: the cheaper validated incumbent replaces the final roster, the cause stays
    out = _select("FEASIBLE", sol("FEASIBLE", 4, 6, 1), sol("FEASIBLE", 2, 8, 0), False, 1.0, 2)
    assert (out.status, out.objective, out.best_bound, out.improvements, out.stopped_by) == ("FEASIBLE", 2, 1, 2, real.stopped_by)
    assert _select("FEASIBLE", sol("FEASIBLE", 2, 6, 1), sol("FEASIBLE", 4, 8, 0), False, 1.0, 2).objective == 2
    # a time limit after a stop: the stopped-at incumbent, cause cancelled
    out = _select("FEASIBLE", sol("FEASIBLE", 2, 6, 1), sol("FEASIBLE", 4, 8, 0), True, 1.0, 1)
    assert (out.status, out.objective, out.stopped_by) == ("FEASIBLE", 4, "cancelled")
    assert _select("FEASIBLE", sol("FEASIBLE", 2, 6, 1), None, True, 1.0, 0).objective == 2   # no incumbent


def test_disagreement_on_either_candidate_is_kept():
    """A final roster that itself disagrees must not vanish
    behind a clean stopped-at incumbent, an incumbent's own under-reporting must not be
    rewritten away, and a mismatch outranks hard errors in the verdict."""
    from nurse_roster.model import _select
    from nurse_roster.request import load_request
    from nurse_roster.result import build_result, verdict

    req = load_request(request_of(mini(30, ["D"], [Nurse("a")]), "2026-06", time_limit=5))
    rm = build_model(req.problem)
    real = solve(rm, req.problem, **req.solver.solve_kwargs(2))

    def sol(status, cost, solver_cost, bound):
        s = copy.deepcopy(real)
        s.status, s.objective, s.solver_objective, s.best_bound = status, float(cost), float(solver_cost), float(bound)
        return s
    out = _select("OPTIMAL", sol("OPTIMAL", 5, 3, 3), sol("FEASIBLE", 3, 3, 0), True, 1.0, 1)
    assert out.objective == 5 and "the final roster validated at 5 against the proven optimum 3" in out.mismatch
    assert verdict(out)[0] == 3 and not build_result(req, rm, out, precheck=[], workers=2)["ok"]
    out = _select("OPTIMAL", sol("OPTIMAL", 3, 3, 3), sol("FEASIBLE", 3, 1, 0), True, 1.0, 1)   # under-reported
    assert out.objective == 3 and "an incumbent validated at 3 above its solver objective 1" in out.mismatch
    out = _select("FEASIBLE", sol("FEASIBLE", 4, 2, 1), sol("FEASIBLE", 6, 8, 0), True, 1.0, 1)
    assert out.objective == 4 and "the final roster validated at 4 above its solver objective 2" in out.mismatch
    out = _select("OPTIMAL", sol("OPTIMAL", 0, 3, 3), sol("FEASIBLE", 1, 9, 0), True, 1.0, 1)
    assert "against the proven optimum 3" in out.mismatch and "below the proven optimum 3" in out.mismatch
    out.hard_errors = ["pin: a day 0 is off, pinned D"]
    rc, reason = verdict(out)
    assert rc == 3 and reason.startswith("model and validator disagree") and reason.endswith("(and the roster breaks 1 hard rule(s))")
    res = build_result(req, rm, out, precheck=[], workers=2)
    assert not res["ok"] and res["message"] == reason


def test_disagreement_is_gathered_without_a_callback():
    """No incumbent at all (the default path) still carries
    the final roster's own disagreement, and the verdict ranks it above hard errors."""
    from nurse_roster.model import _select
    from nurse_roster.request import load_request
    from nurse_roster.result import verdict

    req = load_request(request_of(mini(30, ["D"], [Nurse("a")]), "2026-06", time_limit=5))
    real = solve(build_model(req.problem), req.problem, **req.solver.solve_kwargs(2))
    s = copy.deepcopy(real)
    s.objective, s.solver_objective = 5.0, 0.0
    out = _select("OPTIMAL", s, None, False, 1.0, 0)
    assert "the final roster validated at 5 against the proven optimum 0" in out.mismatch
    s = copy.deepcopy(real)
    s.status, s.objective, s.solver_objective, s.hard_errors = "FEASIBLE", 5.0, 2.0, ["pin: a day 0 is off, pinned D"]
    assert _select("FEASIBLE", s, None, False, 1.0, 0).mismatch.endswith("validated at 5 above its solver objective 2")
    s.mismatch = ""                                            # the numbers alone, next to a hard error
    assert verdict(s) == (3, "the solver's objective is below the roster's cost; model and validator disagree "
                             "(and the roster breaks 1 hard rule(s))")
    s.objective = 2.0
    assert verdict(s) == (2, "the roster breaks 1 hard rule(s)")
