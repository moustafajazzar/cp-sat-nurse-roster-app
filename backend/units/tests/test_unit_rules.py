"""The defaults a new unit's rules start from, held to the solver's own contract."""
import dataclasses
import json
from pathlib import Path

import pytest
from django.apps import apps as django_apps
from nurse_roster.problem import DepartmentRules
from nurse_roster.request import load_request

from accounts.models import Role
from conftest import migration, post
from hospital.models import HospitalSettings, Shift
from hospital.rules import general_block
from units.models import Unit
from units.rules import DEFAULT_UNIT_RULES, unit_block, with_shift_codes

pytestmark = pytest.mark.django_db

ER_REQUEST = Path(__file__).parents[3] / "solver/tests/fixtures/er_request_2026-03.json"


def er_request() -> dict:
    return json.loads(ER_REQUEST.read_text())


@pytest.fixture
def cno(client_of):
    return client_of("chief", Role.CNO)


def new_unit(cno, name="ER") -> Unit:
    response = post(cno, "/api/units", {"name": name, "manager_id": None})
    assert response.status_code == 201, response.content
    return Unit.objects.get(pk=response.json()["id"])


def rename(cno, code, new_code):
    change_shift(cno, code, code=new_code)


def change_shift(cno, current, /, **changes):
    shift = Shift.objects.get(code=current)
    body = {"code": shift.code, "display_name": shift.display_name, "hours": shift.hours,
            "start_time": shift.start_time.strftime("%H:%M"), "end_time": shift.end_time.strftime("%H:%M"),
            "colour": shift.colour, "active": shift.active} | changes
    response = cno.put(f"/api/hospital/shifts/{shift.pk}", body, content_type="application/json")
    assert response.status_code == 200, response.content


def solve_input(unit: Unit) -> dict:
    """What a solve input holds, as far as the rules go: the catalogue's hours and these blocks."""
    shift_hours = {shift.code: shift.hours for shift in Shift.objects.filter(active=True)}
    request = er_request()
    for nurse in request["nurses"]:
        # Her history and her allowed shifts are left to the solve input to carry through a rename.
        nurse["previous"], nurse["allowed_shifts"] = [], None
    return request | {"version": 2, "general": general_block(HospitalSettings.load(), shift_hours=shift_hours),
                      "department": unit_block(unit)}


# ---- the solver's contract ------------------------------------------------------------------------
def test_the_rules_are_every_field_of_the_solvers_block_but_the_units_name():
    fields = {field.name for field in dataclasses.fields(DepartmentRules)}

    assert set(DEFAULT_UNIT_RULES) == fields - {"name"}


def test_the_defaults_are_the_reviewed_set_the_solver_was_shaped_on():
    """The ER sample predates full runs, skills and the overtime share; their cap is the spec's (full-runs.md), 3, a unit
    starts with no skill needs and the needs hard (skills-per-shift-day.md), and shares its overtime with 12 hours free and
    the carry moving a share by 12 hours at most (overtime-share.md)."""
    reviewed = er_request()["department"]
    del reviewed["name"]

    assert DEFAULT_UNIT_RULES == reviewed | {"max_full_runs": 3, "skill_needs": [], "skill_needs_hard": True,
                                             "share_overtime": True, "overtime_tolerance": 12, "overtime_carry_cap": 12}


# ---- seeding --------------------------------------------------------------------------------------
def test_a_new_unit_starts_from_the_defaults_with_the_review_flag_up(cno):
    unit = new_unit(cno)

    assert unit.rules == DEFAULT_UNIT_RULES
    assert unit.rules_need_review is True


def test_a_new_units_block_is_one_the_solver_accepts_under_the_units_name(cno):
    unit = new_unit(cno, "Cardiac care")

    department = load_request(solve_input(unit)).problem.department

    assert department.name == "Cardiac care"
    assert department.shifts == ["D", "N"]


def test_a_new_unit_allows_three_full_runs_a_month_and_its_block_carries_them(cno):
    unit = new_unit(cno)

    assert unit.rules["max_full_runs"] == 3
    assert load_request(solve_input(unit)).problem.department.max_full_runs == 3


def test_a_unit_from_before_the_full_run_cap_gets_three_and_keeps_the_rest(cno):
    unit = new_unit(cno)
    unit.rules = {key: value for key, value in unit.rules.items() if key != "max_full_runs"} | {"max_weekends": 5}
    unit.rules_need_review = False
    unit.save()

    migration("units", "0004_max_full_runs").add(django_apps, None)

    unit.refresh_from_db()
    assert unit.rules == DEFAULT_UNIT_RULES | {"max_weekends": 5} and unit.rules_need_review is False


def test_a_new_unit_has_no_skill_needs_and_the_needs_hard(cno):
    department = load_request(solve_input(new_unit(cno))).problem.department

    assert (department.skill_needs, department.skill_needs_hard) == ([], True)


def test_a_unit_from_before_skill_needs_gets_none_and_the_needs_hard_and_keeps_the_rest(cno):
    unit = new_unit(cno)
    unit.rules = {key: value for key, value in unit.rules.items() if not key.startswith("skill_")} | {"max_weekends": 5}
    unit.rules_need_review = False
    unit.save()

    migration("units", "0005_skill_needs").add(django_apps, None)

    unit.refresh_from_db()
    assert unit.rules == DEFAULT_UNIT_RULES | {"max_weekends": 5} and unit.rules_need_review is False


OVERTIME_SHARE = ("share_overtime", "overtime_tolerance", "overtime_carry_cap")


def test_a_new_unit_shares_its_overtime_with_12_hours_free_and_its_block_carries_it(cno):
    department = load_request(solve_input(new_unit(cno))).problem.department

    assert (department.share_overtime, department.overtime_tolerance, department.overtime_carry_cap) == (True, 12, 12)


def test_a_unit_from_before_the_overtime_share_gets_the_defaults_and_keeps_the_rest(cno):
    unit = new_unit(cno)
    unit.rules = {key: value for key, value in unit.rules.items() if key not in OVERTIME_SHARE} | {"max_weekends": 5}
    unit.rules_need_review = False
    unit.save()

    migration("units", "0006_overtime_share").add(django_apps, None)

    unit.refresh_from_db()
    assert unit.rules == DEFAULT_UNIT_RULES | {"max_weekends": 5} and unit.rules_need_review is False


def test_the_overtime_share_migration_leaves_a_unit_that_already_has_it_alone(cno):
    unit = new_unit(cno)
    unit.rules |= {"share_overtime": False, "overtime_tolerance": 8, "overtime_carry_cap": 0}
    unit.save()

    migration("units", "0006_overtime_share").add(django_apps, None)

    unit.refresh_from_db()
    assert [unit.rules[key] for key in OVERTIME_SHARE] == [False, 8, 0]


def test_units_do_not_share_their_rules(cno):
    first, second = new_unit(cno, "ER"), new_unit(cno, "ICU")
    first.rules["shifts"].append("A")

    assert second.rules == DEFAULT_UNIT_RULES
    assert DEFAULT_UNIT_RULES["shifts"] == ["D", "N"]


def test_a_unit_created_after_a_rename_starts_from_the_shifts_new_code(cno):
    rename(cno, "D", "DAY")
    rename(cno, "DAY", "DY")  # A chain ends at the shift's current code.
    rename(cno, "N", "NT")

    unit = new_unit(cno)

    assert unit.rules["shifts"] == ["DY", "NT"]
    assert unit.rules["day_shift"] == "DY" and unit.rules["night_shift"] == "NT"
    assert unit.rules["fair_shifts"] == ["DY", "NT"]
    assert [headcount["shift"] for headcount in unit.rules["headcounts"]] == ["DY", "NT"]
    assert unit.rules_need_review is True
    assert load_request(solve_input(unit)).problem.department.shifts == ["DY", "NT"]


def test_a_shift_that_went_back_to_its_code_seeds_under_it(cno):
    rename(cno, "D", "DAY")
    rename(cno, "DAY", "D")

    assert new_unit(cno).rules == DEFAULT_UNIT_RULES


def test_a_retired_shift_is_left_out_and_the_solver_refuses_until_the_manager_reviews(cno):
    settings = HospitalSettings.load()
    settings.rules["forbidden_successions"] = []
    settings.save()
    change_shift(cno, "D", active=False)

    unit = new_unit(cno)

    assert unit.rules["shifts"] == unit.rules["fair_shifts"] == ["N"]
    assert [headcount["shift"] for headcount in unit.rules["headcounts"]] == ["N"]
    assert unit.rules["day_shift"] == "" and unit.rules["night_shift"] == "N"
    assert unit.rules_need_review is True
    with pytest.raises(ValueError, match="day_shift"):
        load_request(solve_input(unit))


def test_every_default_shift_retired_still_makes_a_unit(cno):
    settings = HospitalSettings.load()
    settings.rules["forbidden_successions"] = []
    settings.save()
    change_shift(cno, "D", active=False)
    change_shift(cno, "N", active=False)

    unit = new_unit(cno)

    assert unit.rules["shifts"] == unit.rules["fair_shifts"] == unit.rules["headcounts"] == []
    assert unit.rules["day_shift"] == unit.rules["night_shift"] == ""
    assert unit.rules_need_review is True
    with pytest.raises(ValueError, match="department.shifts"):
        load_request(solve_input(unit))


def test_a_rename_after_the_seed_can_be_carried_by_the_same_function():
    carried = with_shift_codes(DEFAULT_UNIT_RULES | {"headcounts": [
        DEFAULT_UNIT_RULES["headcounts"][0] | {"also_counts": ["N"]}, DEFAULT_UNIT_RULES["headcounts"][1]]},
        {"N": "NT"})

    assert carried["night_shift"] == "NT" and carried["day_shift"] == "D"
    assert carried["headcounts"][0]["also_counts"] == ["NT"]
    assert carried["headcounts"][1]["shift"] == "NT"
    assert DEFAULT_UNIT_RULES["night_shift"] == "N"
