"""Two results of one unit's month side by side: what differs between their solve inputs, rule by rule what each
cost on its own input, and which of their entries the other shares. Both inputs are read under today's skill names and
shift codes, so a rename between the two solves is no change. Nothing here prices one roster on the other's input: the
solver's code refuses a roster whose nurses are not its input's."""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from nurse_roster.rules import SOFT
from solver_service.grid import validate_grid

from closes.chain import CARRIED
from hospital.rules import flattened
from rosters.drafts import current_codes
from rosters.versions import differences
from solves.input import in_current_skill_names
from solves.models import Solve
from units.rules import with_shift_codes

# The solver's own reading of a field a stored input has none of, so an input from before it is no change of its own.
_CARRIED_BEFORE = {"previous": [], "last_shift_kind": None, "rest_owed": 0, "preferences_not_kept": 0, "overtime_balance": 0.0}
_ATTRIBUTES = ("gender", "team_leader", "score", "ramadan_reduced", "breastfeeding", "allowed_shifts")
# Every shift-naming field of the department block, read as a block with none of them would be, so a version 1 input maps.
_SHIFT_FIELDS = {"shifts": [], "fair_shifts": [], "night_shift": "", "day_shift": "", "headcounts": [], "skill_needs": []}
CALENDAR = ("ramadan_days", "public_holidays")


@dataclass(frozen=True)
class InputChange:
    group: str
    field: str
    older: object
    newer: object


@dataclass(frozen=True)
class NurseChange:
    hospital_id: str
    kinds: list[str]


@dataclass(frozen=True)
class Side:
    """One result's figures for one soft rule. All None for the `change` rule of a solve that was no re-plan: it has no roster
    in force to stay close to, so it has no price."""
    amount: int | None
    weight: int | None
    penalty: int | None


@dataclass(frozen=True)
class RuleRow:
    rule: str
    older: Side
    newer: Side

    @property
    def weight_differs(self) -> bool:
        return None not in (self.older.weight, self.newer.weight) and self.older.weight != self.newer.weight


def _codes_named(solve_input: dict) -> set[str]:
    general, department = solve_input.get("general", {}), solve_input.get("department", {})
    codes = set(general.get("shift_hours", {})) | {code for pair in general.get("forbidden_successions", []) for code in pair}
    codes |= set(department.get("shifts", [])) | set(department.get("fair_shifts", []))
    codes |= {department.get("night_shift", ""), department.get("day_shift", "")}
    codes |= {code for row in department.get("headcounts", []) for code in (row["shift"], *row.get("also_counts", []))}
    codes |= {need["shift"] for need in department.get("skill_needs", [])}
    for row in solve_input.get("nurses", []):
        codes |= set(row.get("allowed_shifts") or []) | set(row.get("previous") or []) | set(row.get("pinned", {}).values())
    return codes - {""}


class _Reading:
    """A stored input as it reads today: its skills under today's names, its shifts under today's codes."""

    def __init__(self, solve: Solve, codes: dict[str, str | None]):
        self.input = in_current_skill_names(solve.input)
        self.code: Callable[[str], str] = lambda code: codes.get(code) or code
        self.codes = codes

    def hospital_rules(self) -> dict:
        general = dict(self.input["general"])
        if "shift_hours" in general:
            general["shift_hours"] = {self.code(code): hours for code, hours in general["shift_hours"].items()}
        if "forbidden_successions" in general:
            general["forbidden_successions"] = [[self.code(code) for code in pair] for pair in general["forbidden_successions"]]
        return flattened(general)

    def unit_rules(self) -> dict:
        block = {key: value for key, value in self.input["department"].items() if key != "name"}
        mapped = with_shift_codes(_SHIFT_FIELDS | block, self.codes)
        return {key: mapped[key] for key in block}

    def nurses(self) -> dict[str, dict]:
        return {row["id"]: row for row in self.input["nurses"]}

    def what_of(self, row: dict) -> dict[str, object]:
        """Her block, kind by kind, in the words a comparison names a change in."""
        allowed = row.get("allowed_shifts")
        return {
            "attributes": {name: row.get(name) for name in _ATTRIBUTES} | {
                "allowed_shifts": sorted(self.code(code) for code in allowed) if allowed is not None else None},
            "leave": sorted(row.get("leave", [])),
            "pins": {day: self.code(code) if code else code for day, code in row.get("pinned", {}).items()},
            "carry_over": {key: row.get(key, _CARRIED_BEFORE.get(key)) for key in CARRIED} | {
                "previous": [self.code(code) if code else code for code in row.get("previous") or []]},
            "preferences": (sorted(row.get("undesired_days", [])), sorted(row.get("desired_days", []))),
            "skills": row.get("skills", {}),
            # Absent in an input stored before probation was recorded: not on probation, no preceptor.
            "probation": (row.get("on_probation", False), row.get("preceptor")),
        }


def readings(older: Solve, newer: Solve) -> tuple[_Reading, _Reading]:
    codes: dict[str, str | None] = dict(current_codes(_codes_named(older.input) | _codes_named(newer.input)))
    return _Reading(older, codes), _Reading(newer, codes)


def _differing(group: str, old: dict, new: dict) -> list[InputChange]:
    """Each field either side has and the other does not, or holds otherwise, None standing for the side without it."""
    fields = [*old, *(key for key in new if key not in old)]
    return [InputChange(group, field, old.get(field), new.get(field)) for field in fields if old.get(field) != new.get(field)]


def input_changes(older: Solve, newer: Solve) -> tuple[list[InputChange], list[NurseChange]]:
    """A re-plan's `published` and `replan_from` are not compared: each side's facts say where it started from."""
    old, new = readings(older, newer)
    changes = [*_differing("hospital_rules", old.hospital_rules(), new.hospital_rules()),
               *_differing("unit_rules", old.unit_rules(), new.unit_rules())]
    if older.time_limit_seconds != newer.time_limit_seconds:
        changes.append(InputChange("time_limit", "time_limit_seconds", older.time_limit_seconds, newer.time_limit_seconds))
    changes += [InputChange("calendar", field, sorted(old.input.get(field, [])), sorted(new.input.get(field, [])))
                for field in CALENDAR if sorted(old.input.get(field, [])) != sorted(new.input.get(field, []))]
    before, after = old.nurses(), new.nurses()
    nurses = []
    for hospital_id in sorted(before | after):
        if hospital_id not in before:
            nurses.append(NurseChange(hospital_id, ["joined"]))
        elif hospital_id not in after:
            nurses.append(NurseChange(hospital_id, ["left"]))
        else:
            was, now = old.what_of(before[hospital_id]), new.what_of(after[hospital_id])
            differing = [kind for kind in was if was[kind] != now[kind]]
            if differing:
                nurses.append(NurseChange(hospital_id, differing))
    return changes, nurses


def soft_rules(solve: Solve) -> dict[str, dict] | None:
    """Each soft rule of the stored roster on its own input, costing or not, by the solver's own code: its `weight` and its
    amount, `raw`. None when that code no longer reads what is stored."""
    try:
        answer = validate_grid(in_current_skill_names(solve.input), solve.result["roster"])
    except ValueError:
        return None
    return {rule["id"]: rule for rule in answer["rules"] if rule["level"] == SOFT}


def _side(solve: Solve, rule: str, soft: dict[str, dict] | None) -> Side:
    if rule == "change" and not solve.replan_from:
        return Side(None, None, None)
    penalty = (solve.result.get("penalties") or {}).get(rule, 0)
    found = (soft or {}).get(rule)
    return Side(found["raw"], found["weight"], penalty) if found else Side(None, None, penalty)


def rule_rows(older: Solve, newer: Solve, soft_older: dict | None, soft_newer: dict | None) -> list[RuleRow]:
    """Each soft rule that costs something in either, the largest difference of penalty first. The penalties are each result's
    own, as it was kept; the amounts and weights are worked out again on its own input."""
    named = set(older.result.get("penalties") or {}) | set(newer.result.get("penalties") or {})
    rows = [RuleRow(rule, _side(older, rule, soft_older), _side(newer, rule, soft_newer)) for rule in named]
    rows = [row for row in rows if row.older.penalty or row.newer.penalty]
    return sorted(rows, key=lambda row: (-abs((row.newer.penalty or 0) - (row.older.penalty or 0)), row.rule))


def differing_cells(older: Solve, newer: Solve):
    """The cells that differ for the nurses in both, under today's codes. One in only one of them is an input change."""
    old, new = older.result["roster"], newer.result["roster"]
    both = set(old) & set(new)
    return differences({nurse: old[nurse] for nurse in both}, {nurse: new[nurse] for nurse in both})


def grouped(older: Iterable[dict], newer: Iterable[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """In both, only in the older, only in the newer: two entries are the same when their rule, nurse and date agree, and each
    is matched once, so two unit-wide entries of a rule on one side and one on the other leave one over."""
    def key(row: dict) -> tuple:
        return row["rule"], row["nurse"], row["date"]

    older, newer = list(older), list(newer)
    left = Counter(key(row) for row in newer)
    both, only_older = [], []
    for row in older:
        if left[key(row)]:
            left[key(row)] -= 1
            both.append(row)
        else:
            only_older.append(row)
    matched = Counter(key(row) for row in both)
    only_newer = []
    for row in newer:
        if matched[key(row)]:
            matched[key(row)] -= 1
        else:
            only_newer.append(row)
    return both, only_older, only_newer
