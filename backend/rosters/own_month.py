"""A nurse's own hours in the version in force, worked out on the month as it stands with the solver's own arithmetic
(`nurse_roster.problem.Problem`), the same as a result's `nurses` block. A stored result's block is not used: it is the solve's
grid, not the version's, which hand edits and later versions change, and it holds every colleague's leave. What the version in
force kept of her preferences is read the same way, with the solver's own reading of "not kept"."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

from nurse_roster.problem import barred_days
from nurse_roster.request import load_request
from nurse_roster.validate import unkept_days

from closes.chain import Chain
from hospital.models import Shift
from nurses import membership
from nurses.models import NurseProfile
from requesting.models import PreferenceKind
from rosters.drafts import current_code
from solves.input import assemble
from units.models import Unit


@dataclass(frozen=True)
class OwnHours:
    worked: int
    leave_credit: int
    owed: int | None
    overtime: int
    carried: int | None
    shifts: dict[str, int]


def own_hours(unit: Unit, month: dt.date, hospital_id: str, row: dict[str, str]) -> OwnHours | None:
    """None when the month as it stands no longer has this nurse in it, or the solver's code cannot read it. `owed` and
    `carried` are None with the hospital's hours rule off, which counts nothing owed.

    Leave wins over a shift, as everywhere: a shift the version still holds on a day of leave recorded since (a new
    version takes it off) is not counted as worked, since the day already counts through the leave credit."""
    try:
        problem = load_request(assemble(unit, month).input).problem
    except ValueError:
        return None
    nurse = next((nurse for nurse in problem.nurses if nurse.id == hospital_id), None)
    if nurse is None:
        return None
    credited = {month.replace(day=day + 1).isoformat() for day in nurse.unavailable}
    codes = [code for day, code in sorted(row.items()) if code and day not in credited]
    hours = dict(Shift.objects.filter(code__in=codes).values_list("code", "hours"))
    if any(code not in hours for code in codes):
        return None
    worked = sum(hours[code] for code in codes)
    counted = problem.general.hours_rule_enabled
    owed = problem.required_hours(nurse)
    return OwnHours(worked=worked, leave_credit=problem.leave_credit(nurse), owed=owed if counted else None,
                    overtime=max(0, worked - owed) if counted else 0,
                    carried=problem.carry_over(nurse, worked) if counted else None,
                    shifts={code: codes.count(code) for code in dict.fromkeys(codes)})


def closed_hours(close, hospital_id: str) -> OwnHours | None:
    """A closed month's figures for one nurse, as its close stored them (`closes.models.MonthClose`): fact, whatever has been
    corrected since. Its shifts under their codes now. None when the close did not have them in the month."""
    row = close.result["nurses"].get(hospital_id)
    if row is None:
        return None
    counted = close.input["general"]["hours_rule_enabled"]
    shifts: dict[str, int] = {}
    for code, count in row["shifts"].items():
        if count:
            now = current_code(code)
            shifts[now] = shifts.get(now, 0) + count
    return OwnHours(worked=row["hours_worked"], leave_credit=row["leave_credit"], owed=row["required"] if counted else None,
                    overtime=row["overtime"], carried=row["carry_over"]["hours_balance"] if counted else None, shifts=shifts)


Outcome = Literal["kept", "not_kept", "on_leave", "barred"]
# The solver's two preference rules, by the kind of preference each prices (`solves.input._preferred`).
RULES = {"undesired_day": ("undesired_days", PreferenceKind.DAY_OFF), "desired_day": ("desired_days", PreferenceKind.WORKING_DAY)}


@dataclass(frozen=True)
class OwnPreferences:
    """Each preference of hers the solve input holds, by its date and kind, and what the grid made of it."""
    outcomes: dict[tuple[dt.date, PreferenceKind], Outcome]

    @property
    def kept(self) -> int:
        return sum(outcome == "kept" for outcome in self.outcomes.values())

    @property
    def keepable(self) -> int:
        return sum(outcome not in ("on_leave", "barred") for outcome in self.outcomes.values())


def _preferences_on(solve_input: dict, hospital_id: str, row: dict[str, str]) -> OwnPreferences | None:
    """Her preferences as the solve input holds them, so only her live ones of the unit's windows have an outcome, kept or
    not by `row` as the solver's carry-over counts them (`nurse_roster.validate.unkept_days`): whatever the two weights, and a
    day of leave or outside her dates counted neither way, nor one an expired required skill of hers bars her from
    (`barred`)."""
    try:
        problem = load_request(solve_input).problem
    except ValueError:
        return None
    nurse = next((nurse for nurse in problem.nurses if nurse.id == hospital_id), None)
    if nurse is None:
        return None
    first = dt.date.fromisoformat(f"{solve_input['month']}-01")
    dates = [first.replace(day=day + 1) for day in range(problem.days)]
    worked = [0 if row.get(date.isoformat()) else -1 for date in dates]
    leave, barred = set(nurse.unavailable), set(barred_days(problem, nurse))
    outcomes: dict[tuple[dt.date, PreferenceKind], Outcome] = {}
    for rule, (days, kind) in RULES.items():
        unkept = set(unkept_days(problem, nurse, worked, rule))
        for day in getattr(nurse, days):
            outcomes[(dates[day], kind)] = ("on_leave" if day in leave else "barred" if day in barred
                                            else "not_kept" if day in unkept else "kept")
    return OwnPreferences(outcomes)


def own_preferences(unit: Unit, month: dt.date, hospital_id: str, row: dict[str, str]) -> OwnPreferences | None:
    """What the version in force (`row`, hers) kept, on the month as it stands, so leave recorded since is On leave."""
    try:
        solve_input = assemble(unit, month).input
    except ValueError:
        return None
    return _preferences_on(solve_input, hospital_id, row)


def closed_preferences(close, hospital_id: str) -> OwnPreferences | None:
    """What a closed month's facts kept, as its close read them, whatever has been recorded since."""
    facts = close.result["facts"].get(hospital_id)
    return _preferences_on(close.input, hospital_id, facts) if facts is not None else None


def doubled_by(nurse: NurseProfile, month: dt.date) -> dt.date | None:
    """The month out of which she carries preferences not kept, which doubles the price of hers in `month`: the chain's value,
    as the solve input reads it (`solves.input.assemble`), so what she is told and what the solver prices never disagree."""
    if membership.placement_in(nurse, month) is None:
        return None
    carried = Chain().carry_in(nurse, month)
    return carried.out_of if carried.into(month)["preferences_not_kept"] > 0 else None
