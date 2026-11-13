"""Changes to an approved roster: the manager's direct edits, each a new version in force at once with a reason, leave recorded
late taking a shift off as a version of its own, the version list and the comparison of two versions, and who reads or is told
what. The API with the scripted solver and the real worker's code; the roster approved is a real one of the four-nurse sample
(`fixtures/sound_rosters.json`), so the checks are the solver's own code on a sound grid."""
import datetime as dt

import pytest

from accounts.models import Role, User
from audit.models import AuditEntry
from conftest import post
from hospital.models import LeaveType, Shift
from notifications.models import Notification
from nurses import membership
from nurses.models import Leave, NurseProfile, Placement
from rosters import drafts
from rosters.api import NOTHING_CHANGED
from rosters.drafts import FROZEN, in_current_codes
from rosters.models import PinEnded, PinnedCell, PinOrigin, RosterState, RosterVersion, VersionOrigin
from rosters.tests.test_pins import change_shift, clear, icu, off, pin, save
from rosters.tests.test_review import (approved, carla, dina, month_of, ok, profile, refused, roster, solved,  # noqa: F401
                                       submitted, url)
from solves.tests.conftest import MONTH, sample_rules

pytestmark = pytest.mark.django_db

JULY = "2026-07"
REASON = "Badr covers for Amal, who is sick on the 9th."
# In the approved June (`fixtures/sound_rosters.json`): Amal works Night on the 1st to the 4th and is off on the 5th and the 9th;
# Badr works Day on the 2nd and is off on the 3rd; Emad works Day on the 1st.


@pytest.fixture(autouse=True)
def _scene(before_june, solver):
    pass


@pytest.fixture
def nurses(make_user):
    """The sample's nurses can sign in to read what they are told and shown."""
    def sign_in(hospital_id):
        from notifications.tests.test_events import as_user
        return as_user(hospital_id)
    return sign_in


def change(client, unit, *edits, reason=REASON, month=MONTH):
    body = {"month": month, "edits": list(edits)}
    if reason is not None:
        body["reason"] = reason
    return post(client, url(unit, "/roster/versions"), body)


def changed(client, unit, *edits, reason=REASON, month=MONTH) -> dict:
    return ok(change(client, unit, *edits, reason=reason, month=month))


def versions(client, unit, month=MONTH) -> list[dict]:
    return ok(  # type: ignore[return-value]
        client.get(url(unit, "/roster/versions"), {"month": month}))


def compare(client, unit, first, second, month=MONTH):
    return client.get(url(unit, "/roster/versions/compare"), {"month": month, "from_version": first, "to_version": second})


def latest(unit, month=MONTH) -> RosterVersion:
    return roster(unit, month).versions.first()


def in_force_agrees(unit, month=MONTH) -> None:
    """`Roster.grid` is the latest version's grid under today's codes, which every reader of the roster in force relies on."""
    assert roster(unit, month).grid == in_current_codes(latest(unit, month).grid)


def cell_of(unit, nurse, day, month=MONTH) -> str:
    return roster(unit, month).grid[nurse][f"{month}-{day:02d}"]


def everything_said(user) -> str:
    """Every word a user can be told, and every identifier kept for them."""
    notes = Notification.objects.filter(user=user)
    return " ".join(str(note.payload) for note in notes)


def leave_for(client, unit, nurse, first, last=None, month=MONTH, kind="Sick"):
    return post(client, url(unit, "/leave"), {
        "nurse_id": str(profile(nurse).pk), "leave_type_id": str(LeaveType.objects.get(name=kind).pk),
        "first": f"{month}-{first:02d}", "last": f"{month}-{(last or first):02d}"})


class TestADirectEdit:
    def test_it_is_a_new_version_in_force_at_once_with_who_when_and_why(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)

        answer = changed(mona, er, pin("Amal", 9, "N"))

        version = latest(er)
        assert (version.number, version.origin, version.reason) == (2, VersionOrigin.EDIT, REASON)
        assert version.created_by.username == "mona" and version.solve is None
        assert version.grid["Amal"]["2026-06-09"] == "N"
        assert cell_of(er, "Amal", 9) == "N"
        in_force_agrees(er)
        assert answer["state"] == "approved"
        assert answer["approved"]["version"] == 2
        assert roster(er).state == RosterState.APPROVED

    def test_an_edited_cell_becomes_a_managers_pin_that_a_re_plan_will_keep(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)

        changed(mona, er, pin("Amal", 9, "N"), off("Badr", 2))

        pins = {(p.nurse.user.username, p.date.day): (p.code, p.origin, p.pinned_by.username)
                for p in PinnedCell.objects.current()}
        assert pins == {("Amal", 9): ("N", PinOrigin.MANAGER, "mona"), ("Badr", 2): ("", PinOrigin.MANAGER, "mona")}
        shown = month_of(mona, er)["draft"]["pins"]
        assert ("Amal", "2026-06-09", "N") in {(p["hospital_id"], p["date"], p["shift"]) for p in shown}

    def test_each_save_is_the_next_number_and_version_1_stays_as_approved(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        first = latest(er).grid

        changed(mona, er, pin("Amal", 9, "N"))
        changed(mona, er, off("Badr", 2), reason="Badr asked for the 2nd off.")

        assert [v.number for v in roster(er).versions.all()] == [3, 2, 1]
        assert RosterVersion.objects.get(number=1).grid == first
        assert latest(er).grid["Amal"]["2026-06-09"] == "N" and latest(er).grid["Badr"]["2026-06-02"] == ""
        in_force_agrees(er)

    @pytest.mark.parametrize("reason", [None, "", "   "])
    def test_it_needs_a_reason(self, reason, mona, carla, er, solver):
        approved(mona, carla, er, solver)

        response = change(mona, er, pin("Amal", 9, "N"), reason=reason)

        assert response.status_code == 422, response.content
        assert RosterVersion.objects.count() == 1 and not PinnedCell.objects.exists()

    def test_the_drafts_save_is_refused_on_it_and_says_where_to_go(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)

        assert "new version" in refused(save(mona, er, pin("Amal", 9, "N")))
        assert RosterVersion.objects.count() == 1 and not PinnedCell.objects.exists()

    def test_the_check_answers_on_it_as_the_editor_reads_it(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)

        answer = ok(post(mona, url(er, "/roster/check"), {"month": MONTH, "edits": [pin("Amal", 5, "D")]}))

        assert answer["ok"] is False and answer["delta"] > 0
        assert month_of(mona, er)["draft"]["editor"]["refusal"] is None

    def test_a_draft_has_no_versions_to_save_to(self, mona, er, solver):
        solved(mona, er, solver)

        assert "not approved" in refused(change(mona, er, pin("Amal", 9, "N")))
        assert not RosterVersion.objects.exists() and not PinnedCell.objects.exists()

    def test_a_submitted_roster_stays_frozen(self, mona, er, solver):
        submitted(mona, er, solver)

        assert refused(change(mona, er, pin("Amal", 9, "N"))) == FROZEN[RosterState.SUBMITTED]
        assert month_of(mona, er)["draft"]["editor"]["refusal"] == FROZEN[RosterState.SUBMITTED]

    def test_a_solve_and_a_restore_stay_refused_since_a_re_plan_is_another_action(self, mona, carla, er, solver):
        solve = approved(mona, carla, er, solver)
        changed(mona, er, pin("Amal", 9, "N"))

        assert refused(post(mona, url(er, "/solves"), {"month": MONTH, "time_limit_seconds": 60})) == FROZEN[RosterState.APPROVED]
        assert refused(post(mona, url(er, f"/solve-history/{solve}/restore"), {})) == FROZEN[RosterState.APPROVED]
        assert drafts.why_frozen(roster(er)) == FROZEN[RosterState.APPROVED]

    def test_one_that_breaks_a_hard_rule_saves_and_says_so(self, mona, carla, er, solver):
        """Taking a sick nurse off with nobody to cover leaves a shift short: the roster in force has to say what is so."""
        approved(mona, carla, er, solver)

        answer = changed(mona, er, pin("Amal", 5, "D"))

        assert latest(er).number == 2
        assert answer["draft"]["check"]["ok"] is False
        assert answer["draft"]["check"]["violations"]

    def test_a_pin_from_a_request_or_a_swap_is_refused_as_on_the_draft(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        PinnedCell.objects.create(unit=er, nurse=profile("Amal"), date=dt.date(2026, 6, 9), shift=None, origin=PinOrigin.REQUEST,
                                  pinned_by=User.objects.get(username="mona"))

        response = change(mona, er, pin("Amal", 9, "N"))

        assert response.status_code == 422
        assert "accepted request" in response.json()["detail"][0]["msg"]
        assert RosterVersion.objects.count() == 1

    def test_a_month_that_is_over_cannot_change(self, mona, carla, er, solver, monkeypatch):
        approved(mona, carla, er, solver)
        monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 7, 2))

        assert "over" in refused(change(mona, er, pin("Amal", 9, "N")))
        assert "over" in month_of(mona, er)["draft"]["editor"]["refusal"]
        assert RosterVersion.objects.count() == 1

    def test_the_month_under_way_can_change(self, mona, carla, er, solver, monkeypatch):
        approved(mona, carla, er, solver)
        monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 6, 20))

        changed(mona, er, pin("Amal", 9, "N"))

        assert latest(er).number == 2

    def nothing_was_made(self, er) -> None:
        assert RosterVersion.objects.count() == 1 and not PinnedCell.objects.exists()
        assert not AuditEntry.objects.filter(action="roster.changed").exists()
        assert not Notification.objects.filter(event="roster_changed").exists()

    def test_a_save_that_changes_no_cell_is_refused_and_makes_nothing(self, mona, carla, er, solver):
        """No version and nobody told for a save the nurses could not see."""
        approved(mona, carla, er, solver)

        assert refused(change(mona, er, clear("Amal", 9))) == NOTHING_CHANGED

        self.nothing_was_made(er)

    def test_pinning_a_cell_to_what_it_holds_is_nothing_changed_too(self, mona, carla, er, solver):
        """A pin with no cell changed is no version either: nothing is saved, the pin included."""
        approved(mona, carla, er, solver)

        assert refused(change(mona, er, pin("Amal", 1, "N"), off("Amal", 5), reason="Amal keeps her nights.")) == NOTHING_CHANGED

        self.nothing_was_made(er)

    def test_clearing_a_pin_gives_the_cell_back_to_its_solve_as_on_the_draft(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        changed(mona, er, pin("Amal", 9, "N"))

        changed(mona, er, clear("Amal", 9), reason="Amal is well again.")

        assert cell_of(er, "Amal", 9) == "" and latest(er).number == 3
        assert PinnedCell.objects.get().removed_why == PinEnded.CLEARED
        in_force_agrees(er)

    def test_only_the_units_manager_may(self, mona, carla, er, solver, client_of, make_user):
        approved(mona, carla, er, solver)
        omar = client_of("omar", Role.UNIT_MANAGER)
        icu(User.objects.get(username="omar"))

        assert change(carla, er, pin("Amal", 9, "N")).status_code == 403
        assert change(omar, er, pin("Amal", 9, "N")).status_code in (403, 404)
        assert RosterVersion.objects.count() == 1

    def test_it_is_audited_as_a_version_made_and_never_why(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)

        changed(mona, er, pin("Amal", 9, "N"), off("Badr", 2))

        [entry] = AuditEntry.objects.filter(action="roster.changed")
        assert entry.target == "ER, 2026-06"
        assert entry.after == {"version": 2, "pins": 2, "cells": 2}
        assert entry.actor.username == "mona"
        everything = " ".join(str([e.target, e.before, e.after]) for e in AuditEntry.objects.all())
        assert "sick" not in everything.lower() and "covers" not in everything and "Amal" not in everything


class TestOvertimeAboveAShare:
    def test_an_edit_giving_a_nurse_overtime_above_her_share_is_not_refused_and_july_carries_her_balance(self, mona, carla, er,
                                                                                                         solver):
        """The rule is priced, never refused (`one_more_shift_past_her_share`)."""
        from closes.tests.test_close import JULY as JULY_DATE, balances, one_more_shift_past_her_share
        july = one_more_shift_past_her_share("Amal")
        approved(mona, carla, er, solver)

        changed(mona, er, pin("Amal", 9, "N"))

        assert cell_of(er, "Amal", 9) == "N"
        assert balances(er, JULY_DATE) == july


class TestTheVersionList:
    def test_newest_first_with_author_time_reason_origin_and_cells_changed(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        changed(mona, er, pin("Amal", 9, "N"), off("Badr", 2))

        rows = versions(mona, er)

        assert [(r["number"], r["origin"], r["created_by"], r["reason"], r["cells_changed"]) for r in rows] == [
            (2, "edit", "Mona Ali", REASON, 2), (1, "solve", "Carla CNO", "", None)]
        assert rows[0]["created_at"].endswith("+03:00")

    @pytest.mark.parametrize("approver", ["carla", "dina"])
    def test_the_approvers_read_it_and_the_reason(self, approver, request, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        changed(mona, er, pin("Amal", 9, "N"))

        assert versions(request.getfixturevalue(approver), er)[0]["reason"] == REASON

    def test_a_month_with_no_approved_roster_has_none(self, mona, er, solver):
        solved(mona, er, solver)

        assert versions(mona, er) == []

    def test_a_nurse_another_units_manager_and_the_system_admin_cannot_read_it(self, mona, carla, er, solver, client_of, nurses):
        approved(mona, carla, er, solver)
        changed(mona, er, pin("Amal", 9, "N"))
        omar = client_of("omar", Role.UNIT_MANAGER)
        icu(User.objects.get(username="omar"))
        admin = client_of("root", Role.SYSTEM_ADMIN)

        for reader in (nurses("Amal"), admin):
            assert reader.get(url(er, "/roster/versions"), {"month": MONTH}).status_code == 403
            assert compare(reader, er, 1, 2).status_code == 403
        assert omar.get(url(er, "/roster/versions"), {"month": MONTH}).status_code in (403, 404)
        assert compare(omar, er, 1, 2).status_code in (403, 404)


class TestComparing:
    def test_cell_by_cell_from_one_to_the_other(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        changed(mona, er, pin("Amal", 9, "N"), off("Badr", 2))

        answer = ok(compare(mona, er, 1, 2))

        assert (answer["from_version"], answer["to_version"]) == (1, 2)
        assert answer["changes"] == [
            {"hospital_id": "Amal", "full_name": "Amal", "date": "2026-06-09", "before": "", "after": "N"},
            {"hospital_id": "Badr", "full_name": "Badr", "date": "2026-06-02", "before": "D", "after": ""}]
        assert ok(compare(mona, er, 2, 1))["changes"][0] == {
            "hospital_id": "Amal", "full_name": "Amal", "date": "2026-06-09", "before": "N", "after": ""}

    def test_any_two_and_the_same_one_twice_is_no_change(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        changed(mona, er, pin("Amal", 9, "N"))
        changed(mona, er, off("Badr", 2))

        assert {(c["hospital_id"], c["date"]) for c in ok(compare(mona, er, 1, 3))["changes"]} == {
            ("Amal", "2026-06-09"), ("Badr", "2026-06-02")}
        assert ok(compare(mona, er, 2, 2))["changes"] == []

    def test_a_rename_between_versions_is_not_a_change(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        changed(mona, er, pin("Amal", 9, "N"))
        assert change_shift(carla, "N", code="NT").status_code == 200
        changed(mona, er, off("Badr", 2))

        answer = ok(compare(mona, er, 1, 3))

        assert {(c["hospital_id"], c["before"], c["after"]) for c in answer["changes"]} == {
            ("Amal", "", "NT"), ("Badr", "D", "")}
        assert latest(er).grid["Amal"]["2026-06-01"] == "NT"
        assert RosterVersion.objects.get(number=2).grid["Amal"]["2026-06-01"] == "N"

    def test_a_version_that_does_not_exist_is_a_404(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)

        assert compare(mona, er, 1, 7).status_code == 404

    @pytest.mark.parametrize("approver", ["carla", "dina"])
    def test_the_approvers_may_compare(self, approver, request, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        changed(mona, er, pin("Amal", 9, "N"))

        assert len(ok(compare(request.getfixturevalue(approver), er, 1, 2))["changes"]) == 1


class TestTheRosterInForce:
    def test_the_grid_agrees_with_the_latest_version_after_an_edit_and_a_rename_after_it(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        changed(mona, er, pin("Amal", 9, "N"))
        in_force_agrees(er)

        assert change_shift(carla, "N", code="NT").status_code == 200

        in_force_agrees(er)
        assert cell_of(er, "Amal", 9) == "NT" and latest(er).grid["Amal"]["2026-06-09"] == "N"

    def test_the_hours_guard_reads_the_roster_in_force_after_an_edit_and_a_rename(self, mona, carla, er, solver):
        """`drafts.using_shift` reads `Roster.grid` only, which is enough while it agrees with the version in force."""
        approved(mona, carla, er, solver)
        changed(mona, er, pin("Amal", 9, "N"))
        assert change_shift(carla, "N", code="NT").status_code == 200

        assert roster(er) in drafts.using_shift("NT") and roster(er) not in drafts.using_shift("N")
        response = change_shift(carla, "NT", hours=10)
        assert response.status_code == 422 and "ER, June 2026" in response.json()["detail"][0]["msg"]

    def test_a_nurse_reads_the_latest_version_and_its_number(self, mona, carla, er, solver, nurses):
        approved(mona, carla, er, solver)
        changed(mona, er, pin("Amal", 9, "N"))

        answer = ok(nurses("Amal").get("/api/rosters/mine", {"month": MONTH}))["roster"]

        assert answer["version"] == 2
        assert answer["mine"][8] == {"date": "2026-06-09", "kind": "shift"}
        assert ok(nurses("Amal").get("/api/rosters/mine/month", {"month": MONTH}))["roster"]["version"] == 2

    def test_a_nurses_answer_holds_no_reason(self, mona, carla, er, solver, nurses):
        approved(mona, carla, er, solver)
        changed(mona, er, pin("Amal", 9, "N"))

        for nurse in ("Amal", "Badr"):
            for path in ("/api/rosters/mine", "/api/rosters/mine/month"):
                assert "covers" not in nurses(nurse).get(path, {"month": MONTH}).content.decode()


def works_e(unit) -> None:
    """A 12-hour Evening shift in the catalogue, which the unit works beside Day and Night."""
    Shift.objects.create(code="E", display_name="Evening", hours=12, start_time="14:00", end_time="02:00", colour="#00aa00",
                         active=True)
    unit.rules = unit.rules | {"shifts": ["D", "N", "E"]}
    unit.save()


class TestAUnitDroppingAShiftTheRosterInForceHolds:
    """A roster in force reaches the solver whole, as the re-plan's `published` and as month close's facts, and the solver refuses
    a cell on a shift its unit does not work. So the unit may not stop working a shift that a submitted or approved roster of a
    month not yet over still holds, as it may not under a pin."""

    @pytest.fixture
    def holding_e(self, mona, carla, er, solver):
        """Amal works E on 3 June in the roster in force, with no pin there: as approved."""
        works_e(er)
        approved(mona, carla, er, solver)
        version = latest(er)
        version.grid["Amal"]["2026-06-03"] = "E"
        version.save()
        held = roster(er)
        held.grid = version.grid
        held.save(update_fields=["grid"])
        in_force_agrees(er)
        return er

    @staticmethod
    def take_e_out(client, unit):
        body = {"rules": sample_rules(), "request_cap": unit.request_cap, "preference_cap": unit.preference_cap, "default_time_limit": unit.default_time_limit}
        return client.put(f"/api/units/{unit.pk}/rules", body, content_type="application/json")

    def test_it_is_refused_naming_the_month_who_and_when(self, mona, holding_e):
        response = self.take_e_out(mona, holding_e)

        assert response.status_code == 422, response.content
        assert response.json()["detail"][0]["msg"] == (
            "The roster in force for June 2026 has Amal on E on 3 June 2026. Change those cells on the Roster page first, "
            "or take E out once the month is over.")
        assert "E" in type(holding_e).objects.get(pk=holding_e.pk).rules["shifts"]

    def test_a_submitted_roster_holding_it_refuses_it_too(self, mona, carla, er, solver):
        works_e(er)
        submitted(mona, er, solver)
        held = roster(er)
        held.grid["Amal"]["2026-06-03"] = "E"
        held.save(update_fields=["grid"])

        response = self.take_e_out(mona, er)

        assert response.status_code == 422, response.content
        assert response.json()["detail"][0]["msg"] == (
            "The submitted roster for June 2026 has Amal on E on 3 June 2026. Wait for its decision first, or take E out once the "
            "month is over.")

    def test_once_its_cells_are_changed_it_goes_through(self, mona, holding_e):
        changed(mona, holding_e, pin("Amal", 3, "D"))

        assert self.take_e_out(mona, holding_e).status_code == 200

    def test_a_month_holding_a_shift_already_taken_out_does_not_stop_other_saves(self, mona, holding_e):
        """Only a shift being taken out is checked: one dropped before this check existed must not stop every save after."""
        holding_e.rules = holding_e.rules | {"shifts": ["D", "N"]}
        holding_e.save()

        assert self.take_e_out(mona, holding_e).status_code == 200

    def test_a_month_that_is_over_does_not_stop_it(self, mona, holding_e, monkeypatch):
        monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 7, 1))

        assert self.take_e_out(mona, holding_e).status_code == 200


class TestNursesAreTold:
    def test_every_nurse_of_the_month_and_never_the_manager_who_made_it(self, mona, carla, er, solver, nurses):
        approved(mona, carla, er, solver)

        changed(mona, er, pin("Amal", 9, "N"))

        told = Notification.objects.filter(event="roster_changed")
        assert sorted(note.user.username for note in told) == ["Amal", "Badr", "Dana", "Emad"]
        [note] = ok(nurses("Badr").get("/api/notifications"))["items"][:1]
        assert note["event"] == "roster_changed"
        assert note["text"] == "Mona Ali changed the ER roster for June 2026."
        assert note["link"] == {"page": "my_roster", "unit_id": None, "month": "2026-06", "cover_id": None}

    def test_it_carries_no_reason_cell_name_or_version(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        changed(mona, er, pin("Amal", 9, "N"))

        for note in Notification.objects.filter(event="roster_changed"):
            assert set(note.payload) == {"unit_id", "month", "by"}
        said = " ".join(everything_said(user) for user in User.objects.all())
        assert "covers" not in said and "Amal" not in said and "2026-06-09" not in said

    def test_each_new_version_tells_them_again(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        changed(mona, er, pin("Amal", 9, "N"))
        changed(mona, er, off("Badr", 2))

        assert Notification.objects.filter(event="roster_changed", user__username="Dana").count() == 2

    def test_a_manager_who_is_a_nurse_of_the_month_and_made_it_is_not_told(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        nurse = NurseProfile.objects.create(user=User.objects.get(username="mona"), gender="F", start_date=dt.date(2026, 1, 1))
        Placement.objects.create(nurse=nurse, unit=er, from_month=dt.date(2026, 1, 1))

        changed(mona, er, pin("Amal", 9, "N"))

        assert not Notification.objects.filter(event="roster_changed", user__username="mona").exists()


class TestLeaveRecordedLate:
    """Leave wins over a shift. On an approved roster of a month not over, recording it on a day the roster in force works takes
    the shift off as a version of its own, by whoever recorded it, and the nurses are told of a change with no word of why."""

    def test_it_takes_the_shift_off_as_a_new_version_by_the_recorder(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)

        response = leave_for(mona, er, "Amal", 1, 2)

        assert response.status_code == 201, response.content
        assert [{key: row[key] for key in ("date", "shift", "version")} for row in response.json()["shifts_taken_off"]] == [
            {"date": "2026-06-01", "shift": "N", "version": 2}, {"date": "2026-06-02", "shift": "N", "version": 2}]
        version = latest(er)
        assert (version.number, version.origin, version.created_by.username) == (2, VersionOrigin.EDIT, "mona")
        assert cell_of(er, "Amal", 1) == "" and cell_of(er, "Amal", 2) == "" and cell_of(er, "Amal", 3) == "N"
        in_force_agrees(er)

    def test_its_reason_says_leave_and_never_the_type(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)

        leave_for(mona, er, "Amal", 1, kind="Sick")

        assert "leave" in latest(er).reason.lower() and "sick" not in latest(er).reason.lower()

    def test_the_nurses_are_told_with_no_word_of_why_and_the_audit_log_has_only_the_leave(self, mona, carla, er, solver,
                                                                                           nurses):
        approved(mona, carla, er, solver)

        leave_for(mona, er, "Amal", 1)

        told = Notification.objects.filter(event="roster_changed")
        assert sorted(note.user.username for note in told) == ["Amal", "Badr", "Dana", "Emad"]
        assert all(set(note.payload) == {"unit_id", "month", "by"} for note in told)
        assert ok(nurses("Badr").get("/api/notifications"))["items"][0]["text"] == "Mona Ali changed the ER roster for June 2026."
        assert not AuditEntry.objects.filter(action="roster.changed").exists()
        assert AuditEntry.objects.filter(action="leave.recorded").count() == 1

    def test_a_colleague_reads_a_day_off_and_the_nurse_reads_leave(self, mona, carla, er, solver, nurses):
        approved(mona, carla, er, solver)

        leave_for(mona, er, "Amal", 1)

        colleague = ok(nurses("Badr").get("/api/rosters/mine", {"month": MONTH}))["roster"]
        amal = next(row for row in colleague["rows"] if row["hospital_id"] == "Amal")
        assert amal["shifts"][0] == ""
        assert "leave" not in nurses("Badr").get("/api/rosters/mine", {"month": MONTH}).content.decode().lower()
        mine = ok(nurses("Amal").get("/api/rosters/mine", {"month": MONTH}))["roster"]["mine"]
        assert mine[0] == {"date": "2026-06-01", "kind": "leave"}

    def test_leave_on_a_day_off_changes_no_cell_and_makes_no_version(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)

        response = leave_for(mona, er, "Amal", 5)

        assert response.json()["shifts_taken_off"] == []
        assert RosterVersion.objects.count() == 1 and not Notification.objects.filter(event="roster_changed").exists()

    def test_it_ends_a_pin_there_and_takes_its_shift_off(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        changed(mona, er, pin("Amal", 9, "N"))

        response = leave_for(mona, er, "Amal", 9)

        assert response.json()["pins_removed"] == [{"date": "2026-06-09", "shift": "N", "origin": "manager", "roster": "approved"}]
        assert [(row["date"], row["shift"], row["version"]) for row in response.json()["shifts_taken_off"]] == [("2026-06-09", "N", 3)]
        assert cell_of(er, "Amal", 9) == "" and not PinnedCell.objects.current().exists()

    def test_removing_it_afterwards_puts_nothing_back(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        leave_for(mona, er, "Amal", 1)
        [row] = Leave.objects.current()

        assert post(mona, url(er, "/leave/remove"), {"leave_ids": [str(row.pk)]}).status_code == 204

        assert cell_of(er, "Amal", 1) == "" and latest(er).number == 2
        assert Notification.objects.filter(event="roster_changed", user__username="Dana").count() == 1

    def test_changing_it_onto_another_worked_day_takes_that_shift_off_too(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        leave_for(mona, er, "Amal", 1)
        [row] = Leave.objects.current()

        response = post(mona, url(er, "/leave/change"), {
            "leave_ids": [str(row.pk)], "leave_type_id": str(row.leave_type_id), "first": "2026-06-01", "last": "2026-06-02"})

        assert response.status_code == 200, response.content
        assert [(row["date"], row["shift"], row["version"]) for row in response.json()["shifts_taken_off"]] == [("2026-06-02", "N", 3)]
        assert cell_of(er, "Amal", 2) == ""

    def test_one_write_over_two_approved_months_makes_a_version_of_each(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        approved(mona, carla, er, solver, month=JULY)

        response = post(mona, url(er, "/leave"), {
            "nurse_id": str(profile("Emad").pk), "leave_type_id": str(LeaveType.objects.get(name="Sick").pk),
            "first": "2026-06-30", "last": "2026-07-01"})

        assert response.status_code == 201, response.content
        assert [(row["date"], row["version"]) for row in response.json()["shifts_taken_off"]] == [("2026-07-01", 2)]
        assert latest(er).number == 1, "Emad is off on 30 June"
        assert cell_of(er, "Emad", 1, month=JULY) == "" and latest(er, JULY).number == 2
        in_force_agrees(er, JULY)

    def test_a_month_that_is_over_keeps_its_grid_for_month_close(self, mona, carla, er, solver, monkeypatch):
        approved(mona, carla, er, solver)
        monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 7, 2))

        response = leave_for(mona, er, "Amal", 1)

        assert response.status_code == 201, response.content
        assert response.json()["shifts_taken_off"] == []
        assert cell_of(er, "Amal", 1) == "N" and latest(er).number == 1

    def test_a_submitted_roster_keeps_its_shift_for_the_approver_to_see(self, mona, er, solver):
        submitted(mona, er, solver)

        response = leave_for(mona, er, "Amal", 1)

        assert response.json()["shifts_taken_off"] == []
        assert cell_of(er, "Amal", 1) == "N" and not RosterVersion.objects.exists()

    def test_a_draft_keeps_its_shift_until_a_solve_or_a_hand_edit(self, mona, er, solver):
        solved(mona, er, solver)

        assert leave_for(mona, er, "Amal", 1).json()["shifts_taken_off"] == []
        assert cell_of(er, "Amal", 1) == "N"

    def test_leave_recorded_by_the_units_next_manager_is_that_managers_version(self, mona, carla, er, solver, client_of):
        approved(mona, carla, er, solver)
        nadia = client_of("nadia", Role.UNIT_MANAGER, full_name="Nadia Noor")
        er.manager = User.objects.get(username="nadia")
        er.save()

        assert leave_for(nadia, er, "Amal", 1).status_code == 201

        assert latest(er).created_by.username == "nadia"
        told = {note.user.username for note in Notification.objects.filter(event="roster_changed")}
        assert told == {"Amal", "Badr", "Dana", "Emad"}


class TestNursesWhoseDatesOrUnitChanged:
    def test_a_start_date_corrected_after_approval_lets_the_manager_take_those_shifts_off(self, mona, carla, er, solver):
        approved(mona, carla, er, solver)
        NurseProfile.objects.filter(user__username="Amal").update(start_date=dt.date(2026, 6, 3))

        assert "outside" in change(mona, er, pin("Amal", 1, "N")).json()["detail"][0]["msg"]
        changed(mona, er, clear("Amal", 1), clear("Amal", 2), reason="Amal starts on the 3rd.")

        assert cell_of(er, "Amal", 1) == "" and cell_of(er, "Amal", 2) == "" and latest(er).number == 2

    def test_a_nurse_transferred_out_can_have_their_shifts_taken_off_and_nothing_pinned(self, mona, carla, er, solver, client_of):
        approved(mona, carla, er, solver)
        other = icu(client_of("omar", Role.UNIT_MANAGER) and User.objects.get(username="omar"))
        assert carla.post(f"/api/nurses/{profile('Amal').pk}/transfer", {"unit_id": str(other.pk), "from_month": MONTH},
                          content_type="application/json").status_code == 200

        assert change(mona, er, off("Amal", 1)).status_code == 422
        changed(mona, er, clear("Amal", 1), reason="Amal has moved to ICU.")

        assert cell_of(er, "Amal", 1) == "" and not PinnedCell.objects.exists()
        editor = month_of(mona, er)["draft"]["editor"]
        assert "Amal" in editor["gone"]
