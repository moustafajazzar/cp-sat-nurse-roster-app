"""Pinned cells: which of a month's count, and what ending one means. A row can fall out of step with the rest of the database
in ways no refusal can stop, since a nurse's dates, leave and placements are others' to change. Whatever it comes to hold, it
is set aside here, in one place, rather than reach the solver as a failed solve, and the manager is told why. Nothing is
rewritten, as with leave a correction leaves outside a nurse's dates: if the dates come back, so does the pin."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Literal

from django.utils import timezone

from accounts.models import User
from hospital.models import Shift
from hospital.rules import required_skills
from nurses import membership, probation
from nurses.models import Leave, NurseProfile
from nurses.rostering import skills_block
from nurses.skills import current_skills
from rosters.models import PinEnded, PinnedCell
from units.models import Unit

if TYPE_CHECKING:
    from solves.input import Bar

def long_date(day: dt.date) -> str:
    return f"{day.day} {day:%B %Y}"


AsideWhy = Literal["not_in_month", "outside_dates", "leave", "shift_not_worked", "shift_not_allowed", "barred", "night_on_probation"]


class Judge:
    """Why a nurse could not hold a pin on a date in this unit's month, in the same words for a saved pin and a pending one."""

    def __init__(self, unit: Unit, month: dt.date):
        from solves.input import barred_from  # The solve input reads the month's pins through this.

        self.unit, self.month = unit, month
        self.nurses = {nurse.pk: nurse for nurse in membership.rostered_in(unit, month).select_related("user")
                       .prefetch_related(current_skills()) if not membership.no_longer_serving(nurse)}
        self.leave = set(Leave.objects.current().in_month(month).values_list("nurse_id", "date"))
        self.worked = {shift.pk: shift for shift in Shift.objects.filter(active=True, code__in=unit.rules.get("shifts", []))}
        required = required_skills()
        # {nurse's pk: the bar on her month}, read as her solve input reads her skills, so the two cannot disagree.
        self.bars = {pk: bar for pk, nurse in self.nurses.items()
                     if (bar := barred_from(skills_block(nurse.current_skills, month), required, month))}
        # Those on probation this month, who work no night shift : read as her solve input reads it.
        self.on_probation = {pk for pk, nurse in self.nurses.items() if probation.on_probation_in(nurse, month)}
        self.night_shift = unit.rules.get("night_shift")

    def day_why(self, nurse_id, day: dt.date) -> AsideWhy | None:
        nurse = self.nurses.get(nurse_id)
        if nurse is None:
            return "not_in_month"
        if membership.outside_dates(nurse, day):
            return "outside_dates"
        return "leave" if (nurse_id, day) in self.leave else None

    def bar(self, nurse_id, day: dt.date) -> Bar | None:
        """What keeps her off every shift on this day: a required skill of hers that expired before it."""
        bar = self.bars.get(nurse_id)
        return bar if bar is not None and day >= bar.first else None

    def bar_said(self, nurse_id, day: dt.date, whose: str) -> str:
        """"Amal's BLS, required to work, counted until 20 June 2026", with `whose` "Amal's" or "Your": how every sentence that
        says the bar on her day in full begins. Only for a day `bar` holds."""
        bar = self.bar(nurse_id, day)
        return f"{whose} {bar.skill}, required to work, counted until {long_date(bar.last)}"

    def shift_why(self, nurse_id, day: dt.date, shift: Shift | None) -> AsideWhy | None:
        """Why she could not work this shift on this day. A day off is always hers, a barred day's too, as a leave day's is."""
        if shift is None:
            return None
        if shift.pk not in self.worked:
            return "shift_not_worked"
        if self.bar(nurse_id, day):
            return "barred"
        allowed = membership.placement_in(self.nurses[nurse_id], self.month).allowed_shifts
        if allowed is not None and shift.code not in allowed:
            return "shift_not_allowed"
        return "night_on_probation" if nurse_id in self.on_probation and shift.code == self.night_shift else None

    def why(self, nurse_id, day: dt.date, shift: Shift | None) -> AsideWhy | None:
        return self.day_why(nurse_id, day) or self.shift_why(nurse_id, day, shift)

    def refusal(self, nurse_id, name: str, day: dt.date, asked: str | None, shift: Shift | None) -> str | None:
        """Why this nurse could not be pinned to `asked`, a shift's code or None for a day off, on this day, in the words a save
        of pins and a request's decision both use, so the two cannot disagree. `shift` is that code's shift, if it has one."""
        match self.day_why(nurse_id, day):
            case "not_in_month":
                return f"{name} is not on this unit's roster for {self.month:%B %Y}."
            case "outside_dates":
                return f"{long_date(day)} is outside {name}'s dates, so there is nothing to pin."
            case "leave":
                return f"{name} is on leave on {long_date(day)}, and leave wins over a pin."
        if asked is None:
            return None
        match self.shift_why(nurse_id, day, shift) if shift else "shift_not_worked":
            case "shift_not_worked":
                return f"{asked} is not one of this unit's shifts."
            case "barred":
                return f"{self.bar_said(nurse_id, day, name + "'s")}, so {name} can work no shift on {long_date(day)}."
            case "shift_not_allowed":
                return f"{shift.code} is not one of {name}'s allowed shifts."
            case "night_on_probation":
                return (f"{name} is on probation in {self.month:%B %Y}, and a nurse on probation works no {shift.code}, the "
                        "night shift.")
        return None


@dataclass
class MonthPins:
    # {hospital ID: {date: shift code or ""}}: each nurse's `pinned` in a solve input.
    counted: dict[str, dict[str, str]] = field(default_factory=dict)
    # Each pin of the month, with `aside` set on those that do not count.
    rows: list[PinnedCell] = field(default_factory=list)


def month_pins(unit: Unit, month: dt.date, *, facts: bool = False) -> MonthPins:
    """`facts` for a month that is over, which its close makes fact: a pin set aside because of its day (the nurse not
    in the month, outside her dates, on leave) still counts for nothing, but one on a shift the unit has stopped working, or the
    nurse may no longer work, since it was set was worked, and counts, under the shift's code now."""
    judge = Judge(unit, month)
    pins = MonthPins()
    rows = PinnedCell.objects.current().in_month(month).filter(unit=unit).select_related("shift", "nurse__user")
    for pin in rows.order_by("nurse__user__username", "date"):
        pin.aside = judge.day_why(pin.nurse_id, pin.date) if facts else judge.why(pin.nurse_id, pin.date, pin.shift)
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
