"""The one module that knows the queue's library, so it can be replaced without
touching what the jobs do. Procrastinate finds a job by the module's name: put it in an app's `tasks.py`."""
from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from procrastinate import sql
from procrastinate.contrib.django import app
from procrastinate.contrib.django.models import ProcrastinateJob, ProcrastinateWorker
from procrastinate.exceptions import AlreadyEnqueued, UniqueViolation

from ops.status import WORKER_SILENCE

# Jobs sharing a lock run one at a time, lowest job id first, whichever worker fetches them. A job kept waiting by the lock
# takes no place from one without it, so the crons still run between solves.
_HOSPITAL_WIDE_SOLVE = "solve"


def _periodic(cron: str) -> Callable:
    """A job that takes the time it was due, as `timestamp`. At most one of it waits at a time (its queueing lock), and it never
    runs beside its own previous run (its lock), both under its own name: at the worker's concurrency of 4, a periodic
    job runs beside a solve, but never beside itself."""
    def schedule(job: Callable) -> Callable:
        return app.periodic(cron=cron)(app.task(queueing_lock=job.__name__, lock=job.__name__)(job))
    return schedule


def hourly(*, at_minute: int) -> Callable:
    return _periodic(f"{at_minute} * * * *")


def daily(*, at_hour_utc: int, at_minute: int) -> Callable:
    """In UTC, as every schedule here is: for a job whose hour does not matter to the hospital."""
    return _periodic(f"{at_minute} {at_hour_utc} * * *")


every_five_minutes = _periodic("*/5 * * * *")


def one_at_a_time_across_the_hospital(job: Callable) -> Callable:
    return app.task(lock=_HOSPITAL_WIDE_SOLVE)(job)


def under_its_own_lock(job: Callable) -> Callable:
    """A job run on demand that may not run beside itself."""
    return app.task(lock=job.__name__)(job)


def enqueue_once(job: Callable, key: str, **arguments) -> int | None:
    """As `enqueue`, but no second copy waits while one with this key does: None then. In a savepoint of its own, so the copy
    already waiting fails only this and not the caller's transaction."""
    try:
        with transaction.atomic():
            return job.configure(queueing_lock=key).defer(**arguments)
    except AlreadyEnqueued:
        return None


def enqueue(job: Callable, **arguments) -> int:
    """Puts a job on the queue in the caller's transaction, so neither it nor what it is about exists without the other."""
    return job.defer(**arguments)


def enqueue_later(job: Callable, *, seconds: int, priority: int = 0, **arguments) -> int:
    """As `enqueue`, to run no sooner than `seconds` from now. A job waiting for its time still holds its lock against every
    later job of the same lock and priority, so a job that may wait behind it should be deferred at a lower `priority`."""
    return job.configure(schedule_in={"seconds": seconds}, priority=priority).defer(**arguments)


def requeue_stalled() -> list[int]:
    """Jobs whose worker died with them, which would otherwise hold their lock for good. A job is `doing` until then, however
    long it has run: a worker that lives keeps beating. The library's own call for this is async only, and this is the same
    query, so it holds to what `select_stalled_jobs_by_heartbeat` means."""
    silent = ProcrastinateWorker.objects.filter(last_heartbeat__lt=timezone.now() - WORKER_SILENCE)
    stalled = ProcrastinateJob.objects.filter(Q(worker__isnull=True) | Q(worker__in=silent), status="doing")
    requeued = []
    for job_id, queueing_lock in stalled.values_list("id", "queueing_lock"):
        if _requeue(job_id, queueing_lock):
            requeued.append(job_id)
    return requeued


def _requeue(job_id: int, queueing_lock: str | None) -> bool:
    """Back on the queue, unless its next run waits already: then it could not take its queueing lock back, and while it held
    its lock the waiting one could never run. So it ends as failed, which the health page counts, and the next run does its
    work (a restore brings back the backup job that was running as that backup was taken, for one)."""
    if queueing_lock is None or not ProcrastinateJob.objects.filter(queueing_lock=queueing_lock, status="todo").exists():
        try:
            with transaction.atomic():
                app.job_manager.retry_job_by_id(job_id, timezone.now())
            return True
        except UniqueViolation:
            pass  # a copy was deferred since
    with connection.cursor() as cursor:
        cursor.execute(sql.queries["finish_job"], {"job_id": job_id, "status": "failed", "delete_job": False})
    return False


# Long enough to look into a week's runs. A failure is kept a month, which is what the health page's count of failed jobs then
# covers, so a backup failing every five minutes cannot pile its rows up for good.
ENDED_JOBS_KEPT = {("succeeded", "cancelled", "aborted"): timedelta(days=7), ("failed",): timedelta(days=30)}


def remove_ended_jobs() -> int:
    """Removes the rows, with their events, of jobs that ended longer ago than `ENDED_JOBS_KEPT` says for their status: every
    periodic run, every `project_carry_over` and every ended solve, whose `Solve` row is its record. A job waiting or running
    stays. The library's own query, whose call is async only, as `requeue_stalled` does."""
    removed = 0
    with connection.cursor() as cursor:
        for statuses, kept in ENDED_JOBS_KEPT.items():
            cursor.execute(sql.queries["delete_old_jobs"], {"nb_hours": int(kept.total_seconds() // 3600), "queue": None,
                                                            "statuses": list(statuses)})
            removed += cursor.rowcount
    return removed


def still_on_the_queue(job_ids: list[int]) -> set[int]:
    """Those of these jobs that a worker will yet run or is running."""
    return set(ProcrastinateJob.objects.filter(id__in=job_ids, status__in=("todo", "doing")).values_list("id", flat=True))
