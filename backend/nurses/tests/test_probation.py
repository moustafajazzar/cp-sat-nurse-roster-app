"""A nurse's probation: the CNO's tick on the form, in the import and as a correction; the month counting; her unit
manager's length, extension and end on her Rostering attributes row; the night shift alone refused; the end by itself; the
notification at the start of her last month; a transfer; the staff list; and the audit entries. Every last day below was counted by
hand on a calendar, by the rule: whole calendar months from her start date, the month she joins counting only when
she joins on the 1st."""
import csv
import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from accounts.models import Role, User
from audit.models import AuditEntry
from conftest import post
from hospital.catalogue import seed_catalogue
from nurses.models import NurseProfile
from nurses.tasks import tell_probations_ending
from nurses.tests.conftest import NURSES, added, make_unit
from rosters.models import Roster, RosterState
from solves.models import Solve

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def catalogue(db):
    seed_catalogue()


@pytest.fixture(autouse=True)
def today(on_day):
    on_day(2026, 11, 11)
    return on_day


@pytest.fixture
def mona(client_of):
    return client_of("mona", Role.UNIT_MANAGER)


@pytest.fixture
def er(mona):
    return make_unit("ER", User.objects.get(username="mona"))


@pytest.fixture
def omar(client_of):
    return client_of("omar", Role.UNIT_MANAGER)


@pytest.fixture
def icu(omar):
    return make_unit("ICU", User.objects.get(username="omar"))


def on_probation(cno, er, start="2026-12-15", hospital_id="h1", **fields) -> str:
    return added(cno, er, hospital_id, full_name="Layan Alharbi", start_date=start, on_probation=True, **fields)["nurse"]["id"]


def row(client, unit, nurse_id) -> dict:
    response = client.get(f"/api/units/{unit.pk}/nurses")
    assert response.status_code == 200, response.content
    return next(nurse for nurse in response.json() if nurse["id"] == nurse_id)


def read(nurse_row) -> dict | None:
    """Her probation as the row reads it, without what each action would give."""
    probation = nurse_row["probation"]
    return probation and {key: probation[key] for key in ("last_day", "months", "ticked", "ended_early")}


def last_day(client, unit, nurse_id) -> str | None:
    probation = row(client, unit, nurse_id)["probation"]
    return probation and probation["last_day"]


def correct(cno, nurse_id, **fields):
    nurse = NurseProfile.objects.get(pk=nurse_id)
    body = {"gender": nurse.gender, "start_date": nurse.start_date.isoformat()} | fields
    return cno.put(f"{NURSES}/{nurse_id}/details", body, content_type="application/json")


def act(client, unit, nurse_id, action, **body):
    path = f"/api/units/{unit.pk}/nurses/{nurse_id}/probation/{action}"
    if action == "length":
        return client.put(path, body, content_type="application/json")
    return post(client, path, body)


def ok(response) -> dict:
    assert response.status_code == 200, response.content
    return response.json()


def refusal(response, field="probation") -> str:
    assert response.status_code == 422, response.content
    detail = response.json()["detail"][0]
    assert detail["loc"][2:] == [field], detail
    return detail["msg"].removeprefix("Value error, ")


def approve(unit, month):
    """An approved roster for a unit's month, as the approval leaves it: here only its state matters."""
    solve = Solve.objects.create(unit=unit, month=month, requested_by=User.objects.get(username="mona"), input={},
                                 time_limit_seconds=60)
    Roster.objects.create(unit=unit, month=month, state=RosterState.APPROVED, grid={}, solve=solve)


def entries(action):
    return list(AuditEntry.objects.filter(action=action).order_by("id").values("actor__username", "target", "before", "after"))


class TestTheCounting:
    @pytest.mark.parametrize(("start", "months", "last"), [
        ("2026-12-01", 3, "2027-02-28"),  # the 1st: December counts
        ("2026-12-15", 3, "2027-03-31"),  # after the 1st: December does not
        ("2026-11-12", 1, "2026-12-31"),
        ("2027-01-31", 1, "2027-02-28"),  # February
        ("2027-12-20", 2, "2028-02-29"),  # February of a leap year
        ("2026-11-20", 2, "2027-01-31"),  # across a year's end
    ])
    def test_the_last_day_is_counted_in_whole_months_from_her_start_date(self, cno, mona, er, start, months, last):
        nurse = on_probation(cno, er, start)
        if months != 3:
            ok(act(mona, er, nurse, "length", months=months))
        assert last_day(mona, er, nurse) == last

    def test_it_stands_at_three_months_until_her_unit_manager_chooses(self, cno, mona, er):
        nurse = on_probation(cno, er, "2026-12-15")
        assert read(row(mona, er, nurse)) == {"last_day": "2027-03-31", "months": 3, "ticked": True, "ended_early": False}

    def test_a_corrected_start_date_moves_it(self, cno, mona, er):
        nurse = on_probation(cno, er, "2026-12-15")
        assert correct(cno, nurse, start_date="2026-12-01").status_code == 200
        assert last_day(mona, er, nurse) == "2027-02-28"
        assert correct(cno, nurse, start_date="2027-01-02").status_code == 200
        assert last_day(mona, er, nurse) == "2027-04-30"


class TestTheCnosTick:
    def test_on_the_form(self, cno, er):
        nurse = on_probation(cno, er, "2026-12-15")
        assert next(n for n in cno.get(NURSES).json() if n["id"] == nurse)["probation"]["last_day"] == "2027-03-31"
        assert entries("nurse.probation_ticked") == [{"actor__username": "chief", "target": "h1", "before": None,
                                                     "after": {"on_probation": True, "months": 3, "ended_early": False,
                                                               "last_day": "2027-03-31"}}]

    def test_left_out_it_is_no(self, cno, mona, er):
        nurse = added(cno, er)["nurse"]["id"]
        assert row(mona, er, nurse)["probation"] is None
        assert entries("nurse.probation_ticked") == []

    def test_refused_for_a_unit_manager_with_a_nurse_profile(self, cno, mona, er):
        mona_id = str(User.objects.get(username="mona").pk)
        response = post(cno, f"{NURSES}/managers", {"user_id": mona_id, "gender": "F", "unit_id": str(er.pk),
                                                    "start_date": "2026-12-01", "on_probation": True})
        assert refusal(response, "on_probation") == "Only a staff nurse can be on probation, not a unit manager with a nurse profile."
        response = post(cno, f"{NURSES}/managers", {"user_id": mona_id, "gender": "F", "unit_id": str(er.pk),
                                                    "start_date": "2026-12-01"})
        assert response.status_code == 201
        assert refusal(correct(cno, response.json()["id"], on_probation=True), "on_probation").startswith("Only a staff nurse")

    def test_ticked_later_within_six_months_of_her_start_date(self, cno, mona, er):
        # From 10 May 2026, six months are June to November: until 30 November.
        nurse = added(cno, er, start_date="2026-05-10")["nurse"]["id"]
        assert correct(cno, nurse, on_probation=True).status_code == 200
        assert entries("nurse.probation_ticked")[0]["before"] == {"on_probation": False, "months": 3, "ended_early": False,
                                                                 "last_day": None}
        # Its three months, June to August, are over, so she is not on probation now.
        assert entries("nurse.probation_ticked")[0]["after"]["last_day"] == "2026-08-31"
        assert row(mona, er, nurse)["probation"] is None
        recent = added(cno, er, "h2", start_date="2026-09-10")["nurse"]["id"]
        assert correct(cno, recent, on_probation=True).status_code == 200
        assert last_day(mona, er, recent) == "2026-12-31"

    @pytest.mark.parametrize("start", ["2026-04-10", "2026-05-01"])  # six months end on 31 October, before 11 November
    def test_refused_once_six_months_from_her_start_date_have_passed(self, cno, er, start):
        nurse = added(cno, er, start_date=start)["nurse"]["id"]
        assert refusal(correct(cno, nurse, on_probation=True), "on_probation").endswith(
            "more than six months ago, so she can no longer be put on probation.")
        assert entries("nurse.probation_ticked") == []

    def test_unticked_with_no_approved_month_it_is_gone(self, cno, mona, er):
        nurse = on_probation(cno, er, "2026-12-15")
        assert correct(cno, nurse, on_probation=False).status_code == 200
        assert row(mona, er, nurse)["probation"] is None
        assert entries("nurse.probation_unticked")[0]["after"] == {"on_probation": False, "months": 3, "ended_early": False,
                                                                  "last_day": None}

    def test_unticked_it_goes_from_the_first_month_whose_roster_is_not_approved(self, cno, mona, er):
        nurse = on_probation(cno, er, "2026-12-15")
        approve(er, "2026-12-01")
        approve(er, "2027-02-01")  # January is not approved, so February is not kept either
        assert correct(cno, nurse, on_probation=False).status_code == 200
        assert read(row(mona, er, nurse)) == {"last_day": "2026-12-31", "months": 3, "ticked": False, "ended_early": False}
        assert entries("nurse.probation_unticked")[0]["after"]["last_day"] == "2026-12-31"

    def test_an_approved_roster_of_another_unit_does_not_keep_a_month(self, cno, mona, er, icu):
        nurse = on_probation(cno, er, "2026-12-15")
        approve(icu, "2026-12-01")
        assert correct(cno, nurse, on_probation=False).status_code == 200
        assert row(mona, er, nurse)["probation"] is None

    def test_unticked_with_approved_months_kept_her_unit_manager_changes_nothing(self, cno, mona, er):
        nurse = on_probation(cno, er, "2026-12-15")
        approve(er, "2026-12-01")
        assert correct(cno, nurse, on_probation=False).status_code == 200
        probation = row(mona, er, nurse)["probation"]
        assert (probation["lengths"], probation["if_extended"], probation["if_ended"]) == ([], None, None)
        for action, body in (("extend", {}), ("end", {}), ("length", {"months": 1})):
            assert refusal(act(mona, er, nurse, action, **body)) == (
                "The CNO took Layan Alharbi off probation. It ends on 31 December 2026, with the months whose rosters were "
                "approved, and can no longer change.")
        assert entries("nurse.probation_extended") == entries("nurse.probation_ended") == []

    def test_the_tick_reads_as_stored_once_it_has_run_out_and_can_be_taken_off(self, cno, er, today):
        nurse = on_probation(cno, er, "2026-09-01")  # September to November
        today(2026, 12, 1)
        listed = next(n for n in cno.get(NURSES).json() if n["id"] == nurse)
        assert (listed["on_probation"], listed["probation"]) == (True, None)
        assert correct(cno, nurse, on_probation=False).status_code == 200
        assert next(n for n in cno.get(NURSES).json() if n["id"] == nurse)["on_probation"] is False

    def test_ticked_again_it_stands_at_three_months(self, cno, mona, er):
        nurse = on_probation(cno, er, "2026-12-15")
        ok(act(mona, er, nurse, "length", months=1))
        assert correct(cno, nurse, on_probation=False).status_code == 200
        assert correct(cno, nurse, on_probation=True).status_code == 200
        assert last_day(mona, er, nurse) == "2027-03-31"

    def test_refused_while_her_allowed_shifts_are_the_night_shift_alone(self, cno, mona, er):
        nurse = added(cno, er, start_date="2026-11-01")["nurse"]["id"]
        response = mona.put(f"/api/units/{er.pk}/nurses/{nurse}/allowed-shifts", {"allowed_shifts": ["N"]},
                            content_type="application/json")
        assert response.status_code == 200, response.content
        assert refusal(correct(cno, nurse, on_probation=True), "on_probation") == (
            "Amal Saleh may work only N in ER, its night shift, which a nurse on probation does not work. "
            "Her unit manager changes her allowed shifts first.")


class TestTheUnitManager:
    def test_the_row_says_what_each_action_would_make_her_last_day(self, cno, mona, er, today):
        nurse = on_probation(cno, er, "2026-09-01")  # September to November
        probation = row(mona, er, nurse)["probation"]
        assert (probation["lengths"], probation["if_extended"], probation["if_ended"]) == (
            [{"months": 3, "last_day": "2026-11-30"}], "2026-12-31", "2026-11-30")
        later = on_probation(cno, er, "2026-12-15", hospital_id="h2")
        probation = row(mona, er, later)["probation"]
        assert (probation["lengths"], probation["if_extended"], probation["if_ended"]) == (
            [{"months": 1, "last_day": "2027-01-31"}, {"months": 2, "last_day": "2027-02-28"},
             {"months": 3, "last_day": "2027-03-31"}], "2027-04-30", None)
        today(2026, 12, 20)
        ok(act(mona, er, later, "extend"))
        probation = row(mona, er, later)["probation"]
        assert (probation["lengths"], probation["if_extended"], probation["if_ended"]) == ([], "2027-05-31", "2026-12-31")
        ok(act(mona, er, later, "end"))
        probation = row(mona, er, later)["probation"]
        assert (probation["lengths"], probation["if_extended"], probation["if_ended"]) == ([], None, None)

    def test_the_length_one_to_three_at_first(self, cno, mona, er):
        nurse = on_probation(cno, er, "2026-12-15")
        assert ok(act(mona, er, nurse, "length", months=2))["probation"]["last_day"] == "2027-02-28"
        for months in (0, 4):
            assert act(mona, er, nurse, "length", months=months).status_code == 422
        assert entries("nurse.probation_length_set") == [
            {"actor__username": "mona", "target": "h1",
             "before": {"on_probation": True, "months": 3, "ended_early": False, "last_day": "2027-03-31"},
             "after": {"on_probation": True, "months": 2, "ended_early": False, "last_day": "2027-02-28"}}]

    def test_a_length_that_would_have_ended_it_before_this_month_is_refused(self, cno, mona, er):
        nurse = on_probation(cno, er, "2026-09-01")  # September to November
        assert refusal(act(mona, er, nurse, "length", months=2), "months") == (
            "2 months would have ended Layan Alharbi's probation on 31 October 2026, before this month. "
            "End it instead, at the end of this month.")

    def test_extended_a_month_at_a_time_to_six(self, cno, mona, er):
        nurse = on_probation(cno, er, "2026-12-15")
        lasts = [ok(act(mona, er, nurse, "extend"))["probation"]["last_day"] for _ in range(3)]
        assert lasts == ["2027-04-30", "2027-05-31", "2027-06-30"]
        assert refusal(act(mona, er, nurse, "extend")) == "Layan Alharbi's probation is 6 months, the longest it can be."
        assert refusal(act(mona, er, nurse, "length", months=3), "months") == (
            "Layan Alharbi's probation is extended to 6 months. Extend it again or end it.")
        assert [entry["after"]["months"] for entry in entries("nurse.probation_extended")] == [4, 5, 6]
        assert entries("nurse.probation_extended")[0]["before"]["last_day"] == "2027-03-31"

    def test_end_makes_its_last_day_the_last_of_the_month_under_way(self, cno, mona, er, today):
        nurse = on_probation(cno, er, "2026-09-01")
        today(2026, 10, 20)
        assert read(ok(act(mona, er, nurse, "end"))) == {"last_day": "2026-10-31", "months": 3, "ticked": True,
                                                                 "ended_early": True}
        assert entries("nurse.probation_ended")[0]["after"] == {"on_probation": True, "months": 3, "ended_early": True,
                                                               "last_day": "2026-10-31"}
        assert refusal(act(mona, er, nurse, "extend")) == (
            "Layan Alharbi's probation was ended early, on 31 October 2026, so it can no longer change.")
        assert refusal(act(mona, er, nurse, "length", months=3)).endswith("so it can no longer change.")

    def test_end_on_a_months_last_day_ends_it_that_day(self, cno, mona, er, today):
        nurse = on_probation(cno, er, "2026-09-01")
        today(2026, 10, 31)
        assert ok(act(mona, er, nurse, "end"))["probation"]["last_day"] == "2026-10-31"
        today(2026, 11, 1)
        assert row(mona, er, nurse)["probation"] is None

    def test_end_in_the_month_she_joined_after_the_1st(self, cno, mona, er, today):
        nurse = on_probation(cno, er, "2026-11-05")
        today(2026, 11, 20)
        assert ok(act(mona, er, nurse, "end"))["probation"]["last_day"] == "2026-11-30"

    def test_end_before_the_month_she_starts_in_is_refused(self, cno, mona, er):
        nurse = on_probation(cno, er, "2026-12-15")
        assert refusal(act(mona, er, nurse, "end")) == (
            "Layan Alharbi's probation begins on her start date, 15 December 2026. "
            "If she is not to be on probation, the CNO unticks it.")

    def test_a_nurse_not_on_probation_has_none_to_change(self, cno, mona, er):
        nurse = added(cno, er)["nurse"]["id"]
        for action in ("extend", "end"):
            assert refusal(act(mona, er, nurse, action)) == "Amal Saleh is not on probation."

    def test_the_cno_and_the_deputy_read_the_row_and_change_nothing(self, cno, client_of, er):
        nurse = on_probation(cno, er)
        deputy = client_of("dana", Role.DEPUTY_CNO)
        for reader in (cno, deputy):
            assert last_day(reader, er, nurse) == "2027-03-31"
            assert act(reader, er, nurse, "extend").status_code == 403
            assert act(reader, er, nurse, "length", months=1).status_code == 403

    def test_the_system_admin_reads_none_of_it(self, cno, client_of, er):
        on_probation(cno, er)
        admin = client_of("root", Role.SYSTEM_ADMIN)
        assert admin.get(f"/api/units/{er.pk}/nurses").status_code == 403
        assert admin.get(NURSES).status_code == 403

    def test_another_units_manager_changes_nothing(self, cno, omar, er, icu):
        nurse = on_probation(cno, er)
        assert act(omar, er, nurse, "extend").status_code == 403
        assert act(omar, icu, nurse, "extend").status_code == 403


class TestTheNightShiftAlone:
    def shifts(self, mona, er, nurse, allowed):
        return mona.put(f"/api/units/{er.pk}/nurses/{nurse}/allowed-shifts", {"allowed_shifts": allowed},
                        content_type="application/json")

    def test_refused_while_she_is_on_probation(self, cno, mona, er):
        nurse = on_probation(cno, er, "2026-11-01")
        assert refusal(self.shifts(mona, er, nurse, ["N"]), "allowed_shifts") == (
            "Layan Alharbi is on probation, and a nurse on probation does not work N, the night shift, "
            "so it alone would leave her no shift.")
        assert self.shifts(mona, er, nurse, ["D", "N"]).status_code == 200

    def test_also_before_her_probation_begins(self, cno, mona, er):
        nurse = on_probation(cno, er, "2026-12-15")
        assert self.shifts(mona, er, nurse, ["N"]).status_code == 422

    def test_allowed_once_it_is_over(self, cno, mona, er, today):
        nurse = on_probation(cno, er, "2026-09-01")
        today(2026, 12, 1)
        assert self.shifts(mona, er, nurse, ["N"]).status_code == 200


class TestTime:
    def test_it_ends_by_itself_on_its_last_day(self, cno, mona, er, today):
        nurse = on_probation(cno, er, "2026-09-01")
        today(2026, 11, 30)
        assert last_day(mona, er, nurse) == "2026-11-30"
        today(2026, 12, 1)
        assert row(mona, er, nurse)["probation"] is None
        assert refusal(act(mona, er, nurse, "extend")) == "Layan Alharbi is not on probation."

    def notes(self, client):
        return [(item["text"], item["link"]) for item in client.get("/api/notifications").json()["items"]
                if item["event"] == "probation_ending"]

    def test_her_unit_manager_is_told_once_at_the_start_of_her_last_month(self, cno, mona, er, today):
        on_probation(cno, er, "2026-12-15")
        today(2027, 2, 28)
        tell_probations_ending(timestamp=0)
        assert self.notes(mona) == []
        today(2027, 3, 1)
        tell_probations_ending(timestamp=0)
        today(2027, 3, 2)
        tell_probations_ending(timestamp=0)
        assert self.notes(mona) == [("Layan Alharbi's probation ends on 31 March. Extend it or let it end.",
                                     {"page": "rostering_attributes", "month": "2027-03", "unit_id": None, "cover_id": None})]

    def test_an_extension_tells_him_again_in_the_new_last_month(self, cno, mona, er, today):
        nurse = on_probation(cno, er, "2026-12-15")
        today(2027, 3, 1)
        tell_probations_ending(timestamp=0)
        ok(act(mona, er, nurse, "extend"))
        tell_probations_ending(timestamp=0)
        today(2027, 4, 1)
        tell_probations_ending(timestamp=0)
        assert [text for text, _ in self.notes(mona)] == [
            "Layan Alharbi's probation ends on 30 April. Extend it or let it end.",
            "Layan Alharbi's probation ends on 31 March. Extend it or let it end."]

    def test_a_unit_with_no_manager_tells_its_next_one_that_month(self, cno, mona, er, today):
        on_probation(cno, er, "2026-12-15")
        er.manager = None
        er.save(update_fields=["manager"])
        today(2027, 3, 1)
        tell_probations_ending(timestamp=0)
        er.manager = User.objects.get(username="mona")
        er.save(update_fields=["manager"])
        today(2027, 3, 9)
        tell_probations_ending(timestamp=0)
        assert len(self.notes(mona)) == 1

    def test_nobody_is_told_of_one_ended_early_or_unticked(self, cno, mona, er, today):
        ended = on_probation(cno, er, "2026-09-01")
        unticked = on_probation(cno, er, "2026-09-01", hospital_id="h2")
        ok(act(mona, er, ended, "end"))
        assert correct(cno, unticked, on_probation=False).status_code == 200
        tell_probations_ending(timestamp=0)
        assert self.notes(mona) == []

    def test_a_transfer_carries_it_counted_from_her_start_date(self, cno, mona, omar, er, icu, today):
        nurse = on_probation(cno, er, "2026-12-15")
        response = post(cno, f"{NURSES}/{nurse}/transfer", {"unit_id": str(icu.pk), "from_month": "2027-01"})
        assert response.status_code == 200, response.content
        today(2027, 1, 4)
        assert read(row(omar, icu, nurse)) == {"last_day": "2027-03-31", "months": 3, "ticked": True, "ended_early": False}
        assert ok(act(omar, icu, nurse, "extend"))["probation"]["last_day"] == "2027-04-30"
        assert act(mona, er, nurse, "extend").status_code == 403
        today(2027, 4, 1)
        tell_probations_ending(timestamp=0)
        assert len(self.notes(omar)) == 1
        assert self.notes(mona) == []


class TestTheFiles:
    def upload(self, client, path, content, **data):
        file = SimpleUploadedFile("nurses.csv", content.encode(), content_type="text/csv")
        return client.post(path, {"file": file, **data})

    def test_the_template_has_the_column(self, cno):
        assert cno.get(f"{NURSES}/import/template").content.decode("utf-8-sig").strip() == (
            '"Hospital ID","Full name","Gender","Unit","Start date","On probation"')

    def test_the_import_reads_the_column_empty_meaning_no(self, cno, mona, er):
        content = ("Hospital ID,Full name,Gender,Unit,Start date,On probation\n"
                   "h1,Layan Alharbi,F,ER,2026-12-15,Yes\nh2,Amal Saleh,F,ER,2026-12-15,\nh3,Sara Ali,F,ER,2026-12-01,TRUE\n"
                   "h4,Huda Omar,F,ER,2026-12-01,no\n")
        preview = self.upload(cno, f"{NURSES}/import/preview", content).json()
        assert [line["on_probation"] for line in preview["rows"]] == ["Yes", "No", "Yes", "No"]
        response = self.upload(cno, f"{NURSES}/import/confirm", content, token=preview["token"])
        assert response.status_code == 201, response.content
        lasts = {nurse["hospital_id"]: nurse["probation"] and nurse["probation"]["last_day"]
                 for nurse in mona.get(f"/api/units/{er.pk}/nurses").json()}
        assert lasts == {"h1": "2027-03-31", "h2": None, "h3": "2027-02-28", "h4": None}
        assert [entry["target"] for entry in entries("nurse.probation_ticked")] == ["h1", "h3"]

    def test_a_file_without_the_column_still_imports(self, cno, er):
        content = "Hospital ID,Full name,Gender,Unit,Start date\nh1,Amal Saleh,F,ER,2026-12-15\n"
        preview = self.upload(cno, f"{NURSES}/import/preview", content).json()
        assert preview["rows"][0]["problems"] == []
        assert self.upload(cno, f"{NURSES}/import/confirm", content, token=preview["token"]).status_code == 201
        assert NurseProfile.objects.get().on_probation is False

    def test_a_line_with_a_cell_fewer_than_its_header_is_reported(self, cno, er):
        content = "Hospital ID,Full name,Gender,Unit,Start date,On probation\nh1,Amal Saleh,F,ER,2026-12-15\n"
        preview = self.upload(cno, f"{NURSES}/import/preview", content).json()
        assert preview["rows"][0]["problems"] == [
            {"column": None, "message": "This line has 5 cells, not 6. Put double quotes around a name that has a comma in it."}]

    def test_a_word_it_does_not_know_is_a_problem(self, cno, er):
        content = "Hospital ID,Full name,Gender,Unit,Start date,On probation\nh1,Amal Saleh,F,ER,2026-12-15,maybe\n"
        preview = self.upload(cno, f"{NURSES}/import/preview", content).json()
        assert preview["token"] is None
        assert preview["rows"][0]["problems"] == [{"column": "on_probation", "message": "Use Yes or No, or leave it empty for no."}]

    def test_the_staff_list_says_who_is_on_probation_and_until_when(self, cno, er):
        on_probation(cno, er, "2026-12-15")
        added(cno, er, "h2")
        rows = list(csv.reader(io.StringIO(cno.get(f"{NURSES}/export").content.decode("utf-8-sig"))))
        assert rows[0][5:7] == ["On probation", "Probation's last day"]
        assert {line[0]: line[5:7] for line in rows[1:]} == {"h1": ["Yes", "2027-03-31"], "h2": ["", ""]}
