"""The CNO's nurses, and a unit manager's view of the nurses of their unit. Who belongs where is
`nurses.membership`; the account behind a nurse is `accounts.provisioning`."""
from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal
from uuid import UUID

from django.db import transaction
from django.db.models.functions import Lower
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from ninja import Field, Router, Schema, Status
from ninja.errors import HttpError
from pydantic import AfterValidator, ConfigDict

from accounts.api import FullName, Username
from accounts.authz import SignedIn
from accounts.models import Role, User
from accounts.provisioning import UsernameTaken, create_user, deactivate, reactivate, reset_password
from audit.log import record
from closes import making
from hospital.api import differing, refuse
from hospital.catalogue_api import MONTH_PATTERN, IsoDate, iso_date
from hospital.models import HospitalSettings
from nurses import membership, preceptors, probation
from nurses.models import Gender, NurseProfile
from nurses.skills import NurseSkillRow, current_skills, skills_of
from units.models import Unit
from units.scope import unit_in_scope, units_in_scope

router = Router(tags=["nurses"])
cno = SignedIn(Role.CNO)

GenderChoice = Literal[Gender.FEMALE, Gender.MALE]


class Body(Schema):
    # Not StrictSchema, whose strict mode would refuse every id, because JSON has only strings.
    model_config = ConfigDict(extra="forbid")


class ProfileFields(Body):
    gender: GenderChoice
    unit_id: UUID
    start_date: IsoDate
    on_probation: bool = Field(False, description="A staff nurse's only: refused for a unit manager's nurse profile.")


class NewNurseBody(ProfileFields):
    hospital_id: Username
    full_name: FullName


class ManagerProfileBody(ProfileFields):
    user_id: UUID


def _month_in_range(value: str) -> str:
    iso_date(f"{value}-01")  # Year 0 and the like: the same limits as any other date here.
    return value


MonthText = Annotated[str, Field(pattern=MONTH_PATTERN, examples=["2027-01"]), AfterValidator(_month_in_range)]


class TransferBody(Body):
    unit_id: UUID
    from_month: MonthText


class EndDateBody(Body):
    end_date: IsoDate | None


class CorrectionBody(Body):
    """The hospital ID is not here: it is the username, and usernames cannot be reused. Leaving out the name leaves it
    as it is, which is what a manager's form does: the System Admin may have corrected it since the form opened."""
    full_name: FullName | None = None
    gender: GenderChoice
    start_date: IsoDate
    on_probation: bool | None = Field(None, description="Leaving it out leaves it as it is. Unticking removes the probation from "
                                                        "the first month whose roster is not approved; ticking is refused once "
                                                        "six months from her start date have passed.")


class Probation(Schema):
    """A probation not over yet: under way, or to begin on her start date."""
    last_day: dt.date = Field(description="Derived from her start date, the length and an early end, never entered.")
    months: int = Field(description="Whole calendar months from her start date, a month she joins after the 1st not counted.")
    ticked: bool = Field(description="The CNO's tick. False for one unticked whose months up to `last_day` had approved rosters.")
    ended_early: bool = Field(description="Her unit manager pressed End, so it can no longer be extended.")


def probation_of(nurse: NurseProfile) -> Probation | None:
    if not probation.running(nurse):
        return None
    return Probation(last_day=probation.last_day(nurse), months=nurse.probation_months, ticked=nurse.on_probation,
                     ended_early=probation.ended_early(nurse))


class UnitName(Schema):
    id: UUID
    name: str


class ComingTransfer(Schema):
    from_month: str = Field(description="YYYY-MM.")
    unit: UnitName | None = Field(description="Empty when the unit is not the caller's: a manager sees nothing of others.")


class NurseRow(Schema):
    id: UUID
    hospital_id: str
    full_name: str
    role: Role
    gender: GenderChoice
    start_date: dt.date
    end_date: dt.date | None
    is_active: bool
    must_change_password: bool
    unit: UnitName | None = Field(description="The unit this month, empty when it is not the caller's: a nurse due to "
                                              "join a manager's unit is on their list before they arrive.")
    transfers: list[ComingTransfer] = Field(description="The transfers still to come, soonest first.")
    skills: list[NurseSkillRow] = Field(description="Their current skills, the CNO's to set, in the order of their names; "
                                                     "their details for the CNO only.")
    on_probation: bool = Field(description="The CNO's tick as it stands, also once her probation is over, for the correction form.")
    probation: Probation | None = Field(description="Empty when she is not on probation, or it is over.")


class NurseWithPassword(Schema):
    """Holds a password, so it asks not to be stored."""
    nurse: NurseRow
    generated_password: str


class FirstPlacement(Schema):
    unit: UnitName
    from_month: str = Field(description="YYYY-MM.")


class CorrectedNurse(Schema):
    nurse: NurseRow
    first_placement_extended: FirstPlacement | None = Field(
        description="Set when the start date went before the nurse's first placement, which now begins earlier.")


class ManagerToPlace(Schema):
    id: UUID
    username: str
    full_name: str
    unit: UnitName | None = Field(description="The unit they hold, which their nurse profile has to be in.")


def _month(day: dt.date) -> str:
    return day.strftime("%Y-%m")


def _row(nurse: NurseProfile, only_unit: UUID | None = None) -> NurseRow:
    """`only_unit` is the one unit the caller may name, a unit manager's, or None for the CNO, who may name them all and
    alone reads her records' identifiers and issue dates here."""
    def named(unit: Unit) -> UnitName | None:
        return UnitName(id=unit.pk, name=unit.name) if only_unit in (None, unit.pk) else None

    current, *coming = membership.from_now_on(nurse)
    user = nurse.user
    return NurseRow(id=nurse.pk, hospital_id=user.username, full_name=user.full_name, role=user.role,
                    gender=nurse.gender, start_date=nurse.start_date, end_date=nurse.end_date,
                    is_active=user.is_active, must_change_password=user.must_change_password,
                    unit=named(current.unit),
                    transfers=[ComingTransfer(from_month=_month(placement.from_month), unit=named(placement.unit))
                               for placement in coming],
                    skills=skills_of(nurse, details=only_unit is None), on_probation=nurse.on_probation,
                    probation=probation_of(nurse))


def with_units(nurses):
    return (nurses.select_related("user").prefetch_related("placements__unit", current_skills(),
                                                           "preceptor_assignments__preceptor__user")
            .order_by(Lower("user__full_name"), "pk"))


def _row_as_saved(nurse_id: UUID, only_unit: UUID | None = None) -> NurseRow:
    return _row(with_units(NurseProfile.objects).get(pk=nurse_id), only_unit=only_unit)


def _unit_to_join(unit_id: UUID) -> Unit:
    """Locks the unit's row, because archiving it counts its nurses under the same lock: the two cannot interleave."""
    unit = Unit.objects.select_for_update().filter(pk=unit_id).first()
    if unit is None:
        refuse((("unit_id",), "There is no such unit."))
    if unit.archived:
        refuse((("unit_id",), f"{unit.name} is archived, so no nurse can join it."))
    return unit


def _keep_in_held_unit(user: User, unit: Unit) -> None:
    """A manager reaches only the unit they hold, so their profile has to be in it. See `membership.placed_outside`."""
    held = user.held_unit
    if held is not None and held.pk != unit.pk:
        refuse((("unit_id",), f"{user.username} manages {held.name}, so their nurse profile has to be there. "
                              f"Give {held.name} another manager, or none, first."))


def new_profile(request: HttpRequest, user: User, unit: Unit, gender: str, start_date: dt.date, *,
                on_probation: bool = False) -> NurseProfile:
    nurse = NurseProfile.objects.create(user=user, gender=gender, start_date=start_date, on_probation=on_probation)
    membership.place_new_nurse(nurse, unit)
    record("nurse.added", actor=request.user, target=user.username, request=request,
           after={"unit": unit.name, "start_date": start_date.isoformat()})
    if on_probation:
        record("nurse.probation_ticked", actor=request.user, target=user.username, request=request,
               after=probation.describe(nurse))
    return nurse


ONLY_STAFF_NURSES = "Only a staff nurse can be on probation, not a unit manager with a nurse profile."


def _locked_nurse(nurse_id: UUID) -> NurseProfile:
    """Locks the account with the profile: user's row before the unit's, as everywhere."""
    return get_object_or_404(NurseProfile.objects.select_for_update(of=("self", "user"), no_key=True).select_related("user"),
                             pk=nurse_id)


@router.get("", response=list[NurseRow], auth=SignedIn(Role.CNO, Role.UNIT_MANAGER), operation_id="listNurses")
def list_nurses(request: HttpRequest) -> list[NurseRow]:
    """Everyone for the CNO. For a manager, the nurses of their unit and those due to join, naming no other unit."""
    if request.user.role == Role.CNO:
        return [_row(nurse) for nurse in with_units(NurseProfile.objects.all())]
    unit = units_in_scope(request.user).first()
    if unit is None:
        return []
    return [_row(nurse, only_unit=unit.pk) for nurse in with_units(membership.on_the_list_of(unit))]


@router.post("", response={201: NurseWithPassword}, auth=cno, operation_id="createNurse")
def add_nurse(request: HttpRequest, response: HttpResponse, body: NewNurseBody) -> Status:
    """One transaction: the user, the nurse profile and the placement, or none of them."""
    with transaction.atomic():
        try:
            user, password = create_user(request, username=body.hospital_id, full_name=body.full_name,
                                         role=Role.STAFF_NURSE)
        except UsernameTaken:
            refuse((("hospital_id",), f"{body.hospital_id} is already in use."))
        nurse = new_profile(request, user, _unit_to_join(body.unit_id), body.gender, body.start_date,
                            on_probation=body.on_probation)
    response["Cache-Control"] = "no-store"
    return Status(201, NurseWithPassword(nurse=_row_as_saved(nurse.pk), generated_password=password))


@router.get("/my-unit", response=UnitName, auth=SignedIn(Role.STAFF_NURSE), operation_id="getMyUnit")
def my_unit(request: HttpRequest) -> Unit:
    """All a staff nurse reads of their unit here. Colleagues reach them in the roster, as names and shifts only."""
    unit = units_in_scope(request.user).first()
    if unit is None:
        raise Http404("You have no nurse profile.")
    return unit


@router.get("/managers", response=list[ManagerToPlace], auth=cno, operation_id="listManagersToPlace")
def managers_to_place(request: HttpRequest) -> list[ManagerToPlace]:
    """The active unit managers with no nurse profile yet."""
    managers = (User.objects.filter(role=Role.UNIT_MANAGER, is_active=True, nurse_profile__isnull=True)
                .select_related("managed_unit").order_by("username"))
    return [ManagerToPlace(id=manager.pk, username=manager.username, full_name=manager.full_name,
                           unit=UnitName(id=manager.held_unit.pk, name=manager.held_unit.name) if manager.held_unit else None)
            for manager in managers]


@router.post("/managers", response={201: NurseRow}, auth=cno, operation_id="giveManagerNurseProfile")
def give_manager_a_profile(request: HttpRequest, body: ManagerProfileBody) -> Status:
    """The role and the account stay as they are, so no password here. Their hospital ID is the username they have."""
    if body.on_probation:
        refuse((("on_probation",), ONLY_STAFF_NURSES))
    with transaction.atomic():
        manager = User.objects.select_for_update(no_key=True).filter(pk=body.user_id, role=Role.UNIT_MANAGER, is_active=True).first()
        if manager is None:
            refuse((("user_id",), "This is not an active unit manager."))
        if NurseProfile.objects.filter(user=manager).exists():
            refuse((("user_id",), f"{manager.username} already has a nurse profile."))
        unit = _unit_to_join(body.unit_id)
        _keep_in_held_unit(manager, unit)
        nurse = new_profile(request, manager, unit, body.gender, body.start_date)
    return Status(201, _row_as_saved(nurse.pk))


@router.post("/{nurse_id}/transfer", response=NurseRow, auth=cno, operation_id="transferNurse")
def transfer_nurse(request: HttpRequest, nurse_id: UUID, body: TransferBody) -> NurseRow:
    month = dt.date.fromisoformat(f"{body.from_month}-01")
    with transaction.atomic():
        HospitalSettings.load(for_update=True)  # The catalogue's lock: see `membership.transfer`.
        nurse = _locked_nurse(nurse_id)
        user = nurse.user
        if month < membership.this_month():
            refuse((("from_month",), "That month is over. A transfer takes effect this month or later."))
        unit = _unit_to_join(body.unit_id)
        _keep_in_held_unit(user, unit)
        if nurse.end_date and nurse.end_date >= membership.today() and month > nurse.end_date:
            refuse((("from_month",), f"{user.full_name}'s end date is before that month."))
        was = membership.unit_in(nurse, month)
        if not membership.transfer(nurse, unit, month):
            refuse((("unit_id",), f"{user.full_name} is already in {unit.name} from that month."))
        record("nurse.transferred", actor=request.user, target=user.username, request=request,
               before={"unit": was.name}, after={"unit": unit.name, "from_month": body.from_month})
        preceptors.log_ended(preceptors.end_on_leaving(nurse, month, staying_in=unit), request)
    return _row_as_saved(nurse_id)


@router.put("/{nurse_id}/end-date", response=NurseRow, auth=cno, operation_id="setNurseEndDate")
def set_end_date(request: HttpRequest, nurse_id: UUID, body: EndDateBody) -> NurseRow:
    """A staff nurse's account follows: today or earlier deactivates at once, because ending an account should not
    wait, a later date when it has passed (`nurses.tasks`), and clearing it or moving it on brings them back. A
    manager's account is the System Admin's, so their profile ends alone."""
    end = body.end_date
    today = membership.today()
    with transaction.atomic():
        nurse = _locked_nurse(nurse_id)
        user = nurse.user
        if end == nurse.end_date:
            return _row_as_saved(nurse_id)
        if end is not None and not membership.end_date_fits(nurse.start_date, end):
            refuse((("end_date",), "The end date cannot be before the start date."))
        fixed = _closed_for_good(nurse, nurse.end_date, end, end=True)
        if fixed:
            refuse((("end_date",), fixed))
        before = nurse.end_date
        nurse.end_date = end
        nurse.save(update_fields=["end_date"])
        cancelled = membership.cancel_transfers_after(nurse, end) if end else []
        after = {"end_date": end.isoformat() if end else None}
        if cancelled:
            after["transfers_cancelled"] = [{"unit": placement.unit.name, "from_month": _month(placement.from_month)}
                                            for placement in cancelled]
        record("nurse.end_date_changed", actor=request.user, target=user.username, request=request,
               before={"end_date": before.isoformat() if before else None}, after=after)
        if end is not None:
            preceptors.log_ended(preceptors.end_on_leaving(nurse, probation.months_on(end.replace(day=1), 1)), request)
        is_staff_nurse = user.role == Role.STAFF_NURSE
        if is_staff_nurse and end is not None and end <= today:
            deactivate(request, user.pk)
        else:
            in_the_way = membership.archived_unit_in_the_way(user)
            if in_the_way:
                refuse((("end_date",), f"{in_the_way.name} is archived. Transfer {user.full_name} first."))
            if is_staff_nurse:
                reactivate(request, user.pk)
    return _row_as_saved(nurse_id)


def _closed_for_good(nurse: NurseProfile, was: dt.date | None, now: dt.date | None, *, end: bool) -> str | None:
    """A closed month is a fact: a start or end date that would change which of its days counted is refused once the
    month after it has closed too. While it can still be closed again, the change is allowed, and the month's manager sees on the
    Roster page that its facts changed since the close, which closing it again takes in, as for leave recorded late. A start date
    reaching back before the first placement moves that placement into the months before (`membership.reach_back_to_start_date`):
    those may not be fixed months of its unit either."""
    moved = making.days_moved(was, now, end=end)
    if moved is None:
        return None
    fixed = making.fixed_month_between(nurse, *moved)
    first = nurse.placements.select_related("unit").first()
    if fixed is None and not end and first is not None and now.replace(day=1) < first.from_month:
        fixed = making.fixed_month_between(nurse, now, first.from_month - dt.timedelta(days=1), unit=first.unit)
    if fixed is None:
        return None
    return (f"{fixed:%B %Y} has closed, and so has the month after it, so a date that changes which of its days {nurse.user.full_name} "
            "was present cannot be saved.")


@router.put("/{nurse_id}/details", response=CorrectedNurse, auth=cno, operation_id="correctNurseDetails")
def correct_details(request: HttpRequest, nurse_id: UUID, body: CorrectionBody) -> CorrectedNurse:
    """Nothing here changes who belongs where, now or to come, nor an account's state, so no unit's row is locked and
    an ended nurse stays ended. A closed month refuses a start date that rewrites it (`_closed_for_good`), and one that changes
    a solved one is warned of. A gender changes no hours or carry-over: it applies from the next solve on, and a closed month
    keeps what its stored input says."""
    extended = None
    with transaction.atomic():
        nurse = _locked_nurse(nurse_id)
        user = nurse.user
        full_name = user.full_name if body.full_name is None else body.full_name
        name_changes = full_name != user.full_name
        start_changes = body.start_date != nurse.start_date
        gender_changes = body.gender != nurse.gender
        ticking = body.on_probation is not None and body.on_probation != nurse.on_probation
        refusals = []
        if name_changes and user.role != Role.STAFF_NURSE:
            refusals.append((("full_name",), f"{user.username}'s name is on their unit manager account. "
                                             "The System Admin can correct it."))
        # Only when it changes: a nurse never present has an end date before their start, and their name still corrects.
        if start_changes and nurse.end_date and not membership.start_date_fits(nurse.start_date, body.start_date, nurse.end_date):
            refusals.append((("start_date",), "The start date cannot be after the end date. Change the end date first."))
        elif start_changes:
            fixed = _closed_for_good(nurse, nurse.start_date, body.start_date, end=False)
            if fixed:
                refusals.append((("start_date",), fixed))
        if ticking:
            refusals += _probation_refusals(nurse, body.on_probation, body.start_date)
        if refusals:
            refuse(*refusals)

        before, after = differing({"full_name": user.full_name, "start_date": nurse.start_date.isoformat()},
                                  {"full_name": full_name, "start_date": body.start_date.isoformat()})
        if gender_changes:
            after["gender_changed"] = True  # That it did, never what from or to: the System Admin reads the log.
        if name_changes:
            user.full_name = full_name
            user.save(update_fields=["full_name"])
        if start_changes or gender_changes:
            nurse.gender = body.gender
            nurse.start_date = body.start_date
            nurse.save(update_fields=["gender", "start_date"])
        reached_back = membership.reach_back_to_start_date(nurse) if start_changes else None
        if reached_back:
            placement, began = reached_back
            extended = FirstPlacement(unit=UnitName(id=placement.unit.pk, name=placement.unit.name),
                                   from_month=_month(placement.from_month))
            before["first_placement"] = {"unit": placement.unit.name, "from_month": _month(began)}
            after["first_placement"] = {"unit": placement.unit.name, "from_month": extended.from_month}
        if after:
            record("nurse.details_corrected", actor=request.user, target=user.username, request=request,
                   before=before, after=after)
        if ticking:
            was = probation.describe(nurse)
            if body.on_probation:
                probation.tick(nurse)
            else:
                probation.untick(nurse)
            nurse.save(update_fields=["on_probation", "probation_months", "probation_ended_in", "probation_notified_for"])
            record("nurse.probation_ticked" if body.on_probation else "nurse.probation_unticked", actor=request.user,
                   target=user.username, request=request, before=was, after=probation.describe(nurse))
            if not body.on_probation:
                from nurses.attributes_api import tell_released  # It imports this module.

                tell_released(nurse, dt.date.fromisoformat(was["last_day"]) if was["last_day"] else None)
                preceptors.log_ended(preceptors.end_with_probation(nurse), request)
    return CorrectedNurse(nurse=_row_as_saved(nurse_id), first_placement_extended=extended)


def _probation_refusals(nurse: NurseProfile, ticked: bool, start_date: dt.date) -> list:
    """Unticking is always allowed: a correction. Ticking is refused for a manager's profile, once six months from her start
    date (as corrected in the same save) have passed, and while her allowed shifts are a night shift alone."""
    user = nurse.user
    if user.role != Role.STAFF_NURSE:
        return [(("on_probation",), ONLY_STAFF_NURSES)]
    if not ticked:
        return []
    if membership.today() > probation.last_day_after(start_date, probation.TICK_WITHIN):
        return [(("on_probation",), f"{user.full_name} started on {start_date:%-d %B %Y}, more than six months ago, so she can "
                                    "no longer be put on probation.")]
    nights = probation.night_alone(membership.from_now_on(nurse))
    if nights:
        unit = nights[0].unit
        return [(("on_probation",), f"{user.full_name} may work only {unit.rules['night_shift']} in {unit.name}, its night shift, "
                                    "which a nurse on probation does not work. Her unit manager changes her allowed shifts first.")]
    return []


@router.post("/{nurse_id}/reset-password", response=NurseWithPassword, auth=SignedIn(Role.CNO, Role.UNIT_MANAGER),
             operation_id="resetNursePassword")
def reset_nurse_password(request: HttpRequest, response: HttpResponse, nurse_id: UUID) -> NurseWithPassword:
    """A staff nurse's only: a manager's account is the System Admin's, and this would let one bypass their own
    current password. A manager reaches the nurses of their unit this month, and the same 403 otherwise."""
    # The check and the reset are one transaction under the nurse's lock and then the unit's, as a transfer takes them
    # and as taking a manager off takes the unit's, so neither can land between the two and leave a password with a
    # former manager. The unit stays locked through the hash, which keeps nurses from joining it for that moment.
    with transaction.atomic():
        try:
            nurse = _locked_nurse(nurse_id)
        except Http404:
            if request.user.role == Role.CNO:
                raise Http404("There is no such nurse.") from None
            raise HttpError(403, "This is not your unit.") from None
        unit = unit_in_scope(request.user, membership.unit_id_now(nurse), for_update=True)
        if nurse.user.role != Role.STAFF_NURSE:
            refuse((("nurse",), f"The System Admin resets {nurse.user.username}'s password, as a unit manager's."))
        _, password = reset_password(request, nurse.user.pk)
        row = _row_as_saved(nurse_id, only_unit=None if request.user.role == Role.CNO else unit.pk)
    response["Cache-Control"] = "no-store"
    return NurseWithPassword(nurse=row, generated_password=password)
