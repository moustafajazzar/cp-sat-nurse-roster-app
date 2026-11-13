"""How a nurse's current skills read, wherever someone may read them, and the rules a record or a renewal of one keeps,
wherever it is made: see `nurses.skills_api` and the skills import (`nurses.skills_importing`)."""
from __future__ import annotations

import datetime as dt
from typing import Annotated
from uuid import UUID

from django.db.models import Prefetch
from django.utils import timezone
from ninja import Field, Schema
from pydantic import AfterValidator, StringConstraints

from hospital.models import Skill, SkillKind
from nurses import membership
from nurses.models import NurseProfile, NurseSkill


def _no_nul(value: str) -> str:
    # PostgreSQL cannot hold one, as `hospital.catalogue_api.no_nul` says; that module cannot be imported from here.
    if "\x00" in value:
        raise ValueError("cannot contain a NUL character")
    return value


# An identifier as a record keeps it, from the API's bodies and the import's cells alike: trimmed, and none when empty.
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100), AfterValidator(_no_nul),
                       AfterValidator(lambda value: value or None)]


class NurseSkillDetails(Schema):
    """What a record says beyond the expiry. Read by the CNO, the Deputy CNO and the nurse herself only."""
    issuing_body: str = Field(description="The catalogue's: who issues it; empty when no one does.")
    identifier: str | None = Field(description="The number or code the issuing body gave it. Always set on a licence.")
    issue_date: dt.date | None = Field(description="When it was issued. Read by nothing: the expiry is what a roster reads.")


class NurseSkillRow(Schema):
    skill_id: UUID
    name: str
    kind: SkillKind = Field(description="The catalogue's: a skill, a certificate or a licence.")
    expires: bool = Field(description="The catalogue's: whether the kind has an expiry.")
    required_to_work: bool = Field(description="The catalogue's: from the day after its expiry, she works no shift.")
    active: bool = Field(description="False once the CNO has retired the skill. Her record of it stays.")
    expiry: dt.date | None = Field(description="The last day it counts. Null for a kind that does not expire, even when a date "
                                               "recorded while it did is kept.")
    details: NurseSkillDetails | None = Field(description="Empty for a reader who may not read them: a unit manager reading "
                                                          "a nurse of his unit reads kind, name and expiry only.")


def counted_expiry(row: NurseSkill) -> dt.date | None:
    """Her record's expiry as it counts: none for a kind that does not expire, whatever date is kept from when it did."""
    return row.expiry if row.skill.expires else None


def tracked(rows) -> list[NurseSkill]:
    """Her records of live skills. A retired skill is tracked no more: to a solve input and to the expiries it is no record,
    counting toward no need and barring nobody, as `hospital.rules.required_skills` leaves it out."""
    return [row for row in rows if row.skill.active]


def as_rows(rows, *, details: bool) -> list[NurseSkillRow]:
    """`rows`: her current `NurseSkill`s with their skills. `details`: whether the reader may read the identifier, the issue
    date and the issuing body; each caller says, so that none gives them away by default."""
    return [NurseSkillRow(skill_id=row.skill.pk, name=row.skill.name, kind=row.skill.kind, expires=row.skill.expires,
                          required_to_work=row.skill.required_to_work, active=row.skill.active,
                          expiry=counted_expiry(row),
                          details=NurseSkillDetails(issuing_body=row.skill.issuing_body, identifier=row.identifier,
                                                    issue_date=row.issue_date) if details else None)
            for row in sorted(rows, key=lambda row: (row.skill.name.lower(), row.skill.pk))]


def current_skills():
    """A prefetch for a list of nurses: `nurse.current_skills` is then each one's current rows, with their skills."""
    return Prefetch("skills", queryset=NurseSkill.objects.current().select_related("skill"), to_attr="current_skills")


def skills_of(nurse, *, details: bool) -> list[NurseSkillRow]:
    """Her current skills: from `current_skills()` when the list prefetched them, or read here."""
    rows = getattr(nurse, "current_skills", None)
    if rows is None:
        rows = NurseSkill.objects.current().filter(nurse=nurse).select_related("skill")
    return as_rows(rows, details=details)


def problems_with(nurse: NurseProfile, skill: Skill, expiry: dt.date | None, identifier: str | None,
                  issue_date: dt.date | None) -> list[tuple[str, str]]:
    """What a new row would break, beside its expiry's own rules, as (field, words): a record's and a renewal's alike, and
    the import's. The identifier is required on a licence and is one nurse's among an entry's current records, compared
    exactly; an ended record holds none, and two entries may share one. The issue date comes neither after the expiry nor
    after today on the hospital's clock. The database's partial unique constraint holds the identifier's rule too."""
    problems = []
    if identifier is None and skill.kind == Skill.Kind.LICENCE:
        problems.append(("identifier", f"{skill.name} is a licence, so it needs its identifier."))
    if identifier is not None:
        other = (NurseSkill.objects.current().filter(skill=skill, identifier=identifier).exclude(nurse=nurse)
                 .select_related("nurse__user").first())
        if other is not None:
            problems.append(("identifier", f"{other.nurse.user.full_name} already holds {skill.name} with this identifier. "
                                           "Check the number: each one is one nurse's."))
    if issue_date is not None:
        if expiry is not None and issue_date > expiry:
            problems.append(("issue_date", "The issue date cannot be after the expiry."))
        elif issue_date > membership.today():
            problems.append(("issue_date", "The issue date cannot be after today."))
    return problems


def carried(held: NurseSkill | None) -> tuple[str | None, dt.date | None]:
    """The identifier and the issue date a renewal carries forward from the record it ends, when it does not name them."""
    return (held.identifier, held.issue_date) if held is not None else (None, None)


def record_anew(nurse: NurseProfile, skill: Skill, user, *, expiry: dt.date | None, identifier: str | None = None,
                issue_date: dt.date | None = None, ending: NurseSkill | None = None) -> NurseSkill:
    """A record, or with `ending` a renewal: the old row ends rather than being rewritten (`NurseSkill`), keeping what it had."""
    if ending is not None:
        end_record(ending, user)
    return NurseSkill.objects.create(nurse=nurse, skill=skill, expiry=expiry, identifier=identifier, issue_date=issue_date,
                                     recorded_by=user)


def end_record(row: NurseSkill, user) -> None:
    """A renewal or a removal ends her record of a skill rather than rewriting or deleting it (`NurseSkill`)."""
    row.removed_at, row.removed_by = timezone.now(), user
    row.save(update_fields=["removed_at", "removed_by"])
