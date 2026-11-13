"""Regression tests for re-plans and cancellation: recorded facts win over the published plan, and a stop ends a search
that has no incumbent yet."""
from __future__ import annotations

import threading

from nurse_roster.__main__ import main
from tests.er_fixture import generate, request_of, write_request
from nurse_roster.model import STOP_POLL, build_model, solve
from nurse_roster.problem import Headcount, Nurse, Problem
from tests.rules_helper import only
from tests.test_core import mini


def dn(days: int = 4) -> Problem:
    """Two nurses, one D and one N needed per day, four days, only the change weight on."""
    return mini(days, ["D", "N"], [Nurse("a"), Nurse("b")],
                headcounts=[Headcount("D", 1, 1, 1, 1), Headcount("N", 1, 1, 1, 1)],
                headcount_enabled=True, weights=only("change", 3))


# ---- 1: recorded facts win over the published plan ----------------------------------------------
def test_replan_keeps_a_completed_swap():
    p = dn()
    p.published = [["D", "D", "D", "D"], ["N", "N", "N", "N"]]
    p.nurses[0].pinned, p.nurses[1].pinned = {1: "N"}, {1: "D"}      # a and b swapped day 1
    assert p.replan_from(2) == 2                                     # used to raise
    assert p.nurses[0].pinned == {0: "D", 1: "N"} and p.nurses[1].pinned == {0: "N", 1: "D"}
    sol = solve(build_model(p), p, time_limit=5)
    assert sol.status == "OPTIMAL" and sol.assignment == [[0, 1, 0, 0], [1, 0, 1, 1]]
    assert sol.penalty_breakdown["change"] == 0                      # a fact is not a change


def test_replan_treats_past_leave_as_off_and_keeps_the_cover():
    p = mini(4, ["D"], [Nurse("a", unavailable=[1]), Nurse("b", pinned={1: "D"})],
             headcounts=[Headcount("D", 1, 1, 1, 1)], headcount_enabled=True, weights=only("change", 3))
    p.published = [["D", "D", "", ""], ["", "", "D", "D"]]            # a fell sick on day 1, b covered
    assert p.replan_from(3) == 2                                     # used to raise "on leave but pinned"
    assert p.nurses[0].pinned == {0: "D", 1: "", 2: ""}
    assert p.nurses[1].pinned == {0: "", 1: "D", 2: "D"}
    sol = solve(build_model(p), p, time_limit=5)
    assert sol.status == "OPTIMAL" and sol.assignment == [[0, -1, -1, -1], [-1, 0, 0, 0]]
    assert sol.penalty_breakdown["change"] == 0


def test_replan_without_recorded_facts_follows_the_plan():
    p = dn()
    p.published = [["D", "D", "D", "D"], ["N", "N", "N", "N"]]
    assert p.replan_from(2) == 0
    assert p.nurses[0].pinned == {0: "D", 1: "D"} and p.nurses[1].pinned == {0: "N", 1: "N"}


def test_cli_reports_recorded_cells_that_differ_from_the_plan(tmp_path, capsys):
    p = dn(30)
    p.published = [["D"] * 30, ["N"] * 30]
    p.nurses[0].pinned, p.nurses[1].pinned = {1: "N"}, {1: "D"}
    f = str(tmp_path / "r.json")
    write_request(request_of(p, "2026-06", time_limit=2, replan_from=2), f)
    assert main(["solve", f]) == 0
    out = capsys.readouterr().out
    assert "re-planning from 2026-06-03: 2 day(s) pinned to the published roster, " \
           "2 recorded cell(s) differ from it" in out
    p.nurses[0].pinned, p.nurses[1].pinned = {}, {}
    write_request(request_of(p, "2026-06", time_limit=2, replan_from=2), f)
    assert main(["solve", f]) == 0
    out = capsys.readouterr().out
    assert "2 day(s) pinned to the published roster\n" in out and "recorded" not in out


# ---- 2: cancellation does not wait for an incumbent ----------------------------------------------
def test_preset_stop_returns_without_solving():
    p = generate(25, 10, 0, 4, False, [])
    stop = threading.Event()
    stop.set()
    sol = solve(build_model(p), p, time_limit=5, stop=stop)
    assert sol.status == "UNKNOWN" and not sol.assignment and sol.improvements == 0
    assert sol.solve_seconds == 0.0 and "cancelled" in sol.message


def test_stop_ends_a_search_that_has_no_incumbent():
    p = generate()                        # one worker finds no roster for this instance for ~40 s
    stop = threading.Event()              # (OR-Tools 9.15); a faster solver may find one before
    threading.Timer(0.5, stop.set).start()                           # the stop, so the outcome
    sol = solve(build_model(p), p, time_limit=20, workers=1, stop=stop)   # is asserted per case
    assert sol.solve_seconds < 0.5 + 20 * STOP_POLL                  # 1.5 s, not the time limit
    if sol.assignment:                                               # an incumbent came first
        assert sol.status == "FEASIBLE" and sol.improvements >= 1 and sol.hard_errors == []
    else:
        assert sol.status == "UNKNOWN" and sol.improvements == 0 and "cancelled" in sol.message


def test_stop_watcher_exits_with_the_solve_and_a_time_limit_is_still_reported_as_one():
    p = generate()
    before = threading.active_count()
    stop = threading.Event()                                         # never set
    sol = solve(build_model(p), p, time_limit=1, workers=1, stop=stop)
    assert threading.active_count() == before
    if sol.assignment:                                               # a faster solver: a roster,
        assert sol.status in ("FEASIBLE", "OPTIMAL") and "cancelled" not in sol.message   # not a
    else:                                                            # cancellation
        assert sol.status == "UNKNOWN" and sol.message == "no roster found within the time limit"
    p = generate(25, 10, 0, 4, False, [])
    sol = solve(build_model(p), p, time_limit=5, stop=stop)
    assert sol.assignment and threading.active_count() == before
