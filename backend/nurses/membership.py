"""Which unit a nurse belongs to, and when. A nurse's placements answer it month by month: the one in force is
the latest that has begun, and any after it are transfers still to come. Every question about that goes here."""
from __future__ import annotations

import calendar
import datetime as dt
from pathlib import Path
from uuid import UUID

from django.conf import settings
from django.db.models import Exists, F, OuterRef, Q, QuerySet, Subquery
from django.utils import timezone

from accounts.models import User
from nurses.models import NurseProfile, Placement
from units.models import Unit


def today() -> dt.date:
    """The hospital's date. Under the browser checks' clock (`settings.E2E_CLOCK_FILE`, never set in an installation) it is
    moved by the file's number of days, read afresh each time, since every process reads it."""
    day = timezone.localdate()
    if settings.E2E_CLOCK_FILE:
        day += dt.timedelta(days=_clock_days(Path(settings.E2E_CLOCK_FILE)))
    return day


def _clock_days(clock: Path) -> int:
    try:
        written = clock.read_text().strip()
    except FileNotFoundError:
        return 0
    return int(written) if written else 0


def this_month() -> dt.date:
    return today().replace(day=1)


def last_day_of(month: dt.date) -> dt.date:
    return month.replace(day=calendar.monthrange(month.year, month.month)[1])


def place_new_nurse(nurse: NurseProfile, unit: Unit) -> None:
    """A nurse starting later belongs to the unit from now, so their unit's manager can prepare and they can see
    it at first login. Days before the start date are the solve input's concern."""
    Placement.objects.create(nurse=nurse, unit=unit, from_month=min(nurse.start_date.replace(day=1), this_month()))


def reach_back_to_start_date(nurse: NurseProfile) -> tuple[Placement, dt.date] | None:
    """A start date corrected to before the first placement would leave months with no unit, so that placement
    begins earlier. It and the month it began are returned, so the caller can say so. Those months had no unit, so
    none is rewritten. It only ever moves back: a placement before the start date means nothing, as `place_new_nurse`
    shows, and moving it forward could leave a nurse with none in force."""
    first = nurse.placements.select_related("unit").first()
    month = nurse.start_date.replace(day=1)
    if month >= first.from_month:
        return None
    began = first.from_month
    first.from_month = month
    first.save(update_fields=["from_month"])
    return first, began


def from_now_on(nurse: NurseProfile) -> list[Placement]:
    """The placement in force, then any still to come."""
    month = this_month()
    placements = list(nurse.placements.all())
    return [p for p in placements if p.from_month <= month][-1:] + [p for p in placements if p.from_month > month]


def placement_in(nurse: NurseProfile, month: dt.date) -> Placement | None:
    """The placement a month falls under, past, present or to come. None before the first, which a start date can never
    be: see `reach_back_to_start_date`."""
    return next((p for p in reversed(list(nurse.placements.all())) if p.from_month <= month), None)


def elsewhere_in(nurse: NurseProfile, unit: Unit, month: dt.date) -> bool:
    """Whether the month falls under another unit's placement. Before their first placement it falls under none."""
    placement = placement_in(nurse, month)
    return placement is not None and placement.unit_id != unit.pk


def ever_placed_in(nurse: NurseProfile, unit: Unit) -> bool:
    return nurse.placements.filter(unit=unit).exists()


def outside_dates(nurse: NurseProfile, day: dt.date) -> bool:
    """Before their start date or after their end date, where days count like leave whatever is recorded."""
    return day < nurse.start_date or (nurse.end_date is not None and day > nurse.end_date)


def no_longer_serving(nurse: NurseProfile) -> bool:
    """An account deactivated while the profile runs on, which only happens to a manager: a staff nurse's account ends with
    their end date (`nurses.tasks.close_ended_nurses`). A deactivated nurse whose end date has come worked until then, so
    their month still counts. Nobody can say when this one stopped."""
    return not nurse.user.is_active and (nurse.end_date is None or nurse.end_date > today())


def unit_in(nurse: NurseProfile, month: dt.date) -> Unit:
    """The unit a nurse belongs to in a month, this one or a later one."""
    return placement_in(nurse, month).unit


def transfer(nurse: NurseProfile, unit: Unit, month: dt.date) -> bool:
    """From this month on the nurse belongs to this unit, whatever was waiting for then or later: a transfer waiting
    is a plan, and the audit log keeps what it said. False when that changes nothing. Call it under the catalogue's
    lock (`HospitalSettings.load(for_update=True)`), because it copies shift codes a rename would change."""
    if unit_in(nurse, month).pk == unit.pk and not nurse.placements.filter(from_month__gt=month).exists():
        return False
    # A placement beginning that very month is where they are coming from only if it has begun: a plan replaced never was.
    began_by = Q(from_month__lte=month) if month <= this_month() else Q(from_month__lt=month)
    coming_from = nurse.placements.filter(began_by).order_by("-from_month").first()
    replaced = list(nurse.placements.filter(from_month__gte=month))
    nurse.placements.filter(from_month__gte=month).delete()
    before = nurse.placements.order_by("-from_month").first()
    if before is None or before.unit_id != unit.pk:
        waiting_there = next((placement for placement in replaced if placement.unit_id == unit.pk), None)
        if waiting_there:  # Its manager may already have chosen, in that unit's own shifts.
            allowed, to_check = waiting_there.allowed_shifts, waiting_there.allowed_shifts_to_check
        else:
            allowed, to_check = _carried_allowed_shifts(coming_from, unit)
        if _night_alone_on_probation(nurse, allowed, unit, month):
            # She works no night shift, so it alone would leave her no shift: the list empties and her new unit's manager is
            # asked to look, as for a list with no shift in common.
            allowed, to_check = None, True
        Placement.objects.create(nurse=nurse, unit=unit, from_month=month, allowed_shifts=allowed,
                                 allowed_shifts_to_check=to_check)
    return True


def _carried_allowed_shifts(placement: Placement, unit: Unit) -> tuple[list[str] | None, bool]:
    """A restriction follows the nurse as far as the new unit works the same shifts. With none in common it has no
    meaning there, and the solver takes no empty list, so the list empties and the manager is asked to look."""
    if placement.allowed_shifts is None:
        return None, False
    kept = [code for code in placement.allowed_shifts if code in unit.rules["shifts"]]
    return kept or None, not kept


def here_from_now_on(nurse: NurseProfile, unit: Unit) -> list[Placement]:
    """The nurse's placements in this unit, in force or still to come. A chain can hold more than one."""
    return [placement for placement in from_now_on(nurse) if placement.unit_id == unit.pk]


def set_allowed_shifts(nurse: NurseProfile, unit: Unit, allowed: list[str] | None) -> bool:
    """Saving is the check, so it also lowers the flag. False when that changes nothing."""
    changed = [placement for placement in here_from_now_on(nurse, unit)
               if placement.allowed_shifts != allowed or placement.allowed_shifts_to_check]
    for placement in changed:
        placement.allowed_shifts, placement.allowed_shifts_to_check = allowed, False
        placement.save(update_fields=["allowed_shifts", "allowed_shifts_to_check"])
    return bool(changed)


def _unit_placements_from_now_on(unit: Unit) -> QuerySet[Placement]:
    in_force = Subquery(Placement.objects.filter(nurse=OuterRef("nurse"), from_month__lte=this_month())
                        .order_by("-from_month").values("pk")[:1])
    return Placement.objects.filter(Q(pk=in_force) | Q(from_month__gt=this_month()), unit=unit)


def _night_alone_on_probation(nurse: NurseProfile, allowed: list[str] | None, unit: Unit, from_month: dt.date,
                              night_shift: str | None = None) -> bool:
    """Allowed shifts that are the unit's night shift alone, on a placement from `from_month` while she is on probation in one
    of its months from now on: she works no night shift, so they would leave her none."""
    from nurses import probation  # It reads her months through this module.

    night = night_shift if night_shift is not None else unit.rules.get("night_shift")
    if allowed is None or allowed != [night]:
        return False
    last = probation.last_day(nurse)
    return last is not None and last >= max(from_month, this_month()) and last >= nurse.start_date


def stranded_by(unit: Unit, shifts: list[str]) -> list[NurseProfile]:
    """The nurses these shifts would leave with none they may work, now or once they join. Only the manager can say
    what they work instead."""
    placements = (_unit_placements_from_now_on(unit).filter(allowed_shifts__isnull=False)
                  .select_related("nurse__user").order_by("nurse__user__full_name", "pk"))
    stranded = {placement.nurse.pk: placement.nurse for placement in placements
                if not any(code in shifts for code in placement.allowed_shifts)}
    return list(stranded.values())


def left_night_alone(unit: Unit, shifts: list[str], night_shift: str) -> list[NurseProfile]:
    """The nurses on probation these shifts, with the unit rules' new night shift, would leave with that night shift alone,
    now or once they join: she works none, so it would leave her no shift (beside `stranded_by`)."""
    placements = (_unit_placements_from_now_on(unit).filter(allowed_shifts__isnull=False)
                  .select_related("nurse__user").order_by("nurse__user__full_name", "pk"))
    left = {placement.nurse.pk: placement.nurse for placement in placements
            if _night_alone_on_probation(placement.nurse, [code for code in placement.allowed_shifts if code in shifts], unit,
                                         placement.from_month, night_shift)}
    return list(left.values())


def narrow_allowed_shifts(unit: Unit, shifts: list[str]) -> None:
    """Keeps every list of a unit within its shifts, which needs no judgement and never widens what a nurse may do.
    Past placements too, because a transfer back into the unit can bring one back into force, and one left with nothing
    is then asked about, as a transfer asks. Writes no audit entry: it follows from the rules' own, and an entry per
    nurse beside the shift that went would tell the System Admin what that nurse could work."""
    placements = [placement for placement in Placement.objects.filter(unit=unit, allowed_shifts__isnull=False)
                  if any(code not in shifts for code in placement.allowed_shifts)]
    for placement in placements:
        kept = [code for code in placement.allowed_shifts if code in shifts]
        placement.allowed_shifts, placement.allowed_shifts_to_check = kept or None, placement.allowed_shifts_to_check or not kept
    Placement.objects.bulk_update(placements, ["allowed_shifts", "allowed_shifts_to_check"])


def carry_rename_through_allowed_shifts(old_code: str, new_code: str) -> None:
    """Every placement, past ones too, so that a list never names a code the catalogue has given up."""
    placements = list(Placement.objects.filter(allowed_shifts__contains=[old_code]))
    for placement in placements:
        placement.allowed_shifts = [new_code if code == old_code else code for code in placement.allowed_shifts]
    Placement.objects.bulk_update(placements, ["allowed_shifts"])


def _unit_in_force(placements: QuerySet[Placement], month: dt.date | None = None) -> Subquery:
    return Subquery(placements.filter(from_month__lte=month or this_month()).order_by("-from_month").values("unit")[:1])


def placed_in(unit: Unit, month: dt.date) -> QuerySet[NurseProfile]:
    """The nurses whose month this is in this unit, past, present or to come, whatever their dates: its manager answers for
    what is recorded on them that month. A nurse who has since left is here for the months they were in it."""
    return (NurseProfile.objects
            .annotate(unit_that_month=_unit_in_force(Placement.objects.filter(nurse=OuterRef("pk")), month))
            .filter(unit_that_month=unit.pk))


def rostered_in(unit: Unit, month: dt.date) -> QuerySet[NurseProfile]:
    """Those of them with a day in the month between their start and end dates. Not one whose end date is before their
    start date, who was never present (`end_date_fits`)."""
    return (placed_in(unit, month)
            .filter(start_date__lte=last_day_of(month))
            .filter(Q(end_date__isnull=True) | (Q(end_date__gte=month) & Q(end_date__gte=F("start_date")))))


def on_the_list_of(unit: Unit) -> QuerySet[NurseProfile]:
    """Who the unit's manager sees: placed there now, or due to join, deactivated or not."""
    return (NurseProfile.objects
            .annotate(placed_now=_unit_in_force(Placement.objects.filter(nurse=OuterRef("pk"))))
            .filter(Q(placed_now=unit.pk)
                    | Exists(Placement.objects.filter(nurse=OuterRef("pk"), unit=unit, from_month__gt=this_month()))))


def unit_of_nurse(user: User) -> QuerySet[Unit]:
    return Unit.objects.filter(pk=_unit_in_force(Placement.objects.filter(nurse__user=user)))


def unit_id_now(nurse: NurseProfile) -> UUID:
    return from_now_on(nurse)[0].unit_id


def placed_outside(user: User, unit_id: UUID | None) -> Unit | None:
    """The unit a manager's profile would be outside of, were they to hold this one: a manager reaches only the
    unit they hold, so a roster of another's would be one they could not see. Any profile counts, an ended one
    too, because clearing its end date would otherwise bring it back elsewhere."""
    nurse = NurseProfile.objects.filter(user=user).first()
    if nurse is None:
        return None
    return next((p.unit for p in from_now_on(nurse) if p.unit_id != unit_id), None)


def belonging_to(unit: Unit) -> QuerySet[NurseProfile]:
    """The nurses who stop a unit being archived: placed there now or due to join, and not ended. A deactivated
    nurse does not count, because nurses are never deleted and the refusal's advice would otherwise be false."""
    return serving(on_the_list_of(unit))


def serving(nurses: QuerySet[NurseProfile]) -> QuerySet[NurseProfile]:
    """Those with an active account whose end date has not passed: today, the last day, still counts."""
    return nurses.filter(user__is_active=True).filter(Q(end_date__isnull=True) | Q(end_date__gte=today()))


def archived_unit_in_the_way(user: User) -> Unit | None:
    """The archived unit this user would land in by coming back into service, if any. Takes the lock on every unit
    they are placed in, as archiving does, so the unit cannot be archived between this answer and the change. A
    profile whose end date has passed does not count: its nurse is coming back to no unit, whatever the account does."""
    nurse = NurseProfile.objects.filter(user=user).first()
    if nurse is None or (nurse.end_date is not None and nurse.end_date < today()):
        return None
    ids = [placement.unit_id for placement in from_now_on(nurse)]
    return next((unit for unit in Unit.objects.select_for_update().filter(pk__in=ids).order_by("pk") if unit.archived),
                None)


def end_date_fits(start: dt.date, end: dt.date) -> bool:
    """On or after the start date, or today or earlier for a nurse who has not started: one added by mistake has to
    be able to end now. That nurse counts as never present."""
    return end >= start or (start > today() and end <= today())


def start_date_fits(was: dt.date, start: dt.date, end: dt.date) -> bool:
    """On or before the end date, or still to come for a nurse who has not started. The exception needs both dates:
    a nurse who really worked must not be corrected into one who was never present, whose days are dropped."""
    return start <= end or (was > today() and end_date_fits(start, end))


def cancel_transfers_after(nurse: NurseProfile, day: dt.date) -> list[Placement]:
    """Those a nurse's end date has overtaken. Never the placement in force, since a nurse always has one."""
    cancelled = list(nurse.placements.select_related("unit").filter(from_month__gt=max(day, this_month())))
    nurse.placements.filter(pk__in=[placement.pk for placement in cancelled]).delete()
    return cancelled

