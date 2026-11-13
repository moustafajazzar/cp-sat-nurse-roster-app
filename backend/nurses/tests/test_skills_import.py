"""The skills import: the CNO records and renews nurses' skills from a CSV of hospital ID, skill and expiry, read
as the nurse import's file is (its encodings and limits are tested in `test_files.py`, and here only that they are shared),
previewed row by row and confirmed by sending the file again."""
import codecs

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from accounts.models import Role, User
from audit.models import AuditEntry
from conftest import post
from hospital.models import HospitalSettings, Skill, SkillRename
from nurses import importing
from nurses.models import NurseSkill
from nurses.tests.conftest import NURSES, added
from nurses.tests.test_skills import change_skill, held, on_nurses_page, recorded, renew

pytestmark = pytest.mark.django_db

SKILLS = "/api/hospital/skills"
TEMPLATE = f"{NURSES}/skills-import/template"
PREVIEW = f"{NURSES}/skills-import/preview"
CONFIRM = f"{NURSES}/skills-import/confirm"
HEADER = "Hospital ID,Skill,Expiry"


@pytest.fixture(autouse=True)
def today(on_day):
    on_day(2026, 11, 5)


@pytest.fixture
def catalogue(cno):
    """BLS expires and is required to work, ICU does not expire, ACLS expires."""
    for name, expires, required in [("BLS", True, True), ("ICU", False, False), ("ACLS", True, False)]:
        response = post(cno, SKILLS, {"name": name, "kind": "skill", "issuing_body": "", "expires": expires,
                                     "required_to_work": required, "lets_a_nurse_precept": False, "active": True})
        assert response.status_code == 201, response.content
    return {skill.name: str(skill.pk) for skill in Skill.objects.all()}


@pytest.fixture
def amal(cno, er, catalogue) -> str:
    return added(cno, er, "h1", full_name="Amal Saleh")["nurse"]["id"]


@pytest.fixture
def badr(cno, er, catalogue) -> str:
    return added(cno, er, "H2", full_name="Badr Omar", gender="M")["nurse"]["id"]


def rename(client, name, new_name):
    row = next(row for row in client.get(SKILLS).json() if row["name"] == name)
    body = {key: row[key] for key in ("kind", "issuing_body", "expires", "required_to_work", "lets_a_nurse_precept",
                                   "active")} | {"name": new_name}
    return client.put(f"{SKILLS}/{row['id']}", body, content_type="application/json")


def upload(content, name="skills.csv"):
    return SimpleUploadedFile(name, content.encode() if isinstance(content, str) else content, content_type="text/csv")


def csv_of(*rows) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


def preview(client, content):
    return client.post(PREVIEW, {"file": upload(content)})


def previewed(client, content) -> dict:
    response = preview(client, content)
    assert response.status_code == 200, response.content
    return response.json()


def confirm(client, content, token):
    return client.post(CONFIRM, {"file": upload(content), "token": token})


def confirmed(client, content) -> dict:
    response = confirm(client, content, previewed(client, content)["token"])
    assert response.status_code == 200, response.content
    return response.json()


def problems_of(client, *rows) -> list:
    body = previewed(client, csv_of(*rows))
    assert body["token"] is None
    return [[(problem["column"], problem["message"]) for problem in row["problems"]] for row in body["rows"]]


def file_refusal(response) -> str:
    assert response.status_code == 422, response.content
    detail = response.json()["detail"]
    assert detail[0]["loc"][2:] == ["file"]
    return detail[0]["msg"]


# ---- the template ---------------------------------------------------------------------------------------------------
def test_the_template_has_the_five_columns_and_a_byte_order_mark_for_excel(cno):
    response = cno.get(TEMPLATE)

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    assert 'filename="skills-import-template.csv"' in response["Content-Disposition"]
    assert response["Cache-Control"] == "no-store"
    assert response.content == codecs.BOM_UTF8 + b'"Hospital ID","Skill","Expiry","Identifier","Issue date"\r\n'


def test_the_template_is_read_as_a_file_with_no_skills_and_writes_no_audit_entry(cno):
    before = AuditEntry.objects.count()

    template = cno.get(TEMPLATE).content

    assert AuditEntry.objects.count() == before
    assert file_refusal(preview(cno, template)) == "This file has no skills below its first line. Add one skill per line."


def test_the_nurse_import_template_names_no_skill(cno):
    assert cno.get(f"{NURSES}/import/template").content == (
        codecs.BOM_UTF8 + b'"Hospital ID","Full name","Gender","Unit","Start date","On probation"\r\n')


@pytest.mark.parametrize("role", [role for role in Role if role not in (Role.CNO, Role.DEPUTY_CNO)])
def test_only_the_cno_and_the_deputy_cno_import_skills(client_of, role, amal):
    other = client_of("other", role)
    content = csv_of("h1,ICU,")

    assert other.get(TEMPLATE).status_code == 403
    assert preview(other, content).status_code == 403
    assert confirm(other, content, "x").status_code == 403
    assert Client().post(PREVIEW, {"file": upload(content)}).status_code == 401
    assert not NurseSkill.objects.exists()


def test_the_deputy_cno_takes_the_template_previews_and_confirms_as_the_cno_does(cno, client_of, amal, catalogue):
    """The bulk renewal is not the CNO's alone. The audit entry names the Deputy's hand."""
    deputy = client_of("hala", Role.DEPUTY_CNO)
    recorded(cno, amal, catalogue["BLS"], "2026-12-31")
    AuditEntry.objects.all().delete()

    template = deputy.get(TEMPLATE)
    assert template.status_code == 200
    assert template.content == cno.get(TEMPLATE).content
    content = csv_of("h1,BLS,2027-12-31", "h1,ICU,")
    rows = previewed(deputy, content)["rows"]
    assert [(row["skill"], row["change"], row["held_expiry"]) for row in rows] == [
        ("BLS", "renew", "2026-12-31"), ("ICU", "record", None)]

    assert confirmed(deputy, content) == {"recorded": 1, "renewed": 1, "unchanged": 0}

    assert on_nurses_page(cno, "h1") == {"BLS": "2027-12-31", "ICU": None}
    assert {row.recorded_by.username for row in NurseSkill.objects.current()} == {"hala"}
    summary = AuditEntry.objects.get(action="nurse.skills_imported")
    assert (summary.actor.username, summary.target, summary.after) == ("hala", "", {"count": 2})


def test_a_deputy_cno_deactivated_since_the_preview_changes_nothing(client_of, amal):
    deputy = client_of("hala", Role.DEPUTY_CNO)
    content = csv_of("h1,ICU,")
    token = previewed(deputy, content)["token"]
    User.objects.filter(username="hala").update(is_active=False)

    assert confirm(deputy, content, token).status_code in (401, 403)
    assert not NurseSkill.objects.exists()


# ---- what a preview reports -----------------------------------------------------------------------------------------
def test_a_good_file_previews_what_each_row_does_with_a_token_and_saves_nothing(cno, amal, badr, catalogue):
    recorded(cno, amal, catalogue["BLS"], "2026-12-31")
    recorded(cno, amal, catalogue["ICU"])
    entries = AuditEntry.objects.count()

    response = preview(cno, csv_of("h1,bls,2027-12-31", "h1,ICU,", "h2,Acls,2027-03-01"))

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    body = response.json()
    assert body["token"]
    assert [(r["line"], r["hospital_id"], r["full_name"], r["skill"], r["expiry"], r["change"], r["held_expiry"], r["problems"])
            for r in body["rows"]] == [
        (2, "h1", "Amal Saleh", "BLS", "2027-12-31", "renew", "2026-12-31", []),
        (3, "h1", "Amal Saleh", "ICU", "", "none", None, []),
        (4, "H2", "Badr Omar", "ACLS", "2027-03-01", "record", None, []),
    ]
    assert NurseSkill.objects.count() == 2
    assert AuditEntry.objects.count() == entries


def test_every_problem_on_every_row_is_reported_beside_its_column(cno, amal, badr, catalogue, make_user):
    change_skill(cno, "ACLS", active=False)
    make_user("chief2", Role.UNIT_MANAGER)

    assert problems_of(cno,
                       "h1,ICU,",
                       "has space,Nothing,2026-13-40",
                       "nobody,ACLS,2027-01-01",
                       "chief2,BLS,",
                       "h2,ICU,2027-01-01",
                       "h2,BLS,31/12/2027") == [
        [],
        [("hospital_id", "Use 1 to 150 letters, digits, dots, dashes and underscores."),
         ("skill", "There is no such skill. Use its name on the Skills page."),
         ("expiry", "Use a date as YYYY-MM-DD, between 1925 and 2076.")],
        [("hospital_id", "No nurse has this hospital ID. Use the one the Nurses page or Rostering attributes shows."),
         ("skill", "ACLS is retired, so it can no longer be recorded or renewed.")],
        [("hospital_id", "No nurse has this hospital ID. Use the one the Nurses page or Rostering attributes shows."),
         ("expiry", "BLS expires. Enter the last day it counts, as YYYY-MM-DD.")],
        [("expiry", "ICU does not expire, so leave its expiry empty.")],
        [("expiry", "Use a date as YYYY-MM-DD, between 1925 and 2076.")],
    ]


def test_a_deactivated_nurse_is_a_problem(cno, amal):
    response = cno.put(f"{NURSES}/{amal}/end-date", {"end_date": "2026-11-04"}, content_type="application/json")
    assert response.json()["is_active"] is False

    assert problems_of(cno, "h1,ICU,") == [
        [("hospital_id", "Amal Saleh is deactivated. Record their skills on the Nurses page, or remove the line.")]]


def test_a_skill_held_but_retired_cannot_be_renewed_by_file(cno, amal, catalogue):
    recorded(cno, amal, catalogue["BLS"], "2026-12-31")
    change_skill(cno, "BLS", active=False)

    assert problems_of(cno, "h1,BLS,2027-12-31") == [[("skill", "BLS is retired, so it can no longer be recorded or renewed.")]]


def test_a_former_name_is_not_a_skill(cno, amal, catalogue):
    assert rename(cno, "ICU", "Intensive care").status_code == 200
    assert SkillRename.objects.filter(old_name="ICU").exists()

    assert problems_of(cno, "h1,ICU,") == [[("skill", "There is no such skill. Use its name on the Skills page.")]]
    assert previewed(cno, csv_of("h1,intensive CARE,"))["rows"][0]["skill"] == "Intensive care"


def test_a_nurse_and_skill_twice_in_the_file_in_any_case_is_reported_on_both_lines(cno, amal, badr):
    assert problems_of(cno, "h1,BLS,2027-01-01", "h2,BLS,2027-01-01", "H1,bls,2028-01-01") == [
        [("skill", "BLS of h1 is also on line 4. Keep one line for each nurse's skill.")],
        [],
        [("skill", "BLS of h1 is also on line 2. Keep one line for each nurse's skill.")]]


def test_an_expiry_already_past_is_accepted_as_a_fact(cno, amal):
    assert previewed(cno, csv_of("h1,BLS,2025-01-31"))["token"]


def test_a_line_with_too_many_or_too_few_cells_is_reported_once_for_the_whole_line(cno, amal):
    assert problems_of(cno, "h1,BLS,2027-01-01,extra", "h1") == [
        [(None, "This line has 4 cells, not 3. Put double quotes around a skill name that has a comma in it.")],
        [(None, "This line has 1 cells, not 3. Put double quotes around a skill name that has a comma in it.")],
    ]


def test_a_nul_in_a_cell_is_a_problem_and_not_a_server_error(cno, amal):
    assert problems_of(cno, "h1,IC\x00U,") == [[("skill", "There is no such skill. Use its name on the Skills page.")]]
    assert problems_of(cno, "h\x001,ICU,")[0][0][0] == "hospital_id"


# ---- what a file may be: the nurse import's rules, shared -----------------------------------------------------------
def test_utf16_with_a_mark_is_read(cno, amal):
    assert previewed(cno, csv_of("h1,ICU,").replace("\n", "\r\n").encode("utf-16"))["token"]


def test_an_encoding_that_could_only_be_guessed_is_refused(cno, amal):
    assert "CSV UTF-8" in file_refusal(preview(cno, csv_of("h1,Réa,").encode("cp1252")))


@pytest.mark.parametrize("content, words", [
    ("", "This file is empty."),
    ("Hospital ID,Full name,Gender,Unit,Start date\nh1,Amal,F,ER,2026-01-10", "Hospital ID, Skill, Expiry"),
    ("h1,ICU,", "exactly these columns"),
], ids=["empty", "a nurse import file", "no header"])
def test_a_file_that_cannot_be_read_is_refused_as_a_whole(cno, content, words):
    assert words in file_refusal(preview(cno, content))


def test_the_row_and_size_limits_are_the_nurse_import_s(cno, amal):
    rows = ["h1,ICU,"] * importing.MAX_ROWS

    assert len(previewed(cno, csv_of(*rows))["rows"]) == importing.MAX_ROWS
    assert file_refusal(preview(cno, csv_of(*rows, "h1,ICU,"))) == (
        "This file has more than 500 skills. Split it and import each part.")
    assert file_refusal(preview(cno, f"{HEADER}\n" + " " * importing.MAX_BYTES)) == (
        "This file is larger than 1 MB, which is more than 500 skills need.")


def test_a_file_too_large_to_keep_in_memory_never_reaches_the_disk(cno, settings, monkeypatch):
    settings.FILE_UPLOAD_MAX_MEMORY_SIZE = 1000
    monkeypatch.setattr("django.core.files.uploadedfile.TemporaryUploadedFile.__init__",
                        lambda *_, **__: pytest.fail("the upload went to a temporary file"))

    assert preview(cno, f"{HEADER}\n" + "h1,ICU,\n" * 200).status_code == 422


# ---- confirming -----------------------------------------------------------------------------------------------------
def test_confirming_records_new_skills_and_renews_held_ones(cno, amal, badr, catalogue):
    recorded(cno, amal, catalogue["BLS"], "2026-12-31")
    recorded(cno, amal, catalogue["ICU"])
    first = NurseSkill.objects.get(skill__name="BLS")
    content = csv_of("h1,BLS,2027-12-31", "h1,ICU,", "h2,ACLS,2027-03-01", "h2,ICU,")

    response = confirm(cno, content, previewed(cno, content)["token"])

    assert response.status_code == 200, response.content
    assert response["Cache-Control"] == "no-store"
    assert response.json() == {"recorded": 2, "renewed": 1, "unchanged": 1}
    assert on_nurses_page(cno, "h1") == {"BLS": "2027-12-31", "ICU": None}
    assert on_nurses_page(cno, "H2") == {"ACLS": "2027-03-01", "ICU": None}
    first.refresh_from_db()
    assert first.removed_at and first.removed_by.username == "chief", "A renewal ends the row, as by hand."
    renewed = NurseSkill.objects.current().get(skill__name="BLS")
    assert renewed.recorded_by.username == "chief"


def test_a_renewal_of_a_kind_that_no_longer_expires_reads_as_it_does_by_hand(cno, amal, catalogue):
    recorded(cno, amal, catalogue["ACLS"], "2026-12-31")
    assert change_skill(cno, "ACLS", expires=False).status_code == 200

    body = previewed(cno, csv_of("h1,ACLS,"))

    assert (body["rows"][0]["change"], body["rows"][0]["held_expiry"]) == ("none", None)
    assert confirm(cno, csv_of("h1,ACLS,"), body["token"]).json() == {"recorded": 0, "renewed": 0, "unchanged": 1}
    assert NurseSkill.objects.count() == 1


def test_a_file_with_any_problem_saves_nothing_whatever_token_is_sent(cno, amal):
    good_token = previewed(cno, csv_of("h1,ICU,"))["token"]
    bad = csv_of("h1,ICU,", "h1,BLS,")

    for token in ("", "null", good_token):
        response = confirm(cno, bad, token)
        assert response.status_code == 409
        assert response.json()["token"] is None and response.json()["rows"][1]["problems"]
    assert not NurseSkill.objects.exists()
    assert not AuditEntry.objects.filter(action="nurse.skills_imported").exists()


def test_a_file_that_is_not_the_one_previewed_saves_nothing(cno, amal):
    token = previewed(cno, csv_of("h1,BLS,2027-01-01"))["token"]
    other = csv_of("h1,BLS,2027-01-02")

    response = confirm(cno, other, token)

    assert response.status_code == 409
    assert response.json()["token"] == previewed(cno, other)["token"] != token
    assert not NurseSkill.objects.exists()


def test_a_renewal_by_hand_since_the_preview_is_not_what_was_previewed(cno, amal, catalogue):
    recorded(cno, amal, catalogue["BLS"], "2026-12-31")
    content = csv_of("h1,BLS,2027-12-31")
    token = previewed(cno, content)["token"]
    assert cno.put(f"{NURSES}/{amal}/skills/{catalogue['BLS']}", {"expiry": "2027-06-30"},
                   content_type="application/json").status_code == 200

    response = confirm(cno, content, token)

    assert response.status_code == 409
    assert response.json()["rows"][0]["held_expiry"] == "2027-06-30"
    assert response.json()["token"] != token
    assert on_nurses_page(cno, "h1") == {"BLS": "2027-06-30"}


def test_a_skill_renamed_since_the_preview_is_not_what_was_previewed(cno, amal):
    content = csv_of("h1,ICU,")
    token = previewed(cno, content)["token"]
    rename(cno, "ICU", "Intensive care")

    response = confirm(cno, content, token)

    assert response.status_code == 409
    assert response.json()["rows"][0]["problems"][0]["column"] == "skill"
    assert not NurseSkill.objects.exists()


def test_a_change_after_the_first_check_is_caught_under_the_locks(cno, amal, catalogue, monkeypatch):
    """Something done between the check outside the transaction and the locks, as nothing else could arrange. It is done
    inside the confirm's transaction, so the refusal's rollback undoes it too: what is left to see is that nothing was saved."""
    content = csv_of("h1,ICU,")
    token = previewed(cno, content)["token"]
    real = HospitalSettings.load

    def load_after_a_deactivation(*args, **kwargs):
        User.objects.filter(username="h1").update(is_active=False)
        return real(*args, **kwargs)
    monkeypatch.setattr("nurses.files_api.HospitalSettings.load", load_after_a_deactivation)

    response = confirm(cno, content, token)

    assert response.status_code == 409
    assert not NurseSkill.objects.exists()
    assert not AuditEntry.objects.filter(action="nurse.skills_imported").exists()


def test_confirming_twice_changes_nothing_more(cno, amal):
    content = csv_of("h1,BLS,2027-01-01")
    token = previewed(cno, content)["token"]
    assert confirm(cno, content, token).status_code == 200

    response = confirm(cno, content, token)

    assert response.status_code == 409
    assert response.json()["rows"][0]["change"] == "none"
    assert NurseSkill.objects.count() == 1


def test_a_failure_on_the_last_row_undoes_the_first(cno, amal, badr, monkeypatch):
    content = csv_of("h1,ICU,", "h2,ICU,")
    token = previewed(cno, content)["token"]
    real = NurseSkill.objects.create

    def fail_on_second(**fields):
        if fields["nurse"].user.username == "H2":
            raise RuntimeError("the database went away")
        return real(**fields)
    monkeypatch.setattr(NurseSkill.objects, "create", fail_on_second)

    cno.raise_request_exception = False
    assert confirm(cno, content, token).status_code == 500
    assert not NurseSkill.objects.exists()
    assert not AuditEntry.objects.filter(action__startswith="nurse.skills").exists()


def test_a_cno_deactivated_since_the_preview_changes_nothing(cno, amal):
    content = csv_of("h1,ICU,")
    token = previewed(cno, content)["token"]
    User.objects.filter(username="chief").update(is_active=False)

    assert confirm(cno, content, token).status_code in (401, 403)
    assert not NurseSkill.objects.exists()


# ---- what stays behind ----------------------------------------------------------------------------------------------
def test_an_import_is_audited_as_who_and_how_many_rows_and_nothing_from_the_file(cno, amal, badr, catalogue):
    recorded(cno, amal, catalogue["ICU"])
    AuditEntry.objects.all().delete()

    confirmed(cno, csv_of("h1,ICU,", "h1,BLS,2027-12-31", "h2,ACLS,2027-03-01"))

    summary = AuditEntry.objects.get(action="nurse.skills_imported")
    assert summary.actor.username == "chief"
    assert (summary.target, summary.before, summary.after) == ("", None, {"count": 3})
    assert list(AuditEntry.objects.values_list("action", flat=True)) == ["nurse.skills_imported"], (
        "Not even whose skills: a hospital ID is from the file.")
    everything = str(list(AuditEntry.objects.values_list("target", "before", "after")))
    for word in ("BLS", "ICU", "ACLS", "2027"):
        assert word not in everything


def test_a_preview_and_a_refused_confirm_write_no_audit_entry(cno, amal):
    before = AuditEntry.objects.count()

    previewed(cno, csv_of("h1,ICU,"))
    confirm(cno, csv_of("h1,ICU,"), "wrong")

    assert AuditEntry.objects.count() == before


def test_a_file_that_changes_nothing_is_still_audited(cno, amal, catalogue):
    recorded(cno, amal, catalogue["ICU"])
    AuditEntry.objects.all().delete()

    assert confirmed(cno, csv_of("h1,ICU,")) == {"recorded": 0, "renewed": 0, "unchanged": 1}

    assert list(AuditEntry.objects.values_list("action", flat=True)) == ["nurse.skills_imported"]


def test_the_staff_list_holds_no_skills(cno, amal):
    confirmed(cno, csv_of("h1,BLS,2027-01-01"))

    content = cno.get(f"{NURSES}/export").content.decode("utf-8-sig")

    assert content.splitlines()[0] == ('"Hospital ID","Full name","Gender","Unit","Start date","On probation",'
                                       '"Probation\'s last day","End date","Role","Status","Transfers to come"')
    assert "BLS" not in content and "2027-01-01" not in content


def test_skills_imported_show_where_the_nurse_s_skills_are_read(cno, amal):
    confirmed(cno, csv_of("h1,BLS,2027-01-01", "h1,ICU,"))

    assert held(cno.get(NURSES).json()[0]["skills"]) == {"BLS": "2027-01-01", "ICU": None}


# ---- the identifier and the issue date ------------------------------------------------------------------
FULL_HEADER = "Hospital ID,Skill,Expiry,Identifier,Issue date"


def full_csv_of(*rows) -> str:
    """A file of the template's five columns; `csv_of` writes last month's three, which every test above reads."""
    return "\n".join([FULL_HEADER, *rows]) + "\n"


def full_problems_of(client, *rows) -> list:
    body = previewed(client, full_csv_of(*rows))
    assert body["token"] is None
    return [[(problem["column"], problem["message"]) for problem in row["problems"]] for row in body["rows"]]


def now_held(skill_id) -> tuple:
    row = NurseSkill.objects.current().get(skill_id=skill_id)
    return (row.expiry and row.expiry.isoformat(), row.identifier, row.issue_date and row.issue_date.isoformat())


@pytest.fixture
def scfhs(cno) -> str:
    response = post(cno, SKILLS, {"name": "SCFHS", "kind": "licence", "issuing_body": "SCFHS", "expires": True,
                                 "required_to_work": False, "lets_a_nurse_precept": False, "active": True})
    assert response.status_code == 201, response.content
    return response.json()["id"]


@pytest.fixture
def bsn(cno) -> str:
    """A degree: a certificate that does not expire."""
    response = post(cno, SKILLS, {"name": "BSN", "kind": "certificate", "issuing_body": "King Saud University",
                                 "expires": False, "required_to_work": False, "lets_a_nurse_precept": False, "active": True})
    assert response.status_code == 201, response.content
    return response.json()["id"]


def test_a_five_column_file_previews_each_row_with_its_identifier_issue_date_and_kind(cno, amal, badr, scfhs, bsn):
    body = previewed(cno, full_csv_of("h1,SCFHS,2028-03-31, 21-RN-0412 ,2026-04-01", "h2,BSN,,KSU-2019-118,", "h2,ICU,,,"))

    assert body["token"]
    assert [(r["line"], r["skill"], r["kind"], r["expiry"], r["identifier"], r["issue_date"], r["change"])
            for r in body["rows"]] == [
        (2, "SCFHS", "licence", "2028-03-31", "21-RN-0412", "2026-04-01", "record"),
        (3, "BSN", "certificate", "", "KSU-2019-118", "", "record"),
        (4, "ICU", "skill", "", "", "", "record"),
    ]


def test_a_line_of_a_five_column_file_has_five_cells(cno, amal):
    assert full_problems_of(cno, "h1,ICU,") == [
        [(None, "This line has 3 cells, not 5. Put double quotes around a skill name that has a comma in it.")]]


def test_a_header_of_four_columns_is_refused(cno, amal):
    assert file_refusal(preview(cno, "Hospital ID,Skill,Expiry,Identifier\nh1,ICU,,\n")) == (
        "The first line must name exactly these columns: Hospital ID, Skill, Expiry, Identifier, Issue date. "
        "Identifier and Issue date may be left out together. Download the template to start from.")


def test_a_licence_needs_its_identifier_under_either_header(cno, amal, scfhs):
    words = "SCFHS is a licence, so it needs its identifier. Enter it in the Identifier column, as the template has it."

    assert problems_of(cno, "h1,SCFHS,2028-03-31") == [[("identifier", words)]]
    assert full_problems_of(cno, "h1,SCFHS,2028-03-31, ,2026-04-01") == [[("identifier", words)]]


def test_a_licence_held_with_its_identifier_is_still_a_problem_in_last_month_s_file(cno, amal, scfhs):
    """The file says the whole record, and that file has nowhere to say the number."""
    recorded(cno, amal, scfhs, "2026-12-31", "21-RN-0412", "2024-12-15")

    assert problems_of(cno, "h1,SCFHS,2028-12-31")[0][0][0] == "identifier"


IDENTIFIER_WORDS = "Enter the identifier as its issuing body gave it, in at most 100 characters."


def test_an_identifier_or_issue_date_the_record_s_rules_refuse_is_a_problem_beside_it(cno, amal, bsn):
    assert full_problems_of(cno, f"h1,BSN,,{'X' * 101},2026-02-30", "h1,ICU,,A\x00B,soon") == [
        [("identifier", IDENTIFIER_WORDS), ("issue_date", "Use a date as YYYY-MM-DD, between 1925 and 2076.")],
        [("identifier", IDENTIFIER_WORDS), ("issue_date", "Use a date as YYYY-MM-DD, between 1925 and 2076.")],
    ]


def test_an_issue_date_after_the_expiry_or_after_today_is_a_problem(cno, amal, badr, scfhs, bsn):
    assert full_problems_of(cno, "h1,SCFHS,2027-01-31,21-RN-0412,2027-02-01", "h2,BSN,,KSU-1,2026-11-06") == [
        [("issue_date", "The issue date cannot be after the expiry.")],
        [("issue_date", "The issue date cannot be after today.")],
    ]
    assert previewed(cno, full_csv_of("h2,BSN,,KSU-1,2026-11-05"))["token"], "Today is not after today."


def test_an_identifier_another_nurse_s_current_record_of_the_entry_holds_is_a_problem(cno, amal, badr, scfhs, bsn):
    recorded(cno, amal, scfhs, "2026-12-31", "21-RN-0412")
    recorded(cno, amal, bsn, None, "KSU-7")

    assert full_problems_of(cno, "h2,SCFHS,2028-03-31,21-RN-0412,") == [[(
        "identifier", "Amal Saleh already holds SCFHS with this identifier. Check the number: each one is one nurse's.")]]
    assert previewed(cno, full_csv_of("h2,SCFHS,2028-03-31,21-rn-0412,", "h2,BSN,,21-RN-0412,"))["token"], (
        "Compared exactly, and another entry may share it.")
    assert previewed(cno, full_csv_of("h1,SCFHS,2028-12-31,21-RN-0412,"))["rows"][0]["change"] == "renew", (
        "Her own record does not count.")


def test_an_identifier_of_an_ended_record_is_free(cno, amal, badr, scfhs):
    recorded(cno, amal, scfhs, "2026-12-31", "21-RN-0412")
    assert renew(cno, amal, scfhs, "2028-12-31", identifier="21-RN-0999").status_code == 200

    assert previewed(cno, full_csv_of("h2,SCFHS,2028-03-31,21-RN-0412,"))["token"]


def test_an_identifier_repeated_in_the_file_for_one_entry_on_two_nurses_is_a_problem_on_both_lines(cno, amal, badr, scfhs,
                                                                                                    bsn):
    assert full_problems_of(cno, "h1,SCFHS,2028-03-31,21-RN-0412,", "h2,BSN,,21-RN-0412,", "H2,scfhs,2028-03-31,21-RN-0412,"
                            ) == [
        [("identifier", "Line 4 gives H2 this identifier for SCFHS too. Check the number: each one is one nurse's.")],
        [],
        [("identifier", "Line 2 gives h1 this identifier for SCFHS too. Check the number: each one is one nurse's.")],
    ]


def test_an_identifier_another_row_of_the_file_would_free_is_still_held(cno, amal, badr, scfhs):
    """Amal's renewal gives up the number her record holds, and Badr's row takes it: the file is judged against the records
    as they stand, as two renewals by hand would be, so the second is refused whatever the order of the lines."""
    recorded(cno, amal, scfhs, "2026-12-31", "21-RN-0412")

    for rows in [("h1,SCFHS,2028-12-31,21-RN-0999,", "h2,SCFHS,2028-12-31,21-RN-0412,"),
                 ("h2,SCFHS,2028-12-31,21-RN-0412,", "h1,SCFHS,2028-12-31,21-RN-0999,")]:
        problems = full_problems_of(cno, *rows)
        assert sorted(problems, key=len) == [[], [(
            "identifier", "Amal Saleh already holds SCFHS with this identifier. Check the number: each one is one nurse's.")]]


def test_a_row_that_says_what_is_recorded_in_all_three_fields_is_unchanged(cno, amal, scfhs, bsn):
    recorded(cno, amal, scfhs, "2026-12-31", "21-RN-0412", "2024-12-15")
    recorded(cno, amal, bsn, None, "KSU-7", "2019-06-30")
    content = full_csv_of("h1,SCFHS,2026-12-31,21-RN-0412,2024-12-15", "h1,BSN,,KSU-7,2019-06-30")

    assert [row["change"] for row in previewed(cno, content)["rows"]] == ["none", "none"]
    assert confirmed(cno, content) == {"recorded": 0, "renewed": 0, "unchanged": 2}
    assert NurseSkill.objects.count() == 2


@pytest.mark.parametrize("line, held_now", [
    ("h1,SCFHS,2028-12-31,21-RN-0412,2024-12-15", ("2028-12-31", "21-RN-0412", "2024-12-15")),
    ("h1,SCFHS,2026-12-31,21-RN-0999,2024-12-15", ("2026-12-31", "21-RN-0999", "2024-12-15")),
    ("h1,SCFHS,2026-12-31,21-RN-0412,2026-11-01", ("2026-12-31", "21-RN-0412", "2026-11-01")),
    ("h1,SCFHS,2026-12-31,21-RN-0412,", ("2026-12-31", "21-RN-0412", None)),
], ids=["the expiry", "the identifier", "the issue date", "the issue date left empty"])
def test_a_row_that_differs_in_any_of_the_three_is_a_renewal_ending_the_old_row(cno, amal, scfhs, line, held_now):
    recorded(cno, amal, scfhs, "2026-12-31", "21-RN-0412", "2024-12-15")
    first = NurseSkill.objects.get()

    row = previewed(cno, full_csv_of(line))["rows"][0]
    assert (row["change"], row["held_expiry"], row["held_identifier"], row["held_issue_date"]) == (
        "renew", "2026-12-31", "21-RN-0412", "2024-12-15")
    assert confirmed(cno, full_csv_of(line)) == {"recorded": 0, "renewed": 1, "unchanged": 0}

    assert now_held(scfhs) == held_now
    first.refresh_from_db()
    assert first.removed_at and (first.identifier, first.issue_date.isoformat()) == ("21-RN-0412", "2024-12-15"), (
        "The ended row keeps what it had.")


def test_an_empty_identifier_cell_clears_the_one_recorded_on_a_certificate(cno, amal, bsn):
    """The file says the whole record: an empty cell is no identifier, as in last month's file, where every one is empty."""
    recorded(cno, amal, bsn, None, "KSU-7", "2019-06-30")

    assert previewed(cno, csv_of("h1,BSN,"))["rows"][0]["change"] == "renew"
    assert confirmed(cno, full_csv_of("h1,BSN,,,2019-06-30")) == {"recorded": 0, "renewed": 1, "unchanged": 0}
    assert now_held(bsn) == (None, None, "2019-06-30")


def test_a_renewal_of_a_kind_that_no_longer_expires_keeps_its_date_as_by_hand(cno, amal, catalogue):
    recorded(cno, amal, catalogue["ACLS"], "2026-12-31")
    assert change_skill(cno, "ACLS", expires=False).status_code == 200

    assert confirmed(cno, full_csv_of("h1,ACLS,,AHA-55,")) == {"recorded": 0, "renewed": 1, "unchanged": 0}

    assert now_held(catalogue["ACLS"]) == ("2026-12-31", "AHA-55", None)


def test_a_renewal_of_the_identifier_by_hand_since_the_preview_is_not_what_was_previewed(cno, amal, scfhs):
    recorded(cno, amal, scfhs, "2026-12-31", "21-RN-0412")
    content = full_csv_of("h1,SCFHS,2028-12-31,21-RN-0412,")
    token = previewed(cno, content)["token"]
    assert renew(cno, amal, scfhs, "2026-12-31", identifier="21-RN-0413").status_code == 200

    response = confirm(cno, content, token)

    assert response.status_code == 409
    assert response.json()["rows"][0]["held_identifier"] == "21-RN-0413"
    assert now_held(scfhs) == ("2026-12-31", "21-RN-0413", None)


def test_the_token_stands_for_the_identifier_and_the_issue_date(cno, amal, bsn):
    tokens = {previewed(cno, full_csv_of(line))["token"]
              for line in ("h1,BSN,,KSU-7,", "h1,BSN,,KSU-8,", "h1,BSN,,KSU-7,2019-06-30")}

    assert len(tokens) == 3
    assert confirm(cno, full_csv_of("h1,BSN,,KSU-8,"), previewed(cno, full_csv_of("h1,BSN,,KSU-7,"))["token"]
                   ).status_code == 409
    assert not NurseSkill.objects.exists()


def test_an_import_with_identifiers_is_audited_as_how_many_rows_and_nothing_more(cno, amal, badr, scfhs):
    recorded(cno, amal, scfhs, "2026-12-31", "21-RN-0412", "2024-12-15")
    AuditEntry.objects.all().delete()

    confirmed(cno, full_csv_of("h1,SCFHS,2028-12-31,21-RN-0412,2026-11-01", "h2,SCFHS,2028-03-31,21-RN-0777,2026-03-01"))

    assert list(AuditEntry.objects.values_list("action", "target", "before", "after")) == [
        ("nurse.skills_imported", "", None, {"count": 2})]
