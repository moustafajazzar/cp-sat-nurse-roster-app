"""Requests racing each other, leave and the hourly job, on the real PostgreSQL, as `nurses/tests/test_two_at_once.py` does it:
one transaction stays open with its change made, the other is seen waiting for a lock before the first commits, so the order is
forced, not hoped for."""
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from conftest import post
from notifications.models import Notification
from notifications.tests.test_events import as_user
from nurses import membership
from nurses.tests.test_two_at_once import alongside
from requesting.models import Preference, Request, RequestState
from requesting.tasks import open_and_close_request_windows
from requesting.tests.conftest import ask, asked, decide, opened
from requesting.tests.test_preferences import prefer, preferred
from requesting.tests.test_requests import leave_for
from rosters.tests.conftest import locks_on
from rosters.tests.test_pins import solved
from units.models import Unit
from solves.tests.conftest import empty_queue  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _scene(before_june, solver, empty_queue):
    pass


@pytest.fixture
def pool():
    with ThreadPoolExecutor(max_workers=1) as executor:
        yield executor


def test_two_requests_at_the_cap_cannot_both_pass_it(mona, er, pool):
    opened(mona, er)
    amal, again = as_user("Amal"), as_user("Amal")
    asked(amal, 3)
    asked(amal, 4)

    with transaction.atomic():
        assert ask(amal, 5).status_code == 201
        second = alongside(pool, lambda: ask(again, 6))
        assert not second.done()

    answer = second.result()
    assert (answer.status_code, answer.json()["detail"]) == (
        409, "You have 3 requests for June 2026, the most this unit allows. Withdraw a pending one to make another.")
    assert Request.objects.count() == 3


def test_two_preferences_at_the_cap_cannot_both_pass_it(mona, er, pool):
    Unit.objects.filter(pk=er.pk).update(preference_cap=2)
    opened(mona, er)
    amal, again = as_user("Amal"), as_user("Amal")
    preferred(amal, 3)

    with transaction.atomic():
        assert prefer(amal, 4).status_code == 201
        second = alongside(pool, lambda: prefer(again, 5, "working_day"))
        assert not second.done()

    answer = second.result()
    assert (answer.status_code, answer.json()["detail"]) == (
        409, "You have 2 preferences for June 2026, the most this unit allows. Withdraw one to make another.")
    assert Preference.objects.count() == 2


def test_a_request_and_a_preference_on_one_date_at_once_cannot_both_hold_it(mona, er, pool):
    opened(mona, er)
    amal, again = as_user("Amal"), as_user("Amal")

    with transaction.atomic():
        assert ask(amal, 3).status_code == 201
        second = alongside(pool, lambda: prefer(again, 3))
        assert not second.done()

    assert second.result().json()["detail"][0]["msg"].startswith("You have a request on 3 June 2026")
    assert Preference.objects.count() == 0


def test_of_two_acceptances_the_second_waits_and_finds_it_accepted(mona, er, pool):
    opened(mona, er)
    made = asked(as_user("Amal"), 3)
    other_tab = as_user("mona")

    with transaction.atomic():
        assert decide(mona, er, made["id"], "accept").status_code == 200
        second = alongside(pool, lambda: decide(other_tab, er, made["id"], "accept"))
        assert not second.done()

    assert second.result().json()["detail"] == "This request is accepted already."


def test_leave_waits_for_an_acceptance_and_then_replaces_it(mona, er, pool):
    opened(mona, er)
    made = asked(as_user("Amal"), 3)

    with transaction.atomic():
        assert decide(mona, er, made["id"], "accept").status_code == 200
        leave = alongside(pool, lambda: leave_for(as_user("mona"), er, "Amal", 3))
        assert not leave.done()

    assert leave.result().status_code == 201
    assert Request.objects.get().state == RequestState.REPLACED_BY_LEAVE


def test_the_job_waits_for_a_write_opening_the_window_and_tells_nobody_twice(mona, er, pool, monkeypatch):
    opened(mona, er, opens="2026-05-20")
    amal = as_user("Amal")
    monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 5, 20))

    with transaction.atomic():
        assert ask(amal, 3).status_code == 201
        job = alongside(pool, lambda: open_and_close_request_windows(timestamp=0))
        assert not job.done()

    job.result()
    assert Notification.objects.filter(event="window_opened").count() == 4


def test_a_decision_waiting_on_a_withdrawal_finds_it_withdrawn(mona, er, pool):
    opened(mona, er)
    amal = as_user("Amal")
    made = asked(amal, 3)

    with transaction.atomic():
        assert post(amal, f"/api/requests/mine/{made['id']}/withdraw", {}).status_code == 200
        decision = alongside(pool, lambda: decide(as_user("mona"), er, made["id"], "accept"))
        assert not decision.done()

    assert decision.result().json()["detail"] == "This request is withdrawn already."


def test_a_decision_locks_the_requests_row_after_the_months_roster(mona, er, solver):
    """As every change to a unit's month holds it (`rosters.in_force.hold`): the catalogue, the nurse, the unit and the roster,
    then the request. Every write to a request holds its nurse's lock first, so no two wait on each other through those rows."""
    solved(mona, er, solver)
    opened(mona, er)
    made = asked(as_user("Amal"), 3)

    with CaptureQueriesContext(connection) as queries:
        assert decide(mona, er, made["id"], "accept").status_code == 200

    tables = ("hospital_hospitalsettings", "nurses_nurseprofile", "units_unit", "rosters_roster", "requesting_request")
    assert [table for table, _ in locks_on(queries.captured_queries, *tables)][:5] == list(tables)
