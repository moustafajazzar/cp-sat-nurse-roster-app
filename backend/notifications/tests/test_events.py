"""Who is told of what, read through the endpoints the bell and the Notifications page use. The roster's way is the API with the
scripted solver; a solve's end is the real worker's code."""
import datetime as dt
import json

import pytest
from django.test import Client

from accounts.models import Role, User
from conftest import PASSWORD, post
from notifications.models import Notification
from nurses.models import NurseProfile, Placement
from nurses.tests.conftest import make_unit
from solves.tests.conftest import nurse_in
from units.models import Unit
from ops.solver_client import SolverRefused
from rosters.models import Roster
from rosters.tests.test_review import (add_holiday, approve, carla, dina, fix_ramadan, ok, reject, solved, sound,  # noqa: F401
                                        submit)
from solves import running
from solves.tests.conftest import job, make_sample_unit, result_of, started

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _scene(before_june, solver):
    pass


def as_user(username) -> Client:
    client = Client()
    assert post(client, "/api/auth/login", {"username": username, "password": PASSWORD}).status_code == 200
    return client


def told(client, *events) -> list[dict]:
    """Newest first. Only these events, when named: a manager is told of each solve's end too."""
    items = ok(client.get("/api/notifications"))["items"]
    return [item for item in items if not events or item["event"] in events]


def unread(client) -> int:
    return ok(client.get("/api/notifications/unread"))["unread"]


class TestSubmitted:
    def test_both_approvers_are_told_and_nobody_else(self, mona, er, solver, carla, dina):
        solved(mona, er, solver)
        ok(submit(mona, er))

        for approver in (carla, dina):
            [note] = told(approver)
            assert note["event"] == "roster_submitted"
            assert note["text"] == "Mona Ali submitted the ER roster for June 2026."
            assert note["link"] == {"page": "unit_roster", "unit_id": str(er.pk), "month": "2026-06", "cover_id": None}
            assert note["read"] is False
            assert unread(approver) == 1
        assert told(mona, "roster_submitted") == [] and told(as_user("Amal")) == []


def joining(unit, make_user, hospital_id, **fields):
    """A nurse placed in the unit who starts in July, so June's roster is theirs to read and its solve has no row of theirs."""
    return nurse_in(unit, make_user, hospital_id, start=dt.date(2026, 7, 1), **fields)


def every_payload() -> str:
    return json.dumps(list(Notification.objects.values_list("payload", flat=True)))


class TestApproved:
    def test_the_manager_and_every_nurse_of_the_month_are_told(self, mona, er, solver, carla, dina, make_user):
        joining(er, make_user, "Faris", full_name="Faris Joining")
        solved(mona, er, solver)
        ok(submit(mona, er))
        ok(approve(carla, er))

        [to_mona] = told(mona, "roster_approved")
        assert to_mona["text"] == "Carla CNO approved the ER roster for June 2026."
        assert to_mona["link"] == {"page": "roster", "month": "2026-06", "unit_id": None, "cover_id": None}
        for nurse in ("Amal", "Badr", "Dana", "Emad", "Faris"):
            [note] = told(as_user(nurse))
            assert note["text"] == to_mona["text"]
            assert note["link"] == {"page": "my_roster", "month": "2026-06", "unit_id": None, "cover_id": None}
        # The approvers were told of the submit only.
        assert [note["event"] for note in told(dina)] == ["roster_submitted"]

    def test_a_deactivated_account_is_told_nothing(self, mona, er, solver, carla, make_user):
        gone = joining(er, make_user, "Ghada")
        User.objects.filter(pk=gone.user_id).update(is_active=False)
        solved(mona, er, solver)
        ok(submit(mona, er))
        ok(approve(carla, er))

        assert not Notification.objects.filter(user=gone.user).exists()

    def test_a_manager_who_is_a_nurse_of_the_month_is_told_once(self, mona, er, solver, carla):
        nurse = NurseProfile.objects.create(user=User.objects.get(username="mona"), gender="F", start_date=dt.date(2026, 7, 1))
        Placement.objects.create(nurse=nurse, unit=er, from_month=dt.date(2026, 1, 1))
        solved(mona, er, solver)
        ok(submit(mona, er))
        ok(approve(carla, er))

        [note] = told(mona, "roster_approved")
        assert note["link"]["page"] == "roster"

    def test_a_unit_with_no_manager_tells_its_nurses(self, mona, er, solver, carla):
        solved(mona, er, solver)
        ok(submit(mona, er))
        Unit.objects.filter(pk=er.pk).update(manager=None)
        ok(approve(carla, er))

        assert told(mona, "roster_approved") == []
        assert len(told(as_user("Amal"))) == 1

    def test_a_nurse_of_another_unit_is_not_told(self, mona, er, solver, carla, make_user):
        omar = make_user("omar", Role.UNIT_MANAGER, full_name="Omar")
        icu = make_unit("ICU", omar)
        nurse_in(icu, make_user, "Hala")
        solved(mona, er, solver)
        ok(submit(mona, er))
        ok(approve(carla, er))

        assert told(as_user("Hala")) == [] and told(as_user("omar")) == []


class TestRejected:
    COMMENT = "Badr is on sick leave on the 2nd, so take him off."

    def test_only_the_manager_is_told_and_the_comment_is_kept_out(self, mona, er, solver, carla, dina, signed_in):
        admin = signed_in(Role.SYSTEM_ADMIN, username="sysadmin")
        solved(mona, er, solver)
        ok(submit(mona, er))
        ok(reject(dina, er, self.COMMENT))

        [note] = told(mona, "roster_rejected")
        assert note["text"] == "Dina Deputy rejected the ER roster for June 2026. The comment is with the draft."
        assert note["link"] == {"page": "roster", "month": "2026-06", "unit_id": None, "cover_id": None}
        for nurse in ("Amal", "Badr"):
            assert told(as_user(nurse)) == []
        assert told(admin) == []
        assert "Badr" not in every_payload() and "sick" not in every_payload()

    def test_a_unit_with_no_manager_tells_nobody(self, mona, er, solver, dina):
        solved(mona, er, solver)
        ok(submit(mona, er))
        Unit.objects.filter(pk=er.pk).update(manager=None)
        ok(reject(dina, er, self.COMMENT))

        assert not Notification.objects.filter(event="roster_rejected").exists()

    def test_a_manager_since_taken_off_the_unit_keeps_the_words_and_loses_the_link(self, mona, er, solver, dina):
        solved(mona, er, solver)
        ok(submit(mona, er))
        ok(reject(dina, er, self.COMMENT))
        Unit.objects.filter(pk=er.pk).update(manager=None)

        [note] = told(mona, "roster_rejected")
        assert note["link"] is None


class TestSolveEnded:
    def test_the_manager_is_told_its_roster_is_the_draft(self, mona, er, solver, carla):
        solved(mona, er, solver)

        [note] = told(mona)
        assert note["event"] == "solve_ended"
        assert note["text"] == "The solve of the ER roster for June 2026 has finished, and its roster is now the draft."
        assert note["link"] == {"page": "roster", "month": "2026-06", "unit_id": None, "cover_id": None}
        assert told(carla) == [] and told(as_user("Amal")) == []

    def test_a_failed_solve_is_told_too(self, mona, er, solver):
        solver.script(SolverRefused("bad input"))
        running.run(started(mona, er)["id"])

        [note] = told(mona)
        assert note["text"] == "The solve of the ER roster for June 2026 failed. Its result says why."

    def test_a_solve_with_no_roster_to_keep(self, mona, er, solver):
        solver.script([job("finished", result=result_of(None, ok=False) | {"roster": None})])
        running.run(started(mona, er)["id"])

        [note] = told(mona)
        assert note["text"] == "The solve of the ER roster for June 2026 has ended with no roster to keep. Its result says why."

    def test_a_manager_taken_off_the_unit_meanwhile_is_not_told_and_the_new_one_is(self, mona, er, solver, client_of):
        solver.script([sound()])
        solve = started(mona, er)
        noor = client_of("noor", Role.UNIT_MANAGER, full_name="Noor")
        Unit.objects.filter(pk=er.pk).update(manager=User.objects.get(username="noor"))
        running.run(solve["id"])

        assert told(mona) == []
        [note] = told(noor)
        assert note["event"] == "solve_ended" and note["link"]["page"] == "roster"

    def test_a_unit_left_with_no_manager_tells_nobody(self, mona, er, solver):
        solver.script([sound()])
        solve = started(mona, er)
        Unit.objects.filter(pk=er.pk).update(manager=None)
        running.run(solve["id"])

        assert not Notification.objects.exists()


def test_the_system_admin_is_told_of_nothing(mona, er, solver, carla, dina, signed_in):
    """The System Admin sees no roster, so no event of one reaches them: their bell stays empty."""
    admin = signed_in(Role.SYSTEM_ADMIN, username="sysadmin")
    solved(mona, er, solver)
    ok(submit(mona, er))
    ok(reject(dina, er, "Too many nights."))
    solved(mona, er, solver)
    ok(submit(mona, er))
    ok(approve(carla, er))

    assert told(admin) == []
    assert not Notification.objects.filter(user__role=Role.SYSTEM_ADMIN).exists()
    assert Notification.objects.count() > 0


class TestMonthDatesChanged:
    """The manager of every unit with a roster in a month whose Ramadan days or public holidays changed is told once,
    whatever its roster's state. Nobody with no roster in it, and never the CNO who made the change."""

    @pytest.fixture
    def three_rosters(self, mona, er, solver, dina, make_user, client_of):
        """ER's June is a draft, ICU's submitted and PICU's approved; NICU has no roster in June."""
        solved(mona, er, solver)
        omar, pia = client_of("omar", Role.UNIT_MANAGER, full_name="Omar"), client_of("pia", Role.UNIT_MANAGER, full_name="Pia")
        icu = make_sample_unit("ICU", User.objects.get(username="omar"), make_user, prefix="I")
        solved(omar, icu, solver, prefix="I")
        ok(submit(omar, icu))
        picu = make_sample_unit("PICU", User.objects.get(username="pia"), make_user, prefix="P")
        solved(pia, picu, solver, prefix="P")
        ok(submit(pia, picu))
        ok(approve(dina, picu))
        nadia = client_of("nadia", Role.UNIT_MANAGER, full_name="Nadia")
        make_unit("NICU", User.objects.get(username="nadia"))
        Notification.objects.all().delete()
        return {"ER": mona, "ICU": omar, "PICU": pia, "nobody": nadia}

    def test_every_manager_with_a_roster_in_the_month_is_told_once_of_a_ramadan_change(self, three_rosters, carla, dina):
        fix_ramadan(carla, "2026-06", 13, 14)

        for unit in ("ER", "ICU", "PICU"):
            [note] = told(three_rosters[unit])
            assert note["event"] == "month_dates_changed"
            assert note["text"] == (f"Carla CNO changed the Ramadan days of June 2026. Check the {unit} roster for that month: "
                                    "it was planned on the dates before.")
            assert note["link"] == {"page": "roster", "month": "2026-06", "unit_id": None, "cover_id": None}
        assert told(three_rosters["nobody"]) == [] and told(carla) == [] and told(dina) == []
        assert told(as_user("Amal")) == [], "nurses are not told"
        assert set(Notification.objects.values_list("payload__what", flat=True)) == {"ramadan_days"}

    def test_a_holiday_created_changed_or_removed_tells_them_each_time(self, three_rosters, carla):
        holiday = add_holiday(carla, "2026-06", 13)
        changed = carla.put(f"/api/hospital/public-holidays/{holiday}", {"date": "2026-06-20", "name": "A holiday"},
                            content_type="application/json")
        assert changed.status_code == 200, changed.content
        assert carla.delete(f"/api/hospital/public-holidays/{holiday}").status_code == 204

        notes = told(three_rosters["ICU"])
        assert [note["text"] for note in notes] == ["Carla CNO changed the public holidays of June 2026. Check the ICU roster "
                                                    "for that month: it was planned on the dates before."] * 3
        assert len(told(three_rosters["ER"])) == 3 and told(three_rosters["nobody"]) == [] and told(carla) == []

    def test_a_change_that_leaves_the_dates_as_they_were_tells_nobody(self, three_rosters, carla):
        """Audited, since who decides or a name changed, but no day moved: June has no Ramadan day by the calendar."""
        fix_ramadan(carla, "2026-06")
        assert carla.delete("/api/hospital/ramadan/2026-06").status_code == 200
        holiday = add_holiday(carla, "2026-09", 23, "National Day")
        renamed = carla.put(f"/api/hospital/public-holidays/{holiday}", {"date": "2026-09-23", "name": "Saudi National Day"},
                            content_type="application/json")
        assert renamed.status_code == 200, renamed.content
        fix_ramadan(carla, "2026-09", 1)

        assert not Notification.objects.exists(), "no roster in September, and June's dates did not move"

    def test_a_holiday_moved_to_another_month_tells_each_month_with_a_roster(self, mona, er, solver, carla):
        solved(mona, er, solver)
        solved(mona, er, solver, month="2026-07")
        Notification.objects.all().delete()
        holiday = add_holiday(carla, "2026-06", 13)
        moved = carla.put(f"/api/hospital/public-holidays/{holiday}", {"date": "2026-07-04", "name": "A holiday"},
                          content_type="application/json")
        assert moved.status_code == 200, moved.content

        assert [note["link"]["month"] for note in told(mona, "month_dates_changed")] == ["2026-07", "2026-06", "2026-06"]

    def test_a_proposal_added_on_several_days_tells_each_month_once(self, mona, er, solver, carla):
        """Adding public holidays creates a holiday a day through the holiday create, and tells the managers once for them all:
        six days across June and July, one of them a holiday already, are one note for each month."""
        solved(mona, er, solver)
        solved(mona, er, solver, month="2026-07")
        add_holiday(carla, "2026-06", 29)
        Notification.objects.all().delete()

        added = carla.post("/api/hospital/holiday-proposals?year=2026",
                           {"name": "A long Eid", "first": "2026-06-28", "length": 6}, content_type="application/json")
        assert added.status_code == 200, added.content
        assert added.json()["skipped"] == ["2026-06-29"]

        assert sorted(note["link"]["month"] for note in told(mona, "month_dates_changed")) == ["2026-06", "2026-07"]
        assert told(carla) == []

    def test_a_proposal_whose_days_are_all_holidays_tells_nobody(self, mona, er, solver, carla):
        solved(mona, er, solver)
        add_holiday(carla, "2026-06", 29)
        Notification.objects.all().delete()

        added = carla.post("/api/hospital/holiday-proposals?year=2026",
                           {"name": "A holiday", "first": "2026-06-29", "length": 1}, content_type="application/json")
        assert added.json() == {"added": [], "skipped": ["2026-06-29"]}

        assert not Notification.objects.exists()

    def test_a_confirmation_across_two_months_tells_each_once(self, mona, er, solver, carla):
        """Ramadan 1448 runs from February into March 2027. The scene's sound rosters are of June and July 2026, so its two
        rosters are moved to those months, which is all this asks of them."""
        solved(mona, er, solver)
        solved(mona, er, solver, month="2026-07")
        Roster.objects.filter(month=dt.date(2026, 6, 1)).update(month=dt.date(2027, 2, 1))
        Roster.objects.filter(month=dt.date(2026, 7, 1)).update(month=dt.date(2027, 3, 1))
        Notification.objects.all().delete()

        confirmed = carla.put("/api/hospital/ramadans/1448", {"first": "2027-02-09", "last": "2027-03-09"},
                              content_type="application/json")
        assert confirmed.status_code == 200, confirmed.content

        assert sorted(note["link"]["month"] for note in told(mona)) == ["2027-02", "2027-03"]
        assert told(carla) == []

    def test_confirming_the_calendars_own_dates_tells_nobody(self, mona, er, solver, carla):
        solved(mona, er, solver)
        Roster.objects.filter(month=dt.date(2026, 6, 1)).update(month=dt.date(2027, 2, 1))
        Notification.objects.all().delete()

        confirmed = carla.put("/api/hospital/ramadans/1448", {"first": "2027-02-08", "last": "2027-03-08"},
                              content_type="application/json")
        assert confirmed.status_code == 200, confirmed.content

        assert not Notification.objects.exists()
