"""Preferences through the API: what a staff nurse would rather have on a date, a day off or a working day, made in the request window
beside her requests, up to the unit's preference cap; withdrawn, or asked for as a request instead; one wish a date across
requests and preferences; what the manager reads; what the audit log keeps and what nobody else sees. The scene is the solver's
four-nurse sample as ER (Amal, Badr, Dana, Emad, who may work Day only), rostering June 2026 from 15 May, as for requests."""
import datetime as dt

import pytest

from accounts.models import Role, User
from audit.models import AuditEntry
from conftest import post
from notifications.models import Notification
from notifications.tests.test_events import as_user
from nurses.models import NurseProfile, Placement
from nurses.tests.conftest import make_unit
from requesting.making import NO_CAP
from requesting.models import Preference
from requesting.tests.conftest import (MONTH, ask, asked, decided, mine, ok, open_window, opened, refused, requests_in, url,
                                       windows)
from requesting.tests.test_requests import leave_for
from rosters.tests.test_pins import month_of, profile, solved
from rosters.tests.test_review import approved
from solves.tests.conftest import nurse_in
from units.models import Unit

pytestmark = pytest.mark.django_db

AT_THE_CAP = "You have 10 preferences for June 2026, the most this unit allows. Withdraw one to make another."


@pytest.fixture(autouse=True)
def _scene(before_june, solver):
    pass


def prefer(client, day, kind="day_off", month=MONTH):
    return post(client, "/api/preferences/mine", {"date": f"{month}-{day:02d}", "kind": kind})


def preferred(client, day, kind="day_off", month=MONTH) -> dict:
    return ok(prefer(client, day, kind, month), 201)


def my_preferences(client, month=MONTH) -> dict:
    return ok(client.get("/api/preferences/mine", {"month": month}))


def withdraw(client, preference_id):
    return post(client, f"/api/preferences/mine/{preference_id}/withdraw", {})


def ask_instead(client, preference_id, shift=None):
    return post(client, f"/api/preferences/mine/{preference_id}/ask-as-request", {"shift": shift})


def preferences_in(client, unit, window) -> list[dict]:
    return ok(client.get(url(unit, f"/request-windows/{window}/preferences")))  # type: ignore[return-value]


def states(client) -> list[tuple[str, str, str]]:
    return [(row["date"], row["kind"], row["state"]) for row in my_preferences(client)["preferences"]]


class TestMakingOne:
    def test_a_day_off_and_a_working_day_with_no_reason_which_the_manager_reads(self, mona, er):
        window = opened(mona, er)
        amal = as_user("Amal")

        off = preferred(amal, 3)
        work = preferred(amal, 4, "working_day")

        assert (off["date"], off["kind"], off["state"]) == ("2026-06-03", "day_off", "made")
        assert (work["kind"], work["state"]) == ("working_day", "made")
        seen = my_preferences(amal)
        assert (seen["preference_cap"], seen["counted"], seen["refusal"]) == (10, 2, None)
        assert (seen["window"]["state"], seen["first_day"], seen["last_day"]) == ("open", "2026-06-01", "2026-06-30")
        assert [(row["hospital_id"], row["full_name"], row["date"], row["kind"], row["state"])
                for row in preferences_in(mona, er, window["id"])] == [
            ("Amal", "Amal", "2026-06-03", "day_off", "made"), ("Amal", "Amal", "2026-06-04", "working_day", "made")]

    def test_a_reason_or_another_kind_is_refused(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")

        assert post(amal, "/api/preferences/mine", {"date": "2026-06-03", "kind": "day_off", "reason": "Why"}).status_code == 422
        assert prefer(amal, 3, "night").status_code == 422
        assert Preference.objects.count() == 0

    def test_only_while_a_window_is_open_for_the_month(self, mona, er, on_day):
        amal = as_user("Amal")
        assert refused(prefer(amal, 3)) == "No request window is open for June 2026."
        assert my_preferences(amal)["refusal"] == "No request window is open for June 2026."

        opened(mona, er, opens="2026-05-20")
        assert refused(prefer(amal, 3)) == "No request window is open for June 2026."

        on_day(2026, 5, 20)
        preferred(amal, 3)
        assert refused(prefer(amal, 3, month="2026-07")) == "No request window is open for July 2026."

    def test_fixed_once_the_window_closes(self, mona, er, on_day):
        opened(mona, er)
        amal = as_user("Amal")
        made = preferred(amal, 3)
        on_day(2026, 5, 26)

        assert refused(prefer(amal, 4)) == "No request window is open for June 2026."
        assert refused(withdraw(amal, made["id"])) == "The request window has closed, so your preferences are fixed."
        assert refused(ask_instead(amal, made["id"])) == "The request window has closed, so your preferences are fixed."
        assert states(amal) == [("2026-06-03", "day_off", "made")]

    def test_a_leave_date_and_a_date_outside_her_dates_are_refused_at_the_form(self, mona, er):
        opened(mona, er)
        ok(leave_for(mona, er, "Amal", 3), 201)
        NurseProfile.objects.filter(user__username="Amal").update(end_date=dt.date(2026, 6, 20))
        amal = as_user("Amal")

        assert refused(prefer(amal, 3), 422) == "You are on leave on 3 June 2026, so there is nothing to prefer that day."
        assert refused(prefer(amal, 21, "working_day"), 422) == "21 June 2026 is outside your dates on the roster."
        assert my_preferences(amal)["last_day"] == "2026-06-20"

    def test_leave_recorded_later_leaves_it_made(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        preferred(amal, 3)

        ok(leave_for(mona, er, "Amal", 3), 201)

        assert states(amal) == [("2026-06-03", "day_off", "made")]

    def test_a_manager_with_a_nurse_profile_and_other_roles_make_none(self, mona, er, carla):
        opened(mona, er)

        for client in (mona, carla):
            assert prefer(client, 3).status_code == 403
            assert client.get("/api/preferences/mine", {"month": MONTH}).status_code == 403

    def test_a_nurse_due_to_join_makes_them_in_the_new_units_window(self, mona, er, make_user):
        icu = make_unit("ICU")
        joining = nurse_in(icu, make_user, "Faten", full_name="Faten Joining")
        Placement.objects.create(nurse=joining, unit=er, from_month=dt.date(2026, 6, 1))
        window = opened(mona, er)

        preferred(as_user("Faten"), 3)

        assert [row["hospital_id"] for row in preferences_in(mona, er, window["id"])] == ["Faten"]


class TestTheCap:
    """`Unit.preference_cap` preferences a nurse a month, counting those made, across the month's windows."""

    def test_up_to_the_cap_and_no_more_and_a_colleague_has_her_own(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        for day in range(1, 11):
            preferred(amal, day, "working_day" if day % 2 else "day_off")

        assert refused(prefer(amal, 11)) == AT_THE_CAP
        seen = my_preferences(amal)
        assert (seen["counted"], seen["refusal"]) == (10, AT_THE_CAP)
        preferred(as_user("Badr"), 11)

    def test_requests_and_preferences_have_caps_of_their_own(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        for day in (1, 2, 3):
            asked(amal, day)

        preferred(amal, 4)
        assert my_preferences(amal)["counted"] == 1
        assert mine(amal)["counted"] == 3

    def test_withdrawn_and_asked_instead_free_a_place(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        made = [preferred(amal, day) for day in range(1, 11)]

        ok(withdraw(amal, made[0]["id"]))
        preferred(amal, 11)
        ok(ask_instead(amal, made[1]["id"]), 201)
        preferred(amal, 12)

        assert refused(prefer(amal, 13)) == AT_THE_CAP

    def test_a_cap_lowered_mid_window_keeps_those_made_and_refuses_new_ones(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        for day in (3, 4, 5):
            preferred(amal, day)
        Unit.objects.filter(pk=er.pk).update(preference_cap=2)

        assert refused(prefer(amal, 6)) == (
            "You have 3 preferences for June 2026, the most this unit allows. Withdraw one to make another.")
        assert [state for _, _, state in states(amal)] == ["made"] * 3
        assert my_preferences(amal)["preference_cap"] == 2

    def test_the_cap_counts_across_the_months_windows(self, mona, er):
        first = opened(mona, er)
        amal = as_user("Amal")
        Unit.objects.filter(pk=er.pk).update(preference_cap=2)
        preferred(amal, 3)
        ok(post(mona, url(er, f"/request-windows/{first['id']}/close"), {}))

        second = opened(mona, er, closes="2026-05-28")
        preferred(amal, 4)

        assert refused(prefer(amal, 5)).startswith("You have 2 preferences for June 2026")
        assert [row["date"] for row in preferences_in(mona, er, second["id"])] == ["2026-06-04"]

    def test_a_cap_of_0_switches_them_off_while_requests_go_on(self, mona, er):
        Unit.objects.filter(pk=er.pk).update(preference_cap=0)
        opened(mona, er)
        amal = as_user("Amal")

        assert refused(prefer(amal, 3)) == "The ER unit takes no preferences, only requests."
        assert my_preferences(amal)["refusal"] == "The ER unit takes no preferences, only requests."
        asked(amal, 3)


class TestTheWindowOpensOnEitherCap:
    def test_a_unit_taking_preferences_only_opens_a_window_and_refuses_requests(self, mona, er):
        Unit.objects.filter(pk=er.pk).update(request_cap=0)
        assert windows(mona, er)["open_refusal"] is None

        opened(mona, er)
        amal = as_user("Amal")

        assert refused(ask(amal, 3)) == "The ER unit takes no requests, only preferences."
        assert mine(amal)["refusal"] == "The ER unit takes no requests, only preferences."
        preferred(amal, 3)

    def test_a_unit_with_both_caps_at_0_cannot_open_one_and_is_told_of_both(self, mona, er):
        Unit.objects.filter(pk=er.pk).update(request_cap=0, preference_cap=0)

        assert refused(open_window(mona, er)) == NO_CAP
        assert windows(mona, er)["open_refusal"] == NO_CAP
        assert "request cap" in NO_CAP and "preference cap" in NO_CAP

    def test_the_managers_windows_say_both_caps_and_how_many_preferences_each_took(self, mona, er):
        opened(mona, er)
        preferred(as_user("Amal"), 3)
        preferred(as_user("Badr"), 3)

        seen = windows(mona, er)
        assert (seen["request_cap"], seen["preference_cap"]) == (3, 10)
        assert (seen["windows"][0]["requests"], seen["windows"][0]["preferences"]) == (0, 2)


class TestOneWishADate:
    def test_a_preference_where_a_request_holds_the_date_is_refused_naming_the_request(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        made = asked(amal, 3)

        assert refused(prefer(amal, 3, "working_day"), 422) == (
            "You have a request on 3 June 2026, and a date holds one wish. Withdraw the request to make a preference instead.")
        decided(mona, er, made["id"], "accept")
        assert refused(prefer(amal, 3), 422).startswith("You have a request on 3 June 2026")

    @pytest.mark.parametrize("ending", ["withdraw", "reject"])
    def test_a_request_ended_frees_the_date(self, mona, er, ending):
        opened(mona, er)
        amal = as_user("Amal")
        made = asked(amal, 3)
        if ending == "withdraw":
            ok(post(amal, f"/api/requests/mine/{made['id']}/withdraw", {}))
        else:
            decided(mona, er, made["id"], "reject", reason="Short of staff.")

        preferred(amal, 3)

    def test_a_cancelled_request_frees_the_date(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        made = asked(amal, 3)
        decided(mona, er, made["id"], "accept")
        decided(mona, er, made["id"], "cancel", reason="Needed after all.")

        preferred(amal, 3)

    def test_a_request_where_a_preference_holds_the_date_is_refused_naming_the_preference(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        made = preferred(amal, 3)

        assert refused(ask(amal, 3, "N"), 422) == (
            "You have a preference on 3 June 2026, and a date holds one wish. Withdraw it, or ask for it as a request instead.")
        ok(withdraw(amal, made["id"]))
        asked(amal, 3, "N")

    def test_one_preference_a_date(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        made = preferred(amal, 3)

        assert refused(prefer(amal, 3, "working_day"), 422) == (
            "You already have a preference on 3 June 2026. Withdraw it to prefer something else.")
        ok(withdraw(amal, made["id"]))
        preferred(amal, 3, "working_day")
        assert states(amal) == [("2026-06-03", "day_off", "withdrawn"), ("2026-06-03", "working_day", "made")]


class TestWithdrawing:
    def test_a_made_one_while_the_window_is_open_and_nothing_is_deleted(self, mona, er):
        window = opened(mona, er)
        amal = as_user("Amal")
        made = preferred(amal, 3)

        withdrawn = ok(withdraw(amal, made["id"]))

        assert withdrawn["state"] == "withdrawn"
        assert refused(withdraw(amal, made["id"])) == "This preference is withdrawn, so it cannot be withdrawn again."
        stored = Preference.objects.get()
        assert (stored.ended_by.username, stored.ended_at is not None) == ("Amal", True)
        assert [row["state"] for row in preferences_in(mona, er, window["id"])] == ["withdrawn"]

    def test_a_colleagues_is_the_same_404_as_none(self, mona, er):
        opened(mona, er)
        made = preferred(as_user("Amal"), 3)
        badr = as_user("Badr")

        assert withdraw(badr, made["id"]).status_code == 404
        assert ask_instead(badr, made["id"]).status_code == 404
        assert withdraw(badr, "01900000-0000-7000-8000-000000000000").status_code == 404


class TestAskingForItAsARequest:
    def test_a_day_off_becomes_a_pending_request_and_the_preference_ends_as_asked_instead(self, mona, er):
        window = opened(mona, er)
        amal = as_user("Amal")
        made = preferred(amal, 3)

        answer = ok(ask_instead(amal, made["id"]), 201)

        assert (answer["preference"]["state"], answer["request"]["state"], answer["request"]["shift"]) == (
            "asked_instead", "pending", "")
        assert [(row["date"], row["state"]) for row in mine(amal)["requests"]] == [("2026-06-03", "pending")]
        assert states(amal) == [("2026-06-03", "day_off", "asked_instead")]
        assert [row["state"] for row in requests_in(mona, er, window["id"])] == ["pending"]
        assert my_preferences(amal)["counted"] == 0

    def test_a_working_day_is_asked_for_as_the_shift_she_chooses(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        made = preferred(amal, 3, "working_day")

        assert refused(ask_instead(amal, made["id"]), 422) == "Choose the shift you ask for."
        answer = ok(ask_instead(amal, made["id"], "N"), 201)

        assert answer["request"]["shift"] == "N"

    def test_a_day_off_takes_no_shift(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        made = preferred(amal, 3)

        assert refused(ask_instead(amal, made["id"], "N"), 422) == (
            "This preference is for a day off, so it is asked for as a day off.")

    def test_refused_as_a_whole_at_the_request_cap(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        for day in (1, 2, 3):
            asked(amal, day)
        made = preferred(amal, 4)

        assert refused(ask_instead(amal, made["id"])) == (
            "You have 3 requests for June 2026, the most this unit allows. Withdraw a pending one to make another.")
        assert states(amal) == [("2026-06-04", "day_off", "made")]
        assert len(mine(amal)["requests"]) == 3
        assert not AuditEntry.objects.filter(action="preference.asked_instead").exists()

    def test_refused_as_a_whole_when_the_shift_is_not_hers(self, mona, er):
        opened(mona, er)
        emad = as_user("Emad")  # Day only, in the sample.
        made = preferred(emad, 3, "working_day")

        assert refused(ask_instead(emad, made["id"], "N"), 422) == "N is not one of the shifts you work."
        assert states(emad) == [("2026-06-03", "working_day", "made")]

    def test_only_a_made_one(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        made = preferred(amal, 3)
        ok(withdraw(amal, made["id"]))

        assert refused(ask_instead(amal, made["id"])) == "This preference is withdrawn, so it cannot be asked for as a request."


class TestReachingASolve:
    def test_a_solve_takes_her_live_ones_and_the_draft_warns_when_they_change_since(self, mona, er, solver):
        opened(mona, er)
        amal = as_user("Amal")
        kept = preferred(amal, 3)
        preferred(amal, 4, "working_day")
        ok(withdraw(amal, preferred(amal, 5)["id"]))
        solved(mona, er, solver)
        sent = solver.started[-1]["nurses"]

        amal_row = next(row for row in sent if row["id"] == "Amal")
        assert (amal_row["undesired_days"], amal_row["desired_days"]) == (["2026-06-03"], ["2026-06-04"])
        assert month_of(mona, er)["draft"]["changes"] == []

        ok(withdraw(amal, kept["id"]))

        assert [(change["hospital_id"], change["what"]) for change in month_of(mona, er)["draft"]["changes"]] == [
            ("Amal", "preferences")]


class TestATransfer:
    def test_a_nurse_transferred_out_keeps_hers_on_the_old_units_record_counting_against_its_cap_only(self, mona, er,
                                                                                                       client_of):
        window = opened(mona, er)
        amal = as_user("Amal")
        Unit.objects.filter(pk=er.pk).update(preference_cap=2)
        preferred(amal, 3)
        preferred(amal, 4)
        omar = client_of("omar", Role.UNIT_MANAGER)
        icu = make_unit("ICU", User.objects.get(username="omar"))
        Unit.objects.filter(pk=icu.pk).update(rules=er.rules, preference_cap=2)
        Placement.objects.create(nurse=profile("Amal"), unit=icu, from_month=dt.date(2026, 6, 1))

        opened(omar, icu)

        seen = my_preferences(amal)
        assert (seen["window"]["unit"], seen["counted"]) == ("ICU", 0)
        preferred(amal, 5)
        assert [row["date"] for row in preferences_in(mona, er, window["id"])] == ["2026-06-03", "2026-06-04"]
        assert refused(prefer(amal, 3), 422) == (
            "You already have a preference on 3 June 2026. Withdraw it to prefer something else.")
        left = Preference.objects.filter(date=dt.date(2026, 6, 4)).get()
        assert refused(ask_instead(amal, left.pk)) == (
            "You are no longer on this unit's roster for June 2026, so this preference cannot become a request. Make the request "
            "in your new unit's window instead.")
        assert states(amal)[1] == ("2026-06-04", "day_off", "made")


class TestWhatStaysPrivate:
    def scene(self, mona, er):
        opened(mona, er)
        amal = as_user("Amal")
        withdrawn = preferred(amal, 3)
        ok(withdraw(amal, withdrawn["id"]))
        ok(ask_instead(amal, preferred(amal, 4)["id"]), 201)
        preferred(amal, 5, "working_day")
        return amal

    def test_the_audit_log_says_a_preference_moved_and_whose_and_nothing_else(self, mona, er):
        self.scene(mona, er)

        entries = [(entry.action, entry.actor.username, entry.target, entry.before, entry.after)
                   for entry in AuditEntry.objects.filter(action__startswith="preference.").order_by("at", "id")]
        assert entries == [
            ("preference.made", "Amal", "Amal", None, None),
            ("preference.withdrawn", "Amal", "Amal", None, None),
            ("preference.made", "Amal", "Amal", None, None),
            ("preference.asked_instead", "Amal", "Amal", None, None),
            ("preference.made", "Amal", "Amal", None, None),
        ]
        logged = " ".join(str(row) for row in AuditEntry.objects.values_list("action", "target", "before", "after"))
        for secret in ("2026-06-03", "2026-06-04", "2026-06-05", "day_off", "working_day"):
            assert secret not in logged

    def test_no_notification_is_sent(self, mona, er):
        before = Notification.objects.exclude(event="window_opened").count()

        self.scene(mona, er)

        # The request asked for instead is a request like any other, and a request made tells nobody either.
        assert Notification.objects.exclude(event="window_opened").count() == before == 0

    def test_a_colleague_sees_nothing_of_it_in_her_month_her_sheet_her_preferences_or_the_roster_file(self, mona, carla, er,
                                                                                                    solver):
        approved(mona, carla, er, solver)
        badr = as_user("Badr")
        paths = ("/api/rosters/mine/month", "/api/rosters/mine/sheet")
        before = {path: ok(badr.get(path, {"month": MONTH})) for path in paths}
        exported = mona.get(url(er, "/roster/export"), {"month": MONTH, "version": 1, "format": "csv"}).content

        self.scene(mona, er)

        assert {path: ok(badr.get(path, {"month": MONTH})) for path in paths} == before
        assert my_preferences(badr)["preferences"] == []
        assert mine(badr)["requests"] == []
        assert mona.get(url(er, "/roster/export"), {"month": MONTH, "version": 1, "format": "csv"}).content == exported

    def test_another_units_manager_reaches_none_of_them(self, mona, er, client_of):
        window = opened(mona, er)
        preferred(as_user("Amal"), 3)
        omar = client_of("omar", Role.UNIT_MANAGER)
        icu = make_unit("ICU", User.objects.get(username="omar"))

        assert omar.get(url(er, f"/request-windows/{window['id']}/preferences")).status_code == 403
        assert omar.get(url(icu, f"/request-windows/{window['id']}/preferences")).status_code == 404
