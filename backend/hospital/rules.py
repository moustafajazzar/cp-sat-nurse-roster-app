"""The hospital rules: the solver's `general` block, kept in the solver's own field names so a solve
input can carry them as they are. `shift_hours` and `required_skills` are not among them: they come from the shift
catalogue and the skill catalogue."""
from __future__ import annotations

# The reviewed set the solver was shaped on. hospital/tests/test_rules.py holds it to the solver's own.
DEFAULT_RULES = {
    "hours_per_day": 8,
    "ramadan_hours_per_day": 6,
    "breastfeeding_reduction": 1,
    "weekend_weekdays": [4],
    "forbidden_successions": [["D", "C"], ["N", "D"], ["N", "A"], ["N", "B"], ["A", "B"],
                              ["A", "C"], ["B", "D"], ["C", "D"], ["C", "A"], ["C", "B"]],
    "forbidden_successions_enabled": True,
    "hours_rule_enabled": True,
    "overtime_caps_enabled": True,
    "overtime_month_cap": 40,
    "overtime_year_cap": 720,
    "dept_overtime_cap": 400,
    "weights": {
        "night_rest": 5,
        "dept_overtime": 1,
        "nurse_overtime": 1,
        "nurse_shortfall": 1,
        "avoidable_shortfall": 1000,
        "fair_day": 1,
        "fair_night": 1,
        "fair_weekend": 1,
        "overtime_above_share": 1,
        "score_deficit": 2,
        "score_spread": 1,
        "excess_weekend": 6,
        "excess_full_run": 4,
        "skill_shortfall": 0,
        "shift_switch": 8,
        "undesired_day": 2,
        "desired_day": 1,
        "change": 3,
    },
}


def required_skills() -> list[str]:
    """The names of the live skills required to work, in the catalogue's order. A retired skill is tracked no more, so
    it bars nobody."""
    from hospital.models import Skill  # The models read the defaults from here.

    return list(Skill.objects.filter(active=True, required_to_work=True).values_list("name", flat=True))


def general_block(settings, *, shift_hours: dict[str, int]) -> dict:
    """The `general` block of a solve input."""
    return settings.rules | {"shift_hours": shift_hours, "required_skills": required_skills()}


def flattened(rules: dict) -> dict:
    """The rules field by field, each weight as `weights.<name>`: the words the audit log records a change in."""
    return {key: value for key, value in rules.items() if key != "weights"} | {
        f"weights.{key}": value for key, value in rules.get("weights", {}).items()}


def changes(before: dict, after: dict) -> tuple[dict, dict]:
    """Only what differs, with each weight as `weights.<name>`, so the audit log reads field by field."""
    old, new = flattened(before), flattened(after)
    differing = [key for key in old if old[key] != new[key]]
    return {key: old[key] for key in differing}, {key: new[key] for key in differing}
