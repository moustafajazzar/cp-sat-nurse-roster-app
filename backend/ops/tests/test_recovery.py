"""A periodic job whose worker died with it, while its next run already waits (found by a restore: the backup that
job that was running as that backup was taken comes back as `doing`). Put back on the queue it would collide with the waiting copy's
queueing lock, failing `recover_lost_jobs` every five minutes, while the waiting copy never runs behind the dead one's lock."""
import pytest
from django.db import connection
from procrastinate.contrib.django.models import ProcrastinateJob

from ops.queue import requeue_stalled

pytestmark = pytest.mark.django_db


def a_job(task, status, *, lock=None, queueing_lock=None, worker=None):
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO procrastinate_jobs (queue_name, task_name, args, status, lock, queueing_lock, worker_id) "
                       "VALUES ('default', %s, '{}', %s, %s, %s, %s) RETURNING id", [task, status, lock, queueing_lock, worker])
        return cursor.fetchone()[0]


def a_silent_worker():
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now() - interval '5 minutes') RETURNING id")
        return cursor.fetchone()[0]


def status_of(job_id):
    return ProcrastinateJob.objects.get(pk=job_id).status


def test_a_dead_run_whose_next_run_waits_ends_as_failed_and_the_next_one_can_run():
    dead = a_job("ops.tasks.backup", "doing", lock="backup", queueing_lock="backup", worker=a_silent_worker())
    waiting = a_job("ops.tasks.backup", "todo", lock="backup", queueing_lock="backup")

    assert requeue_stalled() == []

    assert (status_of(dead), status_of(waiting)) == ("failed", "todo")


def test_one_that_cannot_go_back_stops_no_other():
    worker = a_silent_worker()
    dead = a_job("ops.tasks.backup", "doing", lock="backup", queueing_lock="backup", worker=worker)
    a_job("ops.tasks.backup", "todo", lock="backup", queueing_lock="backup")
    solve = a_job("solves.tasks.run_solve", "doing", lock="solve", worker=worker)

    assert requeue_stalled() == [solve]

    assert (status_of(dead), status_of(solve)) == ("failed", "todo")


def test_with_no_copy_waiting_it_goes_back_on_the_queue():
    dead = a_job("ops.tasks.backup", "doing", lock="backup", queueing_lock="backup")

    assert requeue_stalled() == [dead]
    assert status_of(dead) == "todo"
