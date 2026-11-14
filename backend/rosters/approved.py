"""An approved roster's month as a change to the roster in force reads it, and how such a change is checked with the solver's own
code: what a swap and a cover share. Each words its own reasons, for its own readers; nothing here says
anything to anyone."""
from __future__ import annotations

import copy
import datetime as dt
from dataclasses import dataclass

from hospital.models import Shift
from nurses.models import NurseProfile
from rosters.drafts import current_code
from rosters.models import PinnedCell, Roster
from rosters.pins import Judge
from rosters.versions import Grid
from solves.result import headcount_of, shift_day, skill_need_of


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


def _beyond(violation: dict) -> int:
    """How far a headcount is outside its range."""
    present, low, high = headcount_of(violation)
    return low - present if present < low else present - high


def _short(violation: dict) -> tuple[tuple, int] | None:
    """A skill need's shift-day and skill, which name it (a shift-day can need several skills), and how many nurses holding
    the skill it is short; None for another rule, or a need that does not state its figures."""
    where, need = shift_day(violation), skill_need_of(violation)
    if violation["rule"] != "skill_need" or where is None or need is None:
        return None
    skill, short = need
    return (where, skill), short


def _as_broken(violation: dict) -> tuple:
    """A violation with how far it breaks its rule, which only its words say: one broken further reads differently."""
    where = shift_day(violation)
    return violation["rule"], violation["nurse"], violation["date"], where and where[1], violation["text"]


def newly_broken(before: list[dict], after: list[dict]) -> list[dict]:
    """The violations of `after` that `before` does not hold, or holds broken less: what a change to the roster in force
    breaks, between that roster's violations and the changed grid's. A hard rule the roster in force already breaks is not the
    change's, since an edit may save one. A headcount is compared by how far it is outside its range, and a skill need by how
    many nurses holding its skill it is short, as their words change when they come nearer: one more nurse on a short
    shift-day breaks it less, not anew. Any other rule, or a skill need that does not state its figures, is compared by its
    words."""
    already = {_as_broken(violation) for violation in before}
    headcounts = {shift_day(row): _beyond(row) for row in before if row["rule"] == "headcount" and shift_day(row)}
    needs = dict(short for row in before if (short := _short(row)))
    new = []
    for violation in after:
        where, short = shift_day(violation), _short(violation)
        if violation["rule"] == "headcount" and where in headcounts:
            if _beyond(violation) > headcounts[where]:
                new.append(violation)
        elif short is not None and short[0] in needs:
            if short[1] > needs[short[0]]:
                new.append(violation)
        elif _as_broken(violation) not in already:
            new.append(violation)
    return new
