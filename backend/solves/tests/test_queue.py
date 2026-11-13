"""The real queue on real connections, with the scripted solver."""
import pytest
from django.db import connection

from accounts.models import Role, User
from conftest import post
from ops import solver_client
from solves.models import Solve
from solves.tasks import recover_lost_jobs
from solves.tests.conftest import NURSES, job, make_sample_unit, result_of, roster_of, run_the_queue, started
from solves.tests.test_flow import url

pytestmark = pytest.mark.usefixtures("empty_queue")


@pytest.fixture
def icu_manager(client_of):
    return client_of("omar", Role.UNIT_MANAGER, full_name="Omar Said")


@pytest.fixture
def icu(icu_manager, make_user, catalogue):
    return make_sample_unit("ICU", User.objects.get(username="omar"), make_user, prefix="i")


@pytest.fixture
def ccu_manager(client_of):
    return client_of("sara", Role.UNIT_MANAGER, full_name="Sara Hadi")


@pytest.fixture
def ccu(ccu_manager, make_user, catalogue):
    return make_sample_unit("CCU", User.objects.get(username="sara"), make_user, prefix="c")


def finished():
    return [job("finished", result=result_of(roster_of(NURSES)))]


@pytest.mark.parametrize("concurrency", [1, 4])
def test_first_come_first_served_across_units_one_at_a_time(mona, er, icu_manager, icu, ccu_manager, ccu, solver, before_june,
                                                            monkeypatch, concurrency):
    """Three solves of three units, since a unit has one active at a time, started out of their names' order. The
    scripted solver proves none overlapped: each `start_solve` finds the row before it ended. At the worker's concurrency of 4
    too: the solves share one lock."""
    solver.script(finished(), finished(), finished())
    first, second, third = started(mona, er), started(icu_manager, icu), started(ccu_manager, ccu)
    assert [first["queue_ahead"], second["queue_ahead"], third["queue_ahead"]] == [0, 1, 2]
    order = []
    original = solver.start_solve

    def seen(solve_input):
        order.append(solve_input["department"]["name"])
        assert Solve.objects.filter(state="running").count() == 1
        return original(solve_input)
    monkeypatch.setattr(solver_client, "start_solve", seen)

    run_the_queue(concurrency)

    assert order == ["ER", "ICU", "CCU"]
    ended = [Solve.objects.get(pk=row["id"]) for row in (first, second, third)]
    assert all(row.state == "succeeded" for row in ended)
    assert ended[0].finished_at <= ended[1].started_at and ended[1].finished_at <= ended[2].started_at


def test_another_units_solve_is_not_held_by_one_units_month(mona, er, icu_manager, icu, solver, before_june):
    """The one-active limit is a unit's: ICU starts while ER's June waits, and only ER's July is refused."""
    started(mona, er)

    assert started(icu_manager, icu)["queue_ahead"] == 1
    refused = post(mona, url(er, "/solves"), {"month": "2026-07", "time_limit_seconds": 60})
    assert refused.status_code == 409 and refused.json()["detail"].startswith("A solve of June 2026 is already queued")


def test_stopping_the_one_ahead_moves_the_next_one_up(mona, er, icu_manager, icu, solver, before_june):
    first, second = started(mona, er), started(icu_manager, icu)
    assert icu_manager.get(url(icu, f"/solves/{second['id']}")).json()["queue_ahead"] == 1

    assert post(mona, url(er, f"/solves/{first['id']}/stop"), {}).status_code == 200

    assert icu_manager.get(url(icu, f"/solves/{second['id']}")).json()["queue_ahead"] == 0
    solver.script(finished())
    run_the_queue()
    assert [input_["department"]["name"] for input_ in solver.started] == ["ICU"]


def test_a_retry_keeps_its_place_in_line(mona, er, icu_manager, icu, solver, before_june):
    solver.script([job("failed", error="crashed")], finished(), finished())
    started(mona, er), started(icu_manager, icu)

    run_the_queue()

    assert [input_["department"]["name"] for input_ in solver.started] == ["ER", "ER", "ICU"]


def test_a_worker_that_dies_is_recovered_by_the_periodic_job(mona, er, solver, before_june):
    """Its job holds the hospital-wide lock, so until the recovery nothing else would run."""
    solve = started(mona, er)
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now() - interval '5 minutes') RETURNING id")
        worker = cursor.fetchone()[0]
        cursor.execute("UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s", [worker])
    Solve.objects.filter(pk=solve["id"]).update(state="running", attempts=1, solver_job_id="job-gone")
    solver.script(finished())

    recover_lost_jobs(timestamp=0)
    run_the_queue()
    assert Solve.objects.get(pk=solve["id"]).state == "succeeded"


def test_a_solve_and_its_job_arrive_together(mona, er, before_june, solver):
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM procrastinate_jobs")
        assert cursor.fetchone()[0] == 0
    started(mona, er)
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*), max(id) FROM procrastinate_jobs WHERE task_name LIKE '%run_solve'")
        count, job_id = cursor.fetchone()
    assert count == 1 and Solve.objects.get().job_id == job_id


def test_every_periodic_job_takes_a_lock_under_its_own_name(transactional_db):
    """Its queueing lock keeps a second copy from waiting, and its lock keeps it from running beside its own previous run, at the
    worker's concurrency of 4. Solves keep the hospital-wide one."""
    from procrastinate.contrib.django import app

    periodic = [task.task for task in app.periodic_registry.periodic_tasks.values()]
    assert {task.name.rsplit(".", 1)[-1] for task in periodic} >= {"close_month", "recover_lost_jobs", "expire_swaps", "expire_covers",
                                                                   "open_and_close_request_windows", "close_ended_nurses"}
    for task in periodic:
        task.defer(timestamp=0)
    with connection.cursor() as cursor:
        cursor.execute("SELECT task_name, lock, queueing_lock FROM procrastinate_jobs")
        rows = cursor.fetchall()
    assert len(rows) == len(periodic)
    assert all(lock == queueing_lock == name.rsplit(".", 1)[-1] for name, lock, queueing_lock in rows)


def july_roster() -> dict:
    return {nurse: {f"2026-07-{day:02d}": "D" for day in range(1, 32)} for nurse in NURSES}


def test_a_scheduled_job_runs_beside_a_solve_and_a_close_never_waits_for_one(mona, er, solver, monkeypatch, client_of):
    """At the worker's concurrency of 4, with a solve held open by the scripted solver until the close's row exists: nothing waits
    on a clock. A worker that ran one job at a time would never let the close run, and the solve would end on its own read
    count, with no close."""
    import datetime as dt

    from closes.models import MonthClose
    from closes.tasks import close_month
    from nurses import membership
    from rosters.tests.test_review import approve, ok, solved, submit
    from solves.tests.conftest import result_of

    monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 5, 15))
    carla = client_of("carla", Role.CNO, full_name="Carla CNO")
    solved(mona, er, solver)
    ok(submit(mona, er))
    ok(approve(carla, er))
    monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 7, 2))
    solve = started(mona, er, "2026-07")
    seen, reads = [], [0]

    def held_open(job_id):
        reads[0] += 1
        if not seen and (MonthClose.objects.exists() or reads[0] > 20000):
            seen.append((MonthClose.objects.exists(), Solve.objects.get(pk=solve["id"]).state))
            solver.reads[job_id] = [job("finished", result=result_of(july_roster()) | {"month": "2026-07"})]
    solver.on_read = held_open
    solver.script([job("running")])
    close_month.defer(timestamp=0)
    recover_lost_jobs.defer(timestamp=0)

    run_the_queue(concurrency=4)

    assert seen == [(True, "running")]
    assert Solve.objects.get(pk=solve["id"]).state == "succeeded"
    assert [(close.unit_id, close.month) for close in MonthClose.objects.all()] == [(er.pk, dt.date(2026, 6, 1))]
    with connection.cursor() as cursor:
        cursor.execute("SELECT task_name, status FROM procrastinate_jobs ORDER BY id")
        statuses = {name.rsplit(".", 1)[-1]: status for name, status in cursor.fetchall()}
    # The worker's own periodic deferrer may add the hourly jobs due now; whatever ran, none failed.
    assert {"run_solve", "close_month", "recover_lost_jobs"} <= set(statuses) and set(statuses.values()) == {"succeeded"}
