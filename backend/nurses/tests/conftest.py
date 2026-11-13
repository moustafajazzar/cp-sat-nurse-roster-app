import copy
import datetime as dt

import pytest

from accounts.models import Role
from conftest import post
from nurses import membership
from units.models import Unit
from units.rules import DEFAULT_UNIT_RULES

NURSES = "/api/nurses"
START = "2026-01-10"


def make_unit(name, manager=None, **fields) -> Unit:
    return Unit.objects.create(name=name, manager=manager, rules=copy.deepcopy(DEFAULT_UNIT_RULES), **fields)


def add(client, unit, hospital_id="h123", **fields):
    body = {"hospital_id": hospital_id, "full_name": "Amal Saleh", "gender": "F", "unit_id": str(unit.pk),
            "start_date": START} | fields
    return post(client, NURSES, body)


def added(client, unit, hospital_id="h123", **fields) -> dict:
    response = add(client, unit, hospital_id, **fields)
    assert response.status_code == 201, response.content
    return response.json()


def refused_fields(response) -> set[tuple]:
    assert response.status_code == 422, response.content
    return {tuple(error["loc"][2:]) for error in response.json()["detail"]}


def message(response) -> str:
    assert response.status_code == 422, response.content
    return response.json()["detail"][0]["msg"]


@pytest.fixture
def cno(client_of):
    return client_of("chief", Role.CNO)


@pytest.fixture
def er(db):
    return make_unit("ER")


@pytest.fixture
def icu(db):
    return make_unit("ICU")


@pytest.fixture
def on_day(monkeypatch):
    """Moves the hospital's today, so a month can begin or an end date pass without waiting."""
    def move(*date):
        monkeypatch.setattr(membership, "today", lambda: dt.date(*date))
    return move
