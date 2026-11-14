"""The unit manager's covers: the unit's covers of a month, one cover's page (its shift-day's lack and who could take it, judged
again each time it is read, and the nurses offered it with their answers), opening one by hand, offering it and withdrawing
it. Only the unit's manager reads or acts on them; the System Admin, the CNO and the Deputy CNO see
none."""
from __future__ import annotations

import datetime as dt
from typing import Literal
from uuid import UUID

from django.db import transaction
from django.http import Http404, HttpRequest
from django.utils import timezone
from ninja import Field, Query, Router, Schema, Status
from ninja.errors import HttpError

from hospital.api import StrictSchema, refuse
from hospital.catalogue_api import IsoDate
from rosters.models import Roster, RosterState
from rosters.review import name_of
from solves.api import MonthFilter, chosen_month, manager
from units.scope import unit_in_scope

from covers import opening, steps
from covers.expiring import expire_passed, state_now
from covers.judging import CoverJudged, judge
from covers.models import WAITING, Cover, CoverOffer

router = Router(tags=["covers"])

CoverStateText = Literal["open", "offered", "taken", "withdrawn", "filled", "expired"]

class UnitCoverRow(Schema):
    id: UUID
    month: str
    date: dt.date
    shift: str = Field(description="The shift's code now.")
    state: CoverStateText = Field(description="As it stands now: a waiting cover whose date has passed is expired.")
    opened: Literal["leave", "manager"] = Field(description="`leave`: leave took the shift off the roster in force, as version "
                                                            "`opened_on`. `manager`: you opened it by hand.")
    opened_at: dt.datetime
    opened_on: int = Field(description="The roster version in force when it was opened; for leave, the one the leave made.")
    note: str
    ended_by: str | None = Field(description="Who withdrew it. Null otherwise: taken, filled and expired are nobody's act.")
    ended_at: dt.datetime | None
    taker: str | None = Field(description="The nurse who took it.")
    version: int | None = Field(description="The roster version its acceptance made.")


def unit_row(cover: Cover) -> UnitCoverRow:
    """`cover` with its shift, versions, ender and taker selected."""
    return UnitCoverRow(id=cover.pk, month=cover.date.strftime("%Y-%m"), date=cover.date, shift=cover.shift.code,
                        state=state_now(cover), opened=cover.opened, opened_at=timezone.localtime(cover.opened_at),
                        opened_on=cover.opened_on.number, note=cover.note,
                        ended_by=name_of(cover.ended_by) if cover.ended_by_id else None,
                        ended_at=timezone.localtime(cover.ended_at) if cover.ended_at else None,
                        taker=name_of(cover.taker.user) if cover.taker_id else None,
                        version=cover.made_version.number if cover.made_version_id else None)


def _with_rows(covers):
    return covers.select_related("shift", "opened_on", "ended_by", "taker__user", "made_version")


class UnitCovers(Schema):
    month: str = Field(description="YYYY-MM: the one asked for, or the hospital's current month.")
    covers: list[UnitCoverRow] = Field(description="The month's, newest first, every state.")


@router.get("/{unit_id}/covers", response=UnitCovers, auth=manager, operation_id="listUnitCovers")
def unit_covers(request: HttpRequest, unit_id: UUID, filters: Query[MonthFilter]) -> UnitCovers:
    unit = unit_in_scope(request.user, unit_id)
    month = chosen_month(filters.month)
    covers = _with_rows(Cover.objects.filter(unit=unit, roster__month=month)).order_by("-opened_at", "-id")
    return UnitCovers(month=month.strftime("%Y-%m"), covers=[unit_row(cover) for cover in covers])


class CouldTakeRow(Schema):
    hospital_id: str
    full_name: str
    worked: int = Field(description="Hours worked this month on the roster in force, by the solver's own accounting.")
    owed: int | None = Field(description="Hours owed this month, as My month says. Null with the hospital's hours rule off.")
    overtime_this_year: int = Field(description="Overtime this year: the carry-over's year to date and this month's.")
    makes_whole: bool = Field(description="With her on it, the shift-day lacks nothing the solver checks.")
    replaces_pin: Literal["manager", "swap"] | None = Field(
        description="Her pin to a day off that day, yours or an approved swap's, which her taking the cover would replace.")


class CoverCheck(Schema):
    reason: str | None = Field(description="Why the roster in force could not be checked, in the solver's words: then nobody "
                                           "is listed.")
    lack: list[str] = Field(description="What the shift-day lacks on the roster in force, from the solver's own check: its "
                                        "headcount, a team leader, both genders, a skill it needs. Empty when it lacks "
                                        "nothing.")
    full: str | None = Field(description="Why nobody is listed: the shift-day is at its maximum headcount.")
    nurses: list[CouldTakeRow] = Field(description="The staff nurses who could take it without newly breaking a hard rule, "
                                                   "furthest below their owed hours first, then least overtime this year, then "
                                                   "by name.")


class CoverOfferRow(Schema):
    hospital_id: str
    full_name: str
    answer: Literal["pending", "accepted", "declined"] = Field(description="`pending`: not answered yet.")
    offered_at: dt.datetime
    answered_at: dt.datetime | None


class CoverPage(Schema):
    cover: UnitCoverRow
    check: CoverCheck | None = Field(description="Worked out now, on the month as it stands. Null once the cover has ended.")
    offers: list[CoverOfferRow] = Field(description="Every nurse it was offered to, by name, with her answer so far.")


def _offer_row(row: CoverOffer) -> CoverOfferRow:
    return CoverOfferRow(hospital_id=row.nurse.user.username, full_name=row.nurse.user.full_name, answer=row.answer,
                         offered_at=timezone.localtime(row.offered_at),
                         answered_at=timezone.localtime(row.answered_at) if row.answered_at else None)


def _check(judged: CoverJudged) -> CoverCheck:
    return CoverCheck(reason=judged.reason, lack=judged.lack, full=judged.full, nurses=[
        CouldTakeRow(hospital_id=could.hospital_id, full_name=could.name, worked=could.worked, owed=could.owed,
                     overtime_this_year=could.overtime_this_year, makes_whole=could.makes_whole,
                     replaces_pin=could.replaces) for could in judged.nurses])


def _page(unit, cover_id: UUID) -> CoverPage:
    cover = _with_rows(Cover.objects.filter(pk=cover_id, unit=unit)).select_related("roster__unit").first()
    if cover is None:
        raise Http404("There is no such cover in your unit.")
    page = CoverPage(cover=unit_row(cover), check=None,
                     offers=[_offer_row(row) for row in cover.offers.select_related("nurse__user")])
    if page.cover.state in WAITING:
        page.check = _check(judge(cover.roster, cover.date, cover.shift))
    return page


@router.get("/{unit_id}/covers/{cover_id}", response=CoverPage, auth=manager, operation_id="getUnitCover")
def unit_cover(request: HttpRequest, unit_id: UUID, cover_id: UUID) -> CoverPage:
    """Judged again now, with the solver's own code in this process: nothing of the list is stored."""
    return _page(unit_in_scope(request.user, unit_id), cover_id)


class OpenCoverBody(StrictSchema):
    date: IsoDate = Field(description="Today or later, in a month whose roster is in force.")
    shift: str = Field(min_length=1, max_length=16, description="One of the unit's shifts, by its code.")


def no_roster_in_force(unit, month: dt.date) -> str:
    return f"The {unit.name} roster for {month:%B %Y} is not approved yet, so there is nothing to cover."


@router.post("/{unit_id}/covers", response={201: CoverPage}, auth=manager, operation_id="openUnitCover")
def open_cover(request: HttpRequest, unit_id: UUID, body: OpenCoverBody) -> Status:
    """For any shift-day of the roster in force, today or later, short for any reason: it waits unoffered, as one opened by
    leave does. Nobody is told, and the audit log has nothing of it."""
    with transaction.atomic():
        # The unit's lock before the roster's, as an edit takes them: the cover's foreign key to the unit is checked at commit,
        # and a rename holds every unit while it waits on the rosters.
        unit = unit_in_scope(request.user, unit_id, for_update=True)
        refused = opening.refusal(unit, body.date, body.shift)
        if refused:
            refuse(((refused[0],), refused[1]))
        month = body.date.replace(day=1)
        roster = (Roster.objects.select_for_update(of=("self",)).select_related("unit")
                  .filter(unit=unit, month=month, state=RosterState.APPROVED).first())
        if roster is None:
            raise HttpError(409, no_roster_in_force(unit, month))
        cover = opening.by_hand(roster, body.date, body.shift)
    return Status(201, _page(unit, cover.pk))


class OfferCoverBody(StrictSchema):
    nurses: list[str] = Field(max_length=500, description="Hospital IDs, each in the list of who could take it as judged now.")
    note: str = Field("", max_length=500, description="Optional; the nurses offered it read it. Kept as first written: an add "
                                                      "leaves it.")


@router.post("/{unit_id}/covers/{cover_id}/offer", response=CoverPage, auth=manager, operation_id="offerUnitCover")
def offer_cover(request: HttpRequest, unit_id: UUID, cover_id: UUID, body: OfferCoverBody) -> CoverPage:
    """Open: it becomes offered to these nurses. Offered: those not yet in it are added, a nurse who declined staying as she
    answered. A nurse not in the list as judged now is refused by name (422). Only those added are told."""
    unit = unit_in_scope(request.user, unit_id)
    with transaction.atomic():  # Its own, so a refusal below keeps the expiry stored.
        expire_passed(Cover.objects.filter(pk=cover_id, unit=unit))
    with transaction.atomic():
        steps.offer(request.user, unit, cover_id, body.nurses, body.note.strip(), request)
    return _page(unit, cover_id)


@router.post("/{unit_id}/covers/{cover_id}/withdraw", response=CoverPage, auth=manager, operation_id="withdrawUnitCover")
def withdraw_cover(request: HttpRequest, unit_id: UUID, cover_id: UUID) -> CoverPage:
    """Open or offered. The nurses offered it are told, never why."""
    unit = unit_in_scope(request.user, unit_id)
    with transaction.atomic():  # Its own, so a refusal below keeps the expiry stored.
        expire_passed(Cover.objects.filter(pk=cover_id, unit=unit))
    with transaction.atomic():
        steps.withdraw(request.user, unit, cover_id, request)
    return _page(unit, cover_id)
