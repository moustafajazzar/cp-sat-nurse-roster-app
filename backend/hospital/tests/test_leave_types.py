import pytest
from django.core.management import call_command

from accounts.models import Role
from audit.models import AuditEntry

pytestmark = pytest.mark.django_db

LEAVE_TYPES = "/api/hospital/leave-types"


def create(client, **fields):
    return client.post(LEAVE_TYPES, {"name": "Hajj", "active": True} | fields, content_type="application/json")


def update(client, current_name: str, /, **changes):
    row = next(row for row in client.get(LEAVE_TYPES).json() if row["name"] == current_name)
    return client.put(f"{LEAVE_TYPES}/{row['id']}", {"name": row["name"], "active": row["active"]} | changes,
                      content_type="application/json")


def refused_fields(response) -> set[tuple]:
    assert response.status_code == 422, response.content
    return {tuple(error["loc"][2:]) for error in response.json()["detail"]}


def names(client) -> list[str]:
    return [row["name"] for row in client.get(LEAVE_TYPES).json()]


def test_first_start_seeds_the_common_types(make_user, signed_in):
    make_user("admin", Role.SYSTEM_ADMIN)

    call_command("bootstrap")

    assert {"Annual", "Sick", "Educational"} <= set(names(signed_in(Role.CNO)))


def test_later_starts_do_not_bring_back_a_renamed_type(make_user, signed_in, catalogue):
    make_user("admin", Role.SYSTEM_ADMIN)
    client = signed_in(Role.CNO)
    update(client, "Annual", name="Yearly")

    call_command("bootstrap")

    assert "Annual" not in names(client) and "Yearly" in names(client)


def test_the_list_is_in_alphabetical_order_with_retired_types_on_it(signed_in, catalogue):
    client = signed_in(Role.CNO)
    create(client, name="Aaa", active=False)

    assert names(client)[0] == "Aaa" and names(client) == sorted(names(client))


def test_the_cno_adds_renames_and_retires_a_type(signed_in, catalogue):
    client = signed_in(Role.CNO)

    created = create(client, name="  Hajj ")
    assert created.status_code == 201 and created.json()["name"] == "Hajj" and created.json()["active"] is True

    renamed = update(client, "Hajj", name="Pilgrimage")
    assert renamed.status_code == 200 and renamed.json()["id"] == created.json()["id"]

    assert update(client, "Pilgrimage", active=False).json()["active"] is False
    assert "Pilgrimage" in names(client)


@pytest.mark.parametrize("name", ["sick", "SICK"])
def test_a_name_cannot_be_used_twice_in_any_case(signed_in, catalogue, name):
    client = signed_in(Role.CNO)

    assert refused_fields(create(client, name=name)) == {("name",)}
    assert refused_fields(update(client, "Annual", name=name)) == {("name",)}


def test_a_type_may_change_only_the_case_of_its_own_name(signed_in, catalogue):
    assert update(signed_in(Role.CNO), "Sick", name="SICK").status_code == 200


@pytest.mark.parametrize("fields, field", [
    ({"name": "   "}, "name"),
    ({"name": "x" * 101}, "name"),
    ({"name": "a\x00b"}, "name"),
    ({"active": "yes"}, "active"),
    ({"hours": 8}, "hours"),
])
def test_a_refusal_names_the_field(signed_in, catalogue, fields, field):
    assert refused_fields(create(signed_in(Role.CNO), **fields)) == {(field,)}


def test_an_unknown_type_is_404(signed_in, catalogue):
    response = signed_in(Role.CNO).put(f"{LEAVE_TYPES}/019a0000-0000-7000-8000-000000000000",
                                       {"name": "x", "active": True}, content_type="application/json")

    assert response.status_code == 404


def test_changes_are_audited_and_saving_no_change_is_not(signed_in, catalogue):
    client = signed_in(Role.CNO, username="the-cno")
    AuditEntry.objects.all().delete()

    create(client)
    update(client, "Hajj", name="Pilgrimage")
    update(client, "Pilgrimage")

    changed, created = AuditEntry.objects.all()
    assert created.action == "leave_type.created" and created.actor.username == "the-cno" and created.target == "Hajj"
    assert created.after == {"name": "Hajj", "active": True}
    assert changed.action == "leave_type.changed" and changed.target == "Pilgrimage"
    assert changed.before == {"name": "Hajj"} and changed.after == {"name": "Pilgrimage"}


@pytest.mark.parametrize("role", list(Role))
def test_every_role_reads_them(signed_in, catalogue, role):
    assert "Sick" in names(signed_in(role))


@pytest.mark.parametrize("role", [role for role in Role if role != Role.CNO])
def test_only_the_cno_changes_them(signed_in, catalogue, role):
    client = signed_in(role)

    assert create(client).status_code == 403
    assert update(client, "Sick", name="Unwell").status_code == 403
