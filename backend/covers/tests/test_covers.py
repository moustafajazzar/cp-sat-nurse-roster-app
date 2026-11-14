"""Covers through the API: how one opens, by leave and by hand; what its page says of the shift-day and who could take it; how it
ends, withdrawn, expired or filled; and that nothing of it reaches the audit log but a withdrawal. See `conftest.py` for the
scene."""
import datetime as dt

import pytest


from accounts.models import Role, User
from audit.models import AuditEntry
from covers.models import Cover, CoverState
from covers.tasks import expire_covers
from covers.tests.conftest import (MONTH, cover_page, covers_of, night_needs, offered, ok, open_cover, opened, record_leave,
                                   refusals, rules_require, wider_day, withdraw)
from notifications.tests.test_events import as_user
from nurses.models import NurseProfile
from requesting.tests.conftest import asked, decided as request_decided, opened as opened_window
from rosters.tests.test_pins import icu, off, pin
from rosters.tests.test_review import holding, solved, submitted
from rosters.tests.test_versions import changed
from swaps.tests.conftest import SOUND_SWAP, acted, cell as swap_cell, decided, proposed

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _scene(before_june, solver):
    pass


class TestOpenedByLeave:
    def test_leave_on_a_worked_day_opens_a_cover_unoffered_and_its_answer_names_it(self, in_force, mona):
        saved = ok(record_leave(mona, in_force, "Badr", 9), 201)

        [taken] = saved["shifts_taken_off"]
        assert (taken["date"], taken["shift"], taken["version"]) == ("2026-06-09", "D", 2)
        [cover] = covers_of(mona, in_force)
        assert taken["cover"] == cover["id"]
        assert (cover["date"], cover["shift"], cover["state"], cover["opened"], cover["opened_on"]) == (
            "2026-06-09", "D", "open", "leave", 2)
        assert cover["month"] == MONTH and cover["taker"] is None and cover["ended_by"] is None
        assert audited() == [], "opening is nobody's entry: one beside the leave's would date it"

    def test_leave_over_several_worked_days_opens_one_cover_for_each_and_none_for_the_day_off(self, in_force, mona):
        # Badr works Day on the 9th and the 11th and is off on the 10th.
        saved = ok(record_leave(mona, in_force, "Badr", 9, 11), 201)

        covers = {cover["id"]: (cover["date"], cover["shift"]) for cover in covers_of(mona, in_force)}
        assert sorted(covers.values()) == [("2026-06-09", "D"), ("2026-06-11", "D")]
        assert {covers[taken["cover"]] for taken in saved["shifts_taken_off"]} == set(covers.values())

    def test_leave_on_a_day_off_opens_none(self, in_force, mona):
        ok(record_leave(mona, in_force, "Badr", 10), 201)

        assert covers_of(mona, in_force) == []

    def test_a_day_passed_is_taken_off_but_opens_none(self, in_force, mona, on_day):
        on_day(2026, 6, 10)

        saved = ok(record_leave(mona, in_force, "Badr", 9, 11), 201)

        assert [(taken["date"], taken["cover"] is None) for taken in saved["shifts_taken_off"]] == [
            ("2026-06-09", True), ("2026-06-11", False)]
        assert [cover["date"] for cover in covers_of(mona, in_force)] == ["2026-06-11"]

    @pytest.mark.parametrize("state", ["draft", "submitted"])
    def test_a_draft_or_a_submitted_roster_opens_none(self, mona, er, solver, state):
        (solved if state == "draft" else submitted)(mona, er, solver)

        saved = ok(record_leave(mona, er, "Badr", 9), 201)

        assert saved["shifts_taken_off"] == [] and covers_of(mona, er) == []
        assert Cover.objects.count() == 0


def my_month(hospital_id) -> dict:
    return ok(as_user(hospital_id).get("/api/rosters/mine/month", {"month": MONTH}))["roster"]["hours"]


def listed(page) -> list[str]:
    return [row["hospital_id"] for row in page["check"]["nurses"]]


class TestWhoCouldTakeIt:
    def test_a_shift_day_left_short_by_leave_lists_who_could_with_the_lack_figures_and_mark(self, in_force, mona):
        """Badr's leave leaves the 9th with no Day nurse. Amal and Emad are off; Amal worked Night on the 8th and a Night then a
        Day is forbidden, so only Emad, whose run of Days on the 7th to the 10th is four, the most allowed."""
        ok(record_leave(mona, in_force, "Badr", 9), 201)
        [cover] = covers_of(mona, in_force)

        page = cover_page(mona, in_force, cover["id"])

        assert page["check"]["lack"] == [
            "Below its minimum headcount: 0 of at least 1 (the solver's check: headcount: 2026-06-09 D = 0, want 1..1)."]
        assert page["check"]["full"] is None and page["check"]["reason"] is None
        [emad] = page["check"]["nurses"]
        assert (emad["hospital_id"], emad["full_name"], emad["makes_whole"], emad["replaces_pin"]) == ("Emad", "Emad", True, None)
        # Seventeen Days of 12 hours; 26 working days of 8 hours (the four Fridays are the weekend).
        assert (emad["worked"], emad["owed"], emad["overtime_this_year"]) == (204, 208, 0)
        assert (emad["worked"], emad["owed"]) == (my_month("Emad")["worked"], my_month("Emad")["owed"])

    def test_a_nurse_whose_taking_it_newly_breaks_a_hard_rule_is_left_out(self, in_force, mona):
        """Amal is off on the 9th but worked Night on the 8th: a Day after it breaks the forbidden succession."""
        ok(record_leave(mona, in_force, "Badr", 9), 201)

        assert "Amal" not in listed(cover_page(mona, in_force, covers_of(mona, in_force)[0]["id"]))


def amal_could(page) -> bool:
    assert page["check"]["reason"] is None, page["check"]["reason"]
    return "Amal" in listed(page)


class TestWhoIsLeftOut:
    """Dana works the only Night on the 9th; Amal and Emad are off, and Emad works only Day: so Amal alone could take it."""

    def test_amal_alone_could_take_the_night_on_the_9th(self, in_force, mona):
        page = opened(mona, in_force, 9, "N")

        assert listed(page) == ["Amal"]
        assert page["check"]["lack"] == [] and page["check"]["nurses"][0]["makes_whole"]

    def test_a_required_skill_of_hers_expired_before_it_and_the_cover_is_still_judged(self, in_force, mona):
        """Her shift on a barred day is the grid's to break, not a pin the solver refuses."""
        holding("Amal", "BLS", dt.date(2026, 6, 8), required=True)

        assert not amal_could(opened(mona, in_force, 9, "N"))

    def test_her_accepted_request_for_the_day_off(self, in_force, mona):
        opened_window(mona, in_force)
        request = asked(as_user("Amal"), 9)
        request_decided(mona, in_force, request["id"], "accept")

        assert not amal_could(opened(mona, in_force, 9, "N"))

    def test_a_manager_with_a_nurse_profile(self, in_force, mona):
        User.objects.filter(username="Amal").update(role=Role.UNIT_MANAGER)

        assert not amal_could(opened(mona, in_force, 9, "N"))

    def test_a_managers_pin_to_a_day_off_is_noted_as_replaced(self, in_force, mona):
        """With room for two on Day, the manager pins Badr off his Day on the 9th: he could take it back."""
        wider_day(in_force)
        changed(mona, in_force, off("Badr", 9))

        page = opened(mona, in_force, 9, "D")

        rows = {row["hospital_id"]: row["replaces_pin"] for row in page["check"]["nurses"]}
        assert rows == {"Badr": "manager", "Emad": None}

    def test_an_approved_swaps_pin_to_a_day_off_is_noted_as_replaced(self, in_force, mona, nurse):
        """The sound swap puts Emad on Day on the 9th and pins Badr off; with room for two, Badr could take it back."""
        wider_day(in_force)
        swap = proposed(nurse("Badr"), *SOUND_SWAP)
        acted(nurse("Emad"), swap["id"], "accept")
        decided(mona, in_force, swap["id"], "approve")

        page = opened(mona, in_force, 9, "D")

        assert [(row["hospital_id"], row["replaces_pin"]) for row in page["check"]["nurses"]] == [("Badr", "swap")]


class TestTheShiftDay:
    def test_a_rule_the_roster_already_breaks_does_not_count_against_her(self, in_force, mona):
        """Night wants three on the 9th and has one: with Amal it has two, still short but less, so she is listed, unmarked."""
        in_force.rules["headcounts"][1].update(weekday_min=3, weekday_max=3)
        in_force.save()

        page = opened(mona, in_force, 9, "N")

        assert page["check"]["lack"] == [
            "Below its minimum headcount: 1 of at least 3 (the solver's check: headcount: 2026-06-09 N = 1, want 3..3)."]
        assert [(row["hospital_id"], row["makes_whole"]) for row in page["check"]["nurses"]] == [("Amal", False)]

    def test_the_mark_is_on_who_makes_it_whole(self, in_force, mona):
        """With a team leader required, Dana alone on Night on the 9th is not one, and Amal is."""
        rules_require(in_force, require_team_leader=True)

        page = opened(mona, in_force, 9, "N")

        assert page["check"]["lack"] == ["No team leader (the solver's check: team leader: 2026-06-09 N has none)."]
        assert [(row["hospital_id"], row["makes_whole"]) for row in page["check"]["nurses"]] == [("Amal", True)]

    def test_one_who_does_not_make_it_whole_is_listed_unmarked(self, in_force, mona):
        """Badr works the only Night on the 23rd and leads no team; Dana could join him, and leads none either."""
        rules_require(in_force, require_team_leader=True)

        page = opened(mona, in_force, 23, "N")

        assert page["check"]["lack"] == ["No team leader (the solver's check: team leader: 2026-06-23 N has none)."]
        assert [(row["hospital_id"], row["makes_whole"]) for row in page["check"]["nurses"]] == [("Dana", False)]

    def test_a_shift_day_at_its_maximum_lists_nobody_and_says_why(self, in_force, mona):
        """Day holds exactly one nurse, and Badr works it on the 9th; it lacks a team leader, which one more cannot bring."""
        rules_require(in_force, require_team_leader=True)

        page = opened(mona, in_force, 9, "D")

        assert page["check"]["nurses"] == []
        assert page["check"]["full"] == ("Day on 9 June 2026 already has 1 of at most 1, so one more nurse would break its "
                                         "headcount. To change who works it, edit the roster.")
        assert page["check"]["lack"] == ["No team leader (the solver's check: team leader: 2026-06-09 D has none)."]


class TestASkillNeed:
    """Night needs a nurse holding ICU every day. Dana works the only Night on the 9th, and Amal alone could join her."""

    SHORT_OF_ICU = ("Short of a skill it needs (the solver's check: skill need: 2026-06-09 N has 0 holding ICU, needs 1 "
                    "(1 short)).")

    def test_a_need_left_short_is_in_the_lack_and_one_who_does_not_hold_the_skill_is_listed_unmarked(self, in_force, mona):
        night_needs(in_force, "ICU")

        page = opened(mona, in_force, 9, "N")

        assert page["check"]["lack"] == [self.SHORT_OF_ICU]
        assert [(row["hospital_id"], row["makes_whole"]) for row in page["check"]["nurses"]] == [("Amal", False)]

    def test_the_mark_is_on_who_holds_the_skill(self, in_force, mona):
        night_needs(in_force, "ICU")
        holding("Amal", "ICU", None, expires=False)

        page = opened(mona, in_force, 9, "N")

        assert page["check"]["lack"] == [self.SHORT_OF_ICU]
        assert [(row["hospital_id"], row["makes_whole"]) for row in page["check"]["nurses"]] == [("Amal", True)]

    def test_one_who_holds_the_skill_and_leaves_a_need_of_two_one_short_is_listed_unmarked(self, in_force, mona):
        """A need broken less is not broken anew, so she is on the list; it is still short with her, so she does not make
        the shift-day whole."""
        night_needs(in_force, "ICU", least=2)
        holding("Amal", "ICU", None, expires=False)

        page = opened(mona, in_force, 9, "N")

        assert page["check"]["lack"] == [
            "Short of a skill it needs (the solver's check: skill need: 2026-06-09 N has 0 holding ICU, needs 2 (2 short))."]
        assert [(row["hospital_id"], row["makes_whole"]) for row in page["check"]["nurses"]] == [("Amal", False)]

    def test_each_need_left_short_is_a_line_and_one_still_short_with_her_leaves_her_unmarked(self, in_force, mona):
        """Night needs a nurse holding Triage too, which Amal does not."""
        night_needs(in_force, "ICU", "Triage")
        holding("Amal", "ICU", None, expires=False)

        page = opened(mona, in_force, 9, "N")

        assert page["check"]["lack"] == [
            self.SHORT_OF_ICU,
            "Short of a skill it needs (the solver's check: skill need: 2026-06-09 N has 0 holding Triage, needs 1 (1 short))."]
        assert [(row["hospital_id"], row["makes_whole"]) for row in page["check"]["nurses"]] == [("Amal", False)]

    def test_a_need_that_is_priced_and_not_required_is_no_lack(self, in_force, mona):
        """A soft need left short costs the roster and breaks no rule, so the shift-day lacks nothing with or without her."""
        night_needs(in_force, "ICU", hard=False)

        page = opened(mona, in_force, 9, "N")

        assert page["check"]["lack"] == []
        assert [(row["hospital_id"], row["makes_whole"]) for row in page["check"]["nurses"]] == [("Amal", True)]


class TestTheRanking:
    """With room for two on Day, Badr and Dana could take Day on the 1st. Badr has worked 204 hours of 208 owed; Dana, who
    breastfeeds and is owed an hour a day less, 180 of 182."""

    @pytest.mark.parametrize("figures, order", [
        ({}, ["Badr", "Dana"]),  # 4 below owed, then 2 below
        ({"Dana": {"hours_balance": 2}}, ["Badr", "Dana"]),  # both 4 below, no overtime: by name
        ({"Dana": {"hours_balance": 2}, "Badr": {"ytd_overtime": 10}}, ["Dana", "Badr"]),  # both 4 below: least overtime
        ({"Dana": {"hours_balance": 6}}, ["Dana", "Badr"]),  # 8 below, then 4
    ], ids=["below owed", "then name", "then overtime", "below owed again"])
    def test_furthest_below_owed_hours_first_then_least_overtime_this_year_then_name(self, in_force, mona, figures, order):
        wider_day(in_force)
        for hospital_id, values in figures.items():
            NurseProfile.objects.filter(user__username=hospital_id).update(**values)

        page = opened(mona, in_force, 1, "D")

        assert listed(page) == order
        for row in page["check"]["nurses"]:
            assert (row["worked"], row["owed"]) == (my_month(row["hospital_id"])["worked"], my_month(row["hospital_id"])["owed"])
            assert row["overtime_this_year"] == figures.get(row["hospital_id"], {}).get("ytd_overtime", 0)


class TestOpenedByHand:
    def test_the_manager_opens_one_for_a_shift_day_of_the_roster_in_force_today_or_later(self, in_force, mona):
        page = opened(mona, in_force, 9, "N")

        cover = page["cover"]
        assert (cover["date"], cover["shift"], cover["state"], cover["opened"], cover["opened_on"]) == (
            "2026-06-09", "N", "open", "manager", 1)
        assert [row["id"] for row in covers_of(mona, in_force)] == [cover["id"]]
        assert audited() == []

    def test_two_on_one_shift_day_are_two_covers(self, in_force, mona):
        opened(mona, in_force, 9, "N")
        opened(mona, in_force, 9, "N")

        assert [(row["date"], row["shift"]) for row in covers_of(mona, in_force)] == [("2026-06-09", "N")] * 2

    def test_a_date_passed_is_refused(self, in_force, mona, on_day):
        on_day(2026, 6, 10)

        assert refusals(open_cover(mona, in_force, 9, "N")) == ["9 June 2026 has passed, so it cannot be covered."]
        assert opened(mona, in_force, 10, "N")["cover"]["date"] == "2026-06-10", "today may be covered"

    @pytest.mark.parametrize("code", ["A", "ZZ"], ids=["in the catalogue", "unknown"])
    def test_a_shift_the_unit_does_not_work_is_refused(self, in_force, mona, code):
        assert refusals(open_cover(mona, in_force, 9, code)) == [f"{code} is not one of this unit's shifts."]

    def test_a_month_with_no_roster_in_force_is_refused(self, in_force, mona):
        assert refusals(open_cover(mona, in_force, "2026-07-09", "N"), 409) == [
            "The ER roster for July 2026 is not approved yet, so there is nothing to cover."]

    @pytest.mark.parametrize("state", ["draft", "submitted"])
    def test_a_draft_or_a_submitted_roster_is_refused(self, mona, er, solver, state):
        (solved if state == "draft" else submitted)(mona, er, solver)

        assert refusals(open_cover(mona, er, 9, "N"), 409) == [
            "The ER roster for June 2026 is not approved yet, so there is nothing to cover."]
        assert Cover.objects.count() == 0

    def test_another_units_cover_is_not_found(self, in_force, mona, client_of):
        cover = opened(mona, in_force, 9, "N")["cover"]
        other = client_of("omar", Role.UNIT_MANAGER, full_name="Omar")
        theirs = icu(User.objects.get(username="omar"))

        assert other.get(f"/api/units/{theirs.pk}/covers/{cover['id']}").status_code == 404
        assert withdraw(other, theirs, cover["id"]).status_code == 404
        assert other.get(f"/api/units/{in_force.pk}/covers").status_code == 403, "not their unit"


def audited() -> list[tuple[str, str, str]]:
    return [(entry.action, entry.actor.username if entry.actor else "", entry.target)
            for entry in AuditEntry.objects.filter(action__startswith="cover.").order_by("id")]


class TestWithdrawing:
    @pytest.mark.parametrize("state", ["open", "offered"])
    def test_the_manager_withdraws_a_waiting_cover_with_an_entry_naming_the_unit(self, in_force, mona, state):
        cover = opened(mona, in_force, 9, "N")["cover"]
        if state == "offered":
            offered(mona, in_force, cover["id"], "Amal")

        page = ok(withdraw(mona, in_force, cover["id"]))

        assert (page["cover"]["state"], page["cover"]["ended_by"], page["check"]) == ("withdrawn", "Mona Ali", None)
        assert page["cover"]["ended_at"] is not None
        assert audited()[-1] == ("cover.withdrawn", "mona", "ER")

    def test_an_ended_cover_cannot_be_withdrawn(self, in_force, mona):
        cover = opened(mona, in_force, 9, "N")["cover"]
        ok(withdraw(mona, in_force, cover["id"]))

        assert refusals(withdraw(mona, in_force, cover["id"]), 409) == ["This cover has already been withdrawn."]
        assert len(audited()) == 1


class TestExpiry:
    def test_every_page_reads_a_date_passed_and_the_job_stores_it_silently(self, in_force, mona, on_day):
        ok(record_leave(mona, in_force, "Badr", 9), 201)
        cover_id = covers_of(mona, in_force)[0]["id"]
        on_day(2026, 6, 10)

        assert [row["state"] for row in covers_of(mona, in_force)] == ["expired"]
        page = cover_page(mona, in_force, cover_id)
        assert (page["cover"]["state"], page["check"]) == ("expired", None)
        assert Cover.objects.get().state == CoverState.OPEN, "only the job or a write stores it"

        expire_covers(0)
        expire_covers(0)

        cover = Cover.objects.get()
        assert (cover.state, cover.ended_by) == (CoverState.EXPIRED, None) and cover.ended_at is not None
        assert audited() == []

    def test_a_withdrawal_reads_the_date_first(self, in_force, mona, on_day):
        cover = opened(mona, in_force, 9, "N")["cover"]
        on_day(2026, 6, 10)

        assert refusals(withdraw(mona, in_force, cover["id"]), 409) == [
            "This cover's date has passed, so there is nothing to withdraw."]
        assert Cover.objects.get().state == CoverState.EXPIRED and audited() == []

    def test_a_cover_today_has_not_expired(self, in_force, mona, on_day):
        cover = opened(mona, in_force, 9, "N")["cover"]
        on_day(2026, 6, 9)
        expire_covers(0)

        assert cover_page(mona, in_force, cover["id"])["cover"]["state"] == "open"


def states() -> list[str]:
    return list(Cover.objects.order_by("opened_at", "id").values_list("state", flat=True))


class TestFilled:
    def test_a_managers_edit_putting_a_nurse_on_its_shift_day_fills_it_silently(self, in_force, mona):
        ok(record_leave(mona, in_force, "Badr", 9), 201)

        changed(mona, in_force, pin("Emad", 9, "D"))

        [cover] = covers_of(mona, in_force)
        assert (cover["state"], cover["ended_by"], cover["taker"]) == ("filled", None, None)
        assert cover_page(mona, in_force, cover["id"])["check"] is None
        assert audited() == []

    def test_one_nurse_added_fills_one_cover_of_the_shift_day_the_oldest(self, in_force, mona):
        """With room for two on Day, Badr's leave opens one cover and the manager a second; one nurse added fills the first."""
        wider_day(in_force)
        ok(record_leave(mona, in_force, "Badr", 9), 201)
        opened(mona, in_force, 9, "D")

        changed(mona, in_force, pin("Emad", 9, "D"))

        assert states() == ["filled", "open"]

    def test_a_version_taking_a_nurse_off_or_adding_one_elsewhere_fills_nothing(self, in_force, mona):
        """Leave takes Badr off Day on the 9th; the manager then puts Amal on Night on the 10th, another shift-day."""
        ok(record_leave(mona, in_force, "Badr", 9), 201)
        opened(mona, in_force, 9, "N")

        changed(mona, in_force, pin("Amal", 10, "N"))

        assert states() == ["open", "open"]

    def test_an_approved_swap_putting_a_nurse_on_its_shift_day_fills_it(self, in_force, mona, nurse):
        """Amal swaps her Night on the 13th, beside Dana's, for Night on the 9th, beside Dana's there: a swap of her own cells,
        so it goes straight to the manager."""
        opened(mona, in_force, 9, "N")
        opened(mona, in_force, 13, "N")
        swap = proposed(nurse("Amal"), swap_cell("Amal", 9, "N"), swap_cell("Amal", 13))

        decided(mona, in_force, swap["id"], "approve")

        assert {(row["date"], row["state"]) for row in covers_of(mona, in_force)} == {
            ("2026-06-09", "filled"), ("2026-06-13", "open")}
