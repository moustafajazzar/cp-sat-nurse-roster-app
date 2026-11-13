"""A unit's rules over the API: who reads and changes them, what is refused, and that what is saved is what the
solver takes. Retirement and renames, which the CNO's catalogue drives, are in test_shift_guards.py."""
import copy
import dataclasses
import json
from pathlib import Path
from uuid import uuid4

import pytest
from nurse_roster.problem import DepartmentRules, Headcount
from nurse_roster.request import load_request

from accounts.models import Role, User
from audit.models import AuditEntry
from hospital.models import HospitalSettings, Shift
from hospital.rules import general_block
from ops.models import TechnicalSettings
from units.models import Unit
from units.rules import DEFAULT_UNIT_RULES, unit_block
from units.rules_api import HeadcountAnswer, HeadcountRule, UnitRules, UnitRulesAnswer

pytestmark = pytest.mark.django_db

ER_REQUEST = Path(__file__).parents[3] / "solver/tests/fixtures/er_request_2026-03.json"


def rules_of(unit) -> str:
    return f"/api/units/{unit.pk}/rules"


def body(request_cap=3, default_time_limit=60, preference_cap=10, **changes) -> dict:
    return {"rules": copy.deepcopy(DEFAULT_UNIT_RULES) | changes, "request_cap": request_cap,
            "preference_cap": preference_cap, "default_time_limit": default_time_limit}


def put(client, unit, payload):
    return client.put(rules_of(unit), payload, content_type="application/json")


def refused_fields(response) -> set[tuple]:
    assert response.status_code == 422, response.content
    return {tuple(error["loc"][2:]) for error in response.json()["detail"]}


def message(response) -> str:
    assert response.status_code == 422, response.content
    return response.json()["detail"][0]["msg"]


def solve_input(unit) -> dict:
    """The ER sample, whose nurses are not this test's concern, under this app's own blocks."""
    request = json.loads(ER_REQUEST.read_text())
    for nurse in request["nurses"]:
        nurse["previous"], nurse["allowed_shifts"] = [], None
    shift_hours = {shift.code: shift.hours for shift in Shift.objects.filter(active=True)}
    return request | {"version": 2, "general": general_block(HospitalSettings.load(), shift_hours=shift_hours),
                      "department": unit_block(unit)}


def headcount(shift, also=(), low=1, high=2) -> dict:
    return {"shift": shift, "weekday_min": low, "weekday_max": high, "weekend_min": low, "weekend_max": high,
            "also_counts": list(also)}


@pytest.fixture
def mona(client_of):
    return client_of("mona", Role.UNIT_MANAGER)


@pytest.fixture
def er(mona):
    return Unit.objects.create(name="ER", manager=User.objects.get(username="mona"), rules=copy.deepcopy(DEFAULT_UNIT_RULES))


@pytest.fixture
def omar(client_of):
    return client_of("omar", Role.UNIT_MANAGER)


@pytest.fixture
def ccu(omar):
    return Unit.objects.create(name="CCU", manager=User.objects.get(username="omar"), rules=copy.deepcopy(DEFAULT_UNIT_RULES))


# ---- the solver's contract ------------------------------------------------------------------------
@pytest.mark.parametrize("schema", [UnitRules, UnitRulesAnswer])
def test_both_schemas_are_every_field_of_the_solvers_block_but_its_name(schema):
    assert set(schema.model_fields) == {field.name for field in dataclasses.fields(DepartmentRules)} - {"name"}


@pytest.mark.parametrize("schema", [HeadcountRule, HeadcountAnswer])
def test_both_headcount_schemas_are_the_solvers(schema):
    assert set(schema.model_fields) == {field.name for field in dataclasses.fields(Headcount)}


def test_what_the_form_saves_is_what_a_solve_input_receives(mona, er):
    saved = body(score_target=0.65, night_rest_days=1, night_rest_hard=False, max_switches=4, carry_cap=1.5,
                 score_balance_mode="spread", switches_hard=True)["rules"]

    assert put(mona, er, body(**saved)).status_code == 200

    er.refresh_from_db()
    department = load_request(solve_input(er)).problem.department
    assert department.name == "ER" and department.score_target == 0.65 and department.night_rest_days == 1
    assert department.score_balance_mode == "spread" and department.switches_hard is True


@pytest.mark.parametrize("changes", [
    {"night_rest_days": 7, "max_consecutive_shifts": 1, "double_cycle_rest": 0},
    {"night_rest_days": 0, "max_consecutive_shifts": 27, "double_cycle_rest": 7},
    {"max_consecutive_shifts": 62, "double_cycle_rest": 0},
    {"max_consecutive_shifts": 0, "double_cycle_rest": 1000, "night_rest_days": 0},
    {"score_target": 0, "score_target_weekend": 0, "score_band_hi": 0, "score_band_hi_weekend": 0},
    {"score_target": 1, "score_target_weekend": 1, "score_band_hi": 1, "score_band_hi_weekend": 1},
    {"score_balance": False, "score_target": 1, "score_band_hi": 0},
    {"score_balance_mode": "spread", "score_target": 1, "score_band_hi": 0},
    {"fair_tolerance": 1000, "carry_cap": 1000, "max_weekends": 1000, "max_switches": 1000},
    {"fair_tolerance": 0, "carry_cap": 0, "max_weekends": 0, "max_switches": 0},
    {"max_full_runs": 0},
    {"max_full_runs": 31},
    {"max_full_runs": 31, "max_consecutive_shifts": 0},
    {"max_full_runs": 0, "max_consecutive_shifts": 1},
    {"share_overtime": False, "overtime_tolerance": 0, "overtime_carry_cap": 0},
    {"overtime_tolerance": 744, "overtime_carry_cap": 744},
    {"share_overtime": True, "carry_fairness": False},
    {"fair_shifts": []},
    {"headcounts": [headcount("D", low=1000, high=1000), headcount("N", low=0, high=0)]},
    {"headcounts": [headcount("D", also=["N"]), headcount("N", also=["D"])]},
    {"shifts": ["D", "N", "A", "B", "C"], "headcounts": [headcount("D", also=["A"]), headcount("N")]},
    {"headcount_enabled": False, "require_team_leader": False, "require_both_genders": False},
])
def test_what_the_api_accepts_at_its_limits_the_solver_accepts(mona, er, changes):
    response = put(mona, er, body(**changes))

    assert response.status_code == 200, response.content
    er.refresh_from_db()
    load_request(solve_input(er))


def test_a_one_shift_unit_is_saved_once_every_night_rule_is_off_and_the_solver_takes_it(mona, er):
    settings = HospitalSettings.load()
    settings.rules["weights"]["shift_switch"] = 0
    settings.save()

    response = put(mona, er, body(shifts=["D"], headcounts=[headcount("D")], fair_shifts=["D"], night_shift="N",
                                  night_rest_days=0, no_mixed_blocks=False, switches_hard=False, carry_fairness=False))

    assert response.status_code == 200, response.content
    er.refresh_from_db()
    load_request(solve_input(er))


# ---- reading --------------------------------------------------------------------------------------
def test_the_answer_says_what_the_page_needs(mona, er):
    state = mona.get(rules_of(er)).json()

    assert state["rules"] == DEFAULT_UNIT_RULES
    assert state["needs_review"] is True
    assert (state["request_cap"], state["preference_cap"], state["default_time_limit"], state["time_limit_cap"]) == (
        3, 10, 60, 300)
    assert [shift["code"] for shift in state["active_shifts"]] == ["D", "N", "A", "B", "C"]
    assert state["active_shifts"][0] == {"code": "D", "display_name": "Day", "hours": 12}


def test_a_retired_shift_is_not_on_offer(mona, er):
    Shift.objects.filter(code="C").update(active=False)

    assert "C" not in [shift["code"] for shift in mona.get(rules_of(er)).json()["active_shifts"]]


def test_a_unit_seeded_with_a_gap_opens(mona, er):
    Unit.objects.filter(pk=er.pk).update(rules=DEFAULT_UNIT_RULES | {
        "shifts": [], "headcounts": [], "fair_shifts": [], "night_shift": "", "day_shift": ""})

    state = mona.get(rules_of(er))

    assert state.status_code == 200, state.content
    assert state.json()["rules"]["shifts"] == [] and state.json()["rules"]["night_shift"] == ""


def test_a_unit_seeded_with_a_gap_is_put_right_by_its_manager(mona, er):
    Unit.objects.filter(pk=er.pk).update(rules=DEFAULT_UNIT_RULES | {
        "shifts": ["N"], "headcounts": [headcount("N")], "fair_shifts": ["N"], "day_shift": ""})

    assert ("rules", "day_shift") in refused_fields(put(mona, er, body(**mona.get(rules_of(er)).json()["rules"])))
    response = put(mona, er, body(shifts=["N", "A"], day_shift="A", fair_shifts=["N"], headcounts=[headcount("N"), headcount("A")]))

    assert response.status_code == 200, response.content
    er.refresh_from_db()
    assert load_request(solve_input(er)).problem.department.day_shift == "A"


# ---- saving ---------------------------------------------------------------------------------------
def test_saving_clears_the_flag_even_with_nothing_changed_and_is_audited(mona, er):
    response = put(mona, er, body())

    assert response.status_code == 200 and response.json()["needs_review"] is False
    er.refresh_from_db()
    assert er.rules_need_review is False
    entry = AuditEntry.objects.get(action="unit_rules.changed")
    assert entry.target == "ER" and entry.actor.username == "mona"
    assert entry.before == {"needs_review": True} and entry.after == {"needs_review": False}


def test_saving_again_with_no_change_writes_nothing(mona, er):
    put(mona, er, body())
    put(mona, er, body())

    assert AuditEntry.objects.filter(action="unit_rules.changed").count() == 1


def test_an_entry_holds_only_what_changed_with_its_values(mona, er):
    put(mona, er, body())

    put(mona, er, body(request_cap=5, preference_cap=0, default_time_limit=120, score_target=0.6))

    entry = AuditEntry.objects.filter(action="unit_rules.changed").latest("id")
    assert entry.before == {"score_target": 0.7, "request_cap": 3, "preference_cap": 10, "default_time_limit": 60}
    assert entry.after == {"score_target": 0.6, "request_cap": 5, "preference_cap": 0, "default_time_limit": 120}
    er.refresh_from_db()
    assert (er.request_cap, er.preference_cap, er.default_time_limit) == (5, 0, 120)
    assert not {"request_cap", "preference_cap"} & set(er.rules), "The block stays the solver's alone."


def test_the_system_admin_can_read_unit_rule_values_in_the_log_as_with_hospital_rules(mona, er, client_of):
    put(mona, er, body(score_target=0.6))

    admin = client_of("it", Role.SYSTEM_ADMIN)
    entries = admin.get("/api/audit/entries").json()["entries"]

    assert next(entry for entry in entries if entry["action"] == "unit_rules.changed")["after"]["score_target"] == 0.6


# ---- the most full runs in a month ----------------------------------------------------------------
def test_the_manager_sets_the_most_full_runs_audited_and_a_solve_input_carries_them(mona, er):
    put(mona, er, body())

    response = put(mona, er, body(max_full_runs=2))

    assert response.status_code == 200 and response.json()["rules"]["max_full_runs"] == 2
    entry = AuditEntry.objects.filter(action="unit_rules.changed").latest("id")
    assert (entry.before, entry.after, entry.actor.username) == ({"max_full_runs": 3}, {"max_full_runs": 2}, "mona")
    er.refresh_from_db()
    assert load_request(solve_input(er)).problem.department.max_full_runs == 2


def test_the_most_full_runs_keep_their_number_through_a_change_to_the_longest_run(mona, er):
    put(mona, er, body(max_full_runs=5))

    for longest in (1, 0, 4):
        assert put(mona, er, body(max_full_runs=5, max_consecutive_shifts=longest)).json()["rules"]["max_full_runs"] == 5


def test_a_missing_full_run_cap_is_refused_rather_than_defaulted(mona, er):
    rules = {key: value for key, value in DEFAULT_UNIT_RULES.items() if key != "max_full_runs"}

    assert ("rules", "max_full_runs") in refused_fields(put(mona, er, body() | {"rules": rules}))


@pytest.mark.parametrize("role", [Role.CNO, Role.DEPUTY_CNO])
def test_the_cno_and_the_deputy_read_the_most_full_runs_but_only_the_manager_changes_them(client_of, mona, er, role):
    put(mona, er, body(max_full_runs=1))
    reader = client_of("reader", role)

    assert reader.get(rules_of(er)).json()["rules"]["max_full_runs"] == 1
    assert put(reader, er, body(max_full_runs=4)).status_code == 403
    er.refresh_from_db()
    assert er.rules["max_full_runs"] == 1


# ---- the overtime share ---------------------------------------------------------------------------
def test_the_manager_sets_the_overtime_share_audited_and_a_solve_input_carries_it(mona, er):
    put(mona, er, body())

    response = put(mona, er, body(share_overtime=False, overtime_tolerance=8, overtime_carry_cap=24))

    assert response.status_code == 200
    assert {key: response.json()["rules"][key] for key in ("share_overtime", "overtime_tolerance", "overtime_carry_cap")} \
        == {"share_overtime": False, "overtime_tolerance": 8, "overtime_carry_cap": 24}
    entry = AuditEntry.objects.filter(action="unit_rules.changed").latest("id")
    assert (entry.before, entry.after, entry.actor.username) == (
        {"share_overtime": True, "overtime_tolerance": 12, "overtime_carry_cap": 12},
        {"share_overtime": False, "overtime_tolerance": 8, "overtime_carry_cap": 24}, "mona")
    er.refresh_from_db()
    department = load_request(solve_input(er)).problem.department
    assert (department.share_overtime, department.overtime_tolerance, department.overtime_carry_cap) == (False, 8, 24)


@pytest.mark.parametrize("field", ["share_overtime", "overtime_tolerance", "overtime_carry_cap"])
def test_a_missing_overtime_share_field_is_refused_rather_than_defaulted(mona, er, field):
    rules = {key: value for key, value in DEFAULT_UNIT_RULES.items() if key != field}

    assert ("rules", field) in refused_fields(put(mona, er, body() | {"rules": rules}))


@pytest.mark.parametrize("role", [Role.CNO, Role.DEPUTY_CNO])
def test_the_cno_and_the_deputy_read_the_overtime_share_but_only_the_manager_changes_it(client_of, mona, er, role):
    put(mona, er, body(share_overtime=False, overtime_tolerance=6, overtime_carry_cap=3))
    reader = client_of("reader", role)

    read = reader.get(rules_of(er)).json()["rules"]
    assert (read["share_overtime"], read["overtime_tolerance"], read["overtime_carry_cap"]) == (False, 6, 3)
    assert put(reader, er, body(share_overtime=True, overtime_tolerance=12)).status_code == 403
    er.refresh_from_db()
    assert (er.rules["share_overtime"], er.rules["overtime_tolerance"]) == (False, 6)


# ---- refusals -------------------------------------------------------------------------------------
@pytest.mark.parametrize("changes, field", [
    ({"shifts": []}, ("rules", "shifts")),
    ({"shifts": ["D", "D", "N"]}, ("rules", "shifts")),
    ({"shifts": ["D", "N", "X"]}, ("rules", "shifts", 2)),
    ({"shifts": [f"S{n}" for n in range(13)]}, ("rules", "shifts")),
    ({"shifts": "D"}, ("rules", "shifts")),
    ({"night_shift": ""}, ("rules", "night_shift")),
    ({"day_shift": ""}, ("rules", "day_shift")),
    ({"night_shift": "X"}, ("rules", "night_shift")),
    ({"day_shift": "N"}, ("rules", "day_shift")),
    ({"night_shift": "A"}, ("rules", "night_shift")),
    ({"headcounts": [headcount("A")]}, ("rules", "headcounts", 0, "shift")),
    ({"headcounts": [headcount("D"), headcount("D")], "fair_shifts": []}, ("rules", "headcounts", 1, "shift")),
    ({"headcounts": [headcount("D", also=["D"]), headcount("N")]}, ("rules", "headcounts", 0, "also_counts")),
    ({"headcounts": [headcount("D", also=["A"]), headcount("N")]}, ("rules", "headcounts", 0, "also_counts")),
    ({"headcounts": [headcount("D", also=["N", "N"]), headcount("N")]}, ("rules", "headcounts", 0, "also_counts")),
    ({"headcounts": [headcount("D") | {"weekday_max": 0}, headcount("N")]}, ("rules", "headcounts", 0, "weekday_max")),
    ({"headcounts": [headcount("D") | {"weekend_max": 0}, headcount("N")]}, ("rules", "headcounts", 0, "weekend_max")),
    ({"headcounts": [headcount("D", low=1001, high=1001), headcount("N")]}, ("rules", "headcounts", 0, "weekday_min")),
    ({"fair_shifts": ["D", "D"]}, ("rules", "fair_shifts")),
    ({"fair_shifts": ["A"]}, ("rules", "fair_shifts")),
    ({"shifts": ["D", "N", "A"], "fair_shifts": ["A"]}, ("rules", "fair_shifts")),
    ({"score_target": 1.01}, ("rules", "score_target")),
    ({"score_target_weekend": -0.1}, ("rules", "score_target_weekend")),
    ({"score_target": "0.5"}, ("rules", "score_target")),
    ({"score_target": True}, ("rules", "score_target")),
    ({"score_band_hi": 0.5}, ("rules", "score_band_hi")),
    ({"score_band_hi_weekend": 0.5}, ("rules", "score_band_hi_weekend")),
    ({"score_balance_mode": "wide"}, ("rules", "score_balance_mode")),
    ({"night_rest_days": 8}, ("rules", "night_rest_days")),
    ({"night_rest_days": 1.5}, ("rules", "night_rest_days")),
    ({"fair_tolerance": -1}, ("rules", "fair_tolerance")),
    ({"carry_cap": 1001}, ("rules", "carry_cap")),
    ({"max_consecutive_shifts": 1001}, ("rules", "max_consecutive_shifts")),
    ({"max_consecutive_shifts": 63}, ("rules", "max_consecutive_shifts")),
    ({"max_consecutive_shifts": 28, "double_cycle_rest": 7}, ("rules", "max_consecutive_shifts")),
    ({"double_cycle_rest": -1}, ("rules", "double_cycle_rest")),
    ({"max_weekends": "3"}, ("rules", "max_weekends")),
    ({"max_full_runs": -1}, ("rules", "max_full_runs")),
    ({"max_full_runs": 32}, ("rules", "max_full_runs")),
    ({"max_full_runs": 2.5}, ("rules", "max_full_runs")),
    ({"max_full_runs": "3"}, ("rules", "max_full_runs")),
    ({"max_full_runs": True}, ("rules", "max_full_runs")),
    ({"max_full_runs": None}, ("rules", "max_full_runs")),
    ({"share_overtime": "yes"}, ("rules", "share_overtime")),
    ({"share_overtime": 1}, ("rules", "share_overtime")),
    ({"share_overtime": None}, ("rules", "share_overtime")),
    ({"overtime_tolerance": -1}, ("rules", "overtime_tolerance")),
    ({"overtime_tolerance": 745}, ("rules", "overtime_tolerance")),
    ({"overtime_tolerance": 2.5}, ("rules", "overtime_tolerance")),
    ({"overtime_tolerance": 12.0}, ("rules", "overtime_tolerance")),
    ({"overtime_tolerance": "12"}, ("rules", "overtime_tolerance")),
    ({"overtime_tolerance": True}, ("rules", "overtime_tolerance")),
    ({"overtime_tolerance": None}, ("rules", "overtime_tolerance")),
    ({"overtime_carry_cap": -1}, ("rules", "overtime_carry_cap")),
    ({"overtime_carry_cap": 745}, ("rules", "overtime_carry_cap")),
    ({"overtime_carry_cap": 0.5}, ("rules", "overtime_carry_cap")),
    ({"overtime_carry_cap": "12"}, ("rules", "overtime_carry_cap")),
    ({"overtime_carry_cap": False}, ("rules", "overtime_carry_cap")),
    ({"max_switches": True}, ("rules", "max_switches")),
    ({"headcount_enabled": "yes"}, ("rules", "headcount_enabled")),
    ({"name": "Renamed"}, ("rules", "name")),
])
def test_a_refusal_names_its_field_and_saves_nothing(mona, er, changes, field):
    assert field in refused_fields(put(mona, er, body(**changes)))
    er.refresh_from_db()
    assert er.rules == DEFAULT_UNIT_RULES and er.rules_need_review is True
    assert not AuditEntry.objects.filter(action="unit_rules.changed").exists()


@pytest.mark.parametrize("changes, field", [
    ({"request_cap": -1}, ("request_cap",)),
    ({"request_cap": 32}, ("request_cap",)),
    ({"request_cap": "3"}, ("request_cap",)),
    ({"preference_cap": -1}, ("preference_cap",)),
    ({"preference_cap": 32}, ("preference_cap",)),
    ({"preference_cap": None}, ("preference_cap",)),
    ({"default_time_limit": 0}, ("default_time_limit",)),
    ({"default_time_limit": 301}, ("default_time_limit",)),
])
def test_the_cap_and_the_time_limit_are_checked(mona, er, changes, field):
    assert field in refused_fields(put(mona, er, body(**changes)))


def test_the_time_limit_follows_the_system_admins_cap_and_says_so(mona, er):
    TechnicalSettings.objects.update_or_create(pk=1, defaults={"time_limit_cap_seconds": 90})

    response = put(mona, er, body(default_time_limit=91))

    assert "90 seconds" in message(response)
    assert put(mona, er, body(default_time_limit=90)).status_code == 200


def test_refusals_come_together(mona, er):
    fields = refused_fields(put(mona, er, body(shifts=["D", "N", "X"], day_shift="", fair_shifts=["A"], default_time_limit=999)))

    assert {("rules", "shifts", 2), ("rules", "day_shift"), ("rules", "fair_shifts"), ("default_time_limit",)} <= fields


def test_a_not_a_number_is_refused(mona, er):
    # Python's JSON reader takes these, and PostgreSQL's jsonb cannot hold them.
    text = json.dumps(body()).replace('"score_target": 0.7', '"score_target": NaN')

    assert ("rules", "score_target") in refused_fields(mona.put(rules_of(er), text, content_type="application/json"))


def test_a_retired_shift_is_refused_though_the_catalogue_once_had_it(mona, er):
    Shift.objects.filter(code="A").update(active=False)

    response = put(mona, er, body(shifts=["D", "N", "A"]))

    assert ("rules", "shifts", 2) in refused_fields(response)
    assert "not an active shift" in message(response)


def test_the_night_shift_may_be_outside_the_units_shifts_only_with_every_night_rule_off(mona, er):
    one_shift = dict(shifts=["D"], headcounts=[headcount("D")], fair_shifts=["D"], night_rest_days=0,
                     no_mixed_blocks=False, switches_hard=False, carry_fairness=False)

    assert ("rules", "night_shift") in refused_fields(put(mona, er, body(**one_shift)))  # The switch weight is on.
    settings = HospitalSettings.load()
    settings.rules["weights"]["shift_switch"] = 0
    settings.save()
    for rule in ({"night_rest_days": 1}, {"no_mixed_blocks": True}, {"switches_hard": True}, {"carry_fairness": True}):
        assert ("rules", "night_shift") in refused_fields(put(mona, er, body(**one_shift | rule))), rule
    assert put(mona, er, body(**one_shift)).status_code == 200


def test_soft_night_rest_with_its_weight_at_zero_needs_no_night_shift(mona, er):
    settings = HospitalSettings.load()
    settings.rules["weights"]["shift_switch"] = settings.rules["weights"]["night_rest"] = 0
    settings.save()

    response = put(mona, er, body(shifts=["D"], headcounts=[headcount("D")], fair_shifts=["D"], night_rest_days=2,
                                  night_rest_hard=False, no_mixed_blocks=False, switches_hard=False, carry_fairness=False))

    assert response.status_code == 200, response.content


# ---- who ------------------------------------------------------------------------------------------
def test_another_units_manager_is_refused_both_ways(omar, er, ccu):
    assert omar.get(rules_of(er)).status_code == 403
    assert put(omar, er, body(score_target=0.1)).status_code == 403
    er.refresh_from_db()
    assert er.rules == DEFAULT_UNIT_RULES


def test_a_manager_is_refused_alike_whether_the_unit_exists_or_not(mona, er):
    assert mona.get(f"/api/units/{uuid4()}/rules").status_code == 403
    assert mona.put(f"/api/units/{uuid4()}/rules", body(), content_type="application/json").status_code == 403


def test_a_manager_with_no_unit_is_refused(client_of, er):
    assert client_of("idle", Role.UNIT_MANAGER).get(rules_of(er)).status_code == 403


@pytest.mark.parametrize("role", [Role.CNO, Role.DEPUTY_CNO])
def test_the_cno_and_the_deputy_read_but_cannot_change(client_of, er, role):
    client = client_of("reader", role)

    assert client.get(rules_of(er)).json()["rules"] == DEFAULT_UNIT_RULES
    assert put(client, er, body(score_target=0.1)).status_code == 403
    assert client.get(f"/api/units/{uuid4()}/rules").status_code == 404


@pytest.mark.parametrize("role", [Role.STAFF_NURSE, Role.SYSTEM_ADMIN])
def test_nobody_else_reads_them(client_of, er, role):
    assert client_of("other", role).get(rules_of(er)).status_code == 403
