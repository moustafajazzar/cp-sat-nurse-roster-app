import pytest

from accounts.models import Role
from audit.models import AuditEntry

pytestmark = pytest.mark.django_db

HOLIDAYS = "/api/hospital/public-holidays"
NATIONAL_DAY = {"date": "2026-09-23", "name": "National Day"}


def add(client, **changes):
    return client.post(HOLIDAYS, NATIONAL_DAY | changes, content_type="application/json")


def in_year(client, year: int) -> list[dict]:
    return client.get(HOLIDAYS, {"year": year}).json()


def refused_fields(response) -> set[tuple]:
    assert response.status_code == 422, response.content
    return {tuple(error["loc"][2:]) for error in response.json()["detail"]}


def test_a_holiday_shows_under_its_own_year_in_date_order(signed_in):
    client = signed_in(Role.CNO)

    created = add(client)
    add(client, date="2026-02-22", name="Founding Day")
    add(client, date="2027-02-22", name="Founding Day")

    assert created.status_code == 201
    assert created.json()["date"] == "2026-09-23" and created.json()["name"] == "National Day"
    assert [row["date"] for row in in_year(client, 2026)] == ["2026-02-22", "2026-09-23"]
    assert [row["date"] for row in in_year(client, 2027)] == ["2027-02-22"]
    assert in_year(client, 2028) == []


def test_a_date_cannot_be_entered_twice(signed_in):
    client = signed_in(Role.CNO)
    add(client)

    response = add(client, name="Something else")

    assert refused_fields(response) == {("date",)}
    assert "already" in response.json()["detail"][0]["msg"]
    assert len(in_year(client, 2026)) == 1


def test_a_holiday_cannot_be_moved_onto_another(signed_in):
    client = signed_in(Role.CNO)
    add(client)
    other = add(client, date="2026-02-22", name="Founding Day").json()

    response = client.put(f"{HOLIDAYS}/{other['id']}", NATIONAL_DAY, content_type="application/json")

    assert refused_fields(response) == {("date",)}


def test_the_cno_corrects_a_holiday(signed_in):
    client = signed_in(Role.CNO)
    row = add(client).json()

    response = client.put(f"{HOLIDAYS}/{row['id']}", {"date": "2026-09-24", "name": "National Day (observed)"},
                          content_type="application/json")

    assert response.status_code == 200
    assert in_year(client, 2026) == [{"id": row["id"], "date": "2026-09-24", "name": "National Day (observed)"}]


def test_the_cno_removes_a_holiday_and_may_enter_the_date_again(signed_in):
    client = signed_in(Role.CNO)
    row = add(client).json()

    assert client.delete(f"{HOLIDAYS}/{row['id']}").status_code == 204

    assert in_year(client, 2026) == []
    assert add(client).status_code == 201


@pytest.mark.parametrize("changes, field", [
    ({"date": "2026-02-30"}, "date"),
    ({"date": "20260923"}, "date"),
    ({"date": "2026-09-23T00:00:00"}, "date"),
    ({"date": 1790000000}, "date"),
    ({"date": "1900-01-01"}, "date"),
    ({"date": "2100-01-01"}, "date"),
    ({"name": "   "}, "name"),
    ({"name": "x" * 101}, "name"),
    ({"name": "a\x00b"}, "name"),
    ({"extra": 1}, "extra"),
])
def test_a_refusal_names_the_field(signed_in, changes, field):
    assert refused_fields(add(signed_in(Role.CNO), **changes)) == {(field,)}


@pytest.mark.parametrize("year", [1924, 2077, "soon"])
def test_a_year_outside_the_calendar_is_refused(signed_in, year):
    assert signed_in(Role.CNO).get(HOLIDAYS, {"year": year}).status_code == 422


def test_unknown_holidays_are_404(signed_in):
    client = signed_in(Role.CNO)
    path = f"{HOLIDAYS}/019a0000-0000-7000-8000-000000000000"

    assert client.put(path, NATIONAL_DAY, content_type="application/json").status_code == 404
    assert client.delete(path).status_code == 404


def test_every_change_is_audited_and_saving_no_change_is_not(signed_in):
    client = signed_in(Role.CNO, username="the-cno")
    AuditEntry.objects.all().delete()

    row = add(client).json()
    client.put(f"{HOLIDAYS}/{row['id']}", NATIONAL_DAY, content_type="application/json")
    client.put(f"{HOLIDAYS}/{row['id']}", NATIONAL_DAY | {"name": "Saudi National Day"},
               content_type="application/json")
    client.delete(f"{HOLIDAYS}/{row['id']}")

    removed, changed, added = AuditEntry.objects.all()
    assert added.action == "public_holiday.created" and added.target == "2026-09-23"
    assert added.actor.username == "the-cno" and added.after == NATIONAL_DAY
    assert changed.action == "public_holiday.changed"
    assert changed.before == {"name": "National Day"} and changed.after == {"name": "Saudi National Day"}
    assert removed.action == "public_holiday.removed"
    assert removed.before == {"date": "2026-09-23", "name": "Saudi National Day"}


@pytest.mark.parametrize("role", list(Role))
def test_every_role_reads_them(signed_in, role):
    client = signed_in(role)

    assert client.get(HOLIDAYS, {"year": 2026}).status_code == 200


@pytest.mark.parametrize("role", [role for role in Role if role != Role.CNO])
def test_only_the_cno_changes_them(signed_in, make_user, role):
    from hospital.models import PublicHoliday
    row = PublicHoliday.objects.create(date="2026-09-23", name="National Day")
    client = signed_in(role)
    path = f"{HOLIDAYS}/{row.pk}"

    assert add(client).status_code == 403
    assert client.put(path, NATIONAL_DAY, content_type="application/json").status_code == 403
    assert client.delete(path).status_code == 403
