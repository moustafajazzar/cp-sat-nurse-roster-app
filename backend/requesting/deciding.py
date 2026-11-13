"""The unit manager's decisions on a request: accept, reject, edit and cancel an accepted one, and leave replacing one. An
accepted request is a pinned cell of origin `request`, refused where a pin would be, in the same words
(`rosters.pins.Judge`), and put on the roster as a pin is: on a draft's cell at once, since a draft never disagrees with a pin, and
on the roster in force of a month not over as a change of origin `request` (`rosters.in_force`), when it alters the cell. The pin
is the request's own, made whether or not the cell changes. A submitted roster is frozen, so a decision
that would move a pin waits for its decision. Call each inside a transaction under `locked_for_decision`."""
from __future__ import annotations

import copy
import datetime as dt
from dataclasses import dataclass
from typing import Iterable
from uuid import UUID

from django.db.models import Q
from django.http import Http404, HttpRequest
from ninja.errors import HttpError

from accounts.models import User
from audit.log import record
from hospital.models import Shift
from notifications import sending
from nurses import membership
from nurses.models import NurseProfile
from requesting.models import Request, RequestState, RequestStep, Step, what_of
from rosters import in_force
from rosters.approved import NewCell
from rosters.drafts import FROZEN, current_code
from rosters.models import PinEnded, PinnedCell, PinOrigin, Roster, RosterState, RosterVersion
from rosters.pins import Judge, end, long_date, pin_cell
from units.models import Unit
from units.scope import unit_in_scope

MONTH_OVER = "This month is over, so its pins cannot change."
NO_SUCH_REQUEST = "There is no such request in your unit."


@dataclass
class Decision:
    """A request and what a decision on it reads: under `locked_for_decision`'s locks to decide, without them to say on a page
    what a decision would do."""

    unit: Unit
    nurse: NurseProfile
    request: Request
    roster: Roster | None
    held: in_force.Held | None = None

    @property
    def month(self) -> dt.date:
        return self.request.window.month

    @property
    def name(self) -> str:
        return self.nurse.user.full_name or self.nurse.user.username


def locked_for_decision(user: User, unit_id: UUID, request_id: UUID) -> Decision:
    """The request's month held for its nurse as any change to it is (`rosters.in_force.hold`), whatever the roster's state,
    then the manager's scope read again on the unit as locked, then the request's row, so two clicks on Accept cannot both find
    it pending. The request's row comes after the roster's: every write to a request holds its nurse's lock first, so no two
    writers wait on each other through those two rows. The same 404 for another unit's request as for none."""
    found = (Request.objects.filter(pk=request_id, window__unit_id=unit_id)
             .values_list("nurse__user__username", "window__month").first())
    if found is None:
        unit_in_scope(user, unit_id)
        raise Http404(NO_SUCH_REQUEST)
    # A request's nurse and month never change, so they can be read before the locks that hold them.
    hospital_id, month = found
    held = in_force.hold(unit_id, month, nurses={hospital_id})
    unit = unit_in_scope(user, unit_id)
    nurse_request = (Request.objects.select_for_update(of=("self",))
                     .select_related("window", "shift", "nurse__user", "pin__shift")
                     .filter(pk=request_id, window__unit=unit).first())
    if nurse_request is None:
        raise Http404(NO_SUCH_REQUEST)
    return Decision(unit, nurse_request.nurse, nurse_request, held.roster, held)


def why_the_pin_cannot_move(decision: Decision) -> str | None:
    """A month that is over and a submitted roster take no pin, and an accepted swap's pin is the swap's to change."""
    if decision.month < membership.this_month():
        return MONTH_OVER
    if decision.roster is not None and decision.roster.state == RosterState.SUBMITTED:
        return FROZEN[RosterState.SUBMITTED]
    return None


def refusal_to_pin(decision: Decision, shift: Shift | None, asked: str | None, judge: Judge | None = None) -> str | None:
    """Why the request's date could not hold this pin now, in a save's words; None when it could."""
    moving = why_the_pin_cannot_move(decision)
    if moving:
        return moving
    held = PinnedCell.objects.current().filter(nurse=decision.nurse, date=decision.request.date, unit=decision.unit).first()
    if held is not None and held.origin == PinOrigin.SWAP:
        return f"An accepted swap holds this cell on {long_date(decision.request.date)}, so change it there."
    judge = judge or Judge(decision.unit, decision.month)
    return judge.refusal(decision.nurse.pk, decision.name, decision.request.date, asked, shift)


def _onto_roster(decision: Decision, shift: Shift | None, by: User, reason: str, request: HttpRequest) -> RosterVersion | None:
    """The pin's cell on the month's roster. A draft takes it in place; the roster in force changes through
    `rosters.in_force`, which makes a version and tells its nurses only when the cell changes. A nurse with no row joined after
    the solve: the next solve takes their pin."""
    roster, hospital_id, day = decision.roster, decision.nurse.user.username, decision.request.date.isoformat()
    row = roster.grid.get(hospital_id) if roster else None
    if roster is None or row is None or day not in row:
        return None
    code = shift.code if shift else ""
    if roster.state == RosterState.DRAFT:
        if row[day] != code:
            grid = copy.deepcopy(roster.grid)
            grid[hospital_id][day] = code
            roster.grid = grid
            roster.save(update_fields=["grid"])  # `updated_at` stays, as for a hand edit.
        return None
    made = in_force.change(decision.held, [NewCell(hospital_id, decision.request.date, code or None)],
                           origin=in_force.Origin.REQUEST, by=by, reason=reason, pins=1, request=request)
    return None if made is in_force.UNCHANGED else made


def _record(decision: Decision, step: str, by: User, request: HttpRequest) -> None:
    """The decision's audit entry, which says the request moved and whose, never what or why."""
    record(f"request.{step}", actor=by, target=decision.nurse.user.username, request=request)


def _record_and_tell(decision: Decision, step: str, by: User, request: HttpRequest) -> None:
    _record(decision, step, by, request)
    sending.request_decided(decision.request, step, by)


def _pin_and_tell(decision: Decision, shift: Shift | None, step: str, by: User, reason: str, version_reason: str,
                  request: HttpRequest) -> RosterVersion | None:
    """The pin, whatever the roster, then the cell. The request's own audit entry comes before the roster change's, as a swap's
    and a cover's do, and she is told of the decision after the unit's nurses are told of the change."""
    pin = pin_cell(decision.unit, decision.nurse, decision.request.date, shift, PinOrigin.REQUEST, by)
    nurse_request = decision.request
    nurse_request.state, nurse_request.pin = RequestState.ACCEPTED, pin
    nurse_request.save(update_fields=["state", "pin"])
    RequestStep.objects.create(request=nurse_request, step=step, by=by, reason=reason, pin=pin)
    _record(decision, step, by, request)
    version = _onto_roster(decision, shift, by, version_reason, request)
    sending.request_decided(nurse_request, step, by)
    return version


def accept(decision: Decision, by: User, reason: str, request: HttpRequest) -> RosterVersion | None:
    """What she asked for becomes a pin. One the manager pinned on that cell ends as replaced, as their next pin there would:
    the page says so before they accept."""
    nurse_request = decision.request
    if nurse_request.state != RequestState.PENDING:
        raise HttpError(409, f"This request is {nurse_request.state_words} already.")
    refusal = refusal_to_pin(decision, nurse_request.shift, nurse_request.shift.code if nurse_request.shift else None)
    if refusal:
        raise HttpError(409, refusal)
    what = nurse_request.what
    return _pin_and_tell(decision, nurse_request.shift, Step.ACCEPTED, by, reason, f"{decision.name}'s request for {what} on "
                   f"{long_date(nurse_request.date)} accepted.", request)


def edit(decision: Decision, asked: str | None, by: User, reason: str, request: HttpRequest) -> RosterVersion | None:
    """An accepted request now promises a different shift, or a day off, on its date. The request keeps what she asked for; its
    new pin holds what is promised."""
    nurse_request = decision.request
    if nurse_request.state != RequestState.ACCEPTED:
        raise HttpError(409, f"Only an accepted request can be changed, and this one is {nurse_request.state_words}.")
    code = current_code(asked) if asked else None
    shift = Shift.objects.filter(code=code, active=True).first() if code else None
    if (code is None and nurse_request.pin.shift_id is None) or (shift is not None and shift.pk == nurse_request.pin.shift_id):
        raise HttpError(409, f"The request already promises {what_of(shift)}. Choose something else, or leave it as it is.")
    refusal = refusal_to_pin(decision, shift, code)
    if refusal:
        raise HttpError(409, refusal)
    return _pin_and_tell(decision, shift, Step.EDITED, by, reason, f"{decision.name}'s accepted request changed to "
                         f"{what_of(shift)} on "
                   f"{long_date(nurse_request.date)}.", request)


def reject(decision: Decision, by: User, reason: str, request: HttpRequest) -> None:
    nurse_request = decision.request
    if nurse_request.state != RequestState.PENDING:
        raise HttpError(409, f"This request is {nurse_request.state_words} already.")
    nurse_request.state = RequestState.REJECTED
    nurse_request.save(update_fields=["state"])
    RequestStep.objects.create(request=nurse_request, step=Step.REJECTED, by=by, reason=reason)
    _record_and_tell(decision, Step.REJECTED, by, request)


def cancel(decision: Decision, by: User, reason: str, request: HttpRequest) -> None:
    """Its pin ends, and no cell changes: the next solve or a hand edit is free to change it. So no version is made and the
    unit's nurses are told nothing."""
    nurse_request = decision.request
    if nurse_request.state != RequestState.ACCEPTED:
        raise HttpError(409, f"Only an accepted request can be cancelled, and this one is {nurse_request.state_words}.")
    moving = why_the_pin_cannot_move(decision)
    if moving:
        raise HttpError(409, moving)
    end([nurse_request.pin], PinEnded.CLEARED, by)
    nurse_request.state, nurse_request.pin = RequestState.CANCELLED, None
    nurse_request.save(update_fields=["state", "pin"])
    RequestStep.objects.create(request=nurse_request, step=Step.CANCELLED, by=by, reason=reason)
    _record_and_tell(decision, Step.CANCELLED, by, request)


def replaced_by_leave(nurse: NurseProfile, days: Iterable[dt.date], ended: Iterable[PinnedCell], by: User) -> list[Request]:
    """Leave wins over a pin, so an accepted request whose pin leave ended takes a final state of its own and stays on record.
    So does one still pending on those days, which could never be accepted now and would hold a place under the cap. Call it
    where leave ends pins, under the nurse's lock, and tell the nurse after the leave's audit entry. No audit entry of its own:
    one beside the leave's would date that leave for the System Admin."""
    accepted = Q(pin__in=[pin.pk for pin in ended], state=RequestState.ACCEPTED)
    pending = Q(nurse=nurse, date__in=list(days), state=RequestState.PENDING)
    rows = list(Request.objects.select_for_update(of=("self",)).select_related("window__unit", "nurse__user", "shift")
                .filter(accepted | pending).order_by("date"))
    for nurse_request in rows:
        nurse_request.state, nurse_request.pin = RequestState.REPLACED_BY_LEAVE, None
        nurse_request.save(update_fields=["state", "pin"])
        RequestStep.objects.create(request=nurse_request, step=Step.REPLACED_BY_LEAVE, by=by)
    return rows


MONTH_CLOSED = "The month closed before a decision."


def expire_at_close(unit: Unit, month: dt.date) -> list[Request]:
    """Each request of the unit's month still pending when it closes takes its final state, `expired`, with the same words for
    everyone and naming no one. Its nurse is told, as of any decision: the close's caller tells
    them after its own audit entry. No audit entry of its own, as for leave replacing a request."""
    rows = list(Request.objects.select_for_update(of=("self",)).select_related("window__unit", "nurse__user", "shift")
                .filter(window__unit=unit, window__month=month, state=RequestState.PENDING).order_by("date", "made_at"))
    for nurse_request in rows:
        nurse_request.state = RequestState.EXPIRED
        nurse_request.save(update_fields=["state"])
        RequestStep.objects.create(request=nurse_request, step=Step.EXPIRED, by=None, reason=MONTH_CLOSED)
    return rows
