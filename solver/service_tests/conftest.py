"""The real service in its own process, on a free port, spoken to only over HTTP."""
from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

TOKEN = "contract-test-token"
SOLVER_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def base_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "SOLVER_TOKEN": TOKEN}
    env.pop("NURSE_ROSTER_WORKERS", None)
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "--factory", "solver_service.app:create_app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=SOLVER_DIR, env=env)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                httpx.get(f"{url}/health", timeout=1)
                break
            except httpx.TransportError:
                assert server.poll() is None, "the service exited during start-up"
                time.sleep(0.1)
        else:
            pytest.fail("the service did not start")
        yield url
    finally:
        server.terminate()
        server.wait(timeout=10)


@pytest.fixture
def client(base_url):
    with httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30) as c:
        yield c


@pytest.fixture
def anonymous(base_url):
    with httpx.Client(base_url=base_url, timeout=30) as c:
        yield c


@pytest.fixture
def sample():
    """The four-nurse sample of how-to.md section 2.5, proven optimal in under a second."""
    return json.loads((Path(__file__).parent / "fixtures" / "four_nurses.json").read_text())


@pytest.fixture
def long_search(sample):
    """The same four nurses with looser headcounts and the soft rules on: a first roster in
    a fraction of a second and no proof for many seconds, so a job is certain to be running
    while a test cancels it or starts another."""
    d = copy.deepcopy(sample)
    d["department"].update(score_balance=True, score_balance_mode="spread", score_target=0.8,
                           score_target_weekend=0.8, fair_tolerance=0.0, max_weekends=2, night_rest_days=1)
    d["department"]["headcounts"] = [
        {"shift": s, "weekday_min": 1, "weekday_max": 2, "weekend_min": 1, "weekend_max": 2, "also_counts": []}
        for s in ("D", "N")]
    d["general"]["weights"].update(score_spread=1, score_deficit=1, excess_weekend=1)
    d["nurses"][3]["allowed_shifts"] = None
    d["solver"]["time_limit_seconds"] = 120
    return d
