"""Each step a swap takes: proposed, accepted or rejected by a colleague, withdrawn by its proposer, approved or rejected by the
unit's manager. Each refusal, then the write, the audit entry and whoever is told; call each inside a transaction.

A proposal and a manager's decision hold the unit's month as every change to the roster in force does (`rosters.in_force.hold`,
and for a proposal the unit its proposer is in), then lock the swap's row, so neither passes a leave write or a version between its check and its write.
An approval's version goes in force through `rosters.in_force.change`, which tells the unit's nurses and writes the roster-change
entry. A colleague's answer and a withdrawal change nothing on the roster and take the swap's row lock alone, which nothing holds
while waiting on another lock, so none of them waits in a circle.

The audit log says a swap moved and whose (the proposer's): never its cells, its dates, a note or a reason."""
from __future__ import annotations

import datetime as dt
from uuid import UUID

from django.db.models import Q
from django.http import Http404, HttpRequest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import User
from audit.log import record
from hospital.api import refuse
from hospital.models import Shift
from notifications import sending
from notifications.sending import SwapStep
from nurses import membership, probation
from nurses.models import NurseProfile
from rosters import in_force
from rosters.drafts import current_code
from rosters.models import PinOrigin, Roster
from rosters.pins import long_date, pin_cell
from swaps.expiring import expire_passed
from swaps.judging import Change, judge, name_of, on_probation_said
from swaps.models import Answer, Swap, SwapCell, SwapParticipant, SwapState
from units.models import Unit
from units.scope import unit_in_scope

# How many swaps a nurse may have withdrawn on one roster in the last day and still propose another there. A withdrawal frees its
# cells, so without it one nurse's rounds would tell a colleague, or the manager, without end.
WITHDRAWALS_A_DAY = 5
A_DAY = dt.timedelta(days=1)
NO_OWN_CELL = "A swap changes at least one of your own cells. Add one of yours."
NOT_IN_UNIT = "There is no such swap in your unit."
NO_CELL_CHANGED = "This swap would change no cell of the roster in force, so there is nothing to approve."
STATE_WORDS = {SwapState.PENDING: "waiting for its colleagues", SwapState.AWAITING_MANAGER: "waiting for the manager",
               SwapState.APPROVED: "approved", SwapState.REJECTED: "rejected", SwapState.WITHDRAWN: "withdrawn",
               SwapState.EXPIRED: "expired"}


def this_swap_is(state: str) -> str:
    """"This swap is approved": to open a refusal that its state explains."""
    return f"This swap is {STATE_WORDS[state]}"


def no_approved_roster(unit: Unit, month: dt.date) -> str:
    return f"The {unit.name} roster for {month:%B %Y} is not approved yet, so there is nothing to swap."


def propose(user: User, month: dt.date, changes: list[Change], note: str, request: HttpRequest) -> Swap:
    """On the approved roster of the unit this month falls under for the proposer, held for her and every nurse named
    (`rosters.in_force.hold_where_placed`), so a transfer of hers that comes while this waits is the unit judged. Refused at
    once, with every reason, if a cell cannot hold its change or the grid would newly break a hard rule, each reason in the
    proposer's words (`swaps.judging`). A swap with no colleague in it goes straight to the manager."""
    held = in_force.hold_where_placed(user.username, month, nurses={change.hospital_id for change in changes})
    if held.unit is None:
        raise HttpError(409, f"You are on no unit's roster for {month:%B %Y}.")
    proposer = NurseProfile.objects.select_related("user").get(user=user)
    if held.month_over:
        raise HttpError(409, in_force.MONTH_OVER)
    unit, roster = held.unit, held.roster
    if not held.approved:
        raise HttpError(409, no_approved_roster(unit, month))
    too_many = _too_many_withdrawn(user, roster)
    if too_many:
        raise HttpError(409, too_many)
    expire_passed(Swap.objects.filter(roster=roster))
    if probation.on_probation_in(proposer, month):
        raise HttpError(409, on_probation_said(month))
    if not any(change.hospital_id == user.username for change in changes):
        refuse((("cells",), NO_OWN_CELL))
    said = judge(roster, changes, proposer).for_proposer()
    if said:
        refuse(*[(("cells",) if index is None else ("cells", index), words) for index, words in said])
    swap = Swap.objects.create(unit=unit, roster=roster, proposed_on=roster.versions.first(), proposer=proposer, note=note)
    nurses = {nurse.user.username: nurse for nurse in NurseProfile.objects.select_related("user").filter(
        user__username__in=[change.hospital_id for change in changes])}
    codes = {current_code(change.shift) for change in changes if change.shift}
    codes |= {roster.grid[change.hospital_id][change.day] for change in changes} - {""}
    shifts = {shift.code: shift for shift in Shift.objects.filter(code__in=codes)}
    for change in changes:
        SwapCell.objects.create(swap=swap, nurse=nurses[change.hospital_id], date=change.date,
                                before=shifts.get(roster.grid[change.hospital_id][change.day]),
                                after=shifts.get(current_code(change.shift)) if change.shift else None)
    colleagues = sorted((nurse for nurse in nurses.values() if nurse.pk != proposer.pk), key=lambda nurse: nurse.pk)
    SwapParticipant.objects.bulk_create([SwapParticipant(swap=swap, nurse=nurse) for nurse in colleagues])
    record("swap.proposed", actor=user, target=user.username, request=request)
    if colleagues:
        sending.swap_step(swap, SwapStep.PROPOSED, user)
    else:
        swap.state = SwapState.AWAITING_MANAGER
        swap.save(update_fields=["state"])
        sending.swap_step(swap, SwapStep.AWAITING, user)
    return swap


def _too_many_withdrawn(user: User, roster: Roster) -> str | None:
    """Counted under her own row's lock, which every proposal of hers takes first, so two at once cannot both pass."""
    ended = list(Swap.objects.filter(roster=roster, proposer__user=user, state=SwapState.WITHDRAWN, ended_by=user,
                                     ended_at__gt=timezone.now() - A_DAY).order_by("ended_at").values_list("ended_at", flat=True))
    if len(ended) < WITHDRAWALS_A_DAY:
        return None
    free = timezone.localtime(ended[-WITHDRAWALS_A_DAY] + A_DAY)
    return (f"You have withdrawn {WITHDRAWALS_A_DAY} swaps on this roster in the last day, so you can propose another here from "
            f"{free:%H:%M} on {long_date(free.date())}.")


def _locked_swap(condition: Q, missing: str) -> Swap:
    swap = (Swap.objects.select_for_update(of=("self",)).select_related("unit__manager", "roster", "proposer__user")
            .filter(condition).first())
    if swap is None:
        raise Http404(missing)
    expire_passed(Swap.objects.filter(pk=swap.pk))
    swap.refresh_from_db(fields=["state", "expired_why", "ended_at"])
    return swap


def answer(user: User, swap_id: UUID, accepted: bool, request: HttpRequest) -> Swap:
    """A colleague's, while the swap waits for its colleagues. One rejection ends it; the last acceptance sends it to the manager.
    The same 404 for a swap the reader is not in as for none."""
    swap = _locked_swap(Q(pk=swap_id, participants__nurse__user=user), "You are in no such swap.")
    if swap.state != SwapState.PENDING:
        raise HttpError(409, f"{this_swap_is(swap.state)}, so it can no longer be answered.")
    mine = swap.participants.select_for_update(of=("self",)).get(nurse__user=user)
    if mine.answer != Answer.PENDING:
        raise HttpError(409, f"You have {mine.answer} this swap already.")
    mine.answer, mine.answered_at = (Answer.ACCEPTED if accepted else Answer.REJECTED), timezone.now()
    mine.save(update_fields=["answer", "answered_at"])
    proposer = swap.proposer.user.username
    if not accepted:
        swap.state, swap.ended_by, swap.ended_at = SwapState.REJECTED, user, timezone.now()
        swap.save(update_fields=["state", "ended_by", "ended_at"])
        record("swap.rejected", actor=user, target=proposer, request=request)
        sending.swap_step(swap, SwapStep.REJECTED, user)
        return swap
    record("swap.accepted", actor=user, target=proposer, request=request)
    sending.swap_step(swap, SwapStep.ACCEPTED, user)
    if not swap.participants.exclude(answer=Answer.ACCEPTED).exists():
        swap.state = SwapState.AWAITING_MANAGER
        swap.save(update_fields=["state"])
        sending.swap_step(swap, SwapStep.AWAITING, user)
    return swap


def withdraw(user: User, swap_id: UUID, request: HttpRequest) -> Swap:
    """By its proposer, while it waits for its colleagues or the manager. Another nurse's is the same 404 as none."""
    swap = _locked_swap(Q(pk=swap_id, proposer__user=user), "You have proposed no such swap.")
    if not swap.waiting:
        raise HttpError(409, f"{this_swap_is(swap.state)}, so it cannot be withdrawn.")
    awaiting = swap.state == SwapState.AWAITING_MANAGER
    swap.state, swap.ended_by, swap.ended_at = SwapState.WITHDRAWN, user, timezone.now()
    swap.save(update_fields=["state", "ended_by", "ended_at"])
    record("swap.withdrawn", actor=user, target=user.username, request=request)
    sending.swap_step(swap, SwapStep.WITHDRAWN, user, awaiting=awaiting)
    return swap


def changes_of(swap: Swap) -> list[Change]:
    return [Change(cell.nurse.user.username, cell.date, cell.after.code if cell.after else None)
            for cell in swap.cells.select_related("nurse__user", "after")]


def locked_for_decision(user: User, unit_id: UUID, swap_id: UUID) -> tuple[in_force.Held, Swap]:
    """The swap's month held for its nurses, then the swap's row. The same 404 for another unit's swap as for none, before any
    lock is taken. The manager's scope is read again on the unit as held, so one taken off it while this waited is refused.
    A swap's roster and cells never change once proposed, so they are read before the hold."""
    unit_in_scope(user, unit_id)
    month = Swap.objects.filter(pk=swap_id, unit_id=unit_id).values_list("roster__month", flat=True).first()
    if month is None:
        raise Http404(NOT_IN_UNIT)
    nurses = set(SwapCell.objects.filter(swap_id=swap_id).values_list("nurse__user__username", flat=True))
    held = in_force.hold(unit_id, month, nurses=nurses)
    unit = unit_in_scope(user, unit_id)
    return held, _locked_swap(Q(pk=swap_id, unit=unit), NOT_IN_UNIT)


def why_not_decide(swap: Swap) -> str | None:
    """Before the swap is judged again: what its state and its month allow."""
    if swap.state != SwapState.AWAITING_MANAGER:
        if swap.state == SwapState.PENDING:
            return "This swap still waits for its colleagues' answers."
        return f"{this_swap_is(swap.state)}, so it cannot be decided."
    if swap.roster.month < membership.this_month():
        return in_force.MONTH_OVER
    return None


def approve(held: in_force.Held, swap: Swap, by: User, reason: str, request: HttpRequest) -> Swap:
    """Under `locked_for_decision`. Each cell a pinned cell of origin `swap`, replacing a manager's pin there, then a change of
    origin `swap` to the roster in force (`rosters.in_force.change`), which tells the unit's nurses; everyone in the swap is told
    it was approved. Refused, with the reasons in full, if a cell can no longer hold its change or the grid would newly break a
    hard rule: leave or a transfer may have come since it was proposed. The version's reason names the swap's people and dates,
    never the proposer's note."""
    refusal = why_not_decide(swap)
    if refusal:
        raise HttpError(409, refusal)
    changes = changes_of(swap)
    judged = judge(held.roster, changes, None, exclude=swap)
    if judged.problems:
        raise HttpError(409, " ".join(dict.fromkeys(problem.manager for problem in judged.problems)))
    swap.state, swap.ended_by, swap.ended_at, swap.reason = SwapState.APPROVED, by, timezone.now(), reason
    swap.save(update_fields=["state", "ended_by", "ended_at", "reason"])
    cells = list(swap.cells.select_related("nurse__user", "after"))
    for cell in cells:
        cell.pin = pin_cell(swap.unit, cell.nurse, cell.date, cell.after, PinOrigin.SWAP, by)
        cell.save(update_fields=["pin"])
    names = ", ".join(dict.fromkeys(name_of(cell.nurse) for cell in cells))
    days = ", ".join(dict.fromkeys(long_date(cell.date) for cell in cells))
    reason_for_version = f"Swap proposed by {name_of(swap.proposer)} approved: {names} on {days}."
    # The swap's own entry first, then the roster-change entry the change writes.
    record("swap.approved", actor=by, target=swap.proposer.user.username, request=request)
    try:
        version = in_force.change(held, changes, origin=in_force.Origin.SWAP, by=by, why=reason_for_version, pins=len(cells),
                                  request=request)
    except in_force.Refused as refused:
        raise HttpError(409, str(refused)) from None
    if version is in_force.UNCHANGED:
        # Raised inside the transaction, so the swap's state, its pins and its entry go with it.
        raise HttpError(409, NO_CELL_CHANGED)
    swap.made_version = version
    swap.save(update_fields=["made_version"])
    sending.swap_step(swap, SwapStep.APPROVED, by)
    return swap


def reject(swap: Swap, by: User, reason: str, request: HttpRequest) -> Swap:
    """By the manager, once its colleagues have all accepted, with a reason everyone in it reads."""
    if swap.state != SwapState.AWAITING_MANAGER:
        raise HttpError(409, why_not_decide(swap) or f"{this_swap_is(swap.state)}, so it cannot be decided.")
    swap.state, swap.ended_by, swap.ended_at, swap.reason = SwapState.REJECTED, by, timezone.now(), reason
    swap.save(update_fields=["state", "ended_by", "ended_at", "reason"])
    record("swap.rejected", actor=by, target=swap.proposer.user.username, request=request)
    sending.swap_step(swap, SwapStep.REJECTED, by)
    return swap

