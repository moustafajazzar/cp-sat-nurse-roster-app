"""Every container logs JSON lines to standard output: this formatter, used by the solver's uvicorn
(`solver_service/logging.json`) and by `web` and `worker` (the backend's settings and gunicorn's config)."""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time

import httpx

from service_tests.conftest import SOLVER_DIR, TOKEN
from solver_service.logs import AccessFormatter, JsonFormatter


def record(message="solve %s failed", args=("j1",), *, name="solver_service.jobs", level=logging.ERROR, exc_info=None, **extra):
    made = logging.LogRecord(name, level, "jobs.py", 129, message, args, exc_info)
    made.__dict__.update(extra)
    return made


def test_one_line_of_json_with_the_time_in_utc():
    line = JsonFormatter().format(record())

    assert "\n" not in line
    entry = json.loads(line)
    assert entry["level"] == "ERROR" and entry["logger"] == "solver_service.jobs" and entry["message"] == "solve j1 failed"
    assert entry["time"].endswith("Z")


def test_what_a_caller_adds_is_kept_and_anything_is_made_text():
    class Job:
        def __str__(self):
            return "job 7"

    entry = json.loads(JsonFormatter().format(record(action="start_job", job={"id": 7, "task": Job()}, at=time.gmtime(0))))

    assert entry["action"] == "start_job" and entry["job"] == {"id": 7, "task": "job 7"}


def test_an_exception_comes_with_its_traceback_on_the_same_line():
    try:
        raise ValueError("bad grid")
    except ValueError:
        line = JsonFormatter().format(record(exc_info=sys.exc_info()))

    assert "\n" not in line
    assert "ValueError: bad grid" in json.loads(line)["exception"]


def test_gunicorns_access_line_is_split_into_fields():
    atoms = {"m": "GET", "U": "/api/health", "q": "", "s": "200", "M": 12, "B": 81, "h": "172.18.0.5",
             "{x-client-ip}i": "203.0.113.9", "{x-forwarded-for}i": "198.51.100.1, 203.0.113.9"}
    entry = json.loads(AccessFormatter().format(record("%(m)s %(U)s", atoms, name="gunicorn.access", level=logging.INFO)))

    assert {key: entry[key] for key in ("method", "path", "status", "duration_ms", "bytes", "remote")} == {
        "method": "GET", "path": "/api/health", "status": 200, "duration_ms": 12, "bytes": 81, "remote": "203.0.113.9"}


def test_the_service_logs_only_json_lines():
    """The real service under its own log config, as `deploy/compose.yaml` starts it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "--factory", "solver_service.app:create_app", "--host", "127.0.0.1", "--port",
         str(port), "--log-config", "solver_service/logging.json"],
        cwd=SOLVER_DIR, env={**os.environ, "SOLVER_TOKEN": TOKEN}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            try:
                answer = httpx.get(f"http://127.0.0.1:{port}/health", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=1)
                break
            except httpx.TransportError:
                assert server.poll() is None, "the service exited during start-up"
                time.sleep(0.1)
        assert answer.status_code == 200
    finally:
        server.terminate()
        output = server.communicate(timeout=10)[0].decode()

    entries = [json.loads(line) for line in output.splitlines() if line.strip()]
    assert any(entry["logger"] == "uvicorn.access" and "/health" in entry["message"] for entry in entries)
    assert any(entry["logger"] == "uvicorn.error" for entry in entries)
