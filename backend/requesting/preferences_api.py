"""A staff nurse's own preferences for a month: the window, the preference cap, making and withdrawing them, and
asking for one as a request instead. Nothing here takes a nurse, so nobody reads a colleague's preferences; her unit's manager
reads them, read-only, beside each window's requests (`requesting.api`)."""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from uuid import UUID

from django.db import transaction
from django.http import HttpRequest
from ninja import Field, Query, Router, Schema, Status

from accounts.authz import SignedIn
from accounts.models import Role
from hospital.api import StrictSchema
from hospital.catalogue_api import IsoDate
from nurses import membership
from nurses.models import NurseProfile
from requesting import making
from requesting.api import PreferenceKindText, PreferenceStateText, with_steps
from requesting.mine_api import MyRequestRow, MyWindow, RequestableShift, my_rows, requestable_shifts
from requesting.models import Preference, PreferenceState, Request, WindowState
from requesting.windows import state_today
from rosters.own_month import Outcome, OwnPreferences, closed_preferences, doubled_by, own_preferences
from rosters.reading_api import MyRosterFilter, in_force

router = Router(tags=["preferences"])
nurse_only = SignedIn(Role.STAFF_NURSE)


class MyPreferenceRow(Schema):
    id: UUID
    date: dt.date
    kind: PreferenceKindText = Field(description="A day off, or a working day of any shift.")
    state: PreferenceStateText = Field(description="`made` while it is live and reaches the solve.")
    outcome: Outcome | None = Field(description=(
        "What the roster in force made of a made one: `kept`, `not_kept`, `on_leave` for a date she is on leave or outside "
        "her dates, or `barred` for one after the expiry of a skill of hers required to work, both counted neither way. Read from the version in force on the month as it stands, and once the month is closed "
        "from the close in force. Null before the roster is approved, and for one that never reached the unit's solve."))


class MyPreferences(Schema):
    month: str = Field(description="YYYY-MM.")
    window: MyWindow | None = Field(description="The latest window for this month of the unit it falls under for you.")
    preference_cap: int | None = Field(description="Preferences a nurse may have made this month.")
    request_cap: int | None = Field(description="Requests a nurse may have pending or accepted this month, which asking for a "
                                                "preference as a request counts against.")
    counted: int = Field(description="Yours made this month, which count against the cap.")
    refusal: str | None = Field(description="Why you cannot make a preference for this month now.")
    shifts: list[RequestableShift] = Field(description="The shifts you may ask for, when asking for a working day as a "
                                                       "request.")
    first_day: dt.date | None = Field(description="The first day of the month you may prefer, within your dates.")
    last_day: dt.date | None
    preferences: list[MyPreferenceRow] = Field(description="Yours for this month, every state, by date.")
    kept: int | None = Field(description="How many the roster in force kept. Null before it is approved.")
    keepable: int | None = Field(description="How many it could keep: those with an outcome, less those on leave or barred.")
    doubled_by: str | None = Field(description=(
        "YYYY-MM: the month one of yours was not kept in, so yours count double this month. From the carry-over the month's "
        "solve input takes. Null when none was."))


def _rows(preferences: Iterable[Preference], kept: OwnPreferences | None = None) -> list[MyPreferenceRow]:
    outcomes = kept.outcomes if kept else {}  # by date and kind: a withdrawn one may share both with the one made since
    return [MyPreferenceRow(id=row.pk, date=row.date, kind=row.kind, state=row.state,
                            outcome=outcomes.get((row.date, row.kind)) if row.state == PreferenceState.MADE else None)
            for row in preferences]


def _what_was_kept(user, chosen: str) -> OwnPreferences | None:
    """What the roster in force kept, from the version My month reads (`rosters.reading_api.in_force`): none before it is
    approved, and none for a month over until it has closed, as My month's hours."""
    found = in_force(user, chosen)
    if found.version is None:
        return None
    mine = found.nurse.user.username
    if found.close is not None:
        return closed_preferences(found.close, mine)
    grid = found.grid
    return own_preferences(found.unit, found.month, mine, grid[mine]) if mine in grid else None


@router.get("/mine", response=MyPreferences, auth=nurse_only, operation_id="getMyPreferences")
def my_preferences(request: HttpRequest, filters: Query[MyRosterFilter]) -> MyPreferences:
    """The hospital's current month when none is given, as the rest of My month."""
    month = dt.date.fromisoformat(f"{filters.month}-01") if filters.month else membership.this_month()
    nurse = NurseProfile.objects.select_related("user").filter(user=request.user).first()
    kept = _what_was_kept(request.user, month.strftime("%Y-%m"))
    rows = _rows(Preference.objects.filter(nurse=nurse, date__year=month.year, date__month=month.month), kept)
    window = making.window_for(nurse, month) if nurse else None
    doubled = doubled_by(nurse, month) if nurse else None
    answer = MyPreferences(month=month.strftime("%Y-%m"), window=None, preference_cap=None, request_cap=None, counted=0,
                           shifts=[], first_day=None, last_day=None, preferences=rows,
                           kept=kept.kept if kept else None, keepable=kept.keepable if kept else None,
                           doubled_by=doubled.strftime("%Y-%m") if doubled else None,
                           refusal=f"No request window is open for {making.month_name(month)}.")
    if window is None:
        return answer
    unit, state = window.unit, state_today(window)
    answer.window = MyWindow(unit=unit.name, opens_on=window.opens_on, closes_on=window.closes_on, state=state,
                             closed_early=window.closed_by_id is not None)
    answer.preference_cap, answer.request_cap = unit.preference_cap, unit.request_cap
    answer.counted = making.live_preferences_in(nurse, month, unit).count()
    first, last = max(month, nurse.start_date), min(membership.last_day_of(month), nurse.end_date or dt.date.max)
    if first <= last:
        answer.first_day, answer.last_day = first, last
    answer.shifts = requestable_shifts(nurse, month, unit)
    if state != WindowState.OPEN:
        return answer
    if first > last:
        answer.refusal = f"You have no days on the {unit.name} roster in {making.month_name(month)}."
    elif unit.preference_cap == 0:
        answer.refusal = making.no_preferences(unit)
    elif answer.counted >= unit.preference_cap:
        answer.refusal = making.at_the_preference_cap(answer.counted, month)
    else:
        answer.refusal = None
    return answer


class NewPreferenceBody(StrictSchema):
    date: IsoDate
    kind: PreferenceKindText


@router.post("/mine", response={201: MyPreferenceRow}, auth=nurse_only, operation_id="makePreference")
def make_preference(request: HttpRequest, body: NewPreferenceBody) -> Status:
    """Within an open window of the unit the date's month falls under for you, up to the unit's preference cap, on a date
    holding no request or other preference. No reason, and nobody is told. The audit log says that you made one, never what or
    when for."""
    with transaction.atomic():
        made = making.make_preference(request.user, body.date, body.kind, request)
    return Status(201, _rows([made])[0])


@router.post("/mine/{preference_id}/withdraw", response=MyPreferenceRow, auth=nurse_only, operation_id="withdrawPreference")
def withdraw_preference(request: HttpRequest, preference_id: UUID) -> MyPreferenceRow:
    """A made one, while its window is open. Another nurse's is the same 404 as none."""
    with transaction.atomic():
        return _rows([making.withdraw_preference(request.user, preference_id, request)])[0]


class AskInsteadBody(StrictSchema):
    shift: str | None = Field(max_length=8, description="For a working day, the shift's code you ask for. Null for a day off.")


class AskedInstead(Schema):
    preference: MyPreferenceRow
    request: MyRequestRow


@router.post("/mine/{preference_id}/ask-as-request", response={201: AskedInstead}, auth=nurse_only,
             operation_id="askPreferenceAsRequest")
def ask_as_request(request: HttpRequest, preference_id: UUID, body: AskInsteadBody) -> Status:
    """A made one, while its window is open, becomes a pending request under the request cap and the window's rules, and ends
    as `asked_instead`. Refused as a whole when the request is: the preference then stays made."""
    with transaction.atomic():
        preference, made = making.ask_instead(request.user, preference_id, body.shift or None, request)
    request_row = my_rows(with_steps(Request.objects.filter(pk=made.pk)))[0]
    return Status(201, AskedInstead(preference=_rows([preference])[0], request=request_row))
