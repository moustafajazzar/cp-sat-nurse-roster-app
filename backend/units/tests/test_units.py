"""The CNO's units: creating, renaming, archiving and assigning their managers, over the API."""
import pytest

from accounts.models import Role, User
from audit.models import AuditEntry
from conftest import post
from units.models import Unit

pytestmark = pytest.mark.django_db

UNITS = "/api/units"


@pytest.fixture
def cno(client_of):
    return client_of("chief", Role.CNO)


@pytest.fixture
def manager(make_user):
    return make_user("mona", Role.UNIT_MANAGER, full_name="Mona Ali")


def create(cno, name="ER", manager=None):
    return post(cno, UNITS, {"name": name, "manager_id": str(manager.pk) if manager else None})


def created(cno, name="ER", manager=None) -> dict:
    response = create(cno, name, manager)
    assert response.status_code == 201, response.content
    return response.json()


def put(cno, unit, name=None, manager="same"):
    manager_id = unit["manager"]["id"] if manager == "same" and unit["manager"] else (
        None if manager in ("same", None) else str(manager.pk))
    return cno.put(f"{UNITS}/{unit['id']}", {"name": name or unit["name"], "manager_id": manager_id},
                   content_type="application/json")


def archive(cno, unit):
    return post(cno, f"{UNITS}/{unit['id']}/archive", {})


def refused_fields(response) -> set[tuple]:
    assert response.status_code == 422, response.content
    return {tuple(error["loc"][2:]) for error in response.json()["detail"]}


def message(response) -> str:
    assert response.status_code == 422, response.content
    return response.json()["detail"][0]["msg"]


def audit(action) -> list[AuditEntry]:
    return list(AuditEntry.objects.filter(action=action).order_by("at"))


# ---- creating -------------------------------------------------------------------------------------
def test_the_cno_creates_a_unit_with_no_manager_yet(cno):
    unit = created(cno, "  Cardiac care ")

    assert unit["name"] == "Cardiac care"
    assert unit["manager"] is None
    assert unit["archived"] is False
    assert unit["rules_need_review"] is True
    assert [row["name"] for row in cno.get(UNITS).json()] == ["Cardiac care"]


def test_the_cno_creates_a_unit_with_its_manager(cno, manager):
    unit = created(cno, manager=manager)

    assert unit["manager"] == {"id": str(manager.pk), "username": "mona", "full_name": "Mona Ali"}


def test_creating_is_audited(cno, manager):
    created(cno, manager=manager)

    entry = audit("unit.created")[0]
    assert entry.actor.username == "chief" and entry.target == "ER"
    assert entry.after == {"name": "ER", "manager": "mona"}


@pytest.mark.parametrize("name", ["", "   ", "x" * 101, "a\x00b", 7, None])
def test_a_name_that_cannot_be_is_refused_beside_its_field(cno, name):
    response = post(cno, UNITS, {"name": name, "manager_id": None})

    assert refused_fields(response) == {("name",)}
    assert not Unit.objects.exists()


def test_an_unknown_field_is_refused(cno):
    assert post(cno, UNITS, {"name": "ER", "manager_id": None, "archived": True}).status_code == 422


def test_two_units_cannot_share_a_name_in_any_case(cno):
    created(cno, "ER")

    response = create(cno, "er")

    assert refused_fields(response) == {("name",)}
    assert len(audit("unit.created")) == 1


def test_a_name_an_archived_unit_held_can_be_used_again(cno):
    archive(cno, created(cno, "ER"))

    assert created(cno, "ER")["archived"] is False
    assert [(row["name"], row["archived"]) for row in cno.get(UNITS).json()] == [("ER", False), ("ER", True)]


# ---- renaming -------------------------------------------------------------------------------------
def test_renaming_is_audited_with_what_changed(cno):
    unit = created(cno, "ER")

    response = put(cno, unit, "Emergency")

    assert response.status_code == 200 and response.json()["name"] == "Emergency"
    entry = audit("unit.changed")[0]
    assert entry.target == "Emergency" and entry.before == {"name": "ER"} and entry.after == {"name": "Emergency"}


def test_saving_nothing_new_writes_nothing(cno, manager):
    unit = created(cno, manager=manager)

    assert put(cno, unit).status_code == 200
    assert audit("unit.changed") == []


def test_a_rename_cannot_take_another_units_name(cno):
    created(cno, "ICU")
    unit = created(cno, "ER")

    assert refused_fields(put(cno, unit, "icu")) == {("name",)}
    assert Unit.objects.get(pk=unit["id"]).name == "ER"


def test_a_unit_may_change_the_case_of_its_own_name(cno):
    assert put(cno, created(cno, "er"), "ER").status_code == 200


def test_renaming_an_unknown_unit_is_404(cno):
    fake = {"id": "019a0000-0000-7000-8000-000000000000", "name": "ER", "manager": None}

    assert put(cno, fake).status_code == 404


# ---- archiving ------------------------------------------------------------------------------------
def test_archiving_a_unit_with_no_nurses_is_audited_and_lets_its_manager_go(cno, manager):
    unit = created(cno, manager=manager)

    response = archive(cno, unit)

    assert response.status_code == 200
    assert response.json()["archived"] is True and response.json()["manager"] is None
    entry = audit("unit.archived")[0]
    assert entry.target == "ER"
    assert entry.before == {"archived": False, "manager": "mona"}
    assert entry.after == {"archived": True, "manager": None}
    assert created(cno, "ICU", manager)["manager"]["username"] == "mona"


def test_archiving_twice_changes_nothing_more(cno):
    unit = created(cno)
    archive(cno, unit)

    assert archive(cno, unit).status_code == 200
    assert len(audit("unit.archived")) == 1


def test_an_archived_unit_cannot_be_changed(cno, manager):
    unit = created(cno, "ER")
    archive(cno, unit)

    assert refused_fields(put(cno, unit, "Emergency")) == {("name",)}
    assert refused_fields(put(cno, unit, manager=manager)) == {("name",)}
    assert Unit.objects.get(pk=unit["id"]).manager is None


def test_there_is_no_way_to_delete_a_unit(cno):
    assert cno.delete(f"{UNITS}/{created(cno)['id']}").status_code == 405


# ---- assignment cardinality -----------------------------------------------------------------------
def test_assigning_a_manager_is_audited(cno, manager):
    unit = created(cno)

    response = put(cno, unit, manager=manager)

    assert response.json()["manager"]["username"] == "mona"
    entry = audit("unit.changed")[0]
    assert entry.target == "ER" and entry.before == {"manager": None} and entry.after == {"manager": "mona"}


def test_reassigning_replaces_the_one_manager(cno, manager, make_user):
    unit = created(cno, manager=manager)
    other = make_user("omar", Role.UNIT_MANAGER)

    assert put(cno, unit, manager=other).json()["manager"]["username"] == "omar"
    entry = audit("unit.changed")[0]
    assert entry.before == {"manager": "mona"} and entry.after == {"manager": "omar"}
    assert created(cno, "ICU", manager)["manager"]["username"] == "mona"


def test_a_manager_holds_one_unit(cno, manager):
    created(cno, "ER", manager)
    unit = created(cno, "ICU")

    response = put(cno, unit, manager=manager)

    assert refused_fields(response) == {("manager_id",)}
    assert "ER" in message(response)
    assert Unit.objects.get(pk=unit["id"]).manager is None
    assert refused_fields(create(cno, "CCU", manager)) == {("manager_id",)}
    assert Unit.objects.count() == 2


def test_two_managers_can_change_places_by_way_of_no_manager(cno, manager, make_user):
    other = make_user("omar", Role.UNIT_MANAGER)
    first, second = created(cno, "ER", manager), created(cno, "ICU", other)

    assert put(cno, first, manager=None).status_code == 200
    assert put(cno, second, manager=manager).status_code == 200
    assert put(cno, first | {"manager": None}, manager=other).status_code == 200

    assert Unit.objects.get(name="ER").manager == other
    assert Unit.objects.get(name="ICU").manager == manager


@pytest.mark.parametrize("role", [Role.CNO, Role.DEPUTY_CNO, Role.STAFF_NURSE, Role.SYSTEM_ADMIN])
def test_only_a_unit_manager_account_can_hold_a_unit(cno, make_user, role):
    # Another active CNO would break the one-CNO constraint, and it is the role being refused here.
    account = make_user("other", role, full_name="Other") if role != Role.CNO else make_user(
        "other", role, is_active=False)

    assert refused_fields(create(cno, "ER", account)) == {("manager_id",)}


def test_a_deactivated_manager_cannot_be_assigned(cno, make_user):
    gone = make_user("gone", Role.UNIT_MANAGER, is_active=False)

    assert refused_fields(create(cno, "ER", gone)) == {("manager_id",)}


def test_an_unknown_manager_is_refused(cno):
    response = post(cno, UNITS, {"name": "ER", "manager_id": "019a0000-0000-7000-8000-000000000000"})

    assert refused_fields(response) == {("manager_id",)}


def test_a_manager_id_that_is_no_id_is_refused(cno):
    assert refused_fields(post(cno, UNITS, {"name": "ER", "manager_id": "mona"})) == {("manager_id",)}


def test_the_manager_must_be_named_even_when_there_is_none(cno):
    assert refused_fields(post(cno, UNITS, {"name": "ER"})) == {("manager_id",)}


def test_the_form_lists_active_managers_and_what_they_hold(cno, manager, make_user):
    make_user("gone", Role.UNIT_MANAGER, is_active=False)
    make_user("omar", Role.UNIT_MANAGER, full_name="Omar Said")
    make_user("deputy", Role.DEPUTY_CNO)
    unit = created(cno, "ER", manager)

    response = cno.get(f"{UNITS}/managers")

    assert response.json() == [
        {"id": str(manager.pk), "username": "mona", "full_name": "Mona Ali", "unit": "ER"},
        {"id": str(User.objects.get(username="omar").pk), "username": "omar", "full_name": "Omar Said", "unit": None},
    ]
    assert unit["manager"]["username"] == "mona"


# ---- deactivating a manager ------------------------------------------------------------------------
@pytest.fixture
def admin(client_of):
    return client_of("sysadmin", Role.SYSTEM_ADMIN)


def test_deactivating_a_manager_leaves_their_unit_with_none_and_says_so_in_the_log(admin, cno, manager):
    unit = created(cno, "ER", manager)

    response = post(admin, f"/api/users/{manager.pk}/deactivate", {})

    assert response.status_code == 200 and response.json()["unit"] is None
    row = cno.get(f"{UNITS}/{unit['id']}").json()
    assert row["manager"] is None and row["archived"] is False
    entry = audit("unit.changed")[0]
    assert entry.actor.username == "sysadmin" and entry.target == "ER"
    assert entry.before == {"manager": "mona"} and entry.after == {"manager": None}


def test_reactivating_does_not_give_the_unit_back(admin, cno, manager):
    unit = created(cno, "ER", manager)
    post(admin, f"/api/users/{manager.pk}/deactivate", {})

    response = post(admin, f"/api/users/{manager.pk}/reactivate", {})

    assert response.json()["unit"] is None
    assert cno.get(f"{UNITS}/{unit['id']}").json()["manager"] is None


def test_the_system_admin_is_told_which_unit_a_manager_holds(admin, cno, manager, make_user):
    make_user("omar", Role.UNIT_MANAGER)
    created(cno, "ER", manager)

    units = {row["username"]: row["unit"] for row in admin.get("/api/users").json()}

    assert units == {"chief": None, "mona": "ER", "omar": None}


def test_deactivating_another_kind_of_user_touches_no_unit(admin, make_user):
    deputy = make_user("deputy", Role.DEPUTY_CNO)

    assert post(admin, f"/api/users/{deputy.pk}/deactivate", {}).status_code == 200
    assert audit("unit.changed") == []
