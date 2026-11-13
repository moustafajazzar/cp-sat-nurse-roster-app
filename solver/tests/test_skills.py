"""Skills: the skill need, nurses holding a skill on exactly a shift each day against the weekday or
weekend minimum, hard or soft by the unit's flag; and the bar, no shift after the expiry of a skill required to work, whose
hours stay owed and are carried as debt. Constraint-verifier style: tiny months where the rule alone decides, expected
numbers worked out by hand. Then the precheck's lines and the explanation's words, then the request's version 2 and its
reading of version 1."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from nurse_roster.__main__ import main
from nurse_roster import fairness as F
from nurse_roster.explain import explain, report
from nurse_roster.model import build_model, solve
from nurse_roster.precheck import precheck
from nurse_roster.problem import Headcount, Nurse, Problem, SkillNeed, barred_days
from nurse_roster.request import load_request
from nurse_roster.rules import HARD, SOFT, active, level
from nurse_roster.validate import validate
from solver_service.grid import nurses_of, validate_grid
from tests.er_fixture import request_of, version_1
from tests.rules_helper import only, penalises, violates
from tests.test_core import hours_general, mini, quiet

D, N, OFF = 0, 1, -1
FIXTURES = Path(__file__).parents[1] / "service_tests" / "fixtures"


def unit(nurses: list[Nurse], *, days: int = 1, needs=(SkillNeed("D", "ICU", 2, 1),), hard: bool = False,
         price: int = 5, required=(), first_weekday: int = 0) -> Problem:
    """Two shifts, every other rule off; the skill need priced at `price` a nurse short when soft."""
    p = mini(days, ["D", "N"], nurses, weights=only("skill_need", price), first_weekday=first_weekday,
             skill_needs=list(needs), skill_needs_hard=hard)
    p.general.required_skills = list(required)
    return p


def icu(*ids: str, last: int | None = None) -> list[Nurse]:
    return [Nurse(i, skills={"ICU": last}) for i in ids]


# ---- the skill need ------------------------------------------------------------------------------
@pytest.mark.parametrize("row, short", [
    ([D, D, OFF], 0),        # two holders on D: met
    ([D, N, D], 1),          # one holder on D; the other on N and a nurse without the skill on D count for nothing
    ([N, N, D], 2),          # no holder on D
])
def test_a_soft_need_pays_its_weight_per_nurse_short(row, short):
    p = unit(icu("a", "b") + [Nurse("c")])
    a = [[v] for v in row]

    assert penalises(p, a, "skill_need") == 5 * short
    assert violates(p, a, "skill_need") == [] and explain(p, a).errors == []


def test_a_hard_need_is_a_violation_per_shift_day_and_costs_nothing():
    p = unit(icu("a", "b"), days=2, hard=True)

    texts = violates(p, [[D, N], [N, N]], "skill_need")

    assert texts == ["skill need: day 0 D has 1 holding ICU, needs 2 (1 short)",
                     "skill need: day 1 D has 0 holding ICU, needs 2 (2 short)"]
    assert penalises(p, [[D, N], [N, N]], "skill_need") == 0
    assert violates(p, [[D, D], [D, D]], "skill_need") == []


def test_the_weekend_minimum_holds_on_a_weekend_day():
    p = unit(icu("a", "b"), days=5, needs=[SkillNeed("D", "ICU", 0, 1)])     # from a Monday: day 4 is the Friday

    assert explain(p, [[OFF] * 5, [OFF] * 5]).rules["skill_need"].matches[0].text == \
        "skill need: day 4 D has 0 holding ICU, needs 1 (1 short)"
    assert penalises(p, [[OFF] * 5, [OFF] * 5], "skill_need") == 5


def test_an_expiry_inside_the_month_counts_on_the_days_before_it_and_not_after():
    p = unit(icu("a", last=1), days=3, needs=[SkillNeed("D", "ICU", 1, 1)])

    an = explain(p, [[D, D, D]])

    assert [m.day for m in an.rules["skill_need"].matches] == [2] and an.total == 5


def test_a_skill_expired_before_the_month_counts_on_no_day():
    p = unit(icu("a", last=-1), days=2, needs=[SkillNeed("D", "ICU", 1, 1)])
    assert penalises(p, [[D, D]], "skill_need") == 10


def test_elapsed_days_are_facts_reported_apart():
    p = unit(icu("a", "b"), days=2, hard=True)
    for nu in p.nurses:
        nu.pinned = {0: "N"}
    p.planning_from = 1

    an = explain(p, [[N, D], [N, D]])

    assert an.errors == [] and an.elapsed == ["skill need: day 0 D has 0 holding ICU, needs 2 (2 short)"]
    soft = unit(icu("a", "b"), days=2)
    for nu in soft.nurses:
        nu.pinned = {0: "N"}
    soft.planning_from = 1
    m, = explain(soft, [[N, D], [N, D]]).rules["skill_need"].matches
    assert m.elapsed and m.amount == 2                                   # still priced, as every soft rule's past


def test_the_solver_meets_a_hard_need_and_prices_a_soft_one_as_the_validator_does():
    hard = unit(icu("a", "b") + [Nurse("c")], days=3, hard=True)
    sol = solve(build_model(hard), hard, time_limit=10, workers=4)
    assert sol.status == "OPTIMAL" and all(sol.assignment[n][d] == D for n in (0, 1) for d in range(3))

    soft = unit(icu("a") + [Nurse("c")], days=3, price=7)              # one holder for a minimum of 2
    sol = solve(build_model(soft), soft, time_limit=10, workers=4)
    assert sol.status == "OPTIMAL" and sol.objective == sol.solver_objective == 3 * 7
    assert validate(soft, sol.assignment)[2] == 21


def test_a_hard_need_no_roster_can_meet_is_infeasible():
    p = unit(icu("a") + [Nurse("c")], hard=True)
    assert solve(build_model(p), p, time_limit=10, workers=4).status == "INFEASIBLE"


# ---- the bar: no shift after a required skill's expiry -------------------------------------------
def barred(**skills) -> Problem:
    return unit([Nurse("a", skills=skills), Nurse("b")], days=3, needs=[], required=["BLS"])


def test_a_shift_after_a_required_skills_expiry_is_a_violation():
    p = barred(BLS=0)

    assert barred_days(p, p.nurses[0]) == [1, 2]
    assert violates(p, [[D, OFF, N], [D, D, D]], "skill_expired") == [
        "skill expired: a works N on day 2, BLS is required to work and its last day was day 0"]
    assert violates(p, [[D, OFF, OFF], [D, D, D]], "skill_expired") == []


def test_a_required_skill_expired_before_the_month_bars_every_day():
    p = barred(BLS=-1)
    assert violates(p, [[D, OFF, OFF], [OFF] * 3], "skill_expired") == [
        "skill expired: a works D on day 0, BLS is required to work and its last day was before the month"]


@pytest.mark.parametrize("skills", [{}, {"ACLS": 0}, {"BLS": None}, {"BLS": 2}])
def test_no_bar_without_a_record_for_a_skill_not_required_or_one_valid_all_month(skills):
    p = barred(**skills)                                          # no record; not required; no expiry; the last day
    assert not active(p, "skill_expired") and barred_days(p, p.nurses[0]) == []
    assert explain(p, [[D, D, D], [D, D, D]]).errors == []


def test_the_first_required_skill_to_expire_bars_from_the_day_after():
    p = barred(BLS=1, ALS=0)
    p.general.required_skills = ["BLS", "ALS"]
    assert barred_days(p, p.nurses[0]) == [1, 2]
    assert violates(p, [[OFF, D, OFF], [OFF] * 3], "skill_expired") == [
        "skill expired: a works D on day 1, ALS is required to work and its last day was day 0"]


def test_the_solver_keeps_her_off_and_carries_the_hours_her_days_cannot_hold_as_debt():
    """Seven days from a Monday, Friday the weekend: 6 working days of 8 h owe 48 h, in 12 h shifts. BLS lasts to
    day 1, so she can work days 0 and 1 only, 24 h: the other 24 h are unreachable, exempt from the avoidable
    shortfall, and carried over whole."""
    p = barred(BLS=1)
    p.days, p.nurses = 7, p.nurses[:1]
    p.general = hours_general(required_skills=["BLS"])
    assert (p.required_hours(p.nurses[0]), p.max_hours(p.nurses[0]), p.unreachable_hours(p.nurses[0])) == (48, 24, 24)

    sol = solve(build_model(p), p, time_limit=10, workers=4)

    assert sol.status == "OPTIMAL" and sol.objective == 0 and sol.hard_errors == []
    row = sol.assignment[0]
    assert row[2:] == [OFF] * 5 and all(v >= 0 for v in row[:2])
    assert p.carry_over(p.nurses[0], 24) == 24                   # the result's carry_over.hours_balance


def test_without_the_bar_the_same_idle_days_are_an_avoidable_shortfall():
    p = barred(BLS=1)
    p.days, p.nurses = 7, p.nurses[:1]
    p.general = hours_general(required_skills=[])                  # not required: no bar
    an = explain(p, [[D, D, OFF, OFF, OFF, OFF, OFF]])
    assert an.rules["avoidable_shortfall"].raw == 48 - 24 - 11     # beyond a remainder below one 12 h shift


def test_a_pin_on_a_barred_free_day_is_refused_and_an_elapsed_one_is_a_fact():
    p = barred(BLS=0)
    p.nurses[0].pinned = {2: "D"}
    with pytest.raises(ValueError, match="problem: a: day 2 is after the expiry of 'BLS', required to work, but pinned to 'D'"):
        p.check()

    p.nurses[0].pinned = {0: "D", 1: "D", 2: ""}                   # everything happened: day 1 was worked
    p.nurses[1].pinned = {0: "", 1: "", 2: ""}
    p.planning_from = 3
    p.check()
    an = explain(p, [[D, D, OFF], [OFF] * 3])
    assert an.errors == [] and an.elapsed == [
        "skill expired: a works D on day 1, BLS is required to work and its last day was day 0"]


def fridays(**nurses) -> Problem:
    """Fourteen days from a Monday: the Fridays are days 4 and 11. D needs 2 and N 1 every day, so the weekend slots are
    2 Fridays x 3 = 6, among five nurses 1.2 each (1200 thousandths). BLS is required to work."""
    team = [Nurse("A"), Nurse("B"), Nurse("C")] + [Nurse(i, **kw) for i, kw in nurses.items()]
    p = mini(14, ["D", "N"], team, weights=only("fair_weekend", 1), fair_weekends=True,
             headcounts=[Headcount("D", 2, 2, 2, 2), Headcount("N", 1, 1, 1, 1)], headcount_enabled=True)
    p.general.required_skills = ["BLS"]
    return p


def test_fairness_counts_a_barred_day_as_leave():
    """A nurse barred from the second Friday by her BLS (last day 6) is owed
    half the Fridays, as one on leave that Friday is, and carries no Friday debt for the Friday she could not work."""
    p = fridays(barred={"skills": {"BLS": 6}}, on_leave={"unavailable": [11]})
    barred, on_leave = p.nurses[3], p.nurses[4]

    assert F.friday_expected_m(p, p.nurses[0]) == 1200
    assert F.friday_expected_m(p, barred) == F.friday_expected_m(p, on_leave) == 600
    worked_the_first_friday = [OFF] * 4 + [D] + [OFF] * 9
    assert F.balances_out(p, barred, worked_the_first_friday)[1] == F.balances_out(p, on_leave, worked_the_first_friday)[1] == 0.4


def test_fairness_owes_no_friday_to_a_nurse_barred_all_month():
    p = fridays(barred={"skills": {"BLS": -1}})

    assert F.friday_expected_m(p, p.nurses[3]) == 0
    assert F.balances_out(p, p.nurses[3], [OFF] * 14)[1] == 0.0


def test_the_solver_prices_a_barred_nurses_fridays_as_the_validator_does():
    p = fridays(barred={"skills": {"BLS": 6}})

    sol = solve(build_model(p), p, time_limit=10, workers=4)

    assert sol.status == "OPTIMAL" and sol.hard_errors == []
    _, pen, total = validate(p, sol.assignment)
    assert total == sol.objective == pen["fair_weekend"]


# ---- the registry ----------------------------------------------------------------------------------
def test_the_registry_switches_both_rules_on_their_data():
    p = unit(icu("a"), hard=True)
    assert active(p, "skill_need") and level(p, "skill_need") == HARD and not active(p, "skill_expired")
    p.department.skill_needs_hard = False
    assert active(p, "skill_need") and level(p, "skill_need") == SOFT
    p.general.weights = quiet()                                    # soft at weight 0: off
    assert not active(p, "skill_need")
    p.department.skill_needs = []
    p.department.skill_needs_hard = True
    assert not active(p, "skill_need")


# ---- precheck --------------------------------------------------------------------------------------
def test_precheck_names_a_need_no_nurse_can_meet():
    p = unit([Nurse("a", skills={"ICU": -1}), Nurse("b")], days=2, hard=True)    # expired before the month
    assert "skill need D/ICU: no nurse holds ICU on any free day" in precheck(p)


def test_precheck_compares_a_minimum_with_the_holders_who_may_work_the_shift():
    p = unit(icu("a", "b", "c"), days=2, needs=[SkillNeed("D", "ICU", 2, 2)], hard=True)
    p.nurses[1].allowed_shifts = ["N"]
    p.nurses[2].allowed_shifts = ["N"]
    assert "skill need D/ICU: needs 2 on some day but only 1 nurse(s) hold ICU and may work D" in precheck(p)


def test_precheck_counts_the_holders_each_day_against_leave_pins_and_the_bar():
    p = unit([Nurse("a", skills={"ICU": None, "BLS": 0}), Nurse("b", skills={"ICU": None}, unavailable=[2]),
              Nurse("c", skills={"ICU": 0})], days=3, needs=[SkillNeed("D", "ICU", 2, 2)], required=["BLS"], hard=True)
    p.nurses[1].pinned = {1: "N"}

    lines = precheck(p)

    assert [line for line in lines if line.startswith("day ")] == [
        f"day {d}: skill need D/ICU needs 2 but only {k} nurse(s) holding ICU may work D (not on leave, pinned "
        f"elsewhere or barred)" for d, k in ((1, 0), (2, 0))]
    assert "a: BLS, required to work, expires inside the month: its last day is day 0, so no shift after it" \
        in lines


def test_precheck_counts_barred_days_like_leave_in_the_capacity_sums():
    p = unit([Nurse("a", skills={"BLS": 0})], days=2, needs=[], required=["BLS"])
    p.department.headcounts, p.department.headcount_enabled = [Headcount("D", 1, 1, 1, 1)], True
    assert "day 1: headcounts need 1 nurses but only 0 are not on leave" in precheck(p)


# ---- explain words ---------------------------------------------------------------------------------
def test_the_explanation_lists_each_shortfall_with_the_shift_the_day_and_the_count():
    p = unit(icu("a", "b"), days=2)
    text = report(explain(p, [[D, N], [N, N]]))
    assert "skill_need 15 = 3 x 5  (nurse short of a skill need on a shift-day)" in text
    assert "skill need: day 1 D has 0 holding ICU, needs 2 (2 short)" in text


# ---- the request: version 2, and version 1 read as no skills ---------------------------------------
def sample(name: str = "four_nurses_skills.json") -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_version_2_carries_skills_needs_required_skills_and_the_weight():
    p = load_request(sample()).problem
    by = {n.id: n for n in p.nurses}
    assert by["Amal"].skills == {"BLS": None, "ICU": None} and by["Badr"].skills == {"BLS": None, "ICU": 19}
    assert by["Dana"].skills == {"BLS": 23} and by["Emad"].skills == {}
    assert p.general.required_skills == ["BLS"] and p.general.weights.skill_shortfall == 50
    assert p.department.skill_needs == [SkillNeed("N", "ICU", 1, 1)] and p.department.skill_needs_hard is False
    assert barred_days(p, by["Dana"]) == list(range(24, 30))


def test_an_expiry_before_the_month_is_read_as_expired_and_one_on_its_last_day_as_a_day():
    req = sample()
    req["nurses"][0]["skills"] = {"BLS": "1999-12-31", "ICU": "2026-06-30"}
    assert load_request(req).problem.nurses[0].skills == {"BLS": -1, "ICU": 29}


def test_version_1_loads_as_version_2_with_no_skills():
    v1 = version_1(sample("four_nurses.json"))
    before = copy.deepcopy(v1)

    p = load_request(v1).problem

    assert v1 == before                                            # the stored input is not touched
    assert p.general.required_skills == [] and p.general.weights.skill_shortfall == 0
    assert p.department.skill_needs == [] and p.department.skill_needs_hard is True
    assert all(n.skills == {} for n in p.nurses)
    assert not active(p, "skill_need") and not active(p, "skill_expired")


def test_the_er_sample_still_loads_and_a_request_written_today_is_version_2():
    er = json.loads((Path(__file__).parent / "fixtures" / "er_request_2026-03.json").read_text())
    assert er["version"] == 1
    p = load_request(er).problem
    req = request_of(p, "2026-03")
    assert req["version"] == 2 and load_request(req).problem == p


def _set(raw: dict, path: list, value) -> dict:
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
    (["version"], 3, "request: version must be 1 or 2, got 3"),
    (["general", "required_skills"], ..., r"request: general: missing field\(s\) \['required_skills'\]"),
    (["general", "weights", "skill_shortfall"], ..., r"request: general.weights: missing field\(s\) \['skill_shortfall'\]"),
    (["department", "skill_needs"], ..., r"request: department: missing field\(s\) \['skill_needs'\]"),
    (["department", "skill_needs_hard"], ..., r"request: department: missing field\(s\) \['skill_needs_hard'\]"),
    (["department", "skill_needs_hard"], "no", "request: department.skill_needs_hard must be true or false"),
    (["nurses", 0, "skills", "BLS"], "2026-07-01",
     r"request: nurses\[0\]\.skills\[BLS\]: 2026-07-01 is after 2026-06 \(a skill that counts all month has no expiry: null\)"),
    (["nurses", 0, "skills", "BLS"], "2026-06-31", r"request: nurses\[0\]\.skills\[BLS\]: 2026-06-31 is not a calendar date"),
    (["nurses", 0, "skills", "BLS"], 5, r"request: nurses\[0\]\.skills\[BLS\]: 5 is not a date"),
    (["nurses", 0, "skills"], ["BLS"], r"request: nurses\[0\]\.skills: must be an object"),
    (["nurses", 0, "skills", ""], None, r"request: Amal\.skills: '' is not a skill name"),
    (["general", "required_skills"], ["BLS", "BLS"], "request: general.required_skills: a skill is listed twice"),
    (["general", "required_skills"], "BLS", "request: general.required_skills must be a list"),
    (["department", "skill_needs", 0, "shift"], "X", r"request: department.skill_needs\[0\]: shift 'X' is not a department shift"),
    (["department", "skill_needs"], [{"shift": "N", "skill": "ICU", "weekday_min": 1, "weekend_min": 1}] * 2,
     r"request: department.skill_needs\[1\]: a second need for 'ICU' on 'N'"),
    (["department", "skill_needs", 0, "weekday_min"], -1, r"request: department.skill_needs\[0\].weekday_min must be an integer"),
    (["department", "skill_needs", 0, "extra"], 1, r"request: department.skill_needs\[0\]: unknown field\(s\) \['extra'\]"),
    (["department", "skill_needs", 0, "skill"], ..., r"request: department.skill_needs\[0\]: missing field\(s\) \['skill'\]"),
])
def test_version_2_refuses(path, value, text):
    with pytest.raises(ValueError, match=text):
        load_request(_set(sample(), path, value))


@pytest.mark.parametrize("path, value, text", [
    (["general", "required_skills"], [], r"request: general: unknown field\(s\) \['required_skills'\] \(version 1 has no skills\)"),
    (["general", "weights", "skill_shortfall"], 0, r"request: general.weights: unknown field\(s\) \['skill_shortfall'\]"),
    (["department", "skill_needs"], [], r"request: department: unknown field\(s\) \['skill_needs'\]"),
    (["department", "skill_needs_hard"], True, r"request: department: unknown field\(s\) \['skill_needs_hard'\]"),
    (["nurses", 1, "skills"], {}, r"request: nurses\[1\]: unknown field\(s\) \['skills'\] \(version 1 has no skills\)"),
])
def test_version_1_refuses_a_field_of_version_2(path, value, text):
    with pytest.raises(ValueError, match=text):
        load_request(_set(version_1(sample("four_nurses.json")), path, value))


def test_a_pin_on_a_barred_day_is_refused_with_its_date():
    req = _set(sample(), ["nurses", 2, "pinned"], {"2026-06-26": "D"})
    with pytest.raises(ValueError, match="request: Dana: 2026-06-26 is after the expiry of 'BLS', required to work"):
        load_request(req)


# ---- the result and the scoring of a grid ----------------------------------------------------------
def test_the_cli_solves_the_skills_sample_with_the_results_version_unchanged(tmp_path, capsys):
    req = sample()
    req["solver"]["time_limit_seconds"] = 2
    path, out = tmp_path / "req.json", tmp_path / "res.json"
    path.write_text(json.dumps(req))

    rc = main(["solve", str(path), "--out", str(out), "--workers", "4", "--explain"])

    res = json.loads(out.read_text())
    assert rc == 0 and res["ok"] and res["version"] == 1
    assert "skill_need" in res["penalties"]
    assert all(res["roster"]["Dana"][f"2026-06-{d}"] == "" for d in range(25, 31))
    assert any(line.startswith("Dana: BLS, required to work, expires inside the month") for line in res["precheck"])
    assert capsys.readouterr().out.startswith("precheck: Dana: BLS, required to work")


def test_a_grid_is_scored_with_the_two_rules_named_and_dated():
    req = sample()
    roster = {n["id"]: {f"2026-06-{d:02d}": "" for d in range(1, 31)} for n in req["nurses"]}
    roster["Dana"]["2026-06-26"] = "N"

    answer = validate_grid(req, roster)

    assert {"rule": "skill_expired", "nurse": "Dana", "date": "2026-06-26",
            "text": "skill expired: Dana works N on day 25, BLS is required to work and its last day was day 23"} \
        in answer["violations"]
    need = next(r for r in answer["rules"] if r["id"] == "skill_need")
    assert need["level"] == SOFT and need["weight"] == 50 and need["raw"] == 30 and need["weighted"] == 30 * 50
    assert need["matches"][0] == {"nurse": None, "date": "2026-06-01", "amount": 1, "elapsed": False,
                                  "text": "skill need: day 0 N has 0 holding ICU, needs 1 (1 short)"}
    # June 2026 from a Monday, four Fridays: 26 working days at 7 h (breastfeeding) = 182 h owed, one 12 h shift worked
    assert nurses_of(req, roster)["Dana"]["carry_over"]["hours_balance"] == 182 - 12


def test_precheck_says_a_soft_need_is_priced_not_infeasible():
    p = unit([Nurse("a", skills={"ICU": -1}), Nurse("b")], days=2)
    assert "skill need D/ICU: no nurse holds ICU on any free day (soft: each nurse short is priced by skill_shortfall)" \
        in precheck(p)
