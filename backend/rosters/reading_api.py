"""Rosters read across units, and the roster a nurse reads. The CNO and the Deputy CNO see every unit's state per month and the
rosters waiting for them. A nurse sees the approved roster of the unit their month falls under, with colleagues' names and
shifts only: nothing here for anyone but the reader comes from `Leave` or a result's `nurses`, and of a stored input's `nurses` only
who is on probation in the month and her preceptor (`rosters.grids.OnProbation`), so a colleague's day of leave is the same `""` as
any day off. Their own row may say leave."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from django.db.models import Q
from django.http import HttpRequest
from django.utils import timezone
from ninja import Field, Query, Router, Schema

from accounts.authz import SignedIn
from accounts.models import Role, User
from hospital.catalogue_api import IsoDate
from hospital.models import PublicHoliday, Shift
from nurses import membership, preceptors, probation
from nurses.api import MonthText
from nurses.models import Leave, NurseProfile
from closes.models import CarryOver, CarrySource, MonthClose
from rosters import review
from rosters.day import Figures, State, hospital_day
from rosters.drafts import in_current_codes
from rosters.grids import GridDay, OnProbation
from rosters.models import Roster, RosterState, RosterVersion
from rosters.own_month import OwnHours, closed_hours, own_hours
from rosters.sheets import MyDay, RosterSheet, as_schema, days_of, sheet_of
from rosters.versions import planned_with
from units.models import Unit

router = Router(tags=["rosters"])
approver = SignedIn(Role.CNO, Role.DEPUTY_CNO)
# A manager with a nurse profile is a nurse too.
nurse_reader = SignedIn(Role.STAFF_NURSE, Role.UNIT_MANAGER)
OVERVIEW_MONTHS = 4



class OverviewFilter(Schema):
    month: MonthText | None = Field(None, description="YYYY-MM, the first month shown. The hospital's current one when left out.")


class OverviewUnit(Schema):
    id: UUID
    name: str
    archived: bool
    states: list[State] = Field(description="One for each of `months`, in order. `closed` is an approved roster whose month has "
                                            "closed.")


class Overview(Schema):
    months: list[str]
    units: list[OverviewUnit] = Field(description="Every unit not archived, and an archived one with a roster in these months.")


def _months_from(first: dt.date, count: int) -> list[dt.date]:
    months = [first]
    for _ in range(count - 1):
        months.append((months[-1] + dt.timedelta(days=32)).replace(day=1))
    return months


@router.get("/overview", response=Overview, auth=approver, operation_id="getRosterOverview")
def overview(request: HttpRequest, filters: Query[OverviewFilter]) -> Overview:
    first = dt.date.fromisoformat(f"{filters.month}-01") if filters.month else membership.this_month()
    months = _months_from(first, OVERVIEW_MONTHS)
    states = {(unit_id, month): state for unit_id, month, state in
              Roster.objects.filter(month__in=months).values_list("unit_id", "month", "state")}
    states |= {pair: "closed" for pair in MonthClose.objects.filter(month__in=months).values_list("unit_id", "month")}
    with_rosters = {unit_id for unit_id, _ in states}
    units = Unit.objects.filter(Q(archived=False) | Q(pk__in=with_rosters))
    return Overview(months=[month.strftime("%Y-%m") for month in months], units=[
        OverviewUnit(id=unit.pk, name=unit.name, archived=unit.archived,
                     states=[states.get((unit.pk, month), "none") for month in months]) for unit in units])


class DayFilter(Schema):
    date: IsoDate | None = Field(None, description="YYYY-MM-DD. Today on the hospital's clock when left out.")


class DayColumn(Schema):
    code: str
    display_name: str


class DayNurse(Schema):
    hospital_id: str
    full_name: str
    team_leader: bool


class DayShiftCell(Schema):
    code: str
    present: int = Field(description="Counted as the solver counts it: the shift's own staff, plus those on the shifts that also "
                                     "count towards it.")
    minimum: int | None = Field(description="The range for this kind of day, weekday or weekend. Null with the unit's headcount "
                                            "rule off, or with no headcount entry for this shift.")
    maximum: int | None
    short: bool
    over: bool
    team_leader: bool | None = Field(description="Whether a team leader is on it. Null where the unit does not require one.")
    both_genders: bool | None = Field(description="Whether both genders are on it. Null where the unit does not require them.")
    nurses: list[DayNurse] = Field(description="The shift's own staff, by name.")


class DayLeave(Schema):
    """Who is away, and never why: no leave type."""
    hospital_id: str
    full_name: str


class DayFigures(Schema):
    shifts: list[DayShiftCell] = Field(description="The shifts the unit works, and any other its roster holds that day.")
    on_leave: list[DayLeave]
    hours_counted: bool = Field(description="False with the hospital's hours rule off, when nothing is owed and overtime is 0.")
    overtime: int = Field(description="The month's overtime in hours, the sum of what each nurse reads on My month.")
    overtime_cap: int = Field(description="The hospital rules' unit overtime cap for the month.")
    with_overtime: int = Field(description="How many nurses have any overtime this month.")


class DayUnit(Schema):
    id: UUID
    name: str
    archived: bool
    state: State = Field(description="The month's. `closed` is an approved roster whose month has closed.")
    figures: DayFigures | None = Field(description="For `approved` and `closed` only: from the roster in force checked on the "
                                                   "month as it stands, or from the close in force.")
    unchecked: str | None = Field(description="Why an approved month's figures could not be worked out, in the solver's words.")


class DayTotal(Schema):
    code: str
    on_duty: int


class DayTotals(Schema):
    on_duty: list[DayTotal] = Field(description="One for each of `shifts`: the units' counts summed.")
    on_leave: int
    overtime: int
    units_short: int = Field(description="How many units are short on at least one shift.")


class HospitalDayAnswer(Schema):
    date: dt.date
    weekday: str
    weekend: bool = Field(description="A weekend day by the hospital rules, whose headcount range applies.")
    holiday: str | None = Field(description="The public holiday's name. It changes the hours owed, not the range.")
    shifts: list[DayColumn] = Field(description="The shifts any unit shown works that day, in the shift catalogue's order.")
    units: list[DayUnit] = Field(description="Every unit not archived, and an archived one with a roster or a close that month.")
    totals: DayTotals


@router.get("/day", response=HospitalDayAnswer, auth=approver, operation_id="getHospitalDay")
def day_view(request: HttpRequest, filters: Query[DayFilter]) -> HospitalDayAnswer:
    """Read-only and not logged: it shows what its readers already read on the Unit roster and the unit's Leave page."""
    date = filters.date or membership.today()
    answer = hospital_day(date)
    names = dict(Shift.objects.filter(code__in=answer.codes).values_list("code", "display_name"))
    totals = answer.totals
    holiday = PublicHoliday.objects.filter(date=date).first()
    return HospitalDayAnswer(
        date=date, weekday=date.strftime("%A"), weekend=answer.weekend,
        holiday=holiday.name if holiday else None,
        shifts=[DayColumn(code=code, display_name=names.get(code, code)) for code in answer.codes],
        units=[DayUnit(id=row.unit.pk, name=row.unit.name, archived=row.unit.archived, state=row.state, unchecked=row.unchecked,
                       figures=_day_figures(row.figures) if row.figures else None) for row in answer.units],
        totals=DayTotals(on_duty=[DayTotal(code=code, on_duty=count) for code, count in totals.on_duty.items()],
                         on_leave=totals.on_leave, overtime=totals.overtime, units_short=totals.units_short))


def _day_figures(figures: Figures) -> DayFigures:
    return DayFigures(
        shifts=[DayShiftCell(code=shift.code, present=shift.present, minimum=shift.minimum, maximum=shift.maximum,
                             short=shift.short, over=shift.over, team_leader=shift.team_leader, both_genders=shift.both_genders,
                             nurses=[DayNurse(hospital_id=nurse.hospital_id, full_name=nurse.full_name,
                                              team_leader=nurse.team_leader) for nurse in shift.nurses])
                for shift in figures.shifts],
        on_leave=[DayLeave(hospital_id=nurse.hospital_id, full_name=nurse.full_name) for nurse in figures.on_leave],
        hours_counted=figures.hours_counted, overtime=figures.overtime, overtime_cap=figures.overtime_cap,
        with_overtime=figures.with_overtime)


class WaitingRoster(Schema):
    unit_id: UUID
    unit: str
    month: str
    submitted_by: str
    submitted_at: dt.datetime
    changed_since: bool = Field(description="Whether something has changed since it was submitted that its manager was not "
                                            "warned of, such as leave recorded while it waits.")


@router.get("/submitted", response=list[WaitingRoster], auth=approver, operation_id="listSubmittedRosters")
def submitted(request: HttpRequest) -> list[WaitingRoster]:
    """Oldest first, as they have waited longest."""
    rosters = Roster.objects.filter(state=RosterState.SUBMITTED).select_related("unit", "solve")
    rows = []
    for roster in rosters:
        waiting = review.waiting(roster)
        rows.append(WaitingRoster(unit_id=roster.unit_id, unit=roster.unit.name, month=roster.month.strftime("%Y-%m"),
                                  submitted_by=review.name_of(waiting.submitted_by),
                                  submitted_at=timezone.localtime(waiting.submitted_at),
                                  changed_since=bool(review.new_since(waiting))))
    return sorted(rows, key=lambda row: row.submitted_at)


class NurseShift(Schema):
    code: str
    display_name: str
    colour: str
    hours: int


class ColleagueRow(Schema):
    """A name, whether she is on probation and with which preceptor, and a shift a day, and nothing else: a day of leave and a
    day off are both `""`."""
    hospital_id: str
    full_name: str
    on_probation: bool = Field(description="On probation in this month, as the roster was planned: the row is one of the last, "
                                           "under the \"On probation\" heading.")
    preceptor_name: str | None = Field(description="Her preceptor's full name, as the roster was planned. Null when she is not "
                                                   "on probation, or no preceptor was in force for her.")
    shifts: list[str] = Field(description="One for each of `days`: the shift's code, or empty for a day not worked.")


class NurseRoster(Schema):
    approved_at: dt.datetime
    version: int
    days: list[GridDay]
    shifts: list[NurseShift]
    rows: list[ColleagueRow] = Field(description="Everyone on the roster, the reader too, by name: the counted nurses, then "
                                                 "those on probation.")
    mine: list[MyDay] | None = Field(description="The reader's own row, one for each of `days`. Null when they are not on it.")


class MyRosterMonth(Schema):
    month: str
    first_month: str = Field(description="The earliest month shown here: this one, or the earliest of theirs that has closed. An "
                                         "earlier month shows once it has closed, as its close read it.")
    unit: str | None = Field(description="The unit this month falls under for the reader. Null with no nurse profile, before "
                                         "their first placement, or for a month that begins after their end date.")
    roster: NurseRoster | None = Field(description="Null until that unit's roster of the month is approved, and for a month "
                                                   "before `first_month`.")


def _mine(nurse: NurseProfile, row: dict[str, str], days: list[GridDay]) -> list[MyDay]:
    leave = set(Leave.objects.current().filter(nurse=nurse, date__range=(days[0].date, days[-1].date))
                .values_list("date", flat=True))

    def kind(day: dt.date) -> str:
        if row.get(day.isoformat()):
            return "shift"
        if membership.outside_dates(nurse, day):
            return "outside"
        return "leave" if day in leave else "off"
    return [MyDay(date=day.date, kind=kind(day.date)) for day in days]


class MyRosterFilter(Schema):
    month: MonthText | None = Field(None, description="YYYY-MM. The hospital's current one when left out.")


@dataclass
class MonthInForce:
    """The month a nurse reads, the unit it falls under for them and the version in force there, if any. A month before this one
    is read once it has closed, as its close read it: the version it closed and the figures it stored."""
    month: dt.date
    first: dt.date
    nurse: NurseProfile | None
    unit: Unit | None
    roster: Roster | None = None
    version: RosterVersion | None = None
    close: MonthClose | None = None

    @property
    def grid(self) -> dict[str, dict[str, str]]:
        return in_current_codes(self.version.grid)

    @property
    def approved_at(self) -> dt.datetime:
        return timezone.localtime(review.approval(self.roster).decided_at)

    def heading(self) -> dict:
        """What both of a nurse's reads begin with."""
        return {"month": self.month.strftime("%Y-%m"), "first_month": self.first.strftime("%Y-%m"),
                "unit": self.unit.name if self.unit else None}


def _first_month(nurse: NurseProfile | None) -> dt.date:
    """This month, or the earliest of theirs that has closed, which they read as it closed."""
    this_month = membership.this_month()
    if nurse is None:
        return this_month
    earliest = CarryOver.objects.filter(nurse=nurse, source=CarrySource.CLOSED).order_by("month").values_list("month", flat=True).first()
    return min(earliest, this_month) if earliest else this_month


def in_force(user: User, chosen: str | None) -> MonthInForce:
    """The unit is the one the month falls under for them, so across a transfer each month shows its own unit's roster, and a
    nurse due to start sees their unit's with no row of their own. A month before this one only once it has closed. A month that
    begins after their end date falls under no unit, whatever their role: the end date is the last day they are part of it, and
    a manager's account outlives their profile (`nurses.api.set_end_date`)."""
    month = dt.date.fromisoformat(f"{chosen}-01") if chosen else membership.this_month()
    nurse = NurseProfile.objects.filter(user=user).select_related("user").first()
    ended = nurse is not None and nurse.end_date is not None and nurse.end_date < month
    placement = membership.placement_in(nurse, month) if nurse and not ended else None
    found = MonthInForce(month=month, first=_first_month(nurse), nurse=nurse, unit=placement.unit if placement else None)
    if found.unit is None or month < found.first:
        return found
    found.roster = Roster.objects.filter(unit=found.unit, month=month, state=RosterState.APPROVED).first()
    if found.roster is None:
        return found
    if month < membership.this_month():
        found.close = MonthClose.objects.filter(unit=found.unit, month=month).first()
        if found.close is None:
            found.roster = None
            return found
        found.version = found.roster.versions.get(number=found.close.version)
        return found
    found.version = found.roster.versions.first()  # The latest, by `RosterVersion.Meta.ordering`: the one in force.
    return found


@router.get("/mine", response=MyRosterMonth, auth=nurse_reader, operation_id="getMyRoster")
def my_roster(request: HttpRequest, filters: Query[MyRosterFilter]) -> MyRosterMonth:
    """Takes no nurse, so it answers only for the reader."""
    found = in_force(request.user, filters.month)
    answer = MyRosterMonth(**found.heading(), roster=None)
    if found.version is None:
        return answer
    grid = found.grid
    days = days_of(found.month)
    on_probation = OnProbation.of(planned_with(found.version).input)
    names = on_probation.names(grid)
    used = {code for row in grid.values() for code in row.values() if code}
    mine = found.nurse.user.username
    answer.roster = NurseRoster(
        approved_at=found.approved_at, version=found.version.number, days=days,
        shifts=[NurseShift(code=shift.code, display_name=shift.display_name, colour=shift.colour, hours=shift.hours)
                for shift in Shift.objects.filter(code__in=used)],
        rows=[ColleagueRow(hospital_id=hospital_id, full_name=names.get(hospital_id, ""), **on_probation.marks(hospital_id, names),
                           shifts=[grid[hospital_id].get(day.date.isoformat(), "") for day in days])
              for hospital_id in on_probation.ordered(grid, names)],
        mine=_mine(found.nurse, grid[mine], days) if mine in grid else None)
    return answer


class MyRosterSheet(Schema):
    month: str
    first_month: str = Field(description="The earliest month shown here: this one, or the earliest of theirs that has closed. An "
                                         "earlier month shows once it has closed, as its close read it.")
    unit: str | None = Field(description="The unit this month falls under for the reader. Null with no nurse profile, before "
                                         "their first placement, or for a month that begins after their end date.")
    sheet: RosterSheet | None = Field(description="Null until that unit's roster of the month is approved, and for a month "
                                                  "before `first_month`.")


@router.get("/mine/sheet", response=MyRosterSheet, auth=nurse_reader, operation_id="getMyRosterSheet")
def my_sheet(request: HttpRequest, filters: Query[MyRosterFilter]) -> MyRosterSheet:
    """The print page of My roster: the version in force of the reader's own unit, as its files hold it, with hours and shift
    counts that come from the cells alone. A colleague's day of leave is therefore the same `""` as a day off, and only the
    reader's own row (`mine`) may say leave. No file is made, and nothing is audited."""
    found = in_force(request.user, filters.month)
    answer = MyRosterSheet(**found.heading(), sheet=None)
    if found.version is None:
        return answer
    sheet = sheet_of(found.version)
    row = found.grid.get(found.nurse.user.username)
    answer.sheet = as_schema(sheet, mine=_mine(found.nurse, row, sheet.days) if row is not None else None)
    return answer


class ShiftCount(Schema):
    code: str
    display_name: str
    colour: str
    count: int


class MyHours(Schema):
    worked: int = Field(description="Hours of the shifts on the reader's row.")
    leave_credit: int = Field(description="Hours their leave and any days outside their dates count as worked.")
    owed: int | None = Field(description="Hours this month asks of them in shifts: what their days owe, less the leave "
                                         "credit, plus what the last month carried. Null with the hours rule off.")
    overtime: int
    carried: int | None = Field(description="Hours carried into next month: owed when above 0, worked ahead when below. "
                                            "Projected until the month closes. Null with the hours rule off.")
    shifts: list[ShiftCount] = Field(description="Each shift they work, in the catalogue's order.")


class MyMonthInForce(Schema):
    version: int
    approved_at: dt.datetime
    closed: bool = Field(description="The month has closed: `hours` are its close's figures, fact, and `carried` is what it "
                                     "handed on.")
    hours: MyHours | None = Field(description="Null when the reader has no row on it, such as a nurse due to start, or when "
                                              "the month as it stands no longer has them.")


class MyProbation(Schema):
    last_day: dt.date = Field(description="Her probation's last day, as it stands.")
    preceptor_name: str | None = Field(description="Her preceptor's full name in this month, as it stands. Null while none is "
                                                   "assigned.")


class MyMonth(Schema):
    month: str
    first_month: str = Field(description="The earliest month with hours here: this one, or the earliest of theirs that has closed.")
    unit: str | None = Field(description="The unit this month falls under for the reader. Null with no nurse profile, before "
                                         "their first placement, or for a month that begins after their end date.")
    roster: MyMonthInForce | None = Field(description="Null until that unit's roster of the month is approved, and for a month "
                                                      "before `first_month`.")
    probation: MyProbation | None = Field(None, description="The reader's own probation, when they are on probation in this "
                                                            "month. Null for anyone else.")
    precepts: str | None = Field(None, description="The full name of the nurse on probation the reader precepts in this month, "
                                                   "as it stands. Null for anyone else.")


def _probation_of(nurse: NurseProfile | None, month: dt.date) -> tuple[MyProbation | None, str | None]:
    """Her own probation, and the name of the nurse on probation she precepts, in this month as it stands, not as its roster was
    planned: what she should know now. At most one of the two, since a nurse on probation precepts no one."""
    if nurse is None:
        return None, None
    if probation.on_probation_in(nurse, month):
        held = preceptors.assignment_in(nurse, month)
        return MyProbation(last_day=probation.last_day(nurse),
                           preceptor_name=preceptors.name_of(held.preceptor) if held else None), None
    precepted = preceptors.precepted_in(nurse, [month])
    if precepted is None:
        return None, None
    precepted_nurse, _ = precepted
    return None, preceptors.name_of(precepted_nurse)


@router.get("/mine/month", response=MyMonth, auth=nurse_reader, operation_id="getMyMonth")
def my_month(request: HttpRequest, filters: Query[MyRosterFilter]) -> MyMonth:
    """The reader's own hours and shift counts in the version in force, worked out on the month as it stands, so leave recorded
    since counts, and a new version is read as it comes. Nothing of a colleague: the arithmetic reads the reader's
    row only. Their leave with its type is `GET /api/nurses/my-leave`, which reads any month."""
    found = in_force(request.user, filters.month)
    own, precepts = _probation_of(found.nurse, found.month)
    answer = MyMonth(**found.heading(), roster=None, probation=own, precepts=precepts)
    if found.version is None:
        return answer
    mine = found.nurse.user.username
    grid = found.grid
    if found.close is not None:
        hours = closed_hours(found.close, mine)
    else:
        hours = own_hours(found.unit, found.month, mine, grid[mine]) if mine in grid else None
    answer.roster = MyMonthInForce(version=found.version.number, approved_at=found.approved_at, closed=found.close is not None,
                                   hours=_my_hours(hours) if hours else None)
    return answer


def _my_hours(hours: OwnHours) -> MyHours:
    shifts = Shift.objects.filter(code__in=hours.shifts)
    return MyHours(worked=hours.worked, leave_credit=hours.leave_credit, owed=hours.owed, overtime=hours.overtime,
                   carried=hours.carried, shifts=[ShiftCount(code=shift.code, display_name=shift.display_name, colour=shift.colour,
                                                             count=hours.shifts[shift.code]) for shift in shifts])
