"""Score explanation: the validator's matches organised per rule and per nurse, and the
difference between two rosters. The per-rule weighted totals are exactly
`validate()`'s `penalty_breakdown`; `nurses` adds the indictment view (what each nurse
costs), with department-level matches (headcount scores, department overtime) under
the key ""."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import IO

from .problem import Problem
from .rules import HARD, RULES, Match, level, weight
from .validate import analyse


@dataclass(slots=True)
class RuleAnalysis:
    id: str
    level: str
    weight: int                 # per unit (0 for hard rules)
    unit: str
    raw: int                    # sum of match amounts, in the unit
    weighted: int               # raw × weight
    matches: list[Match] = field(default_factory=list)


@dataclass(slots=True)
class Analysis:
    total: int                                  # == validate()'s total
    rules: dict[str, RuleAnalysis]
    nurses: dict[str, int]                      # indictments: weighted penalty per nurse id
    errors: list[str]                           # hard matches (and matrix errors)
    elapsed: list[str] = field(default_factory=list)   # hard matches wholly before the planning
                                                       # cutoff: recorded facts, not errors

    def soft(self) -> list[RuleAnalysis]:
        return [r for r in self.rules.values() if r.level != HARD]

    def to_json(self, out: str | os.PathLike | IO[str]) -> None:
        data = asdict(self)
        if isinstance(out, (str, os.PathLike)):
            with open(out, "w") as f:
                json.dump(data, f, indent=1)
        else:
            json.dump(data, out, indent=1)


def explain(p: Problem, a: list[list[int]], *, checked: bool = False) -> Analysis:
    """`checked`: `p` is a snapshot `check()` already passed (the solver's), so the contract
    is not re-validated: on every incumbent that would run inside CP-SAT's callback."""
    bad, matches = analyse(p, a, checked=checked)
    rules: dict[str, RuleAnalysis] = {}
    nurses: dict[str, int] = {nu.id: 0 for nu in p.nurses}
    nurses[""] = 0
    errors, elapsed = list(bad), []
    for rid, ms in matches.items():
        lv, w = level(p, rid), weight(p, rid)
        raw = sum(m.amount for m in ms)
        rules[rid] = RuleAnalysis(rid, lv, w, RULES[rid].unit, raw, w * raw, list(ms))
        if lv == HARD:
            errors += [m.text for m in ms if not m.elapsed]
            elapsed += [m.text for m in ms if m.elapsed]
        else:
            for m in ms:
                nurses[m.nurse or ""] += w * m.amount
    return Analysis(sum(r.weighted for r in rules.values()), rules, nurses, errors, elapsed)


def diff(before: Analysis, after: Analysis) -> dict[str, dict[str, int]]:
    """Per-rule and per-nurse change in weighted penalty (after − before), non-zero only."""
    rules = {rid: after.rules[rid].weighted - before.rules[rid].weighted for rid in after.rules}
    ids = set(before.nurses) | set(after.nurses)
    nurses = {i: after.nurses.get(i, 0) - before.nurses.get(i, 0) for i in ids}
    return {"total": {"": after.total - before.total},
            "rules": {k: v for k, v in rules.items() if v},
            "nurses": {k: v for k, v in sorted(nurses.items()) if v}}


def report(an: Analysis, top: int = 5) -> str:
    """Human-readable explanation: every soft rule with its largest matches, then the
    nurses sorted by what they cost."""
    lines = [f"cost {an.total}"]
    for r in sorted(an.soft(), key=lambda r: -r.weighted):
        if r.weighted == 0:
            continue
        lines.append(f"  {r.id} {r.weighted} = {r.raw} x {r.weight}  ({r.unit})")
        for m in sorted(r.matches, key=lambda m: -m.amount)[:top]:
            lines.append(f"      {m.amount:>4}  {m.text}")
        if len(r.matches) > top:
            lines.append(f"      ... {len(r.matches) - top} more")
    worst = sorted(((v, k) for k, v in an.nurses.items() if v), reverse=True)
    if worst:
        lines.append("  per nurse: " + ", ".join(f"{k or 'dept'} {v}" for v, k in worst))
    if an.errors:
        lines.append(f"  hard violations: {len(an.errors)}")
    if an.elapsed:
        lines.append(f"  recorded before the planning cutoff (facts, not errors): {len(an.elapsed)}")
    return "\n".join(lines)
