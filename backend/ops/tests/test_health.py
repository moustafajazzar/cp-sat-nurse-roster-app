import httpx
import pytest
from django.test import Client


@pytest.fixture
def solver_answers(monkeypatch):
    def fake_get(url, headers, timeout):
        assert url == "http://solver.test/health"
        assert headers == {"Authorization": "Bearer test-token"}
        return httpx.Response(200, json={"status": "ok", "version": "0.1.0", "workers": 8},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", fake_get)


@pytest.fixture
def solver_down(monkeypatch):
    def fake_get(url, headers, timeout):
        raise httpx.ConnectError("no route to solver")
    monkeypatch.setattr(httpx, "get", fake_get)


@pytest.fixture(autouse=True)
def solver_settings(settings):
    settings.SOLVER_URL = "http://solver.test"
    settings.SOLVER_TOKEN = "test-token"


@pytest.mark.django_db
def test_health_reports_the_database_and_the_solver(solver_answers):
    r = Client().get("/api/health")

    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": True, "solver": {"reachable": True}}


@pytest.mark.django_db
def test_health_is_degraded_but_answers_when_the_solver_is_down(solver_down):
    r = Client().get("/api/health")

    assert r.status_code == 200
    assert r.json() == {"status": "degraded", "database": True, "solver": {"reachable": False}}


def test_the_openapi_document_and_docs_are_not_served():
    assert Client().get("/api/openapi.json").status_code == 404
    assert Client().get("/api/docs").status_code == 404


class Solver:
    """The solver's `/health`, answering or not as the test says, counting the calls the application makes to it."""

    def __init__(self):
        self.up, self.calls = True, 0

    def get(self, url, headers, timeout):
        self.calls += 1
        if not self.up:
            raise httpx.ConnectError("no route to solver")
        return httpx.Response(200, json={"status": "ok", "version": "0.1.0", "workers": 8}, request=httpx.Request("GET", url))


@pytest.fixture
def solver(monkeypatch):
    solver = Solver()
    monkeypatch.setattr(httpx, "get", solver.get)
    return solver


class Clock:
    def __init__(self):
        self.seconds = 1000.0

    def __call__(self):
        return self.seconds

    def move(self, seconds):
        self.seconds += seconds


@pytest.fixture
def clock(monkeypatch):
    from ops import public_health
    clock = Clock()
    monkeypatch.setattr(public_health, "clock", clock)
    return clock


OK = {"status": "ok", "database": True, "solver": {"reachable": True}}
DEGRADED = {"status": "degraded", "database": True, "solver": {"reachable": False}}


@pytest.mark.django_db
class TestThePublicAnswerIsHeldFiveSeconds:
    def test_within_five_seconds_the_answer_is_the_same_and_nothing_is_asked_again(self, solver, clock,
                                                                                   django_assert_num_queries):
        assert Client().get("/api/health").json() == OK
        solver.up = False
        clock.move(4.9)

        with django_assert_num_queries(0):
            r = Client().get("/api/health")

        assert r.status_code == 200 and r.json() == OK
        assert solver.calls == 1

    def test_after_five_seconds_it_is_asked_again(self, solver, clock):
        Client().get("/api/health")
        solver.up = False
        clock.move(5.1)

        assert Client().get("/api/health").json() == DEGRADED
        assert solver.calls == 2

    def test_a_degraded_answer_is_held_too_and_then_turns_ok(self, solver, clock):
        solver.up = False
        assert Client().get("/api/health").json() == DEGRADED
        solver.up = True
        clock.move(4.9)
        assert Client().get("/api/health").json() == DEGRADED
        clock.move(0.2)
        assert Client().get("/api/health").json() == OK
        assert solver.calls == 2


@pytest.mark.django_db
def test_the_system_admins_health_page_is_not_held(solver, clock, signed_in):
    client = signed_in()
    assert client.get("/api/ops/health").json()["solver"]["reachable"] is True
    solver.up = False

    assert client.get("/api/ops/health").json()["solver"]["reachable"] is False
    assert solver.calls == 2


@pytest.mark.django_db
def test_metrics_are_not_held(solver, clock):
    def solver_up():
        body = Client().get("/metrics").content.decode()
        return next(line for line in body.splitlines() if line.startswith("nurse_roster_solver_up"))

    assert solver_up().endswith(" 1")
    solver.up = False

    assert solver_up().endswith(" 0")
    assert solver.calls == 2
