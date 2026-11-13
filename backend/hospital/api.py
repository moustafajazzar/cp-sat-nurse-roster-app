from typing import Annotated

from django.db import IntegrityError, models, transaction
from ninja import Router, Schema
from ninja.errors import ValidationError
from pydantic import ConfigDict, Field, ValidationInfo, field_validator

from accounts.authz import SignedIn
from accounts.models import Role
from audit.log import record
from hospital.models import HospitalSettings, Shift
from hospital.rules import changes

router = Router(tags=["hospital"])

# The solver's own limits (nurse_roster/problem.py): anything wider would be accepted here and then
# refused when a solve starts.
MAX_DAILY_HOURS = 24
MAX_HOURS = 10 ** 6
MAX_WEIGHT = 10 ** 6
OVERTIME_SHARE_ABOVE_OVERTIME = "Cannot be above Nurse overtime, or the solver could lower this cost by adding overtime."


def refuse(*errors: tuple[tuple, str]) -> None:
    """A refusal the schema cannot decide alone, in the shape a schema's own refusal has: (where, why)."""
    raise ValidationError([{"type": "value_error", "loc": ("body", "body", *loc), "msg": msg} for loc, msg in errors])


def differing(before: dict, after: dict) -> tuple[dict, dict]:
    """What an audit entry records: only the keys whose values changed."""
    keys = [key for key in before if before[key] != after[key]]
    return {key: before[key] for key in keys}, {key: after[key] for key in keys}


def save_unless_taken(row: models.Model, field: str, message: str) -> None:
    """The constraint decides, so two people saving at once cannot both get through."""
    try:
        with transaction.atomic():
            row.save()
    except IntegrityError:
        refuse(((field,), message))


DailyHours = Annotated[int, Field(ge=0, le=MAX_DAILY_HOURS)]
Hours = Annotated[int, Field(ge=0, le=MAX_HOURS)]
Weight = Annotated[int, Field(ge=0, le=MAX_WEIGHT)]
Weekday = Annotated[int, Field(ge=0, le=6)]


class StrictSchema(Schema):
    # A typing mistake is refused rather than turned into a number, and an unknown field rather than
    # dropped, because what is saved here decides every later solve.
    model_config = ConfigDict(strict=True, extra="forbid")


class Weights(StrictSchema):
    night_rest: Weight
    dept_overtime: Weight
    nurse_overtime: Weight
    nurse_shortfall: Weight
    avoidable_shortfall: Weight
    fair_day: Weight
    fair_night: Weight
    fair_weekend: Weight
    overtime_above_share: Weight
    score_deficit: Weight
    score_spread: Weight
    excess_weekend: Weight
    excess_full_run: Weight
    skill_shortfall: Weight
    shift_switch: Weight
    undesired_day: Weight
    desired_day: Weight
    change: Weight

    @field_validator("overtime_above_share")
    @classmethod
    def not_above_an_hour_of_overtime(cls, value: int, info: ValidationInfo) -> int:
        """A nurse's overtime share rises with the unit's overtime, so above the
        price of an hour of overtime the solver could lower this rule's cost by adding overtime (solver/DESIGN.md)."""
        if value > info.data.get("nurse_overtime", value):
            raise ValueError(OVERTIME_SHARE_ABOVE_OVERTIME)
        return value


class HospitalRules(StrictSchema):
    hours_per_day: DailyHours
    ramadan_hours_per_day: DailyHours
    breastfeeding_reduction: DailyHours
    weekend_weekdays: list[Weekday] = Field(description="0 is Monday and 6 is Sunday.")
    forbidden_successions: list[Annotated[list[str], Field(min_length=2, max_length=2)]] = Field(
        description="Pairs of shift codes: the shift today, and the shift that may not follow it tomorrow.")
    forbidden_successions_enabled: bool
    hours_rule_enabled: bool
    overtime_caps_enabled: bool
    overtime_month_cap: Hours
    overtime_year_cap: Hours
    dept_overtime_cap: Hours
    weights: Weights

    # Field validators, not one for the whole model, so that a refusal names the field. Each runs
    # after the fields declared above it; a neighbour that was itself refused is simply not there.
    @field_validator("ramadan_hours_per_day")
    @classmethod
    def ramadan_within_a_normal_day(cls, value: int, info: ValidationInfo) -> int:
        if value > info.data.get("hours_per_day", value):
            raise ValueError("must not exceed the hours per day")
        return value

    @field_validator("breastfeeding_reduction")
    @classmethod
    def reduction_within_a_ramadan_day(cls, value: int, info: ValidationInfo) -> int:
        if value > info.data.get("ramadan_hours_per_day", value):
            raise ValueError("must not exceed the Ramadan hours per day, or a working day would owe negative hours")
        return value

    @field_validator("weekend_weekdays")
    @classmethod
    def distinct_weekdays_in_order(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("lists a day twice")
        return sorted(value)

    @field_validator("forbidden_successions")
    @classmethod
    def distinct_pairs(cls, value: list[list[str]]) -> list[list[str]]:
        if len({tuple(pair) for pair in value}) != len(value):
            raise ValueError("lists a pair twice")
        return value


class HospitalRulesState(Schema):
    rules: HospitalRules
    needs_review: bool = Field(description="Still the seeded defaults: nobody has saved them yet.")
    shift_codes: list[str] = Field(description="What a forbidden succession may name.")


def _active_codes() -> list[str]:
    return list(Shift.objects.filter(active=True).values_list("code", flat=True))


def _state(row: HospitalSettings) -> dict:
    return {"rules": row.rules, "needs_review": row.rules_need_review, "shift_codes": _active_codes()}


@router.get("/rules", response=HospitalRulesState, auth=SignedIn(Role.CNO, Role.DEPUTY_CNO),
            operation_id="getHospitalRules")
def get_rules(request) -> dict:
    return _state(HospitalSettings.load())


@router.put("/rules", response=HospitalRulesState, auth=SignedIn(Role.CNO), operation_id="updateHospitalRules")
def update_rules(request, body: HospitalRules) -> dict:
    """Saving is the review: it clears the flag even when nothing else changed."""
    with transaction.atomic():
        # Here rather than in the schema, and under the lock every change to the catalogue also takes:
        # so a pair can only ever name an active shift, which is what the page's selects can show.
        row = HospitalSettings.load(for_update=True)
        active = set(_active_codes())
        refused = [(("forbidden_successions", index, side), f"{code} is not an active shift in the catalogue")
                   for index, pair in enumerate(body.forbidden_successions)
                   for side, code in enumerate(pair) if code not in active]
        if refused:
            refuse(*refused)
        after = body.model_dump()
        old, new = changes(row.rules, after)
        if row.rules_need_review:
            old, new = {"needs_review": True, **old}, {"needs_review": False, **new}
        if new:
            row.rules, row.rules_need_review = after, False
            row.save()
            record("hospital_rules.changed", actor=request.user, target="hospital rules",
                   before=old, after=new, request=request)
    return _state(row)
