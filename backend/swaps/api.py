"""The unit manager's swaps: the unit's swaps of a month, the decision view of one (before and after, the validation with its
reasons and the cost change, judged again on the month as it stands) and the decision. Only the unit's manager reads or decides
them; the System Admin, the CNO and the Deputy CNO see none."""
from __future__ import annotations

import datetime as dt
from typing import Literal
from uuid import UUID

from django.db import transaction
from django.http import Http404, HttpRequest
from django.utils import timezone
from ninja import Field, Query, Router, Schema

from hospital.api import StrictSchema, refuse
from rosters.api import GridCheck, _grid_check
from rosters.models import PinnedCell, PinOrigin, Roster
from rosters.review import name_of
from solves.api import MonthFilter, chosen_month, manager
from swaps import steps
from swaps.expiring import state_now
from swaps.judging import judge
from swaps.mine_api import SwapCellRow, SwapColleague, SwapStateText, swap_cells, swap_colleagues, with_rows
from swaps.models import WAITING, Swap
from units.scope import unit_in_scope

router = Router(tags=["swaps"])


class UnitSwapRow(Schema):
    id: UUID
    month: str
    state: SwapStateText = Field(description="As it stands now: a waiting swap whose earliest date has passed is expired.")
    proposed_at: dt.datetime
    proposer: str
    note: str
    cells: list[SwapCellRow]
    colleagues: list[SwapColleague]
    ended_by: str | None
    ended_at: dt.datetime | None
    reason: str
    expired_why: Literal["date_passed", "cell_changed"] | None = Field(
        description="Why it expired: a date passed, or a new version changed one of its cells. For you only: such a "
                    "version can be leave.")
    version: int | None = Field(description="The roster version its approval made.")


def unit_row(swap: Swap) -> UnitSwapRow:
    ender = swap.ended_by
    return UnitSwapRow(id=swap.pk, month=swap.roster.month.strftime("%Y-%m"), state=state_now(swap),
                       proposed_at=timezone.localtime(swap.proposed_at), proposer=swap.proposer.user.full_name, note=swap.note,
                       cells=swap_cells(swap), colleagues=swap_colleagues(swap),
                       ended_by=name_of(ender) if ender else None,
                       ended_at=timezone.localtime(swap.ended_at) if swap.ended_at else None, reason=swap.reason,
                       expired_why=swap.expired_why or ("date_passed" if state_now(swap) != swap.state else None),
                       version=swap.made_version.number if swap.made_version_id else None)


class UnitSwaps(Schema):
    month: str = Field(description="YYYY-MM: the one asked for, or the hospital's current month.")
    swaps: list[UnitSwapRow] = Field(description="The month's, newest first, every state.")


@router.get("/{unit_id}/swaps", response=UnitSwaps, auth=manager, operation_id="listUnitSwaps")
def unit_swaps(request: HttpRequest, unit_id: UUID, filters: Query[MonthFilter]) -> UnitSwaps:
    unit = unit_in_scope(request.user, unit_id)
    month = chosen_month(filters.month)
    swaps = with_rows(Swap.objects.filter(unit=unit, roster__month=month).select_related("made_version"))
    swaps = swaps.order_by("-proposed_at", "-id")
    return UnitSwaps(month=month.strftime("%Y-%m"), swaps=[unit_row(swap) for swap in swaps])


class DecisionCell(SwapCellRow):
    now: str = Field(description="The cell in the roster in force now.")
    replaces_pin: str | None = Field(description="The shift's code, or empty for a day off, of your pin on this cell, which "
                                                 "approving replaces. Null when you pinned none there, or the swap has ended.")


class SwapDecision(Schema):
    swap: UnitSwapRow
    cells: list[DecisionCell]
    check: GridCheck | None = Field(description="What the roster in force would break with the swap's cells, on the month as it "
                                                "stands. Null when a cell cannot hold its change: `problems` says why.")
    cost_before: int | None = Field(description="The roster in force's cost, worked out the same way.")
    cost_after: int | None
    delta: int | None = Field(description="`cost_after` less `cost_before`.")
    problems: list[str] = Field(description="Why it cannot be approved as the month stands now, in full: a cell's or a hard rule "
                                            "it would newly break. Leave or a transfer may have come since it was proposed.")
    approve_refusal: str | None


def _decision(request: HttpRequest, unit_id: UUID, swap_id: UUID) -> SwapDecision:
    unit = unit_in_scope(request.user, unit_id)
    swap = with_rows(Swap.objects.filter(pk=swap_id, unit=unit).select_related("made_version")).first()
    if swap is None:
        raise Http404("There is no such swap in your unit.")
    roster = Roster.objects.select_related("unit").get(pk=swap.roster_id)
    # Only while it may still be approved: once it has ended, no approval will replace anything.
    pins = {(pin.nurse_id, pin.date): pin for pin in PinnedCell.objects.current().filter(
        unit=unit, origin=PinOrigin.MANAGER, nurse__in=[cell.nurse_id for cell in swap.cells.all()])} if swap.waiting else {}
    cells = [DecisionCell(**row.dict(), now=roster.grid.get(row.hospital_id, {}).get(row.date.isoformat(), ""),
                          replaces_pin=pins[(cell.nurse_id, cell.date)].code if (cell.nurse_id, cell.date) in pins else None)
             for row, cell in zip(swap_cells(swap), swap.cells.all())]
    answer = SwapDecision(swap=unit_row(swap), cells=cells, check=None, cost_before=None, cost_after=None, delta=None,
                          problems=[], approve_refusal=None)
    state = state_now(swap)
    if state not in WAITING:
        answer.approve_refusal = f"{steps.this_swap_is(state)}, so it cannot be decided."
        return answer
    judged = judge(roster, steps.new_cells_of(swap), None, exclude=swap)
    answer.problems = list(dict.fromkeys(problem.manager for problem in judged.problems))
    if judged.after is not None:
        answer.check = _grid_check(judged.after)
        answer.cost_before, answer.cost_after = judged.cost_before, judged.cost_after
        if judged.cost_before is not None and judged.cost_after is not None:
            answer.delta = judged.cost_after - judged.cost_before
    answer.approve_refusal = steps.why_not_decide(swap) or (" ".join(answer.problems) or None)
    return answer


@router.get("/{unit_id}/swaps/{swap_id}", response=SwapDecision, auth=manager, operation_id="getSwapDecision")
def swap_decision(request: HttpRequest, unit_id: UUID, swap_id: UUID) -> SwapDecision:
    """Judged again now, with the solver's own code in this process, whatever its state; only one awaiting you can be decided."""
    return _decision(request, unit_id, swap_id)


class ApproveSwapBody(StrictSchema):
    reason: str = Field("", max_length=2000, description="Optional. The proposer and every colleague in it read it.")


class RejectSwapBody(StrictSchema):
    reason: str = Field(min_length=1, max_length=2000, description="The proposer and every colleague in it read it: never a "
                                                                    "notification or the audit log.")


@router.post("/{unit_id}/swaps/{swap_id}/approve", response=SwapDecision, auth=manager, operation_id="approveUnitSwap")
def approve_swap(request: HttpRequest, unit_id: UUID, swap_id: UUID, body: ApproveSwapBody) -> SwapDecision:
    """Its cells become pinned cells of origin `swap` in a new version with origin `swap`, in force at once. Refused while a cell
    cannot hold its change or the grid would newly break a hard rule."""
    with transaction.atomic():
        held, swap = steps.locked_for_decision(request.user, unit_id, swap_id)
        steps.approve(held, swap, request.user, body.reason.strip(), request)
    return _decision(request, unit_id, swap_id)


@router.post("/{unit_id}/swaps/{swap_id}/reject", response=SwapDecision, auth=manager, operation_id="rejectUnitSwap")
def reject_swap(request: HttpRequest, unit_id: UUID, swap_id: UUID, body: RejectSwapBody) -> SwapDecision:
    reason = body.reason.strip()
    if not reason:
        refuse((("reason",), "Say why: everyone in the swap reads it."))
    with transaction.atomic():
        _, swap = steps.locked_for_decision(request.user, unit_id, swap_id)
        steps.reject(swap, request.user, reason, request)
    return _decision(request, unit_id, swap_id)
