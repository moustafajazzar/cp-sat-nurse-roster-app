"""Score a given roster against a solve input, and work out each nurse's hours and carry-over on it, with no search: the
application calls these in its own processes, which need neither the solver container nor its one job."""
from __future__ import annotations

from nurse_roster.explain import explain
from nurse_roster.request import load_request
from nurse_roster.result import nurse_accounting, stated
from nurse_roster.rules import HARD, soft_ids


def _parse_roster(solve_input: object, roster: object) -> list[list[int]]:
    """The roster as the package's assignment matrix. A roster has the shape of the solve
    input's `published` block, so the package's own loader checks it: every nurse, every date,
    only known shifts. This parse is thrown away after that: a solve input's real `published`
    block prices the `change` rule, and must stay what it is."""
    if not isinstance(roster, dict):
        raise ValueError("roster: must be an object {nurse id: {date: shift or \"\"}}")
    try:
        parsed = load_request({**solve_input, "published": roster, "replan_from": None})
    except ValueError as e:
        raise ValueError(str(e).replace("request: published", "roster")) from None
    return parsed.problem.published_matrix()


def validate_grid(solve_input: object, roster: object) -> dict:
    """Hard violations, cost, penalties and the per-rule explanation. The violation, cost and
    penalty fields read exactly as the same fields of a result (see nurse_roster/result.py)."""
    req = load_request(solve_input)
    matrix = _parse_roster(solve_input, roster)
    analysis = explain(req.problem, matrix)
    month = req.month

    violations, elapsed_violations, rules = [], [], []
    for rule in analysis.rules.values():
        rules.append({
            "id": rule.id, "level": rule.level, "weight": rule.weight, "unit": rule.unit,
            "raw": rule.raw, "weighted": rule.weighted,
            "matches": [stated(m, month) | {"amount": m.amount, "elapsed": m.elapsed} for m in rule.matches],
        })
        if rule.level == HARD:
            for m in rule.matches:
                (elapsed_violations if m.elapsed else violations).append({"rule": rule.id, **stated(m, month)})
    return {
        "ok": not violations,
        "violations": violations,
        "elapsed_violations": elapsed_violations,
        "cost": analysis.total,
        "penalties": {rid: analysis.rules[rid].weighted for rid in soft_ids()},
        "department_cost": analysis.nurses.get("", 0),
        "nurses": {nid: cost for nid, cost in analysis.nurses.items() if nid},
        "rules": rules,
    }


def nurses_of(solve_input: object, roster: object) -> dict:
    """Each nurse's hours accounting and carry-over on this roster, exactly as a result's `nurses` block reads it
    (`nurse_roster.result.nurse_accounting`), so a solve's own block equals this on its roster."""
    req = load_request(solve_input)
    return nurse_accounting(req.problem, _parse_roster(solve_input, roster))


def month_facts(solve_input: object) -> dict:
    """The month as it happened, for a solve input whose `replan_from` is the first day after its month: every day a fact, a pin
    that counts first, then leave and the days outside a nurse's dates as off, then the roster in force (`published`). In the
    shape of a result's roster."""
    req = load_request(solve_input)
    p, month = req.problem, req.month
    if req.replan_from != month.days:
        raise ValueError("solve input: replan_from must be the first day after the month, so that every day is a fact")
    return {n.id: {month.iso(day): n.pinned[day] for day in range(month.days)} for n in p.nurses}
