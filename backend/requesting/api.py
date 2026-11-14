"""The unit manager's request windows and the unit's requests in each, and their decisions. Only the unit's manager reads or
decides them: a request's reasons can be about someone's health."""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Literal
from uuid import UUID

from django.db import transaction
from django.db.models import Count, Q, QuerySet
from django.http import Http404, HttpRequest
from django.utils import timezone
from ninja import Field, Router, Schema, Status

from accounts.models import User
from hospital.api import StrictSchema, refuse
from hospital.catalogue_api import IsoDate
from nurses.api import MonthText
from requesting import deciding, making
from requesting.models import Preference, Request, RequestState, RequestStep, RequestWindow, WindowState
from requesting.windows import state_today
from rosters.cells import REQUEST, AsideWhy, MonthCells
from rosters.models import Roster, RosterVersion
from solves.api import manager
from units.models import Unit
from units.scope import unit_in_scope

router = Router(tags=["requests"])

RequestStateText = Literal["pending", "accepted", "rejected", "withdrawn", "cancelled", "replaced_by_leave", "expired"]
PreferenceKindText = Literal["day_off", "working_day"]
PreferenceStateText = Literal["made", "withdrawn", "asked_instead"]
StepText = Literal["made", "withdrawn", "accepted", "rejected", "edited", "cancelled", "replaced_by_leave", "expired"]


class RequestStepRow(Schema):
    step: StepText
    by: str | None = Field(description="Null for `expired`: the month's close ended it, with no one's hand in it.")
    at: dt.datetime
    reason: str = Field(description="The nurse's, when she made it; the manager's, for a decision. Read only by her and the "
                                    "unit's manager.")
    shift: str | None = Field(description="What an acceptance or an edit promised: a shift's code, empty for a day off. Null "
                                          "for other steps.")


def step_rows(steps: list[RequestStep]) -> list[RequestStepRow]:
    return [RequestStepRow(step=step.step, by=(step.by.full_name or step.by.username) if step.by else None, at=timezone.localtime(step.at),
                    reason=step.reason, shift=(step.pin.code if step.pin else None)) for step in steps]


def with_steps(requests: QuerySet[Request]) -> QuerySet[Request]:
    return requests.select_related("shift", "pin__shift", "window__unit", "nurse__user").prefetch_related(
        "steps__by", "steps__pin__shift")


class WindowRow(Schema):
    id: UUID
    month: str = Field(description="YYYY-MM, the target month.")
    opens_on: dt.date
    closes_on: dt.date = Field(description="The last day requests can be made, on the hospital's clock.")
    state: Literal["scheduled", "open", "closed"] = Field(description="As the dates say today.")
    closed_early_by: str | None
    pending: int
    requests: int = Field(description="Every request made in it, whatever its state.")
    preferences: int = Field(description="Every preference made in it, whatever its state.")


class UnitWindows(Schema):
    request_cap: int = Field(description="Requests per nurse per month.")
    preference_cap: int = Field(description="Preferences per nurse per month.")
    windows: list[WindowRow] = Field(description="Newest first.")
    open_refusal: str | None = Field(description="Why a window cannot be opened now.")


def with_counts(windows: QuerySet[RequestWindow]) -> QuerySet[RequestWindow]:
    # A grouped query drops the model's ordering, so it is said again.
    return windows.select_related("closed_by").annotate(
        made=Count("requests", distinct=True), preferred=Count("preferences", distinct=True),
        pending=Count("requests", filter=Q(requests__state=RequestState.PENDING), distinct=True),
    ).order_by("-opens_on", "-created_at")


def window_row(window: RequestWindow) -> WindowRow:
    """`window` from `with_counts`."""
    closer = window.closed_by
    return WindowRow(id=window.pk, month=window.month.strftime("%Y-%m"), opens_on=window.opens_on, closes_on=window.closes_on,
                     state=state_today(window), closed_early_by=(closer.full_name or closer.username) if closer else None,
                     pending=window.pending, requests=window.made, preferences=window.preferred)


def _open_refusal(unit: Unit, windows: list[RequestWindow]) -> str | None:
    if unit.request_cap == 0 and unit.preference_cap == 0:
        return making.NO_CAP
    waiting = next((window for window in windows if state_today(window) != WindowState.CLOSED), None)
    return "A unit has one request window at a time: close this one first." if waiting else None


@router.get("/{unit_id}/request-windows", response=UnitWindows, auth=manager, operation_id="listRequestWindows")
def request_windows(request: HttpRequest, unit_id: UUID) -> UnitWindows:
    unit = unit_in_scope(request.user, unit_id)
    windows = list(with_counts(RequestWindow.objects.filter(unit=unit)))
    return UnitWindows(request_cap=unit.request_cap, preference_cap=unit.preference_cap, windows=[window_row(window) for window in windows],
                       open_refusal=_open_refusal(unit, windows))


class RequestWindowBody(StrictSchema):
    month: MonthText = Field(description="YYYY-MM, a month not yet begun.")
    opens_on: IsoDate = Field(description="Today or later. Nurses are told when it comes.")
    closes_on: IsoDate = Field(description="The last day requests can be made, before the month begins.")


@router.post("/{unit_id}/request-windows", response={201: WindowRow}, auth=manager, operation_id="openRequestWindow")
def open_request_window(request: HttpRequest, unit_id: UUID, body: RequestWindowBody) -> Status:
    """A window whose open date is today opens now, and the unit's nurses of the month are told; a later one opens on its date,
    by the hourly job or the next write, whichever comes first. At most one window of a unit is waiting or open."""
    with transaction.atomic():
        unit = unit_in_scope(request.user, unit_id, for_update=True)
        window = making.open_window(unit, dt.date.fromisoformat(f"{body.month}-01"), body.opens_on, body.closes_on,
                                    request.user, request)
        return Status(201, window_row(with_counts(RequestWindow.objects.filter(pk=window.pk)).get()))


@router.post("/{unit_id}/request-windows/{window_id}/close", response=WindowRow, auth=manager,
             operation_id="closeRequestWindow")
def close_request_window(request: HttpRequest, unit_id: UUID, window_id: UUID) -> WindowRow:
    """Before its close date. Its pending requests stay for the manager's decision."""
    with transaction.atomic():
        unit = unit_in_scope(request.user, unit_id, for_update=True)
        window = making.close_window(unit, window_id, request.user, request)
        return window_row(with_counts(RequestWindow.objects.filter(pk=window.pk)).get())


class UnitRequestRow(Schema):
    id: UUID
    hospital_id: str
    full_name: str
    date: dt.date
    shift: str = Field(description="What she asked for: a shift's code, empty for a day off.")
    state: RequestStateText
    promised: str | None = Field(description="While accepted, what its pin holds, which an edit may have changed. Null "
                                             "otherwise.")
    set_aside: AsideWhy | None = Field(description="Why its pin counts for nothing now, while accepted, as for any pin.")
    accept_refusal: str | None = Field(description="Why it cannot be accepted now, while pending.")
    replaces_pin: str | None = Field(description="While pending, the shift's code, or empty for a day off, of the pin you "
                                                 "put on its cell, which accepting it replaces.")
    steps: list[RequestStepRow]


def _unit_rows(unit: Unit, requests: list[Request]) -> list[UnitRequestRow]:
    months = {row.window.month for row in requests}
    cells_of = {month: MonthCells(unit, month) for month in months}
    rosters = {roster.month: roster for roster in Roster.objects.filter(unit=unit, month__in=months)}
    rows = []
    for row in requests:
        cells, decision = cells_of[row.window.month], deciding.Decision(unit, row.nurse, row, rosters.get(row.window.month))
        pending = row.state == RequestState.PENDING
        aside = cells.set_aside(row.pin) if row.pin else None
        # Said whether or not it can be accepted now, and only of the manager's own pin: a swap's or a cover's refuses the
        # acceptance instead, and says so in its refusal.
        replaced = cells.managers_pin_replaced(row.nurse.user.username, row.date, REQUEST) if pending else None
        rows.append(UnitRequestRow(
            id=row.pk, hospital_id=row.nurse.user.username, full_name=row.nurse.user.full_name, date=row.date, shift=row.code,
            state=row.state, promised=row.pin.code if row.pin else None, set_aside=aside.why if aside else None,
            accept_refusal=deciding.refusal_to_pin(decision, row.shift, cells) if pending else None,
            replaces_pin=replaced, steps=step_rows(list(row.steps.all()))))
    return rows


@router.get("/{unit_id}/request-windows/{window_id}/requests", response=list[UnitRequestRow], auth=manager,
            operation_id="listWindowRequests")
def window_requests(request: HttpRequest, unit_id: UUID, window_id: UUID) -> list[UnitRequestRow]:
    """By date, then when they were made. A nurse transferred out of the month since keeps her request here: it can be
    rejected, and accepting it is refused, as a pin of hers would be."""
    unit = unit_in_scope(request.user, unit_id)
    window = RequestWindow.objects.filter(unit=unit, pk=window_id).first()
    if window is None:
        raise Http404("There is no such request window.")
    return _unit_rows(unit, list(with_steps(window.requests.all()).order_by("date", "made_at")))


class UnitPreferenceRow(Schema):
    id: UUID
    hospital_id: str
    full_name: str
    date: dt.date
    kind: PreferenceKindText
    state: PreferenceStateText = Field(description="Only `made` reaches a solve.")


@router.get("/{unit_id}/request-windows/{window_id}/preferences", response=list[UnitPreferenceRow], auth=manager,
            operation_id="listWindowPreferences")
def window_preferences(request: HttpRequest, unit_id: UUID, window_id: UUID) -> list[UnitPreferenceRow]:
    """Read-only, by date, then when they were made: nobody decides a preference. A nurse transferred out of the
    month since keeps hers here; it reaches no solve of this unit."""
    unit = unit_in_scope(request.user, unit_id)
    window = RequestWindow.objects.filter(unit=unit, pk=window_id).first()
    if window is None:
        raise Http404("There is no such request window.")
    rows = window.preferences.select_related("nurse__user").order_by("date", "made_at")
    return [UnitPreferenceRow(id=row.pk, hospital_id=row.nurse.user.username, full_name=row.nurse.user.full_name,
                              date=row.date, kind=row.kind, state=row.state) for row in rows]


class AcceptRequestBody(StrictSchema):
    reason: str = Field("", max_length=2000, description="Optional. The nurse reads it with her request.")


class RequestReasonBody(StrictSchema):
    reason: str = Field(min_length=1, max_length=2000, description="The nurse reads it with her request, and nobody but you and "
                                                                    "her: never a notification or the audit log.")


class EditRequestBody(RequestReasonBody):
    shift: str | None = Field(max_length=8, description="The shift's code it now promises, or null for a day off.")


class RequestDecided(Schema):
    request: UnitRequestRow
    version: int | None = Field(description="The roster version this made, now in force, when the roster is approved and a "
                                            "cell changed. The unit's nurses are told the roster changed, and not why.")


def _decided(user: User, unit_id: UUID, request_id: UUID,
             act: Callable[[deciding.Decision], RosterVersion | None]) -> RequestDecided:
    with transaction.atomic():
        decision = deciding.locked_for_decision(user, unit_id, request_id)
        version = act(decision)
    row = with_steps(Request.objects.filter(pk=request_id)).get()
    return RequestDecided(request=_unit_rows(decision.unit, [row])[0], version=version.number if version else None)


def _reason(body: RequestReasonBody | AcceptRequestBody) -> str:
    reason = body.reason.strip()
    if isinstance(body, RequestReasonBody) and not reason:
        refuse((("reason",), "Say why: the nurse reads it with her request."))
    return reason


@router.post("/{unit_id}/requests/{request_id}/accept", response=RequestDecided, auth=manager, operation_id="acceptRequest")
def accept_request(request: HttpRequest, unit_id: UUID, request_id: UUID, body: AcceptRequestBody) -> RequestDecided:
    """It becomes a pinned cell of origin `request`, refused where a pin would be. On an approved roster of a month not over,
    a new version with origin `request` when it changes a cell; a submitted roster takes it once decided."""
    reason = _reason(body)
    return _decided(request.user, unit_id, request_id, lambda decision: deciding.accept(decision, request.user, reason, request))


@router.post("/{unit_id}/requests/{request_id}/reject", response=RequestDecided, auth=manager, operation_id="rejectRequest")
def reject_request(request: HttpRequest, unit_id: UUID, request_id: UUID, body: RequestReasonBody) -> RequestDecided:
    reason = _reason(body)
    return _decided(request.user, unit_id, request_id, lambda decision: deciding.reject(decision, request.user, reason, request))


@router.post("/{unit_id}/requests/{request_id}/edit", response=RequestDecided, auth=manager, operation_id="editRequest")
def edit_request(request: HttpRequest, unit_id: UUID, request_id: UUID, body: EditRequestBody) -> RequestDecided:
    """An accepted request's promise changed, with a reason, as acceptance puts it on the roster."""
    reason = _reason(body)
    return _decided(request.user, unit_id, request_id,
                    lambda decision: deciding.edit(decision, body.shift, request.user, reason, request))


@router.post("/{unit_id}/requests/{request_id}/cancel", response=RequestDecided, auth=manager, operation_id="cancelRequest")
def cancel_request(request: HttpRequest, unit_id: UUID, request_id: UUID, body: RequestReasonBody) -> RequestDecided:
    """Its pin ends and no cell changes, so no version is made."""
    reason = _reason(body)
    return _decided(request.user, unit_id, request_id, lambda decision: deciding.cancel(decision, request.user, reason, request))
