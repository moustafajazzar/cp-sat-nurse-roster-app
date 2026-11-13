"""Probation: a nurse on
probation counts toward nothing about a shift, is left out of the fairness, the overtime share and the unit's overtime
totals, works no night shift, works under one shift of overtime, and follows her preceptor (the hard `following` rule).
Small months where the rule alone decides the roster; expected values counted by hand, never recomputed as the code does."""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from nurse_roster.explain import explain
from nurse_roster.model import build_model, fingerprint, solve
from nurse_roster.precheck import precheck
from nurse_roster.problem import Headcount, Nurse, Problem, SkillNeed
from nurse_roster.request import load_request
from nurse_roster.result import nurse_accounting
from nurse_roster.rules import HARD, active, level
from nurse_roster.validate import validate
from tests.rules_helper import violates
from tests.test_core import hours_general, mini, quiet

FIXTURES = Path(__file__).parents[1] / "service_tests" / "fixtures"
OFF = -1


def sample() -> dict:
    return json.loads((FIXTURES / "four_nurses.json").read_text())


def grid(p: Problem, rows: dict[str, str]) -> list[list[int]]:
    """{"a": "D.AN"} -> the matrix, "." off; a nurse not listed is off every day."""
    idx = {s: i for i, s in enumerate(p.department.shifts)}
    out = []
    for nu in p.nurses:
        text = rows.get(nu.id, "." * p.days)
        assert len(text) == p.days, nu.id
        out.append([OFF if c == "." else idx[c] for c in text])
    return out


def cells(p: Problem, a: list[list[int]], nid: str) -> str:
    """A nurse's row back as text, "." off."""
    n = next(i for i, nu in enumerate(p.nurses) if nu.id == nid)
    return "".join("." if v < 0 else p.department.shifts[v] for v in a[n])


def solved(p: Problem):
    sol = solve(build_model(p), p, time_limit=20, workers=4)
    assert sol.status == "OPTIMAL", sol.message
    assert not sol.hard_errors and not sol.mismatch, sol.hard_errors or sol.mismatch
    assert sol.objective == sol.solver_objective                # the model and the validator agree with her in it
    return sol


def pins(text: str) -> dict[int, str]:
    """"DA.N" -> every day pinned: a shift, or off for "."."""
    return {d: ("" if c == "." else c) for d, c in enumerate(text)}


# ---- the loader --------------------------------------------------------------------------------------------------
def test_absent_fields_mean_not_on_probation_and_no_preceptor_and_the_input_gives_what_it_gave():
    raw = sample()
    explicit = copy.deepcopy(raw)
    for nurse in explicit["nurses"]:
        nurse["on_probation"], nurse["preceptor"] = False, None

    before, after = load_request(raw).problem, load_request(explicit).problem

    assert all(not nu.on_probation and nu.preceptor is None for nu in before.nurses)
    assert fingerprint(before) == fingerprint(after)
    assert not active(before, "following")


def test_the_loader_takes_both_fields():
    raw = sample()
    raw["nurses"][2]["on_probation"], raw["nurses"][2]["preceptor"] = True, raw["nurses"][0]["id"]
    raw["nurses"][3]["on_probation"] = True                    # on probation with no preceptor: allowed

    p = load_request(raw).problem

    assert (p.nurses[2].on_probation, p.nurses[2].preceptor) == (True, raw["nurses"][0]["id"])
    assert (p.nurses[3].on_probation, p.nurses[3].preceptor) == (True, None)
    assert level(p, "following") == HARD and active(p, "following")


def _refused(change) -> str:
    raw = sample()
    change(raw["nurses"])
    with pytest.raises(ValueError) as e:
        load_request(raw)
    return str(e.value)


def _ids() -> list[str]:
    return [n["id"] for n in sample()["nurses"]]


def test_the_loader_refuses_a_preceptor_that_is_not_another_nurse_of_the_input():
    def unknown(ns):
        ns[2].update(on_probation=True, preceptor="nobody")

    def herself(ns):
        ns[2].update(on_probation=True, preceptor=ns[2]["id"])

    assert _refused(unknown) == "request: nurses[2].preceptor: 'nobody' is not another nurse of the input"
    assert _refused(herself) == f"request: nurses[2].preceptor: {_ids()[2]!r} is not another nurse of the input"


def test_the_loader_refuses_a_preceptor_on_probation_named_twice_or_on_a_nurse_not_on_probation():
    a, b = _ids()[0], _ids()[1]

    def on_probation(ns):
        ns[0]["on_probation"] = True
        ns[2].update(on_probation=True, preceptor=a)

    def twice(ns):
        ns[2].update(on_probation=True, preceptor=a)
        ns[3].update(on_probation=True, preceptor=a)

    def not_on_probation(ns):
        ns[2]["preceptor"] = b

    assert _refused(on_probation) == f"request: nurses[2].preceptor: {a!r} is herself on probation"
    assert _refused(twice) == f"request: nurses[3].preceptor: {a!r} already precepts nurses[2] (one nurse on probation each)"
    assert _refused(not_on_probation) == f"request: nurses[2].preceptor: {b!r} is named for a nurse not on probation"


@pytest.mark.parametrize("field, value, words", [
    ("on_probation", "yes", "request: nurses[1].on_probation: must be true or false, got 'yes'"),
    ("on_probation", 1, "request: nurses[1].on_probation: must be true or false, got 1"),
    ("preceptor", 7, "request: nurses[1].preceptor: must be a nurse id or null, got 7"),
])
def test_the_loader_refuses_fields_of_the_wrong_type(field, value, words):
    assert _refused(lambda ns: ns[1].__setitem__(field, value)) == words


def test_a_problem_file_carries_the_fields_and_refuses_the_same_by_id(tmp_path):
    p = mini(3, ["D", "N"], [Nurse("q"), Nurse("a", on_probation=True, preceptor="q")])
    f = tmp_path / "p.json"
    p.to_json(f)
    assert Problem.from_json(f) == p

    raw = json.loads(f.read_text())
    raw["nurses"][1]["preceptor"] = "a"
    with pytest.raises(ValueError, match="problem: a: preceptor 'a' is not another nurse of the input"):
        Problem.from_dict(raw)
    raw["nurses"][1]["preceptor"], raw["nurses"][1]["on_probation"] = "q", False
    with pytest.raises(ValueError, match="problem: a: preceptor 'q' is named for a nurse not on probation"):
        Problem.from_dict(raw)


# ---- she counts toward nothing about a shift ---------------------------------------------------------------------
def test_a_headcount_minimum_is_not_met_by_her_and_a_maximum_not_exceeded_by_her():
    """One day, D: 1..1. b alone is the one; a beside her adds nothing; a alone leaves the minimum unmet."""
    p = mini(1, ["D", "N"], [Nurse("a", on_probation=True), Nurse("b")], headcount_enabled=True,
             headcounts=[Headcount("D", 1, 1, 1, 1)])

    assert violates(p, grid(p, {"a": "D", "b": "D"}), "headcount") == []
    assert violates(p, grid(p, {"a": "D"}), "headcount") == ["headcount: day 0 D = 0, want 1..1"]


def test_a_shift_needing_one_nurse_on_a_day_only_she_can_work_has_no_roster():
    """b is on leave on day 1: a could work it, but a shift is staffed without her, so the month is infeasible, and
    precheck says why."""
    p = mini(3, ["D", "N"], [Nurse("a", on_probation=True), Nurse("b", unavailable=[1])], headcount_enabled=True,
             headcounts=[Headcount("D", 1, 1, 1, 1), Headcount("N", 0, 0, 0, 0)])

    assert solve(build_model(p), p, time_limit=10, workers=4).status == "INFEASIBLE"
    assert "day 1: headcounts need 1 nurses but only 0 are not on leave" in precheck(p)


def test_she_is_no_team_leader_no_second_gender_and_meets_no_skill_need():
    p = mini(1, ["D", "N"], [Nurse("a", on_probation=True, team_leader=True, gender="M", skills={"ICU": None}),
                             Nurse("b", gender="F")],
             headcounts=[Headcount("D", 0, 9, 0, 9)], require_team_leader=True, require_both_genders=True,
             skill_needs=[SkillNeed("D", "ICU", 1, 1)])
    a = grid(p, {"a": "D", "b": "D"})

    assert violates(p, a, "team_leader") == ["team leader: day 0 D has none"]
    assert violates(p, a, "gender") == ["gender: day 0 D has only {'F'}"]
    assert violates(p, a, "skill_need") == ["skill need: day 0 D has 0 holding ICU, needs 1 (1 short)"]


def test_her_score_lifts_no_shift():
    """Target 0.80; b scores 0.50 and works D alone: 80 - 50 = 30 short. a, 1.00, beside her: still 30 (counted, the
    two would average 0.75, 160 - 150 = 10). a alone on D is no shift to score."""
    p = mini(1, ["D", "N"], [Nurse("a", on_probation=True, score=1.0), Nurse("b", score=0.5)],
             weights=replace(quiet(), score_deficit=1), headcounts=[Headcount("D", 0, 9, 0, 9)], score_target=0.8,
             score_target_weekend=0.8)

    assert explain(p, grid(p, {"b": "D"})).rules["score_deficit"].raw == 30
    assert explain(p, grid(p, {"a": "D", "b": "D"})).rules["score_deficit"].raw == 30
    assert explain(p, grid(p, {"a": "D"})).rules["score_deficit"].raw == 0
    p.nurses[1].pinned = {0: "D"}                               # b on D: the solver cannot lift her with a
    assert solved(p).objective == 30


def _fair_month(with_her: bool) -> tuple[Problem, list[list[int]]]:
    """Seven days from Saturday (the Friday last), D and N fair, the weekend fair, the overtime share on: b and c share
    the D and N shifts, 24 h over the 48 h they owe each; a on probation, breastfeeding (42 h owed), works four D shifts
    beside them, 6 h over."""
    nurses = [Nurse("b", night_balance=1.0, friday_balance=-1.0), Nurse("c")]
    rows = {"b": "DDNN.DD", "c": "NN.DDNN"}
    if with_her:
        nurses.append(Nurse("a", on_probation=True, breastfeeding=True, night_balance=2.0, friday_balance=1.0,
                            overtime_balance=5.0))
        rows["a"] = "DD.DD.."
    p = mini(7, ["D", "N"], nurses, first_weekday=5, headcount_enabled=False,
             headcounts=[Headcount("D", 1, 2, 1, 2), Headcount("N", 1, 1, 1, 1)], fair_shifts=["D", "N"],
             fair_weekends=True, carry_fairness=True, carry_cap=2.0, share_overtime=True, overtime_tolerance=0,
             overtime_carry_cap=12)
    p.general = hours_general(dept_overtime_cap=0, weights=replace(
        quiet(), fair_day=1, fair_night=1, fair_weekend=1, overtime_above_share=1, dept_overtime=1, nurse_overtime=1))
    return p, grid(p, rows)


def test_the_fairness_figures_the_overtime_share_and_the_units_overtime_are_the_same_with_and_without_her():
    alone, beside = (explain(*_fair_month(w)) for w in (False, True))

    for rid in ("fair_day", "fair_night", "fair_weekend", "overtime_above_share", "dept_overtime"):
        assert [m.text for m in beside.rules[rid].matches] == [m.text for m in alone.rules[rid].matches], rid
        assert beside.rules[rid].raw == alone.rules[rid].raw, rid
    assert alone.rules["dept_overtime"].raw > 0                  # b and c's 24 h over each are the unit's overtime
    assert beside.rules["nurse_overtime"].raw == alone.rules["nurse_overtime"].raw + 6    # hers: her own record


def test_her_carried_fairness_balances_are_zero_out():
    """April 2026 with D and N fair, the weekend fair and the overtime share on: she comes in carrying 2 nights, 1 Friday
    and 5 h above her shares, works 18 D shifts (four of them Fridays) and 8 h over, and carries 0, 0 and 0 out."""
    p = _april(["D", "N"], headcounts=[Headcount("D", 1, 1, 1, 1), Headcount("N", 1, 1, 1, 1)], fair_shifts=["D", "N"],
               fair_weekends=True, share_overtime=True)
    p.nurses[0] = replace(p.nurses[0], night_balance=2.0, friday_balance=1.0, overtime_balance=5.0)

    carry = nurse_accounting(p, grid(p, {"a": "D" * 18 + "." * 12}))["a"]["carry_over"]

    assert (carry["night_balance"], carry["friday_balance"], carry["overtime_balance"]) == (0.0, 0.0, 0.0)


# ---- no night shift ------------------------------------------------------------------------------------------------
def test_her_allowed_shifts_are_narrowed_by_the_night_shift_and_kept_as_set():
    a = Nurse("a", on_probation=True, allowed_shifts=["A", "N"])
    p = mini(3, ["D", "A", "N"], [Nurse("q"), a, Nurse("b", on_probation=True)])

    assert p.allowed(a) == ["A"] and a.allowed_shifts == ["A", "N"]
    assert p.allowed(p.nurses[2]) == ["D", "A"]
    assert violates(p, grid(p, {"a": "N..", "b": ".N."}), "allowed_shifts") == [
        "shift restriction: a works N on day 0", "shift restriction: b works N on day 1"]


def test_her_allowed_shifts_set_to_the_night_shift_alone_are_refused():
    p = mini(3, ["D", "N"], [Nurse("a", on_probation=True, allowed_shifts=["N"])])

    with pytest.raises(ValueError, match="a: on probation she works no night shift \\('N'\\), and her allowed shifts hold no other"):
        p.check()


def test_a_night_pin_on_a_free_day_of_hers_is_refused_and_an_elapsed_night_is_a_fact():
    refused = mini(3, ["D", "N"], [Nurse("a", on_probation=True, pinned={1: "N"})])
    with pytest.raises(ValueError, match="a: pinned shift 'N' on day 1 is the night shift, which a nurse on probation does not work"):
        refused.check()

    elapsed = mini(3, ["D", "N"], [Nurse("a", on_probation=True, pinned={0: "N", 1: ""})])
    elapsed.planning_from = 2
    elapsed.check()


def test_the_solver_gives_her_no_night_when_she_would_work_every_day():
    p = mini(4, ["D", "N"], [Nurse("a", on_probation=True, desired_days=[0, 1, 2, 3])],
             weights=replace(quiet(), desired_day=1))

    assert cells(p, solved(p).assignment, "a") == "DDDD"


# ---- hours ---------------------------------------------------------------------------------------------------------
def _april(shifts: list[str], **dep) -> Problem:
    """April 2026 (30 days from a Wednesday, four Fridays): 26 days owed at 8 h, 208 h. She asks to work every day, at
    100 a day; an hour of overtime costs 1, the month's cap is 40 h. Nothing else is on."""
    a = Nurse("a", on_probation=True, desired_days=list(range(30)), ytd_overtime=100)
    p = mini(30, shifts, [a], first_weekday=2, **dep)
    p.general = hours_general(weights=replace(quiet(), avoidable_shortfall=1000, nurse_overtime=1, desired_day=100))
    p.month = "2026-04"
    return p


def test_with_12_hour_shifts_she_works_18_shifts_8_hours_over_and_never_a_19th():
    """208 h owed + her 12 h shift = 220: she works under it, 18 shifts (216 h, 8 h over); a 19th would be 228 h, 20 h over,
    within the month's 40 h cap of a counted nurse (20 shifts, 240 h)."""
    p = _april(["D", "N"])

    sol = solved(p)
    row = cells(p, sol.assignment, "a")

    assert row.count("D") == 18 and "N" not in row
    nurse = nurse_accounting(p, sol.assignment)["a"]
    assert (nurse["required"], nurse["hours_worked"], nurse["overtime"]) == (208, 216, 8)
    assert nurse["carry_over"]["ytd_overtime"] == 108                # her overtime for the year
    assert violates(p, grid(p, {"a": "D" * 19 + "." * 11}), "hours_bounds") == [
        "overtime: a 20 > cap 11 (on probation: under one shift)"]


def test_with_8_hour_shifts_she_works_26_shifts_and_none_over():
    """A (8 h) and C (8 h), C the night: 208 h + 8 = 216, so 26 shifts (208 h), none over; a 27th is 8 h over a cap of 7.
    A counted nurse would work all 30 days (240 h, 32 h over, within the 40 h cap)."""
    p = _april(["A", "C"], night_shift="C", day_shift="A")

    row = cells(p, solved(p).assignment, "a")

    assert row.count("A") == 26 and "C" not in row


def test_she_may_end_under_by_less_than_a_shift_and_carries_the_debt():
    """17 shifts of 12 h: 204 h, 4 short of 208, under her shortest shift: no hard error, nothing avoidable, 4 carried."""
    p = _april(["D", "N"])
    a = grid(p, {"a": "D" * 17 + "." * 13})

    errors, penalties, _ = validate(p, a)
    assert not errors and penalties["avoidable_shortfall"] == 0
    assert nurse_accounting(p, a)["a"]["carry_over"]["hours_balance"] == 4


def test_her_rounding_overtime_is_outside_the_department_cap():
    """The department may carry no overtime (cap 0, 1 an hour): her 8 h cost the hour of overtime only."""
    p = _april(["D", "N"])
    p.general = replace(p.general, dept_overtime_cap=0, weights=replace(p.general.weights, dept_overtime=1))
    a = grid(p, {"a": "D" * 18 + "." * 12})

    assert explain(p, a).rules["dept_overtime"].raw == 0 and explain(p, a).rules["nurse_overtime"].raw == 8


# ---- following -----------------------------------------------------------------------------------------------------
def _pair(q_row: str, *, days: int | None = None, first_weekday: int = 0, a: dict | None = None,
          hours: bool = False, **dep) -> Problem:
    """q precepts a: q's month pinned to `q_row` ("." off), a free; shifts D (12 h), A (8 h) and N, N the night. a asks
    to work every day (1 a day): only the rules decide where she is off."""
    days = days or len(q_row)
    nurse = Nurse("a", on_probation=True, preceptor="q", desired_days=list(range(days)), **(a or {}))
    p = mini(days, ["D", "A", "N"], [Nurse("q", pinned=pins(q_row)), nurse], first_weekday=first_weekday, **dep)
    if hours:
        p.general = hours_general(weights=replace(quiet(), avoidable_shortfall=1000, nurse_overtime=1, desired_day=1))
    else:
        p.general = replace(p.general, weights=replace(quiet(), desired_day=1))
    return p


def test_on_every_followable_day_she_is_on_her_preceptors_shift():
    """q works D, A, N, off, D, A, D. Followable: days 0, 1, 4, 5, 6 (N is not hers). With nothing else on she follows
    each, and works days 2 and 3 on a day shift of her own (no followable day was left off)."""
    p = _pair("DAN.DAD")

    row = cells(p, solved(p).assignment, "a")

    assert row[0:2] + row[4:] == "DADAD" and row[2] in "DA" and row[3] in "DA"


def test_another_shift_on_a_followable_day_is_a_violation_in_words_naming_both():
    p = _pair("DAN.DAD")

    assert violates(p, grid(p, {"q": "DAN.DAD", "a": "AAAADAD"}), "following") == [
        "following: a works A on day 0, her preceptor q works D"]
    match = explain(p, grid(p, {"q": "DAN.DAD", "a": "AAAADAD"})).rules["following"].matches[0]
    assert (match.nurse, match.day, match.elapsed) == ("a", 0, False)


def test_hours_win_and_she_then_works_no_day_her_preceptor_does_not_give_her():
    """Seven days from Saturday (Friday last): 48 h owed, 12 h shifts, under 60 h: at most 4 shifts. q works D on six days
    and is off on the Monday (day 2): she follows four of the six, and is off on the Monday too, though she asked to work."""
    p = _pair("DD.DDDD", first_weekday=5, hours=True)

    row = cells(p, solved(p).assignment, "a")

    assert row.count("D") == 4 and set(row) <= {"D", "."} and row[2] == "."
    assert violates(p, grid(p, {"q": "DD.DDDD", "a": "DDD.D.."}), "following") == [
        "following: a works D on day 2, when her preceptor q is off, while she is off on day 3, when q works D"]


def test_with_every_followable_day_followed_she_fills_her_hours_on_other_days():
    """q works D only on days 0 and 1: she follows both (24 h) and fills the other 24 h she owes on days of her own,
    two D or three A: 48 h, none over."""
    p = _pair("DD.....", first_weekday=5, hours=True)

    sol = solved(p)
    row = cells(p, sol.assignment, "a")

    assert row[:2] == "DD" and "N" not in row
    assert 12 * row.count("D") + 8 * row.count("A") == 48


def test_a_pin_and_leave_hold_over_following():
    """Her accepted day off on day 0 and her leave on day 4, both days q works D; a pin of hers to A on day 1, when q
    works D: all three hold, none is judged, so no followable day is left off and she still works days 2 and 3."""
    p = _pair("DD.DD..", a={"pinned": {0: "", 1: "A"}, "unavailable": [4]})

    row = cells(p, solved(p).assignment, "a")

    assert row[0] == "." and row[1] == "A" and row[4] == "."
    assert row[3] == "D" and row[2] != "." and row[5] != "." and row[6] != "."


def test_elapsed_days_are_facts_and_are_not_judged():
    """Days 0..2 elapsed: she worked A on two of q's D days. Nothing is reported; the free days follow."""
    p = _pair("DDDDDDD", a={"pinned": {0: "A", 1: "A", 2: ""}})
    p.planning_from = 3

    errors, _, _ = validate(p, grid(p, {"q": "DDDDDDD", "a": "AA.DDDD"}))
    assert errors == []
    assert cells(p, solved(p).assignment, "a") == "AA.DDDD"


def test_her_own_run_rule_applies_and_a_day_she_cannot_follow_leaves_her_off_the_day_her_preceptor_is_off():
    """At most three in a row; she worked D, D, D before the month, so day 0 must be off though q works D on it. That
    followable day left off, she works no day q does not give her: off on days 3, 5 and 6, when q is off."""
    p = _pair("DDD.D..", a={"previous": ["D", "D", "D"]}, max_consecutive_shifts=3)
    p.nurses[0].previous = ["", "", ""]

    assert cells(p, solved(p).assignment, "a") == ".DD.D.."
    assert violates(p, grid(p, {"q": "DDD.D..", "a": ".DD.D.."}), "following") == []


def test_her_successions_apply():
    """A then D is forbidden here, and she worked A the day before the month: q's D on day 0 cannot be hers. That
    followable day left off, she follows q's D on day 1 and works neither day q is off."""
    p = _pair("DD..", a={"previous": ["A"]})
    p.nurses[0].previous = [""]
    p.general = replace(p.general, forbidden_successions=[("A", "D")], forbidden_successions_enabled=True)

    assert cells(p, solved(p).assignment, "a") == ".D.."


def test_the_rule_never_makes_a_month_infeasible_on_its_own():
    """Both free, D: 1..1 and N: 1..1 each of four days, q and r the counted nurses: the month solves at no cost, she
    on q's shift on every day q works D, and on a D of her own on the others (no followable day left off)."""
    nurses = [Nurse("q"), Nurse("r"), Nurse("a", on_probation=True, preceptor="q", desired_days=[0, 1, 2, 3])]
    p = mini(4, ["D", "N"], nurses, weights=replace(quiet(), desired_day=1), headcount_enabled=True,
             headcounts=[Headcount("D", 1, 1, 1, 1), Headcount("N", 1, 1, 1, 1)])

    sol = solved(p)

    assert sol.objective == 0 and cells(p, sol.assignment, "a") == "DDDD"


def test_with_no_preceptor_she_is_rostered_on_freely_placed_day_shifts_and_precheck_names_her():
    p = mini(4, ["D", "N"], [Nurse("q"), Nurse("a", on_probation=True, desired_days=[0, 1, 2, 3])],
             weights=replace(quiet(), desired_day=1))

    assert not active(p, "following")
    assert cells(p, solved(p).assignment, "a") == "DDDD"
    assert "a: on probation with no preceptor, so she follows no one: she is rostered on freely placed day shifts, " \
           "counted toward nothing" in precheck(p)
