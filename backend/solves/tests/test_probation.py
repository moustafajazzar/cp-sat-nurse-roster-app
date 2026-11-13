"""The solve input of a month with a nurse on probation: `on_probation` month by month across her probation's end,
`preceptor` only while an assignment is in force and its preceptor is a nurse of the input, a re-plan's and a close's inputs,
an input the solver never refuses, and "changes since". The scene is `probation_scene`'s."""
import datetime as dt

import pytest

from accounts.models import Role
from conftest import post
from nurses.models import NurseProfile
from rosters.models import Roster
from solves.input import assemble, assemble_replan, changes_since, solver_refusal
from solves.tests.conftest import nurse_in
from solves.tests.probation_scene import add_faten, assign, holds_the_skill, real_job
from rosters.tests.test_review import approve, carla, month_of, submit, ok  # noqa: F401
from solves import running
from solves.tests.conftest import started
from nurses.tests.conftest import cno  # noqa: F401

pytestmark = pytest.mark.django_db

MAY, JUNE, JULY = dt.date(2026, 5, 1), dt.date(2026, 6, 1), dt.date(2026, 7, 1)


@pytest.fixture(autouse=True)
def _scene(before_june, solver):
    pass


@pytest.fixture
def faten(er, make_user, mona):
    faten = add_faten(er, make_user)
    holds_the_skill("Amal")
    assign(mona, er, faten.pk, "Amal", "2026-05")
    return faten


def rows(month) -> dict[str, dict]:
    from units.models import Unit

    return {row["id"]: row for row in assemble(Unit.objects.get(name="ER"), month).nurses}


def marks(month) -> dict[str, tuple]:
    return {hospital_id: (row.get("on_probation"), row.get("preceptor")) for hospital_id, row in rows(month).items()
            if "on_probation" in row or "preceptor" in row}


def test_the_input_marks_her_month_by_month_across_her_probations_end(er, faten):
    assert marks(MAY) == {"Faten": (True, "Amal")}
    assert marks(JUNE) == {"Faten": (True, "Amal")}
    assert marks(JULY) == {}  # Her probation ended on 30 June: she is counted from July, and Amal precepts no one.
    assert solver_refusal(assemble(er, JUNE).input) is None


def test_a_month_with_nobody_on_probation_is_the_input_it_always_was(er):
    assert all("on_probation" not in row and "preceptor" not in row for row in assemble(er, JUNE).nurses)


def test_with_no_preceptor_in_force_she_is_marked_and_follows_no_one(er, make_user):
    add_faten(er, make_user)

    assert marks(JUNE) == {"Faten": (True, None)}
    assert "preceptor" not in rows(JUNE)["Faten"]


def test_a_preceptor_not_in_the_input_is_not_named(er, faten, client_of):
    amal = NurseProfile.objects.get(user__username="Amal")
    amal.user.is_active = False  # A deactivated account whose profile runs on is left out of the month.
    amal.user.save(update_fields=["is_active"])
    amal.user.role = Role.UNIT_MANAGER
    amal.user.save(update_fields=["role"])

    assert "Amal" not in rows(JUNE)
    assert marks(JUNE) == {"Faten": (True, None)}
    assert solver_refusal(assemble(er, JUNE).input) is None


def test_a_preceptor_put_on_probation_later_is_not_named(er, faten, cno):
    amal = NurseProfile.objects.get(user__username="Amal")
    # A correction: she started on 1 May, newly hired, so her probation is May to July.
    response = cno.put(f"/api/nurses/{amal.pk}/details", {"gender": amal.gender, "start_date": "2026-05-01",
                                                           "on_probation": True}, content_type="application/json")
    assert response.status_code == 200, response.content

    assert marks(JUNE) == {"Faten": (True, None), "Amal": (True, None)}
    assert solver_refusal(assemble(er, JUNE).input) is None


def test_of_two_nurses_naming_one_preceptor_the_assignment_made_first_keeps_him(er, faten, make_user, mona, cno):
    hind = nurse_in(er, make_user, "Hind", full_name="Hind Zaki", start=dt.date(2026, 7, 1), on_probation=True)
    assign(mona, er, hind.pk, "Amal", "2026-07")
    # A start date corrected later moves Faten's probation into July, where Amal already precepts Hind.
    response = cno.put(f"/api/nurses/{faten.pk}/details", {"gender": "F", "start_date": "2026-05-01"},
                       content_type="application/json")
    assert response.status_code == 200, response.content

    assert marks(JULY) == {"Faten": (True, "Amal"), "Hind": (True, None)}
    assert solver_refusal(assemble(er, JULY).input) is None


def test_a_replans_and_a_closes_inputs_mark_her_too(er, faten):
    in_force = {row["id"]: {} for row in assemble(er, JUNE).nurses}

    replan = assemble_replan(er, JUNE, dt.date(2026, 6, 10), in_force)

    marked = {row["id"]: (row.get("on_probation"), row.get("preceptor")) for row in replan.nurses if row.get("on_probation")}
    assert marked == {"Faten": (True, "Amal")}


def test_changes_since_report_her_probation_and_her_preceptor(er, faten, cno, mona):
    stored = assemble(er, JUNE).input
    plain = {**stored, "nurses": [{key: value for key, value in row.items() if key not in ("on_probation", "preceptor")}
                                  for row in stored["nurses"]]}
    assert changes_since(stored, er, JUNE) == []
    assert changes_since(plain, er, JUNE) == [{"hospital_id": "Faten", "what": "on_probation"}]

    holds_the_skill("Badr")
    assign(mona, er, faten.pk, "Badr", "2026-06")
    assert changes_since(stored, er, JUNE) == [{"hospital_id": "Badr", "what": "skills"},  # the skill he now holds
                                               {"hospital_id": "Faten", "what": "preceptor"}]

    response = cno.put(f"/api/nurses/{faten.pk}/details", {"gender": "F", "start_date": "2026-04-01", "on_probation": False},
                       content_type="application/json")
    assert response.status_code == 200, response.content
    assert changes_since(stored, er, JUNE)[1:] == [{"hospital_id": "Faten", "what": "off_probation"}]


def test_an_extension_reaching_an_approved_month_changes_nothing_in_it_and_the_page_says_how_it_was_planned(
        er, faten, mona, carla, solver):
    ok(mona.put(f"/api/units/{er.pk}/nurses/{faten.pk}/probation/length", {"months": 2}, content_type="application/json"))
    assert marks(JUNE) == {}  # April and May: June is planned with her counted.
    solver.script([real_job(assemble(er, JUNE).input)])
    running.run(started(mona, er)["id"])
    ok(submit(mona, er))
    ok(approve(carla, er))
    grid = Roster.objects.get(unit=er, month=JUNE).grid

    ok(post(mona, f"/api/units/{er.pk}/nurses/{faten.pk}/probation/extend", {}))

    assert marks(JUNE) == {"Faten": (True, "Amal")}
    assert Roster.objects.get(unit=er, month=JUNE).grid == grid
    page = month_of(mona, er)
    assert page["state"] == "approved"
    assert [(change["hospital_id"], change["what"]) for change in page["draft"]["changes"]] == [("Faten", "on_probation")]


# ---- the real solver, in this process: the roster beside her preceptor, and its explanation -----------------------------------
def solved_for_real(mona, er, solver) -> dict:
    """The real solver's result for June as the app assembles it, through the app's own solve and result reading."""
    solver.script([real_job(assemble(er, JUNE).input)])
    solve_id = started(mona, er)["id"]
    running.run(solve_id)
    return ok(mona.get(f"/api/units/{er.pk}/solve-history/{solve_id}"))


def test_she_is_beside_her_preceptor_works_no_night_and_every_headcount_is_met_without_her(er, faten, mona, solver):
    answer = solved_for_real(mona, er, solver)

    grid = Roster.objects.get(unit=er, month=JUNE).grid
    assert answer["violations"] == []
    for day in grid["Faten"]:
        assert grid["Faten"][day] != "N"
        if grid["Amal"][day] == "D":
            assert grid["Faten"][day] == "D", day
        # The sample's D is exactly one nurse and N one or two, counted without her.
        assert sum(grid[nurse][day] == "D" for nurse in grid if nurse != "Faten") == 1, day
        assert 1 <= sum(grid[nurse][day] == "N" for nurse in grid if nurse != "Faten") <= 2, day
    # Her hours hold every one of Amal's day shifts, however many the search gave her (two, on this machine).
    amal_days = sum(code == "D" for code in grid["Amal"].values())
    assert answer["following"] == [{"hospital_id": "Faten", "full_name": "Faten Ali", "preceptor": "Amal",
                                    "preceptor_name": "Amal", "followed": amal_days, "missed": []}]


def test_the_explanation_says_which_of_her_preceptors_day_shifts_she_did_not_follow_and_why(er, faten, mona, solver):
    from hospital.models import Shift
    from rosters.models import PinOrigin, PinnedCell
    from rosters.tests.test_review import record_leave

    # Amal on D on the 4th, 7th and 10th, pinned; Faten on leave on the 7th and her day off accepted on the 10th.
    amal, shift = NurseProfile.objects.get(user__username="Amal"), Shift.objects.get(code="D")
    for day in (4, 7, 10):
        PinnedCell.objects.create(unit=er, nurse=amal, date=dt.date(2026, 6, day), shift=shift, pinned_by=er.manager)
    assert record_leave(mona, er, "Faten", 7).status_code == 201
    PinnedCell.objects.create(unit=er, nurse=faten, date=dt.date(2026, 6, 10), shift=None, origin=PinOrigin.REQUEST,
                              pinned_by=er.manager)

    answer = solved_for_real(mona, er, solver)

    missed = {day["date"]: (day["shift"], day["why"]) for day in answer["following"][0]["missed"]}
    assert (missed["2026-06-07"], missed["2026-06-10"]) == (("D", "leave"), ("D", "pinned"))
    assert "2026-06-04" not in missed  # nothing keeps her from Amal's pinned D on the 4th
    grid = Roster.objects.get(unit=er, month=JUNE).grid
    assert grid["Faten"]["2026-06-04"] == "D" and grid["Faten"]["2026-06-10"] == ""  # an accepted day off holds over following


def test_with_no_preceptor_the_solves_warnings_name_her(er, make_user, mona, solver):
    add_faten(er, make_user)

    answer = solved_for_real(mona, er, solver)

    assert any(line.startswith("Faten: on probation with no preceptor") for line in answer["precheck"])
    assert answer["following"] == [{"hospital_id": "Faten", "full_name": "Faten Ali", "preceptor": None, "preceptor_name": "",
                                    "followed": 0, "missed": []}]
    assert "N" not in Roster.objects.get(unit=er, month=JUNE).grid["Faten"].values()
