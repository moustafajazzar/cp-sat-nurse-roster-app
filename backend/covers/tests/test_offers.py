"""A cover offered, accepted and declined through the API, and who is told of each step, on the scene of `conftest.py`.

Two scenes recur. Badr's sick leave on the 9th opens a cover for Day there, which Emad alone could take. With room for two on
Day (`wider_day`), a cover opened by hand for Day on the 25th, which Amal works, could be taken by Emad or Dana, Emad first."""
import datetime as dt

import pytest

from accounts.models import Role
from audit.models import AuditEntry
from covers.models import Cover, CoverState
from covers.tasks import expire_covers
from covers.tests.conftest import (MONTH, answer, answered, badrs_day, cover_page, my_covers, offer, offered, ok, opened,
                                   record_leave, refusals, the_25th, wider_day, withdraw)
from notifications.models import Event, Notification
from nurses.models import NurseProfile, Placement
from notifications.tests.test_events import as_user, told
from rosters.models import PinnedCell, PinOrigin, Roster, VersionOrigin
from rosters.tests.test_pins import icu, off, pin
from rosters.tests.test_versions import change, changed

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _scene(before_june, solver):
    pass


def entries() -> list[tuple[str, str, str]]:
    return [(entry.action, entry.actor.username if entry.actor else "", entry.target)
            for entry in AuditEntry.objects.filter(action__in=("cover.offered", "cover.accepted", "cover.declined",
                                                               "cover.withdrawn", "roster.changed")).order_by("id")]


def cover_told(username) -> list[dict]:
    return told(as_user(username), "cover_step")


def words(username) -> list[str]:
    return [item["text"] for item in cover_told(username)]


def answers(page) -> list[tuple[str, str]]:
    return [(row["hospital_id"], row["answer"]) for row in page["offers"]]


def latest_version():
    return Roster.objects.get().versions.first()


class TestOffering:
    def test_an_offer_makes_it_offered_with_its_note_and_tells_those_ticked_and_nobody_else(self, in_force, mona):
        cover_id = the_25th(mona, in_force)

        page = offered(mona, in_force, cover_id, "Emad", "Dana", note="We are short; thank you.")

        assert (page["cover"]["state"], page["cover"]["note"]) == ("offered", "We are short; thank you.")
        assert answers(page) == [("Dana", "pending"), ("Emad", "pending")]
        [item] = cover_told("Emad")
        assert item["text"] == "Mona Ali offered you a cover: Day (D) on 25 June 2026. Accept or decline it on Covers."
        assert item["link"] == {"page": "covers", "month": MONTH, "unit_id": None, "cover_id": None}
        assert len(cover_told("Dana")) == 1
        assert cover_told("Amal") == [] and cover_told("Badr") == [] and cover_told("mona") == []
        assert entries() == [("cover.offered", "mona", "ER")]

    def test_the_nurse_reads_the_date_the_shift_its_hours_and_the_note_and_nothing_of_why_or_who_else(self, in_force, mona):
        cover_id = badrs_day(mona, in_force)
        offered(mona, in_force, cover_id, "Emad", note="Thank you.")

        [row] = my_covers(as_user("Emad"))

        assert set(row) == {"id", "month", "unit", "date", "shift", "hours", "note", "state", "my_answer", "answered_at"}
        assert (row["id"], row["date"], row["shift"], row["hours"], row["note"]) == (cover_id, "2026-06-09", "D", 12, "Thank you.")
        assert (row["unit"], row["state"], row["my_answer"], row["answered_at"]) == ("ER", "offered", "pending", None)
        assert "Badr" not in str(row) and "leave" not in str(row).lower()

    def test_an_add_offers_only_those_not_yet_in_it_tells_only_them_and_keeps_the_note(self, in_force, mona):
        cover_id = the_25th(mona, in_force)
        offered(mona, in_force, cover_id, "Emad", note="First words.")

        page = offered(mona, in_force, cover_id, "Emad", "Dana", note="Ignored.")

        assert answers(page) == [("Dana", "pending"), ("Emad", "pending")]
        assert page["cover"]["note"] == "First words."
        assert (len(cover_told("Emad")), len(cover_told("Dana"))) == (1, 1)
        assert entries() == [("cover.offered", "mona", "ER"), ("cover.offered", "mona", "ER")]

    def test_a_nurse_not_in_the_list_as_judged_now_is_refused_by_name_and_nothing_is_offered(self, in_force, mona):
        cover_id = badrs_day(mona, in_force)

        said = refusals(offer(mona, in_force, cover_id, "Emad", "Amal"))

        assert said == ["Amal cannot take this cover now, so it cannot be offered to them."]
        assert cover_page(mona, in_force, cover_id)["cover"]["state"] == "open"
        assert cover_told("Emad") == [] and entries() == []

    def test_a_nurse_of_another_unit_is_said_back_by_her_id_the_same_as_no_such_nurse(self, in_force, mona, make_user):
        """Only someone on the cover's own roster is named: the manager reads nothing of another unit's nurses, not even
        whether one holds an ID."""
        cover_id = badrs_day(mona, in_force)
        zeta = make_user("icu-0001", Role.STAFF_NURSE, full_name="Zeta Otherunit")
        theirs = NurseProfile.objects.create(user=zeta, gender="F", start_date=dt.date(2026, 1, 1))
        Placement.objects.create(nurse=theirs, unit=icu(make_user("omar", Role.UNIT_MANAGER)), from_month=dt.date(2026, 1, 1))

        response = offer(mona, in_force, cover_id, "icu-0001", "nobody-9999")

        assert refusals(response) == ["icu-0001 cannot take this cover now, so it cannot be offered to them.",
                                      "nobody-9999 cannot take this cover now, so it cannot be offered to them."]
        assert "Zeta" not in response.content.decode() and "Otherunit" not in response.content.decode()
        assert cover_page(mona, in_force, cover_id)["cover"]["state"] == "open" and entries() == []

    def test_an_offer_names_at_least_one_nurse_and_an_add_one_not_yet_in_it(self, in_force, mona):
        cover_id = badrs_day(mona, in_force)
        assert refusals(offer(mona, in_force, cover_id)) == ["Tick at least one nurse to offer it to."]
        offered(mona, in_force, cover_id, "Emad")

        assert refusals(offer(mona, in_force, cover_id, "Emad")) == ["Everyone ticked has been offered it already."]

    def test_an_ended_cover_cannot_be_offered(self, in_force, mona):
        cover_id = badrs_day(mona, in_force)
        ok(withdraw(mona, in_force, cover_id))

        assert refusals(offer(mona, in_force, cover_id, "Emad"), 409) == [
            "This cover has been withdrawn, so it cannot be offered."]

    def test_a_cover_not_offered_to_her_is_the_same_as_none(self, in_force, mona):
        cover_id = badrs_day(mona, in_force)
        offered(mona, in_force, cover_id, "Emad")
        amal = as_user("Amal")

        assert my_covers(amal) == []
        for what in ("accept", "decline"):
            theirs, none = answer(amal, cover_id, what), answer(amal, "0190a6c5-0000-7000-8000-000000000000", what)
            assert (theirs.status_code, theirs.content) == (none.status_code, none.content) == (404, none.content)


class TestAccepting:
    def test_the_first_acceptance_takes_it_as_a_pin_in_a_new_version_and_everyone_is_told(self, in_force, mona):
        cover_id = the_25th(mona, in_force)
        offered(mona, in_force, cover_id, "Emad", "Dana")

        row = answered(as_user("Emad"), cover_id, "accept")

        assert (row["state"], row["my_answer"]) == ("taken", "accepted") and row["answered_at"] is not None
        version = latest_version()
        assert (version.number, version.origin, version.created_by.username) == (2, VersionOrigin.COVER, "Emad")
        assert version.reason == "Cover taken by Emad: Day (D) on 25 June 2026."
        assert Roster.objects.get().grid["Emad"]["2026-06-25"] == "D"
        held = PinnedCell.objects.current().get(nurse__user__username="Emad", date="2026-06-25")
        assert (held.origin, held.shift.code) == (PinOrigin.COVER, "D")

        page = cover_page(mona, in_force, cover_id)
        assert (page["cover"]["state"], page["cover"]["taker"], page["cover"]["version"]) == ("taken", "Emad", 2)
        assert page["cover"]["ended_at"] is not None and page["cover"]["ended_by"] is None
        assert answers(page) == [("Dana", "pending"), ("Emad", "accepted")]
        assert entries()[1:] == [("cover.accepted", "Emad", "Emad"), ("roster.changed", "Emad", "ER, 2026-06")]
        assert AuditEntry.objects.get(action="roster.changed").after == {"version": 2, "pins": 1, "cells": 1}

        assert words("mona")[0] == "Emad accepted the cover for Day (D) on 25 June 2026. It is in the roster in force."
        assert cover_told("mona")[0]["link"] == {"page": "unit_covers", "month": MONTH, "unit_id": None, "cover_id": cover_id}
        assert words("Dana")[0] == "The cover for Day (D) on 25 June 2026 offered to you has been taken."
        assert len(words("Emad")) == 1, "only the offer: she knows she took it"
        changed_for = {note.user.username for note in Notification.objects.filter(event=Event.ROSTER_CHANGED)}
        assert changed_for == {"Amal", "Badr", "Dana"}

    def test_a_second_acceptance_is_refused_as_taken(self, in_force, mona):
        cover_id = the_25th(mona, in_force)
        offered(mona, in_force, cover_id, "Emad", "Dana")
        answered(as_user("Emad"), cover_id, "accept")

        assert refusals(answer(as_user("Dana"), cover_id, "accept"), 409) == [
            "Another nurse has taken this cover, so you can no longer take it."]
        assert refusals(answer(as_user("Emad"), cover_id, "accept"), 409) == ["You have taken this cover already."]
        assert latest_version().number == 2

    def test_leave_recorded_since_refuses_her_in_her_own_words_and_it_stays_offered(self, in_force, mona):
        cover_id = badrs_day(mona, in_force)
        offered(mona, in_force, cover_id, "Emad")
        ok(record_leave(mona, in_force, "Emad", 9), 201)

        assert refusals(answer(as_user("Emad"), cover_id, "accept"), 409) == [
            "You are on leave on 9 June 2026, so you cannot take this cover."]
        page = cover_page(mona, in_force, cover_id)
        assert (page["cover"]["state"], answers(page)) == ("offered", [("Emad", "pending")])
        assert latest_version().number == 2, "the leave's version only"

    def test_a_required_skill_expired_since_refuses_her_in_her_own_words_as_leave_does(self, in_force, mona):
        """A barred day is hers as leave is, and her skills are hers to read."""
        from rosters.tests.test_review import holding
        cover_id = badrs_day(mona, in_force)
        offered(mona, in_force, cover_id, "Emad")
        holding("Emad", "BLS", dt.date(2026, 6, 8), required=True)

        assert refusals(answer(as_user("Emad"), cover_id, "accept"), 409) == [
            "Your BLS, required to work, counted until 8 June 2026, so you cannot take this cover."]
        assert cover_page(mona, in_force, cover_id)["cover"]["state"] == "offered"

    def test_a_rule_it_would_now_break_for_her_is_said_in_full(self, in_force, mona):
        """Emad works Day on the 7th, 8th, 10th and 12th. Once the manager puts him on the 11th too, the 9th would make a run of
        six, beyond the four allowed."""
        cover_id = badrs_day(mona, in_force)
        offered(mona, in_force, cover_id, "Emad")
        changed(mona, in_force, pin("Emad", 11, "D"))

        said = refusals(answer(as_user("Emad"), cover_id, "accept"), 409)

        assert said[0].startswith("This breaks a rule for you: ") and "Emad" in said[0]
        assert Cover.objects.get().state == CoverState.OFFERED

    def test_a_cover_whose_date_has_passed_is_refused_and_stored_expired(self, in_force, mona, on_day):
        cover_id = badrs_day(mona, in_force)
        offered(mona, in_force, cover_id, "Emad")
        on_day(2026, 6, 10)

        assert refusals(answer(as_user("Emad"), cover_id, "accept"), 409) == [
            "This cover's date has passed, so you can no longer take it."]
        assert Cover.objects.get().state == CoverState.EXPIRED

    def test_a_withdrawn_cover_is_refused(self, in_force, mona):
        cover_id = badrs_day(mona, in_force)
        offered(mona, in_force, cover_id, "Emad")
        ok(withdraw(mona, in_force, cover_id))

        assert refusals(answer(as_user("Emad"), cover_id, "accept"), 409) == [
            "Your manager has withdrawn this cover, so you can no longer take it."]

    def test_of_two_covers_on_one_shift_day_one_taken_leaves_the_other_offered_and_she_takes_at_most_one(self, in_force, mona):
        first = the_25th(mona, in_force)
        second = opened(mona, in_force, 25, "D")["cover"]["id"]
        offered(mona, in_force, first, "Emad")
        offered(mona, in_force, second, "Emad", "Dana")

        answered(as_user("Emad"), first, "accept")

        assert cover_page(mona, in_force, second)["cover"]["state"] == "offered", "its own version fills no other cover"
        assert refusals(answer(as_user("Emad"), second, "accept"), 409) == [
            "You already work Day (D) on 25 June 2026, so you cannot take this cover."]


    def test_her_managers_pin_to_a_day_off_is_replaced_by_the_covers(self, in_force, mona):
        """With room for two on Day, the manager pins Badr off his Day on the 9th; a cover there offered to him, he takes it."""
        wider_day(in_force)
        changed(mona, in_force, off("Badr", 9))
        cover_id = opened(mona, in_force, 9, "D")["cover"]["id"]
        offered(mona, in_force, cover_id, "Badr")

        answered(as_user("Badr"), cover_id, "accept")

        pins = PinnedCell.objects.filter(nurse__user__username="Badr", date="2026-06-09").order_by("pinned_at")
        assert [(row.origin, row.removed_why) for row in pins] == [(PinOrigin.MANAGER, "replaced"), (PinOrigin.COVER, "")]

    def test_every_page_reading_its_pin_and_version_reads_them_and_leave_on_her_day_ends_the_pin(self, in_force, mona):
        """The roster as the manager reads it, the version list, and leave recorded on the taker's day since: leave wins over a
        cover's pin as over any."""
        cover_id = the_25th(mona, in_force)
        offered(mona, in_force, cover_id, "Emad")
        answered(as_user("Emad"), cover_id, "accept")

        roster = ok(mona.get(f"/api/units/{in_force.pk}/roster", {"month": MONTH}))
        assert [pin["origin"] for pin in roster["draft"]["pins"] if pin["hospital_id"] == "Emad"] == ["cover"]
        assert ok(mona.get(f"/api/units/{in_force.pk}/roster/versions", {"month": MONTH}))[0]["origin"] == "cover"
        saved = ok(record_leave(mona, in_force, "Emad", 25), 201)
        assert [(pin["date"], pin["origin"]) for pin in saved["pins_removed"]] == [("2026-06-25", "cover")]

    def test_the_managers_editor_refuses_a_cell_a_covers_pin_holds(self, in_force, mona):
        cover_id = the_25th(mona, in_force)
        offered(mona, in_force, cover_id, "Emad")
        answered(as_user("Emad"), cover_id, "accept")

        assert refusals(change(mona, in_force, off("Emad", 25))) == [
            "This pin comes from an accepted cover, on 25 June 2026, so change it there."]
        assert latest_version().number == 2


class TestOvertimeAboveAShare:
    def test_a_cover_giving_a_nurse_overtime_above_her_share_is_offered_and_taken_and_july_carries_her_balance(self, in_force,
                                                                                                               mona):
        """Amal, off on the 9th, takes a second Night there (`one_more_shift_past_her_share`)."""
        from closes.tests.test_close import JULY, balances, one_more_shift_past_her_share
        july = one_more_shift_past_her_share("Amal")
        cover_id = opened(mona, in_force, 9, "N")["cover"]["id"]

        offered(mona, in_force, cover_id, "Amal")
        answered(as_user("Amal"), cover_id, "accept")

        assert Roster.objects.get(unit=in_force).grid["Amal"]["2026-06-09"] == "N"
        assert balances(in_force, JULY) == july


class TestDeclining:
    def test_a_decline_is_her_answer_an_entry_and_the_manager_told_and_it_stays_offered(self, in_force, mona):
        cover_id = the_25th(mona, in_force)
        offered(mona, in_force, cover_id, "Emad")

        row = answered(as_user("Emad"), cover_id, "decline")

        assert (row["state"], row["my_answer"]) == ("offered", "declined")
        assert entries()[1:] == [("cover.declined", "Emad", "Emad")]
        assert words("mona") == ["Emad declined the cover for Day (D) on 25 June 2026."]
        page = cover_page(mona, in_force, cover_id)
        assert (page["cover"]["state"], answers(page)) == ("offered", [("Emad", "declined")]), "nobody left, and still offered"
        assert refusals(answer(as_user("Emad"), cover_id, "decline"), 409) == ["You have declined this cover already."]
        assert refusals(answer(as_user("Emad"), cover_id, "accept"), 409) == ["You have declined this cover already."]

    def test_an_add_does_not_ask_her_again(self, in_force, mona):
        cover_id = the_25th(mona, in_force)
        offered(mona, in_force, cover_id, "Emad")
        answered(as_user("Emad"), cover_id, "decline")

        page = offered(mona, in_force, cover_id, "Emad", "Dana")

        assert answers(page) == [("Dana", "pending"), ("Emad", "declined")]
        assert len(cover_told("Emad")) == 1

    def test_a_taken_cover_cannot_be_declined(self, in_force, mona):
        cover_id = the_25th(mona, in_force)
        offered(mona, in_force, cover_id, "Emad", "Dana")
        answered(as_user("Emad"), cover_id, "accept")

        assert refusals(answer(as_user("Dana"), cover_id, "decline"), 409) == [
            "Another nurse has taken this cover, so there is nothing to decline."]


class TestHowItEndsIsTold:
    """The nurses offered are told it ended, never why it was short, who else was asked or why it ended."""

    def test_a_withdrawal(self, in_force, mona):
        cover_id = the_25th(mona, in_force)
        offered(mona, in_force, cover_id, "Emad", "Dana")

        ok(withdraw(mona, in_force, cover_id))

        assert words("Emad")[0] == "Mona Ali withdrew the cover for Day (D) on 25 June 2026 offered to you."
        assert len(words("Dana")) == 2 and words("mona") == []

    def test_a_fill(self, in_force, mona):
        cover_id = the_25th(mona, in_force)
        offered(mona, in_force, cover_id, "Emad")

        changed(mona, in_force, pin("Dana", 25, "D"))

        assert cover_page(mona, in_force, cover_id)["cover"]["state"] == "filled"
        assert words("Emad")[0] == "The cover for Day (D) on 25 June 2026 offered to you is no longer needed."
        assert cover_told("Emad")[0]["link"] == {"page": "covers", "month": MONTH, "unit_id": None, "cover_id": None}

    def test_an_expiry_by_the_hourly_job(self, in_force, mona, on_day):
        cover_id = badrs_day(mona, in_force)
        offered(mona, in_force, cover_id, "Emad")
        on_day(2026, 6, 10)

        expire_covers(0)
        expire_covers(0)

        assert words("Emad") == ["The cover for Day (D) on 9 June 2026 offered to you has expired.",
                                 "Mona Ali offered you a cover: Day (D) on 9 June 2026. Accept or decline it on Covers."]

    def test_payloads_hold_identifiers_and_the_step_only(self, in_force, mona):
        cover_id = the_25th(mona, in_force)
        offered(mona, in_force, cover_id, "Emad", "Dana", note="A note.")
        answered(as_user("Dana"), cover_id, "decline")
        answered(as_user("Emad"), cover_id, "accept")

        notes = Notification.objects.filter(event=Event.COVER_STEP)
        assert {note.payload["step"] for note in notes} == {"offered", "declined", "accepted", "taken"}
        for note in notes:
            assert set(note.payload) <= {"unit_id", "month", "cover_id", "step", "by"}
            assert note.payload["cover_id"] == cover_id
