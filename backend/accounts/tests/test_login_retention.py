"""The failed-login counts that are no one's record go: a name's count a
day after its last failure unless the name is locked now, and an address's throttle window once it has ended. Driven through
the login, as a client makes the counts, with the server's clock moved on, and the daily job run as the worker runs it."""
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from accounts.models import AddressFailures, LoginAttempts
from accounts.tasks import remove_old_login_failures
from audit.models import AuditEntry
from conftest import PASSWORD

pytestmark = pytest.mark.django_db


def login(username, password="wrong-password", address="10.1.2.3"):
    return Client().post("/api/auth/login", {"username": username, "password": password},
                         content_type="application/json", REMOTE_ADDR=address)


def fail(times, username, address="10.1.2.3"):
    response = None
    for _ in range(times):
        response = login(username, address=address)
    return response


@pytest.fixture
def clock(monkeypatch):
    """Moves the server's clock on by the given time, from where it stands."""
    def move(by: timedelta):
        moved_to = timezone.now() + by
        monkeypatch.setattr(timezone, "now", lambda: moved_to)
    return move


def run_the_job():
    remove_old_login_failures(timestamp=int(timezone.now().timestamp()))


def counted_names() -> set[str]:
    return set(LoginAttempts.objects.values_list("username", flat=True))


class TestTheNamesCounts:
    def test_an_unlocked_count_a_day_old_goes_and_a_fresh_one_stays(self, clock):
        fail(2, "old-name")
        clock(timedelta(hours=23, minutes=59))
        fail(2, "fresh-name")
        clock(timedelta(minutes=2))  # old-name's last failure is now a day and a minute old, fresh-name's two minutes

        run_the_job()

        assert counted_names() == {"fresh-name"}

    def test_a_count_exactly_a_day_old_stays_until_it_is_more(self, clock):
        clock(timedelta(0))  # held, so the day is exact
        fail(1, "someone")
        clock(timedelta(hours=24))

        run_the_job()

        assert counted_names() == {"someone"}

    def test_the_day_counts_from_the_last_failure_not_the_first(self, clock):
        fail(1, "someone")
        clock(timedelta(hours=20))
        fail(1, "someone")
        clock(timedelta(hours=20))

        run_the_job()

        assert counted_names() == {"someone"}

    def test_a_name_locked_now_stays_however_old_its_last_failure(self, clock):
        fail(5, "locked-name")
        LoginAttempts.objects.update(locked_until=timezone.now() + timedelta(days=2))  # a lock longer than a day
        clock(timedelta(days=1, minutes=1))

        run_the_job()

        assert counted_names() == {"locked-name"}
        assert login("locked-name", PASSWORD).status_code == 423

    def test_a_lock_that_has_run_out_goes_with_its_day(self, clock):
        fail(5, "someone")
        clock(timedelta(days=1, minutes=1))

        run_the_job()

        assert counted_names() == set()

    def test_a_row_written_before_the_time_was_kept_goes(self):
        LoginAttempts.objects.create(username="from-before", failures=3)

        run_the_job()

        assert counted_names() == set()

    def test_a_name_whose_count_went_starts_again_from_none(self, make_user, clock):
        make_user()
        fail(4, "someone")
        clock(timedelta(days=1, minutes=1))
        run_the_job()

        assert fail(4, "someone").status_code == 401
        assert login("someone").status_code == 423


class TestTheAddresses:
    def test_an_ended_window_goes_and_the_current_one_stays(self, clock):
        fail(3, "a", address="10.0.0.1")
        clock(timedelta(minutes=10))
        fail(3, "b", address="10.0.0.2")
        clock(timedelta(minutes=5))  # 10.0.0.1's window started 15 minutes ago, 10.0.0.2's 5

        run_the_job()

        assert set(AddressFailures.objects.values_list("address", flat=True)) == {"10.0.0.2"}


def test_no_audit_entry_goes(clock):
    fail(5, "someone")
    fail(50, "guess", address="10.0.0.9")
    entries = AuditEntry.objects.count()
    clock(timedelta(days=2))

    run_the_job()

    assert counted_names() == set() and not AddressFailures.objects.exists()
    assert AuditEntry.objects.count() == entries


def test_the_job_runs_daily_under_a_lock_of_its_own():
    assert (remove_old_login_failures.lock, remove_old_login_failures.queueing_lock) == ("remove_old_login_failures",) * 2
