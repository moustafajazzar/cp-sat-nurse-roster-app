"""A staff nurse's swaps: composing one on the roster in force, the swaps they proposed or are in, and their answers. Nothing here
takes another nurse's identity but as a cell of the swap, and nothing comes from a colleague's leave or attributes: a swap's cells
are shifts of the roster in force, which My roster shows (`rosters.reading_api`), and a refusal about a colleague says only that
the swap cannot work for them on a date (`swaps.judging`)."""
from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal
from uuid import UUID

from django.db import transaction
from django.db.models import Q, QuerySet
from django.http import HttpRequest
from django.utils import timezone
from ninja import Field, Query, Router, Schema, Status

from accounts.authz import SignedIn
from accounts.models import Role
from hospital.api import StrictSchema, refuse
from hospital.catalogue_api import IsoDate
from hospital.models import Shift
from nurses import membership, probation
from nurses.api import MonthText
from nurses.models import NurseProfile
from rosters.grids import GridDay, by_name, full_names
from rosters.in_force import MONTH_OVER
from rosters.reading_api import MyRosterFilter, in_force
from rosters.review import name_of
from rosters.sheets import days_of
from swaps import steps
from swaps.expiring import state_now
from swaps.judging import NewCell, on_probation_said
from swaps.models import Swap

router = Router(tags=["swaps"])
nurse_only = SignedIn(Role.STAFF_NURSE)
# A swap is a few cells: a month of one nurse's and a colleague's at most.
MAX_CELLS = 62

SwapStateText = Literal["pending", "awaiting_manager", "approved", "rejected", "withdrawn", "expired"]
AnswerText = Literal["pending", "accepted", "rejected"]


class SwapCellRow(Schema):
    hospital_id: str
    full_name: str
    date: dt.date
    before: str = Field(description="The cell's shift code in the roster in force when it was proposed. Empty for a day not "
                                    "worked, a colleague's leave included.")
    after: str = Field(description="What the swap makes it. Empty for a day off.")


class SwapColleague(Schema):
    hospital_id: str
    full_name: str
    answer: AnswerText


def swap_cells(swap: Swap) -> list[SwapCellRow]:
    return [SwapCellRow(hospital_id=cell.nurse.user.username, full_name=cell.nurse.user.full_name, date=cell.date,
                        before=cell.before_code, after=cell.after_code) for cell in swap.cells.all()]


def swap_colleagues(swap: Swap) -> list[SwapColleague]:
    return [SwapColleague(hospital_id=row.nurse.user.username, full_name=row.nurse.user.full_name, answer=row.answer)
            for row in swap.participants.all()]


def with_rows(swaps: QuerySet[Swap]) -> QuerySet[Swap]:
    return swaps.select_related("unit", "roster", "proposer__user", "ended_by").prefetch_related(
        "cells__nurse__user", "cells__before", "cells__after", "participants__nurse__user")


class MySwapRow(Schema):
    id: UUID
    month: str
    unit: str
    state: SwapStateText = Field(description="As it stands now: a waiting swap whose earliest date has passed is expired.")
    proposed_at: dt.datetime
    proposer: str = Field(description="The proposer's full name.")
    proposed_by_me: bool
    note: str = Field(description="The proposer's words to everyone in it.")
    cells: list[SwapCellRow] = Field(description="Every cell it changes, by date.")
    colleagues: list[SwapColleague] = Field(description="Everyone but the proposer whose cells it changes, with their answer.")
    my_answer: AnswerText | None = Field(description="Yours, when you are one of its colleagues.")
    ended_by: str | None = Field(description="Who rejected, withdrew or decided it. Null while it waits, and for an expiry.")
    ended_at: dt.datetime | None
    reason: str = Field(description="The manager's, for a decision, which everyone in it reads.")


def my_row(swap: Swap, nurse: NurseProfile) -> MySwapRow:
    """`swap` from `with_rows`."""
    mine = next((row for row in swap.participants.all() if row.nurse_id == nurse.pk), None)
    ender = swap.ended_by
    return MySwapRow(id=swap.pk, month=swap.roster.month.strftime("%Y-%m"), unit=swap.unit.name, state=state_now(swap),
                     proposed_at=timezone.localtime(swap.proposed_at), proposer=swap.proposer.user.full_name,
                     proposed_by_me=swap.proposer_id == nurse.pk, note=swap.note, cells=swap_cells(swap),
                     colleagues=swap_colleagues(swap), my_answer=mine.answer if mine else None,
                     ended_by=name_of(ender) if ender else None,
                     ended_at=timezone.localtime(swap.ended_at) if swap.ended_at else None, reason=swap.reason)


def _mine(request: HttpRequest) -> NurseProfile | None:
    return NurseProfile.objects.select_related("user").filter(user=request.user).first()


class MySwapsFilter(Schema):
    month: MonthText | None = Field(None, description="YYYY-MM. Every month's when left out.")


@router.get("/mine", response=list[MySwapRow], auth=nurse_only, operation_id="listMySwaps")
def my_swaps(request: HttpRequest, filters: Query[MySwapsFilter]) -> list[MySwapRow]:
    """Those you proposed and those whose cells change yours, newest first, every state."""
    nurse = _mine(request)
    if nurse is None:
        return []
    swaps = Swap.objects.filter(Q(proposer=nurse) | Q(participants__nurse=nurse)).distinct()
    if filters.month:
        month = dt.date.fromisoformat(f"{filters.month}-01")
        swaps = swaps.filter(roster__month=month)
    return [my_row(swap, nurse) for swap in with_rows(swaps).order_by("-proposed_at", "-id")]


class ComposerShift(Schema):
    code: str
    display_name: str


class ComposerNurse(Schema):
    hospital_id: str
    full_name: str
    me: bool
    shifts: list[str] = Field(description="One for each of `days`: the roster in force's shift code, empty for a day not worked.")


class SwapComposer(Schema):
    month: str
    unit: str | None
    refusal: str | None = Field(description="Why you cannot propose a swap on this month now.")
    days: list[GridDay] = Field(description="The month's days not yet passed, which a swap can change.")
    shifts: list[ComposerShift] = Field(description="The unit's shifts, which any cell may be swapped to.")
    nurses: list[ComposerNurse] = Field(description="You and the staff nurses of the roster in force, by name.")


@router.get("/compose", response=SwapComposer, auth=nurse_only, operation_id="getSwapComposer")
def composer(request: HttpRequest, filters: Query[MyRosterFilter]) -> SwapComposer:
    """The roster in force as My roster shows it, from today on, and the unit's shifts. The hospital's current month when none
    is given."""
    found = in_force(request.user, filters.month)
    answer = SwapComposer(month=found.month.strftime("%Y-%m"), unit=found.unit.name if found.unit else None, refusal=None,
                          days=[], shifts=[], nurses=[])
    if found.unit is None:
        answer.refusal = f"You are on no unit's roster for {found.month:%B %Y}."
        return answer
    if found.month < found.first:
        answer.refusal = MONTH_OVER
        return answer
    if found.version is None:
        answer.refusal = steps.no_approved_roster(found.unit, found.month)
        return answer
    today = membership.today()
    days = [day for day in days_of(found.month) if day.date >= today]
    if not days:
        answer.refusal = MONTH_OVER
        return answer
    grid = found.roster.grid
    me = found.nurse.user.username
    # A nurse on probation that month is no colleague in a swap, so she is not offered.
    staff = {nurse.user.username for nurse in NurseProfile.objects.filter(user__username__in=list(grid), user__role=Role.STAFF_NURSE)
             .select_related("user") if nurse.user.username == me or not probation.on_probation_in(nurse, found.month)}
    names = full_names(grid)
    answer.days = days
    answer.shifts = [ComposerShift(code=shift.code, display_name=shift.display_name) for shift in Shift.objects.filter(
        active=True, code__in=found.unit.rules.get("shifts", [])).order_by("start_time", "code")]
    answer.nurses = [ComposerNurse(hospital_id=hospital_id, full_name=names.get(hospital_id, ""), me=hospital_id == me,
                                   shifts=[grid[hospital_id].get(day.date.isoformat(), "") for day in days])
                     for hospital_id in by_name(grid, names) if hospital_id in staff]
    if me not in grid:
        answer.refusal = f"You have no row on the {found.unit.name} roster for {found.month:%B %Y}, so you have no cell to swap."
    elif probation.on_probation_in(found.nurse, found.month):
        answer.refusal = on_probation_said(found.month)
    return answer


class SwapCellBody(StrictSchema):
    hospital_id: str = Field(min_length=1, max_length=150)
    date: IsoDate
    shift: str | None = Field(max_length=8, description="The shift's code it should hold, or null for a day off.")


class ProposeSwapBody(StrictSchema):
    month: MonthText
    cells: Annotated[list[SwapCellBody], Field(min_length=1, max_length=MAX_CELLS)]
    note: str = Field("", max_length=500, description="Optional. Everyone in the swap and the unit's manager read it.")


def _changes(body: ProposeSwapBody) -> list[NewCell]:
    """Whatever the roster holds, these could never be meant."""
    wrong, seen = [], set()
    for index, cell in enumerate(body.cells):
        if cell.date.strftime("%Y-%m") != body.month:
            wrong.append((("cells", index), f"{cell.date.isoformat()} is not in {body.month}."))
        if (cell.hospital_id, cell.date) in seen:
            wrong.append((("cells", index), "This cell is in the swap twice."))
        seen.add((cell.hospital_id, cell.date))
    if wrong:
        refuse(*wrong)
    return [NewCell(cell.hospital_id, cell.date, cell.shift or None) for cell in body.cells]


def _answer_row(request: HttpRequest, swap_id: UUID) -> MySwapRow:
    return my_row(with_rows(Swap.objects.filter(pk=swap_id)).get(), _mine(request))


@router.post("/mine", response={201: MySwapRow}, auth=nurse_only, operation_id="proposeSwap")
def propose_swap(request: HttpRequest, body: ProposeSwapBody) -> Status:
    """On the approved roster of the unit the month falls under for you, cells of today or later, at least one of them yours.
    Refused with 422 and every reason, each on its cell when it is about one: a reason that rests on a colleague says only that
    the swap cannot work for them on that date. Each colleague whose cells change is told; with none, the manager is."""
    changes = _changes(body)
    with transaction.atomic():
        swap = steps.propose(request.user, dt.date.fromisoformat(f"{body.month}-01"), changes, body.note.strip(), request)
    return Status(201, _answer_row(request, swap.pk))


@router.post("/mine/{swap_id}/withdraw", response=MySwapRow, auth=nurse_only, operation_id="withdrawSwap")
def withdraw_swap(request: HttpRequest, swap_id: UUID) -> MySwapRow:
    """Yours, while it waits for its colleagues or the manager."""
    with transaction.atomic():
        steps.withdraw(request.user, swap_id, request)
    return _answer_row(request, swap_id)


@router.post("/mine/{swap_id}/accept", response=MySwapRow, auth=nurse_only, operation_id="acceptSwap")
def accept_swap(request: HttpRequest, swap_id: UUID) -> MySwapRow:
    """One whose cells change yours, while it waits for its colleagues. The last acceptance sends it to the manager."""
    with transaction.atomic():
        steps.answer(request.user, swap_id, True, request)
    return _answer_row(request, swap_id)


@router.post("/mine/{swap_id}/reject", response=MySwapRow, auth=nurse_only, operation_id="rejectSwap")
def reject_swap(request: HttpRequest, swap_id: UUID) -> MySwapRow:
    """Ends it, for everyone in it."""
    with transaction.atomic():
        steps.answer(request.user, swap_id, False, request)
    return _answer_row(request, swap_id)

