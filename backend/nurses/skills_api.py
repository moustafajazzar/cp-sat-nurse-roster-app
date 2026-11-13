"""A nurse's skills: the CNO and the Deputy CNO record, renew and take them off (the Deputy on Rostering
attributes), and write nothing to the catalogue, which stays the CNO's; her unit manager (and the manager of a unit she is due to join)
and the Deputy CNO read them on the rows they already read (`nurses.api.NurseRow`, `nurses.attributes_api.NurseAttributesRow`);
she reads her own here, and the expiries of every nurse the reader may see are read here too (`nurses.expiries`). The System
Admin reads the audit log, so an entry says only whose skills changed and by whom: never which skill or date, since a skill's
lapse says something about a person.

The skills are on the profile, not a placement, so they follow her across a transfer and stay when she is deactivated or ends.
Every write takes the catalogue's lock before the nurse's, as `hospital.catalogue_api.update_skill` and a transfer take them,
so a record and a change of the skill's `expires` or `active` cannot pass each other."""
from __future__ import annotations

import datetime as dt
from uuid import UUID

from django.db import transaction
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Field, Router, Schema, Status
from ninja.errors import HttpError

from accounts.authz import SignedIn
from accounts.models import Role
from audit.log import record
from hospital.api import refuse
from hospital.catalogue_api import IsoDate
from hospital.models import HospitalSettings, Skill, SkillKind
from nurses import membership
from nurses.api import Body, UnitName
from nurses.expiries import HORIZON, ExpiryState, expiries
from nurses.models import NurseProfile, NurseSkill
from nurses.skills import Identifier, NurseSkillRow, as_rows, carried, end_record, problems_with, record_anew, skills_of
from units.scope import HOSPITAL_WIDE, units_in_scope

router = Router(tags=["nurses"])
# Who records what a nurse holds. Each entry names the actor, so the log shows whose hand it was.
recorders = SignedIn(Role.CNO, Role.DEPUTY_CNO)


IDENTIFIER = "The number or code its issuing body gave it, trimmed; required on a licence, empty otherwise when there is none."
ISSUE_DATE = "When it was issued: not after the expiry or today. Read by nothing; the expiry is what a roster reads."


class NewNurseSkillBody(Body):
    skill_id: UUID
    expiry: IsoDate | None = Field(description="Required when the skill expires; empty when it does not.")
    identifier: Identifier | None = Field(description=IDENTIFIER)
    issue_date: IsoDate | None = Field(description=ISSUE_DATE)


class NurseSkillChangeBody(Body):
    """The one body here where a field left out means something: a renewal carries forward the identifier and the issue date
    it does not name, from the record it ends, so a new expiry alone keeps the licence's number. Null clears one."""
    expiry: IsoDate | None
    identifier: Identifier | None = Field(None, description=IDENTIFIER + " Left out, the record's is carried forward.")
    issue_date: IsoDate | None = Field(None, description=ISSUE_DATE + " Left out, the record's is carried forward.")


def _locked_nurse(nurse_id: UUID) -> NurseProfile:
    return get_object_or_404(NurseProfile.objects.select_for_update(of=("self", "user"), no_key=True).select_related("user"),
                             pk=nurse_id)


def _refuse_expiry(skill: Skill, expiry: dt.date | None) -> None:
    if skill.expires and expiry is None:
        refuse((("expiry",), f"{skill.name} expires. Enter the last day it counts."))
    if not skill.expires and expiry is not None:
        refuse((("expiry",), f"{skill.name} does not expire, so it takes no expiry."))


def _refuse_problems(nurse: NurseProfile, skill: Skill, expiry: dt.date | None, identifier: str | None,
                     issue_date: dt.date | None) -> None:
    problems = problems_with(nurse, skill, expiry, identifier, issue_date)
    if problems:
        refuse(*(((field,), words) for field, words in problems))


def _held(nurse: NurseProfile, skill_id: UUID) -> NurseSkill:
    """A 409, as for leave changed since the page opened: acting on it could undo someone's correction."""
    row = NurseSkill.objects.current().filter(nurse=nurse, skill_id=skill_id).select_related("skill").first()
    if row is None:
        raise HttpError(409, f"{nurse.user.full_name} does not hold this skill now. Reload the page to see their skills as they are.")
    return row


def _changed(request: HttpRequest, nurse: NurseProfile) -> None:
    record("nurse.skills_changed", actor=request.user, target=nurse.user.username, request=request)


@router.post("/{nurse_id}/skills", response={201: list[NurseSkillRow]}, auth=recorders, operation_id="recordNurseSkill")
def record_skill(request: HttpRequest, nurse_id: UUID, body: NewNurseSkillBody) -> Status:
    """Any nurse's, a manager with a nurse profile's too, deactivated or not: a returning nurse has her record. Answers with all
    her skills as they now are."""
    with transaction.atomic():
        HospitalSettings.load(for_update=True)
        nurse = _locked_nurse(nurse_id)
        skill = Skill.objects.filter(pk=body.skill_id).first()
        if skill is None:
            refuse((("skill_id",), "Choose a skill from the list."))
        if not skill.active:
            refuse((("skill_id",), f"{skill.name} is retired, so it can no longer be recorded."))
        if NurseSkill.objects.current().filter(nurse=nurse, skill=skill).exists():
            refuse((("skill_id",), f"{nurse.user.full_name} already holds {skill.name}. Change or take off the one recorded."))
        _refuse_expiry(skill, body.expiry)
        _refuse_problems(nurse, skill, body.expiry, body.identifier, body.issue_date)
        record_anew(nurse, skill, request.user, expiry=body.expiry, identifier=body.identifier, issue_date=body.issue_date)
        _changed(request, nurse)
        return Status(201, skills_of(nurse, details=True))


@router.put("/{nurse_id}/skills/{skill_id}", response=list[NurseSkillRow], auth=recorders, operation_id="renewNurseSkill")
def renew_skill(request: HttpRequest, nurse_id: UUID, skill_id: UUID, body: NurseSkillChangeBody) -> list[NurseSkillRow]:
    """A renewal, or a correction of what it says: the row ends and another begins, as leave's does, with what the body names
    and, carried from the row it ends, what it does not. Saving it as it reads (for a kind that no longer expires, with no
    date, whatever is kept) changes nothing."""
    with transaction.atomic():
        HospitalSettings.load(for_update=True)
        nurse = _locked_nurse(nurse_id)
        row = _held(nurse, skill_id)
        skill = row.skill
        named, (identifier, issue_date) = body.model_fields_set, carried(row)
        identifier = body.identifier if "identifier" in named else identifier
        issue_date = body.issue_date if "issue_date" in named else issue_date
        if (body.expiry, identifier, issue_date) == (row.expiry if skill.expires else None, row.identifier, row.issue_date):
            return skills_of(nurse, details=True)
        if not skill.active:
            refuse((("expiry",), f"{skill.name} is retired, so it can no longer be renewed."))
        _refuse_expiry(skill, body.expiry)
        _refuse_problems(nurse, skill, body.expiry, identifier, issue_date)
        # A kind that no longer expires keeps the date recorded while it did, as the row it ends kept it.
        kept = body.expiry if skill.expires else row.expiry
        record_anew(nurse, skill, request.user, expiry=kept, identifier=identifier, issue_date=issue_date, ending=row)
        _changed(request, nurse)
        return skills_of(nurse, details=True)


@router.post("/{nurse_id}/skills/{skill_id}/remove", response=list[NurseSkillRow], auth=recorders, operation_id="removeNurseSkill")
def remove_skill(request: HttpRequest, nurse_id: UUID, skill_id: UUID) -> list[NurseSkillRow]:
    """A retired skill's too: a record made in error comes off. The row ends, and stays as the record of what was there."""
    with transaction.atomic():
        HospitalSettings.load(for_update=True)
        nurse = _locked_nurse(nurse_id)
        end_record(_held(nurse, skill_id), request.user)
        _changed(request, nurse)
        return skills_of(nurse, details=True)


@router.get("/my-skills", response=list[NurseSkillRow], auth=SignedIn(Role.STAFF_NURSE, Role.UNIT_MANAGER),
            operation_id="getMySkills")
def my_skills(request: HttpRequest) -> list[NurseSkillRow]:
    """Takes no nurse, so it cannot answer for a colleague. A manager is here because a manager with a nurse profile is a nurse
    too; one with no profile simply has none."""
    return as_rows(NurseSkill.objects.current().filter(nurse__user=request.user).select_related("skill"), details=True)



class ExpiryRow(Schema):
    nurse_id: UUID
    hospital_id: str
    full_name: str
    unit: UnitName = Field(description="Her unit today; for a unit manager, his own, which she may be due to join.")
    skill: str
    kind: SkillKind = Field(description="The catalogue's. The list carries no identifier and no issue date.")
    state: ExpiryState = Field(description="`expired`: the expiry has passed. `expiring`: it is today or within `horizon_days`. "
                                           "`no_record`: the skill is required to work and she has no record of it.")
    expiry: dt.date | None = Field(description="The last day the skill counts. Null for `no_record`.")


class Expiries(Schema):
    horizon_days: int = Field(description="How far ahead an expiry is listed: fixed, the hospital's one meaning of expiring.")
    count: int = Field(description="How many rows there are: the CNO's home shows it.")
    rows: list[ExpiryRow]


@router.get("/expiries", response=Expiries, auth=SignedIn(Role.CNO, Role.DEPUTY_CNO, Role.UNIT_MANAGER),
            operation_id="listExpiries")
def list_expiries(request: HttpRequest) -> Expiries:
    """Every unit's for the CNO and the Deputy CNO; for a manager, his unit's nurses and those due to join it, as his Nurses
    page lists them, naming no other unit."""
    if request.user.role in HOSPITAL_WIDE:
        found = expiries(NurseProfile.objects.all(), lambda nurse: membership.from_now_on(nurse)[0].unit)
    else:
        unit = units_in_scope(request.user).first()
        found = [] if unit is None else expiries(membership.on_the_list_of(unit), lambda nurse: unit)
    rows = [ExpiryRow(nurse_id=row.nurse.pk, hospital_id=row.nurse.user.username, full_name=row.nurse.user.full_name,
                      unit=UnitName(id=row.unit.pk, name=row.unit.name), skill=row.skill, kind=row.kind, state=row.state,
                      expiry=row.expiry)
            for row in found]
    return Expiries(horizon_days=HORIZON.days, count=len(rows), rows=rows)
