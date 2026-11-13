"""How a cover opens: by itself when leave takes a shift off the roster in force, or by the unit's manager's hand. Neither tells
anyone or writes to the audit log: a cover opened by leave waits unoffered until the manager acts, so annual leave planned weeks
ahead sends nobody anything, and an entry beside the leave's own would date it for the System Admin."""
from __future__ import annotations

import datetime as dt

from hospital.models import Shift
from nurses import membership
from rosters.drafts import current_code
from rosters.models import Roster, RosterVersion
from rosters.pins import long_date
from units.models import Unit

from covers.models import Cover, Opened


def for_leave(roster: Roster, version: RosterVersion, days: list[tuple[dt.date, str]]) -> dict[dt.date, Cover]:
    """One cover per shift this leave's version took off, on a day today or later on the hospital's clock: a day passed was
    worked or missed, and is offered to nobody. Called by `rosters.versions.take_off_for_leave` in the leave's transaction, after
    the version, holding the roster's row lock."""
    today = membership.today()
    shifts = {shift.code: shift for shift in Shift.objects.filter(code__in={current_code(code) for _, code in days})}
    return {day: Cover.objects.create(unit=roster.unit, roster=roster, opened_on=version, date=day,
                                      shift=shifts[current_code(code)], opened=Opened.LEAVE)
            for day, code in days if day >= today and current_code(code) in shifts}


def refusal(unit: Unit, day: dt.date, code: str) -> tuple[str, str] | None:
    """Why the manager may not open a cover for this shift-day, with the field it is about, or None."""
    if day < membership.today():
        return "date", f"{long_date(day)} has passed, so it cannot be covered."
    worked = Shift.objects.filter(code=current_code(code), active=True).exists()
    if current_code(code) not in unit.rules.get("shifts", []) or not worked:
        return "shift", f"{code} is not one of this unit's shifts."
    return None


def by_hand(roster: Roster, day: dt.date, code: str) -> Cover:
    """On the roster in force, for a shift-day `refusal` allows. Hold the unit's row lock, then the roster's, so the version it
    reads as in force is the one a fill would compare against."""
    return Cover.objects.create(unit=roster.unit, roster=roster, opened_on=roster.versions.first(), date=day,
                                shift=Shift.objects.get(code=current_code(code)), opened=Opened.MANAGER)
