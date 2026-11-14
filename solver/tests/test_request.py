"""The request / result contract: dates in, dates out, everything explicit."""
from __future__ import annotations

import csv
import dataclasses
import io
import json
import copy
import threading
from dataclasses import asdict
from pathlib import Path

import pytest

from nurse_roster import fairness as F
from nurse_roster.__main__ import main
import nurse_roster.__main__ as cli
from nurse_roster.calendar import MonthInfo, month_info, parse_date, parse_month
from nurse_roster.explain import explain
from nurse_roster.model import Solution, build_model, solve, stop_cause
from nurse_roster.precheck import needed_history, precheck
from nurse_roster.problem import MAX_HISTORY, Headcount, Nurse, Problem
from nurse_roster.request import REPRODUCIBLE_WALL_FACTOR, Request, SolverSettings, load_request, read_request
from nurse_roster.result import build_result, verdict, write_result
from nurse_roster.validate import rest_owed_after, switch_memory, validate
from tests.er_fixture import ER_SCORES, er_department, er_general, er_request, generate, request_of
from tests.rules_helper import only
from tests.test_core import hours_general, mini, quiet

FIXTURE = Path(__file__).parent / "fixtures" / "er_request_2026-03.json"


def fixture() -> dict:
    return json.loads(FIXTURE.read_text())


# ---- 1: calendar ----------------------------------------------------------------------------------
def test_parse_month_is_strict():
    info = parse_month("2026-03")
    assert (info.year, info.month, info.days, info.first_weekday) == (2026, 3, 31, 6) and info.label == "2026-03"
    for bad in ("2026-3", "2026-03-01", 202603, None, "2026/03", " 2026-03"):
        with pytest.raises(ValueError, match="month must be YYYY-MM"):
            parse_month(bad)
    with pytest.raises(ValueError, match="month must be 1..12, got 13"):
        parse_month("2026-13")


def test_dates_round_trip_and_name_their_field():
    for y, m in ((2026, 2), (2026, 3)):
        info = month_info(y, m)
        assert info.dates() == [f"{y}-{m:02d}-{d + 1:02d}" for d in range(info.days)]
        for d in range(info.days):
            assert info.index(info.iso(d), "x") == d and info.date(d).isoformat() == info.iso(d)
    info = month_info(2026, 2)
    with pytest.raises(ValueError, match=r"nurses\[0\]\.leave\[1\]: 2026-03-01 is outside 2026-02"):
        info.index("2026-03-01", "nurses[0].leave[1]")
    with pytest.raises(ValueError, match="2026-02-29 is not a calendar date"):
        info.index("2026-02-29", "x")
    with pytest.raises(ValueError, match=r"x: '2026-2-1' is not a date \(YYYY-MM-DD\)"):
        info.index("2026-2-1", "x")
    with pytest.raises(ValueError, match="is not a date"):
        parse_date(20260201, "x")
    with pytest.raises(ValueError, match="day index 31 outside 0..27"):
        info.iso(31)


# ---- 2: loader ---------------------------------------------------------------------------------------
def test_er_fixture_loads_and_is_the_er_configuration():
    raw = fixture()
    assert raw["version"] == 1                                # a stored input: read as version 2, no skills
    assert [n["score"] for n in raw["nurses"]] == ER_SCORES and len(raw["nurses"]) == 25
    req = read_request(FIXTURE)
    p = req.problem
    assert p.general == er_general() and p.department == er_department()
    assert p.days == 31 and p.first_weekday == 6 and p.department.name == "ER"
    assert p.ramadan_days == list(range(19))                  # the range object, cut to the month
    assert req.replan_from is None and req.differ == 0 and p.published is None
    assert req.solver == SolverSettings(5.0, False)
    assert all(len(n.previous) == 10 for n in p.nurses)


def test_ramadan_has_three_states_and_a_range():
    raw = fixture()
    del raw["ramadan_days"]
    assert load_request(raw).problem.ramadan_days == list(range(19))     # derived (Umm al-Qura)
    raw["ramadan_days"] = None
    assert load_request(raw).problem.ramadan_days == list(range(19))
    raw["ramadan_days"] = []
    assert load_request(raw).problem.ramadan_days == []                  # none
    raw["ramadan_days"] = ["2026-03-02", "2026-03-20"]
    assert load_request(raw).problem.ramadan_days == [1, 19]             # exactly these
    raw["ramadan_days"] = {"from": "2026-03-30", "to": "2026-04-28"}
    assert load_request(raw).problem.ramadan_days == [29, 30]
    for bad, text in ((["2026-03-02", "2026-03-02"], "request: ramadan_days: 2026-03-02 is listed twice"),
                      ({"from": "2026-03-20", "to": "2026-03-02"}, "request: ramadan_days: from 2026-03-20 is after to 2026-03-02"),
                      ({"from": "2026-04-01", "to": "2026-04-28"}, "request: ramadan_days: no day of 2026-03 lies within"),
                      ({"from": "2026-03-01"}, r"request: ramadan_days: missing field\(s\) \['to'\]"),
                      ({"from": "2026-03-01", "to": "x"}, r"request: ramadan_days.to: 'x' is not a date"),
                      ("2026-03-02", "request: ramadan_days: must be a list")):
        raw["ramadan_days"] = bad
        with pytest.raises(ValueError, match=text):
            load_request(raw)


def test_dates_map_to_indices_and_lists_default_to_empty():
    raw = fixture()
    raw["public_holidays"] = ["2026-03-10", "2026-03-01"]
    n = raw["nurses"][0]
    n.update(leave=["2026-03-05"], undesired_days=["2026-03-06"], desired_days=["2026-03-07"],
             pinned={"2026-03-08": "D", "2026-03-09": ""}, allowed_shifts=["D"], previous=["D", ""])
    for k in ("leave", "undesired_days", "desired_days", "pinned", "allowed_shifts", "previous"):
        raw["nurses"][1].pop(k, None)
    p = load_request(raw).problem
    assert p.public_holidays == [9, 0]
    a, b = p.nurses[0], p.nurses[1]
    assert (a.unavailable, a.undesired_days, a.desired_days) == ([4], [5], [6])
    assert a.pinned == {7: "D", 8: ""} and a.allowed_shifts == ["D"] and a.previous == ["D", ""]
    assert (b.unavailable, b.undesired_days, b.desired_days, b.pinned, b.allowed_shifts, b.previous) \
        == ([], [], [], {}, None, [])


def _with(raw: dict, path: list, value) -> dict:
    out = copy.deepcopy(raw)
    node = out
    for key in path[:-1]:
        node = node[key]
    if value is ...:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return out


@pytest.mark.parametrize("path, value, text", [
    (["extra"], 1, r"request: unknown field\(s\) \['extra'\]"),
    (["version"], ..., r"request: missing field\(s\) \['version'\]"),
    (["general"], ..., r"request: missing field\(s\) \['general'\]"),
    (["solver"], ..., r"request: missing field\(s\) \['solver'\]"),
    (["version"], 3, "request: version must be 1 or 2, got 3"),
    (["version"], True, "request: version must be 1 or 2"),
    (["month"], "2026-3", "request: month must be YYYY-MM"),
    (["public_holidays"], ["2026-04-01"], r"request: public_holidays\[0\]: 2026-04-01 is outside 2026-03"),
    (["public_holidays"], ["2026-03-01", "2026-03-01"], "request: public_holidays: 2026-03-01 is listed twice"),
    (["nurses", 0, "leave"], ["2026-02-28"], r"request: nurses\[0\]\.leave\[0\]: 2026-02-28 is outside 2026-03"),
    (["nurses", 0, "undesired_days"], ["x"], r"request: nurses\[0\]\.undesired_days\[0\]: 'x' is not a date"),
    (["nurses", 0, "desired_days"], ["2026-03-01", "2026-03-01"], r"nurses\[0\]\.desired_days: 2026-03-01 is listed twice"),
    (["nurses", 0, "pinned"], {"2026-03-32": "D"}, r"request: nurses\[0\]\.pinned: 2026-03-32 is not a calendar date"),
    (["nurses", 0, "pinned"], {"2026-03-02": "X"}, r"request: nurses\[0\]\.pinned\[2026-03-02\]: 'X' is not a department shift"),
    (["nurses", 0, "pinned"], {"2026-03-02": 1}, r"request: nurses\[0\]\.pinned\[2026-03-02\]: 1 is not a department shift"),
    (["nurses", 0, "previous"], [1], r"request: nurses\[0\]\.previous\[0\]: 1 is not a shift name or \"\""),
    (["nurses", 0, "previous"], ["=D"], "request: N01: history entry '=D' is not a shift name"),
    (["renamed_shifts"], {"X": "Q"}, r"request: renamed_shifts\[X\]: 'Q' is not a department shift"),
    (["renamed_shifts"], {"D": "N"}, "request: renamed_shifts: 'D' is still a department shift"),
    (["renamed_shifts"], ["N"], "request: renamed_shifts: must be an object"),
    (["renamed_shifts"], {"": "D"}, "request: renamed_shifts: '' is not a shift name"),
    (["nurses", 0, "allowed_shifts"], [""], r"request: nurses\[0\]\.allowed_shifts\[0\]: '' is not a department shift"),
    (["nurses", 0, "allowed_shifts"], "D", r"request: nurses\[0\]\.allowed_shifts: must be a list"),
    (["nurses", 0, "last_shift_kind"], "N", r"request: nurses\[0\]\.last_shift_kind: must be \"night\", \"day\" or null, got 'N'"),
    (["nurses", 0, "last_shift_kind"], "", r"request: nurses\[0\]\.last_shift_kind: must be"),
    (["nurses", 0, "last_shift_kind"], ..., r"request: nurses\[0\]: missing field\(s\) \['last_shift_kind'\]"),
    (["nurses", 0, "rest_owed"], -1, r"request: nurses\[0\]\.rest_owed: must be a whole number of days, 0 or more, got -1"),
    (["nurses", 0, "rest_owed"], True, r"request: nurses\[0\]\.rest_owed: must be a whole number of days"),
    (["nurses", 0, "rest_owed"], 1.0, r"request: nurses\[0\]\.rest_owed: must be a whole number of days"),
    (["nurses", 0, "rest_owed"], 8, "request: N01: rest_owed must be an integer within 0..7"),
    (["nurses", 0, "preferences_not_kept"], -1,
     r"request: nurses\[0\]\.preferences_not_kept: must be a whole number, 0 or more, got -1"),
    (["nurses", 0, "preferences_not_kept"], True, r"request: nurses\[0\]\.preferences_not_kept: must be a whole number"),
    (["nurses", 0, "preferences_not_kept"], 1.0, r"request: nurses\[0\]\.preferences_not_kept: must be a whole number"),
    (["nurses", 0, "preferences_not_kept"], "1", r"request: nurses\[0\]\.preferences_not_kept: must be a whole number"),
    (["nurses", 0, "preferences_not_kept"], None, r"request: nurses\[0\]\.preferences_not_kept: must be a whole number"),
    (["nurses", 0, "preferences_not_kept"], 63, "request: N01: preferences_not_kept must be an integer within 0..62"),
    (["nurses", 0, "unavailable"], [], r"request: nurses\[0\]: unknown field\(s\) \['unavailable'\]"),
    (["nurses", 0, "score"], ..., r"request: nurses\[0\]: missing field\(s\) \['score'\]"),
    (["nurses", 0, "id"], 7, r"request: nurses\[0\]\.id: must be a non-empty string, got 7"),
    (["nurses", 0, "gender"], "X", "request: N01: gender must be 'M' or 'F'"),
    (["nurses"], {}, "request: nurses: must be a list"),
    (["solver", "time_limit_seconds"], ..., r"request: solver: missing field\(s\) \['time_limit_seconds'\]"),
    (["solver", "time_limit_seconds"], 0, "request: solver.time_limit_seconds: must be a positive number"),
    (["solver", "time_limit_seconds"], True, "request: solver.time_limit_seconds: must be a positive number"),
    (["solver", "reproducible"], "no", "request: solver.reproducible: must be true or false, got 'no'"),
    (["solver", "workers"], 4, r"request: solver: unknown field\(s\) \['workers'\]"),
    (["general", "weights", "change"], ..., r"request: general.weights: missing field\(s\) \['change'\]"),
    (["general", "hours_per_day"], 30, "request: general.hours_per_day must be an integer within 0..24"),
    (["department", "foo"], 1, r"request: department: unknown field\(s\) \['foo'\]"),
    (["department", "headcounts", 0, "weekend_max"], ..., r"request: department.headcounts\[0\]: missing field\(s\) \['weekend_max'\]"),
    (["department", "fair_shifts"], ["D", "D"], "request: department.fair_shifts must not repeat"),
    (["replan_from"], "2026-03-12", "request: replan_from needs a published roster"),
    (["replan_from"], "2026-04-02", "request: replan_from: 2026-04-02 is outside 2026-03"),   # 04-01 = all elapsed
])
def test_rejections_name_the_field(path, value, text):
    with pytest.raises(ValueError, match=text):
        load_request(_with(fixture(), path, value))


def test_cross_field_nurse_errors_name_the_date():
    raw = fixture()
    raw["nurses"][0].update(leave=["2026-03-05"], pinned={"2026-03-05": "D"})
    with pytest.raises(ValueError, match=r"request: nurses\[0\]\.pinned\[2026-03-05\]: on leave but pinned to 'D'"):
        load_request(raw)
    raw = fixture()
    raw["nurses"][0].update(undesired_days=["2026-03-05"], desired_days=["2026-03-05"])
    with pytest.raises(ValueError, match=r"request: nurses\[0\]: 2026-03-05 is both desired and undesired"):
        load_request(raw)
    raw = fixture()                                            # a Problem.check message: its day index
    raw["nurses"][0].update(allowed_shifts=["N"], pinned={"2026-03-05": "D"})   # becomes the date
    with pytest.raises(ValueError, match="request: N01: pinned shift 'D' on 2026-03-05 is not in her allowed shifts"):
        load_request(raw)


def published_of(p: Problem, month: str, rows: list[list[str]]) -> dict:
    info = parse_month(month)
    return {n.id: {info.iso(d): c for d, c in enumerate(row)} for n, row in zip(p.nurses, rows)}


def test_published_rows_must_be_complete_and_known():
    p = mini(30, ["D", "N"], [Nurse("a"), Nurse("b")])
    rows = [["D"] * 30, ["N"] * 15 + [""] * 15]
    raw = request_of(p, "2026-06")
    raw["published"] = published_of(p, "2026-06", rows)
    assert load_request(raw).problem.published == rows
    cases = [
        (lambda r: r["published"].pop("b"), "request: published: no roster for nurse 'b'"),
        (lambda r: r["published"].__setitem__("c", {}), "request: published: unknown nurse 'c'"),
        (lambda r: r["published"]["a"].pop("2026-06-30"), r"request: published\[a\]: missing date 2026-06-30"),
        (lambda r: r["published"]["a"].__setitem__("2026-07-01", ""), r"request: published\[a\]: 2026-07-01 is outside 2026-06"),
        (lambda r: r["published"]["a"].__setitem__("2026-06-01", "X"), r"request: published\[a\]\[2026-06-01\]: 'X' is not a department shift"),
        (lambda r: r.__setitem__("published", []), r"request: published: must be an object"),
        (lambda r: r["published"].__setitem__("a", []), r"request: published\[a\]: must be an object"),
    ]
    for mutate, text in cases:
        bad = copy.deepcopy(raw)
        mutate(bad)
        with pytest.raises(ValueError, match=text):
            load_request(bad)


def test_replan_from_pins_the_past_and_keeps_the_deferral():
    p = mini(30, ["D", "N"], [Nurse("a"), Nurse("b", allowed_shifts=["N"])])
    p.nurses[0].unavailable = [1]
    p.nurses[1].pinned = {0: "D"}                              # an elapsed D for an N-only nurse: a fact
    rows = [["D", "D", "", "D"] + [""] * 26, ["N", "N", "N", ""] + [""] * 26]
    raw = request_of(p, "2026-06", replan_from=3)
    raw["published"] = published_of(p, "2026-06", rows)
    req = load_request(raw)
    q = req.problem
    assert req.replan_from == 3 and q.planning_from == 3 and q.published == rows
    assert q.nurses[0].pinned == {0: "D", 1: "", 2: ""} and q.nurses[1].pinned == {0: "D", 1: "N", 2: "N"}
    assert req.differ == 2                                     # a's leave on day 1, b's recorded D
    del raw["replan_from"]                                     # without the cutoff the pin is an error
    with pytest.raises(ValueError, match="request: b: pinned shift 'D' on 2026-06-01 is not in her allowed shifts"):
        load_request(raw)


# ---- 3: result --------------------------------------------------------------------------------------
def solved(p: Problem, month: str, **kw) -> tuple[Request, dict]:
    req = load_request(request_of(p, month, **kw))
    rm = build_model(req.problem)
    sol = solve(rm, req.problem, **req.solver.solve_kwargs(4))
    return req, build_result(req, rm, sol, precheck=precheck(req.problem), workers=4)


def test_result_roster_and_carry_over_round_trip():
    p = mini(30, ["D", "N"], [Nurse("a", previous=["N", "N", "", ""], unavailable=[2], ytd_overtime=5,
                                    night_balance=0.5, friday_balance=-1.0),
                              Nurse("b", previous=["D"], hours_balance=4)],
             headcounts=[Headcount("D", 1, 1, 1, 1), Headcount("N", 0, 1, 0, 1)], headcount_enabled=True,
             fair_shifts=["D", "N"], fair_weekends=True, max_consecutive_shifts=3, double_cycle_rest=2)
    p.general = hours_general(overtime_month_cap=40)           # hours rule on
    req, res = solved(p, "2026-06")
    q, info = req.problem, req.month
    assert res["ok"] and res["status"] in ("OPTIMAL", "FEASIBLE") and res["version"] == 1 and res["month"] == "2026-06"
    assert res["violations"] == [] and res["elapsed_violations"] == [] and res["replan"] is None
    assert list(res["roster"]) == ["a", "b"] and all(list(r) == info.dates() for r in res["roster"].values())
    assert all(c in ("D", "N", "") for r in res["roster"].values() for c in r.values())
    assert res["roster"]["a"]["2026-06-03"] == "" and res["nurses"]["a"]["leave"] == ["2026-06-03"]
    assert res["fingerprint"] == build_model(q).fingerprint and len(res["fingerprint"]) == 64
    assert res["cost"] == sum(res["penalties"].values()) >= 0
    assert set(res["penalties"]) == {"night_rest", "shift_switch", "dept_overtime", "nurse_overtime",
                                     "nurse_shortfall", "avoidable_shortfall", "fair_day", "fair_night", "fair_weekend",
                                     "score_deficit", "score_spread", "excess_weekend", "undesired_day",
                                     "desired_day", "change", "skill_need", "excess_full_run",
                                     "overtime_above_share"}
    share, per = F.share_m(q), F.friday_slots_m(q)
    need = needed_history(q.department, q.general)
    assert need == 8
    sh = q.department.shifts
    for n, row_dates in zip(q.nurses, res["roster"].values()):
        row = [sh.index(c) if c else -1 for c in row_dates.values()]
        block, carry = res["nurses"][n.id], res["nurses"][n.id]["carry_over"]
        hrs = sum(q.general.shift_hours[sh[v]] for v in row if v >= 0)
        assert block["hours_worked"] == hrs and block["required"] == q.required_hours(n)
        assert block["contract"] == q.contract_hours(n) and block["leave_credit"] == q.leave_credit(n)
        assert block["overtime"] == max(0, hrs - q.required_hours(n)) and block["shortfall"] == max(0, q.required_hours(n) - hrs)
        assert block["shifts"] == {s: row.count(i) for i, s in enumerate(sh)}
        assert carry["hours_balance"] == q.carry_over(n, hrs)
        assert (carry["night_balance"], carry["friday_balance"]) == F.balances_out(q, n, row, share=share, per=per)
        assert carry["ytd_overtime"] == n.ytd_overtime + block["overtime"]
        assert carry["overtime_balance"] == n.overtime_balance == 0      # the overtime share off: handed on
        assert carry["previous"] == [sh[v] if v >= 0 else "" for v in row]   # the month: it has work
    assert sum(res["nurses"][n]["cost"] for n in res["nurses"]) + res["department_cost"] == res["cost"]
    # next month's request = this month's static nurse fields + carry_over, and it loads
    nxt = request_of(p, "2026-06")
    nxt["month"] = "2026-07"
    for n in nxt["nurses"]:
        n.update({k: v for k, v in res["nurses"][n["id"]]["carry_over"].items()})
        n.update(leave=[], undesired_days=[], desired_days=[], pinned={})
    nxt["ramadan_days"], nxt["public_holidays"] = [], []
    loaded = load_request(nxt).problem
    for n, m in zip(loaded.nurses, q.nurses):
        c = res["nurses"][m.id]["carry_over"]
        assert (n.hours_balance, n.night_balance, n.friday_balance, n.ytd_overtime, n.previous,
                n.last_shift_kind, n.rest_owed, n.preferences_not_kept) == \
            (c["hours_balance"], c["night_balance"], c["friday_balance"], c["ytd_overtime"], c["previous"],
             c["last_shift_kind"], c["rest_owed"], c["preferences_not_kept"])
    assert loaded.days == 31 and loaded.first_weekday == 2


def test_result_without_the_hours_rule_carries_nothing():
    p = mini(30, ["D"], [Nurse("a", ytd_overtime=7, hours_balance=3)])
    req, res = solved(p, "2026-06")
    c = res["nurses"]["a"]["carry_over"]
    assert c["hours_balance"] == 0 and c["ytd_overtime"] == 7 and res["nurses"]["a"]["overtime"] == 0
    assert len(c["previous"]) == 30 and needed_history(req.problem.department, req.problem.general) == 0


def test_result_without_a_roster():
    p = mini(30, ["D"], [Nurse("a", pinned={0: ""})], headcounts=[Headcount("D", 1, 1, 1, 1)], headcount_enabled=True)
    req, res = solved(p, "2026-06")
    assert not res["ok"] and res["status"] == "INFEASIBLE" and "no roster satisfies" in res["message"]
    assert res["roster"] is None and res["nurses"] is None and res["cost"] is None and res["penalties"] == {}
    assert res["violations"] == [] and any("only 0 eligible nurses" in i for i in res["precheck"])
    assert res["solver"]["objective"] is None and res["solver"]["workers"] == 4 and res["solver"]["model"]["variables"] > 0
    json.dumps(res)                                            # no NaN leaks into the JSON


def test_violations_and_replan_facts_carry_rule_nurse_and_date():
    p = mini(30, ["D"], [Nurse("a")], max_consecutive_shifts=2, headcounts=[Headcount("D", 0, 1, 0, 1)],
             headcount_enabled=True)
    rows = [["D", "D", "D", "D"] + [""] * 26]                 # the past broke the run limit
    raw = request_of(p, "2026-06", replan_from=4)
    raw["published"] = published_of(p, "2026-06", rows)
    req = load_request(raw)
    rm = build_model(req.problem)
    sol = solve(rm, req.problem, **req.solver.solve_kwargs(2))
    res = build_result(req, rm, sol, precheck=[], workers=2)
    assert res["ok"] and res["replan"] == {"from": "2026-06-05", "recorded_cells_differ": 0}
    assert res["violations"] == []
    kinds = {(v["rule"], v["nurse"]) for v in res["elapsed_violations"]}
    assert ("consecutive", "a") in kinds
    assert all(v["date"] is None or v["date"].startswith("2026-06-") for v in res["elapsed_violations"])
    assert [v["text"] for v in res["elapsed_violations"]] == sol.analysis.elapsed
    assert verdict(sol) == (0, "")


def test_write_result_takes_a_path_or_a_stream(tmp_path):
    p = mini(30, ["D"], [Nurse("a")])
    _, res = solved(p, "2026-06")
    write_result(res, tmp_path / "r.json")
    buf = io.StringIO()
    write_result(res, buf)
    assert json.loads((tmp_path / "r.json").read_text()) == json.loads(buf.getvalue()) == json.loads(json.dumps(res))


# ---- 4: solver settings --------------------------------------------------------------------------
def test_solve_kwargs_for_both_modes():
    assert SolverSettings(20.0, False).solve_kwargs(8) == dict(time_limit=20.0, workers=8, seed=0)
    assert SolverSettings(20.0, True).solve_kwargs(8) == dict(time_limit=REPRODUCIBLE_WALL_FACTOR * 20.0,
                                                               workers=1, seed=0, deterministic_time=20.0)
    with pytest.raises(ValueError, match="deterministic_time must be a positive number"):
        p = mini(1, ["D"], [Nurse("a")])
        solve(build_model(p), p, deterministic_time=0)


def test_reproducible_solves_give_the_same_roster():
    p = generate(10, 30, 0, 4, False, [])
    p.department.headcounts = [Headcount("D", 2, 3, 2, 3), Headcount("N", 2, 3, 2, 3)]
    req = load_request(request_of(p, "2026-06", time_limit=1, reproducible=True))   # ~5 s of wall each
    runs = []
    for _ in range(2):
        rm = build_model(req.problem)
        sol = solve(rm, req.problem, **req.solver.solve_kwargs(8))
        runs.append(build_result(req, rm, sol, precheck=[], workers=8))
    a, b = runs
    assert a["ok"] and a["roster"] == b["roster"] and a["cost"] == b["cost"]
    assert a["solver"]["workers"] == 1 and a["solver"]["reproducible"] and a["solver"]["deterministic"]
    assert a["solver"]["stopped_by"] == "deterministic_budget" and a["solver"]["deterministic_time"] >= 1.0


# ---- 5: the CLI end to end ---------------------------------------------------------------------
def test_cli_solves_the_er_fixture_and_writes_result_and_csv(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("NURSE_ROSTER_WORKERS", "2")
    r, c = tmp_path / "result.json", tmp_path / "roster.csv"
    assert main(["solve", str(FIXTURE), "--out", str(r), "--csv", str(c), "--workers", "4"]) == 0
    out = capsys.readouterr().out
    assert "validator: all hard rules satisfied" in out and f"wrote {r}" in out and f"wrote {c}" in out
    res = json.loads(r.read_text())
    assert res["ok"] and res["status"] in ("OPTIMAL", "FEASIBLE") and res["violations"] == []
    assert len(res["roster"]) == 25 and all(len(row) == 31 for row in res["roster"].values())
    assert res["solver"]["workers"] == 4 and res["solver"]["model"]["variables"] > 1000
    # N05's preferences were not all kept in February: each of hers counts double, and every nurse carries a count
    assert read_request(FIXTURE).problem.nurses[4].preferences_not_kept == 1
    assert all(isinstance(n["carry_over"]["preferences_not_kept"], int) for n in res["nurses"].values())
    an = explain(read_request(FIXTURE).problem, [[-1] * 31 if nid == "N05" else [0] * 31 for nid in res["roster"]])
    assert [m.text for m in an.rules["desired_day"].matches if m.nurse == "N05"] == \
        ["N05: off on 2026-03-16 (asked to work; counts double, a preference was not kept last month)"]
    header = c.read_text().splitlines()[0].split(",")
    assert header[:4] == ["nurse", "TL", "sex", "score"] and header[14:45] == [f"{d + 1}{w}" for d, w in
                                                                              zip(range(31), ("Su Mo Tu We Th Fr Sa " * 5).split())]
    small = tmp_path / "small.json"                          # the environment's worker count
    small.write_text(json.dumps(request_of(mini(30, ["D"], [Nurse("a")]), "2026-06", time_limit=2)))
    assert main(["solve", str(small), "--out", str(r)]) == 0
    assert json.loads(r.read_text())["solver"]["workers"] == 2


def test_cli_malformed_request_exits_4_without_a_result(tmp_path, capsys):
    raw = fixture()
    del raw["general"]["weights"]["change"]
    f, r = tmp_path / "r.json", tmp_path / "result.json"
    f.write_text(json.dumps(raw))
    assert main(["solve", str(f), "--out", str(r)]) == 4
    assert "error: request: general.weights: missing field(s) ['change']" in capsys.readouterr().err
    assert not r.exists()


def test_cli_infeasible_request_exits_1_with_a_result(tmp_path, capsys):
    p = mini(30, ["D"], [Nurse("a", pinned={0: ""})], headcounts=[Headcount("D", 1, 1, 1, 1)], headcount_enabled=True)
    f, r = tmp_path / "r.json", tmp_path / "result.json"
    f.write_text(json.dumps(request_of(p, "2026-06", time_limit=2)))
    assert main(["solve", str(f), "--out", str(r)]) == 1
    out = capsys.readouterr().out
    assert "no solution found: no roster satisfies the hard rules" in out
    res = json.loads(r.read_text())
    assert not res["ok"] and res["status"] == "INFEASIBLE" and res["roster"] is None and res["precheck"]


def carried(p: Problem, this_month: str, res: dict, month: str) -> dict:
    """Next month's request: this month's static nurse fields plus the result's carry_over."""
    nxt = request_of(p, this_month)
    nxt["month"], nxt["ramadan_days"], nxt["public_holidays"] = month, [], []
    for n in nxt["nurses"]:
        n.update(res["nurses"][n["id"]]["carry_over"])
        n.update(leave=[], undesired_days=[], desired_days=[], pinned={})
    return nxt


def solved_raw(raw: dict, workers: int = 2) -> tuple[Request, dict]:
    req = load_request(raw)
    rm = build_model(req.problem)
    sol = solve(rm, req.problem, **req.solver.solve_kwargs(workers))
    return req, build_result(req, rm, sol, precheck=precheck(req.problem), workers=workers)


# ---- 6: carry-over memory, the solved snapshot, the cutoff and the stop cause --------------------
def test_carry_over_keeps_the_last_worked_shift_across_trailing_off_days():
    """A hard zero-switch cap: June ends D, off. The old tail (`needed_history` = 1 day) carried
    only the off day, so July accepted an N; the D must travel with it."""
    june = {d: "" for d in range(30)}
    june[28] = "D"
    a = Nurse("a", pinned=june)                                # worked once, then off
    b = Nurse("b", previous=["N", "", ""], unavailable=list(range(30)))   # no work this month
    c = Nurse("c", previous=[""] * 3, unavailable=list(range(30)))         # no work known at all
    d = Nurse("d", previous=["D"] + [""] * (MAX_HISTORY - 1), unavailable=list(range(30)))
    p = mini(30, ["D", "N"], [a, b, c, d], max_switches=0, switches_hard=True)
    req, res = solved(p, "2026-06")
    assert res["ok"] and needed_history(req.problem.department, req.problem.general) == 1
    prev = {n: res["nurses"][n]["carry_over"]["previous"] for n in "abcd"}
    last = {n: res["nurses"][n]["carry_over"]["last_shift_kind"] for n in "abcd"}
    assert prev["a"] == [""] * 28 + ["D", ""]                  # the month, with its last worked day
    assert prev == {**prev, "b": [""] * 30, "c": [""] * 30, "d": [""] * 30}   # the month, nothing more:
    assert last == {"a": None, "b": "night", "c": None, "d": "day"}   # the kind before it is carried,
    nxt = carried(p, "2026-06", res, "2026-07")                # from however far back (d: the cap)
    nxt["nurses"][0]["pinned"] = {"2026-07-01": "N"}          # a's N after her D: a switch
    nxt["nurses"][1]["pinned"] = {"2026-07-01": "N"}          # b's N after her N: none
    july = load_request(nxt).problem
    assert july.nurses[0].previous[-2:] == ["D", ""] and july.nurses[1].last_shift_kind == "night"
    assert [switch_memory(n, "N") for n in july.nurses] == [False, True, None, False]
    sol = solve(build_model(july), july, time_limit=5, workers=2)
    assert sol.status == "INFEASIBLE"
    nxt["nurses"][0]["pinned"] = {}
    july = load_request(nxt).problem
    assert solve(build_model(july), july, time_limit=5, workers=2).status == "OPTIMAL"


def test_result_is_serialized_from_the_solved_snapshot():
    """An app that edits the request while the solver runs must not corrupt the stored roster:
    the result comes from the snapshot the roster was scored against, and a problem that no
    longer matches the model is refused."""
    p = mini(30, ["D", "N"], [Nurse("a", pinned={0: "D"})])
    req = load_request(request_of(p, "2026-06"))
    rm = build_model(req.problem)
    sol = solve(rm, req.problem, time_limit=5, workers=2,
                on_solution=lambda s: req.problem.department.shifts.reverse())   # [D, N] -> [N, D]
    req.problem.department.shifts[:] = ["N", "D"]              # whether or not the callback fired
    assert sol.problem is not None and sol.problem.department.shifts == ["D", "N"]
    res = build_result(req, rm, sol, precheck=[], workers=2)
    assert res["roster"]["a"]["2026-06-01"] == "D" and res["fingerprint"] == rm.fingerprint
    assert res["nurses"]["a"]["shifts"]["D"] >= 1
    bare = Solution(sol.status, sol.objective, sol.best_bound, sol.solve_seconds, sol.assignment,
                    sol.penalty_breakdown, analysis=sol.analysis, time_limit=sol.time_limit,
                    workers=sol.workers, seed=sol.seed)        # no snapshot: the request must match
    with pytest.raises(ValueError, match="result: the problem differs from the one the model was built from"):
        build_result(req, rm, bare, precheck=[], workers=2)
    req.problem.department.shifts[:] = ["D", "N"]
    assert build_result(req, rm, bare, precheck=[], workers=2)["roster"] == res["roster"]


def test_cutoff_may_be_the_day_after_the_month(tmp_path, capsys):
    """Month-end reconciliation: the last day's shortage is a fact to record, not a plan to fix."""
    p = mini(30, ["D"], [Nurse("a", unavailable=[29])], headcounts=[Headcount("D", 1, 1, 1, 1)],
             headcount_enabled=True)
    rows = [["D"] * 29 + [""]]
    raw = request_of(p, "2026-06", time_limit=2, replan_from=29)
    raw["published"] = published_of(p, "2026-06", rows)
    req = load_request(raw)
    assert solve(build_model(req.problem), req.problem, time_limit=2).status == "INFEASIBLE"   # day 30 open
    raw["replan_from"] = "2026-07-01"
    req = load_request(raw)
    assert req.replan_from == 30 == req.problem.planning_from
    rm = build_model(req.problem)
    sol = solve(rm, req.problem, **req.solver.solve_kwargs(2))
    res = build_result(req, rm, sol, precheck=[], workers=2)
    assert res["ok"] and sol.status == "OPTIMAL" and list(res["roster"]["a"].values()) == rows[0]
    assert res["replan"] == {"from": "2026-07-01", "recorded_cells_differ": 0}
    assert [(v["rule"], v["date"]) for v in res["elapsed_violations"]] == [("headcount", "2026-06-30")]
    assert res["nurses"]["a"]["hours_worked"] == 29 * 12
    assert request_of(req.problem, "2026-06", replan_from=30)["replan_from"] == "2026-07-01"
    f = tmp_path / "r.json"
    f.write_text(json.dumps(raw))
    assert main(["solve", str(f)]) == 0
    out = capsys.readouterr().out
    assert "re-planning from 2026-07-01: 30 day(s) pinned" in out
    assert "recorded before 2026-07-01 (facts, not errors): 1 rule violation(s)" in out
    raw["replan_from"] = "2026-07-02"
    with pytest.raises(ValueError, match=r"request: replan_from: 2026-07-02 is outside 2026-06 \(the day after it, 2026-07-01"):
        load_request(raw)


def test_deterministic_is_reported_from_the_stop_cause():
    """A run the wall-clock guard or a cancellation ended is not reproducible, however short."""
    # The rule, on fixed numbers: the budget counts only when the wall clock is still under its limit.
    assert stop_cause("FEASIBLE", 0.004, 0.004, 0.039, 0.04, False) == "deterministic_budget"
    assert stop_cause("FEASIBLE", 0.004, 0.004, 0.04, 0.04, False) == "time_limit"   # both tripped: the wall's
    assert stop_cause("FEASIBLE", 0.001, 0.004, 0.04, 0.04, False) == "time_limit"
    assert stop_cause("FEASIBLE", 0.004, 0.004, 0.01, 0.04, True) == "cancelled"
    # A real run this short ends by either limit, by the machine's speed (both happen on GitHub's runners), so it claims
    # only that the result reports what ended it: deterministic only when the budget did.
    raw = fixture()
    raw["solver"] = {"time_limit_seconds": 0.004, "reproducible": True}
    req = load_request(raw)
    rm = build_model(req.problem)
    sol = solve(rm, req.problem, **req.solver.solve_kwargs(8))
    assert sol.stopped_by in ("time_limit", "deterministic_budget")
    res = build_result(req, rm, sol, precheck=[], workers=8)
    assert res["solver"]["reproducible"] and res["solver"]["stopped_by"] == sol.stopped_by
    assert res["solver"]["deterministic"] == (sol.stopped_by == "deterministic_budget")
    p = mini(30, ["D"], [Nurse("a")])                          # a proof
    req = load_request(request_of(p, "2026-06", time_limit=5, reproducible=True))
    rm = build_model(req.problem)
    sol = solve(rm, req.problem, **req.solver.solve_kwargs(8))
    assert sol.status == "OPTIMAL" and sol.stopped_by == "proof"
    assert build_result(req, rm, sol, precheck=[], workers=8)["solver"]["deterministic"]
    stop = threading.Event()                                    # a cancellation at the first incumbent
    p = generate(10, 30, 0, 4, False, [])
    p.department.headcounts = [Headcount("D", 2, 3, 2, 3), Headcount("N", 2, 3, 2, 3)]
    req = load_request(request_of(p, "2026-06", time_limit=30, reproducible=True))
    rm = build_model(req.problem)
    sol = solve(rm, req.problem, on_solution=lambda s: stop.set(), stop=stop, **req.solver.solve_kwargs(8))
    assert sol.status == "FEASIBLE" and sol.stopped_by == "cancelled"
    res = build_result(req, rm, sol, precheck=[], workers=8)
    assert res["ok"] and not res["solver"]["deterministic"] and res["solver"]["stopped_by"] == "cancelled"
    stop = threading.Event()
    stop.set()                                                  # cancelled before the search
    sol = solve(build_model(req.problem), req.problem, stop=stop)
    assert sol.stopped_by == "cancelled" and sol.problem is not None


# ---- 7: history memory, the result's binding, the month-end debt and the stop cause --------------
def test_switch_memory_survives_a_leave_longer_than_the_history_cap():
    """previous = D then 61 off days, then a month of leave: the exported history is the month
    (30 off days), and `last_shift_kind` carries its kind however far back it lies; July then refuses
    an N under a hard zero-switch cap, and so does August after another month of leave."""
    d = Nurse("d", previous=["D"] + [""] * (MAX_HISTORY - 1), unavailable=list(range(30)))
    p = mini(30, ["D", "N"], [d], max_switches=0, switches_hard=True)
    req, res = solved(p, "2026-06")
    c = res["nurses"]["d"]["carry_over"]
    assert res["ok"] and c["previous"] == [""] * 30 and c["last_shift_kind"] == "day" and c["rest_owed"] == 0
    july = carried(p, "2026-06", res, "2026-07")
    july["nurses"][0]["pinned"] = {"2026-07-01": "N"}
    jp = load_request(july).problem
    assert jp.nurses[0].last_shift_kind == "day" and switch_memory(jp.nurses[0], "N") is False
    assert solve(build_model(jp), jp, time_limit=5, workers=2).status == "INFEASIBLE"
    assert validate(jp, [[1] + [-1] * 30])[0] == ["switches: d changes night/day 1 times, max 0"]
    july["nurses"][0]["pinned"] = {"2026-07-01": "D"}        # D after D: no switch
    jp = load_request(july).problem
    assert solve(build_model(jp), jp, time_limit=5, workers=2).status == "OPTIMAL"
    july["nurses"][0]["pinned"], july["nurses"][0]["leave"] = {}, [f"2026-07-{x:02d}" for x in range(1, 32)]
    req2, res2 = solved_raw(july)                              # a second month of leave
    assert res2["ok"] and res2["nurses"]["d"]["carry_over"]["last_shift_kind"] == "day"
    assert res2["nurses"]["d"]["carry_over"]["previous"] == [""] * 31
    aug = carried(req2.problem, "2026-07", res2, "2026-08")
    aug["nurses"][0]["pinned"] = {"2026-08-01": "N"}
    ap = load_request(aug).problem
    assert solve(build_model(ap), ap, time_limit=5, workers=2).status == "INFEASIBLE"


def test_night_rest_owed_survives_a_month_of_other_work():
    """D/N/C, no run limit, hard two-day night rest: an N followed by a month of C has paid no
    rest back. With the whole 31-day history July refuses a D on its first day; the export
    carries that as `rest_owed` = 2 with a history of the month only, and July agrees."""
    a = Nurse("a", previous=["N"], pinned={x: "C" for x in range(30)})
    p = mini(30, ["D", "N", "C"], [a], night_rest_days=2, night_rest_hard=True, max_consecutive_shifts=0)
    req, res = solved(p, "2026-06")
    c = res["nurses"]["a"]["carry_over"]
    assert res["ok"] and c["previous"] == ["C"] * 30 and c["last_shift_kind"] == "night" and c["rest_owed"] == 2
    full = mini(31, ["D", "N", "C"], [Nurse("a", previous=["N"] + ["C"] * 30, pinned={0: "D"})],
                night_rest_days=2, night_rest_hard=True, max_consecutive_shifts=0, first_weekday=2)
    assert solve(build_model(full), full, time_limit=5, workers=2).status == "INFEASIBLE"   # the truth
    july = carried(p, "2026-06", res, "2026-07")
    jp = load_request(july).problem
    assert jp.nurses[0].rest_owed == 2 and rest_owed_after(2, jp.nurses[0].previous, "N", "D", 2) == 2
    assert validate(jp, [[0] + [-1] * 30])[0] == \
        ["night rest: a D 2026-07-01 has only 0 day(s) off, 2 still owed to a night before her history"]
    assert validate(jp, [[-1, -1, 0] + [-1] * 28])[0] == []   # two off days pay it back
    july["nurses"][0]["pinned"] = {"2026-07-01": "D"}
    jp = load_request(july).problem
    assert solve(build_model(jp), jp, time_limit=5, workers=2).status == "INFEASIBLE"
    july["nurses"][0]["pinned"] = {"2026-07-03": "D"}
    jp = load_request(july).problem
    sol = solve(build_model(jp), jp, time_limit=5, workers=2)
    assert sol.status == "OPTIMAL" and sol.assignment[0][:3] == [-1, -1, 0]
    assert rest_owed_after(2, ["C"] * 30, "N", "D", 2) == 2 and rest_owed_after(2, ["C", "", "C", ""], "N", "D", 2) == 0
    assert rest_owed_after(0, ["", "N", "C", ""], "N", "D", 2) == 1 and rest_owed_after(2, ["D"], "N", "D", 2) == 0


def test_result_is_bound_to_what_the_search_ran_with():
    """The request is frozen, and what the result says about the month, the cutoff and the
    solver settings is checked against the solution the search produced."""
    p = mini(30, ["D"], [Nurse("a")])
    req = load_request(request_of(p, "2026-06", time_limit=5))
    rm = build_model(req.problem)
    sol = solve(rm, req.problem, **req.solver.solve_kwargs(2))
    assert (sol.time_limit, sol.workers, sol.seed, sol.deterministic_budget) == (5.0, 2, 0, None)
    for name, value in (("month", parse_month("2026-09")), ("replan_from", 3), ("differ", 1)):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(req, name, value)
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.solver.reproducible = True
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.month.days = 31
    other = Request(parse_month("2026-09"), req.problem, req.solver, None, 0)   # 30 days from a Tuesday
    with pytest.raises(ValueError, match="result: the request's month 2026-09 is not the solved problem's 2026-06"):
        build_result(other, rm, sol, precheck=[], workers=2)
    other = Request(req.month, req.problem, SolverSettings(5.0, True), None, 0)
    with pytest.raises(ValueError, match="result: the request's solver settings differ"):
        build_result(other, rm, sol, precheck=[], workers=2)
    with pytest.raises(ValueError, match="result: the request's solver settings differ"):
        build_result(req, rm, sol, precheck=[], workers=4)     # the worker count is a setting too
    other = Request(req.month, req.problem, req.solver, 3, 7)
    with pytest.raises(ValueError, match="result: the request's cutoff differs from the solved problem's"):
        build_result(other, rm, sol, precheck=[], workers=2)
    res = build_result(req, rm, sol, precheck=[], workers=2)
    assert res["month"] == "2026-06" and res["replan"] is None
    assert (res["solver"]["workers"], res["solver"]["time_limit_seconds"], res["solver"]["reproducible"]) == (2, 5.0, False)
    # the cutoff and its recorded differences come from the solved snapshot, not the request
    rows = [["D"] * 3 + [""] * 27]
    raw = request_of(p, "2026-06", replan_from=3)
    raw["published"] = published_of(p, "2026-06", rows)
    raw["nurses"][0]["pinned"] = {"2026-06-02": ""}             # a recorded difference
    req = load_request(raw)
    assert req.differ == 1 == req.problem.recorded_differences()
    rm = build_model(req.problem)
    sol = solve(rm, req.problem, **req.solver.solve_kwargs(2))
    res = build_result(req, rm, sol, precheck=[], workers=2)
    assert res["replan"] == {"from": "2026-06-04", "recorded_cells_differ": 1}
    assert res["roster"]["a"]["2026-06-02"] == "" and res["elapsed_violations"] == []


def test_month_end_reconciliation_records_an_under_hours_debt():
    """June, hours rule on: 16 twelve-hour shifts and 14 off days are 192 of 208 h. Reconciled
    (replan_from = July 1) that is a recorded fact, not an infeasibility: the roster is
    returned, the under-hours is an elapsed violation, and the 16 h debt is carried whole into
    July's `hours_balance`. With a day still free the cap holds and the solver uses it."""
    june = {x: ("D" if x < 16 else "") for x in range(30)}
    p = mini(30, ["D"], [Nurse("a", pinned=june)])
    p.general = hours_general()
    a = p.nurses[0]
    assert (p.required_hours(a), p.max_hours(a), p.max_shortfall(a)) == (208, 192, 11)
    rows = [[june[x] for x in range(30)]]
    raw = request_of(p, "2026-06", replan_from=30)
    raw["published"] = published_of(p, "2026-06", rows)
    req, res = solved_raw(raw)
    assert res["ok"] and res["status"] == "OPTIMAL" and res["violations"] == [] and res["elapsed_violations"] == []
    assert res["penalties"]["avoidable_shortfall"] == 0        # the 16 h her days cannot give are exempt
    block = res["nurses"]["a"]
    assert (block["hours_worked"], block["required"], block["shortfall"], block["carry_over"]["hours_balance"]) == (192, 208, 16, 16)
    assert any("208 h owed but at most 192 h fit her free days" in i for i in res["precheck"])
    july = carried(p, "2026-06", res, "2026-07")
    jp = load_request(july).problem
    assert jp.nurses[0].hours_balance == 16 and jp.required_hours(jp.nurses[0]) == jp.contract_hours(jp.nurses[0]) + 16
    sol = solve(build_model(jp), jp, time_limit=5, workers=2)
    assert sol.status == "OPTIMAL" and sol.hard_errors == [] and sol.analysis.elapsed == []   # the debt is planned in
    assert sum(12 for v in sol.assignment[0] if v >= 0) >= jp.required_hours(jp.nurses[0]) - 11
    # a day still free: the cap holds and the solver fills it (204 h: 4 h short, under one shift)
    p2 = mini(30, ["D"], [Nurse("a", pinned={x: v for x, v in june.items() if x < 29})])
    p2.general = hours_general()
    raw = request_of(p2, "2026-06", replan_from=29)
    raw["published"] = published_of(p2, "2026-06", rows)
    req2, res2 = solved_raw(raw)
    assert res2["ok"] and res2["elapsed_violations"] == [] and res2["roster"]["a"]["2026-06-30"] == "D"
    assert (res2["nurses"]["a"]["hours_worked"], res2["nurses"]["a"]["carry_over"]["hours_balance"]) == (204, 4)
    assert not any("fit her free days" in i for i in res2["precheck"])


def test_stop_cause_follows_cp_sats_own_limits():
    """CP-SAT stops at or just past the limit it hit: the budget tripped iff the deterministic
    clock is at or past it, the wall clock iff its time is at or past its limit; both tripped
    is ambiguous and counts as the wall clock's. No percentage of the budget certifies it."""
    assert stop_cause("UNKNOWN", 0.417775, 0.42, 0.8004, 0.8, False) == "time_limit"      # a wall stop
    assert stop_cause("UNKNOWN", 0.4199, 0.42, 0.5, 0.8, False) == "time_limit"             # 99.98 % is not the budget
    assert stop_cause("UNKNOWN", 0.420047, 0.42, 0.79, 0.8, False) == "deterministic_budget"
    assert stop_cause("FEASIBLE", 0.420047, 0.42, 0.8001, 0.8, False) == "time_limit"      # both: ambiguous
    assert stop_cause("FEASIBLE", 1.0, 1.0, 5.0, 10.0, True) == "cancelled"
    assert stop_cause("FEASIBLE", 1.0, None, 5.0, 10.0, False) == "time_limit"               # no budget at all
    assert stop_cause("OPTIMAL", 0.1, 1.0, 20.0, 10.0, False) == "proof"
    assert stop_cause("INFEASIBLE", 0.0, None, 0.0, 1.0, False) == "proof"
    assert stop_cause("MODEL_INVALID", 0.0, None, 0.0, 1.0, False) == "model_invalid"
    p = generate(10, 30, 0, 4, False, [])
    p.department.headcounts = [Headcount("D", 2, 3, 2, 3), Headcount("N", 2, 3, 2, 3)]
    req = load_request(request_of(p, "2026-06", time_limit=0.3, reproducible=True))
    rm = build_model(req.problem)
    sol = solve(rm, req.problem, **req.solver.solve_kwargs(8))  # the budget: landed at or past it,
    assert sol.stopped_by == "deterministic_budget"             # the wall clock well under its guard
    assert sol.deterministic_time >= 0.3 and sol.solve_seconds < 3.0
    assert (sol.deterministic_budget, sol.time_limit, sol.workers, sol.seed) == (0.3, 3.0, 1, 0)
    res = build_result(req, rm, sol, precheck=[], workers=8)
    assert res["solver"]["deterministic"] and res["solver"]["time_limit_seconds"] == 0.3 and res["solver"]["workers"] == 1
    sol = solve(rm, req.problem, time_limit=0.3, workers=1, deterministic_time=5)   # the wall clock
    assert sol.stopped_by == "time_limit" and sol.deterministic_time < 5 and sol.solve_seconds >= 0.29   # our


# ---- 8: the month's identity, the hours exemption, switch memory and retired shifts --------------
def test_result_refuses_another_month_of_the_same_shape():
    """June 2026 and November 2027 both have 30 days from a Monday: the month is part of the
    solved problem and of its fingerprint, so a result can only be reported for the month
    that was solved."""
    june, nov = parse_month("2026-06"), parse_month("2027-11")
    assert (june.days, june.first_weekday) == (nov.days, nov.first_weekday) == (30, 0)
    p = mini(30, ["D"], [Nurse("a")])
    raw = request_of(p, "2026-06")
    req = load_request(raw)
    assert req.problem.month == "2026-06"
    rm = build_model(req.problem)
    sol = solve(rm, req.problem, **req.solver.solve_kwargs(2))
    other = Request(nov, req.problem, req.solver, None, 0)
    with pytest.raises(ValueError, match="result: the request's month 2027-11 is not the solved problem's 2026-06"):
        build_result(other, rm, sol, precheck=[], workers=2)
    assert build_result(req, rm, sol, precheck=[], workers=2)["month"] == "2026-06"
    raw["month"] = "2027-11"
    assert build_model(load_request(raw).problem).fingerprint != rm.fingerprint   # not the same problem
    q = copy.deepcopy(req.problem)
    assert Problem.from_dict(json.loads(json.dumps(asdict(q)))).month == "2026-06"
    q.month = "2026-07"
    with pytest.raises(ValueError, match="month 2026-07 has 31 days from weekday 2, not 30 from 0"):
        q.check()
    q.month = "2026-6"
    with pytest.raises(ValueError, match="month must be YYYY-MM or null, got '2026-6'"):
        q.check()
    q.month = None                                             # a problem built by hand has none
    q.check()
    rq = build_model(q)
    with pytest.raises(ValueError, match="result: the solved problem carries no month"):
        build_result(Request(june, q, req.solver, None, 0), rq, solve(rq, q, **req.solver.solve_kwargs(2)),
                     precheck=[], workers=2)
    with pytest.raises(ValueError, match="the problem was built for 2026-06, not 2027-11"):
        request_of(req.problem, "2027-11")


def test_only_the_unreachable_hours_are_exempt_from_the_cap():
    """168 h recorded, 208 h owed, two free 12 h days: the 16 h no roster can work are a fact,
    the cap holds for the rest, so the solver must use both days even against a change weight
    of 100; a roster that leaves them idle breaks the hours rule, not a recorded fact."""
    june = {x: "D" for x in range(14)} | {x: "" for x in range(14, 28)}
    p = mini(30, ["D"], [Nurse("a", pinned=june)])
    p.general = hours_general(weights=dataclasses.replace(quiet(), change=100, nurse_shortfall=1, avoidable_shortfall=1000))
    a = p.nurses[0]
    assert (p.required_hours(a), p.max_hours(a), p.unreachable_hours(a)) == (208, 192, 16)
    rows = [[june.get(x, "") for x in range(30)]]              # the plan left the two days off
    raw = request_of(p, "2026-06", replan_from=28)
    raw["published"] = published_of(p, "2026-06", rows)
    req, res = solved_raw(raw)
    assert res["ok"] and res["status"] == "OPTIMAL" and res["violations"] == []
    assert res["roster"]["a"]["2026-06-29"] == "D" and res["roster"]["a"]["2026-06-30"] == "D"
    block = res["nurses"]["a"]
    assert (block["hours_worked"], block["shortfall"], block["carry_over"]["hours_balance"]) == (192, 16, 16)
    assert res["cost"] == 2 * 100 + 16 and res["penalties"]["change"] == 200 and res["elapsed_violations"] == []
    assert res["penalties"]["avoidable_shortfall"] == 0
    idle = [[0] * 14 + [-1] * 16]                              # both free days left off: 40 h short, 13 h
    errors, weighted, _ = validate(req.problem, idle)          # beyond the remainder and the 16 h
    assert errors == [] and weighted["avoidable_shortfall"] == 13 * 1000
    assert explain(req.problem, idle).rules["avoidable_shortfall"].matches[0].text == \
        "a: 40 h short, 13 h beyond a remainder below one shift and the 16 h her days cannot give"
    one = [[0] * 14 + [-1] * 14 + [0, -1]]                     # one of them: 28 h short, 1 h avoidable
    assert validate(req.problem, one)[1]["avoidable_shortfall"] == 1000
    assert any("the 16 h beyond them are a recorded fact" in i for i in res["precheck"])


def test_switch_memory_holds_through_leading_off_days_without_any_history():
    """No nurse has a history (P = 0): a nurse whose last shift was a night, off on day 1 and asked
    to work day 2, may not work a D there under a hard zero-switch cap; the memory must
    survive the off day, with and without another nurse's history."""
    def month(*extra: Nurse) -> Problem:
        a = Nurse("a", last_shift_kind="night", allowed_shifts=["D"], desired_days=[1])
        return mini(30, ["D", "N"], [a, *extra], max_switches=0, switches_hard=True, weights=only("desired_day", 5))
    for extra in ((), (Nurse("b", previous=["D"]),)):
        req, res = solved(month(*extra), "2026-06")
        assert res["ok"] and res["violations"] == [] and res["status"] == "OPTIMAL", res["violations"]
        assert all(c == "" for c in res["roster"]["a"].values())
        assert res["penalties"]["desired_day"] == 5 and res["cost"] == 5
        assert validate(req.problem, [[-1, 0] + [-1] * 28] + [[-1] * 30] * len(extra))[0] == \
            ["switches: a changes night/day 1 times, max 0"]


def test_padding_cannot_waive_a_carried_rest_penalty():
    """A nurse with no history but rest_owed = 2 and an elapsed D on day 1, soft night rest,
    N->D a forbidden succession: the adjacent-night exemption may only read a real day. With
    another nurse's history the solver used to invent an N in her padding (solver 0, validator
    7, exit 3); the penalty is 7 either way."""
    def month(with_b: bool) -> Problem:
        nurses = [Nurse("a", rest_owed=2, pinned={0: "D"})] + ([Nurse("b", previous=["D"])] if with_b else [])
        p = mini(30, ["D", "N"], nurses, night_rest_days=2, night_rest_hard=False, no_mixed_blocks=True,
                 weights=only("night_rest", 7))
        p.published = [["D"] + [""] * 29] + ([[""] * 30] if with_b else [])
        return p
    for with_b in (True, False):
        req = load_request(request_of(month(with_b), "2026-06", replan_from=1))
        rm = build_model(req.problem)
        sol = solve(rm, req.problem, **req.solver.solve_kwargs(2))
        assert sol.status == "OPTIMAL" and verdict(sol) == (0, ""), (sol.solver_objective, sol.objective)
        assert sol.solver_objective == sol.objective == 7 and sol.penalty_breakdown["night_rest"] == 7
        assert build_result(req, rm, sol, precheck=[], workers=2)["ok"]


def test_a_retired_shift_in_the_history_is_work_of_no_kind():
    """The unit retires C: a history naming it still loads; C counts as worked (a run), as
    neither night nor day (no rest paid, a switch before an N), precheck says so, and the
    kind carried for it is "day"."""
    p = mini(30, ["D", "N"], [Nurse("a", previous=["C", "C", "C"], pinned={0: "D"})], max_consecutive_shifts=3)
    req = load_request(request_of(p, "2026-06"))
    assert req.problem.nurses[0].previous == ["C", "C", "C"]
    assert any("history names ['C'], which the unit no longer defines" in i for i in precheck(req.problem))
    assert solve(build_model(req.problem), req.problem, time_limit=5, workers=2).status == "INFEASIBLE"   # 4 in a row
    assert validate(req.problem, [[0] + [-1] * 29])[0] == ["consecutive: a 4 shifts in a row at 2026-06-01"]
    p.nurses[0].pinned = {1: "D"}
    req, res = solved(p, "2026-06")
    assert res["ok"] and res["roster"]["a"]["2026-06-01"] == "" and res["nurses"]["a"]["carry_over"]["last_shift_kind"] == "day"
    p = mini(30, ["D", "N"], [Nurse("a", previous=["N", "C"], pinned={0: "D"})], night_rest_days=2,
             night_rest_hard=True, max_consecutive_shifts=0)
    req = load_request(request_of(p, "2026-06"))               # the N still owes its rest: C paid none
    assert solve(build_model(req.problem), req.problem, time_limit=5, workers=2).status == "INFEASIBLE"
    p = mini(30, ["D", "N"], [Nurse("a", previous=["C"], pinned={0: "N"})], max_switches=0, switches_hard=True)
    req = load_request(request_of(p, "2026-06"))               # C is day work: the N is a switch
    assert switch_memory(req.problem.nurses[0], "N") is False
    assert solve(build_model(req.problem), req.problem, time_limit=5, workers=2).status == "INFEASIBLE"
    p.nurses[0].pinned, p.nurses[0].unavailable = {}, list(range(30))
    req, res = solved(p, "2026-06")                            # a month of leave carries the kind
    assert res["ok"] and res["nurses"]["a"]["carry_over"]["last_shift_kind"] == "day"


# ---- 9: the avoidable shortfall, renamed shifts and re-plan metadata -----------------------------
def test_avoidable_shortfall_bends_to_the_other_hard_rules():
    """168 h recorded through June 28 with the last three days worked, a run limit of 3 and two
    free days: only one of them can be worked. A hard floor from any per-nurse bound made this
    INFEASIBLE; the dominant weight takes the one day and reports the hour it could not make
    up, and with a shared headcount maximum two short nurses split the two free slots."""
    june = {x: "" for x in range(28)}                          # 14 D within the run limit, the
    for x in [0, 1, 2, 4, 5, 6, 8, 9, 10, 12, 13, 25, 26, 27]:   # last three days worked
        june[x] = "D"
    p = mini(30, ["D"], [Nurse("a", pinned=june)], max_consecutive_shifts=3)
    p.general = hours_general()
    a = p.nurses[0]
    assert (p.required_hours(a), p.max_hours(a), p.unreachable_hours(a)) == (208, 192, 16)
    rows = [[june.get(x, "") for x in range(30)]]
    raw = request_of(p, "2026-06", replan_from=28)
    raw["published"] = published_of(p, "2026-06", rows)
    req, res = solved_raw(raw)
    assert res["ok"] and res["status"] == "OPTIMAL" and res["violations"] == [] and res["elapsed_violations"] == []
    assert (res["roster"]["a"]["2026-06-29"], res["roster"]["a"]["2026-06-30"]) == ("", "D")
    block = res["nurses"]["a"]
    assert (block["hours_worked"], block["shortfall"], block["carry_over"]["hours_balance"]) == (180, 28, 28)
    assert res["penalties"]["avoidable_shortfall"] == 1000 == res["cost"]   # 28 − 11 − 16: the run rule's hour
    p = mini(30, ["D"], [Nurse("a", pinned=june), Nurse("b", pinned=june)],
             headcounts=[Headcount("D", 0, 1, 0, 1)], headcount_enabled=True)
    p.general = hours_general()
    raw = request_of(p, "2026-06", replan_from=28)
    raw["published"] = published_of(p, "2026-06", rows * 2)
    req, res = solved_raw(raw)
    assert res["ok"] and res["status"] == "OPTIMAL" and res["violations"] == []
    worked = [res["nurses"][n]["hours_worked"] for n in "ab"]
    assert sorted(worked) == [180, 180] and res["penalties"]["avoidable_shortfall"] == 2 * 1000


def test_switch_memory_survives_a_renamed_night_shift():
    """The unit renames N to Night: the carried kind still says night, so a D is a switch and
    a Night is not; an old history naming N is mapped by renamed_shifts, and without the map
    it is work of no kind, which precheck reports."""
    def month(kind: str | None, pin: str, previous: list[str] = ()) -> Problem:
        p = mini(30, ["D", "Night"], [Nurse("a", last_shift_kind=kind, previous=list(previous), pinned={0: pin})],
                 night_shift="Night", max_switches=0, switches_hard=True)
        p.general.shift_hours = {**p.general.shift_hours, "Night": 12}
        return p
    for pin, status in (("D", "INFEASIBLE"), ("Night", "OPTIMAL")):
        req = load_request(request_of(month("night", pin), "2026-06"))
        assert solve(build_model(req.problem), req.problem, time_limit=5, workers=2).status == status
    raw = request_of(month(None, "Night", ["N", ""]), "2026-06")
    req = load_request(raw)                                    # "N" is no shift of this unit any more
    assert req.problem.nurses[0].previous == ["N", ""] and switch_memory(req.problem.nurses[0], "Night") is False
    assert any("history names ['N'], which the unit no longer defines" in i for i in precheck(req.problem))
    assert solve(build_model(req.problem), req.problem, time_limit=5, workers=2).status == "INFEASIBLE"
    raw["renamed_shifts"] = {"N": "Night"}
    req = load_request(raw)
    assert req.problem.nurses[0].previous == ["Night", ""] and switch_memory(req.problem.nurses[0], "Night") is True
    assert not any("no longer defines" in i for i in precheck(req.problem))
    req, res = solved_raw(raw)
    assert res["ok"] and res["nurses"]["a"]["carry_over"]["last_shift_kind"] == "night"


def test_replan_metadata_comes_from_the_snapshot():
    """A request loaded with a published roster and no cutoff, whose problem is then re-planned
    by the caller: the result reports the snapshot's cutoff, the only one it was solved with."""
    p = mini(30, ["D"], [Nurse("a", pinned={0: ""})], headcounts=[Headcount("D", 1, 1, 1, 1)], headcount_enabled=True)
    p.published = [[""] * 30]
    req = load_request(request_of(p, "2026-06"))
    assert req.replan_from is None and req.problem.planning_from == 0
    assert req.problem.replan_from(30) == 0
    rm = build_model(req.problem)
    sol = solve(rm, req.problem, **req.solver.solve_kwargs(2))
    res = build_result(req, rm, sol, precheck=[], workers=2)
    assert res["ok"] and res["replan"] == {"from": "2026-07-01", "recorded_cells_differ": 0}
    assert len(res["elapsed_violations"]) == 30 and res["violations"] == []


# ---- 10: the CLI and the result read the snapshot ------------------------------------------------
def test_cli_csv_and_report_come_from_the_snapshot(tmp_path, monkeypatch, capsys):
    """An app that edits the request's problem at solver return (shift order, nurse order) must
    get the same roster in the CSV as in the JSON: both come from the solved snapshot."""
    p = mini(30, ["D", "N"], [Nurse("a", pinned={0: "D"}), Nurse("b", pinned={0: "N"})])
    req = load_request(request_of(p, "2026-06"))
    real = cli.solve

    def edited(rm, q, **kw):
        sol = real(rm, q, **kw)
        req.problem.department.shifts[:] = ["N", "D"]
        req.problem.nurses.reverse()
        return sol
    monkeypatch.setattr(cli, "solve", edited)
    out_json, out_csv = tmp_path / "r.json", tmp_path / "r.csv"
    assert cli.run(req, 2, out=str(out_json), csv_out=str(out_csv), explain_text=True) == 0
    res = json.loads(out_json.read_text())
    assert res["roster"]["a"]["2026-06-01"] == "D" and res["roster"]["b"]["2026-06-01"] == "N"
    rows = list(csv.reader(out_csv.read_text().splitlines()))
    day1 = rows[0].index("1Mo")
    assert [(r[0], r[day1]) for r in rows[1:3]] == [("a", "D"), ("b", "N")]
    assert "vs published" not in capsys.readouterr().out      # no baseline: nothing read from the edit


def test_result_calendar_is_the_snapshots():
    """A hand-built request whose MonthInfo disagrees with the solved problem is refused, and
    the dates the result reports (the cutoff included) come from the snapshot's month."""
    p = mini(30, ["D"], [Nurse("a", pinned={x: "D" if x < 20 else "" for x in range(30)})])
    p.published = [["D"] * 20 + [""] * 10]
    req = load_request(request_of(p, "2026-06", replan_from=30))
    rm = build_model(req.problem)
    sol = solve(rm, req.problem, **req.solver.solve_kwargs(2))
    bad = Request(MonthInfo(2026, 6, 31, 0, []), req.problem, req.solver, 30, 0)
    with pytest.raises(ValueError, match="result: the request's month does not fit the solved problem"):
        build_result(bad, rm, sol, precheck=[], workers=2)
    hand = Request(MonthInfo(2026, 6, 30, 0, [1, 2, 3]), req.problem, req.solver, 30, 0)   # odd Ramadan list
    res = build_result(hand, rm, sol, precheck=[], workers=2)
    assert res["replan"] == {"from": "2026-07-01", "recorded_cells_differ": 0}
    assert list(res["roster"]["a"])[-1] == "2026-06-30" and res["month"] == "2026-06"


def test_cli_names_the_avoidable_shortfall(tmp_path, capsys):
    """The run-limited re-plan of section 9: the roster is returned and the CLI says who ends a
    shift or more short, and that the optimum is one of the weighted objective as a whole."""
    june = {x: "" for x in range(28)}
    for x in [0, 1, 2, 4, 5, 6, 8, 9, 10, 12, 13, 25, 26, 27]:
        june[x] = "D"
    p = mini(30, ["D"], [Nurse("a", pinned=june)], max_consecutive_shifts=3)
    p.general = hours_general()
    p.published = [[june.get(x, "") for x in range(30)]]
    f = tmp_path / "r.json"
    f.write_text(json.dumps(request_of(p, "2026-06", replan_from=28)))
    assert main(["solve", str(f), "--workers", "2"]) == 0
    out = capsys.readouterr().out
    assert "avoidable shortfall: 1 nurse(s) end a shift or more short; optimal for the weighted objective as a whole" in out
    assert "a: 28 h short, 1 h beyond a remainder below one shift and the 16 h her days cannot give" in out


# ---- 11: incumbents from the callback and the CLI's shortfall ------------------------------------
def test_every_incumbent_can_be_serialized_from_the_callback():
    """A front end that builds a result from its progress callback: each incumbent carries the
    search's settings, in the ordinary and in the reproducible mode."""
    for reproducible in (False, True):
        p = generate(10, 30, 0, 4, False, [])
        p.department.headcounts = [Headcount("D", 2, 3, 2, 3), Headcount("N", 2, 3, 2, 3)]
        req = load_request(request_of(p, "2026-06", time_limit=3, reproducible=reproducible))
        rm = build_model(req.problem)
        seen = []
        stop = threading.Event()

        def on_incumbent(s: Solution) -> None:
            seen.append(build_result(req, rm, s, precheck=[], workers=4))
            stop.set()
        sol = solve(rm, req.problem, on_solution=on_incumbent, stop=stop, **req.solver.solve_kwargs(4))
        assert seen and seen[0]["ok"] and seen[0]["solver"]["workers"] == (1 if reproducible else 4)
        assert seen[0]["solver"]["reproducible"] is reproducible and seen[0]["solver"]["stopped_by"] == ""
        assert not seen[0]["solver"]["deterministic"] and seen[0]["solver"]["time_limit_seconds"] == 3
        assert build_result(req, rm, sol, precheck=[], workers=4)["roster"] == seen[0]["roster"]


def test_cli_does_not_blame_hard_rules_for_an_optimal_shortfall(tmp_path, capsys):
    """16 h owed, two free days the nurse asked off at a weight of 10 000 against an avoidable
    shortfall weight of 1 000: idling both is optimal at cost 5 000, and the CLI must say the
    optimum is for the weighted objective as a whole, not that a hard rule allowed no more."""
    p = mini(30, ["D"], [Nurse("a", unavailable=list(range(28)), undesired_days=[28, 29])])
    p.general = hours_general(weights=dataclasses.replace(quiet(), avoidable_shortfall=1000, undesired_day=10000))
    assert p.required_hours(p.nurses[0]) == 16 and p.unreachable_hours(p.nurses[0]) == 0
    f = tmp_path / "r.json"
    f.write_text(json.dumps(request_of(p, "2026-06")))
    assert main(["solve", str(f), "--workers", "2", "--out", str(tmp_path / "out.json")]) == 0
    out = capsys.readouterr().out
    res = json.loads((tmp_path / "out.json").read_text())
    assert res["status"] == "OPTIMAL" and res["cost"] == 5000 and res["nurses"]["a"]["hours_worked"] == 0
    assert "avoidable shortfall: 1 nurse(s) end a shift or more short; optimal for the weighted objective as a whole" in out
    assert "leave no way" not in out and "a: 16 h short, 5 h beyond a remainder below one shift" in out


# ---- 12: the look-back bound, mismatch in the CLI ------------------------------------------------
def test_rules_that_look_back_further_than_a_history_can_hold_are_refused():
    """max_consecutive_shifts 30 with double_cycle_rest 7 look back 67 days; a carried history
    of 62 would lose a double-cycle obligation, so the configuration is refused up front."""
    p = mini(30, ["D"], [Nurse("a")], max_consecutive_shifts=30, double_cycle_rest=7)
    assert needed_history(p.department, p.general) == 67
    with pytest.raises(ValueError, match=r"the pattern rules look back 67 days .* more than a history may hold \(62\)"):
        p.check()
    with pytest.raises(ValueError, match="request: the pattern rules look back 67 days"):
        load_request(request_of(p, "2026-06"))
    p = mini(30, ["D"], [Nurse("a")], max_consecutive_shifts=27, double_cycle_rest=7)   # 61: fits
    load_request(request_of(p, "2026-06"))


def test_cli_reports_a_mismatch_next_to_hard_violations(tmp_path, monkeypatch, capsys):
    p = mini(30, ["D"], [Nurse("a", pinned={0: "D"})])
    req = load_request(request_of(p, "2026-06"))
    real = cli.solve

    def drifted(rm, q, **kw):
        sol = real(rm, q, **kw)
        sol.mismatch = "model and validator disagree: an incumbent validated at 1 below the proven optimum 3"
        sol.hard_errors = ["pin: a day 0 is off, pinned D"]
        return sol
    monkeypatch.setattr(cli, "solve", drifted)
    assert cli.run(req, 2, out=str(tmp_path / "r.json")) == 3
    out = capsys.readouterr().out
    assert "HARD VIOLATIONS: 1" in out and "pin: a day 0 is off, pinned D" in out
    assert "ERROR: model and validator disagree: an incumbent validated at 1 below the proven optimum 3 (and the roster breaks 1 hard rule(s))" in out
    res = json.loads((tmp_path / "r.json").read_text())
    assert not res["ok"] and res["message"].startswith("model and validator disagree")


def test_cli_without_progress_reports_a_mismatch_next_to_hard_violations(tmp_path, monkeypatch, capsys):
    """The default path, no callback: an OPTIMAL roster validated above the solver's objective
    next to a pin violation is exit 3 with both diagnostics, not hard violations only."""
    p = mini(30, ["D"], [Nurse("a", pinned={0: "D"})])
    req = load_request(request_of(p, "2026-06"))
    real = cli.solve

    def drifted(rm, q, **kw):
        sol = real(rm, q, **kw)
        assert sol.status == "OPTIMAL" and kw.get("on_solution") is None
        sol.objective, sol.hard_errors = sol.solver_objective + 5, ["pin: a day 0 is off, pinned D"]
        return sol
    monkeypatch.setattr(cli, "solve", drifted)
    assert cli.run(req, 2, out=str(tmp_path / "r.json")) == 3
    out = capsys.readouterr().out
    assert "HARD VIOLATIONS: 1" in out and "pin: a day 0 is off, pinned D" in out
    assert "ERROR: the solver's objective is below the roster's cost; model and validator disagree (and the roster breaks 1 hard rule(s))" in out
    assert not json.loads((tmp_path / "r.json").read_text())["ok"]
