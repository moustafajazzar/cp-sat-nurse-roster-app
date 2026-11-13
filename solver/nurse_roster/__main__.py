"""CLI.  Example:
  uv run python -m nurse_roster solve request.json --out result.json [--csv roster.csv]

The request (see request.py) is the only input: one unit, one month, every rule explicit,
dates not indices. The result (see result.py) is written as JSON; the CSV is an optional
export of the same roster. The worker count is a server setting (--workers, or the
NURSE_ROSTER_WORKERS environment variable), never part of the request.
"""
from __future__ import annotations

import argparse
import os
import sys

from .explain import diff, explain, report
from .export import write_csv
from .model import MAX_WORKERS, build_model, solve
from .precheck import precheck
from .request import Request, read_request
from .result import build_result, verdict, write_result

EXIT_INPUT = 4           # exit codes: 0 roster, 1 none found, 2 hard violations, 3 model/validator
                         # mismatch, 4 malformed input or arguments (argparse itself exits 2)
DEFAULT_WORKERS = 8


def run(req: Request, workers: int, log: bool = False, out: str | None = None,
        csv_out: str | None = None, explain_text: bool = False, explain_json: str | None = None,
        progress: bool = False) -> int:
    """Solve the request, print the usual report, write the result (and the CSV) when asked.
    The result file is written whenever a solve ran (exit 0..3): its `ok` and `message` say
    what happened; the CSV only when there is a roster."""
    p = req.problem
    issues = precheck(p)
    for issue in issues:
        print("precheck:", issue)
    if req.replan_from is not None:
        print(f"re-planning from {req.month.cutoff_iso(req.replan_from)}: {req.replan_from} day(s) pinned "
              f"to the published roster"
              + (f", {req.differ} recorded cell(s) differ from it" if req.differ else ""))
    rm = build_model(p)
    print(f"build   : {rm.build_seconds*1000:7.1f} ms   vars={rm.stats['variables']:,} "
          f"constraints={rm.stats['constraints']:,} objective_terms={rm.stats['objective_terms']:,}")
    show = (lambda s: print(f"  t={s.solve_seconds:6.2f}s  cost={s.objective:.0f}  "
                            f"(solver {s.solver_objective:.0f}, bound {s.best_bound:.0f})")) if progress else None
    sol = solve(rm, p, log=log, on_solution=show, **req.solver.solve_kwargs(workers))
    p = sol.problem if sol.problem is not None else p         # the snapshot the roster was scored
    print(f"solve   : {sol.solve_seconds:7.2f} s    status={sol.status}"   # against: every line below,
          + (f"  ({sol.improvements} improving rosters)" if progress else ""))   # the CSV included,
                                                              # reads it, never the caller's problem
    rc, reason = verdict(sol)
    result = build_result(req, rm, sol, precheck=issues, workers=workers)
    if out:
        write_result(result, out)
    if not sol.assignment:
        print("no solution found:", reason)
        if out:
            print("wrote", out)
        return rc
    print(f"cost={sol.objective:.0f}  (solver objective {sol.solver_objective:.0f}, bound {sol.best_bound:.0f})")
    print("penalties:", sol.penalty_breakdown)
    short = sol.analysis.rules["avoidable_shortfall"].matches if "avoidable_shortfall" in sol.analysis.rules else []
    if short:                                                 # a weight, however large, is no guarantee
        print(f"avoidable shortfall: {len(short)} nurse(s) end a shift or more short; "
              + ("the search was not proven optimal, a longer time limit may remove it"
                 if sol.status != "OPTIMAL" else
                 "optimal for the weighted objective as a whole: at their weights the other rules and "
                 "preferences cost more than this shortfall, or a hard rule allows no more (the "
                 "solver does not say which)"))
        for m in short[:20]:
            print("  ", m.text)
    if sol.analysis.elapsed:                                  # before the cutoff, or beyond what
        where = (f"recorded before {req.month.cutoff_iso(p.planning_from)}" if p.planning_from
                 else "beyond what the free days can change")   # the free days can reach (hours)
        print(f"{where} (facts, not errors): {len(sol.analysis.elapsed)} rule violation(s)")
        for e in sol.analysis.elapsed[:20]:
            print("  ", e)
    if explain_text:
        print(report(sol.analysis))
        if p.published is not None:
            before = explain(p, p.published_matrix())
            d = diff(before, sol.analysis)
            moved = sum(x != y for pr, nr in zip(p.published_matrix(), sol.assignment)   # every cell
                        for x, y in zip(pr, nr))              # that differs, pinned or not, any weight
            print(f"  vs published: cost {before.total} -> {sol.analysis.total} "
                  f"({d['total']['']:+d}), {moved} cell(s) changed; per rule {d['rules']}")
    if explain_json:
        sol.analysis.to_json(explain_json)
        print("wrote", explain_json)
    if sol.hard_errors and rc in (2, 3):
        print(f"HARD VIOLATIONS: {len(sol.hard_errors)}")
        for e in sol.hard_errors[:20]:
            print("  ", e)
    if rc == 3:
        print("ERROR:", reason)
    elif rc != 2:
        gap = sol.solver_objective - sol.objective
        print("validator: all hard rules satisfied;",
              "cost matches the solver objective" if gap < 0.5 else
              f"solver objective carries {gap:.0f} of slack (not proven optimal); the cost above is the roster's")
    if out:
        print("wrote", out)
    if csv_out:
        write_csv(p, sol.assignment, csv_out)
        print("wrote", csv_out)
    return rc


def _bounded_int(lo: int, hi: int):
    def conv(v: str) -> int:
        try:
            x = int(v)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not an integer: {v!r}")
        if not lo <= x <= hi:
            raise argparse.ArgumentTypeError(f"must be within {lo}..{hi}")
        return x
    return conv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nurse_roster")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("solve", help="solve one request (one unit, one month)")
    s.add_argument("request", help="the request JSON (see request.py)")
    s.add_argument("--out", metavar="JSON", help="write the result JSON here (see result.py)")
    s.add_argument("--csv", metavar="CSV", help="also export the roster as a CSV")
    s.add_argument("--workers", type=_bounded_int(1, MAX_WORKERS),
                   default=os.environ.get("NURSE_ROSTER_WORKERS", str(DEFAULT_WORKERS)),
                   help=f"solver threads (default: $NURSE_ROSTER_WORKERS or {DEFAULT_WORKERS}); "
                        f"a reproducible request always uses one")
    s.add_argument("--log", action="store_true", help="CP-SAT search log")
    s.add_argument("--explain", action="store_true",
                   help="print each soft rule's largest matches and the cost per nurse")
    s.add_argument("--explain-json", metavar="PATH", help="write the full analysis as JSON")
    s.add_argument("--progress", action="store_true", help="print each improving roster's cost")
    a = ap.parse_args(argv)
    try:
        return _main(a)
    except (ValueError, OSError) as e:                        # malformed input: its own exit code,
        print("error:", e, file=sys.stderr)                   # 2 is a roster that breaks hard rules;
        return EXIT_INPUT                                     # a programming error keeps its traceback


def _main(a: argparse.Namespace) -> int:
    req = read_request(a.request)
    return run(req, a.workers, a.log, a.out, a.csv, a.explain, a.explain_json, a.progress)


if __name__ == "__main__":
    sys.exit(main())
