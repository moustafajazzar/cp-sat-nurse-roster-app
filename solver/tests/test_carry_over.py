"""The per-nurse block (hours, overtime and carry-over) as one function of the package: a solve's result and a month with every day
recorded, worked out with no search (the application's month close), cannot work it out differently."""
from __future__ import annotations

import pytest

from nurse_roster.model import build_model, solve
from nurse_roster.precheck import precheck
from nurse_roster.problem import Headcount, Nurse
from nurse_roster.request import load_request
from nurse_roster.result import build_result, nurse_accounting
from solver_service.grid import month_facts, nurses_of, validate_grid
from tests.er_fixture import request_of
from tests.test_core import mini


def _problem():
    return mini(30, ["D", "N"], [Nurse("a", previous=["N", "N", "", ""], unavailable=[2], ytd_overtime=5,
                                       night_balance=0.5, friday_balance=-1.0),
                                 Nurse("b", previous=["D"], hours_balance=4, rest_owed=1, last_shift_kind="night")],
                headcounts=[Headcount("D", 1, 1, 1, 1), Headcount("N", 0, 1, 0, 1)], headcount_enabled=True,
                fair_shifts=["D", "N"], fair_weekends=True, max_consecutive_shifts=3, double_cycle_rest=2)


@pytest.fixture(scope="module")
def solved():
    raw = request_of(_problem(), "2026-06")
    req = load_request(raw)
    rm = build_model(req.problem)
    sol = solve(rm, req.problem, **req.solver.solve_kwargs(4))
    return raw, sol, build_result(req, rm, sol, precheck=precheck(req.problem), workers=4)


def test_a_real_solves_nurses_block_is_the_function_of_its_own_roster(solved):
    raw, sol, result = solved
    assert result["roster"] and result["nurses"]
    assert nurse_accounting(sol.problem, sol.assignment, sol.analysis) == result["nurses"]
    assert nurse_accounting(sol.problem, sol.assignment) == result["nurses"]   # the analysis worked out when not given
    assert nurses_of(raw, result["roster"]) == result["nurses"]                 # and through the service's own module


def _whole_month_recorded(raw: dict, roster: dict) -> dict:
    return raw | {"published": roster, "replan_from": "2026-07-01"}


def test_a_month_with_every_day_recorded_is_its_roster_in_force_with_leave_winning(solved):
    raw, _, result = solved
    roster = {nurse: dict(row) for nurse, row in result["roster"].items()}
    roster["a"]["2026-06-04"] = "D"                       # a shift the roster in force holds on a day leave took
    raw = raw | {"nurses": [dict(row, leave=["2026-06-03", "2026-06-04"]) if row["id"] == "a" else row
                            for row in raw["nurses"]]}
    closing = _whole_month_recorded(raw, roster)

    facts = month_facts(closing)

    assert facts["a"]["2026-06-04"] == "" and facts["a"]["2026-06-03"] == ""
    assert {day: code for day, code in facts["b"].items()} == roster["b"]
    accounts = nurses_of(closing, facts)
    assert accounts["a"]["leave"] == ["2026-06-03", "2026-06-04"]
    hours = raw["general"]["shift_hours"]
    assert accounts["a"]["hours_worked"] == sum(hours[code] for code in facts["a"].values() if code)


def test_a_pin_is_a_fact_over_the_roster_in_force(solved):
    raw, _, result = solved
    roster = result["roster"]
    other = "N" if roster["b"]["2026-06-10"] == "D" else "D"
    raw = raw | {"nurses": [dict(row, pinned={"2026-06-10": other}) if row["id"] == "b" else row for row in raw["nurses"]]}

    assert month_facts(_whole_month_recorded(raw, roster))["b"]["2026-06-10"] == other


def test_what_the_facts_broke_is_reported_as_elapsed(solved):
    raw, _, result = solved
    roster = {nurse: dict.fromkeys(row, "") for nurse, row in result["roster"].items()}   # nobody worked: every D uncovered
    closing = _whole_month_recorded(raw, roster)

    answer = validate_grid(closing, month_facts(closing))

    assert answer["violations"] == []
    assert any(item["rule"] == "headcount" for item in answer["elapsed_violations"])


def test_the_history_carried_out_is_the_months_row():
    raw = request_of(_problem(), "2026-06")
    roster = {"a": {f"2026-06-{day:02d}": ("N" if day > 27 else "") for day in range(1, 31)},
              "b": {f"2026-06-{day:02d}": "D" if day % 2 else "" for day in range(1, 31)}}
    closing = _whole_month_recorded(raw | {"nurses": [dict(row, leave=[]) for row in raw["nurses"]]}, roster)

    carried = nurses_of(closing, month_facts(closing))["a"]["carry_over"]

    assert carried["previous"][-3:] == ["N", "N", "N"] and carried["rest_owed"] == 0
