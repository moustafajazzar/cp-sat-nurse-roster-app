"""One solve from the queue to its end, on whichever worker has it. The queue's lock means only one of these runs at a time
across the hospital, so a job the solver is already running can only be this solve's, or one a dead worker left behind."""
from __future__ import annotations

import logging
import time
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from audit.log import record
from hospital.models import HospitalSettings
from notifications import sending
from notifications.models import SolveOutcome
from ops import solver_client
from ops.queue import still_on_the_queue
from ops.solver_client import SolverBusy, SolverJobUnknown, SolverRefused, SolverUnreachable
from rosters import drafts
from solves import replans
from solves.models import ACTIVE_STATES, Solve, SolveFailure, SolveState
from solves.result import has_a_roster_to_keep

logger = logging.getLogger(__name__)

POLL_SECONDS = 1.0
WAIT_BEFORE_RETRY_SECONDS = 5.0
MAX_ATTEMPTS = 2
# A busy solver can miss a poll or two without anything being wrong. This many in a row at 20 s each is a dead one.
UNANSWERED_POLLS = 5
# How long past its time limit a solve may run before it counts as hung: building the model and the result are not in it.
OVERRUN_SECONDS = 120.0
_ENDED = ("finished", "cancelled", "failed")


class SolveFailed(Exception):
    """This attempt ended with no answer from the solver, for a reason of this `kind`."""

    def __init__(self, message: str, kind: SolveFailure):
        super().__init__(message)
        self.kind = kind


def _kind_of(error: Exception) -> SolveFailure:
    """Which of the fixed kinds an attempt's failure is, from what was raised and never from its words."""
    if isinstance(error, SolveFailed):
        return error.kind
    if isinstance(error, SolverUnreachable):
        return SolveFailure.UNREACHABLE
    if isinstance(error, SolverBusy):
        return SolveFailure.STUCK
    return SolveFailure.UNEXPECTED


def _set(solve: Solve, **fields) -> None:
    Solve.objects.filter(pk=solve.pk).update(**fields)
    for name, value in fields.items():
        setattr(solve, name, value)


def _stop_requested(solve: Solve) -> bool:
    return Solve.objects.filter(pk=solve.pk, stop_requested=True).exists()


def run(solve_id: UUID | str) -> None:
    solve = Solve.objects.select_related("unit", "requested_by").get(pk=solve_id)
    # Stopping a queued solve ends it there and then, so a state that is neither of these was decided already.
    if not Solve.objects.filter(pk=solve.pk, state__in=ACTIVE_STATES).update(
            state=SolveState.RUNNING, started_at=solve.started_at or timezone.now()):
        return
    while True:
        try:
            _finish(solve, _attempt(solve))
            return
        except SolverRefused as e:
            _fail(solve, failure=SolveFailure.REFUSED, error=str(e))
            return
        except Exception as e:  # noqa: BLE001 Whatever went wrong, the row has to say so, or the page waits for good.
            logger.exception("solve %s: attempt %s failed", solve.pk, solve.attempts)
            reason = str(e) or type(e).__name__
            if solve.attempts >= MAX_ATTEMPTS:
                _fail(solve, failure=_kind_of(e), error=reason)
                return
            _set(solve, error=reason, solver_job_id="")
            time.sleep(WAIT_BEFORE_RETRY_SECONDS)


def _attempt(solve: Solve) -> dict:
    """The solver's job as it ended. A job id already on the row means a worker died while the solver went on, which is
    no attempt of its own, so this goes on watching it. One the solver no longer knows is a failed attempt."""
    if not solve.solver_job_id:
        if _stop_requested(solve):
            return {"state": "cancelled", "result": None, "elapsed_seconds": solve.elapsed_seconds}
        if solve.attempts >= MAX_ATTEMPTS:
            raise SolveFailed("The worker stopped during the last attempt.", SolveFailure.WORKER_LOST)
        _set(solve, attempts=solve.attempts + 1)
        _set(solve, solver_job_id=_start(solve))
    return _watch(solve, solve.solver_job_id)


def _start(solve: Solve) -> str:
    try:
        return solver_client.start_solve(solve.input)
    except SolverBusy as busy:
        _cancel_what_a_dead_worker_left(busy.job_id, solve)
    return solver_client.start_solve(solve.input)


def _cancel_what_a_dead_worker_left(job_id: str, solve: Solve) -> None:
    """Nobody is waiting for it: its result never got as far as a row, because the worker that started it died first."""
    logger.warning("solve %s: stopping solver job %s, which no solve is watching", solve.pk, job_id)
    solver_client.cancel_job(job_id)
    deadline = time.monotonic() + OVERRUN_SECONDS
    while solver_client.read_job(job_id)["state"] not in _ENDED:
        if time.monotonic() > deadline:
            raise SolveFailed("The solver would not stop an earlier job.", SolveFailure.STUCK)
        time.sleep(POLL_SECONDS)


def _watch(solve: Solve, job_id: str) -> dict:
    relayed, unanswered = False, 0
    deadline = time.monotonic() + solve.time_limit_seconds + OVERRUN_SECONDS
    while True:
        try:
            job = solver_client.read_job(job_id)
            unanswered = 0
            if job["state"] in _ENDED:
                if job["state"] == "failed":
                    raise SolveFailed(job.get("error") or "The solver failed and gave no reason.", SolveFailure.SOLVER_ERROR)
                return job
            _write_progress(solve, job)
            if not relayed and (job["cancel_requested"] or _stop_requested(solve)):
                solver_client.cancel_job(job_id)
                relayed = True
        except SolverUnreachable:
            unanswered += 1
            if unanswered >= UNANSWERED_POLLS:
                raise
        except SolverJobUnknown as e:
            raise SolveFailed("The solver restarted and lost this solve.", SolveFailure.LOST) from e
        if time.monotonic() > deadline:
            raise SolveFailed(f"The solver had not ended {OVERRUN_SECONDS:.0f} seconds past the time limit.",
                              SolveFailure.OVERRUN)
        time.sleep(POLL_SECONDS)


def _write_progress(solve: Solve, job: dict) -> None:
    fields = {"elapsed_seconds": job["elapsed_seconds"], "best_cost": job["best_cost"], "improvements": job["improvements"]}
    _set(solve, **fields)


def _end(solve: Solve, **fields) -> None:
    _set(solve, finished_at=timezone.now(), **fields)


def _fail(solve: Solve, **fields) -> None:
    with transaction.atomic():
        _end(solve, state=SolveState.FAILED, solver_job_id="", **fields)
        sending.solve_ended(solve.unit_id, solve.month, solve.pk, SolveOutcome.FAILED, replan=bool(solve.replan_from))


def _finish(solve: Solve, job: dict) -> None:
    """A stopped solve keeps the best roster found. A search that ended with no roster, or one breaking a hard rule, which the
    solver reports with `ok` false, leaves the draft as it was: the result says why. So does a roster submitted or approved
    meanwhile, though submitting refuses while a solve is active. A re-plan's end is `solves.replans.finish`'s."""
    result = job["result"]
    state = SolveState.CANCELLED if job["state"] == "cancelled" else SolveState.SUCCEEDED
    # The catalogue's lock first, as everywhere, so a rename cannot pass between reading a code and writing it.
    with transaction.atomic():
        HospitalSettings.load(for_update=True)
        # `error` stays: a retry that worked still says why the first attempt failed.
        fields = {"state": state, "result": result, "solver_job_id": "", "elapsed_seconds": job["elapsed_seconds"]}
        if result is not None:
            fields |= {"best_cost": result["cost"], "improvements": result["solver"]["improvements"]}
        _end(solve, **fields)
        # This solve's own row is written before any other lock a re-plan's end takes, as for a plain solve. No leave, edit or
        # approval locks a solve's row, and a stop takes only that row, so it adds no wait to the order they share.
        if solve.replan_from:
            outcome = replans.finish(solve, result, state)
            sending.solve_ended(solve.unit_id, solve.month, solve.pk, outcome, replan=True)
            return
        # Submitting refuses while a solve is active, so only a roster frozen behind the API's back meets this. The result stays
        # in the history, and the frozen grid as it was.
        frozen = drafts.frozen_month(solve.unit, solve.month)
        outcome = SolveOutcome.NO_ROSTER
        if frozen:
            logger.warning("solve %s: ended on a frozen roster, which keeps its grid", solve.pk)
            outcome = SolveOutcome.FROZEN
        elif has_a_roster_to_keep(result):
            drafts.replace_grid(solve, result["roster"])
            record("roster.draft_replaced", actor=solve.requested_by, target=solve.audit_target,
                   after={"solve": str(solve.pk), "cost": result["cost"]})
            outcome = SolveOutcome.STOPPED_DRAFT if state == SolveState.CANCELLED else SolveOutcome.DRAFT
        sending.solve_ended(solve.unit_id, solve.month, solve.pk, outcome)


def _solves_nothing_will_run() -> list[Solve]:
    active = list(Solve.objects.filter(state__in=ACTIVE_STATES))
    alive = still_on_the_queue([solve.job_id for solve in active])
    return [solve for solve in active if solve.job_id not in alive]


def end_what_the_queue_lost() -> None:
    """A job can end without its row knowing only if the database went away as it tried to write. Without this the row would
    stay active for good, and with it the month's one active solve."""
    for solve in _solves_nothing_will_run():
        _fail(solve, failure=SolveFailure.WORKER_LOST, error="The worker lost this solve.")
