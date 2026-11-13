"""Timefold-style helpers for constraint tests: the amount one rule charges a roster, the
hard violations it reports, and a weight set with a single rule on."""
from __future__ import annotations

from nurse_roster.explain import explain
from nurse_roster.problem import Problem, Weights
from nurse_roster.rules import HARD, RULES, level


def only(rid: str, w: int) -> Weights:
    """Every weight 0 but the rule's own (`RULES[rid].weight`, the rule's id but for skill_need)."""
    return Weights(**{**{f: 0 for f in Weights.__slots__}, RULES[rid].weight: w})


def penalises(p: Problem, a: list[list[int]], rid: str) -> int:
    """Weighted penalty of one soft rule (0 for a hard rule or an inactive one)."""
    return explain(p, a).rules[rid].weighted


def violates(p: Problem, a: list[list[int]], rid: str) -> list[str]:
    """The hard violations one rule reports (empty when the rule is soft or inactive)."""
    r = explain(p, a).rules[rid]
    return [m.text for m in r.matches] if level(p, rid) == HARD else []
