"""Notifications are kept for a year, read or not: nothing else removes them. Push attempts are kept seven days,
which the health page's 24 hours never reach."""
import datetime as dt

import httpx
import pytest
from django.utils import timezone

from accounts.models import Role
from notifications.models import Event, Notification, PushAttempt
from notifications.tasks import remove_old_notifications, remove_old_push_attempts

pytestmark = pytest.mark.django_db


def test_those_older_than_a_year_go_read_or_not(make_user):
    user = make_user("mona", Role.UNIT_MANAGER)
    now = timezone.now()

    def made(days_ago, read=False):
        notification = Notification.objects.create(user=user, event=Event.ROSTER_CHANGED, payload={})
        Notification.objects.filter(pk=notification.pk).update(created_at=now - dt.timedelta(days=days_ago),
                                                               read_at=now if read else None)
        return notification.pk

    gone = {made(366), made(400, read=True)}
    kept = {made(364), made(1, read=True)}

    remove_old_notifications(timestamp=int(now.timestamp()))

    assert set(Notification.objects.values_list("pk", flat=True)) == kept
    assert not Notification.objects.filter(pk__in=gone).exists()


def test_the_job_runs_daily_under_a_lock_of_its_own():
    assert (remove_old_notifications.lock, remove_old_notifications.queueing_lock) == ("remove_old_notifications",) * 2


def attempted(outcome, **ago):
    attempt = PushAttempt.objects.create(outcome=outcome)
    PushAttempt.objects.filter(pk=attempt.pk).update(at=timezone.now() - dt.timedelta(**ago))
    return attempt.pk


def test_push_attempts_older_than_seven_days_go_whatever_came_of_them():
    gone = {attempted("sent", days=7, minutes=1), attempted("unreachable", days=8), attempted("gone", days=30)}
    kept = {attempted("sent", days=6, hours=23), attempted("refused", days=3), attempted("unreachable", hours=1)}

    remove_old_push_attempts(timestamp=int(timezone.now().timestamp()))

    assert set(PushAttempt.objects.values_list("pk", flat=True)) == kept
    assert not PushAttempt.objects.filter(pk__in=gone).exists()


def test_the_health_page_counts_the_same_before_and_after(signed_in, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, headers, timeout: httpx.Response(
        200, json={"version": "0.1.0", "workers": 8}, request=httpx.Request("GET", url)))
    attempted("sent", hours=1), attempted("refused", hours=23), attempted("gone", days=2), attempted("sent", days=9)
    admin = signed_in()
    before = admin.get("/api/ops/health").json()["push"]

    remove_old_push_attempts(timestamp=int(timezone.now().timestamp()))

    assert before == admin.get("/api/ops/health").json()["push"] == {"sent": 1, "failed": 1, "last_failure": "refused"}
    assert PushAttempt.objects.count() == 3


def test_the_push_attempts_job_runs_daily_under_a_lock_of_its_own():
    assert (remove_old_push_attempts.lock, remove_old_push_attempts.queueing_lock) == ("remove_old_push_attempts",) * 2
