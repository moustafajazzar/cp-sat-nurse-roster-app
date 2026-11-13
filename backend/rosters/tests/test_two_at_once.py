"""A decision racing another write, on the real PostgreSQL, as `nurses/tests/test_two_at_once.py` does it: one transaction stays
open with its change made, the other is seen waiting for a lock before the first commits, so the order is forced, not hoped for."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import transaction
from django.test import override_settings

from accounts.models import Role, User
from conftest import post
from hospital.models import LeaveType
from nurses.models import NurseProfile
from nurses.tests.test_two_at_once import alongside
from rosters.drafts import in_current_codes
from rosters.models import RosterState, RosterVersion, Submission
from rosters.tests.test_pins import change_shift, off, pin, save
from rosters.tests.test_replan import mid_june  # noqa: F401
from rosters.tests.test_review import (BADR_OFF, BADR_WORKS, approve, approved, carla, dina, record_leave,  # noqa: F401
                                       reject, roster, solved, submit, submitted)
from rosters.tests.test_versions import change
from solves.tests.conftest import empty_queue, start_solve  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _scene(before_june, solver, empty_queue):
    pass


@pytest.fixture
def pool():
    with ThreadPoolExecutor(max_workers=1) as executor:
        yield executor


def test_of_two_approvers_the_second_waits_and_is_told_the_first_decided(mona, carla, dina, er, solver, pool):
    submitted(mona, er, solver)

    with transaction.atomic():
        assert approve(carla, er).status_code == 200
        second = alongside(pool, lambda: approve(dina, er))
        assert not second.done()

    answer = second.result()
    assert (answer.status_code, answer.json()["detail"]) == (409, "This roster has been approved already by Carla CNO.")
    assert RosterVersion.objects.count() == 1


def test_an_approval_waiting_on_a_rejection_finds_nothing_to_approve(mona, carla, dina, er, solver, pool):
    submitted(mona, er, solver)

    with transaction.atomic():
        assert reject(dina, er).status_code == 200
        second = alongside(pool, lambda: approve(carla, er))
        assert not second.done()

    assert second.result().json()["detail"] == "This month has no roster waiting for a decision."
    assert roster(er).state == RosterState.DRAFT and not RosterVersion.objects.exists()


def test_an_approval_waits_for_leave_being_recorded_and_then_checks_it(mona, carla, er, solver, pool):
    """Without the nurses' lock it would check the grid before the leave committed, and approve a roster that breaks a rule."""
    submitted(mona, er, solver)

    with transaction.atomic():
        assert record_leave(mona, er, "Badr", BADR_WORKS).status_code == 201
        approval = alongside(pool, lambda: approve(carla, er))
        assert not approval.done()

    answer = approval.result()
    assert answer.status_code == 409 and "breaks 1 hard rule" in answer.json()["detail"]
    assert roster(er).state == RosterState.SUBMITTED


def test_leave_waits_for_an_approval_and_is_then_recorded(mona, carla, er, solver, pool):
    submitted(mona, er, solver)

    with transaction.atomic():
        assert approve(carla, er).status_code == 200
        leave = alongside(pool, lambda: record_leave(mona, er, "Badr", BADR_OFF))
        assert not leave.done()

    assert leave.result().status_code == 201
    assert roster(er).state == RosterState.APPROVED


def test_a_submit_waiting_on_a_solve_starting_is_refused(mona, er, solver, pool):
    solved(mona, er, solver)

    with transaction.atomic():
        assert start_solve(mona, er).status_code == 201
        second = alongside(pool, lambda: submit(mona, er))
        assert not second.done()

    assert second.result().json()["detail"].startswith("A solve of this month is queued or running")
    assert not Submission.objects.exists()


def test_of_two_changes_to_an_approved_roster_the_second_waits_and_is_the_next_version(mona, carla, er, solver, pool):
    approved(mona, carla, er, solver)

    with transaction.atomic():
        assert change(mona, er, pin("Amal", 9, "N")).status_code == 200
        second = alongside(pool, lambda: change(mona, er, off("Badr", 2), reason="Badr asked."))
        assert not second.done()

    assert second.result().status_code == 200
    versions = list(roster(er).versions.order_by("number").values_list("number", "reason"))
    assert [number for number, _ in versions] == [1, 2, 3] and versions[2][1] == "Badr asked."
    latest = roster(er).versions.first().grid
    assert latest["Amal"]["2026-06-09"] == "N" and latest["Badr"]["2026-06-02"] == "", "the second built on the first"
    assert roster(er).grid == latest


def test_leave_taking_a_shift_off_waits_for_a_change_to_the_same_nurse_and_comes_after_it(mona, carla, er, solver, pool):
    """The change holds the nurse's lock, which a leave write takes first; the leave then takes the roster's after it."""
    approved(mona, carla, er, solver)

    with transaction.atomic():
        assert change(mona, er, pin("Badr", BADR_OFF, "D")).status_code == 200
        leave = alongside(pool, lambda: record_leave(mona, er, "Badr", BADR_WORKS))
        assert not leave.done()

    answer = leave.result()
    assert answer.status_code == 201, answer.content
    assert [row["version"] for row in answer.json()["shifts_taken_off"]] == [3]
    latest = roster(er).versions.first()
    assert latest.number == 3 and latest.grid["Badr"][f"2026-06-{BADR_OFF:02d}"] == "D"
    assert latest.grid["Badr"][f"2026-06-{BADR_WORKS:02d}"] == ""
    assert roster(er).grid == latest.grid


def test_a_change_waits_for_leave_on_another_nurse_and_keeps_the_shift_it_took_off(mona, carla, er, solver, pool):
    """Different nurses' locks do not meet, so the roster's row lock is what orders them."""
    approved(mona, carla, er, solver)

    with transaction.atomic():
        assert record_leave(mona, er, "Badr", BADR_WORKS).status_code == 201
        edit = alongside(pool, lambda: change(mona, er, pin("Amal", 9, "N")))
        assert not edit.done()

    assert edit.result().status_code == 200
    latest = roster(er).versions.first()
    assert latest.number == 3
    assert latest.grid["Badr"][f"2026-06-{BADR_WORKS:02d}"] == "" and latest.grid["Amal"]["2026-06-09"] == "N"


def test_a_rename_waits_for_leave_taking_shifts_off_two_approved_months_and_then_follows_both(mona, carla, er, solver, pool):
    """Leave over the end of a month locks June's roster, then July's. A rename locks every roster by month, then unit, so it
    waits on June and never holds July while waiting on June. Dana works 30 June and 1 July in the sound rosters."""
    approved(mona, carla, er, solver)
    approved(mona, carla, er, solver, month="2026-07")

    with transaction.atomic():
        answer = post(mona, f"/api/units/{er.pk}/leave", {
            "nurse_id": str(NurseProfile.objects.get(user__username="Dana").pk),
            "leave_type_id": str(LeaveType.objects.get(name="Sick").pk), "first": "2026-06-30", "last": "2026-07-01"})
        assert [row["date"] for row in answer.json()["shifts_taken_off"]] == ["2026-06-30", "2026-07-01"]
        rename = alongside(pool, lambda: change_shift(carla, "N", code="NT"))
        assert not rename.done()

    assert rename.result().status_code == 200
    for month in ("2026-06", "2026-07"):
        held = roster(er, month).grid
        assert "N" not in {code for row in held.values() for code in row.values()}
        assert roster(er, month).grid == in_current_codes(roster(er, month).versions.first().grid)


class TestAReplansEnd:
    """A re-plan's end takes the catalogue's lock, the month's nurses' (`FOR NO KEY UPDATE`), the unit's and the roster's, as an
    approval does, so a leave or an edit either comes before it, and is then what makes its roster out of date, or after it, on
    top of the version it made."""

    @pytest.fixture
    def under_way(self, mona, carla, er, solver, mid_june):
        from rosters.tests.test_replan import SWAPPED, answered, started
        approved(mona, carla, er, solver)
        mid_june()
        solver.script([answered(SWAPPED)])
        return started(mona, er)["id"]

    def test_it_waits_for_leave_on_a_day_off_and_then_leaves_its_roster_out(self, mona, er, under_way, pool):
        """Amal is off on the 15th in the roster in force and on Day there in the re-plan's. Without the nurses' lock the end
        would read the month before the leave committed and put her to work on her leave day."""
        from solves import running
        from solves.models import ReplanOutcome, Solve
        with transaction.atomic():
            assert record_leave(mona, er, "Amal", 15).status_code == 201
            end = alongside(pool, lambda: running.run(under_way))
            assert not end.done()

        end.result()
        assert Solve.objects.get(pk=under_way).replan_outcome == ReplanOutcome.OUTDATED
        assert roster(er).versions.first().number == 1 and roster(er).grid["Amal"]["2026-06-15"] == ""

    def test_leave_waits_for_it_and_then_takes_its_shift_off_as_the_next_version(self, mona, er, under_way, pool):
        from solves import running
        with transaction.atomic():
            running.run(under_way)
            leave = alongside(pool, lambda: record_leave(mona, er, "Amal", 15))
            assert not leave.done()

        assert leave.result().status_code == 201
        numbers = list(roster(er).versions.order_by("number").values_list("number", "origin"))
        assert numbers == [(1, "solve"), (2, "replan"), (3, "edit")]
        assert roster(er).grid["Amal"]["2026-06-15"] == "" and roster(er).grid["Emad"]["2026-06-15"] == ""
        assert roster(er).grid == roster(er).versions.first().grid

    def test_an_edit_waits_for_it_and_builds_on_its_version(self, mona, er, under_way, pool):
        from solves import running
        with transaction.atomic():
            running.run(under_way)
            edit = alongside(pool, lambda: change(mona, er, pin("Badr", 20, "D"), reason="Badr asked."))
            assert not edit.done()

        assert edit.result().status_code == 200
        latest = roster(er).versions.first()
        assert latest.number == 3 and latest.grid["Amal"]["2026-06-15"] == "D" and latest.grid["Badr"]["2026-06-20"] == "D"


class TestDeactivatingAManager:
    """Deactivating locks the manager's user row, then their unit's. A save holding the unit names that user in a foreign key
    (a pin's or a version's), which PostgreSQL checks at commit with `FOR KEY SHARE` on the row: under `FOR UPDATE` each would
    wait on the other and one be killed as a deadlock. `FOR NO KEY UPDATE` lets the check through, so the save commits and the
    deactivation then takes the unit off its manager.

    The save is made without saving her session: a request saves it after its own transaction has committed, but here both are in
    the test's, and the deactivation, which ends her sessions, would wait on that row before reaching the unit."""

    @pytest.fixture
    def system_admin(self, client_of):
        return client_of("root", Role.SYSTEM_ADMIN)

    def deactivating(self, pool, system_admin):
        mona = User.objects.get(username="mona")
        return alongside(pool, lambda: system_admin.post(f"/api/users/{mona.pk}/deactivate"))

    def test_it_waits_for_a_pin_saved_on_the_draft_and_then_takes_the_unit_off_her(self, mona, er, solver, system_admin,
                                                                                    pool):
        solved(mona, er, solver)

        with transaction.atomic(), override_settings(SESSION_SAVE_EVERY_REQUEST=False):
            assert save(mona, er, pin("Amal", 9, "N")).status_code == 200
            deactivation = self.deactivating(pool, system_admin)
            assert not deactivation.done()

        assert deactivation.result().status_code == 200
        er.refresh_from_db()
        assert er.manager is None and not User.objects.get(username="mona").is_active
        assert er.pins.filter(removed_at=None).count() == 1

    def test_it_waits_for_an_edit_of_the_approved_roster_and_then_takes_the_unit_off_her(self, mona, carla, er, solver,
                                                                                         system_admin, pool):
        approved(mona, carla, er, solver)

        with transaction.atomic(), override_settings(SESSION_SAVE_EVERY_REQUEST=False):
            assert change(mona, er, pin("Amal", 9, "N")).status_code == 200
            deactivation = self.deactivating(pool, system_admin)
            assert not deactivation.done()

        assert deactivation.result().status_code == 200
        er.refresh_from_db()
        assert er.manager is None and roster(er).versions.first().number == 2
