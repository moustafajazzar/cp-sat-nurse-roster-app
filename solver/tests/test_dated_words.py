"""Every text that leaves the solver names dates: a violation's words, a match's in a rule's explanation, a precheck issue and
a result's message. A day counted from 0 stays inside, and is said only by a problem that knows no month."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from nurse_roster.model import build_model, solve
from nurse_roster.precheck import precheck
from nurse_roster.request import load_request
from nurse_roster.result import build_result
from solver_service.grid import validate_grid

SKILLS = Path(__file__).parent.parent / "service_tests" / "fixtures" / "four_nurses_skills.json"
# A day by its number, as the app's reader of stored words finds one: not the "day" of "changes night/day 6 times".
DAY_NUMBER = re.compile(r"(?<![/\w])day -?\d")
DAY_BEFORE_A_DATE = re.compile(r"\bday \d{4}-\d{2}-\d{2}")
A_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def june(*days: int) -> list[str]:
    return [f"2026-06-{day:02d}" for day in days]


def a_month_that_breaks_its_rules() -> tuple[dict, dict]:
    """The four-nurse sample of June 2026 with Faten on probation beside Amal, and a roster that breaks rules on days before the
    month, on its first day and inside it. The input and that roster."""
    raw = json.loads(SKILLS.read_text())
    raw["department"] |= {"require_team_leader": True, "require_both_genders": True, "score_target": 0.65,
                          "score_target_weekend": 0.65, "score_balance": True, "score_band_hi": 0.7, "score_band_hi_weekend": 0.7}
    raw["general"]["weights"] |= {"score_deficit": 1, "score_spread": 1, "night_rest": 5}
    raw["department"]["night_rest_hard"] = False
    nurses = {n["id"]: n for n in raw["nurses"]}
    faten = copy.deepcopy(nurses["Emad"]) | {"id": "Faten", "gender": "F", "team_leader": False, "on_probation": True,
                                              "preceptor": "Amal"}
    raw["nurses"].append(faten)
    nurses["Amal"] |= {"previous": ["D", "D", "D", "N"], "leave": june(3), "undesired_days": june(4), "desired_days": june(5)}
    nurses["Badr"] |= {"previous": ["", "", "N", ""], "pinned": {"2026-06-22": "N", "2026-06-23": "D"}}
    nurses["Emad"] |= {"pinned": {"2026-06-17": "D"}}
    for nurse in raw["nurses"]:
        nurse.setdefault("previous", [""] * 4)
        nurse.setdefault("leave", [])
        nurse["leave"] = sorted({*nurse["leave"], "2026-06-10"})                    # everyone away on the 10th
    roster = {n["id"]: dict.fromkeys(june(*range(1, 31)), "") for n in raw["nurses"]}

    def works(nurse: str, shift: str, *days: int) -> None:
        roster[nurse] |= dict.fromkeys(june(*days), shift)

    works("Amal", "D", 1, 3, 4)                # after a night, a fifth shift in a row; on her leave; a day she asked off
    works("Amal", "N", 7)
    works("Badr", "D", 1)                      # one day off since the night before the month
    works("Badr", "D", 11, 12, 13, 14, 16, 17, 18, 19, 21)   # a cycle, a day off, a cycle, and one day of rest
    works("Badr", "N", 22)
    works("Badr", "D", 23)
    works("Emad", "N", 15)                     # he works days only
    works("Dana", "D", 26)                     # after her BLS's last day
    works("Emad", "D", 2)                      # alone, under the score a shift should average
    works("Faten", "D", 8)                     # off beside Amal's day on the 1st, and at work when Amal is off
    return raw, roster


def texts_of_a_check(answer: dict) -> list[str]:
    said = [row["text"] for row in answer["violations"] + answer["elapsed_violations"]]
    return said + [match["text"] for rule in answer["rules"] for match in rule["matches"]]


def texts_of_a_result(result: dict) -> list[str]:
    said = [row["text"] for row in result["violations"] + result["elapsed_violations"]]
    return said + result["precheck"] + [result["message"]]


def checked() -> dict:
    """The roster checked against a published roster it differs from in one cell."""
    raw, roster = a_month_that_breaks_its_rules()
    raw["published"] = copy.deepcopy(roster) | {"Dana": roster["Dana"] | {"2026-06-02": "N"}}
    return validate_grid(raw, roster)


def month_end_result() -> dict:
    """The result of the roster's month-end run: every day a recorded fact, so what it broke is all reported."""
    raw, roster = a_month_that_breaks_its_rules()
    raw |= {"published": roster, "replan_from": "2026-07-01"}
    raw["solver"]["time_limit_seconds"] = 2
    req = load_request(raw)
    issues = precheck(req.problem)
    rm = build_model(req.problem)
    return build_result(req, rm, solve(rm, req.problem, **req.solver.solve_kwargs(2)), precheck=issues, workers=2)


def test_the_fixture_breaks_rules_before_at_the_start_of_and_inside_the_month():
    answer = checked()
    matched = {rule["id"] for rule in answer["rules"] if rule["matches"]}

    assert {"unavailable", "allowed_shifts", "succession", "consecutive", "double_cycle", "headcount", "team_leader", "gender",
            "night_rest", "skill_expired", "skill_need", "following", "score_deficit", "score_spread", "pin", "change",
            "undesired_day", "desired_day"} <= matched


def test_no_text_of_a_grid_check_counts_a_day():
    said = texts_of_a_check(checked())

    assert [text for text in said if DAY_NUMBER.search(text) or DAY_BEFORE_A_DATE.search(text)] == []
    assert any("2026-05-31" in text for text in said)          # a day before the month is a date too


def test_no_text_of_a_result_counts_a_day():
    result = month_end_result()
    said = texts_of_a_result(result)

    assert result["elapsed_violations"] and result["precheck"]
    assert [text for text in said if DAY_NUMBER.search(text) or DAY_BEFORE_A_DATE.search(text)] == []


def test_no_precheck_issue_counts_a_day():
    raw, _ = a_month_that_breaks_its_rules()

    issues = precheck(load_request(raw).problem)

    assert "2026-06-10: headcounts need 2 nurses but only 0 are not on leave" in issues
    assert "Badr: pinned N on 2026-06-22 and D on 2026-06-23 is a forbidden succession" in issues
    assert [issue for issue in issues if DAY_NUMBER.search(issue) or DAY_BEFORE_A_DATE.search(issue)] == []


def test_a_forbidden_succession_names_both_its_days_and_is_dated_on_the_second():
    across, *_ = [row for row in checked()["violations"] if row["rule"] == "succession"]

    assert across == {"rule": "succession", "nurse": "Amal", "date": "2026-06-01",
                      "text": "succession: Amal N on 2026-05-31 -> D on 2026-06-01"}


def test_a_problem_with_a_month_says_a_days_date_inside_and_before_it():
    raw, _ = a_month_that_breaks_its_rules()
    p = load_request(raw).problem

    assert (p.day_words(0), p.day_words(29), p.day_words(-1), p.day_words(-32)) == \
        ("2026-06-01", "2026-06-30", "2026-05-31", "2026-04-30")


def test_a_problem_with_no_month_counts_its_days():
    raw, _ = a_month_that_breaks_its_rules()
    p = load_request(raw).problem
    p.month = None

    assert (p.day_words(0), p.day_words(8), p.day_words(-1)) == ("day 0", "day 8", "day -1")
    assert "Dana: BLS, required to work, expires inside the month: its last day is day 23, so no shift after it" in precheck(p)
