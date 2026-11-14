"""An approved roster's month as a change to the roster in force reads it, and how such a change is checked with the solver's own
code: what a swap and a cover share. Each words its own reasons, for its own readers; nothing here says
anything to anyone."""
from __future__ import annotations

import copy
import datetime as dt
import re
from dataclasses import dataclass

from hospital.models import Shift
from nurses.models import NurseProfile
from rosters.drafts import current_code
from rosters.editing import Checked
from rosters.models import PinnedCell, Roster
from rosters.pins import Judge
from rosters.versions import Grid


@dataclass(frozen=True)
class NewCell:
    """One cell asked for: a nurse's date and what it should hold, a shift's code or None for a day off."""
    hospital_id: str
    date: dt.date
    shift: str | None

    @property
    def day(self) -> str:
        return self.date.isoformat()


class ApprovedMonth:
    """An approved roster's month, read once for every cell a change asks for."""

    def __init__(self, roster: Roster):
        self.roster, self.unit, self.month = roster, roster.unit, roster.month
        self.judge = Judge(self.unit, self.month)
        self.serving = {nurse.user.username: nurse for nurse in self.judge.nurses.values()}
        on_grid = NurseProfile.objects.filter(user__username__in=list(roster.grid)).select_related("user")
        self.on_grid = {nurse.user.username: nurse for nurse in on_grid}
        worked = Shift.objects.filter(active=True, code__in=self.unit.rules.get("shifts", []))
        self.shifts = {shift.code: shift for shift in worked}
        self.pins = {(pin.nurse_id, pin.date): pin for pin in PinnedCell.objects.current().in_month(self.month)
                     .filter(unit=self.unit).select_related("shift")}

    def cell(self, hospital_id: str, day: dt.date) -> str:
        return self.roster.grid.get(hospital_id, {}).get(day.isoformat(), "")

    def shift_of(self, code: str | None) -> Shift | None:
        return self.shifts.get(current_code(code)) if code else None

    def grid_after(self, new_cells: list[NewCell]) -> Grid:
        grid = copy.deepcopy(self.roster.grid)
        for new_cell in new_cells:
            shift = self.shift_of(new_cell.shift)
            grid[new_cell.hospital_id][new_cell.day] = shift.code if shift else ""
        return grid


def with_pins(solve_input: dict, month: ApprovedMonth, new_cells: list[NewCell]) -> dict:
    """The input as the change would make it: each changed cell a pin, so the solver's `pin` rule holds the new cell, and a
    manager's pin there, which the change replaces, does not count against it."""
    edited = copy.deepcopy(solve_input)
    rows = {row["id"]: row for row in edited["nurses"]}
    for new_cell in new_cells:
        row = rows.get(new_cell.hospital_id)
        if row is not None:
            shift = month.shift_of(new_cell.shift)
            row.setdefault("pinned", {})[new_cell.day] = shift.code if shift else ""
    return edited


def violation_key(violation: dict) -> tuple:
    """The solver's text tells the shifts of one day apart, and says how far a rule is broken: one broken further is new."""
    return violation["rule"], violation["nurse"], violation["date"], violation["text"]


def already_broken(before: Checked) -> set[tuple]:
    """A hard rule the roster in force already breaks is not the change's, since an edit may save one."""
    return {violation_key(row) for row in before.answer["violations"]} if before.answer else set()


# The solver's rules of a shift-day as a whole, and how its text names the shift-day: "headcount: day 8 D = 0, want 1..1",
# "team leader: day 8 D has none", "gender: day 8 D has only {'F'}" (`nurse_roster/validate.py`).
SHIFT_DAY_RULES = ("headcount", "team_leader", "gender")
_SHIFT_DAY = re.compile(r"^[a-z ]+: day -?\d+ (\S+) ")
_HEADCOUNT = re.compile(r"= (\d+), want (\d+)\.\.(\d+)")


def shift_day(violation: dict) -> tuple[str, str] | None:
    """The shift-day a violation of a shift-day rule is about, as (date, shift code), or None for any other."""
    if violation["rule"] not in SHIFT_DAY_RULES or violation["nurse"] or not violation["date"]:
        return None
    found = _SHIFT_DAY.match(violation["text"])
    return (violation["date"], found[1]) if found else None


def headcount_of(violation: dict) -> tuple[int, int, int]:
    """A headcount's (present, minimum, maximum), from the solver's text."""
    present, low, high = (int(number) for number in _HEADCOUNT.search(violation["text"]).groups())
    return present, low, high


def _beyond(violation: dict) -> int:
    """How far a headcount is outside its range."""
    present, low, high = headcount_of(violation)
    return low - present if present < low else present - high


def newly_broken(before: Checked, after: Checked) -> list[dict]:
    """What `after` breaks that `before` did not, or breaks further. The solver's text says how far a rule is broken, so one
    broken further reads as new (`violation_key`); a headcount is compared by how far it is outside its range, since its text
    changes as it comes nearer: one more nurse on a short shift-day breaks it less, not anew."""
    old = before.answer["violations"] if before.answer else []
    already = already_broken(before)
    headcounts = {shift_day(row): _beyond(row) for row in old if row["rule"] == "headcount" and shift_day(row)}
    new = []
    for violation in after.answer["violations"]:
        where = shift_day(violation)
        if violation["rule"] == "headcount" and where in headcounts:
            if _beyond(violation) > headcounts[where]:
                new.append(violation)
        elif violation_key(violation) not in already:
            new.append(violation)
    return new
