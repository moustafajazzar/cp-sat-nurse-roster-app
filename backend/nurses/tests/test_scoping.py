"""Who reads which nurses, who resets whose password, and what a nurse sees of their own unit."""
from uuid import uuid4

import pytest
from django.test import Client

from accounts.models import LoginAttempts, Role, User
from audit.models import AuditEntry
from conftest import PASSWORD, post
from nurses.tests.conftest import NURSES, added, make_unit
from nurses.tests.test_transfers import transfer

pytestmark = pytest.mark.django_db


@pytest.fixture
def mona(client_of):
    return client_of("mona", Role.UNIT_MANAGER)


@pytest.fixture
def monas_unit(mona):
    return make_unit("ER", User.objects.get(username="mona"))


@pytest.fixture
def omar(client_of):
    return client_of("omar", Role.UNIT_MANAGER)


@pytest.fixture
def omars_unit(omar):
    return make_unit("CCU", User.objects.get(username="omar"))


def reset(client, nurse_id):
    return post(client, f"{NURSES}/{nurse_id}/reset-password", {})


def ids(client):
    return [row["hospital_id"] for row in client.get(NURSES).json()]


# ---- the list ---------------------------------------------------------------------------------------
def test_the_cno_lists_every_nurse_in_every_unit(cno, monas_unit, omars_unit):
    added(cno, monas_unit, "a1", full_name="Bushra")
    added(cno, omars_unit, "a2", full_name="Amal")

    assert ids(cno) == ["a2", "a1"]  # By name.


def test_a_manager_lists_only_their_units_nurses(cno, mona, monas_unit, omars_unit):
    added(cno, monas_unit, "mine")
    added(cno, omars_unit, "theirs")

    assert ids(mona) == ["mine"]


def test_a_manager_with_no_unit_lists_nobody(cno, mona, omars_unit):
    added(cno, omars_unit, "theirs")

    assert ids(mona) == []


def test_a_manager_sees_a_nurse_due_to_join_without_being_told_from_where(cno, mona, monas_unit, omars_unit, on_day):
    on_day(2026, 11, 15)
    nurse = added(cno, omars_unit, "joining")["nurse"]["id"]
    transfer(cno, nurse, monas_unit, "2027-01")

    [row] = mona.get(NURSES).json()

    assert row["unit"] is None
    assert row["transfers"] == [{"from_month": "2027-01", "unit": {"id": str(monas_unit.pk), "name": "ER"}}]
    assert "CCU" not in str(row)


def test_a_manager_sees_a_nurse_due_to_leave_without_being_told_to_where(cno, mona, monas_unit, omars_unit, on_day):
    on_day(2026, 11, 15)
    nurse = added(cno, monas_unit, "leaving")["nurse"]["id"]
    transfer(cno, nurse, omars_unit, "2027-01")

    [row] = mona.get(NURSES).json()

    assert row["unit"]["name"] == "ER"
    assert row["transfers"] == [{"from_month": "2027-01", "unit": None}]


def test_a_nurse_who_has_left_is_off_the_managers_list(cno, mona, monas_unit, omars_unit, on_day):
    on_day(2026, 11, 15)
    nurse = added(cno, monas_unit, "left")["nurse"]["id"]
    transfer(cno, nurse, omars_unit, "2027-01")

    on_day(2027, 1, 1)

    assert ids(mona) == []


def test_a_deactivated_nurse_stays_on_the_managers_list(cno, mona, monas_unit, on_day):
    on_day(2026, 11, 15)
    nurse = added(cno, monas_unit, "gone")["nurse"]["id"]
    cno.put(f"{NURSES}/{nurse}/end-date", {"end_date": "2026-11-10"}, content_type="application/json")

    [row] = mona.get(NURSES).json()

    assert row["is_active"] is False


def test_only_the_cno_changes_a_nurse(cno, mona, monas_unit, omars_unit):
    nurse = added(cno, monas_unit)["nurse"]["id"]

    assert transfer(mona, nurse, omars_unit, "2076-01").status_code == 403
    assert mona.put(f"{NURSES}/{nurse}/end-date", {"end_date": None}, content_type="application/json").status_code == 403
    assert post(mona, NURSES, {}).status_code == 403


# ---- names and shifts only, from the start -----------------------------------------------------------
def test_a_staff_nurse_is_refused_the_nurse_list_and_every_change(cno, er):
    answer = added(cno, er)
    client = change_password_and_sign_in(answer)
    nurse = answer["nurse"]["id"]

    assert client.get(NURSES).status_code == 403
    assert reset(client, nurse).status_code == 403
    assert transfer(client, nurse, er, "2076-01").status_code == 403


def change_password_and_sign_in(answer) -> Client:
    client = Client()
    username, password = answer["nurse"]["hospital_id"], answer["generated_password"]
    assert post(client, "/api/auth/login", {"username": username, "password": password}).status_code == 200
    assert post(client, "/api/auth/change-password",
                {"current_password": password, "new_password": PASSWORD}).status_code == 200
    return client


def test_a_nurse_reads_their_unit_after_the_forced_change_and_nothing_else_of_it(cno, er):
    answer = added(cno, er)
    client = Client()
    post(client, "/api/auth/login", {"username": "h123", "password": answer["generated_password"]})
    assert client.get(f"{NURSES}/my-unit").status_code == 403

    client = change_password_and_sign_in(answer)

    assert client.get(f"{NURSES}/my-unit").json() == {"id": str(er.pk), "name": "ER"}


def test_a_nurses_unit_changes_when_the_transfer_takes_effect(cno, er, icu, on_day):
    on_day(2026, 11, 15)
    answer = added(cno, er)
    transfer(cno, answer["nurse"]["id"], icu, "2027-01")
    client = change_password_and_sign_in(answer)
    assert client.get(f"{NURSES}/my-unit").json()["name"] == "ER"

    on_day(2027, 1, 1)

    assert client.get(f"{NURSES}/my-unit").json()["name"] == "ICU"


# ---- password resets ---------------------------------------------------------------------------------
def test_the_cno_resets_any_nurses_password(cno, monas_unit, omars_unit):
    nurse = added(cno, omars_unit)["nurse"]["id"]

    response = reset(cno, nurse)

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    client = Client()
    login = post(client, "/api/auth/login", {"username": "h123", "password": response.json()["generated_password"]})
    assert login.status_code == 200 and login.json()["must_change_password"] is True


def test_a_manager_resets_their_own_units_nurse(cno, mona, monas_unit):
    nurse = added(cno, monas_unit)["nurse"]["id"]

    response = reset(mona, nurse)

    assert response.status_code == 200 and response.json()["generated_password"]
    entry = AuditEntry.objects.get(action="user.password_reset", target="h123")
    assert entry.actor.username == "mona"


def test_another_units_manager_gets_403_the_same_as_for_nobody(cno, mona, monas_unit, omars_unit):
    nurse = added(cno, omars_unit)["nurse"]["id"]
    password = User.objects.get(username="h123").password

    refused, missing = reset(mona, nurse), reset(mona, uuid4())

    assert refused.status_code == missing.status_code == 403
    assert refused.json() == missing.json()
    assert User.objects.get(username="h123").password == password


def test_a_manager_may_not_reset_a_nurse_who_has_not_yet_joined(cno, mona, monas_unit, omars_unit, on_day):
    on_day(2026, 11, 15)
    nurse = added(cno, omars_unit)["nurse"]["id"]
    transfer(cno, nurse, monas_unit, "2027-01")

    assert reset(mona, nurse).status_code == 403
    on_day(2027, 1, 1)
    assert reset(mona, nurse).status_code == 200


def test_a_manager_with_no_unit_is_refused(cno, mona, omars_unit):
    assert reset(mona, added(cno, omars_unit)["nurse"]["id"]).status_code == 403


def test_a_reset_lifts_a_lock_from_failed_logins(cno, er):
    nurse = added(cno, er)["nurse"]["id"]
    LoginAttempts.objects.create(username="h123", failures=5)

    reset(cno, nurse)

    assert not LoginAttempts.objects.filter(username="h123").exists()


def test_an_unknown_nurse_is_404_for_the_cno(cno):
    assert reset(cno, uuid4()).status_code == 404
