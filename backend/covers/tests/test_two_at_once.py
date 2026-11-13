"""Covers and other writes at once, on real connections: a cover refers to its unit, whose foreign key is checked at commit, so
opening one takes the unit's lock before the roster's, as an edit does. A rename holds every unit while it waits on the
rosters; it waits for the opening instead of deadlocking with it. An acceptance takes the locks an approval takes, so the last
two acceptances, an acceptance and leave on the taker's day, and an acceptance and a withdrawal each wait for the other and the
second finds what the first did. Leave's own two-at-once tests are in `rosters/tests`."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import transaction

from covers.models import Cover
from covers.tests.conftest import answer, badrs_day, offered, opened, record_leave, the_25th, withdraw
from notifications.tests.test_events import as_user
from rosters.models import RosterVersion
from nurses.tests.test_two_at_once import alongside
from rosters.tests.test_pins import change_shift
from solves.tests.conftest import empty_queue  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _scene(before_june, solver, empty_queue):
    pass


@pytest.fixture
def pool():
    with ThreadPoolExecutor(max_workers=1) as executor:
        yield executor


def test_a_rename_waits_for_a_cover_opened_by_hand_and_then_renames_its_shift(in_force, mona, carla, pool):
    with transaction.atomic():
        opened(mona, in_force, 9, "N")
        rename = alongside(pool, lambda: change_shift(carla, "N", code="NT"))
        assert not rename.done()

    assert rename.result().status_code == 200
    assert Cover.objects.get().shift.code == "NT"


def test_of_the_last_two_acceptances_the_second_waits_and_is_refused_as_taken(in_force, mona, pool):
    cover_id = the_25th(mona, in_force)
    offered(mona, in_force, cover_id, "Emad", "Dana")
    emad, dana = as_user("Emad"), as_user("Dana")

    with transaction.atomic():
        assert answer(emad, cover_id, "accept").status_code == 200
        second = alongside(pool, lambda: answer(dana, cover_id, "accept"))
        assert not second.done()

    assert second.result().json()["detail"] == "Another nurse has taken this cover, so you can no longer take it."
    assert RosterVersion.objects.count() == 2


def test_an_acceptance_waits_for_leave_on_her_day_and_is_refused_in_her_words(in_force, mona, pool):
    cover_id = badrs_day(mona, in_force)
    offered(mona, in_force, cover_id, "Emad")
    emad = as_user("Emad")

    with transaction.atomic():
        assert record_leave(mona, in_force, "Emad", 9).status_code == 201
        acceptance = alongside(pool, lambda: answer(emad, cover_id, "accept"))
        assert not acceptance.done()

    reply = acceptance.result()
    assert (reply.status_code, reply.json()["detail"]) == (409, "You are on leave on 9 June 2026, so you cannot take this cover.")
    assert Cover.objects.get().state == "offered"


def test_an_acceptance_waits_for_a_withdrawal_and_finds_it_withdrawn(in_force, mona, pool):
    cover_id = badrs_day(mona, in_force)
    offered(mona, in_force, cover_id, "Emad")
    emad, other_tab = as_user("Emad"), as_user("mona")

    with transaction.atomic():
        assert withdraw(other_tab, in_force, cover_id).status_code == 200
        acceptance = alongside(pool, lambda: answer(emad, cover_id, "accept"))
        assert not acceptance.done()

    assert acceptance.result().json()["detail"] == "Your manager has withdrawn this cover, so you can no longer take it."


def test_a_withdrawal_waits_for_an_acceptance_and_finds_it_taken(in_force, mona, pool):
    cover_id = badrs_day(mona, in_force)
    offered(mona, in_force, cover_id, "Emad")
    emad, other_tab = as_user("Emad"), as_user("mona")

    with transaction.atomic():
        assert answer(emad, cover_id, "accept").status_code == 200
        withdrawal = alongside(pool, lambda: withdraw(other_tab, in_force, cover_id))
        assert not withdrawal.done()

    assert withdrawal.result().status_code == 409
    assert Cover.objects.get().state == "taken"
