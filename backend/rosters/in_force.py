"""Every change to the roster in force, whatever its origin, in two steps.

`hold` takes a unit's month under the locks every change takes, in one fixed order: the catalogue's (which a solve's start and
end, a restore and a shift's rename hold), then the named nurses' (which a leave write holds, and which come before a unit's in a
transfer), then the unit's and the month's roster's (as a submit and an approval take them). It never refuses: the month's roster
may be a draft, approved or absent, and the origin decides what that means for it.

Between the two steps the origin does what only it knows: checks the user's scope on the held unit, locks its own row (a swap, a
cover, a request), judges whether its change may happen, and makes, replaces or ends its own pins. Pins stay with the origins.

`change` then takes the cells that change and does, for every origin alike, everything that follows: the grid after, the next
version in force at once, waiting swaps it overtakes expired, waiting covers it fills filled, the month's carry-over projected,
the audit entry and the unit's nurses told. What differs between origins is stated once, in `POLICY`. A change that alters no cell
makes nothing and is answered with `UNCHANGED`, which each origin words its own way.

A version's reason is for the unit's manager and the approvers only: it can say a cell was leave. It never reaches a nurse, a
notification or the audit log."""
from __future__ import annotations

import datetime as dt
import enum
from collections.abc import Collection, Iterable
from dataclasses import dataclass

from django.http import HttpRequest

from accounts.models import User
from audit.log import record
from hospital.models import HospitalSettings
from notifications import sending
from nurses import membership
from nurses.models import NurseProfile
from rosters import review
from rosters.approved import Change
from rosters.drafts import current_codes
from rosters.models import Roster, RosterState, RosterVersion, VersionOrigin
from rosters.versions import differences, make_version
from solves.models import Solve
from units.models import Unit

MONTH_OVER = "This month is over, so its roster in force cannot change."
NOT_APPROVED = "This month's roster is not approved, so there is no roster in force to change."


class Refused(Exception):
    """Why the roster in force cannot change now, in words for whoever asked. An endpoint answers it with a 409; a job maps it
    to its own outcome."""


class NotApproved(Refused):
    def __init__(self):
        super().__init__(NOT_APPROVED)


class MonthOver(Refused):
    def __init__(self):
        super().__init__(MONTH_OVER)


class Origin(enum.StrEnum):
    """What causes a change, as this module's callers name it. Leave is one in its own right, though its versions are stored,
    and listed, as edits."""
    EDIT = "edit"
    SWAP = "swap"
    COVER = "cover"
    REQUEST = "request"
    REPLAN = "replan"
    LEAVE = "leave"


@dataclass(frozen=True)
class Policy:
    stored: VersionOrigin
    audited: bool


# Every difference between origins, decided here rather than left to each origin's code. Leave writes no roster-change entry:
# its own entry already names the nurse, and one beside it would tell the System Admin which month she was on leave.
POLICY = {
    Origin.EDIT: Policy(VersionOrigin.EDIT, audited=True),
    Origin.SWAP: Policy(VersionOrigin.SWAP, audited=True),
    Origin.COVER: Policy(VersionOrigin.COVER, audited=True),
    Origin.REQUEST: Policy(VersionOrigin.REQUEST, audited=True),
    Origin.REPLAN: Policy(VersionOrigin.REPLAN, audited=True),
    Origin.LEAVE: Policy(VersionOrigin.EDIT, audited=False),
}


class WholeMonth:
    """Every nurse leave could be recorded for in the month, as a re-plan needs: the month's own and every row on its grid."""

    def __repr__(self) -> str:
        return "WHOLE_MONTH"


WHOLE_MONTH = WholeMonth()


@dataclass
class Held:
    """A unit's month under the locks of a change. `unit` and `roster` are the rows as locked, so read after anything that was
    waited for; `unit` is None for no such unit, and `roster` None for a month with no roster yet."""
    unit: Unit | None
    month: dt.date
    roster: Roster | None
    nurses: frozenset[str]
    whole_month: bool

    @property
    def approved(self) -> bool:
        return self.roster is not None and self.roster.state == RosterState.APPROVED

    @property
    def month_over(self) -> bool:
        return self.month < membership.this_month()

    def refusal(self) -> Refused | None:
        """What `change` would refuse, asked beforehand by an origin that refuses before it judges. A roster not approved comes
        first: a draft has no roster in force, over or not."""
        if not self.approved:
            return NotApproved()
        if self.month_over:
            return MonthOver()
        return None


def hold(unit_id, month: dt.date, *, nurses: Collection[str] | WholeMonth) -> Held:
    """Locks the month for a change of these nurses' cells (hospital IDs), or of any nurse's with `WHOLE_MONTH`. Call it inside
    the transaction that changes, before reading anything the change depends on. The nurses' locks are `FOR NO KEY UPDATE`, which
    serialise as `FOR UPDATE` would but let a colleague's leave commit its notifications, whose foreign keys to users are
    checked then, while this waits on the roster it holds. The unit's and the roster's own rows only, so no user row is locked
    here: a manager's deactivation locks hers before her unit's."""
    HospitalSettings.load(for_update=True)
    whole = isinstance(nurses, WholeMonth)
    if whole:
        unit = Unit.objects.filter(pk=unit_id).first()
        roster = Roster.objects.filter(unit_id=unit_id, month=month).first()
        locked = review.lock_the_months_nurses(unit, month, roster) if unit else []
    else:
        locked = list(NurseProfile.objects.select_for_update(of=("self", "user"), no_key=True).select_related("user")
                      .filter(user__username__in=list(nurses)).order_by("pk"))
    unit = Unit.objects.select_for_update(of=("self",)).filter(pk=unit_id).first()
    roster = None
    if unit is not None:
        roster = (Roster.objects.select_for_update(of=("self",)).select_related("solve", "unit")
                  .filter(unit=unit, month=month).first())
    return Held(unit=unit, month=month, roster=roster, nurses=frozenset(nurse.user.username for nurse in locked),
                whole_month=whole)


def hold_for_leave(unit_id, months: Iterable[dt.date], *, nurse: str) -> list[Held]:
    """Leave's own, narrower hold: each of these months of the unit, in month order, for taking one nurse's (hospital ID)
    shifts off. Call it under her lock, which every leave write takes first. It takes no catalogue lock, so recording leave
    never waits on a solve or a rename there.

    The unit is taken `FOR KEY SHARE`, the weakest lock there is. A cover leave opens refers to its unit, whose foreign key is
    checked at commit under that lock; taking it here, before any roster, keeps the order unit, roster, so an edit or a rename
    holding the unit while it waits on a roster is waited for instead of deadlocked with. A leave write of another unit's nurse,
    or a read of the unit, never waits on it. Then each month's roster, approved ones only: a draft keeps its shift until a solve
    or an edit, so it is neither locked nor held, and its month is held with no roster. Rosters are locked by month, as a rename
    locks them, so the two never wait on each other both ways."""
    months = sorted(set(months))
    if not months:
        return []
    # The ORM takes no `FOR KEY SHARE`.
    unit = next(iter(Unit.objects.raw(f'SELECT * FROM "{Unit._meta.db_table}" WHERE "id" = %s FOR KEY SHARE', [unit_id])),
                None)
    held = []
    for month in months:
        roster = None
        if unit is not None:
            roster = (Roster.objects.select_for_update(of=("self",)).select_related("solve", "unit")
                      .filter(unit=unit, month=month, state=RosterState.APPROVED).first())
        held.append(Held(unit=unit, month=month, roster=roster, nurses=frozenset({nurse}), whole_month=False))
    return held


class Unchanged:
    """The answer to a change that alters no cell: no version, no audit entry, nobody told."""

    def __repr__(self) -> str:
        return "UNCHANGED"


UNCHANGED = Unchanged()


def _grid_after(held: Held, cells: Iterable[Change]) -> dict[str, dict[str, str]]:
    """The roster's grid with these cells set, in today's codes. A cell is a nurse's day on the grid: no row is added or taken
    away, and a cell outside the grid, or of a nurse not held, is the origin's mistake, since judging refuses it first."""
    grid = {nurse: dict(row) for nurse, row in held.roster.grid.items()}
    cells = list(cells)
    codes = current_codes(cell.shift for cell in cells if cell.shift)
    for cell in cells:
        if not held.whole_month and cell.hospital_id not in held.nurses:
            raise ValueError(f"{cell.hospital_id} was not held for this change.")
        if cell.day not in grid.get(cell.hospital_id, ()):
            raise ValueError(f"{cell.hospital_id} has no cell on {cell.day} in the roster of {held.roster.audit_target}.")
        grid[cell.hospital_id][cell.day] = codes[cell.shift] if cell.shift else ""
    return grid


def change(held: Held, cells: Iterable[Change], *, origin: Origin, by: User, why: str, pins: int, solve: Solve | None = None,
           request: HttpRequest | None = None) -> RosterVersion | Unchanged:
    """Changes the roster in force held by `held`: the next version, or `UNCHANGED` when no cell differs. `pins` is how many
    pins the origin made or ended for it, for the audit entry; `solve` the one the grid came from, if any; `request` the HTTP
    request that asked, if any, whose client address the audit entry keeps. Refuses (`Refused`) a roster that is not approved
    and a month that is over. Whoever made the change is not told of it."""
    refused = held.refusal()
    if refused:
        raise refused
    roster, policy = held.roster, POLICY[origin]
    grid = _grid_after(held, cells)
    changed = len(differences(roster.grid, grid))
    if not changed:
        return UNCHANGED
    version = make_version(roster, grid, policy.stored, by, why, solve=solve)
    if policy.audited:
        record("roster.changed", actor=by, target=roster.audit_target, request=request,
               after={"version": version.number, "pins": pins, "cells": changed})
    sending.roster_changed(roster.unit, roster.month, by)
    return version
