"""The solver service's HTTP contract, against the real service and the real solver."""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest

ENDED = ("finished", "cancelled", "failed")


def wait_for(client, job_id, until, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if until(job):
            return job
        time.sleep(0.02)
    pytest.fail(f"job {job_id} did not get there in {timeout} s: {job}")


def ended(job):
    return job["state"] in ENDED


@pytest.fixture
def running(client, long_search):
    """A job that has found at least one roster and is still searching. Always stopped after."""
    r = client.post("/solve", json=long_search)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    wait_for(client, job_id, lambda j: j["improvements"] >= 1 or ended(j))
    yield job_id
    client.post(f"/jobs/{job_id}/cancel")
    wait_for(client, job_id, ended)


# ---- start, poll, finish ----------------------------------------------------------------------
def test_solve_runs_to_a_full_result(client, sample):
    r = client.post("/solve", json=sample)
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    job = wait_for(client, job_id, ended)

    assert job["state"] == "finished" and job["error"] is None
    assert job["cancel_requested"] is False
    assert job["elapsed_seconds"] > 0
    result = job["result"]
    assert result["ok"] is True and result["status"] == "OPTIMAL"
    assert result["month"] == "2026-06" and result["violations"] == []
    assert set(result["roster"]) == {"Amal", "Badr", "Dana", "Emad"}
    assert set(result["nurses"]) == {"Amal", "Badr", "Dana", "Emad"}
    assert "carry_over" in result["nurses"]["Amal"]
    assert all("preferences_not_kept" in n["carry_over"] for n in result["nurses"].values())
    assert job["best_cost"] == result["cost"]
    assert job["best_roster"] == result["roster"]
    assert job["improvements"] == result["solver"]["improvements"] >= 1
    assert result["solver"]["stopped_by"] == "proof"


def test_progress_is_readable_while_a_job_runs(client, running):
    job = client.get(f"/jobs/{running}").json()
    assert job["state"] == "running" and job["result"] is None
    assert job["improvements"] >= 1
    assert isinstance(job["best_cost"], int)
    assert len(job["best_roster"]) == 4 and len(job["best_roster"]["Amal"]) == 30
    assert job["elapsed_seconds"] > 0


def test_unknown_job_is_404(client):
    assert client.get("/jobs/nope").status_code == 404
    assert client.post("/jobs/nope/cancel").status_code == 404


# ---- one job at a time ------------------------------------------------------------------------
def test_second_solve_is_refused_while_one_runs(client, running, sample):
    r = client.post("/solve", json=sample)
    assert r.status_code == 409
    assert r.json()["job_id"] == running


def test_a_new_solve_is_accepted_once_the_last_one_ended(client, sample):
    for _ in range(2):
        r = client.post("/solve", json=sample)
        assert r.status_code == 202
        wait_for(client, r.json()["job_id"], ended)


def test_no_search_calls_still_answer_while_a_job_runs(client, running, sample):
    assert client.post("/precheck", json=sample).status_code == 200
    assert client.get("/health").status_code == 200


# ---- cancel -----------------------------------------------------------------------------------
def test_cancel_keeps_the_best_roster_found(client, running):
    before = client.get(f"/jobs/{running}").json()
    assert before["state"] == "running"

    r = client.post(f"/jobs/{running}/cancel")
    assert r.status_code == 200 and r.json()["cancel_requested"] is True

    job = wait_for(client, running, ended, timeout=10)
    assert job["state"] == "cancelled"
    result = job["result"]
    assert result["solver"]["stopped_by"] == "cancelled"
    assert result["ok"] is True and result["status"] == "FEASIBLE"
    assert result["roster"] is not None and job["best_roster"] == result["roster"]
    assert result["cost"] == job["best_cost"] <= before["best_cost"]
    assert job["elapsed_seconds"] < 60                 # the time limit was 120 s


def test_cancelling_an_ended_job_changes_nothing(client, sample):
    job_id = client.post("/solve", json=sample).json()["job_id"]
    done = wait_for(client, job_id, ended)

    r = client.post(f"/jobs/{job_id}/cancel")

    assert r.status_code == 200
    assert r.json()["state"] == "finished" and r.json()["result"] == done["result"]


# ---- validate ---------------------------------------------------------------------------------
@pytest.fixture
def solved(client, sample):
    job_id = client.post("/solve", json=sample).json()["job_id"]
    return wait_for(client, job_id, ended)["result"]


def test_validate_agrees_with_the_result(client, sample, solved):
    r = client.post("/validate", json={"solve_input": sample, "roster": solved["roster"]})
    assert r.status_code == 200
    v = r.json()
    assert v["ok"] is True and v["violations"] == []
    assert v["cost"] == solved["cost"]
    assert v["penalties"] == solved["penalties"]
    assert v["department_cost"] == solved["department_cost"]
    assert v["nurses"] == {nid: n["cost"] for nid, n in solved["nurses"].items()}
    by_id = {rule["id"]: rule for rule in v["rules"]}
    assert sum(rule["weighted"] for rule in by_id.values()) == v["cost"]
    priced = [rule for rule in by_id.values() if rule["weighted"] > 0]
    assert priced and all(rule["matches"] and rule["matches"][0]["text"] for rule in priced)


def test_validate_reports_a_broken_hard_rule(client, sample, solved):
    roster = copy.deepcopy(solved["roster"])
    roster["Emad"]["2026-06-15"] = "N"                 # Emad may only work days

    v = client.post("/validate", json={"solve_input": sample, "roster": roster}).json()

    assert v["ok"] is False
    assert {"rule": "allowed_shifts", "nurse": "Emad", "date": "2026-06-15"}.items() <= next(
        x for x in v["violations"] if x["rule"] == "allowed_shifts").items()


def test_a_result_carries_the_preferences_it_did_not_keep(client, sample):
    """Dana's two preferences pinned the wrong way (worked the day she asked off, off the day she asked to work): her
    result carries 2 for next month, every other nurse, who made none, 0."""
    pinned = copy.deepcopy(sample)
    dana = next(n for n in pinned["nurses"] if n["id"] == "Dana")
    dana["pinned"] = {"2026-06-05": "D", "2026-06-12": ""}

    result = wait_for(client, client.post("/solve", json=pinned).json()["job_id"], ended)["result"]

    assert result["ok"] is True
    assert {nid: n["carry_over"]["preferences_not_kept"] for nid, n in result["nurses"].items()} == \
        {"Amal": 0, "Badr": 0, "Dana": 2, "Emad": 0}


def test_validate_prices_a_doubled_preference_twice_and_says_so(client, sample, solved):
    roster = copy.deepcopy(solved["roster"])
    roster["Dana"]["2026-06-05"] = "D"                 # the day she asked off, worked
    roster["Dana"]["2026-06-12"] = ""                  # the day she asked to work, off

    v = client.post("/validate", json={"solve_input": sample, "roster": roster}).json()

    rules = {rule["id"]: rule for rule in v["rules"]}
    [off] = [m for m in rules["undesired_day"]["matches"] if m["nurse"] == "Dana"]
    [on] = [m for m in rules["desired_day"]["matches"] if m["nurse"] == "Dana"]
    assert (off["date"], off["amount"], on["date"], on["amount"]) == ("2026-06-05", 2, "2026-06-12", 2)
    assert "counts double" in off["text"] and "counts double" in on["text"]
    assert rules["undesired_day"]["weighted"] == 2 * 2 and rules["desired_day"]["weighted"] == 2 * 1   # weights 2 and 1


def test_validate_keeps_a_replans_published_roster(client, sample, solved):
    """In a re-plan the `change` rule prices a grid against the solve input's own `published`
    block, not against the grid under validation."""
    replan = {**sample, "published": solved["roster"]}
    moved = copy.deepcopy(solved["roster"])
    day = next(d for d, s in moved["Amal"].items() if s == "D")
    moved["Amal"][day] = ""

    v = client.post("/validate", json={"solve_input": replan, "roster": moved}).json()

    change = next(rule for rule in v["rules"] if rule["id"] == "change")
    assert change["raw"] == 1 and change["weighted"] == sample["general"]["weights"]["change"]


@pytest.mark.parametrize("edit, message", [
    (lambda r: r.pop("Amal"), "no roster for nurse 'Amal'"),
    (lambda r: r.update(Zaid={}), "unknown nurse 'Zaid'"),
    (lambda r: r["Amal"].pop("2026-06-01"), "missing date 2026-06-01"),
    (lambda r: r["Amal"].update({"2026-06-01": "X"}), "2026-06-01"),
])
def test_validate_refuses_a_malformed_roster(client, sample, solved, edit, message):
    roster = copy.deepcopy(solved["roster"])
    edit(roster)
    r = client.post("/validate", json={"solve_input": sample, "roster": roster})
    assert r.status_code == 422
    assert message in r.json()["detail"] and r.json()["detail"].startswith("roster")


def test_validate_needs_both_fields(client, sample):
    assert client.post("/validate", json={"solve_input": sample}).status_code == 422
    assert client.post("/validate", json={"solve_input": sample, "roster": None}).status_code == 422


# ---- precheck ---------------------------------------------------------------------------------
def test_precheck_of_a_sound_month_is_empty(client, sample):
    r = client.post("/precheck", json=sample)
    assert r.status_code == 200 and r.json() == {"issues": []}


def test_precheck_says_why_a_month_cannot_work(client, sample):
    sample["department"]["headcounts"][0]["weekday_min"] = 5   # four nurses, five on days
    sample["department"]["headcounts"][0]["weekday_max"] = 5

    issues = client.post("/precheck", json=sample).json()["issues"]

    assert issues and all(isinstance(text, str) and text for text in issues)


# ---- malformed input --------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/solve", "/precheck"])
def test_a_malformed_solve_input_names_the_field(client, sample, path):
    del sample["general"]["weights"]["change"]
    r = client.post(path, json=sample)
    assert r.status_code == 422
    assert "general.weights" in r.json()["detail"] and "change" in r.json()["detail"]


@pytest.mark.parametrize("path", ["/solve", "/precheck", "/validate"])
@pytest.mark.parametrize("body", [b"{not json", b'{"a": 1, "a": 2}', b'{"a":' * 5000 + b"1" + b"}" * 5000, b"[" * 100_000],
                         ids=["syntax", "repeat", "deep", "deep unterminated"])
def test_unusable_json_is_a_422(client, path, body):
    r = client.post(path, content=body)
    assert r.status_code == 422 and r.json()["detail"]


def test_a_malformed_solve_input_leaves_the_service_free(client, sample):
    assert client.post("/solve", json={"version": 1}).status_code == 422
    r = client.post("/solve", json=sample)
    assert r.status_code == 202
    wait_for(client, r.json()["job_id"], ended)


# ---- health and the token ---------------------------------------------------------------------
def test_health_reports_version_and_cpu_count(client):
    from solver_service.app import container_cpus
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": "0.1.0", "workers": container_cpus()}


CALLS = [("POST", "/solve"), ("GET", "/jobs/x"), ("POST", "/jobs/x/cancel"), ("POST", "/validate"),
         ("POST", "/precheck"), ("GET", "/health"), ("GET", "/docs"), ("GET", "/openapi.json"), ("GET", "/nope")]


@pytest.mark.parametrize("method, path", CALLS)
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "contract-test-token"}],
                         ids=["no token", "wrong token", "not bearer"])
def test_every_call_needs_the_token(anonymous, method, path, headers):
    r = anonymous.request(method, path, headers=headers, content=b"{not json" if method == "POST" else None)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_a_refused_call_starts_nothing(anonymous, client, sample):
    assert anonymous.post("/solve", json=sample).status_code == 401
    r = client.post("/solve", json=sample)                 # would be a 409 if the refused one ran
    assert r.status_code == 202
    wait_for(client, r.json()["job_id"], ended)


def test_the_service_refuses_to_start_without_a_token(monkeypatch):
    from solver_service.app import create_app
    monkeypatch.delenv("SOLVER_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="SOLVER_TOKEN"):
        create_app()


# ---- version 2: skills ------------------------------------------------------------------------
def test_a_version_2_input_is_solved_with_its_skills_and_scored_with_both_rules(client):
    """The four nurses with skills (fixtures/four_nurses_skills.json): Dana's BLS, required to work, lasts to 24 June,
    so she works no shift from the 25th; the ICU night need is soft at 50 a nurse short. The precheck says so first,
    the result keeps her off and stays version 1, and /validate names a shift on a barred day and prices the need."""
    skills = json.loads((Path(__file__).parent / "fixtures" / "four_nurses_skills.json").read_text())
    skills["solver"]["time_limit_seconds"] = 2

    issues = client.post("/precheck", json=skills).json()["issues"]
    job = wait_for(client, client.post("/solve", json=skills).json()["job_id"], ended)

    assert "Dana: BLS, required to work, expires inside the month: its last day is 2026-06-24, so no shift after it" \
        in issues
    result = job["result"]
    assert job["state"] == "finished" and result["ok"] is True and result["version"] == 1
    assert [result["roster"]["Dana"][f"2026-06-{d}"] for d in range(25, 31)] == [""] * 6
    assert "skill_need" in result["penalties"] and "skill_expired" not in result["penalties"]

    roster = copy.deepcopy(result["roster"])
    roster["Dana"]["2026-06-27"] = "D"
    v = client.post("/validate", json={"solve_input": skills, "roster": roster}).json()
    assert {"rule": "skill_expired", "nurse": "Dana", "date": "2026-06-27",
            "text": "skill expired: Dana works D on 2026-06-27, BLS is required to work and its last day was 2026-06-24"} \
        in v["violations"]
    need = next(r for r in v["rules"] if r["id"] == "skill_need")
    assert need["level"] == "soft" and need["weight"] == 50 and need["weighted"] == 50 * need["raw"]


# ---- full runs --------------------------------------------------------------------------------
def test_the_sample_carries_the_full_run_cap_and_validate_prices_each_run_above_it(client, sample):
    """The sample (fixtures/four_nurses.json, version 2 with no skills) runs four shifts in a row and caps full runs at 3 a nurse, at 4
    each above it. The solve's result prices the rule; a roster giving Amal six full runs (four days, a day off, five times
    over, and a day off to end the month) costs 3 x 4 for her, in her own explanation, in the solver's words."""
    solved = wait_for(client, client.post("/solve", json=sample).json()["job_id"], ended)["result"]
    assert solved["ok"] is True and "excess_full_run" in solved["penalties"]
    roster = copy.deepcopy(solved["roster"])
    roster["Amal"] = {f"2026-06-{d:02d}": "" if d % 5 == 0 else "D" for d in range(1, 31)}

    v = client.post("/validate", json={"solve_input": sample, "roster": roster}).json()

    rule = next(r for r in v["rules"] if r["id"] == "excess_full_run")
    [match] = [m for m in rule["matches"] if m["nurse"] == "Amal"]
    assert (match["date"], match["amount"], match["text"]) == (None, 3, "Amal: 6 full runs of 4 shifts, max 3")
    assert rule["level"] == "soft" and rule["weight"] == 4 and rule["weighted"] == 4 * rule["raw"]
    assert v["penalties"]["excess_full_run"] == rule["weighted"]
    assert v["nurses"]["Amal"] >= 12


# ---- overtime share ---------------------------------------------------------------------------
def shared(sample: dict) -> dict:
    """The sample, whose unit shares its overtime with 12 h free, the carry moving a share by 12 h at most and an hour above a
    share at 1, with Amal carrying 2.5 h above hers."""
    out = copy.deepcopy(sample)
    out["nurses"][0]["overtime_balance"] = 2.5
    return out


def amal_works_every_owed_day(req: dict) -> dict:
    """June 2026 owes 26 days at 8 h (four Fridays off): Amal 208 h, Badr (two days' leave) 192, Dana (breastfeeding, 7 h a
    day) 182, Emad 208; 790 h in all. Amal works a day shift on each of the 26 days, 312 h, the others nothing: the unit's
    104 h of overtime are all hers. Her share is 104 x 208 / 790 = 27.38 h, Badr's 25.28, Dana's 23.96, Emad's 27.38."""
    dates = [f"2026-06-{d:02d}" for d in range(1, 31)]
    roster = {n["id"]: {day: "" for day in dates} for n in req["nurses"]}
    roster["Amal"] = {day: "" if day in ("2026-06-05", "2026-06-12", "2026-06-19", "2026-06-26") else "D" for day in dates}
    return roster


def test_the_sample_carries_the_overtime_share_and_its_result_carries_each_balance(client, sample):
    """The month as recorded (every day elapsed, a month-end reconciliation): each balance out is in + overtime - share,
    counted by hand: Amal 2.5 + 104 - 27.38 = 79.12, Badr -25.28, Dana -23.96, Emad -27.38."""
    req = shared(sample) | {"replan_from": "2026-07-01"}
    req["published"] = amal_works_every_owed_day(req)

    result = wait_for(client, client.post("/solve", json=req).json()["job_id"], ended)["result"]

    assert result["ok"] is True and "overtime_above_share" in result["penalties"]
    assert {i: n["carry_over"]["overtime_balance"] for i, n in result["nurses"].items()} == {
        "Amal": 79.12, "Badr": -25.28, "Dana": -23.96, "Emad": -27.38}


def test_validate_prices_overtime_above_a_share_in_the_solvers_words(client, sample):
    """Amal's share of the unit's 104 h is 27.38 h (`amal_works_every_owed_day`), her target 27.38 - 2.5 = 24.88 h, and
    104 - 24.88 - 12 = 67.12 h above it: 67 priced, at 1."""
    req = shared(sample)
    roster = amal_works_every_owed_day(req)

    v = client.post("/validate", json={"solve_input": req, "roster": roster}).json()

    rule = next(r for r in v["rules"] if r["id"] == "overtime_above_share")
    assert [(m["nurse"], m["date"], m["amount"], m["text"]) for m in rule["matches"]] == [
        ("Amal", None, 67, "Amal: 104 h overtime, share 27.4 h, target 24.9 h after the carried balance, 67 h priced")]
    assert rule["level"] == "soft" and rule["weight"] == 1 and rule["weighted"] == 67
    assert v["penalties"]["overtime_above_share"] == 67 and v["nurses"]["Amal"] >= 67


# ---- probation ----------------------------------------------------------------------------------
def with_faten(sample: dict, preceptor: str | None = "Amal") -> dict:
    """The sample with a fifth nurse, Faten, on probation and precepted by Amal (owes 208 h in June, as Amal does). A copy:
    the file is also the backend's and the browser checks' sample of a unit's nurses, which carry no probation. The four counted nurses are the sample's, so the month solves as it did."""
    out = copy.deepcopy(sample)
    faten = {k: out["nurses"][0][k] for k in ("score", "ramadan_reduced", "ytd_overtime", "hours_balance",
                                               "last_shift_kind", "rest_owed")}
    faten.update(id="Faten", gender="F", team_leader=True, breastfeeding=False, night_balance=1.5, friday_balance=-1.0,
                 overtime_balance=3.0, on_probation=True, preceptor=preceptor)
    out["nurses"].append(faten)
    return out


def test_the_sample_carries_a_nurse_on_probation_and_her_result_says_she_counted_toward_nothing(client, sample):
    """D needs exactly one nurse a day: Faten on D beside a counted nurse does not exceed it. She works no night, is on
    Amal's shift or off every day Amal works D, under 220 h (208 + 12), and her three balances go out at 0."""
    result = wait_for(client, client.post("/solve", json=with_faten(sample)).json()["job_id"], ended)["result"]

    assert result["ok"] is True and result["violations"] == []
    roster, dates = result["roster"], sorted(result["roster"]["Faten"])
    assert "N" not in roster["Faten"].values()
    assert all(roster["Faten"][d] in ("", "D") for d in dates if roster["Amal"][d] == "D")
    counted = ("Amal", "Badr", "Dana", "Emad")
    assert all(sum(roster[n][d] == "D" for n in counted) == 1 for d in dates)
    assert any(roster["Faten"][d] == "D" for d in dates)                     # beside the one counted nurse
    faten = result["nurses"]["Faten"]
    assert faten["required"] == 208 and 197 <= faten["hours_worked"] <= 219
    assert {k: faten["carry_over"][k] for k in ("night_balance", "friday_balance", "overtime_balance")} == {
        "night_balance": 0.0, "friday_balance": 0.0, "overtime_balance": 0.0}


def test_validate_names_the_nurse_the_day_her_preceptors_shift_and_hers(client, sample):
    """Amal works D on 1 and 2 June, nobody else works: Faten on D on 1 June follows; off on 2 June she leaves a followable
    day off, so her D on 3 June, when Amal is off, breaks the rule."""
    req = with_faten(sample)
    roster = {n["id"]: {f"2026-06-{d:02d}": "" for d in range(1, 31)} for n in req["nurses"]}
    roster["Amal"].update({"2026-06-01": "D", "2026-06-02": "D"})
    roster["Faten"].update({"2026-06-01": "D", "2026-06-03": "D"})

    v = client.post("/validate", json={"solve_input": req, "roster": roster}).json()

    assert [x for x in v["violations"] if x["rule"] == "following"] == [
        {"rule": "following", "nurse": "Faten", "date": "2026-06-03",
         "text": "following: Faten works D on 2026-06-03, when her preceptor Amal is off, while she is off on 2026-06-02, when "
                 "Amal works D"}]
    rule = next(r for r in v["rules"] if r["id"] == "following")
    assert rule["level"] == "hard" and [m["nurse"] for m in rule["matches"]] == ["Faten"]


def test_precheck_names_a_nurse_on_probation_with_no_preceptor(client, sample):
    issues = client.post("/precheck", json=with_faten(sample, None)).json()["issues"]

    assert issues == ["Faten: on probation with no preceptor, so she follows no one: she is rostered on freely placed day "
                      "shifts, counted toward nothing"]


def test_a_preceptor_not_in_the_input_is_refused_with_its_path(client, sample):
    r = client.post("/solve", json=with_faten(sample, "Zaid"))

    assert r.status_code == 422
    assert r.json()["detail"] == "request: nurses[4].preceptor: 'Zaid' is not another nurse of the input"
