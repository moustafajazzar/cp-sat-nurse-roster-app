"""Pinning, the published baseline and nondisruptive re-solves (Timefold: PlanningPin and
nondisruptive replanning)."""
from __future__ import annotations

import json

import pytest

from nurse_roster.__main__ import main
from nurse_roster.calendar import parse_month
from nurse_roster.export import write_csv
from nurse_roster.explain import explain
from tests.er_fixture import generate, request_of, write_request
from nurse_roster.model import build_model, solve
from nurse_roster.precheck import precheck
from nurse_roster.problem import Headcount, Nurse, Problem
from tests.rules_helper import only, penalises, violates
from tests.test_core import mini


def two(**kw) -> Problem:
    """Two D-only nurses, one D needed per day, four days."""
    return mini(4, ["D"], [Nurse("a"), Nurse("b")], headcounts=[Headcount("D", 1, 1, 1, 1)],
                headcount_enabled=True, **kw)


def test_pins_are_hard_and_checked():
    p = two()
    p.nurses[0].pinned = {0: "", 1: "D"}
    sol = solve(build_model(p), p, time_limit=5)
    assert sol.status == "OPTIMAL" and sol.assignment[0][:2] == [-1, 0]
    assert violates(p, [[0, 0, -1, -1], [-1, -1, 0, 0]], "pin") == ["pin: a day 0 is D, pinned off"]
    assert violates(p, sol.assignment, "pin") == []


@pytest.mark.parametrize("pinned,msg", [
    ({4: "D"}, "pinned day 4 outside"),
    ({0: "X"}, "not a department shift"),
    ({"0": "D"}, "pinned day '0' outside"),           # strings only come from JSON, where from_dict converts
])
def test_bad_pins_are_rejected(pinned, msg):
    p = two()
    p.nurses[0].pinned = pinned
    with pytest.raises(ValueError, match=msg):
        p.check()


def test_pin_against_allowed_shifts_and_leave():
    p = mini(2, ["D", "N"], [Nurse("a", allowed_shifts=["D"], unavailable=[1])])
    p.nurses[0].pinned = {0: "N"}
    with pytest.raises(ValueError, match="not in her allowed shifts"):
        p.check()
    p.nurses[0].pinned = {1: "D"}
    with pytest.raises(ValueError, match="on leave but pinned"):
        p.check()
    p.nurses[0].pinned = {1: ""}                      # pinned off on a leave day is fine
    p.check()


def test_precheck_flags_pins_that_contradict_each_other():
    p = mini(3, ["D", "N"], [Nurse("a")], no_mixed_blocks=True)
    p.nurses[0].pinned = {0: "N", 1: "D"}
    assert any("forbidden succession" in i for i in precheck(p))
    p = mini(2, ["D"], [Nurse("a"), Nurse("b")], headcounts=[Headcount("D", 2, 2, 2, 2)], headcount_enabled=True)
    p.nurses[0].pinned = {0: ""}
    assert any("day 0: D needs 2 but only 1" in i for i in precheck(p))


def test_published_baseline_counts_changes_except_pinned_cells():
    p = two(weights=only("change", 3))
    p.published = [["D", "", "D", ""], ["", "D", "", "D"]]
    swapped = [[-1, 0, -1, 0], [0, -1, 0, -1]]
    assert penalises(p, swapped, "change") == 8 * 3
    p.nurses[0].pinned = {0: ""}                      # a pinned cell is not a "change"
    assert penalises(p, swapped, "change") == 7 * 3
    m = explain(p, swapped).rules["change"].matches[0]
    assert (m.nurse, m.day, m.text) == ("a", 1, "a: day 1 off -> D")


def test_replan_moves_only_what_the_new_fact_forces():
    p = two(weights=only("change", 3))
    p.published = [["D", "", "D", ""], ["", "D", "", "D"]]
    p.nurses[0].unavailable = [2]                     # a falls sick on day 2
    p.replan_from(2)
    assert p.nurses[0].pinned == {0: "D", 1: ""} and p.nurses[1].pinned == {0: "", 1: "D"}
    sol = solve(build_model(p), p, time_limit=5)
    assert sol.status == "OPTIMAL"
    assert sol.assignment == [[0, -1, -1, -1], [-1, 0, 0, 0]]      # only day 2 moved
    assert sol.penalty_breakdown["change"] == 2 * 3                # a's and b's day 2
    assert sol.analysis.rules["change"].matches[0].text == "a: day 2 D -> off"


def test_replan_needs_a_baseline_of_the_right_shape_and_keeps_recorded_facts():
    p = two()
    with pytest.raises(ValueError, match="needs a published roster"):
        p.replan_from(1)
    p.published = [["D", "", "D"], ["", "D", "", "D"]]
    with pytest.raises(ValueError, match=r"published\[a\] must have 4 cells"):
        p.check()
    p.published = [["D", "", "D", ""], ["", "D", "", "D"]]
    p.nurses[0].pinned = {0: ""}                      # a recorded fact: a did not work day 0
    assert p.replan_from(1) == 1                      # kept, and counted as differing from the plan
    assert p.nurses[0].pinned == {0: ""} and p.nurses[1].pinned == {0: ""}


def test_pins_and_published_round_trip_through_json(tmp_path):
    p = two()
    p.nurses[0].pinned = {1: "D", 3: ""}
    p.published = [["D", "", "D", ""], ["", "D", "", "D"]]
    f = tmp_path / "p.json"
    p.to_json(str(f))
    assert json.loads(f.read_text())["nurses"][0]["pinned"] == {"1": "D", "3": ""}
    q = Problem.from_json(str(f))
    assert q.nurses[0].pinned == {1: "D", 3: ""} and q.published == p.published


def test_result_roster_feeds_the_replan_cli(tmp_path, capsys):
    p = generate(25, 30, 0, 2, False, [])
    sol = solve(build_model(p), p, time_limit=5)
    assert sol.assignment
    sh = p.department.shifts
    p.published = [[sh[v] if v >= 0 else "" for v in row] for row in sol.assignment]
    req = request_of(p, "2026-06", time_limit=5, replan_from=5)
    info = parse_month("2026-06")                              # the result's roster shape
    assert req["published"] == {n.id: {info.iso(d): c for d, c in enumerate(row)}
                                for n, row in zip(p.nurses, p.published)}
    f = tmp_path / "r.json"
    write_request(req, f)
    rc = main(["solve", str(f), "--explain"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "re-planning from 2026-06-06: 5 day(s) pinned" in out and "vs published: cost" in out
    del req["published"]
    write_request(req, f)
    assert main(["solve", str(f)]) == 4
    assert "needs a published roster" in capsys.readouterr().err
