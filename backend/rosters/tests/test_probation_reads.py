"""What everyone sees of a nurse on probation: wherever the unit's roster is read (the manager's grid, the
approvers' reading of it, My roster, both print sheets, the CSV and the workbook) her row sits below the counted nurses under an
"On probation" heading with her preceptor's name, and each month reads by its own state. My month names her preceptor and her
probation's last day for her, and her for her preceptor.

The scene is `solves.tests.probation_scene`'s, on the solver's four-nurse sample, today 15 May 2026, with one change:
the nurse on probation is Abla Saad, whose name sorts before the sample's, so her row being last is the heading's doing and not
the alphabet's. She started on 1 April 2026; her 3 months are April, May and June, so her probation's last day is 30 June 2026,
counted by hand, and in July she is counted like everyone. Amal precepts her from May. June and July are solved by the real
solver in this process, beside her preceptor in June, and approved."""
import datetime as dt
import io

import pytest

from conftest import post
from rosters.models import Roster
from rosters.tests.test_exports import csv_rows, export, sheet, workbook
from rosters.tests.test_review import approve, carla, dina, month_of, nurses_of_er, ok, record_leave, submit, url  # noqa: F401
from hospital.models import FixedRamadanMonth
from solves import running
from solves.input import assemble
from solves.tests.conftest import nurse_in, started
from solves.tests.probation_scene import assign, holds_the_skill, real_job

pytestmark = pytest.mark.django_db

JUNE, JULY = "2026-06", "2026-07"
COUNTED = ["Amal", "Badr", "Dana", "Emad"]  # The sample's, by name.
LEAVE_DAY = 12  # A day of leave for Abla in June: the solver gives her nothing on it.


@pytest.fixture(autouse=True)
def _scene(before_june, solver):
    FixedRamadanMonth.objects.get_or_create(month=JULY, defaults={"dates": []})


@pytest.fixture
def abla(er, make_user, mona):
    abla = nurse_in(er, make_user, "Abla", full_name="Abla Saad", start=dt.date(2026, 4, 1), on_probation=True)
    holds_the_skill("Amal")
    assign(mona, er, abla.pk, "Amal", "2026-05")
    return abla


def approved_for_real(mona, carla, er, solver, month: str) -> None:
    solver.script([real_job(assemble(er, dt.date.fromisoformat(f"{month}-01")).input, seconds=5)])
    running.run(started(mona, er, month)["id"])
    ok(submit(mona, er, month))
    ok(approve(carla, er, month))


@pytest.fixture
def june_and_july(abla, mona, carla, er, solver):
    assert record_leave(mona, er, "Abla", LEAVE_DAY).status_code == 201
    approved_for_real(mona, carla, er, solver, JUNE)
    approved_for_real(mona, carla, er, solver, JULY)


def marks(rows) -> list[tuple]:
    return [(row["hospital_id"], row["on_probation"], row["preceptor_name"]) for row in rows]


IN_JUNE = [*((nurse, False, None) for nurse in COUNTED), ("Abla", True, "Amal")]
IN_JULY = [("Abla", False, None), *((nurse, False, None) for nurse in COUNTED)]


# ---- the pages: the manager's grid, the approvers', My roster and both print sheets ---------------------------------------------
class TestThePages:
    def test_the_managers_grid_puts_her_last_with_her_preceptor_in_june_and_among_everyone_in_july(self, june_and_july,
                                                                                                    mona, er):
        assert marks(month_of(mona, er, JUNE)["draft"]["rows"]) == IN_JUNE
        assert marks(month_of(mona, er, JULY)["draft"]["rows"]) == IN_JULY

    @pytest.mark.parametrize("approver", ["carla", "dina"])
    def test_the_cno_and_the_deputy_cno_read_it_the_same(self, approver, request, june_and_july, er):
        reader = request.getfixturevalue(approver)
        assert marks(month_of(reader, er, JUNE)["draft"]["rows"]) == IN_JUNE
        assert marks(ok(sheet(reader, er, month=JUNE))["rows"]) == IN_JUNE
        assert marks(ok(sheet(reader, er, month=JULY))["rows"]) == IN_JULY

    def test_the_print_sheet_reads_as_the_grid(self, june_and_july, mona, er):
        assert marks(ok(sheet(mona, er, month=JUNE))["rows"]) == IN_JUNE
        assert marks(ok(sheet(mona, er, month=JULY))["rows"]) == IN_JULY

    def test_a_colleague_reads_the_heading_her_preceptors_name_and_her_cells_and_her_leave_is_a_day_off(
            self, june_and_july, nurses_of_er):
        badr = nurses_of_er("Badr")

        june = ok(badr.get("/api/rosters/mine", {"month": JUNE}))["roster"]

        assert marks(june["rows"]) == IN_JUNE
        her = june["rows"][-1]
        assert set(her) == {"hospital_id", "full_name", "on_probation", "preceptor_name", "shifts"}
        assert her["full_name"] == "Abla Saad"
        grid = Roster.objects.get(unit__name="ER", month=dt.date(2026, 6, 1)).versions.first().grid["Abla"]
        assert her["shifts"] == [grid[day] for day in sorted(grid)]
        assert her["shifts"][LEAVE_DAY - 1] == ""
        assert marks(ok(badr.get("/api/rosters/mine", {"month": JULY}))["roster"]["rows"]) == IN_JULY
        assert marks(ok(badr.get("/api/rosters/mine/sheet", {"month": JUNE}))["sheet"]["rows"]) == IN_JUNE

    def test_her_preceptor_reads_it_as_any_colleague_does(self, june_and_july, nurses_of_er):
        amal = nurses_of_er("Amal")

        assert marks(ok(amal.get("/api/rosters/mine", {"month": JUNE}))["roster"]["rows"]) == IN_JUNE
        assert marks(ok(amal.get("/api/rosters/mine/sheet", {"month": JUNE}))["sheet"]["rows"]) == IN_JUNE

    def test_her_own_my_roster_and_print_sheet_have_her_under_the_heading_too(self, june_and_july, nurses_of_er):
        her = nurses_of_er("Abla")

        assert marks(ok(her.get("/api/rosters/mine", {"month": JUNE}))["roster"]["rows"]) == IN_JUNE
        own = ok(her.get("/api/rosters/mine/sheet", {"month": JUNE}))["sheet"]
        assert marks(own["rows"]) == IN_JUNE
        assert own["mine"][LEAVE_DAY - 1]["kind"] == "leave"  # Only her own row may say so.

    def test_a_month_with_nobody_on_probation_reads_as_it_always_did(self, mona, carla, er, solver):
        from rosters.tests.test_review import approved

        approved(mona, carla, er, solver)

        assert marks(month_of(mona, er)["draft"]["rows"]) == [(nurse, False, None) for nurse in COUNTED]


# ---- the files ---------------------------------------------------------------------------------------------------------------
class TestTheFiles:
    def test_the_csv_heads_her_rows_last_and_names_her_preceptor_in_a_last_column(self, june_and_july, mona, er):
        header, *rows = csv_rows(export(mona, er, month=JUNE))

        assert header[:2] == ["Hospital ID", "Full name"]
        assert header[-4:] == ["Shift hours", "Day (D)", "Night (N)", "Preceptor"]
        assert [row[0] for row in rows] == [*COUNTED, "On probation", "Abla"]
        assert rows[4] == ["On probation", *[""] * (len(header) - 1)]
        assert (rows[5][1], rows[5][-1]) == ("Abla Saad", "Amal")
        assert all(row[-1] == "" for row in rows[:4])
        assert all(len(row) == len(header) for row in rows)

    def test_the_csv_of_the_month_after_counts_her_like_everyone_with_no_heading_or_column(self, june_and_july, mona, er):
        header, *rows = csv_rows(export(mona, er, month=JULY))

        assert header[-3:] == ["Shift hours", "Day (D)", "Night (N)"]
        assert [row[0] for row in rows] == ["Abla", *COUNTED]

    def test_the_workbook_holds_the_same_table(self, june_and_july, mona, er):
        cells = workbook(export(mona, er, file_format="xlsx", month=JUNE))
        table = csv_rows(export(mona, er, month=JUNE))
        last = len(table)  # The workbook's rows are numbered from 1, its header first.
        preceptor_column = _column(len(table[0]))

        assert cells[f"{preceptor_column}1"]["value"] == "Preceptor"
        assert cells[f"A{last - 1}"]["value"] == "On probation"
        assert not any(reference.endswith(str(last - 1)) for reference in cells if not reference.startswith("A"))
        assert (cells[f"A{last}"]["value"], cells[f"{preceptor_column}{last}"]["value"]) == ("Abla", "Amal")
        assert _width(export(mona, er, file_format="xlsx", month=JUNE), len(table[0])) >= 28  # A name's, not the default 8.43.


def test_the_system_admin_reads_nothing_of_it(abla, er, signed_in):
    admin = signed_in()

    assert admin.get(url(er, "/roster"), {"month": JUNE}).status_code == 403
    assert sheet(admin, er, month=JUNE).status_code == 403
    assert export(admin, er, month=JUNE).status_code == 403
    assert admin.get("/api/rosters/mine/month", {"month": JUNE}).status_code == 403


def _width(response, column: int) -> float:
    """The width the workbook sets on a column counted from 1, read from the file's own XML."""
    import zipfile
    from xml.etree import ElementTree

    from rosters.tests.test_exports import NS

    sheet_xml = ElementTree.fromstring(zipfile.ZipFile(io.BytesIO(response.content)).read("xl/worksheets/sheet1.xml"))
    found = next(col for col in sheet_xml.iter(f"{{{NS['x']}}}col") if int(col.get("min")) <= column <= int(col.get("max")))
    return float(found.get("width"))


def _column(number: int) -> str:
    """The workbook's letters for a column counted from 1."""
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


# ---- a version planned before her probation changed ---------------------------------------------------------------------------
def test_a_version_planned_before_an_extension_reads_as_it_was_planned_and_the_page_says_what_changed(
        abla, mona, carla, er, solver, nurses_of_er):
    ok(mona.put(url(er, f"/nurses/{abla.pk}/probation/length"), {"months": 2}, content_type="application/json"))
    approved_for_real(mona, carla, er, solver, JUNE)  # April and May: June is planned with her counted.

    ok(post(mona, url(er, f"/nurses/{abla.pk}/probation/extend"), {}))

    page = month_of(mona, er, JUNE)
    counted = [("Abla", False, None), *((nurse, False, None) for nurse in COUNTED)]
    assert marks(page["draft"]["rows"]) == counted
    assert [(change["hospital_id"], change["what"]) for change in page["draft"]["changes"]] == [("Abla", "on_probation")]
    assert marks(ok(sheet(mona, er, month=JUNE))["rows"]) == counted
    assert [row[0] for row in csv_rows(export(mona, er, month=JUNE))[1:]] == ["Abla", *COUNTED]
    assert marks(ok(nurses_of_er("Badr").get("/api/rosters/mine", {"month": JUNE}))["roster"]["rows"]) == counted


# ---- My month ------------------------------------------------------------------------------------------------------------------
def my_month(client, month) -> dict:
    answer = ok(client.get("/api/rosters/mine/month", {"month": month}))
    return {"probation": answer["probation"], "precepts": answer["precepts"]}


class TestMyMonth:
    @pytest.fixture
    def clients(self, abla, nurses_of_er):
        return {nurse: nurses_of_er(nurse) for nurse in ("Abla", "Amal", "Badr")}

    def test_she_reads_her_preceptor_and_her_last_day_and_her_preceptor_reads_her(self, clients):
        assert my_month(clients["Abla"], JUNE) == {"probation": {"last_day": "2026-06-30", "preceptor_name": "Amal"},
                                                     "precepts": None}
        assert my_month(clients["Amal"], JUNE) == {"probation": None, "precepts": "Abla Saad"}
        assert my_month(clients["Badr"], JUNE) == {"probation": None, "precepts": None}

    def test_the_month_after_her_probation_neither_has_any(self, clients):
        for nurse in ("Abla", "Amal", "Badr"):
            assert my_month(clients[nurse], JULY) == {"probation": None, "precepts": None}, nurse

    def test_before_her_preceptor_is_assigned_she_reads_that_none_is(self, clients):
        assert my_month(clients["Abla"], "2026-04") == {"probation": {"last_day": "2026-06-30", "preceptor_name": None},
                                                          "precepts": None}

    def test_it_reads_her_probation_as_it_stands_once_extended(self, clients, abla, mona, er):
        ok(post(mona, url(er, f"/nurses/{abla.pk}/probation/extend"), {}))

        assert my_month(clients["Abla"], JULY) == {"probation": {"last_day": "2026-07-31", "preceptor_name": "Amal"},
                                                     "precepts": None}
        assert my_month(clients["Amal"], JULY) == {"probation": None, "precepts": "Abla Saad"}
