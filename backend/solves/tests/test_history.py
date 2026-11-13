"""The solve history, a result's details and restoring one, through the API with the scripted solver and the real job. The
explanation is the solver's own, worked out here on what is stored."""
import datetime as dt
import json
import re

import pytest

from accounts.models import Role, User
from audit.models import AuditEntry
from conftest import post
from hospital.models import LeaveType, Shift, ShiftRename
from nurses.models import Leave, NurseProfile
from nurses.tests.conftest import make_unit
from ops.solver_client import SolverRefused, SolverUnreachable
from rosters.models import Roster
from solves import running
from solves.models import Solve
from solves.tests.conftest import MONTH, NURSES, job, nurse_in, result_of, roster_of, started

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _scene(before_june, solver):
    pass


def url(unit, rest=""):
    return f"/api/units/{unit.pk}{rest}"


def history(client, unit, **filters):
    response = client.get(url(unit, "/solve-history"), filters)
    assert response.status_code == 200, response.content
    return response.json()


def details(client, unit, solve_id):
    response = client.get(url(unit, f"/solve-history/{solve_id}"))
    assert response.status_code == 200, response.content
    return response.json()


def restore(client, unit, solve_id):
    return post(client, url(unit, f"/solve-history/{solve_id}/restore"), {})


def month_of(client, unit, month=MONTH):
    response = client.get(url(unit, "/roster"), {"month": month})
    assert response.status_code == 200, response.content
    return response.json()


def accounting(nurses=NURSES) -> dict:
    return {nurse: {"leave": [], "contract": 192, "leave_credit": 24, "required": 168, "hours_worked": 180, "overtime": 12,
                    "shortfall": 0, "shifts": {"D": 15, "N": 0}, "cost": 3, "carry_over": {"hours_balance": 0}}
            for nurse in nurses}


def finished(shift="D", *, cost=7, **options):
    result = result_of(roster_of(NURSES, shift), cost=cost, **options)
    result["nurses"] = accounting()
    return job("finished", result=result, best_cost=cost, improvements=3)


def solved(client, unit, solver, answer=None, month=MONTH) -> str:
    """A solve to its end, its id."""
    solver.script([answer or finished()])
    solve = started(client, unit, month)
    running.run(solve["id"])
    return solve["id"]


def sick_day(nurse: str, day: int) -> Leave:
    profile = NurseProfile.objects.get(user__username=nurse)
    return Leave.objects.create(nurse=profile, date=dt.date(2026, 6, day), leave_type=LeaveType.objects.get(name="Sick"),
                                recorded_by=profile.user)


class TestTheUnitsHistory:
    def test_it_lists_every_solve_newest_first_with_who_asked_how_long_and_the_cost(self, mona, er, solver):
        first = solved(mona, er, solver, finished(cost=7))
        second = solved(mona, er, solver, job("finished", result=result_of(None, ok=False, cost=None,
                                                                           message="no roster found within the time limit")))

        rows = history(mona, er)["solves"]

        assert [row["id"] for row in rows] == [second, first]
        assert rows[1]["state"] == "succeeded" and rows[1]["requested_by"] == "Mona Ali" and rows[1]["elapsed_seconds"] == 1.5
        assert rows[1]["cost"] == 7 and rows[1]["found_roster"] is True and rows[1]["in_draft"] is True
        assert rows[0]["cost"] is None and rows[0]["found_roster"] is False and rows[0]["in_draft"] is False

    def test_a_failure_is_listed_with_its_kind_and_no_result(self, mona, er, solver):
        solver.script([SolverRefused("Amal: on leave but pinned")])
        solve = started(mona, er)
        running.run(solve["id"])

        row = history(mona, er)["solves"][0]

        assert row["state"] == "failed" and row["failure"] == "refused" and row["found_roster"] is False

    def test_a_cost_seen_during_a_search_that_kept_no_roster_is_not_its_cost(self, mona, er, solver):
        solver.script([job("running", best_cost=40), RuntimeError("down")], RuntimeError("down"))
        failed = started(mona, er)
        running.run(failed["id"])
        assert Solve.objects.get(pk=failed["id"]).best_cost == 40
        solver.script([job("running", best_cost=30), job("cancelled", result=None)])
        stopped = started(mona, er)
        running.run(stopped["id"])

        assert [row["cost"] for row in history(mona, er)["solves"]] == [None, None]

    def test_a_month_narrows_it(self, mona, er, solver):
        june = solved(mona, er, solver)
        solved(mona, er, solver, month="2026-07")

        assert [row["id"] for row in history(mona, er, month=MONTH)["solves"]] == [june]

    def test_it_comes_a_page_at_a_time(self, mona, er, solver):
        ids = [solved(mona, er, solver) for _ in range(3)]

        first = history(mona, er, limit=2)
        rest = history(mona, er, limit=2, older_than=first["older_than"])

        assert [row["id"] for row in first["solves"]] == [ids[2], ids[1]]
        assert [row["id"] for row in rest["solves"]] == [ids[0]] and rest["older_than"] is None

    def test_another_units_manager_is_refused_and_their_solve_is_not_here(self, mona, er, solver, client_of, make_user):
        solve = solved(mona, er, solver)
        other = client_of("omar", Role.UNIT_MANAGER)
        icu = make_unit("ICU", User.objects.get(username="omar"))

        assert other.get(url(er, "/solve-history")).status_code == 403
        assert other.get(url(er, f"/solve-history/{solve}")).status_code == 403
        assert restore(other, er, solve).status_code == 403
        assert other.get(url(icu, f"/solve-history/{solve}")).status_code == 404
        assert restore(other, icu, solve).status_code == 404

    @pytest.mark.parametrize("role", [Role.STAFF_NURSE, Role.SYSTEM_ADMIN])
    def test_no_other_role_reads_a_units_results(self, role, mona, er, solver, client_of):
        solve = solved(mona, er, solver)
        someone = client_of("someone", role)

        assert someone.get(url(er, "/solve-history")).status_code == 403
        assert someone.get(url(er, f"/solve-history/{solve}")).status_code == 403
        assert restore(someone, er, solve).status_code == 403

    @pytest.mark.parametrize("role", [Role.CNO, Role.DEPUTY_CNO])
    def test_the_cno_and_the_deputy_read_a_submitted_months_and_see_no_draft_month(self, role, mona, er, solver, client_of):
        """A draft is the unit management's alone, so a month still in draft shows nothing of its solves to them."""
        from rosters.tests.test_review import sound
        solver.script([sound()])
        solve = started(mona, er)["id"]
        running.run(solve)
        someone = client_of("someone", role)

        assert someone.get(url(er, "/solve-history")).json()["solves"] == []
        assert someone.get(url(er, f"/solve-history/{solve}")).status_code == 404

        assert post(mona, url(er, "/roster/submit"), {"month": MONTH}).status_code == 200
        assert [row["id"] for row in someone.get(url(er, "/solve-history")).json()["solves"]] == [solve]
        result = someone.get(url(er, f"/solve-history/{solve}")).json()
        assert result["grid"] is not None
        assert result["restore_refusal"] == "Only the unit's manager can restore a result."
        assert restore(someone, er, solve).status_code == 403


class TestTheSystemAdminsHistory:
    @pytest.fixture
    def admin(self, client_of):
        return client_of("root", Role.SYSTEM_ADMIN)

    def everything(self, admin, **filters) -> dict:
        response = admin.get("/api/solve-history", filters)
        assert response.status_code == 200, response.content
        return response.json()

    def test_it_spans_the_units(self, admin, mona, er, solver, make_user, client_of):
        omar = client_of("omar", Role.UNIT_MANAGER, full_name="Omar Said")
        icu = make_unit("ICU", User.objects.get(username="omar"))
        nurse_in(icu, make_user, "Zaki")
        solved(mona, er, solver)
        solver.script([finished()])
        solve = started(omar, icu)

        rows = self.everything(admin)["solves"]

        assert [row["unit"] for row in rows] == ["ICU", "ER"]
        assert rows[0]["id"] == solve["id"] and rows[0]["requested_by"] == "Omar Said" and rows[0]["state"] == "queued"
        assert rows[1]["month"] == MONTH and rows[1]["elapsed_seconds"] == 1.5 and rows[1]["found_roster"] is True

    def test_it_comes_a_page_at_a_time(self, admin, mona, er, solver):
        ids = [solved(mona, er, solver) for _ in range(3)]

        first = self.everything(admin, limit=2)
        rest = self.everything(admin, limit=2, older_than=first["older_than"])

        assert [row["id"] for row in first["solves"] + rest["solves"]] == list(reversed(ids))

    @pytest.mark.parametrize("role", [Role.CNO, Role.DEPUTY_CNO, Role.UNIT_MANAGER, Role.STAFF_NURSE])
    def test_it_is_only_the_system_admins(self, role, client_of):
        assert client_of("someone", role).get("/api/solve-history").status_code == 403

    def test_it_holds_no_roster_content_whatever_the_solve_says(self, admin, mona, er, solver):
        """Every field the solver or the worker writes free text into, each naming a nurse and a date as it can for real. The
        answer as text is held, so a new field that carried any of it would fail here too."""
        sick_day("Badr", 9)
        says = "Badr: on leave on 2026-06-09 and N on day 8"
        with_text = finished(message=says)
        with_text["result"]["precheck"] = [says, "day 8: no team leader available for D"]
        with_text["result"]["violations"] = [{"rule": "pin", "nurse": "Badr", "date": "2026-06-09", "text": says}]
        with_text["result"]["elapsed_violations"] = with_text["result"]["violations"]
        solved(mona, er, solver, with_text)
        solver.script([SolverRefused(says)])
        refused = started(mona, er)
        running.run(refused["id"])
        solver.script(RuntimeError(says), RuntimeError(says))
        crashed = started(mona, er)
        running.run(crashed["id"])
        assert says in Solve.objects.get(pk=crashed["id"]).error

        answer = self.everything(admin)
        text = json.dumps(answer)

        assert [row["failure"] for row in answer["solves"]] == ["unexpected", "refused", None]
        assert not re.findall("|".join(NURSES), text)
        assert not re.findall(r"2026-06-\d\d", text)
        for leak in ("leave", "precheck", "message", "error", "roster", "violations", "nurses", "input", "result"):
            assert not any(leak in key for row in answer["solves"] for key in row if key != "found_roster")
        assert set(answer["solves"][0]) == {
            "id", "unit", "month", "state", "failure", "requested_by", "requested_at", "finished_at", "elapsed_seconds",
            "time_limit_seconds", "attempts", "cost", "found_roster"}


class TestAResult:
    def test_it_holds_each_nurses_hours_and_cost_by_name(self, mona, er, solver):
        solve = solved(mona, er, solver)

        answer = details(mona, er, solve)

        assert [row["hospital_id"] for row in answer["nurses"]] == NURSES
        assert answer["nurses"][0] == {"hospital_id": "Amal", "full_name": "Amal", "contract": 192, "leave_credit": 24,
                                       "required": 168, "hours_worked": 180, "overtime": 12, "shortfall": 0,
                                       "shifts": {"D": 15, "N": 0}, "cost": 3}
        assert answer["unit_cost"] == 0
        assert answer["solve"]["id"] == solve and answer["solve"]["in_draft"] is True

    def test_each_costly_rule_has_its_penalty_and_the_solvers_largest_occurrences(self, mona, er, solver):
        """Everyone on days all month breaks plenty, so the solver's own code finds these in what is stored."""
        answer = details(mona, er, solved(mona, er, solver))

        rules = answer["rules"]
        assert answer["explained"] is True
        assert [(row["rule"], row["penalty"]) for row in rules] == [("fair_day", 5), ("night_rest", 2)]
        fair = rules[0]
        assert fair["weight"] > 0 and fair["amount"] > 0 and fair["counted_in"]
        assert 1 <= len(fair["largest"]) <= 5
        amounts = [match["amount"] for match in fair["largest"]]
        assert amounts == sorted(amounts, reverse=True)
        assert fair["largest"][0]["hospital_id"] in NURSES and fair["largest"][0]["full_name"] in NURSES

    def test_more_occurrences_than_are_shown_are_counted(self, mona, er, solver, monkeypatch):
        monkeypatch.setattr("solves.history_api.LARGEST", 2)

        fair = details(mona, er, solved(mona, er, solver))["rules"][0]

        assert len(fair["largest"]) == 2 and fair["others"] == 1  # Three nurses stray from their share here.

    def test_a_stored_input_the_solvers_code_no_longer_reads_loses_only_the_explanation(self, mona, er, solver):
        solve = solved(mona, er, solver)
        stored = Solve.objects.get(pk=solve)
        stored.input["nurses"][0]["gender"] = "neither"
        stored.save()

        answer = details(mona, er, solve)

        assert answer["explained"] is False
        assert [(row["rule"], row["penalty"], row["weight"], row["largest"]) for row in answer["rules"]] == [
            ("fair_day", 5, None, []), ("night_rest", 2, None, [])]
        assert len(answer["nurses"]) == 4

    def test_days_in_the_solvers_words_are_dates(self, mona, er, solver):
        answer = finished()
        answer["result"]["precheck"] = ["day 0: D needs 2 but only 1 eligible nurses are not on leave", "Amal: history covers 3 day(s)"]
        answer["result"]["elapsed_violations"] = [{"rule": "consecutive", "nurse": "Amal", "date": "2026-06-05",
                                                   "text": "consecutive: Amal 6 shifts in a row at day 4"},
                                                  {"rule": "shift_switch", "nurse": "Amal", "date": None,
                                                   "text": "switches: Amal changes night/day 6 times, max 2"},
                                                  {"rule": "headcount", "nurse": None, "date": None, "text": "at day -1"}]

        result = details(mona, er, solved(mona, er, solver, answer))

        assert result["precheck"] == ["2026-06-01: D needs 2 but only 1 eligible nurses are not on leave",
                                      "Amal: history covers 3 day(s)"]
        assert result["elapsed_violations"] == [
            {"rule": "consecutive", "hospital_id": "Amal", "full_name": "Amal", "date": "2026-06-05",
             "text": "consecutive: Amal 6 shifts in a row at 2026-06-05"},
            {"rule": "shift_switch", "hospital_id": "Amal", "full_name": "Amal", "date": None,
             "text": "switches: Amal changes night/day 6 times, max 2"},
            {"rule": "headcount", "hospital_id": None, "full_name": "", "date": None, "text": "at 2026-05-31"}]

    def test_a_loaders_refusal_has_dates_too(self, mona, er, solver):
        solver.script([SolverRefused("nurses['Amal'].pinned: day 3 is given twice")])
        solve = started(mona, er)
        running.run(solve["id"])

        assert details(mona, er, solve["id"])["error"] == "nurses['Amal'].pinned: 2026-06-04 is given twice"

    def test_the_solvers_statistics_and_what_stopped_it(self, mona, er, solver):
        answer = finished(stopped_by="time_limit")
        answer["result"]["solver"] |= {"objective": 7.0, "best_bound": 3.0, "solve_seconds": 20.1, "build_seconds": 0.07,
                                       "deterministic_time": 11.3, "workers": 8, "time_limit_seconds": 20,
                                       "model": {"variables": 5169, "constraints": 10218, "objective_terms": 300}}

        statistics = details(mona, er, solved(mona, er, solver, answer))["statistics"]

        assert statistics == {"status": "OPTIMAL", "stopped_by": "time_limit", "objective": 7.0, "best_bound": 3.0,
                              "solve_seconds": 20.1, "build_seconds": 0.07, "deterministic_time": 11.3, "improvements": 3,
                              "workers": 8, "time_limit_seconds": 20.0, "variables": 5169, "constraints": 10218,
                              "objective_terms": 300}

    def test_its_grid_says_leave_as_it_was_and_shifts_under_the_codes_in_use(self, mona, er, solver):
        sick_day("Badr", 9)
        answer = finished()
        answer["result"]["roster"]["Badr"]["2026-06-09"] = ""
        solve = solved(mona, er, solver, answer)
        shift = Shift.objects.get(code="D")
        ShiftRename.objects.create(shift=shift, old_code="D", new_code="DD", by=User.objects.get(username="mona"))
        Shift.objects.filter(pk=shift.pk).update(code="DD")

        result = details(mona, er, solve)

        grid = result["grid"]
        badr = next(row for row in grid["rows"] if row["hospital_id"] == "Badr")
        assert badr["cells"][8] == {"shift": "", "kind": "leave"} and badr["cells"][0] == {"shift": "DD", "kind": "shift"}
        assert [row["code"] for row in grid["shifts"]] == ["DD"]
        assert result["nurses"][0]["shifts"] == {"DD": 15, "N": 0}
        assert Solve.objects.get(pk=solve).result["roster"]["Amal"]["2026-06-01"] == "D"

    def test_a_shift_shows_the_hours_it_had_at_that_solve(self, mona, er, solver):
        solve = solved(mona, er, solver)
        Shift.objects.filter(code="D").update(hours=8)

        assert details(mona, er, solve)["grid"]["shifts"][0]["hours"] == 12

    def test_a_failed_solve_says_why_and_holds_nothing_else(self, mona, er, solver):
        solver.script(SolverUnreachable("the solver fell over"), SolverUnreachable("the solver fell over"))
        solve = started(mona, er)
        running.run(solve["id"])

        answer = details(mona, er, solve["id"])

        assert answer["error"] == "the solver fell over" and answer["solve"]["failure"] == "unreachable"
        assert answer["grid"] is None and answer["rules"] == [] and answer["nurses"] == [] and answer["statistics"] is None
        assert answer["restore_refusal"] == "This solve left no roster to restore."

    def test_a_search_that_found_nothing_keeps_its_message_and_precheck(self, mona, er, solver):
        answer = job("finished", result=result_of(None, ok=False, cost=None, message="no roster satisfies the hard rules"))
        answer["result"]["precheck"] = ["capacity: too few"]
        answer["result"]["penalties"] = {}

        result = details(mona, er, solved(mona, er, solver, answer))

        assert result["message"] == "no roster satisfies the hard rules" and result["precheck"] == ["capacity: too few"]
        assert result["grid"] is None and result["explained"] is True

    def test_a_roster_that_breaks_a_hard_rule_is_shown_and_cannot_be_restored(self, mona, er, solver):
        answer = finished(ok=False, message="the roster breaks 1 hard rule(s)")
        answer["result"]["violations"] = [{"rule": "headcount", "nurse": None, "date": "2026-06-02", "text": "headcount: day 1 D = 4, want 1..2"}]

        result = details(mona, er, solved(mona, er, solver, answer))

        assert result["violations"][0]["text"] == "headcount: 2026-06-02 D = 4, want 1..2"
        assert result["grid"] is not None and result["restore_refusal"] == "This solve left no roster to restore."

    def test_a_solve_under_way_can_be_opened(self, mona, er):
        solve = started(mona, er)

        answer = details(mona, er, solve["id"])

        assert answer["solve"]["state"] == "queued" and answer["grid"] is None
        assert answer["restore_refusal"] == "This solve has not ended yet."


class TestRestoring:
    @pytest.fixture
    def two_solves(self, mona, er, solver):
        return solved(mona, er, solver, finished("D", cost=7)), solved(mona, er, solver, finished("N", cost=9))

    def test_it_puts_that_result_back_as_the_draft(self, mona, er, solver, two_solves):
        first, second = two_solves
        assert month_of(mona, er)["draft"]["solve_id"] == second

        response = restore(mona, er, first)

        assert response.status_code == 200, response.content
        assert response.json()["solve"]["in_draft"] is True and response.json()["restore_refusal"] == "This result is the draft already."
        answer = month_of(mona, er)
        draft = answer["draft"]
        assert draft["solve_id"] == first and draft["cost"] == 7 and draft["rows"][0]["cells"][0]["shift"] == "D"
        assert draft["restored_at"] is not None and draft["restored_at"].endswith("+03:00")
        assert draft["solved_at"] == details(mona, er, first)["solve"]["finished_at"]
        assert [row["in_draft"] for row in history(mona, er)["solves"]] == [False, True]

    def test_a_result_stored_on_a_version_1_input_is_restored_and_checked_on_the_month_as_it_stands(self, mona, er, two_solves):
        """An input from before skills is still read, and the draft it makes is warned of skills since."""
        from hospital.models import Skill
        first, _ = two_solves
        stored = Solve.objects.get(pk=first)
        stored.input["version"] = 1
        for key in ("skill_needs", "skill_needs_hard"):
            del stored.input["department"][key]
        del stored.input["general"]["required_skills"], stored.input["general"]["weights"]["skill_shortfall"]
        stored.save()
        amal = NurseProfile.objects.get(user__username="Amal")
        amal.skills.create(skill=Skill.objects.create(name="ICU", expires=False, required_to_work=False), recorded_by=amal.user)

        assert restore(mona, er, first).status_code == 200
        draft = month_of(mona, er)["draft"]
        assert draft["solve_id"] == first and {"hospital_id": "Amal", "full_name": "Amal", "what": "skills"} in draft["changes"]
        assert details(mona, er, first)["explained"] is True

    def test_the_newer_solve_it_replaced_does_not_read_as_having_found_nothing(self, mona, er, two_solves):
        restore(mona, er, two_solves[0])

        assert month_of(mona, er)["last_solve"] is None

    def test_a_failure_before_the_restore_is_no_longer_news_and_one_after_it_is(self, mona, er, solver, two_solves):
        def fail():
            solver.script(RuntimeError("down"), RuntimeError("down"))
            solve = started(mona, er)
            running.run(solve["id"])
            return solve["id"]

        fail()
        assert month_of(mona, er)["last_solve"]["state"] == "failed"
        restore(mona, er, two_solves[0])
        assert month_of(mona, er)["last_solve"] is None
        later = fail()
        assert month_of(mona, er)["last_solve"]["id"] == later

    def test_a_solve_that_follows_replaces_it_and_clears_the_mark(self, mona, er, solver, two_solves):
        restore(mona, er, two_solves[0])

        third = solved(mona, er, solver)

        draft = month_of(mona, er)["draft"]
        assert draft["solve_id"] == third and draft["restored_at"] is None

    def test_a_solve_that_was_stopped_and_kept_a_roster_can_be_restored_and_says_so(self, mona, er, solver):
        stopped = job("cancelled", result=result_of(roster_of(NURSES), cost=20, stopped_by="cancelled"), best_cost=20)
        first = solved(mona, er, solver, stopped)
        solved(mona, er, solver)

        assert restore(mona, er, first).status_code == 200
        assert month_of(mona, er)["draft"]["stopped_early"] is True

    @pytest.mark.parametrize("answer", ["failed", "nothing", "broken", "stopped_empty"])
    def test_a_result_with_no_roster_to_keep_is_refused_and_the_draft_stays(self, answer, mona, er, solver, two_solves):
        if answer == "failed":
            solver.script(RuntimeError("down"), RuntimeError("down"))
            solve = started(mona, er)["id"]
            running.run(solve)
        else:
            solve = solved(mona, er, solver, {
                "nothing": job("finished", result=result_of(None, ok=False, cost=None)),
                "broken": finished(ok=False),
                "stopped_empty": job("cancelled", result=None),
            }[answer])

        response = restore(mona, er, solve)

        assert response.status_code == 409 and response.json()["detail"] == "This solve left no roster to restore."
        assert month_of(mona, er)["draft"]["solve_id"] == two_solves[1]
        assert not AuditEntry.objects.filter(action="roster.draft_restored").exists()

    def test_it_is_refused_while_a_solve_of_that_month_waits_or_runs(self, mona, er, solver, two_solves):
        waiting = started(mona, er)

        queued = restore(mona, er, two_solves[0])
        Solve.objects.filter(pk=waiting["id"]).update(state="running")
        running_now = restore(mona, er, two_solves[0])

        assert queued.status_code == 409 and "queued or running" in queued.json()["detail"]
        assert running_now.status_code == 409
        assert details(mona, er, two_solves[0])["restore_refusal"] == queued.json()["detail"]

    def test_a_solve_of_another_month_does_not_stop_it(self, mona, er, solver, two_solves):
        started(mona, er, month="2026-07")

        assert restore(mona, er, two_solves[0]).status_code == 200

    def test_a_month_that_is_over_is_refused_and_can_still_be_read(self, mona, er, solver, two_solves, monkeypatch):
        monkeypatch.setattr("nurses.membership.today", lambda: dt.date(2026, 7, 1))

        response = restore(mona, er, two_solves[0])

        assert response.status_code == 409 and response.json()["detail"] == "This month is over, so its draft cannot change."
        assert details(mona, er, two_solves[0])["grid"] is not None

    def test_the_month_under_way_can_be_restored_as_it_can_be_solved(self, mona, er, solver, two_solves, monkeypatch):
        monkeypatch.setattr("nurses.membership.today", lambda: dt.date(2026, 6, 20))

        assert restore(mona, er, two_solves[0]).status_code == 200

    def test_the_draft_itself_is_refused(self, mona, er, two_solves):
        response = restore(mona, er, two_solves[1])

        assert response.status_code == 409 and response.json()["detail"] == "This result is the draft already."

    def test_a_rename_is_carried_into_the_draft_and_the_stored_result_stays(self, mona, er, two_solves):
        shift = Shift.objects.get(code="D")
        ShiftRename.objects.create(shift=shift, old_code="D", new_code="DD", by=User.objects.get(username="mona"))
        Shift.objects.filter(pk=shift.pk).update(code="DD")

        assert restore(mona, er, two_solves[0]).status_code == 200

        draft = month_of(mona, er)["draft"]
        assert draft["rows"][0]["cells"][0]["shift"] == "DD" and [row["code"] for row in draft["shifts"]] == ["DD"]
        assert Roster.objects.get(unit=er).grid["Amal"]["2026-06-01"] == "DD"
        assert Solve.objects.get(pk=two_solves[0]).result["roster"]["Amal"]["2026-06-01"] == "D"

    def test_a_rename_after_the_restore_reaches_the_draft_through_the_catalogue(self, mona, er, client_of, two_solves):
        restore(mona, er, two_solves[0])
        cno = client_of("carla", Role.CNO)
        shift = Shift.objects.get(code="D")

        response = cno.put(f"/api/hospital/shifts/{shift.pk}", {"code": "DD", "display_name": shift.display_name,
                                                                "start_time": f"{shift.start_time:%H:%M}",
                                                                "end_time": f"{shift.end_time:%H:%M}",
                                                                "hours": shift.hours, "colour": shift.colour, "active": True},
                           content_type="application/json")

        assert response.status_code == 200, response.content
        assert Roster.objects.get(unit=er).grid["Amal"]["2026-06-01"] == "DD"

    def test_a_shifts_hours_changed_since_are_refused_naming_it(self, mona, er, two_solves):
        Shift.objects.filter(code="D").update(hours=8)

        response = restore(mona, er, two_solves[0])

        assert response.status_code == 409
        assert response.json()["detail"].startswith("D was 12 hours at this solve and is 8 now")
        assert month_of(mona, er)["draft"]["solve_id"] == two_solves[1]

    def test_hours_changed_on_a_shift_the_result_does_not_use_do_not_matter(self, mona, er, two_solves):
        Shift.objects.filter(code="N").update(hours=8)

        assert restore(mona, er, two_solves[0]).status_code == 200

    def test_hours_are_compared_through_a_rename(self, mona, er, two_solves):
        shift = Shift.objects.get(code="D")
        ShiftRename.objects.create(shift=shift, old_code="D", new_code="DD", by=User.objects.get(username="mona"))
        Shift.objects.filter(pk=shift.pk).update(code="DD", hours=8)

        response = restore(mona, er, two_solves[0])

        assert response.status_code == 409 and response.json()["detail"].startswith("DD was 12 hours")

    def test_a_shift_retired_since_is_refused_naming_it(self, mona, er, two_solves):
        Shift.objects.filter(code="D").update(active=False)

        response = restore(mona, er, two_solves[0])

        assert response.status_code == 409 and response.json()["detail"].startswith("D has been retired")
        assert details(mona, er, two_solves[0])["grid"] is not None

    def test_the_warning_compares_with_the_restored_solves_input(self, mona, er, solver):
        """Leave recorded between the two solves: the second took it in, so only the first is out of date about it."""
        first = solved(mona, er, solver)
        sick_day("Badr", 9)
        second = solved(mona, er, solver)
        assert month_of(mona, er)["draft"]["changes"] == []

        restore(mona, er, first)

        assert month_of(mona, er)["draft"]["changes"] == [{"hospital_id": "Badr", "full_name": "Badr", "what": "leave"}]
        restore(mona, er, second)
        assert month_of(mona, er)["draft"]["changes"] == []

    def test_cells_read_as_that_solve_had_them(self, mona, er, solver):
        first = solved(mona, er, solver)
        sick_day("Badr", 9)
        solved(mona, er, solver)

        restore(mona, er, first)

        badr = next(row for row in month_of(mona, er)["draft"]["rows"] if row["hospital_id"] == "Badr")
        assert badr["cells"][8]["kind"] == "shift"

    def test_a_nurse_who_joined_since_is_warned_of(self, mona, er, solver, make_user):
        first = solved(mona, er, solver)
        nurse_in(er, make_user, "Faris")
        solved(mona, er, solver)

        restore(mona, er, first)

        assert {"hospital_id": "Faris", "full_name": "Faris", "what": "joined"} in month_of(mona, er)["draft"]["changes"]

    def test_it_is_audited_with_nothing_of_a_nurse(self, mona, er, solver, two_solves):
        sick_day("Badr", 9)

        restore(mona, er, two_solves[0])

        entry = AuditEntry.objects.get(action="roster.draft_restored")
        assert entry.actor.username == "mona" and entry.target == "ER, 2026-06"
        assert entry.before is None and entry.after == {"solve": two_solves[0], "cost": 7}
        text = json.dumps([entry.target, entry.before, entry.after])
        assert not re.findall("|".join(NURSES), text) and not re.findall(r"2026-06-\d\d", text)

    def test_a_manager_who_took_over_the_unit_can_restore_what_the_last_one_asked_for(self, mona, er, solver, client_of):
        first = solved(mona, er, solver, finished("D", cost=7))
        solved(mona, er, solver, finished("N", cost=9))
        omar = client_of("omar", Role.UNIT_MANAGER, full_name="Omar Said")
        er.manager = User.objects.get(username="omar")
        er.save()

        assert restore(omar, er, first).status_code == 200
        assert history(omar, er)["solves"][1]["requested_by"] == "Mona Ali"
        assert AuditEntry.objects.get(action="roster.draft_restored").actor.username == "omar"


class TestOvertimeShareWords:
    """The draft's explanation names each nurse's overtime above her share and its cost in the solver's words."""

    def test_overtime_above_a_share_is_explained_by_nurse_with_its_cost(self, mona, er, solver):
        """`amal_ahead_in_sound_june`'s month, counted by hand: Amal's 20 hours against a share of 4.71, 3 priced at 1."""
        from solves.tests.test_input import amal_ahead_in_sound_june
        roster = amal_ahead_in_sound_june()
        result = result_of(roster, cost=3)
        result["penalties"] = {"overtime_above_share": 3}

        answer = details(mona, er, solved(mona, er, solver, job("finished", result=result, best_cost=3)))

        [rule] = answer["rules"]
        assert (rule["rule"], rule["penalty"], rule["weight"], rule["amount"], rule["counted_in"]) == (
            "overtime_above_share", 3, 1, 3, "hour of overtime above the share")
        assert [(match["hospital_id"], match["date"], match["amount"], match["text"]) for match in rule["largest"]] == [
            ("Amal", None, 3, "Amal: 20 h overtime, share 4.7 h, 3 h priced")]


class TestSkillWords:
    """The two skill rules read like every other rule, with dates, and under a skill's name today."""

    def test_a_short_skill_need_is_explained_per_shift_day_with_dates_under_the_skills_name_now(self, mona, er, solver):
        from hospital.models import HospitalSettings, Skill, SkillRename
        icu = Skill.objects.create(name="ICU", expires=True, required_to_work=False)
        amal = NurseProfile.objects.get(user__username="Amal")
        amal.skills.create(skill=icu, expiry=dt.date(2026, 6, 2), recorded_by=amal.user)
        er.rules |= {"skill_needs": [{"shift": "D", "skill": "ICU", "weekday_min": 2, "weekend_min": 2}],
                     "skill_needs_hard": False}
        er.save()
        settings = HospitalSettings.load()
        settings.rules["weights"]["skill_shortfall"] = 3
        settings.save()
        answer = finished()
        answer["result"]["penalties"]["skill_need"] = 3 * (2 * 30 - 2)  # What the solver would report of this grid.
        solve = solved(mona, er, solver, answer)
        Skill.objects.filter(pk=icu.pk).update(name="Intensive care")
        SkillRename.objects.create(skill=icu, old_name="ICU", new_name="Intensive care", by=amal.user)

        answer = details(mona, er, solve)

        [rule] = [rule for rule in answer["rules"] if rule["rule"] == "skill_need"]
        assert (rule["weight"], rule["amount"]) == (3, 2 * 30 - 2)
        assert rule["largest"][0] == {"hospital_id": None, "full_name": "", "date": "2026-06-03", "amount": 2,
                                      "text": "skill need: 2026-06-03 D has 0 holding Intensive care, needs 2 (2 short)"}
        assert Solve.objects.get(pk=solve).input["department"]["skill_needs"][0]["skill"] == "ICU"

    def test_a_version_2_input_the_solver_refuses_is_rules_to_review(self, mona, er, solver):
        from hospital.models import Skill
        bls = Skill.objects.create(name="BLS", expires=True, required_to_work=True)
        amal = NurseProfile.objects.get(user__username="Amal")
        amal.skills.create(skill=bls, expiry=dt.date(2026, 6, 9), recorded_by=amal.user)
        solver.script([SolverRefused("Amal: day 12 is after the expiry of 'BLS', required to work, and is pinned")])
        solve = started(mona, er)
        running.run(solve["id"])

        stored = Solve.objects.get(pk=solve["id"])
        assert (stored.input["version"], stored.failure) == (2, "refused")
        assert stored.input["nurses"][0]["skills"] == {"BLS": "2026-06-09"}
        assert history(mona, er)["solves"][0]["failure"] == "refused"
        assert details(mona, er, solve["id"])["error"] == (
            "Amal: 2026-06-13 is after the expiry of 'BLS', required to work, and is pinned")
