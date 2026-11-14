"""Reading a stored result: the one place that says what its fields mean to this application.

A violation states which shift of its day it is about and, for a headcount, its figures, as fields. The results and month closes
stored before the solver stated them hold only its words, so the patterns that read those words are here and nowhere else."""
from __future__ import annotations

import datetime as dt
import re

from nurse_roster.problem import barred_days, preceptor_of
from nurse_roster.request import load_request
from nurse_roster.rules import SOFT
from solver_service.grid import validate_grid

# Below this many, every occurrence is in view; a long tail of the same thing says nothing more. The solver's own report's size.
LARGEST = 5
_DAY = re.compile(r"(?<![/\w])day (-?\d+)\b")  # Not "changes night/day 6 times", the one place a day's number follows otherwise.

# The solver's rules about a shift-day as a whole, and how its words named one before it stated the shift as a field:
# "headcount: day 8 D = 0, want 1..1", "team leader: day 8 D has none", "gender: day 8 D has only {'F'}",
# "skill need: day 8 N has 0 holding ICU, needs 1 (1 short)".
SHIFT_DAY_RULES = ("headcount", "team_leader", "gender", "skill_need")
_SHIFT_DAY = re.compile(r"^[a-z ]+: day -?\d+ (\S+) ")
_HEADCOUNT = re.compile(r"= (\d+), want (\d+)\.\.(\d+)")


def has_a_roster_to_keep(result: dict | None) -> bool:
    """A result can hold no roster, or one the validator says breaks a hard rule, which `ok` false reports. Neither becomes
    a draft."""
    return bool(result and result.get("ok") and result.get("roster"))


def in_dates(text: str, month: dt.date) -> str:
    """Words kept in a stored result or a stored close, read with dates. Those the solver wrote before it dated its own count
    a month's days from 0 ("day 4" is the 5th), and from below 0 for the days before it, which next to a date in the same line
    reads as a mistake. Those it wrote since name dates and pass unchanged. Only stored words need this: a fresh answer of the
    solver's code is dated already."""
    return _DAY.sub(lambda found: (month + dt.timedelta(days=int(found[1]))).isoformat(), text)


def shift_day(violation: dict) -> tuple[str, str] | None:
    """The shift-day a violation of a shift-day rule is about, as (date, shift code), or None for any other."""
    if violation["rule"] not in SHIFT_DAY_RULES or violation["nurse"] or not violation["date"]:
        return None
    if "shift" in violation:
        return violation["date"], violation["shift"]
    found = _SHIFT_DAY.match(violation["text"])
    return (violation["date"], found[1]) if found else None


def headcount_of(violation: dict) -> tuple[int, int, int]:
    """A headcount violation's (present, minimum, maximum)."""
    if "present" in violation:
        return violation["present"], violation["minimum"], violation["maximum"]
    present, low, high = (int(number) for number in _HEADCOUNT.search(violation["text"]).groups())
    return present, low, high


def explanation(solve_input: dict, result: dict | None) -> dict[str, dict] | None:
    """Each soft rule that costs something: its own largest occurrences, by rule name. A stored result holds only the totals,
    so this asks the solver's own code to score what is stored, here and with no search, which needs no solver running.
    None if the solver's code no longer reads what it was given."""
    if not (result and result.get("roster")):
        return {}
    try:
        answer = validate_grid(solve_input, result["roster"])
    except ValueError:
        return None
    return {rule["id"]: rule | {"matches": sorted(rule["matches"], key=lambda match: -match["amount"])}
            for rule in answer["rules"] if rule["level"] == SOFT and rule["weighted"]}


def not_followed(solve_input: dict, roster: dict[str, dict[str, str]]) -> list[dict]:
    """The following rule's explanation, which a result does not keep (it holds priced soft rules only, and the following rule is hard):
    for each nurse on probation in the input, her preceptor's day shifts she followed, and each she did not with why, read
    with the solver's own `Problem` (her allowed shifts less the night shift, her leave, pins, bars and the cutoff). A day her
    preceptor works a shift she is allowed and she does not is one she could have followed. On a valid roster she is then
    off, and the reason is the first that holds: leave (the days outside her dates included), a barred day, a day already over
    in a re-plan, a pin or an accepted request, or else her own rules: her hours held no more (hours win over
    following), or her rest, run and succession rules kept her off.
    Nothing when the solver's code no longer reads the input."""
    try:
        problem = load_request(solve_input).problem
    except ValueError:
        return []
    found = []
    for nurse in problem.nurses:
        if not nurse.on_probation:
            continue
        row = {"nurse": nurse.id, "preceptor": None, "followed": 0, "missed": []}
        found.append(row)
        if preceptor_of(problem, nurse) is None:
            continue
        row["preceptor"] = nurse.preceptor
        allowed, barred = set(problem.allowed(nurse)), set(barred_days(problem, nurse))
        mine, theirs = roster.get(nurse.id, {}), roster.get(nurse.preceptor, {})
        for day, shift in sorted(theirs.items()):
            index = dt.date.fromisoformat(day).day - 1  # The solver's: the month's days from 0.
            if shift not in allowed:
                continue
            if mine.get(day) == shift:
                row["followed"] += 1
                continue
            why = ("leave" if index in nurse.unavailable else "barred" if index in barred
                   else "elapsed" if index < problem.planning_from else "pinned" if index in nurse.pinned else "own_rules")
            row["missed"].append({"date": day, "shift": shift, "why": why})
    return found


def precheck_of(result: dict, month: dt.date) -> list[str]:
    return [in_dates(issue, month) for issue in result.get("precheck") or []]


def cost_of(result: dict | None) -> int | None:
    """What the roster it answered costs. `Solve.best_cost` is only how far the search got, and outlasts a failure."""
    return (result or {}).get("cost")


def shifts_used(result: dict) -> set[str]:
    return {code for row in result["roster"].values() for code in row.values() if code}


def statistics_of(result: dict) -> dict:
    solver = result.get("solver") or {}
    model = solver.get("model") or {}
    return {"status": result.get("status", ""), "stopped_by": solver.get("stopped_by"), "improvements": solver.get("improvements", 0),
            **{name: solver.get(name) for name in ("objective", "best_bound", "solve_seconds", "build_seconds",
                                                   "deterministic_time", "workers", "time_limit_seconds")},
            **{name: model.get(name) for name in ("variables", "constraints", "objective_terms")}}
