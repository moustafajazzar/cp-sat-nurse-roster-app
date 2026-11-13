"""Leave: recording, correcting and removing it, whose months a manager reaches across a transfer, who can read a type,
what the audit log may hold, and that what is saved reaches the solver as it expects. Today is held still, in November 2026."""
import datetime as dt
import json
from pathlib import Path
from uuid import uuid4

import pytest
from django.db import IntegrityError, transaction
from django.test import Client
from nurse_roster.request import load_request

from accounts.models import Role, User
from audit.models import AuditEntry
from conftest import PASSWORD, post
from hospital.catalogue import seed_catalogue
from hospital.models import HospitalSettings, LeaveType, Shift
from hospital.rules import general_block
from nurses import membership
from nurses.models import Leave, NurseProfile
from nurses.rostering import leave_dates, nurse_block
from nurses.tests.conftest import NURSES, added, make_unit
from units.rules import unit_block

pytestmark = pytest.mark.django_db

ER_REQUEST = Path(__file__).parents[3] / "solver/tests/fixtures/er_request_2026-03.json"
NOVEMBER = "2026-11"


@pytest.fixture(autouse=True)
def catalogue(db, on_day):
    seed_catalogue()
    on_day(2026, 11, 5)


@pytest.fixture
def mona(client_of):
    return client_of("mona", Role.UNIT_MANAGER)


@pytest.fixture
def omar(client_of):
    return client_of("omar", Role.UNIT_MANAGER)


@pytest.fixture
def er(mona):
    return make_unit("ER", User.objects.get(username="mona"))


@pytest.fixture
def icu(omar):
    return make_unit("ICU", User.objects.get(username="omar"))


@pytest.fixture
def amal(cno, er) -> str:
    return added(cno, er)["nurse"]["id"]


def kind(name="Sick") -> str:
    return str(LeaveType.objects.get(name=name).pk)


def record_leave(client, unit, nurse, first="2026-11-10", last=None, leave_type="Sick"):
    return post(client, f"/api/units/{unit.pk}/leave", {"nurse_id": nurse, "leave_type_id": kind(leave_type), "first": first,
                                                        "last": last or first})


def recorded(client, unit, nurse, first="2026-11-10", last=None, leave_type="Sick") -> dict:
    response = record_leave(client, unit, nurse, first, last, leave_type)
    assert response.status_code == 201, response.content
    return response.json()


def change(client, unit, ids, first, last=None, leave_type="Sick"):
    return post(client, f"/api/units/{unit.pk}/leave/change", {"leave_ids": ids, "leave_type_id": kind(leave_type),
                                                               "first": first, "last": last or first})


def remove(client, unit, ids):
    return post(client, f"/api/units/{unit.pk}/leave/remove", {"leave_ids": ids})


def month_of(client, unit, month=NOVEMBER) -> dict:
    response = client.get(f"/api/units/{unit.pk}/leave", {"month": month})
    assert response.status_code == 200, response.content
    return response.json()


def runs_of(client, unit, month=NOVEMBER) -> list[tuple]:
    return [(run["nurse"]["hospital_id"], run["first"], run["last"], run["days"], run["leave_type"]["name"])
            for run in month_of(client, unit, month)["runs"]]


def refusal(response) -> tuple[str, str]:
    assert response.status_code == 422, response.content
    error = response.json()["detail"][0]
    return error["loc"][2], error["msg"]


def transfer(client, nurse_id, unit, month):
    response = post(client, f"{NURSES}/{nurse_id}/transfer", {"unit_id": str(unit.pk), "from_month": month})
    assert response.status_code == 200, response.content


def staff_nurse(username="h123") -> Client:
    user = User.objects.get(username=username)
    user.set_password(PASSWORD)
    user.must_change_password = False
    user.save()
    client = Client()
    assert post(client, "/api/auth/login", {"username": username, "password": PASSWORD}).status_code == 200
    return client


def my_leave(client, month=NOVEMBER) -> dict:
    response = client.get(f"{NURSES}/my-leave", {"month": month})
    assert response.status_code == 200, response.content
    return response.json()


# ---- recording ------------------------------------------------------------------------------------
def test_the_manager_records_one_day_and_it_is_on_the_units_month(mona, er, amal):
    run = recorded(mona, er, amal)

    assert run["first"] == run["last"] == "2026-11-10" and run["days"] == 1 and run["leave_type"]["name"] == "Sick"
    assert runs_of(mona, er) == [("h123", "2026-11-10", "2026-11-10", 1, "Sick")]
    row = Leave.objects.get()
    assert row.recorded_by.username == "mona" and row.removed_at is None


def test_several_days_are_one_row_each_and_read_as_one_run(mona, er, amal):
    run = recorded(mona, er, amal, "2026-11-10", "2026-11-13")

    assert Leave.objects.count() == 4 and len(run["leave_ids"]) == 4
    assert runs_of(mona, er) == [("h123", "2026-11-10", "2026-11-13", 4, "Sick")]


def test_a_run_breaks_at_a_gap_and_at_another_type(mona, er, amal):
    recorded(mona, er, amal, "2026-11-10", "2026-11-11")
    recorded(mona, er, amal, "2026-11-12", leave_type="Annual")
    recorded(mona, er, amal, "2026-11-20")

    assert [run[1:4] for run in runs_of(mona, er)] == [("2026-11-10", "2026-11-11", 2), ("2026-11-12", "2026-11-12", 1),
                                                       ("2026-11-20", "2026-11-20", 1)]


def test_a_month_shows_only_its_own_days_of_a_longer_leave(mona, er, amal):
    recorded(mona, er, amal, "2026-11-29", "2026-12-02")

    assert [run[1:4] for run in runs_of(mona, er)] == [("2026-11-29", "2026-11-30", 2)]
    assert [run[1:4] for run in runs_of(mona, er, "2026-12")] == [("2026-12-01", "2026-12-02", 2)]


def test_the_month_defaults_to_the_hospitals_and_says_which(mona, er, amal):
    response = mona.get(f"/api/units/{er.pk}/leave")

    assert response.json()["month"] == NOVEMBER
    assert [nurse["id"] for nurse in response.json()["nurses"]] == [amal]


@pytest.mark.parametrize("month", ["2026-13", "2026", "0000-01", "1900-01"])
def test_a_month_that_is_not_one_is_refused(mona, er, month):
    assert mona.get(f"/api/units/{er.pk}/leave", {"month": month}).status_code == 422


def test_the_last_day_cannot_be_before_the_first(mona, er, amal):
    assert refusal(record_leave(mona, er, amal, "2026-11-10", "2026-11-09"))[0] == "last"
    assert not Leave.objects.exists()


def test_a_run_longer_than_a_year_is_refused(mona, er, amal):
    field, message = refusal(record_leave(mona, er, amal, "2027-01-01", "2028-01-02"))

    assert field == "last" and "366" in message


def test_a_date_must_be_a_date(mona, er, amal):
    assert refusal(record_leave(mona, er, amal, "tomorrow"))[0] == "first"


# ---- overlaps -------------------------------------------------------------------------------------
def test_a_date_entered_twice_is_refused_beside_the_first_day(mona, er, amal):
    recorded(mona, er, amal)

    field, message = refusal(record_leave(mona, er, amal, leave_type="Annual"))

    assert field == "first" and "already on leave on 10 November 2026" in message
    assert Leave.objects.get().leave_type.name == "Sick"


def test_an_overlap_saves_none_of_the_days_and_says_how_many(mona, er, amal):
    recorded(mona, er, amal, "2026-11-10", "2026-11-11")

    field, message = refusal(record_leave(mona, er, amal, "2026-11-09", "2026-11-12"))

    assert field == "first" and "10 November 2026 and 1 more" in message
    assert Leave.objects.count() == 2


def test_the_database_holds_one_leave_a_day_as_well(mona, er, amal):
    recorded(mona, er, amal)
    row = Leave.objects.get()

    with pytest.raises(IntegrityError), transaction.atomic():
        Leave.objects.create(nurse=row.nurse, date=row.date, leave_type=row.leave_type, recorded_by=row.recorded_by)


def test_two_nurses_may_be_off_on_the_same_day(cno, mona, er, amal):
    badr = added(cno, er, "h456")["nurse"]["id"]
    recorded(mona, er, amal)

    assert record_leave(mona, er, badr).status_code == 201


def test_a_removed_day_can_be_recorded_again(mona, er, amal):
    ids = recorded(mona, er, amal)["leave_ids"]
    assert remove(mona, er, ids).status_code == 204

    assert record_leave(mona, er, amal).status_code == 201
    assert Leave.objects.count() == 2 and Leave.objects.current().count() == 1


# ---- leave types ----------------------------------------------------------------------------------
def test_a_retired_type_cannot_be_chosen_and_stays_readable_on_recorded_leave(mona, er, amal):
    ids = recorded(mona, er, amal, "2026-11-10", "2026-11-11")["leave_ids"]
    LeaveType.objects.filter(name="Sick").update(active=False)

    assert refusal(record_leave(mona, er, amal, "2026-11-20"))[0] == "leave_type_id"
    run = month_of(mona, er)["runs"][0]
    assert run["leave_type"]["name"] == "Sick" and run["leave_type"]["active"] is False
    assert run["leave_ids"] == ids


def test_leave_of_a_retired_type_can_keep_it_when_its_dates_change(mona, er, amal):
    ids = recorded(mona, er, amal, "2026-11-10", "2026-11-11")["leave_ids"]
    LeaveType.objects.filter(name="Sick").update(active=False)

    run = change(mona, er, ids, "2026-11-10").json()

    assert run["leave_type"]["name"] == "Sick" and run["days"] == 1


def test_leave_cannot_move_to_another_retired_type(mona, er, amal):
    ids = recorded(mona, er, amal)["leave_ids"]
    LeaveType.objects.filter(name="Annual").update(active=False)

    assert refusal(change(mona, er, ids, "2026-11-10", leave_type="Annual"))[0] == "leave_type_id"


def test_an_unknown_type_is_refused(mona, er, amal):
    response = post(mona, f"/api/units/{er.pk}/leave", {"nurse_id": amal, "leave_type_id": str(uuid4()),
                                                        "first": "2026-11-10", "last": "2026-11-10"})

    assert refusal(response)[0] == "leave_type_id"


# ---- correcting and removing ----------------------------------------------------------------------
def test_changing_the_type_ends_the_old_rows_and_starts_new_ones(mona, er, amal):
    ids = recorded(mona, er, amal, "2026-11-10", "2026-11-11")["leave_ids"]

    run = change(mona, er, ids, "2026-11-10", "2026-11-11", leave_type="Annual").json()

    assert run["leave_type"]["name"] == "Annual" and run["days"] == 2
    ended = Leave.objects.filter(pk__in=ids)
    assert all(row.removed_at and row.removed_by.username == "mona" and row.leave_type.name == "Sick" for row in ended)
    assert Leave.objects.current().count() == 2


def test_days_that_stay_keep_their_rows_and_days_that_go_end(mona, er, amal):
    ids = recorded(mona, er, amal, "2026-11-10", "2026-11-12")["leave_ids"]

    run = change(mona, er, ids, "2026-11-11", "2026-11-13").json()

    assert (run["first"], run["last"], run["days"]) == ("2026-11-11", "2026-11-13", 3)
    assert set(map(str, Leave.objects.current().values_list("pk", flat=True))) >= set(ids[1:])
    assert Leave.objects.get(pk=ids[0]).removed_at is not None


def test_a_change_that_changes_nothing_writes_nothing(mona, er, amal):
    ids = recorded(mona, er, amal)["leave_ids"]
    AuditEntry.objects.all().delete()

    assert change(mona, er, ids, "2026-11-10").status_code == 200

    assert not AuditEntry.objects.exists() and Leave.objects.count() == 1


def test_a_change_cannot_land_on_other_leave(mona, er, amal):
    ids = recorded(mona, er, amal, "2026-11-10")["leave_ids"]
    recorded(mona, er, amal, "2026-11-12", leave_type="Annual")

    field, message = refusal(change(mona, er, ids, "2026-11-10", "2026-11-12"))

    assert field == "first" and "12 November 2026" in message
    assert runs_of(mona, er)[0][1:3] == ("2026-11-10", "2026-11-10")


def test_removing_takes_it_off_every_view_and_keeps_the_row(cno, mona, er, amal):
    ids = recorded(mona, er, amal, "2026-11-10", "2026-11-11")["leave_ids"]

    assert remove(mona, er, ids).status_code == 204

    assert runs_of(mona, er) == [] and my_leave(staff_nurse())["runs"] == []
    assert leave_dates(NurseProfile.objects.get(pk=amal), membership.this_month()) == []
    assert all(row.removed_at and row.removed_by.username == "mona" for row in Leave.objects.all())


@pytest.mark.parametrize("act", [lambda c, u, ids: remove(c, u, ids), lambda c, u, ids: change(c, u, ids, "2026-11-20")])
def test_acting_on_leave_already_changed_is_a_conflict_and_changes_nothing(mona, er, amal, act):
    ids = recorded(mona, er, amal)["leave_ids"]
    change(mona, er, ids, "2026-11-11")

    assert act(mona, er, ids).status_code == 409
    assert runs_of(mona, er)[0][1] == "2026-11-11"


# ---- the nurse's dates ----------------------------------------------------------------------------
def test_a_date_before_the_start_date_is_refused(mona, er, amal):
    field, message = refusal(record_leave(mona, er, amal, "2026-01-09"))

    assert field == "first" and "starts on 10 January 2026" in message


def test_the_start_date_itself_is_allowed(mona, er, amal):
    assert record_leave(mona, er, amal, "2026-01-10").status_code == 201


def test_a_date_after_the_end_date_is_refused_and_the_end_date_itself_allowed(cno, mona, er, amal):
    assert cno.put(f"{NURSES}/{amal}/end-date", {"end_date": "2026-11-20"}, content_type="application/json").status_code == 200

    field, message = refusal(record_leave(mona, er, amal, "2026-11-20", "2026-11-21"))

    assert field == "last" and "20 November 2026" in message
    assert record_leave(mona, er, amal, "2026-11-20").status_code == 201


def test_a_nurse_who_was_never_present_can_have_no_leave(cno, mona, er):
    nurse = added(cno, er, "h999", start_date="2027-03-01")["nurse"]["id"]
    assert cno.put(f"{NURSES}/{nurse}/end-date", {"end_date": "2026-11-05"}, content_type="application/json").status_code == 200

    for day in ("2026-11-05", "2027-03-01"):
        assert record_leave(mona, er, nurse, day).status_code == 422
    assert nurse not in [row["id"] for row in month_of(mona, er)["nurses"]]


def test_an_end_date_set_afterwards_flags_the_days_it_strands_and_they_can_be_removed(cno, mona, er, amal):
    recorded(mona, er, amal, "2026-11-10", "2026-11-13")
    assert cno.put(f"{NURSES}/{amal}/end-date", {"end_date": "2026-11-11"}, content_type="application/json").status_code == 200

    runs = month_of(mona, er)["runs"]

    assert [(run["first"], run["last"], run["outside_dates"]) for run in runs] == [
        ("2026-11-10", "2026-11-11", False), ("2026-11-12", "2026-11-13", True)]
    assert remove(mona, er, runs[1]["leave_ids"]).status_code == 204


def test_a_nurse_starting_later_has_their_months_with_their_unit_and_none_before(cno, mona, er):
    nurse = added(cno, er, "h777", start_date="2027-03-10")["nurse"]["id"]

    assert record_leave(mona, er, nurse, "2027-03-09").status_code == 422
    assert record_leave(mona, er, nurse, "2027-03-10").status_code == 201
    assert nurse not in [row["id"] for row in month_of(mona, er)["nurses"]]
    assert nurse in [row["id"] for row in month_of(mona, er, "2027-03")["nurses"]]


def test_a_day_left_outside_their_dates_can_only_be_removed_and_its_nurse_does_not_see_it(cno, mona, er, amal):
    recorded(mona, er, amal, "2026-11-10", "2026-11-13", leave_type="Annual")
    assert cno.put(f"{NURSES}/{amal}/end-date", {"end_date": "2026-11-11"}, content_type="application/json").status_code == 200
    stranded = month_of(mona, er)["runs"][1]["leave_ids"]

    assert refusal(change(mona, er, stranded, "2026-11-12", "2026-11-13", leave_type="Sick"))[0] == "last"
    assert [(run["first"], run["last"]) for run in my_leave(staff_nurse())["runs"]] == [("2026-11-10", "2026-11-11")]


def test_a_date_before_their_first_placement_is_about_their_start_date(mona, er, amal):
    assert "starts on" in refusal(record_leave(mona, er, amal, "2025-12-01"))[1]


# ---- whose months: scope and transfers ------------------------------------------------------------
def test_another_units_manager_gets_403_on_every_endpoint(mona, omar, er, icu, amal):
    ids = recorded(mona, er, amal)["leave_ids"]

    assert omar.get(f"/api/units/{er.pk}/leave").status_code == 403
    assert record_leave(omar, er, amal, "2026-11-20").status_code == 403
    assert change(omar, er, ids, "2026-11-20").status_code == 403
    assert remove(omar, er, ids).status_code == 403
    assert Leave.objects.current().get().date.isoformat() == "2026-11-10"


def test_through_their_own_unit_another_units_nurse_and_leave_are_the_same_403(mona, omar, er, icu, amal):
    ids = recorded(mona, er, amal)["leave_ids"]

    assert record_leave(omar, icu, amal, "2026-11-20").status_code == 403
    assert change(omar, icu, ids, "2026-11-20").status_code == 403
    assert remove(omar, icu, ids).status_code == 403
    assert remove(omar, icu, [str(uuid4())]).status_code == 403
    assert record_leave(omar, icu, str(uuid4()), "2026-11-20").status_code == 403
    assert runs_of(omar, icu) == []


def test_a_units_list_has_no_other_units_nurses(mona, omar, er, icu, amal):
    assert month_of(omar, icu)["nurses"] == []


def test_leave_cannot_be_acted_on_across_nurses_in_one_call(cno, mona, er, amal):
    badr = added(cno, er, "h456")["nurse"]["id"]
    ids = recorded(mona, er, amal)["leave_ids"] + recorded(mona, er, badr)["leave_ids"]

    assert remove(mona, er, ids).status_code == 403
    assert Leave.objects.current().count() == 2


def test_across_a_coming_transfer_each_manager_has_their_own_months(cno, mona, omar, er, icu, amal):
    transfer(cno, amal, icu, "2027-01")

    # ER: through December, and January is not theirs.
    assert record_leave(mona, er, amal, "2026-12-31").status_code == 201
    field, message = refusal(record_leave(mona, er, amal, "2027-01-01"))
    assert field == "first" and "not in your unit in January 2027" in message and "ICU" not in message
    # ICU: January before the nurse arrives, and December is not theirs.
    assert record_leave(omar, icu, amal, "2027-01-01").status_code == 201
    assert refusal(record_leave(omar, icu, amal, "2026-12-30"))[0] == "first"

    assert amal in [row["id"] for row in month_of(omar, icu, "2027-01")["nurses"]]
    assert amal not in [row["id"] for row in month_of(omar, icu, "2026-12")["nurses"]]
    assert amal not in [row["id"] for row in month_of(mona, er, "2027-01")["nurses"]]


def test_a_leave_across_the_month_of_a_transfer_is_refused_whole(cno, mona, er, icu, amal):
    transfer(cno, amal, icu, "2027-01")

    assert refusal(record_leave(mona, er, amal, "2026-12-30", "2027-01-02"))[0] == "first"
    assert not Leave.objects.exists()


def test_a_manager_never_reads_another_managers_months_of_a_nurse(cno, mona, omar, er, icu, amal):
    transfer(cno, amal, icu, "2027-01")
    recorded(mona, er, amal, "2026-12-31")
    theirs = recorded(omar, icu, amal, "2027-01-01")["leave_ids"]
    earlier = month_of(mona, er, "2026-12")["runs"][0]["leave_ids"]

    assert runs_of(omar, icu, "2026-12") == [] and runs_of(mona, er, "2027-01") == []
    assert remove(mona, er, theirs).status_code == 403
    assert remove(omar, icu, earlier).status_code == 403
    assert change(mona, er, theirs, "2026-12-20").status_code == 403


def test_after_a_transfer_the_old_unit_keeps_its_past_months_for_late_facts(cno, mona, omar, er, icu, amal, on_day):
    transfer(cno, amal, icu, "2027-01")
    on_day(2027, 2, 3)

    assert amal in [row["id"] for row in month_of(mona, er, "2026-12")["nurses"]]
    run = recorded(mona, er, amal, "2026-12-15")
    assert runs_of(mona, er, "2026-12") == [("h123", "2026-12-15", "2026-12-15", 1, "Sick")]
    assert change(mona, er, run["leave_ids"], "2026-12-16").status_code == 200
    assert refusal(record_leave(mona, er, amal, "2027-02-10"))[0] == "first"
    assert record_leave(omar, icu, amal, "2027-02-10").status_code == 201


def test_a_manager_they_have_left_learns_nothing_of_their_dates_from_a_refusal(cno, mona, er, icu, amal, on_day):
    transfer(cno, amal, icu, "2027-01")
    assert cno.put(f"{NURSES}/{amal}/end-date", {"end_date": "2027-03-31"}, content_type="application/json").status_code == 200

    field, message = refusal(record_leave(mona, er, amal, "2027-04-02"))

    assert field == "first" and "31 March" not in message and "not in your unit" in message


def test_a_transfer_from_this_month_hands_the_whole_month_over(cno, mona, omar, er, icu, amal):
    recorded(mona, er, amal, "2026-11-20")
    transfer(cno, amal, icu, NOVEMBER)

    assert runs_of(mona, er) == []
    assert runs_of(omar, icu) == [("h123", "2026-11-20", "2026-11-20", 1, "Sick")]


def test_a_replaced_transfer_gives_the_month_back(cno, mona, omar, er, icu, amal):
    transfer(cno, amal, icu, "2027-01")
    recorded(omar, icu, amal, "2027-01-10")

    transfer(cno, amal, er, "2027-01")

    assert runs_of(omar, icu, "2027-01") == []
    assert runs_of(mona, er, "2027-01") == [("h123", "2027-01-10", "2027-01-10", 1, "Sick")]


def test_a_manager_records_their_own_leave_and_the_log_shows_whose_hand(cno, mona, er):
    response = post(cno, f"{NURSES}/managers", {"user_id": str(User.objects.get(username="mona").pk), "gender": "F",
                                                "unit_id": str(er.pk), "start_date": "2026-01-10"})
    assert response.status_code == 201, response.content

    recorded(mona, er, response.json()["id"])

    entry = AuditEntry.objects.filter(action="leave.recorded").get()
    assert entry.actor.username == entry.target == "mona"
    assert my_leave(mona)["runs"][0]["leave_type"] == "Sick"


def test_a_manager_with_no_profile_simply_has_no_leave_of_their_own(mona, er):
    assert my_leave(mona)["runs"] == []


# ---- month-aware membership -----------------------------------------------------------------------
def test_placed_in_and_rostered_in_answer_for_a_month(cno, er, icu, amal):
    transfer(cno, amal, icu, "2027-01")
    leaver = added(cno, er, "h456")["nurse"]["id"]
    assert cno.put(f"{NURSES}/{leaver}/end-date", {"end_date": "2026-11-30"}, content_type="application/json").status_code == 200

    def ids(query, unit, *month):
        return {str(pk) for pk in query(unit, dt.date(*month, 1)).values_list("pk", flat=True)}

    assert ids(membership.rostered_in, er, 2026, 11) == {amal, leaver}
    assert ids(membership.rostered_in, er, 2026, 12) == {amal}
    assert ids(membership.placed_in, er, 2026, 12) == {amal, leaver}
    assert ids(membership.rostered_in, er, 2027, 1) == set()
    assert ids(membership.rostered_in, icu, 2027, 1) == {amal}
    assert ids(membership.rostered_in, icu, 2026, 12) == set()


# ---- who reads it ---------------------------------------------------------------------------------
def test_a_nurse_reads_their_own_leave_with_its_type(cno, mona, er, amal):
    recorded(mona, er, amal, "2026-11-10", "2026-11-11", leave_type="Maternity")

    answer = my_leave(staff_nurse())

    assert answer == {"month": NOVEMBER, "runs": [{"first": "2026-11-10", "last": "2026-11-11", "days": 2, "leave_type": "Maternity"}]}


def test_a_nurse_reads_their_own_leave_of_a_month_in_another_unit(cno, mona, er, icu, omar, amal):
    transfer(cno, amal, icu, "2027-01")
    recorded(omar, icu, amal, "2027-01-10")
    recorded(mona, er, amal, "2026-12-10")
    nurse = staff_nurse()

    assert my_leave(nurse, "2027-01")["runs"][0]["first"] == "2027-01-10"
    assert my_leave(nurse, "2026-12")["runs"][0]["first"] == "2026-12-10"


def test_a_nurse_reads_nothing_of_a_colleagues_leave(cno, mona, er, amal):
    badr = added(cno, er, "h456", full_name="Badr Omar")["nurse"]["id"]
    ids = recorded(mona, er, badr, leave_type="Maternity")["leave_ids"]
    nurse = staff_nurse()

    assert my_leave(nurse)["runs"] == []
    # It takes no nurse, so nothing can be asked for a colleague.
    assert nurse.get(f"{NURSES}/my-leave", {"month": NOVEMBER, "nurse_id": badr}).json()["runs"] == []
    assert nurse.get(f"/api/units/{er.pk}/leave").status_code == 403
    assert record_leave(nurse, er, badr, "2026-11-20").status_code == 403
    assert remove(nurse, er, ids).status_code == 403
    assert change(nurse, er, ids, "2026-11-20").status_code == 403
    assert b"Maternity" not in nurse.get("/api/nurses/my-unit").content


def test_the_system_admin_reads_no_units_leave(client_of, mona, er, amal):
    recorded(mona, er, amal)
    admin = client_of("admin", Role.SYSTEM_ADMIN)

    assert admin.get(f"/api/units/{er.pk}/leave").status_code == 403
    assert admin.get(f"{NURSES}/my-leave").status_code == 403


# ---- the CNO and the Deputy CNO --------------------------------------------------------------------
@pytest.fixture(params=["the cno", "the deputy"])
def hospital_wide(request, cno, client_of) -> Client:
    return cno if request.param == "the cno" else client_of("deputy", Role.DEPUTY_CNO)


def test_a_hospital_wide_role_reads_any_units_leave_with_its_type(hospital_wide, cno, mona, omar, er, icu, amal):
    badr = added(cno, icu, "h456", full_name="Badr Omar")["nurse"]["id"]
    recorded(mona, er, amal, "2026-11-10", "2026-11-11", leave_type="Maternity")
    recorded(omar, icu, badr, "2026-11-20")

    assert runs_of(hospital_wide, er) == [("h123", "2026-11-10", "2026-11-11", 2, "Maternity")]
    assert runs_of(hospital_wide, icu) == [("h456", "2026-11-20", "2026-11-20", 1, "Sick")]


def test_a_hospital_wide_role_reads_every_month_each_under_the_unit_it_falls_under(hospital_wide, cno, mona, omar, er, icu, amal):
    recorded(mona, er, amal, "2026-11-10")
    transfer(cno, amal, icu, "2026-12")
    recorded(omar, icu, amal, "2026-12-10")
    far_ahead = "2029-03"

    assert runs_of(hospital_wide, er, NOVEMBER) == [("h123", "2026-11-10", "2026-11-10", 1, "Sick")]
    assert runs_of(hospital_wide, icu, NOVEMBER) == []
    assert runs_of(hospital_wide, icu, "2026-12") == [("h123", "2026-12-10", "2026-12-10", 1, "Sick")]
    assert runs_of(hospital_wide, er, "2026-12") == []
    assert month_of(hospital_wide, icu, far_ahead)["runs"] == []
    assert hospital_wide.get(f"/api/units/{er.pk}/leave").json()["month"] == NOVEMBER


def test_a_hospital_wide_role_reads_an_archived_units_leave(hospital_wide, mona, er, amal):
    recorded(mona, er, amal)
    er.archived, er.manager = True, None
    er.save()

    assert runs_of(hospital_wide, er) == [("h123", "2026-11-10", "2026-11-10", 1, "Sick")]


def test_a_hospital_wide_role_reads_no_ended_rows(hospital_wide, mona, er, amal):
    corrected = recorded(mona, er, amal, "2026-11-10", leave_type="Maternity")["leave_ids"]
    assert change(mona, er, corrected, "2026-11-12").status_code == 200
    remove(mona, er, recorded(mona, er, amal, "2026-11-20")["leave_ids"])

    assert runs_of(hospital_wide, er) == [("h123", "2026-11-12", "2026-11-12", 1, "Sick")]
    assert b"Maternity" not in hospital_wide.get(f"/api/units/{er.pk}/leave", {"month": NOVEMBER}).content


def test_a_unit_that_does_not_exist_is_a_404_for_a_hospital_wide_role(hospital_wide):
    assert hospital_wide.get(f"/api/units/{uuid4()}/leave").status_code == 404


def test_a_hospital_wide_role_can_change_nothing(hospital_wide, mona, er, amal):
    ids = recorded(mona, er, amal, leave_type="Maternity")["leave_ids"]
    AuditEntry.objects.all().delete()

    assert record_leave(hospital_wide, er, amal, "2026-11-20").status_code == 403
    assert change(hospital_wide, er, ids, "2026-11-20").status_code == 403
    assert remove(hospital_wide, er, ids).status_code == 403
    assert hospital_wide.get(f"{NURSES}/my-leave").status_code == 403

    assert runs_of(mona, er) == [("h123", "2026-11-10", "2026-11-10", 1, "Maternity")]
    assert not AuditEntry.objects.exists()


def test_reading_leave_writes_nothing_to_the_audit_log(hospital_wide, mona, er, amal):
    recorded(mona, er, amal)
    AuditEntry.objects.all().delete()

    month_of(hospital_wide, er)

    assert not AuditEntry.objects.exists()


# ---- the audit log --------------------------------------------------------------------------------
def test_each_write_says_whose_leave_and_whose_hand_and_nothing_else(mona, er, amal):
    AuditEntry.objects.all().delete()
    ids = recorded(mona, er, amal, "2026-11-10", "2026-11-11", leave_type="Maternity")["leave_ids"]
    change(mona, er, ids, "2026-11-12", "2026-11-13", leave_type="Sick")
    remove(mona, er, month_of(mona, er)["runs"][0]["leave_ids"])

    entries = list(AuditEntry.objects.order_by("id"))

    assert [entry.action for entry in entries] == ["leave.recorded", "leave.changed", "leave.removed"]
    for entry in entries:
        assert entry.actor.username == "mona" and entry.target == "h123"
        assert entry.before is None and entry.after is None


def test_the_system_admin_reads_no_type_no_date_and_no_count_in_the_log(client_of, mona, er, amal):
    recorded(mona, er, amal, "2026-11-10", "2026-11-11", leave_type="Maternity")
    admin = client_of("admin", Role.SYSTEM_ADMIN)

    page = admin.get("/api/audit/entries", {"action": "leave.recorded"})

    assert page.status_code == 200 and len(page.json()["entries"]) == 1
    entry = page.json()["entries"][0]
    assert entry["before"] is None and entry["after"] is None
    # A date would be the only trace of one here. The entry's own time is left out: it is today's, which can be one of them.
    said = json.dumps({key: value for key, value in entry.items() if key != "at"})
    assert not any(word in said for word in ("Maternity", kind("Maternity"), "2026-11-10", "2026-11-11"))


def test_a_refused_write_is_not_logged(mona, er, amal):
    recorded(mona, er, amal)
    AuditEntry.objects.all().delete()

    assert record_leave(mona, er, amal).status_code == 422
    assert not AuditEntry.objects.exists()


# ---- the solver -----------------------------------------------------------------------------------
def solve_input(unit, nurse: NurseProfile) -> dict:
    """March 2026, the month of the solver's own fixture."""
    request = json.loads(ER_REQUEST.read_text())
    for row in request["nurses"]:
        row["previous"], row["allowed_shifts"] = [], None
    block = nurse_block(nurse, nurse.placements.last()) | {"leave": leave_dates(nurse, dt.date(2026, 3, 1))}
    request["nurses"].append(block)
    shift_hours = {shift.code: shift.hours for shift in Shift.objects.filter(active=True)}
    return request | {"version": 2, "general": general_block(HospitalSettings.load(), shift_hours=shift_hours),
                      "department": unit_block(unit)}


def loaded(unit, nurse_id):
    nurse = NurseProfile.objects.get(pk=nurse_id)
    problem = load_request(solve_input(unit, nurse)).problem
    return problem, next(row for row in problem.nurses if row.id == nurse.user.username)


def test_what_is_saved_reaches_the_solver_as_it_expects(mona, er, amal):
    recorded(mona, er, amal, "2026-03-01", "2026-03-02", leave_type="Maternity")
    recorded(mona, er, amal, "2026-03-03", leave_type="Sick")
    recorded(mona, er, amal, "2026-03-31")

    problem, nurse = loaded(er, amal)

    assert nurse.unavailable == [0, 1, 2, 30]
    assert problem.leave_credit(nurse) > 0


def test_a_month_of_leave_fits_the_solver(mona, er, amal):
    recorded(mona, er, amal, "2026-02-20", "2026-04-05")

    _, nurse = loaded(er, amal)

    assert nurse.unavailable == list(range(31))


def test_no_leave_is_an_empty_list_and_the_solver_takes_it(er, amal):
    assert loaded(er, amal)[1].unavailable == []


def test_corrected_removed_and_other_months_leave_stay_out_of_a_solve(mona, er, amal):
    ids = recorded(mona, er, amal, "2026-03-05")["leave_ids"]
    change(mona, er, ids, "2026-03-06")
    removed = recorded(mona, er, amal, "2026-03-08")["leave_ids"]
    remove(mona, er, removed)
    recorded(mona, er, amal, "2026-04-01")

    assert leave_dates(NurseProfile.objects.get(pk=amal), dt.date(2026, 3, 1)) == ["2026-03-06"]


def test_a_solve_input_carries_dates_and_no_type(mona, er, amal):
    recorded(mona, er, amal, "2026-03-05", leave_type="Maternity")

    assert "Maternity" not in json.dumps(solve_input(er, NurseProfile.objects.get(pk=amal)))
