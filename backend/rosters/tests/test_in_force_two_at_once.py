"""The locks of a change to the roster in force (`rosters.in_force.hold`), proven once on the real PostgreSQL as
`nurses/tests/test_two_at_once.py` does it: one transaction stays open with its write made, the other is seen waiting for a lock
before the first commits, so the order is forced, not hoped for."""
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from accounts.models import User
from nurses.models import NurseProfile
from nurses.tests.test_two_at_once import alongside
from rosters import in_force
from rosters.approved import Change
from rosters.drafts import in_current_codes
from rosters.models import Roster
from rosters.tests.conftest import approved_roster
from rosters.tests.test_pins import change_shift, pin
from rosters.tests.test_review import carla  # noqa: F401
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
