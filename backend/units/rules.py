"""The unit rules: the solver's `department` block, kept in the solver's own field names so a solve input
can carry them as they are. `name` is not among them: it is the unit's own name."""
from __future__ import annotations

from hospital.models import Shift, ShiftRename
from units.models import Unit

_HEADCOUNT = {"weekday_min": 7, "weekday_max": 9, "weekend_min": 8, "weekend_max": 9, "also_counts": []}

# The reviewed set the solver was shaped on. units/tests/test_unit_rules.py holds it to the solver's own.
DEFAULT_UNIT_RULES = {
    "shifts": ["D", "N"],
    "headcounts": [{"shift": "D"} | _HEADCOUNT, {"shift": "N"} | _HEADCOUNT],
    "headcount_enabled": True,
    "require_team_leader": True,
    "require_both_genders": True,
    "score_target": 0.7,
    "score_target_weekend": 0.8,
    "score_balance": True,
    "score_balance_mode": "band",
    "score_band_hi": 0.78,
    "score_band_hi_weekend": 0.88,
    "night_shift": "N",
    "day_shift": "D",
    "night_rest_days": 2,
    "night_rest_hard": True,
    "fair_shifts": ["D", "N"],
    "fair_tolerance": 0.5,
    "fair_weekends": True,
    "carry_fairness": True,
    "carry_cap": 2.0,
    "share_overtime": True,
    "overtime_tolerance": 12,
    "overtime_carry_cap": 12,
    "max_consecutive_shifts": 3,
    "double_cycle_rest": 2,
    "max_weekends": 3,
    "max_full_runs": 3,
    "no_mixed_blocks": True,
    "max_switches": 2,
    "switches_hard": False,
    "skill_needs": [],
    "skill_needs_hard": True,
}


def distinct_shifts(codes: list[str]) -> list[str]:
    """A schema's validator: the solver refuses a shift named twice in any of these lists."""
    if len(set(codes)) != len(codes):
        raise ValueError("Lists a shift twice.")
    return codes


def unit_block(unit) -> dict:
    """The `department` block of a solve input."""
    return unit.rules | {"name": unit.name}


def with_shift_codes(rules: dict, codes: dict[str, str | None]) -> dict:
    """The rules with each code in `codes` replaced, or left out where it maps to None, everywhere the block
    names a shift. A night or day shift left out is "", which the solver refuses, so the gap cannot reach a
    roster unnoticed. Codes not in `codes` stay. Returns a new block."""
    def code(name: str) -> str | None:
        return codes.get(name, name)

    def named(names: list[str]) -> list[str]:
        return [code(name) for name in names if code(name) is not None]

    return rules | {
        "shifts": named(rules["shifts"]),
        "fair_shifts": named(rules["fair_shifts"]),
        "night_shift": code(rules["night_shift"]) or "",
        "day_shift": code(rules["day_shift"]) or "",
        "headcounts": [headcount | {"shift": code(headcount["shift"]), "also_counts": named(headcount["also_counts"])}
                       for headcount in rules["headcounts"] if code(headcount["shift"]) is not None],
        "skill_needs": [need | {"shift": code(need["shift"])} for need in rules["skill_needs"]
                        if code(need["shift"]) is not None],
    }


def with_skill_renamed(rules: dict, old_name: str, new_name: str) -> dict:
    """The rules with every skill need on `old_name` naming `new_name` instead. Returns a new block."""
    return rules | {"skill_needs": [need | {"skill": new_name} if need["skill"] == old_name else need
                                    for need in rules["skill_needs"]]}


def units_using(code: str) -> list[str]:
    """The names of the live units whose rules name this shift. `night_shift` and `day_shift` count because the solver
    wants them in `shifts` only while the night rules are on, so a saved block can name one outside it. The other
    places a block names a shift are all within `shifts`. An archived unit rosters nothing more, so it does not count."""
    return [unit.name for unit in Unit.objects.filter(archived=False)
            if code in (*unit.rules["shifts"], unit.rules["night_shift"], unit.rules["day_shift"])]


def units_needing(skill: str) -> list[str]:
    """The names of the live units with a skill need on this skill. An archived unit rosters nothing more, so it does
    not count."""
    return [unit.name for unit in Unit.objects.filter(archived=False).order_by("name")
            if any(need["skill"] == skill for need in unit.rules["skill_needs"])]


def _current_code(default_code: str) -> str | None:
    """No other shift can ever take a code a shift held, so a default code means one shift for good."""
    shift = Shift.objects.filter(code=default_code).first()
    if shift is None:
        rename = ShiftRename.objects.select_related("shift").filter(old_code=default_code).first()
        shift = rename.shift if rename else None
    return shift.code if shift and shift.active else None


def seeded_rules() -> dict:
    """The defaults as the catalogue stands: a shift the CNO renamed under its new code, and one the CNO
    retired left out rather than swapped for a guess, since the manager reviews them anyway. Call it under the
    catalogue's lock (`HospitalSettings.load(for_update=True)`), so that a rename cannot slip past."""
    return with_shift_codes(DEFAULT_UNIT_RULES, {code: _current_code(code) for code in DEFAULT_UNIT_RULES["shifts"]})
