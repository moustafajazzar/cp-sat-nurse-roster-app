"""Editing the draft by hand: saving pins, what they do to the grid and the next solve, and everything a stored pin can fall out
of step with. The API with the scripted solver and the real worker's code; the check is the solver's own code, here and with no search."""
import datetime as dt
import json

import pytest
from nurse_roster.request import load_request

from accounts.models import Role, User
from audit.models import AuditEntry
from conftest import post
from hospital.models import HospitalSettings, LeaveType, Shift
from nurses import membership
from nurses.models import NurseProfile, Placement
from nurses.tests.conftest import make_unit
from rosters.models import PinEnded, PinnedCell, PinOrigin, Roster
from solves import running
from solves.input import assemble, solver_refusal
from solves.models import Solve
from solves.tests.conftest import MONTH, NURSES, job, nurse_in, result_of, roster_of, sample_rules, started

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _scene(before_june, solver):
    pass


def url(unit, rest=""):
    return f"/api/units/{unit.pk}{rest}"


def finished(shift="D", *, cost=7):
    return job("finished", result=result_of(roster_of(NURSES, shift), cost=cost), best_cost=cost, improvements=3)


def solved(client, unit, solver, answer=None, month=MONTH) -> str:
    solver.script([answer or finished()])
    solve = started(client, unit, month)
    running.run(solve["id"])
    return solve["id"]


def pin(nurse, day, shift) -> dict:
    return {"hospital_id": nurse, "date": f"{MONTH}-{day:02d}", "action": "shift", "shift": shift}


def off(nurse, day) -> dict:
    return {"hospital_id": nurse, "date": f"{MONTH}-{day:02d}", "action": "off"}


def clear(nurse, day) -> dict:
    return {"hospital_id": nurse, "date": f"{MONTH}-{day:02d}", "action": "clear"}


def save(client, unit, *edits, month=MONTH):
    return post(client, url(unit, "/roster/pins"), {"month": month, "edits": list(edits)})


def saved(client, unit, *edits, month=MONTH) -> dict:
    response = save(client, unit, *edits, month=month)
    assert response.status_code == 200, response.content
    return response.json()


def check(client, unit, *edits, month=MONTH) -> dict:
    response = post(client, url(unit, "/roster/check"), {"month": month, "edits": list(edits)})
    assert response.status_code == 200, response.content
    return response.json()


def month_of(client, unit, month=MONTH) -> dict:
    response = client.get(url(unit, "/roster"), {"month": month})
    assert response.status_code == 200, response.content
    return response.json()


def cell(draft, nurse, day) -> dict:
    row = next(row for row in draft["rows"] if row["hospital_id"] == nurse)
    return row["cells"][[d["date"] for d in draft["days"]].index(f"{MONTH}-{day:02d}")]


def pins_of(draft) -> dict:
    return {(p["hospital_id"], p["date"]): p["shift"] for p in draft["pins"]}


def grid(unit) -> dict:
    return Roster.objects.get(unit=unit).grid


def input_row(solver_input, nurse) -> dict:
    return next(row for row in solver_input["nurses"] if row["id"] == nurse)


class TestSavingPins:
    def test_a_pinned_shift_and_a_pinned_day_off_change_the_draft_and_are_stored_as_the_managers(self, mona, er, solver):
        solved(mona, er, solver, finished("D"))

        answer = saved(mona, er, pin("Amal", 3, "N"), off("Badr", 4))

        draft = answer["draft"]
        assert cell(draft, "Amal", 3) == {"shift": "N", "kind": "shift"}
        assert cell(draft, "Badr", 4)["shift"] == ""
        assert cell(draft, "Dana", 3)["shift"] == "D"
        assert {(p["hospital_id"], p["date"], p["shift"], p["origin"]) for p in draft["pins"]} == {
            ("Amal", "2026-06-03", "N", "manager"), ("Badr", "2026-06-04", "", "manager")}
        assert month_of(mona, er)["draft"]["pins"] == draft["pins"]

    def test_clearing_gives_the_cell_back_to_what_its_solve_said(self, mona, er, solver):
        solved(mona, er, solver, finished("D"))
        saved(mona, er, pin("Amal", 3, "N"))

        draft = saved(mona, er, clear("Amal", 3))["draft"]

        assert cell(draft, "Amal", 3)["shift"] == "D"
        assert draft["pins"] == []
        assert draft["edited"] is False

    def test_a_pin_changed_again_is_one_pin(self, mona, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))

        draft = saved(mona, er, off("Amal", 3))["draft"]

        assert pins_of(draft) == {("Amal", "2026-06-03"): ""}

    def test_there_must_be_a_draft_to_edit(self, mona, er, solver):
        assert save(mona, er, pin("Amal", 3, "N")).status_code == 409

    def test_it_is_all_or_nothing_and_names_each_refused_edit_by_its_place(self, mona, er, solver):
        solved(mona, er, solver)

        response = save(mona, er, pin("Amal", 3, "N"), pin("Emad", 3, "N"))

        assert response.status_code == 422
        assert [error["loc"][2:] for error in response.json()["detail"]] == [["edits", 1]]
        assert grid(er)["Amal"]["2026-06-03"] == "D"
        assert month_of(mona, er)["draft"]["pins"] == []

    def test_one_cell_twice_in_a_save_is_refused(self, mona, er, solver):
        solved(mona, er, solver)

        assert save(mona, er, pin("Amal", 3, "N"), off("Amal", 3)).status_code == 422

    def test_another_units_manager_cannot_save_or_check(self, mona, er, solver, client_of):
        solved(mona, er, solver)
        other = client_of("omar", Role.UNIT_MANAGER)

        assert save(other, er, pin("Amal", 3, "N")).status_code == 403
        assert post(other, url(er, "/roster/check"), {"month": MONTH, "edits": []}).status_code == 403


class TestTheNextSolve:
    def test_saved_pins_go_in_the_input_and_the_preview_counts_them(self, mona, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"), off("Badr", 4))

        assert mona.get(url(er, "/solves/preview"), {"month": MONTH}).json()["summary"]["pins"] == 2
        solved(mona, er, solver, finished("N"))

        given = solver.started[-1]
        assert input_row(given, "Amal")["pinned"] == {"2026-06-03": "N"}
        assert input_row(given, "Badr")["pinned"] == {"2026-06-04": ""}
        assert "pinned" not in input_row(given, "Dana")

    def test_solving_again_keeps_them(self, mona, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))

        solved(mona, er, solver, job("finished", result=result_of(
            roster_of(NURSES, "D") | {"Amal": dict.fromkeys(roster_of(["Amal"])["Amal"], "D") | {"2026-06-03": "N"}})))

        draft = month_of(mona, er)["draft"]
        assert cell(draft, "Amal", 3)["shift"] == "N"
        assert pins_of(draft) == {("Amal", "2026-06-03"): "N"}
        assert draft["changes"] == []

    def test_a_cleared_pin_does_not_reach_it(self, mona, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))
        saved(mona, er, clear("Amal", 3))

        solved(mona, er, solver)

        assert "pinned" not in input_row(solver.started[-1], "Amal")

    def test_a_pin_saved_after_a_solve_warns(self, mona, er, solver):
        solved(mona, er, solver)

        draft = saved(mona, er, pin("Amal", 3, "N"))["draft"]

        assert [(c["hospital_id"], c["what"]) for c in draft["changes"]] == [("Amal", "pins")]

    def test_clearing_it_again_stops_the_warning(self, mona, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))

        assert saved(mona, er, clear("Amal", 3))["draft"]["changes"] == []

    def test_a_stored_input_stays_as_it_was(self, mona, er, solver):
        solve_id = solved(mona, er, solver)

        saved(mona, er, pin("Amal", 3, "N"))

        assert "pinned" not in json.dumps(Solve.objects.get(pk=solve_id).input)


class TestCheckingBeforeSaving:
    def test_it_says_what_the_grid_would_break_and_how_the_cost_would_move_and_saves_nothing(self, mona, er, solver):
        roster = {nurse: {day: ("D" if int(day[-2:]) % 2 else "") for day in days} for nurse, days in roster_of(NURSES).items()}
        solved(mona, er, solver, job("finished", result=result_of(roster, cost=7)))

        before = check(mona, er)
        answer = check(mona, er, pin("Amal", 2, "N"), pin("Amal", 3, "D"))

        assert answer["reason"] is None
        assert answer["refusals"] == []
        assert answer["cost_before"] == before["cost"]
        assert answer["cost"] - answer["cost_before"] == answer["delta"]
        assert any(v["rule"] == "succession" and v["hospital_id"] == "Amal" and v["date"] == "2026-06-02" for v in answer["violations"])
        assert answer["ok"] is False
        assert grid(er)["Amal"]["2026-06-02"] == ""
        assert month_of(mona, er)["draft"]["pins"] == []

    def test_a_grid_it_cannot_read_gets_a_reason_not_an_error(self, mona, er, solver):
        solved(mona, er, solver, finished("D"))
        rules = er.rules | {"shifts": ["N"], "headcounts": [h for h in er.rules["headcounts"] if h["shift"] == "N"]}
        type(er).objects.filter(pk=er.pk).update(rules=rules)

        answer = check(mona, er, pin("Amal", 3, "N"))

        assert answer["reason"]
        assert answer["cost"] is None and answer["ok"] is None

    def test_an_edit_that_could_not_be_saved_is_named_and_left_out(self, mona, er, solver):
        solved(mona, er, solver)

        answer = check(mona, er, pin("Emad", 3, "N"))

        assert [(r["hospital_id"], r["reason"]) for r in answer["refusals"]] == [("Emad", "N is not one of Emad's allowed shifts.")]
        assert answer["delta"] == 0


class TestAnEditedDraft:
    def test_its_cost_is_the_grids_with_the_solves_beside_it(self, mona, er, solver):
        solved(mona, er, solver, finished("D", cost=7))

        draft = saved(mona, er, off("Amal", 3))["draft"]

        assert draft["edited"] is True
        assert draft["solved_cost"] == 7
        assert draft["cost"] == check(mona, er)["cost"]
        assert draft["check"]["reason"] is None and draft["check"]["ok"] is False

    def test_an_unedited_one_keeps_the_solves_cost_with_nothing_worked_out(self, mona, er, solver):
        solved(mona, er, solver, finished("D", cost=7))

        draft = month_of(mona, er)["draft"]

        assert draft["edited"] is False and draft["cost"] == 7 and draft["check"] is None

    def test_one_solved_before_leave_is_costed_on_the_month_as_it_stands(self, mona, er, solver):
        solved(mona, er, solver, finished("D", cost=7))
        record_leave(mona, er, "Amal", 3)

        draft = month_of(mona, er)["draft"]

        assert draft["edited"] is False and draft["solved_cost"] == 7
        assert draft["cost"] == check(mona, er)["cost_before"]
        assert any(v["rule"] == "unavailable" and v["date"] == "2026-06-03" for v in draft["check"]["violations"])

    def test_a_nurse_who_joined_since_the_solve_has_no_row_and_their_pin_breaks_nothing(self, mona, er, solver, make_user):
        from solves.tests.conftest import nurse_in
        solved(mona, er, solver)
        nurse_in(er, make_user, "Faisal")
        PinnedCell.objects.create(unit=er, nurse=profile("Faisal"), date="2026-06-03", shift=Shift.objects.get(code="N"),
                                  pinned_by=User.objects.get(username="mona"))

        draft = month_of(mona, er)["draft"]

        assert not any(v["rule"] == "pin" for v in draft["check"]["violations"])

    def test_a_shift_on_a_day_an_expired_required_skill_bars_is_refused_on_save_and_on_a_check_naming_the_skill(
            self, mona, er, solver):
        """As on leave, a shift cannot be pinned there; a day off can, since she works nothing that day either way."""
        from rosters.tests.test_review import holding
        solved(mona, er, solver, finished("N", cost=7))
        holding("Amal", "BLS", dt.date(2026, 6, 20), required=True)
        words = "Amal's BLS, required to work, counted until 20 June 2026, so Amal can work no shift on 25 June 2026."

        response = save(mona, er, pin("Amal", 25, "D"))

        assert response.status_code == 422 and response.json()["detail"][0]["msg"] == words
        assert check(mona, er, pin("Amal", 25, "D"))["refusals"] == [
            {"hospital_id": "Amal", "date": "2026-06-25", "reason": words}]
        assert pins_of(saved(mona, er, off("Amal", 25), pin("Amal", 20, "D"))["draft"]) == {
            ("Amal", "2026-06-25"): "", ("Amal", "2026-06-20"): "D"}

    def test_a_pin_on_what_the_cell_already_held_is_no_edit_of_the_grid(self, mona, er, solver):
        solved(mona, er, solver, finished("D", cost=7))

        draft = saved(mona, er, pin("Amal", 3, "D"))["draft"]

        assert draft["edited"] is False and draft["solved_cost"] == 7
        assert pins_of(draft) == {("Amal", "2026-06-03"): "D"}


def icu(manager):
    unit = make_unit("ICU", manager)
    unit.rules = sample_rules()
    unit.save()
    return unit


def cno_of(client_of):
    return client_of("carla", Role.CNO)


def shift_body(held, **changes) -> dict:
    shift = Shift.objects.get(code=held)
    return {"code": shift.code, "display_name": shift.display_name, "hours": shift.hours, "start_time": shift.start_time.strftime("%H:%M"),
            "end_time": shift.end_time.strftime("%H:%M"), "colour": shift.colour, "active": shift.active} | changes


def change_shift(cno, held, **changes):
    return cno.put(f"/api/hospital/shifts/{Shift.objects.get(code=held).pk}", shift_body(held, **changes), content_type="application/json")


def profile(hospital_id) -> NurseProfile:
    return NurseProfile.objects.get(user__username=hospital_id)


def asides(draft) -> dict:
    return {(p["hospital_id"], p["date"]): p["set_aside"] for p in draft["pins"] if p["set_aside"]}


def solve_input_is_fine(unit) -> None:
    assert solver_refusal(assemble(unit, dt.date(2026, 6, 1)).input) is None


class TestARename:
    def test_a_pin_follows_it_and_nothing_warns(self, mona, er, solver, client_of):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))
        second = solved(mona, er, solver, job("finished", result=result_of(
            roster_of(NURSES, "D") | {"Amal": roster_of(["Amal"], "D")["Amal"] | {"2026-06-03": "N"}})))
        assert month_of(mona, er)["draft"]["changes"] == []

        assert change_shift(cno_of(client_of), "N", code="NT").status_code == 200
        er.refresh_from_db()

        draft = month_of(mona, er)["draft"]
        assert pins_of(draft) == {("Amal", "2026-06-03"): "NT"}
        assert cell(draft, "Amal", 3)["shift"] == "NT"
        assert draft["changes"] == []
        assert input_row(assemble(er, dt.date(2026, 6, 1)).input, "Amal")["pinned"] == {"2026-06-03": "NT"}
        assert input_row(Solve.objects.get(pk=second).input, "Amal")["pinned"] == {"2026-06-03": "N"}

    def test_an_edit_still_holding_the_old_code_is_carried(self, mona, er, solver, client_of):
        solved(mona, er, solver)
        change_shift(cno_of(client_of), "N", code="NT")

        draft = saved(mona, er, pin("Amal", 3, "N"))["draft"]

        assert pins_of(draft) == {("Amal", "2026-06-03"): "NT"}

    def test_clearing_after_one_gives_back_the_new_code(self, mona, er, solver, client_of):
        solved(mona, er, solver, finished("N"))
        saved(mona, er, off("Amal", 3))
        change_shift(cno_of(client_of), "N", code="NT")

        assert cell(saved(mona, er, clear("Amal", 3))["draft"], "Amal", 3)["shift"] == "NT"


class TestTheCataloguesGuards:
    def test_hours_are_refused_while_a_pin_uses_the_shift_even_with_no_grid_holding_it(self, mona, er, solver, client_of):
        solved(mona, er, solver, finished("D"))
        saved(mona, er, pin("Amal", 3, "N"))
        Roster.objects.update(grid=roster_of(NURSES, "D"))

        response = change_shift(cno_of(client_of), "N", hours=10)

        assert response.status_code == 422
        assert "ER, June 2026" in response.json()["detail"][0]["msg"]

    def test_hours_go_through_once_it_is_cleared(self, mona, er, solver, client_of):
        solved(mona, er, solver, finished("D"))
        saved(mona, er, pin("Amal", 3, "N"))
        saved(mona, er, clear("Amal", 3))
        Solve.objects.update(state="succeeded")

        assert change_shift(cno_of(client_of), "N", hours=10).status_code == 200

    def test_retiring_is_refused_while_a_unit_works_it_and_a_counted_pin_needs_the_unit_to_work_it(self, mona, er, solver, client_of):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))
        settings = HospitalSettings.load()
        settings.rules["forbidden_successions"] = []
        settings.save()

        response = change_shift(cno_of(client_of), "N", active=False)

        assert response.status_code == 422 and "ER" in response.json()["detail"][0]["msg"]

    def test_a_pin_on_a_shift_retired_all_the_same_is_set_aside_and_no_failed_solve(self, mona, er, solver):
        solved(mona, er, solver, finished("D"))
        saved(mona, er, pin("Amal", 3, "N"))
        Shift.objects.filter(code="N").update(active=False)
        type(er).objects.filter(pk=er.pk).update(rules=er.rules | {"shifts": ["D"], "headcounts": [h for h in er.rules["headcounts"] if h["shift"] == "D"]})

        assert asides(month_of(mona, er)["draft"]) == {("Amal", "2026-06-03"): "shift_not_worked"}
        assert "pinned" not in input_row(assemble(type(er).objects.get(pk=er.pk), dt.date(2026, 6, 1)).input, "Amal")


class TestAUnitDroppingAShift:
    def rules(self, er, **changes) -> dict:
        return {"rules": sample_rules() | changes, "request_cap": er.request_cap, "preference_cap": er.preference_cap, "default_time_limit": er.default_time_limit}

    def test_it_is_refused_while_a_pin_uses_it_naming_who_and_when(self, mona, er, solver):
        Shift.objects.create(code="E", display_name="Evening", hours=8, start_time="14:00", end_time="22:00", colour="#00aa00", active=True)
        er.rules["shifts"] = ["D", "N", "E"]
        er.save()
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "E"))

        response = mona.put(url(er, "/rules"), self.rules(er), content_type="application/json")

        assert response.status_code == 422, response.content
        assert any("Amal" in e["msg"] and "3 June 2026" in e["msg"] for e in response.json()["detail"])

        saved(mona, er, clear("Amal", 3))
        assert mona.put(url(er, "/rules"), self.rules(er), content_type="application/json").status_code == 200

    def test_a_month_that_is_over_does_not_stop_it(self, mona, er, solver, monkeypatch):
        Shift.objects.create(code="E", display_name="Evening", hours=8, start_time="14:00", end_time="22:00", colour="#00aa00", active=True)
        er.rules["shifts"] = ["D", "N", "E"]
        er.save()
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "E"))
        monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 7, 1))

        assert mona.put(url(er, "/rules"), self.rules(er), content_type="application/json").status_code == 200


class TestAllowedShifts:
    def body(self, allowed) -> dict:
        return {"allowed_shifts": allowed}

    def test_narrowing_them_past_a_pin_is_refused_naming_the_day(self, mona, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))

        response = mona.put(url(er, f"/nurses/{profile('Amal').pk}/allowed-shifts"), self.body(["D"]), content_type="application/json")

        assert response.status_code == 422 and "3 June 2026" in response.json()["detail"][0]["msg"]

    def test_narrowing_them_around_a_pin_is_fine(self, mona, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))

        response = mona.put(url(er, f"/nurses/{profile('Amal').pk}/allowed-shifts"), self.body(["N"]), content_type="application/json")

        assert response.status_code == 200

    def test_a_row_outside_them_all_the_same_is_set_aside_and_no_failed_solve(self, mona, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))
        Placement.objects.filter(nurse=profile("Amal")).update(allowed_shifts=["D"])

        assert asides(month_of(mona, er)["draft"]) == {("Amal", "2026-06-03"): "shift_not_allowed"}
        assert "pinned" not in input_row(assemble(er, dt.date(2026, 6, 1)).input, "Amal")


class TestANurseLeavingTheMonth:
    def test_an_end_date_before_the_day_counts_like_leave_and_coming_back_brings_the_pin_back(self, mona, er, solver, client_of):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 20, "N"), off("Amal", 25))
        cno = cno_of(client_of)

        cno.put(f"/api/nurses/{profile('Amal').pk}/end-date", {"end_date": "2026-06-10"}, content_type="application/json")

        draft = month_of(mona, er)["draft"]
        assert asides(draft) == {("Amal", "2026-06-20"): "outside_dates", ("Amal", "2026-06-25"): "outside_dates"}
        row = input_row(assemble(er, dt.date(2026, 6, 1)).input, "Amal")
        assert "pinned" not in row and "2026-06-20" in row["leave"]
        solve_input_is_fine(er)
        assert ("Amal", "leave") in {(c["hospital_id"], c["what"]) for c in draft["changes"]}

        cno.put(f"/api/nurses/{profile('Amal').pk}/end-date", {"end_date": None}, content_type="application/json")

        assert asides(month_of(mona, er)["draft"]) == {}
        assert input_row(assemble(er, dt.date(2026, 6, 1)).input, "Amal")["pinned"] == {"2026-06-20": "N", "2026-06-25": ""}

    def test_a_start_date_corrected_past_the_day_counts_like_leave(self, mona, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))
        NurseProfile.objects.filter(user__username="Amal").update(start_date=dt.date(2026, 6, 10))

        assert asides(month_of(mona, er)["draft"]) == {("Amal", "2026-06-03"): "outside_dates"}
        solve_input_is_fine(er)

    def test_a_transfer_leaves_the_pin_behind_and_the_new_unit_does_not_get_it(self, mona, er, solver, make_user, client_of):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))
        other = icu(make_user("omar", Role.UNIT_MANAGER))

        answer = cno_of(client_of).post(f"/api/nurses/{profile('Amal').pk}/transfer", {"unit_id": str(other.pk), "from_month": MONTH},
                                        content_type="application/json")

        assert answer.status_code == 200, answer.content
        assert asides(month_of(mona, er)["draft"]) == {("Amal", "2026-06-03"): "not_in_month"}
        assert all("Amal" != row["id"] for row in assemble(er, dt.date(2026, 6, 1)).input["nurses"])
        assert "pinned" not in input_row(assemble(other, dt.date(2026, 6, 1)).input, "Amal")

    def test_the_new_units_manager_can_pin_a_day_the_old_units_pin_still_holds(self, mona, er, solver, make_user, client_of):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))
        omar = client_of("omar", Role.UNIT_MANAGER)
        other = icu(User.objects.get(username="omar"))
        cno_of(client_of).post(f"/api/nurses/{profile('Amal').pk}/transfer", {"unit_id": str(other.pk), "from_month": MONTH},
                               content_type="application/json")
        solved(omar, other, solver)

        draft = saved(omar, other, off("Amal", 3))["draft"]

        assert pins_of(draft) == {("Amal", "2026-06-03"): ""}
        assert asides(month_of(mona, er)["draft"]) == {}
        assert month_of(mona, er)["draft"]["pins"] == []

    def test_a_pin_set_aside_can_still_be_cleared_though_no_new_one_can_be_made(self, mona, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))
        NurseProfile.objects.filter(user__username="Amal").update(start_date=dt.date(2026, 6, 10))

        assert save(mona, er, pin("Amal", 4, "N")).status_code == 422
        assert saved(mona, er, clear("Amal", 3))["draft"]["pins"] == []


class TestABarredDay:
    """A day after a required skill of hers expired is hers to work no shift on, and a pin of a shift there is set
    aside as one on leave is, not removed: it counts again once the skill is renewed or its record taken off."""

    @staticmethod
    def amal_pinned_then_barred(mona, er, solver):
        from rosters.tests.test_review import holding
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 20, "N"), pin("Amal", 25, "D"), off("Amal", 26), pin("Amal", 10, "N"))
        return holding("Amal", "BLS", dt.date(2026, 6, 15), required=True)

    def test_a_shift_pinned_after_the_expiry_is_set_aside_and_reaches_no_solve(self, mona, er, solver):
        self.amal_pinned_then_barred(mona, er, solver)

        draft = month_of(mona, er)["draft"]

        assert asides(draft) == {("Amal", "2026-06-20"): "barred", ("Amal", "2026-06-25"): "barred"}
        assert len(draft["pins"]) == 4
        assert input_row(assemble(er, dt.date(2026, 6, 1)).input, "Amal")["pinned"] == {"2026-06-10": "N", "2026-06-26": ""}
        solve_input_is_fine(er)

    def test_renewing_the_skill_makes_the_pins_count_again(self, mona, er, solver, client_of):
        held = self.amal_pinned_then_barred(mona, er, solver)

        response = cno_of(client_of).put(f"/api/nurses/{profile('Amal').pk}/skills/{held.skill_id}", {"expiry": "2027-06-30"},
                                         content_type="application/json")

        assert response.status_code == 200, response.content
        assert asides(month_of(mona, er)["draft"]) == {}
        assert input_row(assemble(er, dt.date(2026, 6, 1)).input, "Amal")["pinned"] == {
            "2026-06-10": "N", "2026-06-20": "N", "2026-06-25": "D", "2026-06-26": ""}

    def test_taking_the_record_off_makes_the_pins_count_again(self, mona, er, solver, client_of):
        """A nurse with no record of a required skill is rostered as before: the gap is the CNO's to fill."""
        held = self.amal_pinned_then_barred(mona, er, solver)

        response = cno_of(client_of).post(f"/api/nurses/{profile('Amal').pk}/skills/{held.skill_id}/remove")

        assert response.status_code == 200, response.content
        assert asides(month_of(mona, er)["draft"]) == {}

    def test_a_skill_no_longer_required_to_work_bars_nobody(self, mona, er, solver):
        held = self.amal_pinned_then_barred(mona, er, solver)
        type(held.skill).objects.filter(pk=held.skill_id).update(required_to_work=False)

        assert asides(month_of(mona, er)["draft"]) == {}


class TestAMonthThatIsOver:
    def test_a_pin_is_refused_and_the_page_is_told_and_the_row_stays(self, mona, er, solver, monkeypatch):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))
        monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 7, 1))

        assert save(mona, er, pin("Amal", 4, "N")).status_code == 409
        assert save(mona, er, clear("Amal", 3)).status_code == 409
        draft = month_of(mona, er)["draft"]
        assert "over" in draft["editor"]["refusal"]
        assert pins_of(draft) == {("Amal", "2026-06-03"): "N"}

    def test_a_pin_on_the_month_under_way_is_fine(self, mona, er, solver, monkeypatch):
        solved(mona, er, solver)
        monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 6, 20))

        assert month_of(mona, er)["draft"]["editor"]["refusal"] is None
        assert save(mona, er, pin("Amal", 25, "N")).status_code == 200

    def test_an_old_pin_does_not_hold_up_the_hours_of_a_shift(self, mona, er, solver, client_of, monkeypatch):
        solved(mona, er, solver, finished("D"))
        saved(mona, er, pin("Amal", 3, "N"))
        Roster.objects.update(grid=roster_of(NURSES, "D"))
        monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 7, 1))

        assert change_shift(cno_of(client_of), "N", hours=10).status_code == 200


class TestSavingWhileASolveRuns:
    @pytest.mark.parametrize("state", ["queued", "running"])
    def test_it_is_allowed_kept_on_top_of_what_that_solve_answers_and_warned_of(self, mona, er, solver, state):
        solved(mona, er, solver, finished("D"))
        solver.script([finished("D", cost=9)])
        solve = started(mona, er)
        if state == "running":
            Solve.objects.filter(pk=solve["id"]).update(state="running")

        answer = saved(mona, er, pin("Amal", 3, "N"))

        assert answer["active_solve"]["id"] == solve["id"]
        assert "pinned" not in input_row(Solve.objects.get(pk=solve["id"]).input, "Amal")
        running.run(solve["id"])
        draft = month_of(mona, er)["draft"]
        assert str(draft["solve_id"]) == solve["id"]
        assert cell(draft, "Amal", 3)["shift"] == "N"
        assert pins_of(draft) == {("Amal", "2026-06-03"): "N"}
        assert [(c["hospital_id"], c["what"]) for c in draft["changes"]] == [("Amal", "pins")]
        assert draft["edited"] is True and draft["solved_cost"] == 9


def leave_body(nurse, first, last=None) -> dict:
    return {"nurse_id": str(profile(nurse).pk), "leave_type_id": str(LeaveType.objects.get(name="Sick").pk),
            "first": f"{MONTH}-{first:02d}", "last": f"{MONTH}-{(last or first):02d}"}


def record_leave(client, unit, nurse, first, last=None):
    return post(client, url(unit, "/leave"), leave_body(nurse, first, last))


class TestLeaveOnAPinnedDay:
    def test_it_ends_the_pin_and_the_answer_says_so(self, mona, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"), off("Amal", 4), pin("Amal", 9, "N"))

        response = record_leave(mona, er, "Amal", 3, 5)

        assert response.status_code == 201, response.content
        assert response.json()["pins_removed"] == [{"date": "2026-06-03", "shift": "N", "origin": "manager", "roster": "draft"},
                                                   {"date": "2026-06-04", "shift": "", "origin": "manager", "roster": "draft"}]
        draft = month_of(mona, er)["draft"]
        assert pins_of(draft) == {("Amal", "2026-06-09"): "N"}
        ended = PinnedCell.objects.exclude(removed_at=None).get(date="2026-06-03")
        assert ended.removed_why == PinEnded.LEAVE and ended.removed_by.username == "mona"

    def test_the_next_solve_is_given_leave_and_no_pin(self, mona, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))
        record_leave(mona, er, "Amal", 3)

        row = input_row(assemble(er, dt.date(2026, 6, 1)).input, "Amal")

        assert "pinned" not in row and row["leave"] == ["2026-06-03"]
        solve_input_is_fine(er)

    def test_correcting_leave_onto_a_pinned_day_does_the_same(self, mona, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 6, "N"))
        leave = record_leave(mona, er, "Amal", 3).json()

        response = post(mona, url(er, "/leave/change"), {
            "leave_ids": leave["leave_ids"], "leave_type_id": leave["leave_type"]["id"], "first": "2026-06-03", "last": "2026-06-06"})

        assert response.status_code == 200, response.content
        assert response.json()["pins_removed"] == [{"date": "2026-06-06", "shift": "N", "origin": "manager", "roster": "draft"}]
        assert month_of(mona, er)["draft"]["pins"] == []

    def test_with_no_pin_there_the_answer_says_nothing(self, mona, er, solver):
        solved(mona, er, solver)

        assert record_leave(mona, er, "Amal", 3).json()["pins_removed"] == []

    def test_a_manager_who_did_not_pin_it_is_told_and_the_page_keeps_saying_so(self, mona, er, solver, client_of):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))
        type(er).objects.filter(pk=er.pk).update(manager=None)
        omar = client_of("omar", Role.UNIT_MANAGER, full_name="Omar Said")
        type(er).objects.filter(pk=er.pk).update(manager=User.objects.get(username="omar"))

        response = record_leave(omar, er, "Amal", 3)

        assert response.json()["pins_removed"] == [{"date": "2026-06-03", "shift": "N", "origin": "manager", "roster": "draft"}]
        lost = month_of(omar, er)["draft"]["pins_lost_to_leave"]
        assert [(row["hospital_id"], row["date"], row["shift"], row["by"]) for row in lost] == [("Amal", "2026-06-03", "N", "Omar Said")]

    def test_restoring_an_older_result_does_not_bring_back_a_notice_a_newer_solve_ended(self, mona, er, solver):
        older = solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))
        record_leave(mona, er, "Amal", 3)
        solved(mona, er, solver)

        assert post(mona, url(er, f"/solve-history/{older}/restore"), {}).status_code == 200

        assert month_of(mona, er)["draft"]["pins_lost_to_leave"] == []

    def test_a_newer_solve_ends_that_notice(self, mona, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))
        record_leave(mona, er, "Amal", 3)
        assert len(month_of(mona, er)["draft"]["pins_lost_to_leave"]) == 1

        solved(mona, er, solver)

        assert month_of(mona, er)["draft"]["pins_lost_to_leave"] == []

    def test_a_solve_that_failed_after_it_does_not_end_that_notice(self, mona, er, solver):
        """The failed solve kept no roster, so the draft still holds the shift the pin promised on the leave day."""
        from ops.solver_client import SolverUnreachable
        from solves.models import SolveState
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))
        record_leave(mona, er, "Amal", 3)

        solver.script(SolverUnreachable("down"), SolverUnreachable("down"))
        failed = started(mona, er, MONTH)
        running.run(failed["id"])
        assert Solve.objects.get(pk=failed["id"]).state == SolveState.FAILED

        assert [row["date"] for row in month_of(mona, er)["draft"]["pins_lost_to_leave"]] == ["2026-06-03"]

    def test_a_solve_that_found_no_roster_after_it_does_not_end_that_notice(self, mona, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))
        record_leave(mona, er, "Amal", 3)

        solved(mona, er, solver, job("finished", result=result_of(None, ok=False, cost=None)))

        assert [row["date"] for row in month_of(mona, er)["draft"]["pins_lost_to_leave"]] == ["2026-06-03"]

    def test_removing_that_leave_does_not_bring_the_pin_back(self, mona, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))
        leave = record_leave(mona, er, "Amal", 3).json()

        post(mona, url(er, "/leave/remove"), {"leave_ids": leave["leave_ids"]})

        assert month_of(mona, er)["draft"]["pins"] == []

    def test_a_pin_another_unit_left_behind_ends_and_this_manager_is_not_told_of_it(self, mona, er, solver, make_user, client_of):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))
        omar = client_of("omar", Role.UNIT_MANAGER)
        other = icu(User.objects.get(username="omar"))
        cno_of(client_of).post(f"/api/nurses/{profile('Amal').pk}/transfer", {"unit_id": str(other.pk), "from_month": MONTH},
                               content_type="application/json")

        response = record_leave(omar, other, "Amal", 3)

        assert response.status_code == 201, response.content
        assert response.json()["pins_removed"] == []
        assert not PinnedCell.objects.current().exists()

    def test_a_row_holding_that_pair_all_the_same_is_dropped_and_is_no_failed_solve(self, mona, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))
        leave_type = LeaveType.objects.get(name="Sick")
        profile("Amal").leave.create(date="2026-06-03", leave_type=leave_type, recorded_by=User.objects.get(username="mona"))

        assert asides(month_of(mona, er)["draft"]) == {("Amal", "2026-06-03"): "leave"}
        solved_input = assemble(er, dt.date(2026, 6, 1)).input
        load_request(solved_input)
        assert "pinned" not in input_row(solved_input, "Amal")

    def test_clearing_a_shift_left_on_a_day_that_became_leave_takes_it_off(self, mona, er, solver):
        solved(mona, er, solver, finished("D"))
        record_leave(mona, er, "Amal", 3)
        assert any(v["rule"] == "unavailable" for v in check(mona, er)["violations"])

        AuditEntry.objects.all().delete()
        draft = saved(mona, er, clear("Amal", 3))["draft"]

        assert cell(draft, "Amal", 3)["shift"] == ""
        assert not any(v["rule"] == "unavailable" for v in draft["check"]["violations"])
        assert [(e.action, e.after) for e in AuditEntry.objects.all()] == [("roster.draft_edited", {"pins": 0, "cells": 1})]


class TestRestoring:
    def restore(self, client, unit, solve_id):
        return post(client, url(unit, f"/solve-history/{solve_id}/restore"), {})

    def test_an_older_result_comes_back_with_the_pins_on_top_and_warns(self, mona, er, solver):
        older = solved(mona, er, solver, finished("D", cost=7))
        solved(mona, er, solver, finished("N", cost=9))
        saved(mona, er, off("Amal", 3))

        response = self.restore(mona, er, older)

        assert response.status_code == 200, response.content
        draft = month_of(mona, er)["draft"]
        assert cell(draft, "Amal", 3)["shift"] == ""
        assert cell(draft, "Amal", 4)["shift"] == "D"
        assert pins_of(draft) == {("Amal", "2026-06-03"): ""}
        assert [(c["hospital_id"], c["what"]) for c in draft["changes"]] == [("Amal", "pins")]
        assert draft["edited"] is True and draft["solved_cost"] == 7

    def test_a_restore_ending_nothing_does_not_touch_a_pin(self, mona, er, solver):
        older = solved(mona, er, solver)
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))

        self.restore(mona, er, older)

        assert PinnedCell.objects.current().count() == 1

    def test_the_drafts_own_solve_is_refused_while_only_pins_differ_and_says_so(self, mona, er, solver):
        own = solved(mona, er, solver)
        saved(mona, er, off("Amal", 3))

        response = self.restore(mona, er, own)

        assert response.status_code == 409 and "pin" in response.json()["detail"]
        assert mona.get(url(er, "/solve-history")).json()["solves"][0]["in_draft"] is True

    def test_the_drafts_own_solve_can_come_back_when_something_else_differs(self, mona, er, solver):
        own = solved(mona, er, solver, finished("D"))
        leave = record_leave(mona, er, "Amal", 3).json()
        saved(mona, er, clear("Amal", 3))
        post(mona, url(er, "/leave/remove"), {"leave_ids": leave["leave_ids"]})
        assert grid(er)["Amal"]["2026-06-03"] == ""

        assert self.restore(mona, er, own).status_code == 200

        assert grid(er)["Amal"]["2026-06-03"] == "D"

    def test_the_unedited_drafts_own_solve_is_still_refused(self, mona, er, solver):
        own = solved(mona, er, solver)

        assert self.restore(mona, er, own).status_code == 409


class TestLastSolve:
    def test_a_failure_before_a_hand_edit_is_still_news(self, mona, er, solver):
        solved(mona, er, solver)
        solver.script([RuntimeError("the solver went away"), RuntimeError("the solver went away")])
        failed = started(mona, er)
        running.run(failed["id"])
        assert month_of(mona, er)["last_solve"]["id"] == failed["id"]

        saved(mona, er, pin("Amal", 3, "N"))

        assert month_of(mona, er)["last_solve"]["id"] == failed["id"]


class TestARequestsPin:
    def row(self, er, origin) -> PinnedCell:
        return PinnedCell.objects.create(unit=er, nurse=profile("Amal"), date="2026-06-03", shift=None, origin=origin,
                                         pinned_by=User.objects.get(username="mona"))

    @pytest.mark.parametrize("edit", [off("Amal", 3), pin("Amal", 3, "N"), clear("Amal", 3)])
    def test_a_manager_cannot_change_one_from_here(self, mona, er, solver, edit):
        solved(mona, er, solver)
        self.row(er, PinOrigin.REQUEST)

        response = save(mona, er, edit)

        assert response.status_code == 422 and "accepted request" in response.json()["detail"][0]["msg"]

    def test_the_pin_is_said_before_anything_else_that_stops_the_edit(self, mona, er, solver, make_user):
        """A shift she may not work, a date since left outside her dates, a nurse with no row on the draft: the manager is
        still sent to the request, where that cell is changed."""
        solved(mona, er, solver)
        PinnedCell.objects.create(unit=er, nurse=profile("Emad"), date="2026-06-03", shift=None, origin=PinOrigin.REQUEST,
                                  pinned_by=User.objects.get(username="mona"))
        joined_since = nurse_in(er, make_user, "Faris")
        PinnedCell.objects.create(unit=er, nurse=joined_since, date="2026-06-03", shift=None, origin=PinOrigin.REQUEST,
                                  pinned_by=User.objects.get(username="mona"))
        words = "This pin comes from an accepted request, on 3 June 2026, so change it there."

        assert save(mona, er, pin("Emad", 3, "N")).json()["detail"][0]["msg"] == words
        assert save(mona, er, off("Faris", 3)).json()["detail"][0]["msg"] == words
        NurseProfile.objects.filter(user__username="Emad").update(end_date=dt.date(2026, 6, 2))
        assert save(mona, er, off("Emad", 3)).json()["detail"][0]["msg"] == words
        assert save(mona, er, clear("Emad", 3)).json()["detail"][0]["msg"] == words, "set aside, it still holds its cell"

    def test_it_reaches_a_solve_like_any_other(self, mona, er, solver):
        solved(mona, er, solver)
        self.row(er, PinOrigin.REQUEST)

        assert input_row(assemble(er, dt.date(2026, 6, 1)).input, "Amal")["pinned"] == {"2026-06-03": ""}
        assert month_of(mona, er)["draft"]["pins"][0]["origin"] == "request"


@pytest.mark.parametrize("origin", [PinOrigin.SWAP, PinOrigin.COVER])
class TestASwapsOrACoversPin:
    """Amal works Day on the 3rd in the solve, and the pin holds her off, on the draft as its origin would have left it."""

    def row(self, er, origin) -> PinnedCell:
        roster = Roster.objects.get(unit=er)
        roster.grid["Amal"]["2026-06-03"] = ""
        roster.save(update_fields=["grid"])
        return PinnedCell.objects.create(unit=er, nurse=profile("Amal"), date="2026-06-03", shift=None, origin=origin,
                                         pinned_by=User.objects.get(username="mona"))

    def pins(self) -> list[tuple[str, str, str]]:
        return [(row.origin, row.code, row.removed_why)
                for row in PinnedCell.objects.filter(nurse__user__username="Amal", date="2026-06-03").order_by("pinned_at")]

    def test_a_managers_edit_replaces_it(self, mona, er, solver, origin):
        solved(mona, er, solver)
        self.row(er, origin)

        saved(mona, er, pin("Amal", 3, "N"))

        assert self.pins() == [(origin, "", "replaced"), (PinOrigin.MANAGER, "N", "")]
        assert Roster.objects.get(unit=er).grid["Amal"]["2026-06-03"] == "N"

    def test_a_clear_ends_it_and_gives_the_cell_back_to_its_solve(self, mona, er, solver, origin):
        solved(mona, er, solver)
        self.row(er, origin)

        saved(mona, er, clear("Amal", 3))

        assert self.pins() == [(origin, "", "cleared")]
        assert Roster.objects.get(unit=er).grid["Amal"]["2026-06-03"] == "D"

    def test_an_edit_asking_what_it_holds_already_changes_nothing(self, mona, er, solver, origin):
        solved(mona, er, solver)
        self.row(er, origin)

        saved(mona, er, off("Amal", 3))

        assert self.pins() == [(origin, "", "")]


class TestTheAuditLog:
    def words(self) -> str:
        return " ".join(json.dumps([entry.action, entry.target, entry.before, entry.after]) for entry in AuditEntry.objects.all())

    def test_a_saved_pin_says_which_month_and_how_many_and_nothing_a_nurse_could_be_read_from(self, mona, er, solver):
        solved(mona, er, solver, finished("D"))
        AuditEntry.objects.all().delete()

        saved(mona, er, pin("Amal", 3, "N"), off("Badr", 4))
        saved(mona, er, clear("Amal", 3))

        entries = list(AuditEntry.objects.order_by("id"))
        assert [(e.action, e.target, e.after) for e in entries] == [
            ("roster.draft_edited", "ER, 2026-06", {"pins": 2, "cells": 2}), ("roster.draft_edited", "ER, 2026-06", {"pins": 1, "cells": 1})]
        words = self.words()
        for found in ("Amal", "Badr", "2026-06-03", "2026-06-04", '"N"', '"D"', "shift"):
            assert found not in words

    def test_a_save_changing_nothing_and_a_check_are_not_logged(self, mona, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))
        AuditEntry.objects.all().delete()

        saved(mona, er, pin("Amal", 3, "N"))
        check(mona, er, off("Amal", 4))

        assert not AuditEntry.objects.exists()

    def test_leave_ending_a_pin_adds_nothing_to_leaves_own_entry(self, mona, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "N"))
        AuditEntry.objects.all().delete()

        record_leave(mona, er, "Amal", 3)

        entry = AuditEntry.objects.get()
        assert (entry.action, entry.target) == ("leave.recorded", "Amal")
        assert "2026-06" not in self.words() and "pin" not in self.words()
