"""The versions of an approved roster. Every change to it is a new version, in force at once: the manager's direct edits, leave
recorded on a day it works, re-plans, accepted requests and approved swaps. `make_version` is
the one way a version is made, so `Roster.grid` always equals the latest version's grid under today's codes, which every reader
of the roster in force relies on.

A version's reason is for the unit's manager and the approvers only: it can say a cell was leave. It never reaches a nurse, a
notification or the audit log."""
from __future__ import annotations

from dataclasses import dataclass

from accounts.models import User
from rosters.drafts import in_current_codes
from rosters.models import Roster, RosterVersion
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


def make_version(roster: Roster, grid: Grid, origin: str, by: User, reason: str = "",
                 solve: Solve | None = None) -> RosterVersion:
    """The next number, with `grid` in today's codes, which becomes `Roster.grid` too. Hold the roster's row lock, so that two
    changes at once each get their own number in the order they commit. A live swap one of whose cells this changes expires here,
    whatever made the version, and its people are told it expired, never why. Then a waiting cover of a shift-day
    this puts one more nurse on is filled, one per nurse added, oldest first."""
    from closes.tasks import project  # The month close reads rosters, so not at the top.
    from covers.expiring import on_new_version as fill_covers  # covers reads rosters, so not at the top.
    from swaps.expiring import on_new_version  # swaps reads rosters, so not at the top.

    latest = roster.versions.first()
    roster.grid = grid
    roster.save(update_fields=["grid"])  # `updated_at` stays: it is when a solve or a restore last replaced the grid.
    version = RosterVersion.objects.create(roster=roster, number=latest.number + 1 if latest else 1, grid=grid, origin=origin,
                                           solve=solve, created_by=by, reason=reason)
    if latest is not None:
        on_new_version(roster, latest.grid, grid)  # Not `roster.grid`, which a direct edit has written already.
        fill_covers(roster, latest.grid, grid, origin)
    project(roster.unit, roster.month)
    return version


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

