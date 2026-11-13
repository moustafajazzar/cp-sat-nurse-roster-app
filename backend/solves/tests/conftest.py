"""The scripted solver: the real API and the real job, with only the solver service replaced. `end_to_end` has the real one."""
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
from pathlib import Path

import pytest
from asgiref.sync import sync_to_async
from django.db import connection, connections
from django.db.models import Q
from django.utils import timezone
from procrastinate import PsycopgConnector
from procrastinate.contrib.django import app
from procrastinate.contrib.django.db_cleanup import with_db_cleanup
from procrastinate.contrib.django.models import ProcrastinateJob
from procrastinate.contrib.django.utils import connector_params

from accounts.models import Role, User
from conftest import post
from hospital.catalogue import seed_catalogue
from hospital.models import FixedRamadanMonth, HospitalSettings
from nurses import membership
from nurses.models import NurseProfile, Placement
from nurses.tests.conftest import make_unit
from ops import solver_client
from solves import running
from units.models import Unit

SOLVER = Path(__file__).resolve().parents[3] / "solver"
SAMPLE = json.loads((SOLVER / "service_tests" / "fixtures" / "four_nurses.json").read_text())
MONTH = "2026-06"
DATES = [f"2026-06-{day:02d}" for day in range(1, 31)]
BEFORE = (2026, 5, 15)


def roster_of(nurses, shift="D") -> dict:
    return {nurse: dict.fromkeys(DATES, shift) for nurse in nurses}


def result_of(roster, *, ok=True, cost=7, stopped_by="proof", message="") -> dict:
    return {"version": 1, "month": MONTH, "ok": ok, "status": "OPTIMAL" if ok else "INFEASIBLE", "message": message,
            "precheck": [], "roster": roster, "violations": [], "elapsed_violations": [], "cost": cost,
            "penalties": {"night_rest": 2, "fair_day": 5, "change": 0}, "department_cost": 0,
            "solver": {"improvements": 3, "stopped_by": stopped_by}, "replan": None, "nurses": None}


def job(state="running", *, result=None, best_cost=None, improvements=0, elapsed=1.5, error=None, cancel_requested=False):
    return {"job_id": "j", "state": state, "elapsed_seconds": elapsed, "cancel_requested": cancel_requested,
            "best_cost": best_cost, "improvements": improvements, "best_roster": None, "result": result, "error": error}


class ScriptedSolver:
    """Each `start_solve` takes the next attempt's script: a list of what each `read_job` answers, the last repeating, or an
    exception to raise. `on_read` runs before each answer, so a test can act while the solve is under way."""

    def __init__(self):
        self.scripts = []
        self.issues = []
        self.started = []
        self.cancelled = []
        self.reads = {}
        self.on_read = lambda job_id: None
        self.busy_with = None
        self.precheck_error = None

    def script(self, *attempts):
        self.scripts.extend(attempts)

    def precheck(self, solve_input):
        if self.precheck_error:
            raise self.precheck_error
        return self.issues

    def start_solve(self, solve_input):
        if self.busy_with:
            busy, self.busy_with = self.busy_with, None
            raise solver_client.SolverBusy(busy)
        self.started.append(solve_input)
        script = self.scripts.pop(0)
        if isinstance(script, Exception):
            raise script
        job_id = f"job-{len(self.started)}"
        self.reads[job_id] = list(script)
        return job_id

    def read_job(self, job_id):
        self.on_read(job_id)
        answers = self.reads.get(job_id)
        if answers is None:
            raise solver_client.SolverJobUnknown(job_id)
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def cancel_job(self, job_id):
        self.cancelled.append(job_id)
        return {}


@pytest.fixture
def solver(monkeypatch):
    scripted = ScriptedSolver()
    for name in ("precheck", "start_solve", "read_job", "cancel_job"):
        monkeypatch.setattr(solver_client, name, getattr(scripted, name))
    monkeypatch.setattr(running, "POLL_SECONDS", 0)
    monkeypatch.setattr(running, "WAIT_BEFORE_RETRY_SECONDS", 0)
    return scripted


@pytest.fixture
def before_june(monkeypatch):
    monkeypatch.setattr(membership, "today", lambda: dt.date(*BEFORE))


@pytest.fixture
def catalogue(db):
    seed_catalogue()
    FixedRamadanMonth.objects.create(month=MONTH, dates=[])


def nurse_in(unit, make_user, hospital_id, *, full_name=None, gender="F", start=dt.date(2026, 1, 1), end=None, role=Role.STAFF_NURSE,
             **attributes) -> NurseProfile:
    user = make_user(hospital_id, role, full_name=full_name or hospital_id)
    nurse = NurseProfile.objects.create(user=user, gender=gender, start_date=start, end_date=end, **attributes)
    Placement.objects.create(nurse=nurse, unit=unit, from_month=dt.date(2026, 1, 1))
    return nurse


def sample_rules() -> dict:
    return {key: value for key, value in copy.deepcopy(SAMPLE["department"]).items() if key != "name"}


def make_sample_unit(name, manager, make_user, prefix="") -> Unit:
    """The solver's own four-nurse sample, as this app would hold it. The general block's weights are the sample's too."""
    settings = HospitalSettings.load()
    settings.rules = {key: value for key, value in copy.deepcopy(SAMPLE["general"]).items()
                      if key not in ("shift_hours", "required_skills")}
    settings.save()
    unit = make_unit(name, manager)
    unit.rules = sample_rules()
    unit.save()
    for row in SAMPLE["nurses"]:
        nurse = nurse_in(unit, make_user, f"{prefix}{row['id']}", gender=row["gender"], team_leader=row["team_leader"],
                         score=row["score"], ramadan_reduced=row["ramadan_reduced"], breastfeeding=row["breastfeeding"],
                         ytd_overtime=row["ytd_overtime"], hours_balance=row["hours_balance"],
                         night_balance=row["night_balance"], friday_balance=row["friday_balance"])
        Placement.objects.filter(nurse=nurse).update(allowed_shifts=row.get("allowed_shifts"))
    return unit


def start_solve(client, unit, month=MONTH, seconds=60):
    return post(client, f"/api/units/{unit.pk}/solves", {"month": month, "time_limit_seconds": seconds})


def started(client, unit, month=MONTH, seconds=60) -> dict:
    response = start_solve(client, unit, month, seconds)
    assert response.status_code == 201, response.content
    return response.json()


def run_the_queue(concurrency: int = 1, *, leave_waiting: bool = False) -> None:
    """A real worker, on real connections and with the cleanup `manage.py procrastinate worker` has, until the queue is empty:
    needs `django_db(transaction=True)`. `deploy/compose.yaml` runs it at a concurrency of 4. A worker that does not
    wait stops once it finds nothing it may fetch, which at a concurrency above 1 can be while a job waits on another's lock, so it
    is run again until no job waits; with `leave_waiting`, until no job it may run now waits, leaving one deferred for later."""
    async def work():
        queue_app = app.with_connector(PsycopgConnector(kwargs=connector_params()))
        async with queue_app.open_async():
            await queue_app.run_worker_async(wait=False, install_signal_handlers=False, listen_notify=False,
                                           concurrency=concurrency, **with_db_cleanup(None))
        # The periodic deferrer's connection, which nothing else closes and which would stop the test database being dropped.
        await sync_to_async(connections.close_all, thread_sensitive=True)()
    for _ in range(100):
        asyncio.run(work())
        waiting = ProcrastinateJob.objects.filter(status="todo")
        if leave_waiting:
            waiting = waiting.filter(Q(scheduled_at__isnull=True) | Q(scheduled_at__lte=timezone.now()))
        if not waiting.exists():
            return
    raise AssertionError("jobs still wait on the queue")


@pytest.fixture
def empty_queue(transactional_db):
    """A test on real connections commits, and flushing leaves the queue's tables alone, because Django does not manage them."""
    with connection.cursor() as cursor:
        cursor.execute("TRUNCATE procrastinate_jobs, procrastinate_workers, procrastinate_periodic_defers, procrastinate_events")


@pytest.fixture
def mona(client_of):
    return client_of("mona", Role.UNIT_MANAGER, full_name="Mona Ali")


@pytest.fixture
def er(mona, make_user, catalogue):
    return make_sample_unit("ER", User.objects.get(username="mona"), make_user)


NURSES = ["Amal", "Badr", "Dana", "Emad"]
