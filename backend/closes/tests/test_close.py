"""Month close: the roster in force, with every recorded leave and pin, made fact by the solver package's own code with no
search, and each nurse's carry-over stored. The API and the close's own functions on the four-nurse sample's sound June and July
(`rosters/tests/fixtures/sound_rosters.json`), with the scripted solver for the solves that make them. What a close stores is held
against the solver package's own reading of the same input, so the facts are the solver's and not this code's idea of them."""
from __future__ import annotations

import copy
import datetime as dt

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from solver_service.grid import month_facts, nurses_of

from accounts.models import Role
from audit.models import AuditEntry
from closes import making
from closes.chain import CARRIED, CARRIED_SINCE, NEW_YEAR, NO_HISTORY, Carried, Chain, month_before, next_month, opening
from closes.models import CarryOver, CarrySource, MonthClose
from conftest import post
from hospital.models import HospitalSettings, Shift
from notifications.models import Event, Notification
from nurses import membership
from nurses.models import NurseProfile
from ops import solver_client
from requesting.models import PreferenceKind
from rosters.models import PinnedCell, Roster, RosterState
from rosters.tests.test_pins import change_shift
from rosters.tests.test_review import (SOUND, approve, carla, nurses_of_er, ok, record_leave, refused, solved,  # noqa: F401
                                      submit, url)
from solves.input import assemble
from solves.tests.conftest import MONTH, nurse_in
from solves.tests.test_input import full_runs_capped, preference

pytestmark = pytest.mark.django_db

JUNE, JULY, AUGUST = dt.date(2026, 6, 1), dt.date(2026, 7, 1), dt.date(2026, 8, 1)
IN_FORCE = SOUND[MONTH]["roster"]


@pytest.fixture(autouse=True)
def _scene(before_june, solver):
    pass


@pytest.fixture
def queries_counted(django_assert_max_num_queries):
    """A test counting queries, which `every_batched_read_is_the_one_at_a_time_read` leaves unchecked: the reference's own
    reads would be counted with the batch's."""
    return django_assert_max_num_queries


@pytest.fixture(autouse=True)
def every_batched_read_is_the_one_at_a_time_read(request, monkeypatch):
    """Every batched read of the chain in these tests, of any month a test or the months before it ask, answers for
    each nurse exactly what the chain read one nurse at a time answered (`one_at_a_time`), source, month and year included, on
    a reading of its own. Inside the reference, the months a projection steps through are read one nurse at a time too."""
    if "queries_counted" in request.fixturenames:
        return
    batched, checking = Chain.carry_ins, []

    def checked(self, nurses, month):
        nurses = list(nurses)
        if checking:
            return {nurse.user.username: one_at_a_time(self, nurse, month) for nurse in nurses}
        answer = batched(self, nurses, month)
        checking.append(True)
        try:
            reference = Chain(project=self.project)
            for nurse in nurses:
                assert answer[nurse.user.username] == one_at_a_time(reference, nurse, month), (nurse.user.username, month)
        finally:
            checking.pop()
        return answer
    monkeypatch.setattr(Chain, "carry_ins", checked)


def at(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return timezone.make_aware(dt.datetime(day.year, day.month, day.day, hour, minute))


@pytest.fixture
def on(monkeypatch):
    """Moves the hospital's clock to a day, which every month's question reads."""
    def move_to(day: dt.date):
        monkeypatch.setattr(membership, "today", lambda: day)
    return move_to


def approved(mona, carla, er, solver, month=MONTH):  # noqa: F811
    solved(mona, er, solver, month)
    ok(submit(mona, er, month))
    ok(approve(carla, er, month))


@pytest.fixture
def june(mona, carla, er, solver):  # noqa: F811
    approved(mona, carla, er, solver)


@pytest.fixture
def june_and_july(mona, carla, er, solver):  # noqa: F811
    approved(mona, carla, er, solver)
    approved(mona, carla, er, solver, "2026-07")


def closed(unit, month=JUNE) -> MonthClose:
    return MonthClose.objects.filter(unit=unit, month=month).first()


def carried(close: MonthClose) -> dict[str, dict]:
    return {row.nurse.user.username: row.values for row in close.carry_overs.select_related("nurse__user")}


def close_view(client, unit, month="2026-06") -> dict:
    return ok(client.get(url(unit, "/close"), {"month": month}))


def close_again(client, unit, month="2026-06"):
    return post(client, url(unit, "/close"), {"month": month})


def closing_due(now):
    return making.due(now)


def amals_preferences(unit) -> None:
    """Five of Amal's June preferences, counted by hand on SOUND's June ("NNNN.NNN...NN.....NN..DDDD..DD"): a day off on the 2nd,
    which she works, a day off on the 5th and the 10th, which she has, a working day on the 15th, which she has off, and on the
    23rd, which she works. Two are not kept: the 2nd and the 15th."""
    for day, kind in ((2, PreferenceKind.DAY_OFF), (5, PreferenceKind.DAY_OFF), (10, PreferenceKind.DAY_OFF),
                      (15, PreferenceKind.WORKING_DAY), (23, PreferenceKind.WORKING_DAY)):
        preference(unit, "Amal", dt.date(2026, 6, day), kind)


def not_kept(unit, month, hospital_id="Amal") -> int:
    """What the month's solve input hands the solver for her."""
    return next(row for row in assemble(unit, month).nurses if row["id"] == hospital_id)["preferences_not_kept"]


class TestTheSchedule:
    def test_it_closes_an_approved_month_once_its_close_time_has_passed(self, june, er, on):
        on(dt.date(2026, 7, 1))
        assert making.due(at(dt.date(2026, 7, 1), 0, 29)) == []

        [close] = making.catch_up(at(dt.date(2026, 7, 1), 0, 30))

        assert (close.unit, close.month, close.closed_by, close.version) == (er, JUNE, None, 1)
        assert close.result["facts"] == IN_FORCE
        # What it stored is the solver package's own reading of the month with every day recorded.
        assert close.result["nurses"] == nurses_of(close.input, month_facts(close.input))
        assert carried(close) == {nurse: row["carry_over"] for nurse, row in close.result["nurses"].items()}
        assert {row.source for row in close.carry_overs.all()} == {CarrySource.CLOSED}
        assert making.due(at(dt.date(2026, 7, 1), 1, 30)) == []
        assert making.catch_up(at(dt.date(2026, 7, 1), 1, 30)) == []

    def test_the_manager_is_told_and_the_log_says_only_which_version(self, june, er, on):
        on(dt.date(2026, 7, 1))
        [close] = making.catch_up(at(dt.date(2026, 7, 1), 1))

        [note] = Notification.objects.filter(event=Event.MONTH_CLOSED)
        assert note.user.username == "mona"
        assert note.payload == {"unit_id": str(er.pk), "month": "2026-06", "close_id": str(close.pk)}
        [entry] = AuditEntry.objects.filter(action="month.closed")
        assert (entry.actor, entry.target, entry.before, entry.after) == (None, "ER, 2026-06", None, {"version": 1, "again": False})

    def test_a_missed_close_is_due_all_day_after_the_first(self, june, er, on):
        on(dt.date(2026, 7, 2))
        assert closing_due(at(dt.date(2026, 7, 2), 0, 5)) == [(er, JUNE)]

    def test_a_unit_with_no_approved_roster_is_skipped(self, mona, er, solver, on):
        solved(mona, er, solver)
        ok(submit(mona, er))
        on(dt.date(2026, 7, 2))

        assert making.catch_up(at(dt.date(2026, 7, 2), 9)) == []
        assert not MonthClose.objects.exists()

    def test_a_missed_close_is_caught_up_oldest_month_first(self, june_and_july, er, on):
        on(dt.date(2026, 8, 3))

        closes = making.catch_up(at(dt.date(2026, 8, 3), 9))

        assert [close.month for close in closes] == [JUNE, JULY]
        june, july = closes
        # July's close starts from June's closed carry-over, never a projection.
        starts = {row["id"]: {key: row[key] for key in CARRIED} for row in july.input["nurses"]}
        assert starts == carried(june)

    def test_it_locks_as_an_approval_does(self, june, er, on):
        """The catalogue's row, the month's nurses and their users `FOR NO KEY UPDATE`, the unit's, then the roster's."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        on(dt.date(2026, 7, 1))

        with CaptureQueriesContext(connection) as queries:
            making.catch_up(at(dt.date(2026, 7, 1), 1))

        locks = [query["sql"] for query in queries.captured_queries if " FOR " in query["sql"]]
        tables = [next(table for table in ("hospital_hospitalsettings", "nurses_nurseprofile", "units_unit", "rosters_roster",
                                           "requesting_request") if f'FROM "{table}"' in sql) for sql in locks]
        assert tables[:4] == ["hospital_hospitalsettings", "nurses_nurseprofile", "units_unit", "rosters_roster"]
        nurses = locks[1]
        assert "FOR NO KEY UPDATE OF" in nurses and '"nurses_nurseprofile"' in nurses.split("FOR NO KEY UPDATE OF")[1]
        assert '"accounts_user"' in nurses.split("FOR NO KEY UPDATE OF")[1]

    def test_it_stays_out_of_the_system_admins_solve_history(self, june, er, on, client_of):
        admin = client_of("sam", Role.SYSTEM_ADMIN)
        before = ok(admin.get("/api/solve-history"))
        on(dt.date(2026, 7, 1))

        making.catch_up(at(dt.date(2026, 7, 1), 1))

        assert ok(admin.get("/api/solve-history")) == before

    def test_it_never_asks_the_solver_service(self, june, er, on, monkeypatch):
        def refuse_to_solve(solve_input):
            raise AssertionError("A close never calls POST /solve")
        monkeypatch.setattr(solver_client, "start_solve", refuse_to_solve)
        monkeypatch.setattr(solver_client, "precheck", refuse_to_solve)
        on(dt.date(2026, 7, 1))

        assert [close.month for close in making.catch_up(at(dt.date(2026, 7, 1), 1))] == [JUNE]


class TestTheFacts:
    def test_leave_recorded_late_wins_over_the_roster_in_force(self, june, mona, er, on):
        on(dt.date(2026, 7, 1))
        assert IN_FORCE["Amal"]["2026-06-02"] == "N"
        assert record_leave(mona, er, "Amal", 2).status_code == 201
        assert Roster.objects.get(unit=er).grid["Amal"]["2026-06-02"] == "N"  # A month that is over makes no version.

        [close] = making.catch_up(at(dt.date(2026, 7, 1), 1))

        assert close.result["facts"]["Amal"]["2026-06-02"] == ""
        assert close.result["nurses"]["Amal"]["leave"] == ["2026-06-02"]

    def test_the_pins_that_count_are_facts_and_a_pin_set_aside_is_none(self, june, mona, er, on):
        assert IN_FORCE["Badr"]["2026-06-03"] == "" and IN_FORCE["Amal"]["2026-06-03"] == "N"
        badr, amal = (NurseProfile.objects.get(user__username=name) for name in ("Badr", "Amal"))
        user = badr.user
        PinnedCell.objects.create(unit=er, nurse=badr, date=dt.date(2026, 6, 3), shift=Shift.objects.get(code="D"), pinned_by=user)
        PinnedCell.objects.create(unit=er, nurse=amal, date=dt.date(2026, 6, 3), shift=Shift.objects.get(code="D"), pinned_by=user)
        on(dt.date(2026, 7, 1))
        record_leave(mona, er, "Amal", 3)  # Leave wins over a pin, which it ends: one left behind would be set aside.
        PinnedCell.objects.create(unit=er, nurse=amal, date=dt.date(2026, 6, 3), shift=Shift.objects.get(code="D"), pinned_by=user)

        [close] = making.catch_up(at(dt.date(2026, 7, 1), 1))

        assert close.result["facts"]["Badr"]["2026-06-03"] == "D"
        assert close.result["facts"]["Amal"]["2026-06-03"] == ""

    def test_days_outside_a_nurses_dates_count_like_leave(self, june, er, on):
        NurseProfile.objects.filter(user__username="Dana").update(end_date=dt.date(2026, 6, 20))
        on(dt.date(2026, 7, 1))

        [close] = making.catch_up(at(dt.date(2026, 7, 1), 1))

        dana = close.result["facts"]["Dana"]
        assert all(dana[f"2026-06-{day}"] == "" for day in range(21, 31))
        assert close.result["nurses"]["Dana"]["leave"] == [f"2026-06-{day}" for day in range(21, 31)]

    def test_a_shift_renamed_since_is_read_under_its_code_now(self, june, er, on, carla):  # noqa: F811
        on(dt.date(2026, 7, 1))
        assert change_shift(carla, "N", code="NT").status_code == 200

        [close] = making.catch_up(at(dt.date(2026, 7, 1), 1))

        assert close.result["facts"]["Amal"]["2026-06-01"] == "NT"
        assert "NT" in close.input["department"]["shifts"]

    def test_the_close_input_carries_the_units_full_run_cap_and_the_hospitals_weight(self, june, er, on):
        """As a solve's does, so that the close prices the month as the solver would."""
        full_runs_capped(er, 2, 20)
        on(dt.date(2026, 7, 1))

        [close] = making.catch_up(at(dt.date(2026, 7, 1), 1))

        assert close.input["department"]["max_full_runs"] == 2
        assert close.input["general"]["weights"]["excess_full_run"] == 20

    def test_the_close_input_carries_the_units_overtime_share_and_the_hospitals_weight(self, june, er, on):
        """As a solve's does, so that the close moves each balance as the solver would."""
        er.rules |= {"share_overtime": False, "overtime_tolerance": 4, "overtime_carry_cap": 9}
        er.save()
        settings = HospitalSettings.load()
        settings.rules["weights"]["overtime_above_share"] = 0
        settings.save()
        on(dt.date(2026, 7, 1))

        [close] = making.catch_up(at(dt.date(2026, 7, 1), 1))

        assert {key: close.input["department"][key] for key in ("share_overtime", "overtime_tolerance", "overtime_carry_cap")} \
            == {"share_overtime": False, "overtime_tolerance": 4, "overtime_carry_cap": 9}
        assert close.input["general"]["weights"]["overtime_above_share"] == 0

    def test_a_shift_the_unit_dropped_after_its_month_counts_as_worked(self, june, er, on):
        """The close does not refuse, and keeps the past roster as it stands."""
        roster = Roster.objects.get(unit=er)
        grid = copy.deepcopy(roster.grid)
        grid["Emad"]["2026-06-02"] = "A"  # As if the unit had worked Morning in June and a manager put Emad on it.
        roster.grid = grid
        roster.save()
        roster.versions.update(grid=grid)
        on(dt.date(2026, 7, 1))
        er.refresh_from_db()
        assert "A" not in er.rules["shifts"]
        Shift.objects.filter(code="A").update(active=False)

        [close] = making.catch_up(at(dt.date(2026, 7, 1), 1))

        assert close.result["facts"]["Emad"]["2026-06-02"] == "A"
        assert close.result["nurses"]["Emad"]["shifts"]["A"] == 1
        assert close.input["general"]["shift_hours"]["A"] == 8


class TestClosingAgain:
    def test_late_leave_then_closing_again_keeps_the_first_close_as_it_was(self, june, mona, er, on):
        on(dt.date(2026, 7, 1))
        [first] = making.catch_up(at(dt.date(2026, 7, 1), 1))
        stored = copy.deepcopy(first.result)
        assert close_view(mona, er)["closed"]["changed_since"] is False
        assert ok(mona.get(url(er, "/leave"), {"month": "2026-06"}))["closed"] == "closed"

        assert record_leave(mona, er, "Amal", 2).status_code == 201
        assert close_view(mona, er)["closed"]["changed_since"] is True

        answer = ok(close_again(mona, er))

        assert answer["closed"]["closed_by"] == "Mona Ali" and answer["closed"]["changed_since"] is False
        latest = closed(er)
        assert latest.pk != first.pk and latest.result["facts"]["Amal"]["2026-06-02"] == ""
        first.refresh_from_db()
        assert first.result == stored
        entry = AuditEntry.objects.filter(action="month.closed").order_by("-at").first()
        assert entry.after == {"version": 1, "again": True} and entry.actor.username == "mona"
        assert not Notification.objects.filter(event=Event.MONTH_CLOSED, payload__close_id=str(latest.pk)).exists()

    def test_it_is_refused_once_the_month_after_has_closed(self, june_and_july, mona, er, on):
        on(dt.date(2026, 8, 3))
        making.catch_up(at(dt.date(2026, 8, 3), 9))

        assert refused(close_again(mona, er)) == "July 2026 has closed, so June 2026 can no longer be closed again."
        assert close_view(mona, er)["close_refusal"] == "July 2026 has closed, so June 2026 can no longer be closed again."
        assert ok(close_again(mona, er, "2026-07"))["closed"]["closed_by"] == "Mona Ali"
        leave = record_leave(mona, er, "Amal", 2)
        assert leave.status_code == 422
        assert leave.json()["detail"][0]["msg"] == ("June 2026 has closed, and so has the month after it, so its leave can no "
                                                    "longer change.")
        assert ok(mona.get(url(er, "/leave"), {"month": "2026-06"}))["closed"] == "fixed"

    def test_a_roster_left_submitted_past_its_month_cannot_be_approved_and_block_the_closes_after_it(self, mona, carla, er,
                                                                                                      solver, on):
        """The audit's scenario (2026-11-11, section 4), a month on: June left submitted, July approved and closed, then June's
        approval refused, and August's close not blocked by June."""
        from hospital.models import FixedRamadanMonth
        from rosters.tests.test_review import LATE
        for month in ("2026-07", "2026-08"):
            FixedRamadanMonth.objects.get_or_create(month=month, defaults={"dates": []})
        solved(mona, er, solver)
        ok(submit(mona, er))
        approved(mona, carla, er, solver, "2026-07")
        approved(mona, carla, er, solver, "2026-08")
        on(dt.date(2026, 8, 3))
        assert [close.month for close in making.catch_up(at(dt.date(2026, 8, 3), 9))] == [JULY]

        assert refused(approve(carla, er)) == LATE
        assert Roster.objects.get(unit=er, month=JUNE).state == "submitted"

        on(dt.date(2026, 9, 1))
        assert [close.month for close in making.catch_up(at(dt.date(2026, 9, 1), 1))] == [AUGUST]

    def test_a_month_not_over_or_with_a_draft_cannot_close(self, june, mona, er, on, solver):
        assert refused(close_again(mona, er)) == "June 2026 is not over yet, so it cannot close."
        solved(mona, er, solver, "2026-07")
        on(dt.date(2026, 8, 3))
        assert refused(close_again(mona, er, "2026-07")) == ("July 2026 has no approved roster, so there is nothing to close. The "
                                                            "month after starts from the last closed carry-over.")

    def test_only_its_manager_closes_it_again(self, june, er, carla, on):  # noqa: F811
        on(dt.date(2026, 7, 2))
        assert close_again(carla, er).status_code == 403
        assert close_view(carla, er)["close_refusal"] == "Only the unit's manager closes its month again."


class TestTheChain:
    def test_the_next_month_starts_from_the_projected_carry_over_with_its_history(self, june, er):
        july = assemble(er, JULY)
        roster = Roster.objects.get(unit=er)

        projected = Chain().carry_out(roster)

        assert {row["id"]: {key: row[key] for key in CARRIED} for row in july.nurses} == projected
        assert {carry.source for carry in july.carried.values()} == {CarrySource.PROJECTED}
        assert projected["Amal"]["previous"][-2:] == ["D", "D"]  # June ends with two Days for Amal.

    def test_once_closed_the_next_month_starts_from_the_closed_carry_over(self, june, er, on):
        on(dt.date(2026, 7, 1))
        [close] = making.catch_up(at(dt.date(2026, 7, 1), 1))

        july = assemble(er, JULY)

        assert {row["id"]: {key: row[key] for key in CARRIED} for row in july.nurses} == carried(close)
        assert {carry.source for carry in july.carried.values()} == {CarrySource.CLOSED}

    def test_a_close_stored_before_the_count_existed_hands_on_zero(self, june, er, on):
        on(dt.date(2026, 7, 1))
        [close] = making.catch_up(at(dt.date(2026, 7, 1), 1))
        for row in close.carry_overs.all():                   # as a close made before the count was stored
            row.values = {key: value for key, value in row.values.items() if key != "preferences_not_kept"}
            row.save(update_fields=["values"])

        july = assemble(er, JULY)

        assert {row["preferences_not_kept"] for row in july.nurses} == {0}
        assert {carry.source for carry in july.carried.values()} == {CarrySource.CLOSED}

    def test_a_later_draft_solved_before_the_close_is_warned_of(self, june, mona, er, solver, on):
        solved(mona, er, solver, "2026-07")
        on(dt.date(2026, 7, 1))
        draft = ok(mona.get(url(er, "/roster"), {"month": "2026-07"}))["draft"]
        assert not [change for change in draft["changes"] if change["what"] == "closed"]

        making.catch_up(at(dt.date(2026, 7, 1), 1))

        draft = ok(mona.get(url(er, "/roster"), {"month": "2026-07"}))["draft"]
        assert {change["what"] for change in draft["changes"]} == {"closed"}
        assert draft["check"] is not None  # Checked again on the month as it stands, as at submit.

    def test_the_carry_over_into_january_resets_overtime_and_the_three_balances(self, er):
        """The year boundary is applied as January's input is built, never inside a close: December's row keeps
        what December handed on."""
        amal = NurseProfile.objects.get(user__username="Amal")
        close = MonthClose.objects.create(unit=er, month=dt.date(2026, 12, 1), version=1, input={}, result={})
        values = {"ytd_overtime": 40, "hours_balance": 6, "night_balance": 1.5, "friday_balance": -0.5, "previous": ["N", ""],
                  "last_shift_kind": "night", "rest_owed": 1, "preferences_not_kept": 1,   # January keeps the count
                  "overtime_balance": 9.5}
        CarryOver.objects.create(nurse=amal, unit=er, month=close.month, source=CarrySource.CLOSED, close=close, values=values)

        row = next(row for row in assemble(er, dt.date(2027, 1, 1)).nurses if row["id"] == "Amal")

        assert {key: row[key] for key in CARRIED} == values | {"ytd_overtime": 0, "night_balance": 0.0, "friday_balance": 0.0,
                                                               "overtime_balance": 0.0}
        assert CarryOver.objects.get(nurse=amal, month=close.month).values["overtime_balance"] == 9.5

    def test_a_closed_months_overtime_balance_reaches_the_next_months_input(self, er):
        """Passed through as is: the solver hands it on unchanged until a unit shares its overtime."""
        amal = NurseProfile.objects.get(user__username="Amal")
        close = MonthClose.objects.create(unit=er, month=dt.date(2026, 10, 1), version=1, input={}, result={})
        values = {"ytd_overtime": 40, "hours_balance": 6, "night_balance": 1.5, "friday_balance": -0.5, "previous": [],
                  "last_shift_kind": None, "rest_owed": 0, "preferences_not_kept": 0, "overtime_balance": -7.25}
        CarryOver.objects.create(nurse=amal, unit=er, month=close.month, source=CarrySource.CLOSED, close=close, values=values)

        row = next(row for row in assemble(er, dt.date(2026, 11, 1)).nurses if row["id"] == "Amal")

        assert row["overtime_balance"] == -7.25

    def test_opening_values_entered_the_year_before_reset_in_january(self, er):
        """A nurse added in December whose first month in this application is January: her opening values were last year's."""
        from nurses.models import Placement
        amal = NurseProfile.objects.get(user__username="Amal")
        NurseProfile.objects.filter(pk=amal.pk).update(ytd_overtime=30, night_balance=2.0, hours_balance=5, overtime_balance=7.5)
        Placement.objects.filter(nurse=amal).update(from_month=dt.date(2026, 12, 1))

        row = next(row for row in assemble(er, dt.date(2027, 1, 1)).nurses if row["id"] == "Amal")
        december = next(row for row in assemble(er, dt.date(2026, 12, 1)).nurses if row["id"] == "Amal")

        assert (row["ytd_overtime"], row["night_balance"], row["hours_balance"], row["overtime_balance"]) == (0, 0.0, 5, 0.0)
        assert (december["ytd_overtime"], december["night_balance"], december["overtime_balance"]) == (30, 2.0, 7.5)

    def test_a_transferred_nurse_carries_hers_into_the_new_unit(self, june, er, on, client_of, make_user):
        from solves.tests.conftest import make_sample_unit
        client_of("omar", Role.UNIT_MANAGER)
        from accounts.models import User
        icu = make_sample_unit("ICU", User.objects.get(username="omar"), make_user, prefix="i")
        amal = NurseProfile.objects.get(user__username="Amal")
        membership.transfer(amal, icu, JULY)
        on(dt.date(2026, 7, 1))
        [close] = making.catch_up(at(dt.date(2026, 7, 1), 1))

        july = assemble(icu, JULY)

        row = next(row for row in july.nurses if row["id"] == "Amal")
        assert {key: row[key] for key in CARRIED} == carried(close)["Amal"]
        assert "Amal" not in {row["id"] for row in assemble(er, JULY).nurses}

    def test_a_history_naming_a_shift_renamed_since_maps_it(self, june, er, on, carla):  # noqa: F811
        on(dt.date(2026, 7, 1))
        making.catch_up(at(dt.date(2026, 7, 1), 1))
        assert change_shift(carla, "N", code="NT").status_code == 200
        er.refresh_from_db()

        july = assemble(er, JULY).input

        assert july["renamed_shifts"] == {"N": "NT"}
        assert any("N" in row["previous"] for row in july["nurses"])
        from nurse_roster.request import load_request
        loaded = load_request(july)
        assert all("N" not in nurse.previous for nurse in loaded.problem.nurses)


class TestAFarMonth:
    """A month far ahead starts from what the last approved roster handed on, carried through months with no roster, and the
    work of asking for it does not grow with how far ahead it is: once no approved roster lies before the month asked, each month
    only hands on what came in, so the chain does not step through them one by one."""

    def test_it_starts_from_the_last_projection_with_no_history_and_the_january_reset(self, june, er):
        amal = NurseProfile.objects.get(user__username="Amal")
        projected = Chain().carry_out(Roster.objects.get(unit=er))["Amal"]

        far = Chain().carry_in(amal, dt.date(2076, 12, 1))
        near = Chain().carry_in(amal, AUGUST)

        assert (far.source, far.out_of, far.year) == (CarrySource.PROJECTED, dt.date(2076, 11, 1), 2076)
        assert far.into(dt.date(2076, 12, 1)) == {key: projected[key] for key in CARRIED} | NO_HISTORY | NEW_YEAR
        assert (near.source, near.out_of) == (CarrySource.PROJECTED, JULY)
        assert near.into(AUGUST) == {key: projected[key] for key in CARRIED} | NO_HISTORY

    def test_from_a_closed_month_too(self, june, er, on):
        on(dt.date(2026, 7, 1))
        [close] = making.catch_up(at(dt.date(2026, 7, 1), 1))
        amal = NurseProfile.objects.get(user__username="Amal")

        far = Chain().carry_in(amal, dt.date(2076, 12, 1))

        assert (far.source, far.out_of) == (CarrySource.CLOSED, dt.date(2076, 11, 1))
        assert far.into(dt.date(2076, 12, 1)) == carried(close)["Amal"] | NO_HISTORY | NEW_YEAR

    @pytest.mark.parametrize("closed_june", [False, True])
    def test_a_nurses_and_a_managers_reads_cost_the_same_for_any_month_ahead(self, june, er, mona, on, nurses_of_er,
                                                                             closed_june):
        if closed_june:
            on(dt.date(2026, 7, 1))
            making.catch_up(at(dt.date(2026, 7, 1), 1))
        amal = nurses_of_er("Amal")

        def statements(client, path, month):
            with CaptureQueriesContext(connection) as queries:
                ok(client.get(path, {"month": month}))
            return len(queries)

        for client, path in ((amal, "/api/preferences/mine"), (mona, url(er, "/solves/preview")), (mona, url(er, "/close"))):
            near = statements(client, path, "2026-12")  # first, so nothing the far one asks is already warm
            assert statements(client, path, "2076-12") <= near, path

    @pytest.mark.parametrize("scene", ["june", "june_and_august", "june_closed"])
    @pytest.mark.parametrize("project", [True, False])
    def test_every_month_starts_from_what_the_month_by_month_walk_gave(self, er, mona, carla, solver, on, scene, project):  # noqa: F811
        """Against the walk as it was before, one `_through` a month, for every nurse and every month to the end of next year:
        across a run of months with no roster, a roster after one, and the January reset."""
        approved(mona, carla, er, solver)
        if scene == "june_and_august":
            approved(mona, carla, er, solver, "2026-08")
        if scene == "june_closed":
            on(dt.date(2026, 7, 1))
            making.catch_up(at(dt.date(2026, 7, 1), 1))
        nurses = list(NurseProfile.objects.filter(placements__unit=er).select_related("user"))
        months = [dt.date(2026 + (5 + i) // 12, (5 + i) % 12 + 1, 1) for i in range(19)]  # June 2026 to December 2027
        chain, reference = Chain(project=project), Chain(project=project)

        for month in months:
            for nurse in nurses:
                assert chain.carry_in(nurse, month) == walked(reference, nurse, month), (nurse.user.username, month)

    def test_a_refused_start_is_refused_before_the_month_is_assembled(self, june, er, mona, monkeypatch):
        """It holds the hospital settings' lock, which every catalogue change waits on, for no longer than a refusal takes."""
        from solves import api as solves_api
        from solves.models import Solve
        Solve.objects.create(unit=er, month=dt.date(2076, 12, 1), requested_by=mona_user(), time_limit_seconds=60, input={})
        monkeypatch.setattr(solves_api, "assemble", lambda *_, **__: pytest.fail("assembled a month it then refused"))

        response = post(mona, url(er, "/solves"), {"month": "2076-12", "time_limit_seconds": 60})

        assert (response.status_code, response.json()["detail"]) == (409, "A solve of this month is already queued or running.")


class TestTheBatchedRead:
    """A unit's month reads every nurse's carry-over in a fixed number of queries, whatever the count of its nurses;
    the per-nurse answers are held to the one-at-a-time read by `every_batched_read_is_the_one_at_a_time_read`. ER's four
    nurses and one joining it from January, then four more: those joining are in June's and July's months, with nothing worked
    in June and no closed month, so each count is of the same kinds of nurse."""

    @staticmethod
    def joining(er, make_user, numbers):
        """Each with a skill, and every other one on probation from June, precepted by Amal: each per-nurse read of an assembly
        is asked of them."""
        from hospital.models import Skill
        from nurses.models import NurseSkill, PreceptorAssignment
        skill = Skill.objects.get_or_create(name="Preceptorship", defaults={"expires": True, "required_to_work": False,
                                                                             "lets_a_nurse_precept": True})[0]
        amal = NurseProfile.objects.get(user__username="Amal")
        for number in numbers:
            probation = number % 2 == 1
            nurse = nurse_in(er, make_user, f"Nurse{number}", night_balance=number / 4, overtime_balance=-number / 2,
                             start=JUNE if probation else dt.date(2026, 1, 1), on_probation=probation, probation_months=3)
            NurseSkill.objects.create(nurse=nurse, skill=skill, expiry=dt.date(2026, 7, 20), recorded_by=amal.user)
            if probation:
                PreceptorAssignment.objects.create(nurse=nurse, preceptor=amal, unit=er, from_month=JUNE, assigned_by=amal.user)

    @staticmethod
    def sources(carried: dict) -> dict[str, CarrySource]:
        """ER's four's source, and the newcomers': opening, as none is in a roster or a close."""
        return {"four": {carried[hospital_id].source for hospital_id in ("Amal", "Badr", "Dana", "Emad")},
                "joining": {value.source for hospital_id, value in carried.items() if hospital_id.startswith("Nurse")}}

    @staticmethod
    def statements(read) -> int:
        with CaptureQueriesContext(connection) as queries:
            read()
        return len(queries)

    @pytest.mark.parametrize("scene", ["opening", "projected", "closed", "projected_and_closed"])
    @pytest.mark.parametrize("project", [True, False])
    def test_the_chains_queries_for_a_units_month_do_not_grow_with_its_nurses(self, er, mona, carla, solver, on, make_user,
                                                                               queries_counted, scene, project):  # noqa: F811
        if scene != "opening":
            approved(mona, carla, er, solver)
        if scene in ("closed", "projected_and_closed"):
            on(dt.date(2026, 7, 1))
            making.catch_up(at(dt.date(2026, 7, 1), 1))
        if scene == "projected_and_closed":
            approved(mona, carla, er, solver, "2026-07")
        month = AUGUST if scene == "projected_and_closed" else JULY

        def read():
            nurses = list(membership.rostered_in(er, month).select_related("user"))
            return Chain(project=project).carry_ins(nurses, month)

        self.joining(er, make_user, [5])
        five = self.statements(read)
        self.joining(er, make_user, range(6, 10))
        with queries_counted(five):
            carried = read()

        four = {"opening": CarrySource.OPENING, "closed": CarrySource.CLOSED,
                "projected": CarrySource.PROJECTED if project else CarrySource.OPENING,
                "projected_and_closed": CarrySource.PROJECTED if project else CarrySource.CLOSED}[scene]
        assert len(carried) == 9 and self.sources(carried) == {"four": {four}, "joining": {CarrySource.OPENING}}

    @pytest.mark.parametrize("scene", ["opening", "projected", "closed"])
    def test_a_months_assembly_does_not_grow_with_its_nurses_either(self, er, mona, carla, solver, on, make_user,
                                                                    queries_counted, scene):  # noqa: F811
        if scene != "opening":
            approved(mona, carla, er, solver)
        if scene == "closed":
            on(dt.date(2026, 7, 1))
            making.catch_up(at(dt.date(2026, 7, 1), 1))

        self.joining(er, make_user, [5])
        five = self.statements(lambda: assemble(er, JULY))
        self.joining(er, make_user, range(6, 10))
        with queries_counted(five):
            july = assemble(er, JULY)

        four = {"opening": CarrySource.OPENING, "projected": CarrySource.PROJECTED, "closed": CarrySource.CLOSED}[scene]
        assert len(july.nurses) == 9 and self.sources(july.carried) == {"four": {four}, "joining": {CarrySource.OPENING}}
        rows = {row["id"]: row for row in july.nurses}
        assert [hospital_id for hospital_id, row in rows.items() if row.get("on_probation")] == ["Nurse5", "Nurse7", "Nurse9"]
        assert {rows[hospital_id].get("preceptor") for hospital_id in ("Nurse5", "Nurse7", "Nurse9")} <= {"Amal", None}
        assert all(rows[f"Nurse{number}"]["skills"] for number in range(5, 10))

    def test_a_close_reads_each_nurses_carry_over_once_for_both_columns(self, june, er, mona, monkeypatch):
        """June approved and not closed: the projection's assembly of June and the `into` column share one reading, so where
        each nurse's chain into June starts is read once."""
        from closes import chain
        reads = []
        read = chain.latest_closed

        def counted(nurses, before):
            reads.extend((nurse.user.username, before) for nurse in nurses)
            return read(nurses, before)
        monkeypatch.setattr(chain, "latest_closed", counted)

        view = close_view(mona, er, "2026-06")

        assert {row["out"]["source"] for row in view["carry_over"]} == {"projected"}
        assert sorted(reads) == [(hospital_id, JUNE) for hospital_id in ("Amal", "Badr", "Dana", "Emad")]


def walked(chain: Chain, nurse: NurseProfile, month: dt.date):
    """`Chain.carry_in` as it was until a far month stopped stepping through every month: one `_through` a month."""
    carried, step = where_she_starts(chain, nurse, month)
    while step is not None and step < month:
        carried = chain._through(nurse, step, carried)
        step = next_month(step)
    return carried


def one_at_a_time(chain: Chain, nurse: NurseProfile, month: dt.date) -> Carried:
    """`Chain.carry_in` as it was before it was batched: her latest closed month, her first month and her first approved
    roster each read for her alone, then a run of months with no roster as one step."""
    carried, step = where_she_starts(chain, nurse, month)
    while step is not None and step < month:
        ahead = chain._next_roster_month(step, month)
        last = month_before(ahead or month)
        if step <= last:
            carried = Carried(carried.into(last) | NO_HISTORY, carried.source, last, last.year)
        if ahead is None:
            return carried
        carried = chain._through(nurse, ahead, carried)
        step = next_month(ahead)
    return carried


def where_she_starts(chain: Chain, nurse: NurseProfile, month: dt.date) -> tuple[Carried, dt.date | None]:
    """What the chain starts from for her, and the first month it steps through: the latest month before `month` closed with
    her in it, from that month's close in force; else her opening values, at the first approved roster of any unit that holds
    her from her start, if a chain that projects has one."""
    rows = list(CarryOver.objects.filter(nurse=nurse, source=CarrySource.CLOSED, month__lt=month)
                .select_related("close").order_by("-month", "-close__closed_at"))
    latest = {}  # Each unit's month's close in force: the latest.
    for close in MonthClose.objects.filter(unit_id__in={row.unit_id for row in rows}, month__in={row.month for row in rows}
                                           ).order_by("unit_id", "month", "-closed_at", "-id"):
        latest.setdefault((close.unit_id, close.month), close.pk)
    closed = next((row for row in rows if row.close_id in set(latest.values())), None)
    if closed is not None:
        return (Carried(CARRIED_SINCE | closed.values, CarrySource.CLOSED, closed.month, closed.month.year),
                next_month(closed.month))
    first = nurse.placements.order_by("from_month").first()
    added = first.from_month if first else nurse.start_date.replace(day=1)
    roster = (Roster.objects.filter(state=RosterState.APPROVED, month__lt=month, month__gte=nurse.start_date.replace(day=1),
                                    grid__has_key=nurse.user.username).order_by("month").values_list("month", flat=True).first()
              if chain.project else None)
    return Carried(opening(nurse), CarrySource.OPENING, None, added.year), roster


def mona_user():
    from accounts.models import User
    return User.objects.get(username="mona")


class TestTheCountOfPreferencesNotKept:
    """The chain carries each nurse's count of preferences not kept as one more carried field, from the solver's own
    carry-over block. The expected counts are `amals_preferences`'s, by hand."""

    def test_it_is_projected_from_the_roster_in_force(self, er, mona, carla, solver):  # noqa: F811
        amals_preferences(er)
        approved(mona, carla, er, solver)

        assert not_kept(er, JULY) == 2
        assert {not_kept(er, JULY, nurse) for nurse in ("Badr", "Dana", "Emad")} == {0}

    def test_the_close_corrects_it_with_leave_recorded_late(self, er, mona, carla, solver, on):  # noqa: F811
        """Leave on the 2nd, which she asked off and works: neither kept nor not kept, so one is left."""
        amals_preferences(er)
        approved(mona, carla, er, solver)
        on(dt.date(2026, 7, 1))
        assert record_leave(mona, er, "Amal", 2).status_code == 201

        [close] = making.catch_up(at(dt.date(2026, 7, 1), 1))

        assert carried(close)["Amal"]["preferences_not_kept"] == 1
        assert not_kept(er, JULY) == 1
        assert assemble(er, JULY).carried["Amal"].source == CarrySource.CLOSED

    def test_a_month_with_no_roster_hands_on_zero(self, er, mona, carla, solver):  # noqa: F811
        from hospital.models import FixedRamadanMonth
        for month in ("2026-07", "2026-08"):
            FixedRamadanMonth.objects.get_or_create(month=month, defaults={"dates": []})
        amals_preferences(er)
        approved(mona, carla, er, solver)

        assert not_kept(er, JULY) == 2
        assert not_kept(er, AUGUST) == 0  # July has no roster: one month's memory, and nothing was kept or not in July.

    def test_her_first_month_starts_from_zero(self, er):
        """Her opening values hold no count: the months before had no roster in this application to keep anything."""
        amals_preferences(er)

        assert {not_kept(er, JUNE, nurse) for nurse in ("Amal", "Badr", "Dana", "Emad")} == {0}

    def test_it_follows_a_transfer(self, er, mona, carla, solver, client_of, make_user):  # noqa: F811
        from accounts.models import User
        from solves.tests.conftest import make_sample_unit
        client_of("omar", Role.UNIT_MANAGER)
        icu = make_sample_unit("ICU", User.objects.get(username="omar"), make_user, prefix="i")
        amals_preferences(er)
        approved(mona, carla, er, solver)

        membership.transfer(NurseProfile.objects.get(user__username="Amal"), icu, JULY)

        assert not_kept(icu, JULY) == 2

    def test_closing_again_re_works_it(self, er, mona, carla, solver, on):  # noqa: F811
        amals_preferences(er)
        approved(mona, carla, er, solver)
        on(dt.date(2026, 7, 1))
        [first] = making.catch_up(at(dt.date(2026, 7, 1), 1))
        assert carried(first)["Amal"]["preferences_not_kept"] == 2 and not_kept(er, JULY) == 2

        assert record_leave(mona, er, "Amal", 2).status_code == 201
        ok(close_again(mona, er))

        assert carried(closed(er))["Amal"]["preferences_not_kept"] == 1
        assert not_kept(er, JULY) == 1


def amal_works_ahead(unit) -> None:
    """Amal comes into June 24 hours ahead, so SOUND's June, which leaves every nurse 4 hours short of what she owes (Dana 2),
    gives her overtime and no one else any.

    Counted by hand: she owes 208 - 24 = 184 and works 17 twelve-hour shifts, 204, so 20 hours of overtime, the unit's whole
    overtime. The four owe 184 + 208 + 182 + 208 = 782. Her share is 20 x 184 / 782 = 4.71, so her balance moves by
    20 - 4.71 = +15.29; Badr's and Emad's by 0 - 20 x 208 / 782 = -5.32, Dana's by 0 - 20 x 182 / 782 = -4.65.

    With a day of leave on the 1st, a Night she works, recorded late: she owes 184 - 8 = 176 (a day's credit of her 208 hours)
    and works 192, so 16 hours. The four owe 774. +16 - 16 x 176 / 774 = +12.36; Badr and Emad -16 x 208 / 774 = -4.30, Dana
    -16 x 182 / 774 = -3.76."""
    NurseProfile.objects.filter(user__username="Amal").update(hours_balance=-24)


JUNE_OUT = {"Amal": 15.29, "Badr": -5.32, "Dana": -4.65, "Emad": -5.32}
JUNE_WITH_LEAVE_OUT = {"Amal": 12.36, "Badr": -4.3, "Dana": -3.76, "Emad": -4.3}


def one_more_shift_past_her_share(hospital_id: str) -> dict[str, float]:
    """A hand edit, a swap or a cover giving a nurse who comes into June 24 hours ahead one more 12-hour shift in
    SOUND's June. She owes 184 and works 216, so 32 hours of overtime, the unit's whole overtime, 12 of them priced (32 - 32 x
    184 / 782 - 12 = 12.47, rounded down), and nothing refuses it. The four owe 782, so her balance moves by 32 - 7.53 =
    +24.47, a colleague owing 208 by -32 x 208 / 782 = -8.51, Dana (182) by -7.45. What July's projected input carries."""
    NurseProfile.objects.filter(user__username=hospital_id).update(hours_balance=-24)
    owing = {"Amal": 208, "Badr": 208, "Dana": 182, "Emad": 208}
    return {nurse: 24.47 if nurse == hospital_id else {208: -8.51, 182: -7.45}[owed] for nurse, owed in owing.items()}


def balances(unit, month) -> dict[str, float]:
    """What the month's solve input hands the solver."""
    return {row["id"]: row["overtime_balance"] for row in assemble(unit, month).nurses}


class TestTheOvertimeBalance:
    """The chain carries each nurse's overtime balance as one more carried field, from the solver's own carry-over
    block, with the unit sharing its overtime as the sample's rules do (12 hours free, the carry moving a share by 12
    hours at most). The expected balances are `amal_works_ahead`'s, by hand."""

    def test_her_first_month_takes_her_opening_value(self, er):
        NurseProfile.objects.filter(user__username="Dana").update(overtime_balance=-6.5)

        assert balances(er, JUNE) == {"Amal": 0.0, "Badr": 0.0, "Dana": -6.5, "Emad": 0.0}
        assert assemble(er, JUNE).carried["Dana"].source == CarrySource.OPENING

    def test_it_is_projected_from_the_roster_in_force(self, er, mona, carla, solver):  # noqa: F811
        amal_works_ahead(er)
        approved(mona, carla, er, solver)

        assert balances(er, JULY) == JUNE_OUT
        assert assemble(er, JULY).carried["Amal"].source == CarrySource.PROJECTED

    def test_it_moves_on_from_her_opening_value(self, er, mona, carla, solver):  # noqa: F811
        """Her share does not hang on what she carries in, so her balance moves by the same 15.29: 3.5 + 15.29 = 18.79."""
        amal_works_ahead(er)
        NurseProfile.objects.filter(user__username="Amal").update(overtime_balance=3.5)
        approved(mona, carla, er, solver)

        assert balances(er, JULY)["Amal"] == 18.79

    def test_the_close_corrects_it_with_leave_recorded_late(self, er, mona, carla, solver, on):  # noqa: F811
        amal_works_ahead(er)
        approved(mona, carla, er, solver)
        on(dt.date(2026, 7, 1))
        assert record_leave(mona, er, "Amal", 1).status_code == 201

        [close] = making.catch_up(at(dt.date(2026, 7, 1), 1))

        assert {hospital_id: values["overtime_balance"] for hospital_id, values in carried(close).items()} == JUNE_WITH_LEAVE_OUT
        assert balances(er, JULY) == JUNE_WITH_LEAVE_OUT
        assert assemble(er, JULY).carried["Amal"].source == CarrySource.CLOSED

    def test_closing_again_re_works_it(self, er, mona, carla, solver, on):  # noqa: F811
        amal_works_ahead(er)
        approved(mona, carla, er, solver)
        on(dt.date(2026, 7, 1))
        [first] = making.catch_up(at(dt.date(2026, 7, 1), 1))
        assert carried(first)["Amal"]["overtime_balance"] == 15.29 and balances(er, JULY) == JUNE_OUT

        assert record_leave(mona, er, "Amal", 1).status_code == 201
        ok(close_again(mona, er))

        assert carried(closed(er))["Amal"]["overtime_balance"] == 12.36
        assert carried(first)["Amal"]["overtime_balance"] == 15.29, "The first close stays as it was."
        assert balances(er, JULY) == JUNE_WITH_LEAVE_OUT

    def test_a_close_starts_from_the_month_before_closed(self, er, mona, carla, solver, on):  # noqa: F811
        """July's close takes June's closed balances, the leave in them, whatever July's projection had said."""
        amal_works_ahead(er)
        approved(mona, carla, er, solver)
        approved(mona, carla, er, solver, "2026-07")
        on(dt.date(2026, 7, 1))
        assert record_leave(mona, er, "Amal", 1).status_code == 201
        on(dt.date(2026, 8, 3))

        june_close, july_close = making.catch_up(at(dt.date(2026, 8, 3), 9))

        assert (june_close.month, july_close.month) == (JUNE, JULY)
        assert {row["id"]: row["overtime_balance"] for row in july_close.input["nurses"]} == JUNE_WITH_LEAVE_OUT

    def test_a_month_with_no_roster_hands_it_on_unchanged(self, er, mona, carla, solver):  # noqa: F811
        from hospital.models import FixedRamadanMonth
        for month in ("2026-07", "2026-08"):
            FixedRamadanMonth.objects.get_or_create(month=month, defaults={"dates": []})
        amal_works_ahead(er)
        approved(mona, carla, er, solver)

        assert balances(er, AUGUST) == JUNE_OUT  # July has no roster: nothing was worked or owed there to move it.

    def test_it_follows_a_transfer(self, er, mona, carla, solver, client_of, make_user):  # noqa: F811
        from accounts.models import User
        from solves.tests.conftest import make_sample_unit
        client_of("omar", Role.UNIT_MANAGER)
        icu = make_sample_unit("ICU", User.objects.get(username="omar"), make_user, prefix="i")
        amal_works_ahead(er)
        approved(mona, carla, er, solver)

        membership.transfer(NurseProfile.objects.get(user__username="Amal"), icu, JULY)

        assert balances(icu, JULY)["Amal"] == 15.29

    def test_a_close_stored_before_it_existed_hands_on_zero(self, er, mona, carla, solver, on):  # noqa: F811
        amal_works_ahead(er)
        approved(mona, carla, er, solver)
        on(dt.date(2026, 7, 1))
        [close] = making.catch_up(at(dt.date(2026, 7, 1), 1))
        for row in close.carry_overs.all():                   # as a close made before the balance was stored
            row.values = {key: value for key, value in row.values.items() if key != "overtime_balance"}
            row.save(update_fields=["values"])

        assert balances(er, JULY) == {"Amal": 0.0, "Badr": 0.0, "Dana": 0.0, "Emad": 0.0}
        assert assemble(er, JULY).carried["Amal"].source == CarrySource.CLOSED


class TestThreeMonths:
    """The chain across June, July and August, solved by the scripted solver and approved through the API: each month's close starts
    from the one before's, a late fact closed again in the middle moves what the month after starts from, and every month's
    projection is the same computation as its close."""

    @pytest.fixture
    def three(self, mona, carla, er, solver):  # noqa: F811
        from hospital.models import FixedRamadanMonth
        for month in ("2026-07", "2026-08"):
            FixedRamadanMonth.objects.get_or_create(month=month, defaults={"dates": []})
        for month in ("2026-06", "2026-07", "2026-08"):
            approved(mona, carla, er, solver, month)

    def starts(self, close: MonthClose) -> dict[str, dict]:
        return {row["id"]: {key: row[key] for key in CARRIED} for row in close.input["nurses"]}

    def test_each_close_starts_from_the_close_before(self, three, mona, er, on):
        projected = {month: Chain().carry_out(Roster.objects.get(unit=er, month=month)) for month in (JUNE, JULY, AUGUST)}
        on(dt.date(2026, 9, 1))

        june, july, august = making.catch_up(at(dt.date(2026, 9, 1), 1))

        assert [close.month for close in (june, july, august)] == [JUNE, JULY, AUGUST]
        assert self.starts(july) == carried(june) and self.starts(august) == carried(july)
        # Nothing was recorded late, so each projection was what its close found.
        assert [carried(close) for close in (june, july, august)] == [projected[month] for month in (JUNE, JULY, AUGUST)]
        view = close_view(mona, er, "2026-08")
        assert {(row["into"]["source"], row["into"]["out_of"], row["out"]["source"]) for row in view["carry_over"]} == {
            ("closed", "2026-07", "closed")}

    def test_a_late_fact_closed_again_in_the_middle_moves_what_the_next_month_starts_from(self, three, mona, er, on):
        on(dt.date(2026, 8, 3))
        june, july = making.catch_up(at(dt.date(2026, 8, 3), 1))
        worked = next(day for day in range(1, 32) if SOUND["2026-07"]["roster"]["Badr"][f"2026-07-{day:02d}"])
        before = assemble(er, AUGUST).carried["Badr"]

        assert record_leave(mona, er, "Badr", worked, "2026-07").status_code == 201
        assert ok(close_again(mona, er, "2026-07"))["closed"]["changed_since"] is False

        after = assemble(er, AUGUST).carried["Badr"]
        assert before.source == after.source == CarrySource.CLOSED
        assert after.values["previous"] != before.values["previous"]
        assert self.starts(making.latest_close(er, JULY)) == self.starts(july) == carried(june)  # The same start: June's close.
