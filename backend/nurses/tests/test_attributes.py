"""A nurse's rostering attributes and opening values: who sets them, who reads them, what the log may hold, and what
becomes of allowed shifts when the unit's shifts change and when a nurse moves between units."""
import dataclasses
import json
from pathlib import Path
from uuid import uuid4

import pytest
from nurse_roster.problem import Nurse
from nurse_roster.request import load_request

from accounts.models import Role, User
from audit.models import AuditEntry
from conftest import PASSWORD, post
from hospital.catalogue import seed_catalogue
from hospital.models import HospitalSettings, Shift
from hospital.rules import general_block
from nurses.models import NurseProfile
from nurses.rostering import nurse_block
from nurses.tests.conftest import NURSES, added, make_unit
from units.models import Unit
from units.rules import DEFAULT_UNIT_RULES, unit_block

pytestmark = pytest.mark.django_db

ER_REQUEST = Path(__file__).parents[3] / "solver/tests/fixtures/er_request_2026-03.json"
THREE_SHIFTS = DEFAULT_UNIT_RULES | {"shifts": ["D", "N", "A"]}
ATTRIBUTES = {"team_leader": True, "score": 0.61, "ramadan_reduced": True, "breastfeeding": True, "ytd_overtime": 77,
              "hours_balance": -13, "night_balance": 1.5, "friday_balance": -2.25, "overtime_balance": -6.5,
              "allowed_shifts": ["D"]}


@pytest.fixture(autouse=True)
def catalogue(db):
    seed_catalogue()


@pytest.fixture
def mona(client_of):
    return client_of("mona", Role.UNIT_MANAGER)


@pytest.fixture
def er(mona):
    return make_unit("ER", User.objects.get(username="mona"))


@pytest.fixture
def omar(client_of):
    return client_of("omar", Role.UNIT_MANAGER)


@pytest.fixture
def ccu(omar):
    return make_unit("CCU", User.objects.get(username="omar"))


@pytest.fixture
def amal(cno, er) -> str:
    return added(cno, er)["nurse"]["id"]


def listed(client, unit) -> dict:
    response = client.get(f"/api/units/{unit.pk}/nurses")
    assert response.status_code == 200, response.content
    return {row["hospital_id"]: row for row in response.json()}


def save(client, unit, nurse_id, **changes):
    return client.put(f"/api/units/{unit.pk}/nurses/{nurse_id}/attributes", ATTRIBUTES | changes,
                      content_type="application/json")


def save_shifts(client, unit, nurse_id, allowed):
    return client.put(f"/api/units/{unit.pk}/nurses/{nurse_id}/allowed-shifts", {"allowed_shifts": allowed},
                      content_type="application/json")


def transfer(client, nurse_id, unit, month):
    response = post(client, f"{NURSES}/{nurse_id}/transfer", {"unit_id": str(unit.pk), "from_month": month})
    assert response.status_code == 200, response.content


def save_rules(client, unit, **changes):
    return client.put(f"/api/units/{unit.pk}/rules",
                      {"rules": DEFAULT_UNIT_RULES | changes, "request_cap": 3, "preference_cap": 10, "default_time_limit": 60},
                      content_type="application/json")


def refused_fields(response) -> set[tuple]:
    assert response.status_code == 422, response.content
    return {tuple(error["loc"][2:]) for error in response.json()["detail"]}


def solve_input(unit, *nurses) -> dict:
    request = json.loads(ER_REQUEST.read_text())
    for row in request["nurses"]:
        row["previous"], row["allowed_shifts"] = [], None
    request["nurses"] += [nurse_block(nurse, placement) for nurse, placement in nurses]
    shift_hours = {shift.code: shift.hours for shift in Shift.objects.filter(active=True)}
    return request | {"version": 2, "general": general_block(HospitalSettings.load(), shift_hours=shift_hours),
                      "department": unit_block(unit)}


def loaded_nurse(unit, nurse_id, placement=None):
    nurse = NurseProfile.objects.get(pk=nurse_id)
    problem = load_request(solve_input(unit, (nurse, placement or nurse.placements.last()))).problem
    return next(row for row in problem.nurses if row.id == nurse.user.username)


# ---- defaults -------------------------------------------------------------------------------------
def test_a_new_nurse_starts_from_the_solvers_defaults_with_opening_values_at_zero(cno, er):
    nurse = NurseProfile.objects.get(pk=added(cno, er)["nurse"]["id"])
    defaults = {field.name: field.default for field in dataclasses.fields(Nurse)}

    for name in ("team_leader", "score", "ramadan_reduced", "breastfeeding", "ytd_overtime", "hours_balance",
                 "night_balance", "friday_balance", "overtime_balance"):
        assert getattr(nurse, name) == defaults[name], name
    assert nurse.ytd_overtime == nurse.hours_balance == nurse.night_balance == nurse.friday_balance == 0
    assert nurse.overtime_balance == 0
    assert nurse.placements.get().allowed_shifts is None


def test_the_list_says_what_the_page_needs(mona, er, amal):
    row = listed(mona, er)["h123"]

    assert row["id"] == amal and row["full_name"] == "Amal Saleh" and row["gender"] == "F" and row["is_active"] is True
    assert row["score"] == 0.8 and row["allowed_shifts"] is None and row["allowed_shifts_to_check"] is False
    assert row["joins_from"] is None and row["leaves_from"] is None


# ---- saving, and the solver ------------------------------------------------------------------------
def test_the_manager_saves_everything_and_a_solve_input_receives_it(mona, er, amal):
    response = save(mona, er, amal)

    assert response.status_code == 200, response.content
    assert {name: response.json()[name] for name in ATTRIBUTES} == ATTRIBUTES
    assert listed(mona, er)["h123"]["score"] == 0.61
    nurse = loaded_nurse(er, amal)
    assert (nurse.team_leader, nurse.score, nurse.ramadan_reduced, nurse.breastfeeding) == (True, 0.61, True, True)
    assert (nurse.ytd_overtime, nurse.hours_balance, nurse.night_balance, nurse.friday_balance) == (77, -13, 1.5, -2.25)
    assert nurse.overtime_balance == -6.5
    assert nurse.allowed_shifts == ["D"]


@pytest.mark.parametrize("changes", [
    {"score": 0}, {"score": 1},
    {"ytd_overtime": 0}, {"ytd_overtime": 10 ** 6},
    {"hours_balance": 10 ** 6}, {"hours_balance": -10 ** 6},
    {"night_balance": 1000}, {"night_balance": -1000}, {"friday_balance": 1000}, {"friday_balance": -1000},
    {"overtime_balance": 10 ** 6}, {"overtime_balance": -10 ** 6}, {"overtime_balance": 0.25},
    {"allowed_shifts": None}, {"allowed_shifts": ["D", "N"]},
])
def test_what_the_api_accepts_at_its_limits_the_solver_accepts(mona, er, amal, changes):
    assert save(mona, er, amal, **changes).status_code == 200
    loaded_nurse(er, amal)


def test_a_defaulted_nurse_also_passes(mona, er, amal):
    nurse = loaded_nurse(er, amal)

    assert nurse.allowed_shifts is None and nurse.score == 0.8


@pytest.mark.parametrize("changes, field", [
    ({"score": 1.01}, ("score",)),
    ({"score": -0.01}, ("score",)),
    ({"score": "0.5"}, ("score",)),
    ({"score": True}, ("score",)),
    ({"team_leader": "yes"}, ("team_leader",)),
    ({"ramadan_reduced": 1}, ("ramadan_reduced",)),
    ({"breastfeeding": None}, ("breastfeeding",)),
    ({"ytd_overtime": -1}, ("ytd_overtime",)),
    ({"ytd_overtime": 10 ** 6 + 1}, ("ytd_overtime",)),
    ({"ytd_overtime": 1.5}, ("ytd_overtime",)),
    ({"hours_balance": 10 ** 6 + 1}, ("hours_balance",)),
    ({"hours_balance": 0.5}, ("hours_balance",)),
    ({"night_balance": 1001}, ("night_balance",)),
    ({"friday_balance": -1001}, ("friday_balance",)),
    ({"overtime_balance": 10 ** 6 + 1}, ("overtime_balance",)),
    ({"overtime_balance": -10 ** 6 - 0.5}, ("overtime_balance",)),
    ({"overtime_balance": "3"}, ("overtime_balance",)),
    ({"overtime_balance": True}, ("overtime_balance",)),
    ({"overtime_balance": None}, ("overtime_balance",)),
    ({"allowed_shifts": []}, ("allowed_shifts",)),
    ({"allowed_shifts": ["D", "D"]}, ("allowed_shifts",)),
    ({"allowed_shifts": "D"}, ("allowed_shifts",)),
    ({"allowed_shifts": ["A"]}, ("allowed_shifts", 0)),
    ({"allowed_shifts": ["D", "ZZ"]}, ("allowed_shifts", 1)),
    ({"gender": "M"}, ("gender",)),
])
def test_a_refusal_names_its_field_and_saves_nothing(mona, er, amal, changes, field):
    fields = refused_fields(save(mona, er, amal, **changes))

    assert any(found[:len(field)] == field for found in fields), fields
    assert listed(mona, er)["h123"]["score"] == 0.8
    assert not AuditEntry.objects.filter(action="nurse.attributes_changed").exists()


@pytest.mark.parametrize("field, written", [("night_balance", "1.5"), ("overtime_balance", "-6.5")])
def test_infinity_is_refused(mona, er, amal, field, written):
    text = json.dumps(ATTRIBUTES).replace(f'"{field}": {written}', f'"{field}": Infinity')

    response = mona.put(f"/api/units/{er.pk}/nurses/{amal}/attributes", text, content_type="application/json")

    assert (field,) in refused_fields(response)


def test_saving_with_no_change_writes_nothing(mona, er, amal):
    save(mona, er, amal)
    save(mona, er, amal)

    assert AuditEntry.objects.filter(action="nurse.attributes_changed").count() == 1


def test_the_overtime_balance_alone_changed_is_logged_as_the_other_opening_values_are(mona, er, amal):
    save(mona, er, amal)

    assert save(mona, er, amal, overtime_balance=12).status_code == 200

    entries = AuditEntry.objects.filter(action="nurse.attributes_changed")
    assert entries.count() == 2
    assert {(entry.target, entry.before, entry.after) for entry in entries} == {("h123", None, None)}
    assert NurseProfile.objects.get(pk=amal).overtime_balance == 12


# ---- who ------------------------------------------------------------------------------------------
def test_another_units_manager_is_refused_on_every_endpoint(omar, ccu, er, amal):
    assert omar.get(f"/api/units/{er.pk}/nurses").status_code == 403
    assert save(omar, er, amal).status_code == 403
    assert save_shifts(omar, er, amal, ["D"]).status_code == 403


def test_a_nurse_of_another_unit_cannot_be_reached_through_ones_own(omar, ccu, er, amal, mona):
    assert save(omar, ccu, amal).status_code == 403
    assert save_shifts(omar, ccu, amal, ["D"]).status_code == 403
    assert listed(mona, er)["h123"]["score"] == 0.8


def test_a_manager_is_refused_alike_whether_the_nurse_or_unit_exists_or_not(mona, er):
    assert save(mona, er, uuid4()).status_code == 403
    assert mona.get(f"/api/units/{uuid4()}/nurses").status_code == 403


def test_a_manager_with_no_unit_is_refused(client_of, er, amal):
    idle = client_of("idle", Role.UNIT_MANAGER)

    assert idle.get(f"/api/units/{er.pk}/nurses").status_code == 403
    assert save(idle, er, amal).status_code == 403


@pytest.mark.parametrize("role", [Role.CNO, Role.DEPUTY_CNO])
def test_the_cno_and_the_deputy_read_but_cannot_write(client_of, mona, er, role):
    other_cno = client_of("adder", Role.CNO) if role == Role.DEPUTY_CNO else None
    client = client_of("reader", role)
    amal = added(other_cno or client, er)["nurse"]["id"]
    save(mona, er, amal)

    assert listed(client, er)["h123"]["score"] == 0.61
    assert listed(client, er)["h123"]["overtime_balance"] == -6.5
    assert save(client, er, amal, score=0.1).status_code == 403
    assert save_shifts(client, er, amal, None).status_code == 403
    assert listed(client, er)["h123"]["score"] == 0.61


def test_a_hospital_wide_role_gets_404_for_a_unit_that_is_not_there(cno):
    assert cno.get(f"/api/units/{uuid4()}/nurses").status_code == 404


def test_a_nurse_gets_403_on_attributes_their_own_included(client_of, cno, er, amal):
    client = client_of("x", Role.STAFF_NURSE)
    nurse_user = User.objects.get(username="h123")
    nurse_user.set_password(PASSWORD)
    nurse_user.must_change_password = False
    nurse_user.save()
    post(client, "/api/auth/logout", {})
    assert post(client, "/api/auth/login", {"username": "h123", "password": PASSWORD}).status_code == 200

    assert client.get(f"/api/units/{er.pk}/nurses").status_code == 403
    assert client.get(f"/api/units/{er.pk}/rules").status_code == 403
    assert save(client, er, amal).status_code == 403
    assert save_shifts(client, er, amal, None).status_code == 403
    mine = client.get("/api/nurses/my-unit").json()
    assert set(mine) == {"id", "name"}
    session = json.dumps(client.get("/api/auth/session").json())
    assert not any(name in session for name in ATTRIBUTES)


def test_the_system_admin_gets_403(client_of, er, amal):
    assert client_of("it", Role.SYSTEM_ADMIN).get(f"/api/units/{er.pk}/nurses").status_code == 403


def test_a_deactivated_nurse_can_be_changed_because_a_refused_save_may_name_them(mona, cno, er, amal):
    assert post(cno, f"{NURSES}/{amal}/end-date", {}).status_code in (405, 422)
    assert cno.put(f"{NURSES}/{amal}/end-date", {"end_date": "2026-02-01"}, content_type="application/json").status_code == 200
    assert listed(mona, er)["h123"]["is_active"] is False

    assert save(mona, er, amal, allowed_shifts=None).status_code == 200


# ---- a manager with a nurse profile ----------------------------------------------------------------
def test_a_manager_sets_their_own_and_the_log_shows_whose_hand(mona, cno, er):
    manager = User.objects.get(username="mona")
    given = post(cno, f"{NURSES}/managers", {"user_id": str(manager.pk), "unit_id": str(er.pk), "gender": "F",
                                             "start_date": "2026-01-10"})
    assert given.status_code == 201, given.content

    assert save(mona, er, given.json()["id"]).status_code == 200

    assert listed(mona, er)["mona"]["score"] == 0.61
    assert listed(cno, er)["mona"]["score"] == 0.61, "The CNO can read what they set."
    entry = AuditEntry.objects.get(action="nurse.attributes_changed")
    assert entry.actor == manager and entry.target == "mona"


# ---- the audit log --------------------------------------------------------------------------------
def test_the_log_says_whose_changed_and_nothing_more(mona, er, amal, client_of):
    save(mona, er, amal)
    save_shifts(mona, er, amal, ["N"])

    entries = AuditEntry.objects.filter(action="nurse.attributes_changed").order_by("id")

    assert [entry.target for entry in entries] == ["h123", "h123"]
    assert all(entry.before is None and entry.after is None for entry in entries)
    admin = client_of("it", Role.SYSTEM_ADMIN)
    text = admin.get("/api/audit/entries").content.decode()
    for leaked in ('"team_leader"', '"score"', "breastfeeding", "ramadan", "0.61", '"allowed_shifts"', "hours_balance",
                   "ytd_overtime", "night_balance", "friday_balance", "overtime_balance", "-6.5"):
        assert leaked not in text, leaked


def test_nothing_else_an_attribute_goes_through_reaches_the_log(mona, cno, er, ccu, amal):
    """Rules narrowing a nurse's list, a transfer carrying it, and a rename: counts and codes, no person's values."""
    save(mona, er, amal, allowed_shifts=["D", "N"])
    save_rules(mona, er, shifts=["D", "N", "A"], headcounts=DEFAULT_UNIT_RULES["headcounts"])
    transfer(cno, amal, ccu, "2027-01")
    assert save_rules(mona, er, shifts=["N", "A"], day_shift="A", fair_shifts=["N"],
                      headcounts=[DEFAULT_UNIT_RULES["headcounts"][1]]).status_code == 200

    everything = json.dumps([[entry.action, entry.target, entry.before, entry.after] for entry in AuditEntry.objects.all()])

    assert '"allowed_shifts"' not in everything
    for name in ("team_leader", '"score"', "breastfeeding", "ramadan_reduced"):
        assert name not in everything
    last = AuditEntry.objects.filter(action="unit_rules.changed").latest("id")
    assert set(last.after) == {"shifts", "fair_shifts", "headcounts", "day_shift"}, "How many lists it touched says too much."
    assert "h123" not in json.dumps(last.after)


# ---- the staff list -------------------------------------------------------------------------------
def test_the_staff_list_carries_no_attribute(mona, cno, er, amal):
    save(mona, er, amal)

    text = cno.get(f"{NURSES}/export").content.decode("utf-8-sig")

    header = text.splitlines()[0]
    assert not any(word in header.lower() for word in ("score", "team", "allowed", "breast", "ramadan", "overtime", "balance"))
    assert "0.61" not in text


def test_the_cno_list_of_nurses_carries_no_attribute(mona, cno, er, amal):
    save(mona, er, amal)

    row = cno.get(NURSES).json()[0]

    assert not set(row) & set(ATTRIBUTES)


# ---- allowed shifts when the unit's shifts change ---------------------------------------------------
def test_allowed_shifts_keep_to_the_units_shifts(mona, er, amal):
    assert ("allowed_shifts", 0) in refused_fields(save_shifts(mona, er, amal, ["A"]))

    save_rules(mona, er, shifts=["D", "N", "A"])

    assert save_shifts(mona, er, amal, ["A"]).status_code == 200


def test_a_list_loses_a_shift_the_unit_stops_working(mona, er, amal):
    save_rules(mona, er, shifts=["D", "N", "A"])
    save_shifts(mona, er, amal, ["D", "A"])

    assert save_rules(mona, er).status_code == 200

    assert listed(mona, er)["h123"]["allowed_shifts"] == ["D"]
    loaded_nurse(Unit.objects.get(pk=er.pk), amal)


def test_a_rule_save_is_refused_rather_than_leave_a_nurse_with_no_shift(mona, er, amal):
    save_rules(mona, er, shifts=["D", "N", "A"])
    save_shifts(mona, er, amal, ["A"])

    response = save_rules(mona, er)

    assert ("rules", "shifts") in refused_fields(response)
    why = response.json()["detail"][0]["msg"]
    assert "Amal Saleh (h123)" in why and "Change their allowed shifts first" in why
    assert listed(mona, er)["h123"]["allowed_shifts"] == ["A"]
    assert "A" in Unit.objects.get(pk=er.pk).rules["shifts"]


def test_the_refusal_names_a_few_and_counts_the_rest(mona, cno, er):
    save_rules(mona, er, shifts=["D", "N", "A"])
    for number in range(5):
        save_shifts(mona, er, added(cno, er, f"h{number}", full_name=f"Nurse {number}")["nurse"]["id"], ["A"])

    assert "and 2 more" in save_rules(mona, er).json()["detail"][0]["msg"]


def test_a_nurse_with_any_shift_stops_nothing_and_stays_as_they_are(mona, er, amal):
    save_rules(mona, er, shifts=["D", "N", "A"])

    assert save_rules(mona, er).status_code == 200

    assert listed(mona, er)["h123"]["allowed_shifts"] is None


def test_a_nurse_due_to_join_counts_when_the_units_shifts_change(mona, omar, cno, er, ccu, amal):
    save_rules(omar, ccu, shifts=["D", "N", "A"])
    transfer(cno, amal, ccu, "2030-01")
    save_shifts(omar, ccu, amal, ["A"])

    assert ("rules", "shifts") in refused_fields(save_rules(omar, ccu))


def test_a_nurse_who_has_left_does_not_count_when_the_shifts_change(mona, cno, er, ccu, amal, on_day):
    save_rules(mona, er, shifts=["D", "N", "A"])
    save_shifts(mona, er, amal, ["A"])
    on_day(2030, 1, 15)
    transfer(cno, amal, ccu, "2030-01")

    assert save_rules(mona, er).status_code == 200


# ---- allowed shifts through a transfer -------------------------------------------------------------
def test_a_transfer_carries_the_list_as_far_as_the_new_unit_works_the_same_shifts(mona, omar, cno, er, ccu, amal):
    save_rules(mona, er, shifts=["D", "N", "A"])
    save_shifts(mona, er, amal, ["D", "A"])

    transfer(cno, amal, ccu, "2030-01")

    row = listed(omar, ccu)["h123"]
    assert row["allowed_shifts"] == ["D"] and row["allowed_shifts_to_check"] is False
    assert row["joins_from"] == "2030-01"
    assert listed(mona, er)["h123"]["allowed_shifts"] == ["D", "A"], "Their current unit's months keep its own list."
    assert listed(mona, er)["h123"]["leaves_from"] == "2030-01"


def test_a_list_with_nothing_in_common_empties_and_asks_the_new_manager_to_look(mona, omar, cno, er, ccu, amal):
    save_rules(mona, er, shifts=["D", "N", "A"])
    save_shifts(mona, er, amal, ["A"])

    transfer(cno, amal, ccu, "2030-01")

    row = listed(omar, ccu)["h123"]
    assert row["allowed_shifts"] is None and row["allowed_shifts_to_check"] is True
    assert listed(mona, er)["h123"]["allowed_shifts_to_check"] is False


def test_choosing_lowers_the_flag_even_when_any_shift_is_the_answer(omar, mona, cno, er, ccu, amal):
    save_rules(mona, er, shifts=["D", "N", "A"])
    save_shifts(mona, er, amal, ["A"])
    transfer(cno, amal, ccu, "2030-01")

    assert save_shifts(omar, ccu, amal, None).status_code == 200

    assert listed(omar, ccu)["h123"]["allowed_shifts_to_check"] is False
    assert AuditEntry.objects.filter(action="nurse.attributes_changed", actor__username="omar").exists()


def test_any_shift_stays_any_shift_through_a_transfer(omar, cno, ccu, amal):
    transfer(cno, amal, ccu, "2030-01")

    row = listed(omar, ccu)["h123"]
    assert row["allowed_shifts"] is None and row["allowed_shifts_to_check"] is False


def test_each_units_list_reaches_its_own_solve_input(mona, omar, cno, er, ccu, amal):
    save_rules(omar, ccu, shifts=["D", "N", "A"])
    save_shifts(mona, er, amal, ["D"])
    transfer(cno, amal, ccu, "2030-01")
    save_shifts(omar, ccu, amal, ["A"])
    nurse = NurseProfile.objects.get(pk=amal)
    now, then = nurse.placements.all()

    assert loaded_nurse(Unit.objects.get(pk=er.pk), amal, now).allowed_shifts == ["D"]
    assert loaded_nurse(Unit.objects.get(pk=ccu.pk), amal, then).allowed_shifts == ["A"]


def test_replacing_a_transfer_keeps_what_the_new_manager_chose(omar, cno, er, ccu, amal, mona):
    transfer(cno, amal, ccu, "2030-03")
    save_shifts(omar, ccu, amal, ["N"])

    transfer(cno, amal, ccu, "2030-01")

    assert listed(omar, ccu)["h123"]["allowed_shifts"] == ["N"]
    assert listed(omar, ccu)["h123"]["joins_from"] == "2030-01"


def test_a_transfer_from_this_month_over_a_placement_that_began_this_month_carries_it(mona, cno, er, ccu, omar, on_day):
    amal = added(cno, er, start_date="2070-01-01")["nurse"]["id"]
    save_shifts(mona, er, amal, ["D"])
    month = NurseProfile.objects.get(pk=amal).placements.get().from_month.strftime("%Y-%m")

    transfer(cno, amal, ccu, month)

    assert listed(omar, ccu)["h123"]["allowed_shifts"] == ["D"]


def test_a_transfer_carries_the_list_of_the_unit_they_are_coming_from(mona, omar, cno, er, ccu, amal):
    save_shifts(mona, er, amal, ["D"])
    transfer(cno, amal, ccu, "2030-01")
    save_shifts(omar, ccu, amal, ["N"])
    third = make_unit("ICU")

    transfer(cno, amal, third, "2030-01")

    placements = NurseProfile.objects.get(pk=amal).placements.all()
    assert [placement.allowed_shifts for placement in placements] == [["D"], ["D"]], "CCU's never began, so ER's it is."


def test_a_placement_that_has_begun_this_month_is_where_they_are_coming_from(mona, omar, cno, er, ccu, amal, on_day):
    save_shifts(mona, er, amal, ["D"])
    on_day(2030, 1, 15)
    transfer(cno, amal, ccu, "2030-01")
    save_shifts(omar, ccu, amal, ["N"])
    third = make_unit("ICU")

    transfer(cno, amal, third, "2030-01")

    assert NurseProfile.objects.get(pk=amal).placements.last().allowed_shifts == ["N"]


def test_a_later_transfer_in_a_chain_carries_the_list_of_the_one_before_it(omar, cno, ccu, amal):
    transfer(cno, amal, ccu, "2030-01")
    save_shifts(omar, ccu, amal, ["N"])
    third = make_unit("ICU")

    transfer(cno, amal, third, "2030-06")

    assert NurseProfile.objects.get(pk=amal).placements.last().allowed_shifts == ["N"]


def test_coming_back_after_the_unit_dropped_a_shift_brings_no_stale_list(mona, cno, er, ccu, amal, on_day):
    save_rules(mona, er, shifts=["D", "N", "A"])
    save_shifts(mona, er, amal, ["A"])
    on_day(2030, 1, 15)
    transfer(cno, amal, ccu, "2030-01")
    save_rules(mona, er)  # A goes, with no nurse in force here to stop it.

    transfer(cno, amal, er, "2030-01")

    row = listed(mona, er)["h123"]
    assert row["allowed_shifts"] is None and row["allowed_shifts_to_check"] is True
    loaded_nurse(Unit.objects.get(pk=er.pk), amal)


def test_a_past_placement_keeps_what_the_unit_still_works(mona, cno, er, ccu, amal, on_day):
    save_rules(mona, er, shifts=["D", "N", "A"])
    save_shifts(mona, er, amal, ["D", "A"])
    on_day(2030, 1, 15)
    transfer(cno, amal, ccu, "2030-01")
    save_rules(mona, er)

    first = NurseProfile.objects.get(pk=amal).placements.first()
    assert first.allowed_shifts == ["D"] and first.allowed_shifts_to_check is False


def test_a_transfer_cancelled_before_it_began_takes_the_nurse_off_the_new_managers_list(omar, cno, er, ccu, amal):
    transfer(cno, amal, ccu, "2030-01")
    assert cno.put(f"{NURSES}/{amal}/end-date", {"end_date": "2029-06-30"}, content_type="application/json").status_code == 200

    assert save_shifts(omar, ccu, amal, ["D"]).status_code == 403
    assert listed(omar, ccu) == {}


def test_coming_back_keeps_the_list_of_the_unit_they_never_really_left(mona, cno, er, ccu, amal):
    save_shifts(mona, er, amal, ["D"])
    transfer(cno, amal, ccu, "2030-01")

    transfer(cno, amal, er, "2030-01")

    assert listed(mona, er)["h123"]["allowed_shifts"] == ["D"]


def test_a_rule_change_reaches_every_placement_here_in_a_chain(mona, cno, er, ccu, amal):
    transfer(cno, amal, ccu, "2030-01")
    transfer_back = post(cno, f"{NURSES}/{amal}/transfer", {"unit_id": str(er.pk), "from_month": "2030-06"})
    assert transfer_back.status_code == 200, transfer_back.content

    save_shifts(mona, er, amal, ["N"])

    here = [placement.allowed_shifts for placement in NurseProfile.objects.get(pk=amal).placements.all()
            if placement.unit_id == er.pk]
    assert here == [["N"], ["N"]]


# ---- a nurse due to join ----------------------------------------------------------------------------
def test_the_new_manager_reads_a_joiner_and_chooses_only_their_shifts(omar, mona, cno, er, ccu, amal):
    save(mona, er, amal)
    transfer(cno, amal, ccu, "2030-01")

    row = listed(omar, ccu)["h123"]
    assert row["score"] == 0.61, "The nurse's own attributes travel with them, and this manager will roster them."

    assert save(omar, ccu, amal, score=0.1).status_code == 403
    assert save_shifts(omar, ccu, amal, ["N"]).status_code == 200
    assert listed(omar, ccu)["h123"]["score"] == 0.61


def test_their_current_manager_keeps_the_rest_until_they_arrive(mona, omar, cno, er, ccu, amal):
    transfer(cno, amal, ccu, "2030-01")

    assert save(mona, er, amal, score=0.33).status_code == 200

    assert listed(omar, ccu)["h123"]["score"] == 0.33


def test_on_arriving_it_all_passes_to_the_new_manager(mona, omar, cno, er, ccu, amal, on_day):
    transfer(cno, amal, ccu, "2030-01")
    on_day(2030, 1, 1)

    assert save(omar, ccu, amal, allowed_shifts=None, score=0.2).status_code == 200
    assert save(mona, er, amal, allowed_shifts=None).status_code == 403
    assert mona.get(f"/api/units/{er.pk}/nurses").json() == []


def test_a_joiners_shifts_are_checked_against_the_unit_they_are_joining(omar, mona, cno, er, ccu, amal):
    save_rules(mona, er, shifts=["D", "N", "A"])
    transfer(cno, amal, ccu, "2030-01")

    assert ("allowed_shifts", 0) in refused_fields(save_shifts(omar, ccu, amal, ["A"]))


def test_a_new_nurse_starting_later_is_their_managers_at_once(mona, cno, er):
    amal = added(cno, er, start_date="2070-01-01")["nurse"]["id"]

    assert listed(mona, er)["h123"]["joins_from"] is None
    assert save(mona, er, amal).status_code == 200
