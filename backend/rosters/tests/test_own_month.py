"""A nurse's own month: their hours and shift counts from the version in force, worked out on the month as it stands. Nothing
of a colleague. The numbers below are worked by hand from `fixtures/sound_rosters.json`: in June 2026, Amal works 6 Day and 11
Night shifts of 12 hours, and owes 8 hours on each of the 26 days that are not a Friday."""
import copy
import datetime as dt

import pytest

from accounts.models import Role, User
from nurses.models import NurseProfile, Placement
from rosters.models import Roster, RosterVersion, VersionOrigin
from rosters.tests.test_review import (_scene, approve, carla, ok, record_leave, solved, submit,  # noqa: F401
                                       url)
from notifications.tests.test_events import as_user, joining
from closes.tests.test_close import amals_preferences
from conftest import post
from solves.tests.test_input import preference

pytestmark = pytest.mark.django_db

AMAL_SHIFTS = [{"code": "D", "display_name": "Day", "count": 6}, {"code": "N", "display_name": "Night", "count": 11}]


def month(client, chosen="2026-06") -> dict:
    return ok(client.get("/api/rosters/mine/month", {"month": chosen}))


def approved_june(mona, er, solver, carla):
    solved(mona, er, solver)
    ok(submit(mona, er))
    ok(approve(carla, er))


def shifts_of(hours) -> dict[str, int]:
    return {row["code"]: row["count"] for row in hours["shifts"]}


def shifts(answer) -> list[dict]:
    return [{key: row[key] for key in ("code", "display_name", "count")} for row in answer["roster"]["hours"]["shifts"]]


class TestFromTheVersionInForce:
    def test_nothing_before_approval(self, mona, er, solver):
        solved(mona, er, solver)

        answer = month(as_user("Amal"))

        assert answer["unit"] == "ER" and answer["roster"] is None

    def test_hours_and_shift_counts(self, mona, er, solver, carla):
        approved_june(mona, er, solver, carla)

        answer = month(as_user("Amal"))

        assert answer["roster"]["version"] == 1
        hours = answer["roster"]["hours"]
        assert {key: hours[key] for key in ("worked", "leave_credit", "owed", "overtime", "carried")} == {
            "worked": 204, "leave_credit": 0, "owed": 208, "overtime": 0, "carried": 4}
        assert shifts(answer) == AMAL_SHIFTS

    def test_leave_recorded_since_counts(self, mona, er, solver, carla):
        """Wednesday 10 June is a day off for Amal: as leave it is 8 hours credited, so 200 are owed and 4 are overtime."""
        approved_june(mona, er, solver, carla)
        assert record_leave(mona, er, "Amal", 10).status_code == 201

        hours = month(as_user("Amal"))["roster"]["hours"]

        assert (hours["leave_credit"], hours["owed"], hours["overtime"], hours["carried"]) == (8, 200, 4, 0)

    def test_a_later_version_is_the_one_read(self, mona, er, solver, carla):
        """An edit makes a version after the first. Wednesday 10 June as a Night makes 216 hours worked, 8 of them overtime."""
        approved_june(mona, er, solver, carla)
        roster = Roster.objects.get(unit=er)
        grid = copy.deepcopy(roster.grid)
        grid["Amal"]["2026-06-10"] = "N"
        RosterVersion.objects.create(roster=roster, number=2, grid=grid, origin=VersionOrigin.EDIT, created_by=roster.solve.requested_by)

        answer = month(as_user("Amal"))

        assert answer["roster"]["version"] == 2
        assert (answer["roster"]["hours"]["worked"], answer["roster"]["hours"]["overtime"]) == (216, 8)

    def test_leave_on_a_day_the_version_still_works_counts_once(self, mona, er, solver, carla):
        """Leave wins over a shift. Amal works Night on Tuesday 2 June: as leave it is 8 hours credited and 12 not worked, so
        192 worked against 200 owed, and 8 owed into July."""
        approved_june(mona, er, solver, carla)
        assert record_leave(mona, er, "Amal", 2).status_code == 201

        hours = month(as_user("Amal"))["roster"]["hours"]

        assert (hours["worked"], hours["leave_credit"], hours["owed"], hours["overtime"], hours["carried"]) == (192, 8, 200, 0, 8)
        assert shifts_of(hours) == {"D": 6, "N": 10}

    def test_a_manager_with_a_nurse_profile_reads_their_own(self, mona, er, solver, carla):
        """Their profile starts in July, so June's solve has no row of theirs: they read the roster without hours."""
        nurse = NurseProfile.objects.create(user=User.objects.get(username="mona"), gender="F", start_date=dt.date(2026, 7, 1))
        Placement.objects.create(nurse=nurse, unit=er, from_month=dt.date(2026, 1, 1))
        approved_june(mona, er, solver, carla)

        answer = month(mona)

        assert answer["unit"] == "ER" and answer["roster"]["version"] == 1 and answer["roster"]["hours"] is None

class TestNothingOfAColleague:
    def test_a_colleagues_leave_changes_nothing_in_the_answer(self, mona, er, solver, carla):
        approved_june(mona, er, solver, carla)
        amal = as_user("Amal")
        before = month(amal)

        assert record_leave(mona, er, "Badr", 3).status_code == 201

        assert month(amal) == before

    def test_no_colleague_is_named(self, mona, er, solver, carla):
        approved_june(mona, er, solver, carla)

        text = as_user("Amal").get("/api/rosters/mine/month", {"month": "2026-06"}).content.decode()

        assert not any(name in text for name in ("Badr", "Dana", "Emad"))


class TestWhichMonths:
    def test_a_month_already_over_shows_no_hours(self, mona, er, solver, carla, monkeypatch):
        approved_june(mona, er, solver, carla)
        from nurses import membership
        monkeypatch.setattr(membership, "today", lambda: __import__("datetime").date(2026, 7, 2))

        answer = month(as_user("Amal"))

        assert answer["first_month"] == "2026-07" and answer["roster"] is None

    def test_a_nurse_due_to_join_sees_the_roster_without_hours(self, mona, er, solver, carla, make_user):
        joining(er, make_user, "Faris")
        approved_june(mona, er, solver, carla)

        answer = month(as_user("Faris"))

        assert answer["roster"]["version"] == 1 and answer["roster"]["hours"] is None

    def test_a_manager_with_no_nurse_profile_has_no_unit(self, mona, er, solver, carla):
        approved_june(mona, er, solver, carla)

        assert month(mona) == {"month": "2026-06", "first_month": "2026-05", "unit": None, "roster": None,
                               "probation": None, "precepts": None}

    @pytest.mark.parametrize("role", [Role.CNO, Role.DEPUTY_CNO, Role.SYSTEM_ADMIN])
    def test_other_roles_are_refused(self, role, client_of):
        assert client_of("someone", role).get("/api/rosters/mine/month").status_code == 403


# What the roster in force kept of her preferences. Amal's five June preferences are `amals_preferences`'s, counted by
# hand on SOUND's June ("NNNN.NNN...NN.....NN..DDDD..DD"): a day off on the 2nd, which she works, and on the 5th and 10th, which
# she has; a working day on the 15th, which she has off, and on the 23rd, which she works. 3 of 5 kept.
JUNE_OUTCOMES = {"2026-06-02": "not_kept", "2026-06-05": "kept", "2026-06-10": "kept", "2026-06-15": "not_kept",
                 "2026-06-23": "kept"}


def preferences(client, chosen="2026-06") -> dict:
    return ok(client.get("/api/preferences/mine", {"month": chosen}))


def outcomes(answer) -> dict[str, str | None]:
    return {row["date"]: row["outcome"] for row in answer["preferences"]}


def counted(answer) -> tuple:
    return answer["kept"], answer["keepable"]


class TestWhatWasKept:
    def test_nothing_is_kept_or_not_before_approval(self, mona, er, solver):
        amals_preferences(er)
        solved(mona, er, solver)

        answer = preferences(as_user("Amal"))

        assert set(outcomes(answer).values()) == {None} and len(answer["preferences"]) == 5
        assert counted(answer) == (None, None)

    def test_each_is_kept_or_not_by_the_roster_in_force(self, mona, er, solver, carla):
        amals_preferences(er)
        approved_june(mona, er, solver, carla)

        answer = preferences(as_user("Amal"))

        assert outcomes(answer) == JUNE_OUTCOMES
        assert counted(answer) == (3, 5)

    def test_what_she_reads_is_what_the_month_after_is_priced_on(self, mona, er, solver, carla):
        """One computation: July's solve input carries as not kept the two My month says were not."""
        from solves.input import assemble
        amals_preferences(er)
        approved_june(mona, er, solver, carla)

        kept, keepable = counted(preferences(as_user("Amal")))

        july = next(row for row in assemble(er, dt.date(2026, 7, 1)).nurses if row["id"] == "Amal")
        assert keepable - kept == july["preferences_not_kept"] == 2

    def test_a_day_on_leave_is_counted_neither_way(self, mona, er, solver, carla):
        amals_preferences(er)
        approved_june(mona, er, solver, carla)
        assert record_leave(mona, er, "Amal", 2).status_code == 201

        answer = preferences(as_user("Amal"))

        assert outcomes(answer) == JUNE_OUTCOMES | {"2026-06-02": "on_leave"}
        assert counted(answer) == (3, 4)

    def test_a_day_an_expired_required_skill_bars_is_counted_neither_way_and_not_carried(self, mona, er, solver, carla):
        """No roster can let her work it, so it is no preference not kept."""
        from rosters.tests.test_review import holding
        from solves.input import assemble
        amals_preferences(er)
        approved_june(mona, er, solver, carla)
        holding("Amal", "BLS", dt.date(2026, 6, 12), required=True)

        answer = preferences(as_user("Amal"))

        assert outcomes(answer) == JUNE_OUTCOMES | {"2026-06-15": "barred", "2026-06-23": "barred"}
        assert counted(answer) == (2, 3)
        july = next(row for row in assemble(er, dt.date(2026, 7, 1)).nurses if row["id"] == "Amal")
        assert july["preferences_not_kept"] == 1

    @pytest.mark.parametrize("origin", [VersionOrigin.EDIT, VersionOrigin.REPLAN])
    def test_a_later_version_that_breaks_one_counts_it_as_not_kept(self, origin, mona, er, solver, carla):
        """Version 2, a manager's edit or a re-plan's, puts Amal on Night on the 10th, a day she asked off: the roster in force
        however it came about."""
        amals_preferences(er)
        approved_june(mona, er, solver, carla)
        roster = Roster.objects.get(unit=er)
        grid = copy.deepcopy(roster.grid)
        grid["Amal"]["2026-06-10"] = "N"
        RosterVersion.objects.create(roster=roster, number=2, grid=grid, origin=origin, created_by=roster.solve.requested_by)

        answer = preferences(as_user("Amal"))

        assert outcomes(answer) == JUNE_OUTCOMES | {"2026-06-10": "not_kept"}
        assert counted(answer) == (2, 5)

    def test_one_withdrawn_has_no_outcome_and_counts_neither_way(self, mona, er, solver, carla):
        """A withdrawn one on the 3rd, and one on the 5th withdrawn before the day off made again there."""
        from requesting.models import PreferenceState
        preference(er, "Amal", dt.date(2026, 6, 3), state=PreferenceState.WITHDRAWN)
        preference(er, "Amal", dt.date(2026, 6, 5), state=PreferenceState.WITHDRAWN)
        amals_preferences(er)
        approved_june(mona, er, solver, carla)

        answer = preferences(as_user("Amal"))

        assert [(row["date"], row["state"], row["outcome"]) for row in answer["preferences"]][:3] == [
            ("2026-06-02", "made", "not_kept"), ("2026-06-03", "withdrawn", None), ("2026-06-05", "withdrawn", None)]
        assert ("2026-06-05", "made", "kept") in [(row["date"], row["state"], row["outcome"]) for row in answer["preferences"]]
        assert counted(answer) == (3, 5)

    def test_a_closed_month_reads_the_close_in_force(self, mona, er, solver, carla, monkeypatch):
        """Leave on the 2nd recorded before the close is in its facts; leave on the 10th recorded after it is not, until the
        month is closed again."""
        from closes import making
        from nurses import membership
        amals_preferences(er)
        approved_june(mona, er, solver, carla)
        monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 7, 2))
        assert record_leave(mona, er, "Amal", 2).status_code == 201
        making.catch_up()
        assert record_leave(mona, er, "Amal", 10).status_code == 201

        closed = preferences(as_user("Amal"))
        assert outcomes(closed) == JUNE_OUTCOMES | {"2026-06-02": "on_leave"}
        assert counted(closed) == (3, 4)

        ok(post(mona, f"/api/units/{er.pk}/close", {"month": "2026-06"}))

        again = preferences(as_user("Amal"))
        assert outcomes(again) == JUNE_OUTCOMES | {"2026-06-02": "on_leave", "2026-06-10": "on_leave"}
        assert counted(again) == (2, 3)


class TestCountingDouble:
    def test_the_month_after_an_unkept_one_counts_double_and_says_which(self, mona, er, solver, carla):
        amals_preferences(er)
        approved_june(mona, er, solver, carla)

        assert preferences(as_user("Amal"), "2026-07")["doubled_by"] == "2026-06"
        assert preferences(as_user("Amal"))["doubled_by"] is None
        assert preferences(as_user("Badr"), "2026-07")["doubled_by"] is None

    def test_it_is_read_from_the_chain_the_solve_input_takes(self, mona, er, solver, carla, monkeypatch):
        """A close with late leave on both unkept days leaves nothing to double, in July's input and in her read alike."""
        from closes import making
        from nurses import membership
        from solves.input import assemble
        amals_preferences(er)
        approved_june(mona, er, solver, carla)
        monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 7, 2))
        for day in (2, 15):
            assert record_leave(mona, er, "Amal", day).status_code == 201
        making.catch_up()

        july = next(row for row in assemble(er, dt.date(2026, 7, 1)).nurses if row["id"] == "Amal")
        assert july["preferences_not_kept"] == 0
        assert preferences(as_user("Amal"), "2026-07")["doubled_by"] is None


class TestNothingOfAColleaguesPreferences:
    def test_a_colleagues_preferences_change_nothing_and_none_is_named(self, mona, er, solver, carla):
        amals_preferences(er)
        approved_june(mona, er, solver, carla)
        before = as_user("Amal").get("/api/preferences/mine", {"month": "2026-06"}).content.decode()

        preference(er, "Badr", dt.date(2026, 6, 2))

        after = as_user("Amal").get("/api/preferences/mine", {"month": "2026-06"}).content.decode()
        assert after == before
        assert not any(name in after for name in ("Badr", "Dana", "Emad"))
