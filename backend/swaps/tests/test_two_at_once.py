"""Swaps racing each other, leave and edits, on the real PostgreSQL, as `nurses/tests/test_two_at_once.py` does it: one
transaction stays open with its change made, the other is seen waiting for a lock before the first commits, so the order is
forced, not hoped for."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from accounts.models import Role
from conftest import post
from notifications.models import Event, Notification
from notifications.tests.test_events import as_user
from nurses.models import NurseProfile
from nurses.tests.test_two_at_once import alongside
from rosters.models import RosterVersion
from rosters.tests.conftest import locks_on
from rosters.tests.test_pins import icu, pin
from rosters.tests.test_versions import change
from solves.tests.conftest import empty_queue  # noqa: F401
from swaps.models import Swap, SwapState
from swaps.tests.conftest import SOUND_SWAP, act, acted, cell, decide, propose, proposed
from swaps.tests.test_swaps import TWO_COLLEAGUES, awaiting, leave_for

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _scene(before_june, solver, empty_queue):
    pass


@pytest.fixture
def pool():
    with ThreadPoolExecutor(max_workers=1) as executor:
        yield executor


def awaiting_told() -> int:
    return Notification.objects.filter(event=Event.SWAP_STEP, payload__step="awaiting").count()


def test_of_the_last_two_acceptances_the_second_waits_and_sends_it_to_the_manager_once(in_force, pool):
    swap = proposed(as_user("Badr"), *TWO_COLLEAGUES)
    acted(as_user("Amal"), swap["id"], "accept")
    emad, dana = as_user("Emad"), as_user("Dana")

    with transaction.atomic():
        assert act(emad, swap["id"], "accept").status_code == 200
        second = alongside(pool, lambda: act(dana, swap["id"], "accept"))
        assert not second.done()

    assert second.result().status_code == 200
    assert Swap.objects.get().state == SwapState.AWAITING_MANAGER
    assert awaiting_told() == 1


def test_an_acceptance_waits_for_a_withdrawal_and_finds_it_withdrawn(in_force, pool):
    swap = proposed(as_user("Badr"), *SOUND_SWAP)
    badr, emad = as_user("Badr"), as_user("Emad")

    with transaction.atomic():
        assert act(badr, swap["id"], "withdraw").status_code == 200
        second = alongside(pool, lambda: act(emad, swap["id"], "accept"))
        assert not second.done()

    assert second.result().json()["detail"] == "This swap is withdrawn, so it can no longer be answered."


def test_an_approval_waits_for_leave_on_one_of_its_cells_and_is_refused(in_force, mona, pool):
    swap = awaiting(as_user)
    other_tab = as_user("mona")

    with transaction.atomic():
        assert leave_for(mona, in_force, "Emad", 9).status_code == 201
        approval = alongside(pool, lambda: decide(other_tab, in_force, swap["id"], "approve"))
        assert not approval.done()

    answer = approval.result()
    assert (answer.status_code, answer.json()["detail"]) == (409, "Emad is on leave on 9 June 2026, and leave wins over a pin.")
    assert RosterVersion.objects.count() == 1


def test_an_approval_waits_for_an_edit_of_one_of_its_cells_and_finds_it_expired(in_force, mona, pool):
    swap = awaiting(as_user)
    other_tab = as_user("mona")

    with transaction.atomic():
        assert change(mona, in_force, pin("Badr", 9, "N")).status_code == 200
        approval = alongside(pool, lambda: decide(other_tab, in_force, swap["id"], "approve"))
        assert not approval.done()

    assert approval.result().json()["detail"] == "This swap is expired, so it cannot be decided."
    assert RosterVersion.objects.count() == 2


def test_a_proposal_waits_for_an_edit_of_one_of_its_cells_and_is_judged_on_the_new_version(in_force, mona, pool):
    badr = as_user("Badr")

    with transaction.atomic():
        assert change(mona, in_force, pin("Emad", 9, "D")).status_code == 200
        proposal = alongside(pool, lambda: propose(badr, cell("Badr", 9), cell("Emad", 9, "D")))
        assert not proposal.done()

    answer = proposal.result()
    assert answer.status_code == 422
    assert [row["msg"] for row in answer.json()["detail"]] == ["Emad's cell on 9 June 2026 is already Day (D)."]


def test_a_proposal_waits_for_a_transfer_of_its_proposer_and_is_judged_in_the_unit_she_is_now_in(in_force, carla, make_user,
                                                                                                    pool):
    """The transfer moves her while the proposal waits for its locks, and her unit is read once hers is held."""
    other = icu(make_user("omar", Role.UNIT_MANAGER))
    badr = as_user("Badr")

    with transaction.atomic():
        moved = post(carla, f"/api/nurses/{NurseProfile.objects.get(user__username='Badr').pk}/transfer",
                     {"unit_id": str(other.pk), "from_month": "2026-06"})
        assert moved.status_code == 200, moved.content
        proposal = alongside(pool, lambda: propose(badr, *SOUND_SWAP))
        assert not proposal.done()

    answer = proposal.result()
    assert answer.status_code == 409
    assert answer.json()["detail"] == "The ICU roster for June 2026 is not approved yet, so there is nothing to swap."
    assert not Swap.objects.exists()


def test_a_proposal_that_meets_a_transfer_of_its_proposer_takes_each_lock_once_in_the_one_order(in_force, carla, make_user,
                                                                                                    pool):
    """The catalogue's, the nurses', then the unit she is in once hers is held, then its roster: never one unit's lock after
    another's roster."""
    other = icu(make_user("omar", Role.UNIT_MANAGER))
    badr = as_user("Badr")

    def proposing():
        with CaptureQueriesContext(connection) as queries:
            propose(badr, *SOUND_SWAP)
        return queries.captured_queries  # Read here: `connection` is this thread's.

    with transaction.atomic():
        moved = post(carla, f"/api/nurses/{NurseProfile.objects.get(user__username='Badr').pk}/transfer",
                     {"unit_id": str(other.pk), "from_month": "2026-06"})
        assert moved.status_code == 200, moved.content
        proposal = alongside(pool, proposing)
        assert not proposal.done()

    tables = ("hospital_hospitalsettings", "nurses_nurseprofile", "units_unit", "rosters_roster")
    locks = locks_on(proposal.result(), *tables)
    assert [table for table, _ in locks] == list(tables)
    assert str(other.pk).replace("-", "") in locks[2][1]
