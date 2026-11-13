"""`web` and `worker` log one JSON object a line to standard output, each in its own process as the containers
start them: gunicorn's own lines and the application's, the queue worker's through Django's settings, and the start's
migrations. The solver's are `solver/service_tests/test_logs.py`'s."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from django.db import connection

BACKEND = Path(__file__).resolve().parents[2]


def environment() -> dict:
    """The test database's own connection, so a process started here reaches it."""
    settings = connection.settings_dict
    return {**os.environ, "DJANGO_SETTINGS_MODULE": "config.test_settings", "POSTGRES_HOST": settings["HOST"],
            "POSTGRES_PORT": str(settings["PORT"]), "POSTGRES_DB": settings["NAME"], "POSTGRES_PASSWORD": settings["PASSWORD"],
            "SOLVER_URL": "http://127.0.0.1:9"}


def json_lines(output: str) -> list[dict]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


@pytest.mark.django_db
def test_the_application_and_the_queue_worker_log_through_the_settings():
    script = ("import django, logging; django.setup(); "
              "logging.getLogger('procrastinate.worker').info('Job ended', extra={'action': 'job_success', 'job': {'id': 7}}); "
              "logging.getLogger('solves.running').warning('solve %s: attempt %s failed', 'x', 1)")
    done = subprocess.run([sys.executable, "-c", script], cwd=BACKEND, env=environment(), capture_output=True, text=True,
                          check=True)

    entries = [entry for entry in json_lines(done.stdout) if not entry["logger"].startswith(("procrastinate.periodic", "procrastinate.blueprints"))]  # its tasks at start
    assert [(entry["logger"], entry["level"]) for entry in entries] == [("procrastinate.worker", "INFO"),
                                                                        ("solves.running", "WARNING")]
    assert entries[0]["job"] == {"id": 7} and entries[1]["message"] == "solve x: attempt 1 failed"
    assert done.stderr == ""


@pytest.mark.django_db(transaction=True)
def test_the_start_logs_its_migrations_as_json(monkeypatch):
    done = subprocess.run([sys.executable, "manage.py", "prepare"], cwd=BACKEND,
                          env={**environment(), "INITIAL_ADMIN_PASSWORD": "one-time-password"},
                          capture_output=True, text=True, check=True)

    entries = json_lines(done.stdout)
    assert {"No migrations to apply."} <= {entry["message"] for entry in entries}
    assert "Created the System Admin 'admin'; the password must change at first login." in [e["message"] for e in entries]


@pytest.mark.django_db(transaction=True)
def test_gunicorn_logs_its_own_lines_and_each_request_as_json(tmp_path):
    """One request, sent once the worker says it is ready (`config/gunicorn.py`'s `post_worker_init`). gunicorn listens before
    its worker has loaded the application, which takes seconds: a request sent then waits in the socket's queue, and one resent
    after its own timeout would be served too, making two lines."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    out, err = tmp_path / "stdout", tmp_path / "stderr"
    with out.open("w") as stdout, err.open("w") as stderr:
        server = subprocess.Popen([sys.executable, "-m", "gunicorn", "config.wsgi", "-c", "config/gunicorn.py", "--bind",
                                   f"127.0.0.1:{port}", "--workers", "1"], cwd=BACKEND, env=environment(), stdout=stdout,
                                  stderr=stderr, text=True)
    try:
        started = time.monotonic()
        while not any(entry["logger"] == "gunicorn.error" and entry["message"].startswith("Worker ready")
                      for entry in json_lines(out.read_text().rpartition("\n")[0])):  # a line may be half written
            assert server.poll() is None, "gunicorn exited during start-up"
            assert time.monotonic() - started < 60, "gunicorn's worker did not load the application in a minute"
            time.sleep(0.1)
        answer = httpx.get(f"http://127.0.0.1:{port}/api/health?from=test", timeout=5,
                           headers={"X-Client-IP": "203.0.113.9", "X-Forwarded-For": "198.51.100.1, 203.0.113.9"})
        assert answer.status_code == 200
    finally:
        server.terminate()
        server.wait(timeout=10)

    entries = json_lines(out.read_text())
    assert any(entry["logger"] == "gunicorn.error" and "Booting worker" in entry["message"] for entry in entries)
    access = [entry for entry in entries if entry["logger"] == "gunicorn.access"]
    assert [(e["method"], e["path"], e["query"], e["status"], e["remote"]) for e in access] == [
        ("GET", "/api/health", "from=test", 200, "203.0.113.9")]
    assert err.read_text() == ""


@pytest.mark.django_db(transaction=True)
def test_the_queues_command_line_logs_what_it_would_print():
    """`manage.py queue`, which the worker container runs, is Procrastinate's command with the lines it prints to standard
    error ("Launching a worker on all queues", "Launching a job: …") made log lines like the rest."""
    done = subprocess.run([sys.executable, "manage.py", "queue", "defer", "--ignore-already-enqueued", "ops.tasks.backup",
                           '{"timestamp": 0}'], cwd=BACKEND, env=environment(), capture_output=True, text=True, check=True)

    assert done.stderr == ""
    entries = json_lines(done.stdout)
    assert [entry["logger"] for entry in entries if entry["message"].startswith("Launching a job: ops.tasks.backup")] == [
        "procrastinate.cli"]
