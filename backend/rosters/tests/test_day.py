"""The Day view: one date across every unit, for the CNO and the Deputy CNO. The API with the scripted solver and the
real worker's code, on the four-nurse sample's sound June and July 2026 (`fixtures/sound_rosters.json`), approved. The counts
below are read by hand off the fixture's cells; the sample's ranges are Day 1 to 1 and Night 1 to 2 on every kind of day, and
Friday is its weekend day. Amal and Emad lead teams; Amal and Dana are women, Badr and Emad men.

In June: on the 4th (a Thursday) Emad works Day and Amal and Dana Night; on the 5th (a Friday) Emad Day and Dana Night; on the
2nd Badr Day and Amal Night; on the 19th Amal and Badr Night."""
import copy
import datetime as dt
import json

import pytest

from accounts.models import Role, User
from closes import making
from hospital.models import HospitalSettings, PublicHoliday
from notifications.tests.test_events import as_user
from nurses import membership
from nurses.models import NurseProfile
from rosters.tests.test_pins import off, pin
from rosters.tests.test_review import approve, carla, dina, ok, record_leave, solved, submit  # noqa: F401
from rosters.tests.test_versions import changed
from solves.tests.conftest import make_sample_unit

pytestmark = pytest.mark.django_db

NURSES = ["Amal", "Badr", "Dana", "Emad"]


@pytest.fixture(autouse=True)
def _scene(before_june, solver):
    pass


@pytest.fixture
def on(monkeypatch):
    def move_to(day: dt.date):
        monkeypatch.setattr(membership, "today", lambda: day)
    return move_to


def approved(mona, carla, er, solver, month="2026-06"):  # noqa: F811
    solved(mona, er, solver, month)
    ok(submit(mona, er, month))
    ok(approve(carla, er, month))


@pytest.fixture
def june(mona, carla, er, solver):  # noqa: F811
    approved(mona, carla, er, solver)


def day(client, date=None) -> dict:
    response = client.get("/api/rosters/day", {"date": date} if date else {})
    assert response.status_code == 200, response.content
    return response.json()


def row(answer, name="ER") -> dict:
    return next(unit for unit in answer["units"] if unit["name"] == name)


def cell(answer, code, name="ER") -> dict:
    return next(shift for shift in row(answer, name)["figures"]["shifts"] if shift["code"] == code)


def counts(answer, name="ER") -> dict:
    """Each shift as "present of minimum-maximum", with the words short or over, as a reader reads the cell."""
    def said(shift):
        words = f"{shift['present']}" + (f" of {shift['minimum']}-{shift['maximum']}" if shift["minimum"] is not None else "")
        return words + (" short" if shift["short"] else "") + (" over" if shift["over"] else "")
    return {shift["code"]: said(shift) for shift in row(answer, name)["figures"]["shifts"]}


def names(shift) -> list[tuple[str, bool]]:
    return [(nurse["full_name"], nurse["team_leader"]) for nurse in shift["nurses"]]


def set_rules(unit, **changes):
    unit.rules = unit.rules | changes
    unit.save()


def hospital_rules(**changes):
    settings = HospitalSettings.load()
    settings.rules = settings.rules | changes
    settings.save()


class TestTheDay:
    def test_a_weekday_with_its_columns_counts_and_names(self, june, carla):
        answer = day(carla, "2026-06-04")

        assert {key: answer[key] for key in ("date", "weekday", "weekend", "holiday")} == {
            "date": "2026-06-04", "weekday": "Thursday", "weekend": False, "holiday": None}
        assert [(column["code"], column["display_name"]) for column in answer["shifts"]] == [("D", "Day"), ("N", "Night")]
        er = row(answer)
        assert {key: er[key] for key in ("name", "archived", "state", "unchecked")} == {
            "name": "ER", "archived": False, "state": "approved", "unchecked": None}
        assert counts(answer) == {"D": "1 of 1-1", "N": "2 of 1-2"}
        assert names(cell(answer, "D")) == [("Emad", True)]
        assert names(cell(answer, "N")) == [("Amal", True), ("Dana", False)]
        assert cell(answer, "D")["team_leader"] is None and cell(answer, "D")["both_genders"] is None
        assert er["figures"]["on_leave"] == []

    def test_a_weekend_day_reads_the_weekend_range_and_a_holiday_is_named(self, june, carla, er):
        set_rules(er, headcounts=[{"shift": "D", "weekday_min": 1, "weekday_max": 1, "weekend_min": 2, "weekend_max": 3,
                                   "also_counts": []},
                                  {"shift": "N", "weekday_min": 1, "weekday_max": 2, "weekend_min": 1, "weekend_max": 2,
                                   "also_counts": []}])
        PublicHoliday.objects.create(date=dt.date(2026, 6, 4), name="Founding Day")

        friday, thursday = day(carla, "2026-06-05"), day(carla, "2026-06-04")

        assert friday["weekday"] == "Friday" and friday["weekend"] is True and friday["holiday"] is None
        assert counts(friday) == {"D": "1 of 2-3 short", "N": "1 of 1-2"}
        # A public holiday changes the hours owed, not the range.
        assert thursday["holiday"] == "Founding Day" and thursday["weekend"] is False
        assert counts(thursday) == {"D": "1 of 1-1", "N": "2 of 1-2"}

    def test_short_and_over_after_a_hand_edit_of_the_roster_in_force(self, june, mona, carla, er):
        changed(mona, er, off("Emad", 1), pin("Badr", 4, "D"))

        assert counts(day(carla, "2026-06-01")) == {"D": "0 of 1-1 short", "N": "1 of 1-2"}
        answer = day(carla, "2026-06-04")
        assert counts(answer) == {"D": "2 of 1-1 over", "N": "2 of 1-2"}
        assert names(cell(answer, "D")) == [("Badr", False), ("Emad", True)]
        assert answer["totals"]["units_short"] == 0
        assert day(carla, "2026-06-01")["totals"]["units_short"] == 1

    def test_a_shift_that_also_counts_is_counted_as_the_solver_counts_it(self, june, carla, er):
        set_rules(er, headcounts=[{"shift": "D", "weekday_min": 1, "weekday_max": 1, "weekend_min": 1, "weekend_max": 1,
                                   "also_counts": ["N"]},
                                  {"shift": "N", "weekday_min": 1, "weekday_max": 2, "weekend_min": 1, "weekend_max": 2,
                                   "also_counts": []}])

        answer = day(carla, "2026-06-04")

        assert counts(answer) == {"D": "3 of 1-1 over", "N": "2 of 1-2"}
        assert names(cell(answer, "D")) == [("Emad", True)]


    def test_july_reads_its_own_roster(self, june, mona, carla, er, solver):
        approved(mona, carla, er, solver, "2026-07")
        set_rules(er, require_both_genders=True)

        answer = day(carla, "2026-07-03")

        # On Friday the 3rd of July, Emad works Day, and Badr and Dana Night.
        assert answer["weekend"] is True and counts(answer) == {"D": "1 of 1-1", "N": "2 of 1-2"}
        assert names(cell(answer, "N")) == [("Badr", False), ("Dana", False)]
        assert (cell(answer, "D")["both_genders"], cell(answer, "N")["both_genders"]) == (False, True)


class TestTeamLeaderAndGender:
    def test_met_and_not_met_where_the_unit_requires_them(self, june, carla, er):
        set_rules(er, require_team_leader=True, require_both_genders=True)

        second, nineteenth = day(carla, "2026-06-02"), day(carla, "2026-06-19")

        assert cell(second, "D")["team_leader"] is False and cell(second, "N")["team_leader"] is True
        assert cell(second, "D")["both_genders"] is False and cell(second, "N")["both_genders"] is False
        assert cell(nineteenth, "N")["both_genders"] is True and cell(nineteenth, "N")["team_leader"] is True

    def test_null_where_the_unit_requires_neither(self, june, carla):
        answer = day(carla, "2026-06-02")

        assert {(shift["team_leader"], shift["both_genders"]) for shift in row(answer)["figures"]["shifts"]} == {(None, None)}

    def test_headcount_off_gives_counts_with_no_range(self, june, mona, carla, er):
        changed(mona, er, off("Emad", 1))
        set_rules(er, headcount_enabled=False)

        assert counts(day(carla, "2026-06-01")) == {"D": "0", "N": "1"}
        assert day(carla, "2026-06-01")["totals"]["units_short"] == 0


class TestLeave:
    def test_leave_by_name_with_no_type_and_not_counted(self, june, mona, carla, er, on):
        on(dt.date(2026, 7, 2))  # June is over and not closed: its grid keeps Amal's Night on the 4th.
        assert record_leave(mona, er, "Amal", 4).status_code == 201

        response = carla.get("/api/rosters/day", {"date": "2026-06-04"})
        answer = response.json()

        assert row(answer)["state"] == "approved"
        assert row(answer)["figures"]["on_leave"] == [{"hospital_id": "Amal", "full_name": "Amal"}]
        assert counts(answer) == {"D": "1 of 1-1", "N": "1 of 1-2"}
        assert names(cell(answer, "N")) == [("Dana", False)]
        assert "Sick" not in response.content.decode() and "leave_type" not in response.content.decode()
        assert answer["totals"]["on_leave"] == 1

    def test_a_nurse_outside_her_dates_is_neither_on_duty_nor_on_leave(self, june, carla):
        NurseProfile.objects.filter(user__username="Dana").update(end_date=dt.date(2026, 6, 3))

        answer = day(carla, "2026-06-04")

        assert names(cell(answer, "N")) == [("Amal", True)]
        assert row(answer)["figures"]["on_leave"] == []

    def test_leave_a_correction_left_outside_her_dates_is_not_leave_in_a_closed_month(self, june, mona, carla, er, on):
        on(dt.date(2026, 7, 2))
        assert record_leave(mona, er, "Dana", 4).status_code == 201
        NurseProfile.objects.filter(user__username="Dana").update(end_date=dt.date(2026, 6, 3))
        making.close(er, dt.date(2026, 6, 1))

        answer = day(carla, "2026-06-04")

        assert row(answer)["state"] == "closed" and row(answer)["figures"]["on_leave"] == []
        assert names(cell(answer, "N")) == [("Amal", True)]


class TestOvertime:
    def test_each_nurses_overtime_is_my_months_summed_against_the_unit_overtime_cap(self, june, carla):
        mine = {nurse: ok(as_user(nurse).get("/api/rosters/mine/month", {"month": "2026-06"}))["roster"]["hours"]["overtime"]
                for nurse in NURSES}

        figures = row(day(carla, "2026-06-04"))["figures"]

        assert mine == {"Amal": 0, "Badr": 0, "Dana": 0, "Emad": 0}
        assert {key: figures[key] for key in ("hours_counted", "overtime", "overtime_cap", "with_overtime")} == {
            "hours_counted": True, "overtime": sum(mine.values()), "overtime_cap": 400, "with_overtime": 0}

    def test_overtime_from_a_hand_edit_matches_my_month(self, june, mona, carla, er):
        changed(mona, er, pin("Badr", 1, "N"), pin("Badr", 3, "N"))
        mine = {nurse: ok(as_user(nurse).get("/api/rosters/mine/month", {"month": "2026-06"}))["roster"]["hours"]["overtime"]
                for nurse in NURSES}

        figures = row(day(carla, "2026-06-04"))["figures"]

        # Badr works 17 shifts of 12 hours in June, 19 with these two: 228 hours against the 208 owed on 26 days not a Friday.
        assert mine == {"Amal": 0, "Badr": 20, "Dana": 0, "Emad": 0}
        assert (figures["overtime"], figures["with_overtime"]) == (20, 1)
        assert day(carla, "2026-06-04")["totals"]["overtime"] == 20

    def test_the_hours_rule_off_gives_no_overtime(self, june, mona, carla, er):
        changed(mona, er, pin("Badr", 1, "N"), pin("Badr", 3, "N"))
        hospital_rules(hours_rule_enabled=False)

        figures = row(day(carla, "2026-06-04"))["figures"]

        assert (figures["hours_counted"], figures["overtime"], figures["with_overtime"]) == (False, 0, 0)


class TestStates:
    def test_none_draft_and_submitted_have_their_state_alone_and_no_draft_cell(self, june, carla, client_of, make_user,
                                                                                solver):
        nadia = client_of("nadia", Role.UNIT_MANAGER, full_name="Nadia Manager")
        sara = client_of("sara", Role.UNIT_MANAGER, full_name="Sara Manager")
        ward = make_sample_unit("Ward B", User.objects.get(username="nadia"), make_user, prefix="b")
        icu = make_sample_unit("ICU", User.objects.get(username="sara"), make_user, prefix="i")
        make_sample_unit("Ward C", None, make_user, prefix="c")
        solved(nadia, ward, solver, prefix="b")
        solved(sara, icu, solver, prefix="i")
        ok(submit(sara, icu))

        response = carla.get("/api/rosters/day", {"date": "2026-06-04"})
        answer = response.json()

        assert [(unit["name"], unit["state"], unit["figures"]) for unit in answer["units"] if unit["name"] != "ER"] == [
            ("ICU", "submitted", None), ("Ward B", "draft", None), ("Ward C", "none", None)]
        text = response.content.decode()
        assert "bAmal" not in text and "iAmal" not in text and "cAmal" not in text
        assert [column["code"] for column in answer["shifts"]] == ["D", "N"]
        assert answer["totals"] == {"on_duty": [{"code": "D", "on_duty": 1}, {"code": "N", "on_duty": 2}], "on_leave": 0,
                                    "overtime": 0, "units_short": 0}

    def test_an_archived_unit_shows_only_with_a_roster_that_month(self, june, carla, er, make_user):
        gone = make_sample_unit("Old ward", None, make_user, prefix="o")
        gone.archived = True
        gone.save()
        er.archived, er.manager = True, None
        er.save()

        june_day, may_day = day(carla, "2026-06-04"), day(carla, "2026-05-20")

        assert [(unit["name"], unit["archived"]) for unit in june_day["units"]] == [("ER", True)]
        assert may_day["units"] == [] and may_day["shifts"] == []

    def test_an_archived_unit_with_only_a_close_that_month_shows(self, june, carla, er, on):
        on(dt.date(2026, 7, 2))
        making.close(er, dt.date(2026, 6, 1))
        er.archived, er.manager = True, None
        er.save()

        assert [(unit["name"], unit["state"]) for unit in day(carla, "2026-06-04")["units"]] == [("ER", "closed")]

    def test_today_by_default_on_the_hospitals_clock(self, june, carla, on):
        on(dt.date(2026, 6, 4))

        answer = day(carla)

        assert answer["date"] == "2026-06-04" and counts(answer) == {"D": "1 of 1-1", "N": "2 of 1-2"}

    def test_a_malformed_date_is_a_422(self, carla):
        assert carla.get("/api/rosters/day", {"date": "2026-13-01"}).status_code == 422
        assert carla.get("/api/rosters/day", {"date": "4 June"}).status_code == 422


class TestClosedMonth:
    def test_read_from_the_close_in_force_and_unchanged_by_leave_recorded_since(self, june, mona, carla, er, on):
        changed(mona, er, pin("Badr", 4, "D"))
        on(dt.date(2026, 7, 2))
        assert record_leave(mona, er, "Dana", 4).status_code == 201
        making.close(er, dt.date(2026, 6, 1))
        stored = copy.deepcopy(day(carla, "2026-06-04"))

        assert record_leave(mona, er, "Amal", 4).status_code == 201
        changed_since = day(carla, "2026-06-04")

        er_row = row(stored)
        assert er_row["state"] == "closed"
        assert counts(stored) == {"D": "2 of 1-1 over", "N": "1 of 1-2"}
        assert names(cell(stored, "N")) == [("Amal", True)]
        assert er_row["figures"]["on_leave"] == [{"hospital_id": "Dana", "full_name": "Dana"}]
        assert er_row["figures"]["overtime_cap"] == 400
        assert json.dumps(changed_since) == json.dumps(stored)

    def test_a_close_stored_with_the_solvers_old_words_and_no_fields_reads_the_same(self, june, mona, carla, er, on):
        """A close made before the solver stated a violation's shift and figures holds only its words, with days counted
        from 0: "headcount: day 3 D = 2, want 1..1"."""
        set_rules(er, require_team_leader=True, require_both_genders=True)
        changed(mona, er, off("Emad", 1), pin("Badr", 4, "D"))
        on(dt.date(2026, 7, 2))
        close = making.close(er, dt.date(2026, 6, 1))
        as_now = [copy.deepcopy(day(carla, date)) for date in ("2026-06-01", "2026-06-02", "2026-06-04")]

        for broke in close.result["facts_broke"]:
            if broke["date"]:
                broke["text"] = broke["text"].replace(broke["date"], f"day {int(broke['date'][-2:]) - 1}")
            for stated in ("shift", "present", "minimum", "maximum"):
                broke.pop(stated, None)
        close.save()
        as_stored = [day(carla, date) for date in ("2026-06-01", "2026-06-02", "2026-06-04")]

        assert {"rule": "headcount", "nurse": None, "date": "2026-06-04", "text": "headcount: day 3 D = 2, want 1..1"} \
            in close.result["facts_broke"]
        assert counts(as_stored[0])["D"] == "0 of 1-1 short" and counts(as_stored[2])["D"] == "2 of 1-1 over"
        assert cell(as_stored[1], "D")["team_leader"] is False and cell(as_stored[1], "N")["both_genders"] is False
        assert json.dumps(as_stored) == json.dumps(as_now)

    def test_a_month_over_but_not_closed_reads_as_approved(self, june, carla, on):
        on(dt.date(2026, 7, 1))

        answer = day(carla, "2026-06-04")

        assert row(answer)["state"] == "approved" and counts(answer) == {"D": "1 of 1-1", "N": "2 of 1-2"}


class TestWhoMayRead:
    @pytest.mark.parametrize("role", [Role.SYSTEM_ADMIN, Role.UNIT_MANAGER, Role.STAFF_NURSE])
    def test_only_the_cno_and_the_deputy(self, signed_in, role):
        assert signed_in(role).get("/api/rosters/day").status_code == 403

    def test_the_deputy_reads_it(self, june, dina):
        assert counts(day(dina, "2026-06-04")) == {"D": "1 of 1-1", "N": "2 of 1-2"}
