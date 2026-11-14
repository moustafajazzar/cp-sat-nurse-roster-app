"""Swaps through the API: composing and refusing one, what the proposer may and may not learn of a colleague, the colleagues'
answers, the manager's decision, expiry, notifications, the audit log and who reads what. See `conftest.py` for the scene."""
import datetime as dt

import pytest

from accounts.models import Role, User
from audit.models import AuditEntry
from conftest import post
from hospital.models import FixedRamadanMonth, LeaveType
from notifications.models import Event, Notification
from notifications.tests.test_events import as_user, told
from nurses.models import NurseProfile, Placement
from requesting.tests.conftest import asked, decided as request_decided, opened
from rosters.models import PinEnded, PinnedCell, PinOrigin, Roster, RosterVersion, VersionOrigin
from rosters.tests.test_pins import icu, pin, saved
from rosters.tests.test_review import approve, approved, ok as roster_ok, solved, submit
from rosters.tests.test_versions import changed, in_force_agrees
from solves.tests.conftest import nurse_in
from swaps.models import Swap, SwapState
from swaps.tasks import expire_swaps
from swaps.tests.conftest import (MONTH, SOUND_SWAP, act, acted, cell, decide, decided, decision, mine, ok, propose, proposed,
                                  refusals, url)

pytestmark = pytest.mark.django_db

# Badr off and Emad on Day on the 9th, and Dana off and Amal on Night on the 9th: two colleagues besides Badr.
TWO_COLLEAGUES = [*SOUND_SWAP, cell("Dana", 9), cell("Amal", 9, "N")]


@pytest.fixture(autouse=True)
def _scene(before_june, solver):
    pass


def profile(hospital_id) -> NurseProfile:
    return NurseProfile.objects.get(user__username=hospital_id)


def leave_for(client, unit, nurse, day):
    return post(client, url(unit, "/leave"), {
        "nurse_id": str(profile(nurse).pk), "leave_type_id": str(LeaveType.objects.get(name="Sick").pk),
        "first": f"{MONTH}-{day:02d}", "last": f"{MONTH}-{day:02d}"})


def swap_notes(username) -> list[Notification]:
    return list(Notification.objects.filter(user__username=username, event=Event.SWAP_STEP).order_by("created_at"))


def steps_told(username) -> list[str]:
    return [note.payload["step"] for note in swap_notes(username)]


def actions() -> list[tuple[str, str, str]]:
    return [(entry.action, entry.actor.username if entry.actor else "", entry.target)
            for entry in AuditEntry.objects.filter(action__startswith="swap.").order_by("id")]


def in_force_grid(unit) -> dict:
    return Roster.objects.get(unit=unit).grid


class TestComposing:
    def test_the_composer_offers_the_days_to_come_the_units_shifts_and_the_staff_nurses_rows(self, in_force, nurse, on_day):
        on_day(2026, 6, 20)

        answer = ok(nurse("Badr").get("/api/swaps/compose", {"month": MONTH}))

        assert answer["refusal"] is None and answer["unit"] == "ER"
        assert [day["date"] for day in answer["days"]] == [f"2026-06-{day}" for day in range(20, 31)]
        assert [shift["code"] for shift in answer["shifts"]] == ["D", "N"]
        rows = {row["hospital_id"]: row for row in answer["nurses"]}
        assert set(rows) == {"Amal", "Badr", "Dana", "Emad"} and rows["Badr"]["me"] and not rows["Amal"]["me"]
        assert rows["Amal"]["shifts"] == [in_force_grid(in_force)["Amal"][f"2026-06-{day}"] for day in range(20, 31)]
        assert set(rows["Amal"]) == {"hospital_id", "full_name", "me", "shifts"}, "names and shifts only"

    def test_the_composer_says_why_with_no_approved_roster(self, mona, er, solver, nurse):
        solved(mona, er, solver)

        assert ok(nurse("Badr").get("/api/swaps/compose", {"month": MONTH}))["refusal"] == (
            "The ER roster for June 2026 is not approved yet, so there is nothing to swap.")
        assert refusals(propose(nurse("Badr"), *SOUND_SWAP), 409) == [
            "The ER roster for June 2026 is not approved yet, so there is nothing to swap."]

    def test_a_proposal_waits_for_its_colleagues_who_are_told_and_nobody_else(self, in_force, nurse):
        swap = proposed(nurse("Badr"), *SOUND_SWAP, note="My daughter's school play is on the 9th.")

        assert swap["state"] == "pending" and swap["proposed_by_me"] and swap["my_answer"] is None
        assert [(c["hospital_id"], c["date"], c["before"], c["after"]) for c in swap["cells"]] == [
            ("Badr", "2026-06-09", "D", ""), ("Emad", "2026-06-09", "", "D")]
        assert swap["colleagues"] == [{"hospital_id": "Emad", "full_name": "Emad", "answer": "pending"}]
        assert steps_told("Emad") == ["proposed"]
        assert Notification.objects.filter(event=Event.SWAP_STEP).count() == 1
        seen = mine(nurse("Emad"))
        assert [row["id"] for row in seen] == [swap["id"]] and seen[0]["note"] == "My daughter's school play is on the 9th."
        assert seen[0]["my_answer"] == "pending" and not seen[0]["proposed_by_me"]
        assert mine(nurse("Amal")) == [], "a nurse not in it sees nothing of it"
        assert actions() == [("swap.proposed", "Badr", "Badr")]

    def test_a_swap_of_only_ones_own_cells_goes_straight_to_the_manager(self, in_force, nurse):
        swap = proposed(nurse("Badr"), cell("Badr", 19))

        assert swap["state"] == "awaiting_manager" and swap["colleagues"] == []
        assert steps_told("mona") == ["awaiting"]

    def test_a_swap_changes_at_least_one_of_the_proposers_cells(self, in_force, nurse):
        assert refusals(propose(nurse("Badr"), cell("Emad", 9, "D"), cell("Amal", 1))) == [
            "A swap changes at least one of your own cells. Add one of yours."]

    def test_a_cell_already_holding_its_change_is_refused_as_no_change(self, in_force, nurse):
        assert refusals(propose(nurse("Badr"), cell("Badr", 9, "D"), cell("Emad", 10, "D"))) == [
            "Your cell on 9 June 2026 is already Day (D).", "Emad's cell on 10 June 2026 is already Day (D)."]

    def test_a_date_passed_and_a_shift_the_unit_does_not_work_are_refused(self, in_force, nurse, on_day):
        on_day(2026, 6, 10)

        assert refusals(propose(nurse("Badr"), cell("Badr", 9), cell("Badr", 11, "X"))) == [
            "9 June 2026 has passed, so its cells can no longer be swapped.", "X is not one of this unit's shifts."]

    def test_a_cell_outside_the_month_or_twice_is_refused_beside_it(self, in_force, nurse):
        response = propose(nurse("Badr"), cell("Badr", 9), cell("Badr", 9, "N"), cell("Emad", 1, "D", month="2026-07"))

        assert response.status_code == 422
        assert [(row["loc"][-1], row["msg"]) for row in response.json()["detail"]] == [
            (1, "This cell is in the swap twice."), (2, "2026-07-01 is not in 2026-06.")]

    def test_a_nurse_not_on_the_roster_is_refused_the_same_whoever_they_are(self, in_force, nurse, make_user, mona):
        icu_unit = icu(make_user("omar", Role.UNIT_MANAGER))
        nurse_in(icu_unit, make_user, "Zara")

        assert refusals(propose(nurse("Badr"), cell("Badr", 9), cell("Zara", 9, "D"))) == ["There is no Zara on this roster."]
        assert refusals(propose(nurse("Badr"), cell("Badr", 9), cell("Nobody", 9, "D"))) == [
            "There is no Nobody on this roster."]

    def test_a_cell_already_in_another_waiting_swap_is_refused_and_a_colleagues_says_only_it_cannot_work(self, in_force,
                                                                                                         nurse):
        proposed(nurse("Badr"), *SOUND_SWAP)

        assert refusals(propose(nurse("Emad"), cell("Emad", 9, "D"), cell("Amal", 9, "N"))) == [
            "Your cell on 9 June 2026 is already in another swap."]
        # Which swaps a colleague is in is not on My roster.
        assert refusals(propose(nurse("Amal"), cell("Amal", 9, "N"), cell("Emad", 9, "D"))) == [
            "This swap cannot work for Emad on 9 June 2026."]

    def test_a_managers_cells_cannot_be_swapped(self, in_force, nurse, mona):
        manager = User.objects.get(username="mona")
        mona_nurse = NurseProfile.objects.create(user=manager, gender="F", start_date=dt.date(2026, 1, 1))
        Placement.objects.create(nurse=mona_nurse, unit=in_force, from_month=dt.date(2026, 1, 1))
        roster = Roster.objects.get()
        roster.grid["mona"] = dict.fromkeys(roster.grid["Amal"], "")
        roster.save()
        version = roster.versions.first()
        version.grid = roster.grid
        version.save()

        assert refusals(propose(nurse("Badr"), cell("Badr", 9), cell("mona", 9, "D"))) == [
            "Mona Ali is not a staff nurse, so a swap cannot change their cells."]
        offered = ok(nurse("Badr").get("/api/swaps/compose", {"month": MONTH}))["nurses"]
        assert "mona" not in {row["hospital_id"] for row in offered}


class TestAShortShiftDay:
    """A headcount the roster in force already breaks is judged by how far it is outside its range, as a cover's is
    (`rosters.approved.newly_broken`): the solver's text changes with the count, so comparing the text would count one more
    nurse on a short shift-day as breaking it anew."""

    def test_a_swap_bringing_a_short_shift_day_nearer_its_minimum_is_not_refused_for_it(self, in_force, nurse, mona):
        # Day on the 9th wants three nurses and has Badr alone; Emad, off that day, adds himself and it stays short.
        in_force.rules["headcounts"][0].update(weekday_min=3, weekday_max=3, weekend_min=3, weekend_max=3)
        in_force.save()

        swap = proposed(nurse("Emad"), cell("Emad", 9, "D"))

        assert swap["state"] == "awaiting_manager"
        decided(mona, in_force, swap["id"], "approve")
        assert Swap.objects.get().state == SwapState.APPROVED
        assert in_force_grid(in_force)["Emad"]["2026-06-09"] == "D"

    def test_one_taking_a_nurse_off_a_short_shift_day_breaks_it_further_and_is_refused(self, in_force, nurse):
        # Badr off Day on the 9th, which has him alone of three wanted, onto Night beside Dana.
        in_force.rules["headcounts"][0].update(weekday_min=3, weekday_max=3, weekend_min=3, weekend_max=3)
        in_force.save()

        said = refusals(propose(nurse("Badr"), cell("Badr", 9, "N")))

        assert "This swap breaks the unit's rules: headcount: 2026-06-09 D = 0, want 3..3" in said


class TestOvertimeAboveAShare:
    def test_a_swap_giving_a_nurse_overtime_above_her_share_is_not_refused_and_july_carries_her_balance(self, in_force, nurse,
                                                                                                        mona):
        """Emad takes Badr's Day on the 9th (`one_more_shift_past_her_share`)."""
        from closes.tests.test_close import JULY, balances, one_more_shift_past_her_share
        july = one_more_shift_past_her_share("Emad")

        swap = proposed(nurse("Badr"), *SOUND_SWAP)
        acted(nurse("Emad"), swap["id"], "accept")
        decided(mona, in_force, swap["id"], "approve")

        assert Swap.objects.get().state == SwapState.APPROVED
        assert balances(in_force, JULY) == july


class TestTheProposersOwnFacts:
    """The proposer's own facts may be named to them."""

    def test_a_rule_broken_on_their_own_row_is_said_in_full(self, in_force, nurse):
        # Amal works Night on the 4th: Day on the 5th straight after it is a forbidden succession.
        said = refusals(propose(nurse("Amal"), cell("Amal", 5, "D"), cell("Emad", 5)))

        assert said and all(sentence.startswith("This swap breaks a rule for you: ") for sentence in said)
        assert any("Amal" in sentence for sentence in said)

    def test_their_own_leave_is_named(self, in_force, nurse, mona):
        assert leave_for(mona, in_force, "Badr", 9).status_code == 201

        assert refusals(propose(nurse("Badr"), cell("Badr", 9, "N"), cell("Emad", 9, "D"))) == [
            "You are on leave on 9 June 2026, so there is nothing to swap that day."]

    def test_their_own_allowed_shifts_are_named(self, in_force, nurse):
        assert refusals(propose(nurse("Emad"), cell("Emad", 9, "N"), cell("Badr", 9))) == [
            "N is not one of the shifts you work."]

    def test_their_own_accepted_request_is_named(self, mona, carla, er, solver, nurse):
        solved(mona, er, solver)
        opened(mona, er)
        made = asked(nurse("Badr"), 9, "D")
        request_decided(mona, er, made["id"], "accept")
        roster_ok(submit(mona, er))
        roster_ok(approve(carla, er))

        assert refusals(propose(nurse("Badr"), *SOUND_SWAP)) == [
            "Your accepted request holds your cell on 9 June 2026, so a swap cannot change it."]

    def test_their_own_expired_required_skill_is_named(self, in_force, nurse, mona):
        """Her skills are hers to read on My month, so the bar on her own cell is said in full."""
        from rosters.tests.test_review import holding
        holding("Emad", "BLS", dt.date(2026, 6, 8), required=True)

        assert refusals(propose(nurse("Emad"), cell("Emad", 9, "D"), cell("Badr", 9))) == [
            "Your BLS, required to work, counted until 8 June 2026, so you can work no shift on 9 June 2026."]
        TestWhatTheProposerMayNotLearn.the_manager_reads_why(
            mona, in_force, "Emad's BLS, required to work, counted until 8 June 2026, so Emad can work no shift on 9 June 2026.", "D")


class TestASkillNeed:
    """A skill need left short says who holds which skill, so the proposer reads only that the day's shifts would
    break the unit's rules, as for a team leader or both genders; the manager reads the solver's words."""

    def test_a_swap_leaving_it_short_is_refused_naming_no_rule_and_the_manager_reads_it(self, in_force, nurse, mona):
        from rosters.tests.test_review import holding
        holding("Badr", "ICU", None, expires=False)
        rules_require(in_force, skill_needs=[{"shift": "D", "skill": "ICU", "weekday_min": 1, "weekend_min": 0}])

        response = propose(nurse("Badr"), *SOUND_SWAP)

        assert refusals(response) == ["This swap cannot work on 9 June 2026: that day's shifts would break the unit's rules."]
        assert "ICU" not in response.content.decode() and "skill" not in response.content.decode()
        TestWhatTheProposerMayNotLearn.the_manager_reads_why(
            mona, in_force, "skill need: 2026-06-09 D has 0 holding ICU, needs 1 (1 short)", "D")

    def test_a_swap_leaving_a_need_already_short_shorter_is_refused_naming_no_rule(self, in_force, nurse, mona):
        """Day needs two nurses holding ICU and Badr is its only one on the 9th: the roster in force leaves it one short, and
        the swap, moving him off, two."""
        from rosters.tests.test_review import holding
        holding("Badr", "ICU", None, expires=False)
        rules_require(in_force, skill_needs=[{"shift": "D", "skill": "ICU", "weekday_min": 2, "weekend_min": 0}])

        response = propose(nurse("Badr"), *SOUND_SWAP)

        assert refusals(response) == ["This swap cannot work on 9 June 2026: that day's shifts would break the unit's rules."]
        assert "ICU" not in response.content.decode() and "skill" not in response.content.decode()
        TestWhatTheProposerMayNotLearn.the_manager_reads_why(
            mona, in_force, "skill need: 2026-06-09 D has 0 holding ICU, needs 2 (2 short)", "D")

    def test_a_swap_leaving_a_need_less_short_is_not_refused_for_it(self, in_force, nurse):
        """Day needs two nurses holding ICU and has none on the 9th; Emad, who holds it, takes Badr's Day there. One short where
        it was two is broken less, not anew."""
        from rosters.tests.test_review import holding
        holding("Emad", "ICU", None, expires=False)
        rules_require(in_force, skill_needs=[{"shift": "D", "skill": "ICU", "weekday_min": 2, "weekend_min": 0}])

        assert proposed(nurse("Badr"), *SOUND_SWAP)["state"] == "pending"


FORBIDDEN = ["leave", "sick", "allowed", "gender", "team leader", "team_leader", "breastfeed", "ramadan", "score", "overtime",
             "hours", "request", "unavailable", "restriction", "outside", "dates", "transfer", "not on this", "cost", "deactiv",
             "end date", "contract", "cap", "only f", "only m"]


def nothing_hidden_in(text: str) -> None:
    lowered = text.lower()
    assert [word for word in FORBIDDEN if word in lowered] == []


def _leave(mona, unit):
    assert leave_for(mona, unit, "Emad", 9).status_code == 201


def _end_date(mona, unit):
    NurseProfile.objects.filter(user__username="Emad").update(end_date=dt.date(2026, 6, 8))


def _transfer(mona, unit):
    other = User.objects.create_user("omar", "correct-horse-battery", Role.UNIT_MANAGER)
    Placement.objects.create(nurse=profile("Emad"), unit=icu(other), from_month=dt.date(2026, 6, 1))


def _deactivated(mona, unit):
    User.objects.filter(username="Emad").update(is_active=False)


def _overtime(mona, unit):
    # His opening overtime for the year at the cap: one more shift breaks it.
    NurseProfile.objects.filter(user__username="Emad").update(ytd_overtime=720)


# Emad's overtime this year at 712 hours leaves room for the one more Day shift of `SOUND_SWAP`, and no more: each fact below
# lowers the hours his month owes, so that shift would take him past the overtime cap.
NEAR_THE_CAP = 712


def _ramadan_reduced(mona, unit):
    NurseProfile.objects.filter(user__username="Emad").update(ytd_overtime=NEAR_THE_CAP, ramadan_reduced=True)
    FixedRamadanMonth.objects.update_or_create(month="2026-06", defaults={"dates": [dt.date(2026, 6, day) for day in (1, 2, 3)]})


def _breastfeeding(mona, unit):
    NurseProfile.objects.filter(user__username="Emad").update(ytd_overtime=NEAR_THE_CAP, breastfeeding=True)


def _hours_worked_ahead(mona, unit):
    """What his contract still asks of him this month: two shifts' hours already worked ahead."""
    NurseProfile.objects.filter(user__username="Emad").update(ytd_overtime=NEAR_THE_CAP, hours_balance=-24)


def _barred(mona, unit):
    """Emad's BLS, required to work, lasted to the 8th, recorded since the approval."""
    from rosters.tests.test_review import holding
    holding("Emad", "BLS", dt.date(2026, 6, 8), required=True)


def _request(mona, unit):
    opened(mona, unit)
    made = asked(as_user("Emad"), 9)
    request_decided(mona, unit, made["id"], "accept")


class TestWhatTheProposerMayNotLearn:
    """Worth the user's review. A refusal that rests on a colleague's facts says the same sentence whatever the fact: that the
    swap cannot work for them on that date. Each hidden fact of Emad's is set, Badr proposes Emad on Day on the 9th, and the
    answer holds nothing of it; the manager reads why."""

    @pytest.mark.parametrize("fact, shift, manager_reads", [
        (_leave, "D", "Emad is on leave on 9 June 2026, and leave wins over a pin."),
        (None, "N", "N is not one of Emad's allowed shifts."),
        (_end_date, "D", "9 June 2026 is outside Emad's dates, so there is nothing to pin."),
        (_transfer, "D", "Emad is not on this unit's roster for June 2026."),
        (_deactivated, "D", "Emad is not on this unit's roster for June 2026."),
        (_overtime, "D", "overtime"),
        (_ramadan_reduced, "D", "overtime"),
        (_breastfeeding, "D", "overtime"),
        (_hours_worked_ahead, "D", "overtime"),
        (_barred, "D", "Emad's BLS, required to work, counted until 8 June 2026, so Emad can work no shift on 9 June 2026."),
    ], ids=["leave", "allowed_shifts", "end_date", "transferred", "deactivated", "overtime", "ramadan_reduced", "breastfeeding",
            "contract_hours", "barred_by_an_expired_skill"])
    def test_a_colleagues_hidden_fact(self, in_force, nurse, mona, fact, shift, manager_reads):
        badr = nurse("Badr")
        if fact:
            fact(mona, in_force)

        response = propose(badr, cell("Badr", 9), cell("Emad", 9, shift))

        assert refusals(response) == ["This swap cannot work for Emad on 9 June 2026."]
        nothing_hidden_in(response.content.decode())
        assert not Swap.objects.exists() and not Notification.objects.filter(event=Event.SWAP_STEP).exists()
        self.the_manager_reads_why(mona, in_force, manager_reads, shift)

    def test_without_a_reduction_the_same_overtime_leaves_room_for_the_swap(self, in_force, nurse):
        """So in the three cases above the reduction, not the overtime, is what refuses it."""
        NurseProfile.objects.filter(user__username="Emad").update(ytd_overtime=NEAR_THE_CAP)

        assert propose(nurse("Badr"), *SOUND_SWAP).status_code == 201

    @staticmethod
    def the_manager_reads_why(mona, unit, words, shift):
        """The same change, judged for the manager, says it in full."""
        from swaps.judging import NewCell, judge
        judged = judge(Roster.objects.get(), [NewCell("Badr", dt.date(2026, 6, 9), None),
                                             NewCell("Emad", dt.date(2026, 6, 9), shift)], None)
        assert any(words in problem.manager for problem in judged.problems), [p.manager for p in judged.problems]

    @pytest.mark.parametrize("on_leave", [False, True], ids=["a day off", "leave"])
    def test_what_my_roster_shows_is_said_before_any_hidden_fact_so_leave_reads_as_a_day_off(self, in_force, nurse, mona,
                                                                                            on_leave):
        """Emad is off on the 9th, and on leave in one run: asking for what the cell holds, or for a shift the unit does not
        work, gets the same answer either way."""
        if on_leave:
            _leave(mona, in_force)
        badr = nurse("Badr")

        assert refusals(propose(badr, cell("Badr", 9), cell("Emad", 9))) == ["Emad's cell on 9 June 2026 is already a day off."]
        assert refusals(propose(badr, cell("Badr", 9), cell("Emad", 9, "ZZ"))) == ["ZZ is not one of this unit's shifts."]

    def test_a_colleagues_accepted_request(self, in_force, nurse, mona):
        _request(mona, in_force)

        response = propose(nurse("Badr"), *SOUND_SWAP)

        assert refusals(response) == ["This swap cannot work for Emad on 9 June 2026."]
        nothing_hidden_in(response.content.decode())

    def test_a_team_leader_missing_from_a_shift_day(self, in_force, nurse):
        rules_require(in_force, require_team_leader=True)
        # Emad, a team leader, works the only Day shift on the 10th; Badr, who is not one, would take it.
        response = propose(nurse("Badr"), cell("Badr", 10, "D"), cell("Emad", 10))

        assert refusals(response) == ["This swap cannot work on 10 June 2026: that day's shifts would break the unit's rules."]
        nothing_hidden_in(response.content.decode())

    def test_a_gender_missing_from_a_shift_day(self, in_force, nurse):
        rules_require(in_force, require_both_genders=True)
        # Badr and Amal work Night on the 19th; without Badr only women would.
        response = propose(nurse("Badr"), cell("Badr", 19))

        assert refusals(response) == ["This swap cannot work on 19 June 2026: that day's shifts would break the unit's rules."]
        nothing_hidden_in(response.content.decode())

    def test_what_a_colleague_and_the_proposer_read_of_a_swap_holds_nothing_hidden(self, in_force, nurse, mona):
        swap = proposed(nurse("Badr"), *TWO_COLLEAGUES)
        for reader in ("Badr", "Emad", "Dana", "Amal"):
            client = nurse(reader)
            seen = client.get("/api/swaps/mine")
            nothing_hidden_in(seen.content.decode().replace(swap["note"], ""))
            assert set(seen.json()[0]) == {"id", "month", "unit", "state", "proposed_at", "proposer", "proposed_by_me", "note",
                                           "cells", "colleagues", "my_answer", "ended_by", "ended_at", "reason"}
            nothing_hidden_in(client.get("/api/notifications").content.decode())


def rules_require(unit, **rules):
    unit.rules = unit.rules | rules
    unit.save()


class TestTheColleaguesAnswers:
    def test_each_acceptance_tells_the_proposer_and_the_last_sends_it_to_the_manager_once(self, in_force, nurse):
        swap = proposed(nurse("Badr"), *TWO_COLLEAGUES)

        assert acted(nurse("Emad"), swap["id"], "accept")["state"] == "pending"
        assert steps_told("Badr") == ["accepted"] and steps_told("mona") == []
        assert acted(nurse("Dana"), swap["id"], "accept")["state"] == "pending"
        assert acted(nurse("Amal"), swap["id"], "accept")["state"] == "awaiting_manager"

        assert Swap.objects.get().state == SwapState.AWAITING_MANAGER
        assert steps_told("Badr") == ["accepted", "accepted", "accepted"]
        assert steps_told("mona") == ["awaiting"]
        assert {row["answer"] for row in mine(nurse("Badr"))[0]["colleagues"]} == {"accepted"}
        assert [action for action, _, target in actions()] == ["swap.proposed", "swap.accepted", "swap.accepted",
                                                                 "swap.accepted"]
        assert {target for _, _, target in actions()} == {"Badr"}

    def test_one_rejection_ends_it_for_everyone_who_is_told(self, in_force, nurse):
        swap = proposed(nurse("Badr"), *TWO_COLLEAGUES)
        acted(nurse("Emad"), swap["id"], "accept")

        answer = acted(nurse("Dana"), swap["id"], "reject")

        assert answer["state"] == "rejected" and answer["ended_by"] == "Dana" and answer["my_answer"] == "rejected"
        assert steps_told("Badr")[-1] == "rejected" and steps_told("Emad")[-1] == "rejected"
        assert steps_told("Amal") == ["proposed", "rejected"] and steps_told("Dana") == ["proposed"]
        assert refusals(act(nurse("Amal"), swap["id"], "accept"), 409) == [
            "This swap is rejected, so it can no longer be answered."]
        assert ("swap.rejected", "Dana", "Badr") in actions()

    def test_answering_twice_and_answering_a_swap_one_is_not_in_are_refused(self, in_force, nurse):
        swap = proposed(nurse("Badr"), *TWO_COLLEAGUES)
        acted(nurse("Emad"), swap["id"], "accept")

        assert refusals(act(nurse("Emad"), swap["id"], "reject"), 409) == ["You have accepted this swap already."]
        assert act(nurse("Badr"), swap["id"], "accept").status_code == 404, "the proposer is not one of its colleagues"
        other = proposed(nurse("Badr"), cell("Badr", 19))
        assert act(nurse("Emad"), other["id"], "accept").status_code == 404

    def test_the_proposer_withdraws_it_while_it_waits_and_its_people_are_told(self, in_force, nurse):
        swap = proposed(nurse("Badr"), *SOUND_SWAP)
        acted(nurse("Emad"), swap["id"], "accept")

        assert act(nurse("Emad"), swap["id"], "withdraw").status_code == 404, "only its proposer withdraws it"
        answer = acted(nurse("Badr"), swap["id"], "withdraw")

        assert answer["state"] == "withdrawn" and answer["ended_by"] == "Badr"
        assert steps_told("Emad")[-1] == "withdrawn" and steps_told("mona") == ["withdrawn"], "each unread notice became it"
        assert steps_told("Badr") == ["accepted"]
        assert refusals(act(nurse("Badr"), swap["id"], "withdraw"), 409) == ["This swap is withdrawn, so it cannot be withdrawn."]


class TestRoundsOfProposingAndWithdrawing:
    """One nurse's rounds of proposing and withdrawing cannot decide what another reads: each person holds one notice a swap,
    which a withdrawal turns into its own while still unread, and a nurse who has withdrawn `WITHDRAWALS_A_DAY` swaps on a
    roster in the last day proposes no more there until the first of them is a day old."""

    def test_a_colleagues_earlier_notice_stays_in_her_list_and_the_round_after_the_cap_is_refused(self, in_force, nurse):
        from notifications import sending
        from notifications.api import SHOWN
        from swaps.steps import WITHDRAWALS_A_DAY
        emad = User.objects.get(username="Emad")
        [earlier] = Notification.objects.filter(user=emad)              # the roster's approval, unread
        for _ in range(SHOWN - WITHDRAWALS_A_DAY - 1):                  # her list full but for the rounds' room
            sending.notify([emad], Event.ROSTER_CHANGED, unit_id=str(in_force.pk), month=MONTH)
        badr = nurse("Badr")

        for _ in range(WITHDRAWALS_A_DAY):
            acted(badr, proposed(badr, *SOUND_SWAP)["id"], "withdraw")

        listed = told(nurse("Emad"))
        assert str(earlier.pk) in {item["id"] for item in listed} and len(listed) == SHOWN
        assert steps_told("Emad") == ["withdrawn"] * WITHDRAWALS_A_DAY
        assert all(note.read_at is None for note in swap_notes("Emad"))
        assert [item["text"] for item in told(nurse("Emad"), "swap_step")] == [
            "Badr withdrew the swap they proposed on the ER roster for June 2026."] * WITHDRAWALS_A_DAY
        assert refusals(propose(badr, *SOUND_SWAP), 409) == [
            f"You have withdrawn {WITHDRAWALS_A_DAY} swaps on this roster in the last day, so you can propose another here from "
            f"{first_free_at()}."]
        assert Swap.objects.count() == WITHDRAWALS_A_DAY

    def test_the_cap_counts_only_the_last_day(self, in_force, nurse):
        from django.utils import timezone

        from swaps.steps import WITHDRAWALS_A_DAY
        badr = nurse("Badr")
        for _ in range(WITHDRAWALS_A_DAY):
            acted(badr, proposed(badr, *SOUND_SWAP)["id"], "withdraw")
        Swap.objects.filter(pk=Swap.objects.order_by("ended_at").first().pk).update(
            ended_at=timezone.now() - dt.timedelta(days=1, seconds=1))

        assert propose(badr, *SOUND_SWAP).status_code == 201

    def test_a_notice_already_read_stays_and_the_withdrawal_is_a_new_one(self, in_force, nurse):
        badr, emad = nurse("Badr"), nurse("Emad")
        swap = proposed(badr, *SOUND_SWAP)
        assert post(emad, "/api/notifications/read-all", {}).status_code == 204

        acted(badr, swap["id"], "withdraw")

        assert steps_told("Emad") == ["proposed", "withdrawn"]

    def test_the_managers_notice_of_one_awaiting_him_becomes_the_withdrawal(self, in_force, nurse, mona):
        badr = nurse("Badr")
        swap = proposed(badr, cell("Badr", 19))
        assert steps_told("mona") == ["awaiting"]

        acted(badr, swap["id"], "withdraw")

        assert steps_told("mona") == ["withdrawn"]
        assert [item["text"] for item in told(mona, "swap_step")] == [
            "Badr withdrew the swap they proposed on the ER roster for June 2026."]


def first_free_at() -> str:
    """When the oldest withdrawal of the day is a day old, on the hospital's clock, as the refusal says it."""
    from django.utils import timezone

    from rosters.pins import long_date
    oldest = timezone.localtime(Swap.objects.order_by("ended_at").first().ended_at + dt.timedelta(days=1))
    return f"{oldest:%H:%M} on {long_date(oldest.date())}"


def awaiting(nurse, cells=SOUND_SWAP, proposer="Badr") -> dict:
    swap = proposed(nurse(proposer), *cells)
    for colleague in {c["hospital_id"] for c in cells} - {proposer}:
        acted(nurse(colleague), swap["id"], "accept")
    return swap


class TestTheManagersDecision:
    def test_the_decision_view_shows_before_and_after_the_validation_and_the_cost_change(self, in_force, nurse, mona):
        swap = awaiting(nurse)

        answer = decision(mona, in_force, swap["id"])

        assert [(c["hospital_id"], c["before"], c["after"], c["now"], c["replaces_pin"]) for c in answer["cells"]] == [
            ("Badr", "D", "", "D", None), ("Emad", "", "D", "", None)]
        assert answer["check"]["ok"] is True and answer["problems"] == [] and answer["approve_refusal"] is None
        assert answer["delta"] == answer["cost_after"] - answer["cost_before"] and answer["cost_before"] is not None
        assert answer["swap"]["state"] == "awaiting_manager"

    def test_one_still_waiting_for_its_colleagues_cannot_be_approved(self, in_force, nurse, mona):
        swap = proposed(nurse("Badr"), *SOUND_SWAP)

        assert decision(mona, in_force, swap["id"])["approve_refusal"] == "This swap still waits for its colleagues' answers."
        assert refusals(decide(mona, in_force, swap["id"], "approve"), 409) == [
            "This swap still waits for its colleagues' answers."]

    def test_approval_pins_its_cells_as_a_new_version_in_force_and_tells_everyone(self, in_force, nurse, mona):
        swap = awaiting(nurse)

        answer = decided(mona, in_force, swap["id"], "approve", "Fine by me.")

        version = RosterVersion.objects.filter(roster__unit=in_force).first()
        assert (version.number, version.origin) == (2, VersionOrigin.SWAP)
        assert version.reason == "Swap proposed by Badr approved: Badr, Emad on 9 June 2026."
        assert answer["swap"]["state"] == "approved" and answer["swap"]["version"] == 2
        assert answer["swap"]["reason"] == "Fine by me."
        grid = in_force_grid(in_force)
        assert (grid["Badr"]["2026-06-09"], grid["Emad"]["2026-06-09"]) == ("", "D")
        in_force_agrees(in_force)
        pins = {(p.nurse.user.username, p.code, p.origin) for p in PinnedCell.objects.current()}
        assert pins == {("Badr", "", PinOrigin.SWAP), ("Emad", "D", PinOrigin.SWAP)}
        assert steps_told("Badr")[-1] == "approved" and steps_told("Emad")[-1] == "approved"
        changed_to = set(Notification.objects.filter(event=Event.ROSTER_CHANGED).values_list("user__username", flat=True))
        assert changed_to == {"Amal", "Badr", "Dana", "Emad"}
        assert actions()[-1] == ("swap.approved", "mona", "Badr")
        entry = AuditEntry.objects.get(action="roster.changed")
        assert entry.after == {"version": 2, "pins": 2, "cells": 2}
        assert mine(nurse("Emad"))[0]["reason"] == "Fine by me."

    def test_approval_is_audited_as_the_swaps_step_then_the_roster_change_both_from_the_managers_address(
            self, in_force, nurse, mona):
        swap = awaiting(nurse)

        decided(mona, in_force, swap["id"], "approve")

        entries = list(AuditEntry.objects.filter(action__in=("swap.approved", "roster.changed")).order_by("id"))
        assert [(entry.action, entry.actor.username, entry.target) for entry in entries] == [
            ("swap.approved", "mona", "Badr"), ("roster.changed", "mona", "ER, 2026-06")]
        assert [entry.ip for entry in entries] == ["127.0.0.1", "127.0.0.1"]

    def test_a_swap_that_would_change_no_cell_is_refused_with_nothing_saved(self, in_force, nurse, mona, monkeypatch):
        """Judging refuses a cell already holding its change, so only a judge that let one through could reach this."""
        from types import SimpleNamespace

        from swaps import steps
        from swaps.models import SwapCell
        swap = awaiting(nurse)
        for swap_cell in SwapCell.objects.filter(swap_id=swap["id"]):
            swap_cell.after = swap_cell.before
            swap_cell.save(update_fields=["after"])
        monkeypatch.setattr(steps, "judge", lambda *args, **kwargs: SimpleNamespace(problems=[]))

        assert refusals(decide(mona, in_force, swap["id"], "approve"), 409) == [
            "This swap would change no cell of the roster in force, so there is nothing to approve."]
        assert Swap.objects.get().state == SwapState.AWAITING_MANAGER
        assert RosterVersion.objects.filter(roster__unit=in_force).count() == 1
        assert not PinnedCell.objects.filter(origin=PinOrigin.SWAP).exists()
        assert not AuditEntry.objects.filter(action__in=("swap.approved", "roster.changed")).exists()

    def test_approval_replaces_a_managers_pin_and_says_so_first(self, mona, carla, er, solver, nurse):
        solved(mona, er, solver)
        saved(mona, er, pin("Badr", 9, "D"))
        roster_ok(submit(mona, er))
        roster_ok(approve(carla, er))
        swap = awaiting(nurse)

        assert [c["replaces_pin"] for c in decision(mona, er, swap["id"])["cells"]] == ["D", None]
        answer = decided(mona, er, swap["id"], "approve")

        assert [c["replaces_pin"] for c in answer["cells"]] == [None, None], "an ended swap replaces nothing"

        ended = PinnedCell.objects.get(origin=PinOrigin.MANAGER)
        assert ended.removed_why == PinEnded.REPLACED

    def test_approval_is_refused_with_the_reason_in_full_when_the_month_moved_since(self, in_force, nurse, mona):
        swap = awaiting(nurse)
        assert leave_for(mona, in_force, "Emad", 9).status_code == 201  # a day off, so no version: the swap stays live

        assert decision(mona, in_force, swap["id"])["problems"] == [
            "Emad is on leave on 9 June 2026, and leave wins over a pin."]
        assert refusals(decide(mona, in_force, swap["id"], "approve"), 409) == [
            "Emad is on leave on 9 June 2026, and leave wins over a pin."]
        assert Swap.objects.get().state == SwapState.AWAITING_MANAGER and RosterVersion.objects.count() == 1

    def test_approval_is_refused_when_a_required_skill_expired_since_and_the_manager_reads_whose(self, in_force, nurse, mona):
        """Approval judges the swap again on the month as it stands, skills included."""
        from rosters.tests.test_review import holding
        swap = awaiting(nurse)
        holding("Emad", "BLS", dt.date(2026, 6, 8), required=True)
        words = "Emad's BLS, required to work, counted until 8 June 2026, so Emad can work no shift on 9 June 2026."

        assert decision(mona, in_force, swap["id"])["problems"] == [words]
        assert refusals(decide(mona, in_force, swap["id"], "approve"), 409) == [words]
        assert Swap.objects.get().state == SwapState.AWAITING_MANAGER and RosterVersion.objects.count() == 1

    def test_approval_is_refused_when_a_skill_need_written_since_would_be_left_short(self, in_force, nurse, mona):
        from rosters.tests.test_review import holding
        swap = awaiting(nurse)
        holding("Badr", "ICU", None, expires=False)
        rules_require(in_force, skill_needs=[{"shift": "D", "skill": "ICU", "weekday_min": 1, "weekend_min": 0}])
        words = "skill need: 2026-06-09 D has 0 holding ICU, needs 1 (1 short)"

        assert decision(mona, in_force, swap["id"])["problems"] == [words]
        assert refusals(decide(mona, in_force, swap["id"], "approve"), 409) == [words]
        assert Swap.objects.get().state == SwapState.AWAITING_MANAGER and RosterVersion.objects.count() == 1

    def test_a_rejection_needs_a_reason_that_everyone_in_it_reads(self, in_force, nurse, mona):
        swap = awaiting(nurse)

        assert decide(mona, in_force, swap["id"], "reject", " ").status_code == 422
        answer = decided(mona, in_force, swap["id"], "reject", "We need Badr on Day that week.")

        assert answer["swap"]["state"] == "rejected" and answer["swap"]["ended_by"] == "Mona Ali"
        assert mine(nurse("Badr"))[0]["reason"] == "We need Badr on Day that week."
        assert steps_told("Badr")[-1] == "rejected" and steps_told("Emad")[-1] == "rejected"
        assert RosterVersion.objects.count() == 1
        assert ("swap.rejected", "mona", "Badr") in actions()
        assert "We need Badr" not in str(list(AuditEntry.objects.values()))

    def test_the_unit_list_and_another_units_swap(self, in_force, nurse, mona, client_of, make_user):
        swap = awaiting(nurse)

        assert [row["id"] for row in ok(mona.get(url(in_force, "/swaps"), {"month": MONTH}))["swaps"]] == [swap["id"]]
        other = client_of("omar", Role.UNIT_MANAGER)
        unit = icu(User.objects.get(username="omar"))
        assert other.get(url(unit, f"/swaps/{swap['id']}")).status_code == 404
        assert decide(other, unit, swap["id"], "approve").status_code == 404


class TestExpiry:
    def test_a_date_passing_expires_it_on_every_page_at_once_and_the_hourly_job_tells_once(self, in_force, nurse, mona, on_day):
        swap = proposed(nurse("Badr"), *SOUND_SWAP)
        on_day(2026, 6, 10)

        assert mine(nurse("Badr"))[0]["state"] == "expired"
        assert ok(mona.get(url(in_force, "/swaps"), {"month": MONTH}))["swaps"][0]["expired_why"] == "date_passed"
        assert refusals(act(nurse("Emad"), swap["id"], "accept"), 409) == [
            "This swap is expired, so it can no longer be answered."]
        expire_swaps(0)
        expire_swaps(0)

        assert Swap.objects.get().state == SwapState.EXPIRED
        assert steps_told("Badr") == ["expired"] and steps_told("Emad") == ["proposed", "expired"]
        assert not AuditEntry.objects.filter(action="swap.expired").exists()

    def test_the_day_itself_has_not_passed(self, in_force, nurse, on_day):
        proposed(nurse("Badr"), *SOUND_SWAP)
        on_day(2026, 6, 9)
        expire_swaps(0)

        assert Swap.objects.get().state == SwapState.PENDING

    def test_a_managers_edit_of_one_of_its_cells_expires_it_and_one_elsewhere_does_not(self, in_force, nurse, mona):
        proposed(nurse("Badr"), *SOUND_SWAP)
        changed(mona, in_force, pin("Badr", 20, "D"), reason="Badr covers the 20th.")
        assert Swap.objects.get().state == SwapState.PENDING

        changed(mona, in_force, pin("Badr", 9, "N"), reason="Badr moves to Night on the 9th.")

        swap = Swap.objects.get()
        assert (swap.state, swap.expired_why) == (SwapState.EXPIRED, "cell_changed")
        assert steps_told("Badr") == ["expired"] and steps_told("Emad") == ["proposed", "expired"]

    def test_leave_taking_a_shift_off_one_of_its_cells_expires_it_and_nobody_is_told_it_was_leave(self, in_force, nurse, mona):
        # Emad proposes; Badr's Day on the 9th is one of its cells.
        proposed(nurse("Emad"), cell("Emad", 9, "D"), cell("Badr", 9))

        assert leave_for(mona, in_force, "Badr", 9).status_code == 201

        assert Swap.objects.get().state == SwapState.EXPIRED
        said = told(nurse("Emad"), "swap_step")[0]
        assert said["text"] == "The swap you proposed on the ER roster for June 2026 has expired, so it changes nothing."
        nothing_hidden_in(nurse("Emad").get("/api/swaps/mine").content.decode())
        assert set(swap_notes("Emad")[-1].payload) == {"unit_id", "month", "swap_id", "step"}

    def test_an_accepted_request_changing_one_of_its_cells_expires_it(self, mona, carla, er, solver, nurse):
        approved(mona, carla, er, solver)
        opened(mona, er)
        made = asked(nurse("Badr"), 9, "N")
        proposed(nurse("Badr"), *SOUND_SWAP)

        request_decided(mona, er, made["id"], "accept")

        assert Swap.objects.get().state == SwapState.EXPIRED

    def test_another_swaps_approval_expires_none_of_this_ones(self, in_force, nurse, mona):
        first = awaiting(nurse)
        second = proposed(nurse("Dana"), cell("Dana", 9), cell("Amal", 9, "N"))

        decided(mona, in_force, first["id"], "approve")

        assert Swap.objects.get(pk=second["id"]).state == SwapState.PENDING


class TestNotifications:
    def test_the_words_and_the_links_of_each_step(self, in_force, nurse, mona):
        swap = awaiting(nurse)
        decided(mona, in_force, swap["id"], "approve")

        emad = told(nurse("Emad"), "swap_step")
        assert [item["text"] for item in emad] == [
            "Mona Ali approved the swap Badr proposed on the ER roster for June 2026. Its cells are in the roster in force.",
            "Badr proposed a swap on the ER roster for June 2026 that changes your cells. Accept or reject it on Swaps."]
        assert emad[0]["link"] == {"page": "swaps", "month": MONTH, "unit_id": None, "cover_id": None}
        badr = told(nurse("Badr"), "swap_step")
        assert [item["text"] for item in badr] == [
            "Mona Ali approved the swap you proposed on the ER roster for June 2026. Its cells are in the roster in force.",
            "Emad accepted the swap you proposed on the ER roster for June 2026."]
        manager = told(mona, "swap_step")
        assert [item["text"] for item in manager] == [
            "The swap Badr proposed on the ER roster for June 2026 waits for your decision."]
        assert manager[0]["link"] == {"page": "unit_swaps", "month": MONTH, "unit_id": None, "cover_id": None}

    def test_payloads_hold_identifiers_and_the_step_only(self, in_force, nurse, mona):
        swap = awaiting(nurse)
        decided(mona, in_force, swap["id"], "reject", "Badr is needed.")

        for note in Notification.objects.filter(event=Event.SWAP_STEP):
            assert set(note.payload) <= {"unit_id", "month", "swap_id", "step", "by"}
        assert told(nurse("Badr"), "swap_step")[0]["text"] == (
            "Mona Ali rejected the swap you proposed on the ER roster for June 2026. The reason is on Swaps.")

    def test_withdrawn_and_a_colleagues_rejection(self, in_force, nurse):
        first = proposed(nurse("Badr"), *SOUND_SWAP)
        acted(nurse("Badr"), first["id"], "withdraw")
        second = proposed(nurse("Badr"), *SOUND_SWAP)
        acted(nurse("Emad"), second["id"], "reject")

        assert told(nurse("Badr"), "swap_step")[0]["text"] == (
            "Emad rejected the swap you proposed on the ER roster for June 2026, so it has ended.")
        assert told(nurse("Emad"), "swap_step")[1]["text"] == (
            "Badr withdrew the swap they proposed on the ER roster for June 2026.")
