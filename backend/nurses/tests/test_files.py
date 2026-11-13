"""The CNO's files: the import template, previewing and confirming an import, and the staff list."""
import codecs
import csv
import io
import logging

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from accounts import provisioning
from accounts.models import Role, User
from audit.models import AuditEntry
from conftest import CROSS_SITE, SERVED_SITES, from_site, post
from nurses import importing
from nurses.models import NurseProfile
from nurses.spreadsheets import safe_cell
from nurses.tests.conftest import NURSES, added, make_unit
from units.models import Unit

pytestmark = pytest.mark.django_db

HEADER = "Hospital ID,Full name,Gender,Unit,Start date"
TEMPLATE = f"{NURSES}/import/template"
PREVIEW = f"{NURSES}/import/preview"
CONFIRM = f"{NURSES}/import/confirm"
EXPORT = f"{NURSES}/export"
GOOD = f"{HEADER}\nh1,Amal Saleh,F,ER,2026-01-10\nh2,\"Omar, Badr\",m,icu,2026-02-01\n"


def upload(content, name="nurses.csv"):
    return SimpleUploadedFile(name, content.encode() if isinstance(content, str) else content, content_type="text/csv")


def preview(client, content):
    return client.post(PREVIEW, {"file": upload(content)})


def previewed(client, content) -> dict:
    response = preview(client, content)
    assert response.status_code == 200, response.content
    return response.json()


def confirm(client, content, token):
    return client.post(CONFIRM, {"file": upload(content), "token": token})


def problems_of(client, *rows) -> list:
    body = previewed(client, "\n".join([HEADER, *rows]))
    assert body["token"] is None
    return [[(problem["column"], problem["message"]) for problem in row["problems"]] for row in body["rows"]]


def file_refusal(response) -> str:
    assert response.status_code == 422, response.content
    detail = response.json()["detail"]
    assert detail[0]["loc"][2:] == ["file"]
    return detail[0]["msg"]


def table(response) -> list[list[str]]:
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"), newline="")))


@pytest.fixture(autouse=True)
def units(er, icu):
    return er, icu


# ---- the template ---------------------------------------------------------------------------------------------------
def test_the_template_has_the_exact_columns_and_a_byte_order_mark_for_excel(cno):
    response = cno.get(TEMPLATE)

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    assert "attachment" in response["Content-Disposition"]
    assert response.content == codecs.BOM_UTF8 + b'"Hospital ID","Full name","Gender","Unit","Start date","On probation"\r\n'


def test_the_template_itself_is_read_as_a_file_with_no_nurses(cno):
    template = cno.get(TEMPLATE).content

    assert "no nurses" in file_refusal(preview(cno, template))


# ---- what a preview reports -----------------------------------------------------------------------------------------
def test_a_good_file_previews_with_a_token_and_saves_nothing(cno):
    response = preview(cno, GOOD)

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    body = response.json()
    assert body["token"]
    assert [(r["line"], r["hospital_id"], r["full_name"], r["gender"], r["unit"], r["start_date"], r["problems"]) for r in body["rows"]] == [
        (2, "h1", "Amal Saleh", "F", "ER", "2026-01-10", []),
        (3, "h2", "Omar, Badr", "M", "ICU", "2026-02-01", []),
    ]
    assert not NurseProfile.objects.exists()
    assert not User.objects.filter(role=Role.STAFF_NURSE).exists()
    assert not AuditEntry.objects.filter(action__startswith="nurse").exists()


def test_every_problem_on_every_row_is_reported_beside_its_column(cno):
    assert problems_of(cno,
                       "h1,Amal,F,ER,2026-01-10",
                       "has space,  ,X,Nowhere,10/01/2026",
                       ",Badr,M,ER,2026-13-40") == [
        [],
        [("hospital_id", "Use 1 to 150 letters, digits, dots, dashes and underscores."),
         ("full_name", "Enter a full name of at most 200 characters."),
         ("gender", "Use F or M."),
         ("unit", "There is no such unit. Use its name on the Units page."),
         ("start_date", "Use a date as YYYY-MM-DD, between 1925 and 2076.")],
        [("hospital_id", "Use 1 to 150 letters, digits, dots, dashes and underscores."),
         ("start_date", "Use a date as YYYY-MM-DD, between 1925 and 2076.")],
    ]


def test_a_hospital_id_twice_in_the_file_in_any_case_is_reported_on_both_lines(cno):
    assert problems_of(cno, "h1,Amal,F,ER,2026-01-10", "h2,Badr,M,ER,2026-01-10", "H1,Carla,F,ER,2026-01-10") == [
        [("hospital_id", "h1 is also on line 4. Each nurse needs their own hospital ID.")], [], [("hospital_id", "H1 is also on line 2. Each nurse needs their own hospital ID.")]]


@pytest.mark.parametrize("taken", ["h123", "H123", "chief"])
def test_a_hospital_id_already_in_use_in_any_case_is_reported(cno, er, taken):
    added(cno, er)

    assert problems_of(cno, f"{taken.upper()},Amal,F,ER,2026-01-10") == [
        [("hospital_id", f"{taken.upper()} is already in use. Give this nurse another, or remove the line if they are already added.")]]


def test_an_archived_unit_is_refused_and_named_as_archived(cno):
    make_unit("Old", archived=True)

    assert problems_of(cno, "h1,Amal,F,old,2026-01-10") == [[("unit", "Old is archived, so no nurse can join it. Choose a unit that is not archived.")]]


def test_a_live_unit_wins_over_an_archived_one_of_the_same_name(cno, er):
    make_unit("ER", archived=True)

    assert previewed(cno, f"{HEADER}\nh1,Amal,F,ER,2026-01-10")["token"]


def test_a_line_with_too_many_or_too_few_cells_is_reported_once_for_the_whole_line(cno):
    assert problems_of(cno, "h1,Saleh,Amal,F,ER,2026-01-10", "h2,Badr") == [
        [(None, "This line has 6 cells, not 5. Put double quotes around a name that has a comma in it.")],
        [(None, "This line has 2 cells, not 5. Put double quotes around a name that has a comma in it.")],
    ]


def test_a_nul_in_a_cell_is_a_problem_and_not_a_server_error(cno):
    assert problems_of(cno, "h1,Am\x00al,F,ER,2026-01-10") == [[("full_name", "Enter a full name of at most 200 characters.")]]
    assert problems_of(cno, "h\x001,Amal,F,ER,2026-01-10")[0][0][0] == "hospital_id"


@pytest.mark.parametrize("control", ["\x1c", "\x1d", "\x1e", "\x1f", "\x85"])
def test_a_control_character_python_strips_around_a_hospital_id_is_not_a_server_error(cno, control):
    assert previewed(cno, f"{HEADER}\n{control}h1,Amal,F,ER,2026-01-10")["rows"][0]["hospital_id"] == "h1"


@pytest.mark.parametrize("gender", ["female", "Male", "x", ""])
def test_gender_is_f_or_m_as_the_page_says(cno, gender):
    assert problems_of(cno, f"h1,Amal,{gender},ER,2026-01-10") == [[("gender", "Use F or M.")]]


def test_blank_lines_and_excels_padding_are_ignored(cno):
    body = previewed(cno, f"{HEADER},,\n\n,,,,\nh1,Amal,F,ER,2026-01-10,,\n,,,,,,\n")

    assert body["token"] and [row["hospital_id"] for row in body["rows"]] == ["h1"]
    assert body["rows"][0]["line"] == 4


# ---- what a file may be ---------------------------------------------------------------------------------------------
ARABIC = f"{HEADER}\nh1,أمل صالح,F,ER,2026-01-10\n"


@pytest.mark.parametrize("encoded", [
    ARABIC.encode("utf-8"),
    codecs.BOM_UTF8 + ARABIC.encode("utf-8"),
    ARABIC.encode("utf-16"),
    codecs.BOM_UTF16_BE + ARABIC.encode("utf-16-be"),
], ids=["utf-8", "excel's csv utf-8", "utf-16 le", "utf-16 be"])
def test_utf8_and_utf16_with_a_mark_are_read_and_the_header_survives_the_mark(cno, encoded):
    assert previewed(cno, encoded)["rows"][0]["full_name"] == "أمل صالح"


@pytest.mark.parametrize("encoded", [ARABIC.encode("cp1256"), f"{HEADER}\nh1,Renée,F,ER,2026-01-10\n".encode("cp1252")],
                         ids=["windows-1256", "windows-1252"])
def test_an_encoding_that_could_only_be_guessed_is_refused(cno, encoded):
    assert "CSV UTF-8" in file_refusal(preview(cno, encoded))


@pytest.mark.parametrize("delimiter", [";", "\t"])
def test_excels_other_list_separators_are_read(cno, delimiter):
    assert previewed(cno, GOOD.replace('"Omar, Badr"', "Omar").replace(",", delimiter))["token"]


def test_line_endings_from_any_system_and_a_name_over_two_lines_keep_the_file_line_numbers(cno):
    body = previewed(cno, f'{HEADER}\r\nh1,"Amal\nSaleh",X,ER,2026-01-10\r\nh2,Badr,X,ER,2026-01-10\r\n')

    assert [row["line"] for row in body["rows"]] == [3, 4]


@pytest.mark.parametrize("content, words", [
    ("", "empty"),
    ("\n\n", "empty"),
    (HEADER, "no nurses"),
    ("h1,Amal,F,ER,2026-01-10", "exactly these columns"),
    ("Hospital ID,Full name,Gender,Unit", "exactly these columns"),
    (f"{HEADER},Team leader", "exactly these columns"),
    ("Full name,Hospital ID,Gender,Unit,Start date", "exactly these columns"),
    (f'{HEADER}\nh1,"Amal,F,ER,2026-01-10\n', "quotation marks"),
    (b"PK\x03\x04\xff\xfe\x00binary", "CSV UTF-8"),
], ids=["empty", "blank", "header only", "no header", "a column short", "a column more", "reordered", "open quote", "an xlsx"])
def test_a_file_that_cannot_be_read_is_refused_as_a_whole(cno, content, words):
    assert words in file_refusal(preview(cno, content))


def test_the_header_is_read_in_any_case(cno):
    assert previewed(cno, f"{HEADER.upper()}\nh1,Amal,F,ER,2026-01-10")["token"]


def test_more_rows_than_the_limit_are_refused_and_the_limit_itself_is_not(cno):
    rows = [f"h{number},Amal,F,ER,2026-01-10" for number in range(importing.MAX_ROWS)]

    assert previewed(cno, "\n".join([HEADER, *rows]))["token"]
    assert "more than 500" in file_refusal(preview(cno, "\n".join([HEADER, *rows, "last,Amal,F,ER,2026-01-10"])))


def test_a_file_over_the_size_limit_is_refused(cno):
    assert "larger than 1 MB" in file_refusal(preview(cno, f"{HEADER}\n" + " " * importing.MAX_BYTES))


def test_a_file_too_large_to_keep_in_memory_arrives_as_no_file_and_never_reaches_the_disk(cno, settings, monkeypatch):
    settings.FILE_UPLOAD_MAX_MEMORY_SIZE = 1000
    monkeypatch.setattr("django.core.files.uploadedfile.TemporaryUploadedFile.__init__",
                        lambda *_, **__: pytest.fail("the upload went to a temporary file"))

    response = preview(cno, f"{HEADER}\n" + "h,Amal,F,ER,2026-01-10\n" * 100)

    assert response.status_code == 422
    assert not NurseProfile.objects.exists()


def test_no_file_at_all_is_refused(cno):
    assert cno.post(PREVIEW, {}).status_code == 422


# ---- confirming -----------------------------------------------------------------------------------------------------
def test_confirming_creates_every_nurse_and_answers_with_the_passwords_once(cno):
    token = previewed(cno, GOOD)["token"]

    response = confirm(cno, GOOD, token)

    assert response.status_code == 201, response.content
    assert response["Cache-Control"] == "no-store"
    rows = response.json()["rows"]
    assert [(r["hospital_id"], r["full_name"], r["unit"]) for r in rows] == [("h1", "Amal Saleh", "ER"), ("h2", "Omar, Badr", "ICU")]
    assert len({row["generated_password"] for row in rows}) == 2

    nurse = NurseProfile.objects.get(user__username="h2")
    assert (nurse.gender, nurse.start_date.isoformat(), nurse.user.role) == ("M", "2026-02-01", Role.STAFF_NURSE)
    assert nurse.placements.get().unit.name == "ICU"
    login = post(Client(), "/api/auth/login", {"username": "h1", "password": rows[0]["generated_password"]})
    assert login.status_code == 200 and login.json()["must_change_password"] is True


def test_confirming_twice_creates_nothing_more(cno):
    token = previewed(cno, GOOD)["token"]
    assert confirm(cno, GOOD, token).status_code == 201

    response = confirm(cno, GOOD, token)

    assert response.status_code == 409
    assert response.json()["token"] is None
    assert "generated_password" not in response.content.decode()
    assert NurseProfile.objects.count() == 2


def test_a_file_with_any_error_cannot_be_confirmed_whatever_token_is_sent(cno):
    bad = f"{HEADER}\nh1,Amal,F,ER,2026-01-10\nh2,Badr,X,ER,2026-01-10\n"
    good_token = previewed(cno, GOOD)["token"]

    for token in ("", "null", good_token):
        response = confirm(cno, bad, token)
        assert response.status_code == 409
        assert response.json()["token"] is None and response.json()["rows"][1]["problems"]
    assert not NurseProfile.objects.exists()
    assert not User.objects.filter(username="h1").exists()


def test_a_file_that_is_not_the_one_previewed_cannot_be_confirmed(cno):
    token = previewed(cno, GOOD)["token"]
    other = GOOD.replace("Amal Saleh", "Amal Salih")

    response = confirm(cno, other, token)

    assert response.status_code == 409
    assert response.json()["token"] == previewed(cno, other)["token"] != token
    assert not NurseProfile.objects.exists()


def test_the_same_nurses_saved_differently_are_what_was_previewed(cno):
    token = previewed(cno, GOOD)["token"]

    assert confirm(cno, GOOD.replace("\n", "\r\n").encode("utf-16"), token).status_code == 201


def test_a_hospital_id_taken_since_the_preview_saves_nothing_and_says_so(cno, er):
    token = previewed(cno, GOOD)["token"]
    added(cno, er, hospital_id="H2")

    response = confirm(cno, GOOD, token)

    assert response.status_code == 409
    assert response["Cache-Control"] == "no-store"
    assert response.json()["token"] is None
    assert response.json()["rows"][1]["problems"] == [{"column": "hospital_id", "message": "h2 is already in use. Give this nurse another, or remove the line if they are already added."}]
    assert not User.objects.filter(username="h1").exists()


def test_a_unit_archived_since_the_preview_saves_nothing(cno, er):
    token = previewed(cno, GOOD)["token"]
    Unit.objects.filter(pk=er.pk).update(archived=True)

    response = confirm(cno, GOOD, token)

    assert response.status_code == 409
    assert response.json()["rows"][0]["problems"][0]["column"] == "unit"
    assert not User.objects.filter(username="h1").exists()


def test_a_unit_archived_and_another_opened_under_its_name_is_not_what_was_previewed(cno, er):
    token = previewed(cno, GOOD)["token"]
    Unit.objects.filter(pk=er.pk).update(archived=True)
    make_unit("ER")

    response = confirm(cno, GOOD, token)

    assert response.status_code == 409
    assert response.json()["token"] and response.json()["token"] != token
    assert not NurseProfile.objects.exists()


def test_a_unit_renamed_since_the_preview_saves_nothing(cno, er):
    token = previewed(cno, GOOD)["token"]
    Unit.objects.filter(pk=er.pk).update(name="Emergency")

    response = confirm(cno, GOOD, token)

    assert response.status_code == 409
    assert response.json()["rows"][0]["problems"] == [{"column": "unit", "message": "There is no such unit. Use its name on the Units page."}]


@pytest.fixture
def during_hashing(monkeypatch):
    """Something happening after the first check has passed, while the slow part runs, as nothing else could arrange."""
    def do(change):
        real = provisioning.new_passwords

        def slow(count):
            change()
            return real(count)
        monkeypatch.setattr("nurses.files_api.new_passwords", slow)
    return do


def test_a_unit_archived_while_the_passwords_are_hashed_saves_nothing(cno, er, during_hashing):
    token = previewed(cno, GOOD)["token"]
    during_hashing(lambda: Unit.objects.filter(pk=er.pk).update(archived=True))

    response = confirm(cno, GOOD, token)

    assert response.status_code == 409
    assert response.json()["rows"][0]["problems"][0]["column"] == "unit"
    assert not User.objects.filter(username__in=["h1", "h2"]).exists()
    assert not AuditEntry.objects.filter(target="h1").exists()


def test_a_hospital_id_taken_while_the_passwords_are_hashed_saves_nothing(cno, make_user, during_hashing):
    token = previewed(cno, GOOD)["token"]
    during_hashing(lambda: make_user("H2", Role.UNIT_MANAGER))

    response = confirm(cno, GOOD, token)

    assert response.status_code == 409
    assert response.json()["rows"][1]["problems"][0]["column"] == "hospital_id"
    assert not User.objects.filter(username="h1").exists()
    assert not NurseProfile.objects.exists()


def test_a_cno_deactivated_while_the_passwords_are_hashed_adds_nobody(cno, during_hashing):
    token = previewed(cno, GOOD)["token"]
    during_hashing(lambda: User.objects.filter(username="chief").update(is_active=False))

    assert confirm(cno, GOOD, token).status_code == 403
    assert not User.objects.filter(username="h1").exists()


def test_the_sheet_keeps_the_file_s_order_though_users_go_in_by_hospital_id(cno):
    content = f"{HEADER}\nz9,Zaid,M,ER,2026-01-10\nA1,Amal,F,ER,2026-01-10\nm5,Mona,F,ICU,2026-01-10\n"

    rows = confirm(cno, content, previewed(cno, content)["token"]).json()["rows"]

    assert [row["hospital_id"] for row in rows] == ["z9", "A1", "m5"]
    for row in rows:
        assert User.objects.get(username=row["hospital_id"]).check_password(row["generated_password"])
        assert User.objects.get(username=row["hospital_id"]).full_name == row["full_name"]


def test_a_failure_on_the_last_row_undoes_the_first(cno, monkeypatch):
    token = previewed(cno, GOOD)["token"]
    real = NurseProfile.objects.create

    def fail_on_second(**fields):
        if fields["user"].username == "h2":
            raise RuntimeError("the database went away")
        return real(**fields)
    monkeypatch.setattr(NurseProfile.objects, "create", fail_on_second)

    cno.raise_request_exception = False
    assert confirm(cno, GOOD, token).status_code == 500
    assert not User.objects.filter(username="h1").exists()
    assert not AuditEntry.objects.filter(target__in=["h1", "h2"]).exists()


# ---- what stays behind ----------------------------------------------------------------------------------------------
def test_an_import_is_audited_like_adding_each_nurse_with_no_gender_and_no_password(cno):
    rows = confirm(cno, GOOD, previewed(cno, GOOD)["token"]).json()["rows"]

    summary = AuditEntry.objects.get(action="nurses.imported")
    assert summary.actor.username == "chief"
    assert summary.after == {"count": 2, "units": {"ER": 1, "ICU": 1}}
    added_entry = AuditEntry.objects.get(action="nurse.added", target="h1")
    assert added_entry.after == {"unit": "ER", "start_date": "2026-01-10"}
    assert AuditEntry.objects.get(action="user.created", target="h1").after["full_name"] == "Amal Saleh"

    everything = str(list(AuditEntry.objects.values_list("before", "after")))
    assert "gender" not in everything and "'F'" not in everything and "'M'" not in everything
    for row in rows:
        assert row["generated_password"] not in everything


def test_a_preview_and_a_refused_confirm_write_no_audit_entry(cno):
    before = AuditEntry.objects.count()

    previewed(cno, GOOD)
    confirm(cno, GOOD, "wrong")

    assert AuditEntry.objects.count() == before


def test_no_password_reaches_the_logs(cno, caplog):
    caplog.set_level(logging.DEBUG)

    rows = confirm(cno, GOOD, previewed(cno, GOOD)["token"]).json()["rows"]

    assert rows and not any(row["generated_password"] in caplog.text for row in rows)


def test_no_password_is_in_what_is_stored_only_its_hash(cno):
    rows = confirm(cno, GOOD, previewed(cno, GOOD)["token"]).json()["rows"]

    stored = User.objects.get(username="h1").password
    assert rows[0]["generated_password"] not in stored
    assert User.objects.get(username="h1").check_password(rows[0]["generated_password"])
    assert rows[0]["generated_password"] not in cno.get(NURSES).content.decode()


def test_a_new_password_does_not_print_itself():
    password = provisioning.new_password()

    assert password.plain not in repr(password) and password.hashed not in repr(password)


def test_many_passwords_are_all_different_and_each_checks_out():
    passwords = provisioning.new_passwords(8)

    assert len({password.plain for password in passwords}) == 8
    user = User(username="x")
    for password in passwords:
        user.password = password.hashed
        assert user.check_password(password.plain)


# ---- the staff list -------------------------------------------------------------------------------------------------
def test_the_staff_list_has_what_a_cno_may_see_of_everyone(cno, er, icu, make_user):
    nurse = added(cno, er, hospital_id="h1", full_name="Amal Saleh")["nurse"]
    assert post(cno, f"{NURSES}/{nurse['id']}/transfer", {"unit_id": str(icu.pk), "from_month": "2076-01"}).status_code == 200
    gone = added(cno, er, hospital_id="h2", full_name="Badr Omar", gender="M")["nurse"]
    assert cno.put(f"{NURSES}/{gone['id']}/end-date", {"end_date": "2026-02-01"}, content_type="application/json").status_code == 200
    manager = make_user("mona", Role.UNIT_MANAGER, full_name="Mona Ali")
    assert post(cno, f"{NURSES}/managers", {"user_id": str(manager.pk), "gender": "F", "unit_id": str(icu.pk),
                                            "start_date": "2026-03-01"}).status_code == 201

    response = cno.get(EXPORT)

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    assert "staff-list-" in response["Content-Disposition"]
    assert response["Cache-Control"] == "no-store"
    assert response.content.startswith(codecs.BOM_UTF8)
    assert table(response) == [
        ["Hospital ID", "Full name", "Gender", "Unit", "Start date", "On probation", "Probation's last day", "End date", "Role",
         "Status", "Transfers to come"],
        ["h1", "Amal Saleh", "F", "ER", "2026-01-10", "", "", "", "Staff Nurse", "Active", "ICU from 2076-01"],
        ["h2", "Badr Omar", "M", "ER", "2026-01-10", "", "", "2026-02-01", "Staff Nurse", "Deactivated", ""],
        ["mona", "Mona Ali", "F", "ICU", "2026-03-01", "", "", "", "Unit Manager", "Active", ""],
    ]


def test_the_staff_list_holds_no_password_and_no_hash(cno, er):
    password = added(cno, er)["generated_password"]

    content = cno.get(EXPORT).content.decode("utf-8-sig")

    assert password not in content and "pbkdf2" not in content and "md5" not in content


def test_each_taking_of_the_staff_list_is_audited_with_the_count_and_nothing_from_the_file(cno, er, icu):
    added(cno, er, hospital_id="h1", full_name="Amal Saleh", gender="F")
    added(cno, icu, hospital_id="h2", full_name="Badr Omar", gender="M")

    rows = table(cno.get(EXPORT))[1:]

    entry = AuditEntry.objects.get(action="nurses.exported")
    assert entry.actor.username == "chief"
    assert entry.ip == "127.0.0.1"
    assert (entry.target, entry.before, entry.after) == ("", None, {"count": 2})
    held = str((entry.target, entry.before, entry.after))
    assert len(rows) == 2
    for cell in filter(None, (cell for row in rows for cell in row)):
        assert cell not in held

    cno.get(EXPORT)

    assert AuditEntry.objects.filter(action="nurses.exported").count() == 2


def test_a_staff_list_of_nobody_is_still_audited(cno):
    assert len(table(cno.get(EXPORT))) == 1

    assert AuditEntry.objects.get(action="nurses.exported").after == {"count": 0}


@pytest.mark.parametrize("role", [role for role in Role if role != Role.CNO])
def test_a_caller_refused_the_staff_list_writes_no_audit_entry(client_of, role, er):
    other = client_of("other", role)
    before = AuditEntry.objects.count()

    assert other.get(EXPORT).status_code == 403

    assert AuditEntry.objects.count() == before


def test_nobody_signed_in_is_refused_the_staff_list_and_writes_no_audit_entry(db):
    before = AuditEntry.objects.count()

    assert Client().get(EXPORT).status_code == 401

    assert AuditEntry.objects.count() == before


def test_the_template_holds_no_one_s_data_and_writes_no_audit_entry(cno):
    before = AuditEntry.objects.count()

    assert cno.get(TEMPLATE).status_code == 200

    assert AuditEntry.objects.count() == before


@pytest.mark.parametrize("name", ["=HYPERLINK(\"http://x\",\"click\")", "+1+1", "@SUM(A1)", "-2+3", "＝1+1"])
def test_a_formula_in_a_name_reaches_the_spreadsheet_as_text(cno, er, name):
    added(cno, er, full_name=name)

    assert table(cno.get(EXPORT))[1][1] == f"'{name}"


def test_a_formula_in_a_unit_name_reaches_the_spreadsheet_as_text(cno):
    added(cno, make_unit("=cmd()"))

    assert table(cno.get(EXPORT))[1][3] == "'=cmd()"


def test_a_hospital_id_that_opens_like_a_formula_reaches_the_spreadsheet_as_text(cno, er):
    added(cno, er, hospital_id="-A1")

    assert table(cno.get(EXPORT))[1][0] == "'-A1"


def test_every_cell_is_quoted_so_no_locale_s_separator_can_start_a_new_cell(cno, er):
    added(cno, er, full_name="x;=1+1;y")

    assert '"x;=1+1;y"' in cno.get(EXPORT).content.decode("utf-8-sig")


@pytest.mark.parametrize("cell", ["Amal", "", "a=b", "2026-01-10", "h-1"])
def test_ordinary_cells_are_left_alone(cell):
    assert safe_cell(cell) == cell


def test_a_nurse_imported_with_a_formula_for_a_name_is_safe_in_the_staff_list(cno):
    content = f'{HEADER}\nh1,"=1+1",F,ER,2026-01-10\n'
    assert confirm(cno, content, previewed(cno, content)["token"]).status_code == 201

    assert table(cno.get(EXPORT))[1][1] == "'=1+1"


class TestFromAnotherSite:
    """A page on another site cannot make a signed-in CNO's browser take a file, nor tell from the answer that the
    visitor is the CNO: refused before the session is read, so signed out or in, any role, the answer is the same."""
    FILES = [TEMPLATE, EXPORT, f"{NURSES}/skills-import/template"]

    @pytest.mark.parametrize("path", FILES)
    def test_it_is_refused_and_the_staff_list_is_not_logged(self, cno, path):
        response = cno.get(path, **from_site("cross-site"))

        assert (response.status_code, response.json()["detail"]) == (403, CROSS_SITE)
        assert "Content-Disposition" not in response
        assert not AuditEntry.objects.filter(action="nurses.exported").exists()

    def test_it_does_not_keep_the_session_alive(self, cno):
        """A visit to the other page is not the CNO's activity here: the 12 hours of the idle limit do not start again."""
        from django.contrib.sessions.models import Session
        from django.utils import timezone
        import datetime as dt
        earlier = timezone.now() + dt.timedelta(hours=1)
        Session.objects.filter(pk=cno.cookies["sessionid"].value).update(expire_date=earlier)

        assert cno.get(EXPORT, **from_site("cross-site")).status_code == 403
        assert Session.objects.get(pk=cno.cookies["sessionid"].value).expire_date == earlier

    @pytest.mark.parametrize("path", FILES)
    def test_whoever_asks_gets_the_same_answer(self, client_of, path):
        for client in (Client(), client_of("nurse", Role.STAFF_NURSE)):
            response = client.get(path, **from_site("cross-site"))
            assert (response.status_code, response.json()["detail"]) == (403, CROSS_SITE)

    @pytest.mark.parametrize("site", SERVED_SITES)
    @pytest.mark.parametrize("path", FILES)
    def test_the_apps_own_pages_and_a_browser_that_does_not_say_are_served(self, cno, path, site):
        response = cno.get(path, **from_site(site))

        assert response.status_code == 200 and response["Content-Disposition"].startswith("attachment")
        assert AuditEntry.objects.filter(action="nurses.exported").count() == (path == EXPORT)
