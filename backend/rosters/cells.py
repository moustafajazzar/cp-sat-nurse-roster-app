"""Whether a cell of a unit's month can hold a change: the one place it is judged, for a manager's edit, a swap, a cover and a
request alike, so that no two of them can give different accounts of one fact. A pin exists whether or not the month has a
roster, so the month is read from the nurses, their leave and the unit's rules, and needs none.

Each origin keeps its own rules, checked before it asks here, and every sentence it says to a nurse. Nothing here says anything
to anyone: a reason is a value, worded beside this module for a manager (`rosters.cell_words`) and by each origin for a nurse."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

from hospital.models import Shift
from hospital.rules import required_skills
from nurses import membership, probation
from nurses.models import Leave, NurseProfile
from nurses.rostering import skills_block
from nurses.skills import current_skills
from rosters.drafts import current_code
from rosters.models import PinnedCell, PinOrigin
from solves.input import barred_from
from units.models import Unit

Why = Literal["not_in_month", "outside_dates", "leave", "shift_not_worked", "barred", "shift_not_allowed", "night_on_probation",
              "held"]

# Which pin yields to which change: {the origin asking: the origins of the pins it replaces}. A pin of any other origin holds its
# cell against that change, set aside or not, since a pin set aside is not ended and may count again. Leave asks nothing here:
# it ends every pin on its days.
YIELDS = {
    "edit": {PinOrigin.MANAGER},
    "swap": {PinOrigin.MANAGER, PinOrigin.SWAP, PinOrigin.COVER},
    "cover": {PinOrigin.MANAGER, PinOrigin.SWAP, PinOrigin.COVER},
    "request": {PinOrigin.MANAGER, PinOrigin.REQUEST, PinOrigin.COVER},
}


@dataclass(frozen=True)
class Reason:
    """Why a cell cannot hold what is asked, with the facts a sentence about it needs. `shift` is the code of a shift the
    unit does not work, as it was asked, or of the shift she may not work, as it is now; `skill` and `last` are a bar's skill and
    the last day it counted; `origin` is that of the pin that holds the cell."""
    why: Why
    shift: str | None = None
    skill: str | None = None
    last: dt.date | None = None
    origin: str | None = None


@dataclass(frozen=True)
class Answer:
    """Whether a cell may hold a change: it may, with nothing set; it may and replaces the pin `replaces`; it may not, for
    `reason`."""
    reason: Reason | None = None
    replaces: PinnedCell | None = None


class MonthCells:
    """The cells of a unit's month, read once for every question asked of them. A nurse is named by her hospital ID, as a
    grid and every origin name her."""

    def __init__(self, unit: Unit, month: dt.date):
        self.unit, self.month = unit, month
        serving = [nurse for nurse in membership.rostered_in(unit, month).select_related("user")
                   .prefetch_related(current_skills()) if not membership.no_longer_serving(nurse)]
        # {hospital ID: nurse}: those with a day in the month, whom a solve of it would plan for.
        self.nurses = {nurse.user.username: nurse for nurse in serving}
        self._by_pk = {nurse.pk: nurse for nurse in serving}
        self._leave = set(Leave.objects.current().in_month(month).values_list("nurse_id", "date"))
        worked = Shift.objects.filter(active=True, code__in=unit.rules.get("shifts", []))
        self._worked = {shift.code: shift for shift in worked}
        required = required_skills()
        # {nurse's pk: the bar on her month}, read as her solve input reads her skills, so the two cannot disagree.
        self._bars = {nurse.pk: bar for nurse in serving
                      if (bar := barred_from(skills_block(nurse.current_skills, month), required, month))}
        # The hospital IDs of those on probation this month, who work no night shift: read as her solve input reads it.
        self.on_probation = {hospital_id for hospital_id, nurse in self.nurses.items()
                             if probation.on_probation_in(nurse, month)}
        self._night_shift = unit.rules.get("night_shift")
        pins = PinnedCell.objects.current().in_month(month).filter(unit=unit).select_related("shift", "nurse__user")
        # One left behind by a nurse transferred out is among them: it is this unit's still, and holds its cell here.
        self._pins = {(pin.nurse.user.username, pin.date): pin for pin in pins}

    def shift(self, code: str) -> Shift | None:
        """The shift this code names among those the unit works, an older code of it included; None for any other."""
        return self._worked.get(current_code(code))

    def cannot_hold(self, hospital_id: str, day: dt.date, holds: str | None) -> Reason | None:
        """Why this nurse's cell could not hold `holds`, a shift's code or None for a day off, whatever pin is on it; None when
        it could. A day off is always hers on a day she holds anything, a barred day's too."""
        nurse = self.nurses.get(hospital_id)
        reason = self._day(nurse, day)
        if reason or not holds:
            return reason
        return self._shift(nurse, day, holds, self.shift(holds))

    def set_aside(self, pin: PinnedCell, *, over: bool = False) -> Reason | None:
        """Why this saved pin of the unit's month counts for nothing now: its cell could not hold it, by the judgement a new
        pin there gets with precedence left out. None for a pin that counts. `over` for a month that is over, which its close
        makes fact: the reasons about the day still set a pin aside, but one to a shift the unit has stopped working, or she may
        no longer work, was worked when it was set, and counts."""
        nurse = self._by_pk.get(pin.nurse_id)
        reason = self._day(nurse, pin.date)
        if reason or over or pin.shift is None:
            return reason
        return self._shift(nurse, pin.date, pin.shift.code, self._worked.get(pin.shift.code))

    def may_change(self, hospital_id: str, day: dt.date, asking: str) -> Answer:
        """Whether the origin asking (an edit, a swap, a cover or a request, as `rosters.in_force.Origin` names them) may change
        this nurse's cell at all, whatever it would then hold: it may when no pin is on it, and when the pin on it yields to that
        origin, which the change then replaces. All a clear is asked, since ending a pin can break nothing."""
        pin = self._pins.get((hospital_id, day))
        if pin is None:
            return Answer()
        if pin.origin in YIELDS[asking]:
            return Answer(replaces=pin)
        return Answer(reason=Reason("held", origin=pin.origin))

    def may_hold(self, hospital_id: str, day: dt.date, asking: str, holds: str | None) -> Answer:
        """Whether this nurse's cell may hold `holds`, a shift's code or None for a day off, for the origin asking. What the
        cell cannot hold is said before a pin that does not yield."""
        reason = self.cannot_hold(hospital_id, day, holds)
        return Answer(reason=reason) if reason else self.may_change(hospital_id, day, asking)

    def _day(self, nurse: NurseProfile | None, day: dt.date) -> Reason | None:
        """Why she holds nothing that day, a day off included."""
        if nurse is None:
            return Reason("not_in_month")
        if membership.outside_dates(nurse, day):
            return Reason("outside_dates")
        return Reason("leave") if (nurse.pk, day) in self._leave else None

    def _shift(self, nurse: NurseProfile, day: dt.date, asked: str, shift: Shift | None) -> Reason | None:
        """Why she could not work this shift on a day she is there. `shift` is None for one the unit does not work, which
        `asked` names as whoever asked wrote it."""
        if shift is None:
            return Reason("shift_not_worked", shift=asked)
        bar = self._bars.get(nurse.pk)
        if bar is not None and day >= bar.first:
            return Reason("barred", skill=bar.skill, last=bar.last)
        allowed = membership.placement_in(nurse, self.month).allowed_shifts
        if allowed is not None and shift.code not in allowed:
            return Reason("shift_not_allowed", shift=shift.code)
        if nurse.user.username in self.on_probation and shift.code == self._night_shift:
            return Reason("night_on_probation", shift=shift.code)
        return None
