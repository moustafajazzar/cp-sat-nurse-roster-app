"""Which nurse precepts a nurse on probation, month by month: every question
about it goes here, so her row, the solve input, the notifications and the endings read one answer.

An assignment says from a month on who precepts her. The latest that has begun is in force, as a placement is, and only in the
months she is on probation (`probation.on_probation_in`): an end, an extension or an untick moves it with nothing written. A
nurse leaving the assignment's unit ends it for good with her last month there (`end_on_leaving`): the preceptor's transfer or
end date, or her own transfer. The skill that let a nurse precept is read when assigning only, so its expiring later, its mark
being cleared or its record being taken off ends nothing."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.db.models import Q

from accounts.models import Role, User
from nurses import membership, probation
from nurses.models import NurseProfile, PreceptorAssignment
from nurses.skills import counted_expiry, tracked
from rosters.models import Roster, RosterState
from units.models import Unit


def _rows(nurse: NurseProfile) -> list[PreceptorAssignment]:
    """Hers, in month order: a prefetch of `preceptor_assignments` (with `preceptor__user`) is read when there is one."""
    return list(nurse.preceptor_assignments.all())


def assignment_in(nurse: NurseProfile, month: dt.date, rows: list[PreceptorAssignment] | None = None) -> PreceptorAssignment | None:
    """The assignment in force in a month: the latest begun by then, while she is on probation and no one has left the unit."""
    if not probation.on_probation_in(nurse, month):
        return None
    latest = next((row for row in reversed(_rows(nurse) if rows is None else rows) if row.from_month <= month), None)
    if latest is None or (latest.until is not None and month > latest.until):
        return None
    return latest


def probation_months(nurse: NurseProfile) -> list[dt.date]:
    """Each month she is on probation, from her start date's month to her probation's last."""
    last = probation.last_day(nurse)
    if last is None:
        return []
    month, months = nurse.start_date.replace(day=1), []
    while month <= last:
        months.append(month)
        month = probation.months_on(month, 1)
    return months


@dataclass(frozen=True)
class Span:
    """One preceptor over the months they precept her, from `first` to `last`; `assignment` is the first of those months'."""
    assignment: PreceptorAssignment
    first: dt.date
    last: dt.date


def spans(nurse: NurseProfile, since: dt.date) -> list[Span]:
    """Her preceptors from `since` on, each over the months it is in force."""
    rows, found = _rows(nurse), []
    for month in probation_months(nurse):
        if month < since:
            continue
        row = assignment_in(nurse, month, rows)
        if row is None:
            continue
        # One preceptor's months run on across two of her assignments, as a choice from a later month makes.
        if (found and found[-1].assignment.preceptor_id == row.preceptor_id
                and probation.months_on(found[-1].last, 1) == month):
            found[-1] = Span(row, found[-1].first, month)
        else:
            found.append(Span(row, month, month))
    return found


def precepted_in(preceptor: NurseProfile, months: list[dt.date], *, but: NurseProfile | None = None) -> tuple[NurseProfile, dt.date] | None:
    """Another nurse on probation she precepts in one of these months, with the first such month."""
    rows = PreceptorAssignment.objects.filter(preceptor=preceptor).select_related("nurse__user")
    if but is not None:
        rows = rows.exclude(nurse=but)
    for row in rows:
        theirs = _rows(row.nurse)
        hit = next((month for month in months if (held := assignment_in(row.nurse, month, theirs)) and held.pk == row.pk), None)
        if hit is not None:
            return row.nurse, hit
    return None


def qualifies(nurse: NurseProfile, day: dt.date) -> bool:
    """She holds a live skill the catalogue marks as letting a nurse precept, unexpired on this day. A retired skill is tracked
    no more (`nurses.skills.tracked`), as a solve input and the expiries read it, so it lets no one precept."""
    rows = nurse.skills.current().select_related("skill")
    return any(row.skill.lets_a_nurse_precept and ((expiry := counted_expiry(row)) is None or expiry >= day)
               for row in tracked(rows))


def month_refusal(nurse: NurseProfile, unit: Unit, month: dt.date) -> str | None:
    """Why this unit's manager cannot choose her preceptor from this month: over, outside her probation, another unit's, or a
    month whose roster here is approved. None when he can."""
    name = nurse.user.full_name
    if month < membership.this_month():
        return f"{month:%B %Y} is over. A preceptor is chosen from this month or later."
    if not probation.on_probation_in(nurse, month):
        return f"{name} is not on probation in {month:%B %Y}: her probation ends on {probation.last_day(nurse):%-d %B %Y}."
    if membership.unit_in(nurse, month).pk != unit.pk:
        return f"{name} is not in {unit.name} in {month:%B %Y}, so that unit's manager chooses her preceptor there."
    if Roster.objects.filter(unit=unit, month=month, state=RosterState.APPROVED).exists():
        return (f"The roster of {month:%B %Y} is approved, so a preceptor can be chosen from a later month only. To change who "
                f"works beside her in {month:%B %Y}, edit the roster.")
    return None


def months_to_choose(nurse: NurseProfile, unit: Unit) -> list[dt.date]:
    """The months `month_refusal` lets this unit's manager choose her preceptor from."""
    return [month for month in probation_months(nurse) if month_refusal(nurse, unit, month) is None]


def name_of(nurse: NurseProfile) -> str:
    return nurse.user.full_name or nurse.user.username


def why_not(nurse: NurseProfile, unit: Unit, candidate: NurseProfile, month: dt.date) -> str | None:
    """Why this nurse cannot precept her from that month on, or None: the spec's refusals, in the order a manager would check
    them. The months are hers on probation from that month on."""
    name = name_of(candidate)
    if candidate.pk == nurse.pk:
        return f"{name} cannot precept herself."
    if candidate.user.role not in (Role.STAFF_NURSE, Role.UNIT_MANAGER):
        return f"{name} is not a nurse of {unit.name}."
    if (membership.placement_in(candidate, month) is None or membership.unit_in(candidate, month).pk != unit.pk
            or not membership.rostered_in(unit, month).filter(pk=candidate.pk).exists()
            or membership.no_longer_serving(candidate) or not candidate.user.is_active):
        return f"{name} is not a nurse of {unit.name} in {month:%B %Y}."
    today = membership.today()
    if not qualifies(candidate, today):
        return f"{name} holds no skill that lets a nurse precept, unexpired today."
    months = [each for each in probation_months(nurse) if each >= month]
    on_probation = next((each for each in months if probation.on_probation_in(candidate, each)), None)
    if on_probation is not None:
        return f"{name} is on probation in {on_probation:%B %Y}, so she cannot precept."
    taken = precepted_in(candidate, months, but=nurse)
    if taken is not None:
        other, first = taken
        return f"{name} already precepts {name_of(other)} in {first:%B %Y}, and a preceptor has one nurse on probation at a time."
    return None


def candidates(nurse: NurseProfile, unit: Unit, month: dt.date) -> list[NurseProfile]:
    """Who the list offers: the unit's nurses that month whom `why_not` lets precept her."""
    rostered = (membership.rostered_in(unit, month).exclude(pk=nurse.pk).select_related("user")
                .filter(user__role__in=(Role.STAFF_NURSE, Role.UNIT_MANAGER)).order_by("user__full_name", "user__username"))
    return [candidate for candidate in rostered if why_not(nurse, unit, candidate, month) is None]


def first_gained(nurse: NurseProfile, preceptor: NurseProfile, unit: Unit, month: dt.date) -> dt.date | None:
    """The first of her months in this unit from this one on, up to the last the two are there together, that this nurse does
    not precept her yet: where choosing them from `month` first changes anything. None when it changes nothing."""
    rows, last = _rows(nurse), _last_month_together(nurse, preceptor, unit, month)
    return next((each for each in probation_months(nurse) if each >= month and (last is None or each <= last)
                 and ((held := assignment_in(nurse, each, rows)) is None or held.preceptor_id != preceptor.pk)), None)


def preceptors_from(nurse: NurseProfile, month: dt.date, unit: Unit) -> set:
    """The pks of the nurses who precept her in this unit in some month from this one on."""
    return {span.assignment.preceptor_id for span in spans(nurse, month) if span.assignment.unit_id == unit.pk}


def assign(nurse: NurseProfile, unit: Unit, preceptor: NurseProfile, month: dt.date, by: User) -> tuple[PreceptorAssignment, list]:
    """From this month on, whatever this unit assigned for then or later: a later choice replaces a plan, as a transfer does.
    Another unit's plan, for the months she is due there, stays. A leaving already recorded, a transfer or an end date of
    either nurse's, ends it as one recorded later would. Returns it and the plans it replaced, as an audit entry keeps them."""
    replaced = list(nurse.preceptor_assignments.filter(from_month__gte=month, unit=unit).select_related("preceptor__user"))
    nurse.preceptor_assignments.filter(pk__in=[row.pk for row in replaced]).delete()
    made = PreceptorAssignment.objects.create(nurse=nurse, preceptor=preceptor, unit=unit, from_month=month, assigned_by=by,
                                              until=_last_month_together(nurse, preceptor, unit, month))
    return made, [describe(row) for row in replaced]


def _last_month_together(nurse: NurseProfile, preceptor: NurseProfile, unit: Unit, month: dt.date) -> dt.date | None:
    """The month before the first of her months from `month` that one of the two is not in the unit (a transfer, or an end date
    before it: the month of an end date is still theirs), or None when both are there to her probation's end."""
    for each in probation_months(nurse):
        if each < month:
            continue
        gone = any(membership.unit_in(either, each).pk != unit.pk or (either.end_date is not None and either.end_date < each)
                   for either in (nurse, preceptor))
        if gone:
            return probation.months_on(each, -1)
    return None


def released_by_ending(nurse: NurseProfile, was_last: dt.date | None) -> list[tuple[PreceptorAssignment, dt.date]]:
    """When her probation now ends before `was_last` (an End, a shorter Length, the CNO's untick): each assignment that was in
    force in a month it gave up, with the first such month, so its preceptor is told, under its own unit, that they precept her
    no more from then."""
    if was_last is None:
        return []
    now = probation.last_day(nurse)
    month = probation.months_on(now.replace(day=1), 1) if now else nurse.start_date.replace(day=1)
    rows = list(nurse.preceptor_assignments.select_related("preceptor__user", "unit"))
    first: dict = {}
    while month <= was_last:
        held = next((row for row in reversed(rows) if row.from_month <= month), None)
        if held is not None and (held.until is None or month <= held.until):
            first.setdefault(held.pk, (held, month))
        month = probation.months_on(month, 1)
    return list(first.values())


def end_with_probation(nurse: NurseProfile) -> list[Ended]:
    """The CNO's untick: each assignment ends for good with the months of probation it keeps, so a later tick, which starts a
    probation anew, brings no preceptor back. Returned are those that were in force in a month it gave up, to log."""
    last = probation.last_day(nurse)
    last_month = last.replace(day=1) if last else probation.months_on(nurse.start_date.replace(day=1), -1)
    ended = []
    for row in nurse.preceptor_assignments.select_related("nurse__user", "preceptor__user", "unit"):
        if row.until is not None and row.until <= last_month:
            continue
        was = describe(row)
        row.until = max(last_month, probation.months_on(row.from_month, -1))
        row.save(update_fields=["until"])
        ended.append(Ended(row, was, by_preceptor=False))
    return ended


def describe(row: PreceptorAssignment | None) -> dict:
    """An assignment's values as an audit entry keeps them."""
    if row is None:
        return {"preceptor": None}
    return {"preceptor": row.preceptor.user.username, "from_month": row.from_month.strftime("%Y-%m"),
            "until": row.until.strftime("%Y-%m") if row.until else None}


@dataclass(frozen=True)
class Ended:
    """An assignment a nurse's leaving ended, with its values before."""
    assignment: PreceptorAssignment
    was: dict
    by_preceptor: bool  # Her preceptor left, so her unit's manager is told to choose another.


def end_on_leaving(leaver: NurseProfile, gone_from: dt.date, *, staying_in: Unit | None = None) -> list[Ended]:
    """`leaver` is in none of her assignments' units from `gone_from` on (a transfer to `staying_in`, or the month after an end
    date's): each assignment of a unit she leaves, as preceptor or as nurse on probation, ends with the month before. Returned
    are those that were in force in a month from then on, which the caller logs and, for a preceptor, tells the unit's manager."""
    last = probation.months_on(gone_from, -1)
    rows = (PreceptorAssignment.objects.filter(Q(preceptor=leaver) | Q(nurse=leaver)).filter(Q(until__isnull=True) | Q(until__gt=last))
            .select_related("nurse__user", "preceptor__user", "unit"))
    if staying_in is not None:
        rows = rows.exclude(unit=staying_in)
    ended = []
    for row in rows:
        theirs = _rows(row.nurse)
        in_force = any((held := assignment_in(row.nurse, month, theirs)) and held.pk == row.pk
                       for month in probation_months(row.nurse) if month >= gone_from)
        was = describe(row)
        row.until = last
        row.save(update_fields=["until"])
        if in_force:
            ended.append(Ended(row, was, by_preceptor=row.preceptor_id == leaver.pk))
    return ended


def clash_if_lasting(nurse: NurseProfile, months: int) -> str | None:
    """Why her probation cannot be made `months` long: her preceptor in its last month would go on into a month in which they
    precept another nurse, and a preceptor has one nurse on probation at a time. An Extend or a Length is her unit manager's
    act, so it is refused, and the manager changes one preceptor first."""
    last = probation.last_day(nurse)
    if last is None:
        return None
    held = assignment_in(nurse, last.replace(day=1))
    if held is None:
        return None
    longer = probation.last_day_after(nurse.start_date, months)
    added, month = [], probation.months_on(last.replace(day=1), 1)
    while month <= longer:
        added.append(month)
        month = probation.months_on(month, 1)
    taken = precepted_in(held.preceptor, added, but=nurse)
    if taken is None:
        return None
    other, first = taken
    name = name_of(nurse)
    return (f"Extended, {name}'s probation would reach {first:%B %Y}, when her preceptor {name_of(held.preceptor)} precepts "
            f"{name_of(other)}. Choose {name} another preceptor from that month first.")


def log_ended(ended: list[Ended], request) -> None:
    """Each assignment a nurse's leaving ended: an audit entry with its values, as an assignment's, and for a preceptor's
    leaving the unit's manager told to choose another."""
    from audit.log import record
    from notifications.sending import preceptor_leaving

    for each in ended:
        row = each.assignment
        record("nurse.preceptor_ended", actor=request.user, target=row.nurse.user.username, request=request, before=each.was,
               after=describe(row))
        if each.by_preceptor:
            preceptor_leaving(row.unit, row.nurse_id, row.preceptor_id, row.until)
