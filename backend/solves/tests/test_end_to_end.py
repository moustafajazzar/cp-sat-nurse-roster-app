"""Once with everything real: the service in its own process on the solver's own four-nurse sample, the real queue and a real
worker, spoken to through the API. Nothing here assumes how long a search takes: a stop is asked for only once the row shows a
roster was found, and the sample with looser rules cannot be proven optimal for far longer than anything here waits."""
from __future__ import annotations

import datetime as dt
import os
import re
import socket
import subprocess
import sys
import threading
import time

import httpx
import pytest

from accounts.models import Role, User
from conftest import post
from hospital.models import HospitalSettings, LeaveType
from nurses.models import Leave, NurseProfile, Placement
from rosters.models import Roster
from solves import running
from solves.models import Solve
from solves.tests.conftest import MONTH, NURSES, SOLVER, make_sample_unit, run_the_queue, started
from solves.tests.test_flow import month_of, preview, url

pytestmark = pytest.mark.usefixtures("empty_queue")
TOKEN = "end-to-end-token"
SURELY = 120.0  # How long a machine under any load may take before something is really wrong.


@pytest.fixture(scope="module")
def solver_service():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    environment = {key: value for key, value in os.environ.items() if key != "NURSE_ROSTER_WORKERS"} | {"SOLVER_TOKEN": TOKEN}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "--factory", "solver_service.app:create_app", "--host", "127.0.0.1",
                               "--port", str(port), "--log-level", "warning"], cwd=SOLVER, env=environment)
    address = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + SURELY
        while True:
            try:
                httpx.get(f"{address}/health", timeout=1)
                break
            except httpx.TransportError:
                assert server.poll() is None and time.monotonic() < deadline, "the service did not start"
                time.sleep(0.1)
        yield address
    finally:
        server.terminate()
        server.wait(timeout=30)


@pytest.fixture(autouse=True)
def _real_solver(settings, solver_service, before_june, monkeypatch):
    settings.SOLVER_URL, settings.SOLVER_TOKEN = solver_service, TOKEN
    monkeypatch.setattr(running, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(running, "WAIT_BEFORE_RETRY_SECONDS", 0)


def long_search(unit: object) -> None:
    """The same four nurses, as in solver/service_tests/conftest.py: a first roster at once, and no proof for many seconds."""
    settings = HospitalSettings.load()
    settings.rules["weights"].update(score_spread=1, score_deficit=1, excess_weekend=1)
    settings.save()
    unit.rules.update(score_balance=True, score_balance_mode="spread", score_target=0.8, score_target_weekend=0.8,
                      fair_tolerance=0.0, max_weekends=2, night_rest_days=1)
    unit.rules["headcounts"] = [{"shift": shift, "weekday_min": 1, "weekday_max": 2, "weekend_min": 1, "weekend_max": 2,
                                 "also_counts": []} for shift in ("D", "N")]
    unit.save()
    Placement.objects.filter(unit=unit).update(allowed_shifts=None)


def working_in_the_background():
    worker = threading.Thread(target=run_the_queue)
    worker.start()
    return worker


def until(check, what: str):
    deadline = time.monotonic() + SURELY
    while time.monotonic() < deadline:
        answer = check()
        if answer:
            return answer
        time.sleep(0.02)
    pytest.fail(f"never saw {what}")


def test_the_whole_flow(mona, er):
    badr = NurseProfile.objects.get(user__username="Badr")
    sick = LeaveType.objects.get(name="Sick")
    for day in (10, 11):
        Leave.objects.create(nurse=badr, date=dt.date(2026, 6, day), leave_type=sick, recorded_by=badr.user)

    plan = preview(mona, er)
    assert plan["summary"]["nurses"] == 4 and plan["summary"]["leave_days"] == 2 and plan["rules_refusal"] is None
    assert isinstance(plan["precheck"], list)

    solve = started(mona, er, seconds=30)
    run_the_queue()

    stored = Solve.objects.get(pk=solve["id"])
    assert stored.state == "succeeded" and stored.result["ok"] is True and stored.result["month"] == MONTH
    answer = month_of(mona, er)
    draft = answer["draft"]
    assert answer["active_solve"] is None and draft["solve_id"] == solve["id"] and draft["cost"] == stored.result["cost"]
    assert [row["hospital_id"] for row in draft["rows"]] == NURSES and len(draft["days"]) == 30
    badr_cells = next(row for row in draft["rows"] if row["hospital_id"] == "Badr")["cells"]
    assert badr_cells[9] == {"shift": "", "kind": "leave"} and badr_cells[10] == {"shift": "", "kind": "leave"}
    assert {cell["kind"] for row in draft["rows"] for cell in row["cells"]} <= {"shift", "off", "leave"}
    assert Roster.objects.get(unit=er).grid == stored.result["roster"]

    answer = mona.get(url(er, f"/solve-history/{solve['id']}")).json()
    assert answer["explained"] is True and answer["solve"]["in_draft"] is True and answer["statistics"]["workers"] >= 1
    # The solver's own code, on what is stored, comes to what the solver said while it ran.
    assert [rule["penalty"] for rule in answer["rules"]] == sorted((p for p in stored.result["penalties"].values() if p), reverse=True)
    assert all(rule["weight"] * rule["amount"] == rule["penalty"] for rule in answer["rules"])
    assert sum(nurse["cost"] for nurse in answer["nurses"]) + answer["unit_cost"] == stored.result["cost"]
    badr_hours = next(nurse for nurse in answer["nurses"] if nurse["hospital_id"] == "Badr")
    assert badr_hours["leave_credit"] > 0
    assert [row["hospital_id"] for row in answer["grid"]["rows"]] == NURSES
    matches = [match for rule in answer["rules"] for match in rule["largest"] if match["date"]]
    assert not [match["text"] for match in matches if re.search(r"(?<!/)day \d", match["text"])]


def test_a_stop_keeps_the_best_roster_found(mona, er):
    long_search(er)
    solve = started(mona, er, seconds=120)
    worker = working_in_the_background()
    try:
        until(lambda: Solve.objects.get(pk=solve["id"]).improvements >= 1, "a first roster")
        assert Solve.objects.get(pk=solve["id"]).state == "running"
        assert post(mona, url(er, f"/solves/{solve['id']}/stop"), {}).status_code == 200
    finally:
        worker.join(SURELY)
    assert not worker.is_alive()

    stored = Solve.objects.get(pk=solve["id"])
    assert stored.state == "cancelled" and stored.result["solver"]["stopped_by"] == "cancelled"
    draft = month_of(mona, er)["draft"]
    assert draft["stopped_early"] is True and draft["cost"] == stored.result["cost"]


def test_two_units_queue_first_come_first_served(mona, er, make_user, client_of):
    other = client_of("omar", Role.UNIT_MANAGER)
    icu = make_sample_unit("ICU", User.objects.get(username="omar"), make_user, prefix="i")
    first, second = started(mona, er, seconds=30), started(other, icu, seconds=30)
    assert (first["queue_ahead"], second["queue_ahead"]) == (0, 1)

    run_the_queue()

    one, two = Solve.objects.get(pk=first["id"]), Solve.objects.get(pk=second["id"])
    assert one.state == two.state == "succeeded"
    assert one.finished_at <= two.started_at
    assert Roster.objects.filter(unit=er).exists() and Roster.objects.filter(unit=icu).exists()


def test_a_re_plan_from_a_date_keeps_the_days_before_it_and_becomes_a_version(mona, er, client_of, monkeypatch):
    """A re-plan with the real solver: a real solve, a real approval, leave late on a worked day, then a re-plan from the
    12th. The solver's own facts and the version the re-plan made are read back through the API."""
    from rosters.models import RosterVersion
    from rosters.tests.test_review import approve, submit
    carla = client_of("carla", Role.CNO, full_name="Carla CNO")
    started(mona, er, seconds=30)
    run_the_queue()
    assert submit(mona, er).status_code == 200 and approve(carla, er).status_code == 200
    from nurses import membership
    monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 6, 12))
    in_force = Roster.objects.get(unit=er).grid
    worked = next(day for day in range(14, 31) if in_force["Badr"][f"2026-06-{day:02d}"])
    sick = LeaveType.objects.get(name="Sick")
    badr = NurseProfile.objects.get(user__username="Badr")
    response = post(mona, url(er, "/leave"), {"nurse_id": str(badr.pk), "leave_type_id": str(sick.pk),
                                              "first": f"2026-06-{worked:02d}", "last": f"2026-06-{worked:02d}"})
    assert response.status_code == 201, response.content
    before = Roster.objects.get(unit=er).grid
    assert before["Badr"][f"2026-06-{worked:02d}"] == "" and RosterVersion.objects.count() == 2

    response = post(mona, url(er, "/replans"), {"month": MONTH, "replan_from": "2026-06-12", "time_limit_seconds": 30,
                                                "reason": "Badr is off sick."})
    assert response.status_code == 201, response.content
    run_the_queue()

    stored = Solve.objects.get(pk=response.json()["id"])
    assert stored.state == "succeeded" and stored.result["ok"] is True
    assert stored.result["replan"] == {"from": "2026-06-12", "recorded_cells_differ": 0}
    after = Roster.objects.get(unit=er).grid
    for nurse, row in before.items():
        assert [row[f"2026-06-{day:02d}"] for day in range(1, 12)] == [after[nurse][f"2026-06-{day:02d}"] for day in range(1, 12)]
    assert after["Badr"][f"2026-06-{worked:02d}"] == ""
    # Taking Badr's night off leaves him short, which the solver has always answered with changed cells. Whether it does is the
    # solver's search to decide, so an unchanged roster is asserted to be one, not taken for a failure.
    if stored.replan_outcome == "version":
        version = RosterVersion.objects.order_by("-number").first()
        assert (version.number, version.origin, version.reason, version.solve_id) == (3, "replan", "Badr is off sick.", stored.pk)
    else:
        assert stored.replan_outcome == "unchanged" and RosterVersion.objects.count() == 2
    comparison = mona.get(url(er, f"/solve-history/{stored.pk}")).json()["replan"]
    assert comparison["based_on"] == 2 and comparison["cost_after"] == stored.result["cost"]
    assert comparison["cost_before"] is not None and comparison["rules"]


def test_a_real_month_closes_with_the_carry_over_its_real_solve_worked_out(mona, er, client_of, monkeypatch):
    """Month close with the real solver: the four-nurse sample solved by the service, approved, and closed by the schedule. Nothing
    was recorded after the solve, so the close's facts are its roster, and each nurse's hours and carry-over are the solve's own
    `nurses` block: the close's no-search computation (`solver_service.grid.nurses_of`) is the solver's."""
    from django.utils import timezone

    from closes import making
    from nurses import membership
    from rosters.tests.test_review import approve, submit
    carla = client_of("carla", Role.CNO, full_name="Carla CNO")
    solve = started(mona, er, seconds=30)
    run_the_queue()
    stored = Solve.objects.get(pk=solve["id"])
    assert stored.result["ok"] is True
    assert submit(mona, er).status_code == 200 and approve(carla, er).status_code == 200
    monkeypatch.setattr(membership, "today", lambda: dt.date(2026, 7, 1))

    [close] = making.catch_up(timezone.make_aware(dt.datetime(2026, 7, 1, 0, 30)))

    assert close.result["facts"] == stored.result["roster"]
    accounting = ("leave", "contract", "leave_credit", "required", "hours_worked", "overtime", "shortfall", "shifts", "carry_over")
    assert {nurse: {key: row[key] for key in accounting} for nurse, row in close.result["nurses"].items()} == {
        nurse: {key: row[key] for key in accounting} for nurse, row in stored.result["nurses"].items()}
    assert close.result["facts_broke"] == []


def test_a_hard_skill_need_and_an_expired_required_skill_decide_the_roster(mona, er):
    """Skills with the real solver: Day needs one nurse holding ICU every day, which only Amal and Emad hold, and Badr's BLS,
    required to work, lasts to the 20th. The precheck says so in dates beforehand, and the roster keeps both."""
    from hospital.models import Skill
    icu = Skill.objects.create(name="ICU", expires=False, required_to_work=False)
    bls = Skill.objects.create(name="BLS", expires=True, required_to_work=True)
    for hospital_id in ("Amal", "Emad"):
        nurse = NurseProfile.objects.get(user__username=hospital_id)
        nurse.skills.create(skill=icu, recorded_by=nurse.user)
    badr = NurseProfile.objects.get(user__username="Badr")
    badr.skills.create(skill=bls, expiry=dt.date(2026, 6, 20), recorded_by=badr.user)
    er.rules |= {"skill_needs": [{"shift": "D", "skill": "ICU", "weekday_min": 1, "weekend_min": 1}], "skill_needs_hard": True}
    er.save()

    plan = preview(mona, er)
    assert plan["rules_refusal"] is None
    assert ("Badr: BLS, required to work, expires inside the month: its last day is 2026-06-20, so no shift after it"
            in plan["precheck"])

    solve = started(mona, er, seconds=30)
    run_the_queue()

    stored = Solve.objects.get(pk=solve["id"])
    assert stored.state == "succeeded" and stored.result["ok"] is True, stored.result.get("message")
    roster = stored.result["roster"]
    days = [f"2026-06-{day:02d}" for day in range(1, 31)]
    assert all({nurse for nurse in ("Amal", "Emad") if roster[nurse][day] == "D"} for day in days)
    assert not [day for day in days if day > "2026-06-20" and roster["Badr"][day]]
    assert stored.result["violations"] == []
    draft = month_of(mona, er)["draft"]
    assert draft["changes"] == [] and draft["check"] is None
