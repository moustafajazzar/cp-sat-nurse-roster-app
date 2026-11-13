"""The whole flow through the API, with the scripted solver and the real job, called directly."""
import datetime as dt

import pytest
from django.contrib.sessions.models import Session
from django.db import connection
from django.utils import timezone
from procrastinate.contrib.django.models import ProcrastinateJob

from accounts.models import Role, User
from audit.models import AuditEntry
from conftest import post
from hospital.models import HospitalSettings, LeaveType, Shift
from nurses import membership
from nurses.models import Leave, NurseProfile, Placement
from nurses.tests.conftest import make_unit
from ops.models import TechnicalSettings
from ops.queue import requeue_stalled
from ops.solver_client import SolverRefused, SolverUnreachable
from rosters.models import Roster
from solves import running
from solves.models import Solve
from solves.tests.conftest import MONTH, NURSES, job, make_sample_unit, nurse_in, result_of, roster_of, started, start_solve

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _scene(before_june, solver):
    pass


def url(unit, rest=""):
    return f"/api/units/{unit.pk}{rest}"


def preview(client, unit, month=MONTH):
    response = client.get(url(unit, "/solves/preview"), {"month": month})
    assert response.status_code == 200, response.content
    return response.json()


def month_of(client, unit, month=MONTH):
    response = client.get(url(unit, "/roster"), {"month": month})
    assert response.status_code == 200, response.content
    return response.json()


def progress(client, unit, solve_id):
    response = client.get(url(unit, f"/solves/{solve_id}"))
    assert response.status_code == 200, response.content
    return response.json()


def finished(roster=None, **options):
    return job("finished", result=result_of(roster or roster_of(NURSES), **options), best_cost=7, improvements=3)


class TestPreview:
    def test_it_sums_up_what_the_solver_will_receive(self, mona, er, solver):
        solver.issues = ["Too few nurses for the night shift"]
        badr = NurseProfile.objects.get(user__username="Badr")
        Leave.objects.create(nurse=badr, date=dt.date(2026, 6, 9), leave_type_id=_sick(), recorded_by=badr.user)
        amal = NurseProfile.objects.get(user__username="Amal")
        amal.start_date = dt.date(2026, 6, 3)
        amal.save()

        answer = preview(mona, er)

        assert answer["summary"] == {"nurses": 4, "leave_days": 1, "outside_days": 2, "pins": 0}
        assert answer["precheck"] == ["Too few nurses for the night shift"]
        assert answer["refusal"] is None and answer["rules_refusal"] is None

    def test_review_flags_and_unreviewed_scores_warn_and_do_not_block(self, mona, er, solver):
        HospitalSettings.objects.update(rules_need_review=True)
        NurseProfile.objects.filter(user__username="Amal").update(attributes_saved=True)
        solver.script([finished()])

        warnings = preview(mona, er)["warnings"]

        assert warnings["hospital_rules_need_review"] is True and warnings["unit_rules_need_review"] is True
        assert warnings["unreviewed_scores"] == 3
        started(mona, er)

    def test_a_save_that_changes_nothing_still_counts_as_reviewed(self, mona, er):
        amal = NurseProfile.objects.select_related("user").get(user__username="Amal")
        same = {"team_leader": amal.team_leader, "score": amal.score, "ramadan_reduced": amal.ramadan_reduced,
                "breastfeeding": amal.breastfeeding, "ytd_overtime": 0, "hours_balance": 0, "night_balance": 0.0,
                "friday_balance": 0.0, "overtime_balance": 0.0, "allowed_shifts": None}
        assert preview(mona, er)["warnings"]["unreviewed_scores"] == 4

        response = mona.put(url(er, f"/nurses/{amal.pk}/attributes"), same, content_type="application/json")

        assert response.status_code == 200, response.content
        assert preview(mona, er)["warnings"]["unreviewed_scores"] == 3

    def test_saving_only_the_allowed_shifts_is_not_reviewing_the_score(self, mona, er):
        amal = NurseProfile.objects.get(user__username="Amal")

        response = mona.put(url(er, f"/nurses/{amal.pk}/allowed-shifts"), {"allowed_shifts": ["D"]},
                            content_type="application/json")

        assert response.status_code == 200, response.content
        assert preview(mona, er)["warnings"]["unreviewed_scores"] == 4

    def test_rules_the_solver_refuses_read_as_rules_and_make_no_solve(self, mona, er, solver):
        er.rules["night_shift"] = ""
        er.save()

        answer = preview(mona, er)
        refused = start_solve(mona, er)

        assert "night_shift" in answer["rules_refusal"] and answer["precheck"] is None
        assert refused.status_code == 409 and "review the unit rules" in refused.json()["detail"]
        assert not Solve.objects.exists()

    def test_a_solver_that_cannot_be_reached_only_loses_the_precheck(self, mona, er, solver):
        solver.precheck_error = SolverUnreachable("down")

        answer = preview(mona, er)

        assert answer["precheck"] is None and answer["rules_refusal"] is None and answer["refusal"] is None

    def test_a_refusal_at_the_solvers_door_reads_as_rules_too(self, mona, er, solver):
        solver.precheck_error = SolverRefused("request: department: no")

        assert preview(mona, er)["rules_refusal"] == "request: department: no"

    def test_no_month_means_the_next_one(self, mona, er):
        response = mona.get(url(er, "/solves/preview"))

        assert response.json()["month"] == MONTH

    def test_a_deactivated_manager_is_named(self, mona, er, make_user):
        sara = nurse_in(er, make_user, "sara", full_name="Sara Noor", role=Role.UNIT_MANAGER)
        User.objects.filter(pk=sara.user.pk).update(is_active=False)

        answer = preview(mona, er)

        assert answer["warnings"]["left_out"] == ["Sara Noor"] and answer["summary"]["nurses"] == 4


class TestStartRefusals:
    def test_a_real_transfer_that_empties_a_list_blocks_the_new_units_month(self, mona, er, make_user, client_of):
        """With a unit that shares no shift with the list, as `membership.transfer` leaves it."""
        boss = client_of("omar", Role.UNIT_MANAGER)
        icu = make_unit("ICU", User.objects.get(username="omar"))
        icu.rules = {**icu.rules, "shifts": ["A", "B"], "fair_shifts": ["A", "B"], "night_shift": "B", "day_shift": "A",
                     "headcounts": [hc | {"shift": code} for hc, code in zip(icu.rules["headcounts"], ("A", "B"))]}
        icu.save()
        emad = NurseProfile.objects.select_related("user").get(user__username="Emad")
        assert emad.placements.get().allowed_shifts == ["D"]

        membership.transfer(emad, icu, dt.date(2026, 6, 1))

        answer = preview(boss, icu)
        assert "Emad" in answer["refusal"]
        assert start_solve(boss, icu).status_code == 409

    def test_a_list_a_transfer_emptied_blocks_until_checked_and_names_them(self, mona, er, solver):
        amal = NurseProfile.objects.get(user__username="Amal")
        Placement.objects.filter(nurse=amal).update(allowed_shifts_to_check=True)
        User.objects.filter(pk=amal.user.pk).update(full_name="Amal Saleh")

        assert "Amal Saleh" in preview(mona, er)["refusal"]
        refused = start_solve(mona, er)
        assert refused.status_code == 409 and "Amal Saleh" in refused.json()["detail"]

        assert mona.put(url(er, f"/nurses/{amal.pk}/allowed-shifts"), {"allowed_shifts": None},
                        content_type="application/json").status_code == 200
        solver.script([finished()])
        assert preview(mona, er)["refusal"] is None
        started(mona, er)

    def test_a_flag_on_another_months_placement_does_not_block_this_one(self, mona, er, solver):
        amal = NurseProfile.objects.get(user__username="Amal")
        Placement.objects.create(nurse=amal, unit=make_unit("ICU"), from_month=dt.date(2026, 7, 1), allowed_shifts_to_check=True)

        assert preview(mona, er)["refusal"] is None

    def test_a_month_that_is_over(self, mona, er):
        assert start_solve(mona, er, "2026-04").status_code == 409

    def test_this_month_may_be_solved(self, mona, er, solver):
        solver.script([finished()])
        started(mona, er, "2026-05")

    def test_a_unit_with_nobody_in_the_month(self, mona, er):
        Placement.objects.filter(unit=er).update(unit=make_unit("ICU"))

        assert start_solve(mona, er).status_code == 409

    def test_one_solve_a_unit_at_a_time_across_its_months_naming_the_month_that_holds_it(self, mona, er, solver):
        """One unit could otherwise queue every month to 2076 ahead of every other unit's solve."""
        from hospital.models import FixedRamadanMonth
        FixedRamadanMonth.objects.create(month="2026-07", dates=[])
        june = started(mona, er)
        held = ("A solve of June 2026 is already queued or running. A unit solves one month at a time, so stop it or wait for it "
                "first.")

        refused = start_solve(mona, er, "2026-07")
        assert (refused.status_code, refused.json()["detail"]) == (409, held)
        assert preview(mona, er, "2026-07")["refusal"] == held
        assert start_solve(mona, er).json()["detail"] == "A solve of this month is already queued or running."
        assert Solve.objects.count() == 1 and AuditEntry.objects.filter(action="solve.started").count() == 1

        assert post(mona, url(er, f"/solves/{june['id']}/stop"), {}).status_code == 200
        assert preview(mona, er, "2026-07")["refusal"] is None
        started(mona, er, "2026-07")

    def test_another_units_manager_reaches_none_of_it(self, mona, er, client_of):
        solve = started(mona, er)
        other = client_of("omar", Role.UNIT_MANAGER)
        make_unit("ICU", User.objects.get(username="omar"))

        for response in (other.get(url(er, "/solves/preview")), other.get(url(er, "/roster")),
                         other.get(url(er, f"/solves/{solve['id']}")), start_solve(other, er),
                         post(other, url(er, f"/solves/{solve['id']}/stop"), {})):
            assert response.status_code == 403, response.content


class TestTimeLimit:
    def test_it_defaults_to_the_units(self, mona, er):
        er.default_time_limit = 90
        er.save()

        answer = preview(mona, er)

        assert (answer["time_limit_default"], answer["time_limit_cap"]) == (90, 300)

    def test_a_cap_lowered_after_the_units_save_wins(self, mona, er):
        er.default_time_limit = 90
        er.save()
        TechnicalSettings.objects.update_or_create(pk=1, defaults={"time_limit_cap_seconds": 30})

        assert preview(mona, er)["time_limit_default"] == 30

    def test_over_the_cap_is_refused_on_the_field(self, mona, er):
        response = start_solve(mona, er, seconds=301)

        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"][-1] == "time_limit_seconds"
        assert not Solve.objects.exists()

    def test_the_choice_reaches_the_solver(self, mona, er, solver):
        solver.script([finished()])
        solve = started(mona, er, seconds=45)

        running.run(solve["id"])

        assert solver.started[0]["solver"] == {"time_limit_seconds": 45, "reproducible": False}


class TestASolve:
    def test_from_queue_to_draft(self, mona, er, solver):
        seen = []
        result = result_of(roster_of(NURSES), cost=12)
        solver.script([job(best_cost=30, improvements=1, elapsed=0.5), job(best_cost=12, improvements=4, elapsed=2.0),
                        job("finished", result=result, best_cost=12, improvements=4, elapsed=2.5)])
        solve = started(mona, er)
        assert solve["state"] == "queued" and solve["queue_ahead"] == 0
        solver.on_read = lambda _: seen.append(progress(mona, er, solve["id"]))

        running.run(solve["id"])

        assert seen[0]["state"] == "running" and seen[0]["queue_ahead"] is None
        assert (seen[1]["best_cost"], seen[1]["improvements"], seen[1]["elapsed_seconds"]) == (30, 1, 0.5)
        assert (seen[2]["best_cost"], seen[2]["improvements"]) == (12, 4)
        done = progress(mona, er, solve["id"])
        assert (done["state"], done["found_roster"], done["best_cost"], done["improvements"]) == ("succeeded", True, 12, 3)
        stored = Solve.objects.get(pk=solve["id"])
        assert stored.result == result and stored.finished_at is not None
        assert stored.input == solver.started[0]
        draft = month_of(mona, er)["draft"]
        assert draft["cost"] == 12 and draft["solve_id"] == solve["id"]

    def test_each_solve_replaces_the_grid_and_every_result_stays(self, mona, er, solver):
        first = result_of(roster_of(NURSES, "D"), cost=20)
        second = result_of(roster_of(NURSES, "N"), cost=10)
        solver.script([job("finished", result=first)], [job("finished", result=second)])
        one = started(mona, er)
        running.run(one["id"])
        two = started(mona, er)
        running.run(two["id"])

        roster = Roster.objects.get(unit=er)
        assert roster.state == "draft" and roster.grid == second["roster"] and str(roster.solve_id) == two["id"]
        assert Solve.objects.get(pk=one["id"]).result == first
        assert Roster.objects.count() == 1

    def test_a_search_that_finds_nothing_leaves_the_draft_and_says_why(self, mona, er, solver):
        solver.script([finished()], [job("finished", result=result_of(None, ok=False, cost=None, message="No roster exists."))])
        running.run(started(mona, er)["id"])
        solve = started(mona, er)

        running.run(solve["id"])

        answer = month_of(mona, er)
        assert answer["last_solve"]["state"] == "succeeded" and answer["last_solve"]["found_roster"] is False
        assert answer["last_solve"]["message"] == "No roster exists."
        assert answer["draft"]["solve_id"] != solve["id"]

    def test_a_roster_the_validator_says_breaks_a_hard_rule_does_not_become_the_draft(self, mona, er, solver):
        solver.script([job("finished", result=result_of(roster_of(NURSES), ok=False, message="breaks 1 hard rule(s)"))])
        solve = started(mona, er)

        running.run(solve["id"])

        assert not Roster.objects.exists()
        assert progress(mona, er, solve["id"])["found_roster"] is False

    def test_what_a_solve_stored_stays_when_the_nurse_is_corrected(self, mona, er, solver):
        solver.script([finished()])
        solve = started(mona, er)
        kept = Solve.objects.get(pk=solve["id"]).input
        NurseProfile.objects.filter(user__username="Amal").update(gender="M")

        running.run(solve["id"])

        assert solver.started[0] == kept and kept["nurses"][0]["gender"] == "F"
        assert month_of(mona, er)["draft"]["changes"] == [{"hospital_id": "Amal", "full_name": "Amal", "what": "gender"}]

    def test_it_is_audited_without_a_nurse_in_it(self, mona, er, solver):
        solver.script([finished()])
        solve = started(mona, er)
        running.run(solve["id"])

        entries = AuditEntry.objects.filter(action__in=("solve.started", "roster.draft_replaced")).order_by("id")

        assert [(entry.action, entry.target) for entry in entries] == [("solve.started", f"ER, {MONTH}"),
                                                                       ("roster.draft_replaced", f"ER, {MONTH}")]
        assert all(entry.actor.username == "mona" for entry in entries)
        assert "Amal" not in str(entries[1].after)


class TestStopping:
    def test_a_queued_solve_ends_at_once_and_its_job_does_nothing(self, mona, er, solver):
        solve = started(mona, er)

        answer = post(mona, url(er, f"/solves/{solve['id']}/stop"), {}).json()
        running.run(solve["id"])

        assert answer["state"] == "cancelled" and solver.started == []
        assert AuditEntry.objects.filter(action="solve.stopped").count() == 1

    def test_a_running_solve_is_relayed_once_and_keeps_the_best_roster(self, mona, er, solver):
        result = result_of(roster_of(NURSES), cost=25, stopped_by="cancelled")
        solver.script([job(best_cost=25, improvements=1), job(best_cost=25, improvements=1), job(best_cost=25, improvements=1),
                        job("cancelled", result=result, best_cost=25, improvements=1)])
        solve = started(mona, er)
        reads = []

        def stop_on_the_second_read(job_id):
            reads.append(job_id)
            if len(reads) == 2:
                assert post(mona, url(er, f"/solves/{solve['id']}/stop"), {}).json()["state"] == "running"
        solver.on_read = stop_on_the_second_read

        running.run(solve["id"])

        assert solver.cancelled == ["job-1"]
        answer = month_of(mona, er)
        assert answer["draft"]["cost"] == 25 and answer["draft"]["stopped_early"] is True
        assert Solve.objects.get(pk=solve["id"]).state == "cancelled"

    def test_stopped_before_any_roster_leaves_the_draft_alone(self, mona, er, solver):
        result = result_of(None, ok=False, cost=None, stopped_by="cancelled", message="Stopped.")
        solver.script([job("cancelled", result=result)])
        solve = started(mona, er)

        running.run(solve["id"])

        assert not Roster.objects.exists()
        assert progress(mona, er, solve["id"])["state"] == "cancelled"

    def test_a_stop_after_the_proof_changes_nothing(self, mona, er, solver):
        solver.script([finished()])
        solve = started(mona, er)
        running.run(solve["id"])

        assert post(mona, url(er, f"/solves/{solve['id']}/stop"), {}).json()["state"] == "succeeded"
        assert not AuditEntry.objects.filter(action="solve.stopped").exists()

    def test_asking_twice_is_recorded_once(self, mona, er):
        solve = started(mona, er)
        post(mona, url(er, f"/solves/{solve['id']}/stop"), {})
        post(mona, url(er, f"/solves/{solve['id']}/stop"), {})

        assert AuditEntry.objects.filter(action="solve.stopped").count() == 1


class TestFailures:
    def test_a_failed_solve_is_retried_once_and_then_it_can_succeed(self, mona, er, solver):
        solver.script([job("failed", error="MemoryError: out")], [finished()])
        solve = started(mona, er)

        running.run(solve["id"])

        stored = Solve.objects.get(pk=solve["id"])
        assert (stored.state, stored.attempts, stored.error, stored.failure) == ("succeeded", 2, "MemoryError: out", "")
        assert len(solver.started) == 2

    def test_a_second_failure_records_its_reason(self, mona, er, solver):
        solver.script([job("failed", error="first")], [job("failed", error="MemoryError: out")])
        solve = started(mona, er)

        running.run(solve["id"])

        answer = progress(mona, er, solve["id"])
        assert (answer["state"], answer["failure"], answer["error"], answer["attempts"]) == ("failed", "solver_error", "MemoryError: out", 2)
        assert not Roster.objects.exists()

    def test_a_solver_that_cannot_be_reached_at_the_start(self, mona, er, solver):
        solver.script(SolverUnreachable("connection refused"), SolverUnreachable("connection refused"))
        solve = started(mona, er)

        running.run(solve["id"])

        answer = progress(mona, er, solve["id"])
        assert (answer["failure"], answer["error"]) == ("unreachable", "connection refused")

    def test_a_solver_that_goes_away_during_a_solve_is_given_a_few_polls(self, mona, er, solver):
        gone = SolverUnreachable("timed out")
        solver.script([gone, gone, job(best_cost=1, improvements=1), finished()])
        solve = started(mona, er)

        running.run(solve["id"])

        assert Solve.objects.get(pk=solve["id"]).attempts == 1

    def test_too_many_polls_unanswered_fail_the_attempt(self, mona, er, solver):
        solver.script([SolverUnreachable("timed out")], [finished()])
        solve = started(mona, er)

        running.run(solve["id"])

        assert Solve.objects.get(pk=solve["id"]).attempts == 2

    def test_the_kind_of_failure_is_the_last_attempts(self, mona, er, solver):
        solver.script([job("failed", error="first")], [SolverUnreachable("timed out")])
        solve = started(mona, er)

        running.run(solve["id"])

        assert Solve.objects.get(pk=solve["id"]).failure == "unreachable"

    def test_refused_rules_are_no_failed_solve_and_are_not_retried(self, mona, er, solver):
        solver.script(SolverRefused("request: department: night_shift"))
        solve = started(mona, er)

        running.run(solve["id"])

        answer = progress(mona, er, solve["id"])
        assert (answer["failure"], answer["error"], answer["attempts"]) == ("refused", "request: department: night_shift", 1)

    def test_a_solver_job_that_outlasts_its_time_limit_counts_as_hung(self, mona, er, solver, monkeypatch):
        monkeypatch.setattr(running, "OVERRUN_SECONDS", -1000)
        solver.script([job(best_cost=1)], [finished()])
        solve = started(mona, er)

        running.run(solve["id"])

        assert Solve.objects.get(pk=solve["id"]).attempts == 2

    def test_overrunning_twice_fails_as_an_overrun(self, mona, er, solver, monkeypatch):
        monkeypatch.setattr(running, "OVERRUN_SECONDS", -1000)
        solver.script([job(best_cost=1)], [job(best_cost=1)])
        solve = started(mona, er)

        running.run(solve["id"])

        assert Solve.objects.get(pk=solve["id"]).failure == "overrun"

    def test_an_error_of_the_workers_own_is_unexpected(self, mona, er, solver):
        solver.script(KeyError("Badr"), KeyError("Badr"))
        solve = started(mona, er)

        running.run(solve["id"])

        stored = Solve.objects.get(pk=solve["id"])
        assert (stored.failure, stored.error) == ("unexpected", "'Badr'")


class TestAWorkerThatDied:
    def test_it_goes_on_watching_the_job_the_solver_still_has(self, mona, er, solver):
        solve = started(mona, er)
        solver.started.append({})
        solver.reads["job-1"] = [finished()]
        Solve.objects.filter(pk=solve["id"]).update(state="running", solver_job_id="job-1", attempts=1)

        running.run(solve["id"])

        stored = Solve.objects.get(pk=solve["id"])
        assert (stored.state, stored.attempts) == ("succeeded", 1) and len(solver.started) == 1

    def test_a_job_the_solver_lost_is_a_failed_attempt(self, mona, er, solver):
        solver.script([finished()])
        solve = started(mona, er)
        Solve.objects.filter(pk=solve["id"]).update(state="running", solver_job_id="gone", attempts=1)

        running.run(solve["id"])

        assert Solve.objects.get(pk=solve["id"]).attempts == 2

    def test_a_job_nobody_is_watching_is_stopped_before_this_one_starts(self, mona, er, solver):
        solver.busy_with = "orphan"
        solver.reads["orphan"] = [job("cancelled", result=None)]
        solver.script([finished()])
        solve = started(mona, er)

        running.run(solve["id"])

        assert solver.cancelled == ["orphan"]
        assert Solve.objects.get(pk=solve["id"]).state == "succeeded"

    def test_the_last_attempt_lost_is_a_failure(self, mona, er, solver):
        solve = started(mona, er)
        Solve.objects.filter(pk=solve["id"]).update(state="running", attempts=2)

        running.run(solve["id"])

        stored = Solve.objects.get(pk=solve["id"])
        assert (stored.state, stored.failure) == ("failed", "worker_lost") and "worker stopped" in stored.error

    def test_the_solver_losing_the_last_attempt_is_a_restart(self, mona, er, solver):
        solve = started(mona, er)
        Solve.objects.filter(pk=solve["id"]).update(state="running", solver_job_id="gone", attempts=2)

        running.run(solve["id"])

        assert Solve.objects.get(pk=solve["id"]).failure == "lost"

    def test_a_solver_that_will_not_stop_an_earlier_job_is_stuck(self, mona, er, solver, monkeypatch):
        monkeypatch.setattr(running, "OVERRUN_SECONDS", -1000)
        solve = started(mona, er)
        Solve.objects.filter(pk=solve["id"]).update(state="running", attempts=1)
        solver.busy_with = "orphan"
        solver.reads["orphan"] = [job("running")]

        running.run(solve["id"])

        assert Solve.objects.get(pk=solve["id"]).failure == "stuck"

    def test_a_row_whose_job_is_gone_does_not_stay_active_for_good(self, mona, er, client_of, make_user):
        """Another unit's solve stays queued: a unit has one active at a time."""
        omar = client_of("omar", Role.UNIT_MANAGER)
        icu = make_sample_unit("ICU", User.objects.get(username="omar"), make_user, prefix="i")
        solve = started(mona, er)
        kept = started(omar, icu)
        Solve.objects.filter(pk=solve["id"]).update(job_id=999999999)

        running.end_what_the_queue_lost()

        assert (Solve.objects.get(pk=solve["id"]).state, Solve.objects.get(pk=solve["id"]).failure) == ("failed", "worker_lost")
        assert Solve.objects.get(pk=kept["id"]).state == "queued"


class TestTheGrid:
    def test_cells_shifts_leave_and_days_outside_the_dates(self, mona, er, solver):
        badr = NurseProfile.objects.get(user__username="Badr")
        Leave.objects.create(nurse=badr, date=dt.date(2026, 6, 9), leave_type_id=_sick(), recorded_by=badr.user)
        NurseProfile.objects.filter(user__username="Dana").update(start_date=dt.date(2026, 6, 2))
        grid = roster_of(NURSES, "D")
        grid["Amal"]["2026-06-01"] = ""
        grid["Badr"]["2026-06-09"] = ""
        grid["Dana"]["2026-06-01"] = ""
        solver.script([finished(grid)])
        running.run(started(mona, er)["id"])

        draft = month_of(mona, er)["draft"]

        rows = {row["hospital_id"]: row["cells"] for row in draft["rows"]}
        assert len(draft["days"]) == 30
        assert rows["Amal"][0] == {"shift": "", "kind": "off"} and rows["Amal"][1] == {"shift": "D", "kind": "shift"}
        assert rows["Badr"][8] == {"shift": "", "kind": "leave"}
        assert rows["Dana"][0] == {"shift": "", "kind": "outside"}
        assert [shift["code"] for shift in draft["shifts"]] == ["D"]
        assert draft["shifts"][0]["colour"] == Shift.objects.get(code="D").colour

    def test_a_cell_says_what_it_was_when_solved_after_a_later_correction(self, mona, er, solver):
        NurseProfile.objects.filter(user__username="Dana").update(start_date=dt.date(2026, 6, 2))
        grid = roster_of(NURSES, "D")
        grid["Dana"]["2026-06-01"] = ""
        solver.script([finished(grid)])
        running.run(started(mona, er)["id"])

        NurseProfile.objects.filter(user__username="Dana").update(start_date=dt.date(2026, 1, 1))

        draft = month_of(mona, er)["draft"]
        dana = next(row for row in draft["rows"] if row["hospital_id"] == "Dana")
        assert dana["cells"][0] == {"shift": "", "kind": "outside"}
        assert draft["changes"] == [{"hospital_id": "Dana", "full_name": "Dana", "what": "leave"}]

    def test_weekends_and_holidays_are_marked(self, mona, er, solver):
        solver.script([finished()])
        running.run(started(mona, er)["id"])

        days = month_of(mona, er)["draft"]["days"]

        assert [day["date"] for day in days if day["weekend"]][:2] == ["2026-06-05", "2026-06-12"]

    def test_its_time_is_on_the_hospitals_clock(self, mona, er, solver):
        solver.script([finished()])
        running.run(started(mona, er)["id"])

        assert month_of(mona, er)["draft"]["solved_at"].endswith("+03:00")

    def test_no_roster_yet(self, mona, er):
        assert month_of(mona, er) == {
            "month": MONTH, "state": "none", "submitted": None, "last_rejection": None, "approved": None,
            "submit_refusal": "This month has no roster yet.", "approve_refusal": "Only the CNO or the Deputy CNO approve or reject a roster.",
            "reject_refusal": "Only the CNO or the Deputy CNO approve or reject a roster.",
            "active_solve": None, "last_solve": None, "draft": None}

    def test_a_solve_under_way_is_on_the_month(self, mona, er):
        solve = started(mona, er)

        assert month_of(mona, er)["active_solve"]["id"] == solve["id"]

    def test_a_failure_newer_than_the_draft_is_shown_beside_it(self, mona, er, solver):
        solver.script([finished()], SolverRefused("no"))
        running.run(started(mona, er)["id"])
        assert month_of(mona, er)["last_solve"] is None
        failed = started(mona, er)
        running.run(failed["id"])

        answer = month_of(mona, er)

        assert answer["last_solve"]["id"] == failed["id"] and answer["draft"] is not None


class TestThePollIsNotActivity:
    def _expiry(self, client):
        return Session.objects.get(pk=client.cookies["sessionid"].value).expire_date

    def test_polling_does_not_move_the_idle_limit_and_anything_else_does(self, mona, er):
        solve = started(mona, er)
        earlier = timezone.now() + dt.timedelta(hours=1)
        Session.objects.filter(pk=mona.cookies["sessionid"].value).update(expire_date=earlier)

        polled = mona.get(url(er, f"/solves/{solve['id']}"))

        assert polled.status_code == 200 and "sessionid" not in polled.cookies
        assert self._expiry(mona) == earlier

        assert mona.get(url(er, "/roster")).status_code == 200
        assert self._expiry(mona) > earlier + dt.timedelta(hours=1)

    def test_a_session_past_its_limit_ends_the_poll(self, mona, er):
        solve = started(mona, er)
        Session.objects.filter(pk=mona.cookies["sessionid"].value).update(expire_date=timezone.now() - dt.timedelta(seconds=1))

        assert mona.get(url(er, f"/solves/{solve['id']}")).status_code == 401

    def test_a_refused_poll_is_not_activity_either(self, mona, er, client_of):
        """A manager taken off the unit with the page open would otherwise never reach the idle limit."""
        solve = started(mona, er)
        other = client_of("omar", Role.UNIT_MANAGER)
        earlier = timezone.now() + dt.timedelta(hours=1)
        Session.objects.filter(pk=other.cookies["sessionid"].value).update(expire_date=earlier)

        assert other.get(url(er, f"/solves/{solve['id']}")).status_code == 403
        assert self._expiry(other) == earlier


class TestShiftHours:
    def _shift_body(self, shift, **changes):
        return {"code": shift.code, "display_name": shift.display_name, "hours": shift.hours,
                "start_time": shift.start_time.strftime("%H:%M"), "end_time": shift.end_time.strftime("%H:%M"),
                "colour": shift.colour, "active": shift.active} | changes

    def _put(self, client, shift, **changes):
        return client.put(f"/api/hospital/shifts/{shift.pk}", self._shift_body(shift, **changes), content_type="application/json")

    def test_refused_while_a_roster_uses_the_shift_and_names_it(self, mona, er, solver, client_of):
        cno = client_of("chief", Role.CNO)
        solver.script([finished(roster_of(NURSES, "D"))])
        running.run(started(mona, er)["id"])

        response = self._put(cno, Shift.objects.get(code="D"), hours=10)

        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"][-1] == "hours"
        assert "ER, June 2026" in response.json()["detail"][0]["msg"]
        assert Shift.objects.get(code="D").hours == 12

    def test_a_shift_no_roster_uses_can_change(self, mona, er, solver, client_of):
        cno = client_of("chief", Role.CNO)
        solver.script([finished(roster_of(NURSES, "D"))])
        running.run(started(mona, er)["id"])

        assert self._put(cno, Shift.objects.get(code="N"), hours=10).status_code == 200

    def test_other_fields_of_a_shift_in_use_can_change(self, mona, er, solver, client_of):
        cno = client_of("chief", Role.CNO)
        solver.script([finished(roster_of(NURSES, "D"))])
        running.run(started(mona, er)["id"])

        assert self._put(cno, Shift.objects.get(code="D"), display_name="Daytime").status_code == 200

    def test_refused_while_a_solve_of_the_shift_is_on_its_way(self, mona, er, client_of):
        cno = client_of("chief", Role.CNO)
        started(mona, er)

        assert self._put(cno, Shift.objects.get(code="D"), hours=10).status_code == 422
        assert self._put(cno, Shift.objects.get(code="A"), hours=10).status_code == 200

    def test_a_rename_does_not_get_past_a_solve_on_its_way(self, mona, er, client_of):
        cno = client_of("chief", Role.CNO)
        started(mona, er)

        assert self._put(cno, Shift.objects.get(code="D"), code="DD").status_code == 200
        assert self._put(cno, Shift.objects.get(code="DD"), hours=10).status_code == 422

    def test_a_rename_carries_through_the_grid_so_the_guard_still_holds(self, mona, er, solver, client_of):
        cno = client_of("chief", Role.CNO)
        solver.script([finished(roster_of(NURSES, "D"))])
        solve = started(mona, er)
        running.run(solve["id"])
        stored = Solve.objects.get(pk=solve["id"]).result

        assert self._put(cno, Shift.objects.get(code="D"), code="DD").status_code == 200

        roster = Roster.objects.get(unit=er)
        assert set(roster.grid["Amal"].values()) == {"DD"}
        assert Solve.objects.get(pk=solve["id"]).result == stored
        assert self._put(cno, Shift.objects.get(code="DD"), hours=10).status_code == 422
        assert month_of(mona, er)["draft"]["shifts"][0]["code"] == "DD"

    def test_a_rename_while_a_solve_runs_reaches_the_grid_it_returns(self, mona, er, solver, client_of):
        cno = client_of("chief", Role.CNO)
        solver.script([finished(roster_of(NURSES, "D"))])
        solve = started(mona, er)
        solver.on_read = lambda _: self._put(cno, Shift.objects.get(code="D"), code="DD") if Shift.objects.filter(code="D").exists() else None

        running.run(solve["id"])

        assert set(Roster.objects.get(unit=er).grid["Amal"].values()) == {"DD"}


def _doing_under_a_worker(solve, heartbeat):
    """The queue's models are read-only in Django, so this is what its own worker would have written."""
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO procrastinate_workers (last_heartbeat) VALUES (%s) RETURNING id", [heartbeat])
        worker = cursor.fetchone()[0]
        cursor.execute("UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
                       [worker, Solve.objects.get(pk=solve["id"]).job_id])


def test_a_stalled_job_is_put_back_on_the_queue(mona, er):
    solve = started(mona, er)
    job_id = Solve.objects.get(pk=solve["id"]).job_id
    _doing_under_a_worker(solve, timezone.now() - dt.timedelta(minutes=5))

    assert requeue_stalled() == [job_id]

    assert ProcrastinateJob.objects.get(pk=job_id).status == "todo"


def test_a_job_whose_worker_is_alive_is_left_alone(mona, er):
    solve = started(mona, er)
    _doing_under_a_worker(solve, timezone.now())

    assert requeue_stalled() == []


def _sick():
    return LeaveType.objects.get(name="Sick").pk
