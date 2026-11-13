import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from nurse_roster.request import load_request

from accounts.models import Role
from audit.models import AuditEntry
from hospital.catalogue import ramadan_dates

pytestmark = pytest.mark.django_db

RAMADAN = "/api/hospital/ramadan"
ER_REQUEST = Path(__file__).parents[3] / "solver/tests/fixtures/er_request_2026-03.json"
# Umm al-Qura's Ramadan 1447 runs from 18 February to 19 March 2026.
MARCH_2026 = [f"2026-03-{day:02d}" for day in range(1, 20)]
FEBRUARY_2026 = [f"2026-02-{day:02d}" for day in range(18, 29)]


def year(client, value: int = 2026) -> dict[str, dict]:
    return {row["month"]: row for row in client.get(RAMADAN, {"year": value}).json()}


def fix(client, month: str, **body):
    return client.put(f"{RAMADAN}/{month}", body, content_type="application/json")


def refused_fields(response) -> set[tuple]:
    assert response.status_code == 422, response.content
    return {tuple(error["loc"][2:]) for error in response.json()["detail"]}


def test_a_month_left_alone_shows_the_days_derived_from_the_calendar(signed_in):
    months = year(signed_in(Role.CNO))

    assert list(months) == [f"2026-{month:02d}" for month in range(1, 13)]
    assert months["2026-03"] == {"month": "2026-03", "source": "derived", "dates": MARCH_2026,
                                 "derived_dates": MARCH_2026}
    assert months["2026-02"]["dates"] == FEBRUARY_2026
    assert months["2026-07"] == {"month": "2026-07", "source": "derived", "dates": [], "derived_dates": []}


def test_what_is_derived_is_what_the_solver_derives_for_the_same_month(signed_in):
    raw = json.loads(ER_REQUEST.read_text())
    raw.pop("ramadan_days", None)
    solver = load_request(raw).problem.ramadan_days

    derived = year(signed_in(Role.CNO), 2026)[raw["month"]]["dates"]

    assert derived == [f"{raw['month']}-{day + 1:02d}" for day in solver]


def test_the_cno_fixes_a_month_as_a_range(signed_in):
    client = signed_in(Role.CNO)

    response = fix(client, "2026-03", first="2026-03-01", last="2026-03-20")

    assert response.status_code == 200
    expected = [f"2026-03-{day:02d}" for day in range(1, 21)]
    assert response.json() == {"month": "2026-03", "source": "fixed", "dates": expected, "derived_dates": MARCH_2026}
    assert year(client)["2026-03"] == response.json()
    assert year(client)["2026-02"]["source"] == "derived"


def test_the_cno_fixes_a_month_as_a_list_in_any_order(signed_in):
    client = signed_in(Role.CNO)

    response = fix(client, "2026-03", dates=["2026-03-03", "2026-03-01"])

    assert response.json()["source"] == "fixed" and response.json()["dates"] == ["2026-03-01", "2026-03-03"]


def test_an_empty_list_fixes_a_month_to_no_days_at_all(signed_in):
    # The sighting can move Ramadan out of a month the calendar puts its last day in.
    response = fix(signed_in(Role.CNO), "2026-03", dates=[])

    assert response.json()["source"] == "fixed" and response.json()["dates"] == []


def test_a_month_the_calendar_gives_none_can_be_fixed_to_some(signed_in):
    response = fix(signed_in(Role.CNO), "2026-04", dates=["2026-04-01"])

    assert response.json()["dates"] == ["2026-04-01"] and response.json()["derived_dates"] == []


def test_fixing_again_replaces_and_clearing_returns_to_the_calendar(signed_in):
    client = signed_in(Role.CNO)
    fix(client, "2026-03", dates=["2026-03-01"])
    assert fix(client, "2026-03", dates=["2026-03-02"]).json()["dates"] == ["2026-03-02"]

    response = client.delete(f"{RAMADAN}/2026-03")

    assert response.status_code == 200
    assert response.json() == {"month": "2026-03", "source": "derived", "dates": MARCH_2026,
                               "derived_dates": MARCH_2026}


def test_what_a_month_says_reaches_the_solver_as_those_days(signed_in):
    client = signed_in(Role.CNO)
    raw = json.loads(ER_REQUEST.read_text())
    month = raw["month"]
    fix(client, month, first=f"{month}-02", last=f"{month}-04")

    days = ramadan_dates(month)

    raw["ramadan_days"] = [day.isoformat() for day in days]
    assert days == [date(2026, 3, 2) + timedelta(days=offset) for offset in range(3)]
    assert load_request(raw).problem.ramadan_days == [1, 2, 3]


@pytest.mark.parametrize("body, field", [
    ({"first": "2026-02-28", "last": "2026-03-05"}, ("first",)),
    ({"first": "2026-03-01", "last": "2026-04-01"}, ("last",)),
    ({"first": "2026-03-05", "last": "2026-03-01"}, ("last",)),
    ({"first": "2026-03-05"}, ("last",)),
    ({"last": "2026-03-05"}, ("first",)),
    ({}, ("dates",)),
    ({"first": "2026-03-01", "last": "2026-03-02", "dates": []}, ("dates",)),
    ({"dates": ["2026-03-01", "2026-03-01"]}, ("dates",)),
    ({"dates": ["2026-03-01", "2026-04-01"]}, ("dates", 1)),
    ({"dates": ["2026-03-32"]}, ("dates", 0)),
    ({"dates": "2026-03-01"}, ("dates",)),
    ({"first": "2026-03-01", "last": "tomorrow"}, ("last",)),
    ({"extra": 1}, ("extra",)),
])
def test_a_refusal_names_the_field(signed_in, body, field):
    client = signed_in(Role.CNO)

    assert refused_fields(fix(client, "2026-03", **body)) == {field}
    assert year(client)["2026-03"]["source"] == "derived"


@pytest.mark.parametrize("month", ["2026-13", "2026-3", "march", "1900-01", "2100-01"])
def test_a_month_outside_the_calendar_is_refused(signed_in, month):
    client = signed_in(Role.CNO)

    assert fix(client, month, dates=[]).status_code == 422
    assert client.delete(f"{RAMADAN}/{month}").status_code == 422


@pytest.mark.parametrize("value", [1924, 2077, "soon"])
def test_a_year_outside_the_calendar_is_refused(signed_in, value):
    assert signed_in(Role.CNO).get(RAMADAN, {"year": value}).status_code == 422


def test_every_change_is_audited_and_nothing_changing_is_not(signed_in):
    client = signed_in(Role.CNO, username="the-cno")
    AuditEntry.objects.all().delete()

    client.delete(f"{RAMADAN}/2026-03")
    fix(client, "2026-03", dates=["2026-03-01"])
    fix(client, "2026-03", first="2026-03-01", last="2026-03-01")
    client.delete(f"{RAMADAN}/2026-03")

    cleared, fixed = AuditEntry.objects.all()
    assert fixed.action == "ramadan_dates.changed" and fixed.target == "2026-03"
    assert fixed.actor.username == "the-cno"
    assert fixed.before == {"source": "derived", "dates": MARCH_2026}
    assert fixed.after == {"source": "fixed", "dates": ["2026-03-01"]}
    assert cleared.before == fixed.after and cleared.after == fixed.before


@pytest.mark.parametrize("role", list(Role))
def test_every_role_reads_them(signed_in, role):
    assert year(signed_in(role))["2026-03"]["dates"] == MARCH_2026


@pytest.mark.parametrize("role", [role for role in Role if role != Role.CNO])
def test_only_the_cno_changes_them(signed_in, role):
    client = signed_in(role)

    assert fix(client, "2026-03", dates=[]).status_code == 403
    assert client.delete(f"{RAMADAN}/2026-03").status_code == 403
