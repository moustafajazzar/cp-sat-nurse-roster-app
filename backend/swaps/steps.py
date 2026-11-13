"""Each step a swap takes: proposed, accepted or rejected by a colleague, withdrawn by its proposer, approved or rejected by the
unit's manager. Each refusal, then the write, the audit entry and whoever is told; call each inside a transaction.

Locks, in the order every write takes them: the catalogue's, the nurses' `FOR NO KEY UPDATE`,
the unit's, the roster's, then the swap's. A proposal and an approval take them all, as a direct edit does, so neither passes a
leave write or a version between its check and its write. A colleague's answer and a withdrawal change nothing on the roster and
take the swap's row lock alone, which nothing holds while waiting on another lock, so none of them waits in a circle.

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
from hospital.models import HospitalSettings, Shift
from notifications import sending
from notifications.sending import SwapStep
from nurses import membership, probation
from nurses.models import NurseProfile
from rosters.drafts import current_code
from rosters.models import PinOrigin, Roster, RosterState, VersionOrigin
from rosters.pins import long_date, pin_cell
from rosters.versions import make_version
from swaps.expiring import expire_passed
from swaps.judging import Change, judge, name_of, on_probation_said
from swaps.models import Answer, Swap, SwapCell, SwapParticipant, SwapState
from units.models import Unit
from units.scope import unit_in_scope

MONTH_OVER = "This month is over, so its roster in force cannot change."
# How many swaps a nurse may have withdrawn on one roster in the last day and still propose another there. A withdrawal frees its
# cells, so without it one nurse's rounds would tell a colleague, or the manager, without end.
WITHDRAWALS_A_DAY = 5
A_DAY = dt.timedelta(days=1)
NO_OWN_CELL = "A swap changes at least one of your own cells. Add one of yours."
STATE_WORDS = {SwapState.PENDING: "waiting for its colleagues", SwapState.AWAITING_MANAGER: "waiting for the manager",
               SwapState.APPROVED: "approved", SwapState.REJECTED: "rejected", SwapState.WITHDRAWN: "withdrawn",
               SwapState.EXPIRED: "expired"}


def this_swap_is(state: str) -> str:
    """"This swap is approved": to open a refusal that its state explains."""
    return f"This swap is {STATE_WORDS[state]}"


def no_approved_roster(unit: Unit, month: dt.date) -> str:
    return f"The {unit.name} roster for {month:%B %Y} is not approved yet, so there is nothing to swap."


def _lock_nurses(condition: Q) -> None:
    list(NurseProfile.objects.select_for_update(of=("self", "user"), no_key=True).select_related("user").filter(condition)
         .order_by("pk"))


def _approved_roster(unit: Unit, month: dt.date) -> Roster | None:
    return (Roster.objects.select_for_update(of=("self",)).select_related("unit")
            .filter(unit=unit, month=month, state=RosterState.APPROVED).first())


def propose(user: User, month: dt.date, changes: list[Change], note: str, request: HttpRequest) -> Swap:
    """On the approved roster of the unit this month falls under for the proposer. Refused at once, with every reason, if a
    cell cannot hold its change or the grid would newly break a hard rule, each reason in the proposer's words
    (`swaps.judging`). A swap with no colleague in it goes straight to the manager."""
    HospitalSettings.load(for_update=True)
    _lock_nurses(Q(user=user) | Q(user__username__in=[change.hospital_id for change in changes]))
    proposer = NurseProfile.objects.select_related("user").filter(user=user).first()
    placement = membership.placement_in(proposer, month) if proposer else None
    if placement is None:
        raise HttpError(409, f"You are on no unit's roster for {month:%B %Y}.")
    if month < membership.this_month():
        raise HttpError(409, MONTH_OVER)
    unit = Unit.objects.select_for_update(of=("self",)).select_related("manager").get(pk=placement.unit_id)
    roster = _approved_roster(unit, month)
    if roster is None:
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


def locked_for_decision(user: User, unit_id: UUID, swap_id: UUID) -> Swap:
    """The locks an approval takes, in their order. The same 404 for another unit's swap as for none, before any nurse's row
    is locked."""
    unit_in_scope(user, unit_id)
    HospitalSettings.load(for_update=True)
    _lock_nurses(Q(pk__in=SwapCell.objects.filter(swap_id=swap_id, swap__unit_id=unit_id).values("nurse_id")))
    unit = unit_in_scope(user, unit_id, for_update=True)
    roster_id = Swap.objects.filter(pk=swap_id, unit=unit).values_list("roster_id", flat=True).first()
    if roster_id is not None:
        list(Roster.objects.select_for_update(of=("self",)).filter(pk=roster_id))
    return _locked_swap(Q(pk=swap_id, unit=unit), "There is no such swap in your unit.")


def why_not_decide(swap: Swap) -> str | None:
    """Before the swap is judged again: what its state and its month allow."""
    if swap.state != SwapState.AWAITING_MANAGER:
        if swap.state == SwapState.PENDING:
            return "This swap still waits for its colleagues' answers."
        return f"{this_swap_is(swap.state)}, so it cannot be decided."
    if swap.roster.month < membership.this_month():
        return MONTH_OVER
    return None


def approve(swap: Swap, by: User, reason: str, request: HttpRequest) -> Swap:
    """Each cell a pinned cell of origin `swap`, replacing a manager's pin there, then a new version with origin `swap` holding
    them, in force at once, telling the unit's nurses the roster changed and everyone in the swap it was approved. Refused, with
    the reasons in full, if a cell can no longer hold its change or the grid would newly break a hard rule: leave or a transfer
    may have come since it was proposed. The version's reason names the swap's people and dates, never the proposer's note."""
    refusal = why_not_decide(swap)
    if refusal:
        raise HttpError(409, refusal)
    roster = Roster.objects.select_related("unit").get(pk=swap.roster_id)
    judged = judge(roster, changes_of(swap), None, exclude=swap)
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
    version = make_version(roster, judged.grid, VersionOrigin.SWAP, by, reason_for_version)
    swap.made_version = version
    swap.save(update_fields=["made_version"])
    record("swap.approved", actor=by, target=swap.proposer.user.username, request=request)
    record("roster.changed", actor=by, target=roster.audit_target, request=request,
           after={"version": version.number, "pins": len(cells), "cells": len(cells)})
    sending.roster_changed(swap.unit, roster.month, by)
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

