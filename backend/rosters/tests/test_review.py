"""Submitting, approving and rejecting a roster, what a frozen roster refuses, and what the CNO, the Deputy CNO and a nurse read
of it. The API with the scripted solver and the real worker's code. A submit and an approval check the grid with the solver's own
code, so the drafts here are a real roster of the four-nurse sample (`fixtures/sound_rosters.json`, solved once with the real
solver), not the scripted all-Day one, which breaks hard rules."""
import datetime as dt
import json
from pathlib import Path

import pytest

from accounts.models import Role, User
from audit.models import AuditEntry
from conftest import post
from hospital.models import HospitalSettings, LeaveType, PublicHoliday, Shift
from nurses import membership
from nurses.models import NurseProfile
from rosters.drafts import FROZEN
from rosters.models import PinEnded, PinnedCell, Roster, RosterState, RosterVersion, Submission
from rosters.tests.test_pins import change_shift, clear, off, pin, save, saved
from solves import running
from solves.models import Solve, SolveState
from solves.tests.conftest import MONTH, job, make_sample_unit, result_of, start_solve, started

pytestmark = pytest.mark.django_db

SOUND = json.loads((Path(__file__).parent / "fixtures" / "sound_rosters.json").read_text())
JULY = "2026-07"
FROZEN_STATES = [RosterState.SUBMITTED, RosterState.APPROVED]
# In SOUND's June: Badr is off on the 3rd and works Day on the 2nd. Amal on Night on the 9th keeps it sound; Day on the 5th,
# straight after a Night, does not.
BADR_OFF, BADR_WORKS = 3, 2
LATE = ("This month is over, so its roster cannot be approved. Reject it with a comment, so it no longer waits for a "
        "decision.")


@pytest.fixture(autouse=True)
def _scene(before_june, solver):
    pass


@pytest.fixture
def carla(client_of):
    return client_of("carla", Role.CNO, full_name="Carla CNO")


@pytest.fixture
def dina(client_of):
    return client_of("dina", Role.DEPUTY_CNO, full_name="Dina Deputy")


def url(unit, rest=""):
    return f"/api/units/{unit.pk}{rest}"


def sound(month=MONTH, prefix=""):
    found = SOUND[month]
    roster = {prefix + nurse: row for nurse, row in found["roster"].items()}
    return job("finished", result=result_of(roster, cost=found["cost"]) | {"month": month}, best_cost=found["cost"], improvements=3)


def solved(client, unit, solver, month=MONTH, prefix="") -> str:
    solver.script([sound(month, prefix)])
    solve = started(client, unit, month)
    running.run(solve["id"])
    return solve["id"]


def month_of(client, unit, month=MONTH) -> dict:
    response = client.get(url(unit, "/roster"), {"month": month})
    assert response.status_code == 200, response.content
    return response.json()


def submit(client, unit, month=MONTH):
    return post(client, url(unit, "/roster/submit"), {"month": month})


def approve(client, unit, month=MONTH):
    return post(client, url(unit, "/roster/approve"), {"month": month})


def reject(client, unit, comment="Too many nights for Amal.", month=MONTH):
    return post(client, url(unit, "/roster/reject"), {"month": month, "comment": comment})


def ok(response) -> dict:
    assert response.status_code == 200, response.content
    return response.json()


def refused(response, status=409) -> str:
    assert response.status_code == status, response.content
    return response.json()["detail"]


def submitted(client, unit, solver, month=MONTH) -> str:
    solve = solved(client, unit, solver, month)
    ok(submit(client, unit, month))
    return solve


def approved(client, approver, unit, solver, month=MONTH) -> str:
    solve = submitted(client, unit, solver, month)
    ok(approve(approver, unit, month))
    return solve


def profile(hospital_id) -> NurseProfile:
    return NurseProfile.objects.get(user__username=hospital_id)


def record_leave(client, unit, nurse, day, month=MONTH):
    return post(client, url(unit, "/leave"), {
        "nurse_id": str(profile(nurse).pk), "leave_type_id": str(LeaveType.objects.get(name="Sick").pk),
        "first": f"{month}-{day:02d}", "last": f"{month}-{day:02d}"})


def roster(unit, month=MONTH) -> Roster:
    return Roster.objects.get(unit=unit, month=dt.date.fromisoformat(f"{month}-01"))


def entries(action) -> list[AuditEntry]:
    return list(AuditEntry.objects.filter(action=action))


class TestSubmitting:
    def test_it_freezes_the_draft_and_says_who_and_when(self, mona, er, solver):
        solved(mona, er, solver)

        answer = ok(submit(mona, er))

        assert answer["state"] == "submitted"
        assert answer["submitted"]["submitted_by"] == "Mona Ali"
        assert answer["submitted"]["submitted_at"].endswith("+03:00")
        assert answer["submitted"]["new_since"] == []
        assert answer["submit_refusal"] == FROZEN[RosterState.SUBMITTED]
        assert answer["draft"]["editor"]["refusal"] == FROZEN[RosterState.SUBMITTED]
        assert month_of(mona, er) == answer
        assert roster(er).state == RosterState.SUBMITTED

    def test_it_does_not_move_when_the_grid_was_last_replaced(self, mona, er, solver):
        solved(mona, er, solver)
        before = roster(er).updated_at

        ok(submit(mona, er))

        assert roster(er).updated_at == before

    def test_a_month_with_no_draft_has_nothing_to_submit(self, mona, er):
        assert refused(submit(mona, er)) == "This month has no draft yet. Solve it first."
        assert month_of(mona, er)["submit_refusal"] == "This month has no roster yet."

    def test_a_submitted_one_cannot_be_submitted_again(self, mona, er, solver):
        submitted(mona, er, solver)

        assert refused(submit(mona, er)) == FROZEN[RosterState.SUBMITTED]
        assert Submission.objects.count() == 1

    def test_a_draft_out_of_date_goes_through_with_its_warning(self, mona, er, solver):
        """Warn, and block only if checking it again fails. Leave on a day off breaks nothing."""
        solved(mona, er, solver)
        assert record_leave(mona, er, "Badr", BADR_OFF).status_code == 201
        before = month_of(mona, er)
        assert [(c["hospital_id"], c["what"]) for c in before["draft"]["changes"]] == [("Badr", "leave")]
        assert before["submit_refusal"] is None

        answer = ok(submit(mona, er))

        assert answer["state"] == "submitted"
        assert [(c["hospital_id"], c["what"]) for c in answer["draft"]["changes"]] == [("Badr", "leave")]
        assert answer["submitted"]["new_since"] == [], "the manager was warned of this one"

    def test_a_draft_out_of_date_that_now_breaks_a_hard_rule_is_refused(self, mona, er, solver):
        solved(mona, er, solver)
        assert record_leave(mona, er, "Badr", BADR_WORKS).status_code == 201
        month = month_of(mona, er)
        assert month["draft"]["check"]["ok"] is False
        assert month["submit_refusal"].startswith("This draft breaks 1 hard rule on the month as it stands")

        assert refused(submit(mona, er)) == month["submit_refusal"]
        assert roster(er).state == RosterState.DRAFT

    def test_a_draft_edited_by_hand_goes_through_if_it_breaks_nothing(self, mona, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 9, "N"))

        assert ok(submit(mona, er))["state"] == "submitted"

    def test_a_pin_breaking_a_hard_rule_saves_but_cannot_be_submitted(self, mona, er, solver):
        """Blocking that is left to the submit."""
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 5, "D"))

        assert "hard rule" in refused(submit(mona, er))

    def test_a_solve_under_way_refuses_it_so_no_solve_ends_on_a_frozen_grid(self, mona, er, solver):
        solved(mona, er, solver)
        under_way = started(mona, er)

        assert refused(submit(mona, er)).startswith("A solve of this month is queued or running")
        assert month_of(mona, er)["submit_refusal"].startswith("A solve of this month is queued or running")

        ok(post(mona, url(er, f"/solves/{under_way['id']}/stop"), {}))
        assert ok(submit(mona, er))["state"] == "submitted"

    def test_a_month_that_is_over_cannot_be_submitted(self, mona, er, solver, monkeypatch):
        solved(mona, er, solver)
        monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 7, 2))

        assert refused(submit(mona, er)) == "This month is over, so its draft cannot be submitted."

    def test_another_units_manager_cannot(self, mona, er, solver, client_of):
        solved(mona, er, solver)
        omar = client_of("omar", Role.UNIT_MANAGER)

        assert refused(submit(omar, er), 403) == "This is not your unit."

    @pytest.mark.parametrize("role", [Role.CNO, Role.DEPUTY_CNO, Role.STAFF_NURSE, Role.SYSTEM_ADMIN])
    def test_only_a_manager_may(self, role, mona, er, solver, client_of):
        solved(mona, er, solver)

        assert submit(client_of("someone", role), er).status_code == 403
        assert roster(er).state == RosterState.DRAFT

    def test_it_is_audited_with_the_unit_and_month_only(self, mona, er, solver):
        submitted(mona, er, solver)

        [entry] = entries("roster.submitted")
        assert (entry.actor.username, entry.target, entry.before, entry.after) == ("mona", "ER, 2026-06", None, None)


class TestAFrozenRoster:
    """Every path that writes `Roster.grid` or its pins refuses a submitted or an approved roster, in `rosters.drafts.why_frozen`."""

    def freeze(self, mona, carla, er, solver, state) -> str:
        return approved(mona, carla, er, solver) if state == RosterState.APPROVED else submitted(mona, er, solver)

    @pytest.mark.parametrize("state", FROZEN_STATES)
    def test_it_cannot_be_solved(self, state, mona, carla, er, solver):
        self.freeze(mona, carla, er, solver, state)

        assert refused(start_solve(mona, er)) == FROZEN[state]
        assert ok(mona.get(url(er, "/solves/preview"), {"month": MONTH}))["refusal"] == FROZEN[state]
        assert Solve.objects.count() == 1

    @pytest.mark.parametrize("state", FROZEN_STATES)
    def test_a_solve_ending_on_it_leaves_its_grid_alone(self, state, mona, carla, er, solver):
        """Submitting refuses while a solve is active, so only a state set behind the API's back reaches the end of one."""
        self.freeze(mona, carla, er, solver, state)
        grid = roster(er).grid
        Roster.objects.filter(pk=roster(er).pk).update(state=RosterState.DRAFT)
        solver.script([job("finished", result=result_of({nurse: dict.fromkeys(row, "N") for nurse, row in grid.items()}))])
        late = started(mona, er)
        Roster.objects.filter(pk=roster(er).pk).update(state=state)

        running.run(late["id"])

        solve = Solve.objects.get(pk=late["id"])
        assert solve.state == SolveState.SUCCEEDED and solve.result is not None
        assert roster(er).grid == grid
        assert str(roster(er).solve_id) != late["id"]
        assert entries("roster.draft_replaced")[-1].after["solve"] != late["id"]

    @pytest.mark.parametrize("state", FROZEN_STATES)
    def test_an_older_result_cannot_be_restored_onto_it(self, state, mona, carla, er, solver):
        first = solved(mona, er, solver)
        solved(mona, er, solver)
        Roster.objects.filter(pk=roster(er).pk).update(state=state)

        assert refused(post(mona, url(er, f"/solve-history/{first}/restore"), {})) == FROZEN[state]
        assert ok(mona.get(url(er, f"/solve-history/{first}")))["restore_refusal"] == FROZEN[state]

    @pytest.mark.parametrize("state", [RosterState.SUBMITTED])
    def test_no_pin_can_be_saved_or_checked(self, state, mona, carla, er, solver):
        """An approved roster is edited through versions: `test_versions.py`."""
        self.freeze(mona, carla, er, solver, state)
        grid = roster(er).grid

        assert refused(save(mona, er, pin("Amal", 9, "N"))) == FROZEN[state]
        assert refused(post(mona, url(er, "/roster/check"), {"month": MONTH, "edits": [pin("Amal", 9, "N")]})) == FROZEN[state]
        assert month_of(mona, er)["draft"]["editor"]["refusal"] == FROZEN[state]
        assert roster(er).grid == grid and not PinnedCell.objects.exists()

    @pytest.mark.parametrize("state", FROZEN_STATES)
    def test_a_rename_still_follows_it_and_a_version_keeps_its_code(self, state, mona, carla, er, solver):
        """A rename is the same shift: nothing the approver decides on changes, and the hours guard reads `Roster.grid`."""
        self.freeze(mona, carla, er, solver, state)

        assert change_shift(carla, "N", code="NT").status_code == 200

        held = roster(er).grid
        assert "N" not in {code for row in held.values() for code in row.values()}
        assert held["Amal"]["2026-06-01"] == "NT"
        assert roster(er).state == state
        if state == RosterState.APPROVED:
            assert RosterVersion.objects.get().grid["Amal"]["2026-06-01"] == "N"

    def test_leave_can_still_be_recorded_and_ends_a_pin_there(self, mona, er, solver):
        """Allowed, and the approver is told. Leave wins over a pin, as before."""
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 9, "N"))
        ok(submit(mona, er))

        response = record_leave(mona, er, "Amal", 9)

        assert response.status_code == 201, response.content
        assert response.json()["pins_removed"] == [{"date": "2026-06-09", "shift": "N", "origin": "manager", "roster": "submitted"}]
        assert PinnedCell.objects.get().removed_why == PinEnded.LEAVE
        assert roster(er).grid["Amal"]["2026-06-09"] == "N", "the frozen grid keeps the shift"
        assert roster(er).state == RosterState.SUBMITTED


class TestApproving:
    @pytest.mark.parametrize("approver", ["carla", "dina"])
    def test_the_cno_or_the_deputy_approves_and_it_becomes_version_1_from_its_solve(self, approver, request, mona, er, solver):
        solve = submitted(mona, er, solver)

        answer = ok(approve(request.getfixturevalue(approver), er))

        assert answer["state"] == "approved"
        assert answer["approved"]["approved_by"] == {"carla": "Carla CNO", "dina": "Dina Deputy"}[approver]
        assert answer["approved"]["version"] == 1
        version = RosterVersion.objects.get()
        assert (version.number, version.origin, str(version.solve_id), version.created_by.username) == (1, "solve", solve, approver)
        assert version.grid == roster(er).grid
        assert answer["approve_refusal"] == "This roster has been approved already by " + answer["approved"]["approved_by"] + "."

    def test_version_1_holds_the_grid_as_approved_with_its_hand_edits(self, mona, carla, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 9, "N"))
        ok(submit(mona, er))

        ok(approve(carla, er))

        version = RosterVersion.objects.get()
        assert version.grid["Amal"]["2026-06-09"] == "N"
        assert SOUND[MONTH]["roster"]["Amal"]["2026-06-09"] == ""
        assert version.origin == "solve"

    def test_two_approvers_at_once_the_second_is_told_the_first_decided(self, mona, carla, dina, er, solver):
        submitted(mona, er, solver)
        ok(approve(carla, er))

        assert refused(approve(dina, er)) == "This roster has been approved already by Carla CNO."
        assert refused(reject(dina, er)) == "This roster has been approved already by Carla CNO."
        assert RosterVersion.objects.count() == 1
        assert Submission.objects.get().decided_by.username == "carla"

    def test_a_draft_is_not_waiting_for_a_decision(self, mona, carla, er, solver):
        solved(mona, er, solver)

        assert refused(approve(carla, er)) == "This month has no roster waiting for a decision."
        assert refused(reject(carla, er)) == "This month has no roster waiting for a decision."
        assert refused(approve(carla, er, JULY)) == "This month has no roster waiting for a decision."

    @pytest.mark.parametrize("role", [Role.UNIT_MANAGER, Role.STAFF_NURSE, Role.SYSTEM_ADMIN])
    def test_no_one_else_may_approve_or_reject(self, role, mona, er, solver, client_of):
        submitted(mona, er, solver)
        someone = client_of("someone", role)

        assert approve(someone, er).status_code == 403
        assert reject(someone, er).status_code == 403
        assert approve(mona, er).status_code == 403, "the unit's own manager neither: there is no approving one's own"
        assert roster(er).state == RosterState.SUBMITTED

    def test_leave_on_a_day_off_while_it_waits_is_shown_and_does_not_stop_it(self, mona, carla, er, solver):
        submitted(mona, er, solver)
        assert record_leave(mona, er, "Badr", BADR_OFF).status_code == 201

        seen = month_of(carla, er)
        assert [(c["hospital_id"], c["what"]) for c in seen["submitted"]["new_since"]] == [("Badr", "leave")]
        assert seen["approve_refusal"] is None
        assert ok(carla.get("/api/rosters/submitted"))[0]["changed_since"] is True

        assert ok(approve(carla, er))["state"] == "approved"

    def test_more_leave_for_a_nurse_the_manager_was_warned_of_is_still_news(self, mona, carla, er, solver):
        """Found driving the page: comparing only whose and what kind of change hid a second leave of the same nurse."""
        solved(mona, er, solver)
        assert record_leave(mona, er, "Badr", BADR_OFF).status_code == 201
        ok(submit(mona, er))
        assert month_of(carla, er)["submitted"]["new_since"] == []

        assert record_leave(mona, er, "Badr", BADR_WORKS).status_code == 201

        assert [(c["hospital_id"], c["what"]) for c in month_of(carla, er)["submitted"]["new_since"]] == [("Badr", "leave")]
        assert ok(carla.get("/api/rosters/submitted"))[0]["changed_since"] is True

    def test_the_month_kept_at_the_submit_holds_no_leave_type(self, mona, er, solver):
        solved(mona, er, solver)
        assert record_leave(mona, er, "Badr", BADR_OFF).status_code == 201
        ok(submit(mona, er))

        kept = Submission.objects.get().month_at_submit
        assert {key for row in kept for key in row} <= {"id", "gender", "leave", "pinned", "skills"}
        assert "Sick" not in json.dumps(kept)

    def test_leave_on_a_worked_day_while_it_waits_stops_it_until_rejected_and_put_right(self, mona, carla, er, solver):
        submitted(mona, er, solver)
        assert record_leave(mona, er, "Badr", BADR_WORKS).status_code == 201

        seen = month_of(carla, er)
        assert seen["draft"]["check"]["ok"] is False
        assert [v["hospital_id"] for v in seen["draft"]["check"]["violations"]] == ["Badr"]
        assert seen["approve_refusal"].startswith("This roster breaks 1 hard rule on the month as it stands")
        assert refused(approve(carla, er)) == seen["approve_refusal"]
        assert roster(er).state == RosterState.SUBMITTED and not RosterVersion.objects.exists()

        ok(reject(carla, er, "Badr is on leave on 2 June."))
        saved(mona, er, clear("Badr", BADR_WORKS), pin("Dana", BADR_WORKS, "D"))
        ok(submit(mona, er))
        assert ok(approve(carla, er))["state"] == "approved"
        assert RosterVersion.objects.get().grid["Badr"][f"{MONTH}-{BADR_WORKS:02d}"] == ""

    def test_a_month_that_is_over_is_refused_and_nothing_changes_but_it_can_still_be_rejected(self, mona, carla, er, solver,
                                                                                               monkeypatch):
        """Refused, as submitting refuses it: approved late, it would block every later close of the unit for good."""
        from notifications.models import Notification
        submitted(mona, er, solver)
        monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 7, 3))
        told = Notification.objects.count()

        seen = month_of(carla, er)
        assert seen["approve_refusal"] == LATE
        assert refused(approve(carla, er)) == LATE
        assert roster(er).state == RosterState.SUBMITTED and not RosterVersion.objects.exists()
        assert Notification.objects.count() == told and not entries("roster.approved")
        assert seen["reject_refusal"] is None

        assert ok(reject(carla, er, "Too late to approve."))["state"] == "draft"

    def test_a_pin_leave_ended_while_it_waits_is_shown_to_the_approver(self, mona, carla, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 9, "N"))
        ok(submit(mona, er))
        record_leave(mona, er, "Amal", 9)

        seen = month_of(carla, er)

        assert [(p["hospital_id"], p["date"], p["shift"]) for p in seen["draft"]["pins_lost_to_leave"]] == [("Amal", "2026-06-09", "N")]
        assert seen["approve_refusal"] is not None

    def test_it_is_audited_with_the_version_and_nothing_of_a_nurse(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)

        [entry] = entries("roster.approved")
        assert (entry.actor.username, entry.target, entry.before, entry.after) == ("carla", "ER, 2026-06", None, {"version": 1})

    def test_the_approval_takes_the_months_nurses_lock_before_the_units(self, mona, carla, er, solver, monkeypatch):
        """The established order, catalogue, then the user and the profile, then the unit, so that leave, which takes the
        nurse's lock, cannot land between the check and the approval."""
        submitted(mona, er, solver)
        taken = []
        from rosters import api, review
        real_nurses, real_unit = review.lock_the_months_nurses, api.unit_in_scope
        monkeypatch.setattr(review, "lock_the_months_nurses", lambda *a: (taken.append("nurses"), real_nurses(*a))[1])
        monkeypatch.setattr(api, "unit_in_scope", lambda *a, **k: (taken.append("unit" if k.get("for_update") else "read"),
                                                                     real_unit(*a, **k))[1])
        real_settings = HospitalSettings.load
        monkeypatch.setattr(HospitalSettings, "load", lambda **k: (taken.append("catalogue") if k.get("for_update") else None,
                                                                    real_settings(**k))[1])

        ok(approve(carla, er))

        assert [step for step in taken if step != "read"][:3] == ["catalogue", "nurses", "unit"]


class TestRejecting:
    def test_it_returns_to_draft_with_the_comment_for_the_manager(self, mona, dina, er, solver):
        submitted(mona, er, solver)

        ok(reject(dina, er, "Too many nights for Amal."))

        month = month_of(mona, er)
        assert month["state"] == "draft"
        assert month["submitted"] is None
        assert month["last_rejection"]["comment"] == "Too many nights for Amal."
        assert month["last_rejection"]["rejected_by"] == "Dina Deputy"
        assert month["draft"]["editor"]["refusal"] is None
        assert month["submit_refusal"] is None

    def test_the_comment_is_kept_through_the_next_submit_and_every_one_stays(self, mona, dina, carla, er, solver):
        submitted(mona, er, solver)
        ok(reject(dina, er, "First."))
        ok(submit(mona, er))
        assert month_of(mona, er)["last_rejection"]["comment"] == "First."
        assert month_of(carla, er)["last_rejection"]["comment"] == "First."
        ok(reject(carla, er, "Second."))

        assert month_of(mona, er)["last_rejection"]["comment"] == "Second."
        assert [(row.decision, row.comment) for row in Submission.objects.order_by("submitted_at")] == [
            ("rejected", "First."), ("rejected", "Second.")]

    def test_the_manager_can_edit_and_solve_again_after_it(self, mona, dina, er, solver):
        submitted(mona, er, solver)
        ok(reject(dina, er))

        saved(mona, er, pin("Amal", 9, "N"))
        assert start_solve(mona, er).status_code == 201

    @pytest.mark.parametrize("comment", ["", "   "])
    def test_it_needs_a_comment(self, comment, mona, dina, er, solver):
        submitted(mona, er, solver)

        assert reject(dina, er, comment).status_code == 422
        assert roster(er).state == RosterState.SUBMITTED

    def test_the_audit_entry_holds_no_comment(self, mona, dina, er, solver):
        submitted(mona, er, solver)

        ok(reject(dina, er, "Badr is on sick leave on the 2nd."))

        [entry] = entries("roster.rejected")
        assert (entry.actor.username, entry.target, entry.before, entry.after) == ("dina", "ER, 2026-06", None, None)
        logged = json.dumps([[e.target, e.before, e.after] for e in AuditEntry.objects.all()])
        assert "sick" not in logged and "Badr" not in logged


class TestWhatTheApproversRead:
    def test_a_submitted_roster_with_everything_but_the_managers_tools(self, mona, carla, er, solver):
        solve = submitted(mona, er, solver)

        seen = month_of(carla, er)

        assert seen["state"] == "submitted"
        assert seen["draft"]["solve_id"] == solve
        assert {row["hospital_id"] for row in seen["draft"]["rows"]} == {"Amal", "Badr", "Dana", "Emad"}
        assert seen["draft"]["editor"]["refusal"] == "Only the unit's manager changes its roster or submits it."
        assert seen["submit_refusal"] == "Only the unit's manager changes its roster or submits it."
        assert seen["approve_refusal"] is None and seen["reject_refusal"] is None
        assert seen["active_solve"] is None and seen["last_solve"] is None

    def test_a_draft_only_as_one_that_exists(self, mona, dina, er, solver):
        """A draft: nobody outside the unit's management sees one."""
        solved(mona, er, solver)

        seen = month_of(dina, er)

        assert (seen["state"], seen["draft"], seen["active_solve"], seen["last_solve"]) == ("draft", None, None, None)
        assert seen["approve_refusal"] == "This month has no roster waiting for a decision."

    def test_a_draft_after_a_rejection_goes_back_out_of_sight(self, mona, dina, er, solver):
        submitted(mona, er, solver)
        ok(reject(dina, er, "No."))

        seen = month_of(dina, er)

        assert seen["draft"] is None
        assert seen["last_rejection"] is None, "a draft's month shows them only that a draft exists"

    def test_any_unit_and_a_404_for_one_that_does_not_exist(self, mona, carla, er, solver, make_user, client_of):
        omar = client_of("omar", Role.UNIT_MANAGER)
        icu = make_sample_unit("ICU", User.objects.get(username="omar"), make_user, prefix="I")
        solved(omar, icu, solver, prefix="I")
        ok(submit(omar, icu))

        assert month_of(carla, icu)["state"] == "submitted"
        assert carla.get("/api/units/01a1ad3a-c842-7d62-82fc-84b9d422d785/roster", {"month": MONTH}).status_code == 404
        assert omar.get(url(er, "/roster"), {"month": MONTH}).status_code == 403

    @pytest.mark.parametrize("role", [Role.SYSTEM_ADMIN, Role.STAFF_NURSE])
    def test_no_roster_content_for_the_system_admin_or_a_nurse_here(self, role, mona, carla, er, solver, client_of):
        approved(mona, carla, er, solver)
        someone = client_of("someone", role)

        for path in (url(er, "/roster"), "/api/rosters/overview", "/api/rosters/submitted", url(er, "/solve-history")):
            assert someone.get(path, {"month": MONTH}).status_code == 403, path

    def test_the_overview_has_every_units_state_per_month(self, mona, carla, er, solver, make_user, client_of):
        omar = client_of("omar", Role.UNIT_MANAGER)
        icu = make_sample_unit("ICU", User.objects.get(username="omar"), make_user, prefix="I")
        approved(mona, carla, er, solver)
        solved(mona, er, solver, JULY)
        solved(omar, icu, solver, prefix="I")
        ok(submit(omar, icu))

        overview = ok(carla.get("/api/rosters/overview", {"month": "2026-05"}))

        assert overview["months"] == ["2026-05", "2026-06", "2026-07", "2026-08"]
        assert [(unit["name"], unit["states"]) for unit in overview["units"]] == [
            ("ER", ["none", "approved", "draft", "none"]), ("ICU", ["none", "submitted", "none", "none"])]
        assert ok(carla.get("/api/rosters/overview"))["months"][0] == "2026-05", "the hospital's month by default"

    def test_the_waiting_list_is_oldest_first(self, mona, dina, er, solver, make_user, client_of):
        omar = client_of("omar", Role.UNIT_MANAGER)
        icu = make_sample_unit("ICU", User.objects.get(username="omar"), make_user, prefix="I")
        solved(omar, icu, solver, prefix="I")
        ok(submit(omar, icu))
        submitted(mona, er, solver)
        solved(mona, er, solver, JULY)

        waiting = ok(dina.get("/api/rosters/submitted"))

        assert [(row["unit"], row["month"], row["changed_since"]) for row in waiting] == [("ICU", MONTH, False), ("ER", MONTH, False)]


def mine(client, month=MONTH) -> dict:
    return ok(client.get("/api/rosters/mine", {"month": month}))


@pytest.fixture
def nurses_of_er(er, client_of):
    """Sign in as the sample's nurses, whose accounts the scene made."""
    from django.test import Client

    from conftest import PASSWORD

    def sign_in(hospital_id):
        client = Client()
        assert post(client, "/api/auth/login", {"username": hospital_id, "password": PASSWORD}).status_code == 200
        return client
    return sign_in


class TestTheNursesView:
    def test_no_roster_until_it_is_approved(self, mona, carla, er, solver, nurses_of_er):
        amal = nurses_of_er("Amal")
        assert mine(amal) == {"month": MONTH, "first_month": "2026-05", "unit": "ER", "roster": None}
        submitted(mona, er, solver)
        assert mine(amal)["roster"] is None

        ok(approve(carla, er))

        assert mine(amal)["roster"]["version"] == 1

    def test_names_and_shifts_only(self, mona, carla, er, solver, nurses_of_er):
        approved(mona, carla, er, solver)

        seen = mine(nurses_of_er("Amal"))["roster"]

        assert set(seen) == {"approved_at", "version", "days", "shifts", "rows", "mine"}
        assert [set(row) for row in seen["rows"]] == [{"hospital_id", "full_name", "on_probation", "preceptor_name", "shifts"}] * 4
        assert len(seen["days"]) == 30
        badr = next(row for row in seen["rows"] if row["hospital_id"] == "Badr")
        assert badr["shifts"] == [SOUND[MONTH]["roster"]["Badr"][f"{MONTH}-{day:02d}"] for day in range(1, 31)]
        assert {shift["code"] for shift in seen["shifts"]} == {"D", "N"}

    def test_a_colleagues_leave_day_reads_exactly_as_a_day_off_and_their_own_does_not(self, mona, carla, er, solver,
                                                                                      nurses_of_er):
        approved(mona, carla, er, solver)
        amal, badr = nurses_of_er("Amal"), nurses_of_er("Badr")
        before_amal, before_badr = mine(amal), mine(badr)

        assert record_leave(mona, er, "Badr", BADR_OFF).status_code == 201

        assert mine(amal) == before_amal
        after_badr = mine(badr)
        assert after_badr != before_badr
        assert before_badr["roster"]["mine"][BADR_OFF - 1] == {"date": f"{MONTH}-03", "kind": "off"}
        assert after_badr["roster"]["mine"][BADR_OFF - 1] == {"date": f"{MONTH}-03", "kind": "leave"}
        assert after_badr["roster"]["rows"] == before_badr["roster"]["rows"]

    def test_leave_the_solve_was_given_reads_as_a_day_off_to_a_colleague_too(self, mona, carla, er, solver, nurses_of_er):
        """Leave before the solve is in the stored input's `nurses[].leave` and the result's `nurses`: none of it reaches here."""
        assert record_leave(mona, er, "Badr", BADR_OFF).status_code == 201
        approved(mona, carla, er, solver)

        seen = mine(nurses_of_er("Amal"))
        text = json.dumps(seen)

        assert "leave" not in text
        badr = next(row for row in seen["roster"]["rows"] if row["hospital_id"] == "Badr")
        assert badr["shifts"][BADR_OFF - 1] == ""

    def test_another_units_roster_is_never_theirs(self, mona, carla, er, solver, make_user, client_of, nurses_of_er):
        omar = client_of("omar", Role.UNIT_MANAGER)
        icu = make_sample_unit("ICU", User.objects.get(username="omar"), make_user, prefix="I")
        solved(omar, icu, solver, prefix="I")
        ok(submit(omar, icu))
        ok(approve(carla, icu))

        assert mine(nurses_of_er("Amal"))["roster"] is None

    def test_across_a_transfer_each_month_is_its_own_units(self, mona, carla, er, solver, make_user, client_of, nurses_of_er):
        omar = client_of("omar", Role.UNIT_MANAGER)
        icu = make_sample_unit("ICU", User.objects.get(username="omar"), make_user, prefix="I")
        membership.transfer(profile("Dana"), icu, dt.date(2026, 7, 1))
        approved(mona, carla, er, solver)
        solver.script([sound(JULY, "I")])
        running.run(started(omar, icu, JULY)["id"])
        ok(submit(omar, icu, JULY))
        ok(approve(carla, icu, JULY))
        dana = nurses_of_er("Dana")

        june, july = mine(dana), mine(dana, JULY)

        assert (june["unit"], july["unit"]) == ("ER", "ICU")
        assert june["roster"]["mine"] is not None
        assert {row["hospital_id"] for row in july["roster"]["rows"]} == {"IAmal", "IBadr", "IDana", "IEmad"}
        assert july["roster"]["mine"] is None, "solved before they arrived, so not on it"

    def test_a_nurse_due_to_join_sees_their_units_roster_with_no_row(self, mona, carla, er, solver, make_user, nurses_of_er):
        from solves.tests.conftest import nurse_in
        nurse_in(er, make_user, "Farah", start=dt.date(2026, 8, 1))
        approved(mona, carla, er, solver)

        seen = mine(nurses_of_er("Farah"))

        assert seen["unit"] == "ER" and seen["roster"]["mine"] is None

    def test_days_before_a_start_date_are_their_own_outside_days_and_a_colleagues_empty_ones(self, mona, carla, er, solver,
                                                                                              nurses_of_er):
        """A start date corrected to the 10th after the approval: the roster keeps its cells, the nurse sees those days as outside
        their dates, and a colleague sees nothing but the cells."""
        approved(mona, carla, er, solver)
        amal = nurses_of_er("Amal")
        before = mine(amal)
        NurseProfile.objects.filter(user__username="Badr").update(start_date=dt.date(2026, 6, 10))

        assert mine(amal) == before
        own = mine(nurses_of_er("Badr"))["roster"]["mine"]
        assert {day["kind"] for day in own[:9] if SOUND[MONTH]["roster"]["Badr"][day["date"]] == ""} == {"outside"}
        assert own[9]["kind"] in ("shift", "off")

    def test_a_deactivated_colleague_still_reads_as_a_name_and_shifts(self, mona, carla, er, solver, nurses_of_er):
        approved(mona, carla, er, solver)
        amal = nurses_of_er("Amal")
        before = mine(amal)
        User.objects.filter(username="Badr").update(is_active=False)

        assert mine(amal) == before

    def test_a_deactivated_nurse_cannot_sign_in_to_read_it(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        User.objects.filter(username="Badr").update(is_active=False)
        from django.test import Client

        from conftest import PASSWORD
        assert post(Client(), "/api/auth/login", {"username": "Badr", "password": PASSWORD}).status_code != 200

    def test_a_manager_who_is_a_nurse_sees_their_units_and_one_who_is_not_sees_none(self, mona, carla, er, solver):
        from nurses.models import Placement
        nurse = NurseProfile.objects.create(user=User.objects.get(username="mona"), gender="F", start_date=dt.date(2026, 1, 1))
        Placement.objects.create(nurse=nurse, unit=er, from_month=dt.date(2026, 1, 1))
        approved(mona, carla, er, solver)

        seen = mine(mona)
        assert seen["unit"] == "ER" and seen["roster"] is not None and seen["roster"]["mine"] is None

        nurse.placements.all().delete()
        nurse.delete()
        assert mine(mona) == {"month": MONTH, "first_month": "2026-05", "unit": None, "roster": None}

    def test_a_manager_whose_profile_ended_reads_no_later_month_of_the_unit_they_left(self, mona, carla, er, solver, client_of):
        """The end date is the last day a nurse is part of her unit, whatever her role. A manager's account outlives the profile
        by design (`nurses.api.set_end_date`), so the profile's dates decide, not the account."""
        from nurses.models import Placement
        from units.models import Unit
        nurse = NurseProfile.objects.create(user=User.objects.get(username="mona"), gender="F", start_date=dt.date(2026, 1, 1),
                                            end_date=dt.date(2026, 5, 10))
        Placement.objects.create(nurse=nurse, unit=er, from_month=dt.date(2026, 1, 1))
        approved(mona, carla, er, solver)
        client_of("omar", Role.UNIT_MANAGER)
        Unit.objects.filter(pk=er.pk).update(manager=User.objects.get(username="omar"))

        assert mine(mona) == {"month": MONTH, "first_month": "2026-05", "unit": None, "roster": None}
        assert ok(mona.get("/api/rosters/mine/sheet", {"month": MONTH}))["unit"] is None
        assert ok(mona.get("/api/rosters/mine/month", {"month": MONTH}))["roster"] is None

    def test_a_manager_whose_profile_runs_on_still_reads_the_unit_they_left(self, mona, carla, er, solver, client_of):
        from nurses.models import Placement
        from units.models import Unit
        nurse = NurseProfile.objects.create(user=User.objects.get(username="mona"), gender="F", start_date=dt.date(2026, 1, 1))
        Placement.objects.create(nurse=nurse, unit=er, from_month=dt.date(2026, 1, 1))
        approved(mona, carla, er, solver)
        client_of("omar", Role.UNIT_MANAGER)
        Unit.objects.filter(pk=er.pk).update(manager=User.objects.get(username="omar"))

        seen = mine(mona)
        assert seen["unit"] == "ER" and seen["roster"] is not None

    def test_a_staff_nurse_reads_the_month_her_end_date_falls_in_and_no_unit_after_it(self, mona, carla, er, solver,
                                                                                       nurses_of_er):
        """Her account is active until the date passes (`nurses.tasks.close_ended_nurses`); a month that begins after it falls
        under no unit, so she reads no roster of it and the swap composer offers it to her as no unit's."""
        approved(mona, carla, er, solver)
        amal = nurses_of_er("Amal")
        NurseProfile.objects.filter(user__username="Amal").update(end_date=dt.date(2026, 6, 20))

        june, july = mine(amal), mine(amal, JULY)

        assert june["unit"] == "ER" and june["roster"]["mine"] is not None
        assert (july["unit"], july["roster"]) == (None, None)
        assert ok(amal.get("/api/swaps/compose", {"month": JULY}))["refusal"] == "You are on no unit's roster for July 2026."

    def test_a_month_already_over_is_not_shown(self, mona, carla, er, solver, monkeypatch, nurses_of_er):
        approved(mona, carla, er, solver)
        amal = nurses_of_er("Amal")
        monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 7, 2))

        assert mine(amal)["roster"] is None
        assert mine(amal)["first_month"] == JULY

    def test_a_rename_reads_in_todays_code(self, mona, carla, er, solver, nurses_of_er):
        approved(mona, carla, er, solver)
        assert change_shift(carla, "N", code="NT").status_code == 200

        seen = mine(nurses_of_er("Amal"))["roster"]

        assert {shift["code"] for shift in seen["shifts"]} == {"D", "NT"}
        assert seen["rows"][0]["shifts"][0] == "NT"
        assert Shift.objects.filter(code="NT").exists()


def holding(hospital_id, name, expiry=None, *, required=False, expires=True):
    from hospital.models import Skill
    skill = Skill.objects.filter(name=name).first() or Skill.objects.create(name=name, expires=expires,
                                                                            required_to_work=required)
    nurse = profile(hospital_id)
    return nurse.skills.create(skill=skill, expiry=expiry, recorded_by=nurse.user)


def days_worked(hospital_id, month=MONTH) -> list[str]:
    return sorted(day for day, code in SOUND[month]["roster"][hospital_id].items() if code)


class TestSkills:
    """Submit and approval refuse a grid that breaks a hard skill need or fills a barred day on the month as it stands, as for
    any hard rule."""

    def test_a_required_skill_expired_since_the_solve_refuses_the_submit_naming_her_days(self, mona, er, solver):
        solved(mona, er, solver)
        holding("Amal", "BLS", dt.date(2026, 6, 20), required=True)

        month = month_of(mona, er)

        assert {"hospital_id": "Amal", "full_name": "Amal", "what": "skills"} in month["draft"]["changes"]
        after = [day for day in days_worked("Amal") if day > "2026-06-20"]
        expired = [v for v in month["draft"]["check"]["violations"] if v["rule"] == "skill_expired"]
        assert [v["date"] for v in expired] == after
        assert expired[0]["text"] == (f"skill expired: Amal works {SOUND[MONTH]['roster']['Amal'][after[0]]} on {after[0]}, "
                                      "BLS is required to work and its last day was 2026-06-20")
        assert month["submit_refusal"].startswith(f"This draft breaks {len(after)} hard rules on the month as it stands")
        assert refused(submit(mona, er)) == month["submit_refusal"]

    def test_a_hard_skill_need_no_one_meets_refuses_the_submit_and_a_soft_one_does_not(self, mona, er, solver):
        solved(mona, er, solver)
        er.rules |= {"skill_needs": [{"shift": "N", "skill": "ICU", "weekday_min": 1, "weekend_min": 0}]}
        er.save()
        holding("Amal", "ICU", None, expires=False)

        month = month_of(mona, er)

        assert {"hospital_id": "", "full_name": "", "what": "skills"} in month["draft"]["changes"]
        short = [v for v in month["draft"]["check"]["violations"] if v["rule"] == "skill_need"]
        assert short and all(v["hospital_id"] is None and v["text"].startswith(f"skill need: {v['date']} N has ") for v in short)
        assert "hard rule" in refused(submit(mona, er))

        er.rules |= {"skill_needs_hard": False}
        er.save()
        assert ok(submit(mona, er))["state"] == "submitted"

    def test_an_approved_roster_is_not_rewritten_when_an_expiry_passes_and_its_page_shows_the_rule_it_now_breaks(
            self, mona, carla, er, solver):
        """What the grid breaks on the month as it stands, as for any other rule; the manager edits or re-plans."""
        approved(mona, carla, er, solver)
        grid = roster(er).grid
        holding("Amal", "BLS", dt.date(2026, 6, 20), required=True)

        month = month_of(mona, er)

        assert month["state"] == "approved" and roster(er).grid == grid
        after = [day for day in days_worked("Amal") if day > "2026-06-20"]
        expired = [v for v in month["draft"]["check"]["violations"] if v["rule"] == "skill_expired"]
        assert after and [(v["hospital_id"], v["date"]) for v in expired] == [("Amal", day) for day in after]
        assert expired[0]["text"] == (f"skill expired: Amal works {SOUND[MONTH]['roster']['Amal'][after[0]]} on {after[0]}, "
                                      "BLS is required to work and its last day was 2026-06-20")
        assert month["draft"]["check"]["ok"] is False

    def test_a_skill_expiring_while_it_waits_is_news_and_stops_the_approval(self, mona, carla, er, solver):
        submitted(mona, er, solver)
        holding("Badr", "BLS", dt.date(2026, 6, 1), required=True)

        seen = month_of(carla, er)

        assert {"hospital_id": "Badr", "full_name": "Badr", "what": "skills"} in seen["submitted"]["new_since"]
        assert seen["approve_refusal"].startswith("This roster breaks")
        assert refused(approve(carla, er)) == seen["approve_refusal"]
        assert roster(er).state == RosterState.SUBMITTED


def fix_ramadan(cno, month, *days):
    """The CNO fixes the month's Ramadan days to these, through the per-month fix."""
    response = cno.put(f"/api/hospital/ramadan/{month}", {"dates": [f"{month}-{day:02d}" for day in days]},
                       content_type="application/json")
    assert response.status_code == 200, response.content


def add_holiday(cno, month, day, name="A holiday") -> str:
    response = post(cno, "/api/hospital/public-holidays", {"date": f"{month}-{day:02d}", "name": name})
    assert response.status_code == 201, response.content
    return response.json()["id"]


def month_level(changes) -> list[str]:
    return [change["what"] for change in changes if not change["hospital_id"]]


class TestTheMonthsDates:
    """The month's Ramadan days and public holidays join what a draft is warned of since its solve, and a submitted roster since
    its submit: each the list a solve or the submit was given against the month as it stands, with no nurse. Nothing
    blocks. June 2026 has no Ramadan day by the calendar; the 13th is a Saturday, on which a holiday changes no headcount."""

    def test_a_draft_is_warned_after_a_ramadan_change_and_still_submits(self, mona, carla, er, solver):
        solved(mona, er, solver)
        fix_ramadan(carla, MONTH, 13, 14)

        month = month_of(mona, er)

        assert month_level(month["draft"]["changes"]) == ["ramadan_days"]
        assert {"hospital_id": "", "full_name": "", "what": "ramadan_days"} in month["draft"]["changes"]
        assert month["submit_refusal"] is None
        assert ok(submit(mona, er))["state"] == "submitted"

    def test_a_draft_is_warned_after_a_holiday_change_and_still_submits(self, mona, carla, er, solver):
        solved(mona, er, solver)
        add_holiday(carla, MONTH, 13)

        month = month_of(mona, er)

        assert month_level(month["draft"]["changes"]) == ["public_holidays"]
        assert month["submit_refusal"] is None
        assert ok(submit(mona, er))["state"] == "submitted"

    def test_a_draft_is_not_warned_when_the_dates_are_as_they_were(self, mona, carla, er, solver):
        """Fixing the month to the calendar's own days (none), and a holiday of another month, move nothing here."""
        add_holiday(carla, MONTH, 13, "Before")
        solved(mona, er, solver)
        fix_ramadan(carla, MONTH)
        add_holiday(carla, "2026-07", 4)
        holiday = PublicHoliday.objects.get(date=dt.date(2026, 6, 13))
        response = carla.put(f"/api/hospital/public-holidays/{holiday.pk}", {"date": "2026-06-13", "name": "Renamed"},
                             content_type="application/json")
        assert response.status_code == 200, response.content

        assert month_of(mona, er)["draft"]["changes"] == []

    def test_a_submitted_roster_shows_the_change_since_it_was_submitted(self, mona, carla, dina, er, solver):
        submitted(mona, er, solver)
        assert month_of(dina, er)["submitted"]["new_since"] == []

        fix_ramadan(carla, MONTH, 13)
        add_holiday(carla, MONTH, 13)

        seen = month_of(dina, er)
        assert [(c["hospital_id"], c["what"]) for c in seen["submitted"]["new_since"]] == [
            ("", "ramadan_days"), ("", "public_holidays")]
        assert month_level(seen["draft"]["changes"]) == ["ramadan_days", "public_holidays"]
        assert seen["approve_refusal"] is None
        assert ok(dina.get("/api/rosters/submitted"))[0]["changed_since"] is True

    def test_a_change_the_manager_was_warned_of_is_not_new_since_the_submit(self, mona, carla, dina, er, solver):
        solved(mona, er, solver)
        fix_ramadan(carla, MONTH, 13)

        ok(submit(mona, er))

        seen = month_of(dina, er)
        assert seen["submitted"]["new_since"] == []
        assert month_level(seen["draft"]["changes"]) == ["ramadan_days"]
        assert Submission.objects.get().dates_at_submit == {"ramadan_days": ["2026-06-13"], "public_holidays": []}

    def test_a_submission_from_before_the_dates_were_recorded_compares_no_dates(self, mona, carla, dina, er, solver):
        submitted(mona, er, solver)
        Submission.objects.update(dates_at_submit=None)

        fix_ramadan(carla, MONTH, 13)

        assert month_of(dina, er)["submitted"]["new_since"] == []
