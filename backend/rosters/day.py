"""The Day view: one date across every unit, for the CNO and the Deputy CNO. Each unit's figures on that date come
from the roster in force checked on the month as it stands with the solver's own code, or from the close in force for a closed
month, so the page never disagrees with the Roster page's check, My month or the close. Read-only: nothing here writes or logs.

Only the solver's shift-day findings are read off (headcount, team leader, both genders), and each nurse's overtime off its hours
accounting; the count present on a shift is the solver's own way of counting it, pure shift staff plus the shifts that also count
towards it (`nurse_roster.validate._headcount`), worked out here because a shift-day within its range has no finding to read it
from."""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from django.db.models import Q

from closes.making import latest_close
from closes.models import MonthClose
from hospital.models import HospitalSettings, Shift
from nurses import membership
from nurses.models import Leave, NurseProfile
from rosters.approved import headcount_of, shift_day
from rosters.drafts import current_code
from rosters.editing import accounted, scored
from rosters.grids import by_name, full_names
from rosters.models import Roster, RosterState
from solves.input import assemble
from units.models import Unit

State = Literal["none", "draft", "submitted", "approved", "closed"]
Grid = dict[str, dict[str, str]]


@dataclass(frozen=True)
class OnShift:
    hospital_id: str
    full_name: str
    team_leader: bool


@dataclass(frozen=True)
class ShiftFigures:
    """One shift of one unit's day. `minimum` and `maximum` are None where the unit's headcount rule is off or the shift has no
    headcount entry; `team_leader` and `both_genders` None where the unit does not require them on this shift."""
    code: str
    present: int
    minimum: int | None
    maximum: int | None
    short: bool
    over: bool
    team_leader: bool | None
    both_genders: bool | None
    nurses: list[OnShift]


@dataclass(frozen=True)
class OnLeave:
    hospital_id: str
    full_name: str


@dataclass(frozen=True)
class Figures:
    shifts: list[ShiftFigures]
    on_leave: list[OnLeave]
    hours_counted: bool
    overtime: int
    overtime_cap: int
    with_overtime: int

    @property
    def short_somewhere(self) -> bool:
        return any(shift.short for shift in self.shifts)


@dataclass(frozen=True)
class UnitDay:
    unit: Unit
    state: State
    figures: Figures | None = None
    unchecked: str | None = None


@dataclass(frozen=True)
class Totals:
    on_duty: dict[str, int]
    on_leave: int
    overtime: int
    units_short: int


@dataclass
class HospitalDay:
    date: dt.date
    weekend: bool
    units: list[UnitDay]
    codes: list[str]

    @property
    def totals(self) -> Totals:
        shown = [unit.figures for unit in self.units if unit.figures]
        on_duty = {code: sum(shift.present for figures in shown for shift in figures.shifts if shift.code == code)
                   for code in self.codes}
        return Totals(on_duty=on_duty, on_leave=sum(len(figures.on_leave) for figures in shown),
                      overtime=sum(figures.overtime for figures in shown),
                      units_short=sum(figures.short_somewhere for figures in shown))


def _findings(violations: Iterable[dict], day: str) -> dict[tuple[str, str], dict]:
    """The solver's shift-day findings on `day`, by (rule, shift code as the input names it)."""
    found = {}
    for violation in violations:
        where = shift_day(violation)
        if where and where[0] == day:
            found[(violation["rule"], where[1])] = violation
    return found


def _figures(solve_input: dict, grid: Grid, violations: list[dict], nurses_block: dict, date: dt.date,
             on_leave: list[OnLeave], names: dict[str, str]) -> Figures:
    """A unit's day from a solve input, the grid checked on it (leave and the days outside a nurse's dates already off), the
    solver's findings on that grid and its `nurses` block. Codes are the input's until the answer, where they are today's."""
    general, department = solve_input["general"], solve_input["department"]
    day = date.isoformat()
    people = {row["id"]: row for row in solve_input["nurses"]}
    cells = {hospital_id: grid.get(hospital_id, {}).get(day, "") for hospital_id in people}
    # A nurse on probation is listed on her shift and counted toward nothing, as the solver reads her (`problem.counted`).
    counted_cells = {hospital_id: cell for hospital_id, cell in cells.items() if not people[hospital_id].get("on_probation")}
    worked = [code for code in cells.values() if code]
    codes = list(dict.fromkeys([*department["shifts"], *worked]))
    headcounts = {headcount["shift"]: headcount for headcount in department["headcounts"]}
    findings = _findings(violations, day)
    weekend = date.weekday() in general["weekend_weekdays"]
    shifts = []
    for code in codes:
        pure = by_name([hospital_id for hospital_id, cell in cells.items() if cell == code], names)
        headcount = headcounts.get(code)
        present, low, high, team_leader, both = sum(cell == code for cell in counted_cells.values()), None, None, None, None
        short = over = False
        if headcount is not None:
            present += sum(cell in headcount["also_counts"] for cell in counted_cells.values())
            if department["headcount_enabled"]:
                low, high = ((headcount["weekend_min"], headcount["weekend_max"]) if weekend
                             else (headcount["weekday_min"], headcount["weekday_max"]))
                found = findings.get(("headcount", code))
                if found:
                    counted_present, counted_low, counted_high = headcount_of(found)
                    short, over = counted_present < counted_low, counted_present > counted_high
            if department["require_team_leader"]:
                team_leader = ("team_leader", code) not in findings
            if department["require_both_genders"]:
                both = ("gender", code) not in findings
        shifts.append(ShiftFigures(code=current_code(code), present=present, minimum=low, maximum=high, short=short, over=over,
                               team_leader=team_leader, both_genders=both,
                               nurses=[OnShift(hospital_id, names.get(hospital_id) or hospital_id,
                                               bool(people[hospital_id].get("team_leader"))) for hospital_id in pure]))
    counted = bool(general["hours_rule_enabled"])
    # Her rounding overtime is on her own record, outside the unit's totals and the department cap.
    overtimes = [row["overtime"] for hospital_id, row in nurses_block.items()
                 if not people.get(hospital_id, {}).get("on_probation")] if counted else []
    return Figures(shifts=shifts, on_leave=on_leave, hours_counted=counted, overtime=sum(overtimes),
                   overtime_cap=general["dept_overtime_cap"], with_overtime=sum(hours > 0 for hours in overtimes))


def _on_leave(rows: Iterable[str], names: dict[str, str]) -> list[OnLeave]:
    return [OnLeave(hospital_id, names.get(hospital_id) or hospital_id) for hospital_id in by_name(set(rows), names)]


def _approved(roster: Roster, date: dt.date) -> UnitDay:
    """One assembly, one check and one accounting, on the grid in force with every credited day off: leave wins over a shift
    the version may still hold, as My month counts it (`rosters.own_month.own_hours`)."""
    solve_input = assemble(roster.unit, roster.month).input
    grid = {hospital_id: dict(row) for hospital_id, row in roster.grid.items()}
    for row in solve_input["nurses"]:
        for credited in row["leave"]:
            if credited in grid.get(row["id"], {}):
                grid[row["id"]][credited] = ""
    checked = scored(solve_input, grid)
    nurses_block = accounted(solve_input, grid) if checked.answer else None
    if nurses_block is None:
        return UnitDay(roster.unit, "approved", unchecked=checked.reason or "The solver's code could not read this month.")
    ids = [row["id"] for row in solve_input["nurses"]]
    leave = Leave.objects.current().filter(date=date, nurse__user__username__in=ids).select_related("nurse__user")
    away = [row.nurse.user.username for row in leave if not membership.outside_dates(row.nurse, date)]
    names = full_names(ids)
    answer = checked.answer
    figures = _figures(solve_input, grid, answer["violations"] + answer["elapsed_violations"], nurses_block, date,
                       _on_leave(away, names), names)
    return UnitDay(roster.unit, "approved", figures)


def _closed(close: MonthClose, date: dt.date) -> UnitDay:
    """The close in force is the record: its facts, the hard rules they broke and its `nurses` block, worked out again by no
    one. Leave is the rows the close read, those recorded by then and not removed by then, on the days its input credits."""
    solve_input = close.input
    credited = {row["id"]: set(row.get("leave", [])) for row in solve_input["nurses"]}
    day = date.isoformat()
    rows = (Leave.objects.filter(date=date, recorded_at__lte=close.closed_at, nurse__user__username__in=list(credited))
            .filter(Q(removed_at__isnull=True) | Q(removed_at__gt=close.closed_at)).values_list("nurse__user__username", flat=True))
    # The close keeps no nurse's dates, so a day a correction has left outside them is read under her dates now.
    dates = {profile.user.username: profile for profile in NurseProfile.objects.filter(user__username__in=set(rows))
             .select_related("user")}
    away = [hospital_id for hospital_id in rows
            if day in credited[hospital_id] and not membership.outside_dates(dates[hospital_id], date)]
    names = full_names(credited)
    figures = _figures(solve_input, close.result["facts"], close.result["facts_broke"], close.result["nurses"], date,
                       _on_leave(away, names), names)
    return UnitDay(close.unit, "closed", figures)


def hospital_day(date: dt.date) -> HospitalDay:
    """Every unit not archived, and an archived one with a roster or a close that month, as the Rosters overview chooses."""
    month = date.replace(day=1)
    rosters = {roster.unit_id: roster for roster in Roster.objects.filter(month=month).select_related("unit")}
    closed = set(MonthClose.objects.filter(month=month).values_list("unit_id", flat=True))
    units = Unit.objects.filter(Q(archived=False) | Q(pk__in=set(rosters) | closed))
    rows = []
    for unit in units:
        roster = rosters.get(unit.pk)
        if unit.pk in closed:
            rows.append(_closed(latest_close(unit, month), date))
        elif roster is not None and roster.state == RosterState.APPROVED:
            rows.append(_approved(roster, date))
        else:
            rows.append(UnitDay(unit, roster.state if roster else "none"))
    used = {shift.code for row in rows if row.figures for shift in row.figures.shifts}
    catalogue = [code for code in Shift.objects.values_list("code", flat=True) if code in used]
    # The heading's kind of day is the hospital rules' now; a closed month's ranges are its close's, which says what applied.
    weekend = date.weekday() in HospitalSettings.load().rules["weekend_weekdays"]
    return HospitalDay(date=date, weekend=weekend, units=rows, codes=catalogue + sorted(used - set(catalogue)))
