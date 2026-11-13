"""Two results of one month side by side, through the solve history API with the scripted solver and the real job.
Each result is costed on its own input by the solver's own code, worked out here on what is stored."""
import datetime as dt

import pytest

from accounts.models import Role, User
from audit.models import AuditEntry
from hospital.models import HospitalSettings, PublicHoliday, Shift
from nurses.models import NurseProfile
from nurses.tests.conftest import make_unit
from rosters.models import PinnedCell
from solves.models import Solve
from solves.tests.conftest import DATES, MONTH, NURSES, SAMPLE, job, nurse_in, result_of, roster_of
from solves.tests.test_history import accounting, finished, sick_day, solved, url

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _scene(before_june, solver):
    pass


def compare(client, unit, a, b):
    return client.get(url(unit, "/solve-history/compare"), {"a": a, "b": b})


def compared(client, unit, a, b) -> dict:
    response = compare(client, unit, a, b)
    assert response.status_code == 200, response.content
    return response.json()


def answer_for(nurses, shift="D", *, cost=7, penalties=None, **cells) -> dict:
    """A finished solve whose roster has these nurses on `shift` all month, with `Amal_3="N"` style exceptions."""
    roster = roster_of(nurses, shift)
    for key, code in cells.items():
        nurse, day = key.split("_")
        roster[nurse][f"{MONTH}-{int(day):02d}"] = code
    result = result_of(roster, cost=cost)
    result["nurses"] = accounting(nurses)
    if penalties is not None:
        result["penalties"] = penalties
    return job("finished", result=result, best_cost=cost, improvements=3)


def set_weight(name, value):
    settings = HospitalSettings.load()
    settings.rules["weights"][name] = value
    settings.save()


def changes(answer, group=None) -> list[tuple]:
    return [(row["group"], row["field"], row["older"], row["newer"]) for row in answer["inputs"]["changes"]
            if group is None or row["group"] == group]


def kinds(answer) -> dict[str, list[str]]:
    return {row["hospital_id"]: row["kinds"] for row in answer["inputs"]["nurses"]}


class TestThePair:
    def test_the_older_is_named_first_whichever_way_they_are_asked_for(self, mona, er, solver):
        first = solved(mona, er, solver, finished(cost=7))
        second = solved(mona, er, solver, finished("N", cost=9))

        for a, b in ((first, second), (second, first)):
            answer = compared(mona, er, a, b)
            assert answer["month"] == MONTH
            assert (answer["older"]["id"], answer["newer"]["id"]) == (first, second)
            assert (answer["older"]["cost"], answer["newer"]["cost"]) == (7, 9)
        assert answer["older"]["in_draft"] is False and answer["newer"]["in_draft"] is True
        assert answer["older"]["requested_by"] == "Mona Ali" and answer["older"]["unit_cost"] == 0
        assert answer["older"]["statistics"]["stopped_by"] == "proof" and answer["older"]["replan_of"] is None

    def test_the_same_month_given_twice_has_no_input_change(self, mona, er, solver):
        answer = compared(mona, er, solved(mona, er, solver), solved(mona, er, solver))

        assert answer["inputs"] == {"changes": [], "nurses": []}
        assert answer["cells"] == [] and (answer["explained_older"], answer["explained_newer"]) == (True, True)

    def test_nothing_is_written_to_the_audit_log(self, mona, er, solver):
        first, second = solved(mona, er, solver), solved(mona, er, solver)
        entries = AuditEntry.objects.count()

        compared(mona, er, first, second)

        assert AuditEntry.objects.count() == entries


class TestRefusals:
    def test_the_same_solve_twice(self, mona, er, solver):
        solve = solved(mona, er, solver)

        response = compare(mona, er, solve, solve)

        assert response.status_code == 400 and response.json()["detail"] == "Choose two different results to compare."

    def test_two_months(self, mona, er, solver):
        june = solved(mona, er, solver)
        july = solved(mona, er, solver, month="2026-07")

        response = compare(mona, er, june, july)

        assert response.status_code == 400 and response.json()["detail"] == "Only two results of the same month compare."

    @pytest.mark.parametrize("answer", ["failed", "nothing", "broken"])
    def test_a_result_with_no_roster_to_keep(self, answer, mona, er, solver):
        kept = solved(mona, er, solver)
        if answer == "failed":
            solver.script(RuntimeError("down"), RuntimeError("down"))
            from solves import running
            from solves.tests.conftest import started
            other = started(mona, er)["id"]
            running.run(other)
        else:
            other = solved(mona, er, solver, {"nothing": job("finished", result=result_of(None, ok=False, cost=None)),
                                              "broken": finished(ok=False)}[answer])

        response = compare(mona, er, kept, other)

        assert response.status_code == 400 and response.json()["detail"] == "Only results that hold a roster compare."

    def test_another_units_solve_is_not_found_and_another_units_manager_is_refused(self, mona, er, solver, client_of, make_user):
        first, second = solved(mona, er, solver), solved(mona, er, solver)
        omar = client_of("omar", Role.UNIT_MANAGER)
        icu = make_unit("ICU", User.objects.get(username="omar"))
        nurse_in(icu, make_user, "Zaki")

        assert compare(omar, icu, first, second).status_code == 404
        assert compare(omar, er, first, second).status_code == 403
        assert compare(mona, er, first, "0190e9a0-0000-7000-8000-000000000000").status_code == 404

    @pytest.mark.parametrize("role", [Role.STAFF_NURSE, Role.SYSTEM_ADMIN])
    def test_no_other_role(self, role, mona, er, solver, client_of):
        first, second = solved(mona, er, solver), solved(mona, er, solver)

        assert compare(client_of("someone", role), er, first, second).status_code == 403

    @pytest.mark.parametrize("role", [Role.CNO, Role.DEPUTY_CNO])
    def test_not_the_cno_or_the_deputy_even_where_they_read_each_result(self, role, mona, er, solver, client_of):
        from conftest import post
        first, second = solved(mona, er, solver), solved(mona, er, solver)
        from rosters.tests.test_review import sound
        solver.script([sound()])
        from solves import running
        from solves.tests.conftest import started
        third = started(mona, er)["id"]
        running.run(third)
        assert post(mona, url(er, "/roster/submit"), {"month": MONTH}).status_code == 200
        someone = client_of("someone", role)
        assert someone.get(url(er, f"/solve-history/{third}")).status_code == 200

        assert compare(someone, er, first, third).status_code == 403


class TestTheInputs:
    def test_a_weight_and_a_hospital_rule_in_the_audit_logs_words(self, mona, er, solver):
        first = solved(mona, er, solver)
        set_weight("night_rest", 8)
        settings = HospitalSettings.load()
        settings.rules["overtime_month_cap"] = 30
        settings.save()
        second = solved(mona, er, solver)
        was = Solve.objects.get(pk=first).input["general"]

        answer = compared(mona, er, second, first)

        assert changes(answer) == [("hospital_rules", "overtime_month_cap", was["overtime_month_cap"], 30),
                                   ("hospital_rules", "weights.night_rest", was["weights"]["night_rest"], 8)]

    def test_a_unit_rule_field_by_field(self, mona, er, solver):
        first = solved(mona, er, solver)
        was = er.rules["max_consecutive_shifts"]
        er.rules |= {"max_consecutive_shifts": was + 1}
        er.save()
        second = solved(mona, er, solver)

        assert changes(compared(mona, er, first, second)) == [("unit_rules", "max_consecutive_shifts", was, was + 1)]

    def test_the_time_limit(self, mona, er, solver):
        from solves import running
        from solves.tests.conftest import started
        first = solved(mona, er, solver)
        solver.script([finished()])
        second = started(mona, er, seconds=120)["id"]
        running.run(second)

        assert changes(compared(mona, er, first, second)) == [("time_limit", "time_limit_seconds", 60, 120)]

    def test_a_public_holiday_and_the_ramadan_days(self, mona, er, solver):
        from hospital.models import FixedRamadanMonth
        first = solved(mona, er, solver)
        PublicHoliday.objects.create(date=dt.date(2026, 6, 10), name="A holiday")
        FixedRamadanMonth.objects.filter(month=MONTH).update(dates=["2026-06-01"])
        second = solved(mona, er, solver)

        assert changes(compared(mona, er, first, second)) == [("calendar", "ramadan_days", [], ["2026-06-01"]),
                                                              ("calendar", "public_holidays", [], ["2026-06-10"])]

    def test_a_nurses_leave_and_a_pin_are_named_by_kind_and_nothing_more(self, mona, er, solver):
        first = solved(mona, er, solver)
        sick_day("Badr", 9)
        answer = finished()
        answer["result"]["roster"]["Badr"]["2026-06-09"] = ""
        amal = NurseProfile.objects.get(user__username="Amal")
        PinnedCell.objects.create(unit=er, nurse=amal, date=dt.date(2026, 6, 4), shift=Shift.objects.get(code="D"),
                                  pinned_by=amal.user)
        second = solved(mona, er, solver, answer)

        result = compared(mona, er, first, second)

        assert result["inputs"]["nurses"] == [{"hospital_id": "Amal", "full_name": "Amal", "kinds": ["pins"]},
                                              {"hospital_id": "Badr", "full_name": "Badr", "kinds": ["leave"]}]
        assert changes(result) == []
        assert "2026-06-09" not in str(result["inputs"]) and "2026-06-04" not in str(result["inputs"])

    def test_her_attributes_her_carry_over_her_preferences_and_her_skills(self, mona, er, solver):
        from hospital.models import Skill
        from requesting.models import PreferenceKind
        from solves.tests.test_input import preference
        first = solved(mona, er, solver)
        NurseProfile.objects.filter(user__username="Amal").update(score=0.1)
        NurseProfile.objects.filter(user__username="Badr").update(hours_balance=-12)
        preference(er, "Dana", dt.date(2026, 6, 6), PreferenceKind.DAY_OFF)
        emad = NurseProfile.objects.get(user__username="Emad")
        emad.skills.create(skill=Skill.objects.create(name="ICU", expires=False, required_to_work=False), recorded_by=emad.user)
        second = solved(mona, er, solver)

        assert kinds(compared(mona, er, first, second)) == {"Amal": ["attributes"], "Badr": ["carry_over"],
                                                            "Dana": ["preferences"], "Emad": ["skills"]}

    def test_a_nurse_who_joined_and_one_who_left(self, mona, er, solver, make_user):
        first = solved(mona, er, solver)
        faris = nurse_in(er, make_user, "Faris", full_name="Faris Haddad")
        second = solved(mona, er, solver, answer_for([*NURSES, "Faris"]))
        faris.end_date = dt.date(2026, 5, 31)
        faris.save()
        third = solved(mona, er, solver)

        joined, left = compared(mona, er, first, second), compared(mona, er, second, third)

        assert joined["inputs"]["nurses"] == [{"hospital_id": "Faris", "full_name": "Faris Haddad", "kinds": ["joined"]}]
        assert left["inputs"]["nurses"] == [{"hospital_id": "Faris", "full_name": "Faris Haddad", "kinds": ["left"]}]
        assert joined["cells"] == [] and left["cells"] == []
        faris_hours = next(row for row in joined["nurses"] if row["hospital_id"] == "Faris")
        assert faris_hours["older"] is None and faris_hours["newer"]["contract"] == 192
        assert [row["full_name"] for row in joined["nurses"]] == ["Amal", "Badr", "Dana", "Emad", "Faris Haddad"]

    def test_a_rename_between_the_two_is_no_change_in_the_cells_or_the_rules(self, mona, er, solver, client_of):
        from hospital.models import Skill
        Skill.objects.create(name="ICU", expires=False, required_to_work=False)
        er.rules |= {"skill_needs": [{"shift": "D", "skill": "ICU", "weekday_min": 0, "weekend_min": 0}]}
        er.save()
        first = solved(mona, er, solver)
        cno = client_of("carla", Role.CNO)
        shift = Shift.objects.get(code="D")
        response = cno.put(f"/api/hospital/shifts/{shift.pk}", {"code": "DD", "display_name": shift.display_name,
                                                                "start_time": f"{shift.start_time:%H:%M}",
                                                                "end_time": f"{shift.end_time:%H:%M}",
                                                                "hours": shift.hours, "colour": shift.colour, "active": True},
                           content_type="application/json")
        assert response.status_code == 200, response.content
        er.refresh_from_db()
        assert "DD" in er.rules["shifts"] and er.rules["skill_needs"][0]["shift"] == "DD"
        second = solved(mona, er, solver, answer_for(NURSES, "DD"))

        answer = compared(mona, er, first, second)

        assert answer["cells"] == [] and answer["inputs"] == {"changes": [], "nurses": []}

    def test_a_version_1_input_compares_with_a_version_2_one_naming_what_only_one_has(self, mona, er, solver):
        first = solved(mona, er, solver)
        stored = Solve.objects.get(pk=first)
        stored.input["version"] = 1
        for key in ("skill_needs", "skill_needs_hard"):
            del stored.input["department"][key]
        del stored.input["general"]["required_skills"], stored.input["general"]["weights"]["skill_shortfall"]
        stored.save()
        second = solved(mona, er, solver)
        now = Solve.objects.get(pk=second).input

        answer = compared(mona, er, first, second)

        assert sorted(changes(answer), key=str) == sorted([
            ("hospital_rules", "required_skills", None, now["general"]["required_skills"]),
            ("hospital_rules", "weights.skill_shortfall", None, now["general"]["weights"]["skill_shortfall"]),
            ("unit_rules", "skill_needs", None, now["department"]["skill_needs"]),
            ("unit_rules", "skill_needs_hard", None, now["department"]["skill_needs_hard"])], key=str)
        assert answer["explained_older"] is True and answer["inputs"]["nurses"] == []


class TestTheRules:
    def test_each_side_on_its_own_input_with_a_differing_weight_marked(self, mona, er, solver):
        first = solved(mona, er, solver, answer_for(NURSES, penalties={"fair_day": 5, "night_rest": 2, "fair_night": 0}))
        set_weight("fair_day", 3)
        second = solved(mona, er, solver, answer_for(NURSES, penalties={"fair_day": 15, "night_rest": 2, "fair_night": 1}))
        weights = SAMPLE["general"]["weights"]  # The hospital rules the older was solved on, as the sample has them.

        rules = compared(mona, er, second, first)["rules"]

        assert [row["rule"] for row in rules] == ["fair_day", "fair_night", "night_rest"]
        fair = rules[0]
        assert (fair["older"]["weight"], fair["older"]["penalty"]) == (weights["fair_day"], 5)
        assert (fair["newer"]["weight"], fair["newer"]["penalty"]) == (3, 15)
        assert fair["older"]["amount"] == fair["newer"]["amount"] > 0, "the same roster on the same unit rules breaks it as much"
        assert fair["weight_differs"] is True and rules[1]["weight_differs"] is False
        assert rules[1]["older"]["penalty"] == 0 and rules[1]["older"]["weight"] == weights["fair_night"]
        assert "change" not in [row["rule"] for row in rules], "nothing costs it in either"

    def test_one_side_the_solvers_code_cannot_explain_keeps_its_stored_penalties(self, mona, er, solver):
        first, second = solved(mona, er, solver), solved(mona, er, solver)
        stored = Solve.objects.get(pk=first)
        stored.input["nurses"][0]["gender"] = "neither"
        stored.save()

        answer = compared(mona, er, first, second)

        assert (answer["explained_older"], answer["explained_newer"]) == (False, True)
        fair = next(row for row in answer["rules"] if row["rule"] == "fair_day")
        assert fair["older"] == {"amount": None, "weight": None, "penalty": 5} and fair["newer"]["weight"] is not None
        assert fair["weight_differs"] is False


class TestTheCells:
    def test_the_cells_that_differ_by_name_then_date_under_todays_codes(self, mona, er, solver, make_user):
        first = solved(mona, er, solver)
        nurse_in(er, make_user, "Faris")
        second = solved(mona, er, solver, answer_for([*NURSES, "Faris"], Badr_3="N", Amal_9="", Amal_2="N", Faris_1=""))

        cells = compared(mona, er, first, second)["cells"]

        assert [(row["hospital_id"], row["date"], row["older"], row["newer"]) for row in cells] == [
            ("Amal", "2026-06-02", "D", "N"), ("Amal", "2026-06-09", "D", ""), ("Badr", "2026-06-03", "D", "N")]
        assert cells[0]["full_name"] == "Amal"


class TestHoursViolationsAndFacts:
    def test_each_nurses_hours_on_both_sides(self, mona, er, solver):
        first = solved(mona, er, solver)
        answer = finished()
        answer["result"]["nurses"]["Amal"] |= {"hours_worked": 168, "overtime": 0}
        second = solved(mona, er, solver, answer)

        amal = compared(mona, er, first, second)["nurses"][0]

        assert amal["hospital_id"] == "Amal" and amal["older"]["hours_worked"] == 180 and amal["newer"]["hours_worked"] == 168
        assert amal["newer"]["shifts"] == {"D": 15, "N": 0} and amal["older"]["cost"] == 3

    def test_violations_and_facts_in_three_groups(self, mona, er, solver):
        def broken(*rows):
            return [{"rule": rule, "nurse": nurse, "date": date, "text": f"{rule}: {nurse} at day 4"} for rule, nurse, date in rows]

        older = finished()
        older["result"]["violations"] = broken(("headcount", None, None), ("pin", "Amal", "2026-06-05"))
        older["result"]["elapsed_violations"] = broken(("consecutive", "Badr", "2026-06-02"), ("rest", "Dana", "2026-06-03"))
        newer = finished()
        newer["result"]["violations"] = broken(("headcount", None, None), ("headcount", None, None))
        newer["result"]["elapsed_violations"] = broken(("consecutive", "Badr", "2026-06-02"), ("rest", "Emad", "2026-06-03"))
        first, second = solved(mona, er, solver, older), solved(mona, er, solver, newer)

        answer = compared(mona, er, first, second)

        def keys(rows):
            return [(row["rule"], row["hospital_id"], row["date"]) for row in rows]
        violations, facts = answer["violations"], answer["facts"]
        assert keys(violations["both"]) == [("headcount", None, None)]
        assert keys(violations["only_older"]) == [("pin", "Amal", "2026-06-05")]
        assert keys(violations["only_newer"]) == [("headcount", None, None)]
        assert keys(facts["both"]) == [("consecutive", "Badr", "2026-06-02")]
        assert keys(facts["only_older"]) == [("rest", "Dana", "2026-06-03")]
        assert keys(facts["only_newer"]) == [("rest", "Emad", "2026-06-03")]
        assert facts["only_newer"][0]["text"] == "rest: Emad at 2026-06-05" and facts["only_newer"][0]["full_name"] == "Emad"


class TestARePlanOnOneSide:
    @pytest.fixture
    def carla(self, client_of):
        return client_of("carla", Role.CNO, full_name="Carla Diaz")

    def test_its_facts_name_the_date_and_version_and_its_change_rule_has_the_plain_side_empty(self, mona, carla, er, solver,
                                                                                            monkeypatch):
        from nurses import membership
        from rosters.tests.test_replan import SWAPPED, answered, replanned
        from rosters.tests.test_review import approved
        plain = approved(mona, carla, er, solver)
        monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 6, 12))
        again = answered(SWAPPED)
        again["result"]["penalties"] = {"fair_day": 5, "change": 6}
        replan = replanned(mona, er, solver, again)

        answer = compared(mona, er, replan.pk, plain)

        assert (answer["older"]["id"], answer["newer"]["id"]) == (plain, str(replan.pk))
        assert answer["newer"]["replan_from"] == "2026-06-12" and answer["newer"]["replan_of"] == 1
        assert answer["older"]["replan_from"] is None
        change = next(row for row in answer["rules"] if row["rule"] == "change")
        assert change["older"] == {"amount": None, "weight": None, "penalty": None}
        assert change["newer"]["penalty"] == 6 and change["newer"]["weight"] == SAMPLE["general"]["weights"]["change"]
        assert all(row["group"] != "replan" and "published" not in row["field"] for row in answer["inputs"]["changes"])
        assert [(row["hospital_id"], row["date"]) for row in answer["cells"]] == [("Amal", "2026-06-15"), ("Emad", "2026-06-15")]
        assert DATES[0] not in str(answer["inputs"])
