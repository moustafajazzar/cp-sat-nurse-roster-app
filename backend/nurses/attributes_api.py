"""The rostering attributes and opening values of a unit's nurses: its manager sets them, the CNO and the Deputy CNO read
them, and nobody else does. Gender aside, which the CNO sets (`nurses.api`). The System Admin reads the audit log and may
see no attribute, so an entry here says only whose changed. A field's name would say too much: a change to `breastfeeding`
on a nurse who was at the default can mean only one thing. Her probation is the exception: its length, extension
and end are logged with their values."""
from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal
from uuid import UUID

from django.db import transaction
from ninja import Query, Router, Schema
from ninja.errors import HttpError
from nurse_roster.problem import MAX_HOURS, MAX_PARAM
from pydantic import AfterValidator, Field

from accounts.authz import SignedIn
from accounts.models import Role, User
from audit.log import record
from closes.chain import has_closed
from hospital.api import StrictSchema, differing, refuse
from hospital.models import HospitalSettings
from notifications import sending
from nurses import membership, preceptors, probation
from nurses.api import Body, MonthText, Probation, probation_of, with_units
from nurses.models import Gender, NurseProfile
from nurses.skills import NurseSkillRow, skills_of
from rosters.pins import outside_allowed, words_for
from units.models import Unit
from units.rules import distinct_shifts
from units.scope import HOSPITAL_WIDE, unit_in_scope

router = Router(tags=["nurses"])
manager = SignedIn(Role.UNIT_MANAGER)

Balance = Annotated[float, Field(ge=-MAX_PARAM, le=MAX_PARAM, allow_inf_nan=False)]
# The solver bounds a nurse's overtime balance as it bounds an hours balance (`nurse_roster.request`), not as a shift balance.
OvertimeBalance = Annotated[float, Field(ge=-MAX_HOURS, le=MAX_HOURS, allow_inf_nan=False)]


AllowedShifts = Annotated[list[str], Field(min_length=1), AfterValidator(distinct_shifts)] | None


class AllowedShiftsBody(StrictSchema):
    allowed_shifts: AllowedShifts = Field(description="Empty for any shift the unit works, now or later.")


class AttributesBody(AllowedShiftsBody):
    team_leader: bool
    score: float = Field(ge=0, le=1, allow_inf_nan=False)
    ramadan_reduced: bool
    breastfeeding: bool
    ytd_overtime: int = Field(ge=0, le=MAX_HOURS, description="Overtime hours this year.")
    hours_balance: int = Field(ge=-MAX_HOURS, le=MAX_HOURS, description="Hours still owed (+) or worked ahead (-).")
    night_balance: Balance = Field(description="Night shifts above (+) or below (-) their fair share.")
    friday_balance: Balance = Field(description="Weekend days above (+) or below (-) their fair share.")
    overtime_balance: OvertimeBalance = Field(description="Hours of overtime above (+) or below (-) their overtime share so far "
                                                       "this year.")


class LengthChoice(Schema):
    months: int
    last_day: dt.date


class PreceptorOnTheRow(Schema):
    hospital_id: str
    full_name: str
    from_month: str = Field(description="YYYY-MM: the first month this nurse precepts her, or this month for one under way.")
    to_month: str = Field(description="YYYY-MM: the last, with her probation's last month or a nurse's last month in the unit.")


class ProbationOnTheRow(Probation):
    """Her probation as her row shows it, with what each of her unit manager's actions would make its last day, worked out here
    on the hospital's clock. An action that would be refused has none."""
    lengths: list[LengthChoice] = Field(description="The lengths Length may choose, with the last day each gives.")
    if_extended: dt.date | None = Field(description="The last day Extend would give.")
    if_ended: dt.date | None = Field(description="The last day End would give: the last of the month under way.")
    preceptors: list[PreceptorOnTheRow] = Field(description="Who precepts her from this month on, in month order. Empty when no "
                                                            "one does.")
    preceptor_months: list[str] = Field(description="YYYY-MM: the months her unit manager may choose a preceptor from: from "
                                                    "this month on, while she is on probation in this unit, and not one whose "
                                                    "roster is approved.")


def _preceptors_on_the_row(nurse: NurseProfile) -> list[PreceptorOnTheRow]:
    return [PreceptorOnTheRow(hospital_id=span.assignment.preceptor.user.username,
                              full_name=preceptors.name_of(span.assignment.preceptor),
                              from_month=span.first.strftime("%Y-%m"), to_month=span.last.strftime("%Y-%m"))
            for span in preceptors.spans(nurse, membership.this_month())]


def _probation_on_the_row(nurse: NurseProfile, unit: Unit) -> ProbationOnTheRow | None:
    read = probation_of(nurse)
    if read is None:
        return None
    month = membership.this_month()
    open_to_change = read.ticked and not read.ended_early
    lengths = [LengthChoice(months=months, last_day=last) for months in probation.FIRST_LENGTHS
               if (last := probation.last_day_after(nurse.start_date, months)) >= month] \
        if open_to_change and read.months <= probation.FIRST_LENGTHS[-1] else []
    extended = probation.last_day_after(nurse.start_date, read.months + 1) \
        if open_to_change and read.months < probation.LONGEST else None
    ended = membership.last_day_of(month) if open_to_change and month >= nurse.start_date.replace(day=1) else None
    return ProbationOnTheRow(**read.model_dump(), lengths=lengths, if_extended=extended, if_ended=ended,
                             preceptors=_preceptors_on_the_row(nurse),
                             preceptor_months=[month.strftime("%Y-%m") for month in preceptors.months_to_choose(nurse, unit)]
                             if read.ticked else [])


class NurseAttributesRow(Schema):
    id: UUID
    hospital_id: str
    full_name: str
    is_active: bool
    gender: Literal[Gender.FEMALE, Gender.MALE] = Field(description="The CNO's to set: it is here to read.")
    joins_from: str | None = Field(description="YYYY-MM, for a nurse due to join. Until then only their allowed shifts "
                                               "here are this unit's to set.")
    leaves_from: str | None = Field(description="YYYY-MM, for a nurse due to leave. Names no other unit.")
    team_leader: bool
    score: float
    ramadan_reduced: bool
    breastfeeding: bool
    ytd_overtime: int
    hours_balance: int
    night_balance: float
    friday_balance: float
    overtime_balance: float
    allowed_shifts: list[str] | None = Field(description="In this unit. Empty for any shift it works.")
    allowed_shifts_to_check: bool = Field(description="A transfer emptied the list, as none of its shifts are worked here.")
    opening_values_locked: bool = Field(description="Their first month has closed: the opening values (`ytd_overtime`, "
                                                    "`hours_balance`, `night_balance`, `friday_balance`, "
                                                    "`overtime_balance`) can no longer change, "
                                                    "as the carry-over comes from the closes now.")
    skills: list[NurseSkillRow] = Field(description="Their current skills, to read: the CNO records them on the "
                                                     "Nurses page, the Deputy CNO on this page. Their "
                                                     "details for the CNO and the Deputy CNO only.")
    probation: ProbationOnTheRow | None = Field(description="Empty when she is not on probation, or it is over. Her unit manager sets "
                                                    "its length, extends it and ends it; the CNO ticks it.")


def _row(nurse: NurseProfile, unit: Unit, *, details: bool = False) -> NurseAttributesRow:
    """`details`: the CNO and the Deputy CNO read her records' identifiers and issue dates; her unit's manager does not."""
    current, *coming = membership.from_now_on(nurse)
    here = membership.here_from_now_on(nurse, unit)[0]
    joins = here.from_month if current.unit_id != unit.pk else None
    leaves = next((placement.from_month for placement in coming if placement.unit_id != unit.pk), None) if not joins else None
    user = nurse.user
    return NurseAttributesRow(
        id=nurse.pk, hospital_id=user.username, full_name=user.full_name, is_active=user.is_active, gender=nurse.gender,
        joins_from=joins.strftime("%Y-%m") if joins else None, leaves_from=leaves.strftime("%Y-%m") if leaves else None,
        team_leader=nurse.team_leader, score=nurse.score, ramadan_reduced=nurse.ramadan_reduced,
        breastfeeding=nurse.breastfeeding, ytd_overtime=nurse.ytd_overtime, hours_balance=nurse.hours_balance,
        night_balance=nurse.night_balance, friday_balance=nurse.friday_balance, overtime_balance=nurse.overtime_balance,
        allowed_shifts=here.allowed_shifts, allowed_shifts_to_check=here.allowed_shifts_to_check,
        opening_values_locked=has_closed(nurse), skills=skills_of(nurse, details=details),
        probation=_probation_on_the_row(nurse, unit))


OPENING_VALUES = ("ytd_overtime", "hours_balance", "night_balance", "friday_balance", "overtime_balance")


def _refuse_opening_values(nurse: NurseProfile, changing: dict) -> None:
    """Editable until her first month closes. They travel in one body with the attributes, so a save that leaves them
    as they are goes through, and only a field that changes is refused."""
    locked = [name for name in OPENING_VALUES if name in changing]
    if locked and has_closed(nurse):
        refuse(*[((name,), f"{nurse.user.full_name}'s first month has closed, so their opening values can no longer change: "
                           "each month now starts from the carry-over of the one before.") for name in locked])


def _row_as_saved(nurse_id: UUID, unit: Unit) -> NurseAttributesRow:
    return _row(with_units(NurseProfile.objects).get(pk=nurse_id), unit)


def _nurse_to_change(user: User, unit_id: UUID, nurse_id: UUID) -> tuple[Unit, NurseProfile]:
    """The same 403 whether another unit's nurse exists or not. The nurse's lock comes before the check, because a transfer
    and an end date change who is on this list under it, and the account's with it, as they take them."""
    unit = unit_in_scope(user, unit_id)
    NurseProfile.objects.select_for_update(of=("self", "user"), no_key=True).select_related("user").filter(pk=nurse_id).first()
    if not membership.on_the_list_of(unit).filter(pk=nurse_id).exists():
        raise HttpError(403, "This nurse is not in your unit.")
    return unit, with_units(NurseProfile.objects).get(pk=nurse_id)


def _refuse_shifts_outside(unit: Unit, nurse: NurseProfile, allowed: list[str] | None) -> None:
    refused = [(("allowed_shifts", index), f"{code} is not one of this unit's shifts.")
               for index, code in enumerate(allowed or []) if code not in unit.rules["shifts"]]
    if refused:
        refuse(*refused)
    if probation.is_night_alone(allowed, unit) and probation.running(nurse):
        refuse((("allowed_shifts",), f"{nurse.user.full_name} is on probation, and a nurse on probation does not work "
                                     f"{allowed[0]}, the night shift, so it alone would leave her no shift."))
    pinned = outside_allowed(nurse, unit, allowed)
    if pinned:
        refuse((("allowed_shifts",), f"A pin holds a shift this leaves out: {words_for(pinned)}. "
                                     "Clear it on the Roster page first."))


@router.get("/{unit_id}/nurses", response=list[NurseAttributesRow], auth=SignedIn(Role.UNIT_MANAGER, Role.CNO, Role.DEPUTY_CNO),
            operation_id="listNurseAttributes")
def list_nurse_attributes(request, unit_id: UUID) -> list[NurseAttributesRow]:
    """Those placed here now or due to join, deactivated or not, as on the manager's list of nurses."""
    unit = unit_in_scope(request.user, unit_id)
    details = request.user.role in HOSPITAL_WIDE
    return [_row(nurse, unit, details=details) for nurse in with_units(membership.on_the_list_of(unit))]


@router.put("/{unit_id}/nurses/{nurse_id}/attributes", response=NurseAttributesRow, auth=manager,
            operation_id="updateNurseAttributes")
def update_nurse_attributes(request, unit_id: UUID, nurse_id: UUID, body: AttributesBody) -> NurseAttributesRow:
    """A manager with a nurse profile sets their own, because nobody else may and a nurse has to have them. The CNO and the
    Deputy CNO can read the values, and the log shows whose hand changed them. The opening values are refused, on their fields
    alone, once the nurse's first month has closed."""
    with transaction.atomic():
        HospitalSettings.load(for_update=True)
        unit, nurse = _nurse_to_change(request.user, unit_id, nurse_id)
        if membership.unit_id_now(nurse) != unit.pk:
            raise HttpError(403, "Until this nurse joins, their current unit's manager sets everything but their shifts here.")
        _refuse_shifts_outside(unit, nurse, body.allowed_shifts)
        fields = body.model_dump(exclude={"allowed_shifts"})
        _, changing = differing({name: getattr(nurse, name) for name in fields}, fields)
        _refuse_opening_values(nurse, changing)
        for name, value in changing.items():
            setattr(nurse, name, value)
        nurse.attributes_saved = True  # A save that changes nothing still says someone looked.
        nurse.save(update_fields=[*changing, "attributes_saved"])
        shifts_changed = membership.set_allowed_shifts(nurse, unit, body.allowed_shifts)
        if changing or shifts_changed:
            record("nurse.attributes_changed", actor=request.user, target=nurse.user.username, request=request)
    return _row_as_saved(nurse_id, unit)


@router.put("/{unit_id}/nurses/{nurse_id}/allowed-shifts", response=NurseAttributesRow, auth=manager,
            operation_id="updateNurseAllowedShifts")
def update_nurse_allowed_shifts(request, unit_id: UUID, nurse_id: UUID, body: AllowedShiftsBody) -> NurseAttributesRow:
    """All a manager can change of a nurse due to join: a unit may roster a month ahead of their arrival, and this list is
    about this unit's shifts, whereas the rest is about the nurse and stays with the manager of the unit they are in."""
    with transaction.atomic():
        HospitalSettings.load(for_update=True)
        unit, nurse = _nurse_to_change(request.user, unit_id, nurse_id)
        _refuse_shifts_outside(unit, nurse, body.allowed_shifts)
        if membership.set_allowed_shifts(nurse, unit, body.allowed_shifts):
            record("nurse.attributes_changed", actor=request.user, target=nurse.user.username, request=request)
    return _row_as_saved(nurse_id, unit)


# ---- her probation: the unit manager's -----------------------------------------------------------------------------------
class LengthBody(StrictSchema):
    months: int = Field(ge=probation.FIRST_LENGTHS[0], le=probation.FIRST_LENGTHS[-1],
                        description="1, 2 or 3, while it stands at 3 or less. Beyond, it is extended a month at a time.")


def _probation_to_change(request, unit_id: UUID, nurse_id: UUID) -> tuple[Unit, NurseProfile]:
    """Her current unit's manager's, as her other attributes are: not a unit she is due to join."""
    unit, nurse = _nurse_to_change(request.user, unit_id, nurse_id)
    if membership.unit_id_now(nurse) != unit.pk:
        raise HttpError(403, "Until this nurse joins, their current unit's manager sets her probation.")
    name = nurse.user.full_name
    if not probation.running(nurse):
        refuse((("probation",), f"{name} is not on probation."))
    if not nurse.on_probation:
        refuse((("probation",), f"The CNO took {name} off probation. It ends on {probation.last_day(nurse):%-d %B %Y}, with the "
                                "months whose rosters were approved, and can no longer change."))
    if probation.ended_early(nurse):
        refuse((("probation",), f"{name}'s probation was ended early, on {probation.last_day(nurse):%-d %B %Y}, "
                                "so it can no longer change."))
    return unit, nurse


def _save_probation(request, nurse: NurseProfile, action: str, was: dict) -> None:
    nurse.save(update_fields=["probation_months", "probation_ended_in"])
    record(action, actor=request.user, target=nurse.user.username, request=request, before=was,
           after=probation.describe(nurse))
    tell_released(nurse, dt.date.fromisoformat(was["last_day"]) if was["last_day"] else None)


def tell_released(nurse: NurseProfile, was_last: dt.date | None) -> None:
    """A probation now ending sooner ends her preceptor's assignment with it: each nurse who precepted her in a month it gave
    up is told, once, under that assignment's unit. One that runs out by itself tells nobody: nothing was done, and
    the row says it."""
    for assignment, first in preceptors.released_by_ending(nurse, was_last):
        sending.preceptors_released(assignment.unit, nurse, first, [assignment.preceptor])


@router.put("/{unit_id}/nurses/{nurse_id}/probation/length", response=NurseAttributesRow, auth=manager,
            operation_id="setProbationLength")
def set_probation_length(request, unit_id: UUID, nurse_id: UUID, body: LengthBody) -> NurseAttributesRow:
    """Refused once extended beyond 3, and for a length that would have ended it before this month: End does that."""
    with transaction.atomic():
        unit, nurse = _probation_to_change(request, unit_id, nurse_id)
        name = nurse.user.full_name
        if nurse.probation_months > probation.FIRST_LENGTHS[-1]:
            refuse((("months",), f"{name}'s probation is extended to {nurse.probation_months} months. Extend it again or "
                                 "end it."))
        last = probation.last_day_after(nurse.start_date, body.months)
        if last < membership.this_month():
            refuse((("months",), f"{body.months} month{'' if body.months == 1 else 's'} would have ended {name}'s probation on "
                                 f"{last:%-d %B %Y}, before this month. End it instead, at the end of this month."))
        if body.months != nurse.probation_months:
            clash = preceptors.clash_if_lasting(nurse, body.months)
            if clash:
                refuse((("months",), clash))
            was = probation.describe(nurse)
            nurse.probation_months = body.months
            _save_probation(request, nurse, "nurse.probation_length_set", was)
    return _row_as_saved(nurse_id, unit)


@router.post("/{unit_id}/nurses/{nurse_id}/probation/extend", response=NurseAttributesRow, auth=manager,
             operation_id="extendProbation")
def extend_probation(request, unit_id: UUID, nurse_id: UUID) -> NurseAttributesRow:
    """One month at a time, to 6 at most."""
    with transaction.atomic():
        unit, nurse = _probation_to_change(request, unit_id, nurse_id)
        if nurse.probation_months >= probation.LONGEST:
            refuse((("probation",), f"{nurse.user.full_name}'s probation is {probation.LONGEST} months, the longest it can be."))
        clash = preceptors.clash_if_lasting(nurse, nurse.probation_months + 1)
        if clash:
            refuse((("probation",), clash))
        was = probation.describe(nurse)
        nurse.probation_months += 1
        _save_probation(request, nurse, "nurse.probation_extended", was)
    return _row_as_saved(nurse_id, unit)


@router.post("/{unit_id}/nurses/{nurse_id}/probation/end", response=NurseAttributesRow, auth=manager,
             operation_id="endProbation")
def end_probation(request, unit_id: UUID, nurse_id: UUID) -> NurseAttributesRow:
    """Its last day becomes the last day of the month under way on the hospital's clock, so that month keeps its roster. Refused
    before the month she starts in, when there is no month of it under way: the CNO unticks it."""
    with transaction.atomic():
        unit, nurse = _probation_to_change(request, unit_id, nurse_id)
        month = membership.this_month()
        if month < nurse.start_date.replace(day=1):
            refuse((("probation",), f"{nurse.user.full_name}'s probation begins on her start date, "
                                    f"{nurse.start_date:%-d %B %Y}. If she is not to be on probation, the CNO unticks it."))
        was = probation.describe(nurse)
        nurse.probation_ended_in = month
        _save_probation(request, nurse, "nurse.probation_ended", was)
    return _row_as_saved(nurse_id, unit)


# ---- her preceptor: the unit manager's ------------------------------------------------------------------------
class PreceptorChoice(Schema):
    id: UUID = Field(description="The nurse profile's.")
    hospital_id: str
    full_name: str


class PreceptorChoices(Schema):
    month: str = Field(description="YYYY-MM: the month the choice would take effect from.")
    refusal: str | None = Field(description="Why no preceptor can be chosen from that month.")
    nurses: list[PreceptorChoice] = Field(description="The unit's nurses that month who hold a skill marked \"Lets a nurse "
                                                      "precept\", unexpired today, are not on probation in her months from then "
                                                      "and precept no other nurse in any of them. Empty with a refusal.")


class PreceptorMonth(Schema):
    # Optional as every month filter is, since a required one carries its `examples` where the client generator refuses them.
    month: MonthText | None = Field(None, description="YYYY-MM: the month the choice would take effect from. Required.")


class PreceptorBody(Body):
    preceptor_id: UUID = Field(description="The nurse profile's.")
    from_month: MonthText


def _preceptor_to_change(request, unit_id: UUID, nurse_id: UUID) -> tuple[Unit, NurseProfile]:
    """The manager of the unit she is in, now or due to join: the new unit's manager chooses her preceptor there."""
    unit, nurse = _nurse_to_change(request.user, unit_id, nurse_id)
    if not probation.running(nurse):
        refuse((("probation",), f"{nurse.user.full_name} is not on probation."))
    if not nurse.on_probation:
        refuse((("probation",), f"The CNO took {nurse.user.full_name} off probation, so she needs no preceptor."))
    return unit, nurse


@router.get("/{unit_id}/nurses/{nurse_id}/preceptor-choices", response=PreceptorChoices, auth=manager,
            operation_id="listPreceptorChoices")
def list_preceptor_choices(request, unit_id: UUID, nurse_id: UUID, filters: Query[PreceptorMonth]) -> PreceptorChoices:
    """The list Change preceptor offers, from a month on. Read with no lock: the save checks it all again."""
    unit = unit_in_scope(request.user, unit_id)
    nurse = with_units(membership.on_the_list_of(unit)).filter(pk=nurse_id).first()
    if nurse is None:
        raise HttpError(403, "This nurse is not in your unit.")
    if filters.month is None:
        refuse((("month",), "Choose the month the preceptor would take effect from."))
    month = filters.month
    chosen = dt.date.fromisoformat(f"{month}-01")
    refusal = None if probation.running(nurse) and nurse.on_probation else f"{nurse.user.full_name} is not on probation."
    refusal = refusal or preceptors.month_refusal(nurse, unit, chosen)
    offered = [] if refusal else preceptors.candidates(nurse, unit, chosen)
    if not refusal and not offered:
        refusal = (f"No nurse of {unit.name} can precept {nurse.user.full_name} from {chosen:%B %Y}: none holds a skill marked "
                   "\"Lets a nurse precept\", unexpired today, without being on probation or precepting another nurse then.")
    return PreceptorChoices(month=month, refusal=refusal, nurses=[
        PreceptorChoice(id=candidate.pk, hospital_id=candidate.user.username, full_name=preceptors.name_of(candidate))
        for candidate in offered])


@router.put("/{unit_id}/nurses/{nurse_id}/preceptor", response=NurseAttributesRow, auth=manager,
            operation_id="setPreceptor")
def set_preceptor(request, unit_id: UUID, nurse_id: UUID, body: PreceptorBody) -> NurseAttributesRow:
    """Assigns or changes her preceptor from a month on. Both nurses are told, and a nurse who precepted her then and no longer
    does; each assignment is logged with its values before and after, as her probation is."""
    month = dt.date.fromisoformat(f"{body.from_month}-01")
    with transaction.atomic():
        unit, nurse = _preceptor_to_change(request, unit_id, nurse_id)
        # The candidate's row too, in pk order with hers, so two managers cannot give one preceptor two nurses at once.
        locked = NurseProfile.objects.select_for_update(no_key=True).filter(pk__in=[nurse.pk, body.preceptor_id]).order_by("pk")
        list(locked.values_list("pk", flat=True))
        refusal = preceptors.month_refusal(nurse, unit, month)
        if refusal:
            refuse((("from_month",), refusal))
        candidate = NurseProfile.objects.select_related("user").filter(pk=body.preceptor_id).first()
        if candidate is None:
            refuse((("preceptor_id",), "There is no such nurse."))
        why = preceptors.why_not(nurse, unit, candidate, month)
        if why:
            refuse((("preceptor_id",), why))
        gained = preceptors.first_gained(nurse, candidate, unit, month)
        if gained is None:
            return _row_as_saved(nurse_id, unit)  # She is theirs in every month they can be: nothing to change or tell.
        held = preceptors.assignment_in(nurse, month)
        before = preceptors.preceptors_from(nurse, gained, unit)
        was = preceptors.describe(held)
        assigned, replaced = preceptors.assign(nurse, unit, candidate, month, request.user)
        record("nurse.preceptor_assigned", actor=request.user, target=nurse.user.username, request=request,
               before=was | ({"replaced": replaced} if replaced else {}), after=preceptors.describe(assigned))
        released = NurseProfile.objects.select_related("user").filter(pk__in=before - {candidate.pk}).order_by("pk")
        # Once each, from the first month that changes: a nurse already hers then is told only of what changed for them.
        sending.preceptor_assigned(unit, nurse, gained, preceptor=candidate, released=list(released))
    return _row_as_saved(nurse_id, unit)
