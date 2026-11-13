import copy
import dataclasses
import json
from pathlib import Path

import pytest
from django.apps import apps as django_apps
from django.core.management import call_command
from nurse_roster.problem import GeneralRules, Problem, Weights
from nurse_roster.request import load_request
from nurse_roster.rules import active

from accounts.models import Role
from conftest import migration
from audit.models import AuditEntry
from hospital.models import HospitalSettings, Skill
from hospital.rules import DEFAULT_RULES, general_block

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("catalogue")]

RULES = "/api/hospital/rules"
ER_REQUEST = Path(__file__).parents[3] / "solver/tests/fixtures/er_request_2026-03.json"


def body(**changes) -> dict:
    return copy.deepcopy(DEFAULT_RULES) | changes


def weights(**changes) -> dict:
    return body(weights=DEFAULT_RULES["weights"] | changes)


def put(client, rules):
    return client.put(RULES, rules, content_type="application/json")


def refused_fields(response) -> set[tuple]:
    assert response.status_code == 422, response.content
    return {tuple(error["loc"][2:]) for error in response.json()["detail"]}


def er_request() -> dict:
    return json.loads(ER_REQUEST.read_text())


def version_2(request: dict) -> dict:
    """The ER sample is version 1; a general block with `required_skills` is version 2's, whose department has needs."""
    return request | {"version": 2, "department": request["department"] | {"skill_needs": [], "skill_needs_hard": True}}


# ---- the solver's contract ------------------------------------------------------------------------
def test_the_rules_are_every_field_of_the_solvers_block_but_the_catalogues():
    """The shift catalogue gives `shift_hours`, the skill catalogue `required_skills`."""
    fields = {field.name for field in dataclasses.fields(GeneralRules)}

    assert set(DEFAULT_RULES) == fields - {"shift_hours", "required_skills"}
    assert set(DEFAULT_RULES["weights"]) == {field.name for field in dataclasses.fields(Weights)}


def test_the_defaults_are_the_reviewed_set_the_solver_was_shaped_on():
    """The ER sample predates full runs, skills and the overtime share; the full-run weight is the spec's (full-runs.md), 4,
    the skill shortfall's is 0, no price until the CNO sets one (skills-per-shift-day.md), and an hour of overtime above a
    share costs 1 (overtime-share.md)."""
    reviewed = er_request()["general"]
    del reviewed["shift_hours"]
    reviewed["weights"] |= {"excess_full_run": 4, "skill_shortfall": 0, "overtime_above_share": 1}

    assert DEFAULT_RULES == reviewed


def test_what_the_cno_saved_is_what_a_solve_input_receives(signed_in):
    saved = weights(change=9) | {"hours_per_day": 7}
    assert put(signed_in(Role.CNO), saved).status_code == 200
    shift_hours = er_request()["general"]["shift_hours"]

    general = general_block(HospitalSettings.load(), shift_hours=shift_hours)

    assert general == saved | {"shift_hours": shift_hours, "required_skills": []}
    problem = load_request(version_2(er_request()) | {"general": general}).problem
    assert problem.general.hours_per_day == 7 and problem.general.weights.change == 9


# ---- defaults -------------------------------------------------------------------------------------
def test_first_start_seeds_the_defaults_flagged_for_review(monkeypatch):
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "one-time-password")

    call_command("bootstrap")

    row = HospitalSettings.objects.get()
    assert row.rules == DEFAULT_RULES and row.rules_need_review


def test_an_installation_older_than_the_rules_gets_them_on_its_next_start(make_user):
    make_user("admin", Role.SYSTEM_ADMIN)

    call_command("bootstrap")

    assert HospitalSettings.objects.get().rules_need_review


def test_later_starts_leave_saved_rules_alone(monkeypatch, signed_in):
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "one-time-password")
    call_command("bootstrap")
    assert put(signed_in(Role.CNO), body(hours_per_day=7)).status_code == 200

    call_command("bootstrap")

    row = HospitalSettings.objects.get()
    assert row.rules["hours_per_day"] == 7 and not row.rules_need_review


def test_the_cno_reads_the_defaults_and_the_flag(signed_in):
    answer = signed_in(Role.CNO).get(RULES).json()

    assert answer["rules"] == DEFAULT_RULES and answer["needs_review"] is True
    assert answer["shift_codes"] == ["D", "N", "A", "B", "C"]


# ---- saving ---------------------------------------------------------------------------------------
def test_saving_stores_the_rules_and_clears_the_flag(signed_in):
    client = signed_in(Role.CNO)
    saved = body(hours_per_day=7, weekend_weekdays=[4, 5], forbidden_successions=[["N", "D"]],
                 hours_rule_enabled=False)

    response = put(client, saved)

    assert response.status_code == 200
    assert response.json()["rules"] == saved and response.json()["needs_review"] is False
    assert client.get(RULES).json() == response.json()


def test_a_zero_weight_is_allowed_because_it_switches_the_rule_off(signed_in):
    response = put(signed_in(Role.CNO), weights(fair_day=0))

    assert response.status_code == 200 and response.json()["rules"]["weights"]["fair_day"] == 0


def test_weekend_days_are_stored_in_order_however_they_were_sent(signed_in):
    response = put(signed_in(Role.CNO), body(weekend_weekdays=[5, 4]))

    assert response.json()["rules"]["weekend_weekdays"] == [4, 5]


def test_no_weekend_and_no_forbidden_succession_are_both_possible(signed_in):
    response = put(signed_in(Role.CNO), body(weekend_weekdays=[], forbidden_successions=[]))

    assert response.status_code == 200


def test_saving_audits_who_changed_what_from_what_to_what(signed_in):
    client = signed_in(Role.CNO, username="the-cno")
    AuditEntry.objects.all().delete()

    put(client, weights(change=9) | {"hours_per_day": 7})

    entry = AuditEntry.objects.get()
    assert entry.action == "hospital_rules.changed" and entry.actor.username == "the-cno"
    assert entry.target == "hospital rules" and entry.ip
    assert entry.before == {"needs_review": True, "hours_per_day": 8, "weights.change": 3}
    assert entry.after == {"needs_review": False, "hours_per_day": 7, "weights.change": 9}


def test_saving_the_defaults_unchanged_is_a_review_so_it_clears_the_flag_and_is_audited(signed_in):
    client = signed_in(Role.CNO)
    AuditEntry.objects.all().delete()

    response = put(client, body())

    assert response.json()["needs_review"] is False
    entry = AuditEntry.objects.get()
    assert entry.before == {"needs_review": True} and entry.after == {"needs_review": False}


def test_saving_again_with_no_change_writes_no_entry(signed_in):
    client = signed_in(Role.CNO)
    put(client, body())
    AuditEntry.objects.all().delete()

    assert put(client, body()).status_code == 200

    assert not AuditEntry.objects.exists()


def test_a_later_change_does_not_bring_the_flag_back(signed_in):
    client = signed_in(Role.CNO)
    put(client, body())

    assert put(client, body(hours_per_day=7)).json()["needs_review"] is False


# ---- the full-run weight --------------------------------------------------------------------------
def test_a_full_run_above_the_most_costs_4_by_default(signed_in):
    assert signed_in(Role.CNO).get(RULES).json()["rules"]["weights"]["excess_full_run"] == 4


def test_the_cno_changes_the_full_run_weight_audited_and_a_solve_input_carries_it(signed_in):
    client = signed_in(Role.CNO)
    put(client, body())
    AuditEntry.objects.all().delete()

    response = put(client, weights(excess_full_run=50))

    assert response.status_code == 200 and response.json()["rules"]["weights"]["excess_full_run"] == 50
    entry = AuditEntry.objects.get(action="hospital_rules.changed")
    assert entry.before == {"weights.excess_full_run": 4} and entry.after == {"weights.excess_full_run": 50}
    general = general_block(HospitalSettings.load(), shift_hours=er_request()["general"]["shift_hours"])
    assert load_request(version_2(er_request()) | {"general": general}).problem.general.weights.excess_full_run == 50


def test_a_full_run_weight_of_0_switches_the_rule_off_hospital_wide(signed_in):
    def capped_at_0() -> Problem:
        general = general_block(HospitalSettings.load(), shift_hours=er_request()["general"]["shift_hours"])
        request = version_2(er_request()) | {"general": general}
        request["department"] |= {"max_full_runs": 0}
        return load_request(request).problem
    client = signed_in(Role.CNO)
    assert active(capped_at_0(), "excess_full_run")

    assert put(client, weights(excess_full_run=0)).status_code == 200

    assert not active(capped_at_0(), "excess_full_run")


@pytest.mark.parametrize("value", [-1, 10 ** 6 + 1, 2.5, "4", True, None])
def test_a_bad_full_run_weight_is_refused_with_its_field(signed_in, value):
    assert ("weights", "excess_full_run") in refused_fields(put(signed_in(Role.CNO), weights(excess_full_run=value)))


def test_the_cno_prices_a_skill_shortfall_audited_and_a_solve_input_carries_it(signed_in):
    client = signed_in(Role.CNO)
    put(client, body())
    AuditEntry.objects.all().delete()

    response = put(client, weights(skill_shortfall=50))

    assert response.status_code == 200 and response.json()["rules"]["weights"]["skill_shortfall"] == 50
    entry = AuditEntry.objects.get(action="hospital_rules.changed")
    assert entry.before == {"weights.skill_shortfall": 0} and entry.after == {"weights.skill_shortfall": 50}
    general = general_block(HospitalSettings.load(), shift_hours=er_request()["general"]["shift_hours"])
    assert load_request(version_2(er_request()) | {"general": general}).problem.general.weights.skill_shortfall == 50


@pytest.mark.parametrize("value", [-1, 10 ** 6 + 1, 2.5, "4", True, None])
def test_a_bad_skill_shortfall_weight_is_refused_with_its_field(signed_in, value):
    assert ("weights", "skill_shortfall") in refused_fields(put(signed_in(Role.CNO), weights(skill_shortfall=value)))


def test_a_solve_inputs_required_skills_are_the_live_skills_required_to_work():
    Skill.objects.bulk_create([Skill(name="BLS", expires=True, required_to_work=True),
                               Skill(name="acls", expires=True, required_to_work=True),
                               Skill(name="ICU", expires=False, required_to_work=False),
                               Skill(name="PALS", expires=True, required_to_work=True, active=False)])

    general = general_block(HospitalSettings.load(), shift_hours=er_request()["general"]["shift_hours"])

    assert general["required_skills"] == ["acls", "BLS"]
    assert load_request(version_2(er_request()) | {"general": general}).problem.general.required_skills == ["acls", "BLS"]


def test_an_installation_from_before_the_full_run_weight_gets_it_at_4(signed_in):
    put(signed_in(Role.CNO), weights(change=9))
    row = HospitalSettings.load()
    del row.rules["weights"]["excess_full_run"]
    row.save()

    migration("hospital", "0004_excess_full_run_weight").add(django_apps, None)

    row.refresh_from_db()
    assert row.rules["weights"]["excess_full_run"] == 4 and row.rules["weights"]["change"] == 9
    assert not row.rules_need_review


def test_an_installation_from_before_the_skill_shortfall_weight_gets_it_at_0(signed_in):
    put(signed_in(Role.CNO), weights(change=9))
    row = HospitalSettings.load()
    del row.rules["weights"]["skill_shortfall"]
    row.save()

    migration("hospital", "0005_skill_shortfall_weight").add(django_apps, None)

    row.refresh_from_db()
    assert row.rules["weights"]["skill_shortfall"] == 0 and row.rules["weights"]["change"] == 9
    assert not row.rules_need_review


# ---- the overtime share's weight ------------------------------------------------------------------
def shared_problem() -> Problem:
    """The ER sample under the hospital's block, its unit sharing its overtime."""
    general = general_block(HospitalSettings.load(), shift_hours=er_request()["general"]["shift_hours"])
    request = version_2(er_request()) | {"general": general}
    request["department"] |= {"share_overtime": True, "overtime_tolerance": 12, "overtime_carry_cap": 12}
    return load_request(request).problem


def test_an_hour_of_overtime_above_a_share_costs_1_by_default(signed_in):
    assert signed_in(Role.DEPUTY_CNO).get(RULES).json()["rules"]["weights"]["overtime_above_share"] == 1


def test_the_cno_changes_the_overtime_share_weight_audited_and_a_solve_input_carries_it(signed_in):
    client = signed_in(Role.CNO)
    put(client, weights(nurse_overtime=5))
    AuditEntry.objects.all().delete()

    response = put(client, weights(nurse_overtime=5, overtime_above_share=4))

    assert response.status_code == 200 and response.json()["rules"]["weights"]["overtime_above_share"] == 4
    entry = AuditEntry.objects.get(action="hospital_rules.changed")
    assert entry.before == {"weights.overtime_above_share": 1} and entry.after == {"weights.overtime_above_share": 4}
    assert shared_problem().general.weights.overtime_above_share == 4


def test_an_overtime_share_weight_of_0_switches_the_rule_off_hospital_wide(signed_in):
    assert active(shared_problem(), "overtime_above_share")

    assert put(signed_in(Role.CNO), weights(overtime_above_share=0)).status_code == 200

    assert not active(shared_problem(), "overtime_above_share")


def test_the_overtime_share_weight_may_equal_the_hour_of_overtimes(signed_in):
    response = put(signed_in(Role.CNO), weights(nurse_overtime=3, overtime_above_share=3))

    assert response.status_code == 200


@pytest.mark.parametrize("changes", [{"overtime_above_share": 2}, {"nurse_overtime": 0}])
def test_an_overtime_share_weight_above_the_hour_of_overtimes_is_refused_beside_it_saying_why(signed_in, changes):
    """Above it, the solver could lower the rule's cost by adding overtime."""
    response = put(signed_in(Role.CNO), weights(**changes))

    assert refused_fields(response) == {("weights", "overtime_above_share")}
    assert response.json()["detail"][0]["msg"] == (
        "Value error, Cannot be above Nurse overtime, or the solver could lower this cost by adding overtime.")
    assert HospitalSettings.load().rules["weights"]["overtime_above_share"] == 1


@pytest.mark.parametrize("value", [-1, 10 ** 6 + 1, 0.5, "1", True, None])
def test_a_bad_overtime_share_weight_is_refused_with_its_field(signed_in, value):
    assert ("weights", "overtime_above_share") in refused_fields(put(signed_in(Role.CNO), weights(overtime_above_share=value)))


def test_an_installation_from_before_the_overtime_share_weight_gets_it_at_1(signed_in):
    put(signed_in(Role.CNO), weights(change=9))
    row = HospitalSettings.load()
    del row.rules["weights"]["overtime_above_share"]
    row.save()

    migration("hospital", "0008_overtime_above_share_weight").add(django_apps, None)

    row.refresh_from_db()
    assert row.rules["weights"]["overtime_above_share"] == 1 and row.rules["weights"]["change"] == 9
    assert not row.rules_need_review


def test_an_installation_pricing_no_overtime_gets_the_share_weight_at_0_so_its_next_save_is_not_refused(signed_in):
    client = signed_in(Role.CNO)
    put(client, weights(nurse_overtime=0, overtime_above_share=0))
    row = HospitalSettings.load()
    del row.rules["weights"]["overtime_above_share"]
    row.save()

    migration("hospital", "0008_overtime_above_share_weight").add(django_apps, None)

    row.refresh_from_db()
    assert row.rules["weights"]["overtime_above_share"] == 0
    assert put(client, row.rules).status_code == 200


# ---- validation -----------------------------------------------------------------------------------
@pytest.mark.parametrize("rules, field", [
    (body(hours_per_day=25), ("hours_per_day",)),
    (body(hours_per_day=-1), ("hours_per_day",)),
    (body(hours_per_day="8"), ("hours_per_day",)),
    (body(hours_per_day=8.5), ("hours_per_day",)),
    (body(hours_per_day=True), ("hours_per_day",)),
    (body(ramadan_hours_per_day=9), ("ramadan_hours_per_day",)),
    (body(breastfeeding_reduction=7), ("breastfeeding_reduction",)),
    (body(weekend_weekdays=[7]), ("weekend_weekdays", 0)),
    (body(weekend_weekdays=[4, 4]), ("weekend_weekdays",)),
    (body(forbidden_successions=[["N", "X"]]), ("forbidden_successions", 0, 1)),
    (body(forbidden_successions=[["N"]]), ("forbidden_successions", 0)),
    (body(forbidden_successions=[["N", "D", "A"]]), ("forbidden_successions", 0)),
    (body(forbidden_successions=[["N", "D"], ["N", "D"]]), ("forbidden_successions",)),
    (body(forbidden_successions_enabled="yes"), ("forbidden_successions_enabled",)),
    (body(hours_rule_enabled=1), ("hours_rule_enabled",)),
    (body(overtime_caps_enabled=None), ("overtime_caps_enabled",)),
    (body(overtime_month_cap=-1), ("overtime_month_cap",)),
    (body(overtime_year_cap=10 ** 6 + 1), ("overtime_year_cap",)),
    (body(dept_overtime_cap=1.5), ("dept_overtime_cap",)),
    (weights(night_rest=-1), ("weights", "night_rest")),
    (weights(change=10 ** 6 + 1), ("weights", "change")),
    (weights(fair_day="1"), ("weights", "fair_day")),
])
def test_a_refusal_names_the_field(signed_in, rules, field):
    assert refused_fields(put(signed_in(Role.CNO), rules)) == {field}


def test_a_refusal_names_every_bad_field_at_once(signed_in):
    rules = weights(change=-1) | {"hours_per_day": 25, "overtime_month_cap": -1}

    assert refused_fields(put(signed_in(Role.CNO), rules)) == {
        ("hours_per_day",), ("overtime_month_cap",), ("weights", "change")}


def test_a_missing_field_is_named(signed_in):
    rules = body()
    del rules["hours_per_day"]
    del rules["weights"]["change"]

    assert refused_fields(put(signed_in(Role.CNO), rules)) == {("hours_per_day",), ("weights", "change")}


@pytest.mark.parametrize("rules, field", [
    (body(shift_hours={"D": 12}), ("shift_hours",)),
    (weights(extra=1), ("weights", "extra")),
])
def test_an_unknown_field_is_refused_rather_than_dropped(signed_in, rules, field):
    assert refused_fields(put(signed_in(Role.CNO), rules)) == {field}


def test_a_refused_save_changes_nothing(signed_in):
    client = signed_in(Role.CNO)
    AuditEntry.objects.all().delete()

    put(client, body(hours_per_day=25))

    row = HospitalSettings.load()
    assert row.rules == DEFAULT_RULES and row.rules_need_review and not AuditEntry.objects.exists()


@pytest.mark.parametrize("rules", [
    body(hours_per_day=0, ramadan_hours_per_day=0, breastfeeding_reduction=0),
    body(hours_per_day=24, ramadan_hours_per_day=24, breastfeeding_reduction=24),
    body(overtime_month_cap=10 ** 6, overtime_year_cap=0, dept_overtime_cap=0),
    body(forbidden_successions=[["D", "D"]]),
    body(weekend_weekdays=list(range(7))),
    weights(**dict.fromkeys(DEFAULT_RULES["weights"], 10 ** 6)),
    weights(**dict.fromkeys(DEFAULT_RULES["weights"], 0)),
])
def test_what_this_api_accepts_the_solver_accepts_too(signed_in, rules):
    response = put(signed_in(Role.CNO), rules)
    assert response.status_code == 200, response.content

    general = general_block(HospitalSettings.load(), shift_hours=er_request()["general"]["shift_hours"])

    assert load_request(version_2(er_request()) | {"general": general}).problem.general.hours_per_day == rules["hours_per_day"]


# ---- authorization --------------------------------------------------------------------------------
def test_the_deputy_cno_reads_but_cannot_change(signed_in):
    client = signed_in(Role.DEPUTY_CNO)
    AuditEntry.objects.all().delete()

    assert client.get(RULES).json()["rules"] == DEFAULT_RULES
    assert put(client, body(hours_per_day=7)).status_code == 403
    assert HospitalSettings.load().rules == DEFAULT_RULES and not AuditEntry.objects.exists()


@pytest.mark.parametrize("role", [Role.SYSTEM_ADMIN, Role.UNIT_MANAGER, Role.STAFF_NURSE])
def test_every_other_role_can_neither_read_nor_change(signed_in, role):
    client = signed_in(role)

    assert client.get(RULES).status_code == 403
    assert put(client, body(hours_per_day=7)).status_code == 403
