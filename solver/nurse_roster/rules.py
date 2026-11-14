"""Constraint registry: the one list of rules the model, the validator, the explanation and
the tests all iterate, so a rule cannot exist in one and be missing from another.

A rule has a level (hard or soft; two of them switch with a department flag), a weight
field in `Weights` when soft, a unit for the explanation, and an `enabled` predicate on the
problem. `active(p, id)` is the single truth table for "does this rule add anything": the
model adds constraints or penalty terms exactly when it is true, the validator evaluates
the rule exactly when it is true.

Two hard rules have no entry because an assignment matrix cannot break them: one shift
per day (the matrix holds one shift index per cell) and the fixed history (checked by
`Problem.check()`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .problem import Problem, expired_required, preceptor_of

HARD, SOFT = "hard", "soft"


@dataclass(frozen=True, slots=True)
class Match:
    """One occurrence of a rule in a roster: who, when, how much, in words.
    `nurse` / `day` are None for department-level rules; `amount` is in the rule's unit."""
    nurse: str | None
    day: int | None
    amount: int
    text: str
    elapsed: bool = False       # lies wholly before Problem.planning_from: a recorded fact; for a
                                # hard rule it is reported apart from the errors, never enforced
    # What the words say, for a reader that should not parse them. Only the rules that have the fact state it.
    shift: str | None = None    # the shift of the shift-day, on a rule about one
    skill: str | None = None    # a skill need: the skill it needs
    present: int | None = None  # a headcount: the nurses counted on the shift-day, and the range they are outside;
    minimum: int | None = None  # a skill need: the nurses on the shift holding its skill, and the least it needs
    maximum: int | None = None

    def facts(self) -> dict:
        """The stated facts, by name."""
        named = {"shift": self.shift, "skill": self.skill, "present": self.present, "minimum": self.minimum,
                 "maximum": self.maximum}
        return {name: value for name, value in named.items() if value is not None}


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    level: str | Callable[[Problem], str]
    weight: str | None                     # `Weights` field, soft rules only (equals `id`, but for
                                           # skill_need, whose weight is `skill_shortfall`)
    unit: str
    enabled: Callable[[Problem], bool]


def _night(p: Problem) -> bool:
    dep = p.department
    return dep.night_shift in dep.shifts and dep.day_shift in dep.shifts


_RULES = [
    # hard
    Rule("unavailable", HARD, None, "shift on a leave day", lambda p: True),
    Rule("allowed_shifts", HARD, None, "shift outside the nurse's allowed set", lambda p: True),
    Rule("succession", HARD, None, "forbidden pair on consecutive days",
         lambda p: bool(p.successions())),
    Rule("hours_bounds", HARD, None, "hours above contract + cap (overtime)",
         lambda p: p.general.hours_rule_enabled),
    Rule("consecutive", HARD, None, "shift beyond the maximum run",
         lambda p: p.department.max_consecutive_shifts > 0),
    Rule("double_cycle", HARD, None, "worked day in the rest after a double cycle",
         lambda p: p.department.max_consecutive_shifts > 0 and p.department.double_cycle_rest > 1),
    Rule("headcount", HARD, None, "shift-day outside its headcount range",
         lambda p: p.department.headcount_enabled),
    Rule("team_leader", HARD, None, "shift-day without a team leader",
         lambda p: p.department.require_team_leader),
    Rule("gender", HARD, None, "shift-day without both genders",
         lambda p: p.department.require_both_genders),
    Rule("pin", HARD, None, "cell differing from its pin", lambda p: any(nu.pinned for nu in p.nurses)),
    Rule("skill_expired", HARD, None, "shift on a day a required skill has expired",
         lambda p: any(expired_required(p, nu) for nu in p.nurses)),
    Rule("following", HARD, None, "day a nurse on probation is not on her preceptor's day shift, or off it",
         lambda p: any(preceptor_of(p, nu) is not None for nu in p.nurses)),
    # hard or soft, per department flag
    Rule("night_rest", lambda p: HARD if p.department.night_rest_hard else SOFT, "night_rest",
         "D day with too few off days since the last N",
         lambda p: p.department.night_rest_days > 0 and _night(p)),
    Rule("shift_switch", lambda p: HARD if p.department.switches_hard else SOFT, "shift_switch",
         "night/day switch above the maximum",
         lambda p: p.department.night_shift in p.department.shifts),
    Rule("skill_need", lambda p: HARD if p.department.skill_needs_hard else SOFT, "skill_shortfall",
         "nurse short of a skill need on a shift-day", lambda p: bool(p.department.skill_needs)),
    # soft
    Rule("dept_overtime", SOFT, "dept_overtime", "hour over the department cap",
         lambda p: p.general.hours_rule_enabled),
    Rule("nurse_overtime", SOFT, "nurse_overtime", "hour of overtime",
         lambda p: p.general.hours_rule_enabled),
    Rule("nurse_shortfall", SOFT, "nurse_shortfall", "hour under contract (carried over)",
         lambda p: p.general.hours_rule_enabled),
    Rule("avoidable_shortfall", SOFT, "avoidable_shortfall",
         "hour short beyond a remainder below one shift and what her days cannot give",
         lambda p: p.general.hours_rule_enabled),
    Rule("fair_day", SOFT, "fair_day", "0.1 shift from the proportional day share",
         lambda p: any(s != p.department.night_shift for s in p.department.fair_shifts)),
    Rule("fair_night", SOFT, "fair_night", "0.1 shift from the proportional night share",
         lambda p: p.department.night_shift in p.department.fair_shifts),
    Rule("fair_weekend", SOFT, "fair_weekend", "0.1 shift from the fair Friday share",
         lambda p: p.department.fair_weekends),
    Rule("overtime_above_share", SOFT, "overtime_above_share", "hour of overtime above the share",
         lambda p: p.department.share_overtime and p.general.hours_rule_enabled),
    Rule("score_deficit", SOFT, "score_deficit", "0.01 score-point x person under the target",
         lambda p: p.department.score_target > 0 or p.department.score_target_weekend > 0),
    Rule("score_spread", SOFT, "score_spread", "0.01 score-point x person above the band "
         "(band mode) / 0.01 between the strongest and weakest shift (spread mode)",
         lambda p: p.department.score_balance),
    Rule("excess_weekend", SOFT, "excess_weekend", "weekend day above the maximum",
         lambda p: True),
    Rule("excess_full_run", SOFT, "excess_full_run", "full run above the maximum",
         lambda p: p.department.max_full_runs is not None and p.department.max_consecutive_shifts > 1),
    Rule("undesired_day", SOFT, "undesired_day", "requested-off day worked",
         lambda p: any(nu.undesired_days for nu in p.nurses)),
    Rule("desired_day", SOFT, "desired_day", "requested-work day not worked",
         lambda p: any(nu.desired_days for nu in p.nurses)),
    Rule("change", SOFT, "change", "unpinned cell differing from the published roster",
         lambda p: p.published is not None),
]
RULES: dict[str, Rule] = {r.id: r for r in _RULES}


def level(p: Problem, rid: str) -> str:
    lv = RULES[rid].level
    return lv if isinstance(lv, str) else lv(p)


def weight(p: Problem, rid: str) -> int:
    """Penalty per unit: 0 for a hard rule, a disabled rule, or a zero weight."""
    r = RULES[rid]
    if r.weight is None or level(p, rid) == HARD or not r.enabled(p):
        return 0
    return getattr(p.general.weights, r.weight)


def active(p: Problem, rid: str) -> bool:
    """Whether the rule adds anything: enabled, and hard or positively weighted."""
    return RULES[rid].enabled(p) and (level(p, rid) == HARD or weight(p, rid) > 0)


def soft_ids() -> list[str]:
    """Every rule that can carry a weight, one a `Weights` field (`RULES[id].weight`)."""
    return [r.id for r in _RULES if r.weight is not None]
