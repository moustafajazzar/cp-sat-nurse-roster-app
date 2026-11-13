"""Regression tests for the problem file's schema version, the precheck's capacity in a re-plan, the input-error exit
code, exporters taking path objects, the night-rest bound, the Friday share computed once, incumbents scored without
re-checking the contract, and the bounded `--workers` converter."""
from __future__ import annotations

import io
import json
from dataclasses import asdict

import pytest

from nurse_roster import fairness as F
import nurse_roster.__main__ as cli
from nurse_roster.__main__ import EXIT_INPUT, main
from nurse_roster.export import write_csv
from tests.er_fixture import generate
from nurse_roster.model import MAX_WORKERS, build_model, solve
from nurse_roster.precheck import precheck
from nurse_roster.problem import MAX_REST_DAYS, SCHEMA_VERSION, Headcount, Nurse, Problem
from tests.test_core import mini
from tests.test_optimal_mismatch import one_d


def small() -> Problem:
    return generate(25, 10, 0, 4, False, [])


# ---- 2: problem.json carries a schema version; older files are refused, never reinterpreted ----
def test_version_is_written_and_checked(tmp_path):
    p, f = small(), tmp_path / "p.json"
    p.to_json(f)
    raw = json.loads(f.read_text())
    assert raw["version"] == SCHEMA_VERSION == 8 == p.version and Problem.from_json(f) == p
    assert Problem.from_dict(asdict(p)) == p                   # a dict built in code is current
    for bad in (1, 2, 3, 4, 5, 6, 7, SCHEMA_VERSION + 1, "8", 0, True):     # a prototype file (1, 2) relied on
        raw["version"] = bad                                   # defaults the code no longer has
        with pytest.raises(ValueError, match=f"version must be {SCHEMA_VERSION}"):
            Problem.from_dict(raw)
    del raw["version"]
    with pytest.raises(ValueError, match=r"problem: missing field\(s\) \['version'\]"):
        Problem.from_dict(raw)


# ---- 4: precheck capacity counts the free days of a re-plan, as its demand does ------------------
def test_precheck_capacity_counts_the_free_days_only():
    a = Nurse("a", team_leader=True, pinned={0: "D", 1: "D", 2: "D"})   # K=3: day 3 must be off
    b = Nurse("b", pinned={0: "", 1: "", 2: ""})
    p = mini(6, ["D"], [a, b], headcounts=[Headcount("D", 1, 1, 1, 1)], headcount_enabled=True,
             max_consecutive_shifts=3, require_team_leader=True)
    p.planning_from = 3
    issues = precheck(p)                                       # used to count a's whole month: 5
    assert any("team leaders: 3 anchor shifts need one each, but the 1 team leader(s) can work "
               "at most 2" in i for i in issues), issues
    assert not any(i.startswith("capacity:") for i in issues)  # a (2) + b (3) cover the 3 slots
    b.unavailable = [3, 4, 5]
    issues = precheck(p)
    assert any("capacity: headcount needs at least 3 shifts but staff can give at most 2" in i
               for i in issues), issues


# ---- 5: malformed input has its own exit code; a programming error keeps its traceback ---------
def test_input_errors_exit_4_and_bugs_are_not_swallowed(tmp_path, capsys, monkeypatch):
    assert main(["solve", str(tmp_path / "missing.json")]) == EXIT_INPUT == 4   # used to be 2, the
    assert "error:" in capsys.readouterr().err                                   # hard-violation code

    def bug(*a, **k):
        raise TypeError("a bug, not an input")
    monkeypatch.setattr(cli, "read_request", bug)
    with pytest.raises(TypeError, match="a bug"):
        main(["solve", str(tmp_path / "p.json")])


# ---- 6: the exporters take path objects, as Problem.to_json does -------------------------------
def test_exporters_accept_path_objects(tmp_path):
    p = one_d()
    sol = solve(build_model(p), p, time_limit=5)
    write_csv(p, sol.assignment, tmp_path / "r.csv")           # used to raise TypeError
    assert (tmp_path / "r.csv").read_text().startswith("nurse,TL,sex,score,")
    sol.analysis.to_json(tmp_path / "a.json")                  # used to raise AttributeError
    assert json.loads((tmp_path / "a.json").read_text())["total"] == sol.objective


# ---- 7: the rest rule's reach is bounded, since the clause encoding grows with its square -----------
def test_night_rest_days_are_bounded():
    mini(3, ["D", "N"], [Nurse("a")], night_rest_days=MAX_REST_DAYS).check()
    with pytest.raises(ValueError, match=f"night_rest_days must be an integer within 0..{MAX_REST_DAYS}"):
        mini(3, ["D", "N"], [Nurse("a")], night_rest_days=MAX_REST_DAYS + 1).check()


# ---- 8: the Friday share's per-problem part is computed once; balances once per CSV row ----------
def test_friday_share_precomputed_is_identical_and_balances_run_once_per_row(monkeypatch):
    p = small()
    p.nurses[0].unavailable = [4, 5, 6]                        # proration in play
    per, share = F.friday_slots_m(p), F.share_m(p)
    assert F.friday_expected_m(p) == F.friday_expected_m(p, per=per)
    sol = solve(build_model(p), p, time_limit=5)
    for nu, row in zip(p.nurses, sol.assignment):
        assert F.friday_expected_m(p, nu) == F.friday_expected_m(p, nu, per)
        assert F.balances_out(p, nu, row) == F.balances_out(p, nu, row, share=share, per=per)
    calls, real = [], F.balances_out
    monkeypatch.setattr(F, "balances_out", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    write_csv(p, sol.assignment, io.StringIO())
    assert len(calls) == len(p.nurses)                         # used to be twice per nurse


# ---- 9: an incumbent is scored without re-validating the problem contract -----------------------
def test_incumbents_are_not_re_checked(monkeypatch):
    p = small()
    rm = build_model(p)
    calls, real = [], Problem.check
    monkeypatch.setattr(Problem, "check", lambda self, **kw: (calls.append(1), real(self, **kw))[1])
    solve(rm, p, time_limit=5, workers=4)
    plain = len(calls)
    calls.clear()
    sol = solve(rm, p, time_limit=5, workers=4, on_solution=lambda s: None)
    assert sol.improvements >= 1 and len(calls) == plain       # used to be plain + improvements


# ---- 10: --workers uses the shared bounded converter --------------------------------------------
def test_workers_uses_the_shared_bounded_converter(capsys):
    with pytest.raises(SystemExit):
        main(["solve", "p.json", "--workers", "0"])
    assert f"must be within 1..{MAX_WORKERS}" in capsys.readouterr().err
