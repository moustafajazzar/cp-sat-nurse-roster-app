"""Closing a unit's month. The roster in force, with every recorded leave and
pin, becomes fact: the solver package's own code makes every day a fact (`solves.input.assemble_replan` with the first day after
the month), reports what the facts broke, and works out each nurse's carry-over, all in this process and with no search. The
close takes the previous closed month's carry-over, never a projection (`closes.chain.Chain(project=False)`).

Nothing here calls the solver service: `run_solve` is its only caller, so a close neither waits for a running solve nor
stops one, and a solve of a later month in flight is left to end as usual, its draft warned of as older than the close."""
from __future__ import annotations

import datetime as dt
import logging

from django.db import transaction
from django.utils import timezone
from solver_service.grid import month_facts, nurses_of, validate_grid

from accounts.models import User
from audit.log import record
from closes.chain import Chain, month_before, next_month
from closes.models import CarryOver, CarrySource, MonthClose
from hospital.models import HospitalSettings, Shift
from notifications import sending
from nurses import membership
from nurses.models import NurseProfile
from ops.models import TechnicalSettings
from requesting.deciding import expire_at_close
from requesting.models import Step
from rosters import review
from rosters.drafts import current_code
from rosters.models import Roster, RosterState
from solves.input import Assembled, assemble_replan
from units.models import Unit

logger = logging.getLogger(__name__)


class Refused(Exception):
    """Why this month cannot close now, in words for its manager."""


def _name(month: dt.date) -> str:
    return f"{month:%B %Y}"


def latest_close(unit: Unit, month: dt.date) -> MonthClose | None:
    return MonthClose.objects.filter(unit=unit, month=month).select_related("closed_by").first()


def is_closed(unit: Unit, month: dt.date) -> bool:
    return MonthClose.objects.filter(unit=unit, month=month).exists()


def next_month_closed(unit: Unit, month: dt.date) -> bool:
    """Whether the month after has closed, here or, for one of this month's nurses transferred since, in her unit then: its close
    took this month's carry-over, so this one is fact for good."""
    after = next_month(month)
    nurses = membership.placed_in(unit, month).values("pk")
    return (MonthClose.objects.filter(unit=unit, month=after).exists()
            or CarryOver.objects.filter(source=CarrySource.CLOSED, month=after, nurse__in=nurses).exists())


def fixed_for_good(unit: Unit, month: dt.date) -> bool:
    """Closed, and no longer to be closed again: what it says is fact, and nothing may change what it read."""
    return is_closed(unit, month) and next_month_closed(unit, month)


def fixed_month_between(nurse: NurseProfile, first: dt.date, last: dt.date, unit: Unit | None = None) -> dt.date | None:
    """The earliest month from `first` to `last` fixed for good in the unit it falls under for her, or in `unit` if given: a
    correction that changes which of its days counted would rewrite a fact."""
    if last < first:
        return None
    closes = MonthClose.objects.filter(month__gte=first.replace(day=1), month__lte=last.replace(day=1))
    for unit_id, month in sorted(set(closes.values_list("unit_id", "month")), key=lambda pair: pair[1]):
        placement = membership.placement_in(nurse, month) if unit is None else None
        here = unit if unit is not None else (placement.unit if placement else None)
        if here is not None and here.pk == unit_id and fixed_for_good(here, month):
            return month
    return None


def days_moved(was: dt.date | None, now: dt.date | None, *, end: bool) -> tuple[dt.date, dt.date] | None:
    """The first and last day whose side of a nurse's start date (`end` False) or end date moves when it changes from `was` to
    `now`, None standing for no end date. A start date counts from its day; an end date to its day."""
    if was == now:
        return None
    day = dt.timedelta(days=1)
    if not end:
        return min(was, now), max(was, now) - day
    if was is None or now is None:
        return (was or now) + day, membership.today()
    return min(was, now) + day, max(was, now)


def why_not_close(unit: Unit, month: dt.date, roster: Roster | None) -> str | None:
    if month >= membership.this_month():
        return f"{_name(month)} is not over yet, so it cannot close."
    if roster is None or roster.state != RosterState.APPROVED:
        return (f"{_name(month)} has no approved roster, so there is nothing to close. The month after starts from the last closed "
                "carry-over.")
    if next_month_closed(unit, month):
        return f"{_name(next_month(month))} has closed, so {_name(month)} can no longer be closed again."
    earlier = (Roster.objects.filter(unit=unit, state=RosterState.APPROVED, month__lt=month)
               .exclude(month__in=MonthClose.objects.filter(unit=unit).values("month")).order_by("month").first())
    if earlier is not None:
        return f"{_name(earlier.month)} has not closed yet. Close it first: each month's close starts from the one before."
    return None


def close_input(unit: Unit, month: dt.date, in_force: dict[str, dict[str, str]], *, chain: Chain) -> Assembled:
    """The month as a solve input with every day a fact: the roster in force as `published` and the first day after the month as
    `replan_from`. A month that is over takes its pins as facts (`rosters.pins.month_pins`) and the shifts its roster and pins hold
    as worked, whatever the unit and the catalogue say now (`_as_it_was_worked`)."""
    over = month < membership.this_month()
    assembled = assemble_replan(unit, month, next_month(month), in_force, chain=chain, facts=over)
    if over:
        _as_it_was_worked(unit, month, assembled.input)
    return assembled


def _as_it_was_worked(unit: Unit, month: dt.date, solve_input: dict) -> None:
    """A month that is over is closed as it stands. A shift its roster or a pin holds that the
    unit has since stopped working, or that has since been retired, counts as worked: the solver's check that a cell is one of the
    unit's shifts is passed by listing it among them for this input only. Hours are the shift's own, which cannot change while a
    month not closed uses it (`rosters.drafts.using_shift`); closing again reads those the first close read, since a closed month
    no longer holds them."""
    held = {code for row in solve_input["published"].values() for code in row.values() if code}
    held |= {code for row in solve_input["nurses"] for code in row.get("pinned", {}).values() if code}
    shifts, hours = solve_input["department"]["shifts"], solve_input["general"]["shift_hours"]
    first = MonthClose.objects.filter(unit=unit, month=month).order_by("closed_at", "id").first()
    then = {current_code(code): value for code, value in first.input["general"]["shift_hours"].items()} if first else {}
    for code in sorted(held):
        if code not in shifts:
            shifts.append(code)
        if code in then:
            hours[code] = then[code]
        elif code not in hours:
            hours[code] = Shift.objects.get(code=code).hours


def _lock(unit: Unit, month: dt.date) -> Roster | None:
    """The order an approval and a re-plan's end take: the catalogue's, the month's nurses' (`FOR NO KEY UPDATE`, the lock a leave
    write takes), the unit's, then the roster's, so no leave, edit or rename passes between reading the month and storing it."""
    HospitalSettings.load(for_update=True)
    review.lock_the_months_nurses(unit, month, Roster.objects.filter(unit=unit, month=month).first())
    Unit.objects.select_for_update().get(pk=unit.pk)
    return Roster.objects.select_for_update(of=("self",)).filter(unit=unit, month=month).first()


def close(unit: Unit, month: dt.date, by: User | None = None) -> MonthClose:
    """Closes the unit's month, or closes it again: a new close, which the latest is in force of, and the one before it kept. `by` is
    the manager closing it again, None for the schedule. Raises `Refused`, or ValueError when the solver's code cannot read the
    month. Call it in a transaction."""
    roster = _lock(unit, month)
    refusal = why_not_close(unit, month, roster)
    if refusal:
        raise Refused(refusal)
    again = is_closed(unit, month)
    solve_input = close_input(unit, month, roster.grid, chain=Chain(project=False)).input
    facts = month_facts(solve_input)
    accounts = nurses_of(solve_input, facts)
    checked = validate_grid(solve_input, facts)
    closed = MonthClose.objects.create(
        unit=unit, month=month, closed_by=by, version=roster.versions.first().number, input=solve_input,
        result={"facts": facts, "facts_broke": checked["elapsed_violations"] + checked["violations"],
                "cost": checked["cost"], "nurses": accounts})
    nurses = NurseProfile.objects.filter(user__username__in=list(accounts)).select_related("user")
    CarryOver.objects.bulk_create([CarryOver(nurse=nurse, unit=unit, month=month, source=CarrySource.CLOSED, close=closed,
                                             values=accounts[nurse.user.username]["carry_over"]) for nurse in nurses])
    expired = expire_at_close(unit, month)
    # Which version was closed and whether it was again, and nothing of what the facts were: the System Admin reads the log.
    record("month.closed", actor=by, target=closed.audit_target, after={"version": closed.version, "again": again})
    for request in expired:
        sending.request_decided(request, Step.EXPIRED, None)
    if by is None:
        sending.month_closed(unit, month, closed.pk)
    from closes.tasks import project_later_months  # The tasks module closes months through this one.
    project_later_months(unit, month)
    return closed


def changed_since(close_in_force: MonthClose) -> bool:
    """Whether the month's facts read now differ from what its close read: leave or a correction to a nurse's dates recorded since,
    which closing it again would take in."""
    unit, month = close_in_force.unit, close_in_force.month
    roster = Roster.objects.filter(unit=unit, month=month, state=RosterState.APPROVED).first()
    if roster is None:
        return False
    try:
        now = close_input(unit, month, roster.grid, chain=Chain(project=False)).input
        facts = month_facts(now)
    except ValueError:
        return True
    return facts != close_in_force.result["facts"] or _credited(now) != _credited(close_in_force.input)


def _credited(solve_input: dict) -> dict[str, list[str]]:
    """Each nurse's leave and days outside her dates: a day of leave on a day off changes no fact, but does her hours."""
    return {row["id"]: row.get("leave", []) for row in solve_input["nurses"]}


def due(now: dt.datetime | None = None) -> list[tuple[Unit, dt.date]]:
    """Each unit's month whose close time has passed on the hospital's clock, with an approved roster, and not closed: oldest month
    first, across units, as a transferred nurse's month before can be another unit's."""
    now = timezone.localtime(now)
    this_month = now.date().replace(day=1)
    on_time = now.day > 1 or now.time() >= TechnicalSettings.load().close_time
    latest = month_before(this_month) if on_time else month_before(month_before(this_month))
    closed = {(unit_id, month) for unit_id, month in MonthClose.objects.values_list("unit_id", "month")}
    rosters = (Roster.objects.filter(state=RosterState.APPROVED, month__lte=latest).select_related("unit")
               .order_by("month", "unit__name", "unit_id"))
    return [(roster.unit, roster.month) for roster in rosters if (roster.unit_id, roster.month) not in closed]


def catch_up(now: dt.datetime | None = None) -> list[MonthClose]:
    """What the hourly job does: closes every month due, each in its own transaction, so one the solver's code cannot read is
    logged and left for its manager, and the rest close."""
    closed = []
    for unit, month in due(now):
        try:
            with transaction.atomic():
                closed.append(close(unit, month))
        except (Refused, ValueError) as e:
            logger.warning("month close of %s, %s skipped: %s", unit.name, f"{month:%Y-%m}", e)
    return closed

