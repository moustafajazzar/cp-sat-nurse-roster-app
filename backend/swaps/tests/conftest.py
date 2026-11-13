"""Swaps are made on an approved roster, so these tests use the roster tests' scene: the solver's four-nurse sample as ER, its
June 2026 approved as the real optimal roster of `rosters/tests/fixtures/sound_rosters.json`, from 15 May, with the scripted
solver and the real worker's code. Every check of a grid is the solver's own code.

In that June: Badr works Day on the 9th and Emad is off; Emad works Day on the 7th, 8th and 10th, so Emad on Day on the 9th makes
a run of four, the most the rules allow, and Badr off on the 9th breaks nothing. That is `SOUND_SWAP`, which works."""
import datetime as dt

import pytest

from conftest import post
from notifications.tests.test_events import as_user
from nurses import membership
from rosters.tests.conftest import before_june, catalogue, er, mona, solver  # noqa: F401
from rosters.tests.test_review import approved, carla, dina  # noqa: F401

MONTH = "2026-06"


def cell(hospital_id, day, shift=None, month=MONTH) -> dict:
    return {"hospital_id": hospital_id, "date": f"{month}-{day:02d}", "shift": shift}


SOUND_SWAP = [cell("Badr", 9), cell("Emad", 9, "D")]


def url(unit, rest=""):
    return f"/api/units/{unit.pk}{rest}"


def ok(response, status=200) -> dict:
    assert response.status_code == status, response.content
    return response.json()


def refusals(response, status=422) -> list[str]:
    """Every sentence of a refusal, whatever its shape."""
    assert response.status_code == status, response.content
    detail = response.json()["detail"]
    return [detail] if isinstance(detail, str) else [row["msg"].removeprefix("Value error, ") for row in detail]


def propose(client, *cells, note="", month=MONTH):
    return post(client, "/api/swaps/mine", {"month": month, "cells": list(cells), "note": note})


def proposed(client, *cells, note="", month=MONTH) -> dict:
    return ok(propose(client, *cells, note=note, month=month), 201)


def act(client, swap_id, what):
    return post(client, f"/api/swaps/mine/{swap_id}/{what}", {})


def acted(client, swap_id, what) -> dict:
    return ok(act(client, swap_id, what))


def mine(client, month=None) -> list[dict]:
    return ok(client.get("/api/swaps/mine", {"month": month} if month else {}))  # type: ignore[return-value]


def decision(client, unit, swap_id) -> dict:
    return ok(client.get(url(unit, f"/swaps/{swap_id}")))


def decide(client, unit, swap_id, what, reason=""):
    return post(client, url(unit, f"/swaps/{swap_id}/{what}"), {"reason": reason})


def decided(client, unit, swap_id, what, reason="") -> dict:
    return ok(decide(client, unit, swap_id, what, reason))


@pytest.fixture
def in_force(mona, carla, er, solver):
    """June approved as version 1."""
    approved(mona, carla, er, solver)
    return er


@pytest.fixture
def nurse():
    """A nurse of the sample signed in, by hospital ID."""
    return as_user


@pytest.fixture
def on_day(monkeypatch):
    """Moves the hospital's clock to a day, without waiting on one."""
    def move(year, month, day):
        monkeypatch.setattr(membership, "today", lambda: dt.date(year, month, day))
    return move
