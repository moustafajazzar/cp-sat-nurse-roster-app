import datetime as dt

from django.utils import timezone

from notifications import pushes
from notifications.models import Notification, PushAttempt
from ops.queue import daily, enqueue, enqueue_later, under_its_own_lock

KEPT = dt.timedelta(days=365)


@daily(at_hour_utc=22, at_minute=20)
def remove_old_notifications(timestamp: int) -> None:
    """A notification is kept for a year, read or not. A day missed is caught up by the next."""
    Notification.objects.filter(created_at__lt=timezone.now() - KEPT).delete()


ATTEMPTS_KEPT = dt.timedelta(days=7)
"""Long enough to look back over a week of pushes that failed; the health page reads the last 24 hours only."""


@daily(at_hour_utc=22, at_minute=30)
def remove_old_push_attempts(timestamp: int) -> None:
    """A push attempt is no one's record: kept seven days, whatever came of it. A day missed is caught up by the
    next."""
    PushAttempt.objects.filter(at__lt=timezone.now() - ATTEMPTS_KEPT).delete()


TRY_AGAIN_PRIORITY = -1
"""A try again waits minutes for its time, holding the lock `push` meanwhile against any job of its priority deferred after it.
Below the first tries' priority, it holds back no new push; a new push waiting holds it back only until that runs."""


@under_its_own_lock
def push(notification_ids: list[str], attempt: int = 0, remaining: list[list[str]] | None = None) -> None:
    """Deferred by `notifications.sending.notify` in the transaction that writes the notifications, so it exists only if they
    do; one job a call, however many it tells. Under the lock `push`, one push job at a time. The sends
    its budget left unmade go to a new job at the normal priority and the same try, so first tries stay first tries, behind any
    push already waiting."""
    delivered = pushes.deliver(notification_ids, started=pushes.clock(), attempt=attempt, remaining=remaining)
    if delivered.handed_on:
        enqueue(push, notification_ids=notification_ids, attempt=attempt, remaining=delivered.handed_on)
    if delivered.retry:
        enqueue_later(push, seconds=pushes.RETRY_IN[attempt], priority=TRY_AGAIN_PRIORITY,
                      notification_ids=notification_ids, attempt=attempt + 1, remaining=delivered.retry)
