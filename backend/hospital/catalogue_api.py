"""The CNO's lists, which every role may read: the shift catalogue, the leave types, the skill catalogue, the public
holidays and the Ramadan dates."""
from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from typing import Annotated, Literal
from uuid import UUID

from django.db import transaction
from django.db.models.functions import Lower
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Path, Query, Router, Schema
from ninja.errors import HttpError, ValidationError
from nurse_roster.calendar import GREGORIAN_RANGE
from pydantic import AfterValidator, BeforeValidator, Field, StringConstraints, field_validator

from accounts.authz import SignedIn
from accounts.models import Role
from audit.log import record
from hospital.api import StrictSchema, differing, refuse, save_unless_taken
from hospital.catalogue import (CalendarRamadan, calendar_arafat_days, calendar_ramadan, calendar_ramadans,
                                derived_ramadan_dates, month_of, shift_month)
from hospital.models import (FixedRamadanMonth, HospitalSettings, LeaveType, PublicHoliday, Shift, ShiftRename, Skill,
                             SkillKind, SkillRename)
from hospital.rules import changes
from notifications.sending import MonthDates, month_dates_changed
from nurses.membership import carry_rename_through_allowed_shifts, this_month
from nurses.models import NurseSkill
from ops.api import ClockTime
from rosters.drafts import carry_rename_through_grids, current_code, using_shift
from rosters.pins import counted_uses
from solves.models import ACTIVE_STATES, Solve
from units.models import Unit
from units.rules import units_needing, units_using, with_shift_codes, with_skill_renamed

router = Router(tags=["hospital"])
cno = SignedIn(Role.CNO)
RESERVED_CODE = "L"  # nurse_roster.problem.RESERVED_SHIFT: the solver's CSV export marks leave with it.
# The whole years the Umm al-Qura data covers; the holidays and the Ramadan dates keep to them alike.
FIRST_YEAR, LAST_YEAR = GREGORIAN_RANGE[0].year + 1, GREGORIAN_RANGE[1].year - 1
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


def no_nul(value: str) -> str:
    # PostgreSQL cannot hold one; it would otherwise be a 500.
    if "\x00" in value:
        raise ValueError("cannot contain a NUL character")
    return value


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100), AfterValidator(no_nul)]
Colour = Annotated[str, Field(pattern="^#[0-9a-fA-F]{6}$", examples=["#0284c7"]), AfterValidator(str.lower)]


def iso_date(value: object) -> dt.date:
    # A schema's strict mode would refuse every date, because JSON has only strings, and its lax mode
    # would take a number of seconds or a time of day as one.
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("must be a date as YYYY-MM-DD")
    day = dt.date.fromisoformat(value)
    if not FIRST_YEAR <= day.year <= LAST_YEAR:
        raise ValueError(f"must be between {FIRST_YEAR} and {LAST_YEAR}")
    return day


IsoDate = Annotated[dt.date, BeforeValidator(iso_date)]


class YearFilter(Schema):
    year: int = Field(ge=FIRST_YEAR, le=LAST_YEAR)


# ---- shifts ---------------------------------------------------------------------------------------
class ShiftBody(StrictSchema):
    # The first character keeps a spreadsheet from reading an export's cell as a formula, which the solver
    # also insists on; eight is what a roster grid's cell can show.
    code: str = Field(pattern="^[A-Za-z0-9][A-Za-z0-9_-]{0,7}$", description="The name the solver and rosters use.")
    display_name: Name
    hours: int = Field(ge=1, le=24)
    start_time: ClockTime
    end_time: ClockTime = Field(description="Earlier than the start time when the shift runs past midnight.")
    colour: Colour
    active: bool

    @field_validator("code")
    @classmethod
    def not_reserved(cls, value: str) -> str:
        if value.upper() == RESERVED_CODE:
            raise ValueError(f"{RESERVED_CODE} is reserved: exports mark leave with it")
        return value


class ShiftRenameSchema(Schema):
    old_code: str
    new_code: str
    at: dt.datetime = Field(description="In the hospital's time zone.")

    @staticmethod
    def resolve_at(rename: ShiftRename) -> dt.datetime:
        return timezone.localtime(rename.at)


class ShiftSchema(Schema):
    """Not the request's schema: what is stored is shown as it is, whatever the rules were when it was saved."""
    id: UUID
    code: str
    display_name: str
    hours: int
    start_time: str
    end_time: str
    colour: str
    active: bool
    renames: list[ShiftRenameSchema] = Field(description="Every change of this shift's code, oldest first.")

    @staticmethod
    def resolve_start_time(shift: Shift) -> str:
        return shift.start_time.strftime("%H:%M")

    @staticmethod
    def resolve_end_time(shift: Shift) -> str:
        return shift.end_time.strftime("%H:%M")

    @staticmethod
    def resolve_renames(shift: Shift) -> list[ShiftRename]:
        return list(shift.renames.all())


def _shift_fields(shift: Shift) -> dict:
    return {"code": shift.code, "display_name": shift.display_name, "hours": shift.hours,
            "start_time": shift.start_time.strftime("%H:%M"), "end_time": shift.end_time.strftime("%H:%M"),
            "colour": shift.colour, "active": shift.active}


def _write_shift(shift: Shift, body: ShiftBody) -> None:
    shift.code, shift.display_name, shift.hours = body.code, body.display_name, body.hours
    shift.start_time, shift.end_time = dt.time.fromisoformat(body.start_time), dt.time.fromisoformat(body.end_time)
    shift.colour, shift.active = body.colour, body.active
    shift.save()


def _refuse_a_taken_code(code: str, shift_id: UUID | None) -> None:
    """Also a code another shift used to hold: the record says that name in an old history means that
    shift, so it cannot start to mean this one. A shift may go back to a code of its own."""
    held = Shift.objects.filter(code__iexact=code).exclude(pk=shift_id).exists()
    held_before = ShiftRename.objects.filter(old_code__iexact=code).exclude(shift_id=shift_id).exists()
    if held or held_before:
        refuse((("code",), f"{code} is {'already in use' if held else 'the old code of another shift'}"))


@router.get("/shifts", response=list[ShiftSchema], operation_id="listShifts")
def list_shifts(request) -> list[Shift]:
    """Retired shifts too: a code on an old roster still needs its name."""
    return list(Shift.objects.prefetch_related("renames"))


@router.post("/shifts", response={201: ShiftSchema}, auth=cno, operation_id="createShift")
def create_shift(request, body: ShiftBody) -> tuple[int, Shift]:
    with transaction.atomic():
        HospitalSettings.load(for_update=True)  # One writer at a time, so a taken code cannot slip through.
        _refuse_a_taken_code(body.code, None)
        shift = Shift()
        _write_shift(shift, body)
        record("shift.created", actor=request.user, target=shift.code, after=_shift_fields(shift), request=request)
    return 201, shift


@router.put("/shifts/{shift_id}", response=ShiftSchema, auth=cno, operation_id="updateShift")
def update_shift(request, shift_id: UUID, body: ShiftBody) -> Shift:
    """Hold the rules' lock throughout: a forbidden succession only ever names an active shift, and a live unit's
    rules likewise, which both this and saving either set of rules must see the other's finished work to keep true."""
    with transaction.atomic():
        settings = HospitalSettings.load(for_update=True)
        shift = get_object_or_404(Shift, pk=shift_id)
        before, after = _shift_fields(shift), body.model_dump()
        old, new = differing(before, after)
        if not new:
            return shift
        pairs = settings.rules["forbidden_successions"]
        if before["active"] and not body.active and any(before["code"] in pair for pair in pairs):
            refuse((("active",), f"{before['code']} is named in the forbidden successions of the hospital rules. "
                                 "Remove those pairs first."))
        if before["active"] and not body.active:
            # A pin that counts is on a shift its unit works (`rosters.pins`), so this covers those too.
            units = units_using(before["code"])
            if units:
                refuse((("active",), f"{before['code']} is in the unit rules of {', '.join(units)}. "
                                     "The unit's manager has to take it out of them first."))
        if "hours" in new:
            _refuse_hours_in_use(before["code"])
        if "code" in new:
            _refuse_a_taken_code(body.code, shift.pk)
            ShiftRename.objects.create(shift=shift, old_code=before["code"], new_code=body.code, by=request.user)
            _carry_rename_through_the_rules(request, settings, before["code"], body.code)
            _carry_rename_through_the_units(request, lambda rules: with_shift_codes(rules, {before["code"]: body.code}))
            carry_rename_through_allowed_shifts(before["code"], body.code)
            carry_rename_through_grids(before["code"], body.code)
        _write_shift(shift, body)
        record("shift.changed", actor=request.user, target=shift.code, before=old, after=new, request=request)
    return shift


def _refuse_hours_in_use(code: str) -> None:
    """A roster's hours were worked out with the shift's hours as they stood, and so were those of a solve on its way."""
    uses = [f"{roster.unit.name}, {roster.month:%B %Y}" for roster in using_shift(code)]
    waiting = Solve.objects.select_related("unit").filter(state__in=ACTIVE_STATES)
    uses += [f"{solve.unit.name}, {solve.month:%B %Y}" for solve in waiting
             if code in {current_code(shift) for shift in solve.input["department"]["shifts"]}]
    uses += [f"{unit.name}, {month:%B %Y}" for unit, month in counted_uses(code, this_month())]
    if uses:
        refuse((("hours",), f"The hours of {code} cannot change while the roster of an open month uses it: "
                            f"{'; '.join(dict.fromkeys(uses))}."))


def _carry_rename_through_the_rules(request, settings: HospitalSettings, old_code: str, new_code: str) -> None:
    pairs = [[new_code if code == old_code else code for code in pair]
             for pair in settings.rules["forbidden_successions"]]
    after = settings.rules | {"forbidden_successions": pairs}
    old, new = changes(settings.rules, after)
    if new:
        settings.rules = after
        settings.save()
        record("hospital_rules.changed", actor=request.user, target="hospital rules", before=old, after=new,
               request=request)


def _carry_rename_through_the_units(request, renamed: Callable[[dict], dict]) -> None:
    """`renamed` gives a unit's rules under the new name. Archived units too, because a block has to go on naming shifts
    and skills that exist. The review flag stays as it is: a new name is not a review. The rows are locked because
    `units.api.update_unit` saves the whole row."""
    for unit in Unit.objects.select_for_update().order_by("pk"):  # By id, as an import locks them, so neither waits on the other.
        after = renamed(unit.rules)
        old, new = differing(unit.rules, after)
        if new:
            unit.rules = after
            unit.save(update_fields=["rules"])
            record("unit_rules.changed", actor=request.user, target=unit.name, before=old, after=new, request=request)


# ---- leave types ----------------------------------------------------------------------------------
class LeaveTypeBody(StrictSchema):
    name: Name
    active: bool


class LeaveTypeSchema(Schema):
    id: UUID
    name: str
    active: bool


def _write_leave_type(row: LeaveType, body: LeaveTypeBody) -> None:
    row.name, row.active = body.name, body.active
    save_unless_taken(row, "name", f"{body.name} is already a leave type")


@router.get("/leave-types", response=list[LeaveTypeSchema], operation_id="listLeaveTypes")
def list_leave_types(request) -> list[LeaveType]:
    return list(LeaveType.objects.all())


@router.post("/leave-types", response={201: LeaveTypeSchema}, auth=cno, operation_id="createLeaveType")
def create_leave_type(request, body: LeaveTypeBody) -> tuple[int, LeaveType]:
    with transaction.atomic():
        row = LeaveType()
        _write_leave_type(row, body)
        record("leave_type.created", actor=request.user, target=row.name, after=body.model_dump(), request=request)
    return 201, row


@router.put("/leave-types/{leave_type_id}", response=LeaveTypeSchema, auth=cno, operation_id="updateLeaveType")
def update_leave_type(request, leave_type_id: UUID, body: LeaveTypeBody) -> LeaveType:
    with transaction.atomic():
        row = get_object_or_404(LeaveType.objects.select_for_update(), pk=leave_type_id)
        old, new = differing({"name": row.name, "active": row.active}, body.model_dump())
        if new:
            _write_leave_type(row, body)
            record("leave_type.changed", actor=request.user, target=row.name, before=old, after=new, request=request)
    return row


# ---- skills ---------------------------------------------------------------------------------------
def _safe_in_a_file(value: str) -> str:
    # The solver refuses a skill name a spreadsheet would read as a formula, or with a line break, as it refuses such a
    # shift code: a nurse holding one would leave every solve of her unit refused.
    if value.startswith(("=", "+", "-", "@", "'")):
        raise ValueError("cannot start with = + - @ or '")
    if "\n" in value or "\r" in value:
        raise ValueError("cannot hold a line break")
    return value


SkillName = Annotated[Name, AfterValidator(_safe_in_a_file)]
IssuingBody = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100), AfterValidator(no_nul)]


class SkillBody(StrictSchema):
    name: SkillName = Field(description="What nurses' records, unit rules and solve inputs call it.")
    kind: SkillKind = Field(description="A skill (an ability), a certificate (a degree, a course) or a licence (permits a "
                                        "nurse to practise). Every kind is read alike, but a licence always expires.")
    issuing_body: IssuingBody = Field(description="Who issues it, as free text; empty when no one does.")
    expires: bool = Field(description="Every nurse who holds it has an expiry for it, the last day it counts.")
    required_to_work: bool = Field(description="From the day after its expiry, a nurse holding it works no shift.")
    lets_a_nurse_precept: bool = Field(description="A nurse holding it, unexpired, may be assigned as a preceptor. Checked "
                                                   "when a preceptor is assigned only, so turning it off changes no "
                                                   "preceptor already assigned.")
    active: bool = Field(description="Off retires it: it stays on this list, and the nurses who hold it keep it.")


class SkillRenameSchema(Schema):
    old_name: str
    new_name: str
    at: dt.datetime = Field(description="In the hospital's time zone.")

    @staticmethod
    def resolve_at(rename: SkillRename) -> dt.datetime:
        return timezone.localtime(rename.at)


class SkillSchema(Schema):
    id: UUID
    name: str
    kind: SkillKind
    issuing_body: str
    expires: bool
    required_to_work: bool
    lets_a_nurse_precept: bool
    active: bool
    renames: list[SkillRenameSchema] = Field(description="Every change of this skill's name, oldest first.")

    @staticmethod
    def resolve_renames(skill: Skill) -> list[SkillRename]:
        return list(skill.renames.all())


def _skill_fields(skill: Skill) -> dict:
    return {"name": skill.name, "kind": skill.kind, "issuing_body": skill.issuing_body, "expires": skill.expires,
            "required_to_work": skill.required_to_work, "lets_a_nurse_precept": skill.lets_a_nurse_precept,
            "active": skill.active}


def _write_skill(skill: Skill, body: SkillBody) -> None:
    (skill.name, skill.kind, skill.issuing_body, skill.expires, skill.required_to_work, skill.lets_a_nurse_precept,
     skill.active) = (body.name, body.kind, body.issuing_body, body.expires, body.required_to_work,
                      body.lets_a_nurse_precept, body.active)
    # The constraint still decides should anything ever write without the catalogue's lock.
    save_unless_taken(skill, "name", f"{body.name} is already a skill")


def _refuse_a_taken_name(name: str, skill_id: UUID | None) -> None:
    """Also a name another skill used to hold, as for a shift's code: a stored solve input that says that name means
    that skill, so it cannot start to mean this one. A skill may go back to a name of its own, or change its case."""
    held = Skill.objects.filter(name__iexact=name).exclude(pk=skill_id).first()
    if held:
        refuse((("name",), f"{held.name} is already a skill"))
    held_before = SkillRename.objects.filter(old_name__iexact=name).exclude(skill_id=skill_id).first()
    if held_before:
        refuse((("name",), f"{held_before.old_name} is the old name of another skill"))


NAMED_AT_MOST = 3


def _refuse_a_licence_that_does_not_expire(body: SkillBody) -> None:
    # The constraint `hospital_skill_a_licence_expires` holds it in the database too.
    if body.kind == Skill.Kind.LICENCE and not body.expires:
        refuse((("expires",), "A licence always expires."))


def _who_holds_it_without(skill: Skill, field: str) -> str | None:
    """"Amal Saleh and Badr Ali hold": the nurses whose current record of `skill` has no `field` (`expiry`, `identifier`), a
    few by name and the rest counted, with the verb; None when there are none."""
    names = list(NurseSkill.objects.current().filter(skill=skill, **{f"{field}__isnull": True})
                 .order_by(Lower("nurse__user__full_name"), "nurse_id").values_list("nurse__user__full_name", flat=True))
    if not names:
        return None
    if len(names) > NAMED_AT_MOST + 1:
        who = f"{', '.join(names[:NAMED_AT_MOST])} and {len(names) - NAMED_AT_MOST} more"
    else:
        who = " and ".join([", ".join(names[:-1]), names[-1]] if len(names) > 1 else names)
    return f"{who} {'hold' if len(names) > 1 else 'holds'}"


def _without_expiry(skill: Skill, why: str = "") -> str | None:
    """Turning `expires` on would leave a record dateless, an unknown the solver cannot model: refused, naming a few of the
    nurses. Off again is allowed, and the dates stay. A nurse's skill write takes the same lock first (`nurses.skills_api`).
    A change to licence turns it on, so it is refused the same way, beside the kind."""
    who = _who_holds_it_without(skill, "expiry")
    return who and (f"{who} {skill.name} with no expiry{why}. "
                    "Take it off them first, then record it again with its expiry.")


def _without_identifier(skill: Skill) -> str | None:
    """A licence's record always has its identifier, so a change to licence is refused while one has none. A
    renewal on the Nurses page can give it one."""
    who = _who_holds_it_without(skill, "identifier")
    return who and (f"{who} {skill.name} with no identifier, and a licence always has one. "
                    "Renew it with its identifier on the Nurses page first.")


@router.get("/skills", response=list[SkillSchema], operation_id="listSkills")
def list_skills(request) -> list[Skill]:
    """Retired skills too: a nurse's record of one, or a past roster, still names it."""
    return list(Skill.objects.prefetch_related("renames"))


@router.post("/skills", response={201: SkillSchema}, auth=cno, operation_id="createSkill")
def create_skill(request, body: SkillBody) -> tuple[int, Skill]:
    with transaction.atomic():
        HospitalSettings.load(for_update=True)  # One writer at a time, so a taken or old name cannot slip through.
        _refuse_a_licence_that_does_not_expire(body)
        _refuse_a_taken_name(body.name, None)
        skill = Skill()
        _write_skill(skill, body)
        record("skill.created", actor=request.user, target=skill.name, after=_skill_fields(skill), request=request)
    return 201, skill


@router.put("/skills/{skill_id}", response=SkillSchema, auth=cno, operation_id="updateSkill")
def update_skill(request, skill_id: UUID, body: SkillBody) -> Skill:
    """A rename, a change of kind, issuing body or a mark, and a retire alike: `active` off retires it, on brings it back."""
    with transaction.atomic():
        HospitalSettings.load(for_update=True)
        skill = get_object_or_404(Skill, pk=skill_id)
        old, new = differing(_skill_fields(skill), body.model_dump())
        if not new:
            return skill
        _refuse_a_licence_that_does_not_expire(body)
        if new.get("kind") == Skill.Kind.LICENCE:
            # Both refusals at once, in one message: the page shows one error beside a field.
            found = [words for words in (_without_expiry(skill, ", and a licence always expires"), _without_identifier(skill))
                     if words]
            if found:
                refuse((("kind",), " ".join(found)))
        elif new.get("expires") and (words := _without_expiry(skill)):
            refuse((("expires",), words))
        if new.get("active") is False:
            units = units_needing(skill.name)
            if units:
                refuse((("active",), f"{skill.name} is in the skill needs of {', '.join(units)}. "
                                     "The unit's manager has to take it out of them first."))
        if "name" in new:
            _refuse_a_taken_name(body.name, skill.pk)
            SkillRename.objects.create(skill=skill, old_name=old["name"], new_name=body.name, by=request.user)
            # Unit rules store a skill by its name; a nurse's record keeps the skill's id.
            _carry_rename_through_the_units(request, lambda rules: with_skill_renamed(rules, old["name"], body.name))
        _write_skill(skill, body)
        record("skill.changed", actor=request.user, target=skill.name, before=old, after=new, request=request)
    return skill


# ---- public holidays ------------------------------------------------------------------------------
class PublicHolidayBody(StrictSchema):
    date: IsoDate
    name: Name


class PublicHolidaySchema(Schema):
    id: UUID
    date: dt.date
    name: str


def _holiday_fields(row: PublicHoliday) -> dict:
    return {"date": row.date.isoformat(), "name": row.name}


def _save_holiday(row: PublicHoliday) -> None:
    save_unless_taken(row, "date", f"{row.date.isoformat()} is already a public holiday")


def _write_holiday(row: PublicHoliday, body: PublicHolidayBody) -> None:
    row.date, row.name = body.date, body.name
    _save_holiday(row)


def _holidays_changed(request, *days: dt.date) -> None:
    """Tells the managers with a roster in each month these days fall in: a holiday moved to another month changes
    both. A write that adds several days calls this once, with all of them, so a month is told once."""
    month_dates_changed({day.replace(day=1) for day in days}, MonthDates.PUBLIC_HOLIDAYS, request.user)


@router.get("/public-holidays", response=list[PublicHolidaySchema], operation_id="listPublicHolidays")
def list_public_holidays(request, filters: Query[YearFilter]) -> list[PublicHoliday]:
    return list(PublicHoliday.objects.filter(date__year=filters.year))


@router.post("/public-holidays", response={201: PublicHolidaySchema}, auth=cno, operation_id="createPublicHoliday")
def create_public_holiday(request, body: PublicHolidayBody) -> tuple[int, PublicHoliday]:
    with transaction.atomic():
        row = _create_holiday(request, body.date, body.name)
        _holidays_changed(request, row.date)
    return 201, row


def _create_holiday(request, day: dt.date, name: str) -> PublicHoliday:
    """The one way a holiday is created, by hand or from a proposal: refused on `date` when the day is one already.
    The caller holds the transaction, and tells the managers once for all it creates (`_holidays_changed`)."""
    row = PublicHoliday(date=day, name=name)
    _save_holiday(row)
    record("public_holiday.created", actor=request.user, target=row.date.isoformat(),
           after=_holiday_fields(row), request=request)
    return row


@router.put("/public-holidays/{holiday_id}", response=PublicHolidaySchema, auth=cno,
            operation_id="updatePublicHoliday")
def update_public_holiday(request, holiday_id: UUID, body: PublicHolidayBody) -> PublicHoliday:
    with transaction.atomic():
        row = get_object_or_404(PublicHoliday.objects.select_for_update(), pk=holiday_id)
        old, new = differing(_holiday_fields(row), body.model_dump(mode="json"))
        if new:
            moved_from = row.date
            _write_holiday(row, body)
            record("public_holiday.changed", actor=request.user, target=row.date.isoformat(),
                   before=old, after=new, request=request)
            if row.date != moved_from:  # A new name moves no date.
                _holidays_changed(request, moved_from, row.date)
    return row


@router.delete("/public-holidays/{holiday_id}", response={204: None}, auth=cno, operation_id="deletePublicHoliday")
def delete_public_holiday(request, holiday_id: UUID) -> tuple[int, None]:
    """A holiday has no history of its own: an entry made by mistake goes, and the audit log keeps it."""
    with transaction.atomic():
        row = get_object_or_404(PublicHoliday.objects.select_for_update(), pk=holiday_id)
        record("public_holiday.removed", actor=request.user, target=row.date.isoformat(),
               before=_holiday_fields(row), request=request)
        row.delete()
        _holidays_changed(request, row.date)
    return 204, None


# ---- Ramadan dates --------------------------------------------------------------------------------
class RamadanFix(StrictSchema):
    first: IsoDate | None = Field(None, description="With `last`: every day from this one to that one.")
    last: IsoDate | None = None
    dates: list[IsoDate] | None = Field(None, description="Or exactly these days. Empty means none this month.")


class RamadanMonth(Schema):
    month: str
    source: Literal["derived", "fixed"] = Field(description="Fixed by the CNO, or derived from the calendar.")
    dates: list[dt.date] = Field(description="The month's Ramadan days.")
    derived_dates: list[dt.date] = Field(description="What the Umm al-Qura calendar says, whichever is in force.")


def _ramadan_month(month: str, fixed: list[dt.date] | None) -> dict:
    derived = derived_ramadan_dates(month)
    return {"month": month, "source": "derived" if fixed is None else "fixed",
            "dates": derived if fixed is None else fixed, "derived_dates": derived}


def _audited_ramadan(state: dict) -> dict:
    return {"source": state["source"], "dates": [day.isoformat() for day in state["dates"]]}


def _month_within_the_calendar(month: str) -> str:
    if not FIRST_YEAR <= int(month[:4]) <= LAST_YEAR:
        raise HttpError(422, f"The calendar covers {FIRST_YEAR} to {LAST_YEAR}.")
    return month


def _fixed_dates(month: str, body: RamadanFix) -> list[dt.date]:
    """Refused here because only the path says which month a date must fall in."""
    def outside(day: dt.date) -> bool:
        return day.strftime("%Y-%m") != month

    if body.dates is not None:
        if body.first or body.last:
            refuse((("dates",), "Give a range or a list, not both"))
        outsiders = [((("dates", index)), f"{day.isoformat()} is not in {month}")
                     for index, day in enumerate(body.dates) if outside(day)]
        if outsiders:
            refuse(*outsiders)
        if len(set(body.dates)) != len(body.dates):
            refuse((("dates",), "lists a day twice"))
        return sorted(body.dates)
    if not body.first and not body.last:
        refuse((("dates",), "Give a range or a list"))
    if not body.first:
        refuse((("first",), "A range needs its first day"))
    if not body.last:
        refuse((("last",), "A range needs its last day"))
    if outside(body.first):
        refuse((("first",), f"{body.first.isoformat()} is not in {month}"))
    if outside(body.last):
        refuse((("last",), f"{body.last.isoformat()} is not in {month}"))
    if body.last < body.first:
        refuse((("last",), "must not be before the first day"))
    return [body.first + dt.timedelta(days=offset) for offset in range((body.last - body.first).days + 1)]


Month = Annotated[str, Path(pattern=MONTH_PATTERN)]


def _fixed_row(month: str) -> FixedRamadanMonth | None:
    return FixedRamadanMonth.objects.select_for_update().filter(month=month).first()


@router.get("/ramadan", response=list[RamadanMonth], operation_id="listRamadanMonths")
def list_ramadan_months(request, filters: Query[YearFilter]) -> list[dict]:
    """Every month of the year: the sighting can put a day in a month the calendar gives none."""
    fixed = dict(FixedRamadanMonth.objects.filter(month__startswith=f"{filters.year}-").values_list("month", "dates"))
    months = [f"{filters.year}-{number:02d}" for number in range(1, 13)]
    return [_ramadan_month(month, fixed.get(month)) for month in months]


def _ramadan_changed(request, month: str, before: dict, after: dict) -> None:
    """Tells the managers with a roster in the month when its Ramadan days moved. Fixing a month to the calendar's
    own days, or returning one so fixed to the calendar, is audited, since who decides changed, but moves no day."""
    if before["dates"] != after["dates"]:
        month_dates_changed([dt.date.fromisoformat(f"{month}-01")], MonthDates.RAMADAN_DAYS, request.user)


def _fix_month(request, month: str, dates: list[dt.date]) -> dict:
    """The one way a month's dates are fixed, by hand or by confirming a Ramadan: under the row's lock, audited only
    when what is in force changes, and its managers told when its days do. The caller holds the transaction."""
    row = _fixed_row(month)
    before = _ramadan_month(month, row.dates if row else None)
    after = _ramadan_month(month, dates)
    if _audited_ramadan(before) != _audited_ramadan(after):
        FixedRamadanMonth.objects.update_or_create(month=month, defaults={"dates": dates})
        record("ramadan_dates.changed", actor=request.user, target=month, before=_audited_ramadan(before),
               after=_audited_ramadan(after), request=request)
        _ramadan_changed(request, month, before, after)
    return after


@router.put("/ramadan/{month}", response=RamadanMonth, auth=cno, operation_id="fixRamadanMonth")
def fix_ramadan_month(request, month: Month, body: RamadanFix) -> dict:
    dates = _fixed_dates(_month_within_the_calendar(month), body)
    with transaction.atomic():
        return _fix_month(request, month, dates)


@router.delete("/ramadan/{month}", response=RamadanMonth, auth=cno, operation_id="unfixRamadanMonth")
def unfix_ramadan_month(request, month: Month) -> dict:
    """Back to the calendar."""
    _month_within_the_calendar(month)
    after = _ramadan_month(month, None)
    with transaction.atomic():
        row = _fixed_row(month)
        if row:
            before = _ramadan_month(month, row.dates)
            row.delete()
            record("ramadan_dates.changed", actor=request.user, target=month, before=_audited_ramadan(before),
                   after=_audited_ramadan(after), request=request)
            _ramadan_changed(request, month, before, after)
    return after


# ---- a whole Ramadan: the cards ------------------------------------------------------------------
# A Ramadan is 29 or 30 days. The bound only catches a slip such as a wrong year or month, never the sighting.
LONGEST_RAMADAN = 35


class RamadanCard(Schema):
    hijri_year: int = Field(description="The Ramadan's name: Ramadan 1448.")
    first: dt.date | None = Field(description="The first of the days in force; none when its months are fixed to none.")
    last: dt.date | None
    days: int = Field(description="How many days are in force, which a list entered by hand may leave with gaps.")
    source: Literal["derived", "fixed"] = Field(
        description="Derived when every month it touches follows the calendar; fixed when the CNO fixed any of them.")
    calendar_first: dt.date = Field(description="The Umm al-Qura calendar's first day, whatever is in force.")
    calendar_last: dt.date


class RamadanConfirm(StrictSchema):
    first: IsoDate = Field(description="The first day of Ramadan, from the moon sighting.")
    last: IsoDate = Field(description="Its last day.")


def _card_months(ramadan: CalendarRamadan, fixed: dict[str, list[dt.date]]) -> list[str]:
    """The months the calendar's Ramadan touches, and the months either side whose fixed days continue it over the edge:
    a sighting a day or two off, or a confirmation of up to 35 days from a Ramadan inside one month, which can reach two
    months out. Another Ramadan is eleven months away, so they cannot be its days."""
    months = list(ramadan.months)
    for _ in range(2):
        before = shift_month(months[0], -1)
        if not (fixed.get(before) and month_of(max(fixed[before]) + dt.timedelta(days=1)) == months[0]):
            break
        months.insert(0, before)
    for _ in range(2):
        after = shift_month(months[-1], 1)
        if not (fixed.get(after) and min(fixed[after]).day == 1):
            break
        months.append(after)
    return months


def _days_in_force(ramadan: CalendarRamadan, fixed: dict[str, list[dt.date]]) -> list[dt.date]:
    """In order. A list entered by hand may leave gaps, so the nth day is the nth of these, not the first plus n - 1."""
    return sorted(day for month in _card_months(ramadan, fixed)
                  for day in (fixed[month] if month in fixed else derived_ramadan_dates(month)))


def _card(ramadan: CalendarRamadan, fixed: dict[str, list[dt.date]]) -> dict:
    months = _card_months(ramadan, fixed)
    days = _days_in_force(ramadan, fixed)
    return {"hijri_year": ramadan.hijri_year, "first": days[0] if days else None, "last": days[-1] if days else None,
            "days": len(days), "source": "fixed" if any(month in fixed for month in months) else "derived",
            "calendar_first": ramadan.days[0], "calendar_last": ramadan.days[-1]}


def _fixed_around(year: int) -> dict[str, list[dt.date]]:
    """The fixed months a card of `year` can read: October before to March after."""
    months = [shift_month(f"{year:04d}-01", step) for step in range(-3, 15)]
    return dict(FixedRamadanMonth.objects.filter(month__in=months).values_list("month", "dates"))


@router.get("/ramadans", response=list[RamadanCard], auth=cno, operation_id="listRamadans")
def list_ramadans(request, filters: Query[YearFilter]) -> list[dict]:
    """Every Ramadan with a day in the year, by the calendar's dates; one across New Year is on both years."""
    fixed = _fixed_around(filters.year)
    return [_card(ramadan, fixed) for ramadan in calendar_ramadans(filters.year)]


@router.put("/ramadans/{hijri_year}", response=RamadanCard, auth=cno, operation_id="confirmRamadan")
def confirm_ramadan(request, hijri_year: int, body: RamadanConfirm) -> dict:
    """Fixes every month from the first day's to the last day's to the confirmed days in it, exactly as fixing each by
    hand would. A month the card read days from that the range no longer touches is fixed to none, so the calendar's
    days, or an earlier confirmation's, do not survive beside this one."""
    if body.last < body.first:
        refuse((("last",), "must not be before the first day"))
    length = (body.last - body.first).days + 1
    if length > LONGEST_RAMADAN:
        refuse((("last",), f"That makes {length} days. A Ramadan has 29 or 30, so at most {LONGEST_RAMADAN} are taken."))
    ramadan = calendar_ramadan(hijri_year)
    if ramadan is None or not FIRST_YEAR <= ramadan.days[0].year <= LAST_YEAR:
        raise HttpError(404, f"The calendar has no Ramadan {hijri_year} between {FIRST_YEAR} and {LAST_YEAR}.")
    if body.last < ramadan.days[0] or body.first > ramadan.days[-1]:
        # A wrong year or month, which would otherwise empty this Ramadan's months and overwrite another's.
        first, last = ramadan.days[0], ramadan.days[-1]
        calendar = f"{first.day} {first:%B}{f' {first.year}' if first.year != last.year else ''} – {last.day} {last:%B} {last.year}"
        refuse((("first",), f"These days do not touch Ramadan {hijri_year}, which the calendar puts at {calendar}."))
    confirmed = [body.first + dt.timedelta(days=offset) for offset in range(length)]
    with transaction.atomic():
        fixed = _fixed_around(ramadan.days[0].year)
        months = sorted(set(_card_months(ramadan, fixed)) | {month_of(day) for day in confirmed})
        for month in months:  # In order, so two confirmations lock the rows in the same order.
            _fix_month(request, month, [day for day in confirmed if month_of(day) == month])
        return _card(ramadan, _fixed_around(ramadan.days[0].year))


# ---- the holidays card: what the calendar proposes ------------------------------------------------
# The Labour Law's implementing regulation: Eid al-Fitr four days from the day after 29 Ramadan, Eid al-Adha four days
# from Arafat Day. The public sector's dates come by circular each year, so the CNO may change both before adding.
EID_DAYS = 4
LONGEST_HOLIDAY = 14
FIXED_HOLIDAYS = {"Founding Day": (2, 22), "National Day": (9, 23)}


class HolidayProposal(Schema):
    name: str
    first: dt.date = Field(description="Shown while this day is not a public holiday.")
    length: int = Field(description="How many days, from the first.")


class HolidayAdd(StrictSchema):
    name: Name
    first: IsoDate = Field(description="A day in the year asked for.")
    length: int = Field(ge=1, le=LONGEST_HOLIDAY, description="Days running into January are added in the next year.")


class HolidaysAdded(Schema):
    added: list[dt.date]
    skipped: list[dt.date] = Field(description="Holidays already, so left as they were.")


def _eid_al_fitr_days(year: int) -> list[dt.date]:
    """The day after each 29th Ramadan day in force that falls in `year`. A Ramadan of the year before can end on 31
    December, so its Ramadans are read too; one with fewer than 29 days in force has no Eid here."""
    ramadans = {ramadan.hijri_year: ramadan
                for of_year in range(max(year - 1, FIRST_YEAR), year + 1) for ramadan in calendar_ramadans(of_year)}
    days = []
    for ramadan in ramadans.values():
        in_force = _days_in_force(ramadan, _fixed_around(ramadan.days[0].year))
        if len(in_force) >= 29 and (eid := in_force[28] + dt.timedelta(days=1)).year == year:
            days.append(eid)
    return days


@router.get("/holiday-proposals", response=list[HolidayProposal], auth=cno, operation_id="listHolidayProposals")
def list_holiday_proposals(request, filters: Query[YearFilter]) -> list[HolidayProposal]:
    """The Eids and the national days of the year, in date order, each while its first day is not a public holiday.
    Nothing is stored for one: a proposal nobody adds stays."""
    year = filters.year
    proposals = ([HolidayProposal(name="Eid al-Fitr", first=day, length=EID_DAYS) for day in _eid_al_fitr_days(year)]
                 + [HolidayProposal(name="Eid al-Adha", first=day, length=EID_DAYS) for day in calendar_arafat_days(year)]
                 + [HolidayProposal(name=name, first=dt.date(year, month, day), length=1)
                    for name, (month, day) in FIXED_HOLIDAYS.items()])
    entered = set(PublicHoliday.objects.filter(date__year=year).values_list("date", flat=True))
    return sorted((proposal for proposal in proposals if proposal.first not in entered),
                  key=lambda proposal: proposal.first)


@router.post("/holiday-proposals", response=HolidaysAdded, auth=cno, operation_id="addProposedHolidays")
def add_proposed_holidays(request, filters: Query[YearFilter], body: HolidayAdd) -> dict:
    """One public holiday a day under the name, through the holiday create, so each reads and is audited as one entered
    by hand. A day that is a holiday already is skipped, not refused."""
    if body.first.year != filters.year:
        refuse((("first",), f"This is not in {filters.year}. Go to that year to add it."))
    days = [body.first + dt.timedelta(days=offset) for offset in range(body.length)]
    if days[-1].year > LAST_YEAR:
        refuse((("length",), f"This runs past {LAST_YEAR}, the last year the calendar covers."))
    added, skipped = [], []
    with transaction.atomic():
        entered = set(PublicHoliday.objects.filter(date__in=days).values_list("date", flat=True))
        for day in days:
            if day in entered:
                skipped.append(day)
                continue
            try:
                _create_holiday(request, day, body.name)
            except ValidationError:  # Entered by someone else in the meantime: a holiday already, so skipped.
                skipped.append(day)
            else:
                added.append(day)
        if added:  # Once for them all: a four-day Eid tells a manager once a month, not once a day.
            _holidays_changed(request, *added)
    return {"added": added, "skipped": skipped}
