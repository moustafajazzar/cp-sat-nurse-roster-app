"""The rules of a change to the roster in force, proven through `rosters.in_force` alone: each test starts from a roster approved
as version 1, holds its month, changes cells, and looks only at what anyone could see afterwards (the versions, the grid in force,
the audit log, the notices, swaps and covers). The roster is the real one of the four-nurse sample
(`fixtures/sound_rosters.json`). In its June Amal works Night on the 1st to the 4th and is off on the 5th and the 9th; Badr works Day on the 9th and Emad is off;
Dana works the only Night on the 9th."""
import datetime as dt

import pytest
from django.db import transaction
from django.test import RequestFactory
from procrastinate.contrib.django.models import ProcrastinateJob

from accounts.models import User
from audit.log import record
from audit.models import AuditEntry
from covers.models import Cover, CoverState
from covers.tests.conftest import opened
from notifications.models import Event, Notification
from notifications.tests.test_events import as_user
from nurses import membership
from nurses.models import NurseProfile
from rosters import in_force
from rosters.approved import Change
from rosters.drafts import in_current_codes
from rosters.models import Roster, RosterState, RosterVersion
from rosters.tests.conftest import approved_roster
from swaps.models import Swap, SwapState
from swaps.tests.conftest import SOUND_SWAP, proposed

pytestmark = pytest.mark.django_db

JUNE = dt.date(2026, 6, 1)


@pytest.fixture(autouse=True)
def _scene(before_june):
    pass


@pytest.fixture
def june(er) -> Roster:
    return approved_roster(er)


def day(number: int) -> dt.date:
    return JUNE.replace(day=number)


def manager() -> User:
    return User.objects.get(username="mona")


def changing(unit, *cells: Change, origin=in_force.Origin.EDIT, by=None, why="Amal covers Night on the 9th.", pins=None,
             **extra):
    """Holds the month for the cells' nurses and changes them, as an origin does, all in one transaction."""
    with transaction.atomic():
        held = in_force.hold(unit.pk, JUNE, nurses={cell.hospital_id for cell in cells})
        return in_force.change(held, list(cells), origin=origin, by=by or manager(), why=why,
                               pins=len(cells) if pins is None else pins, **extra)


def versions(roster) -> list[RosterVersion]:
    return list(roster.versions.order_by("number"))


def in_force_grid(unit) -> dict:
    return Roster.objects.get(unit=unit, month=JUNE).grid


class TestVersions:
    def test_a_change_is_the_next_version_with_its_origin_who_and_why(self, er, june):
        made = changing(er, Change("Amal", day(9), "N"))

        assert made.number == 2
        stored = versions(june)[-1]
        assert (stored.number, stored.origin, stored.created_by.username, stored.reason, stored.solve) == (
            2, "edit", "mona", "Amal covers Night on the 9th.", None)
        assert stored.grid["Amal"]["2026-06-09"] == "N"

    def test_each_change_is_the_next_number_and_version_1_stays_as_approved(self, er, june):
        approved_grid = versions(june)[0].grid

        changing(er, Change("Amal", day(9), "N"))
        changing(er, Change("Badr", day(2), None), why="Badr asked for the 2nd off.")

        assert [version.number for version in versions(june)] == [1, 2, 3]
        assert versions(june)[0].grid == approved_grid
        latest = versions(june)[-1].grid
        assert latest["Amal"]["2026-06-09"] == "N" and latest["Badr"]["2026-06-02"] == "", "the second built on the first"

    def test_the_grid_in_force_is_the_latest_versions(self, er, june):
        changing(er, Change("Amal", day(9), "N"), Change("Badr", day(2), None))

        assert in_force_grid(er) == in_current_codes(versions(june)[-1].grid)
        assert in_force_grid(er)["Amal"]["2026-06-09"] == "N"

    def test_the_solve_a_grid_came_from_is_kept_on_its_version(self, er, june):
        changing(er, Change("Amal", day(9), "N"), origin=in_force.Origin.REPLAN, solve=june.solve)

        assert versions(june)[-1].solve == june.solve

    def test_a_cell_of_a_nurse_the_hold_did_not_lock_is_the_callers_mistake(self, er, june):
        with transaction.atomic():
            held = in_force.hold(er.pk, JUNE, nurses={"Badr"})
            with pytest.raises(ValueError, match="Amal was not held"):
                in_force.change(held, [Change("Amal", day(9), "N")], origin=in_force.Origin.EDIT, by=manager(), why="", pins=0)


def roster_changed_entries() -> list[AuditEntry]:
    return list(AuditEntry.objects.filter(action="roster.changed"))


def told_of_a_change() -> set[str]:
    return set(Notification.objects.filter(event=Event.ROSTER_CHANGED).values_list("user__username", flat=True))


def projections(unit) -> int:
    return ProcrastinateJob.objects.filter(queueing_lock=f"project_carry_over:{unit.pk}:2026-06").count()


class TestNoCellChanged:
    def test_a_change_to_what_each_cell_holds_makes_nothing_and_says_so(self, er, june):
        """Amal already works Night on the 1st and is off on the 5th."""
        made = changing(er, Change("Amal", day(1), "N"), Change("Amal", day(5), None))

        assert made is in_force.UNCHANGED
        assert [version.number for version in versions(june)] == [1]
        assert roster_changed_entries() == [] and told_of_a_change() == set() and projections(er) == 0


class TestAuditEntry:
    @pytest.mark.parametrize("origin", ["edit", "swap", "cover", "request", "replan"])
    def test_each_origin_but_leave_writes_one_saying_a_version_was_made_with_its_pins_and_cells(self, er, june, origin):
        changing(er, Change("Amal", day(9), "N"), Change("Badr", day(2), None), origin=in_force.Origin(origin), pins=1)

        [entry] = roster_changed_entries()
        assert (entry.actor.username, entry.target) == ("mona", "ER, 2026-06")
        assert entry.after == {"version": 2, "pins": 1, "cells": 2}
        assert versions(june)[-1].origin == origin

    def test_it_never_says_why_or_whose_cells(self, er, june):
        changing(er, Change("Amal", day(9), "N"), why="Amal covers for Badr, who is sick.")

        [entry] = roster_changed_entries()
        assert "Amal" not in str([entry.target, entry.before, entry.after]) and "sick" not in str(entry.after)

    def test_leave_writes_none_and_its_version_is_stored_as_an_edit(self, er, june):
        changing(er, Change("Badr", day(9), None), origin=in_force.Origin.LEAVE, pins=0,
                 why="Leave recorded for Badr took their shift off 9 June.")

        assert roster_changed_entries() == []
        assert versions(june)[-1].origin == "edit"
        assert told_of_a_change() == {"Amal", "Badr", "Dana", "Emad"}

    def test_it_keeps_the_address_of_the_request_that_asked(self, er, june):
        asked = RequestFactory().post("/", REMOTE_ADDR="10.1.2.3")

        changing(er, Change("Amal", day(9), "N"), request=asked)

        assert roster_changed_entries()[0].ip == "10.1.2.3"

    def test_a_requests_comes_after_the_requests_own_entry_as_a_swaps_and_a_covers_do(self, er, june):
        """An origin records its own entry before it changes, so the log reads in the order things were decided."""
        with transaction.atomic():
            held = in_force.hold(er.pk, JUNE, nurses={"Amal"})
            record("request.accepted", actor=manager(), target="Amal")
            in_force.change(held, [Change("Amal", day(9), "N")], origin=in_force.Origin.REQUEST, by=manager(),
                            why="Amal's request for Night (N) on 9 June 2026 accepted.", pins=1)

        assert [(entry.action, entry.target, entry.after) for entry in AuditEntry.objects.order_by("id")][-2:] == [
            ("request.accepted", "Amal", None), ("roster.changed", "ER, 2026-06", {"version": 2, "pins": 1, "cells": 1})]
        assert versions(june)[-1].origin == "request"


class TestWhoIsTold:
    def test_every_nurse_of_the_month_is_told_with_no_reason_cell_or_name(self, er, june):
        changing(er, Change("Amal", day(9), "N"), why="Amal covers for Badr, who is sick.")

        assert told_of_a_change() == {"Amal", "Badr", "Dana", "Emad"}
        for note in Notification.objects.filter(event=Event.ROSTER_CHANGED):
            assert set(note.payload) == {"unit_id", "month", "by"}

    def test_whoever_made_the_change_is_not(self, er, june):
        changing(er, Change("Badr", day(9), "D"), Change("Emad", day(9), "D"), by=User.objects.get(username="Emad"),
                 origin=in_force.Origin.COVER)

        assert told_of_a_change() == {"Amal", "Badr", "Dana"}


class TestWhatFollows:
    def test_a_waiting_swap_one_of_whose_cells_it_changes_expires_and_one_elsewhere_does_not(self, er, june):
        """Badr proposes giving his Day on the 9th to Emad."""
        proposed(as_user("Badr"), *SOUND_SWAP)

        changing(er, Change("Badr", day(20), "D"))
        assert Swap.objects.get().state == SwapState.PENDING

        changing(er, Change("Badr", day(9), "N"))
        swap = Swap.objects.get()
        assert (swap.state, swap.expired_why) == (SwapState.EXPIRED, "cell_changed")

    def test_a_waiting_cover_of_a_shift_day_it_puts_one_more_nurse_on_is_filled(self, er, june, mona):
        """Dana works the only Night on the 9th, which may hold two."""
        cover = opened(mona, er, 9, "N")["cover"]["id"]

        changing(er, Change("Amal", day(9), "N"))

        assert Cover.objects.get(pk=cover).state == CoverState.FILLED

    def test_the_months_carry_over_is_projected_again(self, er, june):
        assert projections(er) == 0

        changing(er, Change("Amal", day(9), "N"))

        assert projections(er) == 1


class TestRefusals:
    def test_a_roster_not_approved_is_refused_and_the_hold_says_so_beforehand(self, er, june):
        Roster.objects.filter(pk=june.pk).update(state=RosterState.DRAFT)

        with transaction.atomic():
            held = in_force.hold(er.pk, JUNE, nurses={"Amal"})
            assert isinstance(held.refusal(), in_force.NotApproved) and held.roster.state == RosterState.DRAFT
            with pytest.raises(in_force.NotApproved, match="not approved"):
                in_force.change(held, [Change("Amal", day(9), "N")], origin=in_force.Origin.EDIT, by=manager(), why="",
                                pins=1)

        assert [version.number for version in versions(june)] == [1] and roster_changed_entries() == []

    def test_a_month_with_no_roster_is_held_all_the_same_and_refused_as_not_approved(self, er):
        with transaction.atomic():
            held = in_force.hold(er.pk, JUNE, nurses={"Amal"})
            assert held.roster is None and held.unit == er
            with pytest.raises(in_force.NotApproved):
                in_force.change(held, [Change("Amal", day(9), "N")], origin=in_force.Origin.EDIT, by=manager(), why="",
                                pins=1)

    def test_a_month_that_is_over_is_refused_in_one_sentence(self, er, june, monkeypatch):
        monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 7, 2))

        with pytest.raises(in_force.MonthOver) as refused:
            changing(er, Change("Amal", day(9), "N"))

        assert str(refused.value) == "This month is over, so its roster in force cannot change."
        assert [version.number for version in versions(june)] == [1]
        assert roster_changed_entries() == [] and told_of_a_change() == set() and projections(er) == 0

    def test_the_month_under_way_can_change(self, er, june, monkeypatch):
        monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 6, 20))

        assert changing(er, Change("Amal", day(25), "N")).number == 2


JULY = dt.date(2026, 7, 1)


def taking_off(unit, hospital_id: str, *days: dt.date) -> list:
    """As a leave write does it: under the nurse's lock, holds the months of these days with leave's own hold and takes her
    shifts on them off each one."""
    with transaction.atomic():
        list(NurseProfile.objects.select_for_update(no_key=True).filter(user__username=hospital_id))
        made = []
        for held in in_force.hold_for_leave(unit.pk, {day.replace(day=1) for day in days}, nurse=hospital_id):
            cells = [Change(hospital_id, day, None) for day in days if day.replace(day=1) == held.month]
            made.append(in_force.change(held, cells, origin=in_force.Origin.LEAVE, by=manager(), pins=0,
                                        why=f"Leave recorded for {hospital_id} took their shift off."))
        return made


class TestLeave:
    def test_leave_writes_no_roster_change_entry_and_its_version_is_an_edit_told_to_the_unit(self, er, june):
        [made] = taking_off(er, "Badr", day(9))

        assert made.number == 2 and versions(june)[-1].origin == "edit"
        assert in_force_grid(er)["Badr"]["2026-06-09"] == ""
        assert roster_changed_entries() == []
        assert told_of_a_change() == {"Amal", "Badr", "Dana", "Emad"}

    def test_leave_over_two_approved_months_makes_one_version_in_each(self, er, june):
        """Dana works Night on 30 June and on 1 July."""
        july = approved_roster(er, "2026-07")

        made = taking_off(er, "Dana", dt.date(2026, 6, 30), dt.date(2026, 7, 1))

        assert [(version.roster.month, version.number) for version in made] == [(JUNE, 2), (JULY, 2)]
        assert versions(june)[-1].grid["Dana"]["2026-06-30"] == "" and versions(july)[-1].grid["Dana"]["2026-07-01"] == ""
        assert [version.origin for version in made] == ["edit", "edit"] and roster_changed_entries() == []

    def test_leaves_hold_takes_the_months_in_order_and_holds_no_roster_that_is_not_approved(self, er, june):
        july = approved_roster(er, "2026-07")
        Roster.objects.filter(pk=july.pk).update(state=RosterState.DRAFT)

        with transaction.atomic():
            held = in_force.hold_for_leave(er.pk, [JULY, JUNE], nurse="Dana")

            assert [(each.month, each.roster) for each in held] == [(JUNE, june), (JULY, None)]
            assert held[0].approved and held[0].unit == er and held[0].nurses == {"Dana"}
            assert isinstance(held[1].refusal(), in_force.NotApproved)

    def test_leave_in_no_month_holds_nothing(self, er):
        with transaction.atomic():
            assert in_force.hold_for_leave(er.pk, [], nurse="Dana") == []
