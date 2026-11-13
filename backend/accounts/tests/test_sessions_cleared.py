"""Expired sessions are cleared daily, as Django's `clearsessions` would: nothing else removes them."""
import datetime as dt

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.utils import timezone

from accounts.tasks import clear_expired_sessions

pytestmark = pytest.mark.django_db


def a_session(expires_in: dt.timedelta) -> str:
    store = SessionStore()
    store["anything"] = 1
    store.create()
    Session.objects.filter(pk=store.session_key).update(expire_date=timezone.now() + expires_in)
    return store.session_key


def test_only_the_expired_go():
    gone = a_session(-dt.timedelta(seconds=1))
    kept = a_session(dt.timedelta(hours=1))

    clear_expired_sessions(timestamp=0)

    assert set(Session.objects.values_list("pk", flat=True)) == {kept}
    assert gone != kept


def test_the_job_runs_daily_under_a_lock_of_its_own():
    assert (clear_expired_sessions.lock, clear_expired_sessions.queueing_lock) == ("clear_expired_sessions",) * 2
