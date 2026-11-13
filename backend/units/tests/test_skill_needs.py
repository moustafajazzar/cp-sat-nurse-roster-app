"""Skill needs among a unit's rules: at least so many nurses holding a skill on exactly a shift, each weekday and
each weekend day, hard or soft by one flag. What the manager saves, what is refused, and what the skill catalogue and the
shift catalogue do to a unit's needs."""
import copy
import dataclasses

import pytest
from nurse_roster.problem import SkillNeed
from nurse_roster.request import load_request

from accounts.models import Role, User
from audit.models import AuditEntry
from hospital.models import Shift, Skill, SkillRename
from units.models import Unit
from units.rules import DEFAULT_UNIT_RULES
from units.rules_api import SkillNeedAnswer, SkillNeedRule
from units.tests.test_rules_api import body, put, refused_fields, rules_of, solve_input
from units.tests.test_shift_guards import change_shift

pytestmark = pytest.mark.django_db

SKILLS = "/api/hospital/skills"


def need(shift="N", skill="ICU", weekday=1, weekend=1) -> dict:
    return {"shift": shift, "skill": skill, "weekday_min": weekday, "weekend_min": weekend}


@pytest.fixture(autouse=True)
def skills():
    Skill.objects.bulk_create([Skill(name="ICU", expires=False, required_to_work=False),
                               Skill(name="BLS", expires=True, required_to_work=True),
                               Skill(name="PALS", expires=True, required_to_work=False, active=False)])


@pytest.fixture
def mona(client_of):
    return client_of("mona", Role.UNIT_MANAGER)


@pytest.fixture
def er(mona):
    return Unit.objects.create(name="ER", manager=User.objects.get(username="mona"), rules=copy.deepcopy(DEFAULT_UNIT_RULES))


@pytest.fixture
def cno(client_of):
    return client_of("chief", Role.CNO)


def unit(name, needs=(), **rules) -> Unit:
    return Unit.objects.create(name=name, rules=copy.deepcopy(DEFAULT_UNIT_RULES) | {"skill_needs": list(needs)} | rules)


def change_skill(cno, current, /, **changes):
    skill = Skill.objects.get(name=current)
    row = {"name": skill.name, "kind": skill.kind, "issuing_body": skill.issuing_body, "expires": skill.expires,
           "required_to_work": skill.required_to_work, "lets_a_nurse_precept": skill.lets_a_nurse_precept,
           "active": skill.active} | changes
    return cno.put(f"{SKILLS}/{skill.pk}", row, content_type="application/json")


def refusal(response) -> tuple[tuple, str]:
    assert response.status_code == 422, response.content
    errors = response.json()["detail"]
    assert len(errors) == 1, errors
    [error] = errors
    return tuple(error["loc"][2:]), error["msg"]


# ---- the solver's contract ------------------------------------------------------------------------
@pytest.mark.parametrize("schema", [SkillNeedRule, SkillNeedAnswer])
def test_both_need_schemas_are_the_solvers(schema):
    assert set(schema.model_fields) == {field.name for field in dataclasses.fields(SkillNeed)}


def test_what_the_manager_saves_is_what_a_solve_input_receives(mona, er):
    needs = [need("N", "ICU", 2, 1), need("D", "ICU", 1, 0), need("N", "BLS", 0, 3)]

    response = put(mona, er, body(skill_needs=needs, skill_needs_hard=False))

    assert response.status_code == 200, response.content
    assert response.json()["rules"]["skill_needs"] == needs and response.json()["rules"]["skill_needs_hard"] is False
    er.refresh_from_db()
    department = load_request(solve_input(er)).problem.department
    assert department.skill_needs == [SkillNeed("N", "ICU", 2, 1), SkillNeed("D", "ICU", 1, 0), SkillNeed("N", "BLS", 0, 3)]
    assert department.skill_needs_hard is False


@pytest.mark.parametrize("weekday, weekend", [(0, 0), (1000, 1000)])
def test_the_minimums_at_their_limits_are_what_the_solver_takes(mona, er, weekday, weekend):
    assert put(mona, er, body(skill_needs=[need(weekday=weekday, weekend=weekend)])).status_code == 200

    er.refresh_from_db()
    assert load_request(solve_input(er)).problem.department.skill_needs == [SkillNeed("N", "ICU", weekday, weekend)]


def test_the_page_is_offered_the_live_skills_in_the_catalogues_order(mona, er):
    Skill.objects.create(name="acls", expires=True, required_to_work=False)

    assert mona.get(rules_of(er)).json()["active_skills"] == ["acls", "BLS", "ICU"]


def test_a_need_may_name_a_licence_or_a_certificate_as_it_names_a_skill(mona, er, cno):
    # Every kind is counted alike, and a change of kind leaves the needs that name the entry as they were.
    Skill.objects.create(name="SCFHS registration", kind=Skill.Kind.LICENCE, expires=True, required_to_work=True)
    needs = [need("N", "SCFHS registration", 2, 2), need("D", "ICU", 1, 1)]
    assert put(mona, er, body(skill_needs=needs)).status_code == 200

    assert change_skill(cno, "ICU", kind="certificate", issuing_body="A university").status_code == 200

    er.refresh_from_db()
    assert load_request(solve_input(er)).problem.department.skill_needs == [
        SkillNeed("N", "SCFHS registration", 2, 2), SkillNeed("D", "ICU", 1, 1)]


# ---- the audit log --------------------------------------------------------------------------------
def test_a_change_to_the_needs_is_audited_with_its_values(mona, er):
    put(mona, er, body())

    put(mona, er, body(skill_needs=[need()], skill_needs_hard=False))

    entry = AuditEntry.objects.filter(action="unit_rules.changed").latest("id")
    assert entry.target == "ER" and entry.actor.username == "mona"
    assert entry.before == {"skill_needs": [], "skill_needs_hard": True}
    assert entry.after == {"skill_needs": [need()], "skill_needs_hard": False}


# ---- refusals -------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad, field", [
    (need(shift="A"), ("shift",)),
    (need(skill="PALS"), ("skill",)),
    (need(skill="Unheard of"), ("skill",)),
    (need(skill="icu"), ("skill",)),
    (need(weekday=-1), ("weekday_min",)),
    (need(weekend=1001), ("weekend_min",)),
    (need(weekday=1.5), ("weekday_min",)),
    (need(weekend="1"), ("weekend_min",)),
    (need(weekday=None), ("weekday_min",)),
    (need() | {"extra": 1}, ("extra",)),
])
def test_a_bad_need_is_refused_beside_its_field_and_nothing_is_saved(mona, er, bad, field):
    response = put(mona, er, body(skill_needs=[need("D"), bad]))

    assert ("rules", "skill_needs", 1, *field) in refused_fields(response)
    er.refresh_from_db()
    assert er.rules["skill_needs"] == []


def test_the_refusals_say_why(mona, er):
    response = put(mona, er, body(skill_needs=[need(shift="A"), need(skill="PALS"), need("D", "ICU"), need("D", "ICU")]))

    assert {tuple(error["loc"][2:]): error["msg"] for error in response.json()["detail"]} == {
        ("rules", "skill_needs", 0, "shift"): "A is not one of this unit's shifts.",
        ("rules", "skill_needs", 1, "skill"): "PALS is not an active skill in the catalogue.",
        ("rules", "skill_needs", 3, "skill"): "D already has a need for ICU.",
    }


def test_two_needs_on_one_shift_for_two_skills_are_kept(mona, er):
    assert put(mona, er, body(skill_needs=[need("N", "ICU"), need("N", "BLS")])).status_code == 200


def test_the_same_skill_on_two_shifts_is_kept(mona, er):
    assert put(mona, er, body(skill_needs=[need("D", "ICU"), need("N", "ICU")])).status_code == 200


@pytest.mark.parametrize("missing", ["skill_needs", "skill_needs_hard"])
def test_a_missing_field_is_refused_rather_than_defaulted(mona, er, missing):
    rules = {key: value for key, value in DEFAULT_UNIT_RULES.items() if key != missing}

    assert ("rules", missing) in refused_fields(put(mona, er, body() | {"rules": rules}))


def test_at_most_a_hundred_needs(mona, er):
    Skill.objects.bulk_create([Skill(name=f"S{number}", expires=False, required_to_work=False) for number in range(101)])
    many = [need("D", f"S{number}") for number in range(101)]

    assert ("rules", "skill_needs") in refused_fields(put(mona, er, body(skill_needs=many)))
    assert put(mona, er, body(skill_needs=many[:100])).status_code == 200


# ---- taking a shift out ---------------------------------------------------------------------------
def test_taking_out_a_shift_a_need_names_is_refused_naming_the_need(mona, er):
    needs = [need("A", "ICU"), need("N", "BLS"), need("A", "BLS")]
    put(mona, er, body(shifts=["D", "N", "A"], skill_needs=needs))

    response = put(mona, er, body(skill_needs=needs))

    assert refusal(response) == (("rules", "shifts"), "A skill need names a shift you are taking out: ICU on A; BLS on A. "
                                                      "Take the need out first.")
    er.refresh_from_db()
    assert er.rules["shifts"] == ["D", "N", "A"]
    assert put(mona, er, body(skill_needs=[need("N", "BLS")])).status_code == 200


# ---- who ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("role", [Role.CNO, Role.DEPUTY_CNO])
def test_the_cno_and_the_deputy_read_the_needs_but_only_the_manager_changes_them(client_of, mona, er, role):
    put(mona, er, body(skill_needs=[need()], skill_needs_hard=False))
    reader = client_of("reader", role)

    rules = reader.get(rules_of(er)).json()["rules"]

    assert (rules["skill_needs"], rules["skill_needs_hard"]) == ([need()], False)
    assert put(reader, er, body(skill_needs=[])).status_code == 403
    er.refresh_from_db()
    assert er.rules["skill_needs"] == [need()]


# ---- retiring a skill -----------------------------------------------------------------------------
def test_retiring_a_skill_a_live_units_need_names_is_refused_naming_the_units(cno):
    unit("ICU", [need("N", "ICU")])
    unit("ER", [need("D", "ICU"), need("N", "BLS")])
    unit("CCU", [need("N", "BLS")])
    Unit.objects.filter(pk=unit("Old", [need("N", "ICU")]).pk).update(archived=True)

    assert refusal(change_skill(cno, "ICU", active=False)) == (
        ("active",), "ICU is in the skill needs of ER, ICU. The unit's manager has to take it out of them first.")
    assert Skill.objects.get(name="ICU").active is True


def test_a_skill_no_live_unit_needs_can_be_retired(cno):
    Unit.objects.filter(pk=unit("Old", [need("N", "ICU")]).pk).update(archived=True)
    unit("ER", [need("N", "BLS")])

    assert change_skill(cno, "ICU", active=False).status_code == 200


def test_once_the_manager_has_taken_the_need_out_the_skill_can_be_retired(cno, mona, er):
    put(mona, er, body(skill_needs=[need()]))
    assert change_skill(cno, "ICU", active=False).status_code == 422

    put(mona, er, body(skill_needs=[]))

    assert change_skill(cno, "ICU", active=False).status_code == 200


def test_other_changes_to_a_needed_skill_pass(cno):
    unit("ER", [need("N", "ICU")])

    assert change_skill(cno, "ICU", required_to_work=True).status_code == 200


# ---- renaming a skill -----------------------------------------------------------------------------
def test_a_skill_rename_carries_through_every_units_needs_archived_too(cno):
    unit("ER", [need("D", "ICU"), need("N", "BLS"), need("N", "ICU", 2, 3)])
    Unit.objects.filter(pk=unit("Old", [need("N", "ICU")]).pk).update(archived=True)
    unit("CCU", [need("N", "BLS")])

    assert change_skill(cno, "ICU", name="Critical care").status_code == 200

    assert Unit.objects.get(name="ER").rules["skill_needs"] == [
        need("D", "Critical care"), need("N", "BLS"), need("N", "Critical care", 2, 3)]
    assert Unit.objects.get(name="Old").rules["skill_needs"] == [need("N", "Critical care")]
    assert Unit.objects.get(name="CCU").rules["skill_needs"] == [need("N", "BLS")]


def test_a_skill_rename_is_audited_for_each_unit_it_changes_and_leaves_the_flag_alone(cno):
    reviewed = unit("ER", [need("N", "ICU")])
    Unit.objects.filter(pk=reviewed.pk).update(rules_need_review=False)
    unit("CCU", [need("N", "BLS")])

    change_skill(cno, "ICU", name="Critical care")

    [entry] = AuditEntry.objects.filter(action="unit_rules.changed")
    assert (entry.target, entry.actor.username) == ("ER", "chief")
    assert entry.before == {"skill_needs": [need("N", "ICU")]}
    assert entry.after == {"skill_needs": [need("N", "Critical care")]}
    assert Unit.objects.get(name="ER").rules_need_review is False


def test_a_skill_is_renamed_atomically_with_the_units_needs(cno, monkeypatch):
    unit("ER", [need("N", "ICU")])

    def fails(*_):
        raise RuntimeError("boom")
    monkeypatch.setattr("hospital.catalogue_api._write_skill", fails)
    cno.raise_request_exception = False

    assert change_skill(cno, "ICU", name="Critical care").status_code == 500
    assert Unit.objects.get(name="ER").rules["skill_needs"] == [need("N", "ICU")]
    assert not SkillRename.objects.exists()


def test_after_a_skill_rename_the_manager_saves_the_need_under_its_new_name(cno, mona, er):
    put(mona, er, body(skill_needs=[need()]))
    change_skill(cno, "ICU", name="Critical care")
    er.refresh_from_db()

    assert put(mona, er, body(skill_needs=er.rules["skill_needs"])).status_code == 200
    assert ("rules", "skill_needs", 0, "skill") in refused_fields(put(mona, er, body(skill_needs=[need()])))


# ---- the shift catalogue --------------------------------------------------------------------------
def test_a_shift_rename_carries_through_the_needs(cno):
    unit("ER", [need("D", "ICU"), need("N", "ICU")])

    assert change_shift(cno, "N", code="NT").status_code == 200

    assert Unit.objects.get(name="ER").rules["skill_needs"] == [need("D", "ICU"), need("NT", "ICU")]


def test_a_unit_created_after_a_retirement_has_no_needs_to_lose(cno):
    Shift.objects.filter(code="D").update(active=False)

    response = cno.post("/api/units", {"name": "New", "manager_id": None}, content_type="application/json")

    assert Unit.objects.get(pk=response.json()["id"]).rules["skill_needs"] == []
