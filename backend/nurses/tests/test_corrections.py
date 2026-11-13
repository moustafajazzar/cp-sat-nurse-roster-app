"""The CNO corrects what was typed wrong when a nurse was added: their name, gender and start date."""
from uuid import uuid4

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.models import Role, User
from audit.models import AuditEntry
from nurses import membership
from nurses.models import NurseProfile, Placement
from nurses.tests.conftest import NURSES, START, added, make_unit, message, refused_fields
from nurses.tests.test_end_dates import end
from nurses.tests.test_managers import given
from nurses.tests.test_transfers import row, transfer
from units.models import Unit

pytestmark = pytest.mark.django_db

AS_ADDED = {"full_name": "Amal Saleh", "gender": "F", "start_date": START}


def correct(client, nurse_id, **fields):
    return client.put(f"{NURSES}/{nurse_id}/details", AS_ADDED | fields, content_type="application/json")


def corrected(client, nurse_id, **fields) -> dict:
    response = correct(client, nurse_id, **fields)
    assert response.status_code == 200, response.content
    return response.json()


def entries():
    return AuditEntry.objects.filter(action="nurse.details_corrected")


def placements(nurse_id) -> list[tuple[str, str]]:
    return [(p.unit.name, p.from_month.strftime("%Y-%m")) for p in Placement.objects.filter(nurse_id=nurse_id)]


@pytest.fixture
def nurse(cno, er, on_day):
    on_day(2026, 11, 15)
    return added(cno, er)["nurse"]["id"]


def test_a_name_is_corrected_and_the_entry_holds_it(cno, nurse):
    answer = corrected(cno, nurse, full_name="  Amal Salih ")

    assert answer["nurse"]["full_name"] == "Amal Salih" and answer["first_placement_extended"] is None
    assert User.objects.get(username="h123").full_name == "Amal Salih"
    entry = entries().get()
    assert entry.target == "h123" and entry.actor.username == "chief"
    assert entry.before == {"full_name": "Amal Saleh"} and entry.after == {"full_name": "Amal Salih"}


def test_a_gender_is_corrected_and_the_entry_says_only_that_it_changed(cno, nurse):
    assert corrected(cno, nurse, gender="M")["nurse"]["gender"] == "M"

    assert NurseProfile.objects.get(pk=nurse).gender == "M"
    entry = entries().get()
    assert entry.before == {} and entry.after == {"gender_changed": True}
    assert not {"F", "M"} & {*map(str, entry.after.values())}


def test_a_start_date_is_corrected_and_the_entry_holds_it(cno, nurse):
    assert corrected(cno, nurse, start_date="2026-01-20")["nurse"]["start_date"] == "2026-01-20"

    entry = entries().get()
    assert entry.before == {"start_date": START} and entry.after == {"start_date": "2026-01-20"}


def test_all_three_at_once_make_one_entry(cno, nurse):
    corrected(cno, nurse, full_name="Amal Salih", gender="M", start_date="2026-01-20")

    entry = entries().get()
    assert set(entry.before) == {"full_name", "start_date"}
    assert set(entry.after) == {"full_name", "start_date", "gender_changed"}


def test_a_save_with_no_change_writes_nothing(cno, nurse):
    with CaptureQueriesContext(connection) as queries:
        answer = corrected(cno, nurse, full_name=" Amal Saleh ")

    assert answer["nurse"]["full_name"] == "Amal Saleh" and answer["first_placement_extended"] is None
    # The session's own save aside: the request itself touches no row of ours.
    writes = [query["sql"] for query in queries if query["sql"].startswith(("INSERT", "UPDATE", "DELETE"))
              and "django_session" not in query["sql"]]
    assert writes == []
    assert not entries().exists()


@pytest.mark.parametrize(("field", "value"), [("full_name", "   "), ("full_name", "x" * 201), ("full_name", "a\x00b"),
                                              ("gender", "X"), ("gender", ""),
                                              ("start_date", "10/01/2026"), ("start_date", "0000-01-01"), ("start_date", None)])
def test_what_is_not_a_value_is_refused_beside_its_field(cno, nurse, field, value):
    assert refused_fields(correct(cno, nurse, **{field: value})) == {(field,)}
    assert not entries().exists()


def test_the_hospital_id_cannot_be_corrected(cno, nurse):
    assert refused_fields(correct(cno, nurse, hospital_id="h999")) == {("hospital_id",)}
    assert User.objects.filter(username="h123").exists()


def test_no_such_nurse(cno, db):
    assert correct(cno, uuid4()).status_code == 404


@pytest.mark.parametrize("role", [Role.UNIT_MANAGER, Role.STAFF_NURSE, Role.DEPUTY_CNO, Role.SYSTEM_ADMIN])
def test_only_the_cno_corrects(client_of, cno, nurse, role):
    assert correct(client_of("other", role), nurse, full_name="Someone Else").status_code == 403
    assert User.objects.get(username="h123").full_name == "Amal Saleh"


def test_a_nurses_own_manager_cannot_correct_them(client_of, cno, er, nurse):
    manager = client_of("mona", Role.UNIT_MANAGER)
    Unit.objects.filter(pk=er.pk).update(manager=User.objects.get(username="mona"))

    assert correct(manager, nurse, gender="M").status_code == 403


# ---- the start date against the end date ------------------------------------------------------------------------------
def test_a_start_date_after_the_end_date_is_refused_beside_its_field(cno, nurse):
    end(cno, nurse, "2026-06-30")

    response = correct(cno, nurse, start_date="2026-07-01")

    assert refused_fields(response) == {("start_date",)}
    assert "after the end date" in message(response)
    assert NurseProfile.objects.get(pk=nurse).start_date.isoformat() == START


def test_a_start_date_on_the_end_date_is_taken(cno, nurse):
    end(cno, nurse, "2026-06-30")

    assert corrected(cno, nurse, start_date="2026-06-30")["nurse"]["start_date"] == "2026-06-30"


def test_a_later_end_date_still_holds_a_later_start_date_back(cno, nurse):
    end(cno, nurse, "2026-12-20")

    assert refused_fields(correct(cno, nurse, start_date="2026-12-21")) == {("start_date",)}
    assert refused_fields(correct(cno, nurse, start_date="2027-02-01")) == {("start_date",)}


def test_a_nurse_who_has_not_started_may_be_corrected_past_an_end_date_of_today_or_earlier(cno, er, on_day):
    """The exception: one added by mistake was ended before they started, and stays never present."""
    on_day(2026, 11, 15)
    nurse = added(cno, er, start_date="2026-12-01")["nurse"]["id"]
    end(cno, nurse, "2026-11-15")

    answer = corrected(cno, nurse, start_date="2026-12-15")

    assert answer["nurse"]["start_date"] == "2026-12-15" and answer["nurse"]["end_date"] == "2026-11-15"
    assert answer["nurse"]["is_active"] is False


def test_a_nurse_who_worked_is_not_corrected_into_one_never_present(cno, nurse):
    """The end date form would accept this pair for a nurse who has not started. This one has, so their days count."""
    end(cno, nurse, "2026-11-15")

    assert refused_fields(correct(cno, nurse, start_date="2026-12-01")) == {("start_date",)}


def test_a_start_date_that_has_passed_may_not_pass_the_end_date(cno, er, on_day):
    on_day(2026, 11, 15)
    nurse = added(cno, er, start_date="2026-12-01")["nurse"]["id"]
    end(cno, nurse, "2026-11-15")
    on_day(2026, 12, 10)

    assert refused_fields(correct(cno, nurse, start_date="2026-12-05")) == {("start_date",)}


def test_a_nurse_never_present_still_has_their_name_and_gender_corrected(cno, er, on_day):
    on_day(2026, 11, 15)
    start = "2026-12-01"
    nurse = added(cno, er, start_date=start)["nurse"]["id"]
    end(cno, nurse, "2026-11-15")
    on_day(2026, 12, 10)

    answer = corrected(cno, nurse, start_date=start, full_name="Amal Salih", gender="M")

    assert (answer["nurse"]["full_name"], answer["nurse"]["gender"]) == ("Amal Salih", "M")


def test_a_correction_leaves_an_account_as_it_was(cno, nurse):
    end(cno, nurse, "2026-11-10")

    answer = corrected(cno, nurse, start_date="2026-02-01", full_name="Amal Salih")

    assert answer["nurse"]["is_active"] is False and answer["nurse"]["end_date"] == "2026-11-10"
    assert not User.objects.get(username="h123").is_active


# ---- the start date against the placements ------------------------------------------------------------------------------
def test_a_start_date_inside_the_first_month_or_later_touches_no_placement(cno, nurse):
    for start in ("2026-01-01", "2026-03-15", "2027-05-01"):
        assert corrected(cno, nurse, start_date=start)["first_placement_extended"] is None
    assert placements(nurse) == [("ER", "2026-01")]


def test_a_start_date_before_the_first_placement_brings_it_back_and_says_so(cno, nurse, er):
    answer = corrected(cno, nurse, start_date="2025-10-05")

    assert answer["first_placement_extended"] == {"unit": {"id": str(er.pk), "name": "ER"}, "from_month": "2025-10"}
    assert placements(nurse) == [("ER", "2025-10")]
    entry = entries().get()
    assert entry.before["first_placement"] == {"unit": "ER", "from_month": "2026-01"}
    assert entry.after["first_placement"] == {"unit": "ER", "from_month": "2025-10"}


def test_moving_a_start_date_later_and_back_never_leaves_a_month_without_a_unit(cno, nurse):
    corrected(cno, nurse, start_date="2026-08-01")
    assert placements(nurse) == [("ER", "2026-01")]

    assert corrected(cno, nurse, start_date="2026-02-01")["first_placement_extended"] is None


def test_a_nurse_added_before_they_start_keeps_their_unit_when_the_start_date_comes_back(cno, er, on_day):
    on_day(2026, 11, 15)
    nurse = added(cno, er, start_date="2027-02-01")["nurse"]["id"]
    assert placements(nurse) == [("ER", "2026-11")]

    assert corrected(cno, nurse, start_date="2027-01-15")["first_placement_extended"] is None
    assert corrected(cno, nurse, start_date="2026-09-15")["first_placement_extended"]["from_month"] == "2026-09"
    assert placements(nurse) == [("ER", "2026-09")]


def test_a_transfer_is_not_rewritten_by_a_start_date_going_back(cno, nurse, icu, on_day):
    transfer(cno, nurse, icu, "2027-01")
    on_day(2027, 3, 1)
    transfer(cno, nurse, make_unit("Ward"), "2027-04")

    answer = corrected(cno, nurse, start_date="2025-06-01")

    assert answer["first_placement_extended"]["unit"]["name"] == "ER"
    assert placements(nurse) == [("ER", "2025-06"), ("ICU", "2027-01"), ("Ward", "2027-04")]
    assert answer["nurse"]["unit"]["name"] == "ICU"
    assert [t["unit"]["name"] for t in answer["nurse"]["transfers"]] == ["Ward"]


def test_a_start_date_after_a_coming_transfer_leaves_it_waiting(cno, nurse, icu):
    transfer(cno, nurse, icu, "2027-01")

    answer = corrected(cno, nurse, start_date="2027-03-01")

    assert answer["nurse"]["unit"]["name"] == "ER"
    assert [t["from_month"] for t in answer["nurse"]["transfers"]] == ["2027-01"]


def test_a_nurse_stays_on_their_units_list_and_count_whatever_the_start_date(cno, nurse, er):
    corrected(cno, nurse, start_date="2027-03-01")
    assert er.nurse_count() == 1
    corrected(cno, nurse, start_date="2025-03-01")
    assert er.nurse_count() == 1
    assert row(cno, nurse)["unit"]["name"] == "ER"


def test_an_archived_unit_in_a_nurses_past_does_not_stop_a_correction(cno, nurse, icu):
    transfer(cno, nurse, icu, "2026-11")
    end(cno, nurse, "2026-11-10")
    Unit.objects.filter(pk=icu.pk).update(archived=True, manager=None)

    assert corrected(cno, nurse, full_name="Amal Salih")["nurse"]["full_name"] == "Amal Salih"


# ---- a manager's profile ------------------------------------------------------------------------------------------------
@pytest.fixture
def mona(make_user, er):
    user = make_user("mona", Role.UNIT_MANAGER, full_name="Mona Ali")
    Unit.objects.filter(pk=er.pk).update(manager=user)
    return user


def manager_nurse(cno, mona, er, on_day) -> str:
    on_day(2026, 11, 15)
    return given(cno, mona, er)["id"]


def test_a_managers_gender_and_start_date_are_corrected(cno, mona, er, on_day):
    nurse = manager_nurse(cno, mona, er, on_day)

    answer = corrected(cno, nurse, full_name="Mona Ali", gender="M", start_date="2026-02-02")

    assert (answer["nurse"]["gender"], answer["nurse"]["start_date"]) == ("M", "2026-02-02")
    assert answer["nurse"]["role"] == "unit_manager"


def test_a_start_date_going_back_keeps_a_managers_profile_in_the_unit_they_hold(cno, mona, er, on_day):
    nurse = manager_nurse(cno, mona, er, on_day)

    assert corrected(cno, nurse, full_name="Mona Ali", start_date="2025-02-02")["first_placement_extended"]
    assert membership.placed_outside(mona, er.pk) is None


def test_a_managers_name_is_refused_because_it_is_on_their_account(cno, mona, er, on_day):
    nurse = manager_nurse(cno, mona, er, on_day)

    response = correct(cno, nurse, full_name="Mona Saleh")

    assert refused_fields(response) == {("full_name",)}
    assert "The System Admin can correct it" in message(response)
    mona.refresh_from_db()
    assert mona.full_name == "Mona Ali"
    assert not entries().exists()


def test_the_system_admin_corrects_a_managers_name_and_the_nurses_page_shows_it(cno, mona, er, on_day, client_of):
    nurse = manager_nurse(cno, mona, er, on_day)

    response = client_of("admin", Role.SYSTEM_ADMIN).put(f"/api/users/{mona.pk}/full-name", {"full_name": "Mona Saleh"},
                                                         content_type="application/json")

    assert response.status_code == 200
    row = next(row for row in cno.get(NURSES).json() if row["id"] == nurse)
    assert (row["full_name"], row["hospital_id"]) == ("Mona Saleh", "mona")
    assert corrected(cno, nurse, full_name="Mona Saleh", gender="M")["nurse"]["gender"] == "M"


def test_a_manager_renamed_while_the_form_was_open_still_has_their_gender_corrected(cno, mona, er, on_day):
    nurse = manager_nurse(cno, mona, er, on_day)
    User.objects.filter(pk=mona.pk).update(full_name="Mona Saleh")

    response = cno.put(f"{NURSES}/{nurse}/details", {"gender": "M", "start_date": START}, content_type="application/json")

    assert response.status_code == 200
    assert (response.json()["nurse"]["full_name"], response.json()["nurse"]["gender"]) == ("Mona Saleh", "M")
    assert set(entries().get().after) == {"gender_changed"}


def test_a_refused_name_saves_nothing_else_and_both_refusals_come_together(cno, mona, er, on_day):
    nurse = manager_nurse(cno, mona, er, on_day)
    end(cno, nurse, "2026-06-30")

    response = correct(cno, nurse, full_name="Mona Saleh", gender="M", start_date="2026-07-01")

    assert refused_fields(response) == {("full_name",), ("start_date",)}
    assert NurseProfile.objects.get(pk=nurse).gender == "F"
