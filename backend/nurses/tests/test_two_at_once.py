"""Two transactions at once, on the real PostgreSQL. One stays open with its change made; the other starts on its own
connection and is seen waiting for a lock, in `pg_stat_activity`, before the first commits. So the order is forced
rather than hoped for, and no test here sleeps its way to it."""
import time
from concurrent.futures import Future, ThreadPoolExecutor

import pytest
from django.db import connection, transaction

from accounts import provisioning
from accounts.models import Role, User
from audit.models import AuditEntry
from conftest import post
from nurses.tasks import close_ended_nurses
from nurses.tests.conftest import added, make_unit
from nurses.tests.test_end_dates import active, end, signed_in_nurse
from nurses.tests.test_skills import change_skill
from nurses.tests.test_skills import record as recorded_skill
from nurses.tests.test_scoping import reset
from nurses.tests.test_transfers import transfer

pytestmark = pytest.mark.django_db(transaction=True)

WAIT_SECONDS = 30


def alongside(pool: ThreadPoolExecutor, work) -> Future:
    """Runs `work` on a connection of its own, and returns once it is waiting for a lock or has finished."""
    def run():
        try:
            return work()
        finally:
            connection.close()

    started = pool.submit(run)
    deadline = time.monotonic() + WAIT_SECONDS
    while not started.done():
        with connection.cursor() as cursor:
            # Within a transaction PostgreSQL would otherwise answer from what it read the first time.
            cursor.execute("SELECT pg_stat_clear_snapshot()")
            cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                           "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()")
            if cursor.fetchone()[0]:
                return started
        assert time.monotonic() < deadline, "It neither waited for a lock nor finished."
        time.sleep(0.01)
    return started


@pytest.fixture
def pool():
    with ThreadPoolExecutor(max_workers=1) as executor:
        yield executor


def the_job():
    close_ended_nurses(timestamp=0)


# ---- the hourly job and an end date --------------------------------------------------------------------
@pytest.fixture
def last_day_over(cno, er, on_day):
    """Two nurses whose last day was yesterday and whom the job has not reached yet."""
    on_day(2026, 11, 15)
    answer = added(cno, er)
    assert end(cno, answer["nurse"]["id"], "2026-11-20").status_code == 200
    assert end(cno, added(cno, er, hospital_id="leaving")["nurse"]["id"], "2026-11-20").status_code == 200
    on_day(2026, 11, 21)
    return answer


@pytest.mark.parametrize("date", [None, "2026-12-31"])
def test_the_job_spares_a_nurse_whose_end_date_changes_while_it_waits(cno, last_day_over, pool, monkeypatch, date):
    """Only the profile's row changes, so after the wait PostgreSQL would not ask the question again. The job has to."""
    session = signed_in_nurse(last_day_over)
    ended = []
    real_end_sessions = provisioning._end_sessions
    monkeypatch.setattr(provisioning, "_end_sessions", lambda ids: (ended.append(ids), real_end_sessions(ids)))

    with transaction.atomic():
        assert end(cno, last_day_over["nurse"]["id"], date).status_code == 200
        job = alongside(pool, the_job)
        assert not job.done()
    job.result(timeout=WAIT_SECONDS)

    assert active()
    assert session.get("/api/auth/session").json()["user"]["username"] == "h123"
    assert not AuditEntry.objects.filter(action="user.deactivated", target="h123").exists()
    # The other nurse goes as ever, and the sessions are still read once.
    assert not active("leaving")
    assert ended == [{str(User.objects.get(username="leaving").pk)}]


def test_the_job_still_ends_a_nurse_whose_row_it_only_waited_for(cno, last_day_over, pool):
    with transaction.atomic():
        User.objects.select_for_update().get(username="h123")
        job = alongside(pool, the_job)
        assert not job.done()
    job.result(timeout=WAIT_SECONDS)

    assert not active()


def test_clearing_the_date_after_the_job_brings_the_nurse_back(cno, last_day_over, pool):
    with transaction.atomic():
        the_job()
        cleared = alongside(pool, lambda: end(cno, last_day_over["nurse"]["id"], None))
        assert not cleared.done()

    assert cleared.result(timeout=WAIT_SECONDS).status_code == 200
    assert active()


# ---- a manager's password reset, and who is theirs ---------------------------------------------------
@pytest.fixture
def mona(client_of):
    return client_of("mona", Role.UNIT_MANAGER)


@pytest.fixture
def monas_unit(mona):
    return make_unit("ER", User.objects.get(username="mona"))


@pytest.fixture
def nurse(cno, monas_unit, on_day):
    on_day(2026, 11, 15)
    return added(cno, monas_unit)["nurse"]["id"]


def assert_not_reset(username="h123"):
    assert not AuditEntry.objects.filter(action="user.password_reset", target=username).exists()


def test_a_manager_gets_no_password_for_a_nurse_transferring_away_at_that_moment(cno, mona, nurse, icu, pool):
    with transaction.atomic():
        assert transfer(cno, nurse, icu, "2026-11").status_code == 200
        asked = alongside(pool, lambda: reset(mona, nurse))
        assert not asked.done()

    response = asked.result(timeout=WAIT_SECONDS)
    assert response.status_code == 403 and "generated_password" not in response.json()
    assert_not_reset()


def test_the_manager_a_nurse_transfers_to_may_reset_once_the_transfer_is_in(cno, client_of, nurse, pool):
    omar = client_of("omar", Role.UNIT_MANAGER)
    ccu = make_unit("CCU", User.objects.get(username="omar"))

    with transaction.atomic():
        assert transfer(cno, nurse, ccu, "2026-11").status_code == 200
        asked = alongside(pool, lambda: reset(omar, nurse))
        assert not asked.done()

    assert asked.result(timeout=WAIT_SECONDS).status_code == 200


def test_a_manager_taken_off_the_unit_at_that_moment_gets_no_password(cno, mona, monas_unit, nurse, pool):
    with transaction.atomic():
        taken_off = cno.put(f"/api/units/{monas_unit.pk}", {"name": "ER", "manager_id": None}, content_type="application/json")
        assert taken_off.status_code == 200
        asked = alongside(pool, lambda: reset(mona, nurse))
        assert not asked.done()

    assert asked.result(timeout=WAIT_SECONDS).status_code == 403
    assert_not_reset()


# ---- a nurse's skill and the catalogue's "expires" ----------------------------------------------------
@pytest.fixture
def icu_skill(cno) -> str:
    response = post(cno, "/api/hospital/skills", {"name": "ICU", "kind": "skill", "issuing_body": "",
                                                  "expires": False, "required_to_work": False,
                                                  "lets_a_nurse_precept": False, "active": True})
    assert response.status_code == 201, response.content
    return response.json()["id"]


def test_expires_turned_on_while_a_dateless_record_is_made_waits_and_is_refused(cno, nurse, icu_skill, pool):
    with transaction.atomic():
        assert recorded_skill(cno, nurse, icu_skill).status_code == 201
        turned = alongside(pool, lambda: change_skill(cno, "ICU", expires=True))
        assert not turned.done()

    assert turned.result(timeout=WAIT_SECONDS).status_code == 422


def test_a_dateless_record_made_while_expires_is_turned_on_waits_and_is_refused(cno, nurse, icu_skill, pool):
    with transaction.atomic():
        assert change_skill(cno, "ICU", expires=True).status_code == 200
        made = alongside(pool, lambda: recorded_skill(cno, nurse, icu_skill))
        assert not made.done()

    assert made.result(timeout=WAIT_SECONDS).status_code == 422


# ---- one identifier on two nurses' records of one entry ------------------------------------
@pytest.fixture
def licence(cno) -> str:
    response = post(cno, "/api/hospital/skills", {"name": "SCFHS", "kind": "licence", "issuing_body": "",
                                                  "expires": True, "required_to_work": False,
                                                  "lets_a_nurse_precept": False, "active": True})
    assert response.status_code == 201, response.content
    return response.json()["id"]


def test_two_records_of_one_identifier_on_one_entry_at_once_the_second_waits_and_is_refused(cno, monas_unit, nurse,
                                                                                            licence, pool):
    badr = added(cno, monas_unit, "h456", full_name="Badr Ali")["nurse"]["id"]

    with transaction.atomic():
        assert recorded_skill(cno, nurse, licence, "2028-03-31", "21-RN-0412").status_code == 201
        second = alongside(pool, lambda: recorded_skill(cno, badr, licence, "2028-03-31", "21-RN-0412"))
        assert not second.done()

    response = second.result(timeout=WAIT_SECONDS)
    assert response.status_code == 422, response.content
    assert response.json()["detail"][0]["msg"].startswith("Amal Saleh already holds SCFHS with this identifier.")


def test_the_deputy_cnos_record_takes_the_cnos_locks_and_waits_for_her_record_of_the_same_identifier(
        cno, client_of, monas_unit, nurse, licence, pool):
    """The Deputy CNO's writes take the hospital settings' lock and then the nurse's, as the CNO's do."""
    deputy = client_of("hala", Role.DEPUTY_CNO)
    badr = added(cno, monas_unit, "h456", full_name="Badr Ali")["nurse"]["id"]

    with transaction.atomic():
        assert recorded_skill(cno, nurse, licence, "2028-03-31", "21-RN-0412").status_code == 201
        second = alongside(pool, lambda: recorded_skill(deputy, badr, licence, "2028-03-31", "21-RN-0412"))
        assert not second.done()

    response = second.result(timeout=WAIT_SECONDS)
    assert response.status_code == 422, response.content
    assert response.json()["detail"][0]["msg"].startswith("Amal Saleh already holds SCFHS with this identifier.")
