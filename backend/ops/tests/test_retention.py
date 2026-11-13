"""What the queue keeps: every run of a job leaves a row and its events, and nothing else removed them. A row that
ended well is removed a week on; a failed one a month on, so the health page counts the failures of the last month, and a backup
that keeps failing every five minutes cannot pile them up for good. Nothing waiting or running is touched."""
import datetime as dt

import pytest
from django.db import connection
from procrastinate.contrib.django.models import ProcrastinateEvent, ProcrastinateJob

from ops import queue
from ops.status import queue_state

pytestmark = pytest.mark.django_db


def a_job(task, status, *, ended_days_ago=0.0):
    """A job as the queue leaves it: deferred, then in `status` since `ended_days_ago`."""
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO procrastinate_jobs (queue_name, task_name, args, status) VALUES ('default', %s, '{}', %s) "
                       "RETURNING id", [task, status])
        job_id = cursor.fetchone()[0]
        cursor.execute("INSERT INTO procrastinate_events (job_id, type, at) VALUES (%s, 'deferred', now() - %s)",
                       [job_id, dt.timedelta(days=ended_days_ago + 1)])
        if status not in ("todo", "doing"):
            cursor.execute("INSERT INTO procrastinate_events (job_id, type, at) VALUES (%s, %s, now() - %s)",
                           [job_id, status, dt.timedelta(days=ended_days_ago)])
    return job_id


def left():
    return set(ProcrastinateJob.objects.values_list("id", flat=True))


def test_a_job_that_ended_well_a_week_ago_goes_with_its_events():
    old = [a_job("recover_lost_jobs", "succeeded", ended_days_ago=8), a_job("project_carry_over", "succeeded", ended_days_ago=7.1),
           a_job("run_solve", "succeeded", ended_days_ago=30), a_job("run_solve", "cancelled", ended_days_ago=8),
           a_job("backup", "aborted", ended_days_ago=8)]
    recent = a_job("recover_lost_jobs", "succeeded", ended_days_ago=6.9)

    removed = queue.remove_ended_jobs()

    assert removed == 5
    assert left() == {recent}
    assert not ProcrastinateEvent.objects.filter(job_id__in=old).exists()


def test_waiting_and_running_jobs_and_a_months_failures_stay_so_the_health_page_says_the_same():
    kept = {a_job("close_month", "failed", ended_days_ago=29), a_job("run_solve", "todo"), a_job("backup", "doing")}
    a_job("recover_lost_jobs", "succeeded", ended_days_ago=8)
    before = queue_state()

    queue.remove_ended_jobs()

    assert left() == kept
    assert queue_state() == before


def test_a_failure_goes_a_month_on():
    kept = a_job("backup", "failed", ended_days_ago=29.9)
    a_job("backup", "failed", ended_days_ago=30.1)

    assert queue.remove_ended_jobs() == 1
    assert left() == {kept}


def test_the_job_runs_daily_under_a_lock_of_its_own():
    from ops.tasks import remove_ended_jobs
    assert (remove_ended_jobs.lock, remove_ended_jobs.queueing_lock) == ("remove_ended_jobs", "remove_ended_jobs")
