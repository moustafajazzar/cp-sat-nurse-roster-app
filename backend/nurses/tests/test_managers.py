"""A unit manager may also be a nurse: the CNO gives them a nurse profile, in the unit they hold, and their role and
their account stay what they were."""
from uuid import uuid4

import pytest

from accounts.models import Role, User
from audit.models import AuditEntry
from conftest import post
from nurses.tests.conftest import NURSES, make_unit, message, refused_fields
from nurses.tests.test_transfers import transfer
from units.models import Unit

pytestmark = pytest.mark.django_db

MANAGERS = f"{NURSES}/managers"


@pytest.fixture
def mona(make_user):
    return make_user("mona", Role.UNIT_MANAGER, full_name="Mona Ali")


def give(client, user, unit, **fields):
    return post(client, MANAGERS, {"user_id": str(user.pk), "gender": "F", "unit_id": str(unit.pk),
                                   "start_date": "2026-01-10"} | fields)


def given(client, user, unit) -> dict:
    response = give(client, user, unit)
    assert response.status_code == 201, response.content
    return response.json()


def assign(client, unit, user):
    return client.put(f"/api/units/{unit.pk}", {"name": unit.name, "manager_id": str(user.pk) if user else None},
                      content_type="application/json")


def test_a_manager_is_given_a_profile_and_stays_a_manager_with_their_password(cno, mona, er):
    password = mona.password
    make_unit_held = Unit.objects.filter(pk=er.pk).update(manager=mona)
    assert make_unit_held == 1

    nurse = given(cno, mona, er)

    assert (nurse["hospital_id"], nurse["full_name"], nurse["role"]) == ("mona", "Mona Ali", "unit_manager")
    assert nurse["unit"]["name"] == "ER"
    mona.refresh_from_db()
    assert mona.role == Role.UNIT_MANAGER and mona.password == password
    assert "generated_password" not in nurse
    assert AuditEntry.objects.get(action="nurse.added").target == "mona"


def test_the_manager_is_on_their_own_units_list_and_it_counts(cno, client_of, er):
    client = client_of("mina", Role.UNIT_MANAGER)
    mina = User.objects.get(username="mina")
    Unit.objects.filter(pk=er.pk).update(manager=mina)

    given(cno, mina, er)

    assert [row["hospital_id"] for row in client.get(NURSES).json()] == ["mina"]
    assert er.nurse_count() == 1


def test_a_profile_is_refused_in_a_unit_other_than_the_one_they_hold(cno, mona, er, icu):
    Unit.objects.filter(pk=er.pk).update(manager=mona)

    response = give(cno, mona, icu)

    assert refused_fields(response) == {("unit_id",)}
    assert "ER" in message(response)


def test_a_manager_holding_no_unit_may_be_placed_in_any(cno, mona, icu):
    assert given(cno, mona, icu)["unit"]["name"] == "ICU"


def test_only_one_profile(cno, mona, er):
    given(cno, mona, er)

    assert refused_fields(give(cno, mona, er)) == {("user_id",)}


@pytest.mark.parametrize("role", [Role.STAFF_NURSE, Role.DEPUTY_CNO, Role.SYSTEM_ADMIN])
def test_only_a_unit_manager_is_given_one_here(cno, make_user, er, role):
    assert refused_fields(give(cno, make_user("x", role), er)) == {("user_id",)}


def test_a_deactivated_manager_is_not_given_one(cno, make_user, er):
    assert refused_fields(give(cno, make_user("x", Role.UNIT_MANAGER, is_active=False), er)) == {("user_id",)}


def test_nobody_is_not_given_one(cno, er):
    response = post(cno, MANAGERS, {"user_id": str(uuid4()), "gender": "F", "unit_id": str(er.pk),
                                    "start_date": "2026-01-10"})
    assert refused_fields(response) == {("user_id",)}


def test_an_archived_unit_is_refused(cno, mona):
    assert refused_fields(give(cno, mona, make_unit("Old", archived=True))) == {("unit_id",)}


def test_the_choices_are_active_managers_with_no_profile_and_the_unit_each_holds(cno, mona, make_user, er):
    Unit.objects.filter(pk=er.pk).update(manager=mona)
    make_user("gone", Role.UNIT_MANAGER, is_active=False)
    placed = make_user("placed", Role.UNIT_MANAGER)
    free = make_user("free", Role.UNIT_MANAGER, full_name="Free One")
    given(cno, placed, make_unit("ICU"))

    rows = cno.get(MANAGERS).json()

    assert rows == [
        {"id": str(free.pk), "username": "free", "full_name": "Free One", "unit": None},
        {"id": str(mona.pk), "username": "mona", "full_name": "Mona Ali", "unit": {"id": str(er.pk), "name": "ER"}},
    ]


# ---- the unit they hold is the unit they are in -------------------------------------------------------
def test_assigning_a_manager_whose_profile_is_elsewhere_is_refused(cno, mona, er, icu):
    given(cno, mona, er)

    response = assign(cno, icu, mona)

    assert refused_fields(response) == {("manager_id",)}
    assert "ER" in message(response)
    assert Unit.objects.get(pk=icu.pk).manager is None


def test_creating_a_unit_with_a_manager_whose_profile_is_elsewhere_is_refused(cno, mona, er):
    given(cno, mona, er)

    assert refused_fields(post(cno, "/api/units", {"name": "New", "manager_id": str(mona.pk)})) == {("manager_id",)}


def test_a_manager_due_to_move_elsewhere_cannot_be_assigned_here(cno, mona, er, icu, on_day):
    on_day(2026, 11, 15)
    given(cno, mona, er)
    transfer(cno, str(mona.nurse_profile.pk), icu, "2027-01")

    assert refused_fields(assign(cno, er, mona)) == {("manager_id",)}


def test_assigning_a_manager_to_the_unit_their_profile_is_in_works(cno, mona, er):
    given(cno, mona, er)

    assert assign(cno, er, mona).status_code == 200


def test_a_manager_holding_a_unit_cannot_be_transferred_out_of_it(cno, mona, er, icu):
    Unit.objects.filter(pk=er.pk).update(manager=mona)
    nurse = given(cno, mona, er)["id"]

    response = transfer(cno, nurse, icu, "2076-01")

    assert refused_fields(response) == {("unit_id",)}
    assert "manages ER" in message(response)


def test_moving_a_manager_takes_them_off_transfers_them_and_assigns_them(cno, mona, er, icu):
    Unit.objects.filter(pk=er.pk).update(manager=mona)
    nurse = given(cno, mona, er)["id"]

    assert assign(cno, er, None).status_code == 200
    # Taking them off touches nothing of the profile.
    assert next(r for r in cno.get(NURSES).json() if r["id"] == nurse)["unit"]["name"] == "ER"
    assert transfer(cno, nurse, icu, "2076-01").status_code == 200
    # Still in ER until then, so not yet ICU's to hold.
    assert assign(cno, icu, mona).status_code == 422


def test_a_manager_is_never_reset_through_the_nurses_endpoint(cno, client_of, er):
    client = client_of("mina", Role.UNIT_MANAGER)
    mina = User.objects.get(username="mina")
    Unit.objects.filter(pk=er.pk).update(manager=mina)
    nurse = given(cno, mina, er)["id"]
    password = User.objects.get(pk=mina.pk).password

    for caller in (cno, client):
        response = post(caller, f"{NURSES}/{nurse}/reset-password", {})
        assert response.status_code == 422
        assert "System Admin" in message(response)
    assert User.objects.get(pk=mina.pk).password == password


# ---- an end date on a manager's profile ---------------------------------------------------------------
def end(client, nurse_id, date):
    return client.put(f"{NURSES}/{nurse_id}/end-date", {"end_date": date}, content_type="application/json")


def test_an_end_date_ends_the_profile_and_leaves_the_account_and_the_unit_alone(cno, mona, er, on_day):
    on_day(2026, 11, 15)
    Unit.objects.filter(pk=er.pk).update(manager=mona)
    nurse = given(cno, mona, er)["id"]

    answer = end(cno, nurse, "2026-11-10").json()

    assert answer["is_active"] is True and answer["end_date"] == "2026-11-10"
    assert Unit.objects.get(pk=er.pk).manager == mona
    assert er.nurse_count() == 0


def test_moving_an_ended_profile_back_into_service_is_refused_if_its_unit_is_archived(cno, mona, er, on_day):
    on_day(2026, 11, 15)
    nurse = given(cno, mona, er)["id"]
    end(cno, nurse, "2026-11-10")
    assert post(cno, f"/api/units/{er.pk}/archive", {}).status_code == 200

    # Today is still in service for a manager, whose account the end date leaves alone.
    assert "ER is archived" in message(end(cno, nurse, "2026-11-15"))
    assert "ER is archived" in message(end(cno, nurse, None))
    assert end(cno, nurse, "2026-11-12").status_code == 200


def test_the_hourly_job_leaves_a_manager_alone(cno, mona, er, on_day):
    from nurses.tasks import close_ended_nurses
    on_day(2026, 11, 15)
    nurse = given(cno, mona, er)["id"]
    end(cno, nurse, "2026-11-20")
    on_day(2026, 11, 21)

    close_ended_nurses(timestamp=0)

    assert User.objects.get(pk=mona.pk).is_active is True


# ---- the System Admin and a manager with a profile ----------------------------------------------------
def test_deactivating_a_manager_lets_their_old_unit_be_archived_and_reactivating_is_then_refused(
        cno, client_of, mona, er):
    admin = client_of("sysadmin", Role.SYSTEM_ADMIN)
    Unit.objects.filter(pk=er.pk).update(manager=mona)
    given(cno, mona, er)
    assert post(cno, f"/api/units/{er.pk}/archive", {}).status_code == 422

    assert post(admin, f"/api/users/{mona.pk}/deactivate", {}).status_code == 200
    assert post(cno, f"/api/units/{er.pk}/archive", {}).status_code == 200

    response = post(admin, f"/api/users/{mona.pk}/reactivate", {})
    assert response.status_code == 409
    assert "ER" in response.json()["detail"]
    assert User.objects.get(pk=mona.pk).is_active is False


def test_a_manager_with_no_profile_reactivates_as_before(client_of, mona):
    admin = client_of("sysadmin", Role.SYSTEM_ADMIN)
    post(admin, f"/api/users/{mona.pk}/deactivate", {})

    assert post(admin, f"/api/users/{mona.pk}/reactivate", {}).status_code == 200
