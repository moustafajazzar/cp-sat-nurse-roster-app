"""The application's only way to the solver service."""
from __future__ import annotations

import httpx
from django.conf import settings

# The search uses every core the solver has, so an answer can be slow while it runs. A result is not small either.
_TIMEOUT = 20.0


class SolverUnreachable(Exception):
    """The solver service did not give a usable answer."""


class SolverRefused(Exception):
    """The solver will not take this solve input (422), and says why. Sending it again will not help."""


class SolverBusy(Exception):
    """A job is running already: `job_id`."""

    def __init__(self, job_id: str):
        super().__init__(job_id)
        self.job_id = job_id


class SolverJobUnknown(Exception):
    """The solver does not hold this job: jobs live in its memory, so it has restarted."""


def _call(method: str, path: str, *, timeout: float = _TIMEOUT, **arguments) -> httpx.Response:
    try:
        return httpx.request(method, f"{settings.SOLVER_URL}{path}", timeout=timeout,
                             headers={"Authorization": f"Bearer {settings.SOLVER_TOKEN}"}, **arguments)
    except httpx.HTTPError as e:
        raise SolverUnreachable(str(e) or type(e).__name__) from e


def _answer(response: httpx.Response) -> dict:
    if response.status_code == 404:
        raise SolverJobUnknown(response.text)
    if response.status_code == 422:
        raise SolverRefused(_detail(response))
    try:
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as e:
        raise SolverUnreachable(str(e)) from e


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json()["detail"])
    except (ValueError, KeyError, TypeError):
        return response.text


def solver_health(timeout: float = 2.0) -> dict:
    try:
        r = httpx.get(f"{settings.SOLVER_URL}/health", timeout=timeout,
                      headers={"Authorization": f"Bearer {settings.SOLVER_TOKEN}"})
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise SolverUnreachable(str(e)) from e


def precheck(solve_input: dict) -> list[str]:
    """Plain-language reasons the month may be infeasible. No search."""
    return _answer(_call("POST", "/precheck", json=solve_input))["issues"]


def start_solve(solve_input: dict) -> str:
    """The solver's job id. Only one job runs at a time."""
    response = _call("POST", "/solve", json=solve_input)
    if response.status_code == 409:
        try:
            raise SolverBusy(response.json()["job_id"])
        except (ValueError, KeyError, TypeError) as e:
            raise SolverUnreachable(response.text) from e
    return _answer(response)["job_id"]


def read_job(job_id: str) -> dict:
    return _answer(_call("GET", f"/jobs/{job_id}"))


def cancel_job(job_id: str) -> dict:
    return _answer(_call("POST", f"/jobs/{job_id}/cancel"))
