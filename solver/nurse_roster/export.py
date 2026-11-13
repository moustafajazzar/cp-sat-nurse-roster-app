"""CSV export of a roster: one row per nurse with the month grid and the hours / balance
summary, then headcount, team-leader and average-score rows per anchor shift. Leave days print
as "L" (RESERVED_SHIFT), which is why no shift may be named that."""
from __future__ import annotations

import csv
import os
from typing import IO

from . import fairness as F
from .problem import Problem
from .validate import matrix_errors

WD = "Mon Tue Wed Thu Fri Sat Sun".split()


def write_csv(p: Problem, a: list[list[int]], out: str | os.PathLike | IO[str]) -> None:
    """`out`: a file path, or an open text stream (e.g. io.StringIO for the app)."""
    bad = matrix_errors(p, a)
    if bad:
        raise ValueError(bad[0])
    if not isinstance(out, (str, os.PathLike)):
        _write_rows(p, a, out)
        return
    with open(out, "w", newline="") as f:
        _write_rows(p, a, f)


_FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(v):
    """Spreadsheets evaluate cells starting with = + - @ as formulas; prefix such free-text
    cells (nurse ids come from the user) with an apostrophe so they stay text."""
    if isinstance(v, str) and v.startswith(_FORMULA_PREFIX):
        return "'" + v
    return v


class _SafeWriter:
    def __init__(self, f: IO[str]):
        self._w = csv.writer(f)

    def writerow(self, row) -> None:
        self._w.writerow([csv_safe(v) for v in row])


def _write_rows(p: Problem, a: list[list[int]], f: IO[str]) -> None:
    g, dep = p.general, p.department
    sh = dep.shifts
    P = p.history_len()
    w = _SafeWriter(f)
    w.writerow(["nurse", "TL", "sex", "score"] + [f"prev{i - P}" for i in range(P)]
               + [f"{d+1}{WD[p.weekday(d)][:2]}" for d in range(p.days)]
               + ["hours", "leave_credit", "contract", "balance_in", "overtime", "carry_over",
                  "night_bal_in", "night_bal_out", "fri_bal_in", "fri_bal_out"]
               + [f"#{s}" for s in sh])
    share, per = F.share_m(p), F.friday_slots_m(p)              # once per roster, not per cell
    for n, row in zip(p.nurses, a):
        hrs = sum(g.shift_hours[sh[s]] for s in row if s >= 0)
        night_out, friday_out = F.balances_out(p, n, row, share=share, per=per)
        credit, contract = p.leave_credit(n), p.contract_hours(n)
        req = p.required_hours(n)
        w.writerow([n.id, ("TL " if n.team_leader else "") + ("BF" if n.breastfeeding else ""), n.gender, n.score]
                   + [""] * (P - len(n.previous)) + [s.lower() for s in n.previous]
                   + [sh[s] if s >= 0 else ("L" if d in n.unavailable else "") for d, s in enumerate(row)]
                   + [hrs, credit, contract, n.hours_balance]
                   + [max(0, hrs - req) if g.hours_rule_enabled else 0, p.carry_over(n, hrs)]
                   + [n.night_balance, night_out, n.friday_balance, friday_out]
                   + [sum(1 for s in row if s == i) for i in range(len(sh))])
    w.writerow([])
    for hc in dep.headcounts:
        ai = sh.index(hc.shift)
        extra = {sh.index(s) for s in hc.also_counts}
        hcount, tls, avg = [], [], []
        for d in range(p.days):
            pure = [n for n in range(len(a)) if a[n][d] == ai]
            hcount.append(len(pure) + sum(1 for n in range(len(a)) if a[n][d] in extra))
            tls.append(sum(1 for n in pure if p.nurses[n].team_leader))
            avg.append(f"{sum(p.nurses[n].score for n in pure)/len(pure):.2f}" if pure else "")
        pad = [""] * P
        w.writerow([f"{hc.shift} headcount", "", "", ""] + pad + hcount)
        w.writerow([f"{hc.shift} team leaders", "", "", ""] + pad + tls)
        w.writerow([f"{hc.shift} avg score", "", "", ""] + pad + avg)
