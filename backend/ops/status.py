"""What the health page reports about this installation."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import OperationalError, connection
from django.db.models import Count
from django.utils import timezone
from procrastinate.contrib.django.models import ProcrastinateJob, ProcrastinateWorker

from notifications.models import PushAttempt, PushOutcome
from ops.solver_client import SolverUnreachable, solver_health

# Procrastinate's workers beat every 10 s and it treats one silent for 30 s as stalled.
WORKER_SILENCE = timedelta(seconds=30)


@dataclass
class DiskUsage:
    total_bytes: int
    free_bytes: int


def disk_usage() -> DiskUsage | None:
    """The disk the backups land on, which on one VPS is the one everything else needs too."""
    try:
        usage = shutil.disk_usage(settings.BACKUPS_DIR)
    except OSError:
        return None
    return DiskUsage(total_bytes=usage.total, free_bytes=usage.free)


def solver_state() -> dict | None:
    """What the solver says of itself (its version and threads), or None when it does not answer."""
    try:
        return solver_health()
    except SolverUnreachable:
        return None


def database_reachable() -> bool:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return True
    except OperationalError:
        return False


def database_size_bytes() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_database_size(current_database())")
        return cursor.fetchone()[0]


@dataclass
class QueueState:
    waiting: int
    running: int
    failed: int
    workers: int


def queue_state() -> QueueState:
    jobs = dict(ProcrastinateJob.objects.order_by().values_list("status").annotate(Count("id")))
    alive = ProcrastinateWorker.objects.filter(last_heartbeat__gte=timezone.now() - WORKER_SILENCE)
    return QueueState(waiting=jobs.get("todo", 0), running=jobs.get("doing", 0), failed=jobs.get("failed", 0),
                      workers=alive.count())


PUSHES_COUNTED = timedelta(hours=24)


@dataclass
class PushState:
    sent: int
    failed: int
    last_failure: str | None


def push_state() -> PushState:
    """The last 24 hours' sends, one a subscription a try, so a push tried four times at one subscription counts four: attempts,
    never jobs. The last of them that failed says why in the app's own word. Nothing here is of a user or a device,
    since an attempt holds neither. The retention keeps attempts seven days, so it never reaches what is read here."""
    counted = PushAttempt.objects.filter(at__gte=timezone.now() - PUSHES_COUNTED)
    by_outcome = dict(counted.order_by().values_list("outcome").annotate(Count("id")))
    sent = by_outcome.pop(PushOutcome.SENT, 0)
    last = counted.exclude(outcome=PushOutcome.SENT).order_by("-at", "-pk").values_list("outcome", flat=True).first()
    return PushState(sent=sent, failed=sum(by_outcome.values()), last_failure=last)
