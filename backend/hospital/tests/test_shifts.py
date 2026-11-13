import copy
import json
from pathlib import Path

import pytest
from django.core.management import call_command
from nurse_roster.request import load_request

from accounts.models import Role
from audit.models import AuditEntry
from hospital.models import HospitalSettings
from hospital.rules import DEFAULT_RULES, general_block
from hospital.tests.test_rules import version_2

pytestmark = pytest.mark.django_db

SHIFTS = "/api/hospital/shifts"
RULES = "/api/hospital/rules"
ER_REQUEST = Path(__file__).parents[3] / "solver/tests/fixtures/er_request_2026-03.json"
NEW = {"code": "T", "display_name": "Twilight", "hours": 6, "start_time": "16:00", "end_time": "22:00",
       "colour": "#123abc", "active": True}


def er_request() -> dict:
    return json.loads(ER_REQUEST.read_text())


def create(client, **changes):
    return client.post(SHIFTS, NEW | changes, content_type="application/json")


def editable(shift: dict) -> dict:
    return {key: value for key, value in shift.items() if key not in ("id", "renames")}


def update(client, current_code: str, /, **changes):
    shift = next(shift for shift in client.get(SHIFTS).json() if shift["code"] == current_code)
    return client.put(f"{SHIFTS}/{shift['id']}", editable(shift) | changes, content_type="application/json")


def refused_fields(response) -> set[tuple]:
    assert response.status_code == 422, response.content
    return {tuple(error["loc"][2:]) for error in response.json()["detail"]}


def saved_pairs() -> list:
    return HospitalSettings.load().rules["forbidden_successions"]


# ---- seeding --------------------------------------------------------------------------------------
def test_first_start_seeds_the_shifts_the_hospital_rules_name(monkeypatch, signed_in):
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "one-time-password")

    call_command("bootstrap")

    shifts = signed_in(Role.CNO).get(SHIFTS).json()
    assert {shift["code"]: shift["hours"] for shift in shifts} == er_request()["general"]["shift_hours"]
    assert [shift["code"] for shift in shifts] == ["D", "N", "A", "B", "C"]
    assert all(shift["active"] and shift["display_name"] and shift["renames"] == [] for shift in shifts)
    named = {code for pair in DEFAULT_RULES["forbidden_successions"] for code in pair}
    assert named <= {shift["code"] for shift in shifts}


def test_an_installation_older_than_the_catalogue_gets_it_on_its_next_start(make_user, signed_in):
    make_user("admin", Role.SYSTEM_ADMIN)

    call_command("bootstrap")

    assert len(signed_in(Role.CNO).get(SHIFTS).json()) == 5


def test_later_starts_do_not_bring_back_a_renamed_shift(make_user, signed_in, catalogue):
    make_user("admin", Role.SYSTEM_ADMIN)
    client = signed_in(Role.CNO)
    update(client, "D", code="DAY")

    call_command("bootstrap")

    assert [shift["code"] for shift in client.get(SHIFTS).json()] == ["DAY", "N", "A", "B", "C"]


def test_the_catalogues_hours_and_the_seeded_rules_pass_the_solver(catalogue, signed_in):
    shifts = signed_in(Role.CNO).get(SHIFTS).json()
    hours = {shift["code"]: shift["hours"] for shift in shifts if shift["active"]}

    general = general_block(HospitalSettings.load(), shift_hours=hours)

    assert load_request(version_2(er_request()) | {"general": general}).problem.general.shift_hours == hours


# ---- the list and the form ------------------------------------------------------------------------
def test_the_cno_adds_a_shift(signed_in, catalogue):
    client = signed_in(Role.CNO)

    response = create(client)

    assert response.status_code == 201
    assert editable(response.json()) == NEW
    assert client.get(SHIFTS).json()[-1] == response.json()


def test_the_cno_changes_every_field_but_the_code_without_a_rename(signed_in, catalogue):
    client = signed_in(Role.CNO)
    changes = {"display_name": "Long day", "hours": 11, "start_time": "06:30", "end_time": "17:30",
               "colour": "#000000", "active": True}

    response = update(client, "D", **changes)

    assert response.status_code == 200
    assert editable(response.json()) == {"code": "D"} | changes
    assert response.json()["renames"] == []
    assert saved_pairs() == DEFAULT_RULES["forbidden_successions"]


def test_a_colour_is_stored_in_one_spelling(signed_in, catalogue):
    assert create(signed_in(Role.CNO), colour="#ABCDEF").json()["colour"] == "#abcdef"


def test_an_unknown_shift_is_404(signed_in, catalogue):
    response = signed_in(Role.CNO).put(f"{SHIFTS}/019a0000-0000-7000-8000-000000000000", NEW,
                                       content_type="application/json")

    assert response.status_code == 404


@pytest.mark.parametrize("changes, field", [
    ({"code": ""}, "code"),
    ({"code": "=SUM"}, "code"),
    ({"code": "A B"}, "code"),
    ({"code": "X" * 9}, "code"),
    ({"code": "L"}, "code"),
    ({"code": "l"}, "code"),
    ({"display_name": "   "}, "display_name"),
    ({"display_name": "x" * 101}, "display_name"),
    ({"display_name": "a\x00b"}, "display_name"),
    ({"hours": 0}, "hours"),
    ({"hours": 25}, "hours"),
    ({"hours": 7.5}, "hours"),
    ({"hours": "8"}, "hours"),
    ({"start_time": "24:00"}, "start_time"),
    ({"end_time": "7am"}, "end_time"),
    ({"colour": "red"}, "colour"),
    ({"colour": "#12345"}, "colour"),
    ({"active": "yes"}, "active"),
    ({"extra": 1}, "extra"),
])
def test_a_refusal_names_the_field(signed_in, catalogue, changes, field):
    assert refused_fields(create(signed_in(Role.CNO), **changes)) == {(field,)}


@pytest.mark.parametrize("code", ["D", "d"])
def test_a_code_cannot_be_used_twice_in_any_case(signed_in, catalogue, code):
    client = signed_in(Role.CNO)

    assert refused_fields(create(client, code=code)) == {("code",)}
    assert refused_fields(update(client, "N", code=code)) == {("code",)}


@pytest.mark.parametrize("hours", [1, 24])
def test_what_this_api_accepts_the_solver_accepts_too(signed_in, catalogue, hours):
    client = signed_in(Role.CNO)
    assert create(client, code="a-1_Z", hours=hours).status_code == 201
    shift_hours = {shift["code"]: shift["hours"] for shift in client.get(SHIFTS).json()}

    general = general_block(HospitalSettings.load(), shift_hours=shift_hours)

    assert load_request(version_2(er_request()) | {"general": general}).problem.general.shift_hours["a-1_Z"] == hours


# ---- a code rename --------------------------------------------------------------------------------
def test_a_rename_is_recorded_with_the_old_name_the_new_name_and_the_date(signed_in, catalogue):
    client = signed_in(Role.CNO)

    response = update(client, "D", code="DAY")

    assert response.status_code == 200 and response.json()["code"] == "DAY"
    (rename,) = response.json()["renames"]
    assert rename["old_code"] == "D" and rename["new_code"] == "DAY"
    assert rename["at"].endswith("+03:00")
    assert client.get(SHIFTS).json()[0] == response.json()


def test_each_rename_of_a_shift_is_kept_in_order(signed_in, catalogue):
    client = signed_in(Role.CNO)
    update(client, "D", code="DAY")
    update(client, "DAY", code="LONG")

    renames = update(client, "LONG", code="D").json()["renames"]

    assert [(rename["old_code"], rename["new_code"]) for rename in renames] == [
        ("D", "DAY"), ("DAY", "LONG"), ("LONG", "D")]


def test_a_new_display_name_is_free_and_records_no_rename(signed_in, catalogue):
    assert update(signed_in(Role.CNO), "D", display_name="Sunshine").json()["renames"] == []


def test_a_code_another_shift_used_to_hold_cannot_be_taken(signed_in, catalogue):
    # The record says that "D" in an old history means this shift, so it cannot also mean another.
    client = signed_in(Role.CNO)
    update(client, "D", code="DAY")

    assert refused_fields(create(client, code="D")) == {("code",)}
    assert refused_fields(create(client, code="d")) == {("code",)}
    assert refused_fields(update(client, "N", code="D")) == {("code",)}


def test_a_rename_carries_through_the_forbidden_successions_and_the_rules_page_still_opens(signed_in, catalogue):
    client = signed_in(Role.CNO)
    AuditEntry.objects.all().delete()

    update(client, "N", code="NIGHT")

    answer = client.get(RULES)
    assert answer.status_code == 200
    assert answer.json()["shift_codes"] == ["D", "NIGHT", "A", "B", "C"]
    pairs = answer.json()["rules"]["forbidden_successions"]
    assert ["NIGHT", "D"] in pairs and ["NIGHT", "A"] in pairs and ["N", "D"] not in pairs
    assert answer.json()["needs_review"] is True
    entry = AuditEntry.objects.get(action="hospital_rules.changed")
    assert ["N", "D"] in entry.before["forbidden_successions"]
    assert ["NIGHT", "D"] in entry.after["forbidden_successions"]


def test_a_rename_that_no_pair_names_leaves_the_rules_alone(signed_in, catalogue):
    client = signed_in(Role.CNO)
    assert client.put(RULES, copy.deepcopy(DEFAULT_RULES) | {"forbidden_successions": []},
                      content_type="application/json").status_code == 200
    AuditEntry.objects.all().delete()

    update(client, "N", code="NIGHT")

    assert not AuditEntry.objects.filter(action="hospital_rules.changed").exists()


# ---- retiring -------------------------------------------------------------------------------------
def test_a_shift_a_forbidden_succession_names_cannot_be_retired(signed_in, catalogue):
    client = signed_in(Role.CNO)

    response = update(client, "N", active=False)

    assert refused_fields(response) == {("active",)}
    assert "forbidden succession" in response.json()["detail"][0]["msg"]
    assert client.get(RULES).status_code == 200
    assert "N" in client.get(RULES).json()["shift_codes"]


def test_once_no_pair_names_it_a_shift_retires_and_leaves_the_rules_choices(signed_in, catalogue):
    client = signed_in(Role.CNO)
    assert client.put(RULES, copy.deepcopy(DEFAULT_RULES) | {"forbidden_successions": [["D", "A"]]},
                      content_type="application/json").status_code == 200

    response = update(client, "N", active=False)

    assert response.status_code == 200 and response.json()["active"] is False
    assert client.get(RULES).json()["shift_codes"] == ["D", "A", "B", "C"]
    assert [shift["code"] for shift in client.get(SHIFTS).json()] == ["D", "N", "A", "B", "C"]


def test_a_retired_shift_cannot_be_named_by_a_new_pair(signed_in, catalogue):
    client = signed_in(Role.CNO)
    client.put(RULES, copy.deepcopy(DEFAULT_RULES) | {"forbidden_successions": []}, content_type="application/json")
    update(client, "N", active=False)

    response = client.put(RULES, copy.deepcopy(DEFAULT_RULES) | {"forbidden_successions": [["N", "D"]]},
                          content_type="application/json")

    assert refused_fields(response) == {("forbidden_successions", 0, 0)}


def test_a_retired_shift_comes_back(signed_in, catalogue):
    client = signed_in(Role.CNO)
    client.put(RULES, copy.deepcopy(DEFAULT_RULES) | {"forbidden_successions": []}, content_type="application/json")
    update(client, "N", active=False)

    assert update(client, "N", active=True).json()["active"] is True
    assert "N" in client.get(RULES).json()["shift_codes"]


# ---- the audit log --------------------------------------------------------------------------------
def test_adding_is_audited(signed_in, catalogue):
    client = signed_in(Role.CNO, username="the-cno")
    AuditEntry.objects.all().delete()

    create(client)

    entry = AuditEntry.objects.get()
    assert entry.action == "shift.created" and entry.actor.username == "the-cno" and entry.target == "T"
    assert entry.after == NEW and entry.ip


def test_a_change_is_audited_with_only_what_changed(signed_in, catalogue):
    client = signed_in(Role.CNO)
    AuditEntry.objects.all().delete()

    update(client, "D", code="DAY", hours=11)

    entry = AuditEntry.objects.get(action="shift.changed")
    assert entry.target == "DAY"
    assert entry.before == {"code": "D", "hours": 12} and entry.after == {"code": "DAY", "hours": 11}


def test_saving_with_no_change_writes_no_entry(signed_in, catalogue):
    client = signed_in(Role.CNO)
    AuditEntry.objects.all().delete()

    assert update(client, "D").status_code == 200

    assert not AuditEntry.objects.exists()


# ---- who may ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("role", list(Role))
def test_every_role_reads_the_catalogue(signed_in, catalogue, role):
    assert len(signed_in(role).get(SHIFTS).json()) == 5


@pytest.mark.parametrize("role", [role for role in Role if role != Role.CNO])
def test_only_the_cno_changes_it(signed_in, catalogue, role):
    client = signed_in(role)
    shift = client.get(SHIFTS).json()[0]

    assert create(client).status_code == 403
    assert client.put(f"{SHIFTS}/{shift['id']}", editable(shift), content_type="application/json").status_code == 403
