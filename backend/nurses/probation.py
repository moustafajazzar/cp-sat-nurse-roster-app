"""A nurse's probation: every question about it goes here, so the row, the Nurses list, the
staff list, the allowed shifts' refusal, the hourly notification and a solve input read one answer.

It begins on her start date and is counted in whole calendar months: the month she joins counts when she joins on the 1st,
and not otherwise. Its last day is derived from the start date, the length (`probation_months`) and an early end
(`probation_ended_in`), never stored, so a corrected start date moves it. An early end is a month: its last day becomes the
probation's, whether her unit manager pressed End in it or the CNO's untick kept the months up to it because their rosters
were approved. The CNO's untick that keeps no month leaves no probation at all."""
from __future__ import annotations

import datetime as dt

from nurses import membership
from nurses.models import NurseProfile, Placement
from rosters.models import Roster, RosterState

FIRST_LENGTHS = (1, 2, 3)  # What her unit manager may choose while the length is 3 or less.
LONGEST = 6
TICK_WITHIN = 6  # Months from her start date, counted the same way, in which the CNO may tick a nurse already added.


def months_on(month: dt.date, count: int) -> dt.date:
    """The first day of the month `count` months after `month`'s (before it, for a negative count)."""
    index = month.year * 12 + month.month - 1 + count
    return dt.date(index // 12, index % 12 + 1, 1)


def first_counted_month(start: dt.date) -> dt.date:
    return start.replace(day=1) if start.day == 1 else months_on(start.replace(day=1), 1)


def last_day_after(start: dt.date, months: int) -> dt.date:
    """The last day of a probation of `months` months from `start`, with no early end."""
    return membership.last_day_of(months_on(first_counted_month(start), months - 1))


def has_probation(nurse: NurseProfile) -> bool:
    """Ticked, or unticked with approved months kept."""
    return nurse.on_probation or nurse.probation_ended_in is not None


def last_day(nurse: NurseProfile) -> dt.date | None:
    if not has_probation(nurse):
        return None
    day = last_day_after(nurse.start_date, nurse.probation_months)
    if nurse.probation_ended_in is not None:
        day = min(day, membership.last_day_of(nurse.probation_ended_in))
    return day


def on_probation_in(nurse: NurseProfile, month: dt.date) -> bool:
    """Whether she is on probation in a month: from her start date's month to her probation's last, as the solve input reads it."""
    end = last_day(nurse)
    return end is not None and nurse.start_date.replace(day=1) <= month <= end


def running(nurse: NurseProfile, today: dt.date | None = None) -> bool:
    """Not over yet, on the hospital's clock: under way, or to begin on her start date."""
    end = last_day(nurse)
    return end is not None and end >= (today or membership.today()) and end >= nurse.start_date


def ended_early(nurse: NurseProfile) -> bool:
    return nurse.on_probation and nurse.probation_ended_in is not None


def first_month_not_approved(nurse: NurseProfile) -> dt.date:
    """From her start date's month on, the first whose roster, in the unit that month falls under, is not approved."""
    month = nurse.start_date.replace(day=1)
    placements = list(nurse.placements.all())
    approved = {(unit_id, roster_month) for unit_id, roster_month in Roster.objects.filter(
        unit__in={placement.unit_id for placement in placements}, month__gte=month, state=RosterState.APPROVED,
    ).values_list("unit_id", "month")}
    while True:
        placement = _placement_in(placements, month)
        if placement is None or (placement.unit_id, month) not in approved:
            return month
        month = months_on(month, 1)


def _placement_in(placements: list[Placement], month: dt.date) -> Placement | None:
    return next((p for p in reversed(placements) if p.from_month <= month), None)


def describe(nurse: NurseProfile) -> dict:
    """Her probation's values as an audit entry keeps them."""
    end = last_day(nurse)
    return {"on_probation": nurse.on_probation, "months": nurse.probation_months, "ended_early": ended_early(nurse), "last_day": end.isoformat() if end else None}


def is_night_alone(allowed: list[str] | None, unit) -> bool:
    """Allowed shifts that are the unit's night shift alone, which a nurse on probation does not work: she would be left with
    no shift."""
    return allowed is not None and allowed == [unit.rules.get("night_shift")]


def night_alone(placements: list[Placement]) -> list[Placement]:
    return [p for p in placements if is_night_alone(p.allowed_shifts, p.unit)]


def tick(nurse: NurseProfile) -> None:
    """The CNO's tick on a nurse already added: a probation from her start date, at the length it stands at until her unit
    manager chooses, with no early end and nothing told yet."""
    nurse.on_probation = True
    nurse.probation_months = FIRST_LENGTHS[-1]
    nurse.probation_ended_in = None
    nurse.probation_notified_for = None


def untick(nurse: NurseProfile) -> None:
    """The CNO's untick: the probation goes from the first month whose roster is not approved, so an approved roster is not
    rewritten behind its unit. The months before stay hers as they were planned, as an early end would keep them."""
    kept_until = months_on(first_month_not_approved(nurse), -1)
    if nurse.probation_ended_in is not None:
        kept_until = min(kept_until, nurse.probation_ended_in)
    nurse.on_probation = False
    nurse.probation_ended_in = kept_until if kept_until >= nurse.start_date.replace(day=1) else None
