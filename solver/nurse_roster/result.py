"""The result: what the solver returns for a request, as one JSON object.

    {"version": 1, "month": "2026-03", "ok": true, "status": "FEASIBLE", "message": "",
     "fingerprint": "<sha256 of the problem the roster was scored against>",
     "precheck": ["..."],                                     # plain-language feasibility issues
     "roster": {"N01": {"2026-03-01": "D", "2026-03-02": "", ...}, ...},   # "" = off or leave
     "violations": [{"rule": "headcount", "nurse": null, "date": "2026-03-02", "text": "...",
                     "shift": "D", "present": 0, "minimum": 1, "maximum": 2}],
     "elapsed_violations": [...],                             # hard rules broken wholly before
                                                              # replan_from: facts, not errors
     "cost": 23, "penalties": {"night_rest": 0, ...}, "department_cost": 9,
     "solver": {"objective": 23.0, "best_bound": 18.0, "solve_seconds": 20.1, "build_seconds": 0.07,
                "deterministic_time": 11.3, "improvements": 7, "workers": 8, "reproducible": false,
                "time_limit_seconds": 20, "stopped_by": "time_limit", "deterministic": false,
                "model": {"variables": 5169, "constraints": 10218, "objective_terms": 300}},
     "replan": {"from": "2026-03-12", "recorded_cells_differ": 2},        # null when absent
     "nurses": {"N01": {"leave": ["2026-03-04"], "contract": 200, "leave_credit": 8,
                        "required": 192, "hours_worked": 192, "overtime": 0, "shortfall": 0,
                        "shifts": {"D": 9, "N": 7}, "cost": 3,
                        "carry_over": {"hours_balance": 0, "night_balance": 0.33,
                                       "friday_balance": -0.2, "ytd_overtime": 100,
                                       "previous": ["N", "N", "", "", "D", "D", "D", ""],
                                       "last_shift_kind": "day", "rest_owed": 0,
                                       "preferences_not_kept": 1, "overtime_balance": 0.0}}}}

`carry_over` uses the request's own nurse field names, so next month's request is this
month's static nurse fields plus this block (the app resets `ytd_overtime` and the balances
at year start). `previous` is this month's row; `last_shift_kind` and `rest_owed` carry the
two memories the rules keep across any number of days (the kind of the last worked shift,
the rest still owed to the last night) walked up to where `previous` begins, so the history
never has to reach back for them and stays bounded. A debt the month's recorded days could
not repay is carried whole in `hours_balance`. `preferences_not_kept` counts this month's days she
asked off and works plus those she asked to work and is off, leave days left out: one month's
memory, which doubles next month's price of her preferences when above 0.
A violation states beside its words what a reader would otherwise parse out of them, each key only where the rule
has the fact: `shift` on the rules of a shift-day (headcount, team leader, both genders, skill need); `present`,
`minimum` and `maximum` on a headcount; and on a skill need its `skill`, `present` (the nurses on its shift holding the
skill) and `minimum`.
Without a roster (INFEASIBLE, UNKNOWN, MODEL_INVALID, or hard violations the validator
found) `ok` is false; `roster`, `nurses` and `cost` are null when there is no roster at all.
`ok` is true exactly when the CLI exits 0 (see `verdict`).
"""
from __future__ import annotations

import json
import math
import os
from typing import IO

from . import fairness as F
from .calendar import MonthInfo
from .explain import Analysis, explain
from .model import RosterModel, Solution, fingerprint
from .precheck import needed_history
from .problem import MAX_HISTORY, Problem
from .request import Request
from .rules import HARD, Match, level
from .validate import last_worked, preferences_not_kept, rest_owed_after

RESULT_VERSION = 1


def verdict(sol: Solution) -> tuple[int, str]:
    """Exit code and reason: 0 a roster that satisfies every hard rule, 1 none found, 2 the
    validator found hard violations, 3 the model and the validator disagree on the cost (on
    the returned roster, or on an incumbent validated below the proven optimum: `mismatch`).
    The solver's penalty variables are bounded from below, so its objective can only overstate
    the roster's validated cost (slack, squeezed out at OPTIMAL): a solver objective below the
    cost, at any status, means the model and the validator disagree, as does any gap at OPTIMAL."""
    if not sol.assignment:
        return 1, sol.message or sol.status
    broken = f" (and the roster breaks {len(sol.hard_errors)} hard rule(s))" if sol.hard_errors else ""
    if sol.mismatch:                                          # evidence from the search itself
        return 3, sol.mismatch + broken
    gap = sol.solver_objective - sol.objective                # a disagreement outranks hard errors
    if gap <= -0.5 or (sol.status == "OPTIMAL" and gap >= 0.5):
        return 3, ("the solver's objective " + ("differs from" if gap >= 0.5 else "is below")
                   + " the roster's cost; model and validator disagree" + broken)
    if sol.hard_errors:
        return 2, broken[6:-1]                                # "the roster breaks N hard rule(s)"
    return 0, ""


def stated(m: Match, month: MonthInfo) -> dict:
    """A match as it leaves the solver: who, the date, the words, and the facts the rule states beside them. The one
    rendering a result's violations and a no-search check of a grid share, so the two cannot differ in shape."""
    return {"nurse": m.nurse, "date": month.iso(m.day) if m.day is not None and 0 <= m.day < month.days else None,
            "text": m.text, **m.facts()}


def build_result(req: Request, rm: RosterModel | None, sol: Solution, *, precheck: list[str],
                 workers: int) -> dict:
    """The result is serialized from what the search ran on: the problem snapshot the roster
    was scored against (`sol.problem`: the roster, the hours, the cutoff and its recorded
    differences) and the limits the search ran with (`sol.time_limit`, `sol.workers`,
    `sol.deterministic_budget`), never from the caller's request, which an app may have
    edited while the solver ran (the request is frozen, its problem is not). The request
    must agree with them: a problem that does not match the model's fingerprint, a month of
    another shape, settings the search did not run with or a cutoff the snapshot does not
    carry are refused."""
    month, s = req.month, req.solver
    p = sol.problem if sol.problem is not None else req.problem
    if rm is not None and fingerprint(p) != rm.fingerprint:
        raise ValueError("result: the problem differs from the one the model was built from "
                         "(edited after build_model?); build the model and solve again")
    if p.month is None:
        raise ValueError("result: the solved problem carries no month (not built by the request loader?)")
    if p.month != month.label:                                # same shape is not the same month
        raise ValueError(f"result: the request's month {month.label} is not the solved problem's {p.month}")
    if (month.days, month.first_weekday) != (p.days, p.first_weekday):
        raise ValueError("result: the request's month does not fit the solved problem")
    y, mo = (int(x) for x in p.month.split("-"))              # the reporting calendar is the snapshot's
    month = MonthInfo(y, mo, p.days, p.first_weekday, list(p.ramadan_days))   # (checked by Problem.check)
    if sol.workers == 0:
        raise ValueError("result: the solution carries no solver settings (not from model.solve?)")
    ran = dict(time_limit=sol.time_limit, workers=sol.workers, seed=sol.seed)
    if sol.deterministic_budget is not None:
        ran["deterministic_time"] = sol.deterministic_budget
    if ran != s.solve_kwargs(workers):
        raise ValueError("result: the request's solver settings differ from the ones the search ran with")
    cut = p.planning_from
    if req.replan_from is not None and req.replan_from != cut:
        raise ValueError("result: the request's cutoff differs from the solved problem's")
    reproducible = sol.deterministic_budget is not None       # what ran: one worker, a budget
    rc, reason = verdict(sol)
    out = {
        "version": RESULT_VERSION, "month": p.month, "ok": rc == 0, "status": sol.status,
        "message": sol.message or reason, "fingerprint": rm.fingerprint if rm else "",
        "precheck": list(precheck), "roster": None, "violations": [], "elapsed_violations": [],
        "cost": None, "penalties": {}, "department_cost": None,
        "solver": {
            "objective": _num(sol.solver_objective), "best_bound": _num(sol.best_bound),
            "solve_seconds": round(sol.solve_seconds, 3),
            "build_seconds": round(rm.build_seconds, 3) if rm else None,
            "deterministic_time": _num(sol.deterministic_time), "improvements": sol.improvements,
            "workers": sol.workers, "reproducible": reproducible,
            "time_limit_seconds": sol.deterministic_budget if reproducible else sol.time_limit,
            "stopped_by": sol.stopped_by,
            # reproducible only when a proof or the deterministic budget ended the search; a
            # wall-clock stop or a cancellation is a function of the machine, not of the input
            "deterministic": reproducible and sol.stopped_by in ("proof", "deterministic_budget"),
            "model": dict(rm.stats) if rm else None},
        "replan": ({"from": month.cutoff_iso(cut), "recorded_cells_differ": p.recorded_differences()}
                   if req.replan_from is not None or cut > 0 else None),   # the snapshot's cutoff, always
        "nurses": None,
    }
    if not sol.assignment:
        return out
    g, dep, an = p.general, p.department, sol.analysis
    sh = dep.shifts
    out["roster"] = {n.id: {month.iso(d): (sh[v] if v >= 0 else "") for d, v in enumerate(row)}
                     for n, row in zip(p.nurses, sol.assignment)}
    for rid, r in an.rules.items():
        if level(p, rid) != HARD:
            continue
        for m in r.matches:
            out["elapsed_violations" if m.elapsed else "violations"].append({"rule": rid, **stated(m, month)})
    out["cost"], out["penalties"] = an.total, dict(sol.penalty_breakdown)
    out["department_cost"] = an.nurses.get("", 0)
    out["nurses"] = nurse_accounting(p, sol.assignment, an)
    return out


def nurse_accounting(p: Problem, assignment: list[list[int]], analysis: Analysis | None = None) -> dict:
    """Each nurse's hours accounting and carry-over (a result's `nurses` block) for a complete grid of `p`: one shift index or -1
    a nurse and day. The one place it is worked out, so that a result and a month with every day recorded, scored with no search
    (`solver_service.grid.nurses_of`), cannot disagree. `analysis` is `explain(p, assignment)`, worked out when not given."""
    if p.month is None:
        raise ValueError("nurse_accounting: the problem carries no month (not built by the request loader?)")
    y, mo = (int(x) for x in p.month.split("-"))
    month = MonthInfo(y, mo, p.days, p.first_weekday, list(p.ramadan_days))
    an = analysis if analysis is not None else explain(p, assignment)
    g, dep = p.general, p.department
    sh = dep.shifts
    share, per = F.share_m(p), F.friday_slots_m(p)
    need = needed_history(dep, g)
    hours = [sum(g.shift_hours[sh[v]] for v in row if v >= 0) for row in assignment]
    overtime = [max(0, h - p.required_hours(n)) if g.hours_rule_enabled else 0 for n, h in zip(p.nurses, hours)]
    unit_ot, owed = F.unit_overtime(p, overtime), F.overtime_owed(p)   # her own overtime is outside the unit's
    nurses = {}
    for n, row, hrs, ot in zip(p.nurses, assignment, hours, overtime):
        required = p.required_hours(n)
        shortfall = max(0, required - hrs) if g.hours_rule_enabled else 0
        night_out, friday_out = F.balances_out(p, n, row, share=share, per=per)
        names = [sh[v] if v >= 0 else "" for v in row]
        tail, before = _history_out(list(n.previous), names, need)
        nurses[n.id] = {
            "leave": [month.iso(d) for d in sorted(set(n.unavailable))],
            "contract": p.contract_hours(n), "leave_credit": p.leave_credit(n), "required": required,
            "hours_worked": hrs, "overtime": ot, "shortfall": shortfall,
            "shifts": {s: sum(1 for v in row if v == i) for i, s in enumerate(sh)},
            "cost": an.nurses.get(n.id, 0),
            "carry_over": {"hours_balance": p.carry_over(n, hrs), "night_balance": night_out,
                           "friday_balance": friday_out, "ytd_overtime": n.ytd_overtime + ot,
                           "previous": tail,
                           # the two memories, walked up to where next month's history begins
                           "last_shift_kind": _kind_out(before, n, dep.night_shift),
                           "rest_owed": rest_owed_after(n.rest_owed, before, dep.night_shift,
                                                        dep.day_shift, dep.night_rest_days),
                           # one month's memory: this month's days of hers not kept
                           "preferences_not_kept": preferences_not_kept(p, n, row),
                           # in + overtime - share, while the unit's switch and the hours rule are on
                           "overtime_balance": F.overtime_balance_out(p, n, ot, unit_ot, owed)}}
    return nurses


def _kind_out(before: list[str], nurse, night: str) -> str | None:
    """Next month's `last_shift_kind`: the kind of the last shift worked in the history left
    behind, else the incoming kind."""
    s = last_worked(before)
    return nurse.last_shift_kind if s is None else ("night" if s == night else "day")


def _history_out(previous: list[str], month: list[str], need: int) -> tuple[list[str], list[str]]:
    """Next month's `previous`, and the older part of the history it leaves behind: this
    month's row (more, when the pattern rules look back further than a month), capped at
    MAX_HISTORY. The memories the rules keep across any number of days (`last_shift_kind`,
    `rest_owed`) are carried as such, so the history never has to reach back for them."""
    seq = previous + month
    keep = min(max(len(month), need), len(seq), MAX_HISTORY)
    return seq[len(seq) - keep:], seq[:len(seq) - keep]


def write_result(result: dict, out: str | os.PathLike | IO[str]) -> None:
    """`out`: a file path, or an open text stream."""
    if isinstance(out, (str, os.PathLike)):
        with open(out, "w") as f:
            json.dump(result, f, indent=1)
    else:
        json.dump(result, out, indent=1)


def _num(v: float) -> float | None:
    return None if v is None or (isinstance(v, float) and not math.isfinite(v)) else v
