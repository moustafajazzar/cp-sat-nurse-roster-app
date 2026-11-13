"""What the CNO's catalogue does to a unit's rules: a shift in use cannot be retired, and a new code carries through."""
import copy
import json
from pathlib import Path

import pytest
from nurse_roster.request import load_request

from accounts.models import Role, User
from audit.models import AuditEntry
from hospital.models import HospitalSettings, Shift
from hospital.rules import general_block
from nurses.models import NurseProfile, Placement
from nurses.rostering import nurse_block
from units.models import Unit
from units.rules import DEFAULT_UNIT_RULES, unit_block

pytestmark = pytest.mark.django_db

ER_REQUEST = Path(__file__).parents[3] / "solver/tests/fixtures/er_request_2026-03.json"


@pytest.fixture
def cno(client_of):
    return client_of("chief", Role.CNO)


def unit(name="ER", **rules) -> Unit:
    return Unit.objects.create(name=name, rules=copy.deepcopy(DEFAULT_UNIT_RULES) | rules)


def change_shift(cno, current, /, **changes):
    shift = Shift.objects.get(code=current)
    body = {"code": shift.code, "display_name": shift.display_name, "hours": shift.hours,
            "start_time": shift.start_time.strftime("%H:%M"), "end_time": shift.end_time.strftime("%H:%M"),
            "colour": shift.colour, "active": shift.active} | changes
    return cno.put(f"/api/hospital/shifts/{shift.pk}", body, content_type="application/json")


def without_pairs():
    settings = HospitalSettings.load()
    settings.rules["forbidden_successions"] = []
    settings.save()


def refusal(response) -> tuple[tuple, str]:
    assert response.status_code == 422, response.content
    error = response.json()["detail"][0]
    return tuple(error["loc"][2:]), error["msg"]


# ---- retiring -------------------------------------------------------------------------------------
def test_retiring_a_shift_a_unit_works_is_refused_with_the_unit_named(cno):
    without_pairs()
    unit("ER")

    where, why = refusal(change_shift(cno, "D", active=False))

    assert where == ("active",)
    assert "ER" in why
    assert Shift.objects.get(code="D").active is True


def test_every_unit_using_it_is_named(cno):
    without_pairs()
    unit("ER")
    unit("ICU")

    assert "ER, ICU" in refusal(change_shift(cno, "D", active=False))[1]


@pytest.mark.parametrize("rules", [
    {"shifts": ["A"], "night_shift": "D", "day_shift": "B"},
    {"shifts": ["A"], "night_shift": "B", "day_shift": "D"},
])
def test_a_night_or_day_shift_named_outside_the_units_shifts_counts(cno, rules):
    without_pairs()
    unit("ER", **rules, headcounts=[], fair_shifts=[])

    assert "ER" in refusal(change_shift(cno, "D", active=False))[1]


def test_an_archived_unit_does_not_stop_a_retirement(cno):
    without_pairs()
    unit("Old", archived=False)
    Unit.objects.filter(name="Old").update(archived=True)

    assert change_shift(cno, "D", active=False).status_code == 200


def test_a_shift_no_unit_uses_can_be_retired(cno):
    without_pairs()
    unit("ER")

    assert change_shift(cno, "C", active=False).status_code == 200


def test_once_the_manager_has_taken_it_out_it_can_be_retired(cno, client_of):
    without_pairs()
    mona = client_of("mona", Role.UNIT_MANAGER)
    er = unit("ER", shifts=["D", "N", "A"])
    Unit.objects.filter(pk=er.pk).update(manager=User.objects.get(username="mona"))
    assert "ER" in refusal(change_shift(cno, "A", active=False))[1]

    saved = mona.put(f"/api/units/{er.pk}/rules", {"rules": DEFAULT_UNIT_RULES, "request_cap": 3, "preference_cap": 10, "default_time_limit": 60},
                     content_type="application/json")

    assert saved.status_code == 200, saved.content
    assert change_shift(cno, "A", active=False).status_code == 200


def test_the_rule_is_on_the_retirement_only_so_other_changes_to_the_shift_pass(cno):
    unit("ER")

    assert change_shift(cno, "D", display_name="Long day").status_code == 200


def test_a_rename_and_a_retirement_in_one_save_are_refused_under_the_old_code(cno):
    without_pairs()
    unit("ER")

    assert "ER" in refusal(change_shift(cno, "D", code="DAY", active=False))[1]
    assert Shift.objects.filter(code="D").exists()
    assert Unit.objects.get(name="ER").rules == DEFAULT_UNIT_RULES


# ---- renaming -------------------------------------------------------------------------------------
def test_a_rename_carries_through_every_unit_in_every_place_a_block_names_a_shift(cno):
    also = [{**DEFAULT_UNIT_RULES["headcounts"][0], "also_counts": ["N"]}, DEFAULT_UNIT_RULES["headcounts"][1]]
    unit("ER", headcounts=also)
    unit("ICU")

    assert change_shift(cno, "N", code="NT").status_code == 200

    for name in ("ER", "ICU"):
        rules = Unit.objects.get(name=name).rules
        assert rules["shifts"] == rules["fair_shifts"] == ["D", "NT"]
        assert rules["night_shift"] == "NT"
        assert [headcount["shift"] for headcount in rules["headcounts"]] == ["D", "NT"]
    assert Unit.objects.get(name="ER").rules["headcounts"][0]["also_counts"] == ["NT"]


def test_a_rename_reaches_an_archived_unit(cno):
    Unit.objects.filter(pk=unit("Old").pk).update(archived=True)

    change_shift(cno, "N", code="NT")

    assert Unit.objects.get(name="Old").rules["night_shift"] == "NT"


def test_a_rename_is_audited_for_each_unit_it_changes_and_leaves_the_flag_alone(cno):
    reviewed = unit("ER")
    Unit.objects.filter(pk=reviewed.pk).update(rules_need_review=False)
    unit("ICU")
    unit("Other", shifts=["A"], night_shift="A", day_shift="B", headcounts=[], fair_shifts=[])

    change_shift(cno, "N", code="NT")

    entries = AuditEntry.objects.filter(action="unit_rules.changed")
    assert {entry.target for entry in entries} == {"ER", "ICU"}
    assert entries.first().actor.username == "chief"
    assert entries.first().before["night_shift"] == "N" and entries.first().after["night_shift"] == "NT"
    assert Unit.objects.get(name="ER").rules_need_review is False
    assert Unit.objects.get(name="ICU").rules_need_review is True


def test_a_rename_carries_through_allowed_shifts_on_every_placement(cno, make_user):
    er = unit("ER")
    user = make_user("h1")
    nurse = NurseProfile.objects.create(user=user, gender="F", start_date="2026-01-01")
    Placement.objects.create(nurse=nurse, unit=er, from_month="2025-01-01", allowed_shifts=["D", "N"])
    Placement.objects.create(nurse=nurse, unit=er, from_month="2030-01-01", allowed_shifts=["N"])
    other = NurseProfile.objects.create(user=make_user("h2"), gender="F", start_date="2026-01-01")
    Placement.objects.create(nurse=other, unit=er, from_month="2025-01-01", allowed_shifts=["D"])
    anyone = NurseProfile.objects.create(user=make_user("h3"), gender="F", start_date="2026-01-01")
    Placement.objects.create(nurse=anyone, unit=er, from_month="2025-01-01")

    assert change_shift(cno, "N", code="NT").status_code == 200

    assert [placement.allowed_shifts for placement in nurse.placements.all()] == [["D", "NT"], ["NT"]]
    assert other.placements.get().allowed_shifts == ["D"]
    assert anyone.placements.get().allowed_shifts is None


def test_a_shift_is_renamed_atomically_with_what_carries_it(cno, monkeypatch):
    unit("ER")

    def fails(*_):
        raise RuntimeError("boom")
    monkeypatch.setattr("hospital.catalogue_api.carry_rename_through_allowed_shifts", fails)
    cno.raise_request_exception = False

    assert change_shift(cno, "N", code="NT").status_code == 500
    assert Shift.objects.filter(code="N").exists()
    assert Unit.objects.get(name="ER").rules["night_shift"] == "N"


def test_after_a_rename_the_solver_still_takes_the_unit_and_its_nurses(cno, make_user):
    er = unit("ER")
    nurse = NurseProfile.objects.create(user=make_user("h1"), gender="F", start_date="2026-01-01")
    placement = Placement.objects.create(nurse=nurse, unit=er, from_month="2025-01-01", allowed_shifts=["N"])

    change_shift(cno, "N", code="NT")

    er.refresh_from_db()
    placement.refresh_from_db()
    request = json.loads(ER_REQUEST.read_text())
    for row in request["nurses"]:
        row["previous"], row["allowed_shifts"] = [], None
    request["nurses"].append(nurse_block(nurse, placement))
    shift_hours = {shift.code: shift.hours for shift in Shift.objects.filter(active=True)}
    loaded = load_request(request | {"version": 2,
                                     "general": general_block(HospitalSettings.load(), shift_hours=shift_hours),
                                     "department": unit_block(er)}).problem
    assert loaded.department.night_shift == "NT"
    assert loaded.nurses[-1].allowed_shifts == ["NT"]
