"""A transfer takes effect from a chosen month, and until then the nurse belongs where they are."""
import pytest

from audit.models import AuditEntry
from conftest import post
from nurses.tests.conftest import NURSES, added, make_unit, message, refused_fields
from units.models import Unit

pytestmark = pytest.mark.django_db


def transfer(client, nurse_id, unit, month):
    return post(client, f"{NURSES}/{nurse_id}/transfer", {"unit_id": str(unit.pk), "from_month": month})


def row(client, nurse_id):
    return next(row for row in client.get(NURSES).json() if row["id"] == nurse_id)


@pytest.fixture
def nurse(cno, er, on_day):
    on_day(2026, 11, 15)
    return added(cno, er)["nurse"]["id"]


def test_a_transfer_from_this_month_moves_the_nurse_at_once(cno, nurse, icu):
    response = transfer(cno, nurse, icu, "2026-11")

    assert response.status_code == 200
    assert response.json()["unit"]["name"] == "ICU" and response.json()["transfers"] == []


def test_a_transfer_from_a_later_month_waits_for_that_month(cno, nurse, icu, on_day):
    answer = transfer(cno, nurse, icu, "2027-01").json()

    assert answer["unit"]["name"] == "ER"
    assert answer["transfers"] == [{"from_month": "2027-01", "unit": {"id": str(icu.pk), "name": "ICU"}}]

    on_day(2026, 12, 31)
    assert row(cno, nurse)["unit"]["name"] == "ER"
    on_day(2027, 1, 1)
    assert row(cno, nurse)["unit"]["name"] == "ICU" and row(cno, nurse)["transfers"] == []


def test_a_month_already_over_is_refused(cno, nurse, icu):
    assert refused_fields(transfer(cno, nurse, icu, "2026-10")) == {("from_month",)}


@pytest.mark.parametrize("month", ["2026-13", "2026", "2026-11-01", "0000-01", 202611])
def test_what_is_not_a_month_is_refused_beside_its_field(cno, nurse, icu, month):
    assert refused_fields(transfer(cno, nurse, icu, month)) == {("from_month",)}


def test_a_nurse_cannot_transfer_into_an_archived_unit(cno, nurse):
    old = make_unit("Old", archived=True)

    assert refused_fields(transfer(cno, nurse, old, "2026-12")) == {("unit_id",)}
    assert row(cno, nurse)["transfers"] == []


def test_a_transfer_to_the_unit_they_are_already_in_is_refused(cno, nurse, er):
    response = transfer(cno, nurse, er, "2026-12")

    assert refused_fields(response) == {("unit_id",)}
    assert "already" in message(response)


def test_a_later_transfer_to_the_same_or_an_earlier_month_replaces_the_one_waiting(cno, nurse, icu, db):
    ccu = make_unit("CCU")
    transfer(cno, nurse, icu, "2027-02")

    transfer(cno, nurse, ccu, "2027-01")

    assert [m["from_month"] for m in row(cno, nurse)["transfers"]] == ["2027-01"]
    assert row(cno, nurse)["transfers"][0]["unit"]["name"] == "CCU"


def test_two_transfers_can_wait_one_after_the_other(cno, nurse, icu, db):
    ccu = make_unit("CCU")
    transfer(cno, nurse, icu, "2027-01")

    transfer(cno, nurse, ccu, "2027-03")

    assert [m["unit"]["name"] for m in row(cno, nurse)["transfers"]] == ["ICU", "CCU"]


def test_transferring_back_to_the_unit_they_are_in_cancels_the_one_waiting(cno, nurse, er, icu):
    transfer(cno, nurse, icu, "2027-01")

    response = transfer(cno, nurse, er, "2027-01")

    assert response.status_code == 200
    assert response.json()["unit"]["name"] == "ER" and response.json()["transfers"] == []
    assert icu.nurse_count() == 0


def test_a_transfer_is_audited_with_units_and_month_only(cno, nurse, icu):
    transfer(cno, nurse, icu, "2027-01")

    entry = AuditEntry.objects.get(action="nurse.transferred")
    assert entry.target == "h123"
    assert entry.before == {"unit": "ER"}
    assert entry.after == {"unit": "ICU", "from_month": "2027-01"}


def test_an_unknown_nurse_is_404_for_the_cno(cno, icu):
    assert transfer(cno, "019a0000-0000-7000-8000-000000000001", icu, "2027-01").status_code == 404


# ---- what "belongs" means, which is what stops a unit being archived -------------------------------
def archive(client, unit):
    return post(client, f"/api/units/{unit.pk}/archive", {})


def test_archiving_is_refused_while_a_nurse_belongs_and_names_the_count(cno, er, nurse):
    added(cno, er, hospital_id="h124")

    response = archive(cno, er)

    assert refused_fields(response) == {("archived",)}
    assert "2 nurses belong to ER" in message(response)
    assert Unit.objects.get(pk=er.pk).archived is False
    assert not AuditEntry.objects.filter(action="unit.archived").exists()


def test_the_refusal_says_one_nurse_in_the_singular(cno, er, nurse):
    assert "1 nurse belongs to ER" in message(archive(cno, er))


def test_a_nurse_counts_for_a_unit_they_are_due_to_join(cno, nurse, icu):
    transfer(cno, nurse, icu, "2027-01")

    assert icu.nurse_count() == 1
    assert "1 nurse" in message(archive(cno, icu))


def test_a_nurse_counts_for_the_unit_they_are_due_to_leave_until_they_leave(cno, er, nurse, icu, on_day):
    transfer(cno, nurse, icu, "2027-01")
    assert er.nurse_count() == 1

    on_day(2027, 1, 1)

    assert er.nurse_count() == 0
    assert archive(cno, er).status_code == 200


def test_a_transfer_from_this_month_frees_the_unit_at_once(cno, er, nurse, icu):
    transfer(cno, nurse, icu, "2026-11")

    assert er.nurse_count() == 0
    assert archive(cno, er).status_code == 200


def test_the_same_transfer_again_changes_nothing(cno, nurse, icu):
    transfer(cno, nurse, icu, "2027-01")

    assert refused_fields(transfer(cno, nurse, icu, "2027-01")) == {("unit_id",)}
    assert AuditEntry.objects.filter(action="nurse.transferred").count() == 1
