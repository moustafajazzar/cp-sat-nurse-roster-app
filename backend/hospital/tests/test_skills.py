"""The skill catalogue: the CNO's list of skills, which every role reads, with its third mark, "Lets a nurse precept", and
the kind and the issuing body. The refusals that depend on nurses' records and unit rules are tested elsewhere."""
import datetime as dt

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from accounts.models import Role
from audit.models import AuditEntry
from hospital.models import Skill
from conftest import post
from nurses import preceptors
from nurses.models import NurseProfile, NurseSkill, PreceptorAssignment
from units.models import Unit
from nurses.tests.conftest import added, make_unit

pytestmark = pytest.mark.django_db

SKILLS = "/api/hospital/skills"
NEW = {"name": "BLS", "kind": "skill", "issuing_body": "", "expires": True, "required_to_work": True,
       "lets_a_nurse_precept": False, "active": True}


def create(client, **fields):
    return client.post(SKILLS, NEW | fields, content_type="application/json")


def editable(row: dict) -> dict:
    return {key: value for key, value in row.items() if key not in ("id", "renames")}


def update(client, current_name: str, /, **changes):
    row = next(row for row in client.get(SKILLS).json() if row["name"] == current_name)
    return client.put(f"{SKILLS}/{row['id']}", editable(row) | changes, content_type="application/json")


def refused_fields(response) -> set[tuple]:
    assert response.status_code == 422, response.content
    return {tuple(error["loc"][2:]) for error in response.json()["detail"]}


def messages(response) -> list[str]:
    return [error["msg"] for error in response.json()["detail"]]


def names(client) -> list[str]:
    return [row["name"] for row in client.get(SKILLS).json()]


def marked(client) -> list[str]:
    return [row["name"] for row in client.get(SKILLS).json() if row["lets_a_nurse_precept"]]


@pytest.fixture
def cno(signed_in):
    client = signed_in(Role.CNO, username="the-cno")
    for name, expires, required in [("ICU", False, False), ("ACLS", True, False), ("BLS", True, True)]:
        create(client, name=name, expires=expires, required_to_work=required)
    return client


# ---- the list and the form ------------------------------------------------------------------------
def test_the_catalogue_starts_empty(make_user, signed_in):
    # The spec names no skill: a hospital's are its own.
    make_user("admin", Role.SYSTEM_ADMIN)

    call_command("bootstrap")

    assert signed_in(Role.CNO).get(SKILLS).json() == []


def test_the_cno_adds_a_skill_with_both_flags(signed_in):
    client = signed_in(Role.CNO)

    response = create(client, name="  Preceptor ", expires=False, required_to_work=False)

    assert response.status_code == 201
    assert editable(response.json()) == {"name": "Preceptor", "kind": "skill", "issuing_body": "", "expires": False,
                                         "required_to_work": False, "lets_a_nurse_precept": False, "active": True}
    assert response.json()["renames"] == []
    assert client.get(SKILLS).json() == [response.json()]


def test_the_list_is_in_alphabetical_order_whatever_the_case_with_retired_skills_on_it(cno):
    create(cno, name="aaa", expires=False, active=False)

    assert names(cno) == ["aaa", "ACLS", "BLS", "ICU"]


def test_the_cno_changes_both_flags(cno):
    response = update(cno, "ICU", expires=True, required_to_work=True)

    assert response.status_code == 200
    assert editable(response.json()) == {"name": "ICU", "kind": "skill", "issuing_body": "", "expires": True,
                                         "required_to_work": True, "lets_a_nurse_precept": False, "active": True}
    assert update(cno, "ICU", expires=False, required_to_work=False).json()["expires"] is False


def test_a_retired_skill_stays_on_the_list_and_comes_back(cno):
    retired = update(cno, "ACLS", active=False)
    assert retired.status_code == 200 and retired.json()["active"] is False
    assert "ACLS" in names(cno)

    assert update(cno, "ACLS", active=True).json()["active"] is True


def test_there_is_no_way_to_delete_a_skill(cno):
    row = cno.get(SKILLS).json()[0]

    assert cno.delete(f"{SKILLS}/{row['id']}").status_code == 405


@pytest.mark.parametrize("fields, field", [
    ({"name": "   "}, "name"),
    ({"name": "x" * 101}, "name"),
    ({"name": "a\x00b"}, "name"),
    ({"name": "=ICU"}, "name"),  # The solver refuses a name a spreadsheet reads as a formula.
    ({"name": "+ICU"}, "name"),
    ({"name": "-ICU"}, "name"),
    ({"name": "@ICU"}, "name"),
    ({"name": "'ICU"}, "name"),
    ({"name": "IC\nU"}, "name"),
    ({"expires": "yes"}, "expires"),
    ({"required_to_work": 1}, "required_to_work"),
    ({"lets_a_nurse_precept": "no"}, "lets_a_nurse_precept"),
    ({"active": None}, "active"),
    ({"kind": "degree"}, "kind"),
    ({"kind": "Licence"}, "kind"),
    ({"kind": None}, "kind"),
    ({"issuing_body": "x" * 101}, "issuing_body"),
    ({"issuing_body": "a\x00b"}, "issuing_body"),
    ({"issuing_body": None}, "issuing_body"),
    ({"colour": "#000000"}, "colour"),
])
def test_a_refusal_names_the_field(signed_in, fields, field):
    assert refused_fields(create(signed_in(Role.CNO), **fields)) == {(field,)}


@pytest.mark.parametrize("field", ["kind", "issuing_body", "expires", "required_to_work", "lets_a_nurse_precept", "active"])
def test_every_field_must_be_given(signed_in, field):
    body = {key: value for key, value in NEW.items() if key != field}

    response = signed_in(Role.CNO).post(SKILLS, body, content_type="application/json")

    assert refused_fields(response) == {(field,)}


def test_an_unknown_skill_is_404(signed_in):
    response = signed_in(Role.CNO).put(f"{SKILLS}/019a0000-0000-7000-8000-000000000000", NEW,
                                       content_type="application/json")

    assert response.status_code == 404


# ---- "Lets a nurse precept" -------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_every_skill_there_was_before_the_mark_has_it_off(signed_in):
    # Goes back to 0005 and forward again: every later migration of `hospital`, and any that depends on one, must reverse.
    executor = MigrationExecutor(connection)
    executor.migrate([("hospital", "0005_skill_shortfall_weight")])
    before = executor.loader.project_state([("hospital", "0005_skill_shortfall_weight")]).apps
    before.get_model("hospital", "Skill").objects.create(name="BLS", expires=True, required_to_work=True)

    MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())

    (row,) = signed_in(Role.STAFF_NURSE).get(SKILLS).json()
    assert (row["name"], row["lets_a_nurse_precept"]) == ("BLS", False)


def test_the_cno_adds_a_skill_that_lets_a_nurse_precept(signed_in):
    client = signed_in(Role.CNO)

    response = create(client, name="Preceptor course", expires=True, required_to_work=False, lets_a_nurse_precept=True)

    assert response.status_code == 201 and response.json()["lets_a_nurse_precept"] is True
    assert client.get(SKILLS).json()[0]["lets_a_nurse_precept"] is True


def test_the_cno_sets_and_clears_the_mark_on_more_than_one_skill(cno):
    assert update(cno, "BLS", lets_a_nurse_precept=True).json()["lets_a_nurse_precept"] is True
    assert update(cno, "ICU", lets_a_nurse_precept=True).json()["lets_a_nurse_precept"] is True
    assert marked(cno) == ["BLS", "ICU"]

    cleared = update(cno, "BLS", lets_a_nurse_precept=False)

    assert cleared.status_code == 200 and cleared.json()["lets_a_nurse_precept"] is False
    assert marked(cno) == ["ICU"]


def test_the_mark_is_cleared_while_a_nurse_holds_the_skill(cno):
    # Unlike turning `expires` on, nothing refuses it: the skill is checked only when a preceptor is assigned.
    update(cno, "ICU", lets_a_nurse_precept=True)
    nurse_id = added(cno, make_unit("ER"))["nurse"]["id"]
    icu = Skill.objects.get(name="ICU")
    assert post(cno, f"/api/nurses/{nurse_id}/skills", {"skill_id": str(icu.pk), "expiry": None, "identifier": None,
                                                          "issue_date": None}).status_code == 201
    assert NurseSkill.objects.current().filter(skill=icu).exists()

    # She precepts a nurse on probation by it, and clearing the mark takes her preceptor from nobody.
    on_probation = NurseProfile.objects.get(pk=added(cno, Unit.objects.get(name="ER"), "h-new", start_date="2026-01-01",
                                                     on_probation=True)["nurse"]["id"])
    preceptor = NurseProfile.objects.get(pk=nurse_id)
    PreceptorAssignment.objects.create(nurse=on_probation, preceptor=preceptor, unit=Unit.objects.get(name="ER"),
                                       from_month=dt.date(2026, 1, 1), assigned_by=on_probation.user)

    response = update(cno, "ICU", lets_a_nurse_precept=False)

    assert response.status_code == 200 and marked(cno) == []
    assert NurseSkill.objects.current().filter(skill=icu).exists()
    assert preceptors.assignment_in(on_probation, dt.date(2026, 2, 1)).preceptor == preceptor


def test_setting_and_clearing_the_mark_are_audited_with_its_value(cno):
    AuditEntry.objects.all().delete()

    update(cno, "ICU", lets_a_nurse_precept=True)
    update(cno, "ICU", lets_a_nurse_precept=False)

    first, second = AuditEntry.objects.order_by("pk")
    assert (first.action, first.target, first.actor.username) == ("skill.changed", "ICU", "the-cno")
    assert first.before == {"lets_a_nurse_precept": False} and first.after == {"lets_a_nurse_precept": True}
    assert (second.action, second.target) == ("skill.changed", "ICU")
    assert second.before == {"lets_a_nurse_precept": True} and second.after == {"lets_a_nurse_precept": False}


def test_adding_a_marked_skill_is_audited_with_the_mark(signed_in):
    client = signed_in(Role.CNO)
    AuditEntry.objects.all().delete()

    create(client, name="Preceptor course", expires=False, required_to_work=False, lets_a_nurse_precept=True)

    entry = AuditEntry.objects.get()
    assert entry.action == "skill.created"
    assert entry.after == {"name": "Preceptor course", "kind": "skill", "issuing_body": "", "expires": False,
                           "required_to_work": False, "lets_a_nurse_precept": True, "active": True}


@pytest.mark.parametrize("role", list(Role))
def test_every_role_reads_the_mark(signed_in, cno, role):
    update(cno, "BLS", lets_a_nurse_precept=True)
    reader = cno if role == Role.CNO else signed_in(role, username=f"a-{role}")

    assert marked(reader) == ["BLS"]


@pytest.mark.parametrize("role", [role for role in Role if role != Role.CNO])
def test_only_the_cno_sets_or_clears_the_mark(signed_in, cno, role):
    update(cno, "BLS", lets_a_nurse_precept=True)
    client = signed_in(role, username=f"a-{role}")
    AuditEntry.objects.all().delete()

    assert update(client, "ICU", lets_a_nurse_precept=True).status_code == 403
    assert update(client, "BLS", lets_a_nurse_precept=False).status_code == 403
    assert create(client, name="Preceptor course", lets_a_nurse_precept=True).status_code == 403
    assert marked(cno) == ["BLS"] and names(cno) == ["ACLS", "BLS", "ICU"]
    assert not AuditEntry.objects.exists()


# ---- the kind and the issuing body -------------------------------------------------------------------
def kinds(client) -> dict[str, tuple[str, str]]:
    return {row["name"]: (row["kind"], row["issuing_body"]) for row in client.get(SKILLS).json()}


def record_on(cno, skill_name: str, full_name: str, hospital_id: str, unit, expiry: str | None = None,
              identifier: str | None = None) -> str:
    nurse_id = added(cno, unit, hospital_id, full_name=full_name)["nurse"]["id"]
    skill = Skill.objects.get(name=skill_name)
    response = post(cno, f"/api/nurses/{nurse_id}/skills", {"skill_id": str(skill.pk), "expiry": expiry,
                                                            "identifier": identifier, "issue_date": None})
    assert response.status_code == 201, response.content
    return nurse_id


@pytest.mark.django_db(transaction=True)
def test_every_entry_there_was_before_the_kind_is_a_skill_with_no_issuing_body(signed_in):
    # Goes back to 0006, which reverses 0007, and forward again, as the precept mark's test does.
    executor = MigrationExecutor(connection)
    executor.migrate([("hospital", "0006_skill_lets_a_nurse_precept")])
    before = executor.loader.project_state([("hospital", "0006_skill_lets_a_nurse_precept")]).apps
    before.get_model("hospital", "Skill").objects.create(name="BLS", expires=True, required_to_work=True)

    MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())

    assert kinds(signed_in(Role.STAFF_NURSE)) == {"BLS": ("skill", "")}


def test_the_cno_adds_a_certificate_and_a_licence_with_their_issuing_bodies(signed_in):
    client = signed_in(Role.CNO)

    degree = create(client, name="BSc Nursing", kind="certificate", issuing_body="  King Saud University ", expires=False,
                    required_to_work=False)
    licence = create(client, name="SCFHS registration", kind="licence",
                     issuing_body="Saudi Commission for Health Specialties", expires=True, required_to_work=True)

    assert degree.status_code == 201 and licence.status_code == 201
    assert (degree.json()["kind"], degree.json()["issuing_body"]) == ("certificate", "King Saud University")
    assert kinds(client) == {"BSc Nursing": ("certificate", "King Saud University"),
                             "SCFHS registration": ("licence", "Saudi Commission for Health Specialties")}


def test_the_issuing_body_may_be_empty_or_a_hundred_characters(signed_in):
    client = signed_in(Role.CNO)

    assert create(client, name="ICU", kind="certificate", issuing_body="   ").json()["issuing_body"] == ""
    assert create(client, name="ACLS", issuing_body="x" * 100).json()["issuing_body"] == "x" * 100


def test_the_cno_changes_the_kind_and_the_issuing_body(cno):
    response = update(cno, "ACLS", kind="certificate", issuing_body="American Heart Association")

    assert response.status_code == 200
    assert kinds(cno)["ACLS"] == ("certificate", "American Heart Association")
    assert update(cno, "ACLS", kind="skill", issuing_body="").status_code == 200
    assert kinds(cno)["ACLS"] == ("skill", "")


def test_a_licence_always_expires(cno):
    created = create(cno, name="SCFHS registration", kind="licence", expires=False)
    changed = update(cno, "ICU", kind="licence")  # ICU does not expire.
    assert update(cno, "BLS", kind="licence").status_code == 200  # BLS does.
    turned_off = update(cno, "BLS", expires=False)

    for response in (created, changed, turned_off):
        assert refused_fields(response) == {("expires",)}
        assert messages(response) == ["A licence always expires."]
    assert kinds(cno) == {"ACLS": ("skill", ""), "BLS": ("licence", ""), "ICU": ("skill", "")}
    assert names(cno) == ["ACLS", "BLS", "ICU"]


def test_a_change_to_licence_is_refused_naming_the_nurses_whose_record_has_no_expiry(cno):
    er = make_unit("ER")
    record_on(cno, "ICU", "Amal Saleh", "h1", er, identifier="ICU-1")
    record_on(cno, "ICU", "Badr Ali", "h2", er, identifier="ICU-2")
    AuditEntry.objects.all().delete()

    response = update(cno, "ICU", kind="licence", expires=True)

    assert refused_fields(response) == {("kind",)}
    assert messages(response) == ["Amal Saleh and Badr Ali hold ICU with no expiry, and a licence always expires. "
                                  "Take it off them first, then record it again with its expiry."]
    assert kinds(cno)["ICU"] == ("skill", "") and Skill.objects.get(name="ICU").expires is False
    assert not AuditEntry.objects.exists()


def test_the_refusal_names_four_at_most_then_counts_the_rest(cno):
    er = make_unit("ER")
    for number in range(4):
        record_on(cno, "ICU", f"Nurse {number}", f"h{number}", er)
    assert messages(update(cno, "ICU", kind="licence", expires=True))[0].startswith(
        "Nurse 0, Nurse 1, Nurse 2 and Nurse 3 hold ICU with no expiry")

    record_on(cno, "ICU", "Nurse 4", "h4", er)
    record_on(cno, "ICU", "Nurse 5", "h5", er)
    assert messages(update(cno, "ICU", kind="licence", expires=True))[0].startswith(
        "Nurse 0, Nurse 1, Nurse 2 and 3 more hold ICU with no expiry")


def test_a_change_to_licence_is_refused_naming_the_nurses_whose_record_has_no_identifier(cno):
    er = make_unit("ER")
    record_on(cno, "BLS", "Amal Saleh", "h1", er, expiry="2027-06-30", identifier="BLS-1")
    badr = record_on(cno, "BLS", "Badr Ali", "h2", er, expiry="2027-06-30")

    response = update(cno, "BLS", kind="licence", issuing_body="Saudi Commission for Health Specialties")

    assert refused_fields(response) == {("kind",)}
    assert messages(response) == ["Badr Ali holds BLS with no identifier, and a licence always has one. "
                                  "Renew it with its identifier on the Nurses page first."]
    assert kinds(cno)["BLS"] == ("skill", "")
    renewed = cno.put(f"/api/nurses/{badr}/skills/{Skill.objects.get(name='BLS').pk}",
                      {"expiry": "2027-06-30", "identifier": "BLS-2"}, content_type="application/json")
    assert renewed.status_code == 200, renewed.content

    response = update(cno, "BLS", kind="licence", issuing_body="Saudi Commission for Health Specialties")

    assert response.status_code == 200 and kinds(cno)["BLS"] == ("licence", "Saudi Commission for Health Specialties")


def test_a_change_away_from_licence_is_free(cno):
    record_on(cno, "BLS", "Amal Saleh", "h1", make_unit("ER"), expiry="2027-06-30", identifier="BLS-1")
    update(cno, "BLS", kind="licence")

    assert update(cno, "BLS", kind="certificate").status_code == 200
    update(cno, "BLS", kind="licence")
    assert update(cno, "BLS", kind="skill", expires=False).status_code == 200
    assert kinds(cno)["BLS"] == ("skill", "") and Skill.objects.get(name="BLS").expires is False


def test_renames_retirement_and_the_marks_treat_a_licence_as_any_skill(cno):
    update(cno, "BLS", kind="licence")

    renamed = update(cno, "BLS", name="Basic life support", lets_a_nurse_precept=True, required_to_work=False)
    retired = update(cno, "Basic life support", active=False)

    assert renamed.status_code == 200 and [(r["old_name"], r["new_name"]) for r in renamed.json()["renames"]] == [
        ("BLS", "Basic life support")]
    assert retired.status_code == 200 and retired.json()["active"] is False
    assert (retired.json()["kind"], retired.json()["lets_a_nurse_precept"]) == ("licence", True)


def test_adding_is_audited_with_the_kind_and_the_issuing_body(signed_in):
    client = signed_in(Role.CNO)
    AuditEntry.objects.all().delete()

    create(client, name="SCFHS registration", kind="licence", issuing_body="Saudi Commission for Health Specialties")

    entry = AuditEntry.objects.get()
    assert entry.action == "skill.created"
    assert entry.after == {"name": "SCFHS registration", "kind": "licence",
                           "issuing_body": "Saudi Commission for Health Specialties", "expires": True,
                           "required_to_work": True, "lets_a_nurse_precept": False, "active": True}


def test_a_change_of_kind_and_issuing_body_is_audited_with_their_values(cno):
    AuditEntry.objects.all().delete()

    update(cno, "ACLS", kind="certificate", issuing_body="American Heart Association")

    entry = AuditEntry.objects.get()
    assert (entry.action, entry.target) == ("skill.changed", "ACLS")
    assert entry.before == {"kind": "skill", "issuing_body": ""}
    assert entry.after == {"kind": "certificate", "issuing_body": "American Heart Association"}


@pytest.mark.parametrize("role", list(Role))
def test_every_role_reads_the_kind_and_the_issuing_body(signed_in, cno, role):
    update(cno, "BLS", kind="licence", issuing_body="Saudi Heart Association")
    reader = cno if role == Role.CNO else signed_in(role, username=f"a-{role}")

    assert kinds(reader) == {"ACLS": ("skill", ""), "BLS": ("licence", "Saudi Heart Association"), "ICU": ("skill", "")}


@pytest.mark.parametrize("role", [role for role in Role if role != Role.CNO])
def test_only_the_cno_sets_the_kind_or_the_issuing_body(signed_in, cno, role):
    client = signed_in(role, username=f"a-{role}")
    AuditEntry.objects.all().delete()

    assert update(client, "BLS", kind="licence").status_code == 403
    assert update(client, "ICU", issuing_body="A university").status_code == 403
    assert create(client, name="SCFHS registration", kind="licence").status_code == 403
    assert kinds(cno) == {"ACLS": ("skill", ""), "BLS": ("skill", ""), "ICU": ("skill", "")}
    assert not AuditEntry.objects.exists()


# ---- a taken name ---------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["icu", "ICU", "Icu "])
def test_a_name_cannot_be_used_twice_in_any_case(cno, name):
    created = create(cno, name=name)
    assert refused_fields(created) == {("name",)} and messages(created) == ["ICU is already a skill"]
    assert refused_fields(update(cno, "BLS", name=name)) == {("name",)}


def test_a_skill_may_change_only_the_case_of_its_own_name(cno):
    response = update(cno, "ICU", name="icu")

    assert response.status_code == 200 and response.json()["name"] == "icu"


def test_a_name_another_skill_used_to_hold_cannot_be_taken(cno):
    # A stored input that says "BLS" means the skill now called "Basic life support", so it cannot also mean another.
    update(cno, "BLS", name="Basic life support")

    created = create(cno, name="bls")
    assert refused_fields(created) == {("name",)} and messages(created) == ["BLS is the old name of another skill"]
    assert refused_fields(update(cno, "ICU", name="BLS")) == {("name",)}


def test_a_skill_may_go_back_to_a_name_of_its_own(cno):
    update(cno, "BLS", name="Basic life support")

    response = update(cno, "Basic life support", name="BLS")

    assert response.status_code == 200 and response.json()["name"] == "BLS"


# ---- a rename -------------------------------------------------------------------------------------
def test_a_rename_is_recorded_with_the_old_name_the_new_name_and_the_date(cno):
    response = update(cno, "BLS", name="Basic life support")

    assert response.status_code == 200 and response.json()["name"] == "Basic life support"
    (rename,) = response.json()["renames"]
    assert rename["old_name"] == "BLS" and rename["new_name"] == "Basic life support"
    assert rename["at"].endswith("+03:00")
    assert response.json() in cno.get(SKILLS).json()


def test_each_rename_of_a_skill_is_kept_in_order(cno):
    update(cno, "BLS", name="Basic")
    update(cno, "Basic", name="Life support")

    renames = update(cno, "Life support", name="BLS").json()["renames"]

    assert [(rename["old_name"], rename["new_name"]) for rename in renames] == [
        ("BLS", "Basic"), ("Basic", "Life support"), ("Life support", "BLS")]


def test_a_change_of_case_is_a_rename_too(cno):
    # A stored input names a skill exactly as it was spelled then.
    (rename,) = update(cno, "ICU", name="Icu").json()["renames"]

    assert (rename["old_name"], rename["new_name"]) == ("ICU", "Icu")


def test_a_change_of_flags_records_no_rename(cno):
    assert update(cno, "ICU", expires=True, active=False).json()["renames"] == []


# ---- the audit log --------------------------------------------------------------------------------
def test_adding_is_audited_with_every_value(signed_in):
    client = signed_in(Role.CNO, username="the-cno")
    AuditEntry.objects.all().delete()

    create(client, name="ICU", expires=False, required_to_work=True)

    entry = AuditEntry.objects.get()
    assert entry.action == "skill.created" and entry.actor.username == "the-cno" and entry.target == "ICU"
    assert entry.before is None
    assert entry.after == {"name": "ICU", "kind": "skill", "issuing_body": "", "expires": False,
                           "required_to_work": True, "lets_a_nurse_precept": False, "active": True}


def test_a_change_is_audited_with_only_what_changed(cno):
    AuditEntry.objects.all().delete()

    update(cno, "ICU", required_to_work=True)

    entry = AuditEntry.objects.get()
    assert entry.action == "skill.changed" and entry.actor.username == "the-cno" and entry.target == "ICU"
    assert entry.before == {"required_to_work": False} and entry.after == {"required_to_work": True}


def test_a_rename_is_audited_under_the_new_name(cno):
    AuditEntry.objects.all().delete()

    update(cno, "BLS", name="Basic life support")

    entry = AuditEntry.objects.get()
    assert entry.action == "skill.changed" and entry.target == "Basic life support"
    assert entry.before == {"name": "BLS"} and entry.after == {"name": "Basic life support"}


def test_a_retire_is_audited(cno):
    AuditEntry.objects.all().delete()

    update(cno, "ACLS", active=False)

    entry = AuditEntry.objects.get()
    assert entry.action == "skill.changed" and entry.target == "ACLS"
    assert entry.before == {"active": True} and entry.after == {"active": False}


def test_saving_with_no_change_writes_no_entry_and_no_rename(cno):
    AuditEntry.objects.all().delete()

    response = update(cno, "ICU")

    assert response.status_code == 200 and response.json()["renames"] == []
    assert not AuditEntry.objects.exists()


def test_a_refused_change_writes_nothing(cno):
    AuditEntry.objects.all().delete()

    update(cno, "BLS", name="icu")

    assert not AuditEntry.objects.exists()
    assert names(cno) == ["ACLS", "BLS", "ICU"]
    assert all(row["renames"] == [] for row in cno.get(SKILLS).json())


# ---- who may ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("role", list(Role))
def test_every_role_reads_the_catalogue(signed_in, cno, role):
    reader = cno if role == Role.CNO else signed_in(role, username=f"a-{role}")  # The hospital has one CNO.

    assert names(reader) == ["ACLS", "BLS", "ICU"]


def test_a_stranger_reads_nothing(client, cno):
    assert client.get(SKILLS).status_code == 401


@pytest.mark.parametrize("role", [role for role in Role if role != Role.CNO])
def test_only_the_cno_changes_it(signed_in, cno, role):
    client = signed_in(role, username=f"a-{role}")
    AuditEntry.objects.all().delete()

    assert create(client, name="Preceptor").status_code == 403
    assert update(client, "ICU", name="Intensive care").status_code == 403
    assert names(cno) == ["ACLS", "BLS", "ICU"] and not AuditEntry.objects.exists()


def test_a_change_to_licence_names_both_refusals_at_once(cno):
    record_on(cno, "ICU", "Amal Saleh", "h1", make_unit("ER"))

    response = update(cno, "ICU", kind="licence", expires=True)

    assert refused_fields(response) == {("kind",)}
    assert messages(response) == ["Amal Saleh holds ICU with no expiry, and a licence always expires. Take it off them first, "
                                  "then record it again with its expiry. Amal Saleh holds ICU with no identifier, and a "
                                  "licence always has one. Renew it with its identifier on the Nurses page first."]
