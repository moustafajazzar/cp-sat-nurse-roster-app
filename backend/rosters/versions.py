"""The versions of an approved roster. Every change to it is a new version, in force at once: the manager's direct edits, leave
recorded on a day it works, re-plans, accepted requests and approved swaps. `make_version` is
the one way a version is made, so `Roster.grid` always equals the latest version's grid under today's codes, which every reader
of the roster in force relies on.

A version's reason is for the unit's manager and the approvers only: it can say a cell was leave. It never reaches a nurse, a
notification or the audit log."""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.db import connection

from accounts.models import User
from nurses import membership
from nurses.models import NurseProfile
from rosters.drafts import in_current_codes
from rosters.models import Roster, RosterState, RosterVersion, VersionOrigin
from rosters.pins import long_date
from solves.models import Solve
from units.models import Unit

if TYPE_CHECKING:
    from covers.models import Cover

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


def _key_share(unit: Unit) -> None:
    """A cover this opens refers to its unit, whose foreign key is checked at commit with `FOR KEY SHARE`. Taken here, before
    any roster, so it keeps the order unit, roster: an edit or a rename holding the unit while it waits on this roster is
    waited for instead of deadlocked with. The weakest lock there is, so a leave write of another unit's nurse, or a read of
    the unit, never waits on it."""
    with connection.cursor() as cursor:
        # The ORM takes no `FOR KEY SHARE`.
        cursor.execute(f"SELECT 1 FROM {Unit._meta.db_table} WHERE id = %s FOR KEY SHARE", [unit.pk])


@dataclass(frozen=True)
class TakenOff:
    roster: Roster
    version: RosterVersion
    days: list[tuple[dt.date, str]]
    covers: dict[dt.date, Cover] = field(default_factory=dict)


def take_off_for_leave(nurse: NurseProfile, unit: Unit, days: Iterable[dt.date], by: User) -> list[TakenOff]:
    """Leave wins over a shift. On each approved roster of this unit for a month not yet over, a shift the roster in force holds
    on one of these days is taken off, as a new version with origin `edit` by whoever recorded the leave. Its reason says leave
    and never the type. No pin is made: the leave is the fact a re-plan keeps. A month that is over keeps its grid, as an edit
    cannot change it, and month close reads the leave itself. Each shift taken off on a day today or later opens a
    cover, after the version, unoffered and told to nobody. Call it under the nurse's lock, as every leave write
    holds it: this takes each roster's row lock after it, never the catalogue's, so the order stays catalogue, nurse, unit,
    roster, with the unit's `FOR KEY SHARE` before the rosters for the covers it opens. The nurses' locks are `FOR NO KEY
    UPDATE`, so the notifications' foreign keys to colleagues' users, checked at commit, never wait on a colleague's edit that
    waits on this roster."""
    from covers import opening  # covers reads rosters, so not at the top.

    hospital_id = nurse.user.username
    by_month: dict[dt.date, list[dt.date]] = {}
    for day in sorted(set(days)):
        if day.replace(day=1) >= membership.this_month():
            by_month.setdefault(day.replace(day=1), []).append(day)
    taken = []
    if by_month:
        _key_share(unit)
    for month, month_days in by_month.items():
        approved = Roster.objects.select_for_update(of=("self",)).filter(state=RosterState.APPROVED)
        roster = approved.filter(unit=unit, month=month).first()
        row = roster.grid.get(hospital_id) if roster else None
        off = [(day, row[day.isoformat()]) for day in month_days if row and row.get(day.isoformat())]
        if not off:
            continue
        grid = {nurse_id: dict(cells) for nurse_id, cells in roster.grid.items()}
        for day, _ in off:
            grid[hospital_id][day.isoformat()] = ""
        name = nurse.user.full_name or hospital_id
        reason = f"Leave recorded for {name} took their shift off {', '.join(long_date(day) for day, _ in off)}."
        version = make_version(roster, grid, VersionOrigin.EDIT, by, reason)
        taken.append(TakenOff(roster, version, off, opening.for_leave(roster, version, off)))
    return taken
