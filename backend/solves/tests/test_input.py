"""The assembler: what the solver receives for a unit's month."""
import copy
import datetime as dt
import json

import pytest
from nurse_roster.request import load_request

from accounts.models import Role
from hospital.models import FixedRamadanMonth, HospitalSettings, LeaveType, PublicHoliday, Shift
from nurses.models import Leave, NurseProfile, Placement
from nurses.tests.conftest import make_unit
from requesting.models import Preference, PreferenceKind, PreferenceState, RequestWindow
from solves.input import assemble, assemble_replan, changes_since, solver_refusal
from solves.tests.conftest import MONTH, SAMPLE, nurse_in

pytestmark = pytest.mark.django_db
JUNE = dt.date(2026, 6, 1)


@pytest.fixture(autouse=True)
def _before_june(before_june):
    pass


def built(unit, **options):
    return assemble(unit, JUNE, time_limit=options.get("time_limit", 20))


def block(assembled, hospital_id):
    return next(row for row in assembled.input["nurses"] if row["id"] == hospital_id)


def preference(unit, hospital_id, day, kind=PreferenceKind.DAY_OFF, state=PreferenceState.MADE) -> Preference:
    """Made in a window of the unit for the day's month, as `requesting` makes one: the assembler reads only the rows."""
    month = day.replace(day=1)
    window = RequestWindow.objects.filter(unit=unit, month=month).first() or RequestWindow.objects.create(
        unit=unit, month=month, opens_on=dt.date(2026, 5, 15), closes_on=dt.date(2026, 5, 25), state="closed",
        created_by=unit.manager or NurseProfile.objects.first().user)
    ended = None if state == PreferenceState.MADE else dt.datetime(2026, 5, 20, tzinfo=dt.UTC)
    return Preference.objects.create(window=window, nurse=NurseProfile.objects.get(user__username=hospital_id), date=day,
                                     kind=kind, state=state, ended_at=ended)


def leave_on(nurse, *days, by):
    kind = LeaveType.objects.get(name="Sick")
    for day in days:
        Leave.objects.create(nurse=nurse, date=day, leave_type=kind, recorded_by=by)


def test_the_solvers_own_sample_as_this_app_holds_it_comes_out_as_the_sample(er, make_user):
    """The sample is the solver's, so it is a truth this code did not write."""
    badr = NurseProfile.objects.get(user__username="Badr")
    leave_on(badr, *next(row for row in SAMPLE["nurses"] if row["id"] == "Badr")["leave"], by=badr.user)
    dana = next(row for row in SAMPLE["nurses"] if row["id"] == "Dana")
    for day in dana["undesired_days"]:
        preference(er, "Dana", dt.date.fromisoformat(day))
    for day in dana["desired_days"]:
        preference(er, "Dana", dt.date.fromisoformat(day), PreferenceKind.WORKING_DAY)

    solve_input = built(er, time_limit=SAMPLE["solver"]["time_limit_seconds"]).input

    expected = copy.deepcopy(SAMPLE)
    expected["department"]["name"] = "ER"
    assert solve_input["department"] == expected["department"]
    assert {key: value for key, value in solve_input["general"].items() if key != "shift_hours"} == {
        key: value for key, value in expected["general"].items() if key != "shift_hours"}
    assert solve_input["general"]["shift_hours"] == {shift.code: shift.hours for shift in Shift.objects.all()}
    assert expected["general"]["shift_hours"].items() <= solve_input["general"]["shift_hours"].items()
    for row in expected["nurses"]:
        row.setdefault("leave", [])
        row.setdefault("allowed_shifts", None)
        # No month before has a roster, so the carry-over is the opening values with no history.
        row["previous"] = []
        # No pin is made here; Dana's two preferences are.
        row.pop("pinned", None)
        # The chain carries the count of preferences not kept: 0, since none were ever priced.
        row["preferences_not_kept"] = 0
        # And the overtime balance: her opening value, 0 by default, as the sample has none.
        row["overtime_balance"] = 0.0
    assert solve_input["nurses"] == expected["nurses"]
    assert (solve_input["version"], solve_input["month"], solve_input["ramadan_days"]) == (2, MONTH, [])
    assert solve_input["solver"] == SAMPLE["solver"]
    assert [row.id for row in load_request(solve_input).problem.nurses] == ["Amal", "Badr", "Dana", "Emad"]


def test_opening_values_reach_the_solver(er, make_user):
    nurse_in(er, make_user, "Omar", gender="M", ytd_overtime=12, hours_balance=-8, night_balance=1.5, friday_balance=-0.5,
             overtime_balance=-9.75)

    row = block(built(er), "Omar")

    assert (row["ytd_overtime"], row["hours_balance"], row["night_balance"], row["friday_balance"]) == (12, -8, 1.5, -0.5)
    assert row["overtime_balance"] == -9.75
    assert next(nurse for nurse in load_request(built(er).input).problem.nurses if nurse.id == "Omar").overtime_balance == -9.75


def test_each_nurses_overtime_balance_comes_from_the_chain(er):
    """May's closed balance for Amal, the opening value, 0, for the others."""
    carried_from_may(er, "Amal", 0, overtime_balance=8.25)

    solve_input = built(er).input

    assert {row["id"]: row["overtime_balance"] for row in solve_input["nurses"]} == {
        "Amal": 8.25, "Badr": 0.0, "Dana": 0.0, "Emad": 0.0}


def test_days_outside_the_dates_are_credited_like_leave_and_listed_once(er, make_user):
    """A date recorded and then left outside the dates by a correction must not reach the solver twice."""
    nurse = nurse_in(er, make_user, "Omar", gender="M", start=dt.date(2026, 6, 3), end=dt.date(2026, 6, 28))
    leave_on(nurse, dt.date(2026, 6, 2), dt.date(2026, 6, 10), by=nurse.user)

    assembled = built(er)

    assert block(assembled, "Omar")["leave"] == ["2026-06-01", "2026-06-02", "2026-06-10", "2026-06-29", "2026-06-30"]
    assert assembled.leave_days == 1
    assert assembled.outside_days == {"Omar": ["2026-06-01", "2026-06-02", "2026-06-29", "2026-06-30"]}
    load_request(assembled.input)


def test_a_nurse_never_present_stays_out(er, make_user):
    nurse_in(er, make_user, "Omar", gender="M", start=dt.date(2026, 7, 1))
    nurse_in(er, make_user, "Zaid", gender="M", start=dt.date(2026, 6, 20), end=dt.date(2026, 5, 1))

    assert {row["id"] for row in built(er).input["nurses"]} == {"Amal", "Badr", "Dana", "Emad"}


def test_allowed_shifts_are_those_of_the_months_placement(er, make_user):
    icu = make_unit("ICU")
    nurse = nurse_in(er, make_user, "Omar", gender="M")
    Placement.objects.filter(nurse=nurse).update(allowed_shifts=["N"])
    Placement.objects.create(nurse=nurse, unit=icu, from_month=dt.date(2026, 7, 1), allowed_shifts=["D"])

    assert block(built(er), "Omar")["allowed_shifts"] == ["N"]
    assert block(assemble(icu, dt.date(2026, 7, 1), time_limit=20), "Omar")["allowed_shifts"] == ["D"]


def test_no_leave_type_is_in_a_solve_input(er, make_user):
    nurse = nurse_in(er, make_user, "Omar", gender="M")
    leave_on(nurse, dt.date(2026, 6, 9), by=nurse.user)

    assert "Sick" not in json.dumps(built(er).input)


def test_a_deactivated_manager_with_no_end_date_is_left_out_and_named(er, make_user):
    nurse = nurse_in(er, make_user, "Sara", role=Role.UNIT_MANAGER)
    nurse.user.is_active = False
    nurse.user.save()

    assembled = built(er)

    assert "Sara" not in {row["id"] for row in assembled.input["nurses"]}
    assert [left.user.username for left in assembled.left_out] == ["Sara"]


def test_a_deactivated_nurse_whose_end_date_has_come_keeps_the_days_they_worked(er, make_user, monkeypatch):
    from nurses import membership
    monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 6, 20))
    nurse = nurse_in(er, make_user, "Omar", gender="M", end=dt.date(2026, 6, 20))
    nurse.user.is_active = False
    nurse.user.save()

    assembled = built(er)

    assert block(assembled, "Omar")["leave"] == [f"2026-06-{day}" for day in range(21, 31)]
    assert assembled.left_out == []


def test_a_list_a_transfer_emptied_is_named(er, make_user):
    nurse = nurse_in(er, make_user, "Omar", gender="M")
    Placement.objects.filter(nurse=nurse).update(allowed_shifts_to_check=True)

    assert [checked.user.username for checked in built(er).to_check] == ["Omar"]


def test_fixed_and_derived_ramadan_and_only_this_months_holidays(er):
    PublicHoliday.objects.create(date=dt.date(2026, 6, 15), name="In")
    PublicHoliday.objects.create(date=dt.date(2026, 7, 15), name="Out")
    FixedRamadanMonth.objects.filter(month=MONTH).update(dates=[dt.date(2026, 6, 1), dt.date(2026, 6, 2)])

    solve_input = built(er).input

    assert solve_input["public_holidays"] == ["2026-06-15"]
    assert solve_input["ramadan_days"] == ["2026-06-01", "2026-06-02"]


def test_a_holiday_added_from_the_calendars_proposals_reaches_the_solver_like_any(er, signed_in):
    """Arafat Day 1447 is 26 May 2026; a CNO adding Eid al-Adha for eight days runs it into June."""
    response = signed_in(Role.CNO).post("/api/hospital/holiday-proposals?year=2026",
                                        {"name": "Eid al-Adha", "first": "2026-05-26", "length": 8},
                                        content_type="application/json")
    assert response.status_code == 200, response.content

    assert built(er).input["public_holidays"] == ["2026-06-01", "2026-06-02"]


def test_what_the_solver_refuses_is_said_in_its_words(er):
    er.rules["night_shift"] = ""
    er.save()

    assert "night_shift" in solver_refusal(built(er).input)


def test_a_raised_weight_can_leave_a_one_shift_unit_for_the_solver_to_refuse(er, make_user):
    """Saved while `shift_switch` and `night_rest` were 0, as the rules allow: the CNO then raises one."""
    settings = HospitalSettings.load()
    settings.rules["weights"] |= {"shift_switch": 0, "night_rest": 0}
    settings.save()
    er.rules |= {"shifts": ["D"], "fair_shifts": ["D"], "night_shift": "N", "day_shift": "D", "no_mixed_blocks": False,
                 "night_rest_hard": False, "carry_fairness": False, "headcounts": er.rules["headcounts"][:1]}
    er.save()
    Placement.objects.filter(unit=er).update(allowed_shifts=None)
    assert solver_refusal(built(er).input) is None

    settings.rules["weights"]["shift_switch"] = 8
    settings.save()

    assert solver_refusal(built(er).input) is not None


def carried_from_may(unit, hospital_id, preferences_not_kept, *, overtime_balance: float | None = None) -> None:
    """As if May had closed with her preferences not kept, and her overtime balance when given: a closed carry-over of May, the
    January test's pattern."""
    from closes.models import CarryOver, CarrySource, MonthClose
    may = dt.date(2026, 5, 1)
    close = MonthClose.objects.filter(unit=unit, month=may).first() or MonthClose.objects.create(
        unit=unit, month=may, version=1, input={}, result={})
    values = {"ytd_overtime": 0, "hours_balance": 0, "night_balance": 0.0, "friday_balance": 0.0, "previous": [],
              "last_shift_kind": None, "rest_owed": 0, "preferences_not_kept": preferences_not_kept}
    if overtime_balance is not None:
        values["overtime_balance"] = overtime_balance
    CarryOver.objects.create(nurse=NurseProfile.objects.get(user__username=hospital_id), unit=unit, month=may,
                             source=CarrySource.CLOSED, close=close, values=values)


class TestTheCountCarriedIn:
    """Each nurse's count of preferences not kept the month before reaches the solver, and above 0 her preferences
    cost twice the weight: priced here by the solver package's own validator on SOUND's June, with no search."""

    def test_it_reaches_the_solver_and_doubles_her_price_alone(self, er):
        from solver_service.grid import validate_grid
        from rosters.tests.test_review import SOUND
        carried_from_may(er, "Dana", 1)
        carried_from_may(er, "Amal", 0)
        preference(er, "Dana", dt.date(2026, 6, 5))   # Dana works Night on the 5th of SOUND's June,
        preference(er, "Amal", dt.date(2026, 6, 2))   # and so does Amal on the 2nd.

        solve_input = built(er).input

        assert (block_of(solve_input, "Dana")["preferences_not_kept"], block_of(solve_input, "Amal")["preferences_not_kept"]) == (1, 0)
        [rule] = [rule for rule in validate_grid(solve_input, SOUND[MONTH]["roster"])["rules"] if rule["id"] == "undesired_day"]
        weight = HospitalSettings.load().rules["weights"]["undesired_day"]
        assert {match["nurse"]: match["amount"] for match in rule["matches"]} == {"Amal": 1, "Dana": 2}
        assert rule["weighted"] == 3 * weight
        dana = next(match for match in rule["matches"] if match["nurse"] == "Dana")
        assert dana["text"].endswith("counts double, a preference was not kept last month)")


def block_of(solve_input, hospital_id):
    return next(row for row in solve_input["nurses"] if row["id"] == hospital_id)


def full_runs_capped(unit, cap: int, weight: int) -> None:
    unit.rules |= {"max_full_runs": cap}
    unit.save()
    settings = HospitalSettings.load()
    settings.rules["weights"]["excess_full_run"] = weight
    settings.save()


class TestFullRuns:
    """The unit's most full runs in a month and the hospital's price of each one above it reach every new input;
    one stored without them is read as no cap. Priced by the solver package's own validator, with no search."""

    @staticmethod
    def amal_in_six_full_runs() -> dict:
        """SOUND's June, but Amal works Day on four days in a row, off on the fifth, all month: six full runs of the
        sample's longest run, 4, the last ended by the 30th off."""
        from rosters.tests.test_review import SOUND
        roster = copy.deepcopy(SOUND[MONTH]["roster"])
        roster["Amal"] = {f"2026-06-{day:02d}": "" if day % 5 == 0 else "D" for day in range(1, 31)}
        return roster

    def test_a_solve_input_carries_the_units_cap_and_the_hospitals_weight(self, er):
        full_runs_capped(er, 2, 20)

        problem = load_request(built(er).input).problem

        assert (problem.department.max_full_runs, problem.general.weights.excess_full_run) == (2, 20)

    def test_each_full_run_above_the_cap_costs_the_weight_in_the_solvers_words(self, er):
        from solver_service.grid import validate_grid
        full_runs_capped(er, 3, 4)

        [rule] = [rule for rule in validate_grid(built(er).input, self.amal_in_six_full_runs())["rules"]
                  if rule["id"] == "excess_full_run"]

        amal = next(match for match in rule["matches"] if match["nurse"] == "Amal")
        assert (amal["amount"], amal["text"]) == (3, "Amal: 6 full runs of 4 shifts, max 3")
        assert rule["weight"] == 4

    def test_an_input_stored_before_full_runs_is_read_as_no_cap_and_prices_the_rest_as_before(self, er):
        from solver_service.grid import validate_grid
        full_runs_capped(er, 3, 4)
        now = built(er).input
        stored = copy.deepcopy(now)
        del stored["department"]["max_full_runs"]
        del stored["general"]["weights"]["excess_full_run"]
        roster = self.amal_in_six_full_runs()

        before, after = validate_grid(stored, roster), validate_grid(now, roster)

        assert load_request(stored).problem.department.max_full_runs is None
        assert before["penalties"]["excess_full_run"] == 0 and after["penalties"]["excess_full_run"] >= 3 * 4
        assert before["penalties"] == after["penalties"] | {"excess_full_run": 0}
        assert before["cost"] == after["cost"] - after["penalties"]["excess_full_run"]


OVERTIME_SHARE = ("share_overtime", "overtime_tolerance", "overtime_carry_cap")


def amal_ahead_in_sound_june() -> dict:
    """SOUND's June, with Amal coming in 24 hours ahead: 20 hours of overtime, all hers, the unit's whole overtime. Counted by
    hand in `closes.tests.test_close.amal_works_ahead`: her share is 20 x 184 / 782 = 4.71 h, so with 12 hours free her
    priced hours are 20 - 4.71 - 12 = 3.29, rounded down to 3."""
    from rosters.tests.test_review import SOUND
    NurseProfile.objects.filter(user__username="Amal").update(hours_balance=-24)
    return copy.deepcopy(SOUND[MONTH]["roster"])


class TestTheOvertimeShare:
    """The unit's three overtime share rules and the hospital's weight reach every new input; one stored without
    them is read as the rule off. Priced by the solver package's own validator, with no search."""

    def test_a_solve_input_carries_the_units_three_fields_and_the_hospitals_weight(self, er):
        er.rules |= {"share_overtime": True, "overtime_tolerance": 8, "overtime_carry_cap": 30}
        er.save()
        settings = HospitalSettings.load()
        settings.rules["weights"] |= {"nurse_overtime": 6, "overtime_above_share": 5}
        settings.save()

        solve_input = built(er).input
        problem = load_request(solve_input).problem

        assert {key: solve_input["department"][key] for key in OVERTIME_SHARE} == {
            "share_overtime": True, "overtime_tolerance": 8, "overtime_carry_cap": 30}
        assert solve_input["general"]["weights"]["overtime_above_share"] == 5
        department = problem.department
        assert (department.share_overtime, department.overtime_tolerance, department.overtime_carry_cap) == (True, 8, 30)
        assert problem.general.weights.overtime_above_share == 5

    def test_overtime_above_a_share_costs_the_weight_in_the_solvers_words(self, er):
        from solver_service.grid import validate_grid
        roster = amal_ahead_in_sound_june()

        answer = validate_grid(built(er).input, roster)

        [rule] = [rule for rule in answer["rules"] if rule["id"] == "overtime_above_share"]
        assert [(match["nurse"], match["amount"], match["text"]) for match in rule["matches"]] == [
            ("Amal", 3, "Amal: 20 h overtime, share 4.7 h, 3 h priced")]
        assert (rule["weight"], answer["penalties"]["overtime_above_share"]) == (1, 3)

    def test_an_input_stored_before_the_overtime_share_is_read_as_the_rule_off_and_prices_the_rest_as_before(self, er):
        from solver_service.grid import validate_grid
        roster = amal_ahead_in_sound_june()
        now = built(er).input
        stored = copy.deepcopy(now)
        for key in OVERTIME_SHARE:
            del stored["department"][key]
        del stored["general"]["weights"]["overtime_above_share"]

        before, after = validate_grid(stored, roster), validate_grid(now, roster)

        department = load_request(stored).problem.department
        assert (department.share_overtime, department.overtime_tolerance, department.overtime_carry_cap) == (False, 0, 0)
        assert before["penalties"]["overtime_above_share"] == 0 and after["penalties"]["overtime_above_share"] == 3
        assert before["penalties"] == after["penalties"] | {"overtime_above_share": 0}
        assert before["cost"] == after["cost"] - 3


class TestPreferences:
    """A nurse's live preferences are the days she asked off (`undesired_days`) and to work (`desired_days`), each in date
    order."""

    def test_her_live_ones_of_the_month_fill_the_two_lists_and_nothing_else_does(self, er, make_user):
        for day in (9, 3):
            preference(er, "Amal", dt.date(2026, 6, day))
        preference(er, "Amal", dt.date(2026, 6, 20), PreferenceKind.WORKING_DAY)
        preference(er, "Amal", dt.date(2026, 6, 4), state=PreferenceState.WITHDRAWN)
        preference(er, "Amal", dt.date(2026, 6, 5), PreferenceKind.WORKING_DAY, state=PreferenceState.ASKED_INSTEAD)
        preference(er, "Amal", dt.date(2026, 7, 2))
        preference(er, "Badr", dt.date(2026, 6, 4), PreferenceKind.WORKING_DAY)

        assembled = built(er)

        amal, badr, emad = (block(assembled, hospital_id) for hospital_id in ("Amal", "Badr", "Emad"))
        assert (amal["undesired_days"], amal["desired_days"]) == (["2026-06-03", "2026-06-09"], ["2026-06-20"])
        assert (badr.get("undesired_days"), badr["desired_days"]) == (None, ["2026-06-04"])
        assert "undesired_days" not in emad and "desired_days" not in emad
        assert solver_refusal(assembled.input) is None

    def test_one_on_leave_or_outside_her_dates_is_left_in_for_the_solver_to_ignore(self, er):
        amal = NurseProfile.objects.get(user__username="Amal")
        amal.end_date = dt.date(2026, 6, 20)
        amal.save()
        leave_on(amal, dt.date(2026, 6, 3), by=amal.user)
        preference(er, "Amal", dt.date(2026, 6, 3))
        preference(er, "Amal", dt.date(2026, 6, 25), PreferenceKind.WORKING_DAY)

        assembled = built(er)

        row = block(assembled, "Amal")
        assert (row["undesired_days"], row["desired_days"]) == (["2026-06-03"], ["2026-06-25"])
        assert {"2026-06-03", "2026-06-25"} <= set(row["leave"])
        assert solver_refusal(assembled.input) is None

    def test_one_left_on_another_units_record_reaches_neither_unit(self, er, make_user):
        icu = make_unit("ICU")
        Unit = type(er)
        Unit.objects.filter(pk=icu.pk).update(rules=er.rules)
        icu.refresh_from_db()
        preference(er, "Amal", dt.date(2026, 6, 3))
        Placement.objects.create(nurse=NurseProfile.objects.get(user__username="Amal"), unit=icu,
                                 from_month=dt.date(2026, 6, 1))

        assert "Amal" not in {row["id"] for row in built(er).input["nurses"]}
        assert "undesired_days" not in block(assemble(icu, JUNE), "Amal")


class TestChangesSince:
    def test_nothing_changed(self, er):
        assert changes_since(built(er).input, er, JUNE) == []

    def test_gender_leave_and_who_is_in_the_month(self, er, make_user):
        solved = built(er).input
        amal = NurseProfile.objects.get(user__username="Amal")
        amal.gender = "M"
        amal.save()
        badr = NurseProfile.objects.get(user__username="Badr")
        leave_on(badr, dt.date(2026, 6, 9), by=badr.user)
        dana = NurseProfile.objects.get(user__username="Dana")
        dana.start_date = dt.date(2026, 7, 1)
        dana.save()
        nurse_in(er, make_user, "Omar", gender="M")

        assert changes_since(solved, er, JUNE) == [
            {"hospital_id": "Amal", "what": "gender"}, {"hospital_id": "Badr", "what": "leave"},
            {"hospital_id": "Dana", "what": "left"}, {"hospital_id": "Omar", "what": "joined"}]

    def test_a_start_date_counts_only_when_it_moves_days_of_this_month(self, er):
        solved = built(er).input
        amal = NurseProfile.objects.get(user__username="Amal")
        amal.start_date = dt.date(2026, 2, 1)
        amal.save()
        assert changes_since(solved, er, JUNE) == []

        amal.start_date = dt.date(2026, 6, 5)
        amal.save()
        assert changes_since(solved, er, JUNE) == [{"hospital_id": "Amal", "what": "leave"}]

    def test_a_preference_made_withdrawn_or_asked_instead_is_a_change_to_her_preferences(self, er):
        made = preference(er, "Amal", dt.date(2026, 6, 3))
        preference(er, "Badr", dt.date(2026, 6, 4), PreferenceKind.WORKING_DAY)
        solved = built(er).input
        assert changes_since(solved, er, JUNE) == []

        Preference.objects.filter(pk=made.pk).update(state=PreferenceState.WITHDRAWN, ended_at=dt.datetime.now(dt.UTC))
        preference(er, "Dana", dt.date(2026, 6, 6))

        assert changes_since(solved, er, JUNE) == [
            {"hospital_id": "Amal", "what": "preferences"}, {"hospital_id": "Dana", "what": "preferences"}]

    def test_an_input_stored_before_preferences_existed_changes_only_where_one_is_made(self, er):
        solved = built(er).input
        for row in solved["nurses"]:
            row.pop("undesired_days", None)
            row.pop("desired_days", None)
        assert changes_since(solved, er, JUNE) == []

        preference(er, "Emad", dt.date(2026, 6, 8), PreferenceKind.WORKING_DAY)

        assert changes_since(solved, er, JUNE) == [{"hospital_id": "Emad", "what": "preferences"}]


def a_skill(name, *, expires=True, required=False, active=True):
    from hospital.models import Skill
    return Skill.objects.create(name=name, expires=expires, required_to_work=required, active=active)


def holds(hospital_id, skill, expiry=None):
    from nurses.models import NurseSkill
    nurse = NurseProfile.objects.get(user__username=hospital_id)
    return NurseSkill.objects.create(nurse=nurse, skill=skill, expiry=expiry, recorded_by=nurse.user)


def ended(row):
    from django.utils import timezone
    row.removed_at, row.removed_by = timezone.now(), row.recorded_by
    row.save()


class TestSkills:
    """Each nurse's `skills` as the month sees them: the expiry when it falls in the month or before it, null
    when it is valid past the month or does not expire, and no entry where she has no record."""

    def test_each_expiry_is_clamped_to_the_month(self, er):
        bls, icu, pals = a_skill("BLS", required=True), a_skill("ICU"), a_skill("PALS", required=True)
        preceptor, acls = a_skill("Preceptor", expires=False), a_skill("ACLS")
        holds("Amal", bls, dt.date(2026, 6, 14))
        holds("Amal", icu, dt.date(2026, 7, 1))
        holds("Amal", pals, dt.date(2026, 5, 20))
        holds("Amal", preceptor)
        holds("Amal", acls, dt.date(2026, 6, 30))

        assembled = built(er)

        assert block(assembled, "Amal")["skills"] == {"ACLS": "2026-06-30", "BLS": "2026-06-14", "ICU": None,
                                                      "PALS": "2026-05-20", "Preceptor": None}
        assert "skills" not in block(assembled, "Badr")
        assert assembled.input["general"]["required_skills"] == ["BLS", "PALS"]
        assert solver_refusal(assembled.input) is None

    def test_a_kind_no_longer_expiring_sends_no_date_and_a_retired_skill_is_no_record(self, er):
        from hospital.models import Skill
        icu, old = a_skill("ICU"), a_skill("Old", required=True)
        holds("Amal", icu, dt.date(2026, 6, 10))
        holds("Amal", old, dt.date(2026, 5, 1))
        Skill.objects.filter(pk=icu.pk).update(expires=False)
        Skill.objects.filter(pk=old.pk).update(active=False)

        assert block(built(er), "Amal")["skills"] == {"ICU": None}

    def test_a_renewed_skill_sends_its_new_expiry_and_one_taken_off_is_gone(self, er):
        bls, icu = a_skill("BLS", required=True), a_skill("ICU")
        ended(holds("Amal", bls, dt.date(2026, 5, 31)))
        holds("Amal", bls, dt.date(2027, 5, 31))
        ended(holds("Badr", icu, dt.date(2027, 1, 1)))

        assembled = built(er)

        assert block(assembled, "Amal")["skills"] == {"BLS": None}
        assert "skills" not in block(assembled, "Badr")

    def test_the_solver_bars_her_from_the_day_after_and_counts_her_toward_a_need_before(self, er):
        from solver_service.grid import validate_grid
        bls = a_skill("BLS", required=True)
        holds("Amal", bls, dt.date(2026, 6, 14))
        er.rules |= {"skill_needs": [{"shift": "D", "skill": "BLS", "weekday_min": 1, "weekend_min": 1}],
                     "skill_needs_hard": True}
        er.save()
        roster = {row["id"]: {f"2026-06-{day:02d}": "D" if row["id"] == "Amal" else "" for day in range(1, 31)}
                  for row in built(er).input["nurses"]}

        answer = validate_grid(built(er).input, roster)

        expired = [row for row in answer["violations"] if row["rule"] == "skill_expired"]
        short = [row for row in answer["violations"] if row["rule"] == "skill_need"]
        assert [row["date"] for row in expired] == [f"2026-06-{day:02d}" for day in range(15, 31)]
        assert [row["date"] for row in short] == [f"2026-06-{day:02d}" for day in range(15, 31)]


class TestSkillChangesSince:
    """`skills` among a draft's changes since its solve: a nurse's skills as the month sees them, or the unit's skill rules."""

    def test_a_recording_a_renewal_and_a_removal_are_changes_to_her_skills(self, er):
        bls, icu = a_skill("BLS", required=True), a_skill("ICU")
        first = holds("Amal", bls, dt.date(2026, 6, 14))
        gone = holds("Dana", icu, dt.date(2027, 1, 1))
        solved = built(er).input
        assert changes_since(solved, er, JUNE) == []

        ended(first)
        holds("Amal", bls, dt.date(2026, 6, 20))
        holds("Badr", icu, dt.date(2027, 1, 1))
        ended(gone)

        assert changes_since(solved, er, JUNE) == [
            {"hospital_id": "Amal", "what": "skills"}, {"hospital_id": "Badr", "what": "skills"},
            {"hospital_id": "Dana", "what": "skills"}]

    def test_a_renewal_the_month_does_not_see_is_no_change(self, er):
        icu = a_skill("ICU")
        first = holds("Amal", icu, dt.date(2026, 8, 1))
        solved = built(er).input

        ended(first)
        holds("Amal", icu, dt.date(2027, 8, 1))

        assert changes_since(solved, er, JUNE) == []

    def test_a_rename_is_no_change(self, er, make_user):
        from hospital.models import Skill, SkillRename
        bls = a_skill("BLS", required=True)
        holds("Amal", bls, dt.date(2026, 6, 14))
        er.rules |= {"skill_needs": [{"shift": "D", "skill": "BLS", "weekday_min": 1, "weekend_min": 0}]}
        er.save()
        solved = built(er).input

        Skill.objects.filter(pk=bls.pk).update(name="Basic life support")
        SkillRename.objects.create(skill=bls, old_name="BLS", new_name="Basic life support",
                                   by=NurseProfile.objects.get(user__username="Amal").user)
        er.rules |= {"skill_needs": [{"shift": "D", "skill": "Basic life support", "weekday_min": 1, "weekend_min": 0}]}
        er.save()

        assert changes_since(solved, er, JUNE) == []

    @pytest.mark.parametrize("change", ["need", "flag", "required", "weight"])
    def test_the_units_skill_rules_changing_is_a_change_of_the_unit(self, er, change):
        from hospital.models import Skill
        bls = a_skill("BLS")
        solved = built(er).input
        if change == "need":
            er.rules |= {"skill_needs": [{"shift": "D", "skill": "BLS", "weekday_min": 1, "weekend_min": 0}]}
        elif change == "flag":
            er.rules |= {"skill_needs_hard": False}
        er.save()
        if change == "required":
            Skill.objects.filter(pk=bls.pk).update(required_to_work=True)
        if change == "weight":
            settings = HospitalSettings.load()
            settings.rules["weights"]["skill_shortfall"] = 9
            settings.save()

        assert changes_since(solved, er, JUNE) == [{"hospital_id": "", "what": "skills"}]

    def test_an_input_stored_in_version_1_changes_only_where_the_month_now_has_skills(self, er):
        bls = a_skill("BLS")
        solved = built(er).input
        solved["version"] = 1
        for key in ("skill_needs", "skill_needs_hard"):
            del solved["department"][key]
        del solved["general"]["required_skills"], solved["general"]["weights"]["skill_shortfall"]
        assert solver_refusal(solved) is None
        assert changes_since(solved, er, JUNE) == []

        holds("Badr", bls, dt.date(2027, 1, 1))

        assert changes_since(solved, er, JUNE) == [{"hospital_id": "Badr", "what": "skills"}]


class TestCertificatesAndLicences:
    """A record's kind, issuing body, identifier and issue date are the catalogue's and the CNO's business, not the
    solver's. The four-nurse sample's input, a re-plan's and a close's alike, is byte for byte what it was."""

    def test_the_input_is_unchanged_by_kind_issuing_body_identifier_and_issue_date(self, er):
        from hospital.models import Skill
        from nurses.models import NurseSkill
        from nurses.skills import record_anew
        licence, degree = a_skill("SCFHS", required=True), a_skill("BSN", expires=False, required=True)
        for number, hospital_id in enumerate(["Amal", "Badr", "Dana", "Emad"]):
            holds(hospital_id, licence, dt.date(2026, 6, 10 + number))
            holds(hospital_id, degree)

        def inputs() -> list[str]:
            return [json.dumps(assembled.input, sort_keys=True)
                    for assembled in (built(er), assemble(er, JUNE, time_limit=20, facts=True),
                                      assemble_replan(er, JUNE, dt.date(2026, 6, 15), {}, time_limit=20))]

        before = inputs()
        Skill.objects.filter(pk=licence.pk).update(kind=Skill.Kind.LICENCE,
                                                   issuing_body="Saudi Commission for Health Specialties")
        Skill.objects.filter(pk=degree.pk).update(kind=Skill.Kind.CERTIFICATE, issuing_body="King Saud University")
        for number, row in enumerate(NurseSkill.objects.current().select_related("nurse", "skill", "recorded_by")):
            record_anew(row.nurse, row.skill, row.recorded_by, expiry=row.expiry, identifier=f"{row.skill.name}-{number}",
                        issue_date=dt.date(2024, 6, 1), ending=row)

        assert inputs() == before
        assert '"SCFHS-' not in before[0] and "2024-06-01" not in inputs()[0]
