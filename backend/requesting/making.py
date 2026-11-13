"""Opening and closing a request window, a nurse making and withdrawing a request, and making, withdrawing and asking for a
preference as a request instead: each refusal, then the write, the audit entry and whoever is told. Call each
inside a transaction. The audit log says that a window or a request moved and whose: never what a request asked for, its date or a reason, since a reason can be about someone's health and a date is part of a roster; nor
a preference's date or kind. Nobody is told of a preference."""
from __future__ import annotations

import datetime as dt
from uuid import UUID

from django.db.models import QuerySet
from django.http import Http404, HttpRequest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import User
from audit.log import record
from hospital.api import refuse
from hospital.models import Shift
from notifications import sending
from nurses import membership
from nurses.models import NurseProfile
from requesting.models import (LIVE, Preference, PreferenceKind, PreferenceState, Request, RequestState, RequestStep,
                               RequestWindow, Step, WindowState)
from requesting.windows import catch_up, not_closed, state_today
from rosters.drafts import current_code
from rosters.pins import Judge, long_date
from units.models import Unit

NO_CAP = ("This unit's request cap and preference cap are both 0, so its nurses could make neither requests nor preferences. "
          "Raise one of them on the Unit rules page to open a window.")
WINDOW_CLOSED_TO_PREFERENCES = "The request window has closed, so your preferences are fixed."


def month_name(month: dt.date) -> str:
    return f"{month:%B %Y}"


def at_the_cap(count: int, month: dt.date) -> str:
    return (f"You have {count} requests for {month_name(month)}, the most this unit allows. Withdraw a pending one to make "
            "another.")


def at_the_preference_cap(count: int, month: dt.date) -> str:
    return (f"You have {count} preferences for {month_name(month)}, the most this unit allows. Withdraw one to make "
            "another.")


def no_requests(unit: Unit) -> str:
    """A window opened on the preference cap alone."""
    return f"The {unit.name} unit takes no requests, only preferences."


def no_preferences(unit: Unit) -> str:
    return f"The {unit.name} unit takes no preferences, only requests."


def open_window(unit: Unit, month: dt.date, opens_on: dt.date, closes_on: dt.date, by: User,
                request: HttpRequest) -> RequestWindow:
    """Hold the unit's lock. A window whose open date is today opens now, and its nurses are told at once; a later one opens,
    and tells them, when the hourly job or the next write finds its date has come."""
    today = membership.today()
    if unit.request_cap == 0 and unit.preference_cap == 0:
        raise HttpError(409, NO_CAP)
    if month <= membership.this_month():
        refuse((("month",), "Choose a coming month: requests are for a month not yet begun."))
    if opens_on < today:
        refuse((("opens_on",), f"Choose today, {long_date(today)}, or a later day."))
    if closes_on < opens_on:
        refuse((("closes_on",), "The close date cannot be before the open date."))
    if closes_on >= month:
        last = month - dt.timedelta(days=1)
        refuse((("closes_on",), f"Requests have to close before {month_name(month)} begins, so that its roster can be made: "
                                f"choose {long_date(last)} or earlier."))
    waiting = next(iter(catch_up(not_closed(unit.pk))), None)
    if waiting is not None and waiting.state != WindowState.CLOSED:
        when = (f"opens on {long_date(waiting.opens_on)}" if waiting.state == WindowState.SCHEDULED
                else f"is open until {long_date(waiting.closes_on)}")
        raise HttpError(409, f"The request window for {month_name(waiting.month)} {when}. A unit has one window at a time: "
                             "close it first.")
    window = RequestWindow.objects.create(unit=unit, month=month, opens_on=opens_on, closes_on=closes_on, created_by=by)
    record("request_window.opened", actor=by, target=window.audit_target, request=request,
           after={"opens_on": opens_on.isoformat(), "closes_on": closes_on.isoformat()})
    return catch_up(RequestWindow.objects.filter(pk=window.pk))[0]


def close_window(unit: Unit, window_id: UUID, by: User, request: HttpRequest) -> RequestWindow:
    """Early, by its manager, under the unit's lock. Nurses told it opened are told it closed; nobody, if it never opened."""
    window = next(iter(catch_up(RequestWindow.objects.filter(unit=unit, pk=window_id))), None)
    if window is None:
        raise Http404("There is no such request window.")
    if window.state == WindowState.CLOSED:
        raise HttpError(409, "This window has closed already.")
    was_open = window.state == WindowState.OPEN
    window.state, window.closed_by = WindowState.CLOSED, by
    window.closed_at = window.closed_at or timezone.now()
    window.save(update_fields=["state", "closed_at", "closed_by"])
    record("request_window.closed", actor=by, target=window.audit_target, request=request)
    if was_open:
        sending.window_closed_early(window, by)
    return window


def locked_nurse(user: User) -> NurseProfile:
    """`FOR NO KEY UPDATE`, as every lock on a nurse's row is taken: it serialises a nurse's
    requests, so two made at once cannot both pass the cap, and orders them with leave and the manager's decisions."""
    nurse = (NurseProfile.objects.select_for_update(of=("self", "user"), no_key=True).select_related("user")
             .filter(user=user).first())
    if nurse is None:
        raise HttpError(403, "Only a nurse makes requests.")
    return nurse


def nurse_refusal(judge: Judge, nurse: NurseProfile, day: dt.date, asked: str | None,
                  shift: Shift | None, verb: str = "request") -> tuple[str, str] | None:
    """`Judge`'s answer, the one a manager's acceptance gets, said to the nurse, with the field it is about: a request that
    could never be accepted is refused here, and a preference that could count for nothing (`verb` "prefer")."""
    match judge.day_why(nurse.pk, day):
        case "not_in_month":
            return "date", f"You have no days on the {judge.unit.name} roster in {month_name(judge.month)}."
        case "outside_dates":
            return "date", f"{long_date(day)} is outside your dates on the roster."
        case "leave":
            return "date", f"You are on leave on {long_date(day)}, so there is nothing to {verb} that day."
    if asked is None:
        return None
    match judge.shift_why(nurse.pk, day, shift) if shift else "shift_not_worked":
        case "shift_not_worked":
            return "shift", f"{asked} is not one of the {judge.unit.name} unit's shifts."
        case "barred":
            # Hers to read in full, as her own skills are on My month; a day off is still hers to ask for.
            return "shift", (f"{judge.bar_said(nurse.pk, day, 'Your')}, so you can work no shift on {long_date(day)}. "
                             "You can still ask for the day off.")
        case "shift_not_allowed":
            return "shift", f"{asked} is not one of the shifts you work."
        case "night_on_probation":
            return "shift", (f"You are on probation in {month_name(judge.month)}, so you work no {asked}, the night shift. You "
                             "can still ask for the day off.")
    return None


def window_for(nurse: NurseProfile, month: dt.date) -> RequestWindow | None:
    """The latest window of the unit this month falls under for the nurse, which may be one they are due to join."""
    placement = membership.placement_in(nurse, month)
    if placement is None:
        return None
    return RequestWindow.objects.filter(unit_id=placement.unit_id, month=month).select_related("unit").first()


def live_in(nurse: NurseProfile, month: dt.date, unit: Unit) -> QuerySet[Request]:
    """Those that count against this unit's cap: one left behind in another unit by a transfer is that unit's to decide."""
    return Request.objects.filter(nurse=nurse, window__unit=unit, date__year=month.year, date__month=month.month,
                                  state__in=LIVE)


def _open_window_for(nurse: NurseProfile, month: dt.date) -> RequestWindow:
    latest = window_for(nurse, month)
    window = catch_up(RequestWindow.objects.filter(pk=latest.pk))[0] if latest else None
    if window is None or window.state != WindowState.OPEN:
        raise HttpError(409, f"No request window is open for {month_name(month)}.")
    return window


def live_request_on(nurse: NurseProfile, day: dt.date) -> bool:
    """Any unit's: a request a transfer left behind still holds its date until its manager decides it."""
    return Request.objects.filter(nurse=nurse, date=day, state__in=LIVE).exists()


def live_preference_on(nurse: NurseProfile, day: dt.date) -> bool:
    """Any unit's: one a transfer left behind still holds its date, as a request does."""
    return Preference.objects.filter(nurse=nurse, date=day, state=PreferenceState.MADE).exists()


def make(user: User, day: dt.date, asked: str | None, reason: str, request: HttpRequest) -> Request:
    nurse = locked_nurse(user)
    month = day.replace(day=1)
    window = _open_window_for(nurse, month)
    unit = Unit.objects.get(pk=window.unit_id)
    if unit.request_cap == 0:
        raise HttpError(409, no_requests(unit))
    code = current_code(asked) if asked else None
    shift = Shift.objects.filter(code=code, active=True).first() if code else None
    refusal = nurse_refusal(Judge(unit, month), nurse, day, code, shift)
    if refusal:
        refuse(((refusal[0],), refusal[1]))
    live = list(live_in(nurse, month, unit))
    if live_request_on(nurse, day):
        refuse((("date",), f"You already have a request on {long_date(day)}. Withdraw it to ask for something else."))
    if live_preference_on(nurse, day):
        refuse((("date",), f"You have a preference on {long_date(day)}, and a date holds one wish. Withdraw it, or ask for it "
                           "as a request instead."))
    if len(live) >= unit.request_cap:
        raise HttpError(409, at_the_cap(len(live), month))
    made = Request.objects.create(window=window, nurse=nurse, date=day, shift=shift)
    RequestStep.objects.create(request=made, step=Step.MADE, by=user, reason=reason)
    record("request.made", actor=user, target=user.username, request=request)
    return made


def withdraw(user: User, request_id: UUID, request: HttpRequest) -> Request:
    nurse = locked_nurse(user)
    mine = Request.objects.select_for_update(of=("self",)).filter(pk=request_id, nurse=nurse).first()
    if mine is None:
        raise Http404("You have no such request.")
    if mine.state != RequestState.PENDING:
        raise HttpError(409, f"This request is {mine.state_words}, so it cannot be withdrawn.")
    window = catch_up(RequestWindow.objects.filter(pk=mine.window_id))[0]
    if state_today(window) != WindowState.OPEN:
        raise HttpError(409, "The request window has closed, so this request waits for your manager's decision.")
    mine.state = RequestState.WITHDRAWN
    mine.save(update_fields=["state"])
    RequestStep.objects.create(request=mine, step=Step.WITHDRAWN, by=user)
    record("request.withdrawn", actor=user, target=user.username, request=request)
    return mine


def live_preferences_in(nurse: NurseProfile, month: dt.date, unit: Unit) -> QuerySet[Preference]:
    """Those that count against this unit's preference cap, across the month's windows: one a transfer left on another unit's
    record counts against that unit's only."""
    return Preference.objects.filter(nurse=nurse, window__unit=unit, date__year=month.year, date__month=month.month,
                                     state=PreferenceState.MADE)


def make_preference(user: User, day: dt.date, kind: PreferenceKind, request: HttpRequest) -> Preference:
    """Under the nurse's lock, as a request's count is taken, so two made at once cannot both pass the cap."""
    nurse = locked_nurse(user)
    month = day.replace(day=1)
    window = _open_window_for(nurse, month)
    unit = Unit.objects.get(pk=window.unit_id)
    if unit.preference_cap == 0:
        raise HttpError(409, no_preferences(unit))
    refusal = nurse_refusal(Judge(unit, month), nurse, day, None, None, verb="prefer")
    if refusal:
        refuse(((refusal[0],), refusal[1]))
    if live_request_on(nurse, day):
        refuse((("date",), f"You have a request on {long_date(day)}, and a date holds one wish. Withdraw the request to make a "
                           "preference instead."))
    if live_preference_on(nurse, day):
        refuse((("date",), f"You already have a preference on {long_date(day)}. Withdraw it to prefer something else."))
    count = live_preferences_in(nurse, month, unit).count()
    if count >= unit.preference_cap:
        raise HttpError(409, at_the_preference_cap(count, month))
    made = Preference.objects.create(window=window, nurse=nurse, date=day, kind=kind)
    record("preference.made", actor=user, target=user.username, request=request)
    return made


def _locked_preference(user: User, preference_id: UUID, doing: str) -> Preference:
    """A made one of hers, while its window is open. Another nurse's is the same 404 as none."""
    nurse = locked_nurse(user)
    mine = Preference.objects.select_for_update(of=("self",)).filter(pk=preference_id, nurse=nurse).first()
    if mine is None:
        raise Http404("You have no such preference.")
    if mine.state != PreferenceState.MADE:
        raise HttpError(409, f"This preference is {mine.state_words}, so it cannot be {doing}.")
    window = catch_up(RequestWindow.objects.filter(pk=mine.window_id))[0]
    if state_today(window) != WindowState.OPEN:
        raise HttpError(409, WINDOW_CLOSED_TO_PREFERENCES)
    return mine


def _end(preference: Preference, state: PreferenceState, user: User) -> None:
    preference.state, preference.ended_at, preference.ended_by = state, timezone.now(), user
    preference.save(update_fields=["state", "ended_at", "ended_by"])


def withdraw_preference(user: User, preference_id: UUID, request: HttpRequest) -> Preference:
    mine = _locked_preference(user, preference_id, "withdrawn again")
    _end(mine, PreferenceState.WITHDRAWN, user)
    record("preference.withdrawn", actor=user, target=user.username, request=request)
    return mine


def ask_instead(user: User, preference_id: UUID, asked: str | None, request: HttpRequest) -> tuple[Preference, Request]:
    """The request is made as any request is, under the request cap and the window's rules, for a day off or, for a working
    day, the shift she chooses. Ending the preference first frees its date; a refusal of the request raises, and the caller's
    transaction then undoes the preference's end too, so it fails as a whole."""
    mine = _locked_preference(user, preference_id, "asked for as a request")
    month = mine.date.replace(day=1)
    current = window_for(mine.nurse, month)
    if current is None or current.pk != mine.window_id:
        # A transfer since: the request would land in another unit's window than the one the preference was made in.
        raise HttpError(409, f"You are no longer on this unit's roster for {month_name(month)}, so this preference cannot "
                             "become a request. Make the request in your new unit's window instead.")
    if mine.kind == PreferenceKind.DAY_OFF and asked:
        refuse((("shift",), "This preference is for a day off, so it is asked for as a day off."))
    if mine.kind == PreferenceKind.WORKING_DAY and not asked:
        refuse((("shift",), "Choose the shift you ask for."))
    _end(mine, PreferenceState.ASKED_INSTEAD, user)
    made = make(user, mine.date, asked, "", request)
    mine.request = made
    mine.save(update_fields=["request"])
    record("preference.asked_instead", actor=user, target=user.username, request=request)
    return mine, made
