"""What a violation states beside its words, at the solver's edge: a result and a no-search check of a grid."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from nurse_roster.model import build_model, solve
from nurse_roster.problem import Headcount, Nurse
from nurse_roster.request import load_request
from nurse_roster.result import build_result
from solver_service.grid import validate_grid
from tests.er_fixture import request_of
from tests.test_core import mini
from tests.test_request import published_of

SKILLS = Path(__file__).parent.parent / "service_tests" / "fixtures" / "four_nurses_skills.json"
FACTS = {"shift", "present", "minimum", "maximum"}


def every_shift_day_rule() -> dict:
    """The four-nurse sample with every rule of a shift-day on and hard: D takes exactly one nurse, N one or two, each
    with a team leader, both genders and one nurse holding ICU on N."""
    raw = json.loads(SKILLS.read_text())
    raw["department"] |= {"require_team_leader": True, "require_both_genders": True, "skill_needs_hard": True}
    return raw


def checked(raw: dict, cells: dict[tuple[str, str], str]) -> dict:
    """The check of a grid with everyone off, but for `cells`."""
    month = load_request(raw).month
    roster = {n["id"]: {month.iso(d): "" for d in range(month.days)} for n in raw["nurses"]}
    for (nurse, date), shift in cells.items():
        roster[nurse][date] = shift
    return validate_grid(raw, roster)


def on(violations: list[dict], rule: str, date: str, shift: str) -> dict:
    found = [v for v in violations if v["rule"] == rule and v["date"] == date and v.get("shift") == shift]
    assert len(found) == 1, found
    return found[0]


def test_a_grid_checks_headcount_violation_states_its_shift_and_its_figures():
    answer = checked(every_shift_day_rule(), {(n, "2026-06-09"): "N" for n in ("Amal", "Badr", "Dana")})

    short = on(answer["violations"], "headcount", "2026-06-09", "D")
    over = on(answer["violations"], "headcount", "2026-06-09", "N")

    assert (short["present"], short["minimum"], short["maximum"]) == (0, 1, 1)
    assert (over["present"], over["minimum"], over["maximum"]) == (3, 1, 2)


def test_the_other_shift_day_rules_state_their_shift_and_no_figures():
    answer = checked(every_shift_day_rule(), {})

    for rule in ("team_leader", "gender", "skill_need"):
        violation = on(answer["violations"], rule, "2026-06-09", "N")
        assert set(violation) == {"rule", "nurse", "date", "text", "shift"}


def test_a_rule_of_no_shift_day_states_none_of_these_facts():
    answer = checked(every_shift_day_rule(), {("Emad", "2026-06-15"): "N"})       # Emad may only work days

    restricted = next(v for v in answer["violations"] if v["rule"] == "allowed_shifts")

    assert set(restricted) == {"rule", "nurse", "date", "text"}


def test_a_match_in_a_rules_explanation_states_the_same_facts():
    raw = every_shift_day_rule()
    raw["department"]["skill_needs_hard"] = False                                 # priced, so explained as a soft rule
    answer = checked(raw, {})
    matches = {rule["id"]: rule["matches"] for rule in answer["rules"]}

    headcount = next(m for m in matches["headcount"] if m["date"] == "2026-06-09" and m["shift"] == "D")
    need = next(m for m in matches["skill_need"] if m["date"] == "2026-06-09")
    hours_short = matches["nurse_shortfall"][0]

    assert headcount == {"nurse": None, "date": "2026-06-09", "amount": 1, "elapsed": False, "text": headcount["text"],
                         "shift": "D", "present": 0, "minimum": 1, "maximum": 1}
    assert need["shift"] == "N" and not FACTS - {"shift"} & set(need)
    assert not FACTS & set(hours_short)


def test_a_result_states_the_same_facts_as_a_grid_check():
    """A month whose last day is a recorded shortage: the result of its month-end run and the check of its roster say
    the same thing of it."""
    p = mini(30, ["D"], [Nurse("a", unavailable=[29])], headcounts=[Headcount("D", 1, 1, 1, 1)], headcount_enabled=True)
    raw = request_of(p, "2026-06", time_limit=2, replan_from=30)
    raw["published"] = published_of(p, "2026-06", [["D"] * 29 + [""]])
    req = load_request(copy.deepcopy(raw))
    rm = build_model(req.problem)
    result = build_result(req, rm, solve(rm, req.problem, **req.solver.solve_kwargs(2)), precheck=[], workers=2)

    [recorded] = result["elapsed_violations"]

    assert recorded == {"rule": "headcount", "nurse": None, "date": "2026-06-30", "text": recorded["text"],
                        "shift": "D", "present": 0, "minimum": 1, "maximum": 1}
    assert validate_grid(raw, result["roster"])["elapsed_violations"] == [recorded]
