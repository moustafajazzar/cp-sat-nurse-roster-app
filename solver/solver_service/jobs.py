"""One solve at a time, in a background thread, on the package's own functions.

The search releases the GIL, so the HTTP side stays responsive while it runs. A job lives in
memory only: every durable fact about a solve belongs to the application database,
so after a restart a job id is simply unknown and the worker marks that solve failed.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict

from nurse_roster.model import Solution, build_model, solve
from nurse_roster.precheck import precheck
from nurse_roster.request import Request
from nurse_roster.result import build_result

logger = logging.getLogger(__name__)

RUNNING, FINISHED, CANCELLED, FAILED = "running", "finished", "cancelled", "failed"
KEEP_ENDED_JOBS = 16       # enough for the worker to read a result; a result is not small


class Busy(Exception):
    """A job is running; the service runs one at a time."""

    def __init__(self, job_id: str):
        super().__init__(job_id)
        self.job_id = job_id


class Job:
    """A solve's progress as the HTTP side reads it. The search thread writes, handlers read."""

    def __init__(self) -> None:
        self.id = uuid.uuid4().hex
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._ended: float | None = None
        self._state = RUNNING
        self._best_cost: int | None = None
        self._improvements = 0
        self._best_roster: dict | None = None
        self._result: dict | None = None
        self._error: str | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._state == RUNNING

    def improved(self, improvements: int, partial: dict) -> None:
        with self._lock:
            self._improvements = improvements
            self._best_cost, self._best_roster = partial["cost"], partial["roster"]

    def finished(self, result: dict) -> None:
        with self._lock:
            self._result = result
            self._improvements = result["solver"]["improvements"]
            self._best_cost, self._best_roster = result["cost"], result["roster"]
            # A cancel that arrived after the proof changed nothing: the search ended by itself.
            self._state = CANCELLED if result["solver"]["stopped_by"] == "cancelled" else FINISHED
            self._ended = time.monotonic()

    def failed(self, error: str) -> None:
        with self._lock:
            self._error = error
            self._state = FAILED
            self._ended = time.monotonic()

    def status(self) -> dict:
        with self._lock:
            end = self._ended if self._ended is not None else time.monotonic()
            return {
                "job_id": self.id,
                "state": self._state,
                "elapsed_seconds": round(end - self._started, 3),
                "cancel_requested": self.stop.is_set(),
                "best_cost": self._best_cost,
                "improvements": self._improvements,
                "best_roster": self._best_roster,
                "result": self._result,
                "error": self._error,
            }


class JobRunner:
    def __init__(self, workers: int):
        self.workers = workers
        self._lock = threading.Lock()
        self._jobs: OrderedDict[str, Job] = OrderedDict()

    def start(self, req: Request) -> Job:
        with self._lock:
            for job in self._jobs.values():
                if job.running:
                    raise Busy(job.id)
            job = Job()
            self._jobs[job.id] = job
            while len(self._jobs) > KEEP_ENDED_JOBS:       # the newest job is running, so
                self._jobs.popitem(last=False)             # only ended ones are ever dropped
        threading.Thread(target=self._run, args=(job, req), name=f"solve-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _run(self, job: Job, req: Request) -> None:
        try:
            issues = precheck(req.problem)
            rm = build_model(req.problem)

            def on_solution(sol: Solution) -> None:
                try:
                    partial = build_result(req, rm, sol, precheck=issues, workers=self.workers)
                    job.improved(sol.improvements, partial)
                except Exception:                          # progress is a view: a failure here
                    logger.exception("progress update failed")   # must not end a good search

            sol = solve(rm, req.problem, on_solution=on_solution, stop=job.stop,
                        **req.solver.solve_kwargs(self.workers))
            job.finished(build_result(req, rm, sol, precheck=issues, workers=self.workers))
        except Exception as e:
            logger.exception("solve %s failed", job.id)
            job.failed(f"{type(e).__name__}: {e}")
