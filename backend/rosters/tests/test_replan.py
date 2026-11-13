"""Re-planning an approved month from a date: days before it are facts (the roster in force, recorded leave and the pins that
count), the solver keeps the rest close to the roster in force, and the result is a new version. The API with the scripted solver
and the real worker's code, on a real roster of the four-nurse sample (`fixtures/sound_rosters.json`). What the solver makes of
the input is read through its own loader, so the facts are the solver's and not this code's idea of them."""
import copy
import datetime as dt

import pytest
from nurse_roster.request import load_request

from accounts.models import Role, User
from audit.models import AuditEntry
from conftest import post
from notifications.models import Event, Notification, SolveOutcome
from nurses import membership
from hospital.models import HospitalSettings, Shift
from nurses.models import Placement
from rosters.drafts import FROZEN
from rosters.models import PinnedCell, PinOrigin, Roster, RosterState, VersionOrigin
from solves.replans import A_DRAFT, ACTIVE, MONTH_OVER, NO_ROSTER_YET
from rosters.tests.test_review import (SOUND, add_holiday, approve, approved, carla, dina, fix_ramadan, month_of, ok,  # noqa: F401
                                       profile, record_leave, refused, roster, solved, submitted, url)
from rosters.tests.test_versions import changed, in_force_agrees, latest, works_e
from rosters.tests.test_pins import change_shift, icu, pin
from requesting.models import PreferenceKind
from solves.tests.conftest import start_solve
from solves.tests.conftest import started as solves_started
from solves.tests.test_input import carried_from_may, full_runs_capped, preference
from solves import running
from solves.models import ReplanOutcome, Solve, SolveState
from solves.tests.conftest import MONTH, NURSES, job, result_of

pytestmark = pytest.mark.django_db

IN_FORCE = SOUND[MONTH]["roster"]
# In SOUND's June, Amal works Night on the 1st to the 4th and is off on the 5th and the 9th; Badr works Day on the 2nd.
TODAY = dt.date(2026, 6, 12)
JULY = "2026-07"


@pytest.fixture(autouse=True)
def _scene(before_june, solver):
    pass


@pytest.fixture
def mid_june(monkeypatch):
    """After the approval, which happened before June: the month is under way."""
    def move_to(day=TODAY):
        monkeypatch.setattr(membership, "today", lambda: day)
    return move_to


@pytest.fixture
def in_force(mona, carla, er, solver, mid_june):  # noqa: F811
    approved(mona, carla, er, solver)
    mid_june()


def start(client, unit, day="2026-06-12", seconds=30, reason=None, month=MONTH):
    body = {"month": month, "replan_from": day, "time_limit_seconds": seconds}
    if reason is not None:
        body["reason"] = reason
    return post(client, url(unit, "/replans"), body)


def started(client, unit, **options) -> dict:
    response = start(client, unit, **options)
    assert response.status_code == 201, response.content
    return response.json()


def barred_pins_of_amal(unit) -> None:
    """Amal's BLS, required to work, counted until 2 June; then Night pinned on the 5th, before the date, and the 20th."""
    from rosters.tests.test_review import holding
    holding("Amal", "BLS", dt.date(2026, 6, 2), required=True)
    made, shift = User.objects.get(username="mona"), Shift.objects.get(code="N")
    for day in (5, 20):
        PinnedCell.objects.create(unit=unit, nurse=profile("Amal"), date=dt.date(2026, 6, day), shift=shift, pinned_by=made)


def facts(solve_input) -> dict[str, dict[int, str]]:
    """Each nurse's pinned cells as the solver's own loader leaves them: the past from the roster in force, leave and pins."""
    loaded = load_request(solve_input)
    return {nurse.id: dict(nurse.pinned) for nurse in loaded.problem.nurses}


def field_refusal(response, field="replan_from") -> str:
    assert response.status_code == 422, response.content
    return {tuple(error["loc"])[-1]: error["msg"] for error in response.json()["detail"]}[field]


def preview(client, unit, day=None, month=MONTH) -> dict:
    return ok(client.get(url(unit, "/replans/preview"), {"month": month} | ({"replan_from": day} if day else {})))


class TestStarting:
    def test_it_stores_the_roster_in_force_and_the_date_and_the_solver_makes_earlier_days_facts(self, mona, er, in_force):
        answer = started(mona, er)

        solve = Solve.objects.get(pk=answer["id"])
        assert solve.replan_from == TODAY and solve.replan_of == 1
        assert solve.input["replan_from"] == "2026-06-12"
        assert solve.input["published"] == IN_FORCE
        pinned = facts(solve.input)
        # Days 0 to 10 (the 1st to the 11th) are the roster in force, cell for cell; nothing from the 12th on is fixed.
        assert pinned["Amal"] == {day: IN_FORCE["Amal"][f"2026-06-{day + 1:02d}"] for day in range(11)}
        assert all(max(cells, default=-1) < 11 for cells in pinned.values())

    def test_today_is_the_earliest_date_and_the_preview_starts_there(self, mona, er, in_force):
        answer = preview(mona, er)

        assert (answer["replan_from"], answer["earliest"], answer["latest"]) == ("2026-06-12", "2026-06-12", "2026-06-30")
        assert answer["in_force"] == 1 and answer["refusal"] is None and answer["date_refusal"] is None
        assert answer["summary"]["nurses"] == 4 and answer["recorded_cells_differ"] == 0

    def test_a_date_already_over_is_refused_the_first_day_of_the_month_under_way_included(self, mona, er, in_force):
        for day in ("2026-06-11", "2026-06-01"):
            assert field_refusal(start(mona, er, day=day)) == (
                f"{int(day[-2:])} June 2026 is over. Re-plan from 12 June 2026 or later: the days before then are facts.")
        assert preview(mona, er, "2026-06-11")["date_refusal"].startswith("11 June 2026 is over.")
        assert not Solve.objects.filter(replan_from__isnull=False).exists()

    @pytest.mark.parametrize("day", ["2026-07-01", "2026-05-31"])
    def test_a_date_outside_the_month_is_refused_and_its_first_day_after_is_month_close(self, mona, er, in_force, day):
        assert field_refusal(start(mona, er, day=day)) == "Choose a date in June 2026."

    def test_the_last_day_of_the_month_is_allowed(self, mona, er, in_force):
        assert started(mona, er, day="2026-06-30")["replan_from"] == "2026-06-30"

    def test_a_month_not_yet_started_is_re_planned_from_any_of_its_days_its_first_included(self, mona, carla, er, solver,
                                                                                          mid_june):
        approved(mona, carla, er, solver, month=JULY)
        mid_june()

        assert preview(mona, er, month=JULY)["earliest"] == "2026-07-01"
        solve = Solve.objects.get(pk=started(mona, er, day="2026-07-01", month=JULY)["id"])
        assert solve.input["replan_from"] == "2026-07-01"
        assert all(not cells for cells in facts(solve.input).values()), "no day of July has happened"

    def test_a_shift_the_unit_stopped_working_that_the_roster_in_force_holds_before_the_date(self, mona, carla, er, solver,
                                                                                            mid_june):
        """The solver refuses the roster in force as `published` with a cell on a shift its unit does not work, whichever side of
        the date the cell is. The unit-rules save now refuses to take out such a shift (`rosters.drafts`), so this month is one
        from before that guard: the re-plan is refused before it starts, not a solve that fails, and no solve is kept."""
        works_e(er)
        approved(mona, carla, er, solver)
        mid_june()
        version = latest(er)
        version.grid["Amal"]["2026-06-03"] = "E"
        version.save()
        Roster.objects.filter(unit=er).update(grid=version.grid)
        body = {"rules": er.rules | {"shifts": ["D", "N"]}, "request_cap": er.request_cap, "preference_cap": er.preference_cap,
                "default_time_limit": er.default_time_limit}
        assert mona.put(url(er, "/rules"), body, content_type="application/json").status_code == 422
        er.rules = er.rules | {"shifts": ["D", "N"]}
        er.save()

        assert preview(mona, er)["rules_refusal"] is not None
        answer = start(mona, er)
        assert answer.status_code == 409 and answer.json()["detail"].startswith("The solver refuses the month as it stands")
        assert not Solve.objects.filter(replan_from__isnull=False).exists()

    def test_a_month_that_is_over_is_refused(self, mona, er, in_force, mid_june):
        mid_june(dt.date(2026, 7, 2))

        assert refused(start(mona, er, day="2026-06-30")) == MONTH_OVER
        assert preview(mona, er)["refusal"] == MONTH_OVER

    def test_a_draft_is_solved_again_rather_than_re_planned(self, mona, er, solver, mid_june):
        solved(mona, er, solver)
        mid_june()

        assert refused(start(mona, er)) == A_DRAFT
        assert preview(mona, er)["refusal"] == A_DRAFT

    def test_a_submitted_roster_waits_for_its_decision(self, mona, er, solver, mid_june):
        submitted(mona, er, solver)
        mid_june()

        assert refused(start(mona, er)) == FROZEN[RosterState.SUBMITTED]

    def test_a_month_with_no_roster_has_nothing_to_re_plan(self, mona, er, mid_june):
        mid_june()

        assert refused(start(mona, er)) == NO_ROSTER_YET
        assert preview(mona, er)["refusal"] == NO_ROSTER_YET and preview(mona, er)["in_force"] is None

    def test_a_plain_solve_of_an_approved_month_is_refused_and_points_to_a_re_plan(self, mona, er, in_force):
        assert refused(start_solve(mona, er)) == FROZEN[RosterState.APPROVED]
        assert "Re-plan" in FROZEN[RosterState.APPROVED]

    def test_one_at_a_time_a_month_and_on_the_same_queue_as_a_solve(self, mona, er, in_force):
        first = started(mona, er)

        assert refused(start(mona, er, day="2026-06-20")) == ACTIVE
        assert preview(mona, er)["refusal"] == ACTIVE
        progress = ok(mona.get(url(er, f"/solves/{first['id']}")))
        assert progress["state"] == "queued" and progress["replan_from"] == "2026-06-12"
        assert month_of(mona, er)["active_solve"]["id"] == first["id"]

    def test_one_a_unit_across_its_months_with_a_solve_of_another_month(self, mona, er, in_force, solver):
        """A re-plan and a solve of the same unit share the one place it has on the queue, whichever month."""
        from hospital.models import FixedRamadanMonth
        FixedRamadanMonth.objects.create(month="2026-07", dates=[])
        july = solves_started(mona, er, "2026-07")
        assert refused(start(mona, er)) == ("A solve of July 2026 is already queued or running. A unit solves one month at a time, "
                                            "so stop it or wait for it first.")
        assert preview(mona, er)["refusal"] == refused(start(mona, er))

        ok(post(mona, url(er, f"/solves/{july['id']}/stop"), {}))
        started(mona, er)
        assert refused(start_solve(mona, er, "2026-07")) == ("A re-plan of June 2026 is already queued or running. A unit solves "
                                                             "one month at a time, so stop it or wait for it first.")

    def test_the_time_limit_cap_holds(self, mona, er, in_force):
        assert field_refusal(start(mona, er, seconds=100000), "time_limit_seconds").startswith("The System Admin's cap is")

    def test_only_the_units_manager_starts_one(self, mona, carla, er, in_force):
        assert start(carla, er).status_code == 403
        assert carla.get(url(er, "/replans/preview"), {"month": MONTH}).status_code == 403


class TestFactsBeforeTheDate:
    def test_leave_recorded_on_a_worked_day_since_approval_is_a_day_off(self, mona, er, in_force):
        """Leave took the shift off as a version of its own, which is what the re-plan starts from."""
        assert record_leave(mona, er, "Amal", 2).status_code == 201
        assert latest(er).number == 2

        solve = Solve.objects.get(pk=started(mona, er)["id"])

        assert solve.replan_of == 2 and facts(solve.input)["Amal"][1] == ""
        assert preview(mona, er)["recorded_cells_differ"] == 0

    def test_a_start_date_corrected_since_leaves_the_days_before_it_off_though_the_roster_in_force_works_them(
            self, mona, er, in_force):
        """A correction makes no version, so the roster in force still has Amal on Night on the 1st and 2nd."""
        nurse = profile("Amal")
        nurse.start_date = dt.date(2026, 6, 3)
        nurse.save()

        assert preview(mona, er)["recorded_cells_differ"] == 2
        solve = Solve.objects.get(pk=started(mona, er)["id"])
        assert (facts(solve.input)["Amal"][0], facts(solve.input)["Amal"][1]) == ("", "")
        assert solve.input["published"]["Amal"]["2026-06-01"] == "N"

    def test_pins_of_every_origin_are_facts(self, mona, er, in_force):
        made = User.objects.get(username="mona")
        for nurse, day, origin in (("Amal", 6, PinOrigin.MANAGER), ("Badr", 2, PinOrigin.REQUEST), ("Dana", 4, PinOrigin.SWAP)):
            PinnedCell.objects.create(unit=er, nurse=profile(nurse), date=dt.date(2026, 6, day), shift=None, origin=origin,
                                      pinned_by=made)
        worked = {nurse: IN_FORCE[nurse][f"2026-06-{day:02d}"] for nurse, day in (("Amal", 6), ("Badr", 2), ("Dana", 4))}
        assert all(worked.values()), "each pin takes a worked day off"

        solve = Solve.objects.get(pk=started(mona, er)["id"])

        pinned = facts(solve.input)
        assert (pinned["Amal"][5], pinned["Badr"][1], pinned["Dana"][3]) == ("", "", "")
        assert preview(mona, er)["recorded_cells_differ"] == 3

    def test_her_live_preferences_reach_a_re_plan_as_they_reach_a_solve(self, mona, er, in_force):
        """The solver prices only the days it plans again; the input holds the month's, as a solve's does."""
        for day, kind in ((3, PreferenceKind.DAY_OFF), (20, PreferenceKind.DAY_OFF), (22, PreferenceKind.WORKING_DAY)):
            preference(er, "Amal", dt.date(2026, 6, day), kind)

        solve = Solve.objects.get(pk=started(mona, er)["id"])

        amal = next(row for row in solve.input["nurses"] if row["id"] == "Amal")
        assert (amal["undesired_days"], amal["desired_days"]) == (["2026-06-03", "2026-06-20"], ["2026-06-22"])

    def test_her_count_of_preferences_not_kept_reaches_a_re_plan_as_it_reaches_a_solve(self, mona, er, in_force):
        """The count carried into June doubles her price in a re-plan too."""
        carried_from_may(er, "Amal", 2)

        solve = Solve.objects.get(pk=started(mona, er)["id"])

        assert {row["id"]: row["preferences_not_kept"] for row in solve.input["nurses"]} == {
            "Amal": 2, "Badr": 0, "Dana": 0, "Emad": 0}

    def test_each_nurses_overtime_balance_reaches_a_re_plan_as_it_reaches_a_solve(self, mona, er, in_force):
        """From the chain: May's closed balance for Amal, the opening value for the others, Dana's set."""
        carried_from_may(er, "Amal", 0, overtime_balance=-11.5)
        from nurses.models import NurseProfile
        NurseProfile.objects.filter(user__username="Dana").update(overtime_balance=4.25)

        solve = Solve.objects.get(pk=started(mona, er)["id"])

        assert {row["id"]: row["overtime_balance"] for row in solve.input["nurses"]} == {
            "Amal": -11.5, "Badr": 0.0, "Dana": 4.25, "Emad": 0.0}
        assert {nurse.id: nurse.overtime_balance for nurse in load_request(solve.input).problem.nurses} == {
            "Amal": -11.5, "Badr": 0.0, "Dana": 4.25, "Emad": 0.0}

    def test_the_units_full_run_cap_and_the_hospitals_weight_reach_a_re_plan_as_they_reach_a_solve(self, mona, er, in_force):
        """The solver prices full runs above the cap in the days it plans again."""
        full_runs_capped(er, 1, 50)

        solve = Solve.objects.get(pk=started(mona, er)["id"])

        problem = load_request(solve.input).problem
        assert (problem.department.max_full_runs, problem.general.weights.excess_full_run) == (1, 50)

    def test_the_units_overtime_share_and_the_hospitals_weight_reach_a_re_plan_as_they_reach_a_solve(self, mona, er, in_force):
        """The solver prices overtime above a share in the days it plans again, the elapsed days counting."""
        er.rules |= {"share_overtime": True, "overtime_tolerance": 6, "overtime_carry_cap": 20}
        er.save()
        settings = HospitalSettings.load()
        settings.rules["weights"] |= {"nurse_overtime": 3, "overtime_above_share": 2}
        settings.save()

        solve = Solve.objects.get(pk=started(mona, er)["id"])

        problem = load_request(solve.input).problem
        department = problem.department
        assert (department.share_overtime, department.overtime_tolerance, department.overtime_carry_cap) == (True, 6, 20)
        assert problem.general.weights.overtime_above_share == 2

    def test_a_shift_pinned_on_a_barred_day_is_set_aside_before_the_date_too_and_the_day_is_the_roster_in_forces(
            self, mona, er, in_force):
        """A pin on a barred day is set aside, as one on leave is, so it is no fact; what the roster in force holds
        that day is, as the solver takes an elapsed shift on a barred day as a fact and refuses one on a day it plans."""
        barred_pins_of_amal(er)

        solve = Solve.objects.get(pk=started(mona, er)["id"])

        amal = next(row for row in solve.input["nurses"] if row["id"] == "Amal")
        assert "pinned" not in amal
        assert facts(solve.input)["Amal"][4] == IN_FORCE["Amal"]["2026-06-05"] == ""

    def test_a_pin_on_a_barred_day_does_not_make_its_end_outdated(self, mona, er, solver, in_force):
        """The month as the end reads it is the month as the re-plan's input read it: the pin set aside in both."""
        barred_pins_of_amal(er)

        assert replanned(mona, er, solver).replan_outcome == ReplanOutcome.VERSION

    def test_her_skills_and_the_units_needs_reach_a_re_plan_as_they_reach_a_solve(self, mona, er, in_force):
        """The solver honours them on the days it plans again; an earlier day is a fact, reported apart."""
        from rosters.tests.test_review import holding
        holding("Amal", "BLS", dt.date(2026, 6, 14), required=True)
        holding("Badr", "ICU", None, expires=False)
        er.rules |= {"skill_needs": [{"shift": "N", "skill": "ICU", "weekday_min": 1, "weekend_min": 0}]}
        er.save()

        solve = Solve.objects.get(pk=started(mona, er)["id"])

        problem = load_request(solve.input).problem
        skills = {nurse.id: nurse.skills for nurse in problem.nurses}
        assert (skills["Amal"], skills["Badr"], skills["Dana"]) == ({"BLS": 13}, {"ICU": None}, {})
        assert [(need.shift, need.skill) for need in problem.department.skill_needs] == [("N", "ICU")]
        assert problem.general.required_skills == ["BLS"]

    def test_a_pin_set_aside_is_no_fact_and_the_day_is_the_roster_in_forces(self, mona, er, in_force):
        """Emad works Day on the 1st. A pin on Night there, then Night taken from his allowed shifts: the pin no longer fits."""
        PinnedCell.objects.create(unit=er, nurse=profile("Emad"), date=dt.date(2026, 6, 1), shift=Shift.objects.get(code="N"),
                                  pinned_by=User.objects.get(username="mona"))
        Placement.objects.filter(nurse=profile("Emad")).update(allowed_shifts=["D"])
        assert month_of(mona, er)["draft"]["pins"][0]["set_aside"] == "shift_not_allowed"

        solve = Solve.objects.get(pk=started(mona, er)["id"])

        assert facts(solve.input)["Emad"][0] == IN_FORCE["Emad"]["2026-06-01"] == "D"

    def test_a_pin_from_the_date_on_is_a_pin_the_re_plan_keeps(self, mona, er, in_force):
        PinnedCell.objects.create(unit=er, nurse=profile("Amal"), date=dt.date(2026, 6, 20), shift=None,
                                  pinned_by=User.objects.get(username="mona"))

        solve = Solve.objects.get(pk=started(mona, er)["id"])

        assert facts(solve.input)["Amal"][19] == ""

    def test_a_nurse_who_left_the_month_is_left_out_and_one_who_joined_worked_nothing_before(self, mona, carla, er, solver,
                                                                                             client_of, make_user, mid_june):
        approved(mona, carla, er, solver)
        other = icu(client_of("omar", Role.UNIT_MANAGER) and User.objects.get(username="omar"))
        assert carla.post(f"/api/nurses/{profile('Amal').pk}/transfer", {"unit_id": str(other.pk), "from_month": MONTH},
                          content_type="application/json").status_code == 200
        from solves.tests.conftest import nurse_in
        nurse_in(er, make_user, "Faris", gender="M", start=dt.date(2026, 6, 1))
        mid_june()

        solve = replanned(mona, er, solver, answered({nurse: row for nurse, row in SWAPPED.items() if nurse != "Amal"}
                                                     | {"Faris": dict.fromkeys(SWAPPED["Amal"], "")}))

        assert set(solve.input["published"]) == {"Badr", "Dana", "Emad", "Faris"}
        assert set(solve.input["published"]["Faris"].values()) == {""}
        assert facts(solve.input)["Faris"] == dict.fromkeys(range(11), "")
        assert "Amal" not in roster(er).grid and solve.replan_outcome == ReplanOutcome.VERSION
        assert set(roster(er).grid) == {"Badr", "Dana", "Emad", "Faris"} and set(roster(er).grid["Faris"].values()) == {""}
        in_force_agrees(er)


def moved(**cells) -> dict:
    """The roster in force with these cells changed: `Amal_15="D"` is Amal on Day on the 15th."""
    grid = copy.deepcopy(IN_FORCE)
    for key, code in cells.items():
        nurse, day = key.split("_")
        grid[nurse][f"2026-06-{int(day):02d}"] = code
    return grid


# Amal is off on the 15th and Emad works Day: the re-plan swaps them, two cells.
SWAPPED = moved(Amal_15="D", Emad_15="")


def answered(grid, *, ok=True, cost=120, state="finished"):
    result = result_of(grid, ok=ok, cost=cost) | {"replan": {"from": "2026-06-12", "recorded_cells_differ": 0}}
    if not ok:
        result |= {"roster": None, "nurses": None, "cost": None}
    return job(state, result=result, best_cost=cost)


def replanned(client, unit, solver, *answers, **options) -> Solve:
    """`answers` is one attempt's script; an exception or a list among them makes them several attempts, as `script` takes."""
    attempts = list(answers) if any(isinstance(a, (Exception, list)) for a in answers) else [list(answers)]
    solver.script(*(attempts if answers else [[answered(SWAPPED)]]))
    solve = started(client, unit, **options)
    running.run(solve["id"])
    return Solve.objects.get(pk=solve["id"])


def roster_changed_to() -> set[str]:
    return set(Notification.objects.filter(event=Event.ROSTER_CHANGED).values_list("user__username", flat=True))


def solve_ended(solve) -> dict:
    return Notification.objects.get(event=Event.SOLVE_ENDED, payload__solve_id=str(solve.pk)).payload


class TestItsEnd:
    def test_its_roster_becomes_the_next_version_in_force_with_origin_replan(self, mona, er, solver, in_force):
        solve = replanned(mona, er, solver)

        version = latest(er)
        assert (version.number, version.origin, version.solve_id) == (2, VersionOrigin.REPLAN, solve.pk)
        assert version.created_by.username == "mona"
        assert version.reason == "Re-planned from 12 June 2026."
        assert roster(er).grid == SWAPPED and roster(er).solve_id == solve.pk
        in_force_agrees(er)
        assert solve.state == SolveState.SUCCEEDED and solve.replan_outcome == ReplanOutcome.VERSION
        answer = month_of(mona, er)
        assert answer["approved"]["version"] == 2 and answer["draft"]["solve_id"] == str(solve.pk)
        assert answer["draft"]["edited"] is False, "the grid is its solve's, so nothing reads as changed by hand"

    def test_a_recorded_reason_is_the_versions(self, mona, er, solver, in_force):
        replanned(mona, er, solver, reason="  Badr is off sick for a week.  ")

        assert latest(er).reason == "Badr is off sick for a week."

    def test_it_is_audited_without_its_reason_and_the_nurses_are_told_but_not_the_manager(self, mona, er, solver, in_force):
        solve = replanned(mona, er, solver, reason="Badr is off sick for a week.")

        entry = AuditEntry.objects.get(action="roster.changed")
        assert entry.actor.username == "mona" and entry.after == {"version": 2, "pins": 0, "cells": 2}
        assert "sick" not in str(AuditEntry.objects.values_list("before", "after"))
        assert roster_changed_to() == set(NURSES)
        assert "sick" not in str(Notification.objects.values_list("payload", flat=True))
        assert solve_ended(solve) == {"unit_id": str(er.pk), "month": MONTH, "solve_id": str(solve.pk),
                                           "outcome": SolveOutcome.REPLANNED, "replan": True}
        started_entry = AuditEntry.objects.filter(action="solve.started").order_by("-pk").first()
        assert started_entry.after == {"time_limit_seconds": 30}, "as a solve's: the System Admin reads no roster"

    def test_one_that_changes_no_cell_makes_no_version_and_tells_no_nurse(self, mona, er, solver, in_force):
        solve = replanned(mona, er, solver, answered(copy.deepcopy(IN_FORCE)))

        assert latest(er).number == 1 and roster(er).solve_id != solve.pk
        assert solve.replan_outcome == ReplanOutcome.UNCHANGED
        assert roster_changed_to() == set() and not AuditEntry.objects.filter(action="roster.changed").exists()
        assert solve_ended(solve)["outcome"] == SolveOutcome.REPLAN_UNCHANGED

    @pytest.mark.parametrize("with_a_roster", [False, True], ids=["no roster", "one breaking a hard rule"])
    def test_one_that_keeps_no_roster_leaves_the_roster_in_force(self, mona, er, solver, in_force, with_a_roster):
        answer = job("finished", result=result_of(SWAPPED, ok=False)) if with_a_roster else answered(SWAPPED, ok=False)
        solve = replanned(mona, er, solver, answer)

        assert latest(er).number == 1 and roster(er).grid == IN_FORCE
        assert solve.replan_outcome == "" and roster_changed_to() == set()
        assert solve_ended(solve)["outcome"] == SolveOutcome.NO_ROSTER and solve_ended(solve)["replan"] is True
        progress = ok(mona.get(url(er, f"/solves/{solve.pk}")))
        assert progress["found_roster"] is False and progress["replan_from"] == "2026-06-12"
        assert progress["replan_outcome"] is None

    def test_a_failure_is_told_as_a_re_plans(self, mona, er, solver, in_force):
        solver.script(RuntimeError("down"), RuntimeError("down"))
        solve = started(mona, er)
        running.run(solve["id"])

        assert latest(er).number == 1
        assert {"outcome": SolveOutcome.FAILED, "replan": True}.items() <= solve_ended(Solve.objects.get(pk=solve["id"])).items()


def while_it_runs(solver, act):
    """`act` once, while the re-plan is under way: after the solver has its input and before its end is written."""
    done = []

    def once(job_id):
        if not done:
            done.append(act())
    solver.on_read = once


def under_way(roster):
    return [job(best_cost=130, improvements=1), answered(roster)]


class TestAChangeWhileItWaitsOrRuns:
    """Worth the user's review: a re-plan whose roster in force, or whose month's facts, moved after it started is not applied.
    Its roster would undo what came since, such as a shift leave took off; it stays in the history and the manager re-plans."""

    def test_a_hand_edit_making_a_new_version_leaves_its_roster_out(self, mona, er, solver, in_force):
        while_it_runs(solver, lambda: changed(mona, er, pin("Amal", 20, "D")))

        solve = replanned(mona, er, solver, *under_way(SWAPPED))

        assert latest(er).number == 2 and latest(er).origin == VersionOrigin.EDIT
        assert roster(er).grid["Amal"]["2026-06-20"] == "D" and roster(er).grid["Amal"]["2026-06-15"] == ""
        assert solve.replan_outcome == ReplanOutcome.OUTDATED
        assert solve_ended(solve)["outcome"] == SolveOutcome.REPLAN_OUTDATED
        assert roster_changed_to() == set(NURSES) - {"mona"}, "the nurses heard of the edit only"
        assert Notification.objects.filter(event=Event.ROSTER_CHANGED).count() == len(NURSES)

    def test_any_new_version_leaves_its_roster_out_even_with_no_pin_or_leave(self, mona, er, solver, in_force):
        """Every change today makes a pin or leave too; a version is what a later kind of change (a swap) is sure to make."""
        from rosters.versions import make_version
        while_it_runs(solver, lambda: make_version(roster(er), moved(Badr_20=""), VersionOrigin.SWAP, User.objects.get(username="mona")))

        solve = replanned(mona, er, solver, *under_way(SWAPPED))

        assert solve.replan_outcome == ReplanOutcome.OUTDATED and latest(er).origin == VersionOrigin.SWAP

    def test_a_ramadan_day_or_a_holiday_changed_while_it_runs_still_lets_its_roster_in(self, mona, carla, er, solver,
                                                                                         in_force):
        """The month's dates join what a draft is warned of, not what outdates a re-plan, as the spec has nothing
        else move. The manager is told of the change and re-plans again if the hours shifted."""
        def change_the_dates():
            fix_ramadan(carla, MONTH, 20)
            add_holiday(carla, MONTH, 20)
        while_it_runs(solver, change_the_dates)

        solve = replanned(mona, er, solver, *under_way(SWAPPED))

        assert solve.replan_outcome == ReplanOutcome.VERSION and latest(er).origin == VersionOrigin.REPLAN

    def test_leave_taking_a_shift_off_leaves_its_roster_out(self, mona, er, solver, in_force):
        """Emad works the 15th in the roster in force and in the re-plan's; the leave took it off as a version."""
        while_it_runs(solver, lambda: record_leave(mona, er, "Emad", 16))

        solve = replanned(mona, er, solver, *under_way(SWAPPED))

        assert solve.replan_outcome == ReplanOutcome.OUTDATED and latest(er).origin == VersionOrigin.EDIT

    def test_leave_on_a_day_off_makes_no_version_and_still_leaves_its_roster_out(self, mona, er, solver, in_force):
        """Amal is off on the 15th in the roster in force, so the leave makes no version: but the re-plan put her on Day there."""
        while_it_runs(solver, lambda: record_leave(mona, er, "Amal", 15))

        solve = replanned(mona, er, solver, *under_way(SWAPPED))

        assert latest(er).number == 1 and roster(er).grid == IN_FORCE
        assert solve.replan_outcome == ReplanOutcome.OUTDATED

    def test_a_skill_recorded_meanwhile_leaves_its_roster_out(self, mona, er, solver, in_force):
        from rosters.tests.test_review import holding
        while_it_runs(solver, lambda: holding("Emad", "BLS", dt.date(2027, 1, 1)))

        solve = replanned(mona, er, solver, *under_way(SWAPPED))

        assert solve.replan_outcome == ReplanOutcome.OUTDATED and latest(er).number == 1

    def test_a_rename_is_no_change_and_its_roster_goes_in_force_under_the_new_code(self, mona, carla, er, solver, in_force):
        while_it_runs(solver, lambda: change_shift(carla, "D", code="DY"))

        solve = replanned(mona, er, solver, *under_way(SWAPPED))

        assert solve.replan_outcome == ReplanOutcome.VERSION
        assert roster(er).grid["Amal"]["2026-06-15"] == "DY" and latest(er).grid["Amal"]["2026-06-15"] == "DY"
        in_force_agrees(er)

    def test_an_hours_change_of_a_shift_the_roster_in_force_works_is_refused_meanwhile(self, mona, carla, er, solver, in_force):
        """The catalogue refuses it (hours cannot change under an open month's roster), so the re-plan goes in force."""
        answers = []
        while_it_runs(solver, lambda: answers.append(change_shift(carla, "D", hours=8)))

        solve = replanned(mona, er, solver, *under_way(SWAPPED))

        assert answers[0].status_code == 422, answers[0].content
        assert "roster" in answers[0].content.decode(), answers[0].content
        assert solve.replan_outcome == ReplanOutcome.VERSION

    def test_a_month_that_ended_while_it_waited_is_not_re_planned(self, mona, er, solver, in_force, mid_june):
        while_it_runs(solver, lambda: mid_june(dt.date(2026, 7, 1)))

        solve = replanned(mona, er, solver, *under_way(SWAPPED))

        assert latest(er).number == 1 and solve.replan_outcome == ReplanOutcome.MONTH_OVER
        assert solve_ended(solve)["outcome"] == SolveOutcome.REPLAN_MONTH_OVER


class TestStopping:
    def test_a_stopped_re_plan_puts_the_best_roster_it_found_in_force(self, mona, er, solver, in_force):
        """Worth the user's review: as a stopped solve's best roster becomes the draft. The button says so."""
        solve_id = []
        while_it_runs(solver, lambda: ok(post(mona, url(er, f"/solves/{solve_id[0]}/stop"), {})))
        solver.script([job(best_cost=130, improvements=1), answered(SWAPPED, state="cancelled")])
        solve_id.append(started(mona, er)["id"])
        running.run(solve_id[0])

        solve = Solve.objects.get(pk=solve_id[0])
        assert solve.state == SolveState.CANCELLED and solve.replan_outcome == ReplanOutcome.VERSION
        assert solver.cancelled == ["job-2"], "job-1 was the solve the approval came from"
        assert latest(er).origin == VersionOrigin.REPLAN and roster(er).grid == SWAPPED
        assert solve_ended(solve)["outcome"] == SolveOutcome.STOPPED_REPLANNED

    def test_a_stopped_one_whose_roster_in_force_moved_meanwhile_is_not_applied_either(self, mona, er, solver, in_force):
        solve_id = []

        def edit_then_stop():
            changed(mona, er, pin("Amal", 20, "D"))
            ok(post(mona, url(er, f"/solves/{solve_id[0]}/stop"), {}))
        while_it_runs(solver, edit_then_stop)
        solver.script([job(best_cost=130, improvements=1), answered(SWAPPED, state="cancelled")])
        solve_id.append(started(mona, er)["id"])
        running.run(solve_id[0])

        solve = Solve.objects.get(pk=solve_id[0])
        assert solve.state == SolveState.CANCELLED and solve.replan_outcome == ReplanOutcome.OUTDATED
        assert latest(er).origin == VersionOrigin.EDIT and roster(er).grid["Amal"]["2026-06-15"] == ""

    def test_a_failed_attempt_is_retried_in_the_same_job_and_its_roster_then_goes_in_force(self, mona, er, solver, in_force):
        solve = replanned(mona, er, solver, RuntimeError("the solver restarted"), [answered(SWAPPED)])

        assert (solve.attempts, solve.state, solve.replan_outcome) == (2, SolveState.SUCCEEDED, ReplanOutcome.VERSION)
        assert solve.error == "the solver restarted" and latest(er).origin == VersionOrigin.REPLAN

    def test_one_taken_out_of_the_queue_changes_nothing(self, mona, er, solver, in_force):
        solve = started(mona, er)

        answer = ok(post(mona, url(er, f"/solves/{solve['id']}/stop"), {}))
        running.run(solve["id"])

        assert answer["state"] == "cancelled" and len(solver.started) == 1, "only the solve the approval came from"
        assert latest(er).number == 1 and Solve.objects.get(pk=solve["id"]).replan_outcome == ""


def result_of_solve(client, unit, solve) -> dict:
    return ok(client.get(url(unit, f"/solve-history/{solve.pk}")))


class TestTheComparison:
    def test_it_shows_the_cells_changed_cost_before_and_after_and_each_rules_difference(self, mona, er, solver, in_force):
        from solver_service.grid import validate_grid
        solve = replanned(mona, er, solver)

        answer = result_of_solve(mona, er, solve)["replan"]

        before = validate_grid(solve.input, IN_FORCE)  # the solver's own code, on the roster in force and the same input
        assert answer["replan_from"] == "2026-06-12" and answer["recorded_cells_differ"] == 0
        assert (answer["based_on"], answer["version"], answer["outcome"]) == (1, 2, "version")
        assert [(c["hospital_id"], c["full_name"], c["date"], c["before"], c["after"]) for c in answer["changes"]] == [
            ("Amal", "Amal", "2026-06-15", "", "D"), ("Emad", "Emad", "2026-06-15", "D", "")]
        assert answer["cost_before"] == before["cost"] and answer["cost_after"] == 120
        rules = {row["rule"]: (row["before"], row["after"]) for row in answer["rules"]}
        assert rules["fair_day"] == (before["penalties"]["fair_day"], 5)
        assert "change" not in rules, "the roster in force is its own baseline, and this scripted re-plan costs none either"
        assert all(row["before"] or row["after"] for row in answer["rules"])

    def test_a_re_plan_not_applied_still_compares_with_what_it_started_from(self, mona, er, solver, in_force):
        while_it_runs(solver, lambda: record_leave(mona, er, "Amal", 15))
        solve = replanned(mona, er, solver, *under_way(SWAPPED))

        answer = result_of_solve(mona, er, solve)["replan"]

        assert answer["outcome"] == "outdated" and answer["version"] is None and len(answer["changes"]) == 2

    def test_elapsed_violations_are_facts_in_the_result(self, mona, er, solver, in_force):
        fact = {"rule": "night_rest", "nurse": "Amal", "date": "2026-06-05", "text": "Amal: day 4 works after night"}
        answer = answered(SWAPPED)
        answer["result"]["elapsed_violations"] = [fact]
        solve = replanned(mona, er, solver, answer)

        result = result_of_solve(mona, er, solve)

        assert result["violations"] == [] and result["elapsed_violations"][0]["text"] == "Amal: 2026-06-05 works after night"
        assert latest(er).origin == VersionOrigin.REPLAN, "a fact is not an error"

    def test_a_plain_solve_has_none_and_the_history_row_says_which_is_a_re_plan(self, mona, er, solver, in_force):
        solve = replanned(mona, er, solver)

        rows = ok(mona.get(url(er, "/solve-history"), {"month": MONTH}))["solves"]

        assert [(row["replan_from"], row["replan_outcome"]) for row in rows] == [("2026-06-12", "version"), (None, None)]
        assert result_of_solve(mona, er, Solve.objects.exclude(pk=solve.pk).get())["replan"] is None
        assert ok(mona.get(url(er, f"/solve-history/{solve.pk}")))["solve"]["in_draft"] is True

    @pytest.mark.parametrize("approver", ["carla", "dina"])
    def test_the_approvers_read_it(self, approver, request, mona, er, solver, in_force):
        solve = replanned(mona, er, solver)

        assert len(result_of_solve(request.getfixturevalue(approver), er, solve)["replan"]["changes"]) == 2

    def test_the_system_admins_history_stays_its_closed_list(self, mona, er, solver, in_force, client_of):
        replanned(mona, er, solver)

        row = ok(client_of("root", Role.SYSTEM_ADMIN).get("/api/solve-history"))["solves"][0]

        assert set(row) == {"id", "month", "state", "failure", "requested_by", "requested_at", "finished_at", "elapsed_seconds",
                            "time_limit_seconds", "attempts", "cost", "found_roster", "unit"}

    def test_a_nurse_reads_none_of_it(self, mona, er, solver, in_force):
        from notifications.tests.test_events import as_user
        solve = replanned(mona, er, solver)

        badr = as_user("Badr")

        assert badr.get(url(er, f"/solve-history/{solve.pk}")).status_code == 403
        assert badr.get(url(er, "/roster/versions"), {"month": MONTH}).status_code == 403


class TestWhatTheBellSays:
    WORDS = {
        "applied": "The re-plan of the ER roster for June 2026 has finished, and its roster is now in force as a new version. "
                   "The unit's nurses are told the roster has changed.",
        "unchanged": "The re-plan of the ER roster for June 2026 has finished without changing a cell, so the roster in force "
                     "stays as it was.",
        "outdated": "The re-plan of the ER roster for June 2026 has ended, but the roster in force or the month changed while it "
                    "ran, so it stays as it was. Re-plan again to take that in.",
        "no roster": "The re-plan of the ER roster for June 2026 has ended with no roster to keep, so the roster in force stays "
                     "as it was. Its result says why.",
    }

    def said(self, client, solve) -> dict:
        from notifications.tests.test_events import told
        [note] = [note for note in told(client, "solve_ended") if note["text"].startswith("The re-plan")]
        assert note["link"] == {"page": "roster", "month": MONTH, "unit_id": None, "cover_id": None}
        return note["text"]

    def test_applied(self, mona, er, solver, in_force):
        assert self.said(mona, replanned(mona, er, solver)) == self.WORDS["applied"]

    def test_unchanged(self, mona, er, solver, in_force):
        assert self.said(mona, replanned(mona, er, solver, answered(copy.deepcopy(IN_FORCE)))) == self.WORDS["unchanged"]

    def test_outdated(self, mona, er, solver, in_force):
        while_it_runs(solver, lambda: record_leave(mona, er, "Amal", 15))
        assert self.said(mona, replanned(mona, er, solver, *under_way(SWAPPED))) == self.WORDS["outdated"]

    def test_no_roster(self, mona, er, solver, in_force):
        assert self.said(mona, replanned(mona, er, solver, answered(SWAPPED, ok=False))) == self.WORDS["no roster"]

    def test_stopped_and_failed(self, mona, er, solver, in_force):
        from notifications.tests.test_events import told
        solver.script(RuntimeError("down"), RuntimeError("down"))
        running.run(started(mona, er)["id"])
        assert told(mona, "solve_ended")[0]["text"] == ("The re-plan of the ER roster for June 2026 failed, so the roster in "
                                                        "force stays as it was. Its result says why.")
