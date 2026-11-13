"""An end date deactivates a nurse, at once or when it passes. Nurses are never deleted."""
import pytest
from django.contrib.sessions.models import Session
from django.test import Client

from accounts.models import Role, User
from accounts.tests.test_authorization import ENDPOINTS
from audit.models import AuditEntry
from conftest import post
from notifications.models import PushSubscription
from nurses.models import NurseProfile
from nurses.tasks import close_ended_nurses
from nurses.tests.conftest import NURSES, added, message, refused_fields
from nurses.tests.test_transfers import row, transfer
from units.models import Unit

pytestmark = pytest.mark.django_db


def end(client, nurse_id, date):
    return client.put(f"{NURSES}/{nurse_id}/end-date", {"end_date": date}, content_type="application/json")


def signed_in_nurse(answer) -> Client:
    client = Client()
    username, password = answer["nurse"]["hospital_id"], answer["generated_password"]
    assert post(client, "/api/auth/login", {"username": username, "password": password}).status_code == 200
    return client


@pytest.fixture
def added_nurse(cno, er, on_day):
    on_day(2026, 11, 15)
    return added(cno, er)


@pytest.fixture
def nurse(added_nurse):
    return added_nurse["nurse"]["id"]


def active(username="h123") -> bool:
    return User.objects.get(username=username).is_active


@pytest.mark.parametrize("date", ["2026-11-14", "2026-11-15"])
def test_an_end_date_today_or_earlier_deactivates_at_once_and_logs_them_out(cno, added_nurse, date):
    session = signed_in_nurse(added_nurse)

    response = end(cno, added_nurse["nurse"]["id"], date)

    assert response.status_code == 200
    assert response.json()["end_date"] == date and response.json()["is_active"] is False
    assert not active()
    assert session.get("/api/auth/session").json()["user"] is None


def test_a_later_end_date_leaves_them_active_until_it_has_passed(cno, nurse, on_day):
    assert end(cno, nurse, "2026-11-20").json()["is_active"] is True

    on_day(2026, 11, 20)
    close_ended_nurses(timestamp=0)
    assert active()

    on_day(2026, 11, 21)
    close_ended_nurses(timestamp=0)
    assert not active()


def test_the_job_deactivates_everyone_whose_last_day_is_over_and_nobody_else(cno, er, on_day, added_nurse):
    for number in range(5):
        end(cno, added(cno, er, hospital_id=f"n{number}")["nurse"]["id"], "2026-11-20")
    end(cno, added_nurse["nurse"]["id"], "2026-11-20")
    session = signed_in_nurse(added_nurse)
    keeps_going = added(cno, er, hospital_id="stays")
    other_session = signed_in_nurse(keeps_going)
    on_day(2026, 11, 21)

    before = AuditEntry.objects.filter(action="user.deactivated").count()
    close_ended_nurses(timestamp=0)

    assert not User.objects.filter(role=Role.STAFF_NURSE, is_active=True).exclude(username="stays").exists()
    assert active("stays")
    assert session.get("/api/auth/session").json()["user"] is None
    assert other_session.get("/api/auth/session").json()["user"]["username"] == "stays"
    entries = AuditEntry.objects.filter(action="user.deactivated")
    assert entries.count() == before + 6
    assert entries.filter(target="h123").get().actor is None


def test_the_job_ends_their_push_subscriptions_and_nobody_elses(cno, er, on_day, added_nurse):
    end(cno, added_nurse["nurse"]["id"], "2026-11-20")
    stays = User.objects.get(username=added(cno, er, hospital_id="stays")["nurse"]["hospital_id"])
    for owner, device in [(User.objects.get(username="h123"), "phone"), (stays, "phone-2")]:
        PushSubscription.objects.create(user=owner, endpoint=f"https://fcm.googleapis.com/fcm/send/{device}", p256dh="k",
                                        auth="a")
    on_day(2026, 11, 21)

    close_ended_nurses(timestamp=0)

    assert list(PushSubscription.objects.values_list("user__username", flat=True)) == ["stays"]


def test_an_end_date_today_ends_their_push_subscriptions_at_once(cno, nurse):
    PushSubscription.objects.create(user=User.objects.get(username="h123"), endpoint="https://fcm.googleapis.com/fcm/send/p",
                                    p256dh="k", auth="a")

    end(cno, nurse, "2026-11-15")

    assert not PushSubscription.objects.exists()


def test_the_job_twice_does_nothing_more(cno, nurse, on_day):
    end(cno, nurse, "2026-11-20")
    on_day(2026, 11, 21)

    close_ended_nurses(timestamp=0)
    close_ended_nurses(timestamp=0)

    assert AuditEntry.objects.filter(action="user.deactivated", target="h123").count() == 1


def test_a_nurse_left_inactive_with_no_end_date_comes_back_through_a_later_date_and_not_through_saving_nothing(cno, nurse):
    """The job could once leave this behind (see `test_two_at_once`). Saving what is already saved stays a no-op that
    writes nothing, and the way back is the one the form offers."""
    User.objects.filter(username="h123").update(is_active=False)
    entries = AuditEntry.objects.count()

    assert end(cno, nurse, None).json()["is_active"] is False
    assert AuditEntry.objects.count() == entries

    assert end(cno, nurse, "2026-12-31").json()["is_active"] is True
    assert end(cno, nurse, None).json()["is_active"] is True
    assert active()


def test_the_job_is_registered_to_run_every_hour():
    from procrastinate.contrib.django import app
    tasks = {task.task.name: task.cron for task in app.periodic_registry.periodic_tasks.values()}
    assert tasks["nurses.tasks.close_ended_nurses"] == "5 * * * *"


def test_an_end_date_before_the_start_date_is_refused(cno, nurse):
    assert refused_fields(end(cno, nurse, "2026-01-09")) == {("end_date",)}


def test_a_nurse_added_by_mistake_and_due_to_start_later_can_be_ended_today(cno, er, on_day):
    on_day(2026, 11, 15)
    nurse = added(cno, er, start_date="2027-03-01")["nurse"]["id"]

    assert refused_fields(end(cno, nurse, "2027-02-01")) == {("end_date",)}
    assert end(cno, nurse, "2026-11-15").json()["is_active"] is False
    assert er.nurse_count() == 0


@pytest.mark.parametrize("value", ["soon", 20261120, "20/11/2026"])
def test_what_is_not_a_date_is_refused_beside_its_field(cno, nurse, value):
    assert refused_fields(end(cno, nurse, value)) == {("end_date",)}


def test_clearing_the_end_date_reactivates_with_the_old_password(cno, added_nurse):
    nurse = added_nurse["nurse"]["id"]
    end(cno, nurse, "2026-11-10")

    response = end(cno, nurse, None)

    assert response.json()["end_date"] is None and response.json()["is_active"] is True
    assert AuditEntry.objects.filter(action="user.reactivated", target="h123").exists()
    signed_in_nurse(added_nurse)


def test_a_later_end_date_also_reactivates(cno, nurse):
    end(cno, nurse, "2026-11-10")

    assert end(cno, nurse, "2026-12-31").json()["is_active"] is True


def test_the_end_date_is_audited_without_any_rostering_attribute(cno, nurse):
    end(cno, nurse, "2026-11-20")

    entry = AuditEntry.objects.get(action="nurse.end_date_changed")
    assert (entry.target, entry.before, entry.after) == ("h123", {"end_date": None}, {"end_date": "2026-11-20"})


def test_the_same_end_date_again_writes_nothing(cno, nurse):
    end(cno, nurse, "2026-11-20")
    assert end(cno, nurse, "2026-11-20").status_code == 200
    assert AuditEntry.objects.filter(action="nurse.end_date_changed").count() == 1


def test_there_is_no_way_to_delete_a_nurse():
    assert not [path for method, path, _ in ENDPOINTS if method == "DELETE" and "/nurses" in path]


# ---- with transfers ---------------------------------------------------------------------------------
def test_a_transfer_after_the_end_date_is_refused(cno, nurse, icu):
    end(cno, nurse, "2026-12-31")

    assert refused_fields(transfer(cno, nurse, icu, "2027-01")) == {("from_month",)}
    assert transfer(cno, nurse, icu, "2026-12").status_code == 200


def test_an_end_date_cancels_a_transfer_after_it_and_says_so_in_the_audit_log(cno, nurse, icu):
    transfer(cno, nurse, icu, "2027-01")

    response = end(cno, nurse, "2026-11-30")

    assert response.json()["transfers"] == []
    assert icu.nurse_count() == 0
    entry = AuditEntry.objects.get(action="nurse.end_date_changed")
    assert entry.after == {"end_date": "2026-11-30", "transfers_cancelled": [{"unit": "ICU", "from_month": "2027-01"}]}


def test_an_end_date_does_not_cancel_the_placement_in_force(cno, nurse, er, icu):
    transfer(cno, nurse, icu, "2026-11")

    assert end(cno, nurse, "2026-11-01").json()["unit"]["name"] == "ICU"


# ---- what "belongs" means when a nurse has ended --------------------------------------------------
def archive(client, unit):
    return post(client, f"/api/units/{unit.pk}/archive", {})


def test_a_deactivated_nurse_no_longer_belongs(cno, er, nurse):
    assert er.nurse_count() == 1

    end(cno, nurse, "2026-11-10")

    assert er.nurse_count() == 0
    assert archive(cno, er).status_code == 200


def test_a_nurse_with_a_later_end_date_belongs_until_it_has_passed(cno, er, nurse, on_day):
    end(cno, nurse, "2026-11-20")
    assert er.nurse_count() == 1

    on_day(2026, 11, 21)  # Even before the hourly job has run.
    assert er.nurse_count() == 0


def test_reactivating_a_nurse_into_an_archived_unit_is_refused(cno, er, nurse):
    end(cno, nurse, "2026-11-10")
    archive(cno, er)

    response = end(cno, nurse, None)

    assert refused_fields(response) == {("end_date",)}
    assert "ER is archived" in message(response)
    assert not active()
    assert NurseProfile.objects.get(pk=nurse).end_date is not None


def test_a_deactivated_nurse_can_be_transferred_out_of_an_archived_unit_and_then_reactivated(cno, er, nurse, icu):
    end(cno, nurse, "2026-11-10")
    archive(cno, er)

    assert transfer(cno, nurse, icu, "2026-11").status_code == 200
    assert end(cno, nurse, None).json()["is_active"] is True
    assert icu.nurse_count() == 1


def test_a_transfer_waiting_for_a_unit_since_archived_blocks_reactivating(cno, nurse, icu):
    transfer(cno, nurse, icu, "2027-01")
    end(cno, nurse, "2027-02-01")
    # Past the waiting transfer and past the end date: the nurse has left ICU's books too.
    NurseProfile.objects.filter(pk=nurse).update(end_date="2026-11-10")
    User.objects.filter(username="h123").update(is_active=False)
    Unit.objects.filter(pk=icu.pk).update(archived=True)

    assert "ICU is archived" in message(end(cno, nurse, None))


def test_the_unit_stays_visible_on_a_deactivated_nurses_row(cno, er, nurse):
    end(cno, nurse, "2026-11-10")
    archive(cno, er)

    assert row(cno, nurse)["unit"]["name"] == "ER"
