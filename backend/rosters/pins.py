"""Pinned cells: which of a month's count, how one is made, and what ending one means. A row can fall out of step with the
rest of the database in ways no refusal can stop, since a nurse's dates, leave and placements are others' to change. Whatever it
comes to hold, it is set aside, by the one judgement of whether its cell could hold it now (`rosters.cells`), rather than reach
the solver as a failed solve, and the manager is told why. Nothing is rewritten, as with leave a correction leaves outside a
nurse's dates: if the dates come back, so does the pin."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Iterable

from django.utils import timezone

from accounts.models import User
from hospital.models import Shift
from nurses import membership
from nurses.models import NurseProfile
from rosters.models import PinEnded, PinnedCell
from units.models import Unit


def long_date(day: dt.date) -> str:
    return f"{day.day} {day:%B %Y}"


@dataclass
class MonthPins:
    # {hospital ID: {date: shift code or ""}}: each nurse's `pinned` in a solve input.
    counted: dict[str, dict[str, str]] = field(default_factory=dict)
    # Each pin of the month, with `aside` the reason (`rosters.cells.Reason`) on those that do not count, and None on the rest.
    rows: list[PinnedCell] = field(default_factory=list)


def month_pins(unit: Unit, month: dt.date, *, over: bool = False) -> MonthPins:
    """Each pin of the unit's month with why it is set aside, as `rosters.cells` judges it. `over` for a month that is over,
    which its close makes fact: a pin set aside because of its day (the nurse not in the month, outside her dates, on leave)
    still counts for nothing, but one on a shift the unit has stopped working, or the nurse may no longer work, since it was set
    was worked, and counts, under the shift's code now."""
    from rosters.cells import MonthCells  # It reads a bar through the solve input, which reads the month's pins through this.

    cells = MonthCells(unit, month)
    pins = MonthPins()
    for pin in cells.pins:
        pin.aside = cells.set_aside(pin, over=over)
        pins.rows.append(pin)
        if pin.aside is None:
            pins.counted.setdefault(pin.nurse.user.username, {})[pin.date.isoformat()] = pin.code
    return pins


def on_top(grid: dict[str, dict[str, str]], pins: MonthPins) -> dict[str, dict[str, str]]:
    """A nurse not in this grid joined after it was solved: their pins wait for the next solve."""
    return {nurse: row | {day: code for day, code in pins.counted.get(nurse, {}).items() if day in row}
            for nurse, row in grid.items()}


def end(rows: Iterable[PinnedCell], why: PinEnded, by: User) -> None:
    """Only those still current: one already ended keeps who ended it, when and why."""
    PinnedCell.objects.current().filter(pk__in=[row.pk for row in rows]).update(removed_at=timezone.now(), removed_by=by,
                                                                                removed_why=why)


def pin_cell(unit: Unit, nurse: NurseProfile, day: dt.date, shift: Shift | None, origin: str, by: User) -> PinnedCell:
    """The one way a pin is made. Whatever pin the nurse held that day ends as replaced: this unit's, or one a transfer left
    behind in their last unit, set aside there, since this month is this unit's. Hold the nurse's lock."""
    end(PinnedCell.objects.current().filter(nurse=nurse, date=day), PinEnded.REPLACED, by)
    return PinnedCell.objects.create(unit=unit, nurse=nurse, date=day, shift=shift, origin=origin, pinned_by=by)


def end_for_leave(nurse: NurseProfile, days: Iterable[dt.date], by: User) -> list[PinnedCell]:
    """Leave wins over a pin. Every pin on these days ends, one a transfer left behind in another unit included: the caller
    says only its own unit's to the manager, as the rest are another manager's. Call it under the nurse's lock, as a leave write
    and a pin's save both hold it."""
    rows = list(PinnedCell.objects.current().filter(nurse=nurse, date__in=list(days)).select_related("shift"))
    end(rows, PinEnded.LEAVE, by)
    return rows


def _counted_from_now_on(unit: Unit) -> list[PinnedCell]:
    """A month that is over can reach no solve, so nothing done now can leave its pins worse off."""
    months = {day.replace(day=1) for day in PinnedCell.objects.current().filter(unit=unit, date__gte=membership.this_month())
              .values_list("date", flat=True)}
    return [pin for month in sorted(months) for pin in month_pins(unit, month).rows if pin.aside is None]


def words_for(pins: Iterable[PinnedCell]) -> str:
    return "; ".join(f"{pin.nurse.user.full_name or pin.nurse.user.username} on {long_date(pin.date)}" for pin in pins)


def on_shifts_going(unit: Unit, shifts: list[str]) -> list[PinnedCell]:
    """Pins the unit's rules could no longer hold with only these shifts. Hold the unit's lock and the catalogue's, as both a
    save of the rules and of a pin do. The solver refuses a pin on a shift its unit does not work."""
    return [pin for pin in _counted_from_now_on(unit) if pin.shift and pin.code not in shifts]


def outside_allowed(nurse: NurseProfile, unit: Unit, allowed: list[str] | None) -> list[PinnedCell]:
    """Pins of this nurse that would break this list, which only makes a month infeasible. A list that empties allows any."""
    if allowed is None:
        return []
    return [pin for pin in _counted_from_now_on(unit) if pin.nurse_id == nurse.pk and pin.shift and pin.code not in allowed]


def lost_to_leave(unit: Unit, month: dt.date, since) -> list[PinnedCell]:
    """Pins of the unit's month that leave recorded after `since` ended, by nurse and date."""
    return list(PinnedCell.objects.in_month(month).filter(unit=unit, removed_why=PinEnded.LEAVE, removed_at__gt=since)
                .select_related("nurse__user", "shift", "removed_by").order_by("nurse__user__username", "date"))


def counted_uses(code: str, since: dt.date) -> list[tuple[Unit, dt.date]]:
    """The units' months from `since` on where a pin that counts holds this shift, soonest first. A month that is over can
    reach no solve, and a pin set aside no solve either, so neither may hold up the CNO: month close has to read
    them itself."""
    pairs = PinnedCell.objects.current().filter(shift__code=code, date__gte=since).select_related("unit").order_by("date")
    uses: dict[tuple, tuple[Unit, dt.date]] = {}
    for pin in pairs:
        month = pin.date.replace(day=1)
        uses.setdefault((pin.unit_id, month), (pin.unit, month))
    return [(unit, month) for unit, month in uses.values() if _holds(month_pins(unit, month), code)]


def _holds(pins: MonthPins, code: str) -> bool:
    return any(pin.code == code and pin.aside is None for pin in pins.rows)
