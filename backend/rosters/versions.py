"""Reading the versions of an approved roster: which solve a version was planned with, and the cells two grids differ in. A
version is written by approval (version 1) and by `rosters.in_force.change` (every one after it), and nowhere else, so
`Roster.grid` always equals the latest version's grid under today's codes, which every reader of the roster in force relies on."""
from __future__ import annotations

from dataclasses import dataclass

from rosters.drafts import in_current_codes
from rosters.models import RosterVersion
from solves.models import Solve

Grid = dict[str, dict[str, str]]


def planned_with(version: RosterVersion) -> Solve:
    """The solve this version was planned with: its own, or for a hand edit the one of the version it was made on. Its input
    holds the shift hours and who was on probation as the version was planned."""
    if version.solve is not None:
        return version.solve
    earlier = version.roster.versions.filter(number__lt=version.number, solve__isnull=False).select_related("solve").first()
    # Version 1 always has one, the approval's; the roster's own is the last resort should that ever not hold.
    return earlier.solve if earlier else version.roster.solve


@dataclass(frozen=True)
class CellChange:
    hospital_id: str
    date: str
    before: str
    after: str


def differences(before: Grid, after: Grid) -> list[CellChange]:
    """Cell by cell, both under today's codes, so a rename between them is not a change. A row one of them lacks reads as
    empty."""
    before, after = in_current_codes(before), in_current_codes(after)
    changes = []
    for nurse in sorted(set(before) | set(after)):
        old, new = before.get(nurse, {}), after.get(nurse, {})
        for day in sorted(set(old) | set(new)):
            if old.get(day, "") != new.get(day, ""):
                changes.append(CellChange(nurse, day, old.get(day, ""), new.get(day, "")))
    return changes


def cells_changed(version: RosterVersion, previous: RosterVersion | None) -> int | None:
    return len(differences(previous.grid, version.grid)) if previous else None

