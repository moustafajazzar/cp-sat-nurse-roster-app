"""A notification's words and where it leads, worked out as it is read from the identifiers it keeps, so they follow a rename and
a corrected name. They say only what the reader could read on the page it leads to, and nothing about any nurse: no leave, no
comment, no colleague; but for a probation ending, which names the nurse to her unit manager, who reads it on Rostering
attributes. The link is the reader's own page for that month, by their role and what they hold now."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from django.db.models import Count, Q

from accounts.models import Role, User
from nurses.membership import last_day_of
from nurses.models import NurseProfile
from closes.models import MonthClose
from notifications.models import Event, Notification, SolveOutcome
from covers.models import Cover
from covers.steps import which as cover_shift_day
from notifications.sending import CoverStep, MonthDates, SwapStep
from requesting.models import Request, RequestState, RequestWindow, Step
from rosters.pins import long_date
from swaps.models import Swap
from units.models import Unit

WINDOW_EVENTS = (Event.WINDOW_OPENED, Event.WINDOW_CLOSED_EARLY, Event.REQUEST_DECIDED)


@dataclass(frozen=True)
class Link:
    page: Literal["roster", "my_roster", "unit_roster", "my_month", "requests", "swaps", "unit_swaps", "covers", "unit_covers",
                  "rostering_attributes"]
    month: str
    unit_id: UUID | None = None
    cover_id: UUID | None = None


class Names:
    """The units and users a page of notifications names, read in two queries."""

    def __init__(self, notes: list[Notification]):
        unit_ids = {note.payload.get("unit_id") for note in notes} - {None}
        user_ids = {note.payload.get("by") for note in notes} - {None}
        self.units = {str(unit.pk): unit for unit in Unit.objects.filter(pk__in=unit_ids)}
        self.users = {str(user.pk): user.full_name or user.username for user in User.objects.filter(pk__in=user_ids)}
        window_ids = {note.payload.get("window_id") for note in notes} - {None}
        request_ids = {note.payload.get("request_id") for note in notes} - {None}
        # How many of each window's requests still wait, counted here rather than once a notification.
        self.windows = {str(window.pk): window for window in RequestWindow.objects.filter(pk__in=window_ids).annotate(
            waiting=Count("requests", filter=Q(requests__state=RequestState.PENDING)))}
        self.requests = {str(row.pk): row for row in Request.objects.filter(pk__in=request_ids).select_related("shift")}
        close_ids = {note.payload.get("close_id") for note in notes} - {None}
        self.closes = {str(close.pk): close for close in MonthClose.objects.filter(pk__in=close_ids)}
        swap_ids = {note.payload.get("swap_id") for note in notes} - {None}
        self.swaps = {str(row.pk): row for row in Swap.objects.filter(pk__in=swap_ids).select_related("proposer__user")}
        cover_ids = {note.payload.get("cover_id") for note in notes} - {None}
        self.covers = {str(row.pk): row for row in Cover.objects.filter(pk__in=cover_ids).select_related("shift")}
        nurse_ids = {note.payload.get(key) for note in notes for key in ("nurse_id", "preceptor_id")} - {None}
        self.nurses = {str(row.pk): row.user.full_name for row in NurseProfile.objects.filter(pk__in=nurse_ids).select_related("user")}


MONTH_DATES_SAY = {MonthDates.RAMADAN_DAYS: "Ramadan days", MonthDates.PUBLIC_HOLIDAYS: "public holidays"}


def _month_name(month: str) -> str:
    return dt.date.fromisoformat(f"{month}-01").strftime("%B %Y")


def _replan_words(payload: dict, roster: str) -> str:
    stays = "so the roster in force stays as it was."
    match payload.get("outcome"):
        case SolveOutcome.REPLANNED:
            return (f"The re-plan of the {roster} has finished, and its roster is now in force as a new version. The unit's "
                    "nurses are told the roster has changed.")
        case SolveOutcome.STOPPED_REPLANNED:
            return (f"The re-plan of the {roster} was stopped, and the best roster it found is now in force as a new version. "
                    "The unit's nurses are told the roster has changed.")
        case SolveOutcome.REPLAN_UNCHANGED:
            return f"The re-plan of the {roster} has finished without changing a cell, {stays}"
        case SolveOutcome.REPLAN_OUTDATED:
            return (f"The re-plan of the {roster} has ended, but the roster in force or the month changed while it ran, so it "
                    "stays as it was. Re-plan again to take that in.")
        case SolveOutcome.REPLAN_MONTH_OVER:
            return f"The re-plan of the {roster} has ended after the month did, {stays}"
        case SolveOutcome.FAILED:
            return f"The re-plan of the {roster} failed, {stays} Its result says why."
        case SolveOutcome.NO_ROSTER:
            return f"The re-plan of the {roster} has ended with no roster to keep, {stays} Its result says why."
    return f"The re-plan of the {roster} has ended."


def _solve_words(payload: dict, roster: str) -> str:
    if payload.get("replan"):
        return _replan_words(payload, roster)
    match payload.get("outcome"):
        case SolveOutcome.DRAFT:
            return f"The solve of the {roster} has finished, and its roster is now the draft."
        case SolveOutcome.STOPPED_DRAFT:
            return f"The solve of the {roster} was stopped, and the best roster it found is now the draft."
        case SolveOutcome.FROZEN:
            return f"The solve of the {roster} has ended. The roster had been submitted or approved meanwhile, so it stays as it was."
        case SolveOutcome.FAILED:
            return f"The solve of the {roster} failed. Its result says why."
        case SolveOutcome.NO_ROSTER:
            return f"The solve of the {roster} has ended with no roster to keep. Its result says why."
    return f"The solve of the {roster} has ended."


def _window_words(note: Notification, names: Names, roster: str, by: str) -> str:
    window = names.windows.get(note.payload.get("window_id"))
    match note.event:
        case Event.WINDOW_OPENED:
            until = f", until {long_date(window.closes_on)}" if window else ""
            return f"{by} opened requests for the {roster}{until}."
        case Event.WINDOW_CLOSED_EARLY:
            return (f"{by} closed the request window for the {roster} early. Any request you made in it waits for a "
                    "decision.")
    waiting = window.waiting if window else 0
    if waiting == 0:
        return f"The request window for the {roster} has closed. Every request made in it has been decided."
    many = f"{waiting} requests wait" if waiting > 1 else "1 request waits"
    return f"The request window for the {roster} has closed, and {many} for your decision."


def _decision_words(note: Notification, names: Names, by: str) -> str:
    """Only the nurse who made the request is told, so it may say what she asked for, read from the request now. Never a
    reason: those are on her page."""
    row = names.requests.get(note.payload.get("request_id"))
    if row is None:
        return "One of your requests has moved."
    what = row.what
    on = long_date(row.date)
    match note.payload.get("step"):
        case Step.ACCEPTED:
            return f"{by} accepted your request for {what} on {on}."
        case Step.REJECTED:
            return f"{by} rejected your request for {what} on {on}. The reason is on My month."
        case Step.EDITED:
            return f"{by} changed what your accepted request on {on} promises. My month says what it is now."
        case Step.CANCELLED:
            return f"{by} cancelled your accepted request for {what} on {on}. The reason is on My month."
        case Step.REPLACED_BY_LEAVE:
            return f"Leave recorded on {on} replaced your request for {what}, as leave wins."
        case Step.EXPIRED:
            return f"Your request for {what} on {on} has ended: the month closed before a decision."
    return f"Your request for {what} on {on} has moved."


def _swap_words(note: Notification, names: Names, roster: str, by: str) -> str:
    """Read from the swap when shown, and only what everyone in it reads on Swaps: never its cells, and never why it expired,
    since a cell a version changed can be a colleague's leave."""
    swap = names.swaps.get(note.payload.get("swap_id"))
    proposer = (swap.proposer.user.full_name or swap.proposer.user.username) if swap else "A nurse"
    whose = "you proposed" if swap and swap.proposer.user_id == note.user_id else f"{proposer} proposed"
    match note.payload.get("step"):
        case SwapStep.PROPOSED:
            return f"{proposer} proposed a swap on the {roster} that changes your cells. Accept or reject it on Swaps."
        case SwapStep.ACCEPTED:
            return f"{by} accepted the swap you proposed on the {roster}."
        case SwapStep.AWAITING:
            return f"The swap {proposer} proposed on the {roster} waits for your decision."
        case SwapStep.REJECTED if swap and swap.reason:
            return f"{by} rejected the swap {whose} on the {roster}. The reason is on Swaps."
        case SwapStep.REJECTED:
            return f"{by} rejected the swap {whose} on the {roster}, so it has ended."
        case SwapStep.WITHDRAWN:
            return f"{proposer} withdrew the swap they proposed on the {roster}."
        case SwapStep.EXPIRED:
            return f"The swap {whose} on the {roster} has expired, so it changes nothing."
        case SwapStep.APPROVED:
            return f"{by} approved the swap {whose} on the {roster}. Its cells are in the roster in force."
    return f"A swap on the {roster} has moved."


def _cover_words(note: Notification, names: Names, by: str) -> str:
    """Read from the cover when shown: its shift and date, which the nurse offered it reads on Covers. Never why it was short,
    who else was asked, who took it or why it ended."""
    cover = names.covers.get(note.payload.get("cover_id"))
    which = cover_shift_day(cover) if cover else "a shift"
    match note.payload.get("step"):
        case CoverStep.OFFERED:
            return f"{by} offered you a cover: {which}. Accept or decline it on Covers."
        case CoverStep.DECLINED:
            return f"{by} declined the cover for {which}."
        case CoverStep.ACCEPTED:
            return f"{by} accepted the cover for {which}. It is in the roster in force."
        case CoverStep.TAKEN:
            return f"The cover for {which} offered to you has been taken."
        case CoverStep.WITHDRAWN:
            return f"{by} withdrew the cover for {which} offered to you."
        case CoverStep.FILLED:
            return f"The cover for {which} offered to you is no longer needed."
        case CoverStep.EXPIRED:
            return f"The cover for {which} offered to you has expired."
    return f"The cover for {which} has moved."


def _close_words(note: Notification, names: Names, roster: str) -> str:
    """How many hard rules the facts broke, read from the close; never which, since one can be a nurse's leave."""
    close = names.closes.get(note.payload.get("close_id"))
    broken = len(close.result["facts_broke"]) if close else 0
    if not broken:
        return f"The {roster} has closed. Its recorded facts broke no hard rule."
    return (f"The {roster} has closed. Its recorded facts broke {broken} hard rule{'' if broken == 1 else 's'}, listed with the "
            "roster.")


def _preceptor_words(note: Notification, names: Names, unit) -> str:
    """The two nurses' names, which each of them and her unit's manager read on Rostering attributes and My month."""
    payload = note.payload
    nurse = names.nurses.get(payload.get("nurse_id")) or "A nurse on probation"
    preceptor = names.nurses.get(payload.get("preceptor_id")) or "A nurse"
    month = _month_name(payload["month"])
    if note.event == Event.PRECEPTOR_LEAVING:
        where = unit.name if unit else "the unit"
        return (f"{month} is {preceptor}'s last month in {where}, so {preceptor} no longer precepts {nurse} after it. Choose "
                f"another preceptor for {nurse}.")
    match payload.get("role"):
        case "nurse":
            return f"From {month} your preceptor is {preceptor}. You work beside them on their day shifts."
        case "preceptor":
            return f"From {month} you precept {nurse}, a nurse on probation, who works beside you on your day shifts."
    return f"From {month} you no longer precept {nurse}."


def text_of(note: Notification, names: Names) -> str:
    payload = note.payload
    unit = names.units.get(payload.get("unit_id"))
    roster = f"{unit.name if unit else 'a unit'} roster for {_month_name(payload['month'])}"
    by = names.users.get(payload.get("by"), "Someone")
    match note.event:
        case Event.ROSTER_SUBMITTED:
            return f"{by} submitted the {roster}."
        case Event.ROSTER_APPROVED:
            return f"{by} approved the {roster}."
        case Event.ROSTER_CHANGED:
            return f"{by} changed the {roster}."
        case Event.ROSTER_REJECTED:
            return f"{by} rejected the {roster}. The comment is with the draft."
        case Event.SOLVE_ENDED:
            return _solve_words(payload, roster)
        case Event.WINDOW_OPENED | Event.WINDOW_CLOSED_EARLY | Event.WINDOW_CLOSED:
            return _window_words(note, names, roster, by)
        case Event.REQUEST_DECIDED:
            return _decision_words(note, names, by)
        case Event.MONTH_CLOSED:
            return _close_words(note, names, roster)
        case Event.SWAP_STEP:
            return _swap_words(note, names, roster, by)
        case Event.COVER_STEP:
            return _cover_words(note, names, by)
        case Event.MONTH_DATES_CHANGED:
            dates = MONTH_DATES_SAY.get(payload.get("what"), "dates")
            return (f"{by} changed the {dates} of {_month_name(payload['month'])}. Check the {unit.name if unit else 'unit'} "
                    "roster for that month: it was planned on the dates before.")
        case Event.PROBATION_ENDING:
            last = last_day_of(dt.date.fromisoformat(f"{payload['month']}-01"))
            return f"{names.nurses.get(payload.get('nurse_id'), 'A nurse')}'s probation ends on {last:%-d %B}. Extend it or let it end."
        case Event.PRECEPTOR_ASSIGNED | Event.PRECEPTOR_LEAVING:
            return _preceptor_words(note, names, unit)
    return "Something has changed."


def link_of(note: Notification, reader: User, names: Names) -> Link | None:
    payload = note.payload
    unit = names.units.get(payload.get("unit_id"))
    if unit is None:
        return None
    month = payload["month"]
    if note.event in WINDOW_EVENTS:
        return Link("my_month", month) if reader.role == Role.STAFF_NURSE else None
    if note.event == Event.SWAP_STEP:
        if unit.manager_id == reader.pk:
            return Link("unit_swaps", month)
        return Link("swaps", month) if reader.role == Role.STAFF_NURSE else None
    if note.event == Event.COVER_STEP:
        if unit.manager_id == reader.pk:
            return Link("unit_covers", month, cover_id=payload.get("cover_id"))
        return Link("covers", month) if reader.role == Role.STAFF_NURSE else None
    if note.event in (Event.PROBATION_ENDING, Event.PRECEPTOR_LEAVING):
        return Link("rostering_attributes", month) if unit.manager_id == reader.pk else None
    if note.event == Event.PRECEPTOR_ASSIGNED:
        return Link("my_month", month) if reader.role == Role.STAFF_NURSE else None
    if note.event == Event.WINDOW_CLOSED:
        return Link("requests", month) if unit.manager_id == reader.pk else None
    if reader.role in (Role.CNO, Role.DEPUTY_CNO):
        return Link("unit_roster", month, unit.pk)
    if reader.role == Role.UNIT_MANAGER and unit.manager_id == reader.pk:
        return Link("roster", month)
    if note.event in (Event.ROSTER_APPROVED, Event.ROSTER_CHANGED) and reader.role in (Role.STAFF_NURSE, Role.UNIT_MANAGER):
        return Link("my_roster", month)
    return None  # A manager who has since lost the unit: its pages are no longer theirs.
