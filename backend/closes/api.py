"""A unit's month close and its carry-over, for its manager, who may close a month again to take late facts in, and to read for the
CNO and the Deputy CNO. Each nurse's carry-over is read here with its source: what the month starts from (opening, projected or
closed) and what it hands on."""
from __future__ import annotations

import datetime as dt
from typing import Literal
from uuid import UUID

from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from ninja import Field, Query, Router, Schema
from ninja.errors import HttpError

from accounts.authz import SignedIn
from accounts.models import Role, User
from closes import making
from closes.chain import Carried, Chain, month_before
from closes.models import CarrySource, MonthClose
from nurses import membership
from nurses.api import MonthText
from rosters.grids import by_name, full_names
from rosters.models import Roster, RosterState
from solves.result import in_dates
from units.models import Unit
from units.scope import unit_in_scope

router = Router(tags=["closes"])
reader = SignedIn(Role.UNIT_MANAGER, Role.CNO, Role.DEPUTY_CNO)
manager = SignedIn(Role.UNIT_MANAGER)

Source = Literal["opening", "projected", "closed"]


class MonthFilter(Schema):
    month: MonthText | None = Field(None, description="YYYY-MM. The month before the hospital's current one when left out: the "
                                                       "latest to have closed, or to close.")


class CloseBody(Schema):
    month: MonthText


class CarryValues(Schema):
    hours_balance: int = Field(description="Hours still owed (+) or worked ahead (-).")
    ytd_overtime: int = Field(description="Overtime hours this year.")
    night_balance: float
    friday_balance: float
    source: Source
    out_of: str | None = Field(description="YYYY-MM: the month these come out of. Null for opening values.")


class NurseCarryOver(Schema):
    hospital_id: str
    full_name: str
    into: CarryValues = Field(description="What the month starts from: January's with overtime and both balances reset.")
    out: CarryValues | None = Field(description="What it hands to the month after: closed once it has, projected from the roster "
                                                "in force while approved, and null with no approved roster.")


class BrokenFact(Schema):
    rule: str
    hospital_id: str | None
    full_name: str
    date: dt.date | None
    text: str


class ClosedMonth(Schema):
    closed_at: dt.datetime
    closed_by: str | None = Field(description="The manager who closed it again. Null when the schedule closed it.")
    version: int = Field(description="The roster version in force that was closed.")
    facts_broke: list[BrokenFact] = Field(description="The hard rules the recorded facts broke: reported, never refused.")
    changed_since: bool = Field(description="Whether leave or a nurse's dates read now differ from what the close read, which "
                                            "closing it again takes in.")


class MonthCloseView(Schema):
    month: str
    closed: ClosedMonth | None = Field(description="The close in force: the latest. Null while the month has not closed.")
    close_refusal: str | None = Field(description="Why the manager cannot close the month, or close it again, now.")
    carry_over: list[NurseCarryOver] = Field(description="Each nurse of the unit's month, by name.")


def _values(values: dict, source: str, out_of: dt.date | None) -> CarryValues:
    return CarryValues(hours_balance=values["hours_balance"], ytd_overtime=values["ytd_overtime"],
                       night_balance=values["night_balance"], friday_balance=values["friday_balance"], source=source,
                       out_of=out_of.strftime("%Y-%m") if out_of else None)


def _into(carried: Carried, month: dt.date) -> CarryValues:
    return _values(carried.into(month), carried.source, carried.out_of)


def _carry_over(unit: Unit, month: dt.date, close: MonthClose | None) -> list[NurseCarryOver]:
    chain = Chain()
    nurses = {nurse.user.username: nurse for nurse in membership.rostered_in(unit, month).select_related("user")}
    roster = Roster.objects.filter(unit=unit, month=month, state=RosterState.APPROVED).select_related("unit").first()
    if close is not None:
        out = {hospital_id: (row["carry_over"], CarrySource.CLOSED) for hospital_id, row in close.result["nurses"].items()}
    elif roster is not None:
        out = {hospital_id: (values, CarrySource.PROJECTED) for hospital_id, values in chain.carry_out(roster).items()}
    else:
        out = {}
    names = full_names(nurses)
    carried_in = chain.carry_ins(nurses.values(), month)  # The projection's assembly read them already: read once.
    return [NurseCarryOver(hospital_id=hospital_id, full_name=names.get(hospital_id, ""),
                           into=_into(carried_in[hospital_id], month),
                           out=_values(out[hospital_id][0], out[hospital_id][1], month) if hospital_id in out else None)
            for hospital_id in by_name(nurses, names)]


def _broken(close: MonthClose) -> list[BrokenFact]:
    rows = close.result["facts_broke"]
    names = full_names(row["nurse"] for row in rows if row["nurse"])
    return [BrokenFact(rule=row["rule"], hospital_id=row["nurse"], full_name=names.get(row["nurse"] or "", ""),
                       date=row["date"], text=in_dates(row["text"], close.month)) for row in rows]


def _close_refusal(unit: Unit, month: dt.date, user: User) -> str | None:
    if user.role != Role.UNIT_MANAGER:
        return "Only the unit's manager closes its month again."
    return making.why_not_close(unit, month, Roster.objects.filter(unit=unit, month=month).first())


def _view(unit: Unit, month: dt.date, user: User) -> MonthCloseView:
    close = making.latest_close(unit, month)
    closed = None
    if close is not None:
        by = close.closed_by
        closed = ClosedMonth(closed_at=timezone.localtime(close.closed_at), closed_by=(by.full_name or by.username) if by else None,
                             version=close.version, facts_broke=_broken(close), changed_since=making.changed_since(close))
    return MonthCloseView(month=month.strftime("%Y-%m"), closed=closed, close_refusal=_close_refusal(unit, month, user),
                          carry_over=_carry_over(unit, month, close))


def _month(text: str) -> dt.date:
    return dt.date.fromisoformat(f"{text}-01")


@router.get("/{unit_id}/close", response=MonthCloseView, auth=reader, operation_id="getMonthClose")
def month_close(request: HttpRequest, unit_id: UUID, filters: Query[MonthFilter]) -> MonthCloseView:
    unit = unit_in_scope(request.user, unit_id)
    month = _month(filters.month) if filters.month else month_before(membership.this_month())
    return _view(unit, month, request.user)


@router.post("/{unit_id}/close", response=MonthCloseView, auth=manager, operation_id="closeMonth")
def close_month(request: HttpRequest, unit_id: UUID, body: CloseBody) -> MonthCloseView:
    """Closes a month that is over, or closes it again to take late facts in, until the month after it has closed. The schedule
    closes each month on its own (`closes.tasks.close_month`); this is for a manager who has recorded something since."""
    unit = unit_in_scope(request.user, unit_id)
    month = _month(body.month)
    with transaction.atomic():
        try:
            making.close(unit, month, request.user)
        except making.Refused as refused:
            raise HttpError(409, str(refused)) from None
        except ValueError as e:
            raise HttpError(409, f"The solver's code cannot read this month as it stands, so it cannot close: {e}")
    return _view(unit, month, request.user)

