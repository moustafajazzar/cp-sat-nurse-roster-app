"""The one way to tell users something. Call `notify` inside the transaction that makes the change, after its audit entry, so a
change rolled back tells nobody. The recipients of each event that exists are worked out here, next to each other."""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import TYPE_CHECKING
from uuid import UUID

from django.db.models import QuerySet, TextChoices
from django.utils import timezone

from accounts.models import Role, User
from notifications.kinds import kind_of
from notifications.models import Event, Notification, SolveOutcome
from nurses import membership
from ops import queue
from units.models import Unit

if TYPE_CHECKING:
    from covers.models import Cover
    from requesting.models import Request, RequestWindow
    from swaps.models import Swap


def notify(users: Iterable[User | None], event: Event, **payload) -> int:
    """Each active user once, whatever the list repeats: a manager with a nurse profile is a nurse of the month too. A
    deactivated user is told nothing, as they can read nothing. Payload values are identifiers and numbers only. A notification
    of a kind with a deadline (`notifications.kinds`) is also pushed, by one job for all its readers, deferred in the same
    transaction so it is rolled back with them."""
    seen, rows = set(), []
    for user in users:
        if user is None or not user.is_active or user.pk in seen:
            continue
        seen.add(user.pk)
        rows.append(Notification(user=user, event=event, payload=payload))
    Notification.objects.bulk_create(rows)
    if rows and kind_of(event, payload) is not None:
        from notifications.tasks import push  # the job reads the notifications' pages, which read this module

        queue.enqueue(push, notification_ids=[str(row.pk) for row in rows])
    return len(rows)


def _about(unit: Unit, month: dt.date) -> dict:
    return {"unit_id": str(unit.pk), "month": month.strftime("%Y-%m")}


def roster_submitted(unit: Unit, month: dt.date, by: User) -> None:
    """To the CNO and the Deputy CNO, who decide."""
    notify(User.objects.filter(role__in=(Role.CNO, Role.DEPUTY_CNO)), Event.ROSTER_SUBMITTED, **_about(unit, month),
           by=str(by.pk))


def roster_approved(unit: Unit, month: dt.date, by: User) -> None:
    """To the unit's manager and every nurse of the unit's month, a nurse due to join included (`membership.placed_in`). No
    version number: a later one would make it untrue, and the page says which is in force."""
    nurses = User.objects.filter(nurse_profile__in=membership.placed_in(unit, month))
    notify([unit.manager, *nurses], Event.ROSTER_APPROVED, **_about(unit, month), by=str(by.pk))


def roster_changed(unit: Unit, month: dt.date, by: User) -> None:
    """To every nurse of the unit's month, as an approval is, but not to whoever made the change, who knows. Never the reason,
    the cells or whose they are, since a cell taken off can be leave and a colleague must not learn that: each nurse reads their
    own row on My roster. No version number, which a later version would make untrue."""
    nurses = User.objects.filter(nurse_profile__in=membership.placed_in(unit, month)).exclude(pk=by.pk)
    notify(nurses, Event.ROSTER_CHANGED, **_about(unit, month), by=str(by.pk))


def roster_rejected(unit: Unit, month: dt.date, by: User) -> None:
    """To the unit's manager only. The comment stays on the roster: it can name a nurse and their leave."""
    notify([unit.manager], Event.ROSTER_REJECTED, **_about(unit, month), by=str(by.pk))


def solve_ended(unit_id: UUID, month: dt.date, solve_id: UUID, outcome: SolveOutcome, *, replan: bool = False) -> None:
    """To whoever manages the unit when the solve ends, read again then: a manager taken off it since starting the solve no
    longer reads its pages, and the one who holds it now has the draft. Nobody when the unit has none. A re-plan says so, so
    that its failure is told as a re-plan's."""
    unit = Unit.objects.select_related("manager").get(pk=unit_id)
    notify([unit.manager], Event.SOLVE_ENDED, **_about(unit, month), solve_id=str(solve_id), outcome=outcome,
           **({"replan": True} if replan else {}))


def _window_nurses(window: "RequestWindow") -> QuerySet[User]:
    """The staff nurses of the unit's target month, a nurse due to join included, as an approval's are. A manager with a nurse
    profile makes no requests."""
    return User.objects.filter(role=Role.STAFF_NURSE, nurse_profile__in=membership.placed_in(window.unit, window.month))


def window_opened(window: "RequestWindow") -> None:
    """When its open date comes, which may be the day it is made. The words read the window's dates when shown."""
    notify(_window_nurses(window), Event.WINDOW_OPENED, **_about(window.unit, window.month), window_id=str(window.pk),
           by=str(window.created_by_id))


def window_closed_early(window: "RequestWindow", by: User) -> None:
    """To the nurses told it opened, who expected it open until its close date."""
    notify(_window_nurses(window), Event.WINDOW_CLOSED_EARLY, **_about(window.unit, window.month), window_id=str(window.pk),
           by=str(by.pk))


def window_closed(window: "RequestWindow") -> None:
    """To the unit's manager, once, when its close date has passed with requests waiting for a decision: one notification a
    window rather than one a request. How many still wait is worked out when it is read."""
    notify([window.unit.manager], Event.WINDOW_CLOSED, **_about(window.unit, window.month), window_id=str(window.pk))


def request_decided(request: "Request", step: str, by: User | None) -> None:
    """To the nurse who made it, and nobody else. Never its date, shift or either reason: the words read them from the request
    when shown, and only she reads them. `by` is None when the month's close ended it (`expired`)."""
    notify([request.nurse.user], Event.REQUEST_DECIDED, **_about(request.window.unit, request.window.month),
           request_id=str(request.pk), step=str(step), **({"by": str(by.pk)} if by is not None else {}))


def probation_ending(unit: Unit, nurse_id: UUID, last_month: dt.date) -> None:
    """To her unit's manager, at the start of her probation's last month, which the payload keeps so that a later extension
    does not rewrite what was said."""
    notify([unit.manager], Event.PROBATION_ENDING, **_about(unit, last_month), nurse_id=str(nurse_id))


class PreceptorRole(TextChoices):
    """Who a preceptor's notification is to, of the nurses it names."""
    NURSE = "nurse"  # The nurse on probation: who her preceptor is from the month.
    PRECEPTOR = "preceptor"  # The nurse who precepts her from the month.
    RELEASED = "released"  # The nurse who precepted her and no longer does from the month.


def preceptor_assigned(unit: Unit, nurse, month: dt.date, *, preceptor, released) -> None:
    """To the nurse on probation and her preceptor from `month`, the first month the choice changes, and to each nurse who
    precepted her in a month from then and no longer does: once each. Profiles, so a manager with a nurse profile is told as
    a preceptor."""
    about = _about(unit, month) | {"nurse_id": str(nurse.pk), "preceptor_id": str(preceptor.pk)}
    notify([nurse.user], Event.PRECEPTOR_ASSIGNED, **about, role=PreceptorRole.NURSE.value)
    notify([preceptor.user], Event.PRECEPTOR_ASSIGNED, **about, role=PreceptorRole.PRECEPTOR.value)
    preceptors_released(unit, nurse, month, released)


def preceptors_released(unit: Unit, nurse, month: dt.date, released) -> None:
    """To each nurse who precepts her no more from `month`: another was chosen, or her probation now ends before it."""
    for gone in released:
        notify([gone.user], Event.PRECEPTOR_ASSIGNED, **_about(unit, month), nurse_id=str(nurse.pk), preceptor_id=str(gone.pk),
               role=PreceptorRole.RELEASED.value)


def preceptor_leaving(unit: Unit, nurse_id: UUID, preceptor_id: UUID, last_month: dt.date) -> None:
    """To the unit's manager, when a preceptor's transfer or end date ends an assignment with her last month in the unit: to
    choose another."""
    notify([unit.manager], Event.PRECEPTOR_LEAVING, **_about(unit, last_month), nurse_id=str(nurse_id),
           preceptor_id=str(preceptor_id))


class MonthDates(TextChoices):
    """Which of a month's own dates changed, as a solve input names them."""
    RAMADAN_DAYS = "ramadan_days"
    PUBLIC_HOLIDAYS = "public_holidays"


def month_dates_changed(months: Iterable[dt.date], what: MonthDates, by: User) -> None:
    """To the manager of every unit with a roster in each month, draft, submitted or approved, once a month: its hours may have
    shifted. Not to whoever made the change, who knows, nor to a manager with no roster in the month. Call it after the audit
    entry, with each month whose dates the write changed and none it left as they were, and never twice for one month in one
    write: a holiday moved across months passes both at once, a Ramadan confirmed calls it once a month it fixes."""
    for month in sorted(set(months)):
        for unit in Unit.objects.filter(rosters__month=month, manager__isnull=False).exclude(manager=by).select_related("manager"):
            notify([unit.manager], Event.MONTH_DATES_CHANGED, **_about(unit, month), what=str(what), by=str(by.pk))


def month_closed(unit: Unit, month: dt.date, close_id: UUID) -> None:
    """To the unit's manager, when the schedule closes its month: how many hard rules the recorded facts broke is read from the
    close when shown, never kept here, since a broken rule can be a nurse's leave. A manager who closes it again sees it there and
    then, so is not told."""
    notify([unit.manager], Event.MONTH_CLOSED, **_about(unit, month), close_id=str(close_id))


class SwapStep(TextChoices):
    """A step a swap took, as its people are told: a payload's `step`, so no migration."""
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    AWAITING = "awaiting"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"
    APPROVED = "approved"


def swap_step(swap: "Swap", step: SwapStep, by: User | None, *, awaiting: bool = False) -> None:
    """Everyone involved, at each step: its colleagues when it is proposed; its proposer at each acceptance; the
    unit's manager when all have accepted, which awaits them. At a rejection, a withdrawal, an expiry and an approval, the
    proposer and every colleague in it, and the manager too for a withdrawal or an expiry of one that was `awaiting` them. Never
    whoever took the step, who knows. The payload holds identifiers and the step only: the words read the swap when shown, and
    never say why one expired, since a cell a version changed can be a colleague's leave. `by` is None for an expiry."""
    colleagues = [row.nurse.user for row in swap.participants.select_related("nurse__user")]
    proposer = swap.proposer.user
    match step:
        case SwapStep.PROPOSED:
            users = colleagues
        case SwapStep.ACCEPTED:
            users = [proposer]
        case SwapStep.AWAITING:
            users = [swap.unit.manager]
        case _:
            users = [proposer, *colleagues, *([swap.unit.manager] if awaiting else [])]
    users = [user for user in users if user is not None and (by is None or user.pk != by.pk)]
    payload = _about(swap.unit, swap.roster.month) | {"swap_id": str(swap.pk), "step": str(step),
                                                       **({"by": str(by.pk)} if by is not None else {})}
    if step == SwapStep.WITHDRAWN:
        users = _instead_of_unread(swap, users, payload)
    notify(users, Event.SWAP_STEP, **payload)


def _instead_of_unread(swap: "Swap", users: list[User], payload: dict) -> list[User]:
    """A withdrawal is told in place of the notice still unread that the swap was proposed to them or awaits them, so a round of
    proposing and withdrawing leaves each person one notice of it, and one nurse's rounds cannot push another's out of the list
    (`notifications.api.SHOWN`). Whoever read that notice is told anew. Those still to be told."""
    unread = Notification.objects.filter(user__in=users, event=Event.SWAP_STEP, read_at__isnull=True,
                                         payload__swap_id=str(swap.pk),
                                         payload__step__in=[SwapStep.PROPOSED, SwapStep.AWAITING])
    told = set()
    for note in unread.select_for_update(of=("self",)):
        note.payload, note.created_at = payload, timezone.now()
        note.save(update_fields=["payload", "created_at"])
        told.add(note.user_id)
    return [user for user in users if user.pk not in told]


class CoverStep(TextChoices):
    """A step a cover took, as its people are told: a payload's `step`, so no migration."""
    OFFERED = "offered"
    DECLINED = "declined"
    ACCEPTED = "accepted"
    TAKEN = "taken"
    WITHDRAWN = "withdrawn"
    FILLED = "filled"
    EXPIRED = "expired"


def cover_step(cover: "Cover", step: CoverStep, by: User | None, *, nurses: Iterable[User] = ()) -> None:
    """The nurses added when it is `offered` (`nurses`); the unit's manager at a decline and at the acceptance; every
    nurse offered it, a nurse who declined included, when it is taken by another, withdrawn, filled or expired. Never whoever
    took the step, who knows. The payload holds identifiers and the step only: the words read the cover when shown, and never
    say why it was short, who else was asked or why it ended. `by` is None for a fill and an expiry, which are nobody's act."""
    match step:
        case CoverStep.OFFERED:
            users = list(nurses)
        case CoverStep.DECLINED | CoverStep.ACCEPTED:
            users = [cover.unit.manager]
        case _:
            users = [offer.nurse.user for offer in cover.offers.select_related("nurse__user")]
    users = [user for user in users if user is not None and (by is None or user.pk != by.pk)]
    notify(users, Event.COVER_STEP, **_about(cover.unit, cover.date.replace(day=1)), cover_id=str(cover.pk), step=str(step),
           **({"by": str(by.pk)} if by is not None else {}))
