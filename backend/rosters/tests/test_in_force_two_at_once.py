"""The locks of a change to the roster in force (`rosters.in_force.hold`), proven once on the real PostgreSQL as
`nurses/tests/test_two_at_once.py` does it: one transaction stays open with its write made, the other is seen waiting for a lock
before the first commits, so the order is forced, not hoped for."""
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from accounts.models import User
from conftest import post
from hospital.models import LeaveType
from nurses.models import NurseProfile
from nurses.tests.test_two_at_once import alongside
from rosters import in_force
from rosters.approved import Change
from rosters.drafts import in_current_codes
from rosters.models import Roster
from rosters.tests.conftest import approved_roster
from rosters.tests.test_pins import change_shift, pin
from rosters.tests.test_review import BADR_OFF, BADR_WORKS, carla, record_leave  # noqa: F401
from rosters.tests.test_versions import change
from solves.tests.conftest import empty_queue  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)

JUNE = dt.date(2026, 6, 1)


@pytest.fixture(autouse=True)
def _scene(before_june, solver, empty_queue):
    pass


@pytest.fixture
def pool():
    with ThreadPoolExecutor(max_workers=1) as executor:
        yield executor


@pytest.fixture
def june(er) -> Roster:
    return approved_roster(er)


def changed(unit, *cells: Change, why="A change.") -> object:
    with transaction.atomic():
        held = in_force.hold(unit.pk, JUNE, nurses={cell.hospital_id for cell in cells})
        return in_force.change(held, list(cells), origin=in_force.Origin.EDIT, by=User.objects.get(username="mona"), why=why,
                               pins=0)


def held_for(unit, nurses) -> None:
    with transaction.atomic():
        in_force.hold(unit.pk, JUNE, nurses=nurses)


def in_force_now(unit) -> Roster:
    return Roster.objects.get(unit=unit, month=JUNE)


@pytest.mark.parametrize("nurses", [{"Amal"}, in_force.WHOLE_MONTH], ids=["named", "whole month"])
def test_a_hold_locks_the_catalogue_the_nurses_and_their_users_the_unit_then_the_roster(er, june, nurses):
    """The order every write that meets a change takes them in, so none waits on another the other way round. The nurses' and
    their users' `FOR NO KEY UPDATE`, which a leave write takes too."""
    with CaptureQueriesContext(connection) as queries:
        held_for(er, nurses)

    locks = [query["sql"] for query in queries.captured_queries if " FOR " in query["sql"]]
    tables = [next(table for table in ("hospital_hospitalsettings", "nurses_nurseprofile", "units_unit", "rosters_roster")
                   if f'FROM "{table}"' in sql) for sql in locks]
    assert tables == ["hospital_hospitalsettings", "nurses_nurseprofile", "units_unit", "rosters_roster"]
    held = locks[1].split("FOR NO KEY UPDATE OF")[1]
    assert '"nurses_nurseprofile"' in held and '"accounts_user"' in held


def test_of_two_changes_at_once_the_second_waits_and_is_the_next_version_built_on_the_first(er, june, pool):
    """Different nurses' cells, so the roster's row lock is what orders them."""
    with transaction.atomic():
        changed(er, Change("Amal", dt.date(2026, 6, 9), "N"))
        second = alongside(pool, lambda: changed(er, Change("Badr", dt.date(2026, 6, 2), None), why="Badr asked."))
        assert not second.done()

    assert second.result().number == 3
    roster = in_force_now(er)
    assert list(roster.versions.order_by("number").values_list("number", "reason")) == [
        (1, ""), (2, "A change."), (3, "Badr asked.")]
    assert roster.grid["Amal"]["2026-06-09"] == "N" and roster.grid["Badr"]["2026-06-02"] == ""
    assert roster.grid == roster.versions.first().grid


def test_a_change_waits_for_a_shift_rename_and_then_holds_only_the_new_code(er, june, carla, pool):
    with transaction.atomic():
        assert change_shift(carla, "N", code="NT").status_code == 200
        edit = alongside(pool, lambda: changed(er, Change("Amal", dt.date(2026, 6, 9), "N")))
        assert not edit.done()

    assert edit.result().number == 2
    roster = in_force_now(er)
    assert roster.grid["Amal"]["2026-06-09"] == "NT"
    assert "N" not in {code for row in roster.grid.values() for code in row.values()}
    assert roster.grid == in_current_codes(roster.versions.first().grid)


def test_a_shift_rename_waits_for_a_change_and_then_follows_its_version(er, june, carla, pool):
    with transaction.atomic():
        changed(er, Change("Amal", dt.date(2026, 6, 9), "N"))
        rename = alongside(pool, lambda: change_shift(carla, "N", code="NT"))
        assert not rename.done()

    assert rename.result().status_code == 200
    roster = in_force_now(er)
    assert roster.versions.first().number == 2 and roster.grid["Amal"]["2026-06-09"] == "NT"
    assert "N" not in {code for row in roster.grid.values() for code in row.values()}
    assert roster.grid == in_current_codes(roster.versions.first().grid)


def test_a_manager_taken_off_the_unit_while_her_edit_waits_is_refused(mona, er, june, client_of, pool):
    cno = client_of("cleo", "cno")
    with transaction.atomic():
        taken_off = cno.put(f"/api/units/{er.pk}", {"name": "ER", "manager_id": None}, content_type="application/json")
        assert taken_off.status_code == 200
        edit = alongside(pool, lambda: change(mona, er, pin("Amal", 9, "N")))
        assert not edit.done()

    answer = edit.result()
    assert answer.status_code == 403, answer.content
    assert in_force_now(er).versions.count() == 1


def test_a_whole_month_hold_waits_on_any_nurse_of_the_month_and_a_named_one_only_on_its_own(er, june, pool):
    """As a leave write holds a nurse's lock while it is recorded."""
    with transaction.atomic():
        list(NurseProfile.objects.select_for_update(no_key=True).filter(user__username="Badr"))
        named = alongside(pool, lambda: held_for(er, {"Amal"}))
        assert named.done(), "a named hold waited on a nurse it did not name"
        named.result()
        whole = alongside(pool, lambda: held_for(er, in_force.WHOLE_MONTH))
        assert not whole.done()

    whole.result()


def leave_over_the_month_end(client, unit):
    """Dana's leave on 30 June and 1 July, both of which she works in the sound rosters."""
    return post(client, f"/api/units/{unit.pk}/leave", {
        "nurse_id": str(NurseProfile.objects.get(user__username="Dana").pk),
        "leave_type_id": str(LeaveType.objects.get(name="Sick").pk), "first": "2026-06-30", "last": "2026-07-01"})


def test_a_leave_write_takes_no_catalogue_lock_and_holds_the_nurse_the_unit_then_each_month_in_order(mona, er, june):
    """So recording leave never waits on a solve or a rename at the catalogue. The unit's lock is `FOR KEY SHARE`, which a
    change's or a rename's `FOR UPDATE` of the unit waits on, and which waits on theirs."""
    approved_roster(er, "2026-07")

    with CaptureQueriesContext(connection) as queries:
        answer = leave_over_the_month_end(mona, er)
    assert [row["version"] for row in answer.json()["shifts_taken_off"]] == [2, 2]

    locks = [query["sql"] for query in queries.captured_queries if " FOR " in query["sql"]]
    assert not [sql for sql in locks if "hospital_hospitalsettings" in sql]
    ordered = [(table, sql) for sql in locks for table in ("nurses_nurseprofile", "units_unit", "rosters_roster")
               if f'FROM "{table}"' in sql]
    assert [table for table, _ in ordered] == ["nurses_nurseprofile", "units_unit", "rosters_roster", "rosters_roster"]
    assert "FOR KEY SHARE" in ordered[1][1]
    assert "2026-06-01" in ordered[2][1] and "2026-07-01" in ordered[3][1]


def test_leave_waits_for_a_change_to_the_same_nurse_and_comes_after_it(mona, er, june, pool):
    """The change holds the nurse's lock, which a leave write takes first."""
    with transaction.atomic():
        changed(er, Change("Badr", dt.date(2026, 6, BADR_OFF), "D"))
        leave = alongside(pool, lambda: record_leave(mona, er, "Badr", BADR_WORKS))
        assert not leave.done()

    answer = leave.result()
    assert answer.status_code == 201, answer.content
    assert [row["version"] for row in answer.json()["shifts_taken_off"]] == [3]
    roster = in_force_now(er)
    assert roster.versions.first().number == 3
    assert roster.grid["Badr"][f"2026-06-{BADR_OFF:02d}"] == "D" and roster.grid["Badr"][f"2026-06-{BADR_WORKS:02d}"] == ""
    assert roster.grid == roster.versions.first().grid


def test_a_change_waits_for_leave_on_another_nurse_and_keeps_the_shift_it_took_off(mona, er, june, pool):
    """Different nurses' locks do not meet: the unit's and the roster's are what order them."""
    with transaction.atomic():
        assert record_leave(mona, er, "Badr", BADR_WORKS).status_code == 201
        edit = alongside(pool, lambda: changed(er, Change("Amal", dt.date(2026, 6, 9), "N")))
        assert not edit.done()

    assert edit.result().number == 3
    roster = in_force_now(er)
    assert roster.grid["Badr"][f"2026-06-{BADR_WORKS:02d}"] == "" and roster.grid["Amal"]["2026-06-09"] == "N"
    assert roster.grid == roster.versions.first().grid


def test_a_shift_rename_waits_for_leave_over_two_approved_months_and_then_follows_both(mona, er, june, carla, pool):
    approved_roster(er, "2026-07")

    with transaction.atomic():
        answer = leave_over_the_month_end(mona, er)
        assert [row["date"] for row in answer.json()["shifts_taken_off"]] == ["2026-06-30", "2026-07-01"]
        rename = alongside(pool, lambda: change_shift(carla, "N", code="NT"))
        assert not rename.done()

    assert rename.result().status_code == 200
    for month, first in ((JUNE, "2026-06-30"), (dt.date(2026, 7, 1), "2026-07-01")):
        roster = Roster.objects.get(unit=er, month=month)
        assert roster.versions.first().number == 2 and roster.grid["Dana"][first] == ""
        assert "N" not in {code for row in roster.grid.values() for code in row.values()}
        assert roster.grid == in_current_codes(roster.versions.first().grid)
