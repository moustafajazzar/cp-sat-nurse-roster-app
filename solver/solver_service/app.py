"""The solver's HTTP service.

It wraps the vendored `nurse_roster` package and adds no rostering knowledge of its own. Every
call carries the shared token as `Authorization: Bearer <token>`. Run one process only, since
the jobs live in its memory:

    uvicorn --factory solver_service.app:create_app --host 0.0.0.0 --port 8080

Calls (bodies and answers are JSON; a malformed solve input or roster is a 422 with `detail`):

    POST /solve             the solve input      202 {"job_id"}; 409 {"detail", "job_id"} while one runs
    GET  /jobs/{id}                              the job: see Job.status in jobs.py
    POST /jobs/{id}/cancel                       the job; the best roster found is kept
    POST /validate          {"solve_input", "roster"}   see grid.py
    POST /precheck          the solve input      {"issues": ["..."]}
    GET  /health                                 {"status", "version", "workers"}
"""
from __future__ import annotations

import math
import os
import secrets
from importlib.metadata import version
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from nurse_roster.model import MAX_WORKERS
from nurse_roster.precheck import precheck
from nurse_roster.problem import parse_json
from nurse_roster.request import load_request

from .grid import validate_grid
from .jobs import Busy, JobRunner


def container_cpus() -> int:
    """CPUs this process may really use: its affinity, further limited by a cgroup v2 quota
    (`docker run --cpus`), which `os.process_cpu_count` does not see."""
    cpus = os.process_cpu_count() or 1
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
    except (OSError, ValueError):
        return cpus
    return cpus if quota == "max" else max(1, min(cpus, math.ceil(int(quota) / int(period))))


def default_workers() -> int:
    """`NURSE_ROSTER_WORKERS` (the package's own server setting) or the container's CPU count."""
    configured = os.environ.get("NURSE_ROSTER_WORKERS")
    if not configured:
        return min(container_cpus(), MAX_WORKERS)
    workers = int(configured)
    if not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"NURSE_ROSTER_WORKERS must be within 1..{MAX_WORKERS}, got {workers}")
    return workers


def create_app() -> FastAPI:
    token = os.environ.get("SOLVER_TOKEN", "")
    if not token:
        raise RuntimeError("SOLVER_TOKEN is not set; the solver service refuses to start without it")
    expected = f"Bearer {token}".encode()
    runner = JobRunner(default_workers())
    # No documentation routes: every path is behind the token, and nothing needs them.
    app = FastAPI(title="Nurse roster solver", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        # A middleware, not a dependency: FastAPI parses a body before it runs dependencies.
        given = request.headers.get("authorization", "").encode()
        if not secrets.compare_digest(given, expected):
            return JSONResponse({"detail": "missing or wrong solver token"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def bad_input(_: Request, e: ValueError) -> JSONResponse:
        # The package reports every malformed input as a ValueError naming the field.
        return JSONResponse({"detail": str(e)}, status_code=422)

    def job_or_404(job_id: str):
        job = runner.get(job_id)
        return job if job is not None else JSONResponse({"detail": f"no job {job_id}"}, status_code=404)

    @app.post("/solve", status_code=202)
    async def start_solve(request: Request):
        raw = parse_json(await request.body(), where="body")
        req = await run_in_threadpool(load_request, raw)
        try:
            return {"job_id": runner.start(req).id}
        except Busy as busy:
            return JSONResponse({"detail": "a solve is already running", "job_id": busy.job_id},
                                status_code=409)

    @app.get("/jobs/{job_id}")
    def read_job(job_id: str):
        job = job_or_404(job_id)
        return job if isinstance(job, JSONResponse) else job.status()

    @app.post("/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        job = job_or_404(job_id)
        if isinstance(job, JSONResponse):
            return job
        job.stop.set()                                     # no effect once the job has ended
        return job.status()

    @app.post("/validate")
    async def validate(request: Request):
        raw = parse_json(await request.body(), where="body")
        if not isinstance(raw, dict) or set(raw) != {"solve_input", "roster"}:
            raise ValueError('body must be an object with exactly "solve_input" and "roster"')
        return await run_in_threadpool(validate_grid, raw["solve_input"], raw["roster"])

    @app.post("/precheck")
    async def check(request: Request):
        raw = parse_json(await request.body(), where="body")
        req = await run_in_threadpool(load_request, raw)
        return {"issues": await run_in_threadpool(precheck, req.problem)}

    @app.get("/health")
    def health():
        return {"status": "ok", "version": version("nurse-roster"), "workers": runner.workers}

    return app
