"""Leave: a unit's manager records it, the nurse reads their own, and the CNO and the Deputy CNO read any unit's. Nobody
else reads it with its type, because a type such as Sick or Maternity says something about a person. The System Admin reads
the audit log, so an entry here says only whose leave changed and by whom: not the type, not the dates and not how many,
since a long run gives the type away.

The manager of a date is the manager of the unit its month falls under (`membership.placement_in`), whichever unit the
nurse is in today. So across a transfer each manager reaches their own months and sees nothing of the other's: the old unit
keeps its past months for late facts, and the new one reaches its months before the nurse arrives."""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Annotated, Literal
from uuid import UUID

from django.db import transaction
from django.db.models.functions import Lower
from django.http import HttpRequest
from django.utils import timezone
from ninja import Field, Query, Router, Schema, Status
from ninja.errors import HttpError

from accounts.authz import SignedIn
from accounts.models import Role, User
from audit.log import record
from closes import making
from hospital.api import refuse
from hospital.catalogue_api import IsoDate
from hospital.models import LeaveType
from notifications import sending
from nurses import membership
from nurses.api import Body, MonthText, with_units
from nurses.models import Leave, NurseProfile
from requesting.deciding import replaced_by_leave
from requesting.models import Request, Step
from covers import opening
from covers.models import Cover
from rosters import in_force
from rosters.approved import NewCell
from rosters.models import Roster, RosterVersion
from rosters.pins import end_for_leave, long_date
from units.models import Unit
from units.scope import unit_in_scope

unit_leave_router = Router(tags=["nurses"])
my_leave_router = Router(tags=["nurses"])
manager = SignedIn(Role.UNIT_MANAGER)
reader = SignedIn(Role.UNIT_MANAGER, Role.CNO, Role.DEPUTY_CNO)

MAX_DAYS = 366
LeaveIds = Annotated[list[UUID], Field(min_length=1, max_length=MAX_DAYS)]


class MonthFilter(Schema):
    month: MonthText | None = Field(None, description="YYYY-MM. The hospital's current month when left out.")


class LeaveDates(Body):
    leave_type_id: UUID
    first: IsoDate
    last: IsoDate = Field(description="The same as `first` for one day.")


class NewLeaveBody(LeaveDates):
    nurse_id: UUID


class LeaveChangeBody(LeaveDates):
    leave_ids: LeaveIds = Field(description="The leave these dates and this type replace, all of one nurse.")


class LeaveRemovalBody(Body):
    leave_ids: LeaveIds


class LeaveNurse(Schema):
    id: UUID
    hospital_id: str
    full_name: str


class LeaveTypeChoice(Schema):
    id: UUID
    name: str
    active: bool = Field(description="False once the CNO has retired it. Recorded leave keeps it.")


class LeaveRun(Schema):
    """Consecutive days of one nurse and one type in the month asked for, which is what a manager thinks of as one leave."""
    leave_ids: list[UUID]
    nurse: LeaveNurse
    leave_type: LeaveTypeChoice
    first: dt.date
    last: dt.date
    days: int
    outside_dates: bool = Field(description="A correction to the nurse's start or end date has left these days outside "
                                            "them, where they count for nothing. Its manager can remove them.")


class UnitLeaveMonth(Schema):
    month: str = Field(description="YYYY-MM.")
    nurses: list[LeaveNurse] = Field(description="Who may have leave recorded this month, and anyone who has some.")
    runs: list[LeaveRun]
    closed: Literal["open", "closed", "fixed"] = Field(
        description="`closed`: the month has closed, and leave changed here is taken in when its manager closes it again. "
                    "`fixed`: the month after has closed too, so its leave can no longer change. `open` otherwise.")


class RemovedPin(Schema):
    date: dt.date
    shift: str = Field(description="The shift's code. Empty for a day off.")
    origin: Literal["manager", "request", "swap", "cover"] = Field(description="An accepted request's pin is the nurse's: the request "
                                                                      "stays on record, replaced by leave, and they are told.")
    roster: Literal["none", "draft", "submitted", "approved"] = Field(
        description="The state of the unit's roster of the pin's month, which decides what the page says follows: a draft "
                    "still holds the shift until it is edited or solved again, a submitted roster keeps it for the decision, "
                    "and on an approved one the leave has taken it off as a version.")


class ShiftTakenOff(Schema):
    date: dt.date
    shift: str = Field(description="The shift's code the roster in force held that day.")
    version: int = Field(description="The roster version that took it off, now in force.")
    cover: UUID | None = Field(description="The cover this opened, waiting unoffered on your Covers page for one more nurse on "
                                           "that shift. Null for a day passed, which nobody can cover.")


class SavedLeave(LeaveRun):
    pins_removed: list[RemovedPin] = Field(description="Pins of this unit these dates replaced, as leave wins over a pin. The "
                                                       "page says so, and the draft goes on saying so until a newer solve.")
    shifts_taken_off: list[ShiftTakenOff] = Field(description="Shifts an approved roster of this unit held on these dates, in a "
                                                              "month not over, which a new version took off, as leave wins. "
                                                              "The unit's nurses are told the roster changed, and not why.")


class MyLeaveRun(Schema):
    first: dt.date
    last: dt.date
    days: int
    leave_type: str


class MyLeaveMonth(Schema):
    month: str = Field(description="YYYY-MM.")
    runs: list[MyLeaveRun]


@dataclass
class _Run:
    rows: list[Leave]
    outside: bool

    @property
    def first(self) -> dt.date:
        return self.rows[0].date

    @property
    def last(self) -> dt.date:
        return self.rows[-1].date


def _runs(rows: Iterable[Leave]) -> list[_Run]:
    """`rows` in order of nurse, then date."""
    runs: list[_Run] = []
    for row in rows:
        outside = membership.outside_dates(row.nurse, row.date)
        run = runs[-1] if runs else None
        if (run and run.rows[-1].nurse_id == row.nurse_id and run.rows[-1].leave_type_id == row.leave_type_id
                and run.last + dt.timedelta(days=1) == row.date and run.outside == outside):
            run.rows.append(row)
        else:
            runs.append(_Run([row], outside))
    return runs


def _leave_nurse(nurse: NurseProfile) -> LeaveNurse:
    return LeaveNurse(id=nurse.pk, hospital_id=nurse.user.username, full_name=nurse.user.full_name)


def _leave_run(run: _Run) -> LeaveRun:
    leave_type = run.rows[0].leave_type
    return LeaveRun(leave_ids=[row.pk for row in run.rows], nurse=_leave_nurse(run.rows[0].nurse),
                    leave_type=LeaveTypeChoice(id=leave_type.pk, name=leave_type.name, active=leave_type.active),
                    first=run.first, last=run.last, days=len(run.rows), outside_dates=run.outside)


def _rows_with_names(rows):
    return rows.select_related("nurse__user", "leave_type")


def _chosen_month(month: str | None) -> dt.date:
    return dt.date.fromisoformat(f"{month}-01") if month else membership.this_month()


def _long_date(day: dt.date) -> str:
    return f"{day.day} {day:%B %Y}"


def _days(first: dt.date, last: dt.date) -> list[dt.date]:
    return [first + dt.timedelta(days=offset) for offset in range((last - first).days + 1)]


def _locked_nurse(nurse_id: UUID) -> NurseProfile | None:
    """A transfer and a corrected date change what this may do under this same lock, and the user's with it, as they
    take them. Leave has no lock of its own, so this one also makes the check for a date already taken safe."""
    locked = NurseProfile.objects.select_for_update(of=("self", "user"), no_key=True).select_related("user")
    if locked.filter(pk=nurse_id).first() is None:
        return None
    return with_units(NurseProfile.objects).get(pk=nurse_id)


def _nurse_to_record(user: User, unit_id: UUID, nurse_id: UUID) -> tuple[Unit, NurseProfile]:
    """The same 403 whether another unit's nurse exists or not."""
    unit = unit_in_scope(user, unit_id)
    nurse = _locked_nurse(nurse_id)
    if nurse is None or not membership.ever_placed_in(nurse, unit):
        raise HttpError(403, "This nurse is not in your unit.")
    return unit, nurse


def _rows_to_change(user: User, unit_id: UUID, ids: list[UUID]) -> tuple[Unit, NurseProfile, list[Leave]]:
    """The same 403 whether leave outside the caller's months exists or not. Leave they do reach that has since been
    corrected or removed is a 409: their page is out of date, and acting on it could undo someone's correction."""
    unit = unit_in_scope(user, unit_id)
    refused = HttpError(403, "This leave is not in your unit.")
    nurses = set(Leave.objects.filter(pk__in=ids).values_list("nurse_id", flat=True))
    if len(nurses) != 1:
        raise refused
    nurse = _locked_nurse(nurses.pop())
    rows = list(Leave.objects.filter(pk__in=ids))
    if len(rows) != len(set(ids)) or any(_not_in_unit(nurse, unit, row.date) for row in rows):
        raise refused
    if any(row.removed_at for row in rows):
        raise HttpError(409, "This leave has changed since the page was opened. Reload the page to see it as it is.")
    _refuse_fixed_months(unit, [row.date for row in rows], "leave_ids")
    return unit, nurse, rows


def _months(days: Iterable[dt.date]) -> list[dt.date]:
    return sorted({day.replace(day=1) for day in days})


def _refuse_fixed_months(unit: Unit, days: Iterable[dt.date], field: str) -> None:
    """A closed month is a fact. Leave stays open while it can be closed again, since late facts are what that is for; once the
    month after it has closed, its leave is fixed for good."""
    fixed = next((month for month in _months(days) if making.fixed_for_good(unit, month)), None)
    if fixed is not None:
        refuse(((field,), f"{fixed:%B %Y} has closed, and so has the month after it, so its leave can no longer change."))


def _not_in_unit(nurse: NurseProfile, unit: Unit, day: dt.date) -> bool:
    placement = membership.placement_in(nurse, day.replace(day=1))
    return placement is None or placement.unit_id != unit.pk


def _chosen_type(leave_type_id: UUID, keeping: Iterable[UUID] = ()) -> LeaveType:
    """Only an active type may be newly chosen. Leave already of a type the CNO has retired may keep it. Under the type's
    lock, which retiring it takes."""
    chosen = LeaveType.objects.select_for_update().filter(pk=leave_type_id).first()
    if chosen is None or not (chosen.active or chosen.pk in keeping):
        refuse((("leave_type_id",), "Choose a leave type from the list."))
    return chosen


def _refuse_dates(nurse: NurseProfile, unit: Unit, first: dt.date, last: dt.date, replacing: Iterable[UUID] = ()) -> None:
    name = nurse.user.full_name
    if last < first:
        refuse((("last",), "The last day cannot be before the first."))
    if (last - first).days >= MAX_DAYS:
        refuse((("last",), f"Enter at most {MAX_DAYS} days at a time."))
    # Before their dates, so a manager they have left learns nothing of what has been corrected since.
    elsewhere = next((day for day in _days(first, last) if membership.elsewhere_in(nurse, unit, day.replace(day=1))), None)
    if elsewhere is not None:
        refuse((("first",), f"{name} is not in your unit in {elsewhere:%B %Y}, so that month's leave is not yours to record."))
    if first < nurse.start_date:
        refuse((("first",), f"{name} starts on {_long_date(nurse.start_date)}, so this is before they do."))
    if nurse.end_date is not None and last > nurse.end_date:
        refuse((("last",), f"The last day of {name} is {_long_date(nurse.end_date)}."))
    _refuse_fixed_months(unit, _days(first, last), "first")
    taken = list(Leave.objects.current().filter(nurse=nurse, date__range=(first, last)).exclude(pk__in=replacing)
                 .values_list("date", flat=True))
    if taken:
        others = f" and {len(taken) - 1} more of these days" if len(taken) > 1 else ""
        refuse((("first",), f"{name} is already on leave on {_long_date(taken[0])}{others}. Change that leave instead."))


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
        row = held.roster.grid.get(hospital_id) if held.approved else None
        off = [(day, row[day.isoformat()]) for day in by_month[held.month] if row and row.get(day.isoformat())]
        if not off:
            continue
        name = nurse.user.full_name or hospital_id
        reason = f"Leave recorded for {name} took their shift off {', '.join(long_date(day) for day, _ in off)}."
        version = in_force.change(held, [NewCell(hospital_id, day, None) for day, _ in off], origin=in_force.Origin.LEAVE,
                                  by=by, reason=reason, pins=0)
        taken.append(TakenOff(held.roster, version, off, opening.for_leave(held.roster, version, off)))
    return taken


@dataclass
class _Begun:
    pins_removed: list[RemovedPin]
    taken_off: list[TakenOff]
    requests_replaced: list[Request] = field(default_factory=list)

    def notify_nurses(self, user: User) -> None:
        """Tells the nurse of each accepted request of theirs the leave replaced. The unit's nurses were told of each version
        as it was made. Call it after the leave's audit entry, as every change tells after its own."""
        for replaced in self.requests_replaced:
            sending.request_decided(replaced, Step.REPLACED_BY_LEAVE, user)

    @property
    def shifts_taken_off(self) -> list[ShiftTakenOff]:
        return [ShiftTakenOff(date=day, shift=code, version=taken.version.number,
                              cover=taken.covers[day].pk if day in taken.covers else None)
                for taken in self.taken_off for day, code in taken.days]


def _begin(nurse: NurseProfile, unit: Unit, leave_type: LeaveType, days: list[dt.date], user: User) -> _Begun:
    """Every write that adds a date comes through here, so here is where leave wins over a pin, and over a shift on an approved
    roster (`take_off_for_leave`). Leave's own audit entry stays as it is, whoever recorded it: an entry of the pin's or of the version's
    beside it would tell the System Admin the month of this leave. Record it before this, as the unit's nurses are told of
    each version here."""
    Leave.objects.bulk_create([Leave(nurse=nurse, date=day, leave_type=leave_type, recorded_by=user) for day in days])
    ended = sorted(end_for_leave(nurse, days, user), key=lambda pin: pin.date)
    replaced = replaced_by_leave(nurse, days, ended, user)
    ours = [pin for pin in ended if pin.unit_id == unit.pk]
    states = dict(Roster.objects.filter(unit=unit, month__in={pin.date.replace(day=1) for pin in ours})
                  .values_list("month", "state"))
    return _Begun([RemovedPin(date=pin.date, shift=pin.code, origin=pin.origin,
                              roster=states.get(pin.date.replace(day=1), "none")) for pin in ours],
                  take_off_for_leave(nurse, unit, days, user), replaced)


def _end(ids: Iterable[UUID], user: User) -> None:
    Leave.objects.filter(pk__in=ids).update(removed_at=timezone.now(), removed_by=user)


def _as_saved(nurse: NurseProfile, first: dt.date, last: dt.date) -> LeaveRun:
    rows = _rows_with_names(Leave.objects.current().filter(nurse=nurse, date__range=(first, last)))
    return _leave_run(_runs(rows)[0])


# Every write below goes through `_refuse_dates`, `_rows_to_change` or both, then writes leave and one audit entry. A date added
# ends any pin on it and takes the shift off an approved roster of a month not over (`_begin`). Leave under a submitted roster is
# allowed, and the approval checks the month again; a draft keeps its shift until a solve or a hand edit.
# Removing leave puts nothing back. A closed month's leave stays open while the month can be closed again, and is fixed once the
# month after has closed (`_refuse_fixed_months`).
@unit_leave_router.get("/{unit_id}/leave", response=UnitLeaveMonth, auth=reader, operation_id="listUnitLeave")
def unit_leave(request: HttpRequest, unit_id: UUID, filters: Query[MonthFilter]) -> UnitLeaveMonth:
    """The unit's month, for its manager and, to read, for the CNO and the Deputy CNO. A nurse's month in another unit is
    not here, before or after a transfer."""
    unit = unit_in_scope(request.user, unit_id)
    month = _chosen_month(filters.month)
    rows = list(_rows_with_names(Leave.objects.current().in_month(month)
                                 .filter(nurse__in=membership.placed_in(unit, month).values("pk")))
                .order_by(Lower("nurse__user__full_name"), "nurse_id", "date"))
    nurses = {nurse.pk: nurse for nurse in membership.rostered_in(unit, month).select_related("user")}
    nurses |= {row.nurse_id: row.nurse for row in rows}
    closed = "fixed" if making.fixed_for_good(unit, month) else "closed" if making.is_closed(unit, month) else "open"
    return UnitLeaveMonth(month=month.strftime("%Y-%m"), runs=[_leave_run(run) for run in _runs(rows)], closed=closed,
                          nurses=[_leave_nurse(nurse) for nurse in
                                  sorted(nurses.values(), key=lambda nurse: (nurse.user.full_name.lower(), nurse.pk))])


@unit_leave_router.post("/{unit_id}/leave", response={201: SavedLeave}, auth=manager, operation_id="recordLeave")
def record_leave(request: HttpRequest, unit_id: UUID, body: NewLeaveBody) -> Status:
    """All of it or none of it. A manager with a nurse profile records their own, because nobody else may, and the log
    shows whose hand it was."""
    with transaction.atomic():
        unit, nurse = _nurse_to_record(request.user, unit_id, body.nurse_id)
        leave_type = _chosen_type(body.leave_type_id)
        _refuse_dates(nurse, unit, body.first, body.last)
        record("leave.recorded", actor=request.user, target=nurse.user.username, request=request)
        begun = _begin(nurse, unit, leave_type, _days(body.first, body.last), request.user)
        begun.notify_nurses(request.user)
        return Status(201, SavedLeave(**_as_saved(nurse, body.first, body.last).dict(), pins_removed=begun.pins_removed,
                                      shifts_taken_off=begun.shifts_taken_off))


@unit_leave_router.post("/{unit_id}/leave/change", response=SavedLeave, auth=manager, operation_id="changeLeave")
def change_leave(request: HttpRequest, unit_id: UUID, body: LeaveChangeBody) -> SavedLeave:
    """Days that stay as they were keep their rows. The rest end, and new ones begin."""
    with transaction.atomic():
        unit, nurse, rows = _rows_to_change(request.user, unit_id, body.leave_ids)
        leave_type = _chosen_type(body.leave_type_id, keeping={row.leave_type_id for row in rows})
        _refuse_dates(nurse, unit, body.first, body.last, replacing=[row.pk for row in rows])
        wanted = set(_days(body.first, body.last))
        ended = [row.pk for row in rows if row.leave_type_id != leave_type.pk or row.date not in wanted]
        kept = {row.date for row in rows if row.pk not in ended}
        begun = _Begun([], [])
        if ended or wanted - kept:
            _end(ended, request.user)
            record("leave.changed", actor=request.user, target=nurse.user.username, request=request)
            begun = _begin(nurse, unit, leave_type, sorted(wanted - kept), request.user)
            begun.notify_nurses(request.user)
        return SavedLeave(**_as_saved(nurse, body.first, body.last).dict(), pins_removed=begun.pins_removed,
                          shifts_taken_off=begun.shifts_taken_off)


@unit_leave_router.post("/{unit_id}/leave/remove", response={204: None}, auth=manager, operation_id="removeLeave")
def remove_leave(request: HttpRequest, unit_id: UUID, body: LeaveRemovalBody) -> Status:
    with transaction.atomic():
        _, nurse, rows = _rows_to_change(request.user, unit_id, body.leave_ids)
        _end([row.pk for row in rows], request.user)
        record("leave.removed", actor=request.user, target=nurse.user.username, request=request)
    return Status(204, None)


@my_leave_router.get("/my-leave", response=MyLeaveMonth, auth=SignedIn(Role.STAFF_NURSE, Role.UNIT_MANAGER),
                     operation_id="getMyLeave")
def my_leave(request: HttpRequest, filters: Query[MonthFilter]) -> MyLeaveMonth:
    """Takes no nurse, so it cannot answer for a colleague. A manager is here because a manager with a nurse profile is a
    nurse too, and one holding no unit has no other way to read theirs; one with no profile simply has none. Days left
    outside the nurse's dates by a later correction are not here: they count for nothing, and are their manager's to clear."""
    month = _chosen_month(filters.month)
    rows = _rows_with_names(Leave.objects.current().in_month(month).filter(nurse__user=request.user)).order_by("date")
    return MyLeaveMonth(month=month.strftime("%Y-%m"), runs=[
        MyLeaveRun(first=run.first, last=run.last, days=len(run.rows), leave_type=run.rows[0].leave_type.name)
        for run in _runs(row for row in rows if not membership.outside_dates(row.nurse, row.date))])
