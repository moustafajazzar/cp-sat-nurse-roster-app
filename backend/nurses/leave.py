"""Leave as a change to the roster in force: the shifts a nurse's leave takes off the approved rosters of her unit. The endpoints
that record leave (`nurses.leave_api`) call it under her lock."""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field

from accounts.models import User
from covers import opening
from covers.models import Cover
from nurses import membership
from nurses.models import NurseProfile
from rosters import in_force
from rosters.approved import NewCell
from rosters.models import Roster, RosterVersion
from rosters.pins import long_date
from units.models import Unit


@dataclass(frozen=True)
class TakenOff:
    roster: Roster
    version: RosterVersion
    days: list[tuple[dt.date, str]]
    covers: dict[dt.date, Cover] = field(default_factory=dict)


def take_off_for_leave(nurse: NurseProfile, unit: Unit, days: Iterable[dt.date], by: User) -> list[TakenOff]:
    """Leave wins over a shift. On each approved roster of this unit for a month not yet over, a shift the roster in force holds
    on one of these days is taken off through `rosters.in_force`, as a change whose reason says leave and never the type. No pin
    is made: the leave is the fact a re-plan keeps. A month that is over keeps its grid, as an edit cannot change it, and the
    month close reads the leave itself. Each shift taken off on a day today or later opens a cover, after the version,
    unoffered and told to nobody. Call it under the nurse's lock, which leave's hold takes the unit's and the rosters' after."""
    hospital_id = nurse.user.username
    by_month: dict[dt.date, list[dt.date]] = {}
    for day in sorted(set(days)):
        if day.replace(day=1) >= membership.this_month():
            by_month.setdefault(day.replace(day=1), []).append(day)
    taken = []
    for held in in_force.hold_for_leave(unit.pk, by_month, nurse=hospital_id):
        # Asked of the hold, which answers once: a month that ended since the days were sorted keeps its grid, as one already
        # over does, and the change below is not refused.
        row = held.roster.grid.get(hospital_id) if held.approved and not held.month_over else None
        off = [(day, row[day.isoformat()]) for day in by_month[held.month] if row and row.get(day.isoformat())]
        if not off:
            continue
        name = nurse.user.full_name or hospital_id
        reason = f"Leave recorded for {name} took their shift off {', '.join(long_date(day) for day, _ in off)}."
        version = in_force.change(held, [NewCell(hospital_id, day, None) for day, _ in off], origin=in_force.Origin.LEAVE,
                                  by=by, reason=reason, pins=0)
        taken.append(TakenOff(held.roster, version, off, opening.for_leave(held.roster, version, off)))
    return taken
