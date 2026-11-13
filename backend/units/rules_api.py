"""A unit's rules: its manager changes them, the CNO and the Deputy CNO read them. Who reaches which unit is
`units.scope`. Every value here may reach the audit log, because no rule says anything about a person."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Annotated, Literal
from uuid import UUID

from django.db import transaction
from ninja import Router, Schema
from nurse_roster.problem import (MAX_FULL_RUNS, MAX_HISTORY, MAX_NURSES, MAX_OVERTIME_HOURS, MAX_PARAM, MAX_REST_DAYS,
                                  MAX_SHIFTS, MAX_SKILLS, needed_history)
from pydantic import Field, ValidationInfo, field_validator

from accounts.authz import SignedIn
from accounts.models import Role
from audit.log import record
from hospital.api import StrictSchema, differing, refuse
from hospital.models import HospitalSettings, Shift, Skill
from nurses.membership import left_night_alone, narrow_allowed_shifts, stranded_by, this_month
from ops.models import TechnicalSettings
from rosters.drafts import frozen_cells_on_shifts_going
from rosters.grids import full_names
from rosters.models import Roster, RosterState
from rosters.pins import long_date, on_shifts_going, words_for
from units.models import Unit
from units.rules import distinct_shifts
from units.scope import unit_in_scope

router = Router(tags=["units"])

Headcount = Annotated[int, Field(ge=0, le=MAX_NURSES)]
Count = Annotated[int, Field(ge=0, le=MAX_PARAM)]
ScoreLevel = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
ShiftCount = Annotated[float, Field(ge=0, le=MAX_PARAM, allow_inf_nan=False)]
FullRuns = Annotated[int, Field(ge=0, le=MAX_FULL_RUNS)]
OvertimeHours = Annotated[int, Field(ge=0, le=MAX_OVERTIME_HOURS)]
MAX_REQUESTS = 31  # A request, or a preference, is about one date of one month.


class HeadcountRule(StrictSchema):
    shift: str
    weekday_min: Headcount
    weekday_max: Headcount
    weekend_min: Headcount
    weekend_max: Headcount
    also_counts: list[str]

    @field_validator("weekday_max", "weekend_max")
    @classmethod
    def not_below_its_minimum(cls, value: int, info: ValidationInfo) -> int:
        if value < info.data.get(info.field_name.replace("max", "min"), value):
            raise ValueError("Must not be below the minimum.")
        return value

    _distinct_also = field_validator("also_counts")(distinct_shifts)


class SkillNeedRule(StrictSchema):
    """At least so many nurses holding `skill` on exactly `shift`, each weekday and each weekend day."""
    shift: str
    skill: str
    weekday_min: Headcount
    weekend_min: Headcount


class UnitRules(StrictSchema):
    """Every field of the solver's `department` block but its name, which is the unit's. What depends on another field,
    on the catalogue or on the hospital rules is in `_refusals`, so that a refusal can name the field it belongs to."""
    shifts: list[str] = Field(max_length=MAX_SHIFTS)
    headcounts: list[HeadcountRule]
    headcount_enabled: bool
    require_team_leader: bool
    require_both_genders: bool
    score_target: ScoreLevel
    score_target_weekend: ScoreLevel
    score_balance: bool
    score_balance_mode: Literal["band", "spread"]
    score_band_hi: ScoreLevel
    score_band_hi_weekend: ScoreLevel
    night_shift: str
    day_shift: str
    night_rest_days: int = Field(ge=0, le=MAX_REST_DAYS)
    night_rest_hard: bool
    fair_shifts: list[str]
    fair_tolerance: ShiftCount
    fair_weekends: bool
    carry_fairness: bool
    carry_cap: ShiftCount
    share_overtime: bool
    overtime_tolerance: OvertimeHours
    overtime_carry_cap: OvertimeHours
    max_consecutive_shifts: Count
    double_cycle_rest: Count
    max_weekends: Count
    max_full_runs: FullRuns
    no_mixed_blocks: bool
    max_switches: Count
    switches_hard: bool
    skill_needs: list[SkillNeedRule] = Field(max_length=MAX_SKILLS)
    skill_needs_hard: bool

    _distinct_lists = field_validator("shifts", "fair_shifts")(distinct_shifts)


class UnitRulesBody(StrictSchema):
    rules: UnitRules
    request_cap: int = Field(ge=0, le=MAX_REQUESTS, description="Requests per nurse per month.")
    preference_cap: int = Field(ge=0, le=MAX_REQUESTS, description="Preferences per nurse per month. 0 switches them off.")
    default_time_limit: int = Field(ge=1, description="Seconds. At most the System Admin's time limit cap.")


class HeadcountAnswer(Schema):
    shift: str
    weekday_min: int
    weekday_max: int
    weekend_min: int
    weekend_max: int
    also_counts: list[str]


class SkillNeedAnswer(Schema):
    shift: str
    skill: str
    weekday_min: int
    weekend_min: int


class UnitRulesAnswer(Schema):
    """Not the request's schema: `shifts` may be empty and `night_shift` and `day_shift` may be "" in a unit whose
    default shifts were retired before it was created (`units.rules.seeded_rules`), and that unit must still open, since
    this is where it gets fixed. tests/test_rules_api.py holds both schemas to the solver's fields."""
    shifts: list[str]
    headcounts: list[HeadcountAnswer]
    headcount_enabled: bool
    require_team_leader: bool
    require_both_genders: bool
    score_target: float
    score_target_weekend: float
    score_balance: bool
    score_balance_mode: Literal["band", "spread"]
    score_band_hi: float
    score_band_hi_weekend: float
    night_shift: str
    day_shift: str
    night_rest_days: int
    night_rest_hard: bool
    fair_shifts: list[str]
    fair_tolerance: float
    fair_weekends: bool
    carry_fairness: bool
    carry_cap: float
    share_overtime: bool
    overtime_tolerance: int
    overtime_carry_cap: int
    max_consecutive_shifts: int
    double_cycle_rest: int
    max_weekends: int
    max_full_runs: int
    no_mixed_blocks: bool
    max_switches: int
    switches_hard: bool
    skill_needs: list[SkillNeedAnswer]
    skill_needs_hard: bool


class ShiftChoice(Schema):
    code: str
    display_name: str
    hours: int


class UnitRulesState(Schema):
    rules: UnitRulesAnswer
    needs_review: bool = Field(description="Still the seeded defaults: nobody has saved them yet.")
    request_cap: int
    preference_cap: int
    default_time_limit: int
    time_limit_cap: int = Field(description="The longest a solve may search, set by the System Admin. In seconds.")
    active_shifts: list[ShiftChoice] = Field(description="What the unit may choose its shifts from.")
    active_skills: list[str] = Field(description="The skills a skill need may name, in the catalogue's order.")


def _state(unit: Unit) -> dict:
    return {"rules": unit.rules, "needs_review": unit.rules_need_review, "request_cap": unit.request_cap,
            "preference_cap": unit.preference_cap, "default_time_limit": unit.default_time_limit,
            "time_limit_cap": TechnicalSettings.load().time_limit_cap_seconds,
            "active_shifts": list(Shift.objects.filter(active=True)),
            "active_skills": list(Skill.objects.filter(active=True).values_list("name", flat=True))}


def _night_rules_on(rules: UnitRules, hospital_weights: dict) -> bool:
    """When the solver wants the night and day shifts among the unit's. A weight is the CNO's and can change after this
    save, so one that goes from zero can still leave a unit the solver refuses: the solve shows that as it shows a gap."""
    rest_on = rules.night_rest_days > 0 and (rules.night_rest_hard or hospital_weights["night_rest"] > 0)
    return rest_on or rules.no_mixed_blocks or rules.switches_hard or hospital_weights["shift_switch"] > 0 \
        or rules.carry_fairness


def _refusals(rules: UnitRules, hospital_weights: dict, saved_shifts: list[str]) -> list[tuple[tuple, str]]:
    """Each thing the solver would refuse, said beside its field, all at once. Call under the catalogue's lock."""
    active = set(Shift.objects.filter(active=True).values_list("code", flat=True))
    shifts = set(rules.shifts)
    found = [(("rules", "shifts", index), f"{code} is not an active shift in the catalogue.")
             for index, code in enumerate(rules.shifts) if code not in active]
    if not rules.shifts:
        found.append((("rules", "shifts"), "Choose at least one shift."))

    night_rules = _night_rules_on(rules, hospital_weights)
    for name, label in (("night_shift", "night"), ("day_shift", "day")):
        code = getattr(rules, name)
        if not code:
            found.append((("rules", name), f"Choose the {label} shift."))
        elif code not in active:
            found.append((("rules", name), f"{code} is not an active shift in the catalogue."))
        elif night_rules and code not in shifts:
            found.append((("rules", name), f"{code} is not one of this unit's shifts, which it has to be while night "
                                           "rest, unmixed runs, carried fairness or the switch limit apply."))
    if rules.night_shift and rules.night_shift == rules.day_shift:
        found.append((("rules", "day_shift"), "Must not be the night shift too."))

    anchors = set()
    for index, headcount in enumerate(rules.headcounts):
        where = ("rules", "headcounts", index)
        if headcount.shift not in shifts:
            found.append(((*where, "shift"), f"{headcount.shift} is not one of this unit's shifts."))
        if headcount.shift in anchors:
            found.append(((*where, "shift"), f"{headcount.shift} has two headcounts."))
        anchors.add(headcount.shift)
        if headcount.shift in headcount.also_counts:
            found.append(((*where, "also_counts"), f"{headcount.shift} cannot count towards itself."))
        found += [((*where, "also_counts"), f"{code} is not one of this unit's shifts.")
                  for code in headcount.also_counts if code not in shifts]

    for code in rules.fair_shifts:
        if code not in shifts:
            found.append((("rules", "fair_shifts"), f"{code} is not one of this unit's shifts."))
        elif code not in anchors:
            found.append((("rules", "fair_shifts"), f"{code} has no headcount, so it has no fair share."))

    # Stricter than the solver, which asks only while two of the CNO's weights are above zero: they can change later.
    if rules.score_balance and rules.score_balance_mode == "band":
        if rules.score_target > rules.score_band_hi:
            found.append((("rules", "score_band_hi"), "Must not be below the score target."))
        if rules.score_target_weekend > rules.score_band_hi_weekend:
            found.append((("rules", "score_band_hi_weekend"), "Must not be below the weekend score target."))

    # A shift being taken out that a need names is refused beside the shifts, as one a pin holds is: the manager takes the
    # need out first. A need on a shift the unit never worked is the need's own mistake.
    going = set(saved_shifts) - shifts
    stranded = "; ".join(f"{need.skill} on {need.shift}" for need in rules.skill_needs if need.shift in going)
    if stranded:
        found.append((("rules", "shifts"), f"A skill need names a shift you are taking out: {stranded}. Take the need out "
                                           "first."))
    skills = set(Skill.objects.filter(active=True).values_list("name", flat=True))
    pairs = set()
    for index, need in enumerate(rules.skill_needs):
        where = ("rules", "skill_needs", index)
        if need.shift not in shifts and need.shift not in going:
            found.append(((*where, "shift"), f"{need.shift} is not one of this unit's shifts."))
        if need.skill not in skills:
            found.append(((*where, "skill"), f"{need.skill} is not an active skill in the catalogue."))
        elif (need.shift, need.skill) in pairs:
            found.append(((*where, "skill"), f"{need.shift} already has a need for {need.skill}."))
        pairs.add((need.shift, need.skill))

    looks_back = needed_history(SimpleNamespace(**rules.model_dump()))
    if looks_back > MAX_HISTORY:
        found.append((("rules", "max_consecutive_shifts"),
                      f"Together with the rest after two runs and the night rest, this looks back {looks_back} days, and "
                      f"a nurse's history holds {MAX_HISTORY}. Lower one of them."))
    return found


def _names(nurses: list) -> str:
    shown = [f"{nurse.user.full_name} ({nurse.user.username})" for nurse in nurses[:3]]
    return ", ".join(shown) + (f" and {len(nurses) - 3} more" if len(nurses) > 3 else "")


def _held_by(roster: Roster, cells: list[tuple[str, dt.date, str]]) -> str:
    """The first roster's cells only: the manager changes them, and saving again names the next."""
    names = full_names(nurse for nurse, _, _ in cells)
    said = "; ".join(f"{names.get(nurse) or nurse} on {code} on {long_date(day)}" for nurse, day, code in cells[:3])
    more = f", and {len(cells) - 3} more" if len(cells) > 3 else ""
    codes = " and ".join(dict.fromkeys(code for _, _, code in cells))
    if roster.state == RosterState.SUBMITTED:
        return (f"The submitted roster for {roster.month:%B %Y} has {said}{more}. Wait for its decision first, or take {codes} out "
                "once the month is over.")
    return (f"The roster in force for {roster.month:%B %Y} has {said}{more}. Change those cells on the Roster page first, "
            f"or take {codes} out once the month is over.")


@router.get("/{unit_id}/rules", response=UnitRulesState, auth=SignedIn(Role.UNIT_MANAGER, Role.CNO, Role.DEPUTY_CNO),
            operation_id="getUnitRules")
def get_unit_rules(request, unit_id: UUID) -> dict:
    return _state(unit_in_scope(request.user, unit_id))


@router.put("/{unit_id}/rules", response=UnitRulesState, auth=SignedIn(Role.UNIT_MANAGER), operation_id="updateUnitRules")
def update_unit_rules(request, unit_id: UUID, body: UnitRulesBody) -> dict:
    """Saving is the review: it clears the flag even when nothing else changed. Under the catalogue's lock, so that a
    code the CNO is retiring or renaming at this moment cannot be written."""
    with transaction.atomic():
        hospital = HospitalSettings.load(for_update=True)
        unit = unit_in_scope(request.user, unit_id, for_update=True)
        refused = _refusals(body.rules, hospital.rules["weights"], unit.rules.get("shifts", []))
        cap = TechnicalSettings.load().time_limit_cap_seconds
        if body.default_time_limit > cap:
            refused.append((("default_time_limit",), f"The System Admin's cap is {cap} seconds."))
        if refused:
            refuse(*refused)
        stranded = stranded_by(unit, body.rules.shifts)
        if stranded:
            refuse((("rules", "shifts"), f"{_names(stranded)} could work none of these shifts. "
                                         "Change their allowed shifts first."))
        night_alone = left_night_alone(unit, body.rules.shifts, body.rules.night_shift)
        if night_alone:
            refuse((("rules", "shifts"), f"{_names(night_alone)} would have only {body.rules.night_shift}, the night shift, of "
                                         "these shifts, and a nurse on probation works no night shift. Change their allowed "
                                         "shifts first."))

        pinned = on_shifts_going(unit, body.rules.shifts)
        if pinned:
            refuse((("rules", "shifts"), f"A pin holds a shift you are taking out: {words_for(pinned)}. Clear those pins on the "
                                         "Roster page first."))
        # Only the shifts being taken out: a month from before this check may already hold one, and must not stop every save.
        going = set(unit.rules.get("shifts", [])) - set(body.rules.shifts)
        held = frozen_cells_on_shifts_going(unit, going, this_month())
        if held:
            refuse((("rules", "shifts"), _held_by(*held[0])))

        rules = body.rules.model_dump()
        old, new = differing(
            unit.rules | {"request_cap": unit.request_cap, "preference_cap": unit.preference_cap,
                          "default_time_limit": unit.default_time_limit},
            rules | {"request_cap": body.request_cap, "preference_cap": body.preference_cap,
                     "default_time_limit": body.default_time_limit})
        if unit.rules_need_review:
            old, new = {"needs_review": True, **old}, {"needs_review": False, **new}
        if new:
            narrow_allowed_shifts(unit, body.rules.shifts)
            unit.rules, unit.rules_need_review = rules, False
            unit.request_cap, unit.preference_cap = body.request_cap, body.preference_cap
            unit.default_time_limit = body.default_time_limit
            unit.save(update_fields=["rules", "rules_need_review", "request_cap", "preference_cap", "default_time_limit"])
            record("unit_rules.changed", actor=request.user, target=unit.name, before=old, after=new, request=request)
    return _state(unit)
