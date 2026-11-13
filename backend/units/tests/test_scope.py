"""`units.scope` is what every unit-scoped endpoint calls. `SignedIn` sees only the role, and the
walking test in accounts/tests/test_authorization.py sees only `SignedIn`, so it is tested here: directly,
and through a unit-scoped endpoint."""
from uuid import uuid4

import pytest
from django.http import Http404
from ninja.errors import HttpError

from accounts.models import Role, User
from conftest import post
from nurses.models import NurseProfile, Placement
from units.models import Unit
from units.scope import unit_in_scope, units_in_scope

pytestmark = pytest.mark.django_db


def make_unit(name, manager=None, **fields) -> Unit:
    return Unit.objects.create(name=name, manager=manager, rules={}, **fields)


@pytest.fixture
def mona(make_user):
    return make_user("mona", Role.UNIT_MANAGER)


@pytest.fixture
def her_unit(mona):
    return make_unit("ER", mona)


@pytest.fixture
def other_unit(make_user):
    return make_unit("ICU", make_user("omar", Role.UNIT_MANAGER))


# ---- the check ------------------------------------------------------------------------------------
def test_a_managers_scope_is_their_unit_alone(mona, her_unit, other_unit):
    assert list(units_in_scope(mona)) == [her_unit]
    assert unit_in_scope(mona, her_unit.pk) == her_unit


def test_a_manager_is_refused_another_unit(mona, her_unit, other_unit):
    with pytest.raises(HttpError) as refused:
        unit_in_scope(mona, other_unit.pk)
    assert refused.value.status_code == 403


def test_a_manager_cannot_tell_a_unit_that_is_not_theirs_from_one_that_does_not_exist(mona, her_unit, other_unit):
    with pytest.raises(HttpError) as other:
        unit_in_scope(mona, other_unit.pk)
    with pytest.raises(HttpError) as missing:
        unit_in_scope(mona, uuid4())

    assert (other.value.status_code, str(other.value)) == (missing.value.status_code, str(missing.value))


def test_a_manager_with_no_unit_has_nothing_in_scope(mona, other_unit):
    assert list(units_in_scope(mona)) == []
    with pytest.raises(HttpError) as refused:
        unit_in_scope(mona, other_unit.pk)
    assert refused.value.status_code == 403


def test_reassigning_moves_the_scope_at_once(mona, make_user, her_unit):
    omar = make_user("omar", Role.UNIT_MANAGER)

    her_unit.manager = omar
    her_unit.save()

    assert list(units_in_scope(mona)) == []
    assert unit_in_scope(omar, her_unit.pk) == her_unit


@pytest.mark.parametrize("role", [Role.CNO, Role.DEPUTY_CNO])
def test_the_cno_and_deputy_see_every_unit(make_user, role, her_unit, other_unit):
    user = make_user("reader", role)

    assert set(units_in_scope(user)) == {her_unit, other_unit}
    assert unit_in_scope(user, other_unit.pk) == other_unit
    with pytest.raises(Http404):
        unit_in_scope(user, uuid4())


def test_the_system_admin_has_nothing_in_scope(make_user, her_unit):
    user = make_user("other", Role.SYSTEM_ADMIN)

    assert list(units_in_scope(user)) == []
    with pytest.raises(HttpError) as refused:
        unit_in_scope(user, her_unit.pk)
    assert refused.value.status_code == 403


def test_a_staff_nurses_scope_is_the_unit_they_belong_to_now(make_user, her_unit, other_unit):
    user = make_user("h123", Role.STAFF_NURSE)
    nurse = NurseProfile.objects.create(user=user, gender="F", start_date="2026-01-01")
    Placement.objects.create(nurse=nurse, unit=her_unit, from_month="2026-01-01")
    Placement.objects.create(nurse=nurse, unit=other_unit, from_month="2076-01-01")

    assert list(units_in_scope(user)) == [her_unit]
    assert unit_in_scope(user, her_unit.pk) == her_unit
    with pytest.raises(HttpError) as refused:
        unit_in_scope(user, other_unit.pk)
    assert refused.value.status_code == 403


# ---- over the API ---------------------------------------------------------------------------------
def test_a_manager_lists_only_their_unit(client_of, other_unit):
    client = client_of("mina", Role.UNIT_MANAGER)
    assert client.get("/api/units").json() == []

    make_unit("CCU", User.objects.get(username="mina"), rules_need_review=False)

    rows = client.get("/api/units").json()
    assert [(row["name"], row["rules_need_review"]) for row in rows] == [("CCU", False)]


def test_a_manager_reads_their_unit_and_is_refused_any_other(client_of, other_unit):
    client = client_of("mina", Role.UNIT_MANAGER)
    unit = make_unit("CCU", User.objects.get(username="mina"))

    assert client.get(f"/api/units/{unit.pk}").json()["name"] == "CCU"
    assert client.get(f"/api/units/{other_unit.pk}").status_code == 403
    assert client.get(f"/api/units/{uuid4()}").status_code == 403


def test_a_manager_with_no_unit_is_refused_every_unit(client_of, other_unit):
    assert client_of("mina", Role.UNIT_MANAGER).get(f"/api/units/{other_unit.pk}").status_code == 403


def test_a_manager_loses_the_unit_on_reassignment_over_the_api(client_of, make_user):
    client = client_of("mina", Role.UNIT_MANAGER)
    cno = client_of("chief", Role.CNO)
    unit = make_unit("CCU", User.objects.get(username="mina"))
    assert client.get(f"/api/units/{unit.pk}").status_code == 200
    omar = make_user("omar", Role.UNIT_MANAGER)

    assert cno.put(f"/api/units/{unit.pk}", {"name": "CCU", "manager_id": str(omar.pk)},
                   content_type="application/json").status_code == 200

    assert client.get(f"/api/units/{unit.pk}").status_code == 403
    assert client.get("/api/units").json() == []


def test_a_manager_cannot_write_to_their_own_unit_here(client_of):
    client = client_of("mina", Role.UNIT_MANAGER)
    unit = make_unit("CCU", User.objects.get(username="mina"))

    assert client.put(f"/api/units/{unit.pk}", {"name": "X", "manager_id": None},
                      content_type="application/json").status_code == 403
    assert post(client, f"/api/units/{unit.pk}/archive", {}).status_code == 403
    assert client.get("/api/units/managers").status_code == 403
    assert post(client, "/api/units", {"name": "X", "manager_id": None}).status_code == 403


def test_the_system_admin_and_a_nurse_are_refused_units(client_of, other_unit):
    for username, role in (("sysadmin", Role.SYSTEM_ADMIN), ("h123", Role.STAFF_NURSE)):
        client = client_of(username, role)
        assert client.get("/api/units").status_code == 403
        assert client.get(f"/api/units/{other_unit.pk}").status_code == 403
