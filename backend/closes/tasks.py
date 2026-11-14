"""The month close's jobs. Neither calls the solver service: each works out what it needs in the worker's process."""
from __future__ import annotations

import datetime as dt

from django.db import transaction

from closes import making
from closes.chain import Chain, next_month
from closes.models import CarryOver, CarrySource
from nurses import membership
from nurses.models import NurseProfile
from ops.queue import enqueue_once, hourly, under_its_own_lock
from rosters.models import Roster, RosterState
from units.models import Unit


@hourly(at_minute=30)
def close_month(timestamp: int) -> None:
    """Closes every unit's month whose close time (00:30 by default, on the hospital's clock) has passed and that is not closed,
    so a worker that was down catches up. The schedule is fixed at import and in UTC, so this runs hourly and reads the time
    itself: the close comes at its time where the hospital's offset from UTC is a whole hour, and within the hour otherwise."""
    making.catch_up()


@under_its_own_lock
def project_carry_over(unit_id: str, month: str) -> None:
    """Stores the projected carry-over out of an approved month not closed, for each of its nurses, and goes on to the unit's
    approved months after it, which start from it. A record: an assembly works the chain out again as it stands."""
    unit = Unit.objects.get(pk=unit_id)
    step = dt.date.fromisoformat(month)
    chain = Chain()
    with transaction.atomic():
        while True:
            roster = Roster.objects.filter(unit=unit, month=step, state=RosterState.APPROVED).select_related("unit").first()
            if roster is None or making.is_closed(unit, step):
                return
            carried = chain.carry_out(roster)
            for nurse in NurseProfile.objects.filter(user__username__in=list(carried)).select_related("user"):
                CarryOver.objects.update_or_create(nurse=nurse, month=step, source=CarrySource.PROJECTED,
                                                   defaults={"unit": unit, "values": carried[nurse.user.username]})
            step = next_month(step)


def project(unit: Unit, month: dt.date) -> None:
    """On demand: after anything that changes an approved roster's month (each of its versions) or the close before it. One
    waits at a time for a unit's month."""
    enqueue_once(project_carry_over, f"project_carry_over:{unit.pk}:{month:%Y-%m}", unit_id=str(unit.pk),
                 month=month.isoformat())


def project_later_months(unit: Unit, month: dt.date) -> None:
    """After a close: the approved months after it, in this unit and in those its nurses are in then, start from it now."""
    after = next_month(month)
    nurses = NurseProfile.objects.filter(carry_overs__close__unit=unit, carry_overs__close__month=month).distinct()
    units = {unit.pk} | {placement.unit_id for placement in map(lambda nurse: membership.placement_in(nurse, after), nurses)
                         if placement is not None}
    for roster in Roster.objects.filter(unit_id__in=units, month=after, state=RosterState.APPROVED).select_related("unit"):
        project(roster.unit, after)
