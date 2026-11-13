"""Overtime share: a nurse's share of the unit's overtime in the month is
that overtime times the hours she owes over the hours every nurse of the input owes; her target is her share less her
carried `overtime_balance` (held within the unit's `overtime_carry_cap`) when the unit carries fairness; her overtime above
the target, beyond `overtime_tolerance`, and never more than her overtime, costs `overtime_above_share` per whole hour,
a part hour never priced. Expected values are counted by hand on small months, never recomputed the way the code does."""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from nurse_roster.explain import explain, report
from nurse_roster.model import build_model, solve
from nurse_roster.problem import Headcount, Nurse, Problem
from nurse_roster.request import load_request
from nurse_roster.result import nurse_accounting
from nurse_roster.rules import active
from nurse_roster.validate import validate
from tests.er_fixture import raw, version_1
from tests.rules_helper import penalises
from tests.test_core import hours_general, mini, quiet

W, OFF = 0, -1
RULE = "overtime_above_share"
FIXTURES = Path(__file__).parents[1] / "service_tests" / "fixtures"


def week(nurses: list[Nurse], *, w: int = 1, tol: int = 12, cap: int = 12, carry: bool = False, share: bool = True,
         heads: tuple[int, int] = (2, 2), hours_rule: bool = True, **dep) -> Problem:
    """Seven days from a Saturday, the Friday last: six days owed at 8 h, so 48 h a nurse, four 12 h day shifts. With two day
    shifts a day the month holds 14 shifts against 12 owed by three nurses: 24 h of overtime, two extra shifts, forced. Only
    the hours rule, the hour of overtime (1) and the new rule are priced."""
    rules = dict(headcount_enabled=True,
                 headcounts=[Headcount("D", heads[0], heads[1], heads[0], heads[1]), Headcount("N", 0, 0, 0, 0)],
                 share_overtime=share, overtime_tolerance=tol, overtime_carry_cap=cap, carry_fairness=carry)
    p = mini(7, ["D", "N"], nurses, first_weekday=5, **{**rules, **dep})
    p.general = hours_general(hours_rule_enabled=hours_rule,
                              weights=replace(quiet(), avoidable_shortfall=1000, nurse_overtime=1, overtime_above_share=w))
    return p


def row(text: str) -> list[int]:
    """"WWWW..." -> a day shift on the first four days, off on the last three."""
    return [W if c == "W" else OFF for c in text]


def pinned(p: Problem, grid: list[list[int]]):
    """The solver with every cell held to the grid (None: free)."""
    rm = build_model(p)
    for n, cells in enumerate(grid):
        for d, v in enumerate(cells):
            if v is not None:
                rm.model.add((rm.off[n][d] if v < 0 else rm.x[n][d][v]) == 1)
    return solve(rm, p, time_limit=10, workers=4)


def extras(grid: list[list[int]]) -> list[int]:
    """Each nurse's extra shifts on `week`: shifts worked beyond the four she owes."""
    return [sum(1 for v in cells if v >= 0) - 4 for cells in grid]


def priced(p: Problem, grid: list[list[int]]) -> dict[str, int]:
    return {m.nurse: m.amount for m in explain(p, grid).rules[RULE].matches}


PILE = [row("WWWWWW."), row("WWW...W"), row("...WWWW")]       # a: 72 h, 24 h over; b and c at 48 h
SPREAD = [row("WWWWW.."), row("WWW..WW"), row("...WWWW")]     # a and b 12 h over each, c none


# ---- the price, counted by hand ------------------------------------------------------------------
def test_with_the_rule_on_no_nurse_holds_a_second_extra_shift_while_another_holds_none():
    """Shares of 8 h each (24 h x 48 / 144). With 12 h free, one extra shift is free and a second costs: the solver spreads them."""
    p = week([Nurse("a"), Nurse("b"), Nurse("c")])

    sol = solve(build_model(p), p, time_limit=10, workers=4)

    assert sol.status == "OPTIMAL" and sol.objective == sol.solver_objective == 24     # the 24 h of overtime alone
    assert sorted(extras(sol.assignment)) == [0, 1, 1]


def test_with_no_tolerance_the_spread_still_wins_and_costs_what_the_hand_count_says():
    """Spread: a and b each 12 h over a share of 8, 4 h priced each, 8 in all; piled on a: 24 - 8 = 16 h."""
    p = week([Nurse("a"), Nurse("b"), Nurse("c")], tol=0)

    sol = solve(build_model(p), p, time_limit=10, workers=4)

    assert sol.status == "OPTIMAL" and sol.objective == sol.solver_objective == 24 + 8
    assert sorted(extras(sol.assignment)) == [0, 1, 1]
    assert priced(p, PILE) == {"a": 16}


def test_the_cost_of_an_uneven_grid_is_the_weight_times_the_hours_above_share_and_tolerance():
    """a: 24 h over, share 8, tolerance 12: 4 h priced, at 3 an hour. b and c have no overtime and cost nothing."""
    p = week([Nurse("a"), Nurse("b"), Nurse("c")], w=3)

    assert priced(p, PILE) == {"a": 4}
    assert penalises(p, PILE, RULE) == 12
    errors, penalties, total = validate(p, PILE)
    assert not errors and (penalties[RULE], penalties["nurse_overtime"], total) == (12, 24, 36)


def test_a_solve_and_a_validation_of_the_same_grid_agree():
    p = week([Nurse("a"), Nurse("b"), Nurse("c")], w=3)

    sol = pinned(p, PILE)

    assert sol.status == "OPTIMAL" and sol.solver_objective == sol.objective == validate(p, PILE)[2] == 36


def test_a_part_hour_is_never_priced():
    """c is on leave on the Saturday (owes 40 h), works four shifts: 8 h over. O = 24 + 8 = 32, R = 48 + 48 + 40 = 136.
    a's share is 32 x 48 / 136 = 11.29 h: with no tolerance 24 - 11.29 = 12.71 h above it, 12 priced; with 12 free, 0.71: none.
    c's share is 32 x 40 / 136 = 9.41 h: her 8 h are below it, not priced."""
    grid = [row("WWWWWW."), row("WWWW..."), row("..WWWW.")]
    nurses = lambda: [Nurse("a"), Nurse("b"), Nurse("c", unavailable=[0])]

    assert priced(week(nurses(), tol=0), grid) == {"a": 12}
    assert priced(week(nurses(), tol=12), grid) == {}
    sol = pinned(week(nurses(), tol=0, headcount_enabled=False), grid)
    assert sol.status == "OPTIMAL" and sol.solver_objective == sol.objective == 32 + 12


def test_a_nurse_with_no_overtime_costs_nothing_whatever_she_carries():
    """b carries 24 h above her share, the carry may move it by 24, nothing is free: her target is 8 - 24 = -16, yet she
    works no overtime, so she costs nothing (her price is never more than her overtime)."""
    p = week([Nurse("a"), Nurse("b", overtime_balance=24.0), Nurse("c")], tol=0, cap=24, carry=True)

    assert "b" not in priced(p, PILE)
    sol = pinned(p, PILE)
    assert sol.status == "OPTIMAL" and sol.solver_objective == sol.objective == validate(p, PILE)[2]


def test_hours_above_the_target_are_priced_but_never_more_than_her_overtime():
    """a carries 30 h above her share, held at the limit of 12: target 8 - 12 = -4, and 12 h over it would be 16, but she only
    worked 12 h of overtime: 12 priced. b: 12 over a share of 8, 4. The model agrees, the cap of her overtime included."""
    p = week([Nurse("a", overtime_balance=30.0), Nurse("b"), Nurse("c")], tol=0, cap=12, carry=True)

    assert priced(p, SPREAD) == {"a": 12, "b": 4}
    sol = pinned(p, SPREAD)
    assert sol.status == "OPTIMAL" and sol.solver_objective == sol.objective == 24 + 16


def test_a_large_carried_balance_never_makes_a_month_infeasible_and_steers_overtime_away():
    """b carries 24 h above her share: any overtime of hers is priced whole. The solver gives the two extra shifts to a and c,
    4 h priced each (12 over a share of 8): 24 + 8."""
    p = week([Nurse("a"), Nurse("b", overtime_balance=24.0), Nurse("c")], tol=0, cap=24, carry=True)

    sol = solve(build_model(p), p, time_limit=10, workers=4)

    assert sol.status == "OPTIMAL" and sol.objective == sol.solver_objective == 32
    assert extras(sol.assignment) == [1, 0, 1]


def test_with_the_rule_on_the_months_total_overtime_is_never_above_what_it_is_with_the_rule_off():
    """a is held to six shifts, 24 h over; the headcount allows a third nurse a day. Giving b an extra shift would raise
    every share (36 h to share) and lower a's priced hours from 16 to 12, but costs 12 h of overtime: the solver gives none."""
    def total(w: int) -> int:
        p = week([Nurse("a"), Nurse("b"), Nurse("c")], w=w, tol=0, heads=(2, 3))
        sol = pinned(p, [row("WWWWWW."), [None] * 7, [None] * 7])
        assert sol.status == "OPTIMAL"
        return sum(max(0, 12 * sum(1 for v in cells if v >= 0) - 48) for cells in sol.assignment)

    assert total(1) == total(0) == 24


def ten_in_february(*, w: int, tol: int) -> Problem:
    """A month larger than `week`. February 2026, 28 days from a Sunday,
    24 owed at 8 h: 192 h, 16 day shifts of 12 h, a nurse. Ten nurses owe 160 shifts; six to eight on Day every day need at
    least 168, so 8 extra shifts, 96 h of overtime, are forced, under the cap of 40 h a nurse."""
    p = week([Nurse(f"n{i}") for i in range(10)], w=w, tol=tol, heads=(6, 8))
    p.days, p.first_weekday, p.month = 28, 6, "2026-02"
    return p


def overtime(grid: list[list[int]]) -> list[int]:
    return [max(0, 12 * sum(1 for v in cells if v >= 0) - 192) for cells in grid]


def test_on_a_larger_month_the_rule_never_raises_the_total_overtime_with_many_nurses_priced_at_once():
    """n0 is held to 19 shifts, 36 h over, so 5 more extra shifts are forced on the others: 96 h. With nothing free, at 96 h
    each share is 9.6 h, n0 is priced 26 h and five others 2 h each. One more extra shift would raise every share to 10.8 h
    and lower the rule's cost by 1 + 5 - 1 = 5, rounding included, against 12 for the hour of overtime: the solver adds none."""
    def total(w: int) -> int:
        p = ten_in_february(w=w, tol=0)
        sol = pinned(p, [[W if d < 19 else OFF for d in range(28)]] + [[None] * 28] * 9)
        assert sol.status == "OPTIMAL"
        return sum(overtime(sol.assignment))

    assert total(1) == total(0) == 96


def test_on_a_larger_month_no_nurse_holds_a_second_extra_shift_while_another_holds_none():
    """With 12 h free the eight forced extra shifts go one each: each share is 9.6 h, so a second would cost 24 - 9.6 - 12."""
    p = ten_in_february(w=1, tol=12)

    sol = solve(build_model(p), p, time_limit=30, workers=8)

    assert sol.status == "OPTIMAL"
    assert sorted(overtime(sol.assignment)) == [0, 0] + [12] * 8


# ---- the share follows the hours owed; the balance, in and out --------------------------------------
def february(nurses: list[Nurse], **over) -> Problem:
    """February 2026, 28 days from a Sunday: four Fridays, 24 days owed at 8 h, 192 h a nurse. No headcount: the grids
    below are read, not solved."""
    p = week(nurses, headcount_enabled=False, **over)
    p.days, p.first_weekday, p.month = 28, 6, "2026-02"
    return p


FRIDAYS = [5, 12, 19, 26]


def worked(n: int, *, skip: tuple[int, ...] = ()) -> list[int]:
    """The first `n` days that are neither a Friday nor skipped, a day shift each."""
    days = [d for d in range(28) if d not in FRIDAYS and d not in skip][:n]
    return [W if d in days else OFF for d in range(28)]


def feb_grid() -> list[list[int]]:
    """a 18 shifts (216 h, 24 over); b, on leave 2-4 February (owes 168 h), and c, breastfeeding (24 x 7 = 168 h), 14 shifts
    each, none over; d, on leave every day she owes (owes none), works two Fridays: 24 h over. O = 48, R = 192 + 168 + 168."""
    d = [W if day in FRIDAYS[:2] else OFF for day in range(28)]
    return [worked(18), worked(14, skip=(1, 2, 3)), worked(14), d]


def feb_nurses(**a) -> list[Nurse]:
    owed = [day for day in range(28) if day not in FRIDAYS]
    return [Nurse("a", **a), Nurse("b", unavailable=[1, 2, 3]), Nurse("c", breastfeeding=True),
            Nurse("d", unavailable=owed)]


def balances(p: Problem, grid: list[list[int]]) -> dict[str, float]:
    return {i: v["carry_over"]["overtime_balance"] for i, v in nurse_accounting(p, grid).items()}


def test_a_share_follows_the_hours_owed_and_the_balance_out_counts_it():
    """Shares: a 48 x 192 / 528 = 17.45 h; b (leave) and c (reduced hours) 48 x 168 / 528 = 15.27 h; d, owing none, 0.
    Balance out = in + overtime - share: a 24 - 17.45 = 6.55, b and c -15.27, d 24. The unit does not carry fairness: the
    balance still moves."""
    p = february(feb_nurses(), carry=False)

    assert balances(p, feb_grid()) == {"a": 6.55, "b": -15.27, "c": -15.27, "d": 24.0}


def test_a_nurse_who_owes_no_hours_is_not_priced():
    """With nothing free: a 24 - 17.45 = 6.55 h above her share, 6 priced; d's 24 h are not priced, as she owes none."""
    assert priced(february(feb_nurses(), tol=0), feb_grid()) == {"a": 6}


def test_the_balance_in_is_carried_on_and_moved():
    p = february(feb_nurses(overtime_balance=5.0), carry=True)

    assert balances(p, feb_grid())["a"] == 11.55                    # 5 + 24 - 17.45


@pytest.mark.parametrize("over", [{"share": False}, {"hours_rule": False}])
def test_the_balance_is_handed_on_unchanged_with_the_switch_or_the_hours_rule_off(over):
    p = february(feb_nurses(overtime_balance=5.0), **over)

    assert balances(p, feb_grid()) == {"a": 5.0, "b": 0.0, "c": 0.0, "d": 0.0}


def test_the_balance_moves_with_the_weight_at_zero():
    """The weight is the hospital's price; the unit's switch is what moves the balance, as the night balance moves."""
    assert balances(february(feb_nurses(), w=0), feb_grid())["a"] == 6.55


@pytest.mark.parametrize("balance, cap, carry, cost", [
    (0.0, 12, True, 4 + 4),     # a and b each 12 over a share of 8
    (-6.0, 12, True, 0 + 4),    # a's target 8 + 6 = 14: her 12 h are below it
    (3.0, 12, True, 7 + 4),     # target 5: 7 above it
    (30.0, 2, True, 6 + 4),     # held at the limit of 2: target 6, 6 above
    (30.0, 12, False, 4 + 4),   # the unit does not carry fairness: the balance moves nothing
])
def test_a_carried_balance_moves_the_target_within_the_limit(balance, cap, carry, cost):
    p = week([Nurse("a", overtime_balance=balance), Nurse("b"), Nurse("c")], tol=0, cap=cap, carry=carry)

    assert penalises(p, SPREAD, RULE) == cost


# ---- the caps, a re-plan, and what turns it off ------------------------------------------------------
def test_the_hard_yearly_cap_still_binds():
    """a has 720 h of overtime this year: none is allowed. Her carried balance (24 h below her share) would make overtime
    free for her, but the cap is hard: b and c take the two extra shifts."""
    p = week([Nurse("a", ytd_overtime=720, overtime_balance=-24.0), Nurse("b"), Nurse("c")], tol=0, cap=24, carry=True)

    sol = solve(build_model(p), p, time_limit=10, workers=4)

    assert sol.status == "OPTIMAL" and extras(sol.assignment) == [0, 1, 1] and not explain(p, sol.assignment).errors


def test_the_hard_monthly_cap_still_binds():
    """a's balance (24 h below her share) makes both extra shifts free for her; the monthly cap of 12 h allows her one."""
    p = week([Nurse("a", overtime_balance=-24.0), Nurse("b"), Nurse("c")], tol=0, cap=24, carry=True)
    p.general.overtime_month_cap = 12

    sol = solve(build_model(p), p, time_limit=10, workers=4)

    assert sol.status == "OPTIMAL" and max(extras(sol.assignment)) == 1 and not explain(p, sol.assignment).errors


def test_in_a_re_plan_the_elapsed_days_count():
    """Published: a works days 0-4, b days 0-2 and 5-6, c days 3-6. Re-planned from day 5, a's five recorded shifts already
    hold 12 h over her share of 8: 4 h priced, though her overtime lies wholly in the elapsed days, and the solver gives the
    free days to b and c rather than add a second extra shift to her."""
    p = week([Nurse("a"), Nurse("b"), Nurse("c")], tol=0)
    plan = [row("WWWWW.."), row("WWW..WW"), row("...WWWW")]
    p.published = [["D" if v >= 0 else "" for v in cells] for cells in plan]
    p.replan_from(5)

    sol = solve(build_model(p), p, time_limit=10, workers=4)

    assert sol.status == "OPTIMAL" and sol.solver_objective == sol.objective == 24 + 8
    assert extras(sol.assignment) == [1, 1, 0] and priced(p, sol.assignment) == {"a": 4, "b": 4}


@pytest.mark.parametrize("over", [{"w": 0}, {"share": False}, {"hours_rule": False}])
def test_the_weight_at_zero_the_switch_off_and_the_hours_rule_off_each_price_nothing(over):
    p = week([Nurse("a"), Nurse("b"), Nurse("c")], tol=0, **over)

    assert not active(p, RULE)
    assert penalises(p, PILE, RULE) == 0 and explain(p, PILE).rules[RULE].matches == []


# ---- the words ---------------------------------------------------------------------------------------
def test_the_match_names_the_nurse_her_overtime_her_share_and_the_hours_priced():
    p = week([Nurse("a"), Nurse("b"), Nurse("c")], w=3)

    an = explain(p, PILE)

    [m] = an.rules[RULE].matches
    assert (m.nurse, m.day, m.amount, m.text) == ("a", None, 4, "a: 24 h overtime, share 8.0 h, 4 h priced")
    assert an.nurses["a"] == 24 + 12                     # her own overtime and this rule's 4 h at 3
    assert f"{RULE} 12 = 4 x 3  (hour of overtime above the share)" in report(an)


def test_the_match_names_the_target_when_the_carried_balance_moves_it():
    p = week([Nurse("a", overtime_balance=3.0), Nurse("b"), Nurse("c")], tol=0, carry=True)

    [m] = [m for m in explain(p, SPREAD).rules[RULE].matches if m.nurse == "a"]
    assert m.text == "a: 12 h overtime, share 8.0 h, target 5.0 h after the carried balance, 7 h priced"


# ---- the request -------------------------------------------------------------------------------------
def sample(name: str) -> dict:
    """"v1" is the four-nurse sample written as version 1."""
    if name == "v1":
        return version_1(json.loads((FIXTURES / "four_nurses.json").read_text()))
    return json.loads((FIXTURES / name).read_text())


SAMPLES = ["v1", "four_nurses.json", "four_nurses_skills.json"]      # versions 1 and 2


@pytest.mark.parametrize("name", SAMPLES)
def test_an_input_without_the_fields_has_the_rule_off_and_every_balance_at_zero(name):
    """As one stored before the rule: four_nurses.json carries the unit's three fields and the weight, so they are
    taken out here."""
    req = sample(name)
    for field in ("share_overtime", "overtime_tolerance", "overtime_carry_cap"):
        req["department"].pop(field, None)
    req["general"]["weights"].pop("overtime_above_share", None)
    before = copy.deepcopy(req)

    p = load_request(req).problem

    assert req == before                                              # the stored input is not touched
    dep = p.department
    assert (dep.share_overtime, dep.overtime_tolerance, dep.overtime_carry_cap) == (False, 0, 0)
    assert p.general.weights.overtime_above_share == 0
    assert {nu.overtime_balance for nu in p.nurses} == {0.0}
    assert not active(p, RULE)


@pytest.mark.parametrize("name", SAMPLES)
def test_either_version_takes_the_three_fields_the_weight_and_the_balance(name):
    req = sample(name)
    req["department"].update(share_overtime=True, overtime_tolerance=12, overtime_carry_cap=744)
    req["general"]["weights"]["overtime_above_share"] = 1
    req["nurses"][0]["overtime_balance"] = -3.25

    p = load_request(req).problem

    dep = p.department
    assert (dep.share_overtime, dep.overtime_tolerance, dep.overtime_carry_cap) == (True, 12, 744)
    assert p.general.weights.overtime_above_share == 1 and p.nurses[0].overtime_balance == -3.25
    assert active(p, RULE)


def hours_text(field: str, value) -> str:
    return f"request: department.{field} must be a whole number of hours within 0..744, got {value!r}"


@pytest.mark.parametrize("name", SAMPLES)
@pytest.mark.parametrize("path, value, text", [
    *[(["department", f], v, hours_text(f, v)) for f in ("overtime_tolerance", "overtime_carry_cap")
      for v in (-1, 745, 2.5, 3.0, True, "3")],
    (["department", "share_overtime"], 1, "request: department.share_overtime must be true or false, got 1"),
    (["department", "share_overtime"], "yes", "request: department.share_overtime must be true or false, got 'yes'"),
    (["general", "weights", "overtime_above_share"], -1, "request: general.weights.overtime_above_share must be an integer within"),
    (["general", "weights", "overtime_above_share"], True, "request: general.weights.overtime_above_share must be an integer within"),
    (["nurses", 0, "overtime_balance"], True, "request: nurses[0].overtime_balance: must be a number of hours within ±1000000, got True"),
    (["nurses", 0, "overtime_balance"], "1", "request: nurses[0].overtime_balance: must be a number of hours within ±1000000, got '1'"),
    (["nurses", 0, "overtime_balance"], 1000000.5, "request: nurses[0].overtime_balance: must be a number of hours within ±1000000, got 1000000.5"),
    (["nurses", 0, "overtime_balance"], float("nan"), "request: nurses[0].overtime_balance: must be a number of hours within ±1000000, got nan"),
])
def test_a_bad_value_is_refused_with_its_field(name, path, value, text):
    req = sample(name)
    node = req
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value

    with pytest.raises(ValueError) as e:
        load_request(req)
    assert str(e.value).startswith(text)


def test_a_problem_file_must_carry_the_rule_fields_and_may_leave_out_the_balance():
    """The problem file (schema 7) names every rule field, as every schema since 3; a nurse's carried balance is optional
    there as her count of preferences not kept is (absent = 0)."""
    for block, key in (("department", "share_overtime"), ("department", "overtime_tolerance"),
                       ("department", "overtime_carry_cap")):
        d = raw()
        del d[block][key]
        with pytest.raises(ValueError, match=rf"{block}: missing field\(s\) \['{key}'\]"):
            Problem.from_dict(d)
    d = raw()
    del d["general"]["weights"]["overtime_above_share"]
    with pytest.raises(ValueError, match=r"general.weights: missing field\(s\) \['overtime_above_share'\]"):
        Problem.from_dict(d)
    assert Problem.from_dict(raw()).nurses[0].overtime_balance == 0.0
