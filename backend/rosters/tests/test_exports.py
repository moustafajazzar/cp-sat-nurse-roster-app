"""A roster version's files and its print sheet: the CSV and the Excel workbook of any version, for the unit's manager and the
approvers, each download audited; the sheet the print page reads; and a nurse's own sheet of their unit's roster in force. The API
with the scripted solver on the real optimal roster of the four-nurse sample (`fixtures/sound_rosters.json`).

A file leaves the system, so it holds what a colleague may read and no more: a day of leave is a day off in it, and nothing
says a leave's type."""
import csv
import io
import json
import tempfile
import zipfile
from xml.etree import ElementTree

import pytest

from accounts.models import Role, User
from audit.models import AuditEntry
from conftest import CROSS_SITE, SERVED_SITES, from_site, post
from rosters.models import Roster, RosterState, RosterVersion
from rosters.tests.test_pins import change_shift, pin
from rosters.tests.test_review import approved, carla, dina, nurses_of_er, ok, refused, solved, url  # noqa: F401
from rosters.tests.test_versions import changed, leave_for
from solves.tests.conftest import MONTH, make_sample_unit
from units.models import Unit

pytestmark = pytest.mark.django_db

DATES = [f"{MONTH}-{day:02d}" for day in range(1, 31)]
# Read off `fixtures/sound_rosters.json`'s June by hand: Amal works Night on the 1st to the 4th, and 11 Nights and 6 Days in
# all, at 12 hours each.
AMAL = ["N", "N", "N", "N", "", "N", "N", "N", "", "", "", "N", "N", "", "", "", "", "", "N", "N", "", "",
        "D", "D", "D", "D", "", "", "D", "D"]


@pytest.fixture(autouse=True)
def _scene(before_june, solver):
    pass


def export(client, unit, version=1, file_format="csv", month=MONTH):
    return client.get(url(unit, "/roster/export"), {"month": month, "version": version, "format": file_format})


def csv_rows(response) -> list[list[str]]:
    assert response.status_code == 200, response.content
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


class TestTheCsv:
    def test_one_row_a_nurse_one_column_a_date_then_hours_and_shift_counts(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)

        header, *rows = csv_rows(export(mona, er))

        assert header == ["Hospital ID", "Full name", *DATES, "Shift hours", "Day (D)", "Night (N)"]
        assert [row[0] for row in rows] == ["Amal", "Badr", "Dana", "Emad"]
        assert rows[0] == ["Amal", "Amal", *AMAL, "204", "6", "11"]

    def test_it_is_a_quoted_utf8_file_with_a_byte_order_mark_that_nothing_keeps(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)

        response = export(mona, er)

        assert response["Content-Type"] == "text/csv; charset=utf-8"
        assert response["Content-Disposition"] == 'attachment; filename="roster-er-2026-06-v1.csv"'
        assert response["Cache-Control"] == "no-store"
        assert response.content.startswith(b'\xef\xbb\xbf"Hospital ID","Full name","2026-06-01",')

    def test_any_version_of_the_month_not_only_the_one_in_force(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        changed(mona, er, pin("Amal", 9, "N"))

        first, second = csv_rows(export(mona, er, 1)), csv_rows(export(mona, er, 2))

        assert first[1][2 + 8] == "" and second[1][2 + 8] == "N"
        assert (first[1][-3:], second[1][-3:]) == (["204", "6", "11"], ["216", "6", "12"])
        assert export(mona, er, 2)["Content-Disposition"] == 'attachment; filename="roster-er-2026-06-v2.csv"'

    def test_an_old_version_reads_under_todays_codes(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        changed(mona, er, pin("Amal", 9, "N"))
        assert change_shift(carla, "N", code="NT").status_code == 200

        header, amal, *_ = csv_rows(export(mona, er, 1))

        assert header[-2:] == ["Day (D)", "Night (NT)"]
        assert amal[2:6] == ["NT"] * 4
        assert RosterVersion.objects.get(number=1).grid["Amal"]["2026-06-01"] == "N"

    def test_a_day_of_leave_is_a_day_off_and_nothing_says_its_type(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        before = export(mona, er).content

        assert leave_for(mona, er, "Badr", 3, kind="Sick").status_code == 201  # a day he is off
        assert leave_for(mona, er, "Amal", 1, kind="Annual").status_code == 201  # a Night she works: version 2 takes it off

        assert export(mona, er, 1).content == before
        _, amal, *_ = csv_rows(export(mona, er, 2))
        assert amal[2] == "" and amal[-3:] == ["192", "6", "10"]
        text = export(mona, er, 2).content.decode("utf-8-sig").lower()
        assert not any(word in text for word in ("leave", "sick", "annual"))

    def test_a_name_that_opens_like_a_formula_stays_text_and_arabic_names_survive(self, mona, carla, er, solver):
        User.objects.filter(username="Amal").update(full_name="=HYPERLINK(\"http://x\",\"y\")")
        User.objects.filter(username="Badr").update(full_name="بدر الحربي")
        approved(mona, carla, er, solver)

        rows = {row[0]: row for row in csv_rows(export(mona, er))[1:]}

        assert rows["Amal"][1] == "'=HYPERLINK(\"http://x\",\"y\")"
        assert rows["Badr"][1] == "بدر الحربي"


NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def workbook(response) -> dict[str, dict]:
    """Each cell of the first sheet by reference, read from the file's own XML rather than by the library that wrote it: its
    type (`s` text, `n` a number), its value, whether it holds a formula, whether it is marked as text, and its number
    format."""
    assert response.status_code == 200, response.content
    parts = zipfile.ZipFile(io.BytesIO(response.content))
    strings = ["".join(t.text or "" for t in si.iter(f"{{{NS['x']}}}t"))
               for si in ElementTree.fromstring(parts.read("xl/sharedStrings.xml")).findall("x:si", NS)]
    styles = ElementTree.fromstring(parts.read("xl/styles.xml"))
    formats = {fmt.get("numFmtId"): fmt.get("formatCode") for fmt in styles.findall("x:numFmts/x:numFmt", NS)}
    xfs = styles.findall("x:cellXfs/x:xf", NS)
    cells = {}
    for cell in ElementTree.fromstring(parts.read("xl/worksheets/sheet1.xml")).iter(f"{{{NS['x']}}}c"):
        kind, value = cell.get("t", "n"), cell.findtext("x:v", namespaces=NS)
        xf = xfs[int(cell.get("s", 0))]
        cells[cell.get("r")] = {"type": kind, "value": strings[int(value)] if kind == "s" else value,
                                "formula": cell.find("x:f", NS) is not None, "text_marked": xf.get("quotePrefix") == "1",
                                "format": formats.get(xf.get("numFmtId"))}
    return cells


class TestTheWorkbook:
    def test_the_same_table_with_dates_as_dates_and_hours_as_numbers(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)

        cells = workbook(export(mona, er, file_format="xlsx"))

        assert [cells[ref]["value"] for ref in ("A1", "B1", "AG1", "AH1", "AI1")] == [
            "Hospital ID", "Full name", "Shift hours", "Day (D)", "Night (N)"]
        # 1 June 2026 is day 46174 of Excel's calendar, which counts from 30 December 1899.
        assert cells["C1"] | {"value": float(cells["C1"]["value"])} == {
            "type": "n", "value": 46174.0, "formula": False, "text_marked": False, "format": "yyyy-mm-dd"}
        assert cells["AF1"]["value"] == "46203"
        assert [cells[ref]["value"] for ref in ("A2", "C2", "H2")] == ["Amal", "N", "N"]
        assert "G2" not in cells, "a day off on the 5th is an empty cell"
        assert (cells["AG2"]["type"], cells["AG2"]["value"], cells["AH2"]["value"], cells["AI2"]["value"]) == ("n", "204", "6", "11")
        assert not any(cell["formula"] for cell in cells.values())

    def test_text_that_opens_like_a_formula_is_text_and_ids_and_arabic_names_keep_what_they_are(self, mona, carla, er, solver):
        User.objects.filter(username="Amal").update(full_name="=HYPERLINK(\"http://x\",\"y\")")
        User.objects.filter(username="Badr").update(full_name="بدر الحربي")
        User.objects.filter(username="Dana").update(full_name="+966 50 000 0000")
        approved(mona, carla, er, solver)

        cells = workbook(export(mona, er, file_format="xlsx"))
        names = {cells[f"A{row}"]["value"]: cells[f"B{row}"] for row in range(2, 6)}

        assert names["Amal"] == {"type": "s", "value": "=HYPERLINK(\"http://x\",\"y\")", "formula": False, "text_marked": True,
                                 "format": None}
        assert names["Dana"] | {"text_marked": True} == names["Dana"] and names["Dana"]["type"] == "s"
        assert names["Badr"]["value"] == "بدر الحربي" and not names["Badr"]["text_marked"]
        assert not any(cell["formula"] for cell in cells.values())

    def test_a_hospital_id_keeps_its_leading_zeros(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        RosterVersion.objects.filter(number=1).update(grid={"007": RosterVersion.objects.get(number=1).grid["Amal"]})

        cells = workbook(export(mona, er, file_format="xlsx"))

        assert (cells["A2"]["type"], cells["A2"]["value"]) == ("s", "007")

    def test_it_is_a_workbook_nothing_keeps_built_without_a_temporary_file(self, mona, carla, er, solver, monkeypatch):
        approved(mona, carla, er, solver)

        def refuse(*args, **kwargs):
            raise AssertionError("a temporary file")
        for name in ("TemporaryFile", "NamedTemporaryFile", "SpooledTemporaryFile", "mkstemp", "mkdtemp"):
            monkeypatch.setattr(tempfile, name, refuse)

        response = export(mona, er, file_format="xlsx")

        assert response.status_code == 200, response.content
        assert response["Content-Type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        assert response["Content-Disposition"] == 'attachment; filename="roster-er-2026-06-v1.xlsx"'
        assert response["Cache-Control"] == "no-store"
        assert zipfile.ZipFile(io.BytesIO(response.content)).testzip() is None


def sheet(client, unit, version=1, month=MONTH):
    return client.get(url(unit, "/roster/sheet"), {"month": month, "version": version})


def my_sheet(client, month=MONTH) -> dict:
    return ok(client.get("/api/rosters/mine/sheet", {"month": month}))


@pytest.fixture
def omar_of_icu(client_of, make_user):
    omar = client_of("omar", Role.UNIT_MANAGER)
    make_sample_unit("ICU", User.objects.get(username="omar"), make_user, prefix="I")
    return omar


class TestWhoTakesThem:
    @pytest.mark.parametrize("file_format", ["csv", "xlsx"])
    @pytest.mark.parametrize("approver", ["carla", "dina"])
    def test_the_cno_and_the_deputy_cno_take_any_units(self, approver, file_format, request, mona, carla, er, solver,
                                                       omar_of_icu):
        approved(mona, carla, er, solver)

        assert export(request.getfixturevalue(approver), er, file_format=file_format).status_code == 200
        assert sheet(request.getfixturevalue(approver), er).status_code == 200

    @pytest.mark.parametrize("file_format", ["csv", "xlsx"])
    def test_another_units_manager_is_refused(self, file_format, mona, carla, er, solver, omar_of_icu):
        approved(mona, carla, er, solver)

        assert refused(export(omar_of_icu, er, file_format=file_format), 403) == "This is not your unit."
        assert refused(sheet(omar_of_icu, er), 403) == "This is not your unit."

    @pytest.mark.parametrize("role", [Role.STAFF_NURSE, Role.SYSTEM_ADMIN])
    def test_a_nurse_and_the_system_admin_take_no_file(self, role, mona, carla, er, solver, signed_in, nurses_of_er):
        approved(mona, carla, er, solver)
        client = nurses_of_er("Amal") if role == Role.STAFF_NURSE else signed_in(role)

        assert export(client, er).status_code == 403
        assert export(client, er, file_format="xlsx").status_code == 403
        assert sheet(client, er).status_code == 403

    def test_a_draft_has_no_version_and_so_no_file(self, mona, er, solver):
        solved(mona, er, solver)

        assert refused(export(mona, er), 404) == "This month's roster has no version 1."
        assert refused(sheet(mona, er), 404) == "This month's roster has no version 1."

    def test_a_version_the_month_does_not_have_is_a_404(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)

        assert export(mona, er, 2).status_code == 404
        assert export(mona, er, 1, month="2026-07").status_code == 404

    @pytest.mark.parametrize("approver", ["carla", "dina"])
    def test_the_approvers_read_a_unit_only_while_it_is_submitted_or_approved(self, approver, request, mona, carla, er, solver):
        """An approved roster never returns to draft; this holds the second guard should one ever do."""
        approved(mona, carla, er, solver)
        Roster.objects.filter(unit=er).update(state=RosterState.DRAFT)

        assert export(request.getfixturevalue(approver), er).status_code == 404
        assert sheet(request.getfixturevalue(approver), er).status_code == 404
        assert export(mona, er).status_code == 200


class TestTheAuditLog:
    @pytest.mark.parametrize("approver", ["mona", "dina"])
    def test_each_download_is_one_entry_saying_who_which_roster_and_version_and_nothing_from_the_file(
            self, approver, request, mona, carla, er, solver):
        User.objects.filter(username="Amal").update(full_name="Amal Saleh")
        approved(mona, carla, er, solver)
        changed(mona, er, pin("Amal", 9, "N"))
        client = request.getfixturevalue(approver)
        AuditEntry.objects.all().delete()

        export(client, er, 1)
        export(client, er, 2, file_format="xlsx")
        export(client, er, 2, file_format="xlsx")

        entries = list(AuditEntry.objects.order_by("id").values("action", "actor__username", "target", "before", "after"))
        assert entries == [
            {"action": "roster.exported", "actor__username": approver, "target": "ER, 2026-06", "before": None,
             "after": {"version": 1, "format": "csv"}},
            *[{"action": "roster.exported", "actor__username": approver, "target": "ER, 2026-06", "before": None,
               "after": {"version": 2, "format": "xlsx"}}] * 2]

    def test_a_refused_download_and_the_print_sheet_write_nothing(self, mona, carla, er, solver, omar_of_icu, nurses_of_er):
        approved(mona, carla, er, solver)
        AuditEntry.objects.all().delete()

        export(omar_of_icu, er)
        export(nurses_of_er("Amal"), er)
        export(mona, er, 9)
        sheet(mona, er)
        my_sheet(nurses_of_er("Amal"))

        assert not AuditEntry.objects.exclude(action="auth.login").exists()


class TestThePrintSheet:
    def test_it_is_the_files_table(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        changed(mona, er, pin("Amal", 9, "N"))

        answer = ok(sheet(mona, er, 1))

        assert {key: answer[key] for key in ("unit", "month", "version", "in_force", "mine")} == {
            "unit": "ER", "month": MONTH, "version": 1, "in_force": 2, "mine": None}
        assert [day["date"] for day in answer["days"]] == DATES
        assert answer["shifts"] == [{"code": "D", "display_name": "Day", "hours": 12},
                                    {"code": "N", "display_name": "Night", "hours": 12}]
        assert answer["rows"][0] == {"hospital_id": "Amal", "full_name": "Amal", "shifts": AMAL, "hours": 204, "counts": [6, 11],
                                    "on_probation": False, "preceptor_name": None}
        assert [[row["hospital_id"], row["full_name"], *row["shifts"], str(row["hours"]), *map(str, row["counts"])]
                for row in answer["rows"]] == csv_rows(export(mona, er, 1))[1:]


class TestANursesOwnSheet:
    def test_it_is_their_units_roster_in_force_as_the_managers_sheet_has_it(self, mona, carla, er, solver, nurses_of_er):
        approved(mona, carla, er, solver)
        changed(mona, er, pin("Amal", 9, "N"))

        seen = my_sheet(nurses_of_er("Badr"))

        assert (seen["unit"], seen["sheet"]["version"], seen["sheet"]["in_force"]) == ("ER", 2, 2)
        assert seen["sheet"]["rows"] == ok(sheet(mona, er, 2))["rows"]

    def test_a_colleagues_leave_day_reads_exactly_as_a_day_off_and_their_own_does_not(self, mona, carla, er, solver,
                                                                                      nurses_of_er):
        approved(mona, carla, er, solver)
        amal, badr = nurses_of_er("Amal"), nurses_of_er("Badr")
        before_amal, before_badr = my_sheet(amal), my_sheet(badr)

        assert leave_for(mona, er, "Badr", 3).status_code == 201  # a day he is off

        assert my_sheet(amal) == before_amal
        after_badr = my_sheet(badr)
        assert after_badr != before_badr
        assert before_badr["sheet"]["mine"][2] == {"date": f"{MONTH}-03", "kind": "off"}
        assert after_badr["sheet"]["mine"][2] == {"date": f"{MONTH}-03", "kind": "leave"}
        assert after_badr["sheet"]["rows"] == before_badr["sheet"]["rows"]
        assert before_amal["sheet"]["mine"] is not None and "leave" not in json.dumps(before_amal["sheet"]["rows"])

    def test_leave_the_solve_was_given_reads_as_a_day_off_to_a_colleague_too(self, mona, carla, er, solver, nurses_of_er):
        assert leave_for(mona, er, "Badr", 3).status_code == 201
        approved(mona, carla, er, solver)

        text = json.dumps(my_sheet(nurses_of_er("Amal")))

        assert "leave" not in text

    def test_no_sheet_before_approval_nor_for_a_month_already_over(self, mona, carla, er, solver, nurses_of_er):
        solved(mona, er, solver)
        amal = nurses_of_er("Amal")
        assert my_sheet(amal)["sheet"] is None

        assert ok(post(mona, url(er, "/roster/submit"), {"month": MONTH}))["state"] == "submitted"
        assert my_sheet(amal)["sheet"] is None
        assert my_sheet(amal, "2026-04") == {"month": "2026-04", "first_month": "2026-05", "unit": "ER", "sheet": None}

    def test_another_units_roster_is_never_theirs(self, mona, carla, er, solver, omar_of_icu, nurses_of_er):
        icu = Unit.objects.get(name="ICU")
        solved(omar_of_icu, icu, solver, prefix="I")
        ok(post(omar_of_icu, url(icu, "/roster/submit"), {"month": MONTH}))
        ok(post(carla, url(icu, "/roster/approve"), {"month": MONTH}))

        assert my_sheet(nurses_of_er("Amal"))["sheet"] is None


class TestFromAnotherSite:
    """A roster version's file only from the app's own pages; a refused one writes no `roster.exported`."""

    @pytest.mark.parametrize("file_format", ["csv", "xlsx"])
    def test_it_is_refused_and_not_logged(self, mona, carla, er, solver, file_format):
        approved(mona, carla, er, solver)

        response = mona.get(url(er, "/roster/export"), {"month": MONTH, "version": 1, "format": file_format},
                            **from_site("cross-site"))

        assert (response.status_code, response.json()["detail"]) == (403, CROSS_SITE)
        assert not AuditEntry.objects.filter(action="roster.exported").exists()

    @pytest.mark.parametrize("site", SERVED_SITES)
    def test_the_apps_own_pages_and_a_browser_that_does_not_say_are_served(self, mona, carla, er, solver, site):
        approved(mona, carla, er, solver)

        response = mona.get(url(er, "/roster/export"), {"month": MONTH, "version": 1, "format": "csv"}, **from_site(site))

        assert response.status_code == 200
        assert AuditEntry.objects.filter(action="roster.exported").count() == 1

    def test_the_print_page_is_no_file_and_is_not_refused(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)

        assert mona.get(url(er, "/roster/sheet"), {"month": MONTH, "version": 1}, **from_site("cross-site")).status_code == 200


class TestNothingOfACertificateOrLicence:
    """Identifiers, issue dates and expiries reach no file and no notification. Each is taken before a licence and a
    certificate are recorded on every nurse of the month, with their identifiers and issue dates, and after, and is the same."""

    def test_the_staff_list_the_roster_files_the_print_page_and_the_notifications_are_as_they_were(self, mona, carla, er,
                                                                                                  solver):
        from hospital.models import Skill
        from notifications.models import Notification
        from nurses.models import NurseProfile
        approved(mona, carla, er, solver)
        for name, kind, expires in [("SCFHS", "licence", True), ("BSN", "certificate", False)]:
            response = post(carla, "/api/hospital/skills", {"name": name, "kind": kind, "issuing_body": "A body",
                                                           "expires": expires, "required_to_work": False,
                                                           "lets_a_nurse_precept": False, "active": True})
            assert response.status_code == 201, response.content

        def taken() -> tuple:
            return (export(mona, er).content, workbook(export(mona, er, file_format="xlsx")), ok(sheet(mona, er)),
                    ok(sheet(carla, er)), carla.get("/api/nurses/export").content,
                    list(Notification.objects.order_by("pk").values()))

        before = taken()
        for number, nurse in enumerate(NurseProfile.objects.order_by("user__username")):
            for skill, expiry in [("SCFHS", "2026-06-20"), ("BSN", None)]:
                response = post(carla, f"/api/nurses/{nurse.pk}/skills", {
                    "skill_id": str(Skill.objects.get(name=skill).pk), "expiry": expiry,
                    "identifier": f"{skill}-QX{number}", "issue_date": "2024-06-01"})
                assert response.status_code == 201, response.content
        after = taken()

        assert after == before
        assert all("-QX" not in str(part) and "2024-06-01" not in str(part) for part in after)
