"""The expiries page: every skill expired or expiring within 90 days of the hospital's today, and every nurse with no
record of a skill required to work, for nurses active and not past their end date. The CNO and the Deputy CNO read every unit's,
a unit manager his own; the CNO's home shows how many rows there are. Today is held still, on 5 November 2026, so the horizon's
last day is 3 February 2027."""
import datetime as dt

import pytest

from accounts.models import Role, User
from conftest import post
from hospital.models import Skill
from nurses.models import NurseProfile
from nurses.tests.conftest import NURSES, added, make_unit
from nurses.tests.test_skills import catalogue, change_skill, recorded, transfer  # noqa: F401 (fixtures)

pytestmark = pytest.mark.django_db

EXPIRIES = f"{NURSES}/expiries"


@pytest.fixture(autouse=True)
def today(on_day):
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


def nurse(cno, unit, hospital_id, full_name) -> str:
    return added(cno, unit, hospital_id, full_name=full_name)["nurse"]["id"]


def expiries(client) -> dict:
    response = client.get(EXPIRIES)
    assert response.status_code == 200, response.content
    return response.json()


def seen(client) -> list[tuple]:
    return [(row["hospital_id"], row["skill"], row["state"], row["expiry"]) for row in expiries(client)["rows"]]


def with_bls(cno, catalogue, unit, hospital_id, full_name, expiry) -> str:  # noqa: F811
    nurse_id = nurse(cno, unit, hospital_id, full_name)
    recorded(cno, nurse_id, catalogue["BLS"], expiry)
    return nurse_id


# ---- the rows ---------------------------------------------------------------------------------------
def test_a_row_names_the_nurse_her_unit_the_skill_its_state_and_date(cno, er, catalogue):  # noqa: F811
    amal = with_bls(cno, catalogue, er, "h1", "Amal Saleh", "2026-11-30")

    assert expiries(cno) == {"horizon_days": 90, "count": 1, "rows": [
        {"nurse_id": amal, "hospital_id": "h1", "full_name": "Amal Saleh", "unit": {"id": str(er.pk), "name": "ER"},
         "skill": "BLS", "kind": "skill", "state": "expiring", "expiry": "2026-11-30"}]}


def test_the_three_kinds_of_row_oldest_date_first_and_no_record_last(cno, er, catalogue):  # noqa: F811
    with_bls(cno, catalogue, er, "h1", "Amal Saleh", "2027-01-15")
    with_bls(cno, catalogue, er, "h2", "Badr Omar", "2026-10-01")
    nurse(cno, er, "h3", "Dana Ali")  # holds no BLS, which is required to work
    with_bls(cno, catalogue, er, "h4", "Carla Nunes", "2026-11-05")
    nurse(cno, er, "h5", "Ali Badr")

    assert seen(cno) == [
        ("h2", "BLS", "expired", "2026-10-01"),
        ("h4", "BLS", "expiring", "2026-11-05"),
        ("h1", "BLS", "expiring", "2027-01-15"),
        ("h5", "BLS", "no_record", None),
        ("h3", "BLS", "no_record", None),
    ]
    assert expiries(cno)["count"] == 5


def test_expired_is_from_the_day_after_the_expiry_which_still_counts(cno, er, catalogue):  # noqa: F811
    with_bls(cno, catalogue, er, "h1", "Amal Saleh", "2026-11-04")
    with_bls(cno, catalogue, er, "h2", "Badr Omar", "2026-11-05")

    assert seen(cno) == [("h1", "BLS", "expired", "2026-11-04"), ("h2", "BLS", "expiring", "2026-11-05")]


def test_the_horizon_is_90_days_its_last_day_in_and_the_next_out(cno, er, catalogue):  # noqa: F811
    with_bls(cno, catalogue, er, "h1", "Amal Saleh", "2027-02-03")
    with_bls(cno, catalogue, er, "h2", "Badr Omar", "2027-02-04")

    assert seen(cno) == [("h1", "BLS", "expiring", "2027-02-03")]


def test_a_skill_not_required_to_work_is_listed_when_it_expires_and_never_as_missing(cno, er, catalogue):  # noqa: F811
    amal = with_bls(cno, catalogue, er, "h1", "Amal Saleh", "2027-06-30")
    recorded(cno, amal, catalogue["ACLS"], "2026-12-01")

    assert seen(cno) == [("h1", "ACLS", "expiring", "2026-12-01")]


def test_a_skill_that_does_not_expire_is_never_listed_and_its_kept_date_counts_for_nothing(cno, er, catalogue):  # noqa: F811
    amal = with_bls(cno, catalogue, er, "h1", "Amal Saleh", "2026-12-01")
    recorded(cno, amal, catalogue["ICU"])
    assert change_skill(cno, "BLS", expires=False).status_code == 200

    # Required and held: a record, so not missing; and its date is kept but counts for nothing.
    assert seen(cno) == []


def test_a_retired_skill_is_no_row_of_any_kind(cno, er, catalogue):  # noqa: F811
    with_bls(cno, catalogue, er, "h1", "Amal Saleh", "2026-10-01")
    nurse(cno, er, "h2", "Badr Omar")
    assert change_skill(cno, "BLS", active=False).status_code == 200

    assert seen(cno) == []


def test_a_renewed_skill_is_listed_by_its_new_date_and_one_taken_off_is_missing(cno, er, catalogue):  # noqa: F811
    amal = with_bls(cno, catalogue, er, "h1", "Amal Saleh", "2026-10-01")
    badr = with_bls(cno, catalogue, er, "h2", "Badr Omar", "2026-10-01")
    assert cno.put(f"{NURSES}/{amal}/skills/{catalogue['BLS']}", {"expiry": "2028-01-01"},
                   content_type="application/json").status_code == 200
    assert cno.post(f"{NURSES}/{badr}/skills/{catalogue['BLS']}/remove").status_code == 200

    assert seen(cno) == [("h2", "BLS", "no_record", None)]


def test_no_required_skill_and_no_expiry_means_nothing_is_listed(cno, er):
    nurse(cno, er, "h1", "Amal Saleh")

    assert expiries(cno) == {"horizon_days": 90, "count": 0, "rows": []}


# ---- who is left out ---------------------------------------------------------------------------------
def test_a_deactivated_nurse_is_left_out(cno, er, catalogue):  # noqa: F811
    with_bls(cno, catalogue, er, "h1", "Amal Saleh", "2026-10-01")
    nurse(cno, er, "h2", "Badr Omar")
    User.objects.filter(username__in=["h1", "h2"]).update(is_active=False)

    assert seen(cno) == []


def test_a_nurse_past_her_end_date_is_left_out_and_one_ending_today_is_not(cno, er, catalogue):  # noqa: F811
    with_bls(cno, catalogue, er, "h1", "Amal Saleh", "2026-10-01")
    with_bls(cno, catalogue, er, "h2", "Badr Omar", "2026-10-01")
    # Her account stays active until the nightly job closes it: the list goes by the date, not the job.
    NurseProfile.objects.filter(user__username="h1").update(end_date=dt.date(2026, 11, 4))
    NurseProfile.objects.filter(user__username="h2").update(end_date=dt.date(2026, 11, 5))

    assert seen(cno) == [("h2", "BLS", "expired", "2026-10-01")]


def test_a_nurse_who_has_not_started_yet_is_listed(cno, er, catalogue):  # noqa: F811
    with_bls(cno, catalogue, er, "h1", "Amal Saleh", "2026-10-01")
    NurseProfile.objects.filter(user__username="h1").update(start_date=dt.date(2027, 1, 1))

    assert seen(cno) == [("h1", "BLS", "expired", "2026-10-01")]


# ---- who reads it ------------------------------------------------------------------------------------
def test_the_cno_and_the_deputy_cno_read_every_unit(cno, client_of, er, icu, catalogue):  # noqa: F811
    with_bls(cno, catalogue, er, "h1", "Amal Saleh", "2026-10-01")
    with_bls(cno, catalogue, icu, "h2", "Badr Omar", "2026-12-01")
    deputy = client_of("hala", Role.DEPUTY_CNO)

    for client in (cno, deputy):
        assert [(row["hospital_id"], row["unit"]["name"]) for row in expiries(client)["rows"]] == [("h1", "ER"), ("h2", "ICU")]


def test_a_unit_manager_reads_his_own_unit_only(cno, mona, omar, er, icu, catalogue):  # noqa: F811
    with_bls(cno, catalogue, er, "h1", "Amal Saleh", "2026-10-01")
    with_bls(cno, catalogue, icu, "h2", "Badr Omar", "2026-12-01")

    assert seen(mona) == [("h1", "BLS", "expired", "2026-10-01")]
    assert seen(omar) == [("h2", "BLS", "expiring", "2026-12-01")]
    assert expiries(mona)["count"] == 1


def test_a_nurse_due_to_join_is_on_both_managers_lists_and_named_by_the_reader_s_unit(cno, mona, omar, er, icu, catalogue):  # noqa: F811
    amal = with_bls(cno, catalogue, er, "h1", "Amal Saleh", "2026-10-01")
    transfer(cno, amal, icu, "2027-01")

    # As on the Nurses page: the manager of a unit she is due to join reads her skills, and names no other unit.
    assert [row["unit"]["name"] for row in expiries(mona)["rows"]] == ["ER"]
    assert [row["unit"]["name"] for row in expiries(omar)["rows"]] == ["ICU"]
    assert [row["unit"]["name"] for row in expiries(cno)["rows"]] == ["ER"]


def test_a_manager_with_no_unit_reads_an_empty_list(cno, client_of, er, catalogue):  # noqa: F811
    with_bls(cno, catalogue, er, "h1", "Amal Saleh", "2026-10-01")

    assert expiries(client_of("idle", Role.UNIT_MANAGER)) == {"horizon_days": 90, "count": 0, "rows": []}


@pytest.mark.parametrize("role", [Role.SYSTEM_ADMIN, Role.STAFF_NURSE])
def test_the_system_admin_and_nurses_are_refused(client_of, role):
    assert client_of("someone", role).get(EXPIRIES).status_code == 403


def test_the_home_count_is_worked_out_when_read(cno, er, catalogue):  # noqa: F811
    amal = with_bls(cno, catalogue, er, "h1", "Amal Saleh", "2026-12-01")
    assert expiries(cno)["count"] == 1

    assert cno.put(f"{NURSES}/{amal}/skills/{catalogue['BLS']}", {"expiry": "2028-01-01"},
                   content_type="application/json").status_code == 200
    nurse(cno, er, "h2", "Badr Omar")
    nurse(cno, er, "h3", "Carla Nunes")

    assert expiries(cno)["count"] == 2


# ---- the kind ---------------------------------------------------------------------------
def test_each_row_carries_its_kind_and_no_identifier_or_issue_date(cno, mona, er, catalogue):  # noqa: F811
    for name, kind in [("SCFHS", "licence"), ("BSN", "certificate")]:
        response = post(cno, "/api/hospital/skills", {"name": name, "kind": kind, "issuing_body": "", "expires": True,
                                                     "required_to_work": kind == "certificate",
                                                     "lets_a_nurse_precept": False, "active": True})
        assert response.status_code == 201, response.content
    amal = with_bls(cno, catalogue, er, "h1", "Amal Saleh", "2026-11-30")
    recorded(cno, amal, str(Skill.objects.get(name="SCFHS").pk), "2026-12-31", "21-RN-0412", "2024-12-15")

    for client in (cno, mona):
        listed = expiries(client)
        assert [(row["skill"], row["kind"], row["state"]) for row in listed["rows"]] == [
            ("BLS", "skill", "expiring"), ("SCFHS", "licence", "expiring"), ("BSN", "certificate", "no_record")]
        assert listed["count"] == 3
        assert all(set(row) == {"nurse_id", "hospital_id", "full_name", "unit", "skill", "kind", "state", "expiry"}
                   for row in listed["rows"])
        text = client.get(EXPIRIES).content.decode()
        assert "21-RN-0412" not in text and "2024-12-15" not in text
