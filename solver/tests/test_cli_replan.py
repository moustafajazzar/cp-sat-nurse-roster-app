"""Full CLI regressions for elapsed night penalties and cutoff-aware input loading."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from nurse_roster.problem import Nurse
from nurse_roster.request import load_request
from tests.rules_helper import only
from tests.test_core import mini
from tests.er_fixture import request_of, write_request

MONTH, DAYS = "2026-06", 30


def cli(*args):
    return subprocess.run(
        [sys.executable, "-B", "-m", "nurse_roster", *map(str, args)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30,
    )


def published_of(rows: list[list[str]], ids=("a", "b")) -> dict:
    return {nid: {f"{MONTH}-{d + 1:02d}": c for d, c in enumerate(row)} for nid, row in zip(ids, rows)}


@pytest.mark.parametrize("shifts,previous,elapsed", [
    pytest.param(["D", "N"], [], ["N", "N", "D"], id="short-gap"),
    pytest.param(["D", "N", "C"], [], ["N", "C", "N", "D"], id="long-gap"),
    pytest.param(["D", "N", "C"], ["N", "C", "C"], ["N", "D"], id="historical-obligation"),
    pytest.param(["D", "N", "C"], ["N", "C", "C", "N"], ["D"], id="month-boundary"),
])
@pytest.mark.parametrize("no_mixed_blocks", [True, False], ids=["forbidden", "permitted"])
def test_cli_elapsed_night_transition_has_matching_cost_and_exports(
        tmp_path, shifts, previous, elapsed, no_mixed_blocks):
    cut = len(elapsed)
    row = elapsed + [""] * (DAYS - cut)                       # on leave for the rest of the month
    p = mini(DAYS, shifts,
             [Nurse("a", previous=previous, pinned=dict(enumerate(elapsed)), unavailable=list(range(cut, DAYS)))],
             weights=only("night_rest", 7), night_rest_days=2, night_rest_hard=False,
             no_mixed_blocks=no_mixed_blocks)
    p.published = [row]
    source, exported, analysis = (tmp_path / name for name in ("r.json", "result.json", "analysis.json"))
    write_request(request_of(p, MONTH, time_limit=2, replan_from=cut), source)

    result = cli("solve", source, "--workers", 1, "--out", exported, "--explain-json", analysis)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = 0 if no_mixed_blocks else 7
    assert "status=OPTIMAL" in result.stdout
    assert f"cost={expected}  (solver objective {expected}, bound {expected})" in result.stdout
    assert "cost matches the solver objective" in result.stdout
    out = json.loads(exported.read_text())
    assert out["ok"] and list(out["roster"]["a"].values()) == row
    assert out["cost"] == expected and out["replan"] == {"from": f"{MONTH}-{cut + 1:02d}", "recorded_cells_differ": 0}
    assert bool(out["elapsed_violations"]) == no_mixed_blocks and out["violations"] == []
    report = json.loads(analysis.read_text())
    assert report["errors"] == []
    assert report["rules"]["night_rest"]["weighted"] == report["total"] == expected
    assert bool(report["elapsed"]) == no_mixed_blocks


def test_cli_applies_cutoff_and_baseline_before_checking_elapsed_eligibility(tmp_path):
    p = mini(DAYS, ["D", "N"], [Nurse("a", allowed_shifts=["D"], pinned={0: "N"}), Nurse("b")],
             weights=only("change", 3))
    p.published = [["D"] * DAYS, ["D"] + [""] * (DAYS - 1)]
    source, exported, analysis = (tmp_path / name for name in ("r.json", "result.json", "analysis.json"))
    write_request(request_of(p, MONTH, time_limit=2, replan_from=1), source)
    # Without a requested cutoff the same pin is rejected.
    with pytest.raises(ValueError, match="not in her allowed shifts"):
        load_request(request_of(p, MONTH, time_limit=2))

    result = cli("solve", source, "--workers", 1, "--out", exported, "--explain-json", analysis)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "status=OPTIMAL" in result.stdout
    assert "1 recorded cell(s) differ from it" in result.stdout
    assert "cost=0  (solver objective 0, bound 0)" in result.stdout
    out = json.loads(exported.read_text())
    assert list(out["roster"]["a"].values()) == ["N"] + ["D"] * (DAYS - 1)
    assert list(out["roster"]["b"].values()) == ["D"] + [""] * (DAYS - 1)
    assert [v["text"] for v in out["elapsed_violations"]] == ["shift restriction: a works N on 2026-06-01"]
    report = json.loads(analysis.read_text())
    assert report["errors"] == [] and report["total"] == 0
    assert report["elapsed"] == ["shift restriction: a works N on 2026-06-01"]


OK_ROW = [["D"] + [""] * (DAYS - 1)]


@pytest.mark.parametrize("pins,published,leave,cutoff,message", [
    ({"2026-06-01": "N", "2026-06-02": "N"}, OK_ROW, [], "2026-06-02", "not in her allowed shifts"),
    ({"2026-06-01": "X"}, OK_ROW, [], "2026-06-02", "pinned[2026-06-01]: 'X' is not a department shift"),
    ({"2026-07-01": "N"}, OK_ROW, [], "2026-06-02", "pinned: 2026-07-01 is outside 2026-06"),
    ([], OK_ROW, [], "2026-06-02", "pinned: must be an object"),
    ({"2026-06-01": "N"}, [["D", ""]], [], "2026-06-02", "published[a]: missing date 2026-06-03"),
    ({"2026-06-01": "N"}, [["D", "", "X"] + [""] * (DAYS - 3)], [], "2026-06-02", "published[a][2026-06-03]: 'X' is not a department shift"),
    ({"2026-06-01": "N"}, OK_ROW, ["2026-06-01"], "2026-06-02", "pinned[2026-06-01]: on leave but pinned to 'N'"),
    ({"2026-06-01": "N"}, None, [], "2026-06-02", "replan_from needs a published roster"),
    ({"2026-06-01": "N"}, OK_ROW, [], "2026-07-02", "replan_from: 2026-07-02 is outside 2026-06"),   # 07-01 = all elapsed
    ({"2026-06-01": "N"}, OK_ROW, [], "2026-06-01", "not in her allowed shifts"),
    ({"2026-06-01": "N"}, OK_ROW, [], None, "not in her allowed shifts"),
])
def test_cli_replan_rejects_invalid_input_without_exporting(
        tmp_path, pins, published, leave, cutoff, message):
    p = mini(DAYS, ["D", "N"], [Nurse("a", allowed_shifts=["D"])])
    req = request_of(p, MONTH, time_limit=2)
    req["nurses"][0].update(pinned=pins, leave=leave)
    if published is not None:
        req["published"] = published_of(published, ("a",))
    if cutoff is not None:
        req["replan_from"] = cutoff
    source, exported, csv_out = tmp_path / "r.json", tmp_path / "result.json", tmp_path / "roster.csv"
    write_request(req, source)
    result = cli("solve", source, "--out", exported, "--csv", csv_out, "--workers", 1)
    assert result.returncode == 4, result.stdout + result.stderr
    assert message in result.stderr and result.stderr.startswith("error: request: ")
    assert "Traceback" not in result.stderr
    assert not exported.exists() and not csv_out.exists()
