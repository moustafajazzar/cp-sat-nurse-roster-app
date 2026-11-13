"""Requests reach a solve as pins, so these tests use the roster tests' scene: the solver's four-nurse sample as ER, rostering
June 2026 from 15 May, with the scripted solver and the real worker's code."""
import datetime as dt

import pytest

from conftest import post
from nurses import membership
from rosters.tests.conftest import before_june, catalogue, er, mona, solver  # noqa: F401
from rosters.tests.test_review import carla, dina  # noqa: F401

MONTH = "2026-06"


def url(unit, rest=""):
    return f"/api/units/{unit.pk}{rest}"


def ok(response, status=200) -> dict:
    assert response.status_code == status, response.content
    return response.json()


def refused(response, status=409) -> str:
    assert response.status_code == status, response.content
    detail = response.json()["detail"]
    return detail if isinstance(detail, str) else detail[0]["msg"]


def open_window(client, unit, month=MONTH, opens="2026-05-15", closes="2026-05-25"):
    return post(client, url(unit, "/request-windows"), {"month": month, "opens_on": opens, "closes_on": closes})


def opened(client, unit, month=MONTH, opens="2026-05-15", closes="2026-05-25") -> dict:
    return ok(open_window(client, unit, month, opens, closes), 201)


def ask(client, day, shift=None, reason="", month=MONTH):
    return post(client, "/api/requests/mine", {"date": f"{month}-{day:02d}", "shift": shift, "reason": reason})


def asked(client, day, shift=None, reason="", month=MONTH) -> dict:
    return ok(ask(client, day, shift, reason, month), 201)


def mine(client, month=MONTH) -> dict:
    return ok(client.get("/api/requests/mine", {"month": month}))


def windows(client, unit) -> dict:
    return ok(client.get(url(unit, "/request-windows")))


def requests_in(client, unit, window_id) -> list[dict]:
    return ok(client.get(url(unit, f"/request-windows/{window_id}/requests")))  # type: ignore[return-value]


def decide(client, unit, request_id, what, **body):
    return post(client, url(unit, f"/requests/{request_id}/{what}"), body)


def decided(client, unit, request_id, what, **body) -> dict:
    return ok(decide(client, unit, request_id, what, **body))


@pytest.fixture
def on_day(monkeypatch):
    """Moves the hospital's clock to a day, without waiting on one."""
    def move(year, month, day):
        monkeypatch.setattr(membership, "today", lambda: dt.date(year, month, day))
    return move
