"""The CNO adds a nurse: user, nurse profile and a password shown once, in one transaction."""
import pytest
from django.test import Client

from accounts.models import Role, User
from audit.models import AuditEntry
from conftest import post
from nurses.models import NurseProfile
from nurses.tests.conftest import NURSES, add, added, make_unit, message, refused_fields

pytestmark = pytest.mark.django_db


def test_adding_creates_the_user_the_profile_and_a_password_that_forces_a_change(cno, er):
    response = add(cno, er)

    assert response.status_code == 201
    assert response["Cache-Control"] == "no-store"
    nurse = response.json()["nurse"]
    assert (nurse["hospital_id"], nurse["full_name"], nurse["gender"], nurse["start_date"]) == (
        "h123", "Amal Saleh", "F", "2026-01-10")
    assert nurse["unit"]["name"] == "ER"
    assert nurse["role"] == "staff_nurse" and nurse["is_active"] is True and nurse["must_change_password"] is True

    client = Client()
    login = post(client, "/api/auth/login", {"username": "h123", "password": response.json()["generated_password"]})
    assert login.status_code == 200 and login.json()["must_change_password"] is True
    assert User.objects.get(username="h123").role == Role.STAFF_NURSE


def test_the_password_is_nowhere_in_what_is_listed_or_audited(cno, er):
    password = added(cno, er)["generated_password"]

    assert password not in cno.get(NURSES).content.decode()
    assert not [entry for entry in AuditEntry.objects.all() if password in str(entry.after)]


@pytest.mark.parametrize("taken", ["h123", "H123", "chief"])
def test_a_hospital_id_is_unique_in_any_case_and_among_all_usernames(cno, er, taken):
    added(cno, er)

    response = add(cno, er, hospital_id=taken)

    assert refused_fields(response) == {("hospital_id",)}
    assert "already" in message(response)
    assert NurseProfile.objects.count() == 1


@pytest.mark.parametrize("fields, field", [
    ({"hospital_id": ""}, "hospital_id"),
    ({"hospital_id": "has space"}, "hospital_id"),
    ({"hospital_id": 123}, "hospital_id"),
    ({"full_name": "  "}, "full_name"),
    ({"full_name": "a\x00b"}, "full_name"),
    ({"gender": "X"}, "gender"),
    ({"start_date": "10/01/2026"}, "start_date"),
    ({"start_date": 20260110}, "start_date"),
    ({"unit_id": "not-an-id"}, "unit_id"),
])
def test_what_cannot_be_is_refused_beside_its_field(cno, er, fields, field):
    assert refused_fields(add(cno, er, **fields)) == {(field,)}
    assert not User.objects.filter(role=Role.STAFF_NURSE).exists()


def test_an_unknown_field_is_refused(cno, er):
    assert add(cno, er, team_leader=True).status_code == 422


def test_a_nurse_cannot_be_added_to_an_archived_unit_and_nothing_stays(cno, db):
    old = make_unit("Old", archived=True)

    response = add(cno, old)

    assert refused_fields(response) == {("unit_id",)}
    assert not User.objects.filter(username="h123").exists()
    assert not AuditEntry.objects.filter(target="h123").exists()


def test_a_unit_that_does_not_exist_is_refused_beside_its_field(cno, er):
    assert refused_fields(add(cno, er, unit_id="019a0000-0000-7000-8000-000000000001")) == {("unit_id",)}


def test_adding_is_audited_with_no_rostering_attribute(cno, er):
    added(cno, er)

    entry = AuditEntry.objects.get(action="nurse.added")
    assert entry.target == "h123"
    assert entry.after == {"unit": "ER", "start_date": "2026-01-10"}
    assert "gender" not in str(AuditEntry.objects.filter(target="h123").values_list("before", "after"))


def test_a_nurse_starting_later_belongs_to_the_unit_from_today(cno, er):
    nurse = added(cno, er, start_date="2076-01-01")["nurse"]

    assert nurse["unit"]["name"] == "ER"
    assert nurse["transfers"] == []
