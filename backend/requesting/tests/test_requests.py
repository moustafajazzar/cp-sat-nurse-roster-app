"""Request windows and requests through the API: the window's dates on the hospital's clock, the cap, each state a request moves
through, who may do what, who is told, what the audit log and notifications keep, and the pins accepted requests become in a
solve. The scene is the solver's four-nurse sample as ER (Amal, Badr, Dana, Emad, who may work Day only), rostering June 2026
from 15 May."""
import datetime as dt

import pytest
from procrastinate.contrib.django import app

from accounts.models import Role, User
from audit.models import AuditEntry
from conftest import post
from hospital.models import LeaveType
from notifications.models import Notification
from notifications.tests.test_events import as_user, told
from nurses.models import NurseProfile, Placement
from nurses.tests.conftest import make_unit
from nurses import membership
from requesting import deciding
from requesting.deciding import MONTH_OVER
from requesting.making import NO_CAP
from requesting.models import RequestWindow
from requesting.tasks import open_and_close_request_windows
from requesting.tests.conftest import (MONTH, ask, asked, decide, decided, mine, ok, open_window, opened, refused,
                                       requests_in, url, windows)
from rosters.drafts import FROZEN
from rosters.models import PinEnded, PinnedCell, PinOrigin, RosterState, VersionOrigin
from rosters.pins import pin_cell
from rosters.tests.test_pins import grid, input_row, month_of, pin, profile, save, saved, solved
from rosters.tests.test_review import approved, roster, submit
from rosters.tests.test_review import solved as review_solved
from solves.input import assemble
from solves.tests.conftest import nurse_in
from units.models import Unit

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _scene(before_june, solver):
    pass


class TestATracer:
    def test_a_window_opened_today_tells_the_nurses_and_takes_a_request_the_manager_sees(self, mona, er):
        window = opened(mona, er)

        amal = as_user("Amal")
        assert [note["text"] for note in told(amal, "window_opened")] == [
            "Mona Ali opened requests for the ER roster for June 2026, until 25 May 2026."]
        made = asked(amal, 3, reason="My sister's wedding.")

        assert made["state"] == "pending"
        assert mine(amal)["requests"][0]["id"] == made["id"]
        rows = requests_in(mona, er, window["id"])
        assert [(row["hospital_id"], row["date"], row["shift"], row["state"]) for row in rows] == [
            ("Amal", "2026-06-03", "", "pending")]
        assert Notification.objects.filter(event="window_opened").count() == 4


def texts(client, *events) -> list[str]:
    return [note["text"] for note in told(client, *events)]


def hourly_job() -> None:
    open_and_close_request_windows(timestamp=0)


class TestTheWindowsDates:
    """Open and close dates are days on the hospital's clock, `membership.today`, which the scene sets to 15 May 2026."""

    def test_a_window_opening_later_waits_and_tells_nobody_until_its_date(self, mona, er):
        window = opened(mona, er, opens="2026-05-20")

        assert window["state"] == "scheduled"
        assert Notification.objects.filter(event="window_opened").count() == 0
        assert refused(ask(as_user("Amal"), 3)) == "No request window is open for June 2026."

    def test_the_hourly_job_opens_it_on_its_date_and_tells_the_nurses_once(self, mona, er, on_day):
        opened(mona, er, opens="2026-05-20")
        on_day(2026, 5, 19)
        hourly_job()
        assert Notification.objects.filter(event="window_opened").count() == 0

        on_day(2026, 5, 20)
        hourly_job()
        hourly_job()

        assert texts(as_user("Badr"), "window_opened") == [
            "Mona Ali opened requests for the ER roster for June 2026, until 25 May 2026."]
        assert Notification.objects.filter(event="window_opened").count() == 4
        assert windows(mona, er)["windows"][0]["state"] == "open"

    def test_pages_and_requests_follow_the_dates_without_waiting_for_the_job(self, mona, er, on_day):
        opened(mona, er, opens="2026-05-20")
        on_day(2026, 5, 20)
        amal = as_user("Amal")

        assert mine(amal)["window"]["state"] == "open"
        asked(amal, 3)

        # The write opened it first and told the nurses, so the job finds nothing left to do.
        assert Notification.objects.filter(event="window_opened").count() == 4
        hourly_job()
        assert Notification.objects.filter(event="window_opened").count() == 4

    def test_the_close_date_is_the_last_day_requests_are_taken(self, mona, er, on_day):
        opened(mona, er, closes="2026-05-25")
        amal = as_user("Amal")
        on_day(2026, 5, 25)
        asked(amal, 3)

        on_day(2026, 5, 26)

        assert mine(amal)["window"]["state"] == "closed"
        assert refused(ask(amal, 4)) == "No request window is open for June 2026."

    def test_closing_by_date_tells_the_manager_once_of_what_waits_counted_when_read(self, mona, er, on_day):
        opened(mona, er, closes="2026-05-25")
        asked(as_user("Amal"), 3)
        asked(as_user("Badr"), 4)
        on_day(2026, 5, 26)

        hourly_job()
        hourly_job()

        assert texts(mona, "window_closed") == [
            "The request window for the ER roster for June 2026 has closed, and 2 requests wait for your decision."]
        assert told(mona, "window_closed")[0]["link"] == {"page": "requests", "month": "2026-06", "unit_id": None, "cover_id": None}
        rows = requests_in(mona, er, windows(mona, er)["windows"][0]["id"])
        decided(mona, er, rows[0]["id"], "reject", reason="Short of staff.")
        assert texts(mona, "window_closed") == [
            "The request window for the ER roster for June 2026 has closed, and 1 request waits for your decision."]

    def test_a_window_closing_with_nothing_waiting_tells_nobody(self, mona, er, on_day):
        opened(mona, er)
        on_day(2026, 5, 26)

        hourly_job()

        assert Notification.objects.filter(event="window_closed").count() == 0
        assert windows(mona, er)["windows"][0]["state"] == "closed"

    def test_a_window_whose_dates_passed_unseen_closes_without_telling_anyone_it_opened(self, mona, er, on_day):
        opened(mona, er, opens="2026-05-20", closes="2026-05-22")
        on_day(2026, 5, 23)

        hourly_job()

        assert Notification.objects.count() == 0
        assert RequestWindow.objects.get().state == "closed"

    def test_the_job_runs_hourly(self):
        tasks = {task.task.name: task.cron for task in app.periodic_registry.periodic_tasks.values()}
        assert tasks["requesting.tasks.open_and_close_request_windows"] == "10 * * * *"


class TestOpeningAWindow:
    @pytest.mark.parametrize("month, opens, closes, field, words", [
        ("2026-05", "2026-05-15", "2026-05-20", "month", "Choose a coming month: requests are for a month not yet begun."),
        ("2026-06", "2026-05-14", "2026-05-20", "opens_on", "Choose today, 15 May 2026, or a later day."),
        ("2026-06", "2026-05-20", "2026-05-19", "closes_on", "The close date cannot be before the open date."),
        ("2026-06", "2026-05-20", "2026-06-01", "closes_on",
         "Requests have to close before June 2026 begins, so that its roster can be made: choose 31 May 2026 or earlier."),
    ])
    def test_its_dates_are_refused_where_they_make_no_sense(self, mona, er, month, opens, closes, field, words):
        response = open_window(mona, er, month, opens, closes)

        assert response.status_code == 422, response.content
        assert [(error["loc"][-1], error["msg"]) for error in response.json()["detail"]] == [(field, words)]
        assert RequestWindow.objects.count() == 0

    def test_a_unit_whose_two_caps_are_0_cannot_open_one(self, mona, er):
        Unit.objects.filter(pk=er.pk).update(request_cap=0, preference_cap=0)

        assert refused(open_window(mona, er)) == NO_CAP
        assert windows(mona, er)["open_refusal"] == NO_CAP

    @pytest.mark.parametrize("opens, words", [
        ("2026-05-15", "The request window for June 2026 is open until 25 May 2026. A unit has one window at a time: close it "
                       "first."),
        ("2026-05-20", "The request window for June 2026 opens on 20 May 2026. A unit has one window at a time: close it first."),
    ])
    def test_a_unit_has_one_window_waiting_or_open_at_a_time_whatever_its_month(self, mona, er, opens, words):
        opened(mona, er, opens=opens)

        assert refused(open_window(mona, er, "2026-07", "2026-05-26", "2026-06-20")) == words
        assert windows(mona, er)["open_refusal"] == "A unit has one request window at a time: close this one first."

    def test_once_one_has_closed_another_may_target_the_same_month_and_the_cap_counts_both(self, mona, er):
        first = opened(mona, er)
        amal = as_user("Amal")
        asked(amal, 3)
        asked(amal, 4)
        ok(post(mona, url(er, f"/request-windows/{first['id']}/close"), {}))

        second = opened(mona, er, opens="2026-05-15", closes="2026-05-28")
        asked(amal, 5)

        assert refused(ask(amal, 6)) == ("You have 3 requests for June 2026, the most this unit allows. Withdraw a pending one "
                                         "to make another.")
        assert [row["date"] for row in requests_in(mona, er, second["id"])] == ["2026-06-05"]
        assert [row["requests"] for row in windows(mona, er)["windows"]] == [1, 2]

    def test_opening_is_audited_with_its_dates_and_nothing_of_anyone(self, mona, er):
        opened(mona, er)

        entry = AuditEntry.objects.get(action="request_window.opened")
        assert (entry.actor.username, entry.target, entry.after) == (
            "mona", "ER, 2026-06", {"opens_on": "2026-05-15", "closes_on": "2026-05-25"})

    def test_a_nurse_due_to_join_is_told_and_one_of_another_unit_is_not(self, mona, er, make_user):
        icu = make_unit("ICU")
        joining = nurse_in(icu, make_user, "Faten", full_name="Faten Joining")
        Placement.objects.create(nurse=joining, unit=er, from_month=dt.date(2026, 6, 1))
        nurse_in(icu, make_user, "Ghada", full_name="Ghada Elsewhere")

        opened(mona, er)

        told_users = set(Notification.objects.filter(event="window_opened").values_list("user__username", flat=True))
        assert told_users == {"Amal", "Badr", "Dana", "Emad", "Faten"}
        asked(as_user("Faten"), 3)


class TestClosingEarly:
    def test_an_open_window_closes_and_its_nurses_are_told_while_its_requests_wait(self, mona, er):
        window = opened(mona, er)
        amal = as_user("Amal")
        asked(amal, 3)

        closed = ok(post(mona, url(er, f"/request-windows/{window['id']}/close"), {}))

        assert (closed["state"], closed["closed_early_by"], closed["pending"]) == ("closed", "Mona Ali", 1)
        assert texts(amal, "window_closed_early") == [
            "Mona Ali closed the request window for the ER roster for June 2026 early. Any request you made in it waits for a "
            "decision."]
        assert refused(ask(amal, 4)) == "No request window is open for June 2026."
        assert AuditEntry.objects.filter(action="request_window.closed", target="ER, 2026-06").count() == 1
        assert Notification.objects.filter(event="window_closed").count() == 0

    def test_one_that_never_opened_closes_telling_nobody(self, mona, er):
        window = opened(mona, er, opens="2026-05-20")

        ok(post(mona, url(er, f"/request-windows/{window['id']}/close"), {}))

        assert Notification.objects.count() == 0

    def test_a_closed_window_cannot_close_again(self, mona, er):
        window = opened(mona, er)
        ok(post(mona, url(er, f"/request-windows/{window['id']}/close"), {}))

        assert refused(post(mona, url(er, f"/request-windows/{window['id']}/close"), {})) == "This window has closed already."


class TestTheCap:
    """`Unit.request_cap` requests a nurse a month, counting those pending or accepted."""

    def test_a_nurse_makes_up_to_the_cap_and_no_more(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        for day in (3, 4, 5):
            asked(amal, day)

        assert refused(ask(amal, 6)) == ("You have 3 requests for June 2026, the most this unit allows. Withdraw a pending one "
                                         "to make another.")
        seen = mine(amal)
        assert (seen["request_cap"], seen["counted"], seen["refusal"]) == (3, 3, (
            "You have 3 requests for June 2026, the most this unit allows. Withdraw a pending one to make another."))
        asked(as_user("Badr"), 6)  # A colleague's own cap.

    def test_withdrawn_rejected_and_cancelled_ones_free_a_place_and_accepted_ones_do_not(self, mona, er):
        window = opened(mona, er)
        amal = as_user("Amal")
        made = [asked(amal, day) for day in (3, 4, 5)]
        ok(post(amal, f"/api/requests/mine/{made[0]['id']}/withdraw", {}))
        decided(mona, er, made[1]["id"], "reject", reason="Short of staff.")
        decided(mona, er, made[2]["id"], "accept")

        asked(amal, 6)
        asked(amal, 7)
        assert refused(ask(amal, 8)).startswith("You have 3 requests")

        decided(mona, er, made[2]["id"], "cancel", reason="Needed after all.")
        asked(amal, 8)
        assert len(requests_in(mona, er, window["id"])) == 6

    def test_a_cap_lowered_mid_window_keeps_the_requests_made_and_stops_new_ones(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        for day in (3, 4, 5):
            asked(amal, day)
        Unit.objects.filter(pk=er.pk).update(request_cap=2)

        assert refused(ask(amal, 6)).startswith("You have 3 requests for June 2026")
        assert [row["state"] for row in mine(amal)["requests"]] == ["pending"] * 3
        assert mine(amal)["request_cap"] == 2


def window_id(client, unit) -> str:
    return windows(client, unit)["windows"][0]["id"]


def row_of(client, unit, request_id) -> dict:
    return next(row for row in requests_in(client, unit, window_id(client, unit)) if row["id"] == request_id)


def current_pin(nurse, day) -> PinnedCell | None:
    return PinnedCell.objects.current().filter(nurse__user__username=nurse, date=dt.date(2026, 6, day)).first()


def leave_for(client, unit, nurse, day):
    return post(client, url(unit, "/leave"), {
        "nurse_id": str(profile(nurse).pk), "leave_type_id": str(LeaveType.objects.get(name="Sick").pk),
        "first": f"2026-06-{day:02d}", "last": f"2026-06-{day:02d}"})


class TestWhatANurseMayRequest:
    def test_a_day_off_or_a_shift_the_unit_works_and_they_work_with_a_reason_of_their_own(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")

        off = asked(amal, 3, reason="A family wedding.")
        night = asked(amal, 4, "N")

        assert (off["shift"], night["shift"]) == ("", "N")
        assert off["steps"][0] | {"at": None} == {"step": "made", "by": "Amal", "at": None, "reason": "A family wedding.",
                                                  "shift": None}
        seen = mine(amal)
        assert [shift["code"] for shift in seen["shifts"]] == ["D", "N"]
        assert (seen["first_day"], seen["last_day"]) == ("2026-06-01", "2026-06-30")

    def test_with_no_month_given_it_reads_the_current_month_as_the_rest_of_my_month_does(self, mona, er):
        amal = as_user("Amal")

        assert ok(amal.get("/api/requests/mine"))["month"] == "2026-05"
        assert ok(amal.get("/api/rosters/mine/month"))["month"] == "2026-05"

    def test_a_shift_they_do_not_work_is_refused_and_not_offered(self, mona, er):
        opened(mona, er)
        emad = as_user("Emad")  # Day only, in the sample.

        response = ask(emad, 3, "N")

        assert response.status_code == 422, response.content
        assert response.json()["detail"][0]["msg"] == "N is not one of the shifts you work."
        assert [shift["code"] for shift in mine(emad)["shifts"]] == ["D"]

    def test_a_shift_the_unit_does_not_work_is_refused(self, mona, er):
        opened(mona, er)

        assert refused(ask(as_user("Amal"), 3, "XX"), 422) == "XX is not one of the ER unit's shifts."

    def test_only_a_date_of_the_month_an_open_window_targets(self, mona, er):
        opened(mona, er)

        assert refused(ask(as_user("Amal"), 3, month="2026-07")) == "No request window is open for July 2026."

    def test_a_date_outside_their_dates_is_refused(self, mona, er):
        opened(mona, er)
        NurseProfile.objects.filter(user__username="Amal").update(end_date=dt.date(2026, 6, 20))
        amal = as_user("Amal")

        assert refused(ask(amal, 21), 422) == "21 June 2026 is outside your dates on the roster."
        assert mine(amal)["last_day"] == "2026-06-20"

    def test_one_request_a_date_until_it_is_withdrawn_or_decided_against(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        first = asked(amal, 3)

        assert refused(ask(amal, 3, "N"), 422) == (
            "You already have a request on 3 June 2026. Withdraw it to ask for something else.")
        decided(mona, er, first["id"], "reject", reason="Short of staff.")
        asked(amal, 3, "N")

    def test_a_day_already_on_leave_needs_no_request(self, mona, er):
        opened(mona, er)
        ok(leave_for(mona, er, "Amal", 3), 201)

        assert refused(ask(as_user("Amal"), 3), 422) == (
            "You are on leave on 3 June 2026, so there is nothing to request that day.")

    def test_a_shift_on_a_day_an_expired_required_skill_bars_is_refused_in_her_own_words_and_a_day_off_is_not(self, mona, er):
        """She reads her own skills on My month, so her reason is said in full; a day off is hers to ask for."""
        from rosters.tests.test_review import holding
        opened(mona, er)
        holding("Amal", "BLS", dt.date(2026, 6, 15), required=True)
        amal = as_user("Amal")

        response = ask(amal, 20, "N")

        assert refused(response, 422) == (
            "Your BLS, required to work, counted until 15 June 2026, so you can work no shift on 20 June 2026. "
            "You can still ask for the day off.")
        assert response.json()["detail"][0]["loc"][-1] == "shift"
        asked(amal, 15, "N")
        made = asked(amal, 20)
        decided(mona, er, made["id"], "accept")
        assert current_pin("Amal", 20).shift is None

    def test_one_accepted_before_the_expiry_is_set_aside_with_its_pin_and_none_is_accepted_after_it(self, mona, er):
        """The manager reads why, naming the skill, as for a pin of his own."""
        from rosters.tests.test_review import holding
        opened(mona, er)
        amal = as_user("Amal")
        before, after = asked(amal, 20, "N"), asked(amal, 25, "D")
        decided(mona, er, before["id"], "accept")
        holding("Amal", "BLS", dt.date(2026, 6, 15), required=True)

        assert row_of(mona, er, before["id"])["set_aside"] == "barred"
        assert refused(decide(mona, er, after["id"], "accept")) == (
            "Amal's BLS, required to work, counted until 15 June 2026, so Amal can work no shift on 25 June 2026.")

    def test_a_manager_with_a_nurse_profile_and_other_roles_make_none(self, mona, er, carla):
        opened(mona, er)

        assert ask(mona, 3).status_code == 403
        assert ask(carla, 3).status_code == 403


class TestWithdrawing:
    def test_a_pending_one_while_the_window_is_open(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        made = asked(amal, 3)

        withdrawn = ok(post(amal, f"/api/requests/mine/{made['id']}/withdraw", {}))

        assert withdrawn["state"] == "withdrawn"
        assert [step["step"] for step in withdrawn["steps"]] == ["made", "withdrawn"]
        assert row_of(mona, er, made["id"])["state"] == "withdrawn"

    def test_not_once_the_window_has_closed(self, mona, er, on_day):
        opened(mona, er)
        amal = as_user("Amal")
        made = asked(amal, 3)
        on_day(2026, 5, 26)

        assert refused(post(amal, f"/api/requests/mine/{made['id']}/withdraw", {})) == (
            "The request window has closed, so this request waits for your manager's decision.")

    def test_not_once_decided(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        made = asked(amal, 3)
        decided(mona, er, made["id"], "accept")

        assert refused(post(amal, f"/api/requests/mine/{made['id']}/withdraw", {})) == (
            "This request is accepted, so it cannot be withdrawn.")

    def test_a_colleagues_is_the_same_404_as_none(self, mona, er):
        opened(mona, er)
        made = asked(as_user("Amal"), 3)

        assert post(as_user("Badr"), f"/api/requests/mine/{made['id']}/withdraw", {}).status_code == 404
        assert row_of(mona, er, made["id"])["state"] == "pending"


class TestDeciding:
    def test_accepting_makes_a_pin_of_origin_request_that_reaches_the_solve_and_tells_only_her(self, mona, er, solver):
        opened(mona, er)
        amal = as_user("Amal")
        made = asked(amal, 3, "N")

        answer = decided(mona, er, made["id"], "accept", reason="Fine by me.")

        assert (answer["request"]["state"], answer["request"]["promised"], answer["version"]) == ("accepted", "N", None)
        pin = current_pin("Amal", 3)
        assert (pin.code, pin.origin, pin.pinned_by.username) == ("N", PinOrigin.REQUEST, "mona")
        assert input_row(assemble(er, dt.date(2026, 6, 1)).input, "Amal")["pinned"] == {"2026-06-03": "N"}
        assert texts(amal, "request_decided") == ["Mona Ali accepted your request for Night (N) on 3 June 2026."]
        assert told(amal, "request_decided")[0]["link"] == {"page": "my_month", "month": "2026-06", "unit_id": None, "cover_id": None}
        assert [note for note in told(as_user("Badr")) if note["event"] != "window_opened"] == []
        assert mine(amal)["requests"][0]["steps"][-1]["reason"] == "Fine by me."

    def test_rejecting_needs_a_reason_she_reads_on_her_page_and_not_in_her_bell(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        made = asked(amal, 3)

        assert refused(decide(mona, er, made["id"], "reject", reason="  "), 422) == (
            "Say why: the nurse reads it with her request.")
        decided(mona, er, made["id"], "reject", reason="Two others are off that day.")

        assert texts(amal, "request_decided") == [
            "Mona Ali rejected your request for a day off on 3 June 2026. The reason is on My month."]
        steps = mine(amal)["requests"][0]["steps"]
        assert [(step["step"], step["by"], step["reason"]) for step in steps] == [
            ("made", "Amal", ""), ("rejected", "Mona Ali", "Two others are off that day.")]
        assert current_pin("Amal", 3) is None

    def test_an_accepted_one_can_be_edited_and_cancelled_each_with_a_reason(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        made = asked(amal, 3, "N")
        decided(mona, er, made["id"], "accept")
        first = current_pin("Amal", 3)

        assert refused(decide(mona, er, made["id"], "edit", shift="N", reason="Same.")) == (
            "The request already promises Night (N). Choose something else, or leave it as it is.")
        edited = decided(mona, er, made["id"], "edit", shift="D", reason="Night is full; Day instead.")

        assert (edited["request"]["shift"], edited["request"]["promised"]) == ("N", "D")
        first.refresh_from_db()
        assert first.removed_why == PinEnded.REPLACED
        assert (current_pin("Amal", 3).code, current_pin("Amal", 3).origin) == ("D", PinOrigin.REQUEST)

        cancelled = decided(mona, er, made["id"], "cancel", reason="Needed on Night after all.")

        assert (cancelled["request"]["state"], cancelled["request"]["promised"]) == ("cancelled", None)
        assert current_pin("Amal", 3) is None
        assert texts(amal, "request_decided") == [
            "Mona Ali cancelled your accepted request for Night (N) on 3 June 2026. The reason is on My month.",
            "Mona Ali changed what your accepted request on 3 June 2026 promises. My month says what it is now.",
            "Mona Ali accepted your request for Night (N) on 3 June 2026."]
        assert [(step["step"], step["shift"]) for step in mine(amal)["requests"][0]["steps"]] == [
            ("made", None), ("accepted", "N"), ("edited", "D"), ("cancelled", None)]

    @pytest.mark.parametrize("what, body, from_state, words", [
        ("accept", {}, "rejected", "This request is rejected already."),
        ("reject", {"reason": "No."}, "accepted", "This request is accepted already."),
        ("edit", {"shift": "D", "reason": "No."}, "pending", "Only an accepted request can be changed, and this one is pending."),
        ("cancel", {"reason": "No."}, "withdrawn", "Only an accepted request can be cancelled, and this one is withdrawn."),
    ])
    def test_each_decision_only_from_its_state(self, mona, er, what, body, from_state, words):
        opened(mona, er)
        amal = as_user("Amal")
        made = asked(amal, 3)
        match from_state:
            case "rejected":
                decided(mona, er, made["id"], "reject", reason="No.")
            case "accepted":
                decided(mona, er, made["id"], "accept")
            case "withdrawn":
                ok(post(amal, f"/api/requests/mine/{made['id']}/withdraw", {}))

        assert refused(decide(mona, er, made["id"], what, **body)) == words

    def test_accepting_onto_your_own_pin_replaces_it_and_the_list_says_so_first(self, mona, er, solver):
        solved(mona, er, solver)
        saved(mona, er, pin("Amal", 3, "D"))
        opened(mona, er)
        made = asked(as_user("Amal"), 3, "N")

        assert row_of(mona, er, made["id"])["replaces_pin"] == "D"
        decided(mona, er, made["id"], "accept")

        assert (current_pin("Amal", 3).code, current_pin("Amal", 3).origin) == ("N", PinOrigin.REQUEST)
        assert PinnedCell.objects.get(origin=PinOrigin.MANAGER).removed_why == PinEnded.REPLACED
        assert grid(er)["Amal"]["2026-06-03"] == "N"

    def test_accepting_where_an_accepted_swap_holds_the_cell_is_refused(self, mona, er):
        opened(mona, er)
        made = asked(as_user("Amal"), 3)
        PinnedCell.objects.create(unit=er, nurse=profile("Amal"), date=dt.date(2026, 6, 3), shift=None, origin=PinOrigin.SWAP,
                                  pinned_by=User.objects.get(username="mona"))

        words = "An accepted swap holds this cell on 3 June 2026, so change it there."
        assert row_of(mona, er, made["id"])["accept_refusal"] == words
        assert refused(decide(mona, er, made["id"], "accept")) == words

    def test_accepting_is_refused_in_the_words_a_pin_gets(self, mona, er, solver):
        """Her end date corrected after she asked: `Judge` refuses it as it refuses pinning there."""
        solved(mona, er, solver)
        opened(mona, er)
        made = asked(as_user("Amal"), 21, "N")
        NurseProfile.objects.filter(user__username="Amal").update(end_date=dt.date(2026, 6, 20))

        words = "21 June 2026 is outside Amal's dates, so there is nothing to pin."
        assert row_of(mona, er, made["id"])["accept_refusal"] == words
        assert refused(decide(mona, er, made["id"], "accept")) == words
        assert refused(save(mona, er, pin("Amal", 21, "N")), 422) == words

    def test_a_nurse_transferred_out_after_requesting_keeps_it_on_record_and_it_cannot_be_accepted(self, mona, er):
        opened(mona, er)
        made = asked(as_user("Amal"), 3)
        Placement.objects.create(nurse=profile("Amal"), unit=make_unit("ICU"), from_month=dt.date(2026, 6, 1))

        words = "Amal is not on this unit's roster for June 2026."
        assert row_of(mona, er, made["id"])["accept_refusal"] == words
        assert refused(decide(mona, er, made["id"], "accept")) == words
        decided(mona, er, made["id"], "reject", reason="You have moved to ICU.")

    def test_a_request_left_behind_by_a_transfer_counts_against_its_own_units_cap_only(self, mona, er, client_of):
        opened(mona, er)
        amal = as_user("Amal")
        for day in (3, 4, 5):
            asked(amal, day)
        omar = client_of("omar", Role.UNIT_MANAGER)
        icu = make_unit("ICU", User.objects.get(username="omar"))
        Unit.objects.filter(pk=icu.pk).update(rules=er.rules)
        Placement.objects.create(nurse=profile("Amal"), unit=icu, from_month=dt.date(2026, 6, 1))

        opened(omar, icu)

        assert mine(amal)["counted"] == 0
        asked(amal, 6)
        assert refused(ask(amal, 3), 422) == "You already have a request on 3 June 2026. Withdraw it to ask for something else."

    def test_cancelling_a_request_whose_pin_another_unit_replaced_keeps_that_pins_history(self, mona, er, client_of):
        opened(mona, er)
        made = asked(as_user("Amal"), 3)
        decided(mona, er, made["id"], "accept")
        omar = client_of("omar", Role.UNIT_MANAGER)
        icu = make_unit("ICU", User.objects.get(username="omar"))
        Placement.objects.create(nurse=profile("Amal"), unit=icu, from_month=dt.date(2026, 6, 1))
        pin_cell(icu, profile("Amal"), dt.date(2026, 6, 3), None, PinOrigin.MANAGER, User.objects.get(username="omar"))
        ended = PinnedCell.objects.get(origin=PinOrigin.REQUEST)
        assert (ended.removed_why, ended.removed_by.username) == (PinEnded.REPLACED, "omar")

        decided(mona, er, made["id"], "cancel", reason="Amal has moved to ICU.")

        ended.refresh_from_db()
        assert (ended.removed_why, ended.removed_by.username) == (PinEnded.REPLACED, "omar")

    def test_a_draft_takes_the_accepted_cell_at_once_and_warns_the_solve_lacked_it(self, mona, er, solver):
        solved(mona, er, solver)
        opened(mona, er)
        made = asked(as_user("Amal"), 3)

        decided(mona, er, made["id"], "accept")

        assert grid(er)["Amal"]["2026-06-03"] == ""
        draft = month_of(mona, er)["draft"]
        assert {"hospital_id": "Amal", "full_name": "Amal", "what": "pins"} in draft["changes"]
        shown = {(p["hospital_id"], p["date"], p["shift"], p["origin"]) for p in draft["pins"]}
        assert ("Amal", "2026-06-03", "", "request") in shown

    def test_a_solve_keeps_an_accepted_request(self, mona, er, solver):
        opened(mona, er)
        decided(mona, er, asked(as_user("Amal"), 3, "N")["id"], "accept")

        solved(mona, er, solver)  # The script answers Day on every cell.

        assert input_row(solver.started[-1], "Amal")["pinned"] == {"2026-06-03": "N"}
        assert grid(er)["Amal"]["2026-06-03"] == "N"

    def test_a_submitted_roster_is_frozen_to_acceptance_but_not_to_a_rejection(self, mona, er, solver):
        review_solved(mona, er, solver)
        opened(mona, er)
        first, second = asked(as_user("Amal"), 3), asked(as_user("Amal"), 5)
        ok(submit(mona, er))

        assert refused(decide(mona, er, first["id"], "accept")) == FROZEN[RosterState.SUBMITTED]
        assert row_of(mona, er, first["id"])["accept_refusal"] == FROZEN[RosterState.SUBMITTED]
        decided(mona, er, second["id"], "reject", reason="No.")

    def test_a_month_that_is_over_takes_no_acceptance(self, mona, er, on_day):
        opened(mona, er)
        made = asked(as_user("Amal"), 3)
        on_day(2026, 7, 1)

        assert refused(decide(mona, er, made["id"], "accept")) == MONTH_OVER

    def test_another_units_manager_reaches_none_of_it(self, mona, er, make_user, client_of):
        window = opened(mona, er)
        made = asked(as_user("Amal"), 3)
        omar = client_of("omar", Role.UNIT_MANAGER)
        make_unit("ICU", User.objects.get(username="omar"))

        assert omar.get(url(er, "/request-windows")).status_code == 403
        assert omar.get(url(er, f"/request-windows/{window['id']}/requests")).status_code == 403
        assert decide(omar, er, made["id"], "accept").status_code == 403
        icu = Unit.objects.get(name="ICU")
        assert decide(omar, icu, made["id"], "accept").status_code == 404
        assert omar.get(url(icu, f"/request-windows/{window['id']}/requests")).status_code == 404


def everything_kept_for(user) -> str:
    return " ".join(str(payload) for payload in Notification.objects.filter(user=user).values_list("payload", flat=True))


class TestAfterApproval:
    """In the approved June (`rosters/tests/fixtures/sound_rosters.json`) Amal works Night on the 1st to the 4th and is off on
    the 5th."""

    def test_accepting_a_change_to_a_cell_is_a_new_version_with_origin_request_told_as_a_roster_change(self, mona, carla, er,
                                                                                                        solver):
        approved(mona, carla, er, solver)
        opened(mona, er)
        amal, badr = as_user("Amal"), as_user("Badr")
        made = asked(amal, 3, reason="My daughter's school play.")

        answer = decided(mona, er, made["id"], "accept", reason="Covered by Dana.")

        version = roster(er).versions.first()
        assert (answer["version"], version.number, version.origin, version.created_by.username) == (
            2, 2, VersionOrigin.REQUEST, "mona")
        assert version.reason == "Amal's request for a day off on 3 June 2026 accepted."
        assert version.grid["Amal"]["2026-06-03"] == ""
        assert texts(badr, "roster_changed") == ["Mona Ali changed the ER roster for June 2026."]
        assert texts(amal, "request_decided") == ["Mona Ali accepted your request for a day off on 3 June 2026."]
        assert ok(badr.get("/api/rosters/mine", {"month": MONTH}))["roster"]["version"] == 2

    def test_accepting_what_the_cell_holds_already_makes_no_version_and_tells_only_her(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        opened(mona, er)
        amal = as_user("Amal")

        answer = decided(mona, er, asked(amal, 5)["id"], "accept")

        assert answer["version"] is None
        assert roster(er).versions.count() == 1
        assert Notification.objects.filter(event="roster_changed").count() == 0
        assert texts(amal, "request_decided") == ["Mona Ali accepted your request for a day off on 5 June 2026."]
        assert (current_pin("Amal", 5).code, current_pin("Amal", 5).origin) == ("", PinOrigin.REQUEST)

    def test_editing_after_approval_is_a_version_and_cancelling_changes_no_cell(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        opened(mona, er)
        made = asked(as_user("Amal"), 3)
        decided(mona, er, made["id"], "accept")

        assert decided(mona, er, made["id"], "edit", shift="D", reason="Day instead.")["version"] == 3
        assert roster(er).grid["Amal"]["2026-06-03"] == "D"
        assert decided(mona, er, made["id"], "cancel", reason="Not needed.")["version"] is None
        assert roster(er).versions.count() == 3
        assert roster(er).grid["Amal"]["2026-06-03"] == "D"

    def test_accepting_a_change_to_a_cell_writes_the_roster_change_entry_after_its_own_as_a_swap_does(self, mona, carla, er,
                                                                                                      solver):
        approved(mona, carla, er, solver)
        opened(mona, er)
        made = asked(as_user("Amal"), 3)

        decided(mona, er, made["id"], "accept")

        entries = [(entry.action, entry.actor.username, entry.target, entry.after)
                   for entry in AuditEntry.objects.order_by("id")][-2:]
        assert entries == [("request.accepted", "mona", "Amal", None),
                           ("roster.changed", "mona", "ER, 2026-06", {"version": 2, "pins": 1, "cells": 1})]
        assert AuditEntry.objects.get(action="roster.changed").ip is not None

    def test_a_month_that_ends_while_a_request_is_accepted_still_takes_its_change(self, mona, carla, er, solver, monkeypatch):
        """Midnight of the month's last day falls after the decision was judged and before the roster changes."""
        approved(mona, carla, er, solver)
        opened(mona, er)
        made = asked(as_user("Amal"), 3)
        pin_cell = deciding.pin_cell

        def pin_then_midnight(*args, **kwargs):
            pin = pin_cell(*args, **kwargs)
            monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 7, 1))
            return pin

        monkeypatch.setattr(deciding, "pin_cell", pin_then_midnight)

        assert decided(mona, er, made["id"], "accept")["version"] == 2
        assert roster(er).grid["Amal"]["2026-06-03"] == ""

    def test_editing_one_that_changes_a_cell_writes_it_too_and_what_changes_nothing_writes_none(self, mona, carla, er, solver):
        """Amal is off on the 5th already."""
        approved(mona, carla, er, solver)
        opened(mona, er)
        made = asked(as_user("Amal"), 3)
        decided(mona, er, made["id"], "accept")
        decided(mona, er, asked(as_user("Amal"), 5)["id"], "accept")

        decided(mona, er, made["id"], "edit", shift="D", reason="Day instead.")

        assert list(AuditEntry.objects.filter(action="roster.changed").order_by("id").values_list("after", flat=True)) == [
            {"version": 2, "pins": 1, "cells": 1}, {"version": 3, "pins": 1, "cells": 1}]

    def test_a_nurse_who_joined_after_the_solve_has_no_row_so_only_her_pin_is_made(self, mona, carla, er, solver, make_user):
        approved(mona, carla, er, solver)
        nurse_in(er, make_user, "Faris")
        opened(mona, er)

        answer = decided(mona, er, asked(as_user("Faris"), 3)["id"], "accept")

        assert answer["version"] is None and roster(er).versions.count() == 1
        assert "Faris" not in roster(er).grid
        assert (current_pin("Faris", 3).code, current_pin("Faris", 3).origin) == ("", PinOrigin.REQUEST)
        assert not AuditEntry.objects.filter(action="roster.changed").exists()

    def test_the_managers_editor_refuses_a_requests_pin(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        opened(mona, er)
        decided(mona, er, asked(as_user("Amal"), 3)["id"], "accept")

        response = post(mona, url(er, "/roster/versions"), {"month": MONTH, "edits": [pin("Amal", 3, "D")], "reason": "x"})

        assert refused(response, 422) == "This pin comes from an accepted request, on 3 June 2026, so change it there."


class TestLeaveOverAnAcceptedRequest:
    def test_leave_ends_its_pin_and_it_stays_on_record_replaced_by_leave_and_she_is_told(self, mona, er, solver):
        solved(mona, er, solver)
        opened(mona, er)
        amal = as_user("Amal")
        made = asked(amal, 3, "N")
        decided(mona, er, made["id"], "accept")

        answer = ok(leave_for(mona, er, "Amal", 3), 201)

        assert answer["pins_removed"] == [{"date": "2026-06-03", "shift": "N", "origin": "request", "roster": "draft"}]
        seen = mine(amal)["requests"][0]
        assert (seen["state"], seen["promised"], seen["steps"][-1]["step"]) == ("replaced_by_leave", None, "replaced_by_leave")
        assert texts(amal, "request_decided")[0] == (
            "Leave recorded on 3 June 2026 replaced your request for Night (N), as leave wins.")
        assert current_pin("Amal", 3) is None
        assert [entry.action for entry in AuditEntry.objects.order_by("at")][-1] == "leave.recorded"
        assert mine(amal)["counted"] == 0

    def test_leave_over_a_pending_one_replaces_it_too_and_frees_its_place(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        made = [asked(amal, day) for day in (3, 4, 5)]

        ok(leave_for(mona, er, "Amal", 4), 201)

        assert [row["state"] for row in mine(amal)["requests"]] == ["pending", "replaced_by_leave", "pending"]
        assert texts(amal, "request_decided") == [
            "Leave recorded on 4 June 2026 replaced your request for a day off, as leave wins."]
        asked(amal, 6)
        assert refused(decide(mona, er, made[1]["id"], "accept")) == "This request is replaced by leave already."

    def test_on_an_approved_roster_the_leave_takes_the_shift_off_and_ends_the_request(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        opened(mona, er)
        made = asked(as_user("Amal"), 3, "D")
        assert decided(mona, er, made["id"], "accept")["version"] == 2

        ok(leave_for(mona, er, "Amal", 3), 201)

        assert roster(er).versions.first().number == 3
        assert roster(er).grid["Amal"]["2026-06-03"] == ""
        assert row_of(mona, er, made["id"])["state"] == "replaced_by_leave"


class TestWhatStaysPrivate:
    """A request's reason and the manager's can be about someone's health, and a colleague learns nothing of a request."""

    REASONS = ("My chemotherapy starts.", "Approved for your treatment.", "Your treatment comes first.",
               "Treatment plan changed.")

    def scene(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        accepted = asked(amal, 3, "N", reason=self.REASONS[0])
        decided(mona, er, accepted["id"], "accept", reason=self.REASONS[1])
        decided(mona, er, accepted["id"], "edit", shift="D", reason=self.REASONS[2])
        decided(mona, er, asked(amal, 4)["id"], "reject", reason=self.REASONS[3])
        return amal

    def test_no_notification_payload_keeps_a_reason_a_date_or_a_shift(self, mona, er):
        self.scene(mona, er)

        payloads = " ".join(str(note.payload) for note in Notification.objects.all())
        for secret in (*self.REASONS, "chemotherapy", "2026-06-03", "2026-06-04", "'N'", "'D'"):
            assert secret not in payloads
        keys = {key for note in Notification.objects.filter(event="request_decided") for key in note.payload}
        assert keys == {"unit_id", "month", "request_id", "step", "by"}

    def test_no_notification_text_says_a_reason(self, mona, er):
        amal = self.scene(mona, er)

        said = " ".join(texts(amal) + texts(mona))
        assert not any(reason in said for reason in self.REASONS)

    def test_the_audit_log_says_a_request_moved_and_whose_and_nothing_else(self, mona, er):
        self.scene(mona, er)

        entries = [(entry.action, entry.actor.username, entry.target, entry.before, entry.after)
                   for entry in AuditEntry.objects.filter(action__startswith="request.").order_by("at")]
        assert entries == [
            ("request.made", "Amal", "Amal", None, None),
            ("request.accepted", "mona", "Amal", None, None),
            ("request.edited", "mona", "Amal", None, None),
            ("request.made", "Amal", "Amal", None, None),
            ("request.rejected", "mona", "Amal", None, None),
        ]
        logged = " ".join(str(row) for row in AuditEntry.objects.values_list("target", "before", "after"))
        for secret in (*self.REASONS, "2026-06-03", "2026-06-04"):
            assert secret not in logged

    def test_a_colleague_sees_nothing_of_it_in_her_requests_her_month_her_roster_or_her_bell(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        badr = as_user("Badr")
        before = {path: ok(badr.get(path, {"month": MONTH}))
                  for path in ("/api/rosters/mine/month", "/api/rosters/mine/sheet")}
        self.scene(mona, er)

        assert mine(badr)["requests"] == []
        month_after, month_before = ok(badr.get("/api/rosters/mine/month", {"month": MONTH})), before["/api/rosters/mine/month"]
        # The edit to Day changed Amal's cell, so version 2 is in force: Badr's own hours and everything else are as they were.
        assert month_after["roster"].pop("version") == 2 and month_before["roster"].pop("version") == 1
        assert month_after == month_before
        exported = mona.get(url(er, "/roster/export"), {"month": MONTH, "version": 2, "format": "csv"})
        assert exported.status_code == 200
        assert not any(word in exported.content.decode("utf-8-sig").lower() for word in ("request", "chemotherapy", "treatment"))
        sheet = ok(badr.get("/api/rosters/mine/sheet", {"month": MONTH}))
        assert "request" not in str(sheet).lower()
        for secret in (*self.REASONS, "request_id"):
            assert secret not in everything_kept_for(User.objects.get(username="Badr"))
        assert {note["event"] for note in told(badr)} <= {"roster_approved", "roster_changed", "window_opened"}
