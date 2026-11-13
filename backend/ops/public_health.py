"""The public `GET /api/health` answer, held five seconds in each web process, so a client polling it cannot make
the application call the solver and the database on every request. Only that route holds it: the System Admin's health page
and `/metrics` ask afresh each time.

Gunicorn's threads in one process share the held answer, and one lock is held while it is worked out, so a burst of requests
as it runs out makes one call to the solver between them. The others wait for that call: the solver's timeout is 2 seconds
for each step of it (connecting, reading), and the database's `SELECT 1` has none of its own, so a database that hangs
rather than refuses holds up the process's health answers with it.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

from pydantic import BaseModel

HELD_SECONDS = 5.0

# Read at each request, so a test can move it (`ops/tests/test_health.py`).
clock: Callable[[], float] = time.monotonic


class HeldAnswer:
    """The answer `work` gives, given again until `HELD_SECONDS` have passed on `clock` since it was worked out."""

    def __init__(self, work: Callable[[], BaseModel]):
        self._work = work
        self._lock = threading.Lock()
        self._answer: BaseModel | None = None
        self._until = 0.0

    def __call__(self) -> BaseModel:
        with self._lock:
            if self._answer is None or clock() >= self._until:
                self._answer = self._work()
                self._until = clock() + HELD_SECONDS
            return self._answer

    def forget(self) -> None:
        """For the tests' own start: each test begins with nothing held."""
        with self._lock:
            self._answer, self._until = None, 0.0
