"""The push job's work: for each notification of a kind with a deadline, to each subscription of its reader, the kind
code alone, but only while the thing still waits and the reader still has a page for it. What each push service answers moves the
subscription: a success resets its failures, a gone address ends it, and a push that does not reach it counts one failure, the
tenth ending it. An attempt that could not reach the service is tried again for that subscription alone (`RETRY_IN`). A job
starts no new send once its budget is spent (`BUDGET_SECONDS`), and hands the sends it has not made to the next."""
from __future__ import annotations

import datetime as dt
import time
from typing import NamedTuple

from accounts.models import User
from notifications import push_keys, pushing, reading, subscriptions
from notifications.kinds import PushKind, kind_of
from notifications.models import Notification, PushAttempt, PushOutcome, PushSubscription
from requesting.models import RequestState, RequestWindow, WindowState
from requesting.windows import state_today
from rosters.models import Roster, RosterState
from swaps.expiring import state_now
from swaps.models import Answer, Swap, SwapParticipant, SwapState

RETRY_IN = (60, 180, 360)
"""Seconds before each try again of a subscription the push service could not be reached at: three, over about ten minutes."""

Pairs = list[list[str]]
"""[notification id, subscription id] of the sends still to make, carried from one try to the next."""

BUDGET_SECONDS = 30
"""How long one push job sends before it hands on the rest, so that no job, of a user with many devices or to a slow
push service, holds every other push back by more than about this and one send (a send waits up to 10 seconds to connect and
10 to read)."""

clock = time.monotonic
"""The job's own clock, in seconds; the tests move it."""


def _pair(note: Notification, subscription_id) -> list[str]:
    return [str(note.pk), str(subscription_id)]


class Delivered(NamedTuple):
    retry: Pairs
    """The sends that could not reach their push service, to try again."""
    handed_on: Pairs
    """The sends not made because the budget was spent, for the next job to make at the same try."""


def _month(payload: dict) -> dt.date:
    return dt.date.fromisoformat(f"{payload['month']}-01")


def _swap(note: Notification) -> Swap | None:
    return Swap.objects.prefetch_related("cells").filter(pk=note.payload.get("swap_id")).first()


def _window(note: Notification) -> RequestWindow | None:
    return RequestWindow.objects.filter(pk=note.payload.get("window_id")).first()


def _roster(note: Notification) -> Roster | None:
    return Roster.objects.filter(unit_id=note.payload.get("unit_id"), month=_month(note.payload)).first()


def _swap_to_answer(note: Notification) -> bool:
    """Still pending, and this colleague has not answered it, on another device say."""
    swap = _swap(note)
    return swap is not None and state_now(swap) == SwapState.PENDING and SwapParticipant.objects.filter(
        swap=swap, nurse__user_id=note.user_id, answer=Answer.PENDING).exists()


def _swap_to_decide(note: Notification) -> bool:
    swap = _swap(note)
    return swap is not None and state_now(swap) == SwapState.AWAITING_MANAGER


def _requests_open(note: Notification) -> bool:
    window = _window(note)
    return window is not None and state_today(window) == WindowState.OPEN


def _requests_to_decide(note: Notification) -> bool:
    window = _window(note)
    return (window is not None and state_today(window) == WindowState.CLOSED
            and window.requests.filter(state=RequestState.PENDING).exists())


def _roster_to_approve(note: Notification) -> bool:
    roster = _roster(note)
    return roster is not None and roster.state == RosterState.SUBMITTED


def _roster_sent_back(note: Notification) -> bool:
    """Still a draft, and not submitted again since the rejection."""
    roster = _roster(note)
    return (roster is not None and roster.state == RosterState.DRAFT
            and not roster.submissions.filter(submitted_at__gt=note.created_at).exists())


_STILL_WAITS = {
    PushKind.SWAP_TO_ANSWER: _swap_to_answer, PushKind.SWAP_TO_DECIDE: _swap_to_decide,
    PushKind.REQUESTS_OPEN: _requests_open, PushKind.REQUESTS_TO_DECIDE: _requests_to_decide,
    PushKind.ROSTER_TO_APPROVE: _roster_to_approve, PushKind.ROSTER_SENT_BACK: _roster_sent_back,
}


def still_waits(kind: PushKind, note: Notification) -> bool:
    """Read as the pages read it, from the dates as well as the stored state, so a swap past its first date or a window past
    its close date is no longer waiting, whether or not the hourly job has stored it."""
    return _STILL_WAITS[kind](note)


def has_page(note: Notification) -> bool:
    """The Notifications page's own rule: a reader it would give no link to (a manager taken off the unit, say) gets no push."""
    reader = User.objects.filter(pk=note.user_id, is_active=True).first()
    return reader is not None and reading.link_of(note, reader, reading.Names([note])) is not None


def deliver(notification_ids: list[str], *, started: float, budget: float = BUDGET_SECONDS, attempt: int = 0,
            remaining: Pairs | None = None) -> Delivered:
    """One try. `remaining`, on a try again or a job handed on, holds the sends left to make; the first try makes one to each
    subscription the reader holds now. `started` is when the job started, on `clock`: once `budget` seconds have passed it starts
    no new send, and returns the sends it did not make beside those to try again. Those handed on are read again by the next
    job, as a try again is: whether the thing still waits, and whose the subscription is."""
    retry: Pairs = []
    handed_on: Pairs = []
    last_try = attempt >= len(RETRY_IN)
    key = None

    def spent() -> bool:
        return clock() - started >= budget

    for note in Notification.objects.filter(pk__in=notification_ids).order_by("pk"):
        # The reader's own: an address moved to another user since is not sent this one.
        held = PushSubscription.objects.filter(user_id=note.user_id).order_by("pk")
        if remaining is not None:
            held = held.filter(pk__in=[subscription_id for note_id, subscription_id in remaining if note_id == str(note.pk)])
        if spent():
            handed_on += [_pair(note, subscription_id) for subscription_id in held.values_list("pk", flat=True)]
            continue
        kind = kind_of(note.event, note.payload)
        if kind is None or not still_waits(kind, note) or not has_page(note):
            continue
        for subscription in held:
            if not subscriptions.allowed(subscription.endpoint):
                subscriptions.end(subscription.pk)
                continue
            if spent():
                handed_on.append(_pair(note, subscription.pk))
                continue
            key = key or push_keys.private_key()
            outcome = pushing.send(endpoint=subscription.endpoint, p256dh=subscription.p256dh, auth=subscription.auth,
                                   kind=kind, private_key=key)
            PushAttempt.objects.create(outcome=outcome)
            match outcome:
                case PushOutcome.SENT:
                    subscriptions.reached(subscription.pk)
                case PushOutcome.GONE:
                    subscriptions.end(subscription.pk)
                case PushOutcome.UNREACHABLE if not last_try:
                    retry.append(_pair(note, subscription.pk))
                case _:  # refused, or unreachable at the last try
                    subscriptions.not_reached(subscription.pk)
    return Delivered(retry, handed_on)
